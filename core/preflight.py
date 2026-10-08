"""Pre-flight checks (L2) — valida o ambiente ANTES de navegar (PR5).

Falhar em 2 segundos com "proxy não conecta" é infinitamente melhor do
que falhar em 90 segundos com timeout de navegação genérico. Cada check
devolve um ``CheckResult`` com ``critical`` (impede a execução) ou não
(vira warning); o agregador ``run_preflight_checks`` monta o
``PreflightReport`` que o runner usa para abortar cedo via
``PreflightError``.

Checks são funções pequenas com dependências injetáveis (``fetch``)
para teste sem rede.
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from urllib.parse import quote

import httpx

from .errors import PreflightError  # noqa: F401 — re-export conveniente

if TYPE_CHECKING:
    from .automation import AutomationRunner

logger = logging.getLogger("meta_automation.preflight")

# Probe leve no próprio domínio-alvo: confirma egress real até o Facebook
# (DNS + TLS + proxy), não só conectividade genérica.
PROBE_URL = "https://www.facebook.com/robots.txt"
PROBE_TIMEOUT_S = 8.0

Fetch = Callable[[str], Awaitable[int]]


@dataclass
class CheckResult:
    name: str
    ok: bool
    critical: bool
    detail: str
    duration_ms: int = 0

    def line(self) -> str:
        status = "ok" if self.ok else ("FAIL" if self.critical else "warn")
        return (
            f"preflight check={self.name} status={status} "
            f"duration_ms={self.duration_ms} detail=\"{self.detail}\""
        )


@dataclass
class PreflightReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True quando nenhum check CRÍTICO falhou (warnings passam)."""
        return all(r.ok for r in self.results if r.critical)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        broken = [r for r in self.failures if r.critical]
        parts = "; ".join(f"{r.name}: {r.detail}" for r in broken)
        return f"Pre-flight reprovado ({len(broken)} falha(s) crítica(s)): {parts}"


def _timed(result: CheckResult, started: float) -> CheckResult:
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Checks individuais
# ---------------------------------------------------------------------------
def check_credentials(email_or_phone: str, password: str) -> CheckResult:
    started = time.monotonic()
    missing = [
        label
        for label, value in (("email/telefone", email_or_phone), ("senha", password))
        if not (value or "").strip()
    ]
    if missing:
        return _timed(
            CheckResult(
                name="credentials",
                ok=False,
                critical=True,
                detail=f"credenciais ausentes: {', '.join(missing)}",
            ),
            started,
        )
    return _timed(
        CheckResult(name="credentials", ok=True, critical=True, detail="presentes"),
        started,
    )


def check_storage_state(path: Path) -> CheckResult:
    """Cookies salvos: ausente é OK (login completo); corrompido é crítico —
    o ``browser.new_context(storage_state=...)`` quebraria no meio do boot."""
    started = time.monotonic()
    if not path.exists():
        return _timed(
            CheckResult(
                name="storage_state",
                ok=True,
                critical=True,
                detail="ausente — login completo será executado",
            ),
            started,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cookies = payload.get("cookies", [])
        return _timed(
            CheckResult(
                name="storage_state",
                ok=True,
                critical=True,
                detail=f"válido ({len(cookies)} cookies)",
            ),
            started,
        )
    except (json.JSONDecodeError, OSError, AttributeError) as exc:
        return _timed(
            CheckResult(
                name="storage_state",
                ok=False,
                critical=True,
                detail=f"arquivo corrompido ({type(exc).__name__}: {exc}) — "
                f"remova {path} e rode novamente",
            ),
            started,
        )


def _proxy_url(proxy_cfg: dict[str, str]) -> str:
    server = proxy_cfg.get("server", "")
    username = proxy_cfg.get("username")
    password = proxy_cfg.get("password")
    if not username:
        return server
    scheme, _, host = server.partition("://")
    if not host:  # server sem scheme explícito
        scheme, host = "http", scheme
    # RFC 3986: user/pass com @ : / quebrariam o parse do userinfo.
    user_q = quote(username, safe="")
    pass_q = quote(password or "", safe="")
    return f"{scheme}://{user_q}:{pass_q}@{host}"


async def _default_fetch_via_proxy(proxy_cfg: Optional[dict[str, str]]) -> Fetch:
    async def fetch(url: str) -> int:
        kwargs: dict = {"timeout": PROBE_TIMEOUT_S}
        if proxy_cfg:
            kwargs["proxy"] = _proxy_url(proxy_cfg)
            # Proxies como Bright Data podem ter certs self-signed
            kwargs["verify"] = False
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url)
            return response.status_code

    return fetch


