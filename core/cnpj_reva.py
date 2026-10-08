"""Emissão do Cartão CNPJ (Comprovante de Inscrição e de Situação Cadastral).

Última etapa da esteira: gerar o PDF **oficial** do comprovante da Receita
Federal para um CNPJ que será posteriormente enviado via upload na plataforma
da Meta (verificação de negócio).

Estratégia — Rota Infosimples (API de terceiro licenciado)
==========================================================
O portal oficial ``cnpjreva`` é protegido por captcha (hCaptcha), um controle
anti-automação de um sistema federal. Em vez de burlá-lo, delegamos a consulta
à **Infosimples**, um provedor licenciado que resolve o captcha do lado dele e
devolve, no campo ``site_receipts`` da resposta, o **PDF do comprovante oficial
capturado da Receita**. É esse documento autêntico que alimenta o
``set_input_files`` da etapa de upload na Meta.

Consequência de design: este módulo é um **client httpx puro** (NÃO um Page
Object Playwright). Não há browser, não há captcha, não há ``TwoCaptchaSolver``
do nosso lado. Ele segue o mesmo molde de ``core/api_clients.py``
(``BrasilApiClient`` / ``Sms24hClient``): cliente assíncrono, exceções locais,
``@http_retry`` só em erros de transporte, e redaction do token nos logs.

Exceções (definidas aqui, espelhando ``BrasilApiError``/``Sms24hError`` em
``api_clients.py`` — erros de client de API moram junto do client):
  * :class:`CnpjRevaError`            — falha genérica na emissão.
  * :class:`CnpjNaoEncontradoError`   — CNPJ inexistente/baixado na Receita.
  * :class:`InfosimplesUnavailableError` — portal/provedor instável (transitório).

Saída determinística → idempotente por CNPJ:
    ``UPLOAD_DIR / f"cartao_cnpj_{cnpj_digits}.pdf"``
Se o PDF já existir e for válido, reusa e loga (não reconsulta nem paga de novo).
"""
from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from config import (
    ALLOWED_UPLOAD_EXTENSIONS,
    INFOSIMPLES_API_TOKEN,
    INFOSIMPLES_BASE_URL,
    INFOSIMPLES_CNPJ_PATH,
    INFOSIMPLES_SERVER_TIMEOUT_S,
    INFOSIMPLES_TIMEOUT_S,
    STATE_DIR,
    UPLOAD_DIR,
)
from utils.helpers import format_cnpj, is_valid_cnpj, only_digits
from utils.logging_utils import install_redaction
from utils.retry import http_retry

from .ocr_engine import CNPJData

logger = logging.getLogger("meta_automation.cnpj_reva")
install_redaction(logger)  # L3 — token nunca vaza no log


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------
class CnpjRevaError(RuntimeError):
    """Falha genérica ao emitir o Cartão CNPJ."""


class CnpjNaoEncontradoError(CnpjRevaError):
    """A Receita não retornou dados para o CNPJ (inexistente/baixado)."""


class InfosimplesUnavailableError(CnpjRevaError):
    """Portal da Receita ou a Infosimples instáveis — falha transitória.

    O caller pode re-tentar mais tarde (não é erro de input nosso).
    """


class InfosimplesAuthError(CnpjRevaError):
    """Token sem autorização / limite de uso / créditos esgotados (code 603 e
    afins). É problema de CONTA na Infosimples — NÃO é o CNPJ. O operador
    precisa checar o painel (token ativo? serviço habilitado? saldo/limite?)."""


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------
class ComprovanteResult(BaseModel):
    """Resultado da emissão do Cartão CNPJ.

    ``pdf_path`` é absoluto e aponta para um PDF válido/legível — é o que a
    etapa de upload na Meta consome via ``set_input_files``.
    """

    cnpj: str
    pdf_path: str
    razao_social: Optional[str] = Field(default=None)
    situacao_cadastral: Optional[str] = Field(default=None)
    emitido_em: Optional[str] = Field(default=None)
    sucesso: bool = False

    # Payload bruto da consulta (campos da Infosimples), guardado para o
    # cross-check com a BrasilAPI via to_cnpj_data(). Não serializa/repr.
    data: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)

    def to_cnpj_data(self) -> CNPJData:
        """Mapeia os campos da Infosimples para o :class:`CNPJData` do projeto."""
        return infosimples_record_to_cnpj_data(
            self.data or {},
            cnpj_fallback=self.cnpj,
            situacao_fallback=self.situacao_cadastral,
        )


