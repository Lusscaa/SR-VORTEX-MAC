"""Rotas do CRM (WhatsApp Cloud API) — registradas em api_server via router.

Fluxo: as BMs provisionadas já guardam ``waba_id`` + ``meta_token`` no store.
Aqui o operador conecta um número (phone_number_id) de cada BM, lista/cria
templates, dispara templates para leads (filtrados por tag) e vê o chat.

Todas as chamadas à Cloud API são best-effort: erro da Graph vira ``detail``
legível (HTTP 400/502), sem derrubar o backend. O token nunca é devolvido ao
cliente.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from core import store, crm_store, whatsapp_api as wa

logger = logging.getLogger("meta_automation.crm_api")

router = APIRouter()

# Chaves de settings do webhook (store.settings).
_WH_VERIFY_KEY = "crm_webhook_verify_token"
_WH_SECRET_KEY = "crm_webhook_app_secret"
_WH_PUBLIC_KEY = "crm_webhook_public_url"

# Registro em memória dos disparos em background (progresso ao vivo). Some quando
# o backend reinicia — é só telemetria de progresso; as mensagens já enviadas
# ficam gravadas em crm_messages de qualquer forma.
_DISPATCH_JOBS: dict[str, dict[str, Any]] = {}
_DISPATCH_LOCK = threading.Lock()
_DISPATCH_MAX = 40   # mantém só os N jobs mais recentes


def _dispatch_prune() -> None:
    if len(_DISPATCH_JOBS) <= _DISPATCH_MAX:
        return
    old = sorted(_DISPATCH_JOBS.values(), key=lambda j: j.get("started_at", 0))
    for j in old[: len(_DISPATCH_JOBS) - _DISPATCH_MAX]:
        _DISPATCH_JOBS.pop(j["id"], None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bm_or_404(business_id: str) -> dict[str, Any]:
    bm = store.get_bm(business_id)
    if not bm:
        raise HTTPException(404, f"BM '{business_id}' não encontrada.")
    return bm


def _token_or_400(bm: dict[str, Any]) -> str:
    token = (bm.get("meta_token") or "").strip()
    if not token:
        raise HTTPException(
            400,
            "Esta BM não tem token da Graph salvo. Cole um token de Usuário do "
            "Sistema (permanente) na aba de conexão da BM.",
        )
    return token


def _waba_or_400(bm: dict[str, Any]) -> str:
    waba = (bm.get("waba_id") or "").strip()
    if not waba:
        raise HTTPException(
            400,
            "Esta BM não tem WABA (conta WhatsApp) capturada. Rode/finalize o "
            "provisionamento do WhatsApp para esta BM antes de usar o CRM.",
        )
    return waba


def _run(coro: Any) -> Any:
    """Executa uma corrotina da Cloud API dentro de um endpoint sync."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# BMs / conexão
# ---------------------------------------------------------------------------
@router.get("/api/crm/bms")
def crm_list_bms() -> dict[str, Any]:
    """BMs disponíveis para o CRM (as que têm WABA), com status de conexão."""
    conns = crm_store.list_bm_connections()
    out: list[dict[str, Any]] = []
    for bm in store.list_bms(limit=1000):
        waba = (bm.get("waba_id") or "").strip()
        if not waba:
            continue  # sem WhatsApp não entra no CRM
        conn = conns.get(bm["business_id"]) or {}
        out.append({
            "business_id": bm["business_id"],
            "portfolio_name": bm.get("portfolio_name"),
            "cnpj": bm.get("cnpj"),
            "waba_id": waba,
            "has_token": bool((bm.get("meta_token") or "").strip()),
            "token_durable": bool(bm.get("token_durable")),
            "tier": bm.get("tier"),
            "manual_status": bm.get("manual_status"),
            "phone_number_id": conn.get("phone_number_id"),
            "display_phone": conn.get("display_phone"),
            "verified_name": conn.get("verified_name"),
            "connected": bool(conn.get("phone_number_id")),
        })
    return {"bms": out, "count": len(out)}


class ManualBmBody(BaseModel):
    name: Optional[str] = None
    waba_id: str
    token: str