async def check_proxy(
    proxy_cfg: Optional[dict[str, str]],
    fetch: Optional[Fetch] = None,
) -> CheckResult:
    """Egress até o Facebook através do proxy configurado (se houver)."""
    started = time.monotonic()
    name = "proxy_egress" if proxy_cfg else "egress"
    if fetch is None:
        fetch = await _default_fetch_via_proxy(proxy_cfg)
    try:
        status = await fetch(PROBE_URL)
    except Exception as exc:  # noqa: BLE001
        return _timed(
            CheckResult(
                name=name,
                ok=False,
                critical=True,
                detail=f"probe {PROBE_URL} falhou: {type(exc).__name__}: {exc}",
            ),
            started,
        )
    if status >= 500:
        return _timed(
            CheckResult(
                name=name, ok=False, critical=True,
                detail=f"probe devolveu HTTP {status}",
            ),
            started,
        )
    return _timed(
        CheckResult(name=name, ok=True, critical=True, detail=f"HTTP {status}"),
        started,
    )


async def check_adspower(
    base_url: str, mode: str, fetch: Optional[Fetch] = None
) -> CheckResult:
    """API local do AdsPower responde? Só roda quando o modo usa AdsPower."""
    started = time.monotonic()
    if mode == "off":
        return _timed(
            CheckResult(
                name="adspower", ok=True, critical=False, detail="modo off — pulado"
            ),
            started,
        )
    # Mesmo endpoint que AdsPowerClient.verify() — a API local não expõe
    # /status; user/list responde HTTP 200 sempre que o serviço está de pé.
    url = f"{base_url.rstrip('/')}/api/v1/user/list?page_size=1"
    if fetch is None:
        fetch = await _default_fetch_via_proxy(None)
    try:
        status = await fetch(url)
        ok = status < 500
        return _timed(
            CheckResult(
                name="adspower",
                ok=ok,
                critical=True,
                detail=f"API local HTTP {status}",
            ),
            started,
        )
    except Exception as exc:  # noqa: BLE001
        return _timed(
            CheckResult(
                name="adspower",
                ok=False,
                critical=True,
                detail=f"API local inacessível em {url}: {type(exc).__name__}",
            ),
            started,
        )


async def check_ixbrowser(
    base_url: str, mode: str, fetch: Optional[Fetch] = None
) -> CheckResult:
    """API local do ixBrowser responde? Só roda quando o provider é ixbrowser."""
    started = time.monotonic()
    if mode == "off":
        return _timed(
            CheckResult(name="ixbrowser", ok=True, critical=False, detail="modo off — pulado"),
            started,
        )
    import httpx  # noqa: PLC0415
    url = f"{base_url.rstrip('/')}/api/v2/group-list"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"page": 1, "limit": 1}, headers={"Content-Type": "application/json"})
            ok = r.status_code < 500
            return _timed(
                CheckResult(name="ixbrowser", ok=ok, critical=True, detail=f"API local HTTP {r.status_code}"),
                started,
            )
    except Exception as exc:  # noqa: BLE001
        return _timed(
            CheckResult(
                name="ixbrowser", ok=False, critical=True,
                detail=f"ixBrowser Local API inacessível ({url}): {type(exc).__name__}. Verifique se o ixBrowser está aberto.",
            ),
            started,
        )