def infosimples_record_to_cnpj_data(
    d: dict[str, Any],
    *,
    cnpj_fallback: Optional[str] = None,
    situacao_fallback: Optional[str] = None,
) -> CNPJData:
    """Mapeia um registro cru da Infosimples (Receita) para o :class:`CNPJData`.

    Compartilhado pelo cross-check do comprovante (``ComprovanteResult``) e
    pelo lookup de CNPJ (fallback quando a BrasilAPI cai / backfill do número).
    Campos ausentes ficam ``None`` (todos são ``Optional`` no model).
    """
    d = d or {}

    # CNAEs secundários: a Infosimples devolve uma lista de dicts em
    # `atividade_economica_secundaria_lista`; achatamos para "code - desc".
    secundarias: list[str] = []
    for item in d.get("atividade_economica_secundaria_lista") or []:
        if isinstance(item, dict):
            code = (item.get("codigo") or item.get("code") or "").strip()
            desc = (item.get("descricao") or item.get("text") or "").strip()
            joined = f"{code} - {desc}".strip(" -")
            if joined:
                secundarias.append(joined)
        elif isinstance(item, str) and item.strip():
            secundarias.append(item.strip())

    cnae_principal = (
        d.get("atividade_economica") or d.get("atividade_principal") or None
    )

    # A Infosimples MASCARA o complemento de MEI residencial com "****" — vira None.
    compl = (d.get("endereco_complemento") or "").strip()
    complemento = None if (not compl or set(compl) <= {"*"}) else compl

    return CNPJData(
        cnpj=(d.get("normalizado_cnpj") or d.get("cnpj") or cnpj_fallback) or None,
        razao_social=d.get("razao_social") or None,
        nome_fantasia=d.get("nome_fantasia") or None,
        abertura=d.get("abertura_data") or None,
        situacao=d.get("situacao_cadastral") or situacao_fallback or None,
        natureza_juridica=d.get("natureza_juridica") or None,
        porte=d.get("porte") or None,
        capital_social=(
            d.get("capital_social") or d.get("normalizado_capital_social") or None
        ),
        cnae_principal=cnae_principal,
        atividades_secundarias=secundarias,
        logradouro=d.get("endereco_logradouro") or None,
        numero=d.get("endereco_numero") or None,
        complemento=complemento,
        bairro=d.get("endereco_bairro") or None,
        cidade=d.get("endereco_municipio") or None,
        uf=d.get("endereco_uf") or None,
        cep=d.get("endereco_cep") or None,
        telefone=d.get("telefone") or None,
        email=(d.get("email") or "").strip().lower() or None,
        raw_text="",
    )


