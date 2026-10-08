"""Streamlit UI — esteira linear:

    1. Busca CNPJ (BrasilAPI)        → razão social + cadastrais.
    2. Geração de e-mail (InvertTexto) + slug do Worker (subdomínio CF).
    3. Deploy instantâneo do Worker  → URL pública.
    4. Alocação SMS (numero-virtual) → número do Facebook visível.
    5. Credenciais Meta + run        → automação Playwright.

Os campos cadastrais herdam compulsoriamente as strings retornadas pela
BrasilAPI (razão social oficial do MEI no padrão "CNPJ_DIGITS NOME"); o
operador pode editar antes do run mas o default é o que veio da Receita.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import asdict
from typing import Any

import streamlit as st

from config import (
    ADSPOWER_API_KEY,
    CLOUDFLARE_ACCOUNT_IDS,
    CLOUDFLARE_API_TOKENS,
    CLOUDFLARE_ROTATION_STATE_PATH,
    CLOUDFLARE_ROTATION_THRESHOLD,
    CLOUDFLARE_SCRIPT_NAME,
    CLOUDFLARE_WORKERS_SUBDOMAIN,
    INVERTTEXTO_API_TOKEN,
    NUMERO_VIRTUAL_SERVICE_NAME,
    NUMERO_VIRTUAL_TARGET_PRICE,
    SMS24H_API_KEY,
    SMS24H_COUNTRY_CODE,
    SMS24H_SERVICE_CODE,
    SMS_CODE_REGEX,
)
from core import (
    AdsPowerClient,
    AdsPowerError,
    AutomationResult,
    AutomationRunner,
    BrasilApiClient,
    BrasilApiError,
    BusinessProfile,
    CloudflareDeployer,
    CloudflareDeployerError,
    CNPJData,
    InvertTextoEmailClient,
    InvertTextoError,
    MetaCredentials,
    Sms24hClient,
    Sms24hError,
    generate_uorak_email,
    slugify_for_worker,
    split_admin_name,
    test_cloudflare_creds,
)
from core import batch_io, job_manager, meta_graph, run_service, store
from core.run_service import JobSpec
from utils.helpers import only_digits
from utils.logging_utils import install_redaction


st.set_page_config(
    page_title="Meta Automation",
    page_icon="◐",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Sistema de design (registro product / estratégia Restrained)
# Tokens vivem no .streamlit/config.toml; o CSS abaixo refina tipografia,
# espaçamento, estados e esconde o chrome do Streamlit. Acento índigo
# (#5B5BD6) só em ação primária / seleção / estado — nunca decoração.
# ---------------------------------------------------------------------------
def _inject_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        :root {
          --bg:#FCFCFD; --panel:#F3F4F8; --surface:#FFFFFF; --border:#E7E8EF;
          --ink:#1F2430; --muted:#6B7180; --accent:#1E7A46; --accent-weak:#E6F4EC;
          --ok:#1F9D6B; --ok-bg:#E7F6EF; --warn:#B5820B; --warn-bg:#FBF3E0;
          --err:#D23F3F; --err-bg:#FBEBEB;
        }
        html, body, .stApp, [data-testid="stAppViewContainer"],
        [data-testid="stSidebar"] {
          font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
          -webkit-font-smoothing:antialiased;
        }

        /* Esconde o chrome do Streamlit (menu, toolbar, footer, decoração) */
        #MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"],
        footer { display:none !important; }
        [data-testid="stHeader"] { background:transparent; height:0; }

        /* Desktop: aproveita a largura sem virar deserto em monitor grande */
        .block-container { max-width:1240px; padding-top:1.8rem; padding-bottom:4rem; }

        /* Stepper horizontal (trilha de progresso) */
        .stepper { display:flex; align-items:center; gap:0; margin:.4rem 0 1.4rem;
                   border:1px solid var(--border); border-radius:12px; background:var(--surface);
                   padding:.55rem .4rem; box-shadow:0 1px 2px rgba(31,36,48,.04); }
        .stepper .seg { display:flex; align-items:center; gap:.55rem; padding:.25rem .85rem; flex:1 1 0; }
        .stepper .seg + .seg { border-left:1px solid var(--border); }
        .st-dot { width:24px; height:24px; border-radius:7px; display:grid; place-items:center;
                  font-size:.78rem; font-weight:700; flex:0 0 auto; }
        .st-done .st-dot { background:var(--ok-bg); color:var(--ok); }
        .st-active .st-dot { background:var(--accent); color:#fff; }
        .st-todo .st-dot { background:var(--panel); color:var(--muted); }
        .st-label { font-size:.86rem; font-weight:600; color:var(--ink); line-height:1.1; }
        .st-todo .st-label { color:var(--muted); font-weight:500; }
        .st-sub { font-size:.72rem; color:var(--muted); }

        /* Tipografia: escala apertada, hierarquia por peso */
        h1 { font-weight:700; font-size:1.55rem; letter-spacing:-0.02em; color:var(--ink); }
        h2 { font-weight:600; font-size:1.2rem; letter-spacing:-0.01em; }
        h3 { font-weight:600; font-size:1.02rem; letter-spacing:-0.01em; }

        /* Cabeçalho do app */
        .app-head { display:flex; align-items:baseline; gap:.6rem; margin-bottom:.15rem; }
        .app-mark { font-weight:700; font-size:1.5rem; letter-spacing:-0.02em; color:var(--ink); }
        .app-sub { color:var(--muted); font-size:.9rem; }

        /* Cabeçalho de etapa: badge numerado + título + chip de status */
        .step-h { display:flex; align-items:center; gap:.6rem; margin:.2rem 0 .9rem; }
        .step-n { width:25px; height:25px; border-radius:7px; background:var(--accent-weak);
                  color:var(--accent); font-weight:700; font-size:.8rem;
                  display:grid; place-items:center; flex:0 0 auto; }
        .step-t { font-weight:600; font-size:1.05rem; color:var(--ink); }
        .chip { display:inline-flex; align-items:center; font-size:.7rem; font-weight:600;
                padding:.16rem .5rem; border-radius:999px; margin-left:auto; }
        .chip-ok { background:var(--ok-bg); color:var(--ok); }
        .chip-wait { background:var(--panel); color:var(--muted); }
        .chip-warn { background:var(--warn-bg); color:var(--warn); }
        .chip-err { background:var(--err-bg); color:var(--err); }

        /* Linha de chips do resultado */
        .chips-row { display:flex; flex-wrap:wrap; gap:.5rem; margin:.5rem 0 1rem; }
        .chips-row .chip { margin-left:0; padding:.28rem .65rem; font-size:.78rem; }

        /* Botões: raio + peso consistentes; acento vem do config */
        .stButton > button, .stFormSubmitButton > button {
          border-radius:9px; font-weight:600; transition:background .15s ease, border-color .15s ease;
        }

        /* Inputs: foco no acento */
        [data-testid="stTextInput"] input:focus,
        [data-testid="stSelectbox"] div[data-baseweb="select"]:focus-within {
          border-color:var(--accent) !important;
          box-shadow:0 0 0 3px var(--accent-weak) !important;
        }

        /* Profile ID GRANDE e muito destacado na sidebar */
        .profile-id-badge {
          background: linear-gradient(135deg, #1F9D6B 0%, #0d5a35 100%);
          color: white;
          padding: 1.5rem 1.2rem;
          border-radius: 16px;
          text-align: center;
          margin: 1.5rem 0;
          box-shadow: 0 8px 24px rgba(30, 122, 70, 0.4), inset 0 1px 0 rgba(255,255,255,0.2);
          border: 2px solid rgba(255, 255, 255, 0.1);
          animation: pulse-badge 2s ease-in-out infinite;
        }
        @keyframes pulse-badge {
          0%, 100% { box-shadow: 0 8px 24px rgba(30, 122, 70, 0.4), inset 0 1px 0 rgba(255,255,255,0.2); }
          50% { box-shadow: 0 12px 32px rgba(30, 122, 70, 0.6), inset 0 1px 0 rgba(255,255,255,0.2); }
        }
        .profile-id-badge .label {
          font-size: 0.7rem;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.1em;
          opacity: 0.95;
          display: block;
          margin-bottom: 0.6rem;
          color: rgba(255,255,255,0.9);
        }
        .profile-id-badge .value {
          font-size: 2.2rem;
          font-weight: 900;
          letter-spacing: -0.02em;
          font-family: 'Monaco', 'Courier New', monospace;
          line-height: 1.2;
          background: linear-gradient(135deg, #fff 0%, #e0f2f1 100%);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          background-clip: text;
        }

        /* Sidebar: segundo plano de painel + borda */
        [data-testid="stSidebar"] { background:var(--panel); border-right:1px solid var(--border); }

        /* Cards (st.container border=True): borda suave, raio, sombra mínima */
        [data-testid="stVerticalBlockBorderWrapper"] {
          border:1px solid var(--border) !important; border-radius:12px;
          background:var(--surface); box-shadow:0 1px 2px rgba(31,36,48,.04);
        }

        /* Divisores e legendas mais discretos */
        hr { border-color:var(--border); margin:1.6rem 0; }
        [data-testid="stCaptionContainer"] { color:var(--muted); }

        /* Métricas: rótulo discreto, valor firme */
        [data-testid="stMetricLabel"] { color:var(--muted); font-size:.78rem; }
        [data-testid="stMetricValue"] { font-size:1.15rem; font-weight:600; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _step(number: str, title: str, status: tuple[str, str] | None = None) -> None:
    """Cabeçalho de etapa: badge numerado + título + chip de status opcional.

    ``status`` = (classe, rótulo), ex.: ("ok", "Concluído"), ("wait",
    "Pendente"), ("warn", "Atenção").
    """
    chip = (
        f'<span class="chip chip-{status[0]}">{status[1]}</span>' if status else ""
    )
    st.markdown(
        f'<div class="step-h"><span class="step-n">{number}</span>'
        f'<span class="step-t">{title}</span>{chip}</div>',
        unsafe_allow_html=True,
    )


def _stepper() -> None:
    """Trilha de progresso horizontal. O estado de cada etapa é derivado do
    session_state; a etapa ativa é a primeira ainda não concluída."""
    ss = st.session_state
    done = [
        "cnpj_data" in ss,
        bool(ss.get("deploy_done") or ss.get("deploy_url") or ss.get("worker_predicted_url")),
        bool(ss.get("sms_done") or ss.get("sms_activation_id")),
        False,  # credenciais: só "concluído" após o run (não rastreado aqui)
    ]
    labels = [
        ("CNPJ", "consulta Receita"),
        ("Site", "Cloudflare Worker"),
        ("SMS", "número do Facebook"),
        ("Credenciais", "login + execução"),
    ]
    active = next((i for i, d in enumerate(done) if not d), 3)
    segs = []
    for i, (label, sub) in enumerate(labels):
        cls = "st-done" if done[i] else ("st-active" if i == active else "st-todo")
        mark = "✓" if done[i] else str(i + 1)
        segs.append(
            f'<div class="seg {cls}"><span class="st-dot">{mark}</span>'
            f'<span><div class="st-label">{label}</div>'
            f'<div class="st-sub">{sub}</div></span></div>'
        )
    st.markdown(f'<div class="stepper">{"".join(segs)}</div>', unsafe_allow_html=True)


def _head(title: str, sub: str) -> None:
    st.markdown(
        f'<div class="app-head"><span class="app-mark">{title}</span>'
        f'<span class="app-sub">{sub}</span></div>',
        unsafe_allow_html=True,
    )


def _mask_token(tok: str | None) -> str:
    """Nunca expõe o token inteiro na tela (só prefixo + tamanho)."""
    if not tok:
        return "—"
    return f"{tok[:6]}…({len(tok)})"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


def _render_nav() -> str:
    """Navegação no topo da sidebar. Retorna a view selecionada."""
    with st.sidebar:
        st.markdown(
            '<div style="display:flex;align-items:center;gap:.5rem;'
            'margin:.2rem 0 1rem;"><span style="font-size:1.3rem;">🟢</span>'
            '<span><b style="font-size:1.05rem;">Meta Farm</b><br>'
            '<span style="font-size:.74rem;color:var(--muted);">painel de '
            'provisionamento</span></span></div>',
            unsafe_allow_html=True,
        )
        choice = st.radio(
            "Navegação",
            ["🚀 Provisionar", "🛰️ Lote", "📊 Dashboard", "🏢 BMs", "📋 Runs"],
            label_visibility="collapsed",
            key="nav_view",
        )
        st.divider()
    return choice.split(" ", 1)[1]


def _view_dashboard() -> None:
    _head("Dashboard", "visão geral do provisionamento")
    rc = store.run_counts()
    bc = store.bm_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runs (total)", rc.get("total", 0))
    c2.metric("Sucesso", rc.get(store.RUN_SUCCESS, 0))
    c3.metric("Falhas", rc.get(store.RUN_FAILED, 0))
    c4.metric("BMs (total)", bc.get("total", 0))
    st.divider()
    d1, d2, d3 = st.columns(3)
    d1.metric("BMs 2k", bc.get("2k", 0))
    d2.metric("BMs não-2k", bc.get("not_2k", 0))
    d3.metric("BMs a checar", bc.get(store.TIER_UNKNOWN, 0))
    st.divider()
    st.subheader("Runs recentes")
    runs = store.list_runs(limit=20)
    if runs:
        st.dataframe(
            [
                {
                    "quando": _fmt_ts(r["created_at"]),
                    "cnpj": r["cnpj"],
                    "empresa": r["label"],
                    "status": r["status"],
                    "etapa": r["step"],
                    "business_id": r["business_id"],
                    "waba_id": r["waba_id"],
                }
                for r in runs
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nenhum run registrado ainda — rode um provisionamento.")


def _view_bms() -> None:
    _head("Business Managers", "BMs provisionadas e status de verificação (2k)")
    bc = store.bm_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", bc.get("total", 0))
    c2.metric("Verificadas (2k)", bc.get("2k", 0))
    c3.metric("Não verificadas", bc.get("not_2k", 0))
    c4.metric("A checar", bc.get(store.TIER_UNKNOWN, 0))

    if st.button("🔄 Atualizar verificação (Graph API)"):
        with st.spinner("Consultando o verification_status de cada BM…"):
            try:
                summ = meta_graph.refresh_bm_verification()
                st.success(
                    f"Verificação: {summ['atualizadas']} atualizada(s), "
                    f"{summ['falhas']} falha(s), {summ['sem_token']} sem token."
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Falha ao atualizar verificação: {exc}")
        st.rerun()

    st.divider()
    bms = store.list_bms(limit=500)
    if bms:
        st.dataframe(
            [
                {
                    "criada": _fmt_ts(b["created_at"]),
                    "empresa": b["portfolio_name"],
                    "cnpj": b["cnpj"],
                    "business_id": b["business_id"],
                    "waba_id": b["waba_id"],
                    "token": _mask_token(b["meta_token"]),
                    "2k": "✅" if b["tier"] == "2k" else (
                        "—" if b["tier"] == store.TIER_UNKNOWN else "❌"
                    ),
                    "status": b.get("status_detail"),
                    "checado": _fmt_ts(b["last_checked_at"]),
                }
                for b in bms
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "'2k' = empresa verificada/aprovada pela Meta (verification_status = "
            "'verified'). A coluna 'status' mostra o valor cru da Graph API."
        )
    else:
        st.caption("Nenhuma BM registrada ainda.")


def _view_runs() -> None:
    _head("Runs", "histórico de execuções do pipeline")
    runs = store.list_runs(limit=200)
    if runs:
        st.dataframe(
            [
                {
                    "quando": _fmt_ts(r["created_at"]),
                    "cnpj": r["cnpj"],
                    "empresa": r["label"],
                    "status": r["status"],
                    "etapa": r["step"],
                    "erro": (r["error"] or "")[:80],
                    "business_id": r["business_id"],
                    "waba_id": r["waba_id"],
                }
                for r in runs
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nenhum run registrado ainda.")


@st.cache_resource
def _get_manager() -> job_manager.JobManager:
    """Singleton do motor paralelo — sobrevive aos reruns do Streamlit."""
    return job_manager.JobManager(concurrency=2)


def _enqueue_specs(specs: list[dict[str, Any]], infra: dict[str, Any]) -> int:
    """Monta um JobSpec por conta (infra compartilhada) e enfileira no pool.

    adspower_mode é forçado a 'create': no lote cada conta ganha um perfil novo
    (idempotente por CNPJ) — 'reuse' apontaria todas pro mesmo perfil.
    """
    mgr = _get_manager()
    proxy_cfg = _build_proxy_cfg(infra)
    for s in specs:
        spec = JobSpec(
            cnpj=s["cnpj"],
            meta_email_or_phone=s["meta_email_or_phone"],
            meta_password=s["meta_password"],
            meta_totp_seed=s["meta_totp_seed"],
            adspower_mode="create",
            adspower_api_key=infra.get("adspower_api_token") or None,
            cloudflare_account_id=infra.get("cf_account_id") or None,
            cloudflare_api_token=infra.get("cf_api_token") or None,
            cloudflare_worker_name=infra.get("cf_worker_name") or None,
            sms_service_code=infra.get("sms_service_code") or None,
            sms_country_code=infra.get("sms_country_code") or None,
            proxy_cfg=proxy_cfg,
            headless=False,
        )
        mgr.submit(run_service.execute_job, spec=spec, cnpj=spec.cnpj)
    return len(specs)


def _render_batch_monitor() -> None:
    st.subheader("Monitor")
    if st.button("🔄 Atualizar"):
        st.rerun()
    runs = store.list_runs(limit=30)
    if not runs:
        st.caption("Nenhum run ainda.")
        return
    icons = {"success": "✅", "failed": "❌", "running": "⏳", "queued": "🕗"}
    for r in runs:
        ic = icons.get(r["status"], "•")
        title = (
            f"{ic} {r['cnpj'] or '—'} · {r['status']} · "
            f"{r['step'] or ''} · {_fmt_ts(r['created_at'])}"
        )
        with st.expander(title):
            st.code(
                job_manager.read_run_log(r["run_id"]) or "(sem log ainda)",
                language="log",
            )


def _view_batch(infra: dict[str, Any]) -> None:
    _head("Lote / Paralelo", "enfileira contas e roda 2–3 em paralelo")

    # ===== MONITOR NO TOPO (RUNS + LOGS) =====
    with st.container(border=True):
        st.subheader("🔴 Monitor em Tempo Real")
        st.caption("Acompanhe os runs e logs em tempo real — atualiza automaticamente.")
        if st.button("🔄 Atualizar Agora"):
            st.rerun()
        _render_batch_monitor()

    st.divider()

    # ===== CONFIGURAÇÕES ABAIXO =====
    mgr = _get_manager()
    c1, c2 = st.columns([1, 2])
    with c1:
        n = int(st.number_input(
            "Runs em paralelo", min_value=1, max_value=4,
            value=mgr.concurrency, step=1,
        ))
        if n != mgr.concurrency:
            if mgr.set_concurrency(n):
                st.caption(f"Paralelismo = {mgr.concurrency}")
            else:
                st.warning("Há runs ativos — termine antes de mudar o paralelismo.")
    with c2:
        st.metric("Jobs ativos", mgr.active_count())

    st.divider()
    st.subheader("Upload de contas (CSV / XLSX)")
    st.caption(
        "Colunas: **cnpj, email, password, totp_seed** (aliases aceitos: "
        "login/senha/2fa…). Uma linha = uma conta."
    )
    up = st.file_uploader(
        "Planilha", type=["csv", "xlsx"], label_visibility="collapsed"
    )
    if up is not None:
        try:
            specs, errors = batch_io.parse_accounts(up.getvalue(), up.name)
        except ValueError as exc:
            st.error(str(exc))
            specs, errors = [], []
        if errors:
            with st.expander(f"⚠️ {len(errors)} linha(s) inválida(s) — ignoradas"):
                for e in errors:
                    st.write(e)
        if specs:
            st.success(f"{len(specs)} conta(s) válida(s).")
            st.dataframe(
                [{"cnpj": s["cnpj"], "email": s["meta_email_or_phone"]} for s in specs],
                use_container_width=True, hide_index=True,
            )
            if st.button(f"🚀 Enfileirar {len(specs)} run(s)", type="primary"):
                _enqueue_specs(specs, infra)
                st.success("Enfileirado! Acompanhe no monitor acima.")


_inject_theme()
_view = _render_nav()
if _view == "Dashboard":
    _view_dashboard()
    st.stop()
elif _view == "BMs":
    _view_bms()
    st.stop()
elif _view == "Runs":
    _view_runs()
    st.stop()
elif _view == "Lote":
    # Precisa da infra (AdsPower/Cloudflare/SMS) → tratado na linha do
    # _render_infra_sidebar (mais abaixo), pois a função é definida depois.
    pass
else:
    # View "Provisionar" — o fluxo de 4 passos (intacto).
    _head("Meta Automation", "provisionamento de contas WhatsApp Business")
    _stepper()


# ---------------------------------------------------------------------------
# Sidebar (Infraestrutura)
# ---------------------------------------------------------------------------
def _read_cloudflare_metrics() -> dict[str, int]:
    try:
        import json  # noqa: PLC0415
        data = json.loads(CLOUDFLARE_ROTATION_STATE_PATH.read_text(encoding="utf-8"))
        return {
            "current_index": int(data.get("current_index", data.get("index", 0))),
            "deploys_on_current": int(data.get("deploys_on_current", 0)),
            "total_deploys": int(data.get("total_deploys", 0)),
        }
    except (FileNotFoundError, OSError, ValueError):
        return {"current_index": 0, "deploys_on_current": 0, "total_deploys": 0}


def _classify_adspower_input(value: str) -> str:
    """Classifica o que o operador colou no campo Profile ID.

    Retorna uma das chaves: ``"ok"``, ``"api_key"``, ``"cnpj"``, ``"cpf"``,
    ``"too_long"``, ``"empty"``.
    """
    v = (value or "").strip()
    if not v:
        return "empty"
    digits_only = v.replace(".", "").replace("-", "").replace("/", "").replace(" ", "")
    if digits_only.isdigit():
        if len(digits_only) == 14:
            return "cnpj"
        if len(digits_only) == 11:
            return "cpf"
    lower = v.lower()
    if len(lower) >= 24 and all(c in "0123456789abcdef" for c in lower):
        return "api_key"
    if len(v) > 20:
        return "too_long"
    return "ok"


def _render_infra_sidebar() -> dict[str, Any]:
    st.sidebar.markdown(
        '<div class="app-sub" style="text-transform:uppercase;letter-spacing:.08em;'
        'font-size:.72rem;font-weight:600;margin-bottom:.4rem;">Infraestrutura</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.subheader("Navegador")
    engine = st.sidebar.radio(
        "Engine",
        options=["AdsPower (antidetect)", "Playwright local"],
        key="infra_engine",
        label_visibility="collapsed",
    )
    using_adspower = engine.startswith("AdsPower")

    adspower_api_token = ""
    adspower_mode = "off"
    adspower_user_id = ""

    if using_adspower:
        with st.sidebar.container(border=True):
            st.markdown("**AdsPower**")
            adspower_api_token = st.text_input(
                "API Token",
                value=ADSPOWER_API_KEY,
                type="password",
                key="infra_adspower_api_token",
                help=(
                    "Token da sua **conta** AdsPower. Painel → Account Settings → API. "
                    "Hash hex longo (32+ chars). Vale pra qualquer perfil."
                ),
            )
            mode_label = st.radio(
                "Modo",
                options=["Criar perfil novo (recomendado)", "Reutilizar perfil existente"],
                key="infra_adspower_mode",
                index=0,
                help=(
                    "**Criar**: bot cria perfil novo via API por CNPJ (idempotente — "
                    "rodar o mesmo CNPJ 2x reusa o perfil). "
                    "**Reutilizar**: você fornece o Profile ID de um perfil que já existe."
                ),
            )
            adspower_mode = "create" if mode_label.startswith("Criar") else "reuse"

            if adspower_mode == "reuse":
                adspower_user_id = st.text_input(
                    "ID do Perfil",
                    key="infra_adspower_user_id",
                    placeholder="k1198abc",
                    help="Painel AdsPower → Browser Profiles → coluna **ID** (curto, alfanumérico).",
                )
                # Exibe o Profile ID de forma destacada se preenchido
                if adspower_user_id.strip():
                    st.markdown(
                        f"""
                        <div class="profile-id-badge">
                            <span class="label">Perfil AdsPower</span>
                            <span class="value">{adspower_user_id.strip()}</span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                classification = _classify_adspower_input(adspower_user_id)
                if classification == "cnpj":
                    st.error(
                        "⚠️ Isso parece um **CNPJ** (14 dígitos), não um Profile ID. "
                        "Use a coluna **ID** em Browser Profiles."
                    )
                elif classification == "cpf":
                    st.error(
                        "⚠️ Isso parece um **CPF** (11 dígitos), não um Profile ID."
                    )
                elif classification == "api_key":
                    st.error(
                        "⚠️ Isso parece uma **API Key** (hash hex longo). "
                        "API Token vai no campo acima; Profile ID é curto (ex.: `k1198abc`)."
                    )
                elif classification == "too_long":
                    st.warning(
                        "⚠️ Profile IDs reais costumam ter < 20 chars. Confira no painel."
                    )

            col_validate, col_status = st.columns([1, 1])
            with col_validate:
                if st.button(
                    "Testar API",
                    disabled=not adspower_api_token.strip(),
                    use_container_width=True,
                    key="btn_test_adspower",
                ):
                    with st.spinner("Pingando API local…"):
                        client = AdsPowerClient(api_key=adspower_api_token.strip())
                        verdict = asyncio.run(client.verify())
                    st.session_state["adspower_validation"] = verdict
            verdict = st.session_state.get("adspower_validation")
            with col_status:
                if verdict:
                    if verdict["ok"]:
                        st.success(verdict["message"])
                    else:
                        st.error(verdict["message"])
                else:
                    st.caption("Sem validação ainda.")
    else:
        with st.sidebar.container(border=True):
            st.markdown("**Proxy direto** (opcional)")
            st.caption("Usado pelo Playwright local. Em branco = sem proxy.")

    proxy_server = ""
    proxy_user = ""
    proxy_pass = ""
    if not using_adspower:
        proxy_server = st.sidebar.text_input(
            "Servidor Proxy", key="infra_proxy_server", placeholder="http://ip:porta",
        )
        proxy_user = st.sidebar.text_input("Usuário do Proxy", key="infra_proxy_user")
        proxy_pass = st.sidebar.text_input(
            "Senha do Proxy", key="infra_proxy_pass", type="password",
        )

    st.sidebar.divider()
    st.sidebar.subheader("SMS")
    with st.sidebar.container(border=True):
        sms_service = SMS24H_SERVICE_CODE or "ot"
        sms_country = (SMS24H_COUNTRY_CODE or "br").strip().lower() or "br"
        st.caption("Provedor: `numero-virtual.com`")
        st.caption(
            f"Serviço fixo: `{NUMERO_VIRTUAL_SERVICE_NAME}` "
            f"(`{sms_service}`) · alvo: `R$ {NUMERO_VIRTUAL_TARGET_PRICE / 100:.2f}`"
        )
        st.caption("País fixo: `Brasil (BR / countryId 24)`")
        if st.button(
            "Verificar catálogo & saldo",
            disabled=not SMS24H_API_KEY,
            use_container_width=True,
            key="btn_sms_catalog",
        ):
            with st.spinner("Consultando numero-virtual.com…"):
                async def _check() -> dict[str, Any]:
                    async with Sms24hClient() as cli:
                        try:
                            balance = await cli.get_balance()
                        except Sms24hError as exc:
                            balance = f"erro: {exc}"
                        try:
                            catalog = await cli.get_numbers_status(sms_country)
                        except Sms24hError as exc:
                            catalog = f"erro: {exc}"
                        return {"balance": balance, "catalog": catalog}
                st.session_state["sms_catalog"] = asyncio.run(_check())
        sms_catalog = st.session_state.get("sms_catalog")
        if sms_catalog:
            bal = sms_catalog["balance"]
            st.caption(f"Saldo: `{bal}`")
            st.code(str(sms_catalog["catalog"])[:600], language="json")

    st.sidebar.divider()
    st.sidebar.subheader("Cloudflare")
    metrics = _read_cloudflare_metrics()
    pool_size = len(CLOUDFLARE_ACCOUNT_IDS)
    threshold = CLOUDFLARE_ROTATION_THRESHOLD
    deploys_current = metrics["deploys_on_current"]
    if pool_size > 0:
        active_idx = metrics["current_index"] % pool_size
        st.sidebar.markdown(
            f"**Conta ativa:** índice `{active_idx}` · {pool_size} contas no pool"
        )
        st.sidebar.progress(
            min(deploys_current / threshold, 1.0),
            text=f"Deploys nesta conta: {deploys_current} / {threshold}",
        )
        if deploys_current >= threshold:
            st.sidebar.error(
                "⚠️ Rotação pendente — próximo deploy trocará de conta."
            )
        elif deploys_current >= max(threshold - 2, 1):
            st.sidebar.warning(
                f"⏳ Restam {threshold - deploys_current} deploys até a rotação."
            )
        st.sidebar.caption(f"Total lifetime: {metrics['total_deploys']} deploys.")
        default_account_id = CLOUDFLARE_ACCOUNT_IDS[active_idx]
        default_api_token = (
            CLOUDFLARE_API_TOKENS[active_idx]
            if active_idx < len(CLOUDFLARE_API_TOKENS) else ""
        )
    else:
        st.sidebar.caption("Pool não configurado — campos avulsos abaixo.")
        default_account_id = ""
        default_api_token = ""

    cf_account_id = st.sidebar.text_input(
        "Cloudflare Account ID", value=default_account_id, key="infra_cf_account_id",
    )
    cf_api_token = st.sidebar.text_input(
        "Cloudflare API Token", value=default_api_token,
        type="password", key="infra_cf_api_token",
    )

    # Validador de credenciais — GET /user/tokens/verify + GET /accounts/{id}.
    if st.sidebar.button(
        "🔎 Validar Credenciais Cloudflare",
        disabled=not (cf_account_id.strip() and cf_api_token.strip()),
        use_container_width=True,
    ):
        with st.spinner("Verificando token na API…"):
            verdict = asyncio.run(
                test_cloudflare_creds(cf_account_id.strip(), cf_api_token.strip())
            )
        st.session_state["cf_validation"] = verdict
    last_verdict = st.session_state.get("cf_validation")
    if last_verdict:
        (st.sidebar.success if last_verdict["ok"] else st.sidebar.error)(
            last_verdict["message"]
        )

    # worker_name (= subdomínio do cliente) é derivado automaticamente da
    # Razão Social na busca CNPJ; não há mais campo manual na sidebar.
    cf_worker_name = (
        st.session_state.get("derived_worker_name") or CLOUDFLARE_SCRIPT_NAME or ""
    )

    return {
        "adspower_mode": adspower_mode,
        "adspower_api_token": adspower_api_token.strip(),
        "adspower_user_id": adspower_user_id.strip(),
        "proxy_server": proxy_server.strip(),
        "proxy_user": proxy_user.strip(),
        "proxy_pass": proxy_pass,
        "sms_service_code": sms_service.strip(),
        "sms_country_code": sms_country.strip(),
        "cf_account_id": cf_account_id.strip(),
        "cf_api_token": cf_api_token.strip(),
        "cf_worker_name": cf_worker_name.strip(),
        "cf_pool_size": pool_size,
    }