async def check_sms_balance(sms_client, *, needed: bool) -> CheckResult:
    """Saldo do gateway SMS — warning (a etapa 4/4 tem fallback manual)."""
    started = time.monotonic()
    if not getattr(sms_client, "enabled", False):
        return _timed(
            CheckResult(
                name="sms_balance",
                ok=True,
                critical=False,
                detail="API SMS desabilitada — verificação manual",
            ),
            started,
        )
    if not needed:
        return _timed(
            CheckResult(
                name="sms_balance",
                ok=True,
                critical=False,
                detail="sem BusinessProfile — etapa SMS não roda",
            ),
            started,
        )
    try:
        balance = await sms_client.get_balance()
    except Exception as exc:  # noqa: BLE001
        return _timed(
            CheckResult(
                name="sms_balance",
                ok=False,
                critical=False,
                detail=f"getBalance falhou: {type(exc).__name__}: {exc}",
            ),
            started,
        )
    if balance <= 0:
        return _timed(
            CheckResult(
                name="sms_balance",
                ok=False,
                critical=False,
                detail=f"saldo zerado (US$ {balance:.2f}) — etapa SMS cairá no manual",
            ),
            started,
        )
    return _timed(
        CheckResult(
            name="sms_balance", ok=True, critical=False,
            detail=f"US$ {balance:.2f}",
        ),
        started,
    )


# Janela do TOTP = 30s. Acima de ~15s de desvio o código gerado já cai fora
# da janela que o Facebook aceita → 2FA rejeitado em TODA tentativa (o robô
# volta pro login). Re-tentar não resolve: só ajustar o relógio resolve.
_CLOCK_SKEW_WARN_S = 15.0
DateFetch = Callable[[], Awaitable[float]]


# Fontes de horário p/ o check de relógio. NÃO usar asset cacheável de CDN: o
# robots.txt do Facebook vinha com Date ~22h ATRASADO e SEM header Age — o que
# produzia o falso "~79000s fora" (relógio real = +1s). Endpoints dinâmicos/204
# devolvem Date FRESCO. Pegamos a Date MAIS RECENTE entre as fontes: cache só
# deixa a Date mais ANTIGA (nunca mais nova), então a mais recente é a verdade.
_CLOCK_PROBE_URLS = (
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.google.com/generate_204",
    PROBE_URL,  # facebook (fallback — pode vir de cache, será descartado se atrasado)
)


async def _default_fetch_server_epoch() -> float:
    """Epoch do servidor pela Date MAIS RECENTE entre várias fontes (robusto a
    respostas servidas de cache, que só atrasam a Date)."""
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    best: Optional[float] = None
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT_S, follow_redirects=True
    ) as client:
        for url in _CLOCK_PROBE_URLS:
            try:
                resp = await client.get(url, headers=headers)
                dt = resp.headers.get("date")
                if not dt:
                    continue
                epoch = email.utils.parsedate_to_datetime(dt).timestamp()
                epoch += float(resp.headers.get("age", "0") or 0)  # compensa cache c/ Age
            except Exception:  # noqa: BLE001 — fonte falhou, tenta a próxima
                continue
            if best is None or epoch > best:
                best = epoch
    if best is None:
        raise RuntimeError("nenhuma fonte de horário respondeu")
    return best


async def check_clock_skew(
    *, has_totp: bool, fetch_epoch: Optional[DateFetch] = None
) -> CheckResult:
    """Compara o relógio local com o do servidor — desvio grande quebra o TOTP.

    Não-crítico (não bloqueia a run), mas com aviso forte: é a causa nº 1 de
    '2FA rejeitado → volta pro login' em loop. Só roda quando a conta usa
    TOTP (seed nas credenciais ou perfil AdsPower)."""
    started = time.monotonic()
    if not has_totp:
        return _timed(
            CheckResult(name="clock", ok=True, critical=False,
                        detail="conta sem seed TOTP — pulado"),
            started,
        )
    if fetch_epoch is None:
        fetch_epoch = _default_fetch_server_epoch
    try:
        server_epoch = await fetch_epoch()
    except Exception as exc:  # noqa: BLE001
        return _timed(
            CheckResult(name="clock", ok=True, critical=False,
                        detail=f"não medido ({type(exc).__name__})"),
            started,
        )
    skew = abs(time.time() - server_epoch)
    if skew > _CLOCK_SKEW_WARN_S:
        return _timed(
            CheckResult(
                name="clock", ok=False, critical=False,
                detail=(
                    f"relógio do sistema fora por ~{skew:.0f}s — o código 2FA "
                    f"(TOTP) será REJEITADO e o login volta pro início. Ajuste "
                    f"a data/hora do Mac p/ automática (NTP) e rode de novo."
                ),
            ),
            started,
        )
    return _timed(
        CheckResult(name="clock", ok=True, critical=False,
                    detail=f"em sincronia (desvio ~{skew:.0f}s)"),
        started,
    )


