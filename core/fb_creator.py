"""FB-Creator — motor de criação de contas Facebook via IxBrowser + Playwright.

Reutiliza a infra existente:
  - IXBrowserClient (perfis antidetect com fingerprint + proxy)
  - Sms24hClient / Sms24hOrgClient (verificação SMS)
  - CapSolverClient (resolução de CAPTCHA)

Cada "job" = 1 conta: cria perfil → abre browser → cadastra no Facebook →
verifica SMS → warm-up básico → fecha.
"""
from __future__ import annotations

import asyncio
import logging
import random
import string
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from config import (
    CAPSOLVER_API_KEY,
    IXBROWSER_API_URL,
    NUMERO_VIRTUAL_API_KEY,
    SMS_POLL_INTERVAL_S,
    SMS_POLL_TIMEOUT_S,
)
from core.capsolver import CapSolverClient
from core.ixbrowser_client import IXBrowserClient, IXBrowserError

logger = logging.getLogger("fb_creator")

# ---------------------------------------------------------------------------
# ProactorEventLoop dedicado para Playwright no Windows (Python 3.13)
# O SelectorEventLoop padrão do uvicorn não suporta subprocess, que o
# Playwright precisa para spawnar o Node.js driver.
# ---------------------------------------------------------------------------
_pw_proactor_loop: asyncio.AbstractEventLoop | None = None
_pw_proactor_lock = threading.Lock()


if sys.platform == "win32":
    def _get_proactor_loop() -> asyncio.AbstractEventLoop:
        global _pw_proactor_loop
        with _pw_proactor_lock:
            if _pw_proactor_loop is not None and _pw_proactor_loop.is_running():
                return _pw_proactor_loop
            _pw_proactor_loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
            t = threading.Thread(
                target=_pw_proactor_loop.run_forever, daemon=True, name="pw-proactor"
            )
            t.start()
            return _pw_proactor_loop


# ---------------------------------------------------------------------------
# Gerador de identidades aleatórias (nomes brasileiros)
# ---------------------------------------------------------------------------
_FIRST_NAMES_M = [
    "Lucas", "Gabriel", "Rafael", "Matheus", "Pedro", "Bruno", "Felipe",
    "Gustavo", "Leonardo", "André", "Thiago", "Diego", "Marcos", "Vinícius",
    "Daniel", "Henrique", "Eduardo", "Ricardo", "Fernando", "Caio",
    "Rodrigo", "Marcelo", "Alex", "João", "Carlos", "Paulo", "José",
    "Fabio", "Renan", "Igor", "Leandro", "Douglas", "Renato", "Julio",
    "Samuel", "Murilo", "Otávio", "Breno", "Luan", "Davi",
]
_FIRST_NAMES_F = [
    "Ana", "Maria", "Juliana", "Camila", "Fernanda", "Beatriz", "Amanda",
    "Larissa", "Carolina", "Gabriela", "Letícia", "Mariana", "Isabela",
    "Raquel", "Vanessa", "Aline", "Patrícia", "Bianca", "Natália",
    "Débora", "Renata", "Tatiana", "Priscila", "Michele", "Jéssica",
    "Bruna", "Carla", "Daniela", "Vitória", "Luana", "Helena",
    "Rafaela", "Érica", "Sandra", "Clara", "Alice", "Laura",
    "Yasmin", "Sophia", "Giovanna",
]
_LAST_NAMES = [
    "Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira",
    "Alves", "Pereira", "Lima", "Gomes", "Costa", "Ribeiro", "Martins",
    "Carvalho", "Almeida", "Lopes", "Soares", "Fernandes", "Vieira",
    "Barbosa", "Rocha", "Dias", "Nascimento", "Andrade", "Moreira",
    "Nunes", "Marques", "Machado", "Mendes", "Freitas", "Cardoso",
    "Ramos", "Gonçalves", "Teixeira", "Araújo", "Correia", "Pinto",
    "Monteiro", "Batista", "Moura",
]

_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.com", "yahoo.com.br"]


def _strip_accents(text: str) -> str:
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _random_identity(gender: str = "random") -> dict[str, Any]:
    if gender == "random":
        gender = random.choice(["male", "female"])
    names = _FIRST_NAMES_M if gender == "male" else _FIRST_NAMES_F
    first = random.choice(names)
    last = random.choice(_LAST_NAMES)
    year = random.randint(1988, 2004)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    suffix = "".join(random.choices(string.digits, k=random.randint(2, 5)))
    email_user = f"{_strip_accents(first).lower()}.{_strip_accents(last).lower()}{suffix}"
    email = f"{email_user}@{random.choice(_EMAIL_DOMAINS)}"
    password = (
        random.choice(string.ascii_uppercase)
        + "".join(random.choices(string.ascii_lowercase, k=6))
        + "".join(random.choices(string.digits, k=3))
        + random.choice("!@#$%&")
    )
    return {
        "first_name": first,
        "last_name": last,
        "gender": gender,
        "birthday": f"{year:04d}-{month:02d}-{day:02d}",
        "email": email,
        "password": password,
    }


_ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "created_accounts.json"