# ---------------------------------------------------------------------------
# Pipeline runner (background thread + log queue)
# ---------------------------------------------------------------------------
class _QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.queue = q
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        # L3 — redação aplicada ANTES de empilhar na queue da UI.
        install_redaction(self)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.format(record))
        except queue.Full:
            pass


def _preflight_check(infra: dict[str, Any]) -> list[str]:
    """L1 — Sanity-check rápido das credenciais ANTES de iniciar a esteira.

    Retorna uma lista de mensagens descrevendo problemas. Lista vazia = OK.
    Não faz round-trip HTTP (custo zero, latência zero) — checa apenas
    presença/formato das chaves.
    """
    problems: list[str] = []
    if not SMS24H_API_KEY:
        problems.append(
            "NUMERO_VIRTUAL_API_KEY ausente — alocação de número falhará na etapa SMS."
        )
    if not INVERTTEXTO_API_TOKEN:
        problems.append("INVERTTEXTO_API_TOKEN ausente — geração de e-mail comercial indisponível.")
    cf_token = (infra.get("cf_api_token") or "").strip()
    cf_account = (infra.get("cf_account_id") or "").strip()
    if not (cf_token and cf_account) and not CLOUDFLARE_API_TOKENS:
        problems.append(
            "Cloudflare ausente — sem CLOUDFLARE_API_TOKEN/ACCOUNT_ID "
            "(ou pool no env) o deploy do Worker falhará."
        )
    if cf_token and len(cf_token) < 30:
        problems.append("CLOUDFLARE_API_TOKEN parece curto demais — verifique se colou inteiro.")

    mode = infra.get("adspower_mode", "off")
    if mode in {"create", "reuse"}:
        if not (infra.get("adspower_api_token") or "").strip():
            problems.append("AdsPower selecionado mas o **API Token** está em branco.")
        if mode == "reuse":
            classification = _classify_adspower_input(infra.get("adspower_user_id") or "")
            if classification == "empty":
                problems.append(
                    "Modo 'Reutilizar' selecionado mas o **Profile ID** está vazio. "
                    "Troque para 'Criar perfil novo' ou cole o ID."
                )
            elif classification in {"cnpj", "cpf", "api_key", "too_long"}:
                problems.append(
                    "Profile ID do AdsPower parece inválido — não é um ID curto. "
                    "Confira em 'Browser Profiles' do painel."
                )
    return problems