@router.post("/api/crm/bms/manual")
def crm_add_manual_bm(body: ManualBmBody) -> dict[str, Any]:
    """Adiciona uma WABA MANUALMENTE ao CRM (número de teste, ou WABA que o bot
    não capturou). Valida o token contra a WABA antes de salvar."""
    waba = (body.waba_id or "").strip()
    token = (body.token or "").strip()
    if not waba:
        raise HTTPException(400, "WABA ID obrigatório.")
    if len(token) < 20:
        raise HTTPException(400, "Token muito curto — cole o token completo.")
    info, err = _run(wa.get_waba_info(waba, token))
    if err:
        raise HTTPException(502, f"Não consegui acessar essa WABA com o token: {err}")
    business_id = f"manual_{waba}"
    store.upsert_bm(
        business_id=business_id,
        waba_id=waba,
        meta_token=token,
        token_durable=1,
        portfolio_name=((body.name or "").strip() or (info or {}).get("name") or "WABA manual"),
    )
    return {"ok": True, "business_id": business_id, "waba_name": (info or {}).get("name")}


class TokenBody(BaseModel):
    token: str
    durable: bool = True


@router.post("/api/crm/bms/{business_id}/token")
def crm_set_token(business_id: str, body: TokenBody) -> dict[str, Any]:
    """Salva/atualiza o token da Graph desta BM (System User = permanente)."""
    _bm_or_404(business_id)
    tok = (body.token or "").strip()
    if len(tok) < 20:
        raise HTTPException(400, "Token muito curto — cole o token completo.")
    store.upsert_bm(
        business_id=business_id,
        meta_token=tok,
        token_durable=1 if body.durable else 0,
    )
    return {"ok": True, "business_id": business_id, "durable": body.durable}


@router.get("/api/crm/bms/{business_id}/phones")
def crm_list_phones(business_id: str) -> dict[str, Any]:
    """Números do WABA desta BM (para o operador escolher qual conectar)."""
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    waba = _waba_or_400(bm)
    phones, err = _run(wa.list_phone_numbers(waba, token))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"phones": phones, "waba_id": waba}


class ConnectBody(BaseModel):
    phone_number_id: str
    display_phone: Optional[str] = None
    verified_name: Optional[str] = None


@router.post("/api/crm/bms/{business_id}/connect")
def crm_connect(business_id: str, body: ConnectBody) -> dict[str, Any]:
    """Conecta (fixa) um número Cloud API a esta BM p/ disparos."""
    _bm_or_404(business_id)
    if not (body.phone_number_id or "").strip():
        raise HTTPException(400, "phone_number_id obrigatório.")
    crm_store.set_bm_connection(
        business_id,
        phone_number_id=body.phone_number_id.strip(),
        display_phone=body.display_phone,
        verified_name=body.verified_name,
    )
    return {"ok": True, "business_id": business_id}


@router.post("/api/crm/bms/{business_id}/disconnect")
def crm_disconnect(business_id: str) -> dict[str, Any]:
    _bm_or_404(business_id)
    crm_store.set_bm_connection(business_id, phone_number_id=None)
    return {"ok": True}


@router.get("/api/crm/bms/{business_id}/token-check")
def crm_token_check(business_id: str) -> dict[str, Any]:
    """Testa se o token da BM ainda funciona (o temporário morre em ~24h)."""
    from core import meta_graph  # noqa: PLC0415

    bm = _bm_or_404(business_id)
    token = (bm.get("meta_token") or "").strip()
    waba = (bm.get("waba_id") or "").strip()
    if not token:
        return {"ok": False, "reason": "sem token"}
    if not waba:
        return {"ok": False, "reason": "sem waba"}
    info, err = _run(wa.get_waba_info(waba, token))
    if err:
        return {"ok": False, "dead": meta_graph.is_dead_token(err), "error": err}
    return {"ok": True, "waba_name": (info or {}).get("name")}


@router.get("/api/crm/bms/{business_id}/webhook-subscription")
def crm_webhook_sub_status(business_id: str) -> dict[str, Any]:
    """Diz se o app já está assinado aos webhooks da WABA desta BM."""
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    waba = _waba_or_400(bm)
    apps, err = _run(wa.list_subscribed_apps(waba, token))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"subscribed": len(apps) > 0, "apps": apps}


