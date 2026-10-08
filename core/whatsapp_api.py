"""Cliente da WhatsApp Cloud API (Graph) usado pelo CRM.

Reusa o ``GRAPH_BASE`` e o parser de erro do :mod:`core.meta_graph` (mesma
disciplina de token morto/inservível). Cada BM provisionada guarda no store
(``bms``) o ``waba_id`` e um ``meta_token`` capturado no run — daqui saem as
chamadas de listar números, listar/criar templates e disparar mensagens.

Tudo async httpx, tolerante a falha: devolve ``(dados, erro)`` e NUNCA levanta
pro chamador (o endpoint decide como mostrar). O token nunca é logado.

Endpoints Cloud API cobertos:
  * ``GET  /{waba_id}/phone_numbers``      — números do WABA (phone_number_id)
  * ``GET  /{waba_id}/message_templates``  — templates + status de aprovação
  * ``POST /{waba_id}/message_templates``  — submeter template p/ aprovação
  * ``POST /{phone_number_id}/messages``   — disparar mensagem de template
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from core.meta_graph import _graph_error

logger = logging.getLogger("meta_automation.whatsapp_api")

_TIMEOUT = httpx.Timeout(30.0)

# Versão da Graph usada pelo WhatsApp Cloud API. Mantida SEPARADA do
# meta_graph.GRAPH_BASE (v19, polling de verificação) para poder acompanhar
# recursos mais novos da Cloud API sem mexer no fluxo de verificação da BM.
WA_API_VERSION = "v21.0"
WA_BASE = f"https://graph.facebook.com/{WA_API_VERSION}"


async def _get(
    path: str, token: str, *, params: Optional[dict[str, Any]] = None
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """GET {WA_BASE}/{path}. Devolve (json, None) ou (None, erro)."""
    if not token:
        return None, "token ausente"
    p = dict(params or {})
    p["access_token"] = token
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{WA_BASE}/{path}", params=p)
        if resp.status_code != 200:
            return None, _graph_error(resp)
        return resp.json(), None
    except (httpx.HTTPError, ValueError) as exc:  # rede/JSON
        return None, f"{type(exc).__name__}: {exc}"


async def _post(
    path: str, token: str, payload: dict[str, Any]
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """POST JSON {WA_BASE}/{path}. Devolve (json, None) ou (None, erro)."""
    if not token:
        return None, "token ausente"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{WA_BASE}/{path}",
                params={"access_token": token},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            return None, _graph_error(resp)
        return resp.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Números / WABA
# ---------------------------------------------------------------------------
async def list_phone_numbers(
    waba_id: str, token: str
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Números de telefone do WABA (cada um tem seu ``id`` = phone_number_id)."""
    if not waba_id:
        return [], "waba_id ausente"
    data, err = await _get(
        f"{waba_id}/phone_numbers",
        token,
        params={
            "fields": (
                "id,display_phone_number,verified_name,quality_rating,"
                "code_verification_status,name_status,platform_type,throughput,"
                "status,messaging_limit_tier"
            ),
            "limit": 50,
        },
    )
    if err:
        return [], err
    return list((data or {}).get("data") or []), None


