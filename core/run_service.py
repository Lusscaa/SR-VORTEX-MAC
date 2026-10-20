"""Execução HEADLESS de um job: cascata (CNPJ→email→SMS→domínio) + run.

É o ``work_fn`` que o :class:`core.job_manager.JobManager` roda em cada worker.
Replica, sem Streamlit, o que o ``app.py`` faz interativamente no fluxo
single-run: busca o CNPJ, deriva nome do admin / e-mail comercial / domínio do
Worker, aloca o chip de SMS e monta o :class:`AutomationRunner`. O PUT do
Worker no Cloudflare acontece DENTRO do runner (passo 3), então aqui só
derivamos o domínio.

Diferença vs o fluxo interativo: não há etapa de MAPEAMENTO manual — os
valores derivados da BrasilAPI são usados direto. É o trade-off do lote
(throughput em vez de edição campo-a-campo). As credenciais da Meta
(e-mail/senha/seed 2FA) vêm no :class:`JobSpec` (por conta — CSV ou UI).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from config import (
    NUMERO_VIRTUAL_TARGET_PRICE,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
    SMS24H_API_KEY,
)
from core import store
from core import run_control
from core.netlify_deployer import validate_netlify_ready
from core.cloudflare_deployer import validate_cloudflare_domain_ready
from core.cnpj_lookup import fetch_cnpj_cached
from core.errors import PreflightError
from core import (
    AutomationResult,
    AutomationRunner,
    BrasilApiError,
    BusinessProfile,
    CNPJData,
    MetaCredentials,
    Sms24hClient,
    Sms24hError,
    Sms24hNoBalanceError,
    generate_uorak_email,
    slugify_for_worker,
)
from utils.helpers import only_digits

logger = logging.getLogger("meta_automation.run_service")


@dataclass
class JobSpec:
    """Tudo que um worker precisa pra provisionar uma conta sem interação."""

    cnpj: str
    meta_email_or_phone: str
    meta_password: str
    meta_totp_seed: str
    # Email da conta + senha do email + recuperação (combolist) — só p/ as
    # Observações do AdsPower, não usados no login.
    meta_email_address: str = ""
    meta_email_password: str = ""
    meta_recovery_email: str = ""
    # Infra compartilhada do lote:
    adspower_mode: str = "create"
    adspower_api_key: Optional[str] = None
    adspower_user_id: Optional[str] = None
    cloudflare_account_id: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    cloudflare_worker_name: Optional[str] = None
    # Subdomínio workers.dev informado manualmente na conta (fallback quando o
    # token não consegue ler pela API). Nunca usa o default velho do .env.
    cloudflare_subdomain: Optional[str] = None
    # Netlify (host atual). Override opcional: domínio-base + token da conta.
    # Quando vazios, o run usa o domínio ativo do pool (store.netlify_domains).
    netlify_domain: Optional[str] = None
    netlify_api_token: Optional[str] = None
    sms_service_code: Optional[str] = None
    sms_country_code: Optional[str] = None
    # Preset de compra do SMS escolhido no painel:
    # 'facebook' | 'other' | 'sms24h' | 'smsrush' | None.
    # None = usa o default do .env/config (NUMERO_VIRTUAL_*).
    sms_option: Optional[str] = None
    sms_ddd: Optional[str] = None
    smsrush_server: Optional[int] = None
    sms_fallback_option: Optional[str] = None
    proxy_cfg: Optional[dict[str, str]] = None
    # Quando False, NÃO arrenda proxy do pool: roda pelo IP direto da máquina
    # (ex.: operador roteando pelo 4G do celular). O preflight sonda direto
    # (egress, não proxy_egress → não bate no 402 do Webshare sem banda) e o
    # AdsPower abre em no_proxy.
    use_proxy: bool = True
    headless: bool = False
    # Correções do operador aos dados da Receita (campo→valor), aplicadas sobre
    # o CNPJData após o fetch. Ex.: {"numero": "608"} quando a BrasilAPI traz "S/N".
    cnpj_overrides: Optional[dict[str, str]] = None
    # 2 BMs por perfil (mesma sessão/navegador/proxy): quando setado, o job cria
    # a 1ª BM com ``cnpj`` e, SEM fechar o navegador, a 2ª BM com ``cnpj2`` —
    # 1 conta → 2 portfólios → 2 CNPJs → 2 BMs. None = fluxo de 1 BM (padrão).
    cnpj2: Optional[str] = None
    cnpj2_overrides: Optional[dict[str, str]] = None
    browser_provider: str = "adspower"
    waba_enabled: bool = False
    stop_after_ad_account: bool = False

    @property
    def cnpj_digits(self) -> str:
        return only_digits(self.cnpj or "")

    @property
    def cnpj2_digits(self) -> str:
        return only_digits(self.cnpj2 or "")


# Campos do CNPJData que o operador pode corrigir pela UI (razao_social fora:
# é a string oficial da Receita, imutável).
_CNPJ_OVERRIDABLE = frozenset({
    "nome_fantasia", "logradouro", "numero", "complemento",
    "bairro", "cidade", "uf", "cep", "telefone", "email",
})


def _derive_admin_full(razao: str) -> str:
    """Nome do admin a partir da razão social — descarta QUALQUER token
    puramente numérico (raiz do CNPJ na FRENTE '97.554.463' OU CPF/número no
    FIM '01841413119'), de qualquer posição. A Receita/API põe o número num
    lado ou no outro conforme a fonte (produção 2026-09, conta Jader)."""
    def _is_num(tok: str) -> bool:
        s = (
            tok.replace(".", "").replace("-", "").replace("/", "").replace(",", "")
        )
        return bool(s) and s.isdigit()

    kept = [t for t in (razao or "").split() if not _is_num(t)]
    return " ".join(kept).strip() or (razao or "")


def _release_account_on_gate_abort(spec: "JobSpec", motivo: str) -> None:
    """Gate de hospedagem abortou ANTES de consumir SMS/conta → devolve a conta.

    O gate existe justamente para não gastar recursos quando o host não está
    pronto — mas a conta já tinha sido arrendada pelo disparo e ficava presa em
    'used' para sempre. Num blip de DNS isso queimava uma conta por run
    abortada (14 de uma vez em 2026-08-27) e o estoque secava sem nenhuma run
    ter de fato rodado. CNPJ não volta (regra absoluta do projeto)."""
    email = (getattr(spec, "meta_email_or_phone", "") or "").strip()
    if not email:
        return
    try:
        rec = store.force_release_fb_account(email)
        logger.warning(
            "gate de hospedagem abortou (%s) — conta %s %s.",
            motivo, email,
            "DEVOLVIDA ao estoque (nada foi consumido)" if rec
            else "NÃO devolvida (descartada ou fora do estoque)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate: falha ao devolver a conta %s ao estoque: %s", email, exc)


def _sms_target_price_brl() -> float:
    return float(NUMERO_VIRTUAL_TARGET_PRICE or 0) / 100.0


async def _wait_for_sms_balance(
    *,
    run_id: Optional[str],
    digits: str,
    detail: str,
) -> None:
    """Pausa a run até o operador confirmar que recarregou o saldo SMS."""
    target_brl = _sms_target_price_brl()
    message = (
        "Sem saldo no numero-virtual para puxar um número novo. "
        f"Adicione pelo menos R$ {target_brl:.2f} e clique em 'Atualizar saldo' para continuar. "
        f"Detalhe: {detail}"
    )
    logger.warning("SMS pausado para saldo (CNPJ %s): %s", digits, detail)
    if run_id:
        store.update_run(
            run_id,
            status=store.RUN_PAUSED,
            step="aguardando saldo SMS",
            error=message,
            sms_phone=None,
        )
    while True:
        mode = run_control.get_stop(run_id)
        if mode:
            raise run_control.RunStopRequested(run_id or "?", mode)
        if not run_id:
            await asyncio.sleep(1.0)
            continue
        current = store.get_run(run_id) or {}
        if current.get("status") == store.RUN_RUNNING:
            logger.info("SMS retomado após recarga de saldo (run %s).", run_id)
            return
        await asyncio.sleep(1.0)


async def _acquire_sms_for_run(
    *,
    spec: "JobSpec",
    run_id: Optional[str],
    digits: str,
) -> tuple[Optional[str], Optional[str]]:
    """Aloca um número SMS ou pausa a run em falta de saldo."""
    _sms_opt = (spec.sms_option or "").strip().lower()
    use_sms24h_org = _sms_opt == "sms24h"
    use_smsrush = _sms_opt == "smsrush"

    if not use_sms24h_org and not use_smsrush and not SMS24H_API_KEY:
        return None, None
    if use_sms24h_org:
        from config import SMS24H_ORG_API_KEY  # noqa: PLC0415
        if not SMS24H_ORG_API_KEY:
            return None, None
    if use_smsrush:
        from config import SMSRUSH_API_TOKEN  # noqa: PLC0415
        if not SMSRUSH_API_TOKEN:
            return None, None

    while True:
        try:
            if use_smsrush:
                from core.api_clients import SmsRushClient  # noqa: PLC0415
                _rush_kw: dict[str, Any] = {}
                if spec.smsrush_server:
                    _rush_kw["server"] = spec.smsrush_server
                async with SmsRushClient(**_rush_kw) as sms:
                    activation_id, phone = await sms.acquire_number()
            elif use_sms24h_org:
                from core.api_clients import Sms24hOrgClient  # noqa: PLC0415
                async with Sms24hOrgClient() as sms:
                    if spec.sms_ddd:
                        _target = spec.sms_ddd.strip()
                        for _ddd_try in range(30):
                            activation_id, phone = await sms.acquire_number()
                            _got = Sms24hClient._extract_ddd(phone)
                            if not _got or _got == _target:
                                break
                            logger.warning(
                                "sms24h.org DDD %s ≠ %s — cancelando %s [%d/30]",
                                _got, _target, activation_id, _ddd_try + 1,
                            )
                            await sms.set_status(activation_id, sms.STATUS_CANCEL)
                        else:
                            raise Sms24hError(
                                f"SMS24H sem número DDD {_target} após 30 tentativas"
                            )
                    else:
                        activation_id, phone = await sms.acquire_number()
            else:
                kwargs: dict[str, Any] = {}
                if spec.sms_service_code:
                    kwargs["service_code"] = spec.sms_service_code
                if spec.sms_country_code:
                    kwargs["country_code"] = spec.sms_country_code
                if spec.sms_ddd:
                    kwargs["allowed_ddds"] = frozenset([spec.sms_ddd.strip()])
                from config import sms_kwargs_for_option  # noqa: PLC0415
                kwargs.update(sms_kwargs_for_option(spec.sms_option))
                async with Sms24hClient(**kwargs) as sms:
                    activation_id, phone = await sms.acquire_number()
            if run_id and phone:
                store.update_run(run_id, sms_phone=phone, error=None)
            return activation_id, phone
        except Sms24hNoBalanceError as exc:
            await _wait_for_sms_balance(run_id=run_id, digits=digits, detail=str(exc))
            continue
        except Sms24hError as exc:
            logger.error(
                "❌ SMS CRÍTICO: %s — run cancelada! CNPJ %s será descartado.",
                exc, digits,
            )
            raise RuntimeError(f"SMS inacessível — run cancelada. {exc}") from exc


async def _execute_job_async(
    spec: JobSpec, run_id: Optional[str] = None
) -> AutomationResult:
    digits = spec.cnpj_digits
    if len(digits) != 14:
        raise ValueError(f"CNPJ inválido: {spec.cnpj!r}")

    # BLOQUEIO IMEDIATO: marca CNPJ como em uso neste run ANTES de fazer qualquer
    # coisa. Se o run morrer/travar, o CNPJ fica bloqueado e não é reutilizado.
    # Garante que settle_cnpj SEMPRE marca como discarded depois (mesmo que falhe).
    if run_id:
        try:
            store.mark_cnpj_in_use(digits, run_id)
            logger.info(f"[CNPJ_LOCK] CNPJ {digits} bloqueado para run {run_id}")
        except Exception as e:
            logger.error(f"[CNPJ_LOCK] Erro ao bloquear CNPJ {digits}: {e}")

    # 1) CNPJ (BrasilAPI, com cache durável — reusa a prévia já consultada na
    #    UI e não re-bate a API por run, evitando o 429 do passo 1).
    #    Sem filtro de logradouro: usa o CNPJ que o operador colocou no pool.
    try:
        data = await fetch_cnpj_cached(digits)
    except BrasilApiError as exc:
        raise RuntimeError(f"CNPJ lookup falhou: {exc}") from exc

    # Overrides do operador (UI): corrige o que a BrasilAPI erra/omite — ex.:
    # número do endereço que vem "S/N". Só campos conhecidos e não-vazios
    # (razao_social fica de fora: string oficial imutável).
    if spec.cnpj_overrides:
        clean = {
            k: v.strip()
            for k, v in spec.cnpj_overrides.items()
            if k in _CNPJ_OVERRIDABLE and isinstance(v, str) and v.strip()
        }
        ignored = [k for k in spec.cnpj_overrides if k not in _CNPJ_OVERRIDABLE]
        if ignored:
            logger.debug("CNPJ overrides ignorados (fora do whitelist): %s", ignored)
        if clean:
            # Re-valida pelo model p/ os field_validators normalizarem (uf →
            # maiúsculo, email → minúsculo etc.) — setattr cru os ignoraria.
            data = type(data).model_validate({**data.model_dump(), **clean})
            logger.info("CNPJ overrides aplicados: %s", ", ".join(sorted(clean)))

    # 2) Deriva admin / slug / e-mail comercial.
    admin_full = _derive_admin_full(data.razao_social or "")
    slug = slugify_for_worker(admin_full or data.nome_fantasia or "")
    if not slug:
        raise RuntimeError(
            "slug vazio (sem razão social/nome fantasia) — sem domínio derivável"
        )
    business_email = generate_uorak_email(admin_full) or ""
    try:
        data.email = business_email or data.email
    except Exception:  # noqa: BLE001
        pass

    # 2.5) GATE de hospedagem — a run NUNCA inicia (nem gasta SMS/conta) sem um
    # domínio pronto (nameservers propagados). O provedor ativo é escolhido em
    # settings.hosting_provider ('netlify' padrão | 'cloudflare'). Netlify segue
    # sendo o padrão e o caminho Cloudflare é ADITIVO (só entra quando marcado).
    hosting_provider = store.get_hosting_provider()
    cf_account_id = None
    cf_api_token = None
    netlify_token = ""
    base_domain = ""
    if hosting_provider == "cloudflare":
        active_cf = store.get_active_cloudflare_domain()
        if active_cf:
            base_domain = active_cf["domain"]
            cf_account_id = active_cf["account_id"]
            cf_api_token = active_cf["api_token"]
        ready = await validate_cloudflare_domain_ready(base_domain, cf_account_id, cf_api_token)
        if not ready["ok"]:
            _release_account_on_gate_abort(spec, "Cloudflare não pronto")
            raise PreflightError(
                "Cloudflare não pronto — run ABORTADA antes de consumir SMS/conta: "
                f"{ready['detail']}"
            )
    else:
        base_domain = (spec.netlify_domain or "").strip().lower().strip(".")
        netlify_token = (spec.netlify_api_token or "").strip()
        if not (base_domain and netlify_token):
            active = store.get_active_netlify_domain()
            if active:
                base_domain = active["domain"]
                netlify_token = active["api_token"]
        nf = await validate_netlify_ready(base_domain, netlify_token)
        if not nf["ok"]:
            _release_account_on_gate_abort(
                spec, "rede/DNS" if nf.get("network") else "Netlify não pronto"
            )
            raise PreflightError(
                "Netlify não pronto — run ABORTADA antes de consumir SMS/conta: "
                f"{nf['detail']}"
            )

    # Domínio do site = {slug}.{seu-dominio}. Ex.: camila.meudominio.com.br.
    domain = f"{slug}.{base_domain}"

    # 3) SMS — ADIADO: o número é comprado DENTRO do runner, logo antes do
    # passo 3/5 (business_details), para não desperdiçar crédito quando a run
    # falha antes da verificação (portfólio restrito, cadeado, bounce, etc.).
    # O runner chama _ensure_sms_phone_for_comprovante() no momento certo.
    sms_activation_id: Optional[str] = None
    sms_phone: Optional[str] = None

    # 4) Monta credenciais/perfil e o runner.
    creds = MetaCredentials(
        email_or_phone=spec.meta_email_or_phone,
        password=spec.meta_password,
        totp_seed=spec.meta_totp_seed,
        email_address=spec.meta_email_address or None,
        email_password=spec.meta_email_password or None,
        recovery_email=spec.meta_recovery_email or None,
    )
    profile = BusinessProfile(
        admin_name=admin_full,
        business_email=business_email,
        domain=domain,
        portfolio_name=data.razao_social or None,
    )

    # 3b) 2 BMs por perfil: prepara a UNIDADE da 2ª BM (CNPJ2) — mesma sessão,
    # navegador e proxy. Reserva o 2º número SMS AGORA (escolha do operador:
    # os 2 no início). Se qualquer preparo falhar, segue com 1 BM (best-effort).
    extra_bm_units: list[dict[str, Any]] = []
    digits2 = spec.cnpj2_digits
    if digits2:
        try:
            data2 = await fetch_cnpj_cached(digits2)
            if spec.cnpj2_overrides:
                clean2 = {
                    k: v.strip()
                    for k, v in spec.cnpj2_overrides.items()
                    if k in _CNPJ_OVERRIDABLE and isinstance(v, str) and v.strip()
                }
                if clean2:
                    data2 = type(data2).model_validate(
                        {**data2.model_dump(), **clean2}
                    )
            admin_full2 = _derive_admin_full(data2.razao_social or "")
            slug2 = slugify_for_worker(admin_full2 or data2.nome_fantasia or "")
            if not slug2:
                raise RuntimeError("slug2 vazio — sem domínio derivável p/ a 2ª BM")
            business_email2 = generate_uorak_email(admin_full2) or ""
            try:
                data2.email = business_email2 or data2.email
            except Exception:  # noqa: BLE001
                pass
            domain2 = f"{slug2}.{base_domain}"
            # SMS da 2ª BM NÃO é comprado aqui: o número da numero-virtual
            # EXPIRA em ~20 min e a BM #2 só chega na verificação ~20 min depois
            # (produção 2026-09: 2º número sempre 'expired' → SMS nunca chega).
            # O runner compra um número FRESCO no início da BM #2 (ver
            # _run_extra_bms). Fica None de propósito.
            sms2_id: Optional[str] = None
            sms2_phone: Optional[str] = None
            profile2 = BusinessProfile(
                admin_name=admin_full2,
                business_email=business_email2,
                domain=domain2,
                portfolio_name=data2.razao_social or None,
            )
            extra_bm_units.append(
                {
                    "cnpj_data": data2,
                    "business_profile": profile2,
                    "sms_activation_id": sms2_id,
                    "sms_phone": sms2_phone,
                }
            )
            logger.info(
                "2-BMs: 2ª BM preparada (CNPJ=%s, domínio=%s, sms=%s) — mesma "
                "sessão/proxy.", digits2, domain2, sms2_phone or "?",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "2-BMs: falha ao preparar a 2ª BM (CNPJ %s): %s — seguindo só "
                "com a 1ª BM.", digits2, exc,
            )
            extra_bm_units = []
    # Proxy do POOL — mascara o IP por conta (evita soft-block/derrubada).
    # STICKY por CNPJ (mesma conta → mesmo IP, sobrevive ao 2º passe); runs
    # paralelas pegam IPs distintos. Aplicado no perfil AdsPower na criação.
    # 'usar proxy' DESLIGADO (operador roteando pelo celular): não arrenda do
    # pool — roda pelo IP direto (preflight sonda 'egress', AdsPower no_proxy).
    leased = store.lease_proxy(digits) if spec.use_proxy else None
    if not spec.use_proxy:
        logger.info(
            "CNPJ %s: 'usar proxy' OFF — rodando pelo IP direto da máquina "
            "(sem pool; preflight direto; AdsPower no_proxy).", digits,
        )
    adspower_proxy = None
    proxy_cfg = spec.proxy_cfg
    if leased:
        adspower_proxy = {
            "scheme": leased.get("scheme") or "http",
            "host": leased.get("host"),
            "port": leased.get("port"),
            "username": leased.get("username"),
            "password": leased.get("password"),
        }
        proxy_cfg = {
            "server": f"{adspower_proxy['scheme']}://{adspower_proxy['host']}:{adspower_proxy['port']}"
        }
        if adspower_proxy["username"]:
            proxy_cfg["username"] = adspower_proxy["username"]
        if adspower_proxy["password"]:
            proxy_cfg["password"] = adspower_proxy["password"]
        logger.info(
            "Proxy do pool atribuído ao CNPJ %s: %s:%s",
            digits, adspower_proxy["host"], adspower_proxy["port"],
        )
    elif spec.use_proxy and PROXY_SERVER and PROXY_USERNAME and PROXY_PASSWORD:
        # Usa proxy Bright Data do .env se nenhum proxy alugado disponível
        host_port = PROXY_SERVER.split(":")
        adspower_proxy = {
            "scheme": "http",
            "host": host_port[0],
            "port": int(host_port[1]) if len(host_port) > 1 else 3128,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        }
        proxy_cfg = {
            "server": f"http://{adspower_proxy['host']}:{adspower_proxy['port']}"
        }
        if PROXY_USERNAME:
            proxy_cfg["username"] = PROXY_USERNAME
        if PROXY_PASSWORD:
            proxy_cfg["password"] = PROXY_PASSWORD
        logger.info(
            "Proxy Bright Data atribuído ao CNPJ %s: %s:%s",
            digits, adspower_proxy["host"], adspower_proxy["port"],
        )
    else:
        logger.info(
            "Pool de proxies vazio/esgotado — CNPJ %s roda sem proxy do pool.",
            digits,
        )

    runner = AutomationRunner(
        credentials=creds,
        cnpj_data=data,
        business_profile=profile,
        headless=spec.headless,
        adspower_mode=spec.adspower_mode,
        adspower_api_key=spec.adspower_api_key,
        adspower_user_id=spec.adspower_user_id,
        cloudflare_account_id=(cf_account_id if hosting_provider == "cloudflare" else spec.cloudflare_account_id),
        cloudflare_api_token=(cf_api_token if hosting_provider == "cloudflare" else spec.cloudflare_api_token),
        cloudflare_worker_name=spec.cloudflare_worker_name,
        hosting_provider=hosting_provider,
        netlify_domain=(None if hosting_provider == "cloudflare" else base_domain),
        netlify_api_token=netlify_token,
        proxy_cfg=proxy_cfg,
        use_proxy=spec.use_proxy,
        adspower_proxy=adspower_proxy,
        preallocated_sms_activation_id=sms_activation_id,
        preallocated_sms_phone=sms_phone,
        sms_service_code=spec.sms_service_code,
        sms_country_code=spec.sms_country_code,
        sms_option=spec.sms_option,
        sms_fallback_option=spec.sms_fallback_option,
        sms_ddd=spec.sms_ddd,
        smsrush_server=spec.smsrush_server,
        extra_bm_units=extra_bm_units,
        browser_provider=spec.browser_provider,
        waba_enabled=spec.waba_enabled,
        stop_after_ad_account=spec.stop_after_ad_account,
    )
    # run_id (o da UI/JobManager) no runner: usado pela parada cooperativa
    # (botões Encerrar/Assumir) em run_control. O saga tem um id próprio
    # (timestamp) — este é o id que o painel conhece.
    runner.run_id = run_id

    # Rotação de IP do proxy móvel ANTES de abrir o navegador: cada conta pega
    # um IP fresco da operadora. Best-effort — nunca derruba a run. Só quando há
    # proxy e PROXY_ROTATION_URL configurada (ver config).
    try:
        from config import PROXY_ROTATION_URL, PROXY_ROTATION_WAIT_S  # noqa: PLC0415
        if spec.use_proxy and (PROXY_ROTATION_URL or "").strip():
            import httpx  # noqa: PLC0415
            logger.info("proxy: rotacionando IP (link do provedor) antes da run…")
            try:
                async with httpx.AsyncClient(timeout=20.0) as _rc:
                    _rr = await _rc.get(PROXY_ROTATION_URL.strip())
                logger.info("proxy: rotação HTTP %s — aguardando %.0fs o novo IP subir.",
                            _rr.status_code, PROXY_ROTATION_WAIT_S)
            except Exception as _rex:  # noqa: BLE001
                logger.warning("proxy: falha ao rotacionar IP (%s) — seguindo com o IP atual.", _rex)
            import asyncio as _aio  # noqa: PLC0415
            await _aio.sleep(max(0.0, float(PROXY_ROTATION_WAIT_S)))
    except Exception as _rex2:  # noqa: BLE001 — rotação é best-effort
        logger.debug("proxy: rotação ignorada (%s).", _rex2)

    try:
        return await runner.run()
    except Exception as e:
        # ASSUMIR: operador assumiu a run — NUNCA re-enfileira nada nem descarta
        # CNPJ. Re-enfileirar criaria outra run/perfil por cima do que ele está
        # usando à mão.
        if run_control.is_assume_requested(run_id):
            logger.info(
                "run %s ASSUMIDA pelo operador — sem retry/re-enfileiramento (%s).",
                run_id, type(e).__name__,
            )
            raise
        # ENCERRADA pelo operador: nunca re-enfileira. Sem isto, o
        # restart_from_zero criava uma run NOVA (perfil novo, login de novo)
        # logo depois de ele fechar.
        if run_control.is_close_requested(run_id):
            logger.info(
                "run %s ENCERRADA pelo operador — sem retry/re-enfileiramento (%s).",
                run_id, type(e).__name__,
            )
            raise
        # Re-raise o erro original
        raise
    finally:
        # Persiste o telefone REALMENTE usado: pré-alocado aqui OU adquirido
        # lazy dentro do runner (passo 4/5). Fica visível no painel mesmo se a
        # run falhar antes do fim.
        final_phone = getattr(runner, "preallocated_sms_phone", None) or sms_phone
        if run_id and final_phone:
            try:
                store.update_run(run_id, sms_phone=final_phone)
            except Exception:  # noqa: BLE001 — telemetria, não derruba a run
                pass
        # Perfil AdsPower usado nesta run (criado ou reusado) — pra Histórico
        # mostrar qual perfil corresponde a qual conta/CNPJ.
        if run_id:
            try:
                # proxy_host: "host:port" para exibição no histórico
                _ap = adspower_proxy
                proxy_host_str: Optional[str] = None
                if _ap and _ap.get("host"):
                    proxy_host_str = f"{_ap['host']}:{_ap.get('port', '')}"
                store.update_run(
                    run_id,
                    profile_user_id=getattr(runner, "adspower_user_id", None),
                    profile_serial_number=getattr(runner, "adspower_serial_number", None),
                    proxy_host=proxy_host_str,
                )
            except Exception:  # noqa: BLE001 — telemetria, não derruba a run
                pass
        # Detecta se o run foi 100% concluído (verificação enviada + CONTA DE
        # ANÚNCIOS criada). Runs 100% consomem TODOS os recursos de uma vez —
        # conta, CNPJ e proxy NÃO devem voltar ao pool nem por engano.
        # 2026-09: o passo 4 virou a conta de anúncios (a WABA saiu do fluxo,
        # ver CRIAR_WABA); o critério espelha job_manager._run_is_complete.
        res = getattr(runner, "last_result", None)
        run_100pct = bool(
            getattr(res, "business_id", None)
            and getattr(res, "business_details_saved", False)
            and getattr(res, "domain_verified", False)
            and (
                getattr(res, "ad_account_id", None)
                or getattr(res, "ad_account_created", False)
            )
            and getattr(res, "business_verification_started", False)
            and getattr(res, "sms_verified", False)  # SMS DEVE SER VERIFICADO
        )

        # Conta: após run 100% concluído, SEMPRE marca como used —
        # independente do número de portfólios. Sem isso, conta com 1 portfólio
        # voltava ao pool (status=available) e era re-arrendada num run futuro
        # (verificada duas vezes). Com run incompleto, mantém a lógica de teto
        # de portfólios (pode rodar de novo se ainda não atingiu 2).
        try:
            pf = getattr(runner, "_portfolio_count", None)
            # Conta ATUAL do runner — pode ter sido trocada por rotação de conta
            # pós-bounce (a anterior já voltou ao pool na própria rotação). Sem
            # isso, a conta rotacionada vazaria 'used' e a antiga seria settle 2×.
            cur_email = getattr(
                getattr(runner, "credentials", None), "email_or_phone", None
            ) or spec.meta_email_or_phone
            wrong_pw = getattr(res, "failure_state", "") == "wrong_password"
            discarded = bool(getattr(res, "account_discarded", False))
            terminated = bool(getattr(res, "terminated", False))
            assumed = bool(getattr(res, "assumed", False)) or run_control.is_assume_requested(run_id)
            if assumed:
                # Operador vai concluir manualmente: NÃO mexe na conta (ele está
                # usando o perfil). A API já marcou a run como 'assumed'.
                logger.info(
                    "conta %s: run ASSUMIDA pelo operador — conta mantida como está.",
                    cur_email,
                )
            elif terminated:
                # 'Encerrar': a API já descartou a conta. Nada a fazer aqui
                # (evita que settle/freeze revertam o descarte).
                logger.info(
                    "conta %s: run ENCERRADA pelo operador — conta já descartada pela API.",
                    cur_email,
                )
            elif discarded:
                # Selfie no login / conta inutilizável: descarte é AUTORITATIVO
                # (settle_fb_account ignoraria o status 'discarded' e poderia
                # devolvê-la ao pool com portfolios<2). Descarta de vez.
                new_status = store.discard_fb_account(cur_email)
                logger.warning(
                    "conta %s: DESCARTADA (selfie/identidade ou inutilizável) → "
                    "fora do pool (status=%s).", cur_email,
                    new_status or "não estava no estoque",
                )
            elif run_100pct:
                # Run 100% concluído: força portfolios=teto para garantir
                # status=used sem depender do contador (que pode ser 1).
                new_status = store.settle_fb_account(
                    cur_email,
                    portfolios=store.FB_ACCOUNT_MAX_PORTFOLIOS,
                    run_failed=False,
                )
                logger.info(
                    "conta %s: run 100%% concluído → CONSUMIDA definitivamente (status=%s).",
                    cur_email, new_status or "não estava no estoque",
                )
            elif wrong_pw:
                # Senha rejeitada: marca como 'wrong_password' (aguardando troca do fornecedor).
                # Não volta ao pool até o operador solicitar troca e readdicioná-la.
                store.mark_fb_account_wrong_password(cur_email)
                logger.warning(
                    "conta %s: senha incorreta → MARCADA PARA TROCA (aguardando fornecedor).",
                    cur_email,
                )
            elif res.error and "captcha" in res.error.lower():
                store.freeze_fb_account(cur_email)
                logger.warning(
                    "conta %s: ciclo captcha indefinido → CONGELADA 3min (volta ao pool depois).",
                    cur_email,
                )
            else:
                error_reason = str(getattr(res, "error", "Erro desconhecido"))[:100]
                fs = getattr(res, "failure_state", "")
                if (
                    fs in ("infrastructure_error", "ui_error", "adspower_cdp")
                    and not getattr(res, "business_id", None)
                ):
                    new_status = store.settle_fb_account(cur_email, run_failed=False)
                    logger.info(
                        "conta %s: erro transitório (%s) sem progresso → volta ao pool (status=%s).",
                        cur_email, fs, new_status or "não estava no estoque",
                    )
                else:
                    store.mark_account_pending_discard(cur_email, reason=error_reason)
                    if pf is not None:
                        logger.info(
                            "conta %s: run incompleto (%d portfólio(s)) → PENDING_DISCARD (motivo: %s).",
                            cur_email, pf, error_reason,
                        )
                    else:
                        logger.info(
                            "conta %s: run incompleto → PENDING_DISCARD (motivo: %s).",
                            cur_email, error_reason,
                        )
        except Exception:  # noqa: BLE001 — accounting é best-effort
            pass

        # CNPJ: SEMPRE é descartado após o run, mesmo que falhe ANTES de criar portfólio.
        # Regra absoluta: nunca reutilizar o mesmo CNPJ. Se res é None (exception antes
        # de last_result), ainda assim descarta o CNPJ especificado na entrada.
        # business_id mora em runner.last_result, setado no início do run → sobrevive
        # à exceção que derruba o pipeline.
        # CNPJ ATUAL do runner — pode ter sido TROCADO em pleno run quando o
        # Meta rejeitou o nome derivado do CNPJ original (ver
        # AutomationRunner._swap_cnpj_for_rejected_name): o antigo já foi
        # explicitamente DESCARTADO lá dentro, então aqui só fazemos settle
        # do CNPJ que realmente terminou o run, igual já é feito pra conta
        # (cur_email acima) — sem isso, settle_cnpj(spec.cnpj_digits, ...)
        # reverteria o descarte do CNPJ antigo de volta a 'available'.
        # Encerrada/assumida: a API já decidiu o destino do CNPJ (descartar no
        # Encerrar; manter no Assumir) — não mexe aqui pra não reverter.
        if (
            getattr(res, "terminated", False)
            or getattr(res, "assumed", False)
            or run_control.is_assume_requested(run_id)
        ):
            logger.info("CNPJ: destino já tratado pela API (Encerrar/Assumir).")
        else:
            try:
                cur_cnpj_digits = (
                    getattr(getattr(runner, "cnpj_data", None), "cnpj", None)
                    or spec.cnpj_digits
                )
                # Se res é None (exception antes de last_result), trata como falha sem portfólio
                created = bool(getattr(res, "business_id", None)) if res else False
                # Detecta se o run falhou: usa run_100pct já calculado acima
                # Se run_100pct=True, tudo foi bem-sucedido. Caso contrário, falhou.
                run_failed = True if res is None else not run_100pct
                error_msg = getattr(res, "error", None) if res else "Exception antes de last_result"
                logger.info(
                    f"[SETTLE_CNPJ_CALL] CNPJ={cur_cnpj_digits}, created={created}, "
                    f"business_id={getattr(res, 'business_id', None) if res else 'N/A'}, "
                    f"logged_in={getattr(res, 'logged_in', None) if res else 'N/A'}, "
                    f"run_failed={run_failed}, run_100pct={run_100pct}"
                )
                cnpj_status = store.settle_cnpj(
                    cur_cnpj_digits,
                    created_portfolio=created,
                    run_failed=run_failed,
                    run_100pct=run_100pct,  # ← Passa flag 100% para descartar permanentemente
                    error_msg=error_msg,
                )
                if cnpj_status:
                    logger.info(
                        "CNPJ %s: %s → status=%s",
                        cur_cnpj_digits,
                        "portfólio criado" if created else "sem portfólio",
                        cnpj_status,
                    )
            except Exception as e:  # noqa: BLE001 — accounting é best-effort
                logger.error(f"[SETTLE_CNPJ] Erro ao descartar CNPJ: {e}")
                pass

            # 2 BMs por perfil: settle também dos CNPJs das BMs ANTERIORES desta
            # mesma sessão (guardados em result.extra_bms). Cada uma que criou
            # portfólio é descartada 100% (regra: CNPJ nunca reutilizado).
            for _bm in getattr(res, "extra_bms", None) or []:
                try:
                    _c = _bm.get("cnpj")
                    if not _c:
                        continue
                    _created = bool(_bm.get("business_id"))
                    _st = store.settle_cnpj(
                        _c,
                        created_portfolio=_created,
                        run_failed=not _bm.get("complete"),
                        run_100pct=bool(_bm.get("complete")),
                        error_msg=None,
                    )
                    logger.info(
                        "2-BMs: CNPJ %s (BM extra) → status=%s (portfólio=%s).",
                        _c, _st, "sim" if _created else "não",
                    )
                except Exception as _e:  # noqa: BLE001 — best-effort
                    logger.error("2-BMs: erro ao settle CNPJ extra: %s", _e)

            # Safety: se a 2ª BM foi reservada (spec.cnpj2) mas NÃO chegou a
            # rodar (prep/auth falhou antes de trocar), o CNPJ2 fica SEGURADO em
            # 'used'. Pedido do operador (2026-09): NÃO devolve ao pool sozinho —
            # só volta pelo botão 'Recuperar Conta + CNPJ' (restore_cnpj), igual
            # ao CNPJ principal e às contas. Antes fazia restore_cnpj automático.
            try:
                _c2 = spec.cnpj2_digits
                if _c2:
                    _handled = {cur_cnpj_digits} | {
                        (b.get("cnpj") or "")
                        for b in (getattr(res, "extra_bms", None) or [])
                    }
                    if _c2 not in _handled:
                        logger.info(
                            "2-BMs: CNPJ2 %s reservado mas não rodou — SEGURADO "
                            "(used); NÃO volta ao pool sozinho, use o botão "
                            "'Recuperar Conta + CNPJ'.", _c2,
                        )
            except Exception as _e2:  # noqa: BLE001
                logger.debug("2-BMs: safety settle do CNPJ2 falhou: %s", _e2)

        # Proxy — 1 proxy por perfil (operador 2026-10):
        #   Proxy fica CONSUMIDO ('used', vinculado ao CNPJ) após o run —
        #   nunca volta ao pool sozinho. Cada perfil gasta 1 proxy.
        if leased:
            logger.info(
                "proxy %s: CONSUMIDO (used) — vinculado ao CNPJ %s, não volta ao pool.",
                leased.get("username", "?"), spec.cnpj_digits,
            )
        try:
            await runner.sms_client.aclose()
        except Exception:  # noqa: BLE001
            pass
        # Limpa o pedido de parada (Encerrar/Assumir) desta run — já tratado.
        try:
            run_control.clear(run_id)
        except Exception:  # noqa: BLE001
            pass


def execute_job(run_id: str, spec: JobSpec) -> AutomationResult:
    """Entry point síncrono chamado pelo JobManager (numa thread de worker)."""
    logger.info("job %s: provisionando CNPJ %s", run_id, spec.cnpj_digits)
    return asyncio.run(_execute_job_async(spec, run_id))


@dataclass
class LoginOnlySpec:
    """Botão "Só logar" (painel, embaixo de Enfileirar e rodar): abre o
    AdsPower, loga na conta e PARA — sem CNPJ, sem SMS, sem site, sem tocar
    no pool de domínio. É o "Assumir" decidido ANTES do run começar, em vez
    de no meio de um erro."""

    meta_email_or_phone: str
    meta_password: str
    meta_totp_seed: str
    meta_email_address: str = ""
    meta_email_password: str = ""
    meta_recovery_email: str = ""
    adspower_api_key: Optional[str] = None
    use_proxy: bool = True
    headless: bool = False
    browser_provider: str = "adspower"


def _synthetic_cnpj_data(email: str) -> CNPJData:
    """CNPJData FALSO — só dá uma chave estável (cache do storage_state, nome
    do perfil AdsPower) pro login-only, que não usa CNPJ de verdade nenhum
    (não gera site, não verifica domínio, não toca no pool de CNPJ). Prefixo
    '9' fixo: nunca colide com um CNPJ real puxado do pool (raiz de CNPJ real
    não usa esse padrão)."""
    digest = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()
    only_digits_hash = "".join(c for c in digest if c.isdigit())
    digits = ("9" + only_digits_hash)[:14].ljust(14, "0")
    return CNPJData(cnpj=digits, razao_social=f"LOGIN {email}".strip())


async def _execute_login_only_async(
    spec: LoginOnlySpec, run_id: Optional[str] = None
) -> AutomationResult:
    """Só login: abre o AdsPower, loga na conta, abre business.facebook.com e
    PARA — sem CNPJ, sem SMS, sem site. O runner entrega pro operador assim
    que a sessão do Facebook está confirmada (mesma guarda do "Assumir": perfil
    e navegador ficam abertos, conta não é descartada)."""
    creds = MetaCredentials(
        email_or_phone=spec.meta_email_or_phone,
        password=spec.meta_password,
        totp_seed=spec.meta_totp_seed,
        email_address=spec.meta_email_address or None,
        email_password=spec.meta_email_password or None,
        recovery_email=spec.meta_recovery_email or None,
    )
    cnpj_data = _synthetic_cnpj_data(spec.meta_email_or_phone)

    # Proxy do pool — NÃO sticky (não existe CNPJ real aqui pra fixar IP);
    # cada clique pega o próximo IP livre. Sem pool, cai no Bright Data do
    # .env (mesmo fallback do fluxo normal); sem nenhum dos dois, roda sem
    # proxy (IP direto da máquina).
    adspower_proxy: Optional[dict[str, object]] = None
    proxy_cfg: Optional[dict[str, str]] = None
    if spec.use_proxy:
        leased = store.lease_proxy("")
        if leased:
            adspower_proxy = {
                "scheme": leased.get("scheme") or "http",
                "host": leased.get("host"),
                "port": leased.get("port"),
                "username": leased.get("username"),
                "password": leased.get("password"),
            }
            proxy_cfg = {
                "server": f"{adspower_proxy['scheme']}://{adspower_proxy['host']}:{adspower_proxy['port']}"
            }
            if adspower_proxy["username"]:
                proxy_cfg["username"] = adspower_proxy["username"]
            if adspower_proxy["password"]:
                proxy_cfg["password"] = adspower_proxy["password"]
        elif PROXY_SERVER and PROXY_USERNAME and PROXY_PASSWORD:
            host_port = PROXY_SERVER.split(":")
            adspower_proxy = {
                "scheme": "http",
                "host": host_port[0],
                "port": int(host_port[1]) if len(host_port) > 1 else 3128,
                "username": PROXY_USERNAME,
                "password": PROXY_PASSWORD,
            }
            proxy_cfg = {
                "server": f"http://{adspower_proxy['host']}:{adspower_proxy['port']}",
                "username": PROXY_USERNAME,
                "password": PROXY_PASSWORD,
            }

    runner = AutomationRunner(
        credentials=creds,
        cnpj_data=cnpj_data,
        business_profile=None,
        headless=spec.headless,
        adspower_mode="create",
        adspower_api_key=spec.adspower_api_key,
        proxy_cfg=proxy_cfg,
        use_proxy=spec.use_proxy,
        adspower_proxy=adspower_proxy,
        login_only=True,
        browser_provider=spec.browser_provider,
    )
    # run_id no runner: usado pela parada cooperativa (Encerrar/Assumir) —
    # mesma convenção do fluxo normal em run_service.execute_job.
    runner.run_id = run_id
    return await runner.run()


def execute_login_only_job(run_id: str, spec: LoginOnlySpec) -> AutomationResult:
    """Entry point síncrono chamado pelo JobManager (numa thread de worker)."""
    logger.info("job %s: login-only (%s)", run_id, spec.meta_email_or_phone)
    return asyncio.run(_execute_login_only_async(spec, run_id))