@router.post("/api/crm/bms/{business_id}/subscribe-webhook")
def crm_subscribe_webhook(business_id: str) -> dict[str, Any]:
    """Assina o app (dono do token) aos webhooks da WABA desta BM. Necessário
    para a Meta ENTREGAR mensagens/status ao endpoint /api/crm/webhook."""
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    waba = _waba_or_400(bm)
    data, err = _run(wa.subscribe_app(waba, token))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"ok": True, "waba_id": waba}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@router.get("/api/crm/bms/{business_id}/templates")
def crm_list_templates(business_id: str) -> dict[str, Any]:
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    waba = _waba_or_400(bm)
    templates, err = _run(wa.list_templates(waba, token))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"templates": templates, "count": len(templates)}


class TemplateButton(BaseModel):
    type: str          # 'QUICK_REPLY' | 'URL' | 'PHONE_NUMBER'
    text: str
    url: Optional[str] = None
    phone_number: Optional[str] = None


class CreateTemplateBody(BaseModel):
    name: str
    category: str = "MARKETING"           # MARKETING | UTILITY
    language: str = "pt_BR"
    header_text: Optional[str] = None
    body_text: str
    body_examples: Optional[list[str]] = None   # amostra p/ cada {{n}}
    footer_text: Optional[str] = None
    buttons: Optional[list[TemplateButton]] = None
    business_ids: list[str]               # enviar o MESMO template p/ estas BMs


_VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def _build_create_components(body: CreateTemplateBody) -> list[dict[str, Any]]:
    comps: list[dict[str, Any]] = []
    if (body.header_text or "").strip():
        comps.append({"type": "HEADER", "format": "TEXT", "text": body.header_text.strip()})
    body_comp: dict[str, Any] = {"type": "BODY", "text": body.body_text}
    nvars = len({int(m) for m in _VAR_RE.findall(body.body_text)})
    if nvars:
        ex = list(body.body_examples or [])
        while len(ex) < nvars:
            ex.append("exemplo")
        body_comp["example"] = {"body_text": [ex[:nvars]]}
    comps.append(body_comp)
    if (body.footer_text or "").strip():
        comps.append({"type": "FOOTER", "text": body.footer_text.strip()})
    if body.buttons:
        btns: list[dict[str, Any]] = []
        for b in body.buttons:
            if b.type == "URL":
                btns.append({"type": "URL", "text": b.text, "url": b.url or ""})
            elif b.type == "PHONE_NUMBER":
                btns.append({"type": "PHONE_NUMBER", "text": b.text, "phone_number": b.phone_number or ""})
            else:
                btns.append({"type": "QUICK_REPLY", "text": b.text})
        comps.append({"type": "BUTTONS", "buttons": btns})
    return comps


@router.post("/api/crm/templates/create")
def crm_create_template(body: CreateTemplateBody) -> dict[str, Any]:
    """Submete o MESMO template para aprovação em várias BMs de uma vez."""
    if not (body.name or "").strip():
        raise HTTPException(400, "Nome do template obrigatório.")
    if not (body.body_text or "").strip():
        raise HTTPException(400, "Corpo (body) do template obrigatório.")
    if not body.business_ids:
        raise HTTPException(400, "Selecione ao menos uma BM.")
    name = re.sub(r"[^a-z0-9_]", "_", body.name.strip().lower())
    comps = _build_create_components(body)

    async def _run_all() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for bid in body.business_ids:
            bm = store.get_bm(bid)
            if not bm:
                results.append({"business_id": bid, "ok": False, "error": "BM não encontrada"})
                continue
            token = (bm.get("meta_token") or "").strip()
            waba = (bm.get("waba_id") or "").strip()
            if not token or not waba:
                results.append({"business_id": bid, "ok": False,
                                "error": "sem token/WABA", "portfolio_name": bm.get("portfolio_name")})
                continue
            data, err = await wa.create_template(
                waba, token, name=name, category=body.category,
                language=body.language, components=comps,
            )
            results.append({
                "business_id": bid,
                "portfolio_name": bm.get("portfolio_name"),
                "ok": err is None,
                "error": err,
                "status": (data or {}).get("status"),
                "id": (data or {}).get("id"),
            })
        return results

    results = _run(_run_all())
    ok = sum(1 for r in results if r["ok"])
    return {"template_name": name, "total": len(results), "ok": ok,
            "failed": len(results) - ok, "results": results}