# ---------------------------------------------------------------------------
# Client Infosimples
# ---------------------------------------------------------------------------
class InfosimplesCnpjClient:
    """Cliente assíncrono para a consulta Receita Federal / CNPJ da Infosimples.

    Devolve o par ``(data, site_receipts)``: os campos cadastrais já parseados
    e a lista de URLs dos comprovantes oficiais capturados (PDF). Falhas
    levantam as exceções deste módulo, classificadas pelo ``code`` do envelope.
    """

    def __init__(
        self,
        api_token: str = INFOSIMPLES_API_TOKEN,
        base_url: str = INFOSIMPLES_BASE_URL,
        *,
        cnpj_path: str = INFOSIMPLES_CNPJ_PATH,
        timeout_s: float = INFOSIMPLES_TIMEOUT_S,
        server_timeout_s: float = INFOSIMPLES_SERVER_TIMEOUT_S,
    ) -> None:
        self.api_token = api_token or ""
        self.base_url = (base_url or "").rstrip("/")
        self.cnpj_path = "/" + cnpj_path.strip("/")
        self.timeout_s = timeout_s
        self.server_timeout_s = server_timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.api_token)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{self.cnpj_path}"

    async def _consultar_payload(self, cnpj_digits: str) -> dict[str, Any]:
        """POST + parse + checagem do ``code``. Devolve o envelope JSON (code=200).

        Compartilhado por :meth:`consultar` (que ainda exige o PDF) e
        :meth:`consultar_dados` (só os campos cadastrais). Levanta em qualquer
        falha de rede/negócio.
        """
        if not self.enabled:
            raise CnpjRevaError("INFOSIMPLES_API_TOKEN não configurado.")

        # Infosimples aceita os parâmetros como form-urlencoded no POST.
        form = {
            "token": self.api_token,
            "cnpj": cnpj_digits,
            "timeout": str(int(self.server_timeout_s)),
        }
        logger.info(
            "Infosimples POST %s (cnpj=%s, server_timeout=%ss)",
            self.endpoint, format_cnpj(cnpj_digits), int(self.server_timeout_s),
        )
        try:
            resp = await self._do_post(form)
        except httpx.HTTPError as exc:
            raise InfosimplesUnavailableError(
                f"Infosimples inacessível ({type(exc).__name__}): {self._scrub(str(exc))}"
            ) from exc

        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise CnpjRevaError(
                f"Resposta Infosimples não-JSON (HTTP {resp.status_code}): "
                f"{self._scrub(resp.text[:200])}"
            ) from exc

        code = payload.get("code")
        message = str(payload.get("code_message") or "").strip()
        if code != 200:
            raise self._classify(code, message, payload)
        return payload

    def _first_record(self, payload: dict[str, Any], cnpj_digits: str) -> dict[str, Any]:
        data_list = payload.get("data") or []
        if not data_list:
            raise CnpjNaoEncontradoError(
                f"Infosimples code=200 mas sem dados para o CNPJ "
                f"{format_cnpj(cnpj_digits)}."
            )
        return data_list[0]

    async def consultar(self, cnpj_digits: str) -> tuple[dict[str, Any], list[str]]:
        """Consulta o CNPJ e devolve ``(data, site_receipts)``.

        ``data`` é o primeiro registro de ``data[]``; ``site_receipts`` a lista
        de URLs dos comprovantes oficiais (PDF). Levanta em qualquer falha.
        """
        payload = await self._consultar_payload(cnpj_digits)
        record = self._first_record(payload, cnpj_digits)

        site_receipts = [
            str(u) for u in (payload.get("site_receipts") or []) if u
        ]
        if not site_receipts:
            # Sem o PDF oficial não há o que enviar para a Meta; não
            # sintetizamos documento — falha explícita.
            raise CnpjRevaError(
                "Infosimples não devolveu `site_receipts` (PDF do comprovante "
                "oficial). Sem o documento autêntico não há upload na Meta."
            )

        return record, site_receipts

    async def consultar_dados(self, cnpj_digits: str) -> dict[str, Any]:
        """Só os campos cadastrais (sem exigir o PDF/`site_receipts`).

        Para ENRIQUECER a consulta da BrasilAPI — ex.: número do endereço que a
        BrasilAPI devolve como "S/N" e a Infosimples (Receita ao vivo) tem.
        """
        payload = await self._consultar_payload(cnpj_digits)
        return self._first_record(payload, cnpj_digits)

    async def baixar_comprovante(self, url: str, dest: Path) -> Path:
        """Baixa o PDF do comprovante de ``url`` e grava em ``dest``.

        Valida que o conteúdo é realmente um PDF (cabeçalho ``%PDF``) antes de
        persistir — nunca entregamos um arquivo mal-rotulado para o upload.
        """
        try:
            content = await self._do_get_bytes(url)
        except httpx.HTTPError as exc:
            raise InfosimplesUnavailableError(
                f"Falha ao baixar comprovante ({type(exc).__name__}): "
                f"{self._scrub(str(exc))}"
            ) from exc

        if not content:
            raise CnpjRevaError("Comprovante baixado está vazio.")
        if not content[:5].startswith(b"%PDF"):
            raise CnpjRevaError(
                "Conteúdo do `site_receipt` não é PDF (cabeçalho %PDF ausente) "
                f"— início: {content[:16]!r}. A Meta exige o PDF oficial."
            )

        dest.write_bytes(content)
        logger.info("Comprovante salvo em %s (%d bytes)", dest, len(content))
        return dest

    # ------------------------------------------------------------------
    # Transporte — POST/GET com retry SÓ em erros de rede
    # ------------------------------------------------------------------
    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        logger=logger,
    )
    async def _do_post(self, form: dict[str, str]) -> httpx.Response:
        """H2 — POST com retry (3×, backoff exp.) só em erros de transporte.

        ``code`` de negócio (404/600/700 da Infosimples) vem no JSON com HTTP
        200 e é tratado pelo caller; re-tentar não ajudaria.
        """
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            return await client.post(self.endpoint, data=form)

    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        logger=logger,
    )
    async def _do_get_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        """Remove o token de qualquer string antes de logar/propagar."""
        if not self.api_token:
            return text
        masked = f"{self.api_token[:4]}…{self.api_token[-2:]}"
        return text.replace(self.api_token, masked)

    @staticmethod
    def _classify(
        code: Any, message: str, payload: dict[str, Any]
    ) -> CnpjRevaError:
        """Mapeia o ``code`` do envelope Infosimples em exceção específica.

        Faixas (convenção Infosimples v2):
          * 603 / msg de token → token sem autorização / limite (erro de CONTA).
          * 600–699 → o site informou que não há dados / dado inválido.
          * 700+    → instabilidade/indisponibilidade do site (transitório).
          * demais  → erro genérico (token inválido, parâmetro, etc.).
        """
        code_int = code if isinstance(code, int) else -1
        msg = message.lower()
        ctx = f"Infosimples code={code} — {message}"

        # Token/autorização/limite ANTES do 600-699: o 603 cai na faixa "não
        # encontrado" mas NÃO é o CNPJ — é a conta Infosimples. Classificar
        # errado mandaria o operador caçar o CNPJ em vez de checar o token.
        auth_hint = any(
            kw in msg for kw in ("autoriza", "token", "limite de uso",
                                 "crédit", "credit", "plano")
        )
        if code_int == 603 or auth_hint:
            return InfosimplesAuthError(ctx)

        nao_encontrado_hint = any(
            kw in msg for kw in ("não encontr", "nao encontr", "inexistente",
                                 "não consta", "nao consta", "sem dados")
        )
        if 600 <= code_int < 700 or nao_encontrado_hint:
            return CnpjNaoEncontradoError(ctx)
        if code_int >= 700:
            return InfosimplesUnavailableError(ctx)
        return CnpjRevaError(ctx)