async def check_cloudflare(runner: "AutomationRunner") -> CheckResult:
    """Netlify configurado E PRONTO? CRÍTICO — sem hospedagem, o passo 3 não
    publica a metatag, o domínio nunca é verificado e os passos 4/5 TRAVAM. A run
    não deve nem começar (queimaria SMS + login + portfólio à toa).

    Resolve o domínio-base que a run usará (override do runner > primeiro domínio
    do pool com cota livre e NS propagados) e valida AO VIVO: token Netlify
    válido, zona DNS existente e nameservers já propagados. Nome da check mantido
    como 'cloudflare' para compatibilidade com a UI/telemetria existente.
    (Para rodar os passos 1-2 sem isso, desligue com PREFLIGHT_ENABLED=false.)"""
    started = time.monotonic()
    from core import store as _store  # noqa: PLC0415

    # Provedor ativo desta run: se for CLOUDFLARE, valida a zona Cloudflare (a
    # função só checava Netlify e reprovava toda run em Cloudflare — produção
    # 2026-09). Senão, segue o fluxo Netlify (padrão) abaixo.
    provider = (getattr(runner, "hosting_provider", "") or "netlify").strip().lower()
    if provider == "cloudflare":
        from .cloudflare_deployer import validate_cloudflare_domain_ready  # noqa: PLC0415
        try:
            active = _store.get_active_cloudflare_domain()
        except Exception:  # noqa: BLE001
            active = None
        acct = (getattr(runner, "cloudflare_account_id", "") or "").strip()
        tok = (getattr(runner, "cloudflare_api_token", "") or "").strip()
        base_domain = ""
        if active:
            base_domain = active["domain"]
            acct = acct or active["account_id"]
            tok = tok or active["api_token"]
        if not (base_domain and acct and tok):
            return _timed(
                CheckResult(
                    name="cloudflare", ok=False, critical=True,
                    detail=(
                        "Cloudflare NÃO configurado — cadastre um domínio Cloudflare "
                        "no painel (Credenciais → Domínios Cloudflare) e aponte os "
                        "nameservers. (PREFLIGHT_ENABLED=false ignora o gate.)"
                    ),
                ),
                started,
            )
        try:
            res = await validate_cloudflare_domain_ready(base_domain, acct, tok)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "detail": f"falha ao validar ({type(exc).__name__}: {exc})"}
        if not res.get("ok"):
            return _timed(
                CheckResult(
                    name="cloudflare", ok=False, critical=True,
                    detail=(
                        f"Cloudflare NÃO pronto: {res.get('detail')} "
                        "(PREFLIGHT_ENABLED=false ignora o gate.)"
                    ),
                ),
                started,
            )
        return _timed(
            CheckResult(
                name="cloudflare", ok=True, critical=True,
                detail=f"válido (Cloudflare) — zona ativa ({base_domain})",
            ),
            started,
        )

    # --- Netlify (fluxo padrão) ---
    from .netlify_deployer import validate_netlify_ready  # noqa: PLC0415

    # 1) Resolve o domínio + token que o passo 3 efetivamente usaria.
    base_domain = (getattr(runner, "netlify_domain", "") or "").strip().lower().strip(".")
    api_token = (getattr(runner, "netlify_api_token", "") or "").strip()
    src = "override (run_service)"
    if not (base_domain and api_token):
        try:
            active = _store.get_active_netlify_domain()
        except Exception:  # noqa: BLE001 — resolver nunca derruba o preflight
            active = None
        if active:
            base_domain, api_token = active["domain"], active["api_token"]
            src = "pool de domínios"

    if not (base_domain and api_token):
        return _timed(
            CheckResult(
                name="cloudflare", ok=False, critical=True,
                detail=(
                    "Netlify NÃO configurado — domínio não seria verificado e os "
                    "passos 4/5 travariam. Cadastre um domínio + token Netlify no "
                    "painel (Configurações → Domínios) e configure os nameservers "
                    "na Hostinger. (PREFLIGHT_ENABLED=false ignora o gate.)"
                ),
            ),
            started,
        )

    # 2) Validação AO VIVO — token + zona DNS + propagação dos nameservers.
    try:
        res = await validate_netlify_ready(base_domain, api_token)
    except Exception as exc:  # noqa: BLE001 — defensivo; nunca derruba o preflight
        res = {"ok": False, "detail": f"falha ao validar ({type(exc).__name__}: {exc})"}
    if not res.get("ok"):
        return _timed(
            CheckResult(
                name="cloudflare", ok=False, critical=True,
                detail=(
                    f"Netlify NÃO pronto ({src}): {res.get('detail')} "
                    f"(PREFLIGHT_ENABLED=false ignora o gate.)"
                ),
            ),
            started,
        )
    return _timed(
        CheckResult(name="cloudflare", ok=True, critical=True,
                    detail=f"válido via {src} — token ativo, zona DNS ativa, NS propagados ({base_domain})"),
        started,
    )