# ---------------------------------------------------------------------------
# Leads / tags
# ---------------------------------------------------------------------------
class LeadsAddBody(BaseModel):
    text: Optional[str] = None            # colado: "phone,nome,tag1|tag2" por linha
    leads: Optional[list[dict[str, Any]]] = None
    tags: Optional[list[str]] = None      # tags aplicadas a TODOS os leads colados


def _parse_leads_text(text: str, base_tags: list[str]) -> list[dict[str, Any]]:
    """Cada linha: telefone[,nome[,tag1|tag2]]. Aceita ; ou tab como separador."""
    out: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = re.split(r"[;,\t]", line)
        phone = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else None
        tags = list(base_tags)
        if len(parts) > 2 and parts[2].strip():
            tags += [t.strip() for t in parts[2].split("|") if t.strip()]
        out.append({"phone": phone, "name": name, "tags": tags})
    return out


@router.get("/api/crm/leads")
def crm_list_leads(tag: str = "", search: str = "") -> dict[str, Any]:
    leads = crm_store.list_leads(tag=tag or None, search=search or None)
    return {"leads": leads, "count": len(leads), "tags": crm_store.all_tags()}


@router.post("/api/crm/leads")
def crm_add_leads(body: LeadsAddBody) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if body.leads:
        for it in body.leads:
            it = dict(it)
            if body.tags:
                it["tags"] = list(set((it.get("tags") or []) + body.tags))
            items.append(it)
    if body.text:
        items += _parse_leads_text(body.text, body.tags or [])
    if not items:
        raise HTTPException(400, "Nenhum lead informado.")
    res = crm_store.add_leads(items)
    return {**res, "tags": crm_store.all_tags()}


class LeadUpdateBody(BaseModel):
    name: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


@router.patch("/api/crm/leads/{lead_id}")
def crm_update_lead(lead_id: int, body: LeadUpdateBody) -> dict[str, Any]:
    lead = crm_store.update_lead(lead_id, name=body.name, tags=body.tags, notes=body.notes)
    if not lead:
        raise HTTPException(404, "Lead não encontrado.")
    return {"lead": lead, "tags": crm_store.all_tags()}


class IdsBody(BaseModel):
    ids: list[int]


@router.post("/api/crm/leads/delete")
def crm_delete_leads(body: IdsBody) -> dict[str, Any]:
    n = crm_store.delete_leads(body.ids)
    return {"deleted": n, "tags": crm_store.all_tags()}


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------
class ParamSpec(BaseModel):
    kind: str = "static"     # 'static' | 'name' | 'phone'
    value: Optional[str] = None


class DispatchBody(BaseModel):
    business_id: str
    template_name: str
    language: str = "pt_BR"
    body_params: Optional[list[ParamSpec]] = None
    header_params: Optional[list[ParamSpec]] = None
    lead_ids: list[int]
    delay_ms: int = 350       # respiro entre envios (anti-flood)


def _resolve_param(spec: ParamSpec, lead: dict[str, Any]) -> str:
    if spec.kind == "name":
        return (lead.get("name") or "").strip() or "cliente"
    if spec.kind == "phone":
        return lead.get("phone") or ""
    return (spec.value or "").strip()


