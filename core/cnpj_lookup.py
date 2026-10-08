"""Consulta de CNPJ com cache durável (compartilhada por prévia e run).

A BrasilAPI pública limita por IP (HTTP 429). Sem cache, cada prévia E cada
run re-batiam a API — e a run morria no 1º passo quando a API recusava. Aqui
buscamos UMA vez e memorizamos o ``CNPJData`` no :mod:`core.store`
(``cnpj_cache``). Depois disso, prévia e run reusam o cache e não dependem
mais da disponibilidade da BrasilAPI.

Enriquecimento multi-fonte: a BrasilAPI usa um dump defasado da Receita e
devolve o número do endereço como "S/N" com frequência. Quando isso acontece,
fazemos um *backfill* consultando a Receita AO VIVO via
:mod:`core.cnpj_providers` (ReceitaWS / CNPJá Open / CNPJ.ws / Infosimples, com
fallback — quando um bloqueia/limita, tenta o próximo). O resultado enriquecido
é re-gravado no cache. A mesma cadeia serve de PRIMÁRIA quando a BrasilAPI cai.
"""
from __future__ import annotations

import logging
import re

from core import store
from core.api_clients import BrasilApiClient, BrasilApiError
from core.cnpj_providers import fetch_live
from core.ocr_engine import CNPJData
from utils.helpers import only_digits

logger = logging.getLogger("meta_automation.cnpj_lookup")

_SN_RE = re.compile(r"^s\/?n$", re.IGNORECASE)


def _numero_ausente(valor: str | None) -> bool:
    """True quando o número do endereço falta de fato (vazio ou 'S/N')."""
    v = (valor or "").strip()
    return not v or bool(_SN_RE.match(v))


def _logradouro_ausente(valor: str | None) -> bool:
    """True quando não há rua utilizável (vazio ou só dígitos/pontuação).

    O dump da Receita usado pela BrasilAPI vem SEM logradouro em ~10% dos MEIs
    (CEP genérico da cidade, terminado em -000, em que o lookup de CEP também
    não devolve rua). Sem esse sinal o endereço do portfólio virava só o
    número ('2320') e a Meta reprovava a verificação (produção 2026-09).
    """
    v = (valor or "").strip()
    return not v or not any(ch.isalpha() for ch in v)


def _norm_to_cnpj_data(norm: dict, digits: str) -> CNPJData:
    """Constrói um CNPJData a partir do dict normalizado de um provedor ao vivo.

    As chaves do dict já são campos do CNPJData; os validators do model
    normalizam (uf → maiúsculo etc.)."""
    fields = {k: v for k, v in norm.items() if v}
    return CNPJData(cnpj=digits, raw_text="", **fields)


async def fetch_cnpj_cached(cnpj: str, *, force: bool = False) -> CNPJData:
    """Devolve o ``CNPJData`` do CNPJ, do cache quando possível.

    ``force=True`` ignora o cache e re-busca (e re-grava). Levanta
    ``BrasilApiError`` se precisar bater na API e ela falhar (ex.: 429) E
    nenhum provedor ao vivo resolver."""
    digits = only_digits(cnpj or "")
    data: CNPJData | None = None
    if not force:
        cached = store.get_cnpj_cache(digits)
        if cached:
            # REGRA ABSOLUTA: nunca reutilizar CNPJs discarded, mesmo que estejam em cache.
            # Verifica o status atual no banco — se discarded, ignora cache.
            cnpj_row = store.get_cnpj_by_digits(digits)
            if cnpj_row and cnpj_row.get("status") == store.POOL_DISCARDED:
                logger.warning(
                    "CNPJ %s está em cache MAS marcado como POOL_DISCARDED no banco — "
                    "ignorando cache, forçando re-consulta.", digits
                )
                cached = None  # Força re-busca
            if cached:
                logger.info("CNPJ %s servido do cache (sem bater na BrasilAPI).", digits)
                data = CNPJData.model_validate_json(cached)

    fresh = data is None
    if data is None:
        try:
            data = await BrasilApiClient().fetch(digits)
        except BrasilApiError as exc:
            # BrasilAPI fora (429/instável): cai pros provedores ao vivo como
            # PRIMÁRIA — pra não interromper o fluxo. Se NENHUM resolver,
            # propaga o erro ORIGINAL (prévia 502 / run falha, como antes).
            data = await _fallback_primary(digits, exc)

    # Backfill do número (e contato) via provedores ao vivo quando a BrasilAPI
    # não trouxe — best-effort, não derruba a consulta se todos caírem.
    enriched = await _maybe_backfill(digits, data)

    if fresh or enriched:
        try:
            store.set_cnpj_cache(digits, data.model_dump_json())
        except Exception as exc:  # noqa: BLE001 — cache é best-effort
            logger.warning("CNPJ %s: falha ao gravar cache (%s).", digits, exc)
    return data