def check_captcha_solver(solver) -> CheckResult:
    started = time.monotonic()
    if solver is None:
        return _timed(
            CheckResult(
                name="captcha_solver",
                ok=False,
                critical=False,
                detail="2captcha sem API key — captcha exigirá operador",
            ),
            started,
        )
    return _timed(
        CheckResult(
            name="captcha_solver", ok=True, critical=False, detail="configurado"
        ),
        started,
    )


# ---------------------------------------------------------------------------
# Agregador
# ---------------------------------------------------------------------------
async def run_preflight_checks(runner: "AutomationRunner") -> PreflightReport:
    """Roda todos os checks aplicáveis ao runner e loga cada resultado."""
    report = PreflightReport()
    report.results.append(
        check_credentials(
            runner.credentials.email_or_phone, runner.credentials.password
        )
    )
    use_adspower = runner.adspower_mode in {"create", "reuse"}
    # Em modo AdsPower os cookies vivem no perfil — storage_state local só
    # importa no launch direto.
    if not use_adspower:
        report.results.append(check_storage_state(runner.storage_state_path))
    report.results.append(await check_proxy(runner.proxy_cfg))
    if getattr(runner, "browser_provider_name", "adspower") == "ixbrowser":
        report.results.append(
            await check_ixbrowser(runner.adspower.base_url, runner.adspower_mode)
        )
    else:
        report.results.append(
            await check_adspower(runner.adspower.base_url, runner.adspower_mode)
        )
    report.results.append(
        await check_sms_balance(
            runner.sms_client, needed=runner.business_profile is not None
        )
    )
    report.results.append(
        check_captcha_solver(getattr(runner, "captcha_solver", None))
    )
    # Cloudflare é dependência dos passos 3→4→5 — só exige quando a esteira
    # completa roda (há BusinessProfile). Sem isso, a run pararia no passo 3.
    if runner.business_profile is not None:
        report.results.append(await check_cloudflare(runner))
    has_totp = bool(
        (getattr(runner.credentials, "totp_seed", None) or "").strip()
        or getattr(runner, "adspower_user_id", None)
    )
    report.results.append(await check_clock_skew(has_totp=has_totp))

    for result in report.results:
        log = logger.info if result.ok else (
            logger.error if result.critical else logger.warning
        )
        log("%s", result.line())
    return report