def _params_component(kind: str, specs: list[ParamSpec], lead: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not specs:
        return None
    return {
        "type": kind,
        "parameters": [
            {"type": "text", "text": _resolve_param(s, lead)} for s in specs
        ],
    }


@router.post("/api/crm/dispatch")
def crm_dispatch(body: DispatchBody) -> dict[str, Any]:
    """Dispara um template aprovado para os leads selecionados.

    Deduplica por telefone, respeita um respiro entre envios e registra cada
    mensagem no chat (com status/erro). Devolve o resumo por lead.
    """
    bm = _bm_or_404(body.business_id)
    token = _token_or_400(bm)
    conn = crm_store.get_bm_connection(body.business_id) or {}
    phone_number_id = (conn.get("phone_number_id") or "").strip()
    if not phone_number_id:
        raise HTTPException(400, "Conecte um número a esta BM antes de disparar.")
    leads = crm_store.leads_by_ids(body.lead_ids)
    if not leads:
        raise HTTPException(400, "Nenhum lead selecionado.")

    # Dedup por telefone (não dispara 2x pro mesmo número na mesma leva).
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for ld in leads:
        p = ld.get("phone") or ""
        if p and p not in seen:
            seen.add(p)
            uniq.append(ld)

    async def _run_all() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i, lead in enumerate(uniq):
            comps: list[dict[str, Any]] = []
            hc = _params_component("header", body.header_params or [], lead)
            if hc:
                comps.append(hc)
            bc = _params_component("body", body.body_params or [], lead)
            if bc:
                comps.append(bc)
            data, err = await wa.send_template_message(
                phone_number_id, token,
                to=lead["phone"], template_name=body.template_name,
                language=body.language, components=comps or None,
            )
            wamid = wa.wamid_of(data)
            crm_store.log_message(
                lead_phone=lead["phone"], business_id=body.business_id,
                phone_number_id=phone_number_id, direction="out",
                template_name=body.template_name,
                body=", ".join(_resolve_param(s, lead) for s in (body.body_params or [])) or None,
                status=("sent" if err is None else "error"),
                wamid=wamid, error=err,
            )
            if err is None:
                crm_store.touch_lead_contacted(lead["phone"])
            results.append({
                "lead_id": lead["id"], "phone": lead["phone"],
                "name": lead.get("name"), "ok": err is None,
                "wamid": wamid, "error": err,
            })
            if i < len(uniq) - 1 and body.delay_ms > 0:
                await asyncio.sleep(min(body.delay_ms, 5000) / 1000.0)
        return results

    results = _run(_run_all())
    ok = sum(1 for r in results if r["ok"])
    return {
        "business_id": body.business_id, "template": body.template_name,
        "total": len(results), "sent": ok, "failed": len(results) - ok,
        "skipped_duplicates": len(leads) - len(uniq),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Disparo MULTI-BM em paralelo (escala): divide os leads entre as BMs
# selecionadas (cada BM no seu limite de 250) e envia todas ao mesmo tempo.
# ---------------------------------------------------------------------------
class DispatchMultiBody(BaseModel):
    business_ids: list[str]
    template_name: str
    language: str = "pt_BR"
    body_params: Optional[list[ParamSpec]] = None
    header_params: Optional[list[ParamSpec]] = None
    lead_ids: list[int]
    per_bm_cap: int = 250       # teto de leads por BM nesta leva (limite Meta/24h)
    delay_ms: int = 350         # respiro DENTRO de cada BM (as BMs rodam em paralelo)


async def _send_one(
    *, phone_number_id: str, token: str, lead: dict[str, Any],
    template_name: str, language: str,
    body_params: list[ParamSpec], header_params: list[ParamSpec],
    business_id: str,
) -> dict[str, Any]:
    comps: list[dict[str, Any]] = []
    hc = _params_component("header", header_params, lead)
    if hc:
        comps.append(hc)
    bc = _params_component("body", body_params, lead)
    if bc:
        comps.append(bc)
    data, err = await wa.send_template_message(
        phone_number_id, token, to=lead["phone"],
        template_name=template_name, language=language, components=comps or None,
    )
    wamid = wa.wamid_of(data)
    crm_store.log_message(
        lead_phone=lead["phone"], business_id=business_id,
        phone_number_id=phone_number_id, direction="out",
        template_name=template_name,
        body=", ".join(_resolve_param(s, lead) for s in body_params) or None,
        status=("sent" if err is None else "error"), wamid=wamid, error=err,
    )
    if err is None:
        crm_store.touch_lead_contacted(lead["phone"])
    return {"lead_id": lead["id"], "phone": lead["phone"], "name": lead.get("name"),
            "ok": err is None, "wamid": wamid, "error": err}


@router.post("/api/crm/dispatch-multi")
def crm_dispatch_multi(body: DispatchMultiBody) -> dict[str, Any]:
    """Divide os leads entre várias BMs conectadas e dispara TODAS em paralelo.

    Cada lead vai para UMA BM (round-robin, respeitando ``per_bm_cap`` por BM).
    Dentro de cada BM os envios são sequenciais com respiro; entre BMs, paralelo.
    Ótimo para escala: 10 BMs × 250 = 2500 destinatários por leva.
    """
    if not body.business_ids:
        raise HTTPException(400, "Selecione ao menos uma BM.")
    if not body.lead_ids:
        raise HTTPException(400, "Selecione ao menos um lead.")

    # Só BMs conectadas e com token entram na leva.
    ready: list[dict[str, Any]] = []
    skipped_bms: list[dict[str, Any]] = []
    for bid in body.business_ids:
        bm = store.get_bm(bid)
        conn = crm_store.get_bm_connection(bid) or {}
        token = (bm or {}).get("meta_token") if bm else None
        pnid = conn.get("phone_number_id")
        if bm and token and pnid:
            ready.append({"business_id": bid, "token": token, "phone_number_id": pnid,
                          "name": bm.get("portfolio_name") or bid})
        else:
            skipped_bms.append({"business_id": bid, "reason": "sem token/número conectado"})
    if not ready:
        raise HTTPException(400, "Nenhuma das BMs selecionadas está conectada (token + número).")

    # Dedup leads por telefone.
    leads = crm_store.leads_by_ids(body.lead_ids)
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for ld in leads:
        p = ld.get("phone") or ""
        if p and p not in seen:
            seen.add(p)
            uniq.append(ld)

    # Distribui round-robin respeitando o teto por BM.
    n = len(ready)
    cap = max(1, int(body.per_bm_cap or 250))
    buckets: dict[str, list[dict[str, Any]]] = {r["business_id"]: [] for r in ready}
    overflow: list[dict[str, Any]] = []
    idx = 0
    for lead in uniq:
        placed = False
        for _ in range(n):
            bid = ready[idx % n]["business_id"]
            idx += 1
            if len(buckets[bid]) < cap:
                buckets[bid].append(lead)
                placed = True
                break
        if not placed:
            overflow.append(lead)

    # ---- Job em background com progresso ao vivo (não bloqueia o HTTP) --------
    assigned = len(uniq) - len(overflow)
    job_id = uuid.uuid4().hex[:12]
    with _DISPATCH_LOCK:
        _DISPATCH_JOBS[job_id] = {
            "id": job_id, "status": "running", "template": body.template_name,
            "total": assigned, "processed": 0, "sent": 0, "failed": 0,
            "bms_used": len(ready), "bms_skipped": skipped_bms,
            "skipped_duplicates": len(leads) - len(uniq),
            "overflow": len(overflow), "capacity": n * cap,
            "started_at": time.time(), "finished_at": None, "error": None,
            "by_bm": {
                r["business_id"]: {"business_id": r["business_id"], "name": r["name"],
                                   "assigned": len(buckets[r["business_id"]]),
                                   "sent": 0, "failed": 0}
                for r in ready
            },
        }
        _dispatch_prune()

    def _bump(bid: str, ok: bool) -> None:
        with _DISPATCH_LOCK:
            j = _DISPATCH_JOBS.get(job_id)
            if not j:
                return
            j["processed"] += 1
            j["sent" if ok else "failed"] += 1
            bm = j["by_bm"].get(bid)
            if bm:
                bm["sent" if ok else "failed"] += 1

    def _worker() -> None:
        async def _run_bm(r: dict[str, Any]) -> None:
            batch = buckets[r["business_id"]]
            for i, lead in enumerate(batch):
                try:
                    res = await _send_one(
                        phone_number_id=r["phone_number_id"], token=r["token"], lead=lead,
                        template_name=body.template_name, language=body.language,
                        body_params=body.body_params or [], header_params=body.header_params or [],
                        business_id=r["business_id"],
                    )
                    _bump(r["business_id"], bool(res.get("ok")))
                except Exception as exc:  # noqa: BLE001 — 1 lead ruim não derruba a leva
                    logger.error("dispatch: erro no lead (%s)", exc)
                    _bump(r["business_id"], False)
                if i < len(batch) - 1 and body.delay_ms > 0:
                    await asyncio.sleep(min(body.delay_ms, 5000) / 1000.0)

        async def _all() -> None:
            await asyncio.gather(*[_run_bm(r) for r in ready])

        try:
            asyncio.run(_all())
            status = "done"
            err = None
        except Exception as exc:  # noqa: BLE001
            logger.error("dispatch job %s falhou: %s", job_id, exc)
            status = "error"
            err = str(exc)
        with _DISPATCH_LOCK:
            j = _DISPATCH_JOBS.get(job_id)
            if j:
                j["status"] = status
                j["error"] = err
                j["finished_at"] = time.time()

    threading.Thread(target=_worker, name=f"crm-dispatch-{job_id}", daemon=True).start()

    # Devolve NA HORA — o frontend acompanha o progresso por /crm/dispatch/{id}.
    return {
        "job_id": job_id,
        "template": body.template_name,
        "bms_used": len(ready), "bms_skipped": skipped_bms,
        "total": assigned, "assigned": assigned,
        "skipped_duplicates": len(leads) - len(uniq),
        "overflow": len(overflow), "capacity": n * cap,
    }


@router.get("/api/crm/dispatch/{job_id}")
def crm_dispatch_status(job_id: str) -> dict[str, Any]:
    """Progresso ao vivo de um disparo em background."""
    with _DISPATCH_LOCK:
        j = _DISPATCH_JOBS.get(job_id)
        if not j:
            raise HTTPException(404, "Job de disparo não encontrado (ou expirado).")
        job = dict(j)
        job["by_bm"] = list(j["by_bm"].values())
    return job


# ---------------------------------------------------------------------------
# Registro de número na BM (após adicionado ao WABA): status → code → register
# ---------------------------------------------------------------------------
@router.get("/api/crm/bms/{business_id}/phones/{pnid}/status")
def crm_phone_status(business_id: str, pnid: str) -> dict[str, Any]:
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    data, err = _run(wa.get_phone_number(pnid, token))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"phone": data}


class CodeReqBody(BaseModel):
    code_method: str = "SMS"     # SMS | VOICE
    language: str = "pt_BR"


@router.post("/api/crm/bms/{business_id}/phones/{pnid}/request-code")
def crm_request_code(business_id: str, pnid: str, body: CodeReqBody) -> dict[str, Any]:
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    data, err = _run(wa.request_code(pnid, token, code_method=body.code_method, language=body.language))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"ok": True, "sent_via": body.code_method}