async def _fallback_primary(digits: str, original_exc: Exception) -> CNPJData:
    """BrasilAPI caiu → tenta a cadeia de provedores ao vivo como PRIMÁRIA.

    Se NENHUM provedor resolver, re-levanta o erro ORIGINAL da BrasilAPI
    (preserva a mensagem de 429 para o caller)."""
    try:
        norm = await fetch_live(digits, want=("numero", "logradouro"))
    except Exception as exc2:  # noqa: BLE001 — fetch_live já engole por provedor
        norm = None
        logger.error("CNPJ %s: cadeia de provedores estourou (%s).", digits, exc2)
    if not norm:
        logger.error(
            "CNPJ %s: BrasilAPI caiu (%s) e nenhum provedor ao vivo resolveu — "
            "propagando erro original.", digits, original_exc,
        )
        raise original_exc
    logger.warning(
        "CNPJ %s: BrasilAPI caiu (%s) — servido por provedor ao vivo.",
        digits, original_exc,
    )
    return _norm_to_cnpj_data(norm, digits)


async def _maybe_backfill(digits: str, data: CNPJData) -> bool:
    """Preenche LOGRADOURO/número/telefone/e-mail via provedores ao vivo quando
    a BrasilAPI omitiu. Muta ``data`` in-place e devolve True se algo mudou.

    Dispara quando falta o NÚMERO **ou** o LOGRADOURO (2026-09: faltando a rua,
    o endereço enviado à Meta ficava só com o número). Best-effort: se todos os
    provedores caírem/limitarem, o que faltar segue vazio e o compositor de
    endereço usa o bairro como último recurso."""
    falta_numero = _numero_ausente(data.numero)
    falta_logradouro = _logradouro_ausente(data.logradouro)
    if not (falta_numero or falta_logradouro):
        return False  # endereço completo — nada a fazer

    want: tuple[str, ...] = tuple(
        k for k, falta in (("numero", falta_numero), ("logradouro", falta_logradouro))
        if falta
    )
    norm = await fetch_live(digits, want=want)
    if not norm:
        logger.warning(
            "CNPJ %s: nenhum provedor ao vivo trouxe dados — faltando %s.",
            digits, ", ".join(want),
        )
        return False

    changed = False
    numero = (norm.get("numero") or "").strip()
    if falta_numero and numero and not _SN_RE.match(numero):
        data.numero = numero
        changed = True
        logger.info("CNPJ %s: número preenchido via provedor ao vivo (%s).", digits, numero)
    if falta_logradouro:
        logradouro = (norm.get("logradouro") or "").strip()
        if logradouro and not _logradouro_ausente(logradouro):
            data.logradouro = logradouro
            changed = True
            logger.info(
                "CNPJ %s: LOGRADOURO preenchido via provedor ao vivo (%s).",
                digits, logradouro,
            )
        else:
            logger.warning(
                "CNPJ %s: nenhum provedor trouxe logradouro — o endereço vai "
                "usar o bairro como fallback (nunca só o número).", digits,
            )
        # Bairro/complemento ajudam a compor o endereço quando não há rua.
        if not (data.bairro or "").strip() and norm.get("bairro"):
            data.bairro = norm["bairro"]
            changed = True
    # Telefone/e-mail: preenche só se a BrasilAPI deixou vazio.
    if not (data.telefone or "").strip() and norm.get("telefone"):
        data.telefone = norm["telefone"]
        changed = True
    if not (data.email or "").strip() and norm.get("email"):
        data.email = str(norm["email"]).lower()
        changed = True
    return changed