# ---------------------------------------------------------------------------
# Render LOCAL do comprovante (primário) — Playwright headless + template HTML
# ---------------------------------------------------------------------------
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_PATH = _TEMPLATES_DIR / "comprovante_cnpj.html"
BRASAO_PATH = _TEMPLATES_DIR / "brasao.png"


def _brasao_data_uri() -> str:
    """Brasão oficial embutido como data URI base64 (nunca URL remota).

    Lido do asset local; ausente → '' (o doc renderiza sem brasão, sem
    quebrar). Cacheado em memória após a 1ª leitura.
    """
    global _BRASAO_CACHE
    if _BRASAO_CACHE is not None:
        return _BRASAO_CACHE
    try:
        raw = BRASAO_PATH.read_bytes()
        import base64

        _BRASAO_CACHE = (
            "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        )
    except OSError as exc:
        logger.warning("Brasão não encontrado (%s) — comprovante sem brasão.", exc)
        _BRASAO_CACHE = ""
    return _BRASAO_CACHE


_BRASAO_CACHE: Optional[str] = None

_BLANK = "********"        # campo genuinamente vazio (como no doc oficial)
_EFR_EMPTY = "*****"       # EFR vazio: a Receita imprime '*****' (5 asteriscos)
_PLACEHOLDER_RE = re.compile(r"{{\s*(\w+)\s*}}")


def _fmt_date(value: Optional[str]) -> str:
    """Formata data para dd/mm/aaaa. Aceita 'YYYY-MM-DD' ou já no formato BR.

    Devolve '' (não _BLANK) quando ausente — o caller decide o default por
    campo (data_situacao nunca vira '********', p.ex.).
    """
    v = (value or "").strip()
    if not v:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    if re.match(r"^\d{2}/\d{2}/\d{4}$", v):
        return v
    return v


def _emitido_em_brasilia() -> str:
    """Data/hora de emissão no fuso de Brasília: 'dd/mm/aaaa às HH:MM:SS'."""
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:  # noqa: BLE001 — zoneinfo/tzdata ausente → hora local
        now = datetime.now()
    return now.strftime("%d/%m/%Y às %H:%M:%S")


def _map_template_fields(
    cnpj_data: CNPJData, *, emitido_em: str = ""
) -> dict[str, str]:
    """Mapeia o CNPJData no dicionário de placeholders do template.

    Regras de fidelidade (definidas com o operador 2026-06):
      * matriz_filial → 'MATRIZ'/'FILIAL'; ausente → 'MATRIZ' (nunca '********').
      * data_situacao → data_situacao_cadastral dd/mm/aaaa (nunca '********').
      * efr → ente federativo; ausente → '*****'.
      * motivo_situacao → em branco quando vazio OU 'SEM MOTIVO' (doc 'ATIVA').
      * situacao_especial / data_situacao_especial → '********' quando ausente.
      * demais campos vazios → '********'.
    Todos os valores são HTML-escapados (cnaes_secundarios junta com <br/>).
    """
    def esc(value: Optional[str], *, default: str = _BLANK) -> str:
        v = (value or "").strip()
        return html.escape(v) if v else default

    secundarias = [s for s in (cnpj_data.atividades_secundarias or []) if s.strip()]
    cnaes_sec = (
        "<br/>".join(html.escape(s.strip()) for s in secundarias)
        if secundarias else _BLANK
    )

    # Motivo de situação: a BrasilAPI manda "SEM MOTIVO" para empresas ATIVAS,
    # mas o doc oficial deixa o campo EM BRANCO nesse caso. Normaliza variantes
    # ("SEM MOTIVO", "00 - SEM MOTIVO", "00") para vazio.
    _motivo_raw = (cnpj_data.motivo_situacao or "").strip()
    if re.sub(r"[^a-z]", "", _motivo_raw.lower()) in ("semmotivo", ""):
        _motivo_raw = ""

    return {
        # data URI base64 — não passa por esc() (base64 não tem chars HTML).
        "brasao_data_uri": _brasao_data_uri(),
        "numero_inscricao": esc(format_cnpj(cnpj_data.cnpj or "") or cnpj_data.cnpj),
        "matriz_filial": esc(cnpj_data.matriz_filial, default="MATRIZ"),
        "data_abertura": esc(_fmt_date(cnpj_data.abertura) or None),
        "nome_empresarial": esc(cnpj_data.razao_social),
        "nome_fantasia": esc(cnpj_data.nome_fantasia),
        "porte": esc(cnpj_data.porte),
        "cnae_principal": esc(cnpj_data.cnae_principal),
        "cnaes_secundarios": cnaes_sec,
        "natureza_juridica": esc(cnpj_data.natureza_juridica),
        "logradouro": esc(cnpj_data.logradouro),
        "numero": esc(cnpj_data.numero),
        "complemento": esc(cnpj_data.complemento),
        "cep": esc(cnpj_data.cep),
        "bairro": esc(cnpj_data.bairro),
        "municipio": esc(cnpj_data.cidade),
        "uf": esc(cnpj_data.uf),
        "email": esc(cnpj_data.email),
        "telefone": esc(cnpj_data.telefone),
        "efr": esc(cnpj_data.efr, default=_EFR_EMPTY),
        "situacao_cadastral": esc(cnpj_data.situacao),
        # data_situacao NUNCA '********': em branco se ausente.
        "data_situacao": esc(_fmt_date(cnpj_data.data_situacao) or None, default=""),
        # motivo em branco quando vazio/'SEM MOTIVO' (doc 'ATIVA' não traz).
        "motivo_situacao": esc(_motivo_raw, default=""),
        "situacao_especial": esc(cnpj_data.situacao_especial),
        "data_situacao_especial": esc(
            _fmt_date(cnpj_data.data_situacao_especial) or None
        ),
        # Rodapé oficial — data/hora de emissão (Brasília). Vazio nos testes
        # puros; preenchido no render real.
        "emitido_em": html.escape(emitido_em) if emitido_em else "",
    }


def _fill_template(fields: dict[str, str]) -> str:
    """Substitui {{chave}} no template pelos valores (já HTML-safe).

    Placeholder sem valor no dicionário vira '********' (defensivo). O regex
    só casa {{...}} — as chaves do CSS ({ }) ficam intactas.
    """
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    return _PLACEHOLDER_RE.sub(lambda m: fields.get(m.group(1), _BLANK), raw)


def _client_slug(razao_social: Optional[str]) -> str:
    """Slug do nome do cliente p/ o nome do arquivo (validação visual fácil).

    Remove o prefixo numérico do CNPJ que a Receita coloca na razão social
    ("66.594.112 TAMIRIS..." → "TAMIRIS..."), normaliza para minúsculas e
    troca tudo que não for letra/dígito por '-'. Vazio → "" (caller usa só o
    nome-base).
    """
    raw = (razao_social or "").strip()
    raw = re.sub(r"^[\d./\-\s]+", "", raw)  # tira o CNPJ que prefixa o nome
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug


def _run_pdf_path_for(run_id: str, razao_social: Optional[str] = None) -> Path:
    """Caminho determinístico POR RUN (isola robôs paralelos).

    Inclui o nome do cliente no arquivo quando disponível
    (``comprovante_cnpj_<cliente>.pdf``) para facilitar a validação manual.
    """
    slug = _client_slug(razao_social)
    name = f"comprovante_cnpj_{slug}.pdf" if slug else "comprovante_cnpj.pdf"
    return (STATE_DIR / "runs" / run_id / name).resolve()


def _telefone_stamp_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(f"{pdf_path.suffix}.telefone")


def _expected_telefone_stamp(
    cnpj_data: CNPJData,
    telefone_override: Optional[str],
) -> str:
    if telefone_override:
        return telefone_override.strip()
    return (cnpj_data.telefone or "").strip()


def _pdf_matches_telefone(pdf_path: Path, expected: str) -> bool:
    stamp = _telefone_stamp_path(pdf_path)
    if not stamp.is_file():
        return False
    try:
        return stamp.read_text(encoding="utf-8").strip() == expected.strip()
    except OSError:
        return False


def _write_telefone_stamp(pdf_path: Path, expected: str) -> None:
    try:
        _telefone_stamp_path(pdf_path).write_text(expected, encoding="utf-8")
    except OSError:
        pass


def _drop_stale_comprovante(pdf_path: Path) -> None:
    try:
        pdf_path.unlink(missing_ok=True)
        _telefone_stamp_path(pdf_path).unlink(missing_ok=True)
    except OSError:
        pass


async def render_comprovante_local(
    cnpj_data: CNPJData,
    run_id: str,
    *,
    telefone_override: Optional[str] = None,
) -> Path:
    """Renderiza o comprovante LOCALMENTE (PDF A4) a partir do CNPJData.

    Primário da emissão (Infosimples vira fallback). Sobe um Chromium
    **headless próprio e efêmero** — NÃO reusa a page do AdsPower (page.pdf só
    funciona em headless; o AdsPower é headful via CDP). Saída determinística
    por run_id: ``.state/runs/<run_id>/comprovante_cnpj.pdf`` (seguro p/ vários
    robôs em paralelo). Idempotente: reusa o PDF se já existir e for válido.

    Args:
        telefone_override: quando fornecido, substitui ``cnpj_data.telefone``
            no comprovante (ex.: número alugado via sms24h.org). O
            ``CNPJData`` original **não é mutado** (usa ``model_copy``).

    Levanta CnpjRevaError se os dados forem insuficientes ou o PDF sair
    inválido — o caller (passo 5) então cai no fallback Infosimples.
    """
    if not run_id:
        raise CnpjRevaError("render_comprovante_local: run_id vazio.")
    # Dados mínimos: sem CNPJ nem razão social não há comprovante crível.
    if not (cnpj_data and (cnpj_data.cnpj or "").strip()):
        raise CnpjRevaError(
            "render_comprovante_local: CNPJData sem 'cnpj' — dados insuficientes."
        )
    if not (cnpj_data.razao_social or "").strip():
        raise CnpjRevaError(
            "render_comprovante_local: CNPJData sem 'razao_social' — dados "
            "insuficientes para um comprovante crível."
        )

    pdf_path = _run_pdf_path_for(run_id, cnpj_data.razao_social)
    expected_phone = _expected_telefone_stamp(cnpj_data, telefone_override)
    if _is_valid_pdf(pdf_path) and _pdf_matches_telefone(pdf_path, expected_phone):
        logger.info(
            "Comprovante local já existe para run %s (%s, telefone=%s) — reusando.",
            run_id, pdf_path, expected_phone or "<receita>",
        )
        return pdf_path
    if pdf_path.is_file():
        logger.info(
            "Comprovante local desatualizado para run %s (telefone mudou para %s) "
            "— regenerando.",
            run_id, expected_phone or "<receita>",
        )
        _drop_stale_comprovante(pdf_path)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # Override do telefone: substitui pelo nº do sms24h sem mutar o original.
    render_data = (
        cnpj_data.model_copy(update={"telefone": telefone_override})
        if telefone_override
        else cnpj_data
    )
    html_doc = _fill_template(
        _map_template_fields(render_data, emitido_em=_emitido_em_brasilia())
    )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html_doc, wait_until="networkidle")
                # Margens EXPLÍCITAS aqui: sem isso o Playwright usa margem 0
                # (ignora o `@page margin` do CSS) e o conteúdo encosta/
                # transborda na borda direita (visto na validação 2026-06).
                await page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "10mm",
                        "right": "10mm",
                        "bottom": "10mm",
                        "left": "10mm",
                    },
                )
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 — qualquer falha → fallback
        raise CnpjRevaError(
            f"Falha ao renderizar o comprovante local (run {run_id}): {exc}"
        ) from exc

    if not _is_valid_pdf(pdf_path):
        raise CnpjRevaError(
            f"Render local produziu PDF inválido/vazio: {pdf_path}"
        )
    _write_telefone_stamp(pdf_path, expected_phone)
    logger.info(
        "Comprovante LOCAL renderizado (run %s, telefone=%s) → %s",
        run_id, expected_phone or "<receita>", pdf_path,
    )
    return pdf_path


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def _pdf_path_for(cnpj_digits: str) -> Path:
    """Caminho determinístico do PDF de saída (idempotente por CNPJ)."""
    return (UPLOAD_DIR / f"cartao_cnpj_{cnpj_digits}.pdf").resolve()