class VerifyCodeBody(BaseModel):
    code: str


@router.post("/api/crm/bms/{business_id}/phones/{pnid}/verify-code")
def crm_verify_code(business_id: str, pnid: str, body: VerifyCodeBody) -> dict[str, Any]:
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    data, err = _run(wa.verify_code(pnid, token, code=body.code))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"ok": True}


class RegisterBody(BaseModel):
    pin: str            # PIN de verificação em 2 etapas (6 dígitos)


@router.post("/api/crm/bms/{business_id}/phones/{pnid}/register")
def crm_register_phone(business_id: str, pnid: str, body: RegisterBody) -> dict[str, Any]:
    bm = _bm_or_404(business_id)
    token = _token_or_400(bm)
    if not re.fullmatch(r"\d{6}", (body.pin or "").strip()):
        raise HTTPException(400, "O PIN deve ter 6 dígitos.")
    data, err = _run(wa.register_phone(pnid, token, pin=body.pin))
    if err:
        raise HTTPException(502, f"Cloud API: {err}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhook (respostas dos leads + status de entrega) — endpoint PÚBLICO.
# GET  = verificação (hub.challenge). POST = eventos (mensagens/status).
# ---------------------------------------------------------------------------
class WebhookConfigBody(BaseModel):
    verify_token: Optional[str] = None
    app_secret: Optional[str] = None
    public_url: Optional[str] = None      # base pública do túnel (sem barra final)


@router.get("/api/crm/webhook-config")
def crm_webhook_config_get() -> dict[str, Any]:
    s = store.get_settings()
    pub = (s.get(_WH_PUBLIC_KEY) or "").rstrip("/")
    return {
        "has_verify_token": bool(s.get(_WH_VERIFY_KEY)),
        "has_app_secret": bool(s.get(_WH_SECRET_KEY)),
        "verify_token": s.get(_WH_VERIFY_KEY) or "",   # single-user local: ok exibir
        "public_url": pub,
        "path": "/api/crm/webhook",
        "webhook_url": (pub + "/api/crm/webhook") if pub else "",
    }


@router.post("/api/crm/webhook-config")
def crm_webhook_config_set(body: WebhookConfigBody) -> dict[str, Any]:
    store.set_settings({
        _WH_VERIFY_KEY: (body.verify_token or "").strip() or None,
        _WH_SECRET_KEY: (body.app_secret or "").strip() or None,
        _WH_PUBLIC_KEY: (body.public_url or "").strip().rstrip("/") or None,
    })
    return {"ok": True}


@router.get("/api/crm/webhook")
def crm_webhook_verify(request: Request) -> Any:
    """Verificação do webhook (Meta): ecoa hub.challenge se o verify_token casar."""
    q = request.query_params
    mode = q.get("hub.mode")
    token = q.get("hub.verify_token")
    challenge = q.get("hub.challenge")
    expected = store.get_setting(_WH_VERIFY_KEY)
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "verify_token inválido.")


