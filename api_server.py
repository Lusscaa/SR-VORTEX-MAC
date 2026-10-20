"""Backend FastAPI do painel novo (React).

Consome o core já existente — NÃO reimplementa lógica:
  * ``core.store``       — runs, BMs, pools (CNPJs/contas) em SQLite.
  * ``core.job_manager`` — pool de workers paralelos + logs por-run.
  * ``core.run_service`` — cascata headless (CNPJ→…→run) por job.
  * ``core.batch_io``    — parse de planilhas (CSV/XLSX).
  * ``core.meta_graph``  — status de verificação (2k) das BMs.

A infra (AdsPower/Cloudflare/SMS) vem do ``config`` (.env), já que o backend
não tem a sidebar do Streamlit. Uma página de Settings pode sobrescrever isso
depois (TODO). Rode com: ``uvicorn api_server:app --port 8000``.
"""
from __future__ import annotations

import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import logging
import re
import threading
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse
from pydantic import BaseModel

from core import BrasilApiError, auth, batch_io, meta_graph, run_service, store
from core import run_control
from core.api_clients import Sms24hClient, Sms24hError
from core.adspower_client import AdsPowerClient, build_tagged_profile_name
from core.cnpj_lookup import fetch_cnpj_cached
from core.job_manager import JobManager, read_run_log
from core.run_service import JobSpec
from core.step_checkpoint import save_checkpoint, get_checkpoint, clear_checkpoint
from config import NUMERO_VIRTUAL_TARGET_PRICE
from utils.helpers import only_digits
from utils.autopilot_state import load_autopilot, save_autopilot, advance_cycle

logger = logging.getLogger("meta_automation.api")

app = FastAPI(title="Meta Farm API", version="1.0")

# ---------------------------------------------------------------------------
# Auto-refresh de verificação: consulta a Graph API periodicamente para
# atualizar o status das BMs (verificada / em análise / rejeitada).
# ---------------------------------------------------------------------------
_BM_REFRESH_INTERVAL_S = 4 * 3600  # 4 horas


_last_bm_refresh: dict[str, Any] = {}


async def _bm_auto_refresh_loop() -> None:
    """Background: atualiza verificação a cada _BM_REFRESH_INTERVAL_S."""
    import time as _time
    await asyncio.sleep(60)
    while True:
        try:
            summary = await meta_graph.refresh_bm_verification_async()
            _last_bm_refresh.update(summary=summary, ts=_time.time(), error=None)
            logger.info("[AUTO-REFRESH] verificação BMs: %s", summary)
        except Exception as exc:  # noqa: BLE001
            _last_bm_refresh.update(summary=None, ts=_time.time(), error=str(exc))
            logger.debug("[AUTO-REFRESH] falha no refresh", exc_info=True)
        await asyncio.sleep(_BM_REFRESH_INTERVAL_S)


@app.on_event("startup")
async def _start_background_tasks() -> None:
    asyncio.create_task(_bm_auto_refresh_loop())
    _reap_orphaned_runs()


def _reap_orphaned_runs() -> None:
    """Marca runs 'running' órfãs como 'failed' no boot.

    Se o backend reiniciou, as threads que controlavam essas runs morreram.
    Sem isto, ficam 'running' para sempre e travam slots do autopilot.
    """
    try:
        orphans = store.list_runs(status="running", limit=100)
        for run in orphans:
            rid = run.get("run_id")
            if rid:
                store.update_run(rid, status=store.RUN_FAILED, error="run órfã (backend reiniciou)")
                logger.warning("[WATCHDOG] run %s era 'running' no boot → marcada 'failed'.", rid)
        if orphans:
            logger.info("[WATCHDOG] %d run(s) órfã(s) limpas no boot.", len(orphans))
    except Exception:  # noqa: BLE001
        logger.debug("[WATCHDOG] falha ao limpar runs órfãs", exc_info=True)