def _save_created_account(job: "FbCreatorJob") -> None:
    """Persiste conta criada em JSON para não perder os dados."""
    import json as _json
    entry = {
        "job_id": job.job_id,
        "first_name": job.first_name,
        "last_name": job.last_name,
        "email": job.email,
        "password": job.password,
        "phone": job.phone,
        "gender": job.gender,
        "birthday": job.birthday,
        "os_type": job.os_type,
        "browser_type": job.browser_type,
        "profile_id": job.profile_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.created_at)),
    }
    try:
        accounts: list[dict] = []
        if _ACCOUNTS_FILE.exists():
            accounts = _json.loads(_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        accounts.append(entry)
        _ACCOUNTS_FILE.write_text(_json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("[job=%s] Conta salva em %s", job.job_id[:8], _ACCOUNTS_FILE)
    except Exception as exc:
        logger.warning("[job=%s] Falha ao salvar conta: %s", job.job_id[:8], exc)


# ---------------------------------------------------------------------------
# Modelos de dados
# ---------------------------------------------------------------------------
class JobStatus(str, Enum):
    QUEUED = "queued"
    CREATING_PROFILE = "creating_profile"
    OPENING_BROWSER = "opening_browser"
    REGISTERING = "registering"
    WAITING_SMS = "waiting_sms"
    SOLVING_CAPTCHA = "solving_captcha"
    WARMUP = "warmup"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class FbCreatorJob:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.QUEUED
    first_name: str = ""
    last_name: str = ""
    gender: str = ""
    birthday: str = ""
    email: str = ""
    password: str = ""
    phone: str = ""
    sms_activation_id: str = ""
    temp_email: str = ""
    verification_method: str = "sms"
    profile_id: str = ""
    os_type: str = "android"
    browser_type: str = "ixbrowser"
    proxy: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    log: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "gender": self.gender,
            "birthday": self.birthday,
            "email": self.email,
            "phone": self.phone,
            "profile_id": self.profile_id,
            "os_type": self.os_type,
            "error": self.error,
            "log": self.log[-50:],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    def append_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log.append(entry)
        logger.info("[job=%s] %s", self.job_id[:8], msg)


# ---------------------------------------------------------------------------
# Motor de criação
# ---------------------------------------------------------------------------
class FbCreatorEngine:
    """Executa o fluxo de criação de UMA conta Facebook."""

    FB_REG_URL = "https://www.facebook.com/reg/?locale=pt_BR"
    CAPTCHA_SITEKEY = "6LcXARcqAAAAAGn3GMxEz_1DL-T0YSZ2sD-HKQJH"

    def __init__(
        self,
        *,
        ix_client: IXBrowserClient,
        sms_api_key: str = "",
        capsolver_api_key: str = "",
        sms_provider: str = "numero_virtual",
        use_email: bool = False,
        sms_service_name: str = "Facebook",
        sms_service_id: int = 60033,
        sms_target_price: int = 130,
        sms_purchase_api_id: int = 1,
        sms_operator: str = "vivo",
        sms_timeout: int = 240,
    ) -> None:
        self.ix = ix_client
        self.sms_api_key = sms_api_key or NUMERO_VIRTUAL_API_KEY
        self.capsolver_key = capsolver_api_key or CAPSOLVER_API_KEY
        self.sms_provider = sms_provider
        self.use_email = use_email
        self.sms_service_name = sms_service_name
        self.sms_service_id = sms_service_id
        self.sms_target_price = sms_target_price
        self.sms_purchase_api_id = sms_purchase_api_id
        self.sms_operator = sms_operator
        self.sms_timeout = sms_timeout

    async def run_job(self, job: FbCreatorJob) -> FbCreatorJob:
        try:
            await self._execute(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.append_log("Job cancelado.")
        except Exception as exc:
            import traceback
            job.status = JobStatus.FAILED
            err_msg = f"{type(exc).__name__}: {exc}" if str(exc) else f"{type(exc).__name__} (sem mensagem)"
            job.error = err_msg[:500]
            tb = traceback.format_exc()
            job.append_log(f"ERRO: {err_msg}")
            for line in tb.strip().splitlines()[-8:]:
                job.append_log(f"  TB: {line}")
            logger.exception("[job=%s] Falha na criação", job.job_id[:8])
        finally:
            job.completed_at = time.time()
            if job.status == JobStatus.COMPLETED:
                _save_created_account(job)
        return job

    async def _execute(self, job: FbCreatorJob) -> None:
        if job.browser_type == "chromium":
            await self._execute_chromium(job)
        else:
            await self._execute_ixbrowser(job)

    async def _execute_ixbrowser(self, job: FbCreatorJob) -> None:
        # 1) Criar perfil IxBrowser
        job.status = JobStatus.CREATING_PROFILE
        job.append_log(f"Criando perfil IxBrowser (OS={job.os_type})...")
        profile = await self.ix.create_profile(
            name=f"fb-{job.first_name}-{job.job_id[:6]}",
            proxy=job.proxy if job.proxy.get("host") else None,
            os_type=job.os_type,
        )
        job.profile_id = profile["user_id"]
        job.append_log(f"Perfil criado: ID={job.profile_id}")

        # 2) Abrir browser
        job.status = JobStatus.OPENING_BROWSER
        job.append_log("Abrindo browser via IxBrowser...")
        payload = await self.ix.start(job.profile_id)
        ws_endpoint = payload.get("ws_puppeteer") or payload.get("ws") or ""
        if not ws_endpoint:
            raise RuntimeError("IxBrowser não retornou WebSocket endpoint.")
        job.append_log(f"Browser aberto. CDP: {ws_endpoint[:60]}...")

        try:
            if sys.platform == "win32":
                proactor = _get_proactor_loop()
                future = asyncio.run_coroutine_threadsafe(
                    self._playwright_section(job, ws_endpoint), proactor
                )
                await asyncio.wrap_future(future)
            else:
                await self._playwright_section(job, ws_endpoint)
        finally:
            try:
                await self.ix.stop(job.profile_id)
                job.append_log("Browser fechado.")
            except Exception as exc:
                job.append_log(f"Aviso: falha ao fechar browser — {exc}")

    async def _execute_chromium(self, job: FbCreatorJob) -> None:
        """Fluxo usando Chromium nativo via Playwright (sem IxBrowser)."""
        job.status = JobStatus.OPENING_BROWSER
        job.append_log(f"Abrindo Chromium nativo (OS emulado={job.os_type})...")

        if sys.platform == "win32":
            proactor = _get_proactor_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._chromium_playwright_section(job), proactor
            )
            await asyncio.wrap_future(future)
        else:
            await self._chromium_playwright_section(job)

    async def _chromium_playwright_section(self, job: FbCreatorJob) -> None:
        """Seção Playwright com Chromium nativo — mobile ou desktop."""
        import tempfile
        import shutil

        user_data_dir = tempfile.mkdtemp(prefix=f"fb-chromium-{job.job_id[:6]}-")
        async with async_playwright() as pw:
            proxy_arg = None
            if job.proxy.get("host"):
                proxy_arg = {
                    "server": f"http://{job.proxy['host']}:{job.proxy.get('port', 80)}",
                }
                if job.proxy.get("user"):
                    proxy_arg["username"] = job.proxy["user"]
                if job.proxy.get("pass"):
                    proxy_arg["password"] = job.proxy["pass"]

            is_mobile = job.os_type in ("android", "ios")

            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            if is_mobile:
                device = dict(pw.devices.get("Pixel 7") or pw.devices.get("Pixel 5") or {})
                device.pop("default_browser_type", None)
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False,
                    proxy=proxy_arg,
                    args=launch_args,
                    **device,
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                )
            else:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False,
                    proxy=proxy_arg,
                    args=launch_args,
                    viewport={"width": 1366, "height": 768},
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                )

            job.append_log(f"Chromium aberto. Pages: {len(context.pages)}")
            page = context.pages[0] if context.pages else await context.new_page()

            try:
                # IP check
                try:
                    await page.goto("https://httpbin.org/ip", wait_until="domcontentloaded", timeout=15_000)
                    ip_text = await page.inner_text("body", timeout=5_000)
                    job.append_log(f"IP detectado: {ip_text.strip()[:60]}")
                except Exception as exc:
                    job.append_log(f"IP check falhou: {exc}")

                await self._register_on_facebook(job, page)

                if job.status == JobStatus.FAILED:
                    return

                if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                    job.status = JobStatus.WARMUP
                    job.append_log("Executando warm-up básico...")
                    await self._basic_warmup(job, page)

                job.status = JobStatus.COMPLETED
                job.append_log("Conta criada com sucesso!")
            finally:
                try:
                    await context.close()
                    job.append_log("Browser fechado.")
                except Exception as exc:
                    job.append_log(f"Aviso: falha ao fechar browser — {exc}")
                try:
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                except Exception:
                    pass

    async def _playwright_section(self, job: FbCreatorJob, ws_endpoint: str) -> None:
        """Seção Playwright — roda no ProactorEventLoop no Windows."""
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(
                ws_endpoint, timeout=30_000
            )
            job.append_log(f"CDP conectado. Contexts: {len(browser.contexts)}")
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = await browser.new_context()

            job.append_log(f"Context obtido. Pages: {len(context.pages)}")
            page = context.pages[0] if context.pages else await context.new_page()
            job.append_log(f"Page obtida: {page.url}")

            # IP check — verificar proxy
            try:
                await page.goto("https://httpbin.org/ip", wait_until="domcontentloaded", timeout=15_000)
                ip_text = await page.inner_text("body", timeout=5_000)
                job.append_log(f"IP detectado: {ip_text.strip()[:60]}")
            except Exception as exc:
                job.append_log(f"IP check falhou: {exc}")

            # 3) Registrar no Facebook
            await self._register_on_facebook(job, page)

            if job.status == JobStatus.FAILED:
                return

            # 4) Warm-up básico
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                job.status = JobStatus.WARMUP
                job.append_log("Executando warm-up básico...")
                await self._basic_warmup(job, page)

            job.status = JobStatus.COMPLETED
            job.append_log("Conta criada com sucesso!")

    async def _register_on_facebook(self, job: FbCreatorJob, page: Page) -> None:
        job.status = JobStatus.REGISTERING
        job.append_log("Navegando para Facebook registro...")

        await page.goto(self.FB_REG_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        job.append_log(f"Página carregada: {page.url}")
        try:
            title = await page.title()
            job.append_log(f"Título: {title}")
        except Exception:
            pass

        job.append_log("Preenchendo formulário de cadastro...")

        # Mobile: Facebook mostra landing antes do formulário
        for landing_name in (
            "Criar nova conta", "Começar", "Create new account",
            "Get started", "Create Account", "Sign Up",
        ):
            try:
                landing_btn = page.get_by_role("link", name=landing_name).or_(
                    page.get_by_role("button", name=landing_name)
                ).first
                if await landing_btn.is_visible(timeout=2_000):
                    job.append_log(f"Mobile: clicando '{landing_name}'...")
                    await landing_btn.click(timeout=10_000)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    job.append_log(f"Pós-landing URL: {page.url}")
                    break
            except (PlaywrightTimeoutError, PlaywrightError):
                continue

        # Mobile wizard: se a página pede nome em etapas, navegar
        is_mobile_wizard = "m.facebook.com" in page.url

        # Facebook usa IDs dinâmicos; usamos get_by_label (Playwright) e
        # fallback por seletor genérico para cobrir versão mobile e desktop.
        fname = page.get_by_label("Nome", exact=True).or_(
            page.get_by_label("First name", exact=True)
        ).or_(
            page.get_by_label("Primeiro nome", exact=False)
        ).or_(
            page.locator('input[name="firstname"]')
        ).or_(
            page.locator('input[name="first_name"]')
        ).first
        lname = page.get_by_label("Sobrenome", exact=True).or_(
            page.get_by_label("Last name", exact=True)
        ).or_(
            page.locator('input[name="lastname"]')
        ).or_(
            page.locator('input[name="last_name"]')
        ).first

        try:
            await self._human_type_locator(fname, job.first_name)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            job.append_log(f"Campo Nome não encontrado: {type(exc).__name__}")
            try:
                visible_text = await page.inner_text("body", timeout=5_000)
                job.append_log(f"Conteúdo página: {visible_text[:400]}")
            except Exception:
                pass
            # Mobile wizard: tentar achar qualquer input text visível
            if is_mobile_wizard:
                job.append_log("Mobile: tentando input genérico para Nome...")
                try:
                    first_input = page.locator('input[type="text"]:visible').first
                    await self._human_type_locator(first_input, job.first_name)
                    job.append_log("Mobile: Nome preenchido via input genérico")
                except Exception:
                    raise exc
            else:
                raise
        try:
            await self._human_type_locator(lname, job.last_name)
        except (PlaywrightTimeoutError, PlaywrightError):
            if is_mobile_wizard:
                job.append_log("Mobile: Sobrenome pode ser campo separado ou na mesma tela")
                try:
                    second_input = page.locator('input[type="text"]:visible').nth(1)
                    await self._human_type_locator(second_input, job.last_name)
                except Exception:
                    pass

        # Mobile wizard: avançar e preencher Birthday → Gender antes do Contact
        if is_mobile_wizard:
            await self._advance_wizard(page, job)
            try:
                await self._fill_birthday(page, job)
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                job.append_log(f"Birthday no wizard falhou: {type(exc).__name__}, pode estar em outra etapa")
            await self._advance_wizard(page, job)
            ok = await self._fill_gender(page, job)
            if not ok:
                job.append_log("Gênero não encontrado nesta etapa do wizard, continuando...")
            await self._advance_wizard(page, job)

        # Mobile wizard: sempre usar email como contato de cadastro.
        # O número SMS alocado serve apenas para verificação posterior.
        if is_mobile_wizard:
            use_email_mode = True
            contact_value = job.email

            # Se não tem telefone, clicar "Cadastrar-se com o email" para mudar o form
            if use_email_mode:
                job.append_log(f"Wizard: sem telefone, tentando modo email...")
                try:
                    page_text = await page.inner_text("body", timeout=3_000)
                    job.append_log(f"Wizard contato página: {page_text[:200]}")
                except Exception:
                    pass
                email_link_texts = (
                    "Cadastrar-se com o email", "Cadastrar-se com o endereço de email",
                    "Sign up with email", "Cadastre-se com o email",
                    "Usar email", "Use email", "email",
                )
                clicked_email = False
                for link_text in email_link_texts:
                    try:
                        link = page.get_by_role("link", name=link_text).first
                        if await link.is_visible(timeout=2_000):
                            await link.click(timeout=5_000)
                            job.append_log(f"Wizard: clicou link '{link_text}'")
                            await asyncio.sleep(random.uniform(2.0, 3.0))
                            clicked_email = True
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                if not clicked_email:
                    for btn_text in email_link_texts:
                        try:
                            btn = page.get_by_role("button", name=btn_text).first
                            if await btn.is_visible(timeout=2_000):
                                await btn.click(timeout=5_000)
                                job.append_log(f"Wizard: clicou botão '{btn_text}'")
                                await asyncio.sleep(random.uniform(2.0, 3.0))
                                clicked_email = True
                                break
                        except (PlaywrightTimeoutError, PlaywrightError):
                            continue
                if not clicked_email:
                    # Último fallback: procurar qualquer link/botão com "email"
                    try:
                        email_el = page.locator('a:has-text("email"), button:has-text("email")').first
                        if await email_el.is_visible(timeout=2_000):
                            text = await email_el.inner_text(timeout=2_000)
                            await email_el.click(timeout=5_000)
                            job.append_log(f"Wizard: clicou fallback email '{text.strip()[:50]}'")
                            await asyncio.sleep(random.uniform(2.0, 3.0))
                    except (PlaywrightTimeoutError, PlaywrightError):
                        pass

            job.append_log(f"Wizard contato: {contact_value}")

            filled_contact = False

            if use_email_mode:
                # Modo email: procurar input[type="email"] ou input genérico
                for sel in ('input[type="email"]:visible', 'input[name="email"]:visible',
                            'input[autocomplete="email"]:visible'):
                    try:
                        inp = page.locator(sel).first
                        if await inp.is_visible(timeout=3_000):
                            await inp.click(timeout=5_000)
                            await asyncio.sleep(0.3)
                            await inp.fill(contact_value, timeout=5_000)
                            job.append_log("Wizard: email preenchido via input[email]")
                            filled_contact = True
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue

            if not filled_contact:
                # 1) input[type="tel"] — mais confiável no mobile
                try:
                    tel_input = page.locator('input[type="tel"]:visible').first
                    if await tel_input.is_visible(timeout=3_000):
                        await tel_input.click(timeout=5_000)
                        await asyncio.sleep(0.3)
                        await tel_input.fill(contact_value, timeout=5_000)
                        await asyncio.sleep(0.5)
                        job.append_log(f"Wizard: contato via input[tel].fill()")
                        filled_contact = True
                except (PlaywrightTimeoutError, PlaywrightError):
                    pass

            # 2) get_by_label com labels do wizard mobile
            if not filled_contact:
                for lbl in ("Número do celular", "Número de celular", "Celular",
                             "Mobile number", "Phone number", "Telefone"):
                    try:
                        inp = page.get_by_label(lbl, exact=False).first
                        if await inp.is_visible(timeout=2_000):
                            await inp.click(timeout=5_000)
                            await asyncio.sleep(0.3)
                            await inp.fill(contact_value, timeout=5_000)
                            await asyncio.sleep(0.5)
                            job.append_log(f"Wizard: contato via label '{lbl}'")
                            filled_contact = True
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue

            # 3) Qualquer input visível vazio
            if not filled_contact:
                try:
                    inputs = page.locator('input:visible')
                    count = await inputs.count()
                    for i in range(count):
                        inp = inputs.nth(i)
                        inp_type = await inp.get_attribute("type") or "text"
                        if inp_type in ("text", "tel", "email", "number"):
                            val = await inp.input_value(timeout=2_000)
                            if not val:
                                await inp.click(timeout=5_000)
                                await asyncio.sleep(0.3)
                                await inp.fill(contact_value, timeout=5_000)
                                job.append_log(f"Wizard: contato via input genérico #{i}")
                                filled_contact = True
                                break
                except (PlaywrightTimeoutError, PlaywrightError):
                    pass

            if not filled_contact:
                job.append_log("ERRO: não conseguiu preencher contato no wizard")

            # Avançar para próxima etapa (Senha)
            await self._advance_wizard(page, job)
        else:
            pass  # Desktop: segue fluxo abaixo

        # Contato — desktop
        if not is_mobile_wizard:
            if job.verification_method == "email" and job.temp_email:
                contact_value = job.temp_email
                job.append_log(f"Contato (email temp): {contact_value}")
            else:
                contact_value = job.email
                job.append_log(f"Contato: {contact_value}")

            contact = page.get_by_label("Número de celular ou email", exact=False).or_(
                page.get_by_label("celular ou email", exact=False)
            ).or_(
                page.get_by_label("Mobile number or email", exact=False)
            ).or_(
                page.get_by_label("Celular", exact=False)
            ).or_(
                page.get_by_label("Telefone", exact=False)
            ).or_(
                page.get_by_label("Phone", exact=False)
            ).or_(
                page.get_by_label("Email ou telefone", exact=False)
            ).or_(
                page.get_by_placeholder("Número de celular ou email")
            ).or_(
                page.get_by_placeholder("Celular ou email")
            ).or_(
                page.locator('input[name="reg_email__"]')
            ).or_(
                page.locator('input[name="contactpoint"]')
            ).or_(
                page.locator('input[type="tel"]')
            ).first
            await self._human_type_locator(contact, contact_value)

        # Senha
        if is_mobile_wizard:
            job.append_log(f"Wizard pré-senha URL: {page.url}")
            try:
                title = await page.title()
                job.append_log(f"Wizard pré-senha título: {title}")
            except Exception:
                pass
            try:
                body = await page.inner_text("body", timeout=5_000)
                job.append_log(f"Wizard pré-senha conteúdo: {body[:500]}")
            except Exception:
                pass

        if is_mobile_wizard:
            # Mobile: usar fill() + eventos para garantir que React capture
            pwd_filled = False
            pwd_el = page.locator('input[type="password"]:visible').first
            try:
                if await pwd_el.is_visible(timeout=5_000):
                    await pwd_el.click(timeout=5_000)
                    await asyncio.sleep(0.3)
                    await pwd_el.fill(job.password, timeout=5_000)
                    await asyncio.sleep(0.5)
                    job.append_log("Wizard: senha preenchida via fill()")
                    pwd_filled = True
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            if not pwd_filled:
                for lbl in ("Senha", "Password", "Criar uma senha", "Nova senha"):
                    try:
                        inp = page.get_by_label(lbl, exact=False).first
                        if await inp.is_visible(timeout=2_000):
                            await inp.click(timeout=5_000)
                            await asyncio.sleep(0.3)
                            await inp.fill(job.password, timeout=5_000)
                            job.append_log(f"Wizard: senha via label '{lbl}'")
                            pwd_filled = True
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
            if not pwd_filled:
                job.append_log("ERRO: senha não preenchida no wizard")
        else:
            pwd = page.get_by_label("Senha", exact=False).or_(
                page.get_by_label("New password", exact=False)
            ).or_(
                page.get_by_label("Password", exact=False)
            ).or_(
                page.get_by_placeholder("Senha")
            ).or_(
                page.get_by_placeholder("Password")
            ).or_(
                page.locator('input[name="reg_passwd__"]')
            ).or_(
                page.locator('input[name="password"]')
            ).or_(
                page.locator('input[type="password"]')
            ).first
            await self._human_type_locator(pwd, job.password)

        # Desktop: Birthday + Gender na mesma página após Senha
        if not is_mobile_wizard:
            await self._fill_birthday(page, job)
            ok = await self._fill_gender(page, job)
            if not ok:
                job.status = JobStatus.FAILED
                job.error = "Gênero não selecionado"
                return
            await asyncio.sleep(random.uniform(0.5, 1.0))

        # Submit — "Enviar" (desktop), "Próximo"/"Next"/"Cadastre-se" (mobile wizard)
        job.append_log("Enviando formulário...")

        async def _find_action_btn() -> Any:
            """Encontra o botão de ação principal da página."""
            for name in ("Enviar", "Submit", "Próximo", "Next", "Avançar", "Continuar", "Continue", "Cadastre-se", "Cadastrar-se", "Sign Up"):
                btn = page.get_by_role("button", name=name).first
                try:
                    if await btn.is_visible(timeout=1_500):
                        return btn
                except (PlaywrightTimeoutError, PlaywrightError):
                    continue
            return page.locator('button[type="submit"]').or_(
                page.locator('form button')
            ).last

        async def _try_submit(attempt_label: str) -> bool:
            """Click submit and check if the form changed. Returns True if form submitted."""
            try:
                btn = await _find_action_btn()
                await btn.scroll_into_view_if_needed(timeout=5_000)
                await asyncio.sleep(0.3)
                await btn.click(timeout=10_000)
            except (PlaywrightTimeoutError, PlaywrightError):
                try:
                    alt = page.locator('form button').last
                    await alt.click(timeout=10_000)
                except (PlaywrightTimeoutError, PlaywrightError):
                    job.append_log(f"{attempt_label}: nenhum botão encontrado")
                    return False
            await asyncio.sleep(random.uniform(7.0, 10.0))
            try:
                for name in ("Enviar", "Submit", "Avançar", "Próximo", "Cadastre-se", "Cadastrar-se", "Sign Up"):
                    try:
                        still = await page.get_by_role("button", name=name).is_visible(timeout=1_500)
                        if still:
                            # Check for validation errors
                            try:
                                err = await page.locator('[role="alert"], [class*="error" i], [class*="Error"]').first.inner_text(timeout=2_000)
                                if err.strip():
                                    job.append_log(f"Erro de validação: {err.strip()[:150]}")
                            except (PlaywrightTimeoutError, PlaywrightError):
                                pass
                            return False
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                return True
            except (PlaywrightTimeoutError, PlaywrightError):
                return True

        form_submitted = await _try_submit("Submit 1")
        if not form_submitted:
            job.append_log("Botão ainda visível após clique. Tentando JS submit...")
            try:
                await page.evaluate("var f=document.querySelector('form'); if(f) f.submit();")
            except Exception:
                pass
            await asyncio.sleep(random.uniform(6.0, 9.0))
            try:
                btn = await _find_action_btn()
                still = await btn.is_visible(timeout=3_000)
                form_submitted = not still
            except (PlaywrightTimeoutError, PlaywrightError):
                form_submitted = True

        if not form_submitted:
            job.append_log("JS submit falhou. Tentando force click + Enter...")
            try:
                btn = await _find_action_btn()
                await btn.click(force=True, timeout=5_000)
            except (PlaywrightTimeoutError, PlaywrightError):
                await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(7.0, 10.0))
            try:
                btn = await _find_action_btn()
                still = await btn.is_visible(timeout=3_000)
                form_submitted = not still
            except (PlaywrightTimeoutError, PlaywrightError):
                form_submitted = True

        if not form_submitted:
            job.append_log("AVISO: Botão de ação ainda visível — prosseguindo para verificar estado.")

        job.append_log(f"Pós-submit URL: {page.url}")
        try:
            title = await page.title()
            job.append_log(f"Pós-submit título: {title}")
        except Exception:
            pass

        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1.0)

        try:
            body_text = await page.inner_text("body", timeout=5_000)
        except Exception:
            body_text = ""

        # Debug: listar todos os inputs na página pós-submit
        try:
            inputs_info = await page.evaluate("""() => {
                const inputs = document.querySelectorAll('input');
                return Array.from(inputs).map((el, i) => ({
                    idx: i,
                    type: el.type,
                    name: el.name,
                    id: el.id,
                    placeholder: el.placeholder,
                    ariaLabel: el.getAttribute('aria-label'),
                    visible: el.offsetParent !== null,
                    value: el.type === 'password' ? '***' : el.value.substring(0, 30)
                }));
            }""")
            job.append_log(f"Inputs pós-submit ({len(inputs_info)}):")
            for inp in inputs_info:
                if inp.get("visible"):
                    job.append_log(f"  [{inp['idx']}] type={inp['type']} name={inp['name']} "
                                   f"placeholder={inp['placeholder']} aria={inp['ariaLabel']} "
                                   f"val={inp['value']}")
        except Exception as exc:
            job.append_log(f"Debug inputs falhou: {exc}")

        try:
            ss_path = Path(f"debug_screenshots/fb_post_submit_{job.job_id}.png")
            ss_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(ss_path), full_page=True)
            job.append_log(f"Screenshot salvo: {ss_path}")
        except Exception as exc:
            job.append_log(f"Screenshot falhou: {exc}")

        # Checar se ainda está na página de registro (= formulário rejeitado)
        current_url = page.url
        if "/reg" in current_url and "checkpoint" not in current_url:
            error_el = page.locator('[class*="error"], [class*="Error"], [role="alert"]').first
            error_text = ""
            try:
                error_text = await error_el.inner_text(timeout=3_000)
                error_text = error_text.strip()[:200]
            except (PlaywrightTimeoutError, PlaywrightError):
                pass

            registration_errors = [
                "an error occurred during",
                "ocorreu um erro durante",
                "is invalid",
                "é inválido",
                "couldn't process your registration",
                "não foi possível processar",
            ]
            found_error = None
            for err_msg in registration_errors:
                if err_msg.lower() in body_text.lower() or err_msg.lower() in error_text.lower():
                    found_error = err_msg
                    break

            if found_error or error_text:
                err_detail = error_text or found_error
                job.append_log(f"AVISO Facebook: {err_detail}")
                job.append_log(f"Tentando re-submit em 5s...")
                await asyncio.sleep(random.uniform(4.0, 6.0))
                try:
                    retry_btn = await _find_action_btn()
                    await retry_btn.click(timeout=10_000)
                except (PlaywrightTimeoutError, PlaywrightError):
                    alt = page.locator('form button').last
                    await alt.click(timeout=10_000)
                await asyncio.sleep(random.uniform(5.0, 8.0))

                try:
                    body_text2 = await page.inner_text("body", timeout=5_000)
                except Exception:
                    body_text2 = ""
                current_url2 = page.url

                still_error = False
                if "/reg" in current_url2 and "checkpoint" not in current_url2:
                    for err_msg in registration_errors:
                        if err_msg.lower() in body_text2.lower():
                            still_error = True
                            break

                if still_error:
                    job.append_log(f"ERRO Facebook (retry falhou): {err_detail}")
                    job.append_log(f"Conteúdo: {body_text2[:400]}")
                    try:
                        ss2 = Path(f"debug_screenshots/fb_retry_fail_{job.job_id}.png")
                        await page.screenshot(path=str(ss2))
                        job.append_log(f"Screenshot retry: {ss2}")
                    except Exception:
                        pass
                    job.status = JobStatus.FAILED
                    job.error = f"Facebook rejeitou (após retry): {err_detail}"
                    return
                else:
                    job.append_log(f"Retry bem-sucedido! URL: {current_url2}")
                    body_text = body_text2

        # Checar se tem CAPTCHA
        captcha_frame = page.frame_locator('iframe[src*="recaptcha"]').first
        try:
            await captcha_frame.locator("body").wait_for(timeout=5_000)
            has_captcha = True
        except (PlaywrightTimeoutError, PlaywrightError):
            has_captcha = False

        if has_captcha and self.capsolver_key:
            job.status = JobStatus.SOLVING_CAPTCHA
            job.append_log("CAPTCHA detectado — resolvendo via CapSolver...")
            await self._solve_captcha(job, page)

        # Verificação SMS — página pede código após submit
        sms_input = page.locator(
            'input[name="code"], input[name="confirmation_code"], '
            'input[aria-label*="code"], input[aria-label*="Code"], '
            'input[aria-label*="código"], input[aria-label*="confirmação"]'
        ).or_(
            page.get_by_label("Confirmation code", exact=False)
        ).or_(
            page.get_by_label("Código de confirmação", exact=False)
        ).or_(
            page.get_by_placeholder("Código de confirmação")
        ).or_(
            page.get_by_placeholder("Confirmation code")
        ).first
        has_sms_step = False
        try:
            await sms_input.wait_for(timeout=15_000)
            has_sms_step = True
            job.append_log("Campo SMS detectado na página.")
        except (PlaywrightTimeoutError, PlaywrightError):
            try:
                visible_text = await page.inner_text("body", timeout=5_000)
                confirm_phrases = [
                    "confirmation code", "código de confirmação",
                    "insira o código", "digite o código", "enter the code",
                    "enviamos um código", "we sent a code", "we sent you a code",
                    "código que enviamos", "code we sent",
                ]
                if any(p in visible_text.lower() for p in confirm_phrases):
                    job.append_log("Página de confirmação detectada pelo texto — buscando input...")
                    all_inputs = page.locator('input[type="text"], input[type="tel"], input[type="number"], input:not([type])')
                    count = await all_inputs.count()
                    job.append_log(f"Inputs encontrados: {count}")
                    for i in range(count):
                        inp = all_inputs.nth(i)
                        try:
                            if await inp.is_visible(timeout=2_000):
                                sms_input = inp
                                has_sms_step = True
                                job.append_log(f"Usando input #{i} para código SMS")
                                break
                        except Exception:
                            continue
                if not has_sms_step:
                    job.append_log("Campo SMS NÃO detectado — capturando conteúdo da página...")
                    job.append_log(f"Conteúdo página: {visible_text[:300]}")
            except Exception:
                job.append_log("Campo SMS NÃO detectado.")

        if has_sms_step:
            job.status = JobStatus.WAITING_SMS
            if job.verification_method == "email":
                job.append_log("Aguardando código por EMAIL (TigrMail)...")
                code = await self._get_email_code(job)
            else:
                job.append_log("Aguardando código SMS...")
                code = await self._get_sms_code(job)
            if code:
                await self._human_type_locator(sms_input, code)
                confirm_btn = page.get_by_role("button", name="Continuar").or_(
                    page.get_by_role("button", name="Confirmar")
                ).or_(
                    page.get_by_role("button", name="Continue")
                ).or_(
                    page.locator('button[type="submit"]')
                ).first
                await confirm_btn.click()
                await asyncio.sleep(random.uniform(3.0, 5.0))
                job.append_log(f"Código verificação inserido: {code}")
            else:
                method = "Email" if job.verification_method == "email" else "SMS"
                job.append_log(f"ERRO: Código {method} não recebido após 5 min.")
                job.status = JobStatus.FAILED
                job.error = f"{method} não recebido"
                return
        else:
            current_url = page.url
            if "/reg" in current_url and "checkpoint" not in current_url:
                enviar_visible = False
                try:
                    for btn_name in ("Enviar", "Submit", "Cadastre-se", "Sign Up"):
                        enviar_visible = await page.get_by_role("button", name=btn_name).is_visible(timeout=1_000)
                        if enviar_visible:
                            break
                except (PlaywrightTimeoutError, PlaywrightError):
                    pass
                if enviar_visible:
                    job.append_log("ERRO: Sem campo de verificação e formulário ainda visível.")
                    job.status = JobStatus.FAILED
                    job.error = "Formulário não submetido ou rejeitado silenciosamente"
                    try:
                        ss = Path(f"debug_screenshots/fb_no_verify_{job.job_id}.png")
                        ss.parent.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(path=str(ss), full_page=True)
                    except Exception:
                        pass
                    return
                job.append_log("Nenhum campo de verificação detectado, mas formulário parece ter sido enviado.")
            else:
                job.append_log(f"Página redirecionou para: {current_url}")

        job.append_log("Registro no Facebook concluído.")

        # Tratar diálogo "Salvar suas informações de login?"
        await self._handle_post_registration_dialogs(page, job)

    async def _handle_post_registration_dialogs(self, page: Page, job: FbCreatorJob) -> None:
        """Trata diálogos pós-registro: 'Salvar login', 'Ativar notificações', etc."""
        for attempt in range(3):
            await asyncio.sleep(random.uniform(1.5, 3.0))
            try:
                body = await page.inner_text("body", timeout=5_000)
            except Exception:
                body = ""

            # "Salvar suas informações de login?" -> clicar "Agora não"
            if "salvar" in body.lower() and "informações de login" in body.lower():
                for btn_text in ("Agora não", "Not Now", "Ahora no"):
                    try:
                        btn = page.get_by_role("button", name=btn_text).first
                        if await btn.is_visible(timeout=3_000):
                            await btn.click(timeout=5_000)
                            job.append_log(f"Pós-registro: clicou '{btn_text}' (salvar login)")
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                continue

            # "Ativar notificações" -> clicar "Agora não" / "Pular"
            if "notificaç" in body.lower() or "notifications" in body.lower():
                for btn_text in ("Agora não", "Pular", "Skip", "Not Now"):
                    try:
                        btn = page.get_by_role("button", name=btn_text).first
                        if await btn.is_visible(timeout=3_000):
                            await btn.click(timeout=5_000)
                            job.append_log(f"Pós-registro: clicou '{btn_text}' (notificações)")
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                continue

            # "Adicionar foto" / "Add photo" -> Pular
            if "foto" in body.lower() or "photo" in body.lower():
                for btn_text in ("Pular", "Skip", "Agora não", "Not Now"):
                    try:
                        btn = page.get_by_role("button", name=btn_text).first
                        if await btn.is_visible(timeout=2_000):
                            await btn.click(timeout=5_000)
                            job.append_log(f"Pós-registro: clicou '{btn_text}' (foto)")
                            await asyncio.sleep(random.uniform(1.5, 3.0))
                            break
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                continue

            # Se chegou no feed ou perfil, parar
            cur = page.url
            if "/home" in cur or "/feed" in cur or "/?sk=" in cur:
                job.append_log("Pós-registro: chegou ao feed.")
                break

            # Nenhum diálogo reconhecido
            break

    async def _solve_captcha(self, job: FbCreatorJob, page: Page) -> None:
        solver = CapSolverClient(api_key=self.capsolver_key)
        try:
            token = await solver.solve_recaptcha_v2(
                sitekey=self.CAPTCHA_SITEKEY,
                page_url=page.url,
            )
            await page.evaluate(
                """(token) => {
                    document.getElementById('g-recaptcha-response').value = token;
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        const keys = Object.keys(___grecaptcha_cfg.clients);
                        if (keys.length) {
                            const cid = keys[0];
                            const cb = ___grecaptcha_cfg.clients[cid]?.S?.S?.callback;
                            if (cb) cb(token);
                        }
                    }
                }""",
                token,
            )
            job.append_log("CAPTCHA resolvido com sucesso.")
            await asyncio.sleep(random.uniform(2.0, 4.0))
        except Exception as exc:
            job.append_log(f"Falha no CAPTCHA: {exc}")

    async def _get_sms_code(self, job: FbCreatorJob) -> Optional[str]:
        if not job.sms_activation_id:
            job.append_log("Sem activation_id SMS — pulando verificação.")
            return None

        if self.sms_provider == "sms24h_org":
            return await self._get_sms_code_sms24h(job)

        from core.api_clients import Sms24hClient  # noqa: PLC0415

        async with Sms24hClient(
            api_key=self.sms_api_key,
            service_id=self.sms_service_id,
            service_name=self.sms_service_name,
        ) as sms:
            try:
                code = await sms.get_sms_code(
                    job.sms_activation_id,
                    poll_timeout_s=self.sms_timeout,
                    poll_interval_s=SMS_POLL_INTERVAL_S,
                )
                return code
            except Exception as exc:
                job.append_log(f"Falha ao obter código SMS: {exc}")
                return None

    async def _get_sms_code_sms24h(self, job: FbCreatorJob) -> Optional[str]:
        from core.api_clients import Sms24hOrgClient  # noqa: PLC0415

        async with Sms24hOrgClient(
            api_key=self.sms_api_key,
            service="fb",
        ) as sms:
            try:
                await sms.set_status(job.sms_activation_id, 1)
                job.append_log("sms24h.org: setStatus(1) — SMS enviado, aguardando código...")
                code = await sms.get_sms_code(
                    job.sms_activation_id,
                    poll_timeout_s=self.sms_timeout,
                    poll_interval_s=SMS_POLL_INTERVAL_S,
                )
                return code
            except Exception as exc:
                job.append_log(f"Falha SMS sms24h.org: {exc}")
                return None

    async def _get_email_code(self, job: FbCreatorJob) -> Optional[str]:
        from core.api_clients import TigrMailClient  # noqa: PLC0415

        if not job.temp_email:
            job.append_log("Sem email temporário configurado.")
            return None

        async with TigrMailClient() as mail:
            mail.inbox = job.temp_email
            job.append_log(f"tigrmail: polling código de {mail.inbox}...")
            code = await mail.wait_for_code(
                max_attempts=3,
                from_domain="facebookmail.com",
            )
            return code

    async def _acquire_temp_email(self, job: FbCreatorJob) -> str:
        from core.api_clients import TigrMailClient  # noqa: PLC0415

        async with TigrMailClient() as mail:
            address = await mail.create_inbox()
            job.temp_email = address
            return address

    async def _acquire_sms_number(self, job: FbCreatorJob) -> tuple[str, str]:
        if self.sms_provider == "sms24h_org":
            return await self._acquire_sms_number_sms24h(job)

        from core.api_clients import Sms24hClient  # noqa: PLC0415

        job.append_log(
            f"SMS config: service={self.sms_service_name} id={self.sms_service_id} "
            f"price={self.sms_target_price} api_id={self.sms_purchase_api_id} op={self.sms_operator}"
        )
        async with Sms24hClient(
            api_key=self.sms_api_key,
            service_id=self.sms_service_id,
            service_name=self.sms_service_name,
            operator=self.sms_operator or None,
            purchase_api_id=self.sms_purchase_api_id,
            target_price=self.sms_target_price,
        ) as sms:
            activation_id, phone = await sms.acquire_number()
            return activation_id, phone

    async def _acquire_sms_number_sms24h(self, job: FbCreatorJob) -> tuple[str, str]:
        from core.api_clients import Sms24hOrgClient  # noqa: PLC0415

        async with Sms24hOrgClient(
            api_key=self.sms_api_key,
            service="fb",
            country="73",
        ) as sms:
            activation_id, phone = await sms.acquire_number()
            job.append_log(f"SMS via sms24h.org — id={activation_id}")
            return activation_id, phone

    async def _basic_warmup(self, job: FbCreatorJob, page: Page) -> None:
        try:
            await page.goto("https://m.facebook.com/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Scroll pelo feed
            for i in range(3):
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(random.uniform(1.5, 3.0))

            job.append_log("Warm-up: navegação no feed concluída.")

            # Visitar perfil
            await page.goto("https://m.facebook.com/me", wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(random.uniform(2.0, 3.0))
            job.append_log("Warm-up: perfil visitado.")

        except Exception as exc:
            job.append_log(f"Warm-up parcial: {exc}")

    @staticmethod
    async def _advance_wizard(page: Page, job: "FbCreatorJob") -> bool:
        """Click Próximo/Next to advance the mobile wizard step."""
        for name in ("Próximo", "Next", "Avançar", "Continue", "Continuar"):
            try:
                btn = page.get_by_role("button", name=name).first
                if await btn.is_visible(timeout=3_000):
                    job.append_log(f"Wizard: clicando '{name}'...")
                    await btn.click(timeout=10_000)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    return True
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        try:
            btn = page.locator('button[type="submit"]:visible').first
            if await btn.is_visible(timeout=2_000):
                text = (await btn.inner_text(timeout=2_000)).strip()
                job.append_log(f"Wizard: clicando submit '{text}'...")
                await btn.click(timeout=10_000)
                await asyncio.sleep(random.uniform(2.0, 4.0))
                return True
        except (PlaywrightTimeoutError, PlaywrightError):
            pass
        job.append_log("Wizard: botão próximo não encontrado")
        return False

    async def _fill_birthday(self, page: Page, job: "FbCreatorJob") -> None:
        """Fill birthday — input[type=date] (mobile) ou selects (desktop) ou idade."""
        job.append_log("Preenchendo data de nascimento...")
        parts = job.birthday.split("-")
        if len(parts) != 3:
            job.append_log("ERRO: formato de birthday inválido")
            return
        year, month, day = parts

        # Variante: Facebook pede idade ao invés de data de nascimento
        try:
            page_text = await page.inner_text("body", timeout=3_000)
        except Exception:
            page_text = ""
        if "sua idade" in page_text.lower() or "your age" in page_text.lower():
            from datetime import date as _date
            birth = _date(int(year), int(month), int(day))
            today = _date.today()
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            age_input = page.locator('input[type="text"]:visible, input[type="number"]:visible, input[type="tel"]:visible').first
            try:
                await age_input.fill(str(age), timeout=3_000)
                await asyncio.sleep(0.5)
                job.append_log(f"Birthday preenchido via idade: {age} anos")
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                job.append_log(f"Idade input falhou: {exc}")

        # Mobile: input[type="date"] — preencher via JS para evitar date picker
        date_input = page.locator('input[type="date"]:visible').first
        try:
            if await date_input.is_visible(timeout=3_000):
                iso_date = f"{year}-{month}-{day}"
                await page.evaluate(
                    """(args) => {
                        const inp = document.querySelector('input[type="date"]');
                        if (inp) {
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            nativeSetter.call(inp, args.date);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }""",
                    {"date": iso_date},
                )
                await asyncio.sleep(0.5)
                job.append_log(f"Birthday preenchido via input[date]: {iso_date}")
                return
        except (PlaywrightTimeoutError, PlaywrightError):
            pass

        try:
            selects_count = await page.locator('select').count()
            job.append_log(f"Selects na página: {selects_count}")
        except Exception:
            pass

        # Desktop: selects/comboboxes para dia, mês, ano
        _MONTHS_PT = {
            "01": "janeiro", "02": "fevereiro", "03": "março", "04": "abril",
            "05": "maio", "06": "junho", "07": "julho", "08": "agosto",
            "09": "setembro", "10": "outubro", "11": "novembro", "12": "dezembro",
        }

        day_box = page.get_by_role("combobox", name="Selecionar o dia").or_(
            page.get_by_label("dia", exact=False)
        ).or_(
            page.locator('select[name="birthday_day"]')
        ).or_(
            page.locator('select#day')
        ).or_(
            page.get_by_role("combobox", name="Day")
        ).first
        try:
            await day_box.select_option(day.lstrip("0"), timeout=2_000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            job.append_log(f"select_option dia falhou: {type(exc).__name__}, tentando click...")
            try:
                await day_box.click(timeout=5_000)
                await asyncio.sleep(0.3)
                opt = page.get_by_role("option", name=day.lstrip("0")).first
                await opt.click()
            except (PlaywrightTimeoutError, PlaywrightError):
                job.append_log("ERRO dia: capturando selects da página...")
                try:
                    selects = await page.locator('select').all()
                    for i, s in enumerate(selects):
                        s_name = await s.get_attribute("name") or ""
                        s_id = await s.get_attribute("id") or ""
                        job.append_log(f"  select[{i}] name={s_name} id={s_id}")
                except Exception:
                    pass
                raise
        await asyncio.sleep(random.uniform(0.3, 0.6))

        _MONTHS_EN = {
            "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
            "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
            "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
        }
        month_box = page.get_by_role("combobox", name="Selecionar o mês").or_(
            page.get_by_label("mês", exact=False)
        ).or_(
            page.locator('select[name="birthday_month"]')
        ).or_(
            page.locator('select#month')
        ).or_(
            page.get_by_role("combobox", name="Month")
        ).first
        month_name = _MONTHS_PT.get(month, month.lstrip("0"))
        month_name_en = _MONTHS_EN.get(month, month.lstrip("0"))
        try:
            await month_box.select_option(month.lstrip("0"), timeout=2_000)
        except (PlaywrightTimeoutError, PlaywrightError):
            try:
                await month_box.select_option(label=month_name, timeout=2_000)
            except (PlaywrightTimeoutError, PlaywrightError):
                try:
                    await month_box.select_option(label=month_name_en, timeout=2_000)
                except (PlaywrightTimeoutError, PlaywrightError):
                    await month_box.click()
                    await asyncio.sleep(0.3)
                    opt = page.get_by_role("option", name=month_name).or_(
                        page.get_by_role("option", name=month_name_en)
                    ).first
                    await opt.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))

        year_box = page.get_by_role("combobox", name="Selecionar o ano").or_(
            page.get_by_label("ano", exact=False)
        ).or_(
            page.locator('select[name="birthday_year"]')
        ).or_(
            page.locator('select#year')
        ).or_(
            page.get_by_role("combobox", name="Year")
        ).first
        try:
            await year_box.select_option(year, timeout=2_000)
        except (PlaywrightTimeoutError, PlaywrightError):
            await year_box.click()
            await asyncio.sleep(0.3)
            opt = page.get_by_role("option", name=year).first
            await opt.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))

    async def _fill_gender(self, page: Page, job: "FbCreatorJob") -> bool:
        """Fill gender field. Returns True if successful."""
        gender_label_pt = "Masculino" if job.gender == "male" else "Feminino"
        gender_label_en = "Male" if job.gender == "male" else "Female"
        gender_val = "2" if job.gender == "male" else "1"
        gender_selected = False

        native_select = page.locator('select[name="sex"]').or_(
            page.locator('select#gender')
        ).first
        try:
            await native_select.select_option(value=gender_val, timeout=3_000)
            gender_selected = True
            job.append_log(f"Gênero selecionado via <select>: {gender_label_en}")
        except (PlaywrightTimeoutError, PlaywrightError):
            pass

        if not gender_selected:
            gender_combo = page.get_by_role("combobox").filter(has_text="Selecione seu gênero").or_(
                page.get_by_role("combobox").filter(has_text="Select your gender")
            ).first
            try:
                await gender_combo.click(timeout=4_000)
                await asyncio.sleep(0.5)
                opt = page.get_by_role("option", name=gender_label_en).or_(
                    page.get_by_role("option", name=gender_label_pt)
                ).or_(
                    page.locator(f'option[value="{gender_val}"]')
                ).first
                await opt.click(timeout=3_000)
                gender_selected = True
                job.append_log(f"Gênero selecionado via combobox: {gender_label_en}")
            except (PlaywrightTimeoutError, PlaywrightError):
                pass

        if not gender_selected:
            try:
                gender_combo = page.get_by_role("combobox").filter(has_text="Select your gender").or_(
                    page.get_by_role("combobox").filter(has_text="Selecione seu gênero")
                ).first
                await gender_combo.click(timeout=3_000)
                await asyncio.sleep(0.5)
                listbox_opt = page.get_by_role("listbox").get_by_text(gender_label_en, exact=True).or_(
                    page.get_by_role("listbox").get_by_text(gender_label_pt, exact=True)
                ).first
                await listbox_opt.click(timeout=3_000)
                gender_selected = True
                job.append_log(f"Gênero selecionado via listbox: {gender_label_en}")
            except (PlaywrightTimeoutError, PlaywrightError):
                pass

        if not gender_selected:
            for label in [gender_label_en, gender_label_pt]:
                try:
                    await page.get_by_text(label, exact=True).first.click(timeout=2_000)
                    gender_selected = True
                    job.append_log(f"Gênero selecionado via texto: {label}")
                    break
                except (PlaywrightTimeoutError, PlaywrightError):
                    pass

        if not gender_selected:
            radio = page.locator(f'input[name="sex"][value="{gender_val}"]').first
            try:
                await radio.click(timeout=3_000)
                gender_selected = True
                job.append_log(f"Gênero selecionado via radio: {gender_val}")
            except (PlaywrightTimeoutError, PlaywrightError):
                pass

        if not gender_selected:
            job.append_log(f"ERRO: Não conseguiu selecionar gênero ({gender_label_en}/{gender_label_pt})")
        return gender_selected

    @staticmethod
    async def _human_type(page: Page, selector: str, text: str) -> None:
        el = page.locator(selector).first
        await el.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        for ch in text:
            await el.type(ch, delay=random.uniform(30, 120))
        await asyncio.sleep(random.uniform(0.3, 0.7))

    @staticmethod
    async def _human_type_locator(locator: Any, text: str) -> None:
        await locator.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        for ch in text:
            await locator.type(ch, delay=random.uniform(30, 120))
        await asyncio.sleep(random.uniform(0.3, 0.7))