@router.post("/api/crm/webhook")
async def crm_webhook_receive(request: Request) -> dict[str, Any]:
    """Recebe eventos do WhatsApp: mensagens recebidas + status de entrega.

    Valida a assinatura X-Hub-Signature-256 (HMAC-SHA256 do corpo cru com o app
    secret) quando o secret está configurado. Best-effort: nunca levanta 500.
    """
    raw = await request.body()
    secret = store.get_setting(_WH_SECRET_KEY)
    if secret:
        sig = request.headers.get("x-hub-signature-256") or ""
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            logger.warning("webhook: assinatura inválida — ignorando evento.")
            raise HTTPException(403, "assinatura inválida")
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {"ok": True}
    try:
        for entry in data.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                pnid = ((value.get("metadata") or {}).get("phone_number_id"))
                bid = crm_store.bm_for_phone_number_id(pnid) if pnid else None
                # Nome do contato (perfil do WhatsApp) por telefone.
                names: dict[str, str] = {}
                for c in value.get("contacts", []) or []:
                    wa_id = c.get("wa_id")
                    nm = ((c.get("profile") or {}).get("name"))
                    if wa_id and nm:
                        names[crm_store.only_phone_digits(wa_id)] = nm
                # Mensagens recebidas.
                for msg in value.get("messages", []) or []:
                    frm = crm_store.only_phone_digits(msg.get("from") or "")
                    body_txt = None
                    mtype = msg.get("type")
                    if mtype == "text":
                        body_txt = (msg.get("text") or {}).get("body")
                    elif mtype in ("button", "interactive"):
                        body_txt = json.dumps(msg.get(mtype) or {}, ensure_ascii=False)[:400]
                    else:
                        body_txt = f"[{mtype}]"
                    crm_store.record_incoming(
                        lead_phone=frm, business_id=bid, phone_number_id=pnid,
                        body=body_txt, wamid=msg.get("id"), name=names.get(frm),
                    )
                # Status de entrega das mensagens que ENVIAMOS.
                for st in value.get("statuses", []) or []:
                    crm_store.update_status_by_wamid(st.get("id") or "", st.get("status") or "")
    except Exception as exc:  # noqa: BLE001 — webhook nunca deve devolver 500
        logger.error("webhook: erro processando evento (%s).", exc)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat / mensagens / stats
# ---------------------------------------------------------------------------
@router.get("/api/crm/chats")
def crm_chats() -> dict[str, Any]:
    return {"chats": crm_store.list_chats()}


@router.get("/api/crm/chats/{phone}")
def crm_chat_messages(phone: str) -> dict[str, Any]:
    return {"phone": phone, "messages": crm_store.list_messages(phone)}


@router.get("/api/crm/stats")
def crm_stats() -> dict[str, Any]:
    return crm_store.messages_stats()


def register_crm_routes(app: Any) -> None:
    """Inclui as rotas do CRM no app FastAPI (chamado no api_server)."""
    app.include_router(router)