async def get_phone_number(
    phone_number_id: str, token: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Detalhes de UM número: status (CONNECTED?), tier do limite, qualidade,
    status do nome e da verificação. ``status`` == 'CONNECTED' = pronto p/ enviar."""
    if not phone_number_id:
        return None, "phone_number_id ausente"
    return await _get(
        phone_number_id,
        token,
        params={
            "fields": (
                "display_phone_number,verified_name,quality_rating,status,"
                "code_verification_status,name_status,throughput,"
                "messaging_limit_tier,platform_type"
            )
        },
    )


# ---------------------------------------------------------------------------
# Registro de número (após adicionado ao WABA): request → verify → register
# ---------------------------------------------------------------------------
async def request_code(
    phone_number_id: str, token: str, *, code_method: str = "SMS", language: str = "pt_BR"
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Pede o código de verificação (SMS/VOICE) para o número ativar a Cloud API.

    O número já precisa estar ADICIONADO ao WABA (via WhatsApp Manager/Embedded
    Signup) — isto só dispara o código de propriedade do número."""
    if not phone_number_id:
        return None, "phone_number_id ausente"
    method = code_method.upper()
    if method not in ("SMS", "VOICE"):
        method = "SMS"
    return await _post(
        f"{phone_number_id}/request_code",
        token,
        {"code_method": method, "language": language},
    )


async def verify_code(
    phone_number_id: str, token: str, *, code: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Confirma o código recebido (SMS/voz)."""
    if not (phone_number_id and code):
        return None, "phone_number_id/code obrigatórios"
    return await _post(
        f"{phone_number_id}/verify_code", token, {"code": str(code).strip()}
    )


async def register_phone(
    phone_number_id: str, token: str, *, pin: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Registra o número na Cloud API com um PIN de verificação em duas etapas
    (6 dígitos). Após isto o ``status`` do número vira 'CONNECTED' e ele pode
    enviar/receber pela Cloud API."""
    if not (phone_number_id and pin):
        return None, "phone_number_id/pin obrigatórios"
    return await _post(
        f"{phone_number_id}/register",
        token,
        {"messaging_product": "whatsapp", "pin": str(pin).strip()},
    )


async def get_waba_info(
    waba_id: str, token: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Nome + status de revisão + limite de mensagens do WABA."""
    if not waba_id:
        return None, "waba_id ausente"
    return await _get(
        waba_id,
        token,
        params={
            "fields": (
                "name,account_review_status,message_template_namespace,"
                "currency,timezone_id"
            )
        },
    )


# ---------------------------------------------------------------------------
# Webhook subscription (app ↔ WABA) — necessário p/ a Meta ENTREGAR os eventos
# ---------------------------------------------------------------------------
async def subscribe_app(
    waba_id: str, token: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Assina o app (dono do token) aos webhooks desta WABA.

    Sem isto a Meta não envia os eventos (mensagens/status) ao seu endpoint,
    mesmo com o webhook configurado no app. POST /{waba_id}/subscribed_apps."""
    if not waba_id:
        return None, "waba_id ausente"
    return await _post(f"{waba_id}/subscribed_apps", token, {})


async def list_subscribed_apps(
    waba_id: str, token: str
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Apps atualmente assinados aos webhooks da WABA (p/ mostrar o status)."""
    if not waba_id:
        return [], "waba_id ausente"
    data, err = await _get(f"{waba_id}/subscribed_apps", token)
    if err:
        return [], err
    return list((data or {}).get("data") or []), None


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
async def list_templates(
    waba_id: str, token: str, *, limit: int = 250
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Templates do WABA com status de aprovação, categoria e componentes."""
    if not waba_id:
        return [], "waba_id ausente"
    data, err = await _get(
        f"{waba_id}/message_templates",
        token,
        params={
            "fields": "name,status,category,language,components,quality_score,id",
            "limit": limit,
        },
    )
    if err:
        return [], err
    return list((data or {}).get("data") or []), None


async def create_template(
    waba_id: str,
    token: str,
    *,
    name: str,
    category: str,
    language: str,
    components: list[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Submete um template para aprovação da Meta.

    ``category`` ∈ {'MARKETING','UTILITY','AUTHENTICATION'}. ``components`` no
    formato da Cloud API (BODY obrigatório; HEADER/FOOTER/BUTTONS opcionais).
    Devolve o payload de criação (id + status inicial) ou erro.
    """
    if not (waba_id and name and category and language):
        return None, "waba_id/name/category/language obrigatórios"
    payload = {
        "name": name,
        "category": category.upper(),
        "language": language,
        "components": components,
    }
    return await _post(f"{waba_id}/message_templates", token, payload)


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------
async def send_template_message(
    phone_number_id: str,
    token: str,
    *,
    to: str,
    template_name: str,
    language: str,
    components: Optional[list[dict[str, Any]]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Dispara uma mensagem de template para ``to`` (E.164 sem '+').

    ``components`` traz os parâmetros preenchidos (BODY/HEADER/BUTTON). Devolve
    o payload com ``messages[0].id`` (wamid) ou o erro da Graph.
    """
    if not (phone_number_id and to and template_name and language):
        return None, "phone_number_id/to/template/language obrigatórios"
    template: dict[str, Any] = {
        "name": template_name,
        "language": {"code": language},
    }
    if components:
        template["components"] = components
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }
    return await _post(f"{phone_number_id}/messages", token, payload)


def wamid_of(send_result: Optional[dict[str, Any]]) -> Optional[str]:
    """Extrai o wamid (id da mensagem) do retorno de send_template_message."""
    try:
        return (send_result or {}).get("messages", [{}])[0].get("id")
    except (IndexError, AttributeError, TypeError):
        return None