def _build_proxy_cfg(infra: dict[str, Any]) -> dict[str, str] | None:
    """M3 — converte os campos da sidebar em proxy_cfg injetável.

    Em modo AdsPower o navegador já injeta proxy pelo perfil; devolvemos
    None nesse caso (e o runner sabe ignorar PROXY_* do env porque o
    flow do AdsPower nem chama ``_effective_proxy_cfg``).
    """
    if infra.get("adspower_mode", "off") in {"create", "reuse"}:
        return None
    if not infra.get("proxy_server"):
        return None
    cfg: dict[str, str] = {"server": infra["proxy_server"]}
    if infra.get("proxy_user"):
        cfg["username"] = infra["proxy_user"]
    if infra.get("proxy_pass"):
        cfg["password"] = infra["proxy_pass"]
    return cfg


def _run_pipeline_thread(runner: AutomationRunner, out_box: dict[str, Any]) -> None:
    try:
        out_box["result"] = asyncio.run(runner.run())
    except Exception as exc:  # noqa: BLE001
        out_box["error"] = exc
        # H1 — captura a etapa onde a esteira parou para a UI exibir.
        partial = getattr(runner, "last_result", None)
        if partial is not None:
            out_box["partial_result"] = partial
            out_box["failed_step"] = getattr(partial, "current_step", None)
    finally:
        # Garante fechamento do socket pool do Sms24hClient mesmo se runner.run()
        # crashou antes do finally interno do _verify_sms_via_api rodar (ex.:
        # exceção precoce no login antes de chegar na etapa de SMS).
        try:
            asyncio.run(runner.sms_client.aclose())
        except Exception:  # noqa: BLE001
            pass


