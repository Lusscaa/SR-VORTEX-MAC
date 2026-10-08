"""Clientes de APIs externas usados na esteira pré-automação.

Substituem o OCR como fonte primária dos dados do cliente:

  * :class:`BrasilApiClient` — consulta `/api/cnpj/v1/{cnpj}` na BrasilAPI
    (Receita Federal). Devolve um :class:`CNPJData` já normalizado.
  * :class:`InvertTextoEmailClient` — gera um e-mail temporário via
    InvertTexto/Uorak para preencher o campo "E-mail comercial".

Ambos são assíncronos e usam ``httpx.AsyncClient``. Falhas levantam exceções
específicas (``BrasilApiError`` / ``InvertTextoError``) que o caller decide
como tratar (fallback OCR / preenchimento manual).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import unicodedata
from typing import Any, Optional

import httpx

from config import (
    BRASILAPI_BASE_URL,
    BRASILAPI_TIMEOUT_S,
    INVERTTEXTO_API_TOKEN,
    INVERTTEXTO_BASE_URL,
    INVERTTEXTO_TIMEOUT_S,
    NUMERO_VIRTUAL_CUSTOMER_SESSION,
    NUMERO_VIRTUAL_COUNTRY_ID,
    NUMERO_VIRTUAL_PRICE_STRICT,
    NUMERO_VIRTUAL_PURCHASE_API_ID,
    NUMERO_VIRTUAL_SERVICE_ID,
    NUMERO_VIRTUAL_SERVICE_NAME,
    NUMERO_VIRTUAL_TARGET_PRICE,
    SMS24H_ALLOWED_DDDS,
    SMS24H_API_KEY,
    SMS24H_BASE_URL,
    SMS24H_COUNTRY_CODE,
    SMS24H_DDD_MAX_ATTEMPTS,
    SMS24H_OPERATOR,
    SMS24H_SERVICE_CODE,
    SMS_POLL_INTERVAL_S,
    SMS_POLL_TIMEOUT_S,
    UORAK_EMAIL_DOMAIN,
)
from utils.helpers import format_cnpj, only_digits
from utils.logging_utils import install_redaction
from utils.retry import http_retry

from .ocr_engine import CNPJData

logger = logging.getLogger("meta_automation.api_clients")
install_redaction(logger)  # L3 — Bearer/token never na saída


# ---------------------------------------------------------------------------
# BrasilAPI — CNPJ lookup
# ---------------------------------------------------------------------------
class BrasilApiError(RuntimeError):
    """Falha ao consultar o CNPJ na BrasilAPI."""


class BrasilApiClient:
    """Cliente assíncrono para o endpoint público de CNPJ da BrasilAPI."""

    def __init__(
        self,
        base_url: str = BRASILAPI_BASE_URL,
        *,
        timeout_s: float = BRASILAPI_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def fetch(self, cnpj: str) -> CNPJData:
        digits = only_digits(cnpj or "")
        if len(digits) != 14:
            raise BrasilApiError(
                f"CNPJ inválido (esperado 14 dígitos): {cnpj!r}"
            )
        url = f"{self.base_url}/{digits}"
        logger.info("BrasilAPI GET %s", url)
        resp = await self._get_with_throttle_retry(url)

        if resp.status_code == 404:
            raise BrasilApiError(f"CNPJ {format_cnpj(digits)} não encontrado.")
        if resp.status_code == 429:
            raise BrasilApiError(
                "BrasilAPI HTTP 429 (rate limit) — esgotou as retentativas. "
                "Reduza runs em paralelo ou tente de novo em instantes."
            )
        if resp.status_code != 200:
            raise BrasilApiError(
                f"BrasilAPI retornou HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise BrasilApiError(f"Resposta BrasilAPI não-JSON: {resp.text[:200]}") from exc

        data = self._to_cnpj_data(payload)
        # Receita devolve logradouro VAZIO para muitos MEIs (só bairro/cidade/
        # CEP). O Meta exige endereço no portfólio — completa via lookup de CEP.
        if not (data.logradouro or "").strip() and (data.cep or "").strip():
            await self._enrich_from_cep(data)
        return data

    async def _enrich_from_cep(self, data: CNPJData) -> None:
        """Preenche logradouro/bairro vazios a partir do CEP (best-effort).

        Não levanta: endereço incompleto não pode derrubar a consulta de
        CNPJ — o operador ainda pode editar o campo manualmente na UI.
        """
        cep_digits = only_digits(data.cep or "")
        if len(cep_digits) != 8:
            return
        # Deriva o host do base_url de CNPJ → endpoint público de CEP v2.
        host = self.base_url.split("/api/")[0]
        url = f"{host}/api/cep/v2/{cep_digits}"
        logger.info("BrasilAPI CEP fallback GET %s", url)
        try:
            resp = await self._do_get(url)
            if resp.status_code != 200:
                logger.warning(
                    "CEP fallback HTTP %s para %s — logradouro fica vazio.",
                    resp.status_code, cep_digits,
                )
                return
            cep_payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("CEP fallback falhou (%s) — logradouro fica vazio.", exc)
            return
        # Schema BrasilAPI CEP v2: street / neighborhood / city / state.
        street = (cep_payload.get("street") or "").strip()
        neighborhood = (cep_payload.get("neighborhood") or "").strip()
        if street:
            data.logradouro = street
            logger.info("Logradouro preenchido via CEP: %s", street)
        if neighborhood and not (data.bairro or "").strip():
            data.bairro = neighborhood

    async def _get_with_throttle_retry(
        self, url: str, *, max_attempts: int = 4
    ) -> httpx.Response:
        """GET que re-tenta em HTTP 429 (rate limit) com backoff + jitter.

        A BrasilAPI pública limita por IP — runs em PARALELO disparam 429 com
        frequência (visto em produção 2026-06). Honra ``Retry-After`` quando
        presente; senão usa backoff exponencial (1.5s, 3s, 6s) + jitter pra não
        sincronizar os workers. Erros de rede continuam no ``_do_get``.
        """
        delay = 1.5
        resp: Optional[httpx.Response] = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self._do_get(url)
            except httpx.HTTPError as exc:
                raise BrasilApiError(f"BrasilAPI inacessível: {exc}") from exc
            if resp.status_code != 429 or attempt == max_attempts:
                return resp
            retry_after = (resp.headers.get("Retry-After") or "").strip()
            wait = (
                float(retry_after)
                if retry_after.replace(".", "", 1).isdigit()
                else delay
            )
            wait += random.uniform(0, 0.75)  # jitter anti-thundering-herd
            logger.warning(
                "BrasilAPI 429 (tentativa %d/%d) — aguardando %.1fs e re-tentando.",
                attempt, max_attempts, wait,
            )
            await asyncio.sleep(wait)
            delay *= 2
        return resp  # type: ignore[return-value]

    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        logger=logger,
    )
    async def _do_get(self, url: str) -> httpx.Response:
        """H2 — GET com retry (3×, backoff exp.) APENAS em erros de rede.

        Status HTTP (incluindo 5xx) é tratado pelo caller — re-tentar 5xx aqui
        traria mais carga sem benefício (BrasilAPI raramente devolve 5xx).
        """
        async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False) as client:
            return await client.get(url)

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _to_cnpj_data(payload: dict[str, Any]) -> CNPJData:
        """Traduz o JSON da BrasilAPI no schema interno :class:`CNPJData`."""
        cnpj_digits = str(payload.get("cnpj") or "")
        raw_phone = payload.get("ddd_telefone_1") or payload.get("telefone") or ""
        situacao_raw = (
            payload.get("descricao_situacao_cadastral")
            or payload.get("situacao_cadastral")
            or ""
        )
        if isinstance(situacao_raw, str) and situacao_raw:
            situacao = situacao_raw.strip().lower().capitalize()
        else:
            situacao = None

        cnae_principal_code = payload.get("cnae_fiscal") or ""
        cnae_principal_desc = payload.get("cnae_fiscal_descricao") or ""
        cnae_principal = (
            f"{cnae_principal_code} - {cnae_principal_desc}".strip(" -")
            if cnae_principal_code or cnae_principal_desc
            else None
        )

        atividades_secundarias = []
        for item in payload.get("cnaes_secundarios") or []:
            if not isinstance(item, dict):
                continue
            code = item.get("codigo") or ""
            desc = item.get("descricao") or ""
            if code or desc:
                atividades_secundarias.append(f"{code} - {desc}".strip(" -"))

        natureza = payload.get("natureza_juridica") or None
        porte = payload.get("porte") or None

        # --- Campos do Comprovante de Inscrição (Cartão CNPJ) --------------
        # matriz_filial: a Receita exibe "MATRIZ"/"FILIAL"; a BrasilAPI traz
        # descricao_identificador_matriz_filial. Fallback pelo código (1=MATRIZ).
        matriz_filial = payload.get("descricao_identificador_matriz_filial") or None
        if not matriz_filial:
            ident = payload.get("identificador_matriz_filial")
            if ident in (1, "1"):
                matriz_filial = "MATRIZ"
            elif ident in (2, "2"):
                matriz_filial = "FILIAL"
        motivo_situacao = (
            payload.get("descricao_motivo_situacao_cadastral") or None
        )
        efr = payload.get("ente_federativo_responsavel") or None
        situacao_especial = payload.get("situacao_especial") or None
        data_situacao_especial = payload.get("data_situacao_especial") or None
        data_situacao = payload.get("data_situacao_cadastral") or None

        mei_flag = payload.get("opcao_pelo_mei")
        if mei_flag is True:
            mei = "Sim"
        elif mei_flag is False:
            mei = "Não"
        else:
            mei = None

        capital_social = payload.get("capital_social")
        if isinstance(capital_social, (int, float)):
            capital_str: Optional[str] = f"R$ {capital_social:,.2f}".replace(
                ",", "X"
            ).replace(".", ",").replace("X", ".")
        elif isinstance(capital_social, str) and capital_social.strip():
            capital_str = capital_social.strip()
        else:
            capital_str = None

        return CNPJData(
            cnpj=cnpj_digits or None,
            razao_social=payload.get("razao_social") or None,
            nome_fantasia=payload.get("nome_fantasia") or None,
            abertura=payload.get("data_inicio_atividade") or None,
            situacao=situacao,
            natureza_juridica=natureza,
            porte=porte,
            mei=mei,
            capital_social=capital_str,
            cnae_principal=cnae_principal,
            atividades_secundarias=atividades_secundarias,
            matriz_filial=matriz_filial,
            data_situacao=data_situacao,
            motivo_situacao=motivo_situacao,
            efr=efr,
            situacao_especial=situacao_especial,
            data_situacao_especial=data_situacao_especial,
            logradouro=payload.get("logradouro") or None,
            # Número em 3 camadas (produção 2026-06):
            #   1. campo `numero` da Receita (ex.: '65', '100', 'SN');
            #   2. extração do logradouro — quando as letras acabam, o próximo
            #      valor numérico é o número (regra do operador). Pega só casos
            #      em que o número vem embutido no logradouro;
            #   3. "S/N" (convenção BR) — MEIs vêm SEM número em qualquer campo.
            numero=(
                (payload.get("numero") or "").strip()
                or _numero_from_logradouro(payload.get("logradouro"))
                or "S/N"
            ),
            complemento=payload.get("complemento") or None,
            bairro=payload.get("bairro") or None,
            cidade=payload.get("municipio") or None,
            uf=payload.get("uf") or None,
            cep=_format_cep(payload.get("cep")),
            telefone=_format_phone(raw_phone),
            email=(payload.get("email") or "").strip().lower() or None,
            raw_text="",
        )


def _numero_from_logradouro(logradouro: Any) -> Optional[str]:
    """Extrai o número do endereço quando a Receita não traz o campo `numero`.

    Regra do operador: o número do imóvel é o primeiro valor numérico que
    aparece DEPOIS que as letras do logradouro acabam. Assim "REPUBLICA DO
    CHILE 65" → "65" e "RUA 7 DE SETEMBRO 100" → "100" (o '7' interno é
    ignorado porque ainda há letras depois dele). Logradouros sem número no
    fim ("AVENIDA PAULISTA", "SAUN QUADRA 5 BLOCO B") → None.

    Best-effort: heurística para o número EMBUTIDO no logradouro. Não inventa
    número — quando o logradouro não tem dígito após a última letra, devolve
    None e o caller cai no "S/N".
    """
    if not logradouro:
        return None
    s = str(logradouro).strip()
    last_alpha = -1
    for i, ch in enumerate(s):
        if ch.isalpha():  # isalpha() Unicode cobre acentos (Ç, Ã, ...)
            last_alpha = i
    if last_alpha == -1:
        return None
    match = re.search(r"\d+", s[last_alpha + 1 :])
    return match.group(0) if match else None


def _format_cep(value: Any) -> Optional[str]:
    if not value:
        return None
    digits = only_digits(str(value))
    if len(digits) != 8:
        return str(value)
    return f"{digits[:5]}-{digits[5:]}"


def _format_phone(value: Any) -> Optional[str]:
    if not value:
        return None
    digits = only_digits(str(value))
    if len(digits) >= 11:
        return f"({digits[-11:-9]}) {digits[-9:-4]}-{digits[-4:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return digits or None


# ---------------------------------------------------------------------------
# Helpers para sócios (extrai primeiro nome / sobrenome do administrador)
# ---------------------------------------------------------------------------
def split_admin_name(raw_name: str) -> tuple[str, str]:
    """Quebra "João da Silva" em ("João", "da Silva"). Tolera string vazia."""
    name = (raw_name or "").strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def slugify_for_worker(value: str) -> str:
    """Sanitiza nome para usar como subdomínio do Worker (a-z0-9, no dashes).

    Ex.: "Rodolfo Gomes Alay Esteves" → "rodolfogomesalayesteves".
    Worker subdomains da Cloudflare aceitam ``[a-z0-9-]`` (sem hífen no
    início/fim), mas o padrão visual do cliente usa só letras+dígitos coladas.
    """
    norm = unicodedata.normalize("NFKD", value or "")
    ascii_only = norm.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", ascii_only)


# ---------------------------------------------------------------------------
# InvertTexto / Uorak — e-mail temporário
# ---------------------------------------------------------------------------
class InvertTextoError(RuntimeError):
    """Falha ao gerar e-mail temporário via InvertTexto."""


def generate_uorak_email(admin_full: str, domain: str = UORAK_EMAIL_DOMAIN) -> str:
    """Gera e-mail local no domínio Uorak com sufixo numérico aleatório.

    Formato: ``slug(admin_full) + 4 dígitos aleatórios + "@" + domain``.
    Exemplo: joaosilva4392@uorak.com. O sufixo garante unicidade entre runs
    do mesmo titular (pedido do operador 2026-06-21). Sem chamada de rede.
    Quando o admin não tem nome utilizável, devolve string vazia.
    """
    slug = slugify_for_worker(admin_full or "")[:28]  # 28+4 dígitos = 32
    if not slug:
        return ""
    suffix = str(random.randint(1000, 9999))
    return f"{slug}{suffix}@{domain}"


class InvertTextoEmailClient:
    """Cliente assíncrono para a API de e-mail temporário da InvertTexto/Uorak.

    Duas rotas de geração:

      * :meth:`generate_email_local` (default na esteira) — não bate em rede.
        Devolve ``{slug(admin_full)}@uorak.com``. Determinístico, instantâneo,
        sem dependência de API key.
      * :meth:`generate_email` — bate em ``POST {base}/mail/new`` para criar
        caixa real (devolve ``email`` + ``token`` da caixa). Útil quando o
        operador quer ler o código de verificação dentro do app.

    Em ambientes onde a URL custom (com subpath ``/v1``) resolve em
    ``[Errno 8]`` (DNS), o default base agora aponta para
    ``https://api.inverttexto.com`` (host puro). Concatenamos a rota canônica
    ``/api/v1/mail/new`` localmente, com fallback para ``/mail/new``.
    """

    # Rotas tentadas em ordem — a primeira a devolver 2xx vence.
    _NEW_PATHS = ("/api/v1/mail/new", "/mail/new")

    def __init__(
        self,
        api_token: str = INVERTTEXTO_API_TOKEN,
        base_url: str = INVERTTEXTO_BASE_URL,
        *,
        timeout_s: float = INVERTTEXTO_TIMEOUT_S,
        domain: str = UORAK_EMAIL_DOMAIN,
    ) -> None:
        self.api_token = api_token or ""
        # Normaliza URL: remove trailing slash e subpath /v1 que tende a
        # gerar Errno 8 em alguns deploys.
        normalized = (base_url or "").rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[:-3]
        self.base_url = normalized or "https://api.inverttexto.com"
        self.timeout_s = timeout_s
        self.domain = domain

    @property
    def enabled(self) -> bool:
        return bool(self.api_token)

    # ------------------------------------------------------------------
    # Geração LOCAL — sem rede, sempre disponível
    # ------------------------------------------------------------------
    def generate_email_local(self, admin_full: str) -> str:
        """Devolve ``{slug(admin_full)}@{domain}``. Sem rede, sem API key."""
        return generate_uorak_email(admin_full, self.domain)

    # ------------------------------------------------------------------
    # Geração via API (caixa real, com token para ler mensagens)
    # ------------------------------------------------------------------
    async def generate_email(
        self, *, prefix_hint: Optional[str] = None
    ) -> tuple[str, str]:
        """Cria uma caixa de e-mail temporária e devolve ``(email, mailbox_token)``.

        Tenta as rotas em :attr:`_NEW_PATHS` na ordem; a primeira 2xx vence.
        Erros de DNS/timeout são consolidados num único ``InvertTextoError``.
        """
        if not self.enabled:
            raise InvertTextoError("INVERTTEXTO_API_TOKEN não configurado.")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {}
        if prefix_hint:
            body["prefix"] = slugify_for_worker(prefix_hint)[:32] or "user"

        last_error: Optional[str] = None
        async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False) as client:
            for path in self._NEW_PATHS:
                url = f"{self.base_url}{path}"
                logger.info(
                    "InvertTexto POST %s (prefix_hint=%r)", url, prefix_hint
                )
                try:
                    resp = await self._do_post(client, url, headers, body)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("InvertTexto %s inacessível: %s", url, exc)
                    continue
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    last_error = f"Resposta não-JSON: {resp.text[:200]}"
                    continue

                email = (
                    data.get("email")
                    or data.get("address")
                    or (data.get("data") or {}).get("email")
                )
                token = (
                    data.get("token")
                    or data.get("id")
                    or (data.get("data") or {}).get("token")
                )
                if not email:
                    last_error = f"Resposta sem campo 'email': {data!r}"
                    continue
                return str(email), str(token or "")

        raise InvertTextoError(
            f"InvertTexto: todas as rotas falharam. Último erro: {last_error}"
        )

    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        logger=logger,
    )
    async def _do_post(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        """H2 — POST com retry (3×) em erros de rede.

        Status HTTP (4xx/5xx) NÃO é retry: o caller já tem fallback de path.
        """
        return await client.post(url, headers=headers, json=body)


# ---------------------------------------------------------------------------
# numero-virtual.com — gateway de recepção SMS (REST JSON)
# ---------------------------------------------------------------------------
class Sms24hError(RuntimeError):
    """Erro genérico do gateway de SMS."""


class Sms24hNoNumbersError(Sms24hError):
    """Sem números disponíveis para o serviço configurado."""


class Sms24hNoBalanceError(Sms24hError):
    """Saldo insuficiente no provedor."""


class Sms24hBadKeyError(Sms24hError):
    """Chave inválida ou ausente."""


class Sms24hBadServiceError(Sms24hError):
    """Serviço configurado não existe ou não está disponível."""


class Sms24hTimeoutError(Sms24hError):
    """Timeout aguardando o código SMS."""


class Sms24hDddError(Sms24hError):
    """Não foi possível obter um número com DDD permitido."""


_NUMERO_VIRTUAL_COUNTRY_ALIAS: dict[str, int] = {
    "24": 24,
    "br": 24,
    "brazil": 24,
    "brasil": 24,
}


def _normalize_country(code: Any) -> int:
    """Converte slugs amigáveis em ``countryId`` numérico."""
    raw = str(code or "").strip().lower()
    if not raw:
        return NUMERO_VIRTUAL_COUNTRY_ID
    if raw.isdigit():
        return int(raw)
    return _NUMERO_VIRTUAL_COUNTRY_ALIAS.get(raw, NUMERO_VIRTUAL_COUNTRY_ID)


class Sms24hClient:
    """Cliente compatível com a interface legada, agora via numero-virtual.

    O restante do projeto ainda instancia ``Sms24hClient``. Para evitar um
    refactor amplo, o nome ficou igual, mas o transporte passou a usar a API
    REST JSON do ``https://app.numero-virtual.com/api/v1``.
    """

    STATUS_FINISH = 6
    STATUS_CANCEL = 8
    STATUS_RESEND = 3

    def __init__(
        self,
        api_key: str = SMS24H_API_KEY,
        base_url: str = SMS24H_BASE_URL,
        *,
        service_code: str = SMS24H_SERVICE_CODE,
        country_code: str = SMS24H_COUNTRY_CODE,
        operator: str = SMS24H_OPERATOR,
        poll_interval_s: float = SMS_POLL_INTERVAL_S,
        poll_timeout_s: float = SMS_POLL_TIMEOUT_S,
        request_timeout_s: float = 30.0,
        allowed_ddds: frozenset[str] = SMS24H_ALLOWED_DDDS,
        ddd_max_attempts: int = SMS24H_DDD_MAX_ATTEMPTS,
        service_id: int = NUMERO_VIRTUAL_SERVICE_ID,
        country_id: int = NUMERO_VIRTUAL_COUNTRY_ID,
        service_name: str = NUMERO_VIRTUAL_SERVICE_NAME,
        target_price: int = NUMERO_VIRTUAL_TARGET_PRICE,
        purchase_api_id: int = NUMERO_VIRTUAL_PURCHASE_API_ID,
        customer_session: str = NUMERO_VIRTUAL_CUSTOMER_SESSION,
        price_strict: bool = NUMERO_VIRTUAL_PRICE_STRICT,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.service_code = (service_code or "").strip()
        self.service_id = int(service_id or NUMERO_VIRTUAL_SERVICE_ID)
        self.service_name = (service_name or NUMERO_VIRTUAL_SERVICE_NAME).strip()
        self.country_id = _normalize_country(country_id or country_code)
        self.country_code = str(country_code or "").strip().lower()
        self.operator = operator
        self.target_price = int(target_price or 0)
        self.purchase_api_id = int(purchase_api_id or 0)
        self.customer_session = (customer_session or "").strip()
        self.price_strict = bool(price_strict)
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.request_timeout_s = request_timeout_s
        self.allowed_ddds = frozenset(allowed_ddds or frozenset())
        self.ddd_max_attempts = max(1, int(ddd_max_attempts or 1))
        self._client: Optional[httpx.AsyncClient] = None
        self._service_cache: Optional[list[dict[str, Any]]] = None
        self._trpc_url = f"{self.base_url.removesuffix('/api/v1')}/api/trpc"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def __aenter__(self) -> "Sms24hClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            self._client = httpx.AsyncClient(
                timeout=self.request_timeout_s,
                headers=headers,
                follow_redirects=True,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def get_balance(self) -> float:
        payload = await self._request_json("GET", "/balance")
        data = self._payload_data(payload)
        formatted = str(data.get("balanceFormatted") or "").strip()
        if formatted:
            match = re.search(r"(\d+[.,]\d{2})", formatted)
            if match:
                try:
                    return float(match.group(1).replace(",", "."))
                except ValueError:
                    pass
        raw_balance = (
            data.get("balance") if isinstance(data, dict) else payload.get("balance")
        )
        try:
            balance = float(raw_balance)
            if str(data.get("currency") or "").upper() == "BRL" and balance.is_integer():
                return balance / 100.0
            return balance
        except (TypeError, ValueError) as exc:
            raise Sms24hError(
                f"Resposta /balance inválida: {self._safe_json(payload)[:200]}"
            ) from exc

    async def get_numbers_status(self, country: Optional[str] = None) -> str:
        if not self.enabled:
            raise Sms24hBadKeyError("NUMERO_VIRTUAL_API_KEY não configurada.")
        country_id = _normalize_country(country) if country else self.country_id
        services = await self._load_services()
        filtered = [
            svc for svc in services
            if int(svc.get("countryId") or 0) == country_id
            and self._service_matches(svc)
        ]
        return self._safe_json(filtered)

    @staticmethod
    def _extract_ddd(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("55") and len(digits) >= 12:
            digits = digits[2:]
        return digits[:2] if len(digits) >= 2 else ""

    async def _acquire_once(
        self,
        service: Optional[str] = None,
        country: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.enabled:
            raise Sms24hBadKeyError("NUMERO_VIRTUAL_API_KEY não configurada.")

        entry = await self._resolve_service_entry(service=service, country=country)
        if self.customer_session:
            payload = await self._purchase_via_trpc(entry)
        else:
            payload = await self._request_json(
                "POST",
                "/activations",
                json={
                    "serviceId": int(entry["serviceId"]),
                    "countryId": int(entry.get("countryId") or self.country_id),
                },
            )
        data = self._payload_data(payload)
        activation_id = str(
            data.get("activationId")
            or data.get("id")
            or data.get("activation_id")
            or ""
        )
        phone = str(
            data.get("phoneNumber")
            or data.get("phone")
            or data.get("number")
            or ""
        )
        if not activation_id or not phone:
            raise Sms24hError(
                f"Resposta /activations inválida: {self._safe_json(payload)[:300]}"
            )

        actual_price = self._extract_price(data)
        if self.target_price and actual_price is not None and actual_price != self.target_price:
            msg = (
                "numero-virtual devolveu preço diferente do solicitado "
                f"(pedido={self.target_price}, recebido={actual_price}, id={activation_id})."
            )
            if self.price_strict:
                raise Sms24hBadServiceError(msg)
            logger.warning(msg)

        logger.info(
            "numero-virtual activation OK — activation_id=%s phone=%s price=%s",
            activation_id, phone, actual_price,
        )
        return activation_id, phone

    async def acquire_number(
        self,
        service: Optional[str] = None,
        country: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.enabled:
            raise Sms24hBadKeyError("NUMERO_VIRTUAL_API_KEY não configurada.")
        if not self.allowed_ddds:
            return await self._acquire_once(service, country)

        seen: list[str] = []
        for attempt in range(1, self.ddd_max_attempts + 1):
            activation_id, phone = await self._acquire_once(service, country)
            ddd = self._extract_ddd(phone)
            if not ddd or ddd in self.allowed_ddds:
                return activation_id, phone

            seen.append(ddd)
            logger.warning(
                "numero-virtual: DDD %s fora da faixa (%s) — tentando cancelar "
                "ativação %s [%d/%d].",
                ddd,
                ",".join(sorted(self.allowed_ddds)),
                activation_id,
                attempt,
                self.ddd_max_attempts,
            )
            cancelled = await self.set_status(activation_id, self.STATUS_CANCEL)
            if not cancelled:
                raise Sms24hDddError(
                    "numero-virtual não confirmou cancelamento de ativação fora "
                    "da faixa de DDD; remova o filtro de DDD no .env para evitar "
                    "acúmulo de compras."
                )
        raise Sms24hDddError(
            f"Sem número com DDD permitido ({','.join(sorted(self.allowed_ddds))}) "
            f"após {self.ddd_max_attempts} tentativa(s); DDDs recebidos: "
            f"{', '.join(seen) or '—'}."
        )

    async def get_sms_code(
        self,
        activation_id: str,
        *,
        poll_interval_s: Optional[float] = None,
        poll_timeout_s: Optional[float] = None,
    ) -> str:
        if not self.enabled:
            raise Sms24hBadKeyError("NUMERO_VIRTUAL_API_KEY não configurada.")

        base_wait = poll_interval_s if poll_interval_s is not None else self.poll_interval_s
        total_timeout = poll_timeout_s if poll_timeout_s is not None else self.poll_timeout_s
        deadline = asyncio.get_event_loop().time() + total_timeout
        wait = max(1.0, base_wait)

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise Sms24hTimeoutError(
                    f"Timeout aguardando código SMS (id={activation_id})."
                )
            payload = await self._request_json("GET", f"/activations/{activation_id}")
            data = self._payload_data(payload)
            sms_code = (
                data.get("smsCode")
                or data.get("sms_code")
                or data.get("code")
            )
            status = str(data.get("status") or "").strip().lower()
            if sms_code:
                return str(sms_code)
            if status in {"cancelled", "expired", "failed"}:
                raise Sms24hError(
                    f"Ativação {activation_id} encerrada pelo provedor ({status})."
                )
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30.0)

    async def set_status(self, activation_id: str, status: int) -> bool:
        if not self.enabled:
            return False

        try:
            if status == self.STATUS_FINISH:
                payload = await self._request_json(
                    "POST", f"/activations/{activation_id}/complete"
                )
                data = self._payload_data(payload)
                provider_status = str(data.get("status") or "").strip().lower()
                ok = provider_status in {"sms_received", "completed", "complete"}
                if ok:
                    logger.info(
                        "numero-virtual complete OK — id=%s status=%s",
                        activation_id, provider_status,
                    )
                return ok

            if status == self.STATUS_CANCEL:
                ok = await self._try_cancel_activation(activation_id)
                if not ok:
                    logger.warning(
                        "numero-virtual não confirmou cancelamento para id=%s; "
                        "a API pública não documenta esse endpoint.",
                        activation_id,
                    )
                return ok

            if status == self.STATUS_RESEND:
                logger.warning(
                    "numero-virtual não expõe endpoint público de reenvio para id=%s.",
                    activation_id,
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "numero-virtual set_status FALHOU (id=%s, status=%s): %s",
                activation_id, status, exc,
            )
            return False
        return False

    async def _load_services(self) -> list[dict[str, Any]]:
        if self._service_cache is not None:
            return self._service_cache
        payload = await self._request_json("GET", "/services")
        data = self._payload_data(payload)
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, list):
            raise Sms24hError(
                f"Resposta /services inválida: {self._safe_json(payload)[:300]}"
            )
        self._service_cache = [
            item for item in services if isinstance(item, dict)
        ]
        return self._service_cache

    async def _resolve_service_entry(
        self,
        *,
        service: Optional[str] = None,
        country: Optional[str] = None,
    ) -> dict[str, Any]:
        country_id = _normalize_country(country) if country else self.country_id
        raw_service = str(service or "").strip().lower()
        services = await self._load_services()
        candidates = [
            svc for svc in services
            if int(svc.get("countryId") or 0) == country_id
            and self._service_matches(svc, raw_service=raw_service)
        ]
        if not candidates:
            raise Sms24hBadServiceError(
                f"Serviço SMS indisponível no numero-virtual "
                f"(service={raw_service or self.service_code or self.service_id}, "
                f"countryId={country_id})."
            )
        if self.target_price:
            exact = [
                svc for svc in candidates
                if self._extract_price(svc) == self.target_price
            ]
            if exact:
                return exact[0]
        return candidates[0]

    def _service_matches(
        self,
        service: dict[str, Any],
        *,
        raw_service: str = "",
    ) -> bool:
        service_id = str(service.get("serviceId") or "").strip()
        service_code = str(service.get("serviceCode") or "").strip().lower()
        service_name = str(service.get("serviceName") or "").strip().lower()
        expected = raw_service or self.service_code.lower()
        expected_id = str(self.service_id)
        expected_name = self.service_name.lower()
        return any(
            candidate for candidate in {
                service_id == expected_id,
                bool(expected and service_code == expected),
                bool(expected and service_name == expected),
                bool(expected_name and service_name == expected_name),
            }
        )

    async def _try_cancel_activation(self, activation_id: str) -> bool:
        attempts = (
            ("POST", f"/activations/{activation_id}/cancel"),
            ("DELETE", f"/activations/{activation_id}"),
            ("POST", f"/activations/{activation_id}/close"),
            ("POST", f"/activations/{activation_id}/release"),
        )
        for method, path in attempts:
            try:
                payload = await self._request_json(method, path)
            except Sms24hError:
                continue
            data = self._payload_data(payload)
            provider_status = str(data.get("status") or "").strip().lower()
            if provider_status in {"cancelled", "canceled", "closed"}:
                return True
            message = str(data.get("message") or "").strip().lower()
            if "cancel" in message or "close" in message:
                return True
        return False

    async def _purchase_via_trpc(self, entry: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        # PREÇO/FAIXA: o mesmo serviceId (ex.: Facebook=60033) tem várias faixas
        # de preço (R$1,00 / 1,25 / 1,30 / 1,86…). Sem mandar o preço, a
        # numero-virtual devolvia a faixa mais barata (R$1,25), que na prática
        # NÃO recebia o SMS do Meta — a faixa R$1,30 recebe (operador testou
        # manual, 2026-09). `_resolve_service_entry` já escolheu a entrada com o
        # preço-alvo; mandamos esse preço na compra pra travar a faixa certa.
        target = int(entry.get("price") or self.target_price or 0)
        inner = {
            "countryId": int(entry.get("countryId") or self.country_id),
            "serviceId": int(entry["serviceId"]),
            "operator": None,
            "apiId": self.purchase_api_id,
            "ddd": None,
        }
        if target > 0:
            inner["price"] = target
        # BLINDAGEM: tenta COM price (trava a faixa R$1,30); se a numero-virtual
        # rejeitar o campo desconhecido, refaz SEM price (comportamento antigo)
        # em vez de deixar a compra do SMS quebrar de vez.
        attempts = [inner]
        if "price" in inner:
            attempts.append({k: v for k, v in inner.items() if k != "price"})
        first: Optional[dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for _idx, _inner in enumerate(attempts):
            _payload = {
                "0": {
                    "json": _inner,
                    "meta": {"values": {"operator": ["undefined"], "ddd": ["undefined"]}},
                }
            }
            logger.info(
                "numero-virtual POST trpc/store.purchaseNumber payload=%s",
                self._scrub(self._safe_json(_payload)),
            )
            try:
                response = await self._do_request(
                    client, "POST",
                    f"{self._trpc_url}/store.purchaseNumber?batch=1",
                    json=_payload,
                    cookies={"customer_session": self.customer_session},
                )
            except httpx.HTTPError as exc:
                raise Sms24hError(
                    f"numero-virtual trpc inacessível ({type(exc).__name__}): "
                    f"{self._scrub(str(exc))}"
                ) from None
            text = (response.text or "").strip()
            logger.info("numero-virtual trpc ← HTTP %s body=%r", response.status_code, text[:200])
            if response.status_code in {401, 403}:
                raise Sms24hBadKeyError(
                    "NUMERO_VIRTUAL_CUSTOMER_SESSION inválida ou expirada."
                )
            _err: Optional[Exception] = None
            if response.status_code >= 400:
                _err = Sms24hError(
                    f"numero-virtual trpc HTTP {response.status_code}: {self._scrub(text[:200])}"
                )
            else:
                try:
                    trpc_payload = response.json()
                except ValueError as exc:
                    raise Sms24hError(
                        f"numero-virtual trpc devolveu resposta não-JSON: {self._scrub(text[:200])}"
                    ) from exc
                if not isinstance(trpc_payload, list) or not trpc_payload:
                    raise Sms24hError(
                        f"numero-virtual trpc devolveu payload inválido: {self._safe_json(trpc_payload)[:200]}"
                    )
                _first = trpc_payload[0]
                if not isinstance(_first, dict):
                    raise Sms24hError(
                        f"numero-virtual trpc devolveu item inválido: {self._safe_json(_first)[:200]}"
                    )
                if "error" in _first:
                    _err = self._classify(_first["error"], context="POST trpc/store.purchaseNumber")
                else:
                    first = _first
                    break
            last_err = _err
            if _idx + 1 < len(attempts):
                logger.warning(
                    "numero-virtual: compra com price=%s falhou (%s) — refazendo "
                    "SEM price.", _inner.get("price"), _err,
                )
                continue
            raise _err
        if first is None:
            raise last_err or Sms24hError("numero-virtual trpc: compra falhou.")
        data = (
            first.get("result", {})
            .get("data", {})
            .get("json", {})
        )
        if not isinstance(data, dict):
            raise Sms24hError(
                f"numero-virtual trpc sem json de compra: {self._safe_json(first)[:300]}"
            )
        return {"data": data}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        url = f"{self.base_url}{path}"
        body = json if json is not None else None
        safe_body = self._scrub(self._safe_json(body)) if body is not None else ""
        logger.info("numero-virtual %s %s payload=%s", method, path, safe_body)
        try:
            response = await self._do_request(client, method, url, json=body)
        except httpx.HTTPError as exc:
            exc_type = type(exc).__name__
            raise Sms24hError(
                f"numero-virtual inacessível ({exc_type}): {self._scrub(str(exc))}"
            ) from None

        text = (response.text or "").strip()
        logger.info("numero-virtual ← HTTP %s body=%r", response.status_code, text[:200])
        if response.status_code in {401, 403}:
            raise Sms24hBadKeyError("NUMERO_VIRTUAL_API_KEY inválida ou sem permissão.")
        if response.status_code == 402:
            raise Sms24hNoBalanceError("Saldo insuficiente no numero-virtual.")
        if response.status_code >= 400:
            raise Sms24hError(
                f"numero-virtual HTTP {response.status_code}: {self._scrub(text[:200])}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise Sms24hError(
                f"numero-virtual devolveu resposta não-JSON: {self._scrub(text[:200])}"
            ) from exc
        if not isinstance(payload, dict):
            raise Sms24hError(
                f"numero-virtual devolveu payload inválido: {self._safe_json(payload)[:200]}"
            )
        if payload.get("success") is False:
            raise self._classify(payload, context=f"{method} {path}")
        return payload

    def _payload_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

    def _extract_price(self, payload: dict[str, Any]) -> Optional[int]:
        raw = payload.get("price")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _safe_json(self, payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=True, sort_keys=True)
        except TypeError:
            return repr(payload)

    def _scrub(self, text: str) -> str:
        out = text
        if self.api_key:
            masked = f"{self.api_key[:4]}…{self.api_key[-2:]}"
            out = out.replace(self.api_key, masked)
        if self.customer_session:
            masked_session = (
                f"{self.customer_session[:6]}…{self.customer_session[-4:]}"
            )
            out = out.replace(self.customer_session, masked_session)
        return out

    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        attempts=5,
        min_wait=0.5,
        max_wait=5.0,
        logger=logger,
    )
    async def _do_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: Optional[dict[str, Any]] = None,
        cookies: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        return await client.request(method, url, json=json, cookies=cookies)

    @staticmethod
    def _classify(payload: Any, *, context: str) -> Sms24hError:
        data = payload.get("data") if isinstance(payload, dict) else None
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or "")
        if not message and isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "")
        low = message.lower()
        if any(token in low for token in {"no numbers", "indispon", "unavailable"}):
            return Sms24hNoNumbersError(f"{context}: {message or 'sem números'}")
        if any(token in low for token in {"balance", "saldo"}):
            return Sms24hNoBalanceError(f"{context}: {message or 'sem saldo'}")
        if any(token in low for token in {"unauthorized", "forbidden", "token", "auth"}):
            return Sms24hBadKeyError(f"{context}: {message or 'token inválido'}")
        if any(token in low for token in {"service", "serviço", "serviceid"}):
            return Sms24hBadServiceError(f"{context}: {message or 'serviço inválido'}")
        return Sms24hError(f"{context}: {message or 'erro inesperado'}")


# ---------------------------------------------------------------------------
# sms24h.org — gateway SMS via protocolo sms-activate.ru (GET text)
# ---------------------------------------------------------------------------
class Sms24hOrgClient:
    """Cliente para sms24h.org (protocolo sms-activate.ru).

    Interface compatível com ``Sms24hClient``: mesmos métodos públicos
    (``acquire_number``, ``get_sms_code``, ``set_status``, ``get_balance``).
    """

    STATUS_FINISH = 6
    STATUS_CANCEL = 8
    STATUS_RESEND = 3

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        service: str = "fb",
        country: str = "73",
        poll_interval_s: float = 5.0,
        poll_timeout_s: float = 300.0,
        request_timeout_s: float = 30.0,
    ) -> None:
        from config import (
            SMS24H_ORG_API_KEY,
            SMS24H_ORG_BASE_URL,
            SMS24H_ORG_COUNTRY,
            SMS24H_ORG_SERVICE,
            SMS_POLL_INTERVAL_S,
            SMS_POLL_TIMEOUT_S,
        )
        self.api_key = api_key or SMS24H_ORG_API_KEY
        self.base_url = (base_url or SMS24H_ORG_BASE_URL).rstrip("/")
        self.service = service or SMS24H_ORG_SERVICE
        self.country = country or SMS24H_ORG_COUNTRY
        self.poll_interval_s = poll_interval_s or SMS_POLL_INTERVAL_S
        self.poll_timeout_s = poll_timeout_s or SMS_POLL_TIMEOUT_S
        self.request_timeout_s = request_timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def __aenter__(self) -> "Sms24hOrgClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.request_timeout_s,
                follow_redirects=True,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get(self, **params: Any) -> str:
        params["api_key"] = self.api_key
        client = self._ensure_client()
        try:
            resp = await client.get(self.base_url, params=params)
        except httpx.HTTPError as exc:
            raise Sms24hError(
                f"sms24h.org inacessível ({type(exc).__name__})"
            ) from exc
        text = resp.text.strip()
        if not text:
            raise Sms24hError("sms24h.org retornou resposta vazia")
        self._check_error(text)
        return text

    @staticmethod
    def _check_error(text: str) -> None:
        up = text.upper()
        if "BAD_KEY" in up or "BADKEY" in up:
            raise Sms24hBadKeyError("SMS24H_ORG_API_KEY inválida.")
        if "NO_BALANCE" in up:
            raise Sms24hNoBalanceError("Sem saldo no sms24h.org.")
        if "NO_NUMBERS" in up:
            raise Sms24hNoNumbersError("Sem números disponíveis no sms24h.org.")
        if "WRONG_SERVICE" in up or "BAD_SERVICE" in up:
            raise Sms24hBadServiceError("Serviço inválido no sms24h.org.")
        if "BAD_ACTION" in up:
            raise Sms24hError("Ação inválida no sms24h.org.")
        if "ERROR" in up and "ACCESS" not in up and "STATUS" not in up:
            raise Sms24hError(f"sms24h.org erro: {text}")

    async def get_balance(self) -> float:
        if not self.enabled:
            raise Sms24hBadKeyError("SMS24H_ORG_API_KEY não configurada.")
        text = await self._get(action="getBalance")
        match = re.search(r"ACCESS_BALANCE[:\s]+([\d.]+)", text)
        if match:
            return float(match.group(1))
        try:
            return float(text)
        except ValueError:
            raise Sms24hError(f"sms24h.org balance inválido: {text!r}")

    async def acquire_number(
        self,
        service: Optional[str] = None,
        country: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.enabled:
            raise Sms24hBadKeyError("SMS24H_ORG_API_KEY não configurada.")
        text = await self._get(
            action="getNumber",
            service=service or self.service,
            country=country or self.country,
        )
        # ACCESS_NUMBER:ID:PHONE
        parts = text.split(":")
        if len(parts) >= 3 and "ACCESS_NUMBER" in parts[0].upper():
            activation_id = parts[1].strip()
            phone = parts[2].strip()
            logger.info(
                "sms24h.org número alocado: id=%s phone=%s",
                activation_id, phone,
            )
            return activation_id, phone
        raise Sms24hError(f"sms24h.org getNumber inesperado: {text!r}")

    async def get_sms_code(
        self,
        activation_id: str,
        *,
        poll_interval_s: Optional[float] = None,
        poll_timeout_s: Optional[float] = None,
    ) -> str:
        if not self.enabled:
            raise Sms24hBadKeyError("SMS24H_ORG_API_KEY não configurada.")
        base_wait = poll_interval_s if poll_interval_s is not None else self.poll_interval_s
        total_timeout = poll_timeout_s if poll_timeout_s is not None else self.poll_timeout_s
        deadline = asyncio.get_event_loop().time() + total_timeout
        wait = max(1.0, base_wait)

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise Sms24hTimeoutError(
                    f"Timeout aguardando código SMS sms24h.org (id={activation_id})."
                )
            text = await self._get(action="getStatus", id=activation_id)
            up = text.upper()
            if "STATUS_OK" in up:
                # STATUS_OK:CODE (code can be full SMS text)
                _, _, code = text.partition(":")
                return code.strip()
            if "STATUS_CANCEL" in up:
                raise Sms24hError(
                    f"Ativação {activation_id} cancelada pelo sms24h.org."
                )
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY / STATUS_WAIT_RESEND
            await asyncio.sleep(wait)
            wait = min(wait * 1.5, 20.0)

    async def set_status(self, activation_id: str, status: int) -> bool:
        if not self.enabled:
            return False
        try:
            text = await self._get(
                action="setStatus", id=activation_id, status=status,
            )
            ok = "ACCESS" in text.upper()
            logger.info(
                "sms24h.org setStatus id=%s status=%s → %s",
                activation_id, status, text,
            )
            return ok
        except Sms24hError as exc:
            logger.error(
                "sms24h.org setStatus FALHOU (id=%s, status=%s): %s",
                activation_id, status, exc,
            )
            return False


# ---------------------------------------------------------------------------
# SMSRush (smsrush.com.br) — API REST JSON
# ---------------------------------------------------------------------------
class SmsRushClient:
    """Cliente para smsrush.com.br (API REST JSON).

    Interface compatível com ``Sms24hClient``: mesmos métodos públicos
    (``acquire_number``, ``get_sms_code``, ``set_status``, ``get_balance``).
    """

    STATUS_FINISH = 6
    STATUS_CANCEL = 8

    def __init__(
        self,
        *,
        api_token: str = "",
        base_url: str = "",
        service_id: int = 0,
        server: Optional[int] = None,
        poll_interval_s: float = 5.0,
        poll_timeout_s: float = 300.0,
        request_timeout_s: float = 30.0,
    ) -> None:
        from config import (
            SMSRUSH_API_TOKEN,
            SMSRUSH_BASE_URL,
            SMSRUSH_SERVICE_ID,
            SMS_POLL_INTERVAL_S,
            SMS_POLL_TIMEOUT_S,
        )
        self.api_token = api_token or SMSRUSH_API_TOKEN
        self.base_url = (base_url or SMSRUSH_BASE_URL).rstrip("/")
        self.service_id = service_id or SMSRUSH_SERVICE_ID
        self.server = server
        self.poll_interval_s = poll_interval_s or SMS_POLL_INTERVAL_S
        self.poll_timeout_s = poll_timeout_s or SMS_POLL_TIMEOUT_S
        self.request_timeout_s = request_timeout_s
        self._client: Optional[httpx.AsyncClient] = None
        self._user_id: Optional[int] = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_token)

    async def __aenter__(self) -> "SmsRushClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.request_timeout_s,
                follow_redirects=True,
                trust_env=False,
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get(self, path: str, **params: Any) -> dict:
        client = self._ensure_client()
        try:
            resp = await client.get(f"{self.base_url}{path}", params=params or None)
        except httpx.HTTPError as exc:
            raise Sms24hError(
                f"smsrush inacessível ({type(exc).__name__})"
            ) from exc
        if resp.status_code in {401, 403}:
            raise Sms24hBadKeyError("SMSRUSH_API_TOKEN inválido ou expirado.")
        if resp.status_code == 402:
            raise Sms24hNoBalanceError("Saldo insuficiente no SMSRush.")
        data = resp.json()
        if not data.get("success", False):
            msg = data.get("message", resp.text[:200])
            if "saldo" in msg.lower() or "balance" in msg.lower():
                raise Sms24hNoBalanceError(f"SMSRush: {msg}")
            raise Sms24hError(f"SMSRush: {msg}")
        return data

    async def _post(self, path: str, body: dict) -> dict:
        client = self._ensure_client()
        try:
            resp = await client.post(
                f"{self.base_url}{path}", json=body,
            )
        except httpx.HTTPError as exc:
            raise Sms24hError(
                f"smsrush inacessível ({type(exc).__name__})"
            ) from exc
        if resp.status_code in {401, 403}:
            raise Sms24hBadKeyError("SMSRUSH_API_TOKEN inválido ou expirado.")
        if resp.status_code == 402:
            raise Sms24hNoBalanceError("Saldo insuficiente no SMSRush.")
        data = resp.json()
        if not data.get("success", False):
            msg = data.get("message", resp.text[:200])
            if "saldo" in msg.lower() or "balance" in msg.lower():
                raise Sms24hNoBalanceError(f"SMSRush: {msg}")
            if "sem números" in msg.lower() or "no numbers" in msg.lower():
                raise Sms24hNoNumbersError(f"SMSRush: {msg}")
            raise Sms24hError(f"SMSRush: {msg}")
        return data

    async def _delete(self, path: str) -> dict:
        client = self._ensure_client()
        try:
            resp = await client.delete(f"{self.base_url}{path}")
        except httpx.HTTPError as exc:
            raise Sms24hError(
                f"smsrush inacessível ({type(exc).__name__})"
            ) from exc
        data = resp.json()
        return data

    async def _get_user_id(self) -> int:
        if self._user_id is not None:
            return self._user_id
        data = await self._get("/auth/me")
        self._user_id = data["data"]["user"]["id"]
        return self._user_id

    async def get_balance(self) -> float:
        if not self.enabled:
            raise Sms24hBadKeyError("SMSRUSH_API_TOKEN não configurado.")
        uid = await self._get_user_id()
        data = await self._get(f"/users/{uid}")
        return float(data["data"].get("current_balance", 0))

    async def acquire_number(
        self,
        service: Optional[str] = None,
        country: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.enabled:
            raise Sms24hBadKeyError("SMSRUSH_API_TOKEN não configurado.")
        body: dict[str, Any] = {"service_id": self.service_id}
        if self.server is not None:
            body["server"] = self.server
        data = await self._post("/virtual-numbers", body)
        entry = data.get("data", {})
        vn_id = str(entry.get("id", ""))
        phone = str(entry.get("number", ""))
        if not vn_id or not phone:
            raise Sms24hError(f"SMSRush acquire inesperado: {data!r}")
        logger.info(
            "smsrush número alocado: id=%s phone=%s", vn_id, phone,
        )
        return vn_id, phone

    async def get_sms_code(
        self,
        activation_id: str,
        *,
        poll_interval_s: Optional[float] = None,
        poll_timeout_s: Optional[float] = None,
    ) -> str:
        if not self.enabled:
            raise Sms24hBadKeyError("SMSRUSH_API_TOKEN não configurado.")
        base_wait = poll_interval_s if poll_interval_s is not None else self.poll_interval_s
        total_timeout = poll_timeout_s if poll_timeout_s is not None else self.poll_timeout_s
        deadline = asyncio.get_event_loop().time() + total_timeout
        wait = max(1.0, base_wait)

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise Sms24hTimeoutError(
                    f"Timeout aguardando código SMS smsrush (id={activation_id})."
                )
            data = await self._get(f"/virtual-numbers/{activation_id}")
            entry = data.get("data", {})
            code = entry.get("latest_sms_code")
            if code and code not in ("[removido por politica de retencao]",):
                logger.info(
                    "smsrush código recebido: id=%s code=%s",
                    activation_id, code,
                )
                return str(code).strip()
            status = (entry.get("rental_info") or {}).get("status", "")
            if status == "CANCELLED":
                raise Sms24hError(
                    f"Ativação {activation_id} cancelada pelo SMSRush."
                )
            await asyncio.sleep(wait)
            wait = min(wait * 1.5, 20.0)

    async def set_status(self, activation_id: str, status: int) -> bool:
        if not self.enabled:
            return False
        try:
            if status == self.STATUS_CANCEL:
                result = await self._delete(f"/virtual-numbers/{activation_id}")
                ok = result.get("success", False)
            else:
                ok = True
            logger.info(
                "smsrush setStatus id=%s status=%s → success=%s",
                activation_id, status, ok,
            )
            return ok
        except Sms24hError as exc:
            logger.error(
                "smsrush setStatus FALHOU (id=%s, status=%s): %s",
                activation_id, status, exc,
            )
            return False