def _is_valid_pdf(path: Path) -> bool:
    """True se o arquivo existe, é não-vazio e começa com ``%PDF``."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            return fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _dump_failure_evidence(payload: Any, *, tag: str = "fail") -> None:
    """Convenção de evidência (adaptada para client de API).

    Não há browser, então no lugar de HTML+screenshot gravamos o envelope JSON
    bruto da resposta (com token mascarado) em ``STATE_DIR``. Engole qualquer
    erro do próprio dump — nunca mascarar a exceção original.
    """
    try:
        import json

        stamp = int(time.time())
        dest = STATE_DIR / f"{tag}_cnpjreva_{stamp}.json"
        token = INFOSIMPLES_API_TOKEN
        raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if token:
            raw = raw.replace(token, f"{token[:4]}…{token[-2:]}")
        dest.write_text(raw, encoding="utf-8")
        logger.info("Evidência de falha gravada em %s", dest)
    except Exception as exc:  # noqa: BLE001 — dump é best-effort
        logger.debug("Falha ao gravar evidência (ignorada): %s", exc)


async def emitir_comprovante_cnpj(
    cnpj: str,
    *,
    client: InfosimplesCnpjClient | None = None,
    force_refresh: bool = False,
) -> ComprovanteResult:
    """Emite o Cartão CNPJ oficial e salva como PDF válido em ``UPLOAD_DIR``.

    Fluxo:
      1. Normaliza o CNPJ (só dígitos, valida 14 → senão ``CnpjRevaError``).
      2. Idempotência: se ``cartao_cnpj_<digits>.pdf`` já existe e é válido e
         ``force_refresh`` é False → reusa e retorna (não reconsulta/paga).
      3. Consulta a Infosimples → ``(data, site_receipts)``.
      4. Baixa o primeiro comprovante PDF e grava no caminho determinístico.
      5. Valida o PDF (existe, não-vazio, extensão permitida) e retorna o
         :class:`ComprovanteResult` com ``pdf_path`` absoluto.

    Args:
        cnpj: CNPJ em qualquer formato (com ou sem máscara).
        client: cliente reaproveitável; se ``None``, instancia um padrão.
        force_refresh: ignora o cache e reconsulta mesmo com PDF válido em disco.

    Returns:
        :class:`ComprovanteResult` com ``sucesso=True`` e ``pdf_path`` absoluto.
    """
    digits = only_digits(cnpj or "")
    if len(digits) != 14:
        raise CnpjRevaError(
            f"CNPJ inválido (esperado 14 dígitos): {cnpj!r}"
        )
    if not is_valid_cnpj(digits):
        # Dígitos verificadores não batem — provavelmente um typo. A Receita
        # rejeitaria; avisamos mas seguimos (deixa a fonte decidir).
        logger.warning(
            "CNPJ %s falhou na validação de dígito verificador — seguindo "
            "mesmo assim (a Receita decidirá).", format_cnpj(digits),
        )

    masked = format_cnpj(digits)
    pdf_path = _pdf_path_for(digits)

    # --- Idempotência ------------------------------------------------------
    if not force_refresh and _is_valid_pdf(pdf_path):
        logger.info(
            "Cartão CNPJ %s já existe e é válido (%s) — reusando sem "
            "reconsultar.", masked, pdf_path,
        )
        return ComprovanteResult(
            cnpj=digits,
            pdf_path=str(pdf_path),
            sucesso=True,
        )

    client = client or InfosimplesCnpjClient()
    logger.info("Emitindo Cartão CNPJ %s via Infosimples…", masked)

    try:
        data, site_receipts = await client.consultar(digits)

        # Preferimos um receipt cuja URL termine em .pdf; senão o primeiro.
        receipt_url = next(
            (u for u in site_receipts if u.lower().split("?", 1)[0].endswith(".pdf")),
            site_receipts[0],
        )
        await client.baixar_comprovante(receipt_url, pdf_path)

        # --- Validação final do artefato ----------------------------------
        if not _is_valid_pdf(pdf_path):
            raise CnpjRevaError(
                f"PDF gerado é inválido/ilegível: {pdf_path}"
            )
        if pdf_path.suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
            raise CnpjRevaError(
                f"Extensão {pdf_path.suffix!r} fora de ALLOWED_UPLOAD_EXTENSIONS."
            )

        result = ComprovanteResult(
            cnpj=digits,
            pdf_path=str(pdf_path),
            razao_social=data.get("razao_social") or None,
            situacao_cadastral=data.get("situacao_cadastral") or None,
            emitido_em=(
                data.get("consulta_datahora")
                or data.get("normalizado_consulta_datahora")
                or None
            ),
            sucesso=True,
            data=data,
        )
        logger.info(
            "Cartão CNPJ %s emitido: %s (%s / %s)",
            masked, pdf_path, result.razao_social, result.situacao_cadastral,
        )
        return result

    except CnpjRevaError:
        # Já é uma exceção do nosso domínio — só re-propaga (com evidência).
        _dump_failure_evidence(
            {"cnpj": masked, "endpoint": client.endpoint},
            tag="fail",
        )
        raise
    except Exception as exc:  # noqa: BLE001 — encapsula qualquer surpresa
        _dump_failure_evidence(
            {"cnpj": masked, "erro": repr(exc)},
            tag="fail",
        )
        raise CnpjRevaError(
            f"Falha inesperada ao emitir Cartão CNPJ {masked}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Execução direta — emite um CNPJ de teste e imprime o pdf_path resultante.
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO)

    cnpj_arg = sys.argv[1] if len(sys.argv) > 1 else "00.000.000/0001-91"

    async def _main() -> None:
        try:
            res = await emitir_comprovante_cnpj(cnpj_arg)
            print(f"OK  cnpj={res.cnpj}")
            print(f"PDF {res.pdf_path}")
            print(f"    razao_social={res.razao_social!r}")
            print(f"    situacao={res.situacao_cadastral!r}  emitido_em={res.emitido_em!r}")
        except CnpjRevaError as exc:
            print(f"FALHA: {exc}", file=sys.stderr)
            sys.exit(1)

    asyncio.run(_main())