def _persist_run_outcome(
    run_id: str | None, runner: AutomationRunner, out_box: dict[str, Any]
) -> None:
    """Grava o desfecho do run no store SQLite (Fase 0 — antes era jogado fora).

    Best-effort: o store NUNCA pode derrubar a UI. ``business_id``/``waba_id``/
    ``meta_token`` ficavam só no session_state e sumiam; agora viram registro
    durável de run + BM (base da seção de BMs e do dashboard).
    """
    if not run_id:
        return
    try:
        result = out_box.get("result") or out_box.get("partial_result")
        cnpj = getattr(getattr(runner, "cnpj_data", None), "cnpj", None)
        razao = getattr(getattr(runner, "cnpj_data", None), "razao_social", None)
        fields: dict[str, Any] = {}
        if out_box.get("error") is not None:
            fields["status"] = store.RUN_FAILED
            fields["error"] = str(out_box.get("error"))[:500]
        elif result is not None:
            fields["status"] = store.RUN_SUCCESS
        else:
            fields["status"] = store.RUN_FAILED
            fields["error"] = "run sem resultado"

        biz = waba = token = None
        if result is not None:
            biz = getattr(result, "business_id", None)
            waba = getattr(result, "waba_id", None)
            token = getattr(result, "meta_token", None)
            fields["step"] = getattr(result, "current_step", None)
            fields["business_id"] = biz
            fields["waba_id"] = waba
            fields["meta_token"] = token
        store.update_run(run_id, **fields)

        if biz:
            store.upsert_bm(
                business_id=biz,
                cnpj=cnpj,
                portfolio_name=razao,
                waba_id=waba,
                meta_token=token,
                last_run_id=run_id,
            )
    except Exception:  # noqa: BLE001
        logging.getLogger("meta_automation.app").debug(
            "store: falha ao persistir desfecho do run (seguindo).",
            exc_info=True,
        )


