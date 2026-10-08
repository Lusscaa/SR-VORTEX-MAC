"""Consulta o STATUS DE VERIFICAÇÃO da empresa na Graph API (classificação 2k).

Definição confirmada com o operador (2026-06-14): uma BM "virou 2k" quando a
empresa foi **verificada e aprovada pela Meta** (Central de Segurança) — NÃO é
o tier de mensagens do WhatsApp nem o limite de gasto de anúncios.

O sinal de verdade é o ``verification_status`` do objeto Business na Graph API
(``GET /{business_id}?fields=verification_status``). A aprovação acontece de
forma assíncrona dias após a submissão, então o pipeline (que só sabe que
SUBMETEU) não basta — daí o polling.

Usa o ``meta_token`` capturado no run. Tolerante a falhas: token expirado / sem
escopo → ``(None, erro)`` sem derrubar nada. A Graph API é a única peça que não
dá pra testar sem um token real; os testes mockam o transporte httpx.

Valores de ``verification_status`` (Meta): 'verified', 'not_verified',
'pending', 'pending_need_more_info', 'pending_submission', 'rejected',
'failed', 'expired'. '2k' = 'verified'.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from core import store

logger = logging.getLogger("meta_automation.meta_graph")

GRAPH_BASE = "https://graph.facebook.com/v19.0"

# Status que contam como "aprovada" (= 2k).
_VERIFIED = {"verified"}
# Status conhecidos que NÃO são aprovação (mas a conta existe/foi avaliada).
_NOT_VERIFIED = {
    "not_verified", "pending", "pending_need_more_info",
    "pending_submission", "rejected", "failed", "expired", "revoked",
}


def classify_verification(status: Optional[str]) -> str:
    """Mapeia o verification_status cru → '2k' / 'not_2k' / 'unknown'."""
    if not status:
        return store.TIER_UNKNOWN
    s = status.strip().lower()
    if s in _VERIFIED:
        return "2k"
    if s in _NOT_VERIFIED:
        return "not_2k"
    return store.TIER_UNKNOWN


# Erros da Graph API que significam TOKEN MORTO/INSERVÍVEL (não "pendente").
# Distinguir o MOTIVO é o que faltava: ``code 190`` é genérico e cobre tanto
# "sessão expirada" (token vivo mas velho → RE-INSPECIONAR depois) quanto
# "não consigo decodificar o token" (capturamos o token ERRADO → RE-CAPTURAR).
# Antes colapsávamos tudo em TOKEN_EXPIRED e ficávamos cegos — daí o
# "token JÁ expirado na captura (incomum)": não estava expirado, era o token
# errado (o do payload do WhatsApp, não um token de escopo Graph).
_EXPIRED_ERROR_CODES = {102, 463, 467}
# Subcódigos do 190 = sessão morta (expirou / logout / senha trocada).
_EXPIRED_SUBCODES = {458, 459, 460, 463, 464, 467, 492}
# Marcadores na mensagem do 190 = token MALFORMADO / de tipo errado.
_MALFORMED_MARKERS = ("cannot parse", "malformed", "signature", "invalid key")

# Sentinelas estáveis devolvidas por _graph_error p/ token inservível (os
# chamadores comparam por igualdade; o detalhe cru vai pro log, sem vazar token).
TOKEN_EXPIRED = "TOKEN_EXPIRED"
TOKEN_INVALID = "TOKEN_INVALID"
_DEAD_TOKEN_ERRORS = {TOKEN_EXPIRED, TOKEN_INVALID}


def is_dead_token(err: Optional[str]) -> bool:
    """True quando o erro indica token inservível (expirado OU malformado)."""
    return err in _DEAD_TOKEN_ERRORS


def _graph_error(resp: "httpx.Response") -> str:
    """Erro legível p/ resposta != 200.

    Classifica o token morto em DOIS casos (antes era um só):
      * ``TOKEN_EXPIRED`` — sessão expirada/invalidada (190 c/ subcódigo ou
        mensagem de expiração; 102/463/467). Sinal: RE-INSPECIONAR depois (token
        da run vive ~2h e a Meta aprova horas/dias depois).
      * ``TOKEN_INVALID`` — token malformado / de tipo errado (190 'cannot
        parse'/'signature'…). Sinal: capturamos o token ERRADO → RE-CAPTURAR.
    Outros erros → ``GRAPH <code>/<sub>: <msg>`` (token autenticou, mas faltou
    permissão/escopo, ou objeto inexistente). Sem JSON → ``HTTP <status>: …``.
    """
    try:
        err = (resp.json() or {}).get("error") or {}
    except (ValueError, TypeError):
        err = {}
    if err:
        code = int(err.get("code", 0) or 0)
        sub = int(err.get("error_subcode", 0) or 0)
        msg = str(err.get("message", "")).lower()
        if code == 190:
            if any(k in msg for k in _MALFORMED_MARKERS):
                return TOKEN_INVALID
            return TOKEN_EXPIRED
        if code in _EXPIRED_ERROR_CODES or sub in _EXPIRED_SUBCODES:
            return TOKEN_EXPIRED
        return f"GRAPH {code}/{sub}: {str(err.get('message', ''))[:140]}"
    return f"HTTP {resp.status_code}: {resp.text[:160]}"


def _log_graph_error(kind: str, oid: str, resp: "httpx.Response") -> None:
    """Loga o erro CRU da Graph (code/subcode/message) — verdade de campo p/
    saber por que o token não serviu. NUNCA loga o token (não está no corpo)."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except (ValueError, TypeError):
        err = {}
    if err:
        logger.info(
            "[GRAPH] %s %s → cód=%s sub=%s tipo=%s msg=%s",
            kind, oid, err.get("code"), err.get("error_subcode"),
            err.get("type"), str(err.get("message", ""))[:160],
        )
    else:
        logger.info(
            "[GRAPH] %s %s → HTTP %s: %s",
            kind, oid, resp.status_code, resp.text[:160],
        )


async def fetch_waba_review(
    waba_id: str, token: str, *, client: Optional[httpx.AsyncClient] = None
) -> tuple[Optional[str], Optional[str]]:
    """Retorna ``(account_review_status_cru, erro)`` do WABA. Nunca levanta.

    ``account_review_status`` (APPROVED/PENDING/REJECTED) é o sinal do WhatsApp
    que costuma aprovar em HORAS — distinto do ``verification_status`` do
    Business (Central de Segurança, dias). Pollar os dois cobre os dois
    milestones que o operador acompanha."""
    if not (waba_id and token):
        return None, "waba_id/token ausente"
    url = f"{GRAPH_BASE}/{waba_id}"
    params = {"fields": "account_review_status,name", "access_token": token}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            _log_graph_error("waba", waba_id, resp)
            return None, _graph_error(resp)
        status = (resp.json() or {}).get("account_review_status")
        if not status:
            return None, "sem account_review_status na resposta"
        return status, None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if owns_client:
            await client.aclose()


async def fetch_business_verification(
    business_id: str, token: str, *, client: Optional[httpx.AsyncClient] = None
) -> tuple[Optional[str], Optional[str]]:
    """Retorna ``(verification_status_cru, erro)`` do Business. Nunca levanta."""
    if not (business_id and token):
        return None, "business_id/token ausente"
    url = f"{GRAPH_BASE}/{business_id}"
    params = {"fields": "verification_status,name", "access_token": token}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            _log_graph_error("business", business_id, resp)
            return None, _graph_error(resp)
        payload = resp.json()
        status = payload.get("verification_status")
        if not status:
            return None, "sem verification_status na resposta"
        return status, None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if owns_client:
            await client.aclose()


async def refresh_bm_verification_async() -> dict[str, int]:
    """Consulta a Graph API por BM (empresa + WABA) e atualiza o store.

    Polla os DOIS sinais: ``verification_status`` do Business (Central de
    Segurança, ~dias) e ``account_review_status`` do WABA (~horas) — o operador
    acompanha os dois. Distingue TOKEN EXPIRADO (re-inspecionar) de pendente.
    Retorna ``{atualizadas, falhas, sem_token, token_expirado}``.
    """
    bms = store.list_bms(limit=1000)
    summary = {
        "atualizadas": 0, "falhas": 0, "sem_token": 0,
        "token_expirado": 0, "token_invalido": 0,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        for bm in bms:
            biz = bm.get("business_id")
            token = bm.get("meta_token")
            waba = bm.get("waba_id")
            if not (biz and token):
                summary["sem_token"] += 1
                continue
            status, err = await fetch_business_verification(biz, token, client=client)
            if is_dead_token(err):
                # Token inservível — NÃO é "pendente". Distingue expirado
                # (re-inspecionar p/ token fresco) de malformado/errado
                # (re-capturar), em vez de sumir como falha silenciosa (era a
                # causa nº1 de "nenhuma conta validou").
                if err == TOKEN_EXPIRED:
                    detail = "token_expirado (re-inspecionar)"
                    summary["token_expirado"] += 1
                else:
                    detail = "token_invalido (re-capturar)"
                    summary["token_invalido"] += 1
                store.set_bm_tier(biz, tier=store.TIER_UNKNOWN, detail=detail)
                continue
            if err:
                logger.info("BM %s: verificação não obtida (%s).", biz, err)
                summary["falhas"] += 1
                continue
            # WABA review (best-effort): só enriquece o detail quando há waba_id.
            detail = status
            if waba:
                waba_status, waba_err = await fetch_waba_review(
                    waba, token, client=client
                )
                if waba_status:
                    detail = f"{status} · waba:{waba_status}"
                elif waba_err and not is_dead_token(waba_err):
                    logger.debug("WABA %s: review não obtido (%s).", waba, waba_err)
            store.set_bm_tier(biz, tier=classify_verification(status), detail=detail)
            summary["atualizadas"] += 1
    return summary


def refresh_bm_verification() -> dict[str, int]:
    """Wrapper síncrono (para a UI)."""
    import asyncio

    return asyncio.run(refresh_bm_verification_async())