# ---------------------------------------------------------------------------
# Gerenciador de batch (controla N jobs em paralelo)
# ---------------------------------------------------------------------------
class FbCreatorManager:
    """Orquestra N criações simultâneas com controle de concorrência."""

    def __init__(self) -> None:
        self.jobs: dict[str, FbCreatorJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._engine: Optional[FbCreatorEngine] = None

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict[str, Any]:
        total = len(self.jobs)
        completed = sum(1 for j in self.jobs.values() if j.status == JobStatus.COMPLETED)
        failed = sum(1 for j in self.jobs.values() if j.status == JobStatus.FAILED)
        running = sum(
            1 for j in self.jobs.values()
            if j.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED)
        )
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "running": running,
            "queued": total - completed - failed - running,
            "is_running": self._running,
        }

    def get_jobs(self) -> list[dict[str, Any]]:
        return [j.to_dict() for j in self.jobs.values()]

    def get_job_logs(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return job.log if job else []

    async def start_batch(
        self,
        *,
        count: int = 1,
        max_concurrent: int = 1,
        os_type: str = "android",
        gender: str = "random",
        proxy_mode: str = "none",
        proxies: list[dict[str, Any]] | None = None,
        sms_api_key: str = "",
        capsolver_api_key: str = "",
        use_sms: bool = True,
        use_email: bool = False,
        sms_provider: str = "numero_virtual",
        sms_service_name: str = "Facebook",
        sms_service_id: int = 60033,
        sms_target_price: int = 130,
        sms_purchase_api_id: int = 1,
        sms_operator: str = "vivo",
        sms_timeout: int = 240,
        browser_type: str = "ixbrowser",
        custom_emails: list[str] | None = None,
    ) -> dict[str, Any]:
        if self._running:
            return {"error": "Batch já em execução."}

        self._running = True
        self.jobs.clear()
        self._tasks.clear()

        ix = IXBrowserClient(base_url=IXBROWSER_API_URL)
        self._engine = FbCreatorEngine(
            ix_client=ix,
            sms_api_key=sms_api_key,
            capsolver_api_key=capsolver_api_key,
            sms_provider=sms_provider,
            use_email=use_email,
            sms_service_name=sms_service_name,
            sms_service_id=sms_service_id,
            sms_target_price=sms_target_price,
            sms_purchase_api_id=sms_purchase_api_id,
            sms_operator=sms_operator,
            sms_timeout=sms_timeout,
        )

        proxy_list = proxies or []
        email_list = [e.strip() for e in (custom_emails or []) if e.strip()]
        jobs: list[FbCreatorJob] = []
        for i in range(count):
            identity = _random_identity(gender)
            proxy: dict[str, Any] = {}
            if proxy_mode == "unit" and proxy_list:
                proxy = proxy_list[i % len(proxy_list)]
            elif proxy_mode == "rotate" and proxy_list:
                proxy = proxy_list[0]

            if email_list and i < len(email_list):
                identity["email"] = email_list[i]

            job = FbCreatorJob(
                first_name=identity["first_name"],
                last_name=identity["last_name"],
                gender=identity["gender"],
                birthday=identity["birthday"],
                email=identity["email"],
                password=identity["password"],
                os_type=os_type,
                browser_type=browser_type,
                proxy=proxy,
            )
            jobs.append(job)
            self.jobs[job.job_id] = job

        sem = asyncio.Semaphore(max_concurrent)

        async def _run_with_sem(j: FbCreatorJob) -> None:
            async with sem:
                if not self._running:
                    j.status = JobStatus.CANCELLED
                    return
                # Alocar verificação: email temporário OU SMS
                if use_email and self._engine:
                    try:
                        j.append_log("Criando email temporário (mail.tm)...")
                        address = await self._engine._acquire_temp_email(j)
                        j.verification_method = "email"
                        j.append_log(f"Email temporário criado: {address}")
                    except Exception as exc:
                        j.append_log(f"Falha ao criar email temp: {exc}")
                        j.status = JobStatus.FAILED
                        j.error = f"Email temporário falhou: {exc}"
                        j.completed_at = time.time()
                        return
                elif use_sms and self._engine and self._engine.sms_api_key:
                    try:
                        j.append_log("Alocando número SMS...")
                        aid, phone = await self._engine._acquire_sms_number(j)
                        j.sms_activation_id = aid
                        j.phone = phone
                        j.append_log(f"Número SMS alocado: {phone}")
                    except Exception as exc:
                        j.append_log(f"Falha ao alocar SMS: {exc}")

                await self._engine.run_job(j)

        async def _run_all() -> None:
            tasks = [asyncio.create_task(_run_with_sem(j)) for j in jobs]
            for i, t in enumerate(tasks):
                self._tasks[jobs[i].job_id] = t
            await asyncio.gather(*tasks, return_exceptions=True)
            self._running = False

        asyncio.create_task(_run_all())

        return {
            "started": count,
            "max_concurrent": max_concurrent,
            "job_ids": [j.job_id for j in jobs],
        }

    async def stop(self) -> dict[str, Any]:
        self._running = False
        cancelled = 0
        for tid, task in self._tasks.items():
            if not task.done():
                task.cancel()
                cancelled += 1
        return {"cancelled": cancelled}


# Singleton global
fb_creator_manager = FbCreatorManager()