def _stream_pipeline(runner: AutomationRunner) -> dict[str, Any]:
    log_queue: "queue.Queue[str]" = queue.Queue(maxsize=2048)
    handler = _QueueLogHandler(log_queue)
    pipeline_logger = logging.getLogger("meta_automation")
    pipeline_logger.addHandler(handler)
    pipeline_logger.setLevel(logging.INFO)

    placeholder = st.empty()
    lines: list[str] = []
    placeholder.code("…aguardando boot do navegador…", language="log")

    # Fase 0: registra o run no store (durável) antes de iniciar.
    run_id: str | None = None
    try:
        run_id = store.create_run(
            cnpj=getattr(getattr(runner, "cnpj_data", None), "cnpj", None),
            label=getattr(getattr(runner, "cnpj_data", None), "razao_social", None),
            status=store.RUN_RUNNING,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger("meta_automation.app").debug(
            "store: falha ao criar run record (seguindo).", exc_info=True
        )

    out_box: dict[str, Any] = {}
    thread = threading.Thread(
        target=_run_pipeline_thread, args=(runner, out_box), daemon=True
    )
    thread.start()

    try:
        while thread.is_alive() or not log_queue.empty():
            try:
                line = log_queue.get(timeout=0.3)
                lines.append(line)
                placeholder.code("\n".join(lines[-300:]), language="log")
            except queue.Empty:
                if not thread.is_alive():
                    break
                time.sleep(0.05)
        thread.join(timeout=10)
    finally:
        pipeline_logger.removeHandler(handler)

    while not log_queue.empty():
        lines.append(log_queue.get_nowait())
    placeholder.code("\n".join(lines[-300:]), language="log")

    _persist_run_outcome(run_id, runner, out_box)
    return out_box


# ---------------------------------------------------------------------------
# Sync wrappers for async clients (Streamlit roda em thread síncrono)
# ---------------------------------------------------------------------------
def _fetch_cnpj_sync(cnpj_digits: str) -> CNPJData:
    return asyncio.run(BrasilApiClient().fetch(cnpj_digits))


def _generate_email_sync(prefix_hint: str) -> tuple[str, str]:
    return asyncio.run(
        InvertTextoEmailClient().generate_email(prefix_hint=prefix_hint)
    )


def _allocate_sms_sync(
    *, service_code: str | None = None, country_code: str | None = None,
) -> tuple[str, str]:
    async def _go() -> tuple[str, str]:
        kwargs: dict[str, Any] = {}
        if service_code:
            kwargs["service_code"] = service_code
        if country_code:
            kwargs["country_code"] = country_code
        async with Sms24hClient(**kwargs) as sms:
            return await sms.acquire_number()
    return asyncio.run(_go())


def _fetch_sms_code_sync(
    activation_id: str, *, poll_timeout_s: float = 120.0
) -> str:
    """Busca no numero-virtual o CÓDIGO de verificação que a Meta enviou por SMS.

    Distinto do ``activation_id`` (id da reserva do chip). O operador
    confundia os dois e colava o activation_id no campo da Meta — daí o
    'código incorreto'. Esta função devolve SÓ o código numérico real.

    Faz polling (até ``poll_timeout_s``) e extrai a maior sequência numérica
    do payload via ``SMS_CODE_REGEX`` (mesma regra do bot em
    :meth:`AutomationRunner._verify_sms_via_api`). Levanta ``Sms24hError`` se
    o código ainda não chegou (timeout) ou se o payload não tiver dígitos.
    """
    async def _go() -> str:
        async with Sms24hClient() as sms:
            payload = await sms.get_sms_code(
                activation_id, poll_timeout_s=poll_timeout_s
            )
        matches = SMS_CODE_REGEX.findall(payload or "")
        if not matches:
            raise Sms24hError(
                f"SMS chegou mas sem código numérico reconhecível: {payload!r}"
            )
        return max(matches, key=len)  # maior sequência = o código

    return asyncio.run(_go())


def _deploy_cloudflare_sync(
    *, account_id: str, api_token: str, worker_name: str,
    cnpj_digits: str, token_hash: str, domain: str,
) -> str:
    deployer = CloudflareDeployer(
        account_id=account_id, api_token=api_token, worker_name=worker_name,
    )
    return asyncio.run(
        deployer.deploy(domain, cnpj_digits=cnpj_digits, token_hash=token_hash)
    )


def _is_cnpj_complete(data: CNPJData) -> tuple[bool, list[str]]:
    """Avalia se a BrasilAPI devolveu campos mínimos para popular o form."""
    missing: list[str] = []
    if not (data.razao_social or "").strip():
        missing.append("Razão Social")
    if not (data.logradouro or "").strip():
        missing.append("Logradouro")
    if not (data.cidade or "").strip():
        missing.append("Município")
    if not (data.uf or "").strip():
        missing.append("UF")
    return (not missing), missing


async def _run_cascade(cnpj_digits: str) -> dict[str, Any]:
    """Esteira reativa pós-CNPJ.

    Encadeia: BrasilAPI → e-mail Uorak (local) → SMS (best-effort) → URL
    pública do Worker (derivada). Cada etapa registra status em ``steps``;
    falhas não-fatais não bloqueiam as próximas.
    """
    steps: dict[str, dict[str, Any]] = {}
    out: dict[str, Any] = {"steps": steps}

    # Etapa 1 — CNPJ
    try:
        data = await BrasilApiClient().fetch(cnpj_digits)
    except BrasilApiError as exc:
        steps["cnpj"] = {"ok": False, "error": str(exc)}
        return out
    out["cnpj_data"] = data
    complete, missing = _is_cnpj_complete(data)
    steps["cnpj"] = {
        "ok": True, "complete": complete, "missing": missing,
    }

    # Deriva nome admin + slug imediatamente (necessário para email + worker).
    razao = data.razao_social or ""
    admin_full = razao
    if razao and razao.split()[0].replace(".", "").isdigit():
        admin_full = " ".join(razao.split()[1:])
    out["admin_full"] = admin_full
    first, last = split_admin_name(admin_full)
    out["admin_first"] = first
    out["admin_last"] = last
    slug = slugify_for_worker(admin_full or data.nome_fantasia or "")
    out["slug"] = slug

    # Etapa 2 — E-mail Uorak (local, sem rede).
    email_addr = generate_uorak_email(admin_full)
    if email_addr:
        steps["email"] = {"ok": True, "address": email_addr}
        out["email"] = email_addr
    else:
        steps["email"] = {"ok": False, "error": "Slug vazio — admin_full ausente."}

    # Etapa 3 — numero-virtual.com (chip do Facebook).
    if SMS24H_API_KEY:
        try:
            async with Sms24hClient() as sms:
                activation_id, phone = await sms.acquire_number()
        except Sms24hError as exc:
            steps["sms"] = {"ok": False, "error": str(exc)}
        else:
            steps["sms"] = {"ok": True, "activation_id": activation_id, "phone": phone}
            out["sms_activation_id"] = activation_id
            out["sms_phone"] = phone
    else:
        steps["sms"] = {
            "ok": False,
            "error": "NUMERO_VIRTUAL_API_KEY ausente no .env.",
        }

    # Etapa 4 — URL pública do Worker (derivada; não há PUT ainda).
    if slug:
        out["worker_url"] = (
            f"https://{slug}.{CLOUDFLARE_WORKERS_SUBDOMAIN}.workers.dev/"
        )
        steps["worker_url"] = {"ok": True, "url": out["worker_url"]}
    else:
        steps["worker_url"] = {"ok": False, "error": "Slug vazio."}

    return out


def _run_cascade_sync(cnpj_digits: str) -> dict[str, Any]:
    return asyncio.run(_run_cascade(cnpj_digits))


def _format_phone_display(raw: str) -> str:
    digits = only_digits(raw)
    if len(digits) >= 11:
        return f"({digits[-11:-9]}) {digits[-9:-4]}-{digits[-4:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return raw


# ---------------------------------------------------------------------------
# Render: Resultado
# ---------------------------------------------------------------------------
def _render_result(result: AutomationResult) -> None:
    st.subheader("Resultado da execução")
    # H1 — etapa final visível (em caso de falha mostra ONDE parou).
    if getattr(result, "current_step", None):
        st.info(f"Etapa final: **{result.current_step}**")
    # PR1 — diagnóstico de falha de login (POR QUE parou, não só onde).
    if getattr(result, "failure_state", None):
        st.error(
            f"Diagnóstico do login: {result.failure_detail or result.failure_state} "
            f"(estado: `{result.failure_state}`)"
        )
    # Chips de status: legível de relance (ok/pendente/atenção).
    chips: list[tuple[str, str]] = [
        ("Login", "ok" if result.logged_in else "err"),
        # "ok" também quando a BM já existia e foi reusada (idempotência).
        ("Portfólio", "ok" if (result.portfolio_created or result.portfolio_reused) else "wait"),
        ("Detalhes", "ok" if result.business_details_saved else "wait"),
        ("Domínio", "ok" if result.domain_added else "wait"),
        ("SMS validado", "ok" if result.sms_verified else "wait"),
    ]
    if result.used_storage_state:
        chips.insert(1, ("Sessão restaurada", "ok"))
    if result.captcha_solved:
        chips.append(("Captcha resolvido", "ok"))
    if result.sms_intervention:
        chips.append(("Intervenção manual", "warn"))
    if result.portfolio_limit_reached:
        chips.append(("Limite de portfólios", "warn"))
    _mark = {"ok": "✓", "wait": "○", "warn": "!", "err": "✕"}
    chips_html = "".join(
        f'<span class="chip chip-{state}">{_mark[state]}&nbsp; {label}</span>'
        for label, state in chips
    )
    st.markdown(f'<div class="chips-row">{chips_html}</div>', unsafe_allow_html=True)
    if result.domain_meta_tag:
        st.subheader("Metatag de verificação de domínio")
        st.code(result.domain_meta_tag, language="html")
    if result.meta_verification_token:
        st.subheader("Token Meta capturado (injetado no Worker)")
        st.code(result.meta_verification_token, language="text")
        st.caption(
            "Capturado automaticamente do modal de verificação do Meta e "
            "re-injetado no Worker antes do clique em 'Verificar'."
        )


# ===========================================================================
# Renderização principal
# ===========================================================================
infra = _render_infra_sidebar()

# View "Lote / Paralelo": usa a mesma infra (AdsPower/Cloudflare/SMS) e para
# aqui — não renderiza o fluxo single-run de 4 passos.
if _view == "Lote":
    _view_batch(infra)
    st.stop()


# ---------------------------------------------------------------------------
# Etapa 1 — Busca CNPJ via BrasilAPI
# ---------------------------------------------------------------------------
_step("1", "Consulta CNPJ")
cnpj_col, btn_col = st.columns([3, 1])
with cnpj_col:
    cnpj_raw = st.text_input(
        "CNPJ do cliente (somente dígitos ou com pontuação)",
        key="cnpj_raw_input",
        placeholder="00.000.000/0000-00",
    )
with btn_col:
    st.write("")  # alinha verticalmente com o input
    fetch_clicked = st.button("Buscar via API", type="primary")

if fetch_clicked:
    digits = only_digits(cnpj_raw)
    if len(digits) != 14:
        st.error("CNPJ inválido — informe 14 dígitos.")
    else:
        # Reset estados dependentes (evita mistura entre buscas).
        # IMPORTANTE: inclui TODAS as keys form_* — o Streamlit ignora o
        # value= de um widget cuja key já existe no session_state, então sem
        # este pop os campos (endereço, telefone…) ficariam com os dados do
        # CNPJ anterior em vez de puxar os do novo.
        for k in (
            "cnpj_data", "admin_full", "admin_first_name", "admin_last_name",
            "derived_worker_name", "generated_email", "deploy_url",
            "deploy_done", "sms_phone", "sms_activation_id", "sms_done",
            "sms_code",
            "worker_predicted_url", "cascade_steps", "form_domain",
            "form_admin_full", "form_admin_first", "form_admin_last",
            "form_razao_social", "form_nome_fantasia", "form_cnpj",
            "form_business_email", "form_logradouro", "form_numero",
            "form_bairro", "form_cidade", "form_uf", "form_cep",
            "form_telefone",
        ):
            st.session_state.pop(k, None)

        with st.spinner("Executando esteira: CNPJ → e-mail → SMS → site…"):
            cascade = _run_cascade_sync(digits)

        steps = cascade["steps"]
        st.session_state["cascade_steps"] = steps

        cnpj_step = steps.get("cnpj") or {}
        if not cnpj_step.get("ok"):
            st.error(f"BrasilAPI: {cnpj_step.get('error', 'falha desconhecida')}.")
            st.markdown(
                "Consulte a Receita manualmente: "
                "[Cartão CNPJ — Receita Federal]"
                "(https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/)"
            )
        else:
            data = cascade["cnpj_data"]
            st.session_state["cnpj_data"] = data.model_dump()
            st.session_state["admin_full"] = cascade["admin_full"]
            st.session_state["admin_first_name"] = cascade["admin_first"]
            st.session_state["admin_last_name"] = cascade["admin_last"]
            if cascade.get("slug"):
                st.session_state["derived_worker_name"] = cascade["slug"]
            if cascade.get("email"):
                st.session_state["generated_email"] = cascade["email"]
            if cascade.get("sms_activation_id"):
                st.session_state["sms_activation_id"] = cascade["sms_activation_id"]
                st.session_state["sms_phone"] = cascade["sms_phone"]
                st.session_state["sms_done"] = True
            if cascade.get("worker_url"):
                worker_url = cascade["worker_url"]
                st.session_state["worker_predicted_url"] = worker_url
                # Auto-preenchimento: Meta aceita só o hostname (sem protocolo
                # e sem barra final). Cliente pode editar antes do deploy se
                # quiser apontar um CNAME custom.
                host = (
                    worker_url.replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/")
                )
                st.session_state["form_domain"] = host

            if not cnpj_step.get("complete"):
                st.warning(
                    "Cadastro incompleto na BrasilAPI (campos faltantes: "
                    f"{', '.join(cnpj_step.get('missing', []))}). "
                    "Complemente manualmente ou consulte o "
                    "[Cartão CNPJ — Receita]"
                    "(https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/)."
                )
            else:
                st.success("Dados extraídos da Receita.")
            st.rerun()


# ---------------------------------------------------------------------------
# Painel da esteira reativa (status visual de cada etapa do cascade)
# ---------------------------------------------------------------------------
if st.session_state.get("cascade_steps"):
    _steps = st.session_state["cascade_steps"]
    badge_cols = st.columns(4)
    _labels = [
        ("cnpj", "🏢 CNPJ"),
        ("email", "✉️ E-mail"),
        ("sms", "📲 SMS"),
        ("worker_url", "☁️ Site"),
    ]
    for col, (key, label) in zip(badge_cols, _labels):
        step = _steps.get(key) or {}
        with col:
            if step.get("ok"):
                st.success(f"{label}: OK")
            else:
                err = step.get("error", "—")
                st.error(f"{label}: {err[:80]}")


# ---------------------------------------------------------------------------
# Etapa 1.5 — Mapeamento de campos (auto-populado, editável)
# ---------------------------------------------------------------------------
if "cnpj_data" in st.session_state:
    data = CNPJData(**st.session_state["cnpj_data"])
    st.divider()
    st.subheader("Dados do cliente")
    st.caption("Autopreenchidos pela Receita — edite se necessário.")

    c1, c2 = st.columns(2)
    with c1:
        st.text_input(
            "Nome completo do administrador",
            value=st.session_state.get("admin_full", ""),
            key="form_admin_full",
        )
        st.text_input(
            "Nome", value=st.session_state.get("admin_first_name", ""),
            key="form_admin_first",
        )
        st.text_input(
            "Sobrenome", value=st.session_state.get("admin_last_name", ""),
            key="form_admin_last",
        )
    with c2:
        st.text_input(
            "Razão social",
            value=data.razao_social or "",
            key="form_razao_social",
            disabled=True,
            help="Imutável — string oficial da Receita. Padrão MEI: CNPJ + Nome.",
        )
        st.text_input(
            "Nome fantasia",
            value=data.nome_fantasia or "",
            key="form_nome_fantasia",
        )
        st.text_input("CNPJ", value=data.cnpj or "", disabled=True, key="form_cnpj")

    # E-mail: auto-gerado (slug@uorak.com). Caixa real via API é opcional.
    em_col, em_btn = st.columns([3, 1])
    with em_col:
        default_email = (
            st.session_state.get("generated_email")
            or data.email
            or ""
        )
        email_value = st.text_input(
            "E-mail comercial",
            value=default_email,
            key="form_business_email",
            help="Auto-gerado como slug(admin)@uorak.com. Edite se necessário.",
        )
    with em_btn:
        st.write("")
        gen_clicked = st.button("Caixa real (API)", disabled=not INVERTTEXTO_API_TOKEN)
    if gen_clicked:
        with st.spinner("Solicitando caixa real na InvertTexto…"):
            try:
                addr, mailbox_token = _generate_email_sync(
                    prefix_hint=st.session_state.get("admin_full", "")
                )
            except InvertTextoError as exc:
                st.error(f"InvertTexto: {exc}")
            else:
                st.session_state["generated_email"] = addr
                st.session_state["inverttexto_mailbox_token"] = mailbox_token
                st.success(f"Caixa criada: {addr}")
                st.rerun()

    st.markdown("**Endereço**")
    a1, a2, a3 = st.columns([3, 1, 2])
    with a1:
        st.text_input("Logradouro", value=data.logradouro or "", key="form_logradouro")
    with a2:
        st.text_input("Número", value=data.numero or "", key="form_numero")
    with a3:
        st.text_input("Bairro", value=data.bairro or "", key="form_bairro")
    b1, b2, b3 = st.columns([3, 1, 2])
    with b1:
        st.text_input("Cidade", value=data.cidade or "", key="form_cidade")
    with b2:
        st.text_input("UF", value=data.uf or "", key="form_uf")
    with b3:
        st.text_input("CEP", value=data.cep or "", key="form_cep")
    st.text_input(
        "Telefone comercial", value=data.telefone or "", key="form_telefone",
    )

    # -----------------------------------------------------------------------
    # Etapa 2 — Deploy instantâneo do Worker
    # -----------------------------------------------------------------------
    st.divider()
    _step("2", "Site (Cloudflare Worker)")

    # URL prevista — derivada da Razão Social na esteira, sem precisar
    # publicar o Worker. Aparece imediatamente após a busca CNPJ.
    predicted_url = st.session_state.get("worker_predicted_url")
    if predicted_url:
        st.info(f"🔗 URL prevista: {predicted_url}")
        st.caption(
            f"Subdomínio Cloudflare: `{CLOUDFLARE_WORKERS_SUBDOMAIN}.workers.dev` "
            f"— ajuste via env `CLOUDFLARE_WORKERS_SUBDOMAIN`."
        )

    domain_input = st.text_input(
        "Domínio para verificação de marca",
        key="form_domain",
        placeholder="empresa.com.br",
        help="Usado no PUT do Worker e na verificação do Business Suite.",
    )
    token_hash_input = st.text_input(
        "Token de verificação Meta (facebook-domain-verification)",
        key="form_token_hash",
        help="Cole o conteúdo do meta tag emitido pelo Business Suite (≥8 chars).",
    )

    cf_ready = bool(
        infra["cf_account_id"] and infra["cf_api_token"] and infra["cf_worker_name"]
    )
    deploy_clicked = st.button(
        "Deploy do Site agora",
        type="primary",
        disabled=not (cf_ready and domain_input and token_hash_input),
    )
    if deploy_clicked:
        cnpj_digits = only_digits(data.cnpj or "")
        with st.spinner("Publicando Worker no edge…"):
            try:
                worker_name = _deploy_cloudflare_sync(
                    account_id=infra["cf_account_id"],
                    api_token=infra["cf_api_token"],
                    worker_name=infra["cf_worker_name"],
                    cnpj_digits=cnpj_digits,
                    token_hash=token_hash_input.strip(),
                    domain=domain_input.strip(),
                )
            except CloudflareDeployerError as exc:
                st.error(f"Cloudflare: {exc}")
            else:
                public_url = (
                    predicted_url
                    or f"https://{worker_name}.{CLOUDFLARE_WORKERS_SUBDOMAIN}.workers.dev/"
                )
                st.session_state["deploy_url"] = public_url
                st.session_state["deploy_done"] = True
                # Auto-preenche o "Domínio para verificação de marca" com o
                # host workers.dev — Meta usará essa URL no GET de verificação.
                deployed_host = (
                    public_url.replace("https://", "")
                    .replace("http://", "")
                    .rstrip("/")
                )
                st.session_state["form_domain"] = deployed_host
                st.success(f"Worker publicado: {worker_name}")
                st.rerun()

    if st.session_state.get("deploy_done"):
        deployed = st.session_state["deploy_url"]
        st.markdown(f"### [`{deployed}`]({deployed})")
        st.caption(
            "URL pública do Worker — Meta usará esta URL para localizar a "
            "meta-tag `facebook-domain-verification`."
        )

    # -----------------------------------------------------------------------
    # Etapa 3 — Número do Facebook (SMS)
    # -----------------------------------------------------------------------
    st.divider()
    _step("3", "Número do Facebook (SMS)")

    if not SMS24H_API_KEY:
        st.warning(
            "`NUMERO_VIRTUAL_API_KEY` não configurada no `.env` — o gateway de SMS "
            "ficará indisponível. Defina a chave e reinicie o app."
        )

    sms_col_a, sms_col_b = st.columns([1, 2])
    with sms_col_a:
        sms_alloc = st.button("Alocar número", disabled=not SMS24H_API_KEY)
    with sms_col_b:
        if st.session_state.get("sms_phone"):
            st.markdown(
                f"### 📞 `{_format_phone_display(st.session_state['sms_phone'])}`"
            )
            st.caption(
                f"activation_id (id da reserva, **NÃO é o código**) = "
                f"`{st.session_state['sms_activation_id']}`"
            )
            # --- Código de verificação enviado pela Meta -------------------
            # O operador colava o activation_id no campo da Meta por engano.
            # Aqui buscamos no numero-virtual o CÓDIGO real e mostramos em destaque.
            fetch_code = st.button(
                "📨 Buscar código SMS da Meta",
                help="Clique DEPOIS de acionar 'Enviar SMS' na janela da Meta.",
            )
            if fetch_code:
                with st.spinner("Aguardando o SMS da Meta chegar no numero-virtual…"):
                    try:
                        code = _fetch_sms_code_sync(
                            st.session_state["sms_activation_id"]
                        )
                    except Sms24hError as exc:
                        st.warning(
                            f"Código ainda não chegou ({exc}). Reenvie o SMS "
                            "na Meta e tente de novo."
                        )
                    else:
                        st.session_state["sms_code"] = code
            if st.session_state.get("sms_code"):
                st.success("Código recebido da Meta — digite ESTE na janela:")
                st.markdown(f"# `{st.session_state['sms_code']}`")
    if sms_alloc:
        with st.spinner("Reservando chip no numero-virtual…"):
            try:
                activation_id, phone = _allocate_sms_sync(
                    service_code=st.session_state.get("sms_service_code"),
                    country_code=st.session_state.get("sms_country_code"),
                )
            except Sms24hError as exc:
                st.error(f"numero-virtual: {exc}")
            else:
                st.session_state["sms_activation_id"] = activation_id
                st.session_state["sms_phone"] = phone
                st.session_state["sms_done"] = True
                st.session_state.pop("sms_code", None)  # chip novo → código velho inválido
                st.rerun()

    # -----------------------------------------------------------------------
    # Etapa 4 — Credenciais Meta + run pipeline
    # -----------------------------------------------------------------------
    st.divider()
    _step("4", "Credenciais Meta")
    with st.form("meta_creds_form"):
        email_meta = st.text_input("E-mail ou telefone do Facebook")
        password = st.text_input("Senha", type="password")
        totp_seed = st.text_input(
            "Chave Secreta do 2FA (Seed TOTP)", type="password"
        )
        st.caption(
            f"Nome do Portfólio Comercial será **{data.razao_social or '—'}** "
            "(fixo = Razão Social)."
        )
        headless = st.checkbox("Executar em modo headless", value=False)
        calibration_mode = st.checkbox(
            "🔧 Modo calibração (não para no 1º erro; salva DOM de cada passo)",
            value=False,
            help="A esteira segue mesmo quando um passo falha, salvando DOM + "
            "screenshot de cada falha em .state/. Use em contas que JÁ têm "
            "portfólio para capturar o DOM real dos passos 2/3/4 num run só.",
        )
        recon_mode = st.checkbox(
            "🔍 Modo recon (só captura DOM das telas, sem preencher)",
            value=False,
            help="Após login, percorre overview/detalhes/domínios abrindo cada "
            "'Editar' e salva DOM+screenshot. Zero risco — não preenche nada.",
        )
        submitted = st.form_submit_button("Iniciar automação", type="primary")

    if submitted:
        if not (email_meta and password and totp_seed):
            st.error("Preencha e-mail/telefone, senha e seed do 2FA.")
            st.stop()
        if not st.session_state.get("form_business_email"):
            st.error("E-mail comercial vazio — gere via InvertTexto ou preencha.")
            st.stop()

        # L1 — Preflight de credenciais (zero round-trip, só presença).
        preflight = _preflight_check(infra)
        if preflight:
            st.error("Preflight bloqueou a execução:")
            for msg in preflight:
                st.error(f"  • {msg}")
            st.stop()

        # Reidrata CNPJData com edições da UI.
        edited = {
            **st.session_state["cnpj_data"],
            "razao_social": data.razao_social,  # imutável
            "nome_fantasia": st.session_state.get("form_nome_fantasia") or None,
            "logradouro": st.session_state.get("form_logradouro") or None,
            "numero": st.session_state.get("form_numero") or None,
            "bairro": st.session_state.get("form_bairro") or None,
            "cidade": st.session_state.get("form_cidade") or None,
            "uf": (st.session_state.get("form_uf") or "").upper() or None,
            "cep": st.session_state.get("form_cep") or None,
            "telefone": st.session_state.get("form_telefone") or None,
            "email": st.session_state.get("form_business_email") or None,
        }
        cnpj_data_final = CNPJData(**edited)
        creds = MetaCredentials(
            email_or_phone=email_meta, password=password, totp_seed=totp_seed,
        )
        profile = BusinessProfile(
            admin_name=st.session_state.get("form_admin_full") or "",
            business_email=st.session_state.get("form_business_email") or "",
            domain=st.session_state.get("form_domain") or None,
            portfolio_name=data.razao_social or None,
        )

        proxy_cfg = _build_proxy_cfg(infra)

        runner = AutomationRunner(
            credentials=creds,
            cnpj_data=cnpj_data_final,
            business_profile=profile,
            headless=headless,
            adspower_mode=infra.get("adspower_mode", "off"),
            adspower_api_key=infra.get("adspower_api_token") or None,
            adspower_user_id=infra.get("adspower_user_id") or None,
            cloudflare_account_id=infra["cf_account_id"] or None,
            cloudflare_api_token=infra["cf_api_token"] or None,
            proxy_cfg=proxy_cfg,
            cloudflare_worker_name=infra["cf_worker_name"] or None,
            preallocated_sms_activation_id=st.session_state.get("sms_activation_id"),
            preallocated_sms_phone=st.session_state.get("sms_phone"),
            sms_service_code=infra.get("sms_service_code") or None,
            sms_country_code=infra.get("sms_country_code") or None,
            calibration_mode=calibration_mode,
            recon_dump=recon_mode,
        )

        st.subheader("Execução")
        out = _stream_pipeline(runner)
        if "error" in out:
            failed_step = out.get("failed_step") or "boot"
            st.error(
                f"Pipeline falhou na etapa **{failed_step}**: "
                f"{type(out['error']).__name__}: {out['error']}"
            )
        result = out.get("result") or out.get("partial_result")
        if isinstance(result, AutomationResult) and getattr(
            result, "account_discarded", False
        ):
            st.warning(
                "🗑️ **DESCARTAR esta conta** — ela já possui ≥2 portfólios "
                "comerciais (no limite do Meta, não cria outro). Use outra "
                "conta/CNPJ na próxima execução."
            )
        if isinstance(result, AutomationResult):
            _render_result(result)
            with st.expander("AutomationResult bruto"):
                st.json(asdict(result))