# Rotas /api/* públicas (não exigem token): login + healthcheck + webhook do
# WhatsApp (a Meta chama sem Bearer; a segurança é o verify_token no GET e a
# assinatura X-Hub-Signature-256 no POST, validados dentro do handler).
_PUBLIC_PATHS = {"/api/login", "/api/health", "/api/crm/webhook"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Protege TODA rota ``/api/*`` com Bearer token, exceto as públicas.

    Registrado ANTES do CORS de propósito: o CORS fica por fora (outermost),
    então preflights OPTIONS e os headers CORS continuam funcionando mesmo nas
    respostas 401. Caminhos fora de ``/api/`` (docs, estáticos) passam direto."""
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or not path.startswith("/api/")
        or path in _PUBLIC_PATHS
    ):
        return await call_next(request)

    token = auth.token_from_header(request.headers.get("authorization"))
    if not token:
        return JSONResponse({"detail": "Não autenticado"}, status_code=401)
    try:
        auth.decode_token(token)
    except auth.AuthError:
        return JSONResponse({"detail": "Token inválido ou expirado"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # app local single-user; libera o dev server do Vite
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singleton do motor paralelo (vive enquanto o processo do backend roda).
# Default 10 — o operador ajusta via UI (setConcurrency); o limite real é
# MAX_CONCURRENCY. O valor inicial alto evita que runs fiquem na fila quando
# o servidor reinicia antes de o operador configurar o painel.
_manager = JobManager(concurrency=10)

MAX_CONCURRENCY = 50  # sem limite prático — o operador controla


# Settings que o usuário insere no painel (sobrescrevem o .env). Secretas são
# mascaradas no GET; as demais (id de conta) voltam inteiras.
# SMS serviço/país NÃO são editáveis: sempre fb/br (default no config.py).
SETTING_KEYS = [
    "adspower_api_key", "cloudflare_account_id", "cloudflare_api_token",
    "cloudflare_worker_name", "default_template",
]
_SECRET_KEYS = {"adspower_api_key", "cloudflare_api_token"}


def _infra() -> dict[str, Any]:
    """Infra compartilhada dos jobs: settings do painel sobre config/.env.

    Prioridade para credenciais Cloudflare:
      1. Pool multi-conta (cloudflare_accounts): primeira conta com remaining > 0
      2. Settings legados (cloudflare_account_id / cloudflare_api_token)
      3. Variáveis de ambiente (CLOUDFLARE_ACCOUNT_IDS / CLOUDFLARE_API_TOKENS)
    """
    import config as cfg

    s = store.get_settings()
    accts = getattr(cfg, "CLOUDFLARE_ACCOUNT_IDS", None) or []
    toks = getattr(cfg, "CLOUDFLARE_API_TOKENS", None) or []

    # --- Cloudflare: tenta pool multi-conta primeiro ---
    active_cf = store.get_active_cloudflare_account()
    cf_account_id = (
        (active_cf["account_id"] if active_cf else None)
        or s.get("cloudflare_account_id")
        or (accts[0] if accts else None)
    )
    cf_api_token = (
        (active_cf["api_token"] if active_cf else None)
        or s.get("cloudflare_api_token")
        or (toks[0] if toks else None)
    )

    # --- Netlify: domínio ativo do pool (host atual). ---
    active_nf = store.get_active_netlify_domain()
    nf_domain = (active_nf["domain"] if active_nf else None) or None
    nf_token = (active_nf["api_token"] if active_nf else None) or getattr(cfg, "NETLIFY_API_TOKEN", None) or None

    return {
        "adspower_mode": "create",
        "adspower_api_key": s.get("adspower_api_key") or getattr(cfg, "ADSPOWER_API_KEY", None) or None,
        "netlify_domain": nf_domain,
        "netlify_api_token": nf_token,
        "cloudflare_account_id": cf_account_id,
        "cloudflare_api_token": cf_api_token,
        # Subdomínio workers.dev informado manualmente na conta ativa (fallback
        # quando o token não lê pela API). Vazio se não informado.
        "cloudflare_subdomain": (active_cf.get("subdomain") if active_cf else None) or None,
        # Worker script name (CLOUDFLARE_SCRIPT_NAME): COMPARTILHADO entre as
        # contas. Era forçado a None — então account_id+token da sidebar nunca
        # habilitavam o deployer (enabled exige os 3). Resolve do painel ou .env.
        # 'nome base' não é mais pedido ao operador: em domínio workers.dev o
        # nome do script é DERIVADO do domínio e o subdomínio vem da API. Mantém
        # um placeholder não-vazio só p/ os checks internos (ui_override/enabled)
        # — o valor em si é ignorado no deploy workers.dev.
        "cloudflare_worker_name": (
            s.get("cloudflare_worker_name")
            or getattr(cfg, "CLOUDFLARE_SCRIPT_NAME", None)
            or "worker"
        ),
        # SMS sempre fb/br — não editável no painel (default no config.py).
        "sms_service_code": getattr(cfg, "SMS24H_SERVICE_CODE", None) or None,
        "sms_country_code": getattr(cfg, "SMS24H_COUNTRY_CODE", None) or None,
    }


def _mask(tok: Optional[str]) -> Optional[str]:
    return f"{tok[:6]}…({len(tok)})" if tok else None


def _require_cloudflare_filled() -> None:
    """Aborta o disparo (HTTP 400) se não houver domínio Netlify PRONTO.

    Checagem de PRESENÇA (rápida): exige ao menos 1 domínio com cota livre e
    nameservers já propagados. A validade ao vivo é barrada no run_service antes
    do SMS + no preflight. (Nome mantido por compatibilidade dos call sites.)"""
    if store.get_hosting_provider() == "cloudflare":
        active_cf = store.get_active_cloudflare_domain()
        if not active_cf:
            domains = store.list_cloudflare_domains()
            if not domains:
                raise HTTPException(
                    400,
                    "Nenhum domínio Cloudflare cadastrado. Adicione um domínio + "
                    "Account ID + API Token em Configurações > Cloudflare e configure "
                    "os nameservers na Hostinger.",
                )
            raise HTTPException(
                400,
                "Nenhum domínio Cloudflare disponível: todos atingiram o limite (cap) "
                "ou os nameservers ainda não propagaram. Adicione outro domínio ou "
                "verifique a propagação dos NS na Hostinger.",
            )
        return
    active = store.get_active_netlify_domain()
    if not active:
        # Distingue 'nenhum domínio' de 'todos cheios/sem propagar' para a msg.
        domains = store.list_netlify_domains()
        if not domains:
            raise HTTPException(
                400,
                "Nenhum domínio Netlify cadastrado. Adicione um domínio + token "
                "em Configurações > Domínios e configure os nameservers na Hostinger.",
            )
        raise HTTPException(
            400,
            "Nenhum domínio Netlify disponível: todos atingiram o limite (cap) ou "
            "ainda estão com os nameservers sem propagar. Adicione outro domínio "
            "ou verifique a propagação dos NS na Hostinger.",
        )
    # Verifica se ainda há slots disponíveis na credencial ativa.
    # REMOVIDO: Agora todas as contas CF são usadas independentemente do limite
    # usage = _cf_usage_view()
    # if usage.get("remaining", 1) <= 0:
    #     raise HTTPException(...)


# ---------------------------------------------------------------------------
# Auth (públicas — fora do gate do middleware)
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, str]:
    """Healthcheck público (sem token) — usado por load balancer / probes."""
    return {"status": "ok"}


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(req: LoginReq) -> dict[str, Any]:
    """Login do operador. 200 {token, expires_in} | 401 em credencial errada."""
    try:
        ok = auth.verify_credentials(req.username, req.password)
    except auth.AuthError as exc:
        # Auth mal configurada no servidor (env faltando) — 500, não 401.
        raise HTTPException(500, str(exc))
    if not ok:
        raise HTTPException(401, "Usuário ou senha inválidos")
    token, expires_in = auth.create_token(req.username)
    return {"token": token, "expires_in": expires_in}


# ---------------------------------------------------------------------------
# Dashboard / leitura
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats(period: str = "total", date: str = "") -> dict[str, Any]:
    """Dashboard stats. Aceita período: 'total', 'dia', 'semana', 'mês'.

    Ou specific_date em formato YYYY-MM-DD para filtrar um dia específico.
    """
    return {
        "runs": store.run_counts(period=period, specific_date=date),
        # consistente com /api/bms: card conta só BMs de runs 100%.
        "bms": store.bm_counts(success_only=True, period=period, specific_date=date),
        "pools": store.pool_counts(),
        "projection": store.pool_drain_projection(),
        # Relatório de falhas por motivo (ao vivo) — seção fixa do painel.
        "failures": store.failure_breakdown(period=period, specific_date=date),
        "jobs": {
            "active": _manager.active_count(),
            "concurrency": _manager.concurrency,
        },
        "autopilot": load_autopilot(),
    }


@app.get("/api/runs")
def runs(status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    out = []
    _acc_cache: dict[str, dict] = {}
    for r in store.list_runs(status=status, limit=limit):
        r["meta_token"] = _mask(r.get("meta_token"))
        # Junta credenciais da conta (senha, 2FA, email) pelo login (label) —
        # ferramenta local, o operador precisa disso no Histórico p/ investigar.
        email = (r.get("label") or "").strip()
        if email:
            acc = _acc_cache.get(email)
            if acc is None:
                acc = store.get_fb_account(email) or {}
                _acc_cache[email] = acc
            r["account_password"] = acc.get("password") or ""
            r["account_totp_seed"] = acc.get("totp_seed") or ""
            r["account_email_address"] = acc.get("email_address") or ""
            r["account_email_password"] = acc.get("email_password") or ""
        out.append(r)
    return out


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "run não encontrado")
    r["meta_token"] = _mask(r.get("meta_token"))
    return r


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: str, tail: int = 500) -> dict[str, Any]:
    return {"run_id": run_id, "log": read_run_log(run_id, tail=tail)}


@app.post("/api/runs/{run_id}/retry")
def retry_run(run_id: str) -> dict[str, Any]:
    """Reinicia uma run com MESMO login mas NOVO CNPJ + NOVO perfil Adspower.

    Fluxo:
    1. Libera a conta e o CNPJ antigos (marcando-os como available)
    2. Toma a MESMA conta de volta (reusa o email/senha/2FA)
    3. Reserva um NOVO CNPJ do pool
    4. Cria um novo perfil no Adspower para o novo CNPJ
    5. Enfileira uma nova run com essa combinação

    Evita reutilizar contas com muitos erros — se a conta foi marcada com
    "wrong_password" ou similar, a tomada falhará.
    """
    _require_cloudflare_filled()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    cnpj = run.get("cnpj")
    email = run.get("label")
    if not cnpj or not email:
        raise HTTPException(
            400,
            "Run sem CNPJ/conta associada — não dá para reiniciar "
            "automaticamente (rodou fora do fluxo normal de pool).",
        )

    # 1. Descarta CNPJ ANTIGO permanentemente
    store.discard_cnpj(cnpj)
    logger.info(f"[RESTART] CNPJ {cnpj} descartado permanentemente")

    # 2. Libera CONTA (pode ser reutilizada com novo CNPJ)
    acc = store.force_release_fb_account(email)
    was_running = run.get("status") == store.RUN_RUNNING

    if not acc:
        raise HTTPException(
            409,
            f"Conta '{email}' não consegui liberar (provavelmente descartada/"
            "bloqueada). Use outra conta manualmente.",
        )

    # 2. Marca a run antiga como cancelada
    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="cancelada — reiniciada com novo CNPJ e perfil (ver run nova)",
    )

    # 3. Toma a MESMA conta (se falhar, quer dizer que foi descartada/bloqueada)
    acc = store.take_fb_account(acc["email"])
    if not acc:
        raise HTTPException(
            409,
            f"Não consegui tomar conta '{email}' novamente — foi descartada/"
            "bloqueada. Tente com outra conta.",
        )

    # 4. Reserva um NOVO CNPJ (excluindo o antigo)
    new_cnpj_digits = store.reserve_one_cnpj(exclude={only_digits(cnpj)})
    if not new_cnpj_digits:
        raise HTTPException(409, "Sem CNPJs disponíveis no pool para reiniciar.")
    new_cnp = store.get_cnpj_by_digits(new_cnpj_digits)
    if not new_cnp:
        raise HTTPException(409, f"CNPJ {new_cnpj_digits} não encontrado.")

    # 5. Marca o CNPJ novo como "em uso" (já feito por reserve_one_cnpj)
    spec = JobSpec(
        cnpj=new_cnp["cnpj"],
        meta_email_or_phone=acc["email"],
        meta_password=acc.get("password") or "",
        meta_totp_seed=acc.get("totp_seed") or "",
        meta_email_address=acc.get("email_address") or "",
        meta_email_password=acc.get("email_password") or "",
        meta_recovery_email=acc.get("recovery_email") or "",
        **_infra(),
    )
    new_id = _manager.submit(
        run_service.execute_job, spec=spec, cnpj=new_cnp["cnpj"], label=acc["email"]
    )
    return {
        "old_run_id": run_id,
        "new_run_id": new_id,
        "same_account": acc["email"],
        "old_cnpj": cnpj,
        "new_cnpj": new_cnp["cnpj"],
        "warning": (
            "a run antiga pode ainda estar viva em background (thread não "
            "matável) — acompanhe se os logs das duas colidem."
            if was_running else None
        ),
    }


@app.post("/api/runs/{run_id}/rerun-same")
def rerun_same_pair(run_id: str) -> dict[str, Any]:
    """CONTINUAR de onde parou com o MESMO CNPJ (não descarta nada).

    Diferente do 'Reiniciar' (que puxa um CNPJ NOVO e cria outra BM), aqui o par
    é o MESMO — enfileira uma run NOVA com a mesma conta + mesmo CNPJ. O pipeline
    reaproveita o perfil AdsPower (idempotente por CNPJ), a BM já criada e o
    domínio já verificado (flags de idempotência) e segue do ponto que travou
    (ex.: WhatsApp depois que o Meta finalmente verificar o domínio).

    Use isto quando a run PAROU (ex.: 'domínio ainda não verificado pelo Meta')
    e você quer voltar mais tarde sem perder o que já foi feito."""
    _require_cloudflare_filled()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    cnpj = run.get("cnpj")
    email = run.get("label")
    if not cnpj or not email:
        raise HTTPException(
            400,
            "Run sem CNPJ/conta associada — não dá para continuar "
            "(rodou fora do fluxo normal de pool).",
        )
    was_running = run.get("status") == store.RUN_RUNNING

    # Re-toma a MESMA conta (mantém email/senha/2FA). NÃO descarta o CNPJ.
    store.force_release_fb_account(email)
    acc = store.take_fb_account(email)
    if not acc:
        raise HTTPException(
            409,
            f"Não consegui tomar a conta '{email}' de volta "
            "(descartada/bloqueada?). Use 'Resgatar Conta' antes de continuar.",
        )
    # Re-reserva o MESMO CNPJ (continuação legítima do próprio par).
    cnp = store.take_cnpj(cnpj)
    if not cnp:
        raise HTTPException(409, f"CNPJ '{cnpj}' não encontrado no pool.")

    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="continuada — mesma conta + mesmo CNPJ (ver run nova).",
    )

    spec = JobSpec(
        cnpj=cnp["cnpj"],
        meta_email_or_phone=acc["email"],
        meta_password=acc.get("password") or "",
        meta_totp_seed=acc.get("totp_seed") or "",
        meta_email_address=acc.get("email_address") or "",
        meta_email_password=acc.get("email_password") or "",
        meta_recovery_email=acc.get("recovery_email") or "",
        **_infra(),
    )
    new_id = _manager.submit(
        run_service.execute_job, spec=spec, cnpj=cnp["cnpj"], label=acc["email"]
    )
    return {
        "old_run_id": run_id,
        "new_run_id": new_id,
        "same_account": acc["email"],
        "cnpj": cnp["cnpj"],
        "warning": (
            "a run antiga pode ainda estar viva em background — acompanhe se os "
            "logs das duas colidem." if was_running else None
        ),
    }


@app.post("/api/runs/{run_id}/continuar-bm")
def continuar_bm(run_id: str) -> dict[str, Any]:
    """CONTINUAR A MESMA BM: refaz só o que falta (conta de anúncios + verificação).

    Diferente de 'Continuar (mesmo CNPJ)', que confia 100% nas flags de
    idempotência do perfil, aqui as flags dos passos que AINDA NÃO ENTREGARAM
    ativo são LIMPAS antes de enfileirar — hoje só ``ad_account_ok``. Motivo:
    em produção 2026-09 o passo 4 gravou ``ad_account_ok`` por falso-positivo
    (leu o LOGIN da conta do Facebook como se fosse uma conta de anúncios) e,
    com a flag no perfil, toda retomada PULAVA a criação e a BM ficava sem
    conta de anúncios para sempre.

    O que é PRESERVADO (não se refaz o que já está pronto): o portfólio/BM já
    criado, ``details_ok`` (informações da empresa) e ``domain_verified_ok``
    (domínio verificado). Conta do Facebook, CNPJ e perfil AdsPower são os
    MESMOS — nenhum CNPJ novo é consumido.
    """
    from utils.profiles_state import find_profile_by_cnpj, set_profile_flag

    # 404 ANTES do gate de infra: run inexistente é 404, não "falta Cloudflare".
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    _require_cloudflare_filled()
    cnpj = run.get("cnpj")
    email = run.get("label")
    if not cnpj or not email:
        raise HTTPException(
            400,
            "Run sem CNPJ/conta associada — não dá para continuar a BM "
            "(rodou fora do fluxo normal de pool).",
        )
    if run.get("status") == store.RUN_RUNNING:
        raise HTTPException(
            409,
            "Esta run ainda está RODANDO — use 'Encerrar' ou 'Assumir' antes "
            "de continuar a BM, senão as duas brigam pelo mesmo perfil.",
        )

    # Re-toma a MESMA conta e o MESMO CNPJ (mesmo que o settle os tenha
    # marcado como usados/descartados — continuar a própria BM é legítimo).
    store.force_release_fb_account(email)
    acc = store.take_fb_account(email)
    if not acc:
        raise HTTPException(
            409,
            f"Não consegui tomar a conta '{email}' de volta "
            "(descartada/bloqueada?). Use 'Resgatar Conta' antes de continuar.",
        )
    cnp = store.take_cnpj(cnpj)
    if not cnp:
        raise HTTPException(409, f"CNPJ '{cnpj}' não encontrado no pool.")

    # Limpa as flags dos passos que precisam ser REFEITOS. O passo 4 detecta
    # sozinho uma conta de anúncios que já exista de verdade (link act_… na
    # lista de ativos) e pula — então limpar a flag não cria conta duplicada.
    perfil_antes = find_profile_by_cnpj(cnpj) or {}
    flags_limpas = []
    for flag in ("ad_account_ok",):
        if perfil_antes.get(flag):
            set_profile_flag(cnpj, flag, False)
            flags_limpas.append(flag)

    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="continuada — mesma BM (conta de anúncios + verificação; ver run nova).",
    )

    spec = JobSpec(
        cnpj=cnp["cnpj"],
        meta_email_or_phone=acc["email"],
        meta_password=acc.get("password") or "",
        meta_totp_seed=acc.get("totp_seed") or "",
        meta_email_address=acc.get("email_address") or "",
        meta_email_password=acc.get("email_password") or "",
        meta_recovery_email=acc.get("recovery_email") or "",
        **_infra(),
    )
    new_id = _manager.submit(
        run_service.execute_job, spec=spec, cnpj=cnp["cnpj"], label=acc["email"]
    )
    return {
        "old_run_id": run_id,
        "new_run_id": new_id,
        "same_account": acc["email"],
        "cnpj": cnp["cnpj"],
        "business_id": run.get("business_id"),
        "flags_limpas": flags_limpas,
        "preservado": [
            k for k in ("details_ok", "domain_verified_ok", "locale_ok")
            if perfil_antes.get(k)
        ],
    }


def _delete_profile_encerrada(
    user_id: Optional[str],
    api_key: Optional[str],
    browser_provider: str = "adspower",
) -> bool:
    """Botão Encerrar: EXCLUI o perfil do multi-login (run encerrada não fica
    no painel — só os 'em análise' permanecem). delete_profile fecha o perfil
    antes de apagar. Best-effort, síncrono (worker thread do FastAPI); funciona
    esteja a run rodando ou não."""
    if not user_id:
        return False

    async def _do() -> bool:
        if browser_provider == "ixbrowser":
            from core.ixbrowser_client import IXBrowserClient
            client = IXBrowserClient()
            return await client.delete_profile(user_id)
        client = AdsPowerClient(api_key=api_key or "")
        return await client.delete_profile(user_id)

    try:
        return asyncio.run(_do())
    except Exception as exc:  # noqa: BLE001
        logger.warning("terminate: falha ao excluir perfil (id=%s, provider=%s): %s", user_id, browser_provider, exc)
        return False


@app.post("/api/runs/{run_id}/terminate")
def terminate_run(run_id: str) -> dict[str, Any]:
    """ENCERRA uma run de vez (botão 'Encerrar'): para o bot, descarta a conta
    E o CNPJ (saem do pool, não puxam mais) e marca a run como cancelada.

    A parada é COOPERATIVA (a thread não é matável): registra o pedido em
    ``run_control`` e o runner para no próximo checkpoint, fechando o perfil
    AdsPower. Os recursos são descartados aqui mesmo, na hora, pra não serem
    re-arrendados enquanto a thread ainda desenrola."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    run_control.request_stop(run_id, run_control.TERMINATE)
    email = run.get("label")
    cnpj = run.get("cnpj")
    acc_status = store.discard_fb_account(email) if email else None
    cnpj_status = store.discard_cnpj(cnpj) if cnpj else None
    profile_deleted = _delete_profile_encerrada(
        run.get("profile_user_id"), _infra().get("adspower_api_key"),
        browser_provider=run.get("browser_provider") or "adspower",
    )
    was_running = run.get("status") == store.RUN_RUNNING
    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="encerrada pelo operador — conta e CNPJ descartados (não puxam mais).",
    )
    return {
        "run_id": run_id,
        "account_discarded": bool(acc_status),
        "cnpj_discarded": bool(cnpj_status),
        "profile_deleted": bool(profile_deleted),
        "warning": (
            "a run ainda pode estar viva em background por alguns segundos até "
            "parar no próximo checkpoint (thread não matável)."
            if was_running else None
        ),
    }


@app.post("/api/runs/{run_id}/release-account")
def release_account_run(run_id: str) -> dict[str, Any]:
    """DEVOLVE a conta ao pool (botão 'Voltar Conta'): para o bot, mas
    LIBERA a conta sem DESCARTAR (status volta a 'available'). O CNPJ é
    descartado (não pode ser reutilizado com a mesma conta).

    Usado quando há um erro pontual (não é da conta): crosspost bloqueado,
    limite de criação, etc. A conta volta pro pool pra ser testada com outro CNPJ.

    A parada é COOPERATIVA — o runner para no próximo checkpoint, fechando o
    perfil AdsPower e devolvendo a conta."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")

    email = run.get("label")
    cnpj = run.get("cnpj")

    if not email:
        raise HTTPException(400, "Run sem email da conta — não dá para liberar.")

    # LIBERA a conta PRIMEIRO (antes de parar o runner)
    # Isso garante que o accounting não reverta o status
    acc_status = store.force_release_fb_account(email)

    if not acc_status:
        # Verifica por que falhou
        with store._conn() as conn:
            row = conn.execute(
                "SELECT status FROM fb_accounts WHERE lower(email) = ? LIMIT 1",
                (email.lower(),)
            ).fetchone()

        if not row:
            raise HTTPException(
                409,
                f"Conta '{email}' não está no estoque (pode ter sido deletada)."
            )
        elif row["status"] == store.POOL_DISCARDED:
            raise HTTPException(
                409,
                f"Conta '{email}' foi DESCARTADA permanentemente — não pode ser recuperada. "
                "Use outra conta."
            )
        elif row["status"] == store.POOL_PENDING_DISCARD:
            # Erro terminal que o operador REVISOU e decidiu recuperar (ex.: a
            # run caiu por um bug do bot, não por culpa da conta). force_release
            # recusa pending_discard de propósito (p/ fluxos automáticos não
            # reviverem); aqui é ação MANUAL do operador, então libera de fato.
            with store._POOL_LOCK, store._conn() as conn:
                conn.execute(
                    "UPDATE fb_accounts SET status = ?, used_by_run = NULL, "
                    "remark = NULL, updated_at = ? WHERE lower(email) = ?",
                    (store.POOL_AVAILABLE, store._now(), email.lower()),
                )
            acc_status = {"email": email, "status": store.POOL_AVAILABLE}
            logger.info(
                "release-account: conta %s estava PENDING_DISCARD — recuperada "
                "por decisão do operador.", email,
            )
        else:
            raise HTTPException(
                500,
                f"Falha ao liberar conta '{email}' (status: {row['status']}) — contate o suporte."
            )

    # Agora para o runner com RELEASE_ACCOUNT (não reverte a conta)
    run_control.request_stop(run_id, run_control.RELEASE_ACCOUNT)
    # Descarta o CNPJ (não pode ser reutilizado com essa conta)
    cnpj_status = store.discard_cnpj(cnpj) if cnpj else None

    profile_deleted = _delete_profile_encerrada(
        run.get("profile_user_id"), _infra().get("adspower_api_key"),
        browser_provider=run.get("browser_provider") or "adspower",
    )
    was_running = run.get("status") == store.RUN_RUNNING
    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="encerrada pelo operador — conta DEVOLVIDA ao pool, CNPJ descartado.",
    )
    return {
        "run_id": run_id,
        "account_released": bool(acc_status),
        "account_email": acc_status.get("email") if acc_status else None,
        "cnpj_discarded": bool(cnpj_status),
        "profile_deleted": bool(profile_deleted),
        "message": f"Conta '{email}' devolvida ao pool (disponível para reusar).",
        "warning": (
            "a run ainda pode estar viva em background por alguns segundos até "
            "parar no próximo checkpoint (thread não matável)."
            if was_running else None
        ),
    }


@app.post("/api/runs/{run_id}/recover-account-cnpj")
def recover_account_cnpj_run(run_id: str) -> dict[str, Any]:
    """RECUPERA CONTA + CNPJ (botão 'Recuperar Conta + CNPJ'): para o bot,
    devolve a CONTA ao pool E devolve o CNPJ ao pool (available) — em vez de
    descartar o CNPJ como o 'Voltar Conta' faz.

    Usado quando a run falhou SEM criar a BM (ex.: AdsPower fechado, proxy,
    pré-flight): nada foi usado na Meta, então nem a conta nem o CNPJ devem ser
    perdidos. Fecha o perfil AdsPower."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    email = run.get("label")
    cnpj = run.get("cnpj")
    if not email:
        raise HTTPException(400, "Run sem email da conta — não dá para recuperar.")

    # 1) Libera a conta (mesma lógica robusta do release-account, inclui resgatar
    #    de PENDING_DISCARD por decisão manual do operador).
    acc_status = store.force_release_fb_account(email)
    if not acc_status:
        with store._conn() as conn:
            row = conn.execute(
                "SELECT status FROM fb_accounts WHERE lower(email) = ? LIMIT 1",
                (email.lower(),),
            ).fetchone()
        if row and row["status"] in (store.POOL_PENDING_DISCARD, store.POOL_DISCARDED):
            with store._POOL_LOCK, store._conn() as conn:
                conn.execute(
                    "UPDATE fb_accounts SET status = ?, used_by_run = NULL, "
                    "remark = NULL, updated_at = ? WHERE lower(email) = ?",
                    (store.POOL_AVAILABLE, store._now(), email.lower()),
                )
            acc_status = {"email": email, "status": store.POOL_AVAILABLE}

    # 2) Para o runner sem reverter a conta.
    run_control.request_stop(run_id, run_control.RELEASE_ACCOUNT)

    # 3) DEVOLVE os DOIS CNPJs ao pool (primário + 2º da feature 2-BMs).
    cnpj2 = run.get("cnpj2")
    cnpj_status = store.restore_cnpj(cnpj) if cnpj else None
    cnpj2_status = store.restore_cnpj(cnpj2) if cnpj2 else None
    restored = [c for c, ok in ((cnpj, cnpj_status), (cnpj2, cnpj2_status)) if ok]

    profile_deleted = _delete_profile_encerrada(
        run.get("profile_user_id"), _infra().get("adspower_api_key"),
        browser_provider=run.get("browser_provider") or "adspower",
    )
    store.update_run(
        run_id,
        status=store.RUN_CANCELLED,
        error="recuperada pelo operador — conta E CNPJ(s) devolvidos ao pool.",
    )
    return {
        "run_id": run_id,
        "account_released": bool(acc_status),
        "account_email": acc_status.get("email") if acc_status else None,
        "cnpj_restored": bool(cnpj_status),
        "cnpj2_restored": bool(cnpj2_status),
        "cnpjs_restored": restored,
        "profile_deleted": bool(profile_deleted),
        "message": f"Conta '{email}' e {len(restored)} CNPJ(s) devolvidos ao pool.",
    }


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str) -> dict[str, Any]:
    """PAUSAR temporariamente: o bot entra em espera no próximo checkpoint seguro.
    Browser e perfil ficam abertos e intocados. 'Retomar' faz o bot continuar
    de onde parou — mesmo pipeline, mesma task, sem criar run nova."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    if run.get("status") not in (store.RUN_RUNNING,):
        raise HTTPException(
            409,
            f"Só runs em execução podem ser pausadas (status atual: '{run.get('status')}').",
        )
    run_control.request_pause(run_id)
    step = run.get("step") or "?"
    return {
        "run_id": run_id,
        "paused": True,
        "current_step": step,
        "message": f"Pausa solicitada — o bot vai pausar no próximo checkpoint seguro (etapa atual: '{step}').",
    }


@app.post("/api/runs/{run_id}/assume")
def assume_run(run_id: str) -> dict[str, Any]:
    """ASSUMIR manualmente (botão 'Assumir'): para o bot mas MANTÉM o perfil
    AdsPower aberto pro operador concluir à mão. Conta e CNPJ ficam como estão
    (o operador está usando). Mostra em que etapa o bot parou."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    run_control.request_stop(run_id, run_control.ASSUME)
    step = run.get("step") or "?"
    store.update_run(
        run_id,
        status=store.RUN_ASSUMED,
        error=f"assumida pelo operador — bot parado na etapa '{step}'. "
        "Perfil AdsPower mantido aberto para conclusão manual.",
    )
    return {
        "run_id": run_id,
        "stopped_at_step": step,
        "profile_user_id": run.get("profile_user_id"),
        "profile_serial_number": run.get("profile_serial_number"),
    }


class ResumeReq(BaseModel):
    next_step: Optional[str] = None  # Etapa a continuar (opcional)


@app.post("/api/runs/{run_id}/refresh-sms-balance")
def refresh_sms_balance(run_id: str) -> dict[str, Any]:
    """Checa o saldo SMS e retoma a run pausada quando já houver crédito."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")
    if run.get("status") != store.RUN_PAUSED:
        raise HTTPException(
            409,
            f"Run não está aguardando saldo SMS (status atual: '{run.get('status')}').",
        )

    async def _check_balance() -> float:
        async with Sms24hClient() as sms:
            return await sms.get_balance()

    try:
        balance = asyncio.run(_check_balance())
    except Sms24hError as exc:
        raise HTTPException(502, f"Falha ao consultar saldo SMS: {exc}") from exc

    required = float(NUMERO_VIRTUAL_TARGET_PRICE or 0) / 100.0
    enough = balance >= required if required > 0 else balance > 0
    if not enough:
        detail = (
            "Sem saldo suficiente ainda. "
            f"Saldo atual: R$ {balance:.2f}. Necessário: R$ {required:.2f}."
        )
        store.update_run(
            run_id,
            status=store.RUN_PAUSED,
            step="aguardando saldo SMS",
            error=(
                "Sem saldo no numero-virtual para puxar um número novo. "
                f"Adicione pelo menos R$ {required:.2f} e clique em 'Atualizar saldo' para continuar. "
                f"Detalhe: {detail}"
            ),
        )
        return {
            "run_id": run_id,
            "resumed": False,
            "balance": balance,
            "required_balance": required,
            "message": detail,
        }

    store.update_run(
        run_id,
        status=store.RUN_RUNNING,
        error=None,
        step="retomando após recarga de saldo SMS",
    )
    return {
        "run_id": run_id,
        "resumed": True,
        "balance": balance,
        "required_balance": required,
        "message": (
            f"Saldo detectado (R$ {balance:.2f}). A run vai puxar um número novo e continuar."
        ),
    }


class DeleteItemsReq(BaseModel):
    # Valores a apagar do pool — CNPJ (qualquer formatação), e-mail ou
    # label de proxy (host:porta), dependendo do endpoint. Apaga 1 ou
    # vários de uma vez (seleção múltipla no painel).
    values: list[str]


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, req: ResumeReq) -> dict[str, Any]:
    """CONTINUAR de onde parou (botão 'Continuar'): o operador terminou a
    intervenção manual (ex: adicionou o portfolio à mão) e quer que o bot
    retome a execução da mesma run, no MESMO perfil Adspower, sem resetar nada.

    Opcionalmente, o operador pode especificar uma NOVA etapa para continuar
    (ex: "estou na verificação de domínio, pule direto pro WhatsApp").

    Marca a run como RUNNING de novo e a worker thread retoma no próximo checkpoint.
    Se a run anterior ainda estiver viva, executará em paralelo (thread não matável).
    """
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")

    if run.get("status") not in (store.RUN_ASSUMED, store.RUN_PAUSED):
        raise HTTPException(
            409,
            f"Run não pode ser retomada — status atual é '{run.get('status')}' "
            "(só runs 'assumed' ou 'paused' podem continuar)."
        )
    is_paused = run.get("status") == store.RUN_PAUSED

    current_step = run.get("step") or "?"
    next_step = (req.next_step or "").strip() or current_step

    run_control.request_resume(run_id)

    if is_paused:
        # Run PAUSADA: o runner está vivo num loop de espera — request_resume
        # já fez o loop sair e o runner atualiza status sozinho.
        # Não atualizamos o store aqui para evitar race com o runner.
        return {
            "run_id": run_id,
            "resumed": True,
            "current_step": current_step,
            "resuming_from_step": current_step,
            "step_changed": False,
            "message": f"Run retomada — continuando da etapa '{current_step}'.",
        }

    # Run ASSUMIDA: pipeline já acabou, precisa re-enfileirar manualmente
    store.update_run(
        run_id,
        status=store.RUN_RUNNING,
        step=next_step,
        error=None,
    )
    return {
        "run_id": run_id,
        "resumed": True,
        "current_step": current_step,
        "resuming_from_step": next_step,
        "step_changed": next_step != current_step,
        "message": (
            f"Run retomada a partir da etapa '{next_step}'. "
            f"O perfil Adspower continua aberto."
            if next_step == current_step
            else f"Run pulou de '{current_step}' para '{next_step}'. "
                 f"O perfil Adspower continua aberto."
        ),
        "warning": (
            "Se a run anterior ainda estiver viva em background, "
            "pode haver colisão de logs/ações. Monitore as duas."
        ),
    }


@app.post("/api/runs/{run_id}/restart-to-business-details")
def restart_to_business_details(run_id: str) -> dict[str, Any]:
    """Reiniciar no passo de PREENCHER INFORMAÇÕES DA EMPRESA (3/5 business_details).

    Mantém navegador aberto, mesma sessão, cookies e cache.
    Limpa todos os passos após business_details (domínio, portfolio, etc).

    Uso: quando o preenchimento de empresa ficou errado ou precisa refazer.
    """
    from core.step_checkpoint import delete_steps_after
    from utils.helpers import only_digits

    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' não encontrada.")

    # Funciona com runs em FAILED ou RUNNING
    status = run.get("status")
    if status not in (store.RUN_FAILED, store.RUN_RUNNING):
        raise HTTPException(
            409,
            f"Run não pode ser reiniciada — status atual é '{status}' "
            "(só 'failed' ou 'running' podem reiniciar para business_details)."
        )

    current_step = run.get("step") or "?"
    target_step = "3/5 business_details"

    # Deleta todos os checkpoints DEPOIS de business_details
    # (domínio, portfolio, etc — volta ao fim de business_details)
    delete_steps_after(run_id, target_step)

    # Marca pra CONTINUAR (mantém navegador aberto)
    run_control.request_resume(run_id)

    # Atualiza run pra RUNNING, voltando ao passo de business_details
    store.update_run(
        run_id,
        status=store.RUN_RUNNING,
        step=target_step,
        error=None,  # Limpa erro anterior
    )

    return {
        "run_id": run_id,
        "restarted": True,
        "previous_step": current_step,
        "resuming_from_step": target_step,
        "message": (
            f"Reiniciado! Voltou de '{current_step}' para PREENCHER EMPRESA. "
            f"Navegador aberto — mesma sessão, cookies, cache. "
            f"Tentando novamente..."
        ),
        "action": "MANTER NAVEGADOR ABERTO — vai retomar automaticamente."
    }


@app.get("/api/bms")
def bms(tier: Optional[str] = None) -> list[dict[str, Any]]:
    out = []
    # Só BMs de runs que fecharam 100% — runs incompletas/parciais (ex.: criaram
    # o portfólio mas não capturaram waba_id) ficam na tabela como ativo durável
    # a rastrear, mas não aparecem aqui na seção de BMs.
    for b in store.list_bms(tier=tier, success_only=True):
        b["meta_token"] = _mask(b.get("meta_token"))
        # Observação operacional: credenciais da conta FB/AdsPower que criou a
        # BM (login/senha/2FA), lidas de fb_accounts pelo account_email — fonte
        # ÚNICA, sempre em dia (não duplica segredo na tabela bms). Ferramenta
        # local: exibidas em claro p/ o operador logar/gerir a conta na mão.
        acc = store.get_fb_account(b.get("account_email") or "")
        b["account_password"] = (acc or {}).get("password")
        b["account_totp_seed"] = (acc or {}).get("totp_seed")
        out.append(b)
    return out


@app.post("/api/bms/refresh")
def bms_refresh() -> dict[str, Any]:
    result = meta_graph.refresh_bm_verification()
    import time as _time
    _last_bm_refresh.update(summary=result, ts=_time.time(), error=None)
    return result


@app.get("/api/bms/refresh-status")
def bms_refresh_status() -> dict[str, Any]:
    """Info do auto-refresh: último resultado, timestamp, intervalo."""
    return {
        "auto_refresh_enabled": True,
        "interval_hours": _BM_REFRESH_INTERVAL_S / 3600,
        "last_refresh": _last_bm_refresh.get("ts"),
        "last_summary": _last_bm_refresh.get("summary"),
        "last_error": _last_bm_refresh.get("error"),
    }


class BmManualStatusReq(BaseModel):
    status: str = ""  # 'analise'|'aprovada1'|'aprovada2'|'2k'|'banida' | '' = automático


@app.post("/api/bms/{business_id}/status")
def bms_set_manual_status(business_id: str, req: BmManualStatusReq) -> dict[str, Any]:
    """Define o status MANUAL de uma BM (rótulo visual do operador). String
    vazia limpa o override e volta ao status automático da Graph."""
    store.set_bm_manual_status(business_id, req.status or None)
    return {"business_id": business_id, "manual_status": req.status or None}


# ---------------------------------------------------------------------------
# Pools (uploads de CNPJs e contas)
# ---------------------------------------------------------------------------
@app.get("/api/pools")
def pools() -> dict[str, Any]:
    # Só itens 'available' — usados/descartados/congelados não aparecem na lista.
    accounts = [
        {k: v for k, v in a.items() if k != "password"}
        for a in store.list_fb_accounts(status=store.POOL_AVAILABLE)
    ]
    # Proxies: nunca devolve a senha do proxy pela API.
    proxies = [
        {k: v for k, v in p.items() if k != "password"}
        for p in store.list_proxies(status=store.POOL_AVAILABLE)
    ]
    return {
        "counts": store.pool_counts(),
        "cnpjs": store.list_cnpjs(status=store.POOL_AVAILABLE),
        "accounts": accounts,
        "proxies": proxies,
    }


@app.get("/api/sms/session-status")
def sms_session_status() -> dict[str, Any]:
    """Status do cookie CUSTOMER_SESSION (JWT) — alerta se perto de expirar."""
    import base64
    import json
    import time as _time

    from config import NUMERO_VIRTUAL_CUSTOMER_SESSION

    session = (NUMERO_VIRTUAL_CUSTOMER_SESSION or "").strip()
    if not session:
        return {"configured": False, "warning": "CUSTOMER_SESSION não configurada"}
    try:
        parts = session.split(".")
        if len(parts) < 2:
            return {"configured": True, "valid": False, "warning": "formato inválido (não é JWT)"}
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if not exp:
            return {"configured": True, "valid": True, "expires_at": None, "warning": None}
        now = _time.time()
        remaining_s = exp - now
        remaining_h = remaining_s / 3600
        expired = remaining_s <= 0
        warning = None
        if expired:
            warning = "EXPIRADA — renove o cookie em numero-virtual.com e reinicie o backend"
        elif remaining_h < 48:
            warning = f"Expira em {remaining_h:.0f}h — renove em breve"
        return {
            "configured": True,
            "valid": not expired,
            "expires_at": int(exp),
            "remaining_hours": round(remaining_h, 1) if not expired else 0,
            "warning": warning,
        }
    except Exception:
        return {"configured": True, "valid": True, "expires_at": None, "warning": None}


@app.post("/api/proxies/health-check")
async def proxy_health_check() -> dict[str, Any]:
    """Testa conectividade de cada proxy available (GET facebook.com/robots.txt)."""
    from core import preflight

    proxies = store.list_proxies(status=store.POOL_AVAILABLE)
    results: dict[str, Any] = {"total": len(proxies), "ok": 0, "failed": 0, "details": []}
    for p in proxies:
        cfg = {
            "server": f"{p['scheme']}://{p['host']}:{p['port']}",
            "username": p.get("username"),
            "password": p.get("password"),
        }
        check = await preflight.check_proxy(cfg)
        detail = {
            "label": p["label"],
            "username": p.get("username", ""),
            "ok": check.ok,
            "detail": check.detail,
            "duration_ms": check.duration_ms,
        }
        results["details"].append(detail)
        if check.ok:
            results["ok"] += 1
        else:
            results["failed"] += 1
    return results


@app.get("/api/accounts/soft-blocked")
def soft_blocked_accounts() -> list[dict[str, Any]]:
    """Retorna contas com soft-block (login_bounced) nos últimos 7 dias
    e contas atualmente congeladas."""
    blocked = store.get_soft_blocked_accounts()
    frozen = store.get_frozen_accounts()
    seen = {a["email"] for a in blocked}
    for f in frozen:
        if f["email"] not in seen:
            blocked.append(f)
    return blocked


@app.get("/api/accounts/discarded")
def discarded_accounts() -> list[dict[str, Any]]:
    """Retorna contas DESCARTADAS (status=discarded) que podem ser resgatadas."""
    try:
        from core import store
        accts = store.get_accounts_by_status(store.POOL_DISCARDED)
        # Enriquece com credenciais completas
        enriched = []
        for acct in accts:
            email = acct.get("email")
            creds = store.get_fb_account(email) if email else None
            enriched.append({
                **acct,
                "login": email or "",
                "password": creds.get("password", "") if creds else "",
                "totp_seed": creds.get("totp_seed", "") if creds else "",
                "email_pass": creds.get("email_pass", "") if creds else "",
                "email_address": creds.get("email_address", "") if creds else "",
            })
        return enriched
    except Exception as e:
        logger.error(f"Erro ao listar discarded accounts: {e}")
        raise HTTPException(500, f"Erro ao listar contas descartadas: {e}")


@app.get("/api/accounts/pending-discard")
def pending_discard_accounts() -> list[dict[str, Any]]:
    """Retorna contas pendentes de discard com credenciais completas."""
    try:
        from core import store
        accts = store.get_accounts_by_status(store.POOL_PENDING_DISCARD)
        # Enriquece com credenciais completas
        enriched = []
        for acct in accts:
            email = acct.get("email")
            creds = store.get_fb_account(email) if email else None
            enriched.append({
                **acct,
                "login": email or "",
                "password": creds.get("password", "") if creds else "",
                "totp_seed": creds.get("totp_seed", "") if creds else "",
                "email_pass": creds.get("email_pass", "") if creds else "",
                "email_address": creds.get("email_address", "") if creds else "",
            })
        return enriched
    except Exception as e:
        logger.error(f"Erro ao listar pending-discard accounts: {e}")
        raise HTTPException(500, f"Erro ao listar contas pendentes de descarte: {e}")


@app.post("/api/accounts/discard-manual")
def discard_accounts_manual(req: DeleteItemsReq) -> dict[str, Any]:
    """Descarta manualmente contas (operador revisou e confirmou)."""
    from core import store

    discarded = []
    failed = []

    # DEBUG: Log dos emails recebidos
    logger.info(f"[DISCARD_MANUAL] Recebidos {len(req.values)} itens para descartar")
    logger.info(f"[DISCARD_MANUAL] Valores (repr): {repr(req.values)}")
    logger.info(f"[DISCARD_MANUAL] Valores (tipo): {type(req.values)}")

    for i, email in enumerate(req.values):
        # Valida email vazio
        if not email or (isinstance(email, str) and not email.strip()):
            logger.warning(f"[DISCARD_MANUAL] [{i}] Email vazio: {repr(email)}")
            failed.append({"email": email, "reason": "Email vazio"})
            continue

        email_str = str(email).strip().lower()
        logger.info(f"[DISCARD_MANUAL] [{i}] Processando: {repr(email)} (tipo={type(email).__name__}) → {repr(email_str)}")

        try:
            result = store.discard_fb_account(email_str)
            logger.info(f"[DISCARD_MANUAL] [{i}] Resultado: {repr(result)}")

            if result:
                discarded.append(email_str)
                logger.info(f"[DISCARD_MANUAL] [{i}] ✓ Descartada: {email_str}")
            else:
                # Não encontrou a conta no banco
                logger.warning(f"[DISCARD_MANUAL] [{i}] ✗ Conta não encontrada no pool para: {email_str}")
                failed.append({"email": email_str, "reason": "Conta não encontrada no pool"})
        except Exception as e:
            logger.error(f"[DISCARD_MANUAL] [{i}] ✗ Erro ao descartar {email_str}: {e}", exc_info=True)
            failed.append({"email": email_str, "reason": str(e)})

    logger.info(f"[DISCARD_MANUAL] Resultado FINAL: {len(discarded)} descartadas, {len(failed)} falhadas")
    logger.info(f"[DISCARD_MANUAL] Descartadas: {discarded}")
    logger.info(f"[DISCARD_MANUAL] Falhadas: {failed}")

    return {
        "discarded": discarded,
        "total": len(discarded),
        "failed": failed,
        "failed_count": len(failed)
    }


@app.post("/api/pools/cnpjs/delete")
def delete_cnpjs(req: DeleteItemsReq) -> dict[str, Any]:
    from utils.profiles_state import delete_profile, find_profile_by_cnpj
    import asyncio

    deleted = store.delete_cnpjs(req.values)

    for cnpj_val in req.values:
        try:
            profile = find_profile_by_cnpj(cnpj_val)
            if not profile:
                profile = find_profile_by_cnpj(cnpj_val, provider="ixbrowser")
                _prov = "ixbrowser" if profile else None
            else:
                _prov = None
            if profile:
                profile_id = profile.get("user_id")
                if profile_id:
                    s = store.get_settings()
                    import config as cfg
                    if _prov == "ixbrowser":
                        from core.ixbrowser_client import IXBrowserClient
                        client = IXBrowserClient()
                        asyncio.run(client.delete_profile(profile_id))
                    else:
                        api_key = s.get("adspower_api_key") or getattr(cfg, "ADSPOWER_API_KEY", None)
                        if api_key:
                            client = AdsPowerClient(api_key)
                            asyncio.run(client.delete_profile(profile_id))
                delete_profile(cnpj_val, provider=_prov)
        except Exception as e:
            logger.warning(f"Erro ao deletar perfil para CNPJ {cnpj_val}: {e}")

    return {"deleted": deleted}


@app.post("/api/pools/accounts/delete")
def delete_accounts(req: DeleteItemsReq) -> dict[str, Any]:
    deleted = store.delete_fb_accounts(req.values)
    return {"deleted": deleted}


@app.get("/api/pools/accounts/export", response_class=PlainTextResponse)
def export_accounts() -> str:
    """Exporta apenas as contas disponíveis em formato limpo: login:senha:2fa:email:senh_email

    Retorna texto puro (uma conta por linha) - sem JSON, sem aspas, sem vírgulas.
    """
    accounts = store.list_fb_accounts(status='available')  # apenas contas disponíveis do estoque

    # Formata como: login:senha:2fa:email:senha_email (uma por linha)
    lines = []
    for acc in accounts:
        email = acc.get("email") or ""
        password = acc.get("password") or ""
        totp_seed = acc.get("totp_seed") or ""
        email_address = acc.get("email_address") or ""
        email_password = acc.get("email_password") or ""

        line = f"{email}:{password}:{totp_seed}:{email_address}:{email_password}"
        lines.append(line)

    return "\n".join(lines)


class ResetWrongPasswordReq(BaseModel):
    email: str


@app.post("/api/pools/accounts/reset-wrong-password")
def reset_wrong_password_account(req: ResetWrongPasswordReq) -> dict[str, Any]:
    """Reseta o status de uma conta de 'wrong_password' para 'available'.

    Usada quando o operador trocou a senha com o fornecedor e quer colocar
    a conta de volta no pool.
    """
    status = store.settle_fb_account(req.email, portfolios=0)
    return {"email": req.email, "new_status": status}


@app.post("/api/pools/accounts/rescue")
def rescue_discarded_account(req: ResetWrongPasswordReq) -> dict[str, Any]:
    """Resgata uma conta DESCARTADA de volta ao pool (status=available).

    Usado quando o operador quer reutilizar uma conta que foi descartada
    por engano ou quer tentar de novo. A conta volta a 'available' e pode
    ser usada em novos runs.

    Retorna o novo status ou erro se a conta não estiver descartada.
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email vazio")

    if not email:
        raise HTTPException(400, "Email vazio")

    try:
        store.init_db()
        with store._POOL_LOCK, store._conn() as conn:
            row = conn.execute(
                "SELECT id, status FROM fb_accounts WHERE lower(email) = ? LIMIT 1",
                (email,),
            ).fetchone()

            if not row:
                raise HTTPException(404, f"Conta '{email}' não encontrada no pool")

            id_, status = row
            # Permite resgatar contas 'discarded' ou 'used'
            if status not in (store.POOL_DISCARDED, store.POOL_USED):
                raise HTTPException(
                    409,
                    f"Conta '{email}' está com status '{status}'. "
                    f"Só contas descartadas ou usadas podem ser resgatadas.",
                )

            # Resgata para 'available'
            conn.execute(
                "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
                (store.POOL_AVAILABLE, store._now(), id_),
            )

        logger.info(f"[RESCUE_ACCOUNT] Conta {email} resgatada de '{status}' → 'available'")
        return {
            "email": email,
            "old_status": status,
            "new_status": "available",
            "message": f"Conta '{email}' resgatada com sucesso!",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RESCUE_ACCOUNT] Erro ao resgatar {email}: {e}")
        raise HTTPException(500, f"Erro ao resgatar conta: {str(e)}")


@app.post("/api/pools/accounts/discard")
def discard_account(req: ResetWrongPasswordReq) -> dict[str, Any]:
    """Descarta uma conta manualmente (marca como POOL_DISCARDED).

    Usado quando o operador quer remover uma conta problemática do pool.
    A conta fica marcada como descartada e não pode ser usada novamente.

    Retorna o novo status ou erro se a conta não existir.
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email vazio")

    try:
        store.init_db()
        with store._POOL_LOCK, store._conn() as conn:
            row = conn.execute(
                "SELECT id, status FROM fb_accounts WHERE lower(email) = ? LIMIT 1",
                (email,),
            ).fetchone()

            if not row:
                raise HTTPException(404, f"Conta '{email}' não encontrada no pool")

            id_, status = row

            # Descarta a conta
            conn.execute(
                "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
                (store.POOL_DISCARDED, store._now(), id_),
            )

        logger.info(f"[DISCARD_ACCOUNT] Conta {email} descartada (status anterior: '{status}')")
        return {
            "email": email,
            "old_status": status,
            "new_status": "discarded",
            "message": f"Conta '{email}' descartada com sucesso!",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DISCARD_ACCOUNT] Erro ao descartar {email}: {e}")
        raise HTTPException(500, f"Erro ao descartar conta: {str(e)}")


@app.post("/api/pools/proxies/delete")
def delete_proxies(req: DeleteItemsReq) -> dict[str, Any]:
    return {"deleted": store.delete_proxies(req.values)}


@app.get("/api/cnpj/{cnpj}")
async def cnpj_info(cnpj: str) -> dict[str, Any]:
    """Consulta a BrasilAPI e devolve os dados do CNPJ (para conferência antes
    do run — o operador puxa o telefone/endereço manualmente quando preciso)."""
    digits = only_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido")
    try:
        data = await fetch_cnpj_cached(digits)
    except BrasilApiError as exc:
        raise HTTPException(502, f"BrasilAPI: {exc}")
    endereco = ", ".join(
        p for p in (
            data.logradouro, data.numero, data.bairro,
            f"{data.cidade}/{data.uf}" if data.cidade and data.uf else (data.cidade or data.uf),
            data.cep,
        ) if p
    )
    return {
        "cnpj": data.cnpj or digits,
        "razao_social": data.razao_social,
        "nome_fantasia": data.nome_fantasia,
        "telefone": data.telefone,
        "email": data.email,
        "situacao": data.situacao,
        "abertura": data.abertura,
        "porte": data.porte,
        "cnae_principal": data.cnae_principal,
        "endereco": endereco or None,
        # Endereço estruturado (além do `endereco` montado) — a UI usa estes
        # campos para edição inline. `numero` em destaque: a prévia avisa
        # quando vem "S/N"/vazio (BrasilAPI usa dump defasado da Receita; o
        # operador corrige na UI ou a consulta ao vivo / Infosimples preenche).
        "numero": data.numero,
        "logradouro": data.logradouro,
        "complemento": data.complemento,
        "bairro": data.bairro,
        "cidade": data.cidade,
        "uf": data.uf,
        "cep": data.cep,
    }


async def _validate_cnpjs_receita(
    cnpjs: list[str],
) -> tuple[list[str], list[str]]:
    """Valida CNPJs na BrasilAPI/Receita. Retorna (válidos, erros)."""
    valid: list[str] = []
    errors: list[str] = []
    sem = asyncio.Semaphore(3)
    async def _check(c: str) -> None:
        async with sem:
            try:
                await fetch_cnpj_cached(c)
                valid.append(c)
            except Exception:  # noqa: BLE001
                errors.append(f"{c}: não encontrado na Receita Federal")
    await asyncio.gather(*[_check(c) for c in cnpjs])
    return valid, errors


@app.post("/api/pools/cnpjs")
async def upload_cnpjs(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        cnpjs, errors = batch_io.parse_cnpjs(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    valid, receita_errors = await _validate_cnpjs_receita(cnpjs)
    errors.extend(receita_errors)
    return {"added": store.add_cnpjs(valid), "parsed": len(cnpjs), "errors": errors}


class ManualCnpjsReq(BaseModel):
    # 1 ou vários CNPJs digitados/colados (linha, vírgula ou ; como separador).
    text: str


@app.post("/api/pools/cnpjs/manual")
async def add_cnpjs_manual(req: ManualCnpjsReq) -> dict[str, Any]:
    """Adiciona CNPJ(s) ao pool digitados na UI (1 por vez ou colados em lote),
    sem precisar de arquivo. Reusa o parser do .txt (valida 14 dígitos)."""
    norm = (req.text or "").replace(",", "\n").replace(";", "\n")
    cnpjs, errors = batch_io.parse_cnpjs(norm.encode("utf-8"), "manual.txt")
    valid, receita_errors = await _validate_cnpjs_receita(cnpjs)
    errors.extend(receita_errors)
    return {"added": store.add_cnpjs(valid), "parsed": len(cnpjs), "errors": errors}


class CnpjSearchReq(BaseModel):
    nome: Optional[str] = None
    somente_mei: bool = False
    somente_matriz: bool = False
    situacao_ativa: bool = True
    capital_min: Optional[float] = None
    capital_max: Optional[float] = None
    abertura_de: Optional[str] = None   # "YYYY-MM-DD"
    abertura_ate: Optional[str] = None  # "YYYY-MM-DD"
    uf: Optional[str] = None
    quantidade: int = 20
    pagina: int = 1


@app.post("/api/cnpjs/search")
async def cnpjs_search(req: CnpjSearchReq) -> dict[str, Any]:
    """Busca uma LISTA de CNPJs por filtros (Casa dos Dados) p/ o operador
    revisar e jogar no pool. NÃO adiciona sozinho — só devolve a lista.
    Filtra automaticamente CNPJs já presentes no pool (qualquer status).
    ``pagina`` avança nos resultados (cada página traz CNPJs diferentes)."""
    from core import casadosdados_client as cdd  # noqa: PLC0415
    qtd = max(1, min(1000, int(req.quantidade or 20)))
    pag = max(1, int(req.pagina or 1))

    pool_set = store.get_pool_cnpj_set()

    def _build(p: int) -> dict[str, Any]:
        return cdd.build_body(
            nome=req.nome, somente_mei=req.somente_mei, somente_matriz=req.somente_matriz,
            situacao_ativa=req.situacao_ativa, capital_min=req.capital_min,
            capital_max=req.capital_max, abertura_de=req.abertura_de,
            abertura_ate=req.abertura_ate, uf=req.uf, limite=qtd, pagina=p,
        )

    try:
        data = await cdd.search(_build(pag), tipo_resultado="simples")
    except cdd.CasaDosDadosError as exc:
        raise HTTPException(400, str(exc))

    total_api = data.get("total", 0)
    filtrados = 0

    def _parse(raw_data: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal filtrados
        items: list[dict[str, Any]] = []
        for it in (raw_data.get("cnpjs") or []):
            cnpj = (it.get("cnpj") or "").strip()
            if not cnpj:
                continue
            if cnpj in pool_set:
                filtrados += 1
                continue
            sit = it.get("situacao_cadastral")
            sit_str = (sit.get("situacao_atual") or sit.get("situacao_cadastral")) if isinstance(sit, dict) else sit
            items.append({
                "cnpj": cnpj,
                "razao_social": it.get("razao_social") or "",
                "nome_fantasia": it.get("nome_fantasia") or "",
                "situacao": sit_str or "",
            })
        return items

    out = _parse(data)

    if len(out) < qtd and filtrados > 0 and total_api > pag * qtd:
        try:
            data2 = await cdd.search(_build(pag + 1), tipo_resultado="simples")
            out.extend(_parse(data2))
            out = out[:qtd]
        except cdd.CasaDosDadosError:
            pass

    return {"total": total_api, "cnpjs": out, "pagina": pag, "quantidade": qtd, "filtrados_pool": filtrados}


@app.post("/api/pools/accounts")
async def upload_accounts(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        accs, errors = batch_io.parse_fb_accounts(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"added": store.add_fb_accounts(accs), "parsed": len(accs), "errors": errors}


class ManualAccountsReq(BaseModel):
    text: str


@app.post("/api/pools/accounts/manual")
def add_accounts_manual(req: ManualAccountsReq) -> dict[str, Any]:
    """Adiciona conta(s) FB coladas na UI (combolist, 1 por linha) sem arquivo.
    Reusa o parser do .txt (login:senha:seed:email, separador :/|/;/tab)."""
    accs, errors = batch_io.parse_fb_accounts((req.text or "").encode("utf-8"), "manual.txt")
    return {"added": store.add_fb_accounts(accs), "parsed": len(accs), "errors": errors}


@app.post("/api/pools/proxies")
async def upload_proxies(
    file: UploadFile = File(...), scheme: Optional[str] = Form(None)
) -> dict[str, Any]:
    raw = await file.read()
    try:
        proxies, errors = batch_io.parse_proxies(raw, file.filename or "", default_scheme=scheme or "http")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"added": store.add_proxies(proxies), "parsed": len(proxies), "errors": errors}


class ManualProxiesReq(BaseModel):
    text: str
    scheme: Optional[str] = None  # http | https | socks5 — tipo escolhido na UI


@app.post("/api/pools/proxies/manual")
def add_proxies_manual(req: ManualProxiesReq) -> dict[str, Any]:
    """Adiciona proxy(s) digitados/colados na UI (1 ou vários), sem arquivo.

    Usa o parser robusto do batch_io (mesmo do upload por arquivo): aceita
    ``scheme://user:pass@host:port``, ``user:pass@host:port``,
    ``host:port:user:pass`` e ``host:port``. O parser antigo aqui fazia só
    ``split(':')`` e quebrava no formato com ``scheme://`` e ``@`` (dava 0 de 0).
    """
    norm = (req.text or "").replace(",", "\n").replace(";", "\n")
    scheme = (req.scheme or "http").lower()
    lines = [l.strip() for l in norm.split("\n") if l.strip()]
    proxies: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, line in enumerate(lines, start=1):
        p = batch_io._parse_proxy_line(line, default_scheme=scheme)
        if p:
            proxies.append(p)
        else:
            errors.append(f"linha {i}: proxy inválido ({line!r})")
    added = store.add_proxies(proxies)
    return {"added": added, "parsed": len(proxies), "errors": errors}


class DuplicateProxyReq(BaseModel):
    proxy: str  # host:port:user:pass
    times: int = 10  # quantas vezes duplicar


@app.post("/api/pools/proxies/duplicate")
def add_proxy_duplicate(req: DuplicateProxyReq) -> dict[str, Any]:
    """Adiciona 1 proxy duplicada N vezes no estoque."""
    line = (req.proxy or "").strip()
    if not line:
        return {"added": 0, "error": "proxy vazia"}

    parts = line.split(":")
    if len(parts) < 2:
        return {"added": 0, "error": "formato inválido (use host:port:user:pass)"}

    host = parts[0].strip()
    port = parts[1].strip()

    if not host or not port:
        return {"added": 0, "error": "host ou port vazio"}

    try:
        port_int = int(port)
    except ValueError:
        return {"added": 0, "error": "port não é número"}

    user = parts[2].strip() if len(parts) >= 3 else None
    pwd = parts[3].strip() if len(parts) >= 4 else None

    # Cria N cópias
    proxies = []
    times = max(1, min(req.times, 100))  # 1-100 cópias
    for _ in range(times):
        proxies.append({
            "label": f"{host}:{port}",
            "scheme": "http",
            "host": host,
            "port": port_int,
            "username": user or None,
            "password": pwd or None,
        })

    added = store.add_proxies(proxies)
    return {"added": added, "requested": times, "success": True}


# ---------------------------------------------------------------------------
# Fila / execução
# ---------------------------------------------------------------------------
class EnqueueReq(BaseModel):
    count: int
    # Usar proxy do pool (Webshare)? Default True. False = rodar pela conexão
    # DIRETA da máquina (5G/VPN) — sem pool, preflight sonda direto, AdsPower
    # abre em no_proxy. Mesmo efeito do toggle do modo Avulso, agora no Lote.
    use_proxy: bool = True
    proxy_type: str = "datacenter"  # datacenter | residential | mobile
    sms_option: Optional[str] = None  # 'facebook' | 'other_1' | 'other_2' | 'other_3' | 'sms24h' | 'smsrush' | None
    sms_ddd: Optional[str] = None     # DDD filter (2 dígitos); vazio/null = aleatório
    smsrush_server: Optional[int] = None  # servidor SMSRush (1–4, vem do dropdown)
    sms_fallback_option: Optional[str] = None  # 2ª provedora SMS p/ recuperação
    dual_bm: bool = False  # True = reserva 2º CNPJ e cria 2 BMs por perfil
    browser_provider: str = "adspower"  # "adspower" | "ixbrowser"
    waba_enabled: bool = False  # True = cria WABA antes da verificação
    stop_after_ad_account: bool = False  # True = encerra após criar conta de anúncios


def _do_enqueue(count: int, use_proxy: bool = True, sms_option: Optional[str] = None, sms_ddd: Optional[str] = None, smsrush_server: Optional[int] = None, sms_fallback_option: Optional[str] = None, dual_bm: bool = False, browser_provider: str = "adspower", waba_enabled: bool = False, stop_after_ad_account: bool = False) -> dict[str, Any]:
    """Função auxiliar para enfileirar runs (usada por enqueue e autopilot).

    ``sms_option`` ('facebook'|'other'|'sms24h'|'smsrush'|None) escolhe o preset
    de compra do SMS para TODAS as runs desta leva (None = default do .env).
    ``sms_ddd`` filtra por DDD (2 dígitos) — None/vazio = aleatório."""
    _require_cloudflare_filled()  # NUNCA reserva par sem Cloudflare preenchido
    pairs = store.reserve_pairs(max(0, count))

    # Distribuição automática entre múltiplos domínios Netlify (respeita o cap
    # de cada um). Ex.: domínio A com 4 vagas + domínio B com 6 vagas e 5 runs →
    # usa 4 do A e 1 do B. Só entram domínios com NS já propagados.
    nf_domains = store.get_netlify_domains_for_runs(len(pairs))
    if not nf_domains:
        active = store.get_active_netlify_domain()
        nf_domains = [active] if active else []

    # Expande para uma fila por run (round-robin por vagas).
    nf_domain_iter = []
    for nd in nf_domains:
        for _ in range(nd.get("slots_to_use", 1) if "slots_to_use" in nd else nd.get("remaining", 1)):
            nf_domain_iter.append(nd)

    run_ids: list[str] = []
    s = store.get_settings()
    import config as cfg

    for i, p in enumerate(pairs):
        acc, cnp = p["account"], p["cnpj"]

        # Domínio Netlify correspondente (ou recai no 1º se a fila acabar).
        nd = nf_domain_iter[i] if i < len(nf_domain_iter) else (nf_domain_iter[0] if nf_domain_iter else {})
        nf_domain = nd.get("domain") if nd else None
        nf_token = (nd.get("api_token") if nd else None) or getattr(cfg, "NETLIFY_API_TOKEN", None)

        infra = {
            "adspower_mode": "create",
            "adspower_api_key": s.get("adspower_api_key") or getattr(cfg, "ADSPOWER_API_KEY", None),
            "netlify_domain": nf_domain,
            "netlify_api_token": nf_token,
            "sms_service_code": getattr(cfg, "SMS24H_SERVICE_CODE", None),
            "sms_country_code": getattr(cfg, "SMS24H_COUNTRY_CODE", None),
        }

        # 2 BMs por perfil: reserva um 2º CNPJ para a MESMA conta/perfil
        # (mesma sessão/navegador/proxy). Só quando dual_bm=True (toggle 2BM).
        cnpj2_val: Optional[str] = None
        if dual_bm:
            try:
                _c2 = store.reserve_one_cnpj(exclude={only_digits(cnp["cnpj"])})
                if _c2:
                    cnpj2_val = _c2
            except Exception:  # noqa: BLE001
                cnpj2_val = None

        spec = JobSpec(
            cnpj=cnp["cnpj"],
            cnpj2=cnpj2_val,
            meta_email_or_phone=acc["email"],
            meta_password=acc.get("password") or "",
            meta_totp_seed=acc.get("totp_seed") or "",
            meta_email_address=acc.get("email_address") or "",
            meta_email_password=acc.get("email_password") or "",
            meta_recovery_email=acc.get("recovery_email") or "",
            use_proxy=use_proxy,
            sms_option=(sms_option or None),
            sms_ddd=(sms_ddd or None),
            smsrush_server=(smsrush_server or None),
            sms_fallback_option=(sms_fallback_option or None),
            browser_provider=browser_provider,
            waba_enabled=waba_enabled,
            stop_after_ad_account=stop_after_ad_account,
            **infra,
        )
        # Escalonamento: a 1ª run começa na hora; cada seguinte espera
        # RUN_STAGGER_S a mais (evita corrida no login/2FA/captcha do Facebook
        # quando várias sobem juntas — dava bounce/timeout).
        import config as _cfg_stagger  # noqa: PLC0415
        _stagger = i * float(getattr(_cfg_stagger, "RUN_STAGGER_S", 25) or 0)
        rid = _manager.submit(
            run_service.execute_job, spec=spec, cnpj=cnp["cnpj"], label=acc["email"],
            start_delay_s=_stagger,
        )
        # Persiste o 2º CNPJ na run (feature 2-BMs) — assim a recuperação
        # ('Recuperar Conta + CNPJ') devolve OS DOIS ao pool, não só o primário.
        try:
            _run_update = {}
            if cnpj2_val:
                _run_update["cnpj2"] = cnpj2_val
            if browser_provider != "adspower":
                _run_update["browser_provider"] = browser_provider
            if stop_after_ad_account:
                _run_update["stop_after_ad_account"] = 1
            if _run_update:
                store.update_run(rid, **_run_update)
        except Exception:  # noqa: BLE001
            pass
        run_ids.append(rid)

    return {"enqueued": len(run_ids), "run_ids": run_ids}


# ── Piloto Automático (CICLOS SEQUENCIAIS) ─────────────────────────────────
# RUN = quantidade de execuções simultâneas por ciclo.
# PILOTO AUTOMÁTICO = quantidade de ciclos sequenciais.
# Dentro do ciclo: runs simultâneas. Entre ciclos: sequencial (aguarda o ciclo
# anterior finalizar tudo — runs, navegadores, recursos). Um lock evita que
# duas runs terminando "juntas" disparem dois ciclos.
_autopilot_lock = threading.Lock()


def _autopilot_next_cycle() -> None:
    """Avança para o próximo ciclo quando o ciclo atual termina.
    Chamado pelo JobManager quando a última run ativa termina. Best-effort."""
    ap = load_autopilot()
    if not ap.get("enabled"):
        return
    with _autopilot_lock:
        if _manager.active_count() > 0:
            return
        current = ap.get("current_cycle", 0)
        total = ap.get("total_cycles", 1)
        if current >= total:
            advance_cycle()
            logger.info(
                "Piloto Automático: Ciclo %d/%d foi o último — FINALIZADO.",
                current, total,
            )
            return
        ap = advance_cycle()
        if not ap.get("enabled"):
            return
        new_cycle = ap.get("current_cycle", 1)
        runs = int(ap.get("runs_per_cycle", 1) or 1)
        try:
            res = _do_enqueue(
                runs,
                ap.get("use_proxy", True),
                sms_option=ap.get("sms_option"),
                sms_ddd=ap.get("sms_ddd"),
                smsrush_server=ap.get("smsrush_server"),
                sms_fallback_option=ap.get("sms_fallback_option"),
                dual_bm=ap.get("dual_bm", False),
                browser_provider=ap.get("browser_provider", "adspower"),
                waba_enabled=ap.get("waba_enabled", False),
            )
            n = res.get("enqueued", 0)
            if n > 0:
                logger.info(
                    "Piloto Automático: Ciclo %d/%d iniciado — %d run(s).",
                    new_cycle, total, n,
                )
            else:
                save_autopilot(enabled=False, **{k: ap[k] for k in ap if k != "enabled"})
                logger.info(
                    "Piloto Automático: pool vazio no Ciclo %d/%d — DESLIGANDO.",
                    new_cycle, total,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Piloto Automático: falha ao iniciar Ciclo %d/%d (%s).",
                new_cycle, total, exc,
            )


_manager.on_all_done = _autopilot_next_cycle


class LoginOnlyReq(BaseModel):
    count: int = 1
    # Usar proxy do pool (Webshare)? Default True — mesmo toggle do lote normal.
    use_proxy: bool = True


@app.post("/api/jobs/login-only")
def login_only(req: LoginOnlyReq) -> dict[str, Any]:
    """Botão "Só logar" (painel, embaixo de Enfileirar e rodar): pega N
    conta(s) do estoque, abre o AdsPower, loga e PARA em business.facebook.com
    — sem CNPJ, sem SMS, sem site. Não mexe no pool de CNPJ (não paira/gasta
    nenhum). O resto (empresa, WhatsApp, verificação) fica manual, do jeito
    que o operador pediu."""
    n = max(0, int(req.count))
    run_ids: list[str] = []
    api_key = _infra().get("adspower_api_key")
    for _ in range(n):
        acc = store.lease_fb_account()
        if not acc:
            break
        spec = run_service.LoginOnlySpec(
            meta_email_or_phone=acc["email"],
            meta_password=acc.get("password") or "",
            meta_totp_seed=acc.get("totp_seed") or "",
            meta_email_address=acc.get("email_address") or "",
            meta_email_password=acc.get("email_password") or "",
            meta_recovery_email=acc.get("recovery_email") or "",
            adspower_api_key=api_key,
            use_proxy=req.use_proxy,
        )
        rid = _manager.submit(
            run_service.execute_login_only_job, spec=spec, cnpj=None, label=acc["email"]
        )
        run_ids.append(rid)
    return {
        "enqueued": len(run_ids),
        "run_ids": run_ids,
        "warning": (
            "sem contas disponíveis no estoque." if not run_ids and n > 0 else None
        ),
    }


@app.post("/api/jobs/enqueue")
def enqueue(req: EnqueueReq) -> dict[str, Any]:
    """Pareia N contas↔CNPJs disponíveis e enfileira no pool de workers.

    **Distribuição automática de Cloudflare**: se você tem 2 contas CF
    (4 slots + 6 slots) e quer 5 runs, usa 4 da 1ª e 1 da 2ª automaticamente.
    Cada run pega a conta CF correspondente em ordem (round-robin inteligente).
    """
    return _do_enqueue(req.count, req.use_proxy, sms_option=req.sms_option, sms_ddd=req.sms_ddd, smsrush_server=req.smsrush_server, sms_fallback_option=req.sms_fallback_option, dual_bm=req.dual_bm, browser_provider=req.browser_provider, waba_enabled=req.waba_enabled, stop_after_ad_account=req.stop_after_ad_account)


class ManualReq(BaseModel):
    cnpj: str
    # Credenciais digitadas (opcionais quando se importa do estoque).
    email: str = ""
    password: str = ""
    totp_seed: str = ""
    # Importar conta do ESTOQUE (pool): se setado, usa email/senha/2FA salvos
    # (o operador não re-digita) e marca a conta como usada.
    account_email: Optional[str] = None
    # Correções do operador aos dados da Receita (ex.: número do endereço que a
    # BrasilAPI devolve como "S/N"). Aplicadas sobre o CNPJData no run_service.
    overrides: Optional[dict[str, str]] = None
    # Usar proxy do pool (Webshare)? Default True. Desligue para rodar pelo IP
    # direto da máquina (operador roteando pelo 4G do celular) — evita o 402 do
    # Webshare sem banda; preflight sonda direto e o AdsPower abre em no_proxy.
    use_proxy: bool = True


@app.post("/api/run/manual")
def manual(req: ManualReq) -> dict[str, Any]:
    """Modo manual — 1 conta avulsa (teste/one-off). A conta pode ser digitada
    OU importada do estoque (``account_email``)."""
    _require_cloudflare_filled()  # NUNCA consome conta sem Cloudflare preenchido
    email, password, totp = req.email, req.password, req.totp_seed
    email_addr = email_pwd = rec_email = ""
    if req.account_email:
        acc = store.take_fb_account(req.account_email)
        if not acc:
            raise HTTPException(404, f"Conta '{req.account_email}' não está no estoque.")
        email = acc["email"]
        password = acc.get("password") or ""
        totp = acc.get("totp_seed") or ""
        email_addr = acc.get("email_address") or ""
        email_pwd = acc.get("email_password") or ""
        rec_email = acc.get("recovery_email") or ""
    if not (email and password):
        raise HTTPException(400, "Informe e-mail e senha (ou importe uma conta do estoque).")
    spec = JobSpec(
        cnpj=req.cnpj,
        meta_email_or_phone=email,
        meta_password=password,
        meta_totp_seed=totp,
        meta_email_address=email_addr,
        meta_email_password=email_pwd,
        meta_recovery_email=rec_email,
        cnpj_overrides=req.overrides,
        use_proxy=req.use_proxy,
        **_infra(),
    )
    rid = _manager.submit(
        run_service.execute_job, spec=spec, cnpj=req.cnpj, label=email
    )
    return {"run_id": rid}


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    return {"active": _manager.active_count(), "concurrency": _manager.concurrency}


# ---------------------------------------------------------------------------
# Settings (credenciais de infra inseridas no painel)
# ---------------------------------------------------------------------------
def _setting_view(key: str, val: Optional[str]) -> dict[str, Any]:
    if not val:
        return {"set": False, "value": ""}
    if key in _SECRET_KEYS:
        hint = f"{val[:4]}…{val[-2:]}" if len(val) > 8 else "configurado"
        return {"set": True, "value": "", "hint": hint}
    return {"set": True, "value": val}  # não-secreto: mostra inteiro


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    s = store.get_settings()
    return {k: _setting_view(k, s.get(k)) for k in SETTING_KEYS}


class SettingsReq(BaseModel):
    adspower_api_key: Optional[str] = None
    cloudflare_account_id: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    cloudflare_worker_name: Optional[str] = None
    default_template: Optional[str] = None


@app.post("/api/settings")
def post_settings(req: SettingsReq) -> dict[str, Any]:
    store.set_settings(req.model_dump())
    return get_settings()


# ---------------------------------------------------------------------------
# Reiniciar o serviço — recarrega o .env (ex.: nova sessão do SMS,
# NUMERO_VIRTUAL_CUSTOMER_SESSION, que expira ~a cada 7 dias). O estado durável
# (runs, BMs, pools) vive no SQLite, então o restart não perde nada; runs em
# ANDAMENTO (navegador aberto) são interrompidas — por isso a UI confirma antes.
#
# Mecanismo: sob `uvicorn --reload` (o padrão do INICIAR_BACKEND.bat) basta
# tocar um .py vigiado que o reloader sobe um worker novo relendo o .env. Sem o
# reloader, faz exec de um uvicorn novo na mesma porta. Em ambos os casos o
# navegador reconecta sozinho quando o backend volta (porta 8000 inalterada).
# ---------------------------------------------------------------------------
from pathlib import Path as _Path  # noqa: E402

_RELOAD_TRIGGER = _Path(__file__).resolve().parent / "_restart_trigger.py"


def _perform_restart() -> None:
    import multiprocessing  # noqa: PLC0415
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    # Deixa a resposta HTTP chegar ao navegador antes de derrubar o processo.
    _time.sleep(0.6)

    # Sob --reload o app roda num processo-filho do reloader: tocar um .py
    # vigiado faz o reloader subir um worker novo (relendo o .env) sem conflito
    # de porta. parent_process() é None só no processo principal (sem reload).
    under_reloader = multiprocessing.parent_process() is not None
    if under_reloader:
        try:
            _RELOAD_TRIGGER.write_text(
                f"# gatilho de reload do painel - {_time.time()}\n",
                encoding="utf-8",
            )
            return
        except OSError:
            pass  # sem permissão de escrita? cai no exec abaixo.

    # Fallback (backend iniciado sem --reload): substitui o processo por um
    # uvicorn novo — re-importa tudo e relê o .env.
    os.execv(
        sys.executable,
        [
            sys.executable, "-m", "uvicorn", "api_server:app",
            "--host", "0.0.0.0", "--port", "8000", "--reload",
        ],
    )


@app.post("/api/system/restart")
def system_restart() -> dict[str, Any]:
    """Reinicia o backend para recarregar o .env. Responde na hora e dispara o
    restart num thread; o painel espera o /api/health voltar e recarrega."""
    import threading  # noqa: PLC0415

    active = _manager.active_count()
    logger.warning(
        "Restart do backend solicitado pelo painel (jobs ativos=%s).", active
    )
    threading.Thread(target=_perform_restart, daemon=True).start()
    return {"ok": True, "restarting": True, "active_jobs": active}


# ---------------------------------------------------------------------------
# APIs — editor visual das chaves do .env (aba "APIs" do painel). Deixa o
# operador (ou um amigo usando o bot) trocar TODAS as chaves de serviço que
# gastam saldo — SMS, captcha, consulta de CNPJ, WABA, AdsPower — sem abrir o
# .env na mão. Grava direto no .env (fonte que o config.py relê no restart);
# a UI chama /system/restart em seguida. Domínios Netlify/Cloudflare NÃO
# entram aqui: têm fluxo próprio (pool + nameservers) na aba Credenciais.
# ---------------------------------------------------------------------------
API_REGISTRY: list[dict[str, Any]] = [
    {
        "id": "sms",
        "label": "SMS — Verificação por telefone",
        "desc": "Compra o número que recebe o código do Meta. Gasta saldo por número.",
        "required": True,
        "combos": [
            ["NUMERO_VIRTUAL_API_KEY", "NUMERO_VIRTUAL_CUSTOMER_SESSION"],
            ["SMS24H_ORG_API_KEY"],
            ["SMSRUSH_API_TOKEN"],
        ],
        "fields": [
            {"key": "NUMERO_VIRTUAL_API_KEY", "label": "numero-virtual — API Key",
             "required": True,
             "site": "app.numero-virtual.com",
             "url": "https://app.numero-virtual.com",
             "get": "Menu Developers → API Key (começa com nv_live_…).",
             "help": "É o token principal do provedor de números."},
            {"key": "NUMERO_VIRTUAL_CUSTOMER_SESSION", "label": "numero-virtual — Customer Session (cookie)",
             "required": True,
             "site": "app.numero-virtual.com",
             "url": "https://app.numero-virtual.com",
             "get": "Logado, F12 → Application → Cookies → copie o customer_session (JWT eyJ…).",
             "help": "EXPIRA ~7 dias — precisa renovar de tempos em tempos."},
            {"key": "SMS24H_ORG_API_KEY", "label": "SMS24H — API Key",
             "required": False,
             "site": "sms24h.org",
             "url": "https://sms24h.org",
             "get": "Logado → Perfil → API Key.",
             "help": "Chave para o botão SMS24H na aba Disparar. Facebook R$0,80."},
            {"key": "SMSRUSH_API_TOKEN", "label": "SMSRush — Token JWT",
             "required": False,
             "site": "smsrush.com.br",
             "url": "https://smsrush.com.br",
             "get": "Logado → Integrações → Documentação → Authorization Token (JWT).",
             "help": "Token JWT para o botão SMSRush na aba Disparar. Expira ~24h."},
        ],
    },
    {
        "id": "captcha",
        "label": "Captcha",
        "desc": "Resolve os reCAPTCHA do Meta. Gasta saldo por captcha resolvido.",
        "required": True,
        "combos": [["CAPSOLVER_API_KEY"], ["TWO_CAPTCHA_API_KEY"]],
        "fields": [
            {"key": "CAPSOLVER_API_KEY", "label": "CapSolver — API Key",
             "required": True,
             "site": "dashboard.capsolver.com",
             "url": "https://dashboard.capsolver.com",
             "get": "Dashboard → API Key (começa com CAP-…).",
             "help": "NUNCA use o caractere # no valor."},
            {"key": "TWO_CAPTCHA_API_KEY", "label": "2Captcha — API Key (alternativo)",
             "required": False, "alt": True,
             "site": "2captcha.com",
             "url": "https://2captcha.com/enterpage",
             "get": "Dashboard → API Key.",
             "help": "Só se usar o 2Captcha no lugar do CapSolver."},
        ],
    },
    {
        "id": "cnpj",
        "label": "Consulta de CNPJ",
        "desc": "Puxa os dados cadastrais do CNPJ (razão social, endereço, telefone).",
        "required": True,
        "combos": [["INFOSIMPLES_API_TOKEN"], ["INVERTTEXTO_API_TOKEN"]],
        "fields": [
            {"key": "INFOSIMPLES_API_TOKEN", "label": "InfoSimples — API Token",
             "required": True,
             "site": "infosimples.com.br",
             "url": "https://infosimples.com.br",
             "get": "Painel → conta → Token da API.",
             "help": "Fonte principal (dados da Receita). Gasta créditos por consulta."},
            {"key": "INVERTTEXTO_API_TOKEN", "label": "Invertexto — API Token",
             "required": False, "alt": True,
             "site": "invertexto.com",
             "url": "https://invertexto.com/api",
             "get": "Cadastro → API → gere o Token.",
             "help": "Apoio/fallback da consulta de CNPJ."},
        ],
    },
    {
        "id": "cnpj_lista",
        "label": "Casa dos Dados — Gerar lista de CNPJ",
        "desc": "Busca CNPJs por filtros (aba Disparar) p/ abastecer o pool. Paga (~R$0,01/CNPJ; trial 7d/200).",
        "required": False,
        "combos": [["CASADOSDADOS_API_KEY"]],
        "fields": [
            {"key": "CASADOSDADOS_API_KEY", "label": "Casa dos Dados — API Key",
             "required": False,
             "site": "casadosdados.com.br",
             "url": "https://casadosdados.com.br",
             "get": "portal.casadosdados.com.br → conta → API (header api-key).",
             "help": "Opcional — só p/ gerar listas de CNPJ na aba Disparar."},
        ],
    },
    {
        "id": "waba",
        "label": "DataCrazy — WhatsApp Business",
        "desc": "Registro da WABA. Só é usado quando CRIAR_WABA=true.",
        "required": False,
        "combos": [["DATACRAZY_API_KEY"]],
        "fields": [
            {"key": "DATACRAZY_API_KEY", "label": "DataCrazy — API Key",
             "required": False,
             "site": "datacrazy.io",
             "url": "https://app.datacrazy.io",
             "get": "Painel → API → API Key (começa com dc_…).",
             "help": "Opcional — só se você cria conta de WhatsApp Business (CRIAR_WABA=true)."},
        ],
    },
    {
        "id": "browser",
        "label": "AdsPower — Navegador",
        "desc": "O AdsPower abre os perfis e é ESSENCIAL estar ABERTO p/ rodar. Só a chave abaixo é opcional — teste a conexão no botão \"Verificar APIs\".",
        "required": False,
        "combos": [["ADSPOWER_API_KEY"]],
        "fields": [
            {"key": "ADSPOWER_API_URL", "label": "AdsPower — Ligação (URL da API local)",
             "required": True, "secret": False, "default": "http://127.0.0.1:50325",
             "site": "AdsPower → Configurações de API → Ligação",
             "get": "Copie a URL de \"Ligação\" (ex.: http://127.0.0.1:50325).",
             "help": "É ISTO que conecta o AdsPower ao bot. A porta muda de máquina p/ máquina — cole a EXATA que aparece no seu AdsPower. Padrão: http://127.0.0.1:50325."},
            {"key": "ADSPOWER_API_KEY", "label": "AdsPower — API Key",
             "required": False,
             "site": "AdsPower (app na sua máquina)",
             "url": "https://www.adspower.com",
             "get": "AdsPower → Configurações de API → API Key.",
             "help": "Opcional: só precisa se você LIGAR \"Verificação de API\" no AdsPower. Desligada (padrão), deixe em branco — a conexão funciona mesmo assim."},
        ],
    },
    {
        "id": "ixbrowser",
        "label": "ixBrowser — Navegador",
        "desc": "Alternativa ao AdsPower. Selecione na aba Disparar qual multi-login usar. Sem chave — basta abrir o ixBrowser.",
        "required": False,
        "combos": [["IXBROWSER_API_URL"]],
        "fields": [
            {"key": "IXBROWSER_API_URL", "label": "ixBrowser — URL da API local",
             "required": True, "secret": False, "default": "http://127.0.0.1:53200",
             "site": "ixBrowser (app na sua máquina)",
             "get": "Padrão: http://127.0.0.1:53200 — mude só se alterou a porta no ixBrowser.",
             "help": "Conecta o bot ao ixBrowser. A porta padrão é 53200; só altere se mudou nas configurações do ixBrowser."},
        ],
    },
]

# Chaves que TAMBÉM têm override no store (painel) — precisam ser espelhadas ali
# senão o override antigo mascararia o novo valor do .env. Mapa ENV → store key.
_API_STORE_MIRROR = {"ADSPOWER_API_KEY": "adspower_api_key"}
_API_WHITELIST = {f["key"] for g in API_REGISTRY for f in g["fields"]}
# Só as chaves SECRETAS (paga/credencial) entram no "Limpar todas as APIs".
# Campos não-secretos (ex.: a URL de Ligação do AdsPower) NÃO são apagados —
# não vazam saldo e a próxima pessoa precisa da conexão funcionando.
_API_SECRET_KEYS = {
    f["key"] for g in API_REGISTRY for f in g["fields"] if f.get("secret") is not False
}
_ENV_PATH = _Path(__file__).resolve().parent / ".env"


def _read_env_raw() -> dict[str, str]:
    """Lê o .env como {KEY: value} igual ao python-dotenv: ignora comentários de
    linha, tira aspas de valor quotado e CORTA comentário inline (`valor  # ...`)
    dos valores não-quotados. Sem isso, uma linha como
    `ADSPOWER_API_URL=http://127.0.0.1:50325  # comentário` viria com o comentário
    grudado (a URL fica inválida)."""
    import re as _re
    vals: dict[str, str] = {}
    try:
        text = _ENV_PATH.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return vals
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]  # valor quotado: mantém tudo dentro das aspas
        else:
            m = _re.search(r"\s#", v)  # comentário inline começa com espaço + #
            if m:
                v = v[:m.start()].rstrip()
        vals[k.strip()] = v
    return vals


def _update_env_file(updates: dict[str, str]) -> None:
    """Reescreve SÓ as linhas das chaves em `updates`, preservando todo o resto
    do .env (PANEL_*, ajustes de timing, comentários). Chave inexistente é
    anexada no fim. Não apaga nada — só grava valores não-vazios."""
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _apis_snapshot() -> dict[str, Any]:
    """Estado atual das APIs para a UI — valor mascarado, nunca em claro.

    Além dos campos, calcula por grupo se ele está `connected` (satisfaz ao
    menos um `combo` de chaves) e um resumo `checker` p/ o painel mostrar o que
    já está conectado e o que falta pro fluxo rodar."""
    raw = _read_env_raw()

    def _combo_ok(combo: list[str]) -> bool:
        return all((raw.get(k) or "").strip() for k in combo)

    groups: list[dict[str, Any]] = []
    for g in API_REGISTRY:
        fields = []
        for f in g["fields"]:
            v = (raw.get(f["key"]) or "").strip()
            if f.get("secret") is False:
                # Campo NÃO-secreto (ex.: URL de Ligação): mostra o valor real,
                # editável, caindo no default quando o .env não tem a linha.
                shown = v or (f.get("default") or "")
                fields.append({**f, "set": bool(shown), "hint": "", "value": shown})
            elif v:
                hint = f"{v[:4]}…{v[-3:]}" if len(v) > 9 else "configurado"
                fields.append({**f, "set": True, "hint": hint})
            else:
                fields.append({**f, "set": False, "hint": ""})
        combos = g.get("combos") or [[f["key"]] for f in g["fields"]]
        connected = any(_combo_ok(c) for c in combos)
        groups.append({
            "id": g["id"], "label": g["label"], "desc": g["desc"],
            "required": bool(g.get("required")), "connected": connected,
            "fields": fields,
        })

    required = [g for g in groups if g["required"]]
    optional = [g for g in groups if not g["required"]]
    missing = [g["label"] for g in required if not g["connected"]]
    checker = {
        "ready": len(missing) == 0,
        "required_total": len(required),
        "required_connected": sum(1 for g in required if g["connected"]),
        "missing": missing,
        "optional_connected": [g["label"] for g in optional if g["connected"]],
    }
    return {"groups": groups, "checker": checker}


@app.get("/api/apis")
def get_apis() -> dict[str, Any]:
    return _apis_snapshot()


class ApisReq(BaseModel):
    values: dict[str, str] = {}


@app.post("/api/apis")
def post_apis(req: ApisReq) -> dict[str, Any]:
    """Grava no .env só as chaves da whitelist com valor não-vazio (campo em
    branco = mantém a atual). Espelha no store as que têm override no painel."""
    updates = {
        k: v.strip()
        for k, v in (req.values or {}).items()
        if k in _API_WHITELIST and isinstance(v, str) and v.strip()
    }
    if updates:
        _update_env_file(updates)
        mirror = {
            store_key: updates[env_key]
            for env_key, store_key in _API_STORE_MIRROR.items()
            if env_key in updates
        }
        if mirror:
            store.set_settings(mirror)
        logger.warning(
            "APIs atualizadas pelo painel: %s (restart recarrega o .env).",
            ", ".join(sorted(updates.keys())),
        )
    return {"ok": True, "updated": sorted(updates.keys()), **_apis_snapshot()}


def _clear_env_keys(keys: set[str]) -> None:
    """Zera (KEY=) as chaves informadas no .env, preservando todo o resto —
    comentários, PANEL_*, timings e as credenciais de domínio/hospedagem
    (Netlify/Cloudflare), que NÃO estão na whitelist e ficam intactas."""
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in keys:
                out.append(f"{k}=")
                continue
        out.append(ln)
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


@app.post("/api/apis/clear")
def clear_apis() -> dict[str, Any]:
    """Apaga só as chaves SECRETAS (SMS, captcha, CNPJ, WABA, AdsPower API Key).
    Serve p/ entregar o bot a outra pessoa sem vazar o seu saldo. NÃO toca em
    campos não-secretos (URL de Ligação do AdsPower) nem em domínios/hospedagem
    (Netlify/Cloudflare)."""
    _clear_env_keys(_API_SECRET_KEYS)
    mirror = {
        store_key: "" for env_key, store_key in _API_STORE_MIRROR.items()
        if env_key in _API_SECRET_KEYS
    }
    if mirror:
        store.set_settings(mirror)
    logger.warning(
        "APIs LIMPAS pelo painel — %d chave(s) secreta(s) zerada(s) (restart aplica).",
        len(_API_SECRET_KEYS),
    )
    return {"ok": True, "cleared": sorted(_API_SECRET_KEYS), **_apis_snapshot()}


# ---------------------------------------------------------------------------
# Verificador ao vivo das APIs (botão "Verificar APIs"). Testa de verdade as
# chaves que dá pra testar de graça — SMS (saldo), captcha (saldo), AdsPower
# (ping na API local) — e confere presença nas de consulta (CNPJ/lista/WABA),
# que gastariam crédito se fossem consultadas. Reflete o estado do bot RODANDO
# (valores que o config.py carregou no boot), então rode DEPOIS de salvar+reiniciar.
# ---------------------------------------------------------------------------
_EXTRA_LABELS = {"sms24h_org": "SMS24H.org", "smsrush": "SMSRush"}

def _api_label(group_id: str) -> str:
    for g in API_REGISTRY:
        if g["id"] == group_id:
            return g["label"]
    return _EXTRA_LABELS.get(group_id, group_id)


def _api_required(group_id: str) -> bool:
    for g in API_REGISTRY:
        if g["id"] == group_id:
            return bool(g.get("required"))
    return False


def _api_result(group_id: str, status: str, detail: str, *, live: bool) -> dict[str, Any]:
    """status: ok | fail | missing | warn."""
    return {
        "id": group_id, "label": _api_label(group_id),
        "required": _api_required(group_id),
        "status": status, "detail": detail, "live": live,
    }


async def _check_sms(raw: dict[str, str]) -> dict[str, Any]:
    from core.api_clients import Sms24hClient
    key = (raw.get("NUMERO_VIRTUAL_API_KEY") or raw.get("SMS24H_API_KEY") or "").strip()
    if not key:
        return _api_result("sms", "missing", "numero-virtual sem API Key.", live=False)
    client = Sms24hClient(api_key=key)
    err: Exception | None = None
    bal: float | None = None
    try:
        bal = await client.get_balance()
    except Exception as exc:  # noqa: BLE001
        err = exc
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    if err is not None or bal is None:
        return _api_result("sms", "fail",
                           f"chave recusada/erro no saldo: {type(err).__name__ if err else 'sem resposta'}",
                           live=True)
    has_session = bool((raw.get("NUMERO_VIRTUAL_CUSTOMER_SESSION") or "").strip())
    if bal <= 0:
        return _api_result("sms", "fail", f"saldo zerado (R$ {bal:.2f}) — recarregue.", live=True)
    if not has_session:
        return _api_result("sms", "warn",
                           f"saldo R$ {bal:.2f}, mas falta o Customer Session (cookie) p/ comprar número.",
                           live=True)
    return _api_result("sms", "ok", f"conectada · saldo R$ {bal:.2f}", live=True)


async def _check_sms24h_org(raw: dict[str, str]) -> dict[str, Any]:
    from core.api_clients import Sms24hOrgClient
    key = (raw.get("SMS24H_ORG_API_KEY") or "").strip()
    if not key:
        return _api_result("sms24h_org", "missing", "sms24h.org sem API Key.", live=False)
    client = Sms24hOrgClient(api_key=key)
    err: Exception | None = None
    bal: float | None = None
    try:
        bal = await client.get_balance()
    except Exception as exc:  # noqa: BLE001
        err = exc
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    if err is not None or bal is None:
        return _api_result("sms24h_org", "fail",
                           f"chave recusada/erro: {type(err).__name__ if err else 'sem resposta'}",
                           live=True)
    if bal <= 0:
        return _api_result("sms24h_org", "fail", f"saldo zerado (R$ {bal:.2f}).", live=True)
    return _api_result("sms24h_org", "ok", f"conectada · saldo R$ {bal:.2f}", live=True)


async def _check_smsrush(raw: dict[str, str]) -> dict[str, Any]:
    from core.api_clients import SmsRushClient
    token = (raw.get("SMSRUSH_API_TOKEN") or "").strip()
    if not token:
        return _api_result("smsrush", "missing", "SMSRush sem token.", live=False)
    client = SmsRushClient(api_token=token)
    err: Exception | None = None
    bal: float | None = None
    try:
        bal = await client.get_balance()
    except Exception as exc:  # noqa: BLE001
        err = exc
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    if err is not None or bal is None:
        return _api_result("smsrush", "fail",
                           f"token recusado/erro: {type(err).__name__ if err else 'sem resposta'}",
                           live=True)
    if bal <= 0:
        return _api_result("smsrush", "fail", f"saldo zerado (R$ {bal:.2f}).", live=True)
    return _api_result("smsrush", "ok", f"conectada · saldo R$ {bal:.2f}", live=True)


async def _check_captcha(raw: dict[str, str]) -> dict[str, Any]:
    import httpx as _httpx
    import config as _cfg
    cap = (raw.get("CAPSOLVER_API_KEY") or "").strip()
    two = (raw.get("TWO_CAPTCHA_API_KEY") or "").strip()
    if cap:
        try:
            base = (getattr(_cfg, "CAPSOLVER_BASE_URL", "https://api.capsolver.com") or "").rstrip("/")
            async with _httpx.AsyncClient(timeout=15) as cl:
                r = await cl.post(f"{base}/getBalance", json={"clientKey": cap})
                d = r.json()
            if d.get("errorId"):
                return _api_result("captcha", "fail",
                                   f"CapSolver recusou: {d.get('errorDescription') or d.get('errorCode') or 'chave inválida'}",
                                   live=True)
            bal = float(d.get("balance") or 0)
            if bal <= 0:
                return _api_result("captcha", "fail", f"CapSolver saldo US$ {bal:.2f} — recarregue.", live=True)
            return _api_result("captcha", "ok", f"CapSolver OK · US$ {bal:.2f}", live=True)
        except Exception as exc:  # noqa: BLE001
            return _api_result("captcha", "fail", f"CapSolver erro: {type(exc).__name__}", live=True)
    if two:
        try:
            base = (getattr(_cfg, "TWO_CAPTCHA_BASE_URL", "https://2captcha.com") or "").rstrip("/")
            async with _httpx.AsyncClient(timeout=15) as cl:
                r = await cl.get(f"{base}/res.php", params={"key": two, "action": "getbalance", "json": 1})
                d = r.json()
            if str(d.get("status")) == "1":
                return _api_result("captcha", "ok", f"2Captcha OK · US$ {float(d.get('request') or 0):.2f}", live=True)
            return _api_result("captcha", "fail", f"2Captcha recusou: {d.get('request')}", live=True)
        except Exception as exc:  # noqa: BLE001
            return _api_result("captcha", "fail", f"2Captcha erro: {type(exc).__name__}", live=True)
    return _api_result("captcha", "missing", "sem chave de captcha (CapSolver ou 2Captcha).", live=False)


async def _check_cnpj(raw: dict[str, str]) -> dict[str, Any]:
    info = (raw.get("INFOSIMPLES_API_TOKEN") or "").strip()
    inv = (raw.get("INVERTTEXTO_API_TOKEN") or "").strip()
    if info:
        return _api_result("cnpj", "ok", "InfoSimples · chave presente (consulta gastaria crédito, não testada).", live=False)
    if inv:
        return _api_result("cnpj", "ok", "Invertexto · chave presente.", live=False)
    return _api_result("cnpj", "missing", "sem token de consulta de CNPJ (InfoSimples ou Invertexto).", live=False)


async def _check_cnpj_lista(raw: dict[str, str]) -> dict[str, Any]:
    key = (raw.get("CASADOSDADOS_API_KEY") or "").strip()
    if key:
        return _api_result("cnpj_lista", "ok", "Casa dos Dados · chave presente.", live=False)
    return _api_result("cnpj_lista", "warn", "não configurada — só precisa p/ gerar listas de CNPJ.", live=False)


async def _check_waba(raw: dict[str, str]) -> dict[str, Any]:
    key = (raw.get("DATACRAZY_API_KEY") or "").strip()
    if key:
        return _api_result("waba", "ok", "DataCrazy · chave presente.", live=False)
    return _api_result("waba", "warn", "não configurada — só precisa se CRIAR_WABA=true.", live=False)


async def _check_browser(raw: dict[str, str]) -> dict[str, Any]:
    import config as _cfg
    from core.adspower_client import AdsPowerClient
    base = (raw.get("ADSPOWER_API_URL") or getattr(_cfg, "ADSPOWER_API_URL", "") or "http://127.0.0.1:50325").strip()
    key = (raw.get("ADSPOWER_API_KEY") or "").strip()
    client = AdsPowerClient(base_url=base, api_key=key)
    try:
        res = await client.verify()
    except Exception as exc:  # noqa: BLE001
        return _api_result("browser", "warn",
                           f"AdsPower não respondeu em {base} ({type(exc).__name__}) — abra o app / confira a Ligação.",
                           live=True)
    if res.get("ok"):
        return _api_result("browser", "ok", f"Ligação {base} · {res.get('message') or 'conectado'}", live=True)
    return _api_result("browser", "warn", f"AdsPower ({base}): {res.get('message') or 'API local não respondeu'}", live=True)


async def _check_ixbrowser(_raw: dict[str, str]) -> dict[str, Any]:
    import config as _cfg
    from core.ixbrowser_client import IXBrowserClient
    base = getattr(_cfg, "IXBROWSER_API_URL", "http://127.0.0.1:53200")
    client = IXBrowserClient(base_url=base)
    try:
        res = await client.verify()
    except Exception as exc:  # noqa: BLE001
        return _api_result("ixbrowser", "warn",
                           f"ixBrowser não respondeu em {base} ({type(exc).__name__}) — abra o app.",
                           live=True)
    if res.get("ok"):
        return _api_result("ixbrowser", "ok", f"Ligação {base} · {res.get('message') or 'conectado'}", live=True)
    return _api_result("ixbrowser", "warn", f"ixBrowser ({base}): {res.get('message') or 'API local não respondeu'}", live=True)


@app.post("/api/apis/check")
async def check_apis() -> dict[str, Any]:
    """Testa cada API ao vivo (onde dá de graça) e diz o que está OK, o que
    falta e o que está com problema. Não gasta crédito das consultas de CNPJ."""
    import asyncio as _asyncio
    raw = _read_env_raw()  # mesma fonte do painel (o .env), evita divergir do config
    checks = [
        _check_sms(raw), _check_sms24h_org(raw), _check_smsrush(raw),
        _check_captcha(raw),
        _check_cnpj(raw), _check_cnpj_lista(raw), _check_waba(raw),
        _check_browser(raw), _check_ixbrowser(raw),
    ]
    raw = await _asyncio.gather(*checks, return_exceptions=True)
    order = ["sms", "sms24h_org", "smsrush", "captcha", "cnpj", "cnpj_lista", "waba", "browser", "ixbrowser"]
    results: list[dict[str, Any]] = []
    for gid, item in zip(order, raw):
        if isinstance(item, Exception):
            results.append(_api_result(gid, "fail", f"erro no teste: {type(item).__name__}", live=True))
        else:
            results.append(item)
    problems = [
        r for r in results
        if r["status"] == "fail" or (r["status"] == "missing" and r["required"])
    ]
    warns = [r for r in results if r["status"] == "warn"]
    required = [r for r in results if r["required"]]
    return {
        "ok": len(problems) == 0,
        "results": results,
        "summary": {
            "required_total": len(required),
            "required_ok": sum(1 for r in required if r["status"] == "ok"),
            "problems": [f'{r["label"]}: {r["detail"]}' for r in problems],
            "warnings": [f'{r["label"]}: {r["detail"]}' for r in warns],
        },
    }


# ---------------------------------------------------------------------------
# Netlify — domínios (host atual). Cada CNPJ vira um subdomínio servido por
# uma Edge Function. O operador cadastra: domínio + token + cap (limite). O
# sistema cria a zona DNS, mostra os nameservers (p/ colocar na Hostinger) e
# checa a propagação. Quando propaga (dns_ok), o domínio entra em uso.
# ---------------------------------------------------------------------------
def _netlify_domain_view(d: dict[str, Any]) -> dict[str, Any]:
    """Serializa um domínio para a UI (token mascarado)."""
    return {
        "domain": d["domain"],
        "api_token_masked": _mask(d.get("api_token")),
        "account_slug": d.get("account_slug") or "",
        "nameservers": d.get("nameservers") or [],
        "cap": d.get("cap"),
        "sites_used": d.get("sites_used"),
        "remaining": d.get("remaining"),
        "dns_ok": d.get("dns_ok"),
        "created_at": d.get("created_at"),
    }


@app.get("/api/netlify/domains")
def netlify_domains_list() -> list[dict[str, Any]]:
    """Lista os domínios cadastrados (com uso, cota e nameservers)."""
    return [_netlify_domain_view(d) for d in store.list_netlify_domains()]


@app.get("/api/netlify/usage")
def netlify_usage() -> dict[str, Any]:
    """Resumo do domínio ativo (em uso agora) — quantos sites ainda restam."""
    active = store.get_active_netlify_domain()
    if not active:
        domains = store.list_netlify_domains()
        return {"configured": bool(domains), "active": None, "domains": len(domains)}
    return {
        "configured": True,
        "active": active["domain"],
        "sites_used": active["sites_used"],
        "cap": active["cap"],
        "remaining": active["remaining"],
    }


class NetlifyDomainReq(BaseModel):
    domain: str
    api_token: str
    cap: int = 10


@app.post("/api/netlify/domains")
def netlify_domains_add(req: NetlifyDomainReq) -> dict[str, Any]:
    """Adiciona um domínio: valida o token, cria a zona DNS na Netlify, obtém os
    nameservers e checa se já propagaram. Retorna o domínio + nameservers + status.

    O operador então coloca esses nameservers na Hostinger. Enquanto não propagar,
    o domínio fica com ``dns_ok=false`` e não é usado nas runs."""
    from core.netlify_deployer import (  # noqa: PLC0415
        NetlifyDeployer, NetlifyDeployerError, check_domain_nameservers,
    )

    domain = (req.domain or "").strip().lower().strip(".")
    token = (req.api_token or "").strip()
    if not domain or "." not in domain:
        raise HTTPException(400, "Informe um domínio válido (ex.: meudominio.com.br).")
    if not token:
        raise HTTPException(400, "Cole o token (Personal Access Token) da Netlify.")

    dep = NetlifyDeployer(api_token=token)
    creds = asyncio.run(dep.test_creds())
    if not creds.get("ok"):
        raise HTTPException(400, f"Token Netlify inválido: {creds.get('message')}")

    try:
        zone = asyncio.run(dep.ensure_dns_zone(domain))
    except NetlifyDeployerError as e:
        raise HTTPException(400, str(e)) from e

    nameservers = zone.get("nameservers") or []
    ns = check_domain_nameservers(domain, nameservers)
    propagated = bool(ns["propagated"])
    # Tolerante: "pronto" a menos que os NS apontem comprovadamente pra fora.
    dns_ok = not (ns["found"] and not ns["propagated"])

    store.add_netlify_domain(
        domain, token,
        cap=req.cap,
        account_slug=creds.get("account_slug") or "",
        zone_id=zone.get("zone_id") or "",
        nameservers=nameservers,
        dns_ok=dns_ok,
    )

    # Já cria o site ÚNICO do domínio + alias *.dominio e dispara o certificado
    # WILDCARD agora (assim, quando os CNPJs rodarem, o HTTPS já está pronto —
    # zero espera/erro de SSL por CNPJ). Best-effort: não derruba o cadastro.
    try:
        site_id = asyncio.run(dep.ensure_domain_site(domain))
    except Exception as exc:  # noqa: BLE001
        site_id = ""
        import logging as _lg  # noqa: PLC0415
        _lg.getLogger("meta_automation.api").warning(
            "ensure_domain_site(%s) falhou no cadastro (segue): %s", domain, exc
        )

    row = store.get_netlify_domain(domain)
    out = _netlify_domain_view(row) if row else {}
    out["propagated"] = propagated
    out["ns_found"] = ns["found"]
    out["created_zone"] = zone.get("created")
    out["site_id"] = site_id
    return out


@app.get("/api/netlify/domains/{domain}/status")
def netlify_domain_status(domain: str) -> dict[str, Any]:
    """Re-checa AO VIVO se os nameservers do domínio já propagaram para a Netlify
    e atualiza ``dns_ok``. Use para o operador acompanhar a propagação."""
    from core.netlify_deployer import check_domain_nameservers  # noqa: PLC0415

    row = store.get_netlify_domain(domain)
    if not row:
        raise HTTPException(404, f"Domínio '{domain}' não cadastrado.")
    nameservers = row.get("nameservers") or []
    ns = check_domain_nameservers(row["domain"], nameservers)
    propagated = bool(ns["propagated"])
    # Tolerante: "pronto" a menos que os NS apontem comprovadamente pra fora.
    dns_ok = not (ns["found"] and not ns["propagated"])
    store.netlify_set_dns_ok(row["domain"], dns_ok)
    return {
        "domain": row["domain"],
        "nameservers": nameservers,
        "ns_found": ns["found"],
        "propagated": propagated,
        "dns_ok": dns_ok,
    }


@app.delete("/api/netlify/domains/{domain}")
def netlify_domains_delete(domain: str) -> list[dict[str, Any]]:
    """Remove um domínio do pool."""
    store.delete_netlify_domain(domain)
    return netlify_domains_list()


@app.post("/api/netlify/domains/{domain}/reset")
def netlify_domains_reset(domain: str) -> list[dict[str, Any]]:
    """Zera o contador de sites de um domínio (recomeça a cota)."""
    store.netlify_usage_reset(domain)
    return netlify_domains_list()


class DomainQuotaReq(BaseModel):
    remaining: Optional[int] = None
    cap: Optional[int] = None


@app.post("/api/netlify/domains/{domain}/quota")
def netlify_domains_quota(domain: str, req: DomainQuotaReq) -> list[dict[str, Any]]:
    """Ajusta vagas restantes e/ou o teto do domínio. A contagem automática segue."""
    if req.remaining is None and req.cap is None:
        raise HTTPException(400, "Informe remaining e/ou cap.")
    row = store.netlify_set_quota(domain, remaining=req.remaining, cap=req.cap)
    if not row:
        raise HTTPException(404, f"Domínio '{domain}' não cadastrado.")
    return netlify_domains_list()


def _cloudflare_domain_view(d: dict) -> dict:
    """Serializa um domínio Cloudflare para a UI (token mascarado)."""
    return {
        "domain": d["domain"],
        "account_id": d.get("account_id") or "",
        "api_token_masked": _mask(d.get("api_token")),
        "nameservers": d.get("nameservers") or [],
        "zone_id": d.get("zone_id") or "",
        "cap": d.get("cap"),
        "sites_used": d.get("sites_used"),
        "remaining": d.get("remaining"),
        "dns_ok": d.get("dns_ok"),
        "created_at": d.get("created_at"),
        "priority": d.get("priority", 0),
    }


@app.get("/api/hosting/provider")
def hosting_provider_get() -> dict:
    """Provedor de hospedagem ativo: 'netlify' (padrão) ou 'cloudflare'."""
    return {"provider": store.get_hosting_provider()}


class HostingProviderReq(BaseModel):
    provider: str


@app.post("/api/hosting/provider")
def hosting_provider_set(req: HostingProviderReq) -> dict:
    """Troca o provedor ativo. Netlify continua sendo o padrão."""
    prov = (req.provider or "").strip().lower()
    if prov not in ("netlify", "cloudflare"):
        raise HTTPException(400, "provider deve ser 'netlify' ou 'cloudflare'.")
    store.set_hosting_provider(prov)
    return {"provider": store.get_hosting_provider()}


@app.get("/api/cloudflare/domains")
def cloudflare_domains_list() -> list:
    """Lista os domínios Cloudflare cadastrados (uso, cota e nameservers)."""
    return [_cloudflare_domain_view(d) for d in store.list_cloudflare_domains()]


class CloudflareDomainReq(BaseModel):
    domain: str
    account_id: str
    api_token: str
    cap: int = 100


@app.post("/api/cloudflare/domains")
def cloudflare_domains_add(req: CloudflareDomainReq) -> dict:
    """Adiciona um domínio Cloudflare: cria (ou lê) a zona na conta, obtém os
    nameservers e checa se já propagaram (status == active). Retorna o domínio
    + nameservers + status. O operador coloca esses NS na Hostinger."""
    from core.cloudflare_deployer import (  # noqa: PLC0415
        CloudflareDomainDeployer, CloudflareDeployerError,
    )

    domain = (req.domain or "").strip().lower().strip(".")
    account_id = (req.account_id or "").strip()
    token = (req.api_token or "").strip()
    if not domain or "." not in domain:
        raise HTTPException(400, "Informe um domínio válido (ex.: meudominio.com.br).")
    if not account_id:
        raise HTTPException(400, "Informe o Account ID da Cloudflare.")
    if not token:
        raise HTTPException(400, "Cole o API Token da Cloudflare.")

    dep = CloudflareDomainDeployer(account_id, token)
    try:
        zone = asyncio.run(dep.ensure_zone(domain))
    except CloudflareDeployerError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Erro ao falar com a Cloudflare: {e}") from e

    nameservers = zone.get("nameservers") or []
    propagated = (zone.get("status") or "") == "active"

    store.add_cloudflare_domain(
        domain, account_id, token,
        cap=req.cap,
        zone_id=zone.get("zone_id") or "",
        nameservers=nameservers,
        dns_ok=propagated,
    )
    row = store.get_cloudflare_domain(domain)
    out = _cloudflare_domain_view(row) if row else {}
    out["propagated"] = propagated
    out["status"] = zone.get("status")
    return out


@app.get("/api/cloudflare/domains/{domain}/status")
def cloudflare_domain_status(domain: str) -> dict:
    """Re-checa AO VIVO se a zona do domínio já está ATIVA (NS propagados) e
    atualiza dns_ok."""
    from core.cloudflare_deployer import CloudflareDomainDeployer  # noqa: PLC0415

    row = store.get_cloudflare_domain(domain)
    if not row:
        raise HTTPException(404, f"Domínio Cloudflare '{domain}' não cadastrado.")
    dep = CloudflareDomainDeployer(row.get("account_id"), row.get("api_token"))
    try:
        zone = asyncio.run(dep.find_zone(row["domain"]))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Erro ao consultar a Cloudflare: {e}") from e
    status = (zone or {}).get("status")
    nameservers = (zone or {}).get("nameservers") or row.get("nameservers") or []
    propagated = status == "active"
    store.cloudflare_domain_set_dns_ok(row["domain"], propagated)
    if zone and zone.get("zone_id"):
        store.cloudflare_domain_set_zone_id(row["domain"], zone["zone_id"])
    return {
        "domain": row["domain"],
        "nameservers": nameservers,
        "status": status,
        "propagated": propagated,
        "dns_ok": propagated,
    }


@app.delete("/api/cloudflare/domains/{domain}")
def cloudflare_domains_delete(domain: str) -> list:
    """Remove um domínio Cloudflare do pool."""
    store.delete_cloudflare_domain(domain)
    return cloudflare_domains_list()


@app.post("/api/cloudflare/domains/{domain}/reset")
def cloudflare_domains_reset(domain: str) -> list:
    """Zera o contador de sites de um domínio Cloudflare (recomeça a cota)."""
    store.cloudflare_domain_usage_reset(domain)
    return cloudflare_domains_list()


@app.post("/api/cloudflare/domains/{domain}/quota")
def cloudflare_domains_quota(domain: str, req: DomainQuotaReq) -> list:
    """Ajusta vagas restantes e/ou o teto do domínio Cloudflare."""
    if req.remaining is None and req.cap is None:
        raise HTTPException(400, "Informe remaining e/ou cap.")
    row = store.cloudflare_set_quota(domain, remaining=req.remaining, cap=req.cap)
    if not row:
        raise HTTPException(404, f"Domínio Cloudflare '{domain}' não cadastrado.")
    return cloudflare_domains_list()


class CloudflareDomainReorderReq(BaseModel):
    domains: list[str]


@app.post("/api/cloudflare/domains/reorder")
def cloudflare_domains_reorder(req: CloudflareDomainReorderReq) -> list:
    """Reordena prioridade dos domínios Cloudflare (drag-and-drop)."""
    store.reorder_cloudflare_domains(req.domains)
    return cloudflare_domains_list()


class ConcurrencyReq(BaseModel):
    n: int


@app.post("/api/jobs/concurrency")
def set_concurrency(req: ConcurrencyReq) -> dict[str, Any]:
    applied = _manager.set_concurrency(max(1, min(MAX_CONCURRENCY, req.n)))
    return {"applied": applied, "concurrency": _manager.concurrency}


class AutopilotReq(BaseModel):
    enabled: bool
    runs_per_cycle: int = 1
    total_cycles: int = 3
    use_proxy: bool = True
    proxy_type: str = "datacenter"
    sms_option: Optional[str] = None
    sms_ddd: Optional[str] = None
    smsrush_server: Optional[int] = None
    sms_fallback_option: Optional[str] = None
    dual_bm: bool = False
    browser_provider: str = "adspower"
    waba_enabled: bool = False


@app.post("/api/jobs/autopilot")
def set_autopilot(req: AutopilotReq) -> dict[str, Any]:
    """Liga/desliga piloto automático (CICLOS SEQUENCIAIS).

    RUN = runs simultâneas por ciclo (runs_per_cycle).
    PILOTO = ciclos sequenciais (total_cycles).

    Ao LIGAR com nada rodando, já dispara o 1º ciclo na hora. Ao desligar, para
    de avançar ciclos (runs em andamento seguem até o fim)."""
    save_autopilot(
        enabled=req.enabled,
        runs_per_cycle=req.runs_per_cycle,
        total_cycles=req.total_cycles,
        current_cycle=1 if req.enabled else 0,
        use_proxy=req.use_proxy,
        proxy_type=req.proxy_type,
        sms_option=req.sms_option,
        sms_ddd=req.sms_ddd,
        smsrush_server=req.smsrush_server,
        sms_fallback_option=req.sms_fallback_option,
        dual_bm=req.dual_bm,
        browser_provider=req.browser_provider,
        waba_enabled=req.waba_enabled,
    )
    kicked = 0
    if req.enabled and _manager.active_count() == 0:
        with _autopilot_lock:
            if _manager.active_count() == 0:
                try:
                    kicked = _do_enqueue(
                        req.runs_per_cycle, req.use_proxy,
                        sms_option=req.sms_option,
                        sms_ddd=req.sms_ddd,
                        smsrush_server=req.smsrush_server,
                        sms_fallback_option=req.sms_fallback_option,
                        dual_bm=req.dual_bm,
                        browser_provider=req.browser_provider,
                    ).get("enqueued", 0)
                    logger.info(
                        "Piloto Automático LIGADO — Ciclo 1/%d: %d run(s).",
                        req.total_cycles, kicked,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Piloto Automático: falha ao disparar Ciclo 1 (%s).", exc)
    return {
        "enabled": req.enabled,
        "runs_per_cycle": req.runs_per_cycle,
        "total_cycles": req.total_cycles,
        "current_cycle": 1 if req.enabled else 0,
        "use_proxy": req.use_proxy,
        "kicked": kicked,
    }

# ══════════════════════════════════════════════════════════════════════
# GERAR SITE (aba "Gerar Site") — publicação MANUAL de um site por CNPJ
#
# Fluxo em 2 etapas, espelhando o que a run faz sozinha:
#   1. PUBLICAR  — digita o CNPJ, o backend puxa os dados (BrasilAPI, mesmo
#      cache das runs), deriva o subdomínio e publica o site na Netlify.
#   2. METATAG   — o operador cria o domínio no Meta, copia a metatag
#      <meta name="facebook-domain-verification" …>, cola aqui e salva: o
#      MESMO site é re-deployado com a tag embutida (sem perder DNS/SSL) e o
#      backend confere por HTTP se ela está no ar antes de dizer "pronto".
# ══════════════════════════════════════════════════════════════════════

_FB_TOKEN_RE = re.compile(
    r'facebook-domain-verification[^>]*content\s*=\s*["\']([A-Za-z0-9_-]+)["\']',
    re.IGNORECASE,
)


def _extract_fb_token(raw: Optional[str]) -> str:
    """Extrai o hash da metatag. Aceita a tag inteira OU só o código colado."""
    txt = (raw or "").strip()
    if not txt:
        return ""
    m = _FB_TOKEN_RE.search(txt)
    if m:
        return m.group(1).strip()
    bare = txt.strip('"').strip("'").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", bare):
        return bare
    return ""


def _manual_slug_for(data: Any, override: Optional[str] = None) -> str:
    """Subdomínio do CNPJ — MESMA derivação usada pelas runs do bot."""
    from core.netlify_deployer import slugify_label  # noqa: PLC0415
    from core.run_service import _derive_admin_full  # noqa: PLC0415
    from core import slugify_for_worker  # noqa: PLC0415

    manual = (override or "").strip().lower()
    if manual:
        return slugify_label(manual)
    admin_full = _derive_admin_full(getattr(data, "razao_social", "") or "")
    return slugify_for_worker(
        admin_full or getattr(data, "nome_fantasia", "") or ""
    )


def _active_netlify_or_400() -> dict[str, Any]:
    """Domínio Netlify ATIVO (o mesmo que as runs usam) — ou erro explicado."""
    active = store.get_active_netlify_domain()
    if not active:
        raise HTTPException(
            400,
            "Nenhum domínio Netlify com cota livre. Cadastre um domínio em "
            "Credenciais ou aumente o limite do atual.",
        )
    if not (active.get("api_token") or "").strip():
        raise HTTPException(
            400, f"O domínio {active['domain']} está sem token da Netlify."
        )
    return active


def _find_manual_site(
    *, cnpj: str = "", fqdn: str = ""
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Acha (domínio, entrada) de um site já publicado, por CNPJ ou FQDN."""
    digits = only_digits(cnpj or "")
    fq = (fqdn or "").strip().lower().strip(".")
    for dom in store.list_netlify_domains():
        for entry in store.netlify_list_site_entries(dom["domain"]):
            if fq and (entry.get("fqdn") or "").lower() == fq:
                return dom, entry
            if digits and only_digits(entry.get("cnpj") or "") == digits:
                return dom, entry
    return None, None


async def _probe_site(fqdn: str, token_hash: str = "", *, attempts: int = 3,
                      interval: float = 4.0) -> dict[str, Any]:
    """GET https://<fqdn>/ — o MESMO que o crawler do Meta faz.

    Devolve se o site responde (``site_ok``) e se a metatag com o token está
    servida (``metatag_ok``). É o que separa "publiquei" de "está no ar"."""
    import httpx  # noqa: PLC0415

    url = f"https://{fqdn}/"
    out: dict[str, Any] = {
        "url": url, "site_ok": False, "metatag_ok": False,
        "status": None, "detail": "",
    }
    for i in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get(url, headers={"Cache-Control": "no-cache"})
            body = r.text or ""
            out["status"] = r.status_code
            out["site_ok"] = r.status_code == 200 and len(body) > 200
            out["metatag_ok"] = bool(
                token_hash
                and "facebook-domain-verification" in body
                and f'content="{token_hash}"' in body
            )
            if token_hash:
                out["detail"] = (
                    "metatag no ar" if out["metatag_ok"]
                    else f"HTTP {r.status_code} sem a metatag do token"
                )
                if out["metatag_ok"]:
                    return out
            else:
                out["detail"] = (
                    "site no ar" if out["site_ok"] else f"HTTP {r.status_code}"
                )
                if out["site_ok"]:
                    return out
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"{type(exc).__name__}: {exc}"[:160]
        if i < attempts:
            await asyncio.sleep(interval)
    return out


def _netlify_http_error(exc: Exception) -> HTTPException:
    """Erro da Netlify traduzido para o operador.

    Timeout/erro de conexão NÃO é problema de token, domínio ou CNPJ: é a
    máquina não alcançando api.netlify.com. Sem esta distinção o painel só
    mostrava "Netlify: Falha ao criar site: " e o botão parecia travado."""
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in (
        "connecttimeout", "connecterror", "readtimeout", "timeout",
        "nodename nor servname", "getaddrinfo", "name or service not known",
    )):
        return HTTPException(
            504,
            "A Netlify não respondeu — timeout ao conectar em api.netlify.com "
            f"({msg[:120]}). Não é o token nem o domínio: a máquina não está "
            "alcançando a Netlify. Confira VPN/firewall/DNS e tente de novo.",
        )
    return HTTPException(502, f"Netlify: {msg}")


# ---------------------------------------------------------------------------
# Gerar Site (manual) — campos EDITÁVEIS + publish/update no Cloudflare
# Todos os campos abaixo são editáveis na UI; o site EMBUTE esses dados
# (empresa_json) em vez de fazer lookup LIVE, então editar aqui muda o site de
# verdade no próximo "Atualizar" (metatag, telefone, email, endereço, etc.).
# ---------------------------------------------------------------------------
_MANUAL_FIELDS = [
    "razao_social", "nome_fantasia", "situacao", "abertura",
    "natureza_juridica", "porte", "capital_social", "cnae_principal",
    "logradouro", "numero", "complemento", "bairro", "cidade", "uf", "cep",
    "telefone", "email",
]


def _manual_flat_from_cnpjdata(data: Any) -> dict[str, str]:
    """Campos editáveis (planos) a partir do CNPJData — autofill do preview."""
    def g(a: str) -> str:
        v = getattr(data, a, None)
        return (str(v).strip() if v is not None else "")
    return {k: g(k) for k in _MANUAL_FIELDS}


def _manual_flat_from_empresa_obj(emp: dict, fallback: dict) -> dict[str, str]:
    """Reverte o empresa_json (nested) para os campos planos — p/ EDITAR site já salvo."""
    end = emp.get("endereco") or {}
    con = emp.get("contato") or {}
    out = dict(fallback or {})
    out.update({
        "razao_social": emp.get("razaoSocial") or out.get("razao_social", ""),
        "nome_fantasia": emp.get("nomeFantasia") or out.get("nome_fantasia", ""),
        "situacao": emp.get("situacao") or out.get("situacao", ""),
        "abertura": emp.get("abertura") or out.get("abertura", ""),
        "natureza_juridica": emp.get("naturezaJuridica") or out.get("natureza_juridica", ""),
        "porte": emp.get("porte") or out.get("porte", ""),
        "capital_social": emp.get("capitalSocial") or out.get("capital_social", ""),
        "cnae_principal": emp.get("cnaePrincipal") or out.get("cnae_principal", ""),
        "logradouro": end.get("logradouro") or out.get("logradouro", ""),
        "numero": end.get("numero") or out.get("numero", ""),
        "complemento": end.get("complemento") or out.get("complemento", ""),
        "bairro": end.get("bairro") or out.get("bairro", ""),
        "cidade": end.get("cidade") or out.get("cidade", ""),
        "uf": end.get("uf") or out.get("uf", ""),
        "cep": end.get("cep") or out.get("cep", ""),
        "telefone": con.get("telefone") or out.get("telefone", ""),
        "email": con.get("email") or out.get("email", ""),
    })
    ni = "Não informado"
    return {k: ("" if v == ni else v) for k, v in out.items()}


def _manual_empresa_json(digits: str, flat: Optional[dict]) -> Optional[str]:
    """Monta o empresa_json (formato do template) a partir dos campos EDITÁVEIS.
    ``flat`` vazio → None (o caller cai no lookup do CNPJ)."""
    if not flat or not isinstance(flat, dict):
        return None

    def v(k: str, default: str = "") -> str:
        return (str(flat.get(k) or "").strip()) or default
    ni = "Não informado"
    cnpj_fmt = (
        f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
        if len(digits) == 14 else digits
    )
    empresa = {
        "cnpj": cnpj_fmt,
        "razaoSocial": v("razao_social") or ni,
        "nomeFantasia": v("nome_fantasia") or ni,
        "abertura": v("abertura") or ni,
        "situacao": v("situacao") or "Ativa",
        "naturezaJuridica": v("natureza_juridica") or ni,
        "porte": v("porte") or ni,
        "mei": ni,
        "capitalSocial": v("capital_social") or ni,
        "cnaePrincipal": v("cnae_principal") or ni,
        "atividadesSecundarias": [],
        "endereco": {
            "logradouro": v("logradouro") or ni,
            "numero": v("numero") or "S/N",
            "complemento": v("complemento"),
            "bairro": v("bairro") or ni,
            "cidade": v("cidade") or ni,
            "uf": v("uf") or ni,
            "cep": v("cep") or ni,
        },
        "contato": {"telefone": v("telefone") or ni, "email": v("email") or ni},
    }
    try:
        return json.dumps(empresa, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return None


def _active_cloudflare_or_400(domain: Optional[str] = None) -> dict[str, Any]:
    """Domínio Cloudflare escolhido (ou o ativo) — ou erro explicado."""
    dom = (domain or "").strip().lower().strip(".")
    row = None
    if dom:
        for d in store.list_cloudflare_domains():
            if d.get("domain") == dom:
                row = d
                break
        if not row:
            raise HTTPException(400, f"Domínio Cloudflare {dom!r} não cadastrado.")
    else:
        row = store.get_active_cloudflare_domain()
    if not row:
        raise HTTPException(
            400,
            "Nenhum domínio Cloudflare cadastrado/ativo. Adicione em "
            "Credenciais → Domínios Cloudflare.",
        )
    if not (row.get("api_token") and row.get("account_id")):
        raise HTTPException(400, f"Domínio {row['domain']} sem Account ID/Token.")
    return row


@app.get("/api/manual-site/preview/{cnpj}")
async def manual_site_preview(cnpj: str) -> dict[str, Any]:
    """Autofill: busca o CNPJ e devolve os campos EDITÁVEIS + domínios + se já existe."""
    digits = only_digits(cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido (precisa ter 14 dígitos).")
    try:
        data = await fetch_cnpj_cached(digits)
    except BrasilApiError as exc:
        raise HTTPException(502, f"BrasilAPI: {exc}")
    fields = _manual_flat_from_cnpjdata(data)
    slug = _manual_slug_for(data)
    provider = store.get_hosting_provider()
    try:
        if provider == "cloudflare":
            active = store.get_active_cloudflare_domain()
        else:
            active = store.get_active_netlify_domain()
    except Exception:  # noqa: BLE001
        active = None
    base = (active or {}).get("domain") or ""
    if provider == "cloudflare":
        domains = [
            {"domain": d.get("domain"), "cap": d.get("cap"),
             "used": d.get("sites_used"), "remaining": d.get("remaining")}
            for d in store.list_cloudflare_domains()
        ]
    else:
        domains = [
            {"domain": d.get("domain"), "cap": d.get("cap"),
             "used": d.get("sites_used"), "remaining": d.get("remaining")}
            for d in store.list_netlify_domains()
        ]
    existing = None
    if provider == "cloudflare":
        for s in store.cloudflare_domain_list_all_sites():
            if only_digits(s.get("cnpj") or "") == digits:
                existing = s
                break
    else:
        for dom in store.list_netlify_domains():
            for s in store.netlify_list_site_entries(dom["domain"]):
                if only_digits(s.get("cnpj") or "") == digits:
                    existing = s
                    break
            if existing:
                break
    if existing and existing.get("empresa_json"):
        try:
            fields = _manual_flat_from_empresa_obj(json.loads(existing["empresa_json"]), fields)
        except Exception:  # noqa: BLE001
            pass
    return {
        "cnpj": digits,
        "fields": fields,
        "slug": (existing or {}).get("slug") or slug,
        "base_domain": base,
        "domains": domains,
        "already": ({
            "fqdn": existing["fqdn"], "domain": existing["domain"],
            "token": existing["token"], "url": existing["url"],
            "source": existing["source"],
        } if existing else None),
    }


class ManualSiteReq(BaseModel):
    cnpj: str
    slug: Optional[str] = None
    metatag: Optional[str] = None
    domain: Optional[str] = None          # domínio-base escolhido (Cloudflare)
    fields: Optional[dict] = None         # campos editáveis (razao_social, telefone, …)
    template: Optional[str] = None        # 'classic' | 'professional' | 'nextgen' | 'premium' | 'corporate'


@app.post("/api/manual-site/publish")
async def manual_site_publish(req: ManualSiteReq) -> dict[str, Any]:
    """Publica (ou ATUALIZA) o site do CNPJ via Netlify ou Cloudflare.

    O MESMO endpoint serve pra 'Publicar' e 'Atualizar' — re-publica com os
    dados novos (empresa_json embutido) + metatag."""
    digits = only_digits(req.cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido (precisa ter 14 dígitos).")

    empresa_json = _manual_empresa_json(digits, req.fields)
    razao = (req.fields or {}).get("razao_social", "") if req.fields else ""
    if not empresa_json:
        try:
            data = await fetch_cnpj_cached(digits)
        except BrasilApiError as exc:
            raise HTTPException(502, f"BrasilAPI: {exc}")
        empresa_json = empresa_json_from_cnpjdata(data)
        razao = data.razao_social or ""

    from core.netlify_deployer import slugify_label  # noqa: PLC0415
    slug = slugify_label((req.slug or "").strip().lower())
    if not slug:
        from core.run_service import _derive_admin_full  # noqa: PLC0415
        from core import slugify_for_worker  # noqa: PLC0415
        slug = slugify_for_worker(_derive_admin_full(razao) or razao)
    if not slug:
        raise HTTPException(400, "Não consegui derivar o subdomínio — informe um manualmente.")

    token_hash = _extract_fb_token(req.metatag)
    if req.metatag and not token_hash:
        raise HTTPException(
            400,
            'Metatag não reconhecida. Cole a tag inteira (<meta name="facebook-'
            'domain-verification" content="…" />) ou só o código.',
        )

    provider = store.get_hosting_provider()

    if provider == "cloudflare":
        from core.cloudflare_deployer import (  # noqa: PLC0415
            CloudflareDomainDeployer, CloudflareDeployerError,
        )
        active = _active_cloudflare_or_400(req.domain)
        base = active["domain"]
        fqdn = f"{slug}.{base}"

        ja = any(e["slug"] == slug for e in store.cloudflare_domain_list_site_entries(base))
        if not ja and int(active.get("remaining") or 0) <= 0:
            raise HTTPException(
                409, f"Domínio {base} sem cota livre "
                     f"({active.get('sites_used')}/{active.get('cap')}).",
            )
        store.cloudflare_domain_upsert_site_entry(
            base, slug, digits, token_hash, fqdn,
            empresa_json=empresa_json, source="manual",
            template=req.template or "classic",
        )
        entries = store.cloudflare_domain_list_site_entries(base)
        dep = CloudflareDomainDeployer(active["account_id"], active["api_token"])
        if not dep.enabled:
            raise HTTPException(400, f"Domínio {base}: Account ID/Token inválidos.")
        try:
            await dep.publish_domain(base, entries)
        except CloudflareDeployerError as exc:
            raise HTTPException(502, f"Cloudflare: {exc}")
    else:
        from core.netlify_deployer import (  # noqa: PLC0415
            NetlifyDeployer, NetlifyDeployerError,
        )
        dom_name = (req.domain or "").strip().lower()
        active = store.get_netlify_domain(dom_name) if dom_name else store.get_active_netlify_domain()
        if not active:
            raise HTTPException(
                400,
                "Nenhum domínio Netlify disponível. Cadastre um domínio + token "
                "em Configurações > Domínios Netlify.",
            )
        base = active["domain"]
        fqdn = f"{slug}.{base}"

        ja = any(e["slug"] == slug for e in store.netlify_list_site_entries(base))
        if not ja and int(active.get("remaining") or 0) <= 0:
            raise HTTPException(
                409, f"Domínio {base} sem cota livre "
                     f"({active.get('sites_used')}/{active.get('cap')}).",
            )
        deployer = NetlifyDeployer(api_token=active["api_token"])
        if not deployer.enabled:
            raise HTTPException(400, f"Domínio {base}: token Netlify ausente.")
        try:
            url_result, criado = await deployer.redeploy_subdomain(
                fqdn, cnpj_digits=digits, token_hash=token_hash or "",
            )
        except NetlifyDeployerError as exc:
            raise _netlify_http_error(exc)
        store.netlify_upsert_site_entry(base, slug, digits, token_hash or "", fqdn)
        if not ja:
            store.netlify_usage_increment(base)

    probe = await _probe_site(fqdn, token_hash, attempts=3, interval=4.0)
    url = f"https://{fqdn}"

    try:
        from core.store import POOL_AVAILABLE  # noqa: PLC0415
        pool_row = store.list_cnpjs(status=POOL_AVAILABLE)
        if any(r["cnpj"] == digits for r in pool_row):
            store.delete_cnpjs([digits])
            logger.info("manual-site: CNPJ %s removido do pool (usado para site).", digits)
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "manual-site: %s publicado via %s (cnpj=%s, metatag=%s, site_ok=%s)",
        fqdn, provider, digits, bool(token_hash), probe.get("site_ok"),
    )
    return {
        "ok": True, "cnpj": digits, "slug": slug, "domain": base,
        "fqdn": fqdn, "url": url, "created": not ja, "token": token_hash,
        "probe": probe,
        "message": (
            (f"Site publicado em {url}"
             + (" já com a metatag." if token_hash
                else ". Adicione o domínio no Meta, cole a metatag aqui e clique em Atualizar."))
            if probe.get("site_ok") else
            f"Site publicado em {url}, mas ainda não respondeu "
            f"({probe.get('detail') or 'aguarde alguns segundos e recarregue'})."
        ),
    }


class ManualMetatagReq(BaseModel):
    metatag: str
    cnpj: Optional[str] = None
    fqdn: Optional[str] = None


@app.post("/api/manual-site/metatag")
async def manual_site_metatag(req: ManualMetatagReq) -> dict[str, Any]:
    """ETAPA 2 — injeta a metatag do Meta no site já publicado e confere.

    Re-deploya o MESMO site (mantém subdomínio, DNS e certificado SSL) e faz o
    probe HTTP: só devolve ``metatag_ok=true`` quando a tag está mesmo servida
    no domínio — aí pode clicar "Verificar" no Facebook com segurança."""
    from core.netlify_deployer import (  # noqa: PLC0415
        NetlifyDeployer, NetlifyDeployerError,
    )

    token_hash = _extract_fb_token(req.metatag)
    if not token_hash:
        raise HTTPException(
            400,
            "Metatag não reconhecida. Cole a tag inteira "
            '(<meta name="facebook-domain-verification" content="…" />) ou só o código.',
        )
    dom, entry = _find_manual_site(cnpj=req.cnpj or "", fqdn=req.fqdn or "")
    if not dom or not entry:
        raise HTTPException(
            404,
            "Site não encontrado para esse CNPJ/endereço. Publique o site na "
            "etapa 1 antes de salvar a metatag.",
        )
    base = dom["domain"]
    slug = entry["slug"]
    fqdn = entry.get("fqdn") or f"{slug}.{base}"
    cnpj_digits = only_digits(entry.get("cnpj") or req.cnpj or "")
    if len(cnpj_digits) != 14:
        raise HTTPException(400, "CNPJ do site inválido no cadastro.")
    if not (dom.get("api_token") or "").strip():
        raise HTTPException(400, f"O domínio {base} está sem token da Netlify.")

    deployer = NetlifyDeployer(api_token=dom["api_token"])
    try:
        url, criado = await deployer.redeploy_subdomain(
            fqdn, cnpj_digits=cnpj_digits, token_hash=token_hash,
        )
    except NetlifyDeployerError as exc:
        raise _netlify_http_error(exc)

    store.netlify_upsert_site_entry(base, slug, cnpj_digits, token_hash, fqdn)
    # Janela curta (~25s): re-deploy num site que já existe propaga rápido.
    # Se ainda não pegou, o botão "Conferir metatag" refaz o probe sob demanda.
    probe = await _probe_site(fqdn, token_hash, attempts=5, interval=5.0)
    logger.info(
        "manual-site: metatag salva em %s (token=%s, no_ar=%s)",
        fqdn, token_hash[:10], probe.get("metatag_ok"),
    )
    return {
        "ok": True,
        "fqdn": fqdn,
        "url": url,
        "token": token_hash,
        "recreated": criado,
        "probe": probe,
        "message": (
            f"Metatag no ar em {url} — pode clicar em Verificar no Facebook."
            if probe.get("metatag_ok")
            else (
                "Metatag salva e re-publicada, mas ainda não apareceu no ar "
                f"({probe.get('detail') or 'sem detalhe'}). Aguarde alguns "
                "segundos e clique em 'Conferir metatag'."
            )
        ),
    }


@app.get("/api/manual-site/list")
def manual_site_list() -> list[dict[str, Any]]:
    """Meus Sites — TODOS os sites Cloudflare (manuais + de RUN), com dados de card."""
    out: list[dict[str, Any]] = []
    for s in store.cloudflare_domain_list_all_sites():
        card = {"razao_social": "", "logradouro": "", "numero": "", "bairro": "",
                "cidade": "", "uf": "", "cep": "", "telefone": "", "email": ""}
        if s.get("empresa_json"):
            try:
                emp = json.loads(s["empresa_json"])
                end = emp.get("endereco") or {}
                con = emp.get("contato") or {}
                card["razao_social"] = emp.get("razaoSocial") or emp.get("nomeFantasia") or ""
                card["logradouro"] = end.get("logradouro") or ""
                card["numero"] = end.get("numero") or ""
                card["bairro"] = end.get("bairro") or ""
                card["cidade"] = end.get("cidade") or ""
                card["uf"] = end.get("uf") or ""
                card["cep"] = end.get("cep") or ""
                card["telefone"] = con.get("telefone") or ""
                card["email"] = con.get("email") or ""
            except Exception:  # noqa: BLE001
                pass
        ni = "Não informado"
        card = {k: ("" if v == ni else v) for k, v in card.items()}
        out.append({
            "domain": s["domain"], "slug": s["slug"], "cnpj": s.get("cnpj") or "",
            "token": s.get("token") or "", "fqdn": s["fqdn"], "url": s["url"],
            "source": s.get("source") or "run", "updated_at": s.get("updated_at"),
            "verified": bool(s.get("token")), **card,
        })
    return out


class ManualDeleteReq(BaseModel):
    domain: str
    slug: str


@app.post("/api/manual-site/delete")
async def manual_site_delete(req: ManualDeleteReq) -> dict[str, Any]:
    """Remove um site do Cloudflare (do mapa) e re-publica o Worker sem ele."""
    from core.cloudflare_deployer import CloudflareDomainDeployer  # noqa: PLC0415
    dom = (req.domain or "").strip().lower().strip(".")
    slug = (req.slug or "").strip().lower()
    if not dom or not slug:
        raise HTTPException(400, "domain e slug são obrigatórios.")
    removed = store.cloudflare_domain_remove_site_entry(dom, slug)
    row = next((d for d in store.list_cloudflare_domains() if d.get("domain") == dom), None)
    if row and row.get("api_token"):
        try:
            entries = store.cloudflare_domain_list_site_entries(dom)
            dep = CloudflareDomainDeployer(row["account_id"], row["api_token"])
            await dep.publish_domain(dom, entries)
        except Exception as exc:  # noqa: BLE001
            logger.debug("manual-site delete: re-deploy falhou (%s).", exc)
    return {"ok": True, "removed": removed}


class ManualCheckReq(BaseModel):
    fqdn: str
    token: Optional[str] = None


@app.post("/api/manual-site/check")
async def manual_site_check(req: ManualCheckReq) -> dict[str, Any]:
    """Confere AGORA, por HTTP, se o site (e a metatag) estão no ar."""
    fq = (req.fqdn or "").strip().lower().strip(".")
    if not fq or "." not in fq:
        raise HTTPException(400, "Endereço inválido.")
    token = (req.token or "").strip()
    if not token:
        _dom, entry = _find_manual_site(fqdn=fq)
        token = (entry or {}).get("token") or ""
    return await _probe_site(fq, token, attempts=2, interval=3.0)


# ══════════════════════════════════════════════════════════════════════
# PDFs / CNPJs (aba "Gerar Site / PDFs") — lista os comprovantes CNPJ gerados
# nas runs (o mesmo PDF anexado na verificação), com preview, edição de
# email/telefone comercial (re-render) e download.
# ══════════════════════════════════════════════════════════════════════
from config import COMPROVANTES_REVIEW_DIR as _CDIR, BASE_DIR as _BASE  # noqa: E402
_CEDIT_DIR = _BASE / "artifacts" / "comprovantes_edited"
# Serializa os re-renders (cada um abre um Chromium headless): edições rápidas
# disparavam vários ao mesmo tempo → colisão/falha. Um render por vez.
_comprovante_render_lock = asyncio.Lock()


def _comprovante_meta(pdf: "_Path") -> dict[str, Any]:
    mp = pdf.with_suffix(pdf.suffix + ".meta.json")
    if mp.is_file():
        try:
            return json.loads(mp.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _extract_cnpj_from_pdf(pdf: "_Path") -> str:
    """Lê o CNPJ impresso no próprio comprovante (1ª página) via pdfplumber."""
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(str(pdf)) as doc:
            txt = (doc.pages[0].extract_text() or "") if doc.pages else ""
        m = re.search(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", txt)
        if m:
            return re.sub(r"\D", "", m.group(0))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _comprovante_card(pdf: "_Path") -> dict[str, Any]:
    name = pdf.name
    meta = _comprovante_meta(pdf)
    parts = name.split("_")
    run_tag = parts[0] if parts else ""
    phone_sms = parts[1] if len(parts) > 1 else ""
    m = re.search(r"comprovante_cnpj_(.+)\.pdf$", name, re.I)
    slug = re.sub(r"-\d{5,}$", "", m.group(1)) if m else ""  # tira CPF/dígitos do fim
    razao = (meta.get("razao_social") or slug.replace("-", " ").strip().title() or "—")
    data_fmt = ""
    dm = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", run_tag)
    if dm:
        data_fmt = f"{dm.group(3)}/{dm.group(2)}/{dm.group(1)} {dm.group(4)}:{dm.group(5)}"
    # CNPJ: do sidecar, ou extraído do PDF (1ª vez) e cacheado no sidecar.
    cnpj = meta.get("cnpj") or ""
    if not cnpj:
        cnpj = _extract_cnpj_from_pdf(pdf)
        if cnpj:
            meta["cnpj"] = cnpj
            try:
                pdf.with_suffix(pdf.suffix + ".meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001
                pass
    return {
        "id": name,
        "cnpj": cnpj,
        "razao_social": razao,
        "email": meta.get("email_override", meta.get("email") or ""),
        "telefone": meta.get("telefone_override", meta.get("telefone") or phone_sms or ""),
        "phone_sms": phone_sms,
        "data": data_fmt,
        "mtime": pdf.stat().st_mtime,
        "editavel": bool(cnpj),
        "edited": (_CEDIT_DIR / name).is_file(),
    }


def _safe_comprovante(cid: str) -> "_Path":
    """Resolve o PDF de review por id (nome do arquivo), barrando path traversal."""
    base = _CDIR.resolve()
    p = (base / (cid or "")).resolve()
    if base != p.parent or p.suffix.lower() != ".pdf" or not p.is_file():
        raise HTTPException(404, "Comprovante não encontrado.")
    return p


@app.get("/api/comprovantes")
def comprovantes_list() -> list[dict[str, Any]]:
    """Lista todos os comprovantes CNPJ (PDFs) gerados, mais novos primeiro."""
    if not _CDIR.exists():
        return []
    cards = [_comprovante_card(p) for p in _CDIR.glob("*.pdf")]
    cards.sort(key=lambda c: c.get("mtime", 0), reverse=True)
    return cards


@app.get("/api/comprovantes/{cid}/file")
def comprovante_file(cid: str, edited: int = 0) -> FileResponse:
    """Serve o PDF (inline p/ preview e download). ?edited=1 = versão editada."""
    pdf = _safe_comprovante(cid)
    if edited:
        ed = _CEDIT_DIR / pdf.name
        if ed.is_file():
            pdf = ed
    return FileResponse(str(pdf), media_type="application/pdf", filename=pdf.name)


class ComprovanteEditReq(BaseModel):
    email: Optional[str] = None
    telefone: Optional[str] = None
    cnpj: Optional[str] = None  # só necessário se o comprovante não tiver meta


@app.post("/api/comprovantes/{cid}/edit")
async def comprovante_edit(cid: str, req: ComprovanteEditReq) -> dict[str, Any]:
    """Re-renderiza o comprovante com EMAIL/TELEFONE comercial editados. Salva a
    versão editada em artifacts/comprovantes_edited/ e atualiza o sidecar."""
    pdf = _safe_comprovante(cid)
    meta = _comprovante_meta(pdf)
    cnpj = (meta.get("cnpj") or req.cnpj or "").strip()
    if not cnpj:
        raise HTTPException(400, "Este comprovante não tem o CNPJ salvo — informe o CNPJ para editar.")
    from core.cnpj_lookup import fetch_cnpj_cached  # noqa: PLC0415
    from core.cnpj_reva import render_comprovante_local  # noqa: PLC0415
    import shutil as _shutil  # noqa: PLC0415
    try:
        data = await fetch_cnpj_cached(cnpj)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Não consegui buscar os dados do CNPJ {cnpj}: {exc}")
    upd: dict[str, Any] = {}
    if req.email is not None:
        # Mantém como veio (o painel já manda em MAIÚSCULO) — não força .lower().
        upd["email"] = (req.email or "").strip() or None
    data2 = data.model_copy(update=upd) if upd else data
    tel = (req.telefone or "").strip() or None
    # run_id sintético = hash do CONTEÚDO (cnpj+email+telefone). Assim cada
    # combinação distinta renderiza FRESCO (o cache do render só olhava o
    # telefone → editar só o email não regenerava). Mesma edição reusa (rápido).
    import hashlib as _hl  # noqa: PLC0415
    _h = _hl.md5(f"{cnpj}|{(req.email or '').strip()}|{(req.telefone or '').strip()}".encode("utf-8")).hexdigest()[:12]
    synth = f"edit_{_h}"

    # O render abre um Chromium headless (subprocess). No Windows isso exige o
    # ProactorEventLoop — o loop do uvicorn NÃO suporta subprocess e dava "Falha
    # ao renderizar". Rodamos numa THREAD própria com asyncio.run (mesmo padrão
    # das runs), que cria um loop compatível. O lock serializa (1 por vez).
    def _render_sync() -> Any:
        return asyncio.run(render_comprovante_local(data2, synth, telefone_override=tel))

    try:
        async with _comprovante_render_lock:
            rendered = await asyncio.to_thread(_render_sync)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Falha ao re-renderizar o PDF: {exc}")
    _CEDIT_DIR.mkdir(parents=True, exist_ok=True)
    _shutil.copy2(str(rendered), str(_CEDIT_DIR / pdf.name))
    # Atualiza o sidecar com os overrides (persistem a edição em tempo real).
    meta.update({
        "cnpj": cnpj,
        "email_override": (req.email or "").strip(),
        "telefone_override": (req.telefone or "").strip(),
    })
    try:
        pdf.with_suffix(pdf.suffix + ".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "edited": True, "id": pdf.name}


class ComprovanteGenReq(BaseModel):
    cnpj: str
    email: Optional[str] = None
    telefone: Optional[str] = None


@app.post("/api/comprovantes/generate")
async def comprovante_generate(req: ComprovanteGenReq) -> dict[str, Any]:
    """Gera um comprovante CNPJ MANUALMENTE (digita o CNPJ) — sem depender de
    run. Renderiza o PDF e salva na pasta de review; passa a aparecer na lista
    de PDFs/CNPJs igual aos das runs (com preview/editar/baixar). Email e
    telefone comerciais são opcionais."""
    from core.cnpj_lookup import fetch_cnpj_cached  # noqa: PLC0415
    from core.cnpj_reva import render_comprovante_local  # noqa: PLC0415
    import shutil as _shutil  # noqa: PLC0415
    import hashlib as _hl  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    digits = only_digits(req.cnpj)
    if len(digits) != 14:
        raise HTTPException(400, "CNPJ inválido — informe os 14 dígitos.")
    try:
        data = await fetch_cnpj_cached(digits)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Não consegui buscar os dados do CNPJ {digits}: {exc}")
    upd: dict[str, Any] = {}
    if req.email is not None:
        upd["email"] = (req.email or "").strip() or None
    data2 = data.model_copy(update=upd) if upd else data
    tel = (req.telefone or "").strip() or None

    _h = _hl.md5(
        f"{digits}|{(req.email or '').strip()}|{(req.telefone or '').strip()}".encode("utf-8")
    ).hexdigest()[:12]
    synth = f"manual_{_h}"

    def _render_sync() -> Any:
        return asyncio.run(render_comprovante_local(data2, synth, telefone_override=tel))

    try:
        async with _comprovante_render_lock:
            rendered = await asyncio.to_thread(_render_sync)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Falha ao gerar o PDF: {exc}")

    # Nome no padrão da pasta de review: <ts-hash>_<phone>_comprovante_cnpj_<slug>.pdf
    razao = (getattr(data2, "razao_social", "") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", razao.lower()).strip("-") or "empresa"
    phone_digits = only_digits(tel or "")
    ts = _dt.now().strftime("%Y%m%d-%H%M%S") + f"-{_h[:6]}"
    fname = f"{ts}_{phone_digits}_comprovante_cnpj_{slug}.pdf"
    _CDIR.mkdir(parents=True, exist_ok=True)
    dest = _CDIR / fname
    _shutil.copy2(str(rendered), str(dest))
    meta = {
        "cnpj": digits,
        "razao_social": razao,
        "email": (req.email or "").strip() or (getattr(data, "email", "") or ""),
        "telefone": (req.telefone or "").strip(),
        "run_id": synth,
        "manual": True,
    }
    try:
        dest.with_suffix(dest.suffix + ".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "id": fname, "card": _comprovante_card(dest)}


# --- CRM (WhatsApp Cloud API) ---------------------------------------------
# Rotas do CRM registradas via router separado (crm_api.py) para manter este
# arquivo enxuto. O middleware require_auth acima cobre /api/crm/* normalmente.
from crm_api import register_crm_routes  # noqa: E402

register_crm_routes(app)


# --- FB-Creator (criação de contas Facebook) --------------------------------
from core.fb_creator import fb_creator_manager, FbCreatorJob  # noqa: E402


class FbCreatorStartReq(BaseModel):
    count: int = 1
    max_concurrent: int = 1
    os_type: str = "android"
    gender: str = "random"
    proxy_mode: str = "none"
    proxies: list[dict[str, Any]] = []
    use_sms: bool = True
    use_email: bool = False
    sms_provider: str = "numero_virtual"
    sms_service_name: str = "Facebook"
    sms_service_id: int = 60033
    sms_target_price: int = 130
    sms_purchase_api_id: int = 1
    sms_operator: str = "vivo"
    sms_timeout: int = 240
    browser_type: str = "ixbrowser"
    custom_emails: list[str] = []


@app.post("/api/fb-creator/start")
async def fb_creator_start(req: FbCreatorStartReq) -> dict[str, Any]:
    """Inicia um batch de criação de contas Facebook."""
    from config import NUMERO_VIRTUAL_API_KEY as _nv_key  # noqa: PLC0415
    from config import CAPSOLVER_API_KEY as _cap_key  # noqa: PLC0415
    from config import SMS24H_ORG_API_KEY as _s24h_key  # noqa: PLC0415

    sms_key = _s24h_key if req.sms_provider == "sms24h_org" else _nv_key
    count = max(1, min(req.count, 50))
    concurrent = max(1, min(req.max_concurrent, 10))
    result = await fb_creator_manager.start_batch(
        count=count,
        max_concurrent=concurrent,
        os_type=req.os_type,
        gender=req.gender,
        proxy_mode=req.proxy_mode,
        proxies=req.proxies,
        sms_api_key=sms_key,
        capsolver_api_key=_cap_key,
        use_sms=req.use_sms,
        use_email=req.use_email,
        sms_provider=req.sms_provider,
        sms_service_name=req.sms_service_name,
        sms_service_id=req.sms_service_id,
        sms_target_price=req.sms_target_price,
        sms_purchase_api_id=req.sms_purchase_api_id,
        sms_operator=req.sms_operator,
        sms_timeout=req.sms_timeout,
        browser_type=req.browser_type,
        custom_emails=req.custom_emails,
    )
    return result


@app.get("/api/fb-creator/stats")
def fb_creator_stats() -> dict[str, Any]:
    return fb_creator_manager.stats()


@app.get("/api/fb-creator/jobs")
def fb_creator_jobs() -> list[dict[str, Any]]:
    return fb_creator_manager.get_jobs()


@app.get("/api/fb-creator/jobs/{job_id}")
def fb_creator_job(job_id: str) -> dict[str, Any]:
    job = fb_creator_manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return job.to_dict()


@app.get("/api/fb-creator/jobs/{job_id}/logs")
def fb_creator_job_logs(job_id: str) -> dict[str, Any]:
    job = fb_creator_manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return {"job_id": job_id, "logs": job.log}


@app.post("/api/fb-creator/stop")
async def fb_creator_stop() -> dict[str, Any]:
    return await fb_creator_manager.stop()


@app.get("/api/fb-creator/accounts")
def fb_creator_accounts() -> list[dict[str, Any]]:
    """Lista contas criadas (persistidas em created_accounts.json)."""
    import json as _json
    from pathlib import Path as _Path
    f = _Path(__file__).resolve().parent / "created_accounts.json"
    if not f.exists():
        return []
    try:
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


@app.get("/api/fb-creator/sms-balance")
async def fb_creator_sms_balance(provider: str = "numero_virtual") -> dict[str, Any]:
    """Consulta saldo do provedor SMS."""
    try:
        if provider == "sms24h_org":
            from core.api_clients import Sms24hOrgClient  # noqa: PLC0415
            async with Sms24hOrgClient() as sms:
                balance = await sms.get_balance()
        else:
            from core.api_clients import Sms24hClient  # noqa: PLC0415
            async with Sms24hClient() as sms:
                balance = await sms.get_balance()
        return {"balance": balance, "currency": "BRL", "provider": provider}
    except Exception as exc:
        return {"balance": None, "error": str(exc), "provider": provider}
