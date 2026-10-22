"""Playwright automation: resilient login + session persistence + 2FA + captcha.

The runner is async; call it via ``asyncio.run(runner.run())`` or schedule it
on an existing event loop from the Streamlit layer.

Key responsibilities:
  * Reuse Playwright ``storage_state`` so repeat logins are short-circuited.
  * Type credentials character-by-character with randomized delay.
  * Inject TOTP codes generated from the user-provided seed.
  * Fall back to manual SMS / interactive wait when 2FA-by-seed fails.
  * Detect reCAPTCHA / hCaptcha iframes and resolve via 2captcha API.
  * Force interface to pt-BR after successful authentication.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import (
    AD_ACCOUNT_CURRENCY,
    AD_ACCOUNT_TIMEZONE,
    ADSPOWER_API_KEY,
    ADSPOWER_API_URL,
    ADSPOWER_CDP_CONNECT_TIMEOUT_MS,
    ADSPOWER_FALLBACK_DIRECT,
    ADSPOWER_START_PATH,
    ADSPOWER_STOP_PATH,
    ADSPOWER_TIMEOUT_S,
    BUSINESS_AD_ACCOUNTS_URL,
    BUSINESS_CREATE_PORTFOLIO_URL,
    BUSINESS_DOMAINS_URL,
    BUSINESS_INFO_URL,
    BUSINESS_OVERVIEW_URL,
    BUSINESS_SELECT_URL,
    BUSINESS_RESTRICTED_PATTERN,
    BUSINESS_LOCKED_PATTERN,
    BUSINESS_SECURITY_URL,
    BUSINESS_WHATSAPP_URL,
    CRIAR_WABA,
    WABA_FALLBACK_SE_VERIFICACAO_INDISPONIVEL,
    BV_AUTO_SUBMIT,
    BV_SMS_RESEND_ATTEMPTS,
    KEEP_BROWSER_OPEN_ON_BV_ERROR,
    BUSINESS_SSO_MANUAL_TIMEOUT_S,
    CALIBRATION_MODE,
    RECON_DUMP,
    DATACRAZY_API_KEY,
    DATACRAZY_ENDPOINT,
    DATACRAZY_TIMEOUT_S,
    LANGUAGE_PREFERENCES_URL,
    META_HOME_URL,
    META_LOGIN_URL,
    ORGANIC_WAIT_MAX_MS,
    ORGANIC_WAIT_MIN_MS,
    PLAYWRIGHT_HEADLESS,
    PLAYWRIGHT_NAV_TIMEOUT_MS,
    PLAYWRIGHT_TIMEOUT_MS,
    PLAYWRIGHT_USER_AGENT,
    PLAYWRIGHT_VIEWPORT,
    PORTFOLIO_LIMIT_PATTERN,
    POST_LOGIN_INDICATOR,
    PREFLIGHT_ENABLED,
    FORCE_LOCALE,
    CLOUDFLARE_DEPLOY_WAIT_S,
    DOMAIN_VERIFY_STRICT,
    DOMAIN_VERIFY_WAIT_S,
    METATAG_PROBE_ATTEMPTS,
    METATAG_PROBE_INTERVAL_S,
    NETWORKIDLE_TIMEOUT_MS,
    NETLIFY_DEPLOY_WAIT_S,
    NETLIFY_WILDCARD,
    PROXY_PASSWORD,
    PROXY_ROTATION_COOLDOWN_S,
    PROXY_ROTATION_TIMEOUT_S,
    PROXY_ROTATION_URL,
    PROXY_SERVER,
    PROXY_USERNAME,
    SCREENSHOTS_DIR,
    STATE_DIR,
    MEASURE_ASSET_BYTES,
    BANDWIDTH_DIR,
    CAPTCHA_MANUAL_TIMEOUT_S,
    CAPTCHA_REFRESH_INTERVAL_S,
    COMPROVANTES_REVIEW_DIR,
    SMS_API_FALLBACK_MANUAL,
    SMS_CODE_REGEX,
    SMS_MANUAL_TIMEOUT_S,
    SMS_POLL_INTERVAL_S,
    SMS_POLL_TIMEOUT_S,
    SMS_RETRY_WAIT_S,
    SMS_VERIFICATION_TIMEOUT_S,
    STORAGE_STATE_PATH,
    TARGET_LOCALE,
    TARGET_LOCALE_HEADER,
    TARGET_TIMEZONE,
    CAPSOLVER_API_KEY,
    TWO_CAPTCHA_API_KEY,
    TWO_CAPTCHA_BASE_URL,
    TWO_CAPTCHA_POLL_INTERVAL_S,
    TWO_CAPTCHA_TIMEOUT_S,
    TYPING_DELAY_MAX_MS,
    TYPING_DELAY_MIN_MS,
    BM_INVITE_ENABLED,
    BM_INVITE_EMAIL,
    BM_INVITE_MAX_RETRIES,
    BM_INVITE_RETRY_INTERVAL_S,
    BM_INVITE_ROLE,
)
from .api_clients import Sms24hClient, Sms24hError, Sms24hTimeoutError
from .auth_fsm import AuthFSM
from .cnpj_reva import emitir_comprovante_cnpj, render_comprovante_local
from .comprovante_phone import (
    resolve_comprovante_telefone,
    should_skip_sms_acquire_for_comprovante,
)
from .errors import (
    CaptchaUnsupportedError,
    CdpConnectionError,
    DomainAlreadyClaimedError,
    InfrastructureError,
    LoginError,
    PreflightError,
    UIError,
)
from .facebook_state import FacebookState, classify_facebook_state
from . import run_control
from .run_control import RunStopRequested
from .pages import FacebookLoginPage, FacebookTwoFactorPage
from .pages import first_visible as _pages_first_visible
from .preflight import run_preflight_checks
from .saga import PipelineSaga
from .netlify_deployer import (
    NetlifyDeployer,
    NetlifyDeployerError,
    NetlifyDomainPool,
    split_subdomain,
)
from .ocr_engine import CNPJData
from utils.logging_utils import (
    install_redaction,
    install_structured_logging,
    step,
    suppress_and_log,
)
from utils.autopilot_state import load_autopilot
from utils.retry import http_retry

# ---------------------------------------------------------------------------
# Logging — add a "SUCCESS" level between INFO and WARNING.
# ---------------------------------------------------------------------------
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


def _success(self, message, *args, **kws):  # noqa: ANN001
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kws)


logging.Logger.success = _success  # type: ignore[attr-defined]

logger = logging.getLogger("meta_automation.automation")
# PR5 — logging estruturado no logger-pai "meta_automation": key=value no
# console + JSONL diário em .state/logs/. Todos os módulos do core
# (automation, auth_fsm, saga, pages, facebook_state, preflight) herdam
# por propagação. A redação L3 vive nos HANDLERS, cobrindo também os
# loggers filhos (que não passam pelos filtros por-logger do pai).
install_structured_logging("meta_automation", jsonl_dir=STATE_DIR / "logs")
# L3 — filtros por-logger mantidos (cobrem records emitidos diretamente).
install_redaction(logger)
install_redaction(logging.getLogger("meta_automation"))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class MetaCredentials:
    email_or_phone: str
    password: str
    totp_seed: Optional[str] = None
    # Email da conta + senha do email + email de recuperação — só pra registrar
    # nas Observações do AdsPower (não usados no login). Vêm da combolist.
    email_address: Optional[str] = None
    email_password: Optional[str] = None
    recovery_email: Optional[str] = None


@dataclass
class BusinessProfile:
    """Inputs required by the business-portfolio creation flow."""
    admin_name: str
    business_email: str
    domain: Optional[str] = None
    portfolio_name: Optional[str] = None  # derived from CNPJData if absent


@dataclass
class AutomationResult:
    logged_in: bool
    used_storage_state: bool
    captcha_solved: bool = False
    captcha_type: Optional[str] = None  # 'recaptcha'|'hcaptcha'|'arkose'|None
    sms_intervention: bool = False
    locale_forced: bool = False
    final_url: Optional[str] = None
    portfolio_created: bool = False
    # Idempotência: conta já tinha portfólio (BM) — criação pulada, ID capturado.
    portfolio_reused: bool = False
    # Regra do operador: conta com ≥2 portfólios é inutilizável → descartar.
    account_discarded: bool = False
    business_id: Optional[str] = None
    portfolio_limit_reached: bool = False
    # "Cadeado vermelho" — Meta restringiu a BM logo após criar (pedido do
    # operador 2026-06). True só se a troca de CNPJ (mesmo perfil) TAMBÉM
    # restringiu — senão a 2ª tentativa segue normal e isto fica False.
    business_restricted: bool = False
    # CADEADO de anúncio na BM recém-criada ('não pode anunciar/criar anúncios').
    # Quando True na 1ª BM, a esteira abandona essa BM e roda a próxima no MESMO
    # perfil (a conta continua boa; só aquele portfólio queimou).
    bm_locked: bool = False
    business_details_saved: bool = False
    domain_added: bool = False
    domain_meta_tag: Optional[str] = None
    meta_verification_token: Optional[str] = None
    site_deployed: bool = False
    # Metatag CONFIRMADA no ar por HTTP (probe) — prova de que o site público
    # responde com o token antes de pedir verificação ao Meta.
    domain_metatag_live: bool = False
    domain_verified: bool = False
    sms_verified: bool = False
    sms_failed_reason: Optional[str] = None  # "timeout", "client_disabled", etc
    waba_id: Optional[str] = None
    # Passo 4 (2026-09) — CONTA DE ANÚNCIOS criada no portfólio, com o MESMO
    # nome da BM e moeda BRL. Substituiu a WABA como ativo do passo 4; é o que
    # define run "100%" junto com a verificação da empresa.
    ad_account_id: Optional[str] = None
    ad_account_name: Optional[str] = None
    ad_account_created: bool = False
    bm_invite_sent: bool = False
    bm_invite_email: Optional[str] = None
    stopped_at_ad_account: bool = False
    meta_token: Optional[str] = None
    # Token de ESCOPO GRAPH (apiAccessToken da bizKitSettingsConfig / Manager) —
    # serve p/ consultar a verificação na Graph API. Distinto do meta_token, que
    # pode vir do payload do WhatsApp (tipo errado, rejeitado com cód 190). O
    # _verify_on_capture tenta este PRIMEIRO e o promove a meta_token se servir.
    verify_token: Optional[str] = None
    # Token de SYSTEM USER (Business Settings → Usuários do sistema) — PERMANENTE
    # (não expira). É o único token que sobrevive até a Meta aprovar (empresa:
    # 2-7 dias úteis, até 30; WABA: até ~7 dias) — o apiAccessToken/verify_token
    # morre em ~1h. Gerado na própria run (_generate_system_user_token), guardado
    # aqui e PROMOVIDO a meta_token p/ as re-checagens consultarem a Graph sem
    # reabrir o browser. Sem ele, mostrar o flip 'aprovada' exigiria reabrir o
    # perfil a cada poll (decisão 18/jun, após pesquisa de lifetime de token).
    system_user_token: Optional[str] = None
    # Verificação consultada NA HORA da captura (token fresco) — prova ao vivo
    # que o método funciona e registra o status inicial (normalmente 'pending').
    verification_status: Optional[str] = None  # empresa (Central de Segurança)
    waba_review_status: Optional[str] = None   # WABA (account_review_status)
    verify_token_ok: bool = False              # token serviu p/ consultar a Graph API
    # Passo 5 — verificação da empresa (Central de Segurança): True quando a
    # esteira chegou na tela 'Carregar documentos' e selecionou o tipo CNPJ
    # (o upload em si ainda NÃO é feito por esta fatia).
    business_verification_started: bool = False
    datacrazy_registered: bool = False
    notes: list[str] = field(default_factory=list)
    # 2 BMs por perfil: snapshots das BMs ANTERIORES concluídas nesta MESMA
    # sessão (a BM corrente fica nos campos business_id/waba_id/etc. de cima).
    # Cada item: {business_id, cnpj, waba_id, ad_account_id, meta_token,
    # system_user_token, complete}. Vazio no fluxo de 1 BM (padrão).
    extra_bms: list = field(default_factory=list)
    # H1 — etapa corrente; atualizada pelo context manager `step()`.
    # Permite ao operador saber em qual sub-passo a esteira parou.
    current_step: str = "boot"
    # PR1 — diagnóstico de falha de login: estado classificado da página
    # (FacebookState.value) + descrição legível para o operador.
    failure_state: Optional[str] = None
    failure_detail: Optional[str] = None
    bounce_post_auth_step: bool = False
    # Controle do operador (botões Encerrar/Assumir) + descarte por selfie.
    # force_close_profile: força fechar o perfil AdsPower no finally (normalmente
    # runs não-100% ficam abertas p/ intervenção; selfie e 'Encerrar' fecham).
    force_close_profile: bool = False
    terminated: bool = False  # 'Encerrar' — run abortada pelo operador
    assumed: bool = False     # 'Assumir' — operador vai concluir manualmente
    # Erro terminal que marca o nome do perfil AdsPower e o fecha (selfie, senha
    # incorreta, login bounce, conta restringida). Ex.: "SENHA", "SELFIE".
    profile_error_tag: Optional[str] = None
    # Run terminou com ERRO (exceção propagou). Usado pra apagar o perfil do
    # AdsPower em qualquer falha (login, captcha, timeout, passo de negócio…),
    # mantendo só as 'em análise' (sucesso) e as 'assumidas'.
    run_failed: bool = False

    def all_steps_ok(self) -> bool:
        """Verifica se todos os 100% dos passos foram concluídos com sucesso.

        2026-09: o passo 4 passou a ser a CONTA DE ANÚNCIOS (a WABA saiu do
        fluxo — ver CRIAR_WABA no config). O critério exige ``ad_account_id``
        no lugar de ``waba_id``/``meta_token``: sem o fluxo do WhatsApp não há
        token de sessão trafegando, e exigi-lo marcaria 100% das runs como
        incompletas.

        Modalidade ``stopped_at_ad_account``: 100% = portfólio + detalhes +
        domínio + conta de anúncios. Verificação da empresa NÃO é exigida.
        """
        if self.stopped_at_ad_account:
            return (
                bool(self.business_id)
                and self.business_details_saved
                and self.domain_verified
                and (bool(self.ad_account_id) or self.ad_account_created)
            )
        return (
            bool(self.business_id)
            and self.business_details_saved
            and self.domain_verified
            # A CONTA existir é o que conta; o ``act_<id>`` é telemetria (o
            # sniffer perde com frequência — ver _ad_finalizar_como_criada).
            and (bool(self.ad_account_id) or self.ad_account_created)
            and self.business_verification_started
        )


# PR2 — LoginError agora vive em core/errors.py (importada no topo) para
# que core/pages/ possa levantá-la sem ciclo de import. O nome continua
# acessível como core.automation.LoginError e core.LoginError.


class PortfolioLimitError(RuntimeError):
    """Raised when Meta refuses portfolio creation due to account limits."""


class AccountDiscardError(RuntimeError):
    """Conta inutilizável (ex.: já possui ≥2 portfólios) — descartar e seguir
    para a próxima. NÃO é falha do bot: é regra de negócio do operador."""


class BusinessDataQueryError(RuntimeError):
    """A Central de Verificacao devolveu 'Erro ao realizar a consulta.' ao
    validar os dados da empresa (CNPJ/endereco/contato) contra o registro
    oficial. Pedido do operador (2026-06): esse erro e TERMINAL pra esta
    conta — nao importa quantas vezes clique Avancar, nunca passa. Descarta
    a conta (nunca mais arrendada) e aborta a run JA, sem gastar tempo nos
    passos seguintes da Central (recaptcha/upload)."""


class DomainNotVerifiedError(RuntimeError):
    """Dominio nao foi verificado pela Meta. Step 4 (WhatsApp) e BLOQUEADO ate
    que o passo 3 detecte o badge 'verified'. Re-rodar a conta mais tarde --
    a Meta verifica de forma assincrona (pode levar ate 72 horas)."""


class BusinessDetailsError(RuntimeError):
    """Passo 2/5 não conseguiu salvar as informações da empresa (diálogo de
    edição não abriu, 0/N campos, ou sessão ausente). É pré-requisito de TUDO
    que vem depois: sem os dados de contato o Meta bloqueia o WhatsApp com
    'atualize os dados da empresa' e a verificação falha pela política.
    LEVANTAR (em vez de retornar) garante step=2/5 status=fail honesto e
    aborta a esteira em produção — antes mascarava o erro como status=ok e a
    cascata queimava a conta no passo 4/5 (produção 2026-06)."""


class AdAccountLimitReached(RuntimeError):
    """A Meta recusou criar por cota — o portfólio JÁ TEM conta de anúncios.

    Não é falha: o passo 4 trata como sucesso (confirma pela lista e segue
    para a verificação) em vez de re-tentar contra o mesmo muro.
    """


class PortfolioIdCaptureError(RuntimeError):
    """Passo 1/5 marcou o portfólio como criado mas NÃO capturou o business_id
    (diff vazio / nome não casou / idempotência cega). Sem business_id real, o
    passo 2 navega com business_id=None, cai em /latest/home (página errada) e
    reporta o FALSO 'editor não abriu' — a cascata vista em produção
    (run 66533593000160, 2026-06-17). ABORTAR claro em 1/5 é honesto: a run não
    tem BM utilizável; melhor falhar aqui que mascarar a falha em 2/5."""

class _SmsFallbackNeeded(Exception):
    """Sinal interno: polling SMS esgotou 2 tentativas na 1ª provedora."""

class _BvSmsRestartNeeded(Exception):
    """Sinal interno: fallback SMS ativado — reiniciar BV desde 'Iniciar verificação'."""

# Re-export extracted classes for backward compatibility
from .capsolver import CapSolverClient
from .captcha import TwoCaptchaSolver
from .adspower_client import AdsPowerClient, AdsPowerError, AdsPowerProfileNotFound


# ---------------------------------------------------------------------------
# Automation runner
# ---------------------------------------------------------------------------
# Rótulos do formulário de criação de portfólio (Business Suite). O Meta
# renderiza os inputs SEM atributo name (só id auto-gerado js_* + aria-labelledby),
# então a detecção primária é por get_by_label (rótulo acessível), com fallback
# estrutural por aria-labelledby dentro do dialog. Bilíngue PT/EN; o rótulo EN
# "Your business and account name" tem ordem invertida vs PT — daí o ramo
# "business and account".
_PORTFOLIO_NAME_LABEL_RE = re.compile(
    r"nome da sua conta e empresa|nome do portf[oó]lio|business name|"
    r"nome da empresa|account and business name|business and account|"
    r"conta e empresa|conta.*empresa",
    re.I,
)
_PORTFOLIO_ADMIN_LABEL_RE = re.compile(r"seu nome|administrador|your name", re.I)
_PORTFOLIO_EMAIL_LABEL_RE = re.compile(
    r"e-?mail.*comercial|business email|seu e-?mail|your.*e-?mail|e-?mail",
    re.I,
)

# --- Credenciais da WABA no payload do POST 'phone-numbers' ----------------
# O fluxo WhatsApp envia { phoneNumberId, token, wabaId } no CORPO da
# requisição (não na resposta). Chaves camelCase; token da família EAB…/EAA…
# (produção 2026-06, conta Sergio). Tentamos as chaves exatas primeiro e, como
# rede, qualquer token EA[A-Z]… / waba_id snake_case que apareça no corpo.
_WA_PAYLOAD_TOKEN_RE = re.compile(r'"token"\s*:\s*"([A-Za-z0-9_\-]{30,})"')
_WA_PAYLOAD_WABA_RE = re.compile(r'"wabaId"\s*:\s*"(\d{10,})"')
_WA_TOKEN_FALLBACK_RE = re.compile(r'(EA[A-Z][A-Za-z0-9]{25,}[A-Za-z0-9_\-]*)')
_WA_WABA_FALLBACK_RE = re.compile(
    r'["\'](?:whatsapp_business_account_id|waba_id|wabaId)["\']\s*:\s*["\'](\d{10,})["\']'
)

# Erro DURO ao adicionar domínio: já verificado/reivindicado por OUTRA empresa
# (regra do Meta: 1 domínio por empresa). NÃO é transitório — re-tentar 3× não
# resolve; o subdomínio (derivado do nome do admin) colidiu com uma BM anterior.
# Abortar rápido com causa clara (produção 2026-06-17, domain_add_failed).
_DOMAIN_HARD_ERR_RE = re.compile(
    r"j[áa] foi verificado por outra empresa"
    r"|j[áa] foi reivindicad"
    r"|already (been )?(verified|claimed) by (another|a different)"
    r"|pertence a outra empresa",
    re.I,
)


def extract_waba_creds_from_payload(
    body: str,
) -> tuple[Optional[str], Optional[str]]:
    """Extrai ``(token, waba_id)`` do corpo de um POST do fluxo WhatsApp.

    Procura as chaves camelCase exatas (``"token"``/``"wabaId"``) e, como
    fallback, qualquer token da família ``EA[A-Z]…`` e ``waba_id`` snake_case.
    Devolve ``(None, None)`` quando nada casa. Função pura (testável).
    """
    if not body:
        return None, None
    tok_m = _WA_PAYLOAD_TOKEN_RE.search(body) or _WA_TOKEN_FALLBACK_RE.search(body)
    waba_m = _WA_PAYLOAD_WABA_RE.search(body) or _WA_WABA_FALLBACK_RE.search(body)
    return (tok_m.group(1) if tok_m else None,
            waba_m.group(1) if waba_m else None)


# --- Sniffer de RESPOSTA (corpo de respostas GraphQL/Graph API) ------------
# Token JSON sem prefixo EA (alguns endpoints) e ``apiAccessToken`` do bootstrap
# (bizKitSettingsConfig) — chaves que o token EA[A-Z]… (_WA_TOKEN_FALLBACK_RE)
# não cobre sozinho. waba_id reaproveita _WA_WABA_FALLBACK_RE (chaves EXPLÍCITAS
# de WABA; o 'id' genérico casava o business_id → waba_id FALSO em produção).
_RESP_ACCESS_TOKEN_JSON_RE = re.compile(
    r'"access_token"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_\-|]{29,})"'
)
_RESP_API_ACCESS_TOKEN_RE = re.compile(
    r'"apiAccessToken"\s*:\s*"(EA[A-Z][A-Za-z0-9_\-|]{20,})"'
)


def extract_waba_creds_from_response(
    text: str, business_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extrai ``(token, verify_token, waba_id)`` do CORPO de uma resposta
    GraphQL/Graph API da Meta — o que o sniffer de rede lê.

    * ``token`` — access_token de uso geral: família ``EA[A-Z]…`` ou as chaves
      JSON ``access_token``/``apiAccessToken``.
    * ``verify_token`` — token de ESCOPO GRAPH (``apiAccessToken``), o melhor p/
      consultar a verificação; vai num slot SEPARADO mesmo quando já há ``token``
      (o do payload do WhatsApp é de tipo errado e a Graph rejeita com cód 190).
    * ``waba_id`` — SÓ de chaves EXPLÍCITAS de WABA e NUNCA igual ao
      ``business_id`` (guarda de falso-positivo: já houve ``waba_id ==
      business_id`` em produção, quando o WhatsApp nem chegou a ser criado).
    Função pura (testável). ``None`` em cada slot que não casar.
    """
    if not text:
        return None, None, None
    token: Optional[str] = None
    if "access_token" in text or "apiAccessToken" in text or "EAA" in text:
        m = (
            _WA_TOKEN_FALLBACK_RE.search(text)
            or _RESP_ACCESS_TOKEN_JSON_RE.search(text)
            or _RESP_API_ACCESS_TOKEN_RE.search(text)
        )
        token = m.group(1) if m else None
    verify_token: Optional[str] = None
    if "apiAccessToken" in text:
        am = _RESP_API_ACCESS_TOKEN_RE.search(text)
        verify_token = am.group(1) if am else None
    waba_id: Optional[str] = None
    if "whatsapp_business_account" in text or "waba" in text.lower():
        wm = _WA_WABA_FALLBACK_RE.search(text)
        if wm and wm.group(1) != business_id:
            waba_id = wm.group(1)
    return token, verify_token, waba_id


# --- Conta de anúncios (passo 4, 2026-09) ---------------------------------
# O id da conta de anúncios aparece como ``act_<digitos>`` na URL/DOM e nas
# respostas GraphQL da criação; as chaves explícitas cobrem as respostas que
# devolvem só o número. NUNCA aceitar a chave ``"id"`` genérica: ela casaria o
# ``business_id`` do portfólio — exatamente o falso-positivo que já mordeu o
# ``waba_id`` em produção (waba_id == business_id, 2026-06).
# "Você atingiu o número máximo de contas de anúncios permitido para um novo
# portfólio empresarial" — a Meta permite UMA conta por portfólio novo. Ver
# esta mensagem significa que a conta JÁ EXISTE: é sucesso, não falha.
_AD_LIMIT_RE = re.compile(
    r"n[úu]mero m[áa]ximo de contas de an[úu]ncios|"
    r"maximum number of ad accounts|"
    r"atingiu o (?:n[úu]mero m[áa]ximo|limite)",
    re.I,
)
# Empty-state da lista de ativos. É o sinal CONFIÁVEL de "não há conta" (foi o
# que funcionou em produção); a ausência dele depois do fluxo de criação é a
# prova de que a conta entrou.
_AD_EMPTY_RE = re.compile(
    r"nenhuma conta de an[úu]ncios|no ad accounts|"
    r"0 contas de an[úu]ncios|adicione uma conta de an[úu]ncios",
    re.I,
)
_AD_ACT_RE = re.compile(r"act[_=](\d{8,})")
# CHAVES EXPLÍCITAS de conta de anúncios apenas. ``account_id`` genérico foi
# REMOVIDO: nas páginas do Business Manager ele é o id da CONTA DO FACEBOOK
# logada (que também é o login da conta no pool). Em produção 2026-09 isso fez
# o passo 4 "achar" a conta de anúncios act_61579183973766 — o próprio login —
# e PULAR a criação em 3 runs seguidas, marcando ad_account_ok à toa.
_AD_ACCOUNT_KEY_RE = re.compile(
    r'["\'](?:ad_?account_?id|adaccount_id|adAccountID|ad_account_ids)["\']'
    r'\s*:\s*\[?\s*["\']?(?:act_)?(\d{8,})',
    re.I,
)


def extract_ad_account_id(
    text: str,
    business_id: Optional[str] = None,
    *,
    exclude: "Optional[set[str]]" = None,
) -> Optional[str]:
    """Extrai o id numérico da conta de anúncios de um texto (URL/HTML/JSON).

    Aceita ``act_123…`` (URL/DOM do Gerenciador) e as chaves EXPLÍCITAS de
    conta de anúncios no JSON do GraphQL. Descarta o ``business_id`` e
    qualquer id em ``exclude`` (o login/uid da conta do Facebook) — sem essa
    guarda o id da própria conta logada passava por conta de anúncios.
    Função pura (testável).
    """
    if not text:
        return None
    bloqueados = {str(x) for x in (exclude or set()) if x}
    if business_id:
        bloqueados.add(str(business_id))
    for rx in (_AD_ACT_RE, _AD_ACCOUNT_KEY_RE):
        for m in rx.finditer(text):
            cand = m.group(1)
            if cand and cand.strip("0") and cand not in bloqueados:
                return cand
    return None


# --- Fallback do WhatsApp Manager (URL + HTML do DOM) ----------------------
# Nenhum dos dois trafega como resposta graphql que o sniffer lê: o waba_id é o
# ``asset_id`` da URL (ou marcadores do DOM) e o token é o ``apiAccessToken`` do
# bootstrap. Recipe calibrado em produção 2026-06 (conta Thiago).
_MGR_ASSET_ID_RE = re.compile(r"asset_id=(\d{8,})")
_MGR_DOM_WABA_RES = (
    re.compile(
        r'"localScopeID"\s*:\s*"(\d{10,})"\s*,\s*"type"\s*:\s*'
        r'"WHATSAPP_BUSINESS_ACCOUNT"'
    ),
    re.compile(
        r'"id"\s*:\s*"(\d{10,})"\s*,\s*"type"\s*:\s*"WHATSAPP_BUSINESS_ACCOUNT"'
    ),
    re.compile(r'"assetID"\s*:\s*"(\d{10,})"'),
)
_MGR_TOKEN_RE = re.compile(
    r'"(?:apiAccessToken|access_token)"\s*:\s*"(EA[A-Z][A-Za-z0-9_\-|]{20,})"'
)
# Token de System User exibido após "Gerar token" — EAA… num campo copiável.
# Sem chave JSON garantida (é texto no DOM), então casa o token cru EAA longo.
# Formato: EA[2-3 caracteres alnum] + 40+ caracteres (alfanum, -, _)
_SYS_USER_TOKEN_RE = re.compile(r"(EA[A-Za-z0-9]{2,}[A-Za-z0-9_\-]{40,})")


def extract_waba_from_manager(
    html: str, page_url: str, business_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extrai ``(waba_id, token, source)`` da URL+HTML do WhatsApp Manager.

    ``waba_id`` vem do ``asset_id`` da URL (``source='url'``) ou, em fallback,
    de marcadores de DOM do Manager (``source='dom'``); NUNCA igual ao
    ``business_id``. ``token`` é o ``apiAccessToken``/``access_token`` (escopo
    Graph). Função pura (testável)."""
    waba_id: Optional[str] = None
    source: Optional[str] = None
    if page_url:
        um = _MGR_ASSET_ID_RE.search(page_url)
        if um and um.group(1) != business_id:
            waba_id, source = um.group(1), "url"
    if waba_id is None and html:
        for rx in _MGR_DOM_WABA_RES:
            dm = rx.search(html)
            if dm and dm.group(1) != business_id:
                waba_id, source = dm.group(1), "dom"
                break
    token: Optional[str] = None
    if html:
        tm = _MGR_TOKEN_RE.search(html)
        if tm:
            token = tm.group(1)
    return waba_id, token, source


class AutomationRunner:
    """Playwright orchestrator. One instance per session."""

    POST_LOGIN_INDICATOR = POST_LOGIN_INDICATOR

    def __init__(
        self,
        credentials: MetaCredentials,
        cnpj_data: CNPJData,
        business_profile: Optional[BusinessProfile] = None,
        *,
        headless: bool = PLAYWRIGHT_HEADLESS,
        storage_state_path: Path = STORAGE_STATE_PATH,
        adspower_mode: str = "off",
        adspower_api_key: Optional[str] = None,
        browser_provider: str = "adspower",
        adspower_user_id: Optional[str] = None,
        adspower_group_id: str = "0",
        cloudflare_account_id: Optional[str] = None,
        cloudflare_api_token: Optional[str] = None,
        cloudflare_worker_name: Optional[str] = None,
        hosting_provider: str = "netlify",
        netlify_domain: Optional[str] = None,
        netlify_api_token: Optional[str] = None,
        preallocated_sms_activation_id: Optional[str] = None,
        preallocated_sms_phone: Optional[str] = None,
        sms_service_code: Optional[str] = None,
        sms_country_code: Optional[str] = None,
        sms_option: Optional[str] = None,
        sms_fallback_option: Optional[str] = None,
        sms_ddd: Optional[str] = None,
        smsrush_server: Optional[int] = None,
        proxy_cfg: Optional[dict[str, str]] = None,
        use_proxy: bool = True,
        adspower_proxy: Optional[dict[str, Any]] = None,
        recon_dump: bool = RECON_DUMP,
        calibration_mode: bool = CALIBRATION_MODE,
        login_only: bool = False,
        extra_bm_units: Optional[list[dict[str, Any]]] = None,
        waba_enabled: bool = False,
        stop_after_ad_account: bool = False,
    ) -> None:
        self.credentials = credentials
        self.cnpj_data = cnpj_data
        self.business_profile = business_profile
        # 2 BMs por perfil: unidades EXTRAS (além da 1ª) rodadas na MESMA sessão.
        # Cada dict: {cnpj_data, business_profile, sms_activation_id, sms_phone}.
        # Vazio = fluxo de 1 BM (padrão) — comportamento inalterado.
        self.extra_bm_units: list[dict[str, Any]] = list(extra_bm_units or [])
        # Perfil AGED que já vem com o portfólio do dono: setado True na criação
        # da 1ª BM (foreign_ids ≥ 1). Só informativo — o fluxo de 2 BMs segue
        # normal e criamos o NOSSO portfólio via /select/. O flag habilita apenas
        # o reuse-fallback DORMENTE (PORTFOLIO_REUSE_FALLBACK, default OFF).
        self._profile_had_foreign_portfolio: bool = False
        self.headless = headless
        # 'usar proxy' OFF (operador roteando pelo celular): além de não arrendar
        # no boot (run_service), as rotações de bounce NÃO podem re-injetar proxy
        # do pool — era o furo que reintroduzia o 402 do Webshare no retry.
        self.use_proxy = use_proxy
        self.waba_enabled = waba_enabled or CRIAR_WABA
        self.stop_after_ad_account = stop_after_ad_account
        # Tracking de CNPJs tentados neste run — evita loop infinito
        # com mesmo CNPJ (2026-06-22: bug onde portfolio tentava N vezes com mesmo CNPJ).
        self._cnpjs_attempted: set[str] = {self._profile_cnpj_digits()}
        # Tracking de ciclo de login→captcha→login (indefinido):
        # se LoginError aparecer MÚLTIPLAS vezes, congela em vez de descartar.
        self._login_error_count: int = 0
        # Flags de calibração (default = config/env; UI pode sobrescrever por
        # execução). recon = só captura DOM de todas as telas; calibration =
        # roda a esteira sem fail-fast, dumpando cada passo que falha.
        self.recon_dump = recon_dump
        self.calibration_mode = calibration_mode
        # Botão "Só logar" (painel, embaixo de Enfileirar e rodar): loga e
        # PARA — sem etapas de negócio nenhuma. Igual ao "Assumir", só que
        # decidido pelo operador ANTES do run começar, não no meio de um erro.
        self.login_only = bool(login_only)
        # Storage_state ESPECÍFICO POR CNPJ para evitar cruzamento de contas
        # (antes estava global .state/storage_state.json, compartilhado por todos)
        cnpj_digits = self._profile_cnpj_digits()
        if str(storage_state_path) == str(STORAGE_STATE_PATH):
            # Path padrão → criar específico por CNPJ
            storage_cache_dir = STATE_DIR / "cache" / cnpj_digits
            storage_cache_dir.mkdir(parents=True, exist_ok=True)
            self.storage_state_path = storage_cache_dir / "storage_state.json"
        else:
            # Path customizado (de testes ou debug) → usar como está
            self.storage_state_path = Path(storage_state_path)
        # adspower_mode: "off" (Playwright direto) | "create" (cria perfil novo
        # idempotente por CNPJ) | "reuse" (reutiliza user_id fornecido).
        self.adspower_mode = adspower_mode if adspower_mode in {"off", "create", "reuse"} else "off"
        self.adspower_user_id = (adspower_user_id or "").strip() or None
        self.adspower_serial_number: Optional[str] = None  # número sequencial do painel AdsPower (ex: "9001")
        self.adspower_profile_name: Optional[str] = None    # nome do perfil (p/ marcar com erro)
        self.adspower_group_id = adspower_group_id or "0"
        # M3 — proxy injetado por execução (substitui globals em automation_module).
        # Quando None, cai nos defaults do env (PROXY_SERVER/USER/PASS).
        self.proxy_cfg: Optional[dict[str, str]] = proxy_cfg
        # Proxy do POOL (do painel), aplicado no perfil AdsPower na CRIAÇÃO —
        # mascara o IP por conta. {scheme,host,port,username,password} ou None.
        self.adspower_proxy: Optional[dict[str, Any]] = adspower_proxy
        # Rotação de IP pós login_bounced: o username é a identidade do IP no
        # Webshare (todos compartilham host:port). Guardamos o atual + a blocklist
        # dos que já bouncearam neste run p/ pegar OUTRO IP no retry.
        self._current_proxy_username: Optional[str] = (
            (adspower_proxy or {}).get("username") or None
        )
        self._bad_proxy_usernames: set[str] = set()
        # Rotação de CONTA pós bounce (IP+conta): contas que já bouncearam neste
        # run (voltam ao pool, mas não são re-tentadas aqui).
        self._bad_account_emails: set[str] = set()
        # Portfólios (BMs) DETECTADOS na conta neste run — o run_service usa p/
        # decidir reuso (conta sem 2 portfólios volta ao pool). None = nem chegou
        # ao passo 1 (não consumiu nada).
        self._portfolio_count: Optional[int] = None
        # Sniffer da conta de anúncios ARMADO (passo 4). Fora do passo o
        # sniffer ignora ``act_…`` — a página de ativos lista contas
        # PRÉ-EXISTENTES e capturá-las daria um id alheio como "criado".
        self._ad_capture_armed: bool = False
        # Overrides dinâmicos por execução (vindos da UI). Quando None, o
        # SiteDeployer cai nos defaults de config.py / env.
        self.cloudflare_account_id = (cloudflare_account_id or "").strip() or None
        self.cloudflare_api_token = (cloudflare_api_token or "").strip() or None
        self.cloudflare_worker_name = (cloudflare_worker_name or "").strip() or None
        # Provedor de hospedagem desta run: "netlify" (padrão) | "cloudflare".
        # Escolhido pelo run_service a partir de settings.hosting_provider.
        self.hosting_provider = (hosting_provider or "netlify").strip().lower()
        # Netlify (substitui a Cloudflare como host). Override por execução vindo
        # do run_service (domínio ativo do pool); quando None, o deploy lê o pool
        # de domínios do store (NetlifyDomainPool).
        self.netlify_domain = (netlify_domain or "").strip() or None
        self.netlify_api_token = (netlify_api_token or "").strip() or None
        # Quando a UI já alocou um número antes do pipeline rodar, passamos o
        # (activation_id, phone) e o runner pula o getNumber interno — apenas
        # faz polling/release no MESMO id. Mantém saldo consistente no SMS2H.
        self.preallocated_sms_activation_id = (
            (preallocated_sms_activation_id or "").strip() or None
        )
        self.preallocated_sms_phone = (
            (preallocated_sms_phone or "").strip() or None
        )
        # CapSolver cobre reCAPTCHA Enterprise/Arkose (o que o Meta serve no 2FA
        # e o 2captcha devolve como ERROR_CAPTCHA_UNSOLVABLE) — preferido quando
        # configurado; senão cai no 2captcha; senão None (só manual).
        if CAPSOLVER_API_KEY:
            self.captcha_solver = CapSolverClient(CAPSOLVER_API_KEY)
        elif TWO_CAPTCHA_API_KEY:
            self.captcha_solver = TwoCaptchaSolver(TWO_CAPTCHA_API_KEY)
        else:
            self.captcha_solver = None
        sms_kwargs: dict[str, Any] = {}
        if sms_service_code:
            sms_kwargs["service_code"] = sms_service_code
        if sms_country_code:
            sms_kwargs["country_code"] = sms_country_code
        self.sms_option = sms_option
        _sms_opt = (sms_option or "").strip().lower()
        if _sms_opt == "sms24h":
            from core.api_clients import Sms24hOrgClient  # noqa: PLC0415
            self.sms_client = Sms24hOrgClient()
        elif _sms_opt == "smsrush":
            from core.api_clients import SmsRushClient  # noqa: PLC0415
            self.sms_client = SmsRushClient()
        else:
            from config import sms_kwargs_for_option  # noqa: PLC0415
            _opt_kw = sms_kwargs_for_option(sms_option)
            sms_kwargs.update(_opt_kw)
            logger.info(
                "sms_client: sms_option=%r → preset=%r → api_id=%s price=%s",
                sms_option, _opt_kw,
                sms_kwargs.get("purchase_api_id", "default"),
                sms_kwargs.get("target_price", "default"),
            )
            self.sms_client = Sms24hClient(**sms_kwargs)
        self.sms_fallback_option = (sms_fallback_option or "").strip().lower() or None
        self.sms_ddd = (sms_ddd or "").strip() or None
        self.smsrush_server = smsrush_server
        self._sms_erro2 = False
        self.browser_provider_name = browser_provider or "adspower"
        if self.browser_provider_name == "ixbrowser":
            from core.ixbrowser_client import IXBrowserClient  # noqa: PLC0415
            self.adspower = IXBrowserClient()
        else:
            adspower_kwargs: dict[str, Any] = {}
            if adspower_api_key:
                adspower_kwargs["api_key"] = adspower_api_key
            self.adspower = AdsPowerClient(**adspower_kwargs)
        # H1 — referência ao último AutomationResult em curso (acessível pelo
        # caller mesmo após exceção, para inspecionar ``current_step``).
        self.last_result: Optional[AutomationResult] = None
        # PR4 — saga transacional da execução corrente (criada em
        # _run_with_playwright; None fora de uma execução).
        self.saga: Optional[PipelineSaga] = None
        # Último exception durante a execução (armazenado no except block);
        # None se nenhuma exceção foi levantada. Usado para classificação de
        # erros terminal vs não-terminal na lógica de descarte de conta.
        self._last_exc: Optional[BaseException] = None

    def _effective_proxy_cfg(self) -> Optional[dict[str, str]]:
        """M3 — proxy efetivo: injetado > env. Sem mutação de globals."""
        if self.proxy_cfg and self.proxy_cfg.get("server"):
            return dict(self.proxy_cfg)
        if PROXY_SERVER:
            cfg: dict[str, str] = {"server": PROXY_SERVER}
            if PROXY_USERNAME:
                cfg["username"] = PROXY_USERNAME
            if PROXY_PASSWORD:
                cfg["password"] = PROXY_PASSWORD
            return cfg
        return None

    async def _resolve_or_create_adspower_profile(self) -> None:
        """Idempotência por CNPJ: se já criamos um perfil para esse CNPJ no
        passado, reusa o user_id; senão cria novo via API e persiste o mapping.

        ESPECIAL: Em modo RESUME (reiniciar), FORÇA reutilização do perfil existente
        para manter o navegador AdsPower aberto (não cria novo).
        """
        from utils.helpers import only_digits  # noqa: PLC0415
        from utils.profiles_state import (  # noqa: PLC0415
            delete_profile, find_profile_by_cnpj, save_profile, touch_profile,
        )
        from core import run_control  # noqa: PLC0415

        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
        raw_cnpj = (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
        cnpj_digits = only_digits(raw_cnpj)

        # RESUME mode: força reutilização do perfil existente (não cria novo)
        is_resuming = run_control.is_resume_requested(self.run_id)
        if is_resuming:
            logger.info(
                "AdsPower: RESUME mode ativado — forçando reutilização do perfil "
                "existente (não cria novo, mantém navegador aberto)."
            )
        if len(cnpj_digits) != 14:
            raise AdsPowerError(
                "Modo 'create' exige CNPJ válido (14 dígitos) para indexar "
                f"o perfil — recebi {raw_cnpj!r} → digits={cnpj_digits!r}."
            )

        acct = (self.credentials.email_or_phone or "").strip().lower()

        # RESUME mode: usa o perfil que já está aberto no AdsPower
        if is_resuming and self.adspower_user_id:
            logger.info(
                "AdsPower: RESUME mode — reutilizando perfil já aberto (user_id=%s, "
                "navegador continua aberto).",
                self.adspower_user_id,
            )
            return

        # Auto-curador de perfil órfão: se um perfil foi salvo aqui mas depois
        # deletado no painel AdsPower, recriar faz sentido só se o id veio do cache.
        existing = find_profile_by_cnpj(cnpj_digits, provider=_bp)
        if existing and existing.get("user_id"):
            prev_email = (existing.get("email") or "").strip().lower()
            if prev_email and acct and prev_email != acct:
                # A CONTA mudou para este CNPJ. Reusar o perfil restauraria a
                # SESSÃO da conta antiga (cookies do perfil AdsPower) e o login
                # da nova nunca aconteceria — a run rodaria com a conta ERRADA
                # (bug visto em produção 2026-06). Descarta o mapping (e as
                # flags antigas) e cai no caminho de criação → perfil novo, sem
                # sessão → login fresco da conta certa.
                old_user_id = existing.get("user_id")
                logger.warning(
                    "AdsPower: CNPJ %s estava ligado à conta %r e agora veio %r "
                    "— fechando perfil antigo %s e criando NOVO (login fresco).",
                    cnpj_digits, prev_email, acct, old_user_id,
                )
                # Fecha o perfil antigo antes de deletar o mapping
                if old_user_id:
                    try:
                        await self.adspower.stop(old_user_id)
                        logger.info("AdsPower: perfil %s fechado com sucesso.", old_user_id)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("AdsPower: erro ao fechar perfil %s (%s).", old_user_id, e)
                delete_profile(cnpj_digits, provider=_bp)
            else:
                self.adspower_user_id = existing["user_id"]
                touch_profile(cnpj_digits, provider=_bp)
                logger.info(
                    "AdsPower: perfil já existe para CNPJ %s — reusando user_id=%s",
                    cnpj_digits, self.adspower_user_id,
                )
                await self._persist_credentials_remark()
                # Captura serial_number para exibir no painel (número sequencial do AdsPower)
                try:
                    info = await self.adspower.get_profile_info(self.adspower_user_id)
                    self.adspower_serial_number = str(info.get("serial_number") or "").strip() or None
                except Exception as _e:
                    logger.debug("AdsPower: não foi possível obter serial_number: %s", _e)
                self._persist_profile_to_run()
                return

        # Nome do perfil: <slug_admin>_<cnpj14>. Determinístico, legível no painel.
        from .api_clients import slugify_for_worker  # noqa: PLC0415
        razao = (self.cnpj_data.razao_social or "") if self.cnpj_data else ""
        slug = slugify_for_worker(razao)[:24] or "cliente"
        profile_name = f"{slug}_{cnpj_digits}"
        self.adspower_profile_name = profile_name

        logger.info("AdsPower: criando perfil novo (name=%s)…", profile_name)
        # group_id None aqui → AdsPowerClient.create_profile resolve o
        # primeiro grupo válido da conta automaticamente. O default antigo
        # ('0') quebrava quando o grupo padrão foi deletado/arquivado.
        explicit_gid = self.adspower_group_id if self.adspower_group_id not in {"", "0"} else None
        created = await self.adspower.create_profile(
            name=profile_name,
            group_id=explicit_gid,
            remark=self._build_credentials_remark(),
            proxy=self.adspower_proxy,
        )
        if self.adspower_proxy and self.adspower_proxy.get("host"):
            logger.info(
                "AdsPower: perfil sairá pelo proxy %s:%s (IP mascarado).",
                self.adspower_proxy.get("host"), self.adspower_proxy.get("port"),
            )
        self.adspower_user_id = created["user_id"]
        save_profile(
            cnpj_digits,
            user_id=self.adspower_user_id,
            name=profile_name,
            status="active",
            email=acct,
            provider=_bp,
        )
        logger.success(
            "AdsPower: perfil criado e persistido (CNPJ=%s → user_id=%s)",
            cnpj_digits, self.adspower_user_id,
        )
        # Captura serial_number para exibir no painel (número sequencial do AdsPower)
        try:
            info = await self.adspower.get_profile_info(self.adspower_user_id)
            self.adspower_serial_number = str(info.get("serial_number") or "").strip() or None
        except Exception as _e:
            logger.debug("AdsPower: não foi possível obter serial_number: %s", _e)
        self._persist_profile_to_run()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    # Retry de boot+auth quando o ALVO CDP FECHA no meio do run (browser/
    # context/page). Causa típica (produção 2026-06, conta Cleber): o Meta
    # serve a 2FA na rota /authentication/ (mais hostil — captcha/Arkose) e,
    # em conta nova, derruba a aba com um bounce transitório ('erro da
    # plataforma' que o operador também via no manual). Reabrir o perfil e
    # refazer do zero costuma passar — toda a esteira é idempotente (reuso de
    # portfólio, flags de locale/detalhes), então repetir é seguro.
    _TARGET_CLOSED_RETRIES = 2
    _TARGET_CLOSED_BACKOFF_S = 8.0
    # login_bounced (soft-block de IP): nº de IPs alternativos do pool a tentar
    # antes de desistir do login (rotação de IP + recriação do perfil).
    _LOGIN_BOUNCE_PROXY_ROTATIONS = 2
    # IPs esgotados e ainda bounce → a CONTA também pode estar marcada: troca de
    # conta (volta a atual ao pool) + IP novo, e refaz. Nº de contas a tentar.
    _LOGIN_BOUNCE_ACCOUNT_ROTATIONS = 2
    # Túnel do proxy falhou (ERR_TUNNEL_CONNECTION_FAILED etc.): o IP arrendado
    # nem CONECTA — é 100% IP morto (não a conta). Pool residencial tem vários
    # exits mortos, então o orçamento é maior; NUNCA escala p/ troca de conta.
    _PROXY_TUNNEL_ROTATIONS = 3
    _PROXYLESS_LOGIN_ATTEMPTS = 3
    _PROXYLESS_LOGIN_TIMEOUT_S = 35.0
    # Marcadores de erro de TÚNEL/CONEXÃO do proxy (Chromium net errors). São
    # inequívocos: o proxy não estabeleceu a conexão → descartar o IP e trocar.
    _PROXY_TUNNEL_MARKERS = (
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_SOCKS_CONNECTION_FAILED",
        "ERR_PROXY_AUTH_REQUESTED",
        "ERR_PROXY_AUTH_UNSUPPORTED",
        "ERR_NO_SUPPORTED_PROXIES",
        "ERR_PROXY_CERTIFICATE_INVALID",
    )

    @staticmethod
    def _is_target_closed(exc: BaseException) -> bool:
        names = {type(exc).__name__}
        msg = str(exc) or ""
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        if cause is not None:
            names.add(type(cause).__name__)
            msg += " " + (str(cause) or "")
        return (
            "TargetClosedError" in names
            or "Target page, context or browser has been closed" in msg
            or "Target closed" in msg
        )

    @staticmethod
    def _is_proxy_tunnel_error(exc: BaseException) -> bool:
        """True p/ falhas de TÚNEL/CONEXÃO do proxy (o IP arrendado não conecta).
        Inequivocamente problema de IP → descartar e trocar (não é a conta).
        Olha a mensagem e a causa encadeada (o erro do Playwright traz o net
        error do Chromium na string, ex.: 'net::ERR_TUNNEL_CONNECTION_FAILED')."""
        msg = str(exc) or ""
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        if cause is not None:
            msg += " " + (str(cause) or "")
        up = msg.upper()
        return any(m in up for m in AutomationRunner._PROXY_TUNNEL_MARKERS)

    async def run(self) -> AutomationResult:
        """Entry point público — contabiliza esta run como "ativa" para o
        throttle de chamadas ao AdsPower (``core.adspower_client``: com mais
        de 2 runs concorrentes, as chamadas à API local passam a respeitar
        um intervalo mínimo entre si, pra não estourar "too many requests").
        A lógica de fato mora em ``_run_impl`` — inalterada."""
        from core.adspower_client import (  # noqa: PLC0415
            adspower_run_finished,
            adspower_run_started,
        )
        from core import store  # noqa: PLC0415

        adspower_run_started()
        try:
            return await self._run_impl()
        finally:
            adspower_run_finished()
            # Descarte de CNPJ e perfil Adspower agora é MANUAL via endpoint /terminate
            # (operador decide se quer reiniciar com novo CNPJ ou descartar de vez)

    # ------------------------------------------------------------------
    # ASSUMIR — parada IMEDIATA e TOTAL a pedido do operador
    # ------------------------------------------------------------------
    def _assume_requested(self) -> bool:
        """True se o operador clicou "Assumir" nesta run.

        Lê o flag PEGAJOSO de ``run_control`` (não some ao ser lido): todo o
        caminho de erro/limpeza consulta isto e NÃO fecha navegador, NÃO apaga
        o perfil AdsPower e NÃO re-tenta nada — mesmo que o pipeline tenha
        morrido por outro erro antes de chegar no próximo checkpoint."""
        try:
            return run_control.is_assume_requested(getattr(self, "run_id", None))
        except Exception:  # noqa: BLE001
            return False

    def _mark_assumed(self, result: Optional["AutomationResult"]) -> None:
        """Marca o resultado como ASSUMIDO (e desmarca falha/fechamento)."""
        if result is None:
            return
        result.assumed = True
        result.run_failed = False
        result.force_close_profile = False
        result.current_step = "assumed"

    def _close_requested(self) -> bool:
        """True se o operador clicou "Encerrar"/"Voltar Conta" nesta run.

        É o ÚNICO gatilho que fecha o navegador e apaga o perfil AdsPower —
        e também o que proíbe qualquer retry/rotação depois do clique (senão
        o bot reabria o perfil e tentava logar de novo na conta que o operador
        acabou de encerrar)."""
        try:
            if run_control.is_close_requested(getattr(self, "run_id", None)):
                return True
        except Exception:  # noqa: BLE001
            pass
        res = self.last_result
        return bool(res is not None and getattr(res, "terminated", False))

    def _mark_terminated(self, result: Optional["AutomationResult"]) -> None:
        """Marca o resultado como ENCERRADO pelo operador (fecha o perfil)."""
        if result is None:
            return
        result.terminated = True
        result.force_close_profile = True
        if result.current_step != "released_account":
            result.current_step = "terminated"

    async def _assume_watchdog(self, task: "asyncio.Task") -> None:
        """Vigia o clique em "Assumir" e CANCELA o pipeline na hora.

        Os checkpoints cooperativos (``_check_stop``) só rodam ENTRE passos —
        um passo longo (login, SMS, verificação de domínio) seguiria clicando
        por minutos depois de o operador assumir. Aqui o cancelamento chega no
        meio do ``await``: o bot para de fato, sem tocar em nada."""
        while not task.done():
            if self._assume_requested():
                self._mark_assumed(self.last_result)
                logger.warning(
                    "ASSUMIR: pedido do operador — CANCELANDO o pipeline AGORA "
                    "(nenhuma ação a mais no navegador; perfil AdsPower intacto)."
                )
                task.cancel()
                return
            if self._close_requested():
                self._mark_terminated(self.last_result)
                logger.warning(
                    "ENCERRAR: pedido do operador — CANCELANDO o pipeline AGORA. "
                    "Nada de re-tentar login; o perfil é fechado e apagado "
                    "porque foi ELE quem mandou fechar."
                )
                task.cancel()
                return
            await asyncio.sleep(0.4)

    async def _run_with_assume_guard(self, pw: Playwright) -> AutomationResult:
        """Roda o pipeline com o watchdog de "Assumir" ligado."""
        task = asyncio.ensure_future(self._run_with_playwright(pw))
        watchdog = asyncio.ensure_future(self._assume_watchdog(task))
        try:
            return await task
        except asyncio.CancelledError:
            # Cancelamento causado pelo "Assumir"/"Encerrar": devolve o
            # resultado parcial em vez de propagar — a run acabou aqui.
            res = self.last_result
            if self._assume_requested() and res is not None:
                self._mark_assumed(res)
                return res
            if self._close_requested() and res is not None:
                self._mark_terminated(res)
                return res
            raise
        finally:
            watchdog.cancel()

    async def _run_impl(self) -> AutomationResult:
        async with async_playwright() as pw:
            attempt = 0
            proxy_rotations = 0
            account_rotations = 0
            proxy_tunnel_rotations = 0
            proxyless_attempts = 0
            while True:
                attempt += 1
                try:
                    return await self._run_with_assume_guard(pw)
                except BaseException as exc:  # noqa: BLE001
                    # ASSUMIR: operador assumiu — NADA de retry aqui. As rotações
                    # de IP/conta recriam o perfil (fecham e APAGAM o AdsPower que
                    # o operador está usando) — é exatamente o que ele não quer.
                    if self._assume_requested():
                        res = self.last_result
                        logger.warning(
                            "ASSUMIR: run assumida pelo operador — retry/rotação "
                            "CANCELADOS (perfil AdsPower preservado). Causa da "
                            "interrupção: %s", type(exc).__name__,
                        )
                        if res is not None:
                            self._mark_assumed(res)
                            return res
                        raise
                    # ENCERRAR: o operador fechou. NÃO re-tenta login, NÃO troca
                    # de IP/conta, NÃO recria perfil — era exatamente isso que
                    # fazia a conta encerrada abrir de novo e tentar logar.
                    if self._close_requested():
                        res = self.last_result
                        logger.warning(
                            "ENCERRAR: run encerrada pelo operador — retry, "
                            "rotação de IP/conta e recriação de perfil "
                            "CANCELADOS (causa da interrupção: %s).",
                            type(exc).__name__,
                        )
                        if res is not None:
                            self._mark_terminated(res)
                            return res
                        raise
                    # NÃO retry em cancel/interrupt — só no alvo CDP fechado.
                    if attempt <= self._TARGET_CLOSED_RETRIES and self._is_target_closed(exc):
                        logger.warning(
                            "Alvo CDP fechou no meio do run (%s) — provável bounce "
                            "transitório do Facebook / instabilidade do AdsPower. "
                            "Reabrindo o perfil e refazendo do zero (tentativa "
                            "%d/%d).",
                            type(exc).__name__, attempt, self._TARGET_CLOSED_RETRIES,
                        )
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    # Falha de TÚNEL do proxy (ERR_TUNNEL_CONNECTION_FAILED etc.):
                    # o IP arrendado nem conecta ao Facebook — é 100% IP morto
                    # (não a conta). Descarta o IP e troca por outro (MESMA conta),
                    # com orçamento próprio; NUNCA escala p/ troca de conta. Visto
                    # em produção 2026-06-17 (run 49bc1615, IP BR-24 logo após uma
                    # rotação de bounce): antes a run só morria aqui.
                    if (
                        self._is_proxy_tunnel_error(exc)
                        and proxy_tunnel_rotations < self._PROXY_TUNNEL_ROTATIONS
                        and await self._rotate_pool_proxy_for_retry()
                    ):
                        proxy_tunnel_rotations += 1
                        logger.warning(
                            "túnel do proxy falhou (IP morto) — IP DESCARTADO e "
                            "trocado (rotação %d/%d); refazendo.",
                            proxy_tunnel_rotations, self._PROXY_TUNNEL_ROTATIONS,
                        )
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    # Bounce de login = soft-block de IP — SILENCIOSO (volta ao
                    # form) OU 'A senha inserida está incorreta' (o operador
                    # confirmou: ISSO é IP, não senha). NÍVEL 1: troca de IP
                    # (mantém a conta). NÍVEL 2 (IPs esgotados e ainda bounce): a
                    # CONTA também pode estar marcada → troca de conta (volta a
                    # atual ao pool) + IP novo. (3 IPs bouncearam na MESMA conta —
                    # run a906c698 — confirmando que precisa trocar a conta.)
                    bounce = self._is_login_bounced(exc) or self._is_wrong_password(exc)
                    # Bounce PÓS captcha/2FA: login aceito mas volta ao form
                    # depois da verificação → é o PROXY, não o IP/conta.
                    # Rotações de IP/conta são inúteis — vai direto pro proxyless.
                    _bounce_post_2fa = getattr(
                        self.last_result, "bounce_post_auth_step", False
                    )
                    if (
                        bounce
                        and not _bounce_post_2fa
                        and proxy_rotations < self._LOGIN_BOUNCE_PROXY_ROTATIONS
                        and await self._rotate_pool_proxy_for_retry()
                    ):
                        proxy_rotations += 1
                        logger.warning(
                            "bounce de login — IP trocado (rotação %d/%d); refazendo.",
                            proxy_rotations, self._LOGIN_BOUNCE_PROXY_ROTATIONS,
                        )
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    if (
                        bounce
                        and not _bounce_post_2fa
                        and account_rotations < self._LOGIN_BOUNCE_ACCOUNT_ROTATIONS
                        and await self._rotate_account_and_proxy_for_retry(
                            # senha inválida persistiu pelas rotações de IP = é a
                            # CONTA, não o IP → descarta (não re-arrenda).
                            discard_current=self._is_wrong_password(exc),
                        )
                    ):
                        account_rotations += 1
                        proxy_rotations = 0  # conta nova → reinicia o orçamento de IP
                        logger.warning(
                            "bounce persistente — CONTA trocada (rotação %d/%d); "
                            "refazendo o login com nova conta + IP.",
                            account_rotations, self._LOGIN_BOUNCE_ACCOUNT_ROTATIONS,
                        )
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    # PROXYLESS: rotações de IP e conta esgotadas (ou erro de
                    # login/túnel que NÃO é senha) — tira o proxy e tenta com IP
                    # local. Algumas contas FB não logam via proxy; IP direto resolve.
                    _is_login_fail = (
                        bounce
                        or self._is_login_bounced(exc)
                        or isinstance(exc, LoginError)
                        or isinstance(exc, asyncio.TimeoutError)
                        or self._is_proxy_tunnel_error(exc)
                    )
                    if self._is_terminal_login_failure(exc):
                        logger.warning(
                            "PROXYLESS: estado terminal (%s) — retry SEM sentido, "
                            "conta perdida. Propagando erro.",
                            getattr(self.last_result, "failure_state", "?"),
                        )
                        raise
                    if (
                        _is_login_fail
                        and not self._is_wrong_password(exc)
                        and self.adspower_proxy is not None
                        and proxyless_attempts < self._PROXYLESS_LOGIN_ATTEMPTS
                    ):
                        proxyless_attempts += 1
                        self.adspower_proxy = None
                        self.proxy_cfg = None
                        self._current_proxy_username = None
                        self._login_attempt_timeout_s = self._PROXYLESS_LOGIN_TIMEOUT_S
                        logger.warning(
                            "PROXYLESS: login com proxy falhou — removendo proxy "
                            "e tentando com IP local da máquina (tentativa %d/%d, "
                            "timeout %ds).",
                            proxyless_attempts, self._PROXYLESS_LOGIN_ATTEMPTS,
                            int(self._PROXYLESS_LOGIN_TIMEOUT_S),
                        )
                        await self._recreate_adspower_profile_for_retry()
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    if (
                        _is_login_fail
                        and not self._is_wrong_password(exc)
                        and self.adspower_proxy is None
                        and proxyless_attempts > 0
                        and proxyless_attempts < self._PROXYLESS_LOGIN_ATTEMPTS
                    ):
                        proxyless_attempts += 1
                        logger.warning(
                            "PROXYLESS: login sem proxy falhou — re-tentando "
                            "(tentativa %d/%d).",
                            proxyless_attempts, self._PROXYLESS_LOGIN_ATTEMPTS,
                        )
                        await self._recreate_adspower_profile_for_retry()
                        await self._reacquire_sms_for_retry()
                        await asyncio.sleep(self._TARGET_CLOSED_BACKOFF_S)
                        continue
                    raise

    def _is_login_bounced(self, exc: BaseException) -> bool:
        """True só p/ o soft-block de IP no login (login_bounced) — único caso em
        que trocar de IP e refazer o login faz sentido (demais LoginError não)."""
        if not isinstance(exc, LoginError):
            return False
        res = self.last_result
        if res is not None and getattr(res, "failure_state", "") == "login_bounced":
            return True
        msg = (str(exc) or "").lower()
        return "login bounced" in msg or "[estado=login_bounced]" in msg

    async def _rotate_pool_proxy_for_retry(self) -> bool:
        """Pós bounce: DESCARTA o IP atual (não volta ao pool — 'ips que não
        passarem descarta'), pega OUTRO IP BR do pool e recria o perfil AdsPower
        com o novo IP (o proxy entra na CRIAÇÃO do perfil). Devolve True só se
        trocou de IP de fato."""
        if not getattr(self, "use_proxy", True):
            logger.info(
                "rotação de IP pulada — 'usar proxy' OFF (IP direto não tem o que "
                "rotacionar; bounce/senha incorreta vira terminal)."
            )
            return False
        from core import store  # noqa: PLC0415
        from utils.helpers import only_digits  # noqa: PLC0415

        cnpj_digits = only_digits(
            (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
        )
        if len(cnpj_digits) != 14:
            return False
        cur = self._current_proxy_username or (self.adspower_proxy or {}).get("username")
        if cur:
            self._bad_proxy_usernames.add(cur)
        try:
            if cur:
                store.discard_proxy(cur)  # IP ruim → fora do pool de vez
            else:
                store.release_proxy(cnpj_digits)
            leased = store.lease_proxy(
                cnpj_digits, exclude_usernames=self._bad_proxy_usernames
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rotação de IP: falha ao re-arrendar proxy (%s).", exc)
            return False
        if not leased:
            logger.warning(
                "rotação de IP: pool sem outro IP BR livre (descartados=%d) — sem "
                "retry de login.", len(self._bad_proxy_usernames),
            )
            return False
        new_user = leased.get("username")
        if new_user and new_user == cur:
            logger.warning("rotação de IP: pool devolveu o MESMO IP — sem retry.")
            return False
        scheme = leased.get("scheme") or "http"
        self.adspower_proxy = {
            "scheme": scheme,
            "host": leased.get("host"),
            "port": leased.get("port"),
            "username": new_user,
            "password": leased.get("password"),
        }
        server = f"{scheme}://{leased.get('host')}:{leased.get('port')}"
        pc: dict[str, str] = {"server": server}
        if new_user:
            pc["username"] = new_user
        if leased.get("password"):
            pc["password"] = leased["password"]
        self.proxy_cfg = pc
        self._current_proxy_username = new_user
        await self._recreate_adspower_profile_for_retry()
        logger.warning(
            "rotação de IP (bounce): IP anterior DESCARTADO; novo IP BR "
            "username=%s host=%s — perfil recriado, refazendo o login.",
            new_user, leased.get("host"),
        )
        return True

    async def _recreate_adspower_profile_for_retry(self) -> None:
        """Fecha+apaga o perfil AdsPower atual e zera o user_id — força um perfil
        NOVO no retry (o IP/proxy entra na CRIAÇÃO; a API não tem update de
        proxy). Compartilhado pela rotação de IP e de conta."""
        from utils.helpers import only_digits  # noqa: PLC0415
        from utils.profiles_state import delete_profile  # noqa: PLC0415

        old_id = self.adspower_user_id
        if old_id:
            try:
                await self.adspower.stop(old_id)
            except Exception:  # noqa: BLE001
                pass
        cnpj_digits = only_digits(
            (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
        )
        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
        if len(cnpj_digits) == 14:
            try:
                delete_profile(cnpj_digits, provider=_bp)
            except Exception:  # noqa: BLE001
                pass
        self.adspower_user_id = None

    _TERMINAL_FAILURE_STATES = frozenset({
        "account_suspended", "account_disabled", "account_locked",
        "rate_limited", "identity_selfie",
    })

    def _is_terminal_login_failure(self, exc: BaseException) -> bool:
        """True quando o login falhou por estado terminal (conta bloqueada,
        suspensa, desabilitada, rate-limited ou selfie). Trocar proxy não
        resolve — a conta está perdida."""
        if not isinstance(exc, LoginError):
            return False
        res = self.last_result
        fs = getattr(res, "failure_state", "") if res is not None else ""
        return fs in self._TERMINAL_FAILURE_STATES

    def _is_wrong_password(self, exc: BaseException) -> bool:
        """True p/ 'A senha inserida está incorreta' (failure_state=wrong_password).
        O operador constatou que ISSO é soft-block de IP, não senha errada — então
        tratamos como bounce e trocamos IP+conta (a conta volta ao pool)."""
        if not isinstance(exc, LoginError):
            return False
        res = self.last_result
        if res is not None and getattr(res, "failure_state", "") == "wrong_password":
            return True
        msg = (str(exc) or "").lower()
        return (
            ("senha" in msg and "incorret" in msg)
            or "wrong password" in msg
            or "incorrect password" in msg
        )

    async def _rotate_account_and_proxy_for_retry(
        self, *, discard_current: bool = False
    ) -> bool:
        """IPs esgotados (ou 'senha incorreta'): a CONTA também pode estar
        marcada. Pega OUTRA conta disponível, troca o IP e recria o perfil.

        ``discard_current``: quando a falha foi 'A entrada Senha é inválida'
        confirmada em 2 IPs (= senha genuinamente RUIM, não IP — decisão do
        operador 2026-06-17), DESCARTA a conta (não volta ao pool, nunca mais
        arrendada) em vez de devolvê-la. Bounce SILENCIOSO (pode ser IP) segue a
        regra original: conta volta ao pool. Devolve True só se trocou de conta."""
        if not getattr(self, "use_proxy", True):
            # Manual sem proxy: não troca a conta escolhida pelo operador nem
            # re-arrenda proxy — bounce/senha incorreta é terminal (honesto).
            return False
        from core import store  # noqa: PLC0415

        cur_email = (self.credentials.email_or_phone or "").strip()
        if cur_email:
            self._bad_account_emails.add(cur_email.lower())
            try:
                if discard_current:
                    store.discard_fb_account(cur_email)  # senha ruim → fora do pool
                else:
                    # 0 portfólios nesta tentativa → volta ao pool (available) p/
                    # ser re-tentada depois com um IP bom (bounce pode ser IP).
                    store.settle_fb_account(cur_email, portfolios=0)
            except Exception:  # noqa: BLE001
                pass
        try:
            acc = store.lease_fb_account(exclude_emails=self._bad_account_emails)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rotação de conta: falha ao arrendar conta (%s).", exc)
            return False
        if not acc:
            logger.warning(
                "rotação de conta: pool sem outra conta disponível (excluídas=%d) "
                "— sem retry.", len(self._bad_account_emails),
            )
            return False
        self.credentials = MetaCredentials(
            email_or_phone=acc["email"],
            password=acc.get("password") or "",
            totp_seed=acc.get("totp_seed") or "",
        )
        logger.warning(
            "rotação de CONTA: %s %s; nova conta %s — trocando IP e refazendo o "
            "login.",
            cur_email,
            "DESCARTADA (senha inválida)" if discard_current else "devolvida ao pool",
            acc["email"],
        )
        # Troca o IP também; se não houver IP novo, ainda recria o perfil p/ a
        # conta nova (a recriação já acontece dentro da rotação de IP).
        if not await self._rotate_pool_proxy_for_retry():
            await self._recreate_adspower_profile_for_retry()
        return True

    async def _reacquire_sms_for_retry(self) -> None:
        """Reabastece o número SMS antes de um re-run pós-bounce CDP.

        A compensação da tentativa que falhou SEMPRE cancela (setStatus=8) o
        número pré-alocado em aberto. Se o re-run reusar o mesmo
        ``activation_id``, o passo 5 recebe ``STATUS_CANCEL`` e o código nunca
        chega (produção 2026-06, conta Michael: número 102024091 cancelado na
        1ª falha, reusado morto na 2ª). Como o número precisa ser o MESMO em
        todo o run (comprovante = telefone da org na Meta = quem recebe o SMS),
        re-alocamos AQUI — antes de qualquer passo re-executar — para manter a
        consistência. Best-effort: se falhar, loga e segue (passo 5 cairá no
        fallback manual).
        """
        old = self.preallocated_sms_activation_id
        if not old:
            return  # nada pré-alocado → cada passo aloca sob demanda, ok
        self.preallocated_sms_activation_id = None
        self.preallocated_sms_phone = None
        if not self.sms_client.enabled:
            logger.warning(
                "retry: cliente SMS desabilitado — número %s morto não "
                "re-alocado; passo 5 dependerá de número novo sob demanda.", old,
            )
            return
        try:
            activation_id, phone = await self.sms_client.acquire_number()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "retry: falha ao re-alocar número SMS após cancelamento da "
                "tentativa anterior (%s) — passo 5 pode falhar.", exc,
            )
            return
        self.preallocated_sms_activation_id = activation_id
        self.preallocated_sms_phone = phone
        logger.success(
            "retry: número SMS re-alocado (morto=%s → novo=%s, phone=%s) — "
            "comprovante e verificação usarão o número novo.",
            old, activation_id, phone,
        )

    @staticmethod
    def _should_keep_browser_open(flag_on: bool, reached_bv: bool) -> bool:
        """Decide se mantém navegador + perfil AdsPower abertos após o passo 5.

        Com ``KEEP_BROWSER_OPEN_ON_BV_ERROR`` ON, segura SEMPRE que o passo 5
        foi alcançado — inclusive no SUCESSO (antes só no erro/não-concluído).
        Permite inspecionar a conta recém-criada (ex.: abrir a conta WhatsApp
        Business e ler o ``waba_id`` que a Meta só atribui pós-criação).
        Default OFF → produção fecha tudo normalmente.
        """
        return bool(flag_on and reached_bv)

    async def _run_with_playwright(self, pw: Playwright) -> AutomationResult:
        self._pw = pw
        result = AutomationResult(logged_in=False, used_storage_state=False)
        self.last_result = result  # H1 — observabilidade pós-falha

        # PR4 — saga transacional: rastreia recursos com custo financeiro
        # (número SMS) e os compensa se a esteira falhar em QUALQUER ponto.
        # Snapshot persistido em .state/saga/ para auditoria/recuperação.
        self.saga = PipelineSaga(STATE_DIR / "saga")
        if self.preallocated_sms_activation_id:
            self._track_sms_activation(
                self.preallocated_sms_activation_id,
                detail=(
                    "pré-alocado pela UI "
                    f"(phone={self.preallocated_sms_phone or '?'})"
                ),
            )

        use_adspower = self.adspower_mode in {"create", "reuse"}
        browser = None
        context: Optional[BrowserContext] = None  # pré-declarado pra salvar storage_state no finally
        ws_endpoint: Optional[str] = None
        page: Optional[Page] = None  # pré-declarado p/ autópsia visual
        adspower_started = False  # H1 — só chama stop() se start() foi OK

        try:
            # PR5 — pre-flight (L2): valida o ambiente (credenciais, cookies,
            # egress via proxy, AdsPower, saldo SMS, 2captcha) ANTES de gastar
            # boot de browser. Falha crítica aborta em segundos com causa
            # clara; não-críticas viram warnings estruturados.
            if PREFLIGHT_ENABLED:
                with step(logger, "preflight", result=result, saga=self.saga):
                    report = await run_preflight_checks(self)
                    if not report.ok:
                        raise PreflightError(report.summary())
            else:
                logger.info("Pre-flight desabilitado (PREFLIGHT_ENABLED=false).")

            # Rotação de IP 4G/móvel ANTES de qualquer boot — em modo AdsPower
            # o reset propaga pelo perfil; em modo direto, atinge o proxy do
            # env. Skip silencioso quando PROXY_ROTATION_URL é vazio.
            await self._rotate_proxy_ip()

            if use_adspower:
                try:
                    if self.adspower_mode == "create":
                        await self._resolve_or_create_adspower_profile()
                    if not self.adspower_user_id:
                        raise AdsPowerError(
                            "AdsPower em modo 'reuse' exige Profile ID — campo vazio."
                        )
                    try:
                        payload = await self.adspower.start(
                            self.adspower_user_id, headless=self.headless
                        )
                    except AdsPowerProfileNotFound as exc:
                        # O mapping local aponta para um perfil que não existe
                        # mais no AdsPower (operador resetou/excluiu perfis).
                        # Em modo 'create' com perfil já salvo isso é AUTO-CURÁVEL:
                        # descartamos o registro órfão e recriamos um perfil novo.
                        # Antes disso a run só morria com "AdsPower indisponível".
                        from utils.helpers import only_digits  # noqa: PLC0415
                        from utils.profiles_state import delete_profile, find_profile_by_cnpj  # noqa: PLC0415
                        cnpj_digits = only_digits(
                            (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
                        )
                        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
                        existing = find_profile_by_cnpj(cnpj_digits, provider=_bp)
                        if not (self.adspower_mode == "create" and existing):
                            raise
                        stale_id = self.adspower_user_id
                        logger.warning(
                            "AdsPower: perfil salvo %s sumiu do painel (%s) — "
                            "descartando o mapping do CNPJ %s e recriando um "
                            "perfil novo (auto-cura).",
                            stale_id, exc, cnpj_digits,
                        )
                        delete_profile(cnpj_digits, provider=_bp)
                        self.adspower_user_id = None
                        await self._resolve_or_create_adspower_profile()
                        try:
                            payload = await self.adspower.start(
                                self.adspower_user_id, headless=self.headless
                            )
                        except AdsPowerError as exc2:
                            # Auto-cura recriou o perfil mas o start falhou de novo
                            # (slots esgotados, browser não sobe). Mensagem clara
                            # em vez do AdsPowerError cru lá de baixo.
                            raise AdsPowerError(
                                "Auto-cura do AdsPower falhou: perfil recriado "
                                f"({self.adspower_user_id}) não iniciou — {exc2}"
                            ) from exc2
                    adspower_started = True
                    ws_endpoint = payload["ws_puppeteer"]
                    logger.success(
                        "AdsPower aberto (user_id=%s) — conectando via CDP: %s",
                        self.adspower_user_id, ws_endpoint,
                    )
                    try:
                        browser = await self._connect_browser_cdp(pw, ws_endpoint)
                    except CdpConnectionError as exc:
                        # AdsPower iniciou (o finally fará stop()), mas o CDP não
                        # conectou. Antes a exceção crua do Playwright escapava do
                        # except AdsPowerError (sem classificação nem fallback).
                        if not ADSPOWER_FALLBACK_DIRECT:
                            raise LoginError(str(exc)) from exc
                        logger.warning(
                            "%s — fallback para launch direto.", exc
                        )
                        use_adspower = False
                except AdsPowerError as exc:
                    if not ADSPOWER_FALLBACK_DIRECT:
                        raise InfrastructureError(f"AdsPower indisponível: {exc}") from exc
                    logger.warning(
                        "AdsPower falhou (%s) — fallback para launch direto.", exc,
                    )
                    use_adspower = False

            if browser is None:  # modo direto (sem AdsPower)
                launch_kwargs: dict[str, Any] = {"headless": self.headless}
                # M3 — prioriza ``self.proxy_cfg`` injetado pelo caller;
                # cai nos globals do env quando ausente. Sem race condition
                # entre runners concorrentes.
                effective_proxy = self._effective_proxy_cfg()
                if effective_proxy:
                    launch_kwargs["proxy"] = effective_proxy
                    logger.info(
                        "Proxy aplicado no launch direto: %s",
                        effective_proxy.get("server", "?"),
                    )
                browser = await pw.chromium.launch(**launch_kwargs)

            context, used_state = await self._create_context(
                browser, via_adspower=use_adspower
            )
            result.used_storage_state = used_state
            # Passo 0 — medição de banda SÓ-LOG no CONTEXTO (cobre todas as
            # páginas/navegações/popups). No-op se MEASURE_ASSET_BYTES off.
            self._install_bandwidth_meter(context)
            page = await self._ensure_page(context)
            page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
            page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
            # Mata prompts nativos (notificações Web Push, diálogos) que ficam
            # por cima da página e travam o fluxo — causa de TargetClosedError
            # na espera de 2FA (ver _suppress_native_blockers).
            await self._suppress_native_blockers(context, page)

            # ------------------------------------------------------------------
            # Network sniffer: intercepta respostas do GraphQL / Graph API da
            # Meta para extrair WABA ID e access_token (EAAB...) que trafegam
            # em background durante o fluxo do WhatsApp Business Suite.
            # ------------------------------------------------------------------
            # Parsing das respostas vive em extract_waba_creds_from_response
            # (função pura, testável). A guarda de falso-positivo (waba_id !=
            # business_id) e o slot separado do apiAccessToken moram lá.
            async def intercept_meta_network(response) -> None:
                url = response.url
                if "graphql" not in url and "graph.facebook.com" not in url:
                    return
                # H3 — exceções no sniffer NÃO podem derrubar o bot, mas
                # precisam virar log DEBUG para que regressões de parse não
                # passem batido (sem isso o token desaparece "por magia").
                text = ""
                try:
                    if not (200 <= response.status < 300):
                        return
                    text = await response.text()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "sniffer: leitura de response falhou url=%s err=%s",
                        url, type(exc).__name__,
                    )
                    return
                try:
                    tok, vtok, waba = extract_waba_creds_from_response(
                        text, result.business_id
                    )
                    if result.meta_token is None and tok:
                        result.meta_token = tok
                        logger.info(
                            "[NETWORK] Access Token da Meta capturado "
                            "(prefixo=%s, %d chars).", tok[:4], len(tok),
                        )
                    # Token de escopo Graph (apiAccessToken): melhor candidato p/
                    # consultar a verificação. Slot separado, mesmo com meta_token
                    # já setado pelo payload do WhatsApp.
                    if result.verify_token is None and vtok:
                        result.verify_token = vtok
                        logger.info(
                            "[NETWORK] apiAccessToken (escopo Graph) capturado "
                            "p/ verificação (prefixo=%s, %d chars).",
                            vtok[:4], len(vtok),
                        )
                    if result.waba_id is None and waba:
                        result.waba_id = waba
                        logger.info("[NETWORK] WABA ID capturado: %s", result.waba_id)
                    # Conta de anúncios: só ARMADO durante o passo 4. Fora dele
                    # a página de ativos lista contas PRÉ-EXISTENTES e o
                    # sniffer capturaria uma conta alheia como se fosse a
                    # recém-criada (mesmo tipo de falso-positivo do waba_id).
                    if self._ad_capture_armed and result.ad_account_id is None:
                        act = extract_ad_account_id(
                            text, result.business_id,
                            exclude=self._ad_excluded_ids(),
                        )
                        if act:
                            result.ad_account_id = act
                            logger.info(
                                "[NETWORK] Ad Account ID capturado: %s", act
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "sniffer: parse falhou url=%s err=%s: %s",
                        url, type(exc).__name__, exc,
                    )

            async def intercept_meta_request(request) -> None:
                # Token + wabaId vêm no PAYLOAD do POST 'phone-numbers'
                # (não na resposta). Lê request.post_data e extrai as chaves
                # camelCase. Há 2 requests 'phone-numbers'; só o de payload
                # JSON traz 'token' — o filtro por conteúdo seleciona o certo.
                if result.meta_token is not None and result.waba_id is not None:
                    return
                url = request.url
                if (
                    "phone-numbers" not in url
                    and "graphql" not in url
                    and "graph.facebook.com" not in url
                ):
                    return
                try:
                    body = request.post_data
                except Exception:  # noqa: BLE001
                    body = None
                if not body or ("token" not in body and "waba" not in body.lower()):
                    return
                try:
                    tok, waba = extract_waba_creds_from_payload(body)
                    if result.meta_token is None and tok:
                        result.meta_token = tok
                        logger.info(
                            "[NETWORK] Access Token capturado do PAYLOAD "
                            "(prefixo=%s, %d chars).", tok[:4], len(tok),
                        )
                    if result.waba_id is None and waba:
                        result.waba_id = waba
                        logger.info(
                            "[NETWORK] WABA ID capturado do PAYLOAD: %s", waba,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "sniffer(req): parse falhou url=%s err=%s",
                        url, type(exc).__name__,
                    )

            page.on("request", intercept_meta_request)
            page.on("response", intercept_meta_network)

            # Try the cheap path first: navigate home, see if we're authenticated.
            # IMPORTANTE: o AdsPower guarda os cookies DENTRO do perfil (não há
            # storage_state.json → used_state=False). Sem incluir use_adspower
            # aqui, um perfil AdsPower JÁ LOGADO (ex.: 'Continuar BM' reusando o
            # perfil) caía direto no AuthFSM → login.php → tela de QR/'continuar
            # como' → falha 'campo de e-mail não visível' e a conta boa era
            # marcada ERRO SENHA (produção 2026-09, conta Joyce/Sahiab). Checar
            # a home ANTES pula o login quando a sessão do perfil ainda vale.
            with step(logger, "auth", result=result, saga=self.saga):
                if (used_state or use_adspower) and await self._is_authenticated(
                    page, navigate=True
                ):
                    logger.success(
                        "Sessão restaurada via %s.",
                        "perfil AdsPower" if use_adspower else "storage_state.json",
                    )
                    result.logged_in = True
                else:
                    self._active_page = page
                    await self._perform_login(page, result)
                    if self._active_page is not page:
                        page = self._active_page
                        context = page.context
                        browser = context.browser
                    self._login_attempt_timeout_s = None
                    # LGPD: o FB pode abrir o interstício "Review your data
                    # settings" (/privacy/consent/) LOGO após o login, antes de
                    # a home carregar — o _is_authenticated abaixo veria a tela
                    # de consentimento (estado 'unknown') e falharia o login
                    # (produção 2026-09, conta Elza Zani: login OK no AuthFSM,
                    # mas a re-checagem caiu na tela LGPD). Passa por ele ANTES
                    # de confirmar o login (Get Started → Accept and continue →
                    # Close).
                    await self._dismiss_consent_flow(page)
                    result.logged_in = await self._is_authenticated(page, navigate=False)
                    # O interstício LGPD de BLOQUEIO (lgpd_migrated /
                    # is_new_user_blocking_flow: 'Required: Review Your Data
                    # Settings' → 'Get Started' → 'Activity information from ad
                    # partners'/'Accept and Continue' → 'You're all set'/
                    # 'Close') costuma aparecer com ATRASO — o _dismiss acima
                    # roda cedo demais (home ainda na tela) e o redirect pro
                    # consent vem ~2s depois, então nada o fecha e a
                    # re-checagem cai em 'unknown' e falha o login (produção
                    # 2026-09, conta Romildo). Re-dispensa + re-checa até 3x:
                    # a cada volta o _dismiss sai rápido se não houver tela, ou
                    # clica o wizard inteiro quando ele finalmente aparece.
                    for _consent_attempt in range(3):
                        if result.logged_in:
                            break
                        await self._dismiss_consent_flow(page)
                        result.logged_in = await self._is_authenticated(
                            page, navigate=False
                        )
                        # A home pode não recarregar sozinha após fechar o
                        # consentimento — re-navega e checa (o blocking flow que
                        # reaparecer é pego na próxima volta do _dismiss).
                        if not result.logged_in:
                            result.logged_in = await self._is_authenticated(
                                page, navigate=True
                            )

                if not result.logged_in:
                    # PR1 — antes de falhar, classifica em qual tela o fluxo
                    # parou para que a UI reporte a CAUSA (senha incorreta,
                    # conta suspensa, 2FA pendente…), não só o sintoma.
                    diagnosis = await classify_facebook_state(page)
                    result.failure_state = diagnosis.state.value
                    result.failure_detail = diagnosis.description
                    result.notes.append(f"login: {diagnosis.summary()}")
                    if diagnosis.state in (
                        FacebookState.AUTHENTICATED,
                        FacebookState.SAVE_LOGIN_PROMPT,
                    ):
                        logger.warning(
                            "Página sugere sessão ativa (%s), mas o indicador "
                            "pós-login não confirmou — possível seletor "
                            "POST_LOGIN_INDICATOR desatualizado.",
                            diagnosis.state.value,
                        )
                    raise LoginError(diagnosis.summary())

            # Logo após o login o FB pode forçar o interstício LGPD
            # "Review your data settings" (/privacy/consent/), que trava a
            # navegação pro Business Suite. Passa por ele antes de seguir.
            await self._dismiss_consent_flow(page)

            if getattr(self, "_proxyless_login", False) and self.waba_enabled:
                logger.warning(
                    "Login via IP local (sem proxy) — WABA desabilitada "
                    "para esta run (conta sem proxy não cria WABA).",
                )
                self.waba_enabled = False

            # Aquecimento do perfil (parece humano antes de criar BM) — pulado no
            # login-only. Best-effort: nunca derruba a run.
            if not self.login_only:
                try:
                    await self._warmup_profile(page)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("warmup: ignorado (%s).", exc)

            with step(logger, "locale", result=result, saga=self.saga):
                result.locale_forced = await self._ensure_locale(page)
            # No modo AdsPower o estado é persistido pelo próprio perfil; só
            # gravamos storage_state.json quando estamos em launch direto.
            if not use_adspower:
                await context.storage_state(path=str(self.storage_state_path))
                logger.success("Cookies de sessão salvos em %s", self.storage_state_path)

            if self.login_only:
                # Só logar (pedido do operador): confirma o login, abre o
                # Business Manager e devolve o navegador pronto — nada de
                # empresa/domínio/WhatsApp. Reusa o MESMO caminho do
                # "Assumir" (_mark_assumed) pra herdar de graça todas as
                # guardas já testadas: perfil AdsPower e navegador ficam
                # ABERTOS, conta não é descartada, sem dialog de erro, sem
                # marcar pending_discard — o operador segue à mão daqui.
                logger.success(
                    "LOGIN-ONLY: login confirmado — abrindo business.facebook.com "
                    "e parando aqui (o resto fica com o operador)."
                )
                try:
                    await page.goto(
                        "https://business.facebook.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as exc:  # noqa: BLE001 — login já confirmado, best-effort
                    logger.warning(
                        "LOGIN-ONLY: não consegui abrir business.facebook.com "
                        "sozinho (%s) — login está OK, navegue à mão.", exc,
                    )
                result.final_url = page.url
                self._mark_assumed(result)
                return result
            if self.recon_dump:
                # Modo calibração: percorre todas as telas e despeja DOM +
                # screenshot, sem tocar na esteira normal (que fail-fasta).
                await self._dump_business_recon(page)
            elif self.business_profile is not None:
                await self._run_business_pipeline(page, result)
                # 2 BMs por perfil (pedido do operador 2026-09): repete o
                # pipeline de negócio para os CNPJs EXTRAS na MESMA sessão —
                # mesmo navegador, proxy e login. No-op se não houver extras.
                # Vale inclusive em conta AGED: a criação do 2º portfólio NÃO é
                # bloqueada pelo Meta (contas com 2+ portfólios existem) — o que
                # faltava era abrir a tela de criação, agora feito pela página
                # /select/ ('Criar empresa'). Nunca reusa o portfólio do dono.
                if self.extra_bm_units:
                    await self._run_extra_bms(page, result)
            else:
                logger.info("BusinessProfile ausente — etapas pós-login puladas.")

            result.final_url = page.url
            # Recursos OPEN num run de SUCESSO = nunca consumidos (limite de
            # portfólio, business_profile ausente, etapa SMS pulada) —
            # cancela para estornar em vez de deixar o crédito órfão.
            await self._release_orphan_sms(result)
            self.saga.finish(success=True)
            return result
        except BaseException as exc:  # noqa: BLE001
            # BaseException (não Exception): CancelledError/KeyboardInterrupt
            # também precisam compensar o crédito SMS — eram exatamente os
            # casos (operador clica parar / SIGTERM) que furavam a rede.
            # Autópsia visual: captura screenshot full-page + HTML bruto
            # ANTES de o browser fechar no finally. Não suprime a exceção —
            # apenas anexa evidência ao filesystem e propaga.
            # ERRO real (qualquer Exception) → marca run_failed: o finally apaga
            # o perfil. CancelledError/KeyboardInterrupt (parada do processo) NÃO
            # marcam — não queremos apagar perfis num shutdown do servidor.

            # Guarda o erro pra exibir no dialog + classificar depois
            self._last_error = str(exc) if exc else "Erro desconhecido"
            self._last_exc = exc  # Guarda exception object pra classificar (terminal vs transitório)

            # ── ASSUMIR: para tudo AQUI. Sem screenshot de autópsia, sem
            # compensar SMS (o operador pode precisar do código), sem renomear
            # perfil. O ``finally`` devolve o resultado sem fechar nada.
            if self._assume_requested() or (
                isinstance(exc, RunStopRequested)
                and getattr(exc, "mode", "") == run_control.ASSUME
            ):
                self._mark_assumed(result)
                logger.warning(
                    "Run ASSUMIDA pelo operador — bot parado imediatamente; "
                    "navegador e perfil AdsPower mantidos como estão."
                )
                raise

            if self._close_requested():
                # Operador encerrou: não é falha do bot a diagnosticar, e o
                # finally fecha/apaga porque FOI ELE quem pediu.
                self._mark_terminated(result)
                logger.warning(
                    "Run ENCERRADA pelo operador — parando sem re-tentar nada."
                )
                raise

            if isinstance(exc, Exception):
                result.run_failed = True
            if isinstance(exc, RunStopRequested):
                # Parada pedida pelo operador (Encerrar/Assumir): não é falha do
                # bot — sem screenshot de autópsia. O finally fecha o perfil se
                # 'terminate' (force_close_profile) ou mantém aberto se 'assume'.
                logger.warning(
                    "Run %s pelo operador — encerrando o pipeline (%s).",
                    "ENCERRADA" if getattr(result, "terminated", False) else "ASSUMIDA",
                    exc,
                )
            elif isinstance(exc, Exception) and page is not None:
                await self._dump_failure_evidence(page, exc)
            elif isinstance(exc, Exception):
                # Inclui o TIPO: timeouts httpx têm str vazia e o log saía
                # 'interrompido antes do boot: ' sem causa nenhuma (visto em
                # produção 2026-06 — era httpx no start do AdsPower).
                logger.error(
                    "[ERROR] Pipeline interrompido antes do boot da page "
                    "(%s: %s) — sem evidência visual disponível.",
                    type(exc).__name__, exc or "sem detalhe",
                )
            else:
                logger.warning(
                    "Pipeline cancelado/interrompido (%s) — executando "
                    "compensações antes de propagar.", type(exc).__name__,
                )
            # PR4 — compensa recursos financeiros em aberto (ex.: número SMS
            # pré-alocado que nunca chegou à etapa 4/4). Best-effort: nunca
            # mascara a exceção raiz. shield protege a compensação de um
            # SEGUNDO cancel durante o próprio await.
            # Se o run NÃO foi 100% concluído, mantemos o browser aberto p/
            # intervenção manual — NÃO cancelamos o SMS pois o operador pode
            # precisar receber o código p/ concluir manualmente.
            keep_open_intent = not (
                bool(result.business_id)
                and result.business_details_saved
                and result.domain_verified
                and bool(result.waba_id)
                and bool(result.meta_token)
                and result.business_verification_started
            )

            # Se completou 100%, renomeia o perfil com tag "analise"
            if not keep_open_intent:  # 100% completo
                try:
                    if self.adspower and self.adspower_user_id:
                        old_name = getattr(self, "_profile_name", "unknown")
                        new_name = build_tagged_profile_name(old_name, "ANALISE")
                        await self.adspower.update_profile_remark(
                            self.adspower_user_id, new_name
                        )
                        logger.success(
                            "Perfil AdsPower renomeado com tag ANALISE: %s → %s",
                            old_name, new_name
                        )
                except Exception as rename_err:
                    logger.warning("Erro ao renomear perfil com ANALISE: %s", rename_err)
            if self.saga is not None and not keep_open_intent:
                try:
                    await asyncio.shield(self.saga.compensate_pending(exc))
                except BaseException:  # noqa: BLE001
                    logger.error(
                        "Compensação interrompida no meio — confira o snapshot "
                        "em %s para recuperação manual.", self.saga.state_path,
                    )
            raise
        finally:
            # Passo 0 — relatório de banda do run (no-op se MEASURE_ASSET_BYTES off).
            self._flush_bandwidth_report()

            # ── ASSUMIR (botão do painel): PARA TUDO E NÃO MEXE EM NADA.
            # Nada de dialog na página, de descarte de conta, de exclusão do
            # perfil AdsPower, de fechar o navegador ou de salvar/apagar
            # storage_state. O ``return`` aqui encerra a run devolvendo o
            # resultado parcial (engole a exceção/cancelamento em curso, que
            # é justamente o pedido de parada do operador).
            if bool(getattr(result, "assumed", False)) or self._assume_requested():
                self._mark_assumed(result)
                # Marca o NOME do perfil com "ASSUMIDO - <cnpj>" p/ achar no
                # painel AdsPower (sem fechar/excluir nada). Não sobrescreve
                # um perfil que já chegou em ANALISE.
                if not (self.adspower_profile_name or "").startswith("ANALISE -"):
                    try:
                        await self._rename_profile_result("ASSUMIDO")
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning(
                    "ASSUMIR: bot encerrado a pedido do operador — navegador e "
                    "perfil AdsPower (%s) MANTIDOS ABERTOS; nada foi fechado, "
                    "excluído ou descartado.", self.adspower_user_id or "?",
                )
                return result

            # MARCAR CONTA COMO PENDENTE DE DISCARD SE FALHOU
            # Qualquer erro que não deu 100% vai pra aba "Descartes Pendentes"
            _run_100pct_check = (
                result.all_steps_ok()
                and result.sms_verified
                and all(bm.get("complete") for bm in result.extra_bms)
            )

            # CONTAGEM DO LIMITE (cap) — só quando a run conclui 100%. Run que
            # falhou no meio NÃO consome o limite do domínio: o slot continua
            # disponível (pedido do operador: 15 de limite, 2 ok + 3 com bug →
            # restam 13). Best-effort: nunca derruba o fechamento da run.
            if _run_100pct_check and result.site_deployed:
                try:
                    from core import store as _store_cap  # noqa: PLC0415
                    from core.netlify_deployer import split_subdomain as _split_sub  # noqa: PLC0415
                    _dom = (self.business_profile.domain or "").strip().lower()
                    _cnpj = re.sub(r"\D+", "", (self.cnpj_data.cnpj or ""))
                    if _dom and "." in _dom:
                        _slug, _base = _split_sub(_dom)
                        if self.hosting_provider == "cloudflare":
                            _dtmpl = _store_cap.get_setting("default_template") or "corporate"
                            _store_cap.cloudflare_domain_upsert_site_entry(
                                _base, _slug, _cnpj,
                                result.meta_verification_token or "", _dom,
                                template=_dtmpl,
                            )
                            _used = _store_cap.cloudflare_domain_count_sites(_base)
                            _row = _store_cap.get_cloudflare_domain(_base)
                        else:
                            _store_cap.netlify_upsert_site_entry(
                                _base, _slug, _cnpj,
                                result.meta_verification_token or "", _dom,
                            )
                            _used = _store_cap.netlify_count_sites(_base)
                            _row = _store_cap.get_netlify_domain(_base)
                        _cap = _row.get("cap") if _row else None
                        logger.info(
                            "Netlify: run 100%% concluída — %s CNPJ(s) contado(s) no "
                            "domínio %s%s.",
                            _used, _base,
                            f" (cap {_cap}, {max(0, _cap - _used)} restante(s))" if _cap else "",
                        )
                except Exception as _exc:  # noqa: BLE001 — contador é best-effort
                    logger.debug("contagem netlify (100%%) falhou: %s", _exc)

            if not _run_100pct_check and not getattr(result, "account_discarded", False):
                # Run falhou — marca conta como PENDING_DISCARD APENAS se for erro TERMINAL
                # Erros terminais: soft-block, senha incorreta, preflight crítico
                # Erros transitórios: campo não preenchido, domínio já verificado, infraestrutura, UI, etc → permite retry

                # Classifica erro: True = terminal (descarta), False = transitório (permite retry)
                # EXCEÇÕES: DomainAlreadyClaimedError, InfrastructureError, UIError são LoginError mas NÃO descartam
                # - DomainAlreadyClaimedError: culpa do CNPJ/subdomínio, pode reutilizar com outro CNPJ
                # - InfrastructureError: culpa da infra (SMS, AdsPower, rede), pode reutilizar depois
                # - UIError: botão/campo não encontrado, timeout de carregamento, glitch transitório
                #
                # PreflightError NÃO descarta: o pré-flight roda ANTES do login
                # (checa AdsPower/proxy/SMS/Cloudflare/relógio) — uma falha aqui é
                # de infra/config, NUNCA prova que a conta é ruim. Ex.: AdsPower
                # fechado mandava a conta boa pro Descartes injustamente.
                is_terminal_error = isinstance(self._last_exc, LoginError)
                is_non_discardable = isinstance(self._last_exc, (DomainAlreadyClaimedError, InfrastructureError, UIError))
                is_terminal_error = is_terminal_error and not is_non_discardable

                email = (self.credentials.email_or_phone or "").strip() if self.credentials else ""
                if email and is_terminal_error:
                    try:
                        from core import store
                        error_reason = str(getattr(self, "_last_error", result.failure_detail or "Erro desconhecido"))[:100]
                        store.mark_account_pending_discard(email, error_reason)
                        logger.info(
                            "✓ Conta %s marcada como PENDING_DISCARD (motivo TERMINAL: %s)",
                            email, error_reason
                        )
                    except Exception as e:
                        logger.warning("Erro ao marcar conta pending_discard: %s", e)
                elif email and (not is_terminal_error or is_non_discardable):
                    logger.info(
                        "⚠️  Run falhou mas erro é TRANSITÓRIO — conta %s NÃO descartada. "
                        "Navegador aberto para retry com 'Preencher Empresa', novo CNPJ ou posterior (motivo: %s)",
                        email, getattr(self, "_last_error", result.failure_detail or "?")[:80]
                    )

                # Marca o NOME do perfil AdsPower com o motivo da falha
                # ("ERRO <TAG> - <cnpj>", ex.: 'ERRO SENHA - 123') p/ o operador
                # identificar no painel por que morreu. NÃO sobrescreve perfil
                # que já chegou em ANALISE (submeteu verificação à Meta).
                if not (self.adspower_profile_name or "").startswith("ANALISE -"):
                    try:
                        await self._rename_profile_result(f"ERRO {self._error_tag()}")
                    except Exception:  # noqa: BLE001
                        pass

            # FECHA o perfil AdsPower quando:
            # 1) Run 100% concluída → sempre fecha
            # 2) Piloto automático LIGADO + erro → fecha p/ liberar slot
            # 3) Operador clicou "Encerrar"
            # 4) Todas as BMs vieram com cadeado
            # Caso contrário, mantém aberto p/ intervenção manual.
            _run_100pct = _run_100pct_check
            _encerrou = self._close_requested()
            _autopilot_on = load_autopilot().get("enabled", False)
            keep_open = True
            if _run_100pct:
                keep_open = False
                logger.info(
                    "Run 100%% concluída — perfil AdsPower (%s) será fechado.",
                    self.adspower_user_id,
                )
            if _autopilot_on and not _run_100pct:
                keep_open = False
                logger.info(
                    "AUTOPILOT: run com erro — perfil AdsPower (%s) será "
                    "fechado para liberar slot ao próximo da fila.",
                    self.adspower_user_id,
                )
            if _encerrou:
                keep_open = False
                logger.info(
                    "GUARD: 'Encerrar' do operador — perfil AdsPower (%s) será "
                    "FECHADO (foi ele quem pediu).", self.adspower_user_id,
                )
            if getattr(result, "bm_locked", False):
                keep_open = False
                logger.info(
                    "CADEADO: todas as BMs vieram com cadeado — perfil AdsPower "
                    "(%s) será FECHADO (não tem o que fazer manual).",
                    self.adspower_user_id,
                )
            if keep_open:
                # Mostra dialog interativo perguntando se quer voltar 1 passo
                # (só chega aqui com autopilot OFF e run não-100%)
                if page is not None:
                    try:
                        from core.error_dialog import show_error_dialog_before_close
                        from core.step_checkpoint import get_last_completed_step

                        error_str = str(getattr(self, "_last_error", "Erro desconhecido"))
                        current_step = getattr(result, "step", "?") or "?"
                        last_step = get_last_completed_step(self.run_id) if self.run_id else None

                        logger.info(
                            f"Mostrando dialog de erro — passo atual={current_step}, "
                            f"último completado={last_step}"
                        )

                        wants_retry = await show_error_dialog_before_close(
                            page,
                            error_msg=error_str[:500],
                            step_name=current_step
                        )

                        if wants_retry and last_step and self.run_id:
                            logger.info(
                                f"Dialog: operador escolheu TENTAR DE NOVO — "
                                f"voltando de '{current_step}' para '{last_step}'"
                            )
                            from core.step_checkpoint import delete_steps_after
                            from core import run_control

                            delete_steps_after(self.run_id, last_step)
                            run_control.request_resume(self.run_id)
                            keep_open = True
                        else:
                            logger.info("Dialog: operador escolheu FECHAR")

                    except Exception as dialog_err:
                        logger.warning(f"Erro ao mostrar dialog: {dialog_err}")

                logger.warning(
                    "GUARD: run não concluído — navegador e perfil AdsPower (%s) "
                    "MANTIDOS ABERTOS p/ intervenção manual do operador. "
                    "Feche o perfil no painel AdsPower ao terminar.",
                    self.adspower_user_id,
                )
            # EXCLUI o perfil do AdsPower em runs que não deram certo — EXCETO
            # se estiver em "ANALISE" (analisando pelo Meta). Pedido do operador:
            # no painel do AdsPower só ficam as 'em análise' (ANALISE -) e sucesso.
            # NÃO apaga as ASSUMIDAS (operador vai concluir à mão).
            # delete_profile fecha o perfil antes de apagar.

            # Verifica se o perfil está em ANALISE (nome começa com "ANALISE -")
            profile_name = (self.adspower_profile_name or "").strip()
            is_in_analysis = profile_name.startswith("ANALISE -")

            # SÓ o "Encerrar" do operador apaga perfil. Erro de login, selfie
            # ou conta descartada: o perfil FICA no AdsPower para ele decidir.
            _should_delete = (
                use_adspower
                and self.adspower_user_id
                and _encerrou
                and not getattr(result, "assumed", False)
                and not is_in_analysis  # ← NÃO deleta se estiver em ANALISE
            )
            if _should_delete:
                try:
                    ok = await self.adspower.delete_profile(self.adspower_user_id)
                    self._profile_deleted = bool(ok)
                    logger.info(
                        "AdsPower: perfil %s da run com erro — exclusão %s.",
                        self.adspower_user_id, "OK" if ok else "FALHOU (ver log acima)",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("AdsPower: falha ao excluir perfil pós-erro: %s", exc)
            elif is_in_analysis and (getattr(result, "run_failed", False) or getattr(result, "profile_error_tag", None)):
                # Se está em ANALISE mas teve erro, apenas loga (mantém para análise)
                logger.info(
                    "AdsPower: perfil %s em ANALISE — mantido no painel (não será deletado).",
                    self.adspower_user_id,
                )
            # SALVAR STORAGE STATE ANTES DE FECHAR BROWSER
            # Assim, próximas runs reutilizam a sessão (cookies) da conta
            # MAS: NÃO salvar pra contas descartadas (evita reutilizar contas ruins)
            account_discarded = getattr(result, "account_discarded", False)
            run_success = getattr(result, "run_success", False) or result.all_steps_ok()

            if context is not None and run_success and not account_discarded:
                # Run 100% bem-sucedida: SALVAR cache
                try:
                    storage = await context.storage_state()
                    self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                    import json
                    with open(self.storage_state_path, "w") as f:
                        json.dump(storage, f)
                    logger.info("✓ storage_state salvo (run 100%%) — sessão será reutilizada.")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Falha ao salvar storage_state: %s", exc)
            elif account_discarded:
                # Conta descartada: deletar storage_state antigo (evita reutilizar conta ruim)
                try:
                    if self.storage_state_path.exists():
                        self.storage_state_path.unlink()
                        logger.info("✗ storage_state deletado (conta descartada).")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Falha ao deletar storage_state: %s", exc)

            # Em modo CDP, NÃO chamamos browser.close() — apenas desconectamos
            # e pedimos ao AdsPower para fechar o perfil (libera o slot).
            if browser is not None and not keep_open:
                try:
                    if use_adspower:
                        await browser.close()  # encerra apenas a sessão CDP
                    else:
                        await browser.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Falha ao fechar browser: %s", exc)
            # Só pede ao AdsPower para fechar o perfil se ele FOI aberto com
            # sucesso. Caso contrário, o stop bate em "User_id is not open" e
            # polui o log sem necessidade.
            if (use_adspower and adspower_started and self.adspower_user_id
                    and not keep_open and not getattr(self, "_profile_deleted", False)):
                await self.adspower.stop(self.adspower_user_id)

    # ------------------------------------------------------------------
    # PR4 — compensação de crédito SMS
    # ------------------------------------------------------------------
    def _track_sms_activation(self, activation_id: str, *, detail: str) -> None:
        """Registra a ativação SMS na saga com compensação de cancelamento."""
        if self.saga is None:
            return
        self.saga.track_resource(
            "sms_activation",
            activation_id,
            detail=detail,
            compensate=lambda: self._cancel_sms_activation(activation_id),
        )

    async def _release_orphan_sms(self, result: AutomationResult) -> None:
        """Cancela ativações SMS OPEN ao fim de um run BEM-SUCEDIDO.

        OPEN no sucesso significa que a etapa 4/4 nunca consumiu o número
        (limite de portfólio, perfil ausente, etapa pulada). Exceção: se o
        SMS foi verificado mas o release falhou, NÃO cancela um número já
        usado — só sinaliza para auditoria manual.
        """
        if self.saga is None:
            return
        for res in list(self.saga.open_resources):
            if res.kind != "sms_activation":
                continue
            if result.sms_verified:
                logger.warning(
                    "Ativação %s permanece OPEN após sucesso com SMS "
                    "verificado (release falhou?) — auditoria manual em %s.",
                    res.ref, self.saga.state_path,
                )
                continue
            try:
                await self._cancel_sms_activation(res.ref)
                self.saga.settle_resource(
                    "sms_activation", res.ref,
                    outcome="cancelado: não consumido no run",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Falha ao cancelar ativação órfã %s: %s — recuperação "
                    "manual em %s.", res.ref, exc, self.saga.state_path,
                )

    async def _cancel_sms_activation(self, activation_id: str) -> None:
        """Compensação: cancela a ativação no provedor (estorno parcial).

        Levanta quando o provedor NÃO confirma — assim a saga marca o
        recurso como ``compensation_failed`` (recuperação manual) em vez
        de fingir que compensou.
        """
        ok = await self.sms_client.set_status(
            activation_id, Sms24hClient.STATUS_CANCEL
        )
        if not ok:
            raise RuntimeError(
                f"setStatus=8 não confirmado pelo provedor para ativação "
                f"{activation_id} — verifique o painel sms24h.org"
            )
        logger.warning(
            "Compensação SMS executada: ativação %s cancelada (setStatus=%s).",
            activation_id, Sms24hClient.STATUS_CANCEL,
        )

    @staticmethod
    async def _dump_failure_evidence(page: Page, exc: BaseException) -> None:
        """Salva screenshot full-page + dump HTML antes do browser fechar.

        Best-effort: cada captura tem seu próprio try/except para que falha
        em uma não impeça a outra (ex.: screenshot pode falhar em CDP modo
        AdsPower se o tab estiver detached, mas o ``page.content()`` ainda
        funcionar). O carimbo de tempo Unix garante nomes únicos em runs
        concorrentes.
        """
        # uuid evita colisão de evidência entre runs paralelos no mesmo segundo.
        stamp = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        shot_path = SCREENSHOTS_DIR / f"fail_{stamp}.png"
        html_path = STATE_DIR / f"fail_{stamp}.html"

        try:
            await page.screenshot(path=str(shot_path), full_page=True)
            logger.info("Screenshot de falha salvo: %s", shot_path)
        except Exception as inner:  # noqa: BLE001
            logger.warning("Falha ao gerar screenshot da falha: %s", inner)

        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
            html_path.write_text(html, encoding="utf-8")
            logger.info("HTML da falha salvo: %s (%d bytes)", html_path, len(html))
        except Exception as inner:  # noqa: BLE001
            logger.warning("Falha ao salvar HTML da falha: %s", inner)

        logger.error(
            "[ERROR] Pipeline interrompido (%s: %s). "
            "Evidências visuais salvas em %s/",
            type(exc).__name__, exc, SCREENSHOTS_DIR,
        )

    @staticmethod
    def _slugify(text: str) -> str:
        """Slug curto e seguro p/ nome de arquivo (a-z0-9 + hífen)."""
        s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        return s[:40] or "x"

    async def _recon_open_label(self, page: Page, base_url: str) -> str:
        """Rótulo do editor que abriu: heading do diálogo, ou cauda da URL.

        Serve pra nomear o dump pelo que de FATO abriu (ex.: 'editar-endereco',
        'editar-nome-de-contato') — o 'Editar' por índice antes caía sempre no
        diálogo do portfólio sem a gente saber qual seção era.
        """
        try:
            dlg = page.locator('[role="dialog"]').first
            if await dlg.is_visible(timeout=1500):
                heading = dlg.get_by_role("heading").first
                if await heading.is_visible(timeout=1000):
                    return self._slugify(await heading.inner_text())
        except Exception:  # noqa: BLE001
            pass
        try:
            if page.url and page.url != base_url:
                tail = page.url.rstrip("/").split("/")[-1].split("?")[0]
                return self._slugify(tail) or "route"
        except Exception:  # noqa: BLE001
            pass
        return "noheading"

    async def _recon_capture(self, page: Page, label: str) -> None:
        """Salva DOM + screenshot da tela atual (best-effort, modo recon)."""
        stamp = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
            (STATE_DIR / f"recon_{label}_{stamp}.html").write_text(
                html, encoding="utf-8"
            )
            logger.success("recon[%s]: DOM salvo (%d bytes).", label, len(html))
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon[%s]: falha ao salvar DOM (%s).", label, exc)
        try:
            await page.screenshot(
                path=str(SCREENSHOTS_DIR / f"recon_{label}_{stamp}.png"),
                full_page=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon[%s]: falha no screenshot (%s).", label, exc)

    async def _dump_business_recon(self, page: Page) -> None:
        """Percorre TODAS as telas de negócio salvando DOM+screenshot.

        Objetivo: calibração rápida. Um run rende o DOM de overview,
        detalhes da empresa (+cada diálogo 'Editar') e domínios — sem
        preencher, sem submeter, sem fail-fast. Cada passo é isolado em
        try/except para que uma tela quebrada não impeça as seguintes.
        """
        logger.info("=== MODO RECON: capturando DOM de todas as telas ===")

        # 1) Overview + detecção de business_id (cola nas URLs de settings).
        business_id: Optional[str] = None
        try:
            await page.goto(BUSINESS_OVERVIEW_URL, wait_until="domcontentloaded")
            await self._organic_wait()
            await self._ensure_business_session(page, target_url=BUSINESS_OVERVIEW_URL)
            business_id = await self._detect_existing_business_id(page)
            logger.info("recon: business_id detectado=%s", business_id)
            await self._recon_capture(page, "overview")
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon: overview falhou (%s).", exc)

        # 2) Detalhes da empresa (settings/info) — tela em modo LEITURA, com
        #    editores POR SEÇÃO (Detalhes da empresa / Endereço / Informações
        #    de contato). O 'Editar' genérico por índice caía SEMPRE no diálogo
        #    do PORTFÓLIO (nome+página) — os campos razão social/endereço/
        #    telefone vivem noutros editores. Aqui: (a) expande blocos ocultos,
        #    (b) enumera Editar/Adicionar/Atualizar, (c) abre cada um e rotula o
        #    dump pelo HEADING do editor que de fato abriu, (d) recupera
        #    (go_back se virou rota; senão Escape).
        try:
            info_url = self._with_business_id(BUSINESS_INFO_URL, business_id)
            await page.goto(info_url, wait_until="domcontentloaded")
            await self._organic_wait()
            await self._ensure_business_session(page, target_url=info_url)
            await self._recon_capture(page, "info_read")
            base_url = page.url

            # Revela editores escondidos atrás de 'Mostrar mais opções'/'Ver mais'.
            for exp_re in (r"mostrar mais op[cç][oõ]es", r"ver mais", r"show more"):
                try:
                    exp = page.get_by_text(re.compile(exp_re, re.I)).first
                    if await exp.is_visible(timeout=1500):
                        await exp.click(timeout=5000)
                        await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass

            edit_re = re.compile(
                r"editar|^edit$|edit\b|adicionar|^add$|atualizar|update", re.I
            )
            edit_btns = page.get_by_role("button", name=edit_re).or_(
                page.get_by_role("link", name=edit_re)
            )
            try:
                count = await edit_btns.count()
            except Exception:  # noqa: BLE001
                count = 0
            logger.info(
                "recon: %d afordância(s) de edição (Editar/Adicionar/Atualizar) "
                "na tela de detalhes.",
                count,
            )
            for i in range(min(count, 12)):
                slug = f"edit_{i}"
                try:
                    btn = edit_btns.nth(i)
                    if not await btn.is_visible(timeout=1500):
                        continue
                    try:
                        label = (await btn.inner_text()).strip()[:30]
                    except Exception:  # noqa: BLE001
                        label = ""
                    await btn.scroll_into_view_if_needed(timeout=5000)
                    await btn.click(timeout=5000)
                    await self._organic_wait()
                    opened = await self._recon_open_label(page, base_url)
                    slug = f"edit_{i}_{self._slugify(label)}__{opened}"
                    await self._recon_capture(page, f"info_{slug}")
                    # Recupera o estado base p/ a próxima afordância.
                    if page.url != base_url:
                        await page.go_back(wait_until="domcontentloaded")
                    else:
                        await page.keyboard.press("Escape")
                    await self._organic_wait()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("recon: edição #%d (%s) falhou (%s).", i, slug, exc)
                    try:
                        if page.url != base_url:
                            await page.goto(base_url, wait_until="domcontentloaded")
                        else:
                            await page.keyboard.press("Escape")
                        await self._organic_wait()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon: detalhes da empresa falhou (%s).", exc)

        # 3) Domínios / segurança da marca.
        try:
            domains_url = self._with_business_id(BUSINESS_DOMAINS_URL, business_id)
            await page.goto(domains_url, wait_until="domcontentloaded")
            await self._organic_wait()
            await self._ensure_business_session(page, target_url=domains_url)
            await self._recon_capture(page, "domains")
            # Tenta abrir o fluxo 'Adicionar' para capturar o input do domínio.
            try:
                add_btn = page.get_by_role(
                    "button", name=re.compile(r"adicionar|add", re.I)
                ).first
                if await add_btn.is_visible(timeout=4000):
                    await add_btn.click()
                    await self._organic_wait()
                    await self._recon_capture(page, "domains_add")
            except Exception as exc:  # noqa: BLE001
                logger.warning("recon: abrir 'Adicionar domínio' falhou (%s).", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon: domínios falhou (%s).", exc)

        logger.success(
            "=== MODO RECON concluído — DOMs em .state/recon_*.html, "
            "screenshots em .state/screenshots/recon_*.png ==="
        )

    @staticmethod
    async def _dump_debug_html(page: Page, tag: str) -> None:
        """Salva o HTML atual em .state/ para diagnóstico (best-effort).

        Diferente de ``_dump_failure_evidence`` (que só dispara quando o
        pipeline levanta exceção), serve para capturar telas que NÃO
        crasham — ex.: 2FA que cai em intervenção manual — e que de outra
        forma somem quando o operador segue ou o browser fecha. O ``tag``
        nomeia o arquivo (``debug_<tag>_<stamp>.html``).
        """
        stamp = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        html_path = STATE_DIR / f"debug_{tag}_{stamp}.html"
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
            html_path.write_text(html, encoding="utf-8")
            logger.info(
                "HTML de diagnóstico salvo: %s (%d bytes)", html_path, len(html)
            )
        except Exception as inner:  # noqa: BLE001
            logger.warning("Falha ao salvar HTML de diagnóstico (%s): %s", tag, inner)

    @staticmethod
    async def _rotate_proxy_ip() -> None:
        """Dispara a URL de rotação do modem 4G / proxy móvel e aguarda.

        Best-effort: se ``PROXY_ROTATION_URL`` estiver vazio, retorna
        silenciosamente (rota desligada). Se a chamada HTTP falhar, loga
        warning e segue — o pipeline NÃO é abortado por falha de rotação
        (o IP antigo provavelmente ainda funciona; o reset é "soft hardening").
        """
        if not PROXY_ROTATION_URL:
            return
        logger.info("Rotacionando IP via %s …", PROXY_ROTATION_URL)
        try:
            async with httpx.AsyncClient(timeout=PROXY_ROTATION_TIMEOUT_S) as client:
                r = await client.get(PROXY_ROTATION_URL)
                # Aceita 2xx e 3xx — alguns modems devolvem 302 após reset.
                if r.status_code >= 400:
                    logger.warning(
                        "Reset 4G devolveu HTTP %s — seguindo sem garantia de novo IP.",
                        r.status_code,
                    )
                else:
                    logger.success("Reset 4G OK (HTTP %s).", r.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Falha na rotação de IP: %s — seguindo sem reset.", exc)
            return
        logger.info(
            "Cooldown de %.0fs para o modem renegociar IP…",
            PROXY_ROTATION_COOLDOWN_S,
        )
        await asyncio.sleep(PROXY_ROTATION_COOLDOWN_S)

    # ------------------------------------------------------------------
    # Business pipeline orchestration
    # ------------------------------------------------------------------
    async def _capture_step_failure(self, page: Page, label: str) -> None:
        """Salva DOM + screenshot de um passo que falhou (modo calibração)."""
        tag = "stepfail_" + label.split()[0].replace("/", "_")
        await self._dump_debug_html(page, tag)
        try:
            stamp = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
            await page.screenshot(
                path=str(SCREENSHOTS_DIR / f"{tag}_{stamp}.png"), full_page=True
            )
        except Exception:  # noqa: BLE001
            pass

    # Exceções que NÃO devem ser re-tentadas (erros terminais de negócio).
    _NON_RETRYABLE = (
        RunStopRequested,
        LoginError,
        PreflightError,
        AccountDiscardError,
        PortfolioLimitError,
        BusinessDataQueryError,
        DomainNotVerifiedError,
        BusinessDetailsError,
        PortfolioIdCaptureError,
    )

    def _persist_profile_to_run(self) -> None:
        """Grava user_id + serial do perfil AdsPower no registro da run AGORA
        (logo após criar/reusar) — não só no finally. Assim o painel mostra o
        NÚMERO do perfil durante a run, antes de finalizar (pedido do operador)."""
        rid = getattr(self, "run_id", None)
        if not rid:
            return
        try:
            from core import store  # noqa: PLC0415
            store.update_run(
                rid,
                profile_user_id=self.adspower_user_id,
                profile_serial_number=self.adspower_serial_number,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _mark_profile_error(self, tag: str) -> None:
        """Marca o NOME do perfil AdsPower com o motivo do erro (ex.: 'SENHA',
        'SELFIE', 'LOGIN', 'RESTRITA') antes de fechá-lo — pedido do operador
        2026-06 p/ identificar no painel quais perfis morreram e por quê.

        Best-effort: nunca levanta. Acrescenta '[ERRO <tag> <dd/mm HH:MM>]' ao
        nome atual (busca o nome se não estiver em cache); idempotente (não
        duplica a marca)."""
        if not (self.adspower_user_id and getattr(self, "adspower", None)):
            return
        base = (self.adspower_profile_name or "").strip()
        if not base:
            try:
                info = await self.adspower.get_profile_info(self.adspower_user_id)
                base = str((info or {}).get("name") or "").strip()
            except Exception:  # noqa: BLE001
                base = ""
        from .adspower_client import build_tagged_profile_name  # noqa: PLC0415
        # Marca SEMPRE como PREFIXO (antes do nome), ex.: "[ERRO SENHA 22/06 …] nome".
        new_name = build_tagged_profile_name(base, f"ERRO {tag}")
        try:
            ok = await self.adspower.update_profile(self.adspower_user_id, name=new_name)
            logger.info(
                "AdsPower: perfil marcado com erro no nome (%s) → %s",
                tag, "ok" if ok else "recusado",
            )
            if ok:
                self.adspower_profile_name = new_name
        except Exception as exc:  # noqa: BLE001
            logger.debug("AdsPower: falha ao marcar nome do perfil (%s).", exc)

    def _error_tag(self) -> str:
        """Classifica o motivo da falha num rótulo curto p/ o nome do perfil
        (ex.: 'SENHA', 'SELFIE', 'SOFTBLOCK', 'DOMINIO'). Espelha a lógica de
        getFailureReason do painel. Best-effort: default 'GERAL'."""
        res = getattr(self, "last_result", None)
        if getattr(res, "bm_locked", False):
            return "CADEADO"
        e = str(
            getattr(self, "_last_error", "")
            or getattr(res, "failure_detail", "")
            or ""
        ).lower()
        exc = getattr(self, "_last_exc", None)
        if "senha" in e or "password" in e or "incorreta" in e or "login bounced" in e:
            return "SENHA"
        if "selfie" in e or "identidade" in e:
            return "SELFIE"
        if "restrita" in e or "restricted" in e:
            return "RESTRITA"
        if "suspensa" in e or "suspended" in e or "disabled" in e or "desativ" in e:
            return "DESATIVADA"
        if "soft-block" in e or "soft block" in e or "bounced" in e:
            return "SOFTBLOCK"
        if "captcha" in e:
            return "CAPTCHA"
        if "2fa" in e or "two-factor" in e or "two factor" in e:
            return "2FA"
        if "dominio" in e or "domínio" in e or "domain" in e or "nameserver" in e:
            return "DOMINIO"
        if getattr(self, "_sms_erro2", False):
            return "SMS/ERRO2"
        if "sms" in e:
            return "SMS"
        if "timeout" in e or "locator" in e or "playwright" in e:
            return "TIMEOUT"
        if "preflight" in e or isinstance(exc, PreflightError):
            return "PREFLIGHT"
        return "GERAL"

    async def _rename_profile_result(self, label: str) -> None:
        """Renomeia o perfil AdsPower p/ ``<label> - <cnpj>`` (mesmo estilo do
        'ANALISE - <cnpj>'), ex.: 'ASSUMIDO - 123', 'ERRO SENHA - 123'. Usado
        p/ marcar no painel o desfecho de runs que NÃO deram 100%. Best-effort:
        nunca levanta."""
        if not (self.adspower_user_id and getattr(self, "adspower", None)):
            return
        try:
            cnpj_digits = self._profile_cnpj_digits()
        except Exception:  # noqa: BLE001
            cnpj_digits = ""
        new_name = f"{label} - {cnpj_digits}" if cnpj_digits else label
        try:
            ok = await self.adspower.update_profile(self.adspower_user_id, name=new_name)
            if ok:
                self.adspower_profile_name = new_name
                logger.success(
                    "AdsPower: perfil %s renomeado para %r.",
                    self.adspower_user_id, new_name,
                )
            else:
                logger.warning(
                    "AdsPower: não consegui renomear o perfil %s para %r.",
                    self.adspower_user_id, new_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("AdsPower: falha ao renomear perfil (%s).", exc)

    def _check_stop(self) -> None:
        """Ponto de parada cooperativa (botões Encerrar/Assumir/Voltar Conta).

        Lê o pedido registrado em ``run_control`` para a run atual (via
        ``self.run_id``). Se houver, marca o resultado e levanta
        ``RunStopRequested`` — que é NON_RETRYABLE e desenrola o pipeline até o
        ``finally`` de limpeza. 'terminate' força fechar o perfil; 'assume'
        mantém aberto pro operador; 'release_account' libera conta sem descartar.
        Chamado em pontos seguros (loop do AuthFSM, antes de cada passo de negócio,
        no retry de passo)."""
        rid = getattr(self, "run_id", None)
        mode = run_control.get_stop(rid)
        if not mode:
            return
        res = self.last_result
        if res is not None:
            if mode == run_control.TERMINATE:
                res.terminated = True
                res.force_close_profile = True
                res.current_step = "terminated"
            elif mode == run_control.RELEASE_ACCOUNT:
                res.terminated = True  # marca como terminada para não fazer accounting normal
                res.force_close_profile = True
                res.current_step = "released_account"
            elif mode == run_control.ASSUME:
                res.assumed = True
                res.current_step = "assumed"
        raise RunStopRequested(rid or "?", mode)

    async def _await_if_paused(self, result: "AutomationResult") -> None:
        """Se a run está pausada, espera até o operador clicar 'Retomar'.

        Chamado antes de cada passo de negócio. Enquanto espera, o browser e o
        perfil ficam abertos e intocados — nenhuma ação no navegador."""
        rid = getattr(self, "run_id", None)
        if not rid or not run_control.is_pause_requested(rid):
            return
        paused_at = getattr(result, "current_step", "?") if result else "?"
        logger.info("PAUSAR: operador pausou a run na etapa '%s'. Aguardando retomada…", paused_at)
        from core import store  # noqa: PLC0415
        store.update_run(rid, status=store.RUN_PAUSED, error=f"pausada na etapa '{paused_at}' — aguardando retomada")
        while run_control.is_pause_requested(rid):
            if self._assume_requested() or self._close_requested():
                return
            await asyncio.sleep(1)
        logger.info("RETOMAR: operador retomou a run — continuando da etapa '%s'.", paused_at)
        store.update_run(rid, status=store.RUN_RUNNING, error=None)

    async def _run_business_step(
        self,
        label: str,
        fn,
        page: "Page",
        result: "AutomationResult",
        *,
        max_retries: int = 2,
    ) -> None:
        """Roda um passo de negócio com até ``max_retries`` tentativas extras.

        Erros terminais de negócio (senha errada, conta descartada, CNPJ
        rejeitado, etc.) propagam imediatamente sem retry — só erros de UI/
        rede (Playwright timeout, elemento não encontrado, navegação falhou)
        justificam tentar de novo. Em CALIBRATION_MODE captura DOM e segue
        em vez de abortar."""
        last_exc: Exception | None = None
        attempts = max_retries + 1
        for attempt in range(attempts):
            await self._await_if_paused(result)
            self._check_stop()  # operador pediu Encerrar/Assumir?
            try:
                with step(logger, label, result=result, saga=self.saga):
                    await fn(page, result)
                return  # sucesso
            except self._NON_RETRYABLE:
                raise  # erros terminais — sem retry
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not self.calibration_mode:
                    if attempt < max_retries:
                        logger.warning(
                            "passo '%s' falhou (tentativa %d/%d): %s — "
                            "aguardando 4s e re-tentando.",
                            label, attempt + 1, attempts, exc,
                        )
                        await asyncio.sleep(4)
                        continue
                    raise
                # Calibração: captura DOM e segue para o próximo passo.
                await self._capture_step_failure(page, label)
                logger.error(
                    "CALIBRAÇÃO: passo '%s' falhou (%s) — DOM salvo, seguindo.",
                    label, exc,
                )
                return

    async def _attempt_portfolio_create(
        self, page: Page, result: AutomationResult, *, label: str = "1/5 portfolio_create"
    ) -> bool:
        """Roda o passo 1 (criação do portfólio) com o tratamento padrão de
        ``AccountDiscardError``/``PortfolioLimitError``. Devolve False quando o
        pipeline deve ABORTAR agora (``result`` já marcado com o motivo); True
        quando deve seguir (criado, reusado, ou falhou mas em calibração)."""
        try:
            with step(logger, label, result=result, saga=self.saga):
                await self._create_business_portfolio(page, result)
            return True
        except AccountDiscardError:
            # Conta inutilizável (≥2 portfólios) — descarta SEMPRE, mesmo em
            # calibração (não adianta seguir; a conta vai pra próxima).
            result.account_discarded = True
            result.current_step = "account_discarded"
            return False
        except PortfolioLimitError:
            logger.error("Limite de portfólios comerciais atingido para esta conta")
            result.portfolio_limit_reached = True
            result.current_step = "portfolio_limit_reached"
            return False
        except Exception:  # noqa: BLE001
            # CALIBRAÇÃO: mesmo a criação falhando, segue p/ capturar o DOM
            # das telas seguintes (mais erros por run). Produção: aborta.
            if not self.calibration_mode:
                raise
            await self._capture_step_failure(page, label)
            return True

    async def _check_business_restriction(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """"Cadeado vermelho" — a Meta às vezes restringe a BM IMEDIATAMENTE
        após a criação (pedido do operador 2026-06). Varre as mesmas 4
        superfícies de ``_maybe_handle_portfolio_limit`` (body, alert/dialog/
        status/aria-live, iframes) procurando ``BUSINESS_RESTRICTED_PATTERN``.

        Best-effort: nunca levanta — devolve True/False. Em caso positivo,
        dumpa o DOM para calibração (o texto exato varia por experimento da
        Meta) e o caller decide o que fazer (trocar CNPJ e recriar)."""
        async def _collect_text(loc) -> str:
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                return ""
            chunks: list[str] = []
            for i in range(min(count, 8)):
                try:
                    t = await loc.nth(i).inner_text(timeout=1500)
                except Exception:  # noqa: BLE001
                    continue
                if t:
                    chunks.append(t)
            return "\n".join(chunks)

        scopes = [
            page.locator("body"),
            page.locator('[role="alert"]'),
            page.locator('[role="dialog"]'),
            page.locator('[role="status"]'),
            page.locator('[aria-live]'),
        ]
        bag: list[str] = []
        for scope in scopes:
            text = await _collect_text(scope)
            if text:
                bag.append(text)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                t = await frame.locator("body").inner_text(timeout=1500)
            except Exception:  # noqa: BLE001
                continue
            if t:
                bag.append(t)
        combined = "\n---\n".join(bag)
        if not BUSINESS_RESTRICTED_PATTERN.search(combined):
            return False
        logger.error(
            "BM restringida pela Meta ('cadeado vermelho') detectada. "
            "Trecho: %s", combined[:280].replace("\n", " "),
        )
        await self._dump_debug_html(page, "business_restricted")
        return True

    async def _check_bm_locked(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """CADEADO de anúncio: a Meta trava o portfólio recém-criado ('não pode
        anunciar / criar anúncios'). Aparece na conta de anúncios E em domínios
        (screenshot do operador 2026-09). Navega à página de DOMÍNIOS desta BM
        (onde o cadeado aparece cedo) e procura ``BUSINESS_LOCKED_PATTERN``.

        Best-effort: nunca levanta. True = cadeado detectado (o caller abandona
        esta BM e roda a próxima no mesmo perfil)."""
        if not result.business_id:
            return False
        try:
            url = self._with_business_id(BUSINESS_DOMAINS_URL, result.business_id)
            await page.goto(url, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001 — best-effort; checa o DOM que tiver
            pass
        # Polling: a SPA pode demorar segundos para renderizar o banner.
        deadline = asyncio.get_event_loop().time() + 8.0
        while True:
            try:
                txt = await asyncio.wait_for(
                    page.evaluate("() => document.body ? document.body.innerText : ''"),
                    timeout=4.0,
                )
            except Exception:  # noqa: BLE001
                txt = ""
            if BUSINESS_LOCKED_PATTERN.search(txt or ""):
                logger.error(
                    "BM CADEADO (não pode anunciar) detectado. Trecho: %s",
                    (txt or "")[:220].replace("\n", " "),
                )
                try:
                    await self._dump_debug_html(page, "bm_locked")
                except Exception:  # noqa: BLE001
                    pass
                return True
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(1.0)
        return False

    def _snapshot_current_bm(self, result: AutomationResult) -> None:
        """Guarda a BM ATUAL (recém-concluída) em ``result.extra_bms`` antes de
        trocar o estado para a próxima BM. Só guarda se houver business_id."""
        if not result.business_id:
            return
        digits = self._profile_cnpj_digits()
        result.extra_bms.append(
            {
                "business_id": result.business_id,
                "cnpj": digits,
                "waba_id": result.waba_id,
                "ad_account_id": result.ad_account_id,
                "bm_invite_sent": result.bm_invite_sent,
                "bm_invite_email": result.bm_invite_email,
                "meta_token": result.meta_token,
                "system_user_token": result.system_user_token,
                "verification_status": result.verification_status,
                "complete": bool(
                    result.business_id
                    and result.ad_account_id
                    and result.business_verification_started
                ),
            }
        )
        logger.info(
            "2-BMs: BM concluída guardada (business_id=%s, cnpj=%s). Total "
            "guardadas: %d.",
            result.business_id, digits, len(result.extra_bms),
        )

    def _reset_result_for_next_bm(self, result: AutomationResult) -> None:
        """Zera SÓ os campos por-BM do ``result`` (mantém login/sessão) para a
        próxima BM na mesma sessão."""
        result.portfolio_created = False
        result.portfolio_reused = False
        result.business_id = None
        result.business_restricted = False
        result.bm_locked = False
        result.portfolio_limit_reached = False
        result.business_details_saved = False
        result.domain_added = False
        result.domain_meta_tag = None
        result.meta_verification_token = None
        result.site_deployed = False
        result.domain_metatag_live = False
        result.domain_verified = False
        result.sms_verified = False
        result.sms_failed_reason = None
        result.waba_id = None
        result.ad_account_id = None
        result.ad_account_name = None
        result.ad_account_created = False
        result.bm_invite_sent = False
        result.bm_invite_email = None
        result.meta_token = None
        result.verify_token = None
        result.system_user_token = None
        result.verification_status = None
        result.waba_review_status = None
        result.verify_token_ok = False
        result.business_verification_started = False
        result.datacrazy_registered = False
        # Snapshot de portfólios pré-criação é re-tirado em _attempt_portfolio_create,
        # mas zeramos por garantia (a BM nova será o diff sobre os existentes).
        self._pre_creation_portfolio_ids = set()

    async def _run_extra_bms(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Roda as BMs EXTRAS (2ª em diante) na MESMA sessão — mesmo navegador,
        proxy e login. Para cada unidade: guarda a BM anterior, troca o
        CNPJ/portfólio/SMS e refaz o pipeline de negócio (portfólio → domínio →
        dados → conta de anúncios → verificação). Best-effort: uma BM extra que
        falhar NÃO desfaz a(s) anterior(es) já enviada(s) para análise."""
        for idx, unit in enumerate(self.extra_bm_units, start=2):
            new_data = unit.get("cnpj_data")
            new_profile = unit.get("business_profile")
            if new_data is None or new_profile is None:
                logger.warning("2-BMs: unidade %d inválida (sem dados) — pulando.", idx)
                continue
            try:
                self._check_stop()
            except Exception:  # noqa: BLE001 — parada cooperativa propaga normalmente
                raise
            # 1) Guarda a BM que acabou de concluir e zera o estado por-BM.
            self._snapshot_current_bm(result)
            self._reset_result_for_next_bm(result)
            # 2) Troca o alvo para a próxima BM (mesmo login/perfil/proxy).
            self.cnpj_data = new_data
            self.business_profile = new_profile
            # SMS SOB DEMANDA: compra um número FRESCO agora, no início da BM
            # #%d — o número da numero-virtual expira em ~20 min, e pré-comprar
            # no início do run fazia ele morrer antes da verificação da 2ª BM
            # (produção 2026-09: status 'expired', SMS nunca chegava). Se algum
            # id veio no unit (compat), é ignorado/liberado pela saga.
            self.preallocated_sms_activation_id = None
            self.preallocated_sms_phone = None
            if getattr(self, "sms_client", None) and self.sms_client.enabled:
                try:
                    _act_id, _phone = await self.sms_client.acquire_number()
                    self.preallocated_sms_activation_id = _act_id
                    self.preallocated_sms_phone = _phone
                    self._track_sms_activation(
                        _act_id, detail=f"BM #{idx} sob demanda (phone={_phone})"
                    )
                    logger.success(
                        "2-BMs: número SMS FRESCO comprado para a BM #%d "
                        "(phone=%s) — evita expiração do pré-comprado.",
                        idx, _phone,
                    )
                except Exception as _sms_exc:  # noqa: BLE001
                    logger.error(
                        "2-BMs: falha ao comprar número SMS fresco p/ BM #%d "
                        "(%s) — passo 5 cairá no fallback manual.", idx, _sms_exc,
                    )
            new_digits = self._profile_cnpj_digits()
            self._cnpjs_attempted = {new_digits} if new_digits else set()
            logger.info(
                "════════ 2-BMs: iniciando BM #%d (CNPJ=%s, domínio=%s) na "
                "MESMA sessão ════════",
                idx, new_digits, getattr(new_profile, "domain", "?"),
            )
            # 3) Refaz o pipeline de negócio para esta BM.
            try:
                await self._run_business_pipeline(page, result)
                logger.success(
                    "2-BMs: BM #%d concluída (business_id=%s).",
                    idx, result.business_id,
                )
            except self._NON_RETRYABLE:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "2-BMs: BM #%d falhou (%s) — a(s) BM(s) anterior(es) já "
                    "enviada(s) permanecem; seguindo.", idx, exc,
                )
        # Garante que a ÚLTIMA BM concluída também entre no snapshot para a
        # persistência (o caller lê result.business_id + result.extra_bms).
        # NÃO fazemos snapshot aqui: a última BM fica nos campos correntes do
        # result (business_id etc.), e o persistidor grava essa + as extras.

    async def _run_business_pipeline(
        self, page: Page, result: AutomationResult
    ) -> None:
        # H1 — cada bloco `step()` loga start/ok/fail + duração e mantém
        # ``result.current_step`` atualizado para inspeção externa.
        self._check_stop()  # operador pediu Encerrar/Assumir antes de começar?
        if not await self._attempt_portfolio_create(page, result):
            return

        # FALLBACK REUSO (2026-09, conta AGED): em conta que já vem com o
        # portfólio do dono, a criação do 2º portfólio SEMPRE falha (bounce p/
        # loginpage, SSO nega, diff vazio) — em VÁRIOS caminhos, nem todos
        # marcam portfolio_created=True. Por isso o reuso NÃO pode ficar só no
        # _guard_portfolio_id (que exige portfolio_created=True): aqui cobre
        # TODOS os caminhos — se após a criação o business_id continua None e o
        # perfil tinha portfólio do dono, REUSA o pré-existente e segue a
        # verificação nele em vez de cair no passo 2 com id=None.
        if not result.business_id and self._profile_had_foreign_portfolio:
            reuse_id = await self._reuse_preexisting_portfolio_id(page)
            if reuse_id:
                result.business_id = reuse_id
                result.portfolio_reused = True
                result.portfolio_created = True
                logger.warning(
                    "1/5: criação do portfólio novo não capturou id (conta aged "
                    "— Meta bloqueia 2º portfólio). FALLBACK: reusando o "
                    "portfólio pré-existente (business_id=%s) e seguindo a "
                    "verificação NELE.",
                    reuse_id,
                )

        # Guard 1/5 → 2/5: portfólio "criado" mas SEM business_id real é a
        # cascata famosa (passo 2 navega com id=None → /latest/home → falso
        # "editor não abriu"). Aborta claro aqui em vez de mascarar em 2/5.
        # No-op quando o fallback acima já reusou (business_id preenchido).
        await self._guard_portfolio_id(page, result)

        # "Cadeado vermelho" pós-criação: perfil/conta continuam bons, só o
        # CNPJ desta tentativa "queimou" — troca de CNPJ (MESMO perfil/conta,
        # pedido do operador) e recria, 1x. Restringindo de novo, desiste
        # desta conta nesta run (result.business_restricted fica marcado).
        if await self._check_business_restriction(page, result):
            swapped = await self._swap_cnpj_for_rejected_name()
            if not swapped:
                result.business_restricted = True
                result.current_step = "business_restricted"
                logger.error(
                    "BM restringida e NÃO há CNPJ livre no pool p/ trocar — "
                    "desistindo desta conta nesta run."
                )
                return
            result.portfolio_created = False
            result.business_id = None
            result.portfolio_reused = False
            if not await self._attempt_portfolio_create(
                page, result, label="1/5 portfolio_create (retry-restrição)"
            ):
                return
            await self._guard_portfolio_id(page, result)
            if await self._check_business_restriction(page, result):
                result.business_restricted = True
                result.current_step = "business_restricted"
                logger.error(
                    "BM restringida NOVAMENTE após troca de CNPJ — desistindo "
                    "desta conta nesta run."
                )
                return

        # CADEADO de anúncio (operador 2026-09): a Meta às vezes trava o
        # portfólio recém-criado ('não pode anunciar / criar anúncios' — aparece
        # na conta de anúncios E em domínios). BM cadeada é INÚTIL, MAS o PERFIL
        # continua bom: ABANDONA esta BM aqui e o caller cria/roda a PRÓXIMA BM
        # no MESMO perfil (não desperdiça a conta). O CNPJ desta BM é descartado
        # no settle (portfólio foi criado). Só a 1ª BM cai aqui; a 2ª segue o
        # fluxo normal de criação + verificação.
        if await self._check_bm_locked(page, result):
            result.bm_locked = True
            result.current_step = "bm_locked"
            logger.warning(
                "BM veio com CADEADO (não pode anunciar) logo após a criação — "
                "ABANDONANDO esta BM; o perfil segue para a PRÓXIMA BM."
            )
            return

        # SMS24h ANTES do passo 2: o telefone comercial salvo em
        # business_details (linha ~5797) é o MESMO que a verificação (passo 5)
        # mostra no campo TRAVADO 'Telefone da organização'. Se o número sms24h
        # ainda não existe aqui, o passo 2 cai no telefone da Receita (ex.: DDD
        # 43) → a Meta manda o código pra esse número e o polling trava eterno em
        # STATUS_WAIT_CODE. Adquirindo agora, o mesmo número flui em
        # cadastro → comprovante → verificação. Idempotente: no-op se a UI já
        # pré-alocou; respeita COMPROVANTE_TELEFONE fixo no .env.
        if not should_skip_sms_acquire_for_comprovante():
            await self._ensure_sms_phone_for_comprovante()

        await self._run_business_step(
            "2/5 domain_security", self._configure_domain_security, page, result
        )
        await self._run_business_step(
            "3/5 business_details", self._fill_business_details, page, result
        )
        # Passo 4 — CONTA DE ANÚNCIOS (2026-09). Substituiu a criação da WABA
        # a pedido do operador: a esteira entrega a BM com uma conta de
        # anúncios em BRL, com o MESMO nome do portfólio. O fluxo do WhatsApp
        # continua íntegro logo abaixo, controlado por self.waba_enabled.
        # Falha aqui NÃO derruba o pipeline: a verificação da empresa (passo 5)
        # é o ativo mais caro da esteira e não depende da conta de anúncios. O
        # _run_business_step já re-tentou 3x; se ainda assim falhou, segue e a
        # run fica marcada como incompleta (all_steps_ok exige ad_account_id).
        try:
            await self._run_business_step(
                "4/5 ad_account",
                self._create_ad_account,
                page,
                result,
            )
        except self._NON_RETRYABLE:
            raise
        except Exception as exc:  # noqa: BLE001
            result.current_step = "ad_account_failed"
            logger.error(
                "ad_account falhou após as re-tentativas (%s) — seguindo para "
                "a verificação da empresa; a run NÃO fechará 100%%.", exc,
            )

        # Modalidade "Até Conta de Anúncios": encerra aqui — sem WABA, sem
        # BM Invite, sem verificação da empresa.
        if self.stop_after_ad_account:
            result.stopped_at_ad_account = True
            logger.info(
                "stop_after_ad_account: conta de anúncios concluída — "
                "encerrando pipeline (sem WABA/BM-invite/verificação)."
            )
            return

        if self.waba_enabled:
            logger.info("WABA: ON — iniciando criação da WABA (passo 4b).")
            await self._run_business_step(
                "4b/5 whatsapp_account",
                self._create_whatsapp_business_account,
                page,
                result,
            )
        else:
            logger.info("WABA: OFF — pulando criação da WABA.")

        # BM Invite: convite automático de Admin ao portfólio após criar a
        # conta de anúncios. Best-effort — falha NÃO bloqueia o pipeline.
        try:
            await self._send_bm_invite(page, result)
        except self._NON_RETRYABLE:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("bm_invite: erro não tratado (%s) — continuando.", exc)

        await self._run_business_step(
            "5/5 business_verification",
            self._run_business_verification,
            page,
            result,
        )

        # Token de escopo Graph: apiAccessToken do bootstrap da página Business
        # Suite — SEMPRE presente no HTML de business.facebook.com/*. Extrai do
        # DOM ATUAL (sem navegação extra). Funciona com ou sem WABA. É a 1ª
        # tentativa de obter um token utilizável pela Graph API; se o sniffer já
        # tiver capturado verify_token (raro — o token vem no HTML, não em
        # respostas GraphQL), este bloco é no-op.
        if not result.verify_token:
            try:
                await self._capture_verify_token_from_page(page, result)
            except Exception as exc:  # noqa: BLE001
                logger.debug("capture_verify_token_from_page falhou: %s", exc)

        # GARANTIA de waba_id/meta_token (WABA-específico): o sniffer perde
        # porque waba_id e apiAccessToken vivem no DOM do Manager, não em
        # respostas GraphQL. Lê DIRETO do WhatsApp Manager. Best-effort.
        if self.waba_enabled and not (
            result.waba_id and result.meta_token and result.verify_token
        ):
            try:
                await self._capture_waba_via_manager(page, result)
            except Exception as exc:  # noqa: BLE001
                logger.debug("capture_waba_via_manager falhou: %s", exc)

        # Token PERMANENTE (System User): o apiAccessToken/verify_token morre em
        # ~1h, mas a aprovação leva dias — só o token de System User sobrevive até
        # o flip. Gera AGORA (a sessão admin está viva) e PROMOVE a meta_token p/
        # as re-checagens consultarem a Graph sem reabrir o browser. Best-effort.
        # Sem WABA: gera c/ business_management (suficiente p/ verification_status).
        if result.business_id and not result.system_user_token and (
            self.waba_enabled and result.waba_id
            or not self.waba_enabled
        ):
            try:
                await self._generate_system_user_token(page, result)
            except Exception as exc:  # noqa: BLE001
                logger.debug("generate_system_user_token falhou: %s", exc)
        if result.system_user_token:
            result.meta_token = result.system_user_token

        if not self.waba_enabled:
            logger.info(
                "Datacrazy PULADO — WABA OFF (sem WABA para registrar)."
            )
        elif result.waba_id and result.meta_token:
            with step(logger, "datacrazy_register", result=result, saga=self.saga):
                await self._register_on_datacrazy(result)
        else:
            missing = [
                name for name, val in (
                    ("waba_id", result.waba_id),
                    ("meta_token", result.meta_token),
                ) if not val
            ]
            result.current_step = "datacrazy_skipped"
            logger.warning(
                "Datacrazy SKIPPED — sniffer não capturou: %s", ", ".join(missing)
            )

        # Prova ao vivo que a verificação funciona: consulta a Graph API AGORA,
        # com o token recém-capturado (fresco). O token morre em ~2h, então é a
        # janela em que dá pra confirmar o método. Loga o status inicial.
        await self._verify_on_capture(result)

    async def _verify_on_capture(self, result: AutomationResult) -> None:
        """Consulta a verificação NA HORA, com o(s) token(s) recém-capturado(s).

        Prova, em TODA run, que o método funciona. Ordem dos candidatos, do
        melhor p/ o pior: ``system_user_token`` (PERMANENTE — sobrevive até a
        aprovação) → ``verify_token`` (apiAccessToken, escopo Graph, ~1h) →
        ``meta_token`` (pode ser o token do payload do WhatsApp, de tipo errado).
        O primeiro que a Graph API ACEITAR (resposta não-morta) vira a prova e é
        PROMOVIDO a meta_token, p/ as re-checagens posteriores usarem o token bom.
        NÃO passa pelo proxy (GET direto), então funciona mesmo com o Webshare
        sem banda. Best-effort: nunca derruba o run."""
        from core import meta_graph  # noqa: PLC0415

        biz, waba = result.business_id, result.waba_id
        candidates: list[str] = []
        for tok in (
            result.system_user_token, result.verify_token, result.meta_token
        ):
            if tok and tok not in candidates:
                candidates.append(tok)
        if not (biz and candidates):
            logger.info(
                "[VERIFY] checagem pulada — falta business_id/token "
                "(biz=%s tokens=%d).", bool(biz), len(candidates),
            )
            return
        dead: list[str] = []
        for tok in candidates:
            try:
                bs, be = await meta_graph.fetch_business_verification(biz, tok)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[VERIFY] tentativa c/ token %s… falhou (ignorado): %s",
                    tok[:4], exc,
                )
                continue
            if meta_graph.is_dead_token(be):
                dead.append(f"{tok[:4]}:{be}")
                continue
            # Token AUTENTICOU (resposta não-morta) — é a prova do método.
            ws = None
            if waba:
                try:
                    ws, _we = await meta_graph.fetch_waba_review(waba, tok)
                except Exception:  # noqa: BLE001
                    ws = None
            result.verification_status = bs
            result.waba_review_status = ws
            result.verify_token_ok = True
            if result.meta_token != tok:
                logger.info(
                    "[VERIFY] token de escopo Graph (%s…) promovido a meta_token "
                    "p/ as re-checagens.", tok[:4],
                )
                result.meta_token = tok
            if be and bs is None:
                logger.warning(
                    "[VERIFY] empresa ainda sem status (token OK): %s", be,
                )
            logger.success(
                "[VERIFY] MÉTODO OK (token fresco %s…) — empresa "
                "verification_status=%s | WABA account_review_status=%s. Quando a "
                "Meta aprovar vira 'verified'/'APPROVED' (re-check pega depois).",
                tok[:4], bs or "?", (ws or "?") if waba else "—(sem waba_id)",
            )
            return
        logger.warning(
            "[VERIFY] nenhum token serviu p/ a Graph API (%d tentado(s): %s). "
            "Provável token de tipo errado p/ consultar a empresa; a verificação "
            "exigirá re-inspeção com token de escopo Graph.",
            len(candidates), "; ".join(dead) or "—",
        )

    async def _capture_verify_token_from_page(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Extrai apiAccessToken do DOM da página Business Suite atual.

        Após os passos 2-5 o bot está em ``business.facebook.com/*``. O HTML
        bootstrap de TODA página Business Suite contém
        ``bizKitSettingsConfig.apiAccessToken`` (escopo Graph, prefixo EAA…),
        que serve p/ consultar ``verification_status`` na Graph API. Extrai do
        DOM ATUAL — sem navegação extra. Funciona com ou sem WABA.
        Best-effort: nunca derruba a run.
        """
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
        except Exception:  # noqa: BLE001
            return
        tm = _MGR_TOKEN_RE.search(html)
        if not tm:
            return
        token = tm.group(1)
        if result.verify_token is None:
            result.verify_token = token
            logger.info(
                "[TOKEN] apiAccessToken (escopo Graph) extraído da página "
                "Business Suite (prefixo=%s, %d chars).",
                token[:4], len(token),
            )
        if result.meta_token is None:
            result.meta_token = token
            logger.info(
                "[TOKEN] meta_token preenchido com apiAccessToken da "
                "página (prefixo=%s, %d chars).",
                token[:4], len(token),
            )

    async def _capture_waba_via_manager(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Lê waba_id + meta_token DIRETO do WhatsApp Manager (fallback robusto).

        O sniffer de rede perde porque NENHUM dos dois trafega como resposta
        graphql: o waba_id é o ``asset_id`` da URL do Manager e o token é
        ``bizKitSettingsConfig.apiAccessToken`` no bootstrap do DOM. Recipe
        calibrado em produção 2026-06 (conta Thiago, business_id 363486…):
          GET /wa/manage/home/?business_id=<biz>  → redireciona para
          /latest/whatsapp_manager/overview/?business_id=<biz>&asset_id=<WABA>
        e o DOM traz ``"localScopeID":"<WABA>" … "type":"WHATSAPP_BUSINESS_ACCOUNT"``.

        Best-effort: navega, dispensa o coachmark do Manager ('Este é o
        Gerenciador do WhatsApp'), lê da URL e do DOM, e preenche
        result.waba_id/result.meta_token se faltarem. Dumpa se não achar o
        waba_id. Devolve True se capturou o waba_id.
        """
        biz = result.business_id
        url = self._with_business_id(
            "https://business.facebook.com/wa/manage/home/", biz
        )
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("waba capture: navegação ao Manager falhou (%s).", exc)
            return False
        # Meta às vezes serve a tela de erro transitória ('Sorry, something went
        # wrong / Go back') no Manager → o DOM fica só com o erro e waba_id=None
        # (run 64910427 jun/2026). Dá F5 antes de ler (_recover_from_fb_error já
        # existe, detecta o erro e recarrega até 3×; best-effort, nunca levanta).
        await self._recover_from_fb_error(page)
        # O Manager abre um coachmark de onboarding ('Este é o Gerenciador do
        # WhatsApp') — dispensa antes de ler (não bloqueia a URL, limpa o DOM).
        await self._dismiss_onboarding_coachmarks(page)
        await self._organic_wait()

        # Parsing (URL + DOM) vive em extract_waba_from_manager (função pura,
        # testável); a guarda waba_id != business_id mora lá.
        html = ""
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        waba, mgr_token, source = extract_waba_from_manager(
            html, page.url, result.business_id
        )
        if waba and result.waba_id is None:
            result.waba_id = waba
        if waba:
            logger.success(
                "waba capture: WABA ID lido %s do Manager: %s",
                "da URL" if source == "url" else "do DOM", result.waba_id,
            )
        if mgr_token:
            # apiAccessToken do Manager = token de ESCOPO GRAPH (consulta a
            # verificação). Guarda no slot verify_token SEMPRE — antes só lia
            # se meta_token estivesse vazio, e o token do payload do WhatsApp
            # (capturado no passo 4) bloqueava esta leitura, deixando a
            # verificação sem um token utilizável.
            if result.verify_token is None:
                result.verify_token = mgr_token
                logger.success(
                    "waba capture: apiAccessToken (escopo Graph) lido do "
                    "Manager p/ verificação (prefixo=%s, %d chars).",
                    mgr_token[:4], len(mgr_token),
                )
            if result.meta_token is None:
                result.meta_token = mgr_token
                logger.success(
                    "waba capture: access_token lido do DOM do Manager "
                    "(prefixo=%s, %d chars).", mgr_token[:4], len(mgr_token),
                )

        if result.waba_id is None:
            await self._dump_debug_html(page, "waba_manager_no_id")
            logger.warning(
                "waba capture: WABA ID não encontrado no Manager — DOM salvo "
                "p/ calibração."
            )
            return False
        self._set_profile_flag("waba_ok")
        return True

    async def _generate_system_user_token(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Gera um token de SYSTEM USER (PERMANENTE) em Business Settings.

        POR QUÊ: o apiAccessToken/verify_token capturado morre em ~1h, mas a Meta
        só aprova a empresa em 2-7 dias úteis (até 30) e o WABA em até ~7 dias —
        então o token capturado SEMPRE está morto quando a aprovação sai. O token
        de System User NÃO expira (método oficial da Meta p/ automação), logo é o
        único que sobrevive até o flip 'aprovada'. Guardá-lo como meta_token faz
        o re-check (botão/scheduler) consultar a Graph SEM reabrir o browser.

        Recipe (Business Settings → Usuários do sistema):
          1. Adicionar usuário do sistema (nome + função Admin).
          2. Atribuir ativos: o app do WhatsApp / a WABA, Controle total.
          3. Gerar token com whatsapp_business_management (+ _messaging).
          4. Ler o token EAA… exibido no diálogo.

        BEST-EFFORT / A CALIBRAR: a UI de System Users tem várias telas e os
        seletores abaixo são a 1ª aproximação (PT/EN). Cada ponto-cego dá dump de
        DOM p/ calibrar na run real, EXATAMENTE como _capture_waba_via_manager foi
        calibrado. NUNCA levanta (a conta Meta já existe). Devolve True se leu o
        token. O sniffer de rede já está ligado: o POST de geração também pode
        cair em extract_waba_creds_from_response como rede de segurança.
        """
        if not result.business_id:
            return False
        url = self._with_business_id(
            "https://business.facebook.com/settings/system-users",
            result.business_id,
        )
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("system_user: navegação a System Users falhou (%s).", exc)
            return False
        await self._recover_from_fb_error(page)
        await self._dismiss_onboarding_coachmarks(page)
        await self._organic_wait()
        # Dump SEMPRE: é a fonte de verdade p/ calibrar os seletores na 1ª run
        # real (a UI de System Users ainda não foi mapeada em produção).
        await self._dump_debug_html(page, "system_users_page")

        # 1) Adicionar usuário do sistema. Rótulos PT/EN; best-effort.
        add_re = re.compile(r"adicionar|criar.*usu[áa]rio|add|create", re.I)
        try:
            btn = page.get_by_role("button", name=add_re).first
            if await btn.count():
                await self._robust_click(btn)
                await self._organic_wait()
                # nome + função Admin + criar (a CALIBRAR contra o diálogo real)
                name_in = page.get_by_role("textbox").first
                if await name_in.count():
                    await name_in.fill(f"automation-{result.business_id[-6:]}")
                role_re = re.compile(r"admin", re.I)
                try:
                    await page.get_by_text(role_re).first.click(timeout=4000)
                except Exception:  # noqa: BLE001
                    pass
                create_re = re.compile(r"criar|create", re.I)
                try:
                    await page.get_by_role("button", name=create_re).first.click(
                        timeout=4000
                    )
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("system_user: criação best-effort falhou: %s", exc)

        # 2) Gerar token (whatsapp_business_management). A seleção de app +
        # permissões é multi-tela → dump p/ calibrar e segue best-effort.
        gen_re = re.compile(r"gerar token|generate token|gerar novo token", re.I)
        try:
            gbtn = page.get_by_role("button", name=gen_re).first
            if await gbtn.count():
                await self._robust_click(gbtn)
                await self._organic_wait()
                perms = ["business_management"]
                if self.waba_enabled:
                    perms += ["whatsapp_business_management", "whatsapp_business_messaging"]
                for perm in perms:
                    try:
                        await page.get_by_text(perm).first.click(timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    await page.get_by_role(
                        "button", name=re.compile(r"gerar|generate", re.I)
                    ).first.click(timeout=4000)
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("system_user: geração best-effort falhou: %s", exc)
        await self._dump_debug_html(page, "system_users_after_generate")

        # 3) Lê o token EAA… exibido. O token de System User é longo (EAA…) e
        # aparece num campo copiável após "Gerar token".
        html = ""
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        tok = None
        if html:
            tm = _SYS_USER_TOKEN_RE.search(html)
            tok = tm.group(1) if tm else None

        # Validação do token capturado
        if tok:
            # Remove espaços em branco extras (pode haver no DOM)
            tok = tok.strip()

            # Token deve ter pelo menos 50 chars (EAA… padrão Meta)
            if len(tok) < 50:
                logger.warning(
                    "system_user: token capturado muito curto (%d chars) — "
                    "provável truncamento. Tentando novamente.",
                    len(tok),
                )
                tok = None
            # Se termina com caracteres que sugerem truncamento, descarta
            elif tok.endswith(("...", "­", "­­")) or " " in tok or "\n" in tok:
                logger.warning(
                    "system_user: token parece truncado ou contém caracteres inválidos — "
                    "descartando. Terminação: %s",
                    repr(tok[-10:]),
                )
                tok = None
            # Verifica se o token tem apenas caracteres válidos (alfanum, -, _)
            elif not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in tok):
                logger.warning(
                    "system_user: token contém caracteres inválidos — "
                    "descartando."
                )
                tok = None
            else:
                result.system_user_token = tok
                logger.success(
                    "system_user: token PERMANENTE gerado (prefixo=%s, %d chars). "
                    "Token validado — pronto para Graph API.",
                    tok[:4], len(tok),
                )
                return True

        logger.warning(
            "system_user: token não capturado ou inválido — DOM salvo (system_users_*) p/ "
            "calibrar os seletores na próxima run real. Verifique se o token foi realmente gerado na UI."
        )
        return False

    # ------------------------------------------------------------------
    # Datacrazy integration
    # ------------------------------------------------------------------
    async def _register_on_datacrazy(self, result: AutomationResult) -> None:
        """Encaminha (waba_id, meta_token) para a plataforma Datacrazy.

        Contrato esperado (placeholder — confirmar com Datacrazy):
            POST {DATACRAZY_ENDPOINT}
            Headers:
                Authorization: Bearer {DATACRAZY_API_KEY}
                Content-Type:  application/json
            Body (JSON):
                {
                    "waba_id":      "<id numérico do WhatsApp Business Account>",
                    "access_token": "<EAAB... token de longa duração>",
                    "source":       "meta_automation"
                }

        Estratégia:
          - Sem DATACRAZY_ENDPOINT no .env → loga em modo "dry-run" (a casca
            funciona, só falta o endpoint real).
          - HTTP 2xx → ``result.datacrazy_registered = True``.
          - Falhas de rede / status não-2xx → anexa nota em ``result.notes``
            mas NÃO derruba o pipeline (a conta no Meta já foi criada).
        """
        if not DATACRAZY_ENDPOINT:
            logger.warning(
                "[DATACRAZY] DATACRAZY_ENDPOINT não configurado — dry-run "
                "(waba_id=%s, token=%s...)",
                result.waba_id,
                (result.meta_token or "")[:12],
            )
            result.notes.append(
                "Datacrazy: endpoint ausente — payload não enviado."
            )
            return

        payload = {
            "waba_id": result.waba_id,
            "access_token": result.meta_token,
            "source": "meta_automation",
        }
        headers = {"Content-Type": "application/json"}
        if DATACRAZY_API_KEY:
            headers["Authorization"] = f"Bearer {DATACRAZY_API_KEY}"

        try:
            async with httpx.AsyncClient(timeout=DATACRAZY_TIMEOUT_S) as client:
                r = await client.post(
                    DATACRAZY_ENDPOINT, json=payload, headers=headers
                )
            if 200 <= r.status_code < 300:
                result.datacrazy_registered = True
                logger.success(
                    "[DATACRAZY] WABA registrada com sucesso (HTTP %s).",
                    r.status_code,
                )
            else:
                body_snippet = (r.text or "")[:300]
                logger.error(
                    "[DATACRAZY] Falha no registro (HTTP %s): %s",
                    r.status_code, body_snippet,
                )
                result.notes.append(
                    f"Datacrazy HTTP {r.status_code}: {body_snippet}"
                )
        except httpx.HTTPError as exc:
            logger.error("[DATACRAZY] Erro de rede ao chamar endpoint: %s", exc)
            result.notes.append(f"Datacrazy network error: {exc}")

    # ------------------------------------------------------------------
    # Context / session
    # ------------------------------------------------------------------
    async def _create_context(
        self, browser, *, via_adspower: bool = False
    ) -> tuple[BrowserContext, bool]:
        # Modo AdsPower (CDP): o navegador antidetect já gerencia UA, viewport,
        # timezone, proxy e cookies do perfil. Reutilizamos o contexto default
        # para preservar a fingerprint configurada no painel.
        if via_adspower:
            contexts = browser.contexts
            if contexts:
                # SEMPRE reutiliza contexto existente via CDP (já está aberto no AdsPower)
                # NÃO criar novo para evitar duas janelas abertas do mesmo perfil
                logger.info("Reaproveitando contexto padrão do AdsPower via CDP.")
                has_storage_state = self.storage_state_path.exists()
                if has_storage_state:
                    logger.info("storage_state.json encontrado — será restaurada sessão anterior na próxima navegação.")
                return contexts[0], has_storage_state
            else:
                # Fallback (improvável): AdsPower conectou mas sem contexto?
                # Cria novo contexto com storage_state se houver
                logger.warning("AdsPower sem contexto — criando novo contexto fresco.")
                kwargs = {"ignore_https_errors": True}
                if self.storage_state_path.exists():
                    kwargs["storage_state"] = str(self.storage_state_path)
                    logger.info("storage_state.json encontrado — restaurando sessão anterior.")
                else:
                    logger.info("Sem storage_state — será feito login novo.")
                return await browser.new_context(**kwargs), self.storage_state_path.exists()

        # Modo direto (launch local): aplicamos UA/viewport/locale/timezone e,
        # se houver, restauramos storage_state.json local.
        kwargs: dict[str, Any] = {
            "user_agent": PLAYWRIGHT_USER_AGENT,
            "viewport": PLAYWRIGHT_VIEWPORT,
            "locale": TARGET_LOCALE_HEADER,
            "timezone_id": TARGET_TIMEZONE,
            "extra_http_headers": {"Accept-Language": f"{TARGET_LOCALE_HEADER},pt;q=0.9"},
            # ignore_https_errors para suportar proxies com certs self-signed (ex.: Bright Data)
            "ignore_https_errors": True,
        }
        used_state = False
        if self.storage_state_path.exists():
            kwargs["storage_state"] = str(self.storage_state_path)
            used_state = True
            logger.info("storage_state.json encontrado — tentando reaproveitar sessão.")
        else:
            logger.info("storage_state ausente — login completo será executado.")
        return await browser.new_context(**kwargs), used_state

    @staticmethod
    async def _ensure_page(context: BrowserContext) -> Page:
        """Garante uma Page utilizável via CDP (AdsPower).

        Após ``connect_over_cdp`` o Playwright pode ainda não ter recebido os
        eventos ``Target.targetCreated`` que populam ``context.pages`` — corrida
        entre a conexão CDP e a descoberta das abas existentes. Tentamos até 3 s
        antes de criar uma nova aba. Se a criação falhar (alguns builds do AdsPower
        bloqueiam ``Target.createTarget`` via CDP), reaproveitamos qualquer aba
        aberta no browser (ex.: a página de IP start.adspower.net).
        """
        # Polling até as páginas existentes aparecerem (~3s, 5 × 600ms).
        for _ in range(5):
            for page in context.pages:
                if not page.is_closed():
                    return page
            await asyncio.sleep(0.6)
        # Nenhuma página conhecida — tenta criar.
        try:
            return await context.new_page()
        except Exception as exc:  # noqa: BLE001
            # "Failed to open a new tab": o AdsPower bloqueou Target.createTarget.
            # Percorre TODOS os contextos do browser — a aba start.adspower.net
            # já está lá, só não foi mapeada no contexto default.
            browser = context.browser
            if browser:
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        if not page.is_closed():
                            logger.warning(
                                "_ensure_page: new_page falhou (%s) — reaproveitando "
                                "aba existente %s.", type(exc).__name__, page.url or "?",
                            )
                            return page
            raise

    # JS injetado em TODA página (via add_init_script) para silenciar o
    # diálogo 'Deseja sair?' (beforeunload) da SPA da Meta. Em modo CDP/headed
    # (AdsPower), o Playwright NÃO intercepta beforeunload nativamente via
    # page.on("dialog", ...) — o diálogo aparece como UI nativa do Chrome e
    # bloqueia o fluxo. A única solução confiável é impedir o REGISTRO do
    # listener no JS da página, antes mesmo dos scripts da Meta rodarem.
    _BEFOREUNLOAD_SUPPRESS_JS = """
        (() => {
            // 1. Adiciona listener de CAPTURA com stopImmediatePropagation —
            //    roda ANTES de qualquer listener já registrado pela SPA da Meta
            //    e impede TODOS eles de chegar ao browser (prevenindo o dialogo
            //    nativo 'Deseja sair?'). Precisa ser feito UMA vez por página.
            if (!window.__pw_bu_cap) {
                window.__pw_bu_cap = true;
                window.addEventListener('beforeunload', function(e) {
                    // stopImmediatePropagation impede TODOS os outros handlers
                    // (da SPA da Meta, do AdsPower, etc.) de rodar.
                    // NÃO chamamos e.preventDefault() nem retornamos string —
                    // ambos DISPARAM o diálogo de confirmação no Chrome em vez
                    // de suprimi-lo.
                    e.stopImmediatePropagation();
                    try { delete e.returnValue; } catch (_e) {}
                }, { capture: true });
            }
            // 2. Remove window.onbeforeunload (sempre — a SPA pode sobrescrever).
            try { window.onbeforeunload = null; } catch (_e) {}
            // 3. Intercepta addEventListener para descartar FUTURAS registrações
            //    de beforeunload (feito uma vez).
            if (!window.__pw_bu_ael) {
                window.__pw_bu_ael = true;
                try {
                    const _origAEL = EventTarget.prototype.addEventListener;
                    EventTarget.prototype.addEventListener = function(type, listener, opts) {
                        if (type === 'beforeunload') return;
                        return _origAEL.call(this, type, listener, opts);
                    };
                } catch (_e) {}
            }
        })();
    """

    async def _suppress_native_blockers(
        self, context: BrowserContext, page: Page
    ) -> None:
        """Neutraliza prompts NATIVOS do navegador que travam o fluxo.

        Sintoma em produção (2026-06-17): runs caindo em ``TargetClosedError``
        na espera manual do 2FA. O balão do Chrome "Mostrar notificações?"
        (permissão Web Push) e diálogos nativos (alert/confirm/beforeunload)
        ficam POR CIMA da página: o Playwright NÃO os enxerga como DOM, eles
        bloqueiam todo clique e acabam derrubando a conexão CDP. O tratamento
        certo é no nível do contexto/página, não por seletor:

        1. Pré-concede ``notifications`` → ``Notification.requestPermission()``
           resolve na hora e o balão de permissão nunca aparece (registrar um
           handler de ``dialog`` NÃO cobre isso — o prompt de permissão é UI do
           próprio Chrome, não um evento ``dialog``).
        2. ``context.add_init_script`` injeta JS que IMPEDE o registro de
           listeners ``beforeunload`` antes dos scripts da Meta rodarem —
           abordagem mais robusta que page.on("dialog") em modo CDP/headed.
        3. Handler de ``dialog`` → loga (diagnóstico do "bot travou") e dá
           ``dismiss`` (cobre alert/confirm; o beforeunload é bloqueado em (2)).

        Best-effort: qualquer falha é logada em DEBUG e NUNCA derruba o run."""
        try:
            await context.grant_permissions(["notifications"])
            logger.debug("Permissão de notificações pré-concedida (sem balão).")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "grant_permissions(notifications) falhou (segue sem): %s",
                type(exc).__name__,
            )

        # Injeta supressor de beforeunload no contexto inteiro — roda em TODA
        # navegação futura ANTES dos scripts da página (add_init_script garante
        # isso). Também avalia na página ATUAL (já carregada) para remover
        # listeners que a Meta possa ter registrado antes desta chamada.
        try:
            await context.add_init_script(self._BEFOREUNLOAD_SUPPRESS_JS)
            logger.debug("beforeunload suprimido via add_init_script no contexto.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("add_init_script beforeunload falhou: %s", type(exc).__name__)
        try:
            await page.evaluate(self._BEFOREUNLOAD_SUPPRESS_JS)
        except Exception:  # noqa: BLE001
            pass

        def _on_dialog(dialog) -> None:
            dtype = getattr(dialog, "type", "") or ""
            logger.warning(
                "Diálogo nativo do navegador (%s) — dismiss automático p/ não "
                "travar: %r",
                dtype, (getattr(dialog, "message", "") or "")[:120],
            )
            try:
                # beforeunload: accept() = "Sair da página" (deixa a navegação
                # continuar).  dismiss() significaria "Ficar" e CANCELARIA a
                # navegação em curso — causando ERR_ABORTED.
                if dtype == "beforeunload":
                    asyncio.create_task(dialog.accept())
                else:
                    asyncio.create_task(dialog.dismiss())
            except Exception as exc:  # noqa: BLE001
                logger.debug("dismiss do diálogo falhou: %s", type(exc).__name__)

        suppress_js = self._BEFOREUNLOAD_SUPPRESS_JS

        def _guard_new_page(new_page) -> None:
            """Instala handler de diálogo E supressor de beforeunload em TODA
            página nova (popups do assistente de verificação). Em modo CDP/
            headed o beforeunload nativo não é capturado pelo dialog-event;
            o add_init_script cobre futuras navegações, mas o evaluate aqui
            garante que popups JÁ carregados também sejam cobertos."""
            try:
                new_page.on("dialog", _on_dialog)
            except Exception:  # noqa: BLE001
                pass
            async def _eval_suppress():
                try:
                    await new_page.evaluate(suppress_js)
                except Exception:  # noqa: BLE001
                    pass
            try:
                asyncio.create_task(_eval_suppress())
            except Exception:  # noqa: BLE001
                pass

        page.on("dialog", _on_dialog)
        try:
            context.on("page", _guard_new_page)
        except Exception:  # noqa: BLE001
            pass

    def _install_bandwidth_meter(self, context: BrowserContext) -> None:
        """Passo 0 — mede banda por (resource_type, domínio). SÓ-LOG, NUNCA altera.

        Gated por MEASURE_ASSET_BYTES (default OFF → zero impacto). Engancha no
        CONTEXTO (não na página): cobre a página inicial, TODAS as navegações
        (page.goto reusa a mesma página) E popups/abas novas — que um page.on
        perderia. Igual no AdsPower/CDP (contexts[0]) e no launch-direto.

        Métrica: request.sizes() (bytes de FIO: req+resp, headers+corpo,
        pós-compressão) — o que o cap do Webshare conta. Content-Length entra só
        de fallback (cobre só o corpo da RESPOSTA; perde upload/headers e é
        inconsistente em chunked/comprimido). Blind spots: frames de WebSocket
        (não são 'request') e handshake TLS/TCP — registrados no JSON."""
        if not MEASURE_ASSET_BYTES:
            return
        self._bw_stats: dict[tuple[str, str], dict[str, int]] = {}
        self._bw_failed = 0

        async def _on_finished(request) -> None:
            try:
                from urllib.parse import urlsplit  # noqa: PLC0415

                rtype = request.resource_type or "other"
                host = (urlsplit(request.url).hostname or "?").lower()
                total = 0
                try:
                    s = await request.sizes()
                    total = int(
                        (s.get("requestHeadersSize") or 0)
                        + (s.get("requestBodySize") or 0)
                        + (s.get("responseHeadersSize") or 0)
                        + (s.get("responseBodySize") or 0)
                    )
                except Exception:  # noqa: BLE001
                    # Fallback pedido pelo operador: Content-Length, senão corpo.
                    try:
                        resp = await request.response()
                        if resp is not None:
                            cl = resp.headers.get("content-length")
                            if cl and cl.isdigit():
                                total = int(cl)
                            else:
                                total = len(await resp.body())
                    except Exception:  # noqa: BLE001
                        total = 0
                agg = self._bw_stats.setdefault(
                    (rtype, host), {"bytes": 0, "count": 0}
                )
                agg["bytes"] += total
                agg["count"] += 1
            except Exception:  # noqa: BLE001
                pass  # medição NUNCA derruba o run

        def _on_failed(request) -> None:
            self._bw_failed += 1

        context.on("requestfinished", _on_finished)
        context.on("requestfailed", _on_failed)
        logger.info(
            "[BANDWIDTH] medição SÓ-LOG ligada (MEASURE_ASSET_BYTES) — não "
            "altera o tráfego; relatório no fim do run."
        )

    def _flush_bandwidth_report(self) -> None:
        """Emite a tabela de banda do run (log + JSON por run). No-op se OFF."""
        if not MEASURE_ASSET_BYTES:
            return
        stats = getattr(self, "_bw_stats", None)
        if not stats:
            return
        try:
            import json  # noqa: PLC0415
            import time  # noqa: PLC0415

            rows = [
                {
                    "resource_type": rt,
                    "domain": host,
                    "bytes": v["bytes"],
                    "mb": round(v["bytes"] / 1048576, 3),
                    "count": v["count"],
                }
                for (rt, host), v in stats.items()
            ]
            rows.sort(key=lambda r: r["bytes"], reverse=True)
            total = sum(r["bytes"] for r in rows) or 1
            for r in rows:
                r["pct"] = round(100 * r["bytes"] / total, 1)

            by_type: dict[str, int] = {}
            by_domain: dict[str, int] = {}
            for r in rows:
                by_type[r["resource_type"]] = (
                    by_type.get(r["resource_type"], 0) + r["bytes"]
                )
                by_domain[r["domain"]] = by_domain.get(r["domain"], 0) + r["bytes"]

            total_mb = round(total / 1048576, 2)
            cnpj = self._profile_cnpj_digits() or "unknown"
            stamp = int(time.time())
            rid = (getattr(self, "run_id", None) or "norun")[:8]

            lines = [
                f"[BANDWIDTH] run {rid} cnpj={cnpj} — TOTAL {total_mb} MB "
                f"({len(rows)} pares tipo×domínio, {self._bw_failed} requests falhos)",
                f"{'tipo':<12} {'domínio':<34} {'MB':>9} {'%':>6}",
            ]
            for r in rows[:30]:
                lines.append(
                    f"{r['resource_type']:<12} {r['domain'][:34]:<34} "
                    f"{r['mb']:>9.3f} {r['pct']:>5.1f}%"
                )
            logger.info("\n".join(lines))

            BANDWIDTH_DIR.mkdir(parents=True, exist_ok=True)
            out = BANDWIDTH_DIR / f"{cnpj}-{stamp}-{rid}.json"
            payload = {
                "cnpj": cnpj,
                "run_id": getattr(self, "run_id", None),
                "ts": stamp,
                "total_bytes": total,
                "total_mb": total_mb,
                "failed_requests": self._bw_failed,
                "metric": (
                    "playwright request.sizes() — bytes de fio req+resp incl. "
                    "headers (fallback: Content-Length/corpo)"
                ),
                "blind_spots": [
                    "frames de websocket (não contados)",
                    "handshake TLS/TCP (não contado)",
                    "hits de cache de disco contam ~0 (correto p/ o cap)",
                ],
                "by_resource_type": dict(
                    sorted(by_type.items(), key=lambda x: -x[1])
                ),
                "by_domain": dict(sorted(by_domain.items(), key=lambda x: -x[1])),
                "rows": rows,
            }
            out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
            logger.info("[BANDWIDTH] relatório salvo em %s", out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BANDWIDTH] falha ao emitir relatório (ignorado): %s", exc)

    async def _is_authenticated(self, page: Page, *, navigate: bool) -> bool:
        try:
            if navigate:
                try:
                    await page.goto(META_HOME_URL, wait_until="domcontentloaded")
                except Exception as _nav_exc:  # noqa: BLE001
                    # net::ERR_ABORTED é normal quando o Facebook redireciona
                    # (ex.: / → /login.php) antes do domcontentloaded disparar.
                    # O redirect pode ter carregado com sucesso; verifica o DOM.
                    logger.debug(
                        "_is_authenticated: goto home levantou %s (%s) "
                        "— verificando DOM mesmo assim.",
                        type(_nav_exc).__name__, _nav_exc,
                    )
                await self._organic_wait()
            try:
                await page.wait_for_selector(
                    self.POST_LOGIN_INDICATOR, timeout=8000
                )
                return True
            except PlaywrightTimeoutError:
                pass
            # Fallback por URL: o nav com aria-label nem sempre renderiza no
            # prazo em web.facebook.com (domínio legado; home pós-redirect
            # web.facebook.com/?_rdc=1&_rdr), mas a sessão ESTÁ válida — sem
            # este fallback a home logada era classificada 'unknown' e a run
            # falhava mesmo logada (produção 2026-09, conta Suelen). Aceita a
            # HOME do facebook.com (www OU web) como logada, desde que NÃO seja
            # login/checkpoint/2FA/consent e não haja campo de senha na tela.
            from urllib.parse import urlsplit  # noqa: PLC0415

            u = (page.url or "").lower()
            parts = urlsplit(u)
            on_fb_home = (
                parts.netloc.endswith("facebook.com")
                and (parts.path or "/") in ("/", "")
                and not any(
                    seg in u
                    for seg in (
                        "/login", "/checkpoint", "/two_step_verification",
                        "/two_factor", "/recover", "/consent", "/privacy/consent",
                    )
                )
            )
            if on_fb_home:
                try:
                    pass_visible = await page.locator(
                        'input[name="pass"]'
                    ).first.is_visible(timeout=1000)
                except Exception:  # noqa: BLE001
                    pass_visible = False
                if not pass_visible:
                    logger.info(
                        "_is_authenticated: nav aria-label não renderizou, mas "
                        "URL é home logada do facebook.com (%s) e não há campo "
                        "de senha — considerando AUTENTICADO.",
                        parts.netloc,
                    )
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------
    async def _perform_login(self, page: Page, result: AutomationResult) -> None:
        # PR3 — fluxo linear substituído pela AuthFSM: sente o estado da
        # tela (classify_facebook_state) e reage (page objects), em vez de
        # seguir roteiro fixo. Helpers de gesto humano, captcha e fallback
        # manual continuam neste runner e são invocados pela FSM.
        timeout = getattr(self, "_login_attempt_timeout_s", None)
        coro = AuthFSM(runner=self, page=page, result=result).run()
        if timeout:
            try:
                await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "PROXYLESS: login não completou em %.0fs — abortando "
                    "tentativa.", timeout,
                )
                raise
        else:
            await coro

    @staticmethod
    async def _dismiss_cookie_banner(page: Page) -> None:
        # PR2 — delega ao Page Object; mantido para compatibilidade.
        await FacebookLoginPage(page).dismiss_cookie_banner()

    @staticmethod
    def _first_visible(
        page: Page, locators: list[Locator], timeout_ms: int = 5000
    ):
        """Return the first locator that becomes visible (awaitable).

        PR2 — delega para core/pages/base.py (mesma semântica/timeout);
        a assinatura com ``page`` é preservada para os call sites do
        pipeline de negócio. ``timeout_ms`` por candidato: reduzir acelera
        muito as buscas de campos AUSENTES (cada candidato espera o teto
        antes de desistir; o form de detalhes tem campos que não existem no
        diálogo e gastavam ~85s cada com o default de 5s × 17 candidatos).
        """
        return _pages_first_visible(locators, timeout_ms)

    @staticmethod
    async def _first_visible_scan(
        page: Page,
        locators: list[Locator],
        *,
        scan: int = 10,
        rounds: int = 1,
        round_wait_s: float = 1.0,
    ):
        """Como ``_first_visible``, mas varre TODOS os matches por VISIBILIDADE.

        ``.first`` + ``wait_for(visible)`` trava no 1º match do DOM mesmo que
        esteja oculto (menus fechados, templates, variantes EN) — o elemento
        REAL visível pode ser o 2º/3º match e o wait estoura à toa (visto em
        produção 2026-06: 'Adicionar' da página de domínios). Aqui cada
        locator tem seus ``scan`` primeiros matches checados com
        ``is_visible()``; ``rounds``×``round_wait_s`` dá tempo de render.
        """
        for r in range(rounds):
            for loc in locators:
                try:
                    n = await loc.count()
                except Exception:  # noqa: BLE001
                    continue
                for i in range(min(n, scan)):
                    try:
                        if await loc.nth(i).is_visible():
                            return loc.nth(i)
                    except Exception:  # noqa: BLE001
                        continue
            if r < rounds - 1:
                await asyncio.sleep(round_wait_s)
        # UIError (não LoginError): scan sem match visível = glitch de UI / render
        # lento, TRANSITÓRIO — não descarta a conta (subclasse de LoginError, os
        # `except LoginError` seguem pegando).
        raise UIError(
            "Nenhum match visível no scan de locators "
            f"({rounds} rodada(s), {scan} matches/locator)."
        )

    async def _warmup_profile(self, page: "Page") -> None:
        """Aquecimento leve do perfil recém-logado, ANTES de criar a BM — pra
        parecer humano e reduzir a desconfiança da Meta (ajuda no soft-block/SMS):
          1) rola o feed,
          2) curte 1 post,
          3) (opcional) compartilha 1 post,
          4) passa no Marketplace e rola,
          5) vê alguns segundos de Reels.
        BEST-EFFORT: cada ação é try/except; NUNCA derruba a run. Configurável
        via WARMUP_ENABLED / WARMUP_REELS_SECONDS / WARMUP_SHARE no .env."""
        from config import (  # noqa: PLC0415
            WARMUP_ENABLED, WARMUP_REELS_SECONDS, WARMUP_SHARE,
        )
        if not WARMUP_ENABLED:
            return
        logger.info("warmup: aquecendo o perfil (feed → curtir → marketplace → reels)…")

        # 1) FEED — rola algumas vezes.
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            await self._organic_wait()
            for _ in range(random.randint(3, 6)):
                await page.mouse.wheel(0, random.randint(400, 900))
                await asyncio.sleep(random.uniform(1.0, 2.5))
            logger.info("warmup: feed rolado.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("warmup: feed falhou (%s).", exc)

        # 2) CURTIR 1 post (o 1º botão 'Curtir/Like' visível).
        try:
            like = page.get_by_role(
                "button", name=re.compile(r"^\s*(curtir|like)\s*$", re.I)
            ).first
            await like.wait_for(state="visible", timeout=4000)
            await self._robust_click(like)
            await self._organic_wait()
            logger.info("warmup: 1 post curtido.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("warmup: curtir falhou (%s).", exc)

        # 3) COMPARTILHAR 1 post (opcional — mais arriscado; default OFF).
        if WARMUP_SHARE:
            try:
                share = page.get_by_role(
                    "button", name=re.compile(r"compartilhar|share", re.I)
                ).first
                await share.wait_for(state="visible", timeout=4000)
                await self._robust_click(share)
                await asyncio.sleep(random.uniform(1.5, 3.0))
                now = page.get_by_role(
                    "button", name=re.compile(r"compartilhar agora|share now", re.I)
                ).first
                await now.wait_for(state="visible", timeout=4000)
                await self._robust_click(now)
                await self._organic_wait()
                logger.info("warmup: 1 post compartilhado.")
            except Exception as exc:  # noqa: BLE001
                logger.debug("warmup: compartilhar falhou (%s).", exc)

        # 4) MARKETPLACE — entra e rola.
        try:
            await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded", timeout=30000)
            await self._organic_wait()
            for _ in range(random.randint(2, 4)):
                await page.mouse.wheel(0, random.randint(500, 1000))
                await asyncio.sleep(random.uniform(1.0, 2.2))
            logger.info("warmup: marketplace visto.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("warmup: marketplace falhou (%s).", exc)

        # 5) REELS — assiste alguns segundos e passa 1-2.
        try:
            await page.goto("https://www.facebook.com/reel/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(max(2.0, float(WARMUP_REELS_SECONDS)))
            for _ in range(random.randint(1, 2)):
                try:
                    await page.keyboard.press("ArrowDown")
                except Exception:  # noqa: BLE001
                    break
                await asyncio.sleep(random.uniform(3.0, 6.0))
            logger.info("warmup: reels assistidos.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("warmup: reels falhou (%s).", exc)

        logger.success("warmup: aquecimento concluído — seguindo o fluxo normal.")

    # ------------------------------------------------------------------
    # 2FA
    # ------------------------------------------------------------------
    # PR3 — _handle_two_factor foi absorvido pela AuthFSM (core/auth_fsm.py):
    # a entrada de TOTP virou o handler _handle_totp, o fail-fast em estados
    # terminais virou regra geral da FSM, e a resolução de seed
    # (credenciais → AdsPower fakey) vive em _resolve_totp_code.

    @staticmethod
    def _normalize_totp_seed(raw: str) -> str:
        """Remove caracteres invisíveis e formatação humana da seed.

        Clientes mandam seed em mil formatos: ``"abcd 1234 xyz9"``,
        ``"ABCD-1234-XYZ9"``, com NBSP, zero-width spaces, tabs, etc.
        O ``pyotp`` exige base32 puro (A-Z + 2-7) sem espaços.
        """
        if not raw:
            return ""
        # Remove TODOS whitespace (inclui  , ​, \t, \n).
        cleaned = re.sub(r"\s+", "", raw, flags=re.UNICODE)
        # Remove caracteres Unicode categoria Cf (ZWSP, ZWNJ, BOM, LRM, RLM)
        # que \s NÃO captura — fonte clássica de seeds quebradas.
        import unicodedata  # noqa: PLC0415
        cleaned = "".join(ch for ch in cleaned if unicodedata.category(ch) != "Cf")
        # Remove separadores comuns: hífen, underscore, ponto.
        cleaned = re.sub(r"[-_.]+", "", cleaned)
        return cleaned.upper()

    async def _detect_totp_input(self, page: Page) -> Optional[Locator]:
        # PR2 — seletores e timing centralizados no Page Object.
        return await FacebookTwoFactorPage(page).detect_totp_input()

    async def _await_manual_intervention(
        self, page: Page, result: AutomationResult
    ) -> None:
        result.sms_intervention = True
        logger.warning(
            "Fallback de 2FA: complete o desafio (SMS/aprovação) na janela do navegador. "
            "Aguardando indicador pós-login por até %.0fs.",
            SMS_MANUAL_TIMEOUT_S,
        )
        try:
            await page.wait_for_selector(
                self.POST_LOGIN_INDICATOR, timeout=int(SMS_MANUAL_TIMEOUT_S * 1000)
            )
            logger.success("Intervenção manual concluída — sessão autenticada.")
        except PlaywrightTimeoutError as exc:
            # PR1 — carimba o estado em que a página ficou ao estourar o
            # timeout (ex.: ainda na tela 2FA vs conta bloqueada no meio).
            diagnosis = await classify_facebook_state(page)
            result.failure_state = diagnosis.state.value
            result.failure_detail = diagnosis.description
            result.notes.append(f"2fa: {diagnosis.summary()}")
            raise LoginError(
                f"Timeout aguardando intervenção manual de 2FA. {diagnosis.summary()}"
            ) from exc

    # ------------------------------------------------------------------
    # Captcha
    # ------------------------------------------------------------------
    # Domínios/sinais de captcha — cobre reCAPTCHA, hCaptcha E Arkose/
    # FunCaptcha (o que o Meta usa no /two_step_verification/authentication/,
    # visto em produção 2026-06). Usado p/ ROTEAR ao handler de captcha.
    _CAPTCHA_FRAME_RE = re.compile(
        r"recaptcha|hcaptcha|funcaptcha|arkose", re.I
    )

    @staticmethod
    async def _connect_browser_cdp(pw: Any, ws_endpoint: str) -> Any:
        """``connect_over_cdp`` com timeout explícito + classificação.

        O AdsPower pode reportar 'iniciado' e mesmo assim o CDP travar no boot
        (porta ocupada, ws inválido, browser pendurado). Aqui o erro do
        Playwright vira ``CdpConnectionError`` (classificado) — o chamador
        decide fallback p/ launch direto. Static p/ ser testável sem montar um
        runner inteiro.
        """
        try:
            return await pw.chromium.connect_over_cdp(
                ws_endpoint, timeout=ADSPOWER_CDP_CONNECT_TIMEOUT_MS
            )
        except (PlaywrightTimeoutError, PlaywrightError, OSError) as exc:
            raise CdpConnectionError(
                "AdsPower iniciou mas a conexão CDP falhou em "
                f"{ADSPOWER_CDP_CONNECT_TIMEOUT_MS}ms: {exc}"
            ) from exc

    async def _restart_browser_without_proxy(self) -> "Page":
        """Fecha browser AdsPower, remove proxy do perfil, reabre e retorna nova Page."""
        user_id = self.adspower_user_id
        logger.info(
            "Proxyless retry: fechando browser AdsPower (user_id=%s)…", user_id,
        )
        try:
            if hasattr(self, '_active_page') and self._active_page:
                try:
                    await self._active_page.context.browser.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        await self.adspower.stop(user_id)
        await asyncio.sleep(2)

        logger.info("Proxyless retry: removendo proxy do perfil…")
        await self.adspower.update_profile(
            user_id, user_proxy_config={"proxy_soft": "no_proxy"},
        )
        await asyncio.sleep(1)

        logger.info("Proxyless retry: reabrindo browser sem proxy…")
        payload = await self.adspower.start(user_id, headless=self.headless)
        ws_endpoint = payload["ws_puppeteer"]
        logger.info(
            "Proxyless retry: browser reaberto — conectando CDP: %s", ws_endpoint,
        )

        browser = await self._connect_browser_cdp(self._pw, ws_endpoint)
        context, _ = await self._create_context(browser, via_adspower=True)
        self._install_bandwidth_meter(context)
        page = await self._ensure_page(context)
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
        await self._suppress_native_blockers(context, page)

        logger.success("Proxyless retry: browser pronto sem proxy (user_id=%s).", user_id)
        return page

    # Classificação de TIPO de captcha. A FONTE confiável é a URL dos frames
    # (o widget renderiza num iframe próprio); o HTML do documento principal
    # carrega ruído (ex.: a tela de 2FA do Meta tem 'recaptcha' num blob de
    # config mesmo quando o desafio real é outro), então só casamos sinais
    # FORTES de script/iframe no HTML. Ordem importa: Arkose primeiro (é o que
    # o 2captcha-reCAPTCHA NÃO cobre).
    _CAPTCHA_KIND_PATTERNS = (
        ("arkose", re.compile(r"arkoselabs|funcaptcha|fc_token", re.I)),
        ("hcaptcha", re.compile(r"hcaptcha\.com|\bh-captcha\b", re.I)),
        ("recaptcha", re.compile(r"google\.com/recaptcha|recaptcha/api|g-recaptcha|grecaptcha", re.I)),
    )

    @staticmethod
    def _classify_captcha_kind(
        frame_urls: list[str], html: str = ""
    ) -> Optional[str]:
        """Deduz o tipo de captcha a partir das URLs de frame (forte) + HTML
        (sinais fortes só). Devolve 'arkose'|'hcaptcha'|'recaptcha'|None."""
        frame_blob = " ".join(frame_urls or [])
        for kind, pat in AutomationRunner._CAPTCHA_KIND_PATTERNS:
            if pat.search(frame_blob):
                return kind
        for kind, pat in AutomationRunner._CAPTCHA_KIND_PATTERNS:
            if pat.search(html or ""):
                return kind
        return None

    async def _detect_captcha_kind(self, page: Page) -> Optional[str]:
        """Tipo do captcha presente (ou None). Usa frames + HTML do runtime."""
        urls: list[str] = []
        try:
            urls = [f.url or "" for f in page.frames]
        except Exception:  # noqa: BLE001
            pass
        html = ""
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        return self._classify_captcha_kind(urls, html)

    async def _has_captcha(self, page: Page) -> bool:
        """True se há QUALQUER captcha na página (frame ou DOM)."""
        try:
            for frame in page.frames:
                if self._CAPTCHA_FRAME_RE.search(frame.url or ""):
                    return True
        except Exception:  # noqa: BLE001
            pass
        try:
            html = await asyncio.wait_for(page.content(), timeout=15)
            # Sinais no HTML (Arkose injeta scripts arkoselabs/funcaptcha).
            return bool(
                re.search(r"arkoselabs|funcaptcha|fc_token|g-recaptcha|h-captcha",
                          html, re.I)
            )
        except Exception:  # noqa: BLE001
            return False

    async def _maybe_solve_captcha(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Resolve captcha pela CORRIDA manual×2captcha (o 1º vence).

        Devolve True se resolvido. Mantido como entrada única — _handle_captcha
        e o caminho 2FA-sem-input-com-captcha convergem aqui.
        """
        if not await self._has_captcha(page):
            return False
        return await self._solve_captcha_race(page, result)

    async def _await_captcha_cleared(
        self, page: Page, start_url: str, *, timeout_s: float = 25.0
    ) -> bool:
        """Espera o desafio de captcha REALMENTE sair da tela: URL mudou (saiu
        de /authentication/), indicador pós-login visível, ou o captcha sumiu do
        DOM. Best-effort — usado p/ confirmar que o token foi aceito antes de
        declarar sucesso (evita o loop de re-injeção)."""
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            cur = page.url or ""
            if cur != start_url and "/two_step_verification/authentication" not in cur:
                return True
            try:
                if await page.locator(self.POST_LOGIN_INDICATOR).first.is_visible(
                    timeout=400
                ):
                    return True
            except Exception:  # noqa: BLE001
                pass
            try:
                if not await self._has_captcha(page):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _solve_captcha_race(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Corre 2captcha (auto) vs resolução MANUAL — o primeiro a concluir
        destrava. Pensado para FLEETS: o solver tenta sozinho enquanto um
        humano pode resolver qualquer instância na janela do AdsPower.

        'Resolvido' = a página SAIU do desafio (URL mudou) ou o token foi
        injetado. Sinal por URL cobre QUALQUER tipo de captcha (Arkose
        inclusive), não só reCAPTCHA.
        """
        start_url = page.url or ""
        challenge = await self._detect_captcha_iframe(page)
        kind = await self._detect_captcha_kind(page)
        result.captcha_type = kind
        # 2captcha (método userrecaptcha) só cobre reCAPTCHA. Arkose/FunCaptcha
        # NÃO são cobertos — não adianta prometer "2captcha ativo".
        auto_covers_kind = kind in (None, "recaptcha")
        has_auto = (
            self.captcha_solver is not None
            and challenge is not None
            and auto_covers_kind
        )
        solver_name = getattr(self.captcha_solver, "name", "solver")
        if has_auto:
            auto_label = f"{solver_name} tentando ({kind or 'recaptcha'})"
        elif self.captcha_solver is not None and not auto_covers_kind:
            auto_label = f"{solver_name} NÃO cobre {kind} — só MANUAL"
        else:
            auto_label = "solver automático indisponível p/ este tipo — só MANUAL"
        logger.warning(
            "Captcha detectado (tipo=%s) — CORRIDA manual×2captcha (%s). Pode "
            "resolver na janela do AdsPower; o primeiro a concluir destrava "
            "(até %.0fs).",
            kind or "desconhecido",
            auto_label,
            CAPTCHA_MANUAL_TIMEOUT_S,
        )

        _auto_done = asyncio.Event()

        async def _auto() -> bool:
            if not has_auto:
                _auto_done.set()
                return False
            frame, sitekey, is_enterprise, origin = challenge  # type: ignore[misc]
            try:
                token = await self.captcha_solver.solve_recaptcha_v2(
                    sitekey=sitekey, page_url=origin or page.url,
                    enterprise=is_enterprise,
                )
                await self._inject_recaptcha_token(page, token)
                logger.success(
                    "2captcha: token injetado — confirmando avanço da página…"
                )
            except CaptchaUnsupportedError as exc:
                logger.warning(
                    "2captcha não conseguiu resolver este captcha (%s) — "
                    "provavelmente reCAPTCHA Enterprise ou variante não suportada "
                    "pelo método padrão. Só MANUAL a partir daqui.",
                    exc,
                )
                _auto_done.set()
                return False
            except Exception as exc:  # noqa: BLE001
                logger.warning("2captcha falhou (%s) — seguindo só com manual.", exc)
                _auto_done.set()
                return False
            try:
                if await self._await_captcha_cleared(page, start_url, timeout_s=25.0):
                    logger.success("2captcha: token aceito — desafio saiu da tela.")
                    return True
                logger.warning(
                    "2captcha: token injetado mas o desafio NÃO saiu (FB não aceitou "
                    "o token) — corrida MANUAL segue; resolva na janela do AdsPower."
                )
                return False
            finally:
                _auto_done.set()

        async def _manual() -> bool:
            if CAPTCHA_MANUAL_TIMEOUT_S <= 0:
                return False
            deadline = time.monotonic() + CAPTCHA_MANUAL_TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                cur = page.url or ""
                # Saiu do desafio (URL mudou) → humano resolveu.
                if cur != start_url and "/two_step_verification/authentication" not in cur:
                    return True
                # Indicador pós-login já visível (resolveu e logou direto).
                try:
                    if await page.locator(self.POST_LOGIN_INDICATOR).first.is_visible(
                        timeout=500
                    ):
                        return True
                except Exception:  # noqa: BLE001
                    pass
                # Captcha conhecido (reCAPTCHA/hCaptcha) sumiu da DOM.
                if not await self._has_captcha(page):
                    return True
            return False

        async def _refresher() -> bool:
            # Espera o _auto terminar (token injetado+confirmado OU falhou) antes
            # de clicar ↻ — senão o refresh invalida o token que está sendo
            # confirmado (produção 2026-10: ↻ aos 21s matava token pendente).
            if CAPTCHA_REFRESH_INTERVAL_S <= 0:
                return False
            await _auto_done.wait()
            deadline = time.monotonic() + CAPTCHA_MANUAL_TIMEOUT_S
            while time.monotonic() < deadline:
                if (page.url or "") != start_url:
                    return False
                if not await self._has_captcha(page):
                    return False
                if await self._refresh_captcha_challenge(page):
                    logger.info(
                        "Captcha: token rejeitado — clicando ↻ (novo desafio) "
                        "para o operador/extensão tentar um mais fácil.",
                    )
                await asyncio.sleep(CAPTCHA_REFRESH_INTERVAL_S)
            return False

        tasks = [
            asyncio.create_task(_auto()),
            asyncio.create_task(_manual()),
            asyncio.create_task(_refresher()),
        ]
        resolved = False
        pending = set(tasks)
        try:
            while pending and not resolved:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    try:
                        if t.result():
                            resolved = True
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            for t in pending:
                t.cancel()
        result.captcha_solved = resolved
        if resolved:
            logger.success("Captcha resolvido (corrida manual×2captcha).")
        else:
            logger.error(
                "Captcha NÃO resolvido em %.0fs (nem manual, nem 2captcha) — tipo=%s.",
                CAPTCHA_MANUAL_TIMEOUT_S,
                kind or "desconhecido",
            )
            # Evidência do estado final do desafio + detalhe classificado para a
            # UI/telemetria (o failure_state fino é estampado por quem levanta o
            # CaptchaUnsolvedError — aqui guardamos o detalhe e o DOM).
            await self._dump_debug_html(page, "captcha_unsolved_race")
            result.failure_detail = (
                f"Captcha não resolvido (tipo={kind or 'desconhecido'}; "
                f"manual e 2captcha esgotados em {CAPTCHA_MANUAL_TIMEOUT_S:.0f}s)."
            )
        return resolved

    async def _refresh_captcha_challenge(self, page: Page) -> bool:
        """Clica o ↻ ('obter novo desafio') do reCAPTCHA para TROCAR o desafio.

        O botão vive dentro do iframe 'bframe' (cross-origin
        google.com/recaptcha/.../bframe) e tem id ``#recaptcha-reload-button``.
        Best-effort: devolve True se conseguiu clicar em algum frame, False se
        não achou o botão (ex.: captcha não-reCAPTCHA ou desafio já fechado)."""
        reload_re = re.compile(
            r"new challenge|novo desafio|obter um novo|recarregar|reload|"
            r"get a new challenge",
            re.I,
        )
        for frame in page.frames:
            url = frame.url or ""
            if "recaptcha" not in url or "bframe" not in url:
                continue
            # 1) id determinístico do botão de reload do reCAPTCHA.
            try:
                btn = frame.locator("#recaptcha-reload-button")
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001
                pass
            # 2) fallback por aria-label (locale pt/en).
            try:
                btn = frame.get_by_role("button", name=reload_re)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    @staticmethod
    async def _detect_captcha_iframe(
        page: Page, *, max_wait_s: float = 4.0, poll_s: float = 0.4
    ) -> Optional[tuple[Frame, str, bool, Optional[str]]]:
        """Varre frames procurando reCAPTCHA/hCaptcha.

        O iframe do captcha às vezes monta de forma assíncrona depois que o
        submit dispara o desafio; por isso, fazemos pequenas reverificações
        em loop até ``max_wait_s``. Quando o iframe é encontrado, tentamos
        extrair o sitekey de 3 fontes: query string, ``[data-sitekey]`` na
        page principal, e ``[data-sitekey]`` em qualquer frame ancestral.

        Devolve ``(frame, sitekey, enterprise, origin)``:
          * ``enterprise`` — a URL do iframe é ``/recaptcha/enterprise/anchor``
            (o solver precisa do task type Enterprise; senão "Invalid domain").
          * ``origin`` — domínio REGISTRADO do sitekey, decodificado do param
            ``co`` do anchor (base64 de ``https://host:443``). É o websiteURL
            certo p/ o solver — passar ``page.url`` (ex.: business.facebook.com)
            quando o widget é de ``www.facebook.com`` causa "Invalid domain for
            site key". ``None`` se não der p/ decodificar (cai p/ page.url).
        """
        deadline = asyncio.get_event_loop().time() + max_wait_s
        while True:
            for frame in page.frames:
                url = frame.url or ""
                if "recaptcha" not in url and "hcaptcha" not in url:
                    continue
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                sitekey = (qs.get("k") or qs.get("sitekey") or [""])[0]
                src = "iframe-k" if sitekey else None
                if not sitekey:
                    sitekey = await AutomationRunner._scrape_data_sitekey(page)
                    if sitekey:
                        src = "data-sitekey"
                if sitekey:
                    enterprise = "/enterprise/" in url
                    origin = AutomationRunner._origin_from_co(
                        (qs.get("co") or [""])[0]
                    )
                    # Um sitekey reCAPTCHA bem-formado é "6L"+38 chars (40 total).
                    # Logamos a fonte e o formato p/ flagrar extração de lixo
                    # (ex.: substring de um token __rc do Facebook) — causa de
                    # "Invalid site key" no CapSolver.
                    well_formed = bool(re.fullmatch(r"6[Ll][A-Za-z0-9_-]{38}", sitekey))
                    logger.info(
                        "Captcha sitekey extraído via %s: %r (%s) enterprise=%s "
                        "origin=%s frame=%s",
                        src, sitekey,
                        "formato reCAPTCHA OK" if well_formed else "FORMATO SUSPEITO",
                        enterprise, origin or "?", (url or "")[:90],
                    )
                    return frame, sitekey, enterprise, origin
            if asyncio.get_event_loop().time() >= deadline:
                return None
            await asyncio.sleep(poll_s)

    @staticmethod
    def _origin_from_co(co: str) -> Optional[str]:
        """Decodifica o ``co`` do anchor do reCAPTCHA → origem (``scheme://host``).

        O ``co`` é base64 (urlsafe, padding opcional) de ``https://host:porta``;
        é o domínio para o qual o sitekey está REGISTRADO. Devolve sem a porta;
        ``None`` se vazio/indecodificável. Função pura (testável)."""
        if not co:
            return None
        try:
            import base64  # noqa: PLC0415

            raw = base64.urlsafe_b64decode(co + "=" * (-len(co) % 4))
            origin = raw.decode("utf-8", "replace").strip()
            # "https://www.facebook.com:443" → "https://www.facebook.com"
            origin = re.sub(r":\d+$", "", origin).rstrip("/")
            return origin if origin.startswith("http") else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _scrape_data_sitekey(page: Page) -> str:
        """Procura o atributo ``data-sitekey`` em qualquer elemento visível.

        Faz um único ``evaluate`` que varre todos os ``[data-sitekey]`` da DOM
        (incluindo containers ocultos) e devolve o primeiro valor não vazio.
        Captchas inline às vezes ficam em divs com ``display:none`` enquanto
        o iframe ainda carrega.
        """
        try:
            value = await page.evaluate(
                """() => {
                    const nodes = document.querySelectorAll('[data-sitekey]');
                    for (const n of nodes) {
                        const k = n.getAttribute('data-sitekey');
                        if (k && k.trim().length > 8) return k.trim();
                    }
                    return '';
                }"""
            )
            return value or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    async def _inject_recaptcha_token(page: Page, token: str) -> None:
        """Injeta o token do 2captcha e dispara o callback registrado.

        Três etapas — todas best-effort:
          1. **Preenchimento** de ``textarea#g-recaptcha-response`` (e variantes
             ``g-recaptcha-response-N`` quando há múltiplos widgets na página)
             via ``nativeInputValueSetter`` para que forms React/Vue percebam
             a mudança através de ``input``/``change`` events.
          2. **Disparo do callback** registrado no ``___grecaptcha_cfg.clients``
             (cobre todos os widgets, inclusive invisible reCAPTCHA).
          3. **Fallback hCaptcha**: se o token for de hCaptcha, preenche
             ``[name="h-captcha-response"]`` e ``[name="g-recaptcha-response"]``
             (alguns sites usam o segundo como espelho).
        """
        _js = """(token) => {
                // 1. Preenche todas as textareas g-recaptcha-response e h-captcha-response.
                const setNativeValue = (el, val) => {
                    const proto = Object.getPrototypeOf(el);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (setter && setter.set) setter.set.call(el, val);
                    else el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const selectors = [
                    'textarea#g-recaptcha-response',
                    'textarea[name="g-recaptcha-response"]',
                    'textarea[id^="g-recaptcha-response"]',
                    'textarea[name="h-captcha-response"]',
                    'input[name="h-captcha-response"]',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach((el) => {
                        try {
                            el.style.display = 'block';
                            setNativeValue(el, token);
                        } catch (_) {}
                    });
                }

                // 2. Dispara callback registrado no widget reCAPTCHA.
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {
                        const walk = (obj) => {
                            if (!obj || typeof obj !== 'object') return;
                            for (const k of Object.keys(obj)) {
                                const v = obj[k];
                                if (v && typeof v.callback === 'function') {
                                    try { v.callback(token); } catch (_) {}
                                }
                                if (v && typeof v === 'object') walk(v);
                            }
                        };
                        Object.values(cfg.clients).forEach(walk);
                    }
                } catch (_) {}

                // 2b. reCAPTCHA ENTERPRISE: o FB lê o token via getResponse()
                // (não por textarea). Sobrescreve p/ devolver o NOSSO token —
                // quando o FB consultar, recebe o token válido do CapSolver.
                try {
                    if (window.grecaptcha) {
                        if (typeof window.grecaptcha.getResponse === 'function') {
                            window.grecaptcha.getResponse = () => token;
                        }
                        if (window.grecaptcha.enterprise &&
                            typeof window.grecaptcha.enterprise.getResponse === 'function') {
                            window.grecaptcha.enterprise.getResponse = () => token;
                        }
                    }
                } catch (_) {}

                // 3. Fallback hCaptcha: window.hcaptcha + onSuccess callback.
                try {
                    if (window.hcaptcha && typeof window.hcaptcha.execute === 'function') {
                        // No-op: hcaptcha já tem o token via textarea.
                    }
                } catch (_) {}
            }"""
        # Roda em TODOS os frames: o g-recaptcha-response/callback/grecaptcha do
        # FB Enterprise costuma viver num SUB-FRAME (não no top) — page.content()
        # nem mostra, mas o Playwright alcança via page.frames. Best-effort/frame.
        for _fr in page.frames:
            try:
                await _fr.evaluate(_js, token)
            except Exception:  # noqa: BLE001
                pass

    async def _click_recaptcha_checkbox(self, page: Page) -> bool:
        """Clica o checkbox 'Não sou um robô' dentro do iframe anchor do
        reCAPTCHA (nudge pós-solve).

        Alguns fluxos do FB só avançam quando o widget é MARCADO depois que o
        token foi injetado (pedido do operador 2026-09). O checkbox vive num
        iframe cross-origin ``.../recaptcha/.../anchor`` — o Playwright acessa
        o frame e clica ``#recaptcha-anchor``. Best-effort, nunca levanta."""
        for frame in page.frames:
            url = frame.url or ""
            if "recaptcha" not in url or "anchor" not in url:
                continue
            try:
                box = frame.locator(
                    "#recaptcha-anchor, .recaptcha-checkbox, "
                    "div[role='checkbox']"
                ).first
                if await box.is_visible(timeout=1500):
                    await box.click(timeout=4000)
                    logger.info(
                        "captcha: checkbox 'Não sou um robô' clicado "
                        "(nudge pós-solve)."
                    )
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "captcha: falha ao clicar o checkbox no anchor (%s).", exc
                )
        return False

    # ------------------------------------------------------------------
    # Locale
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_lang(raw: Optional[str]) -> str:
        """Normaliza ``document.documentElement.lang`` para comparação.

        ``"pt-BR"`` → ``"pt_br"``, ``"bn"`` → ``"bn"``, ``None`` → ``""``.
        """
        return (raw or "").strip().replace("-", "_").lower()

    @staticmethod
    def _locale_needs_normalization(normalized_lang: str, force: bool) -> bool:
        """Decide se o idioma da conta precisa ser forçado para pt-BR.

        Matriz de decisão (idioma detectado × FORCE_LOCALE):
          * ``pt_br``           → nunca (já é o alvo).
          * ``pt*`` / ``en*``   → só com FORCE_LOCALE=true (os seletores da
            esteira são bilíngues PT/EN; forçar é atrito desnecessário).
          * ``""`` (detecção falhou) → só com FORCE_LOCALE=true (forçar às
            cegas poderia agir numa página intermediária errada).
          * qualquer outro (bengali, hindi, …) → SEMPRE, independente da
            flag — os seletores por texto do pipeline quebrariam (visto em
            produção 2026-06 com ``lang="bn"`` / locale ``bn_IN``).
        """
        if normalized_lang.startswith("pt_br"):
            return False
        if not normalized_lang or normalized_lang.startswith(("pt", "en")):
            return force
        return True

    @staticmethod
    def _with_locale_param(url: str, locale: str) -> str:
        """Anexa ``locale=`` a uma URL preservando a query string existente.

        O parâmetro força o idioma de RENDERIZAÇÃO daquela navegação — não
        muda o setting da conta, mas garante que a própria página de
        preferências apareça em pt-BR (fallbacks por texto "Editar/Salvar"
        voltam a valer) mesmo quando a conta está em bengali.
        """
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}locale={locale}"

    @staticmethod
    def _with_business_id(url: str, business_id: Optional[str]) -> str:
        """Anexa ``business_id=`` quando conhecido (idem semântica acima).

        As rotas de settings do Business Manager aceitam o parâmetro para
        cravar QUAL BM abrir — sem ele o Meta escolhe a "padrão", que em
        contas recém-criadas pode cair no redirect de marketing/escolha.
        """
        if not business_id:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}business_id={business_id}"

    async def _detect_interface_lang(self, page: Page) -> str:
        """Lê ``document.documentElement.lang`` e SEMPRE loga (telemetria).

        Ainda não temos HTML pós-login em idiomas exóticos (só a tela de
        login em bengali) — logar o idioma cru em todo run permite observar
        a distribuição real antes de endurecer mais a heurística.
        """
        try:
            html_lang = await page.evaluate("document.documentElement.lang || ''")
        except Exception:  # noqa: BLE001
            html_lang = ""
        logger.info(
            "locale-telemetry: documentElement.lang=%r url=%s",
            html_lang,
            getattr(page, "url", "?"),
        )
        return html_lang

    # Página "Idioma e região" (USF, Meta 2025+): NÃO existe mais <select>
    # nativo — cada ajuste é uma linha div[role=button] que abre um picker
    # rolável (CometUSFScrollablePickerRow). Padrões calibrados com DOM real
    # de produção (.state/debug_locale_force_*.html, 2026-06).
    _LOCALE_ROW_ACCOUNT_LANGUAGE = re.compile(
        r"account language|idioma da conta", re.I
    )
    _LOCALE_ROW_DATE_FORMAT = re.compile(
        r"formats? for dates|formatos? (?:de|para) datas?"
        r"|datas?, horas? e n[úu]meros|dates, times and numbers",
        re.I,
    )
    # Nome NATIVO do idioma — renderiza igual em qualquer idioma de UI
    # (inclusive bengali), então é o único texto realmente locale-agnóstico.
    _LOCALE_OPTION_PT_BR = re.compile(r"portugu[êe]s\s*\(brasil\)", re.I)
    _LOCALE_OPTION_REGION_BR = re.compile(
        r"portugu[êe]s\s*\(brasil\)|\bbrasil\b|\bbrazil\b", re.I
    )

    async def _click_settings_row(
        self, page: Page, locators: list[Locator], tag: str
    ) -> bool:
        """Clica numa linha de configuração USF (abre o picker correspondente).

        Devolve False com DOM salvo para calibração em vez de levantar —
        o step de locale é hardening e não pode derrubar a esteira.

        REESCRITA 2026-07-09: Estratégia agressiva com retry automático.
        """
        logger.info("_click_settings_row: abrindo picker (%s)…", tag)

        for attempt in range(1, 6):  # 5 tentativas
            try:
                # Tenta encontrar a linha
                row = None
                try:
                    row = await self._first_visible(page, locators, timeout_ms=2000)
                except Exception as e:
                    logger.debug("_click_settings_row [%d]: _first_visible falhou: %s", attempt, type(e).__name__)
                    if attempt == 5:
                        await self._dump_debug_html(page, tag)
                        return False
                    await asyncio.sleep(1.0)
                    continue

                if not row:
                    logger.debug("_click_settings_row [%d]: row é None", attempt)
                    await asyncio.sleep(1.0)
                    continue

                # Valida que está visível
                is_visible = await row.is_visible(timeout=1000)
                if not is_visible:
                    logger.debug("_click_settings_row [%d]: row não está visível", attempt)
                    await asyncio.sleep(1.0)
                    continue

                # Scroll para garantir que está na viewport
                try:
                    await row.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

                # Clica com estratégia robusta
                logger.info("_click_settings_row [%d]: clicando a linha…", attempt)
                await self._robust_click(row)

                # Aguarda o picker renderizar
                logger.info("_click_settings_row [%d]: aguardando picker (5s)…", attempt)
                await asyncio.sleep(5.0)
                await self._organic_wait()

                # Valida que o picker abriu (procura por algum elemento do picker)
                picker_indicators = [
                    page.locator('[role="dialog"]'),
                    page.locator('[role="listbox"]'),
                    page.locator('[role="menu"]'),
                    page.locator('[role="option"]'),
                    page.locator('div[role="radio"]'),
                ]

                picker_found = False
                for indicator in picker_indicators:
                    try:
                        if await indicator.first.is_visible(timeout=1000):
                            picker_found = True
                            logger.info("_click_settings_row [%d]: ✅ picker detectado!", attempt)
                            break
                    except Exception:
                        pass

                if picker_found:
                    return True

                logger.debug("_click_settings_row [%d]: picker não foi detectado", attempt)
                await asyncio.sleep(1.0)

            except Exception as exc:  # noqa: BLE001
                logger.debug("_click_settings_row [%d] erro: %s", attempt, type(exc).__name__)
                await asyncio.sleep(1.0)

        # Falhou em todas as 5 tentativas
        await self._dump_debug_html(page, tag)
        logger.error("_click_settings_row: ❌ Falhou em 5 tentativas de abrir o picker")
        return False

    async def _select_picker_option(
        self, page: Page, pattern: re.Pattern, tag: str
    ) -> bool:
        """Seleciona uma opção no picker rolável aberto por uma linha USF.

        O picker não é <select>: as opções são radios/linhas clicáveis e a
        seleção aplica na hora (sem botão Salvar). Roles ARIA primeiro;
        texto cru como último recurso.

        REESCRITA 2026-07-09: Mais rounds, retry automático, validação pós-clique.
        """
        candidates = [
            page.get_by_role("radio", name=pattern),
            page.get_by_role("option", name=pattern),
            page.get_by_role("button", name=pattern),
            page.get_by_text(pattern),
        ]

        logger.info("_select_picker_option: procurando por padrão: %s (%s)…", pattern.pattern[:50], tag)

        try:
            # Aumentado de 8 rounds × 1.5s para 12 rounds × 2s (total 24s)
            # para contas muito lentas onde o React renderiza o picker muito devagar
            option = await self._first_visible_scan(
                page, candidates, rounds=12, round_wait_s=2.0
            )

            logger.info("_select_picker_option: ✅ opção encontrada! Clicando…")

            # Scroll para garantir visibilidade
            try:
                await option.scroll_into_view_if_needed()
                await asyncio.sleep(0.3)
            except Exception:
                pass

            # Clica com robustez
            await self._robust_click(option)

            # Aguarda a aplicação da seleção
            logger.info("_select_picker_option: aguardando aplicação da seleção (3s)…")
            await asyncio.sleep(3.0)
            await self._organic_wait()

            logger.info("_select_picker_option: ✅ SUCESSO!")
            return True

        except Exception as exc:  # noqa: BLE001
            await self._dump_debug_html(page, tag)
            logger.error(
                "Opção do picker não encontrada (%s): %s (DOM salvo).", tag, exc
            )
            return False

    async def _select_pais_brasil(self, page: Page, combobox: "Locator") -> bool:
        """Seleciona 'Brasil' num combobox de País (não-<select>).

        O campo País do diálogo 'Editar detalhes da empresa' é um combobox
        custom: digitar nele não funciona e pular deixava o campo vazio
        ('Selecione um país no menu suspenso'). Abre o dropdown, filtra por
        'Brasil' quando há caixa de busca, e clica na opção.

        RACE CONDITION (2026-06-17): combobox tem aria-expanded=true e value='Brasil',
        mas a listbox (aria-controls=...) não existe no DOM (hidratação do React
        incompleta). Solução: aguardar a listbox renderizar antes de buscar options.

        MUDANÇA 2026-07-01: Sem reload (que trava a página), apenas tenta abrir/fechar
        o combobox repetidamente com esperas progressivas até renderizar a listbox.
        """
        brasil_re = re.compile(r"^\s*bra[sz]il\s*$", re.I)
        # Estratégia: tenta abrir e selecionar Brasil até 10 vezes
        # sem fazer reload (reload trava a página)
        for attempt in range(1, 11):
            try:
                logger.info("País: tentativa %d/10 - clicando combobox", attempt)
                await self._robust_click(combobox)
                # Aumenta o sleep progressivamente conforme as tentativas
                sleep_time = 1.0 + (attempt * 0.5)  # 1.5s, 2.0s, 2.5s, ...
                await asyncio.sleep(sleep_time)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "País: não foi possível abrir o combobox (tentativa %d/10): %s",
                    attempt, exc,
                )
                if attempt < 10:
                    await asyncio.sleep(2.0)
                    continue
                return False

            # O dropdown abre com uma caixa de busca JÁ focada (visto em produção
            # 2026-06: 'Q Bras…'). Digita 'Brasil' via TECLADO — usar fill() num
            # locator page-wide pegava o PRIMEIRO textbox (Razão social, no topo
            # do diálogo) e corrompia esse campo em vez de filtrar a lista.
            try:
                logger.info("País: digitando 'Brasil' no filtro")
                await page.keyboard.type("Brasil", delay=200)
                await asyncio.sleep(2.0)  # Aguarda mais tempo para o filtro processar
            except Exception as exc:  # noqa: BLE001
                logger.warning("País: digitação 'Brasil' no filtro falhou (%s).", exc)
                # Fecha o dropdown e tenta novamente
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(3.0)
                continue

            # ESPERA CRÍTICA: aguardar a listbox (aria-controls) renderizar.
            # Aumentado para 30s considerando React lento no Facebook
            logger.info("País: aguardando listbox renderizar (timeout=30s)")
            listbox_rendered = await self._wait_for_listbox_options(
                page, combobox, timeout_ms=30000
            )

            # Estratégia 1: achar e clicar a opção (funciona quando a lista renderiza).
            if await self._select_picker_option(page, brasil_re, tag="pais_picker"):
                logger.success("País selecionado: Brasil (clique direto).")
                return True

            # Estratégia 2: TECLADO. A lista é virtual e às vezes NÃO monta no DOM
            # (aria-owns aponta p/ um id inexistente — análise do DOM da run 9,
            # jun/2026: zero role=option). Aí não há o que clicar, MAS o combobox
            # responde a teclas: ArrowDown destaca a 1ª opção filtrada ('Brasil')
            # e Enter seleciona. Confirma pelo aria-expanded fechar (dropdown sumiu).
            try:
                logger.info("País: tentando seleção por teclado (ArrowDown+Enter)")
                await page.keyboard.press("ArrowDown")
                await asyncio.sleep(2.0)  # Espera mais tempo para highlight aparecer
                await page.keyboard.press("Enter")
                await asyncio.sleep(2.0)  # Espera mais tempo pós-seleção
                expanded = await combobox.first.get_attribute("aria-expanded")
                if (expanded or "").lower() != "true":
                    logger.success("País selecionado: Brasil (via teclado).")
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("País: seleção por teclado falhou (%s).", exc)

            # Fecha o dropdown e tenta novamente na próxima iteração
            if attempt < 10:
                logger.info("País: fechando combobox e tentando novamente")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(3.0)

        logger.error("País CRÍTICO: não conseguiu selecionar Brasil após 10 tentativas")
        return False

    async def _wait_for_listbox_options(
        self, page: Page, combobox: "Locator", timeout_ms: int = 3000
    ) -> bool:
        """Aguarda a listbox (aria-controls) e suas opções renderizarem.

        Quando o combobox está expandido (aria-expanded=true), ele aponta para
        uma listbox via aria-controls="<id>". Este método aguarda que o elemento
        com aquele ID exista e contenha ao menos uma opção (role=option ou
        role=radio). Isso defende contra race conditions de hidratação React.

        Retorna True se encontrou opções, False se estorou o timeout.
        """
        try:
            aria_controls = await combobox.first.get_attribute("aria-controls")
            if not aria_controls:
                # Sem aria-controls, não há listbox a aguardar.
                return False

            # Aguarda o elemento com id=aria_controls existir (até timeout_ms)
            try:
                listbox = page.locator(f"#{aria_controls}")
                await listbox.first.wait_for(state="attached", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning(
                    "País: listbox aria-controls='%s' não apareceu em %dms.",
                    aria_controls, timeout_ms,
                )
                return False

            # Listbox existe. Aguarda role=option ou role=radio visível.
            candidates = [
                page.get_by_role("option"),
                page.get_by_role("radio"),
            ]
            for candidate in candidates:
                try:
                    # Aumentado de 1500ms para 10000ms (React pode ser lento)
                    await candidate.first.wait_for(state="visible", timeout=10000)
                    return True  # Achou pelo menos 1 opção visível
                except PlaywrightTimeoutError:
                    continue

            logger.warning(
                "País: listbox '%s' renderizou mas sem opções visíveis.",
                aria_controls,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "País: erro ao aguardar listbox renderizar: %s", exc
            )
            return False

    async def _ensure_region_format(self, page: Page) -> None:
        """Ajusta o formato de datas/horas/números para o padrão Brasil.

        Equivale ao 2º passo do operador: Configurações → Idioma → "ver
        todas as configurações" → formato para data e hora → Brasil. Roda
        DEPOIS da troca de idioma (a página já tende a renderizar em
        pt-BR), mas os padrões cobrem PT e EN porque a propagação do novo
        idioma pode atrasar uma navegação. Best-effort por design: falha
        loga warning e segue — formato de data é cosmético perto dos
        seletores de texto que motivam a troca de idioma.
        """
        try:
            await page.goto(
                self._with_locale_param(LANGUAGE_PREFERENCES_URL, TARGET_LOCALE),
                wait_until="domcontentloaded",
            )
            await self._organic_wait()
            opened = await self._click_settings_row(
                page,
                [page.get_by_role("button", name=self._LOCALE_ROW_DATE_FORMAT)],
                tag="locale_region_row",
            )
            if not opened:
                return
            if await self._select_picker_option(
                page, self._LOCALE_OPTION_REGION_BR, tag="locale_region_picker"
            ):
                logger.success(
                    "Formato regional (datas/horas/números) ajustado para Brasil."
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Formato regional não ajustado: %s — seguindo (best-effort).", exc
            )

    def _profile_cnpj_digits(self) -> str:
        from utils.helpers import only_digits  # noqa: PLC0415

        return only_digits((self.cnpj_data.cnpj or "") if self.cnpj_data else "")

    def _profile_flag(self, flag: str) -> bool:
        """True se o flag está gravado no registro do perfil deste CNPJ."""
        from utils.profiles_state import find_profile_by_cnpj  # noqa: PLC0415

        cnpj = self._profile_cnpj_digits()
        if len(cnpj) != 14:
            return False
        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
        record = find_profile_by_cnpj(cnpj, provider=_bp) or {}
        return bool(record.get(flag))

    def _set_profile_flag(self, flag: str) -> None:
        """Grava um flag no registro do perfil (best-effort)."""
        from utils.profiles_state import set_profile_flag  # noqa: PLC0415

        cnpj = self._profile_cnpj_digits()
        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
        if len(cnpj) == 14:
            try:
                set_profile_flag(cnpj, flag, True, provider=_bp)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flag %s não persistido (%s).", flag, exc)

    def _profile_locale_ok(self) -> bool:
        """True se este perfil já teve idioma+formato configurados (flag)."""
        return self._profile_flag("locale_ok")

    def _mark_profile_locale_ok(self) -> None:
        """Grava o flag locale_ok no registro do perfil (best-effort)."""
        self._set_profile_flag("locale_ok")

    async def _ensure_locale(self, page: Page) -> bool:
        """Garante que a interface esteja em pt-BR (locale-agnóstica).

        Fluxo do operador replicado por URL direta (mais estável que abrir
        o menu da foto de perfil): foto de perfil → Configurações → Idioma
        → Português; depois Configurações → Idioma → ver todas → formato
        de data e hora → Brasil. A URL ``settings/?tab=language_and_region``
        cai na mesma tela final.

        Estratégia em duas camadas — o idioma da CONTA é sticky no servidor:
          1. **Setting da conta** (autoritativo): linha "Idioma da conta"
             abre um picker; selecionar "Português (Brasil)" aplica na hora
             e vale para TODAS as telas seguintes (2FA, portfólio, domínio,
             WhatsApp). Depois, formato regional → Brasil (best-effort).
          2. **``?locale=pt_BR`` na navegação** (complemento): força a página
             de preferências a renderizar em pt-BR, então mesmo partindo de
             bengali os fallbacks por texto desta função são válidos NESTA
             página. Sozinho não basta — a SPA não propaga o parâmetro.

        Gatilho: idioma fora de pt/en dispara SEMPRE (auto); pt/en respeita
        FORCE_LOCALE (seletores da esteira são bilíngues).
        """
        html_lang = await self._detect_interface_lang(page)
        normalized = self._normalize_lang(html_lang)
        if not normalized:
            # Detecção pode falhar logo após o load (SPA ainda montando o
            # <html lang>) — re-tenta uma vez antes de decidir forçar, senão
            # contas já em português caíam no fluxo de settings à toa.
            await asyncio.sleep(1.5)
            html_lang = await self._detect_interface_lang(page)
            normalized = self._normalize_lang(html_lang)
        if normalized.startswith("pt"):
            # Qualquer variante de português (pt_br, pt, pt_pt) já atende:
            # os seletores da esteira são PT e o picker de País mostra
            # "Brasil". Ir na tela de settings nesse caso só queima tempo.
            logger.info(
                "Interface já em português (lang=%s) — pulando etapa de "
                "configuração de idioma/região.",
                html_lang,
            )
            return False

        # Cache por perfil: o setting de idioma/formato é STICKY no servidor
        # do Meta — uma vez configurado NESTE perfil, repetir o fluxo em todo
        # re-run custava ~12-25s à toa (visto em produção 2026-06: 'lang
        # pt → pt' forçado em todos os runs). O flag locale_ok é gravado no
        # adspower_profiles.json após o primeiro sucesso.
        # DESABILITADO 2026-07-01: cache causava que linguagem não era alterada
        # para pt-BR em alguns runs, deixando em inglês (Brazil vs Brasil).
        # Agora força mudança de idioma em TODA run.
        # if self._profile_locale_ok():
        #     logger.info(
        #         "Locale já configurado neste perfil (flag locale_ok) — skip."
        #     )
        #     return False

        # DESABILITADO 2026-07-01: Mesmo que FORCE_LOCALE=false, agora SEMPRE
        # força pt-BR para garantir que o picker de País mostre "Brasil" em
        # vez de "Brazil". Seletores bilíngues funciona, mas o país fica errado.
        # if not self._locale_needs_normalization(normalized, FORCE_LOCALE):
        #     logger.info(
        #         "Idioma atual=%s — dentro de pt/en e FORCE_LOCALE=false, "
        #         "seguindo sem forçar (seletores são bilíngues).",
        #         html_lang or "?",
        #     )
        #     return False

        logger.warning(
            "Idioma atual=%s fora do esperado — forçando %s via setting da conta.",
            html_lang or "?",
            TARGET_LOCALE,
        )
        try:
            await page.goto(
                self._with_locale_param(LANGUAGE_PREFERENCES_URL, TARGET_LOCALE),
                wait_until="domcontentloaded",
            )
            await self._organic_wait()

            # 1) Linha "Idioma da conta" abre o picker. Fallback estrutural
            #    (vale para idiomas exóticos, onde o texto não casa pt/en):
            #    é a PRIMEIRA linha clicável da aba "Idioma e região".
            opened = await self._click_settings_row(
                page,
                [
                    page.get_by_role(
                        "button", name=self._LOCALE_ROW_ACCOUNT_LANGUAGE
                    ),
                    page.locator('div[role="main"] div[role="button"]'),
                ],
                tag="locale_row",
            )
            if not opened:
                return False

            # 2) Opção "Português (Brasil)" no picker — nome nativo.
            if not await self._select_picker_option(
                page, self._LOCALE_OPTION_PT_BR, tag="locale_picker"
            ):
                return False

            # O picker aplica imediatamente; a SPA re-renderiza em pt-BR.
            # Telemetria antes → depois (o setting pode só refletir na
            # próxima navegação, então não falha se ainda não mudou).
            post_lang = await self._detect_interface_lang(page)
            logger.success(
                "Idioma da conta alterado para pt-BR (lang %s → %s).",
                html_lang or "?",
                post_lang or "?",
            )

            # 3) Formato de datas/horas/números → Brasil (best-effort).
            await self._ensure_region_format(page)
            self._mark_profile_locale_ok()
            return True
        except Exception as exc:  # noqa: BLE001
            # Falha ao localizar o seletor de idioma / salvar — captura o DOM
            # da página de preferências para calibrar (seletores podem estar
            # desatualizados; este caminho nunca rodou em produção até agora).
            await self._dump_debug_html(page, "locale_force")
            logger.warning(
                "Não foi possível forçar pt-BR: %s (DOM salvo p/ diagnóstico).", exc
            )
            return False

    # ------------------------------------------------------------------
    # Business portfolio
    # ------------------------------------------------------------------
    # Captura o ID da BM em URLs da Meta Business Suite autenticada
    # (business.facebook.com/latest/...?business_id=<ID>). Independe de
    # idioma — casa parâmetro de query, não texto renderizado.
    _BUSINESS_ID_RE = re.compile(r"[?&]business_id=(\d+)")

    # Título do diálogo de criação de portfólio. Em pt-BR o Meta usa o
    # IMPERATIVO "Crie seu portfólio empresarial no Gerenciador de Negócios"
    # (visto em produção 2026-06, .state/fail_*.html) — "criar" sozinho não
    # casa e o filtro do diálogo falhava com a UI em português.
    _PORTFOLIO_DIALOG_TITLE_RE = re.compile(
        # Cobre os 3 títulos vistos em produção:
        #   "Create your business portfolio" (EN)
        #   "Crie seu portfólio empresarial" (pt-BR, fluxo antigo)
        #   "Criar um portfólio empresarial" (pt-BR, diálogo NOVO com
        #    Nome/Sobrenome separados) — o "um/uma" entre criar e portfólio
        #    fazia o título escapar do regex e o diálogo não era detectado.
        r"create your business portfolio"
        r"|cri(?:ar|e)\s+(?:(?:seu|sua|um|uma)\s+)?portf[oó]lio",
        re.I,
    )

    # Erro explícito de nome inválido na criação do portfólio.
    # A Meta mostra 'Unable to Create Account' (EN) ou 'Não foi possível
    # criar a conta' (pt-BR) com subtexto 'O nome escolhido … não é
    # adequado'. Quando detectado, troca o CNPJ IMEDIATAMENTE (sem a
    # espera de 30s do retry-com-mesmo-CNPJ) — o erro é definitivo,
    # nenhum delay resolve (pedido do operador 2026-06-21).
    _PORTFOLIO_NAME_REJECT_RE = re.compile(
        r"unable to create account"
        r"|n[ãa]o foi poss[íi]vel criar a? ?conta"
        r"|nome.*n[ãa]o.*adequado|n[ãa]o.*adequado.*nome"
        r"|name.*not.*appropriate|not.*appropriate.*name",
        re.I,
    )

    # Nome do portfólio DUPLICADO ('Você já tem um portfólio comercial com
    # esse nome'). É DISTINTO da rejeição por nome inadequado: aqui o nome é
    # válido, só já foi usado numa BM anterior deste mesmo usuário — o CNPJ
    # continua bom. Pedido do operador (2026-09): NÃO descartar a conta/CNPJ;
    # re-tentar com um sufixo numérico (' 2', ' 3', …) SÓ no campo 'Nome da
    # sua conta e empresa'.
    _PORTFOLIO_NAME_DUPLICATE_RE = re.compile(
        r"j[áa] tem um portf[óo]lio"
        r"|j[áa] existe um portf[óo]lio"
        r"|j[áa] possui um portf[óo]lio"
        r"|(esse|este) nome j[áa] est[áa] em uso"
        r"|nome j[áa] (est[áa] )?em uso"
        r"|already have a business portfolio"
        r"|name is already (taken|in use)"
        r"|escolha (um )?outro nome|choose a different name",
        re.I,
    )
    # Quantos sufixos tentar (' 2', ' 3', ' 4') antes de desistir.
    _PORTFOLIO_DUP_MAX_RETRIES: int = 3

    # CTA de criação no overview. Bilíngue + infixos enumerados: casa
    # "Create NEW account", "Criar NOVA conta", "Create a business
    # portfolio" mas NÃO o link de rodapé "Create and manage accounts".
    # Não exige locale pt-BR (o forçar-locale é best-effort).
    _PORTFOLIO_CREATE_CTA_RE = re.compile(
        r"(criar|create)( (a|uma|um|nov[ao]|new))?"
        r"( (business|comercial|de neg[oó]cios))? "
        r"(conta|account|portf[oó]lio|portfolio)",
        re.I,
    )
    # Fluxo IN-APP na home LOGADA (screenshot do operador): o seletor de
    # portfólios (canto sup. esq.) revela TODOS os portfólios + o botão 'Criar
    # um portfólio empresarial'. Usado quando o deep-link bounceia.
    _PORTFOLIO_SWITCHER_CREATE_RE = re.compile(
        r"criar (um |uma )?portf[óo]lio empresarial"
        r"|create (a )?business portfolio",
        re.I,
    )
    _PORTFOLIO_LIST_HEADING_RE = re.compile(
        r"portf[óo]lios empresariais|business portfolios", re.I,
    )
    # Botão "Criar empresa"/"Create Business" da página /select/ ("Selecionar
    # empresa"). Caminho CONFIÁVEL de criação numa conta que já tem portfólios
    # (aged) — o seletor do header vem ofuscado e o deep-link bounceia, mas
    # este botão abre o diálogo normalmente (screenshot do operador 2026-09).
    _PORTFOLIO_SELECT_CREATE_RE = re.compile(
        r"criar (uma |um )?(empresa|neg[óo]cio|portf[óo]lio)"
        r"|create (a )?business",
        re.I,
    )

    async def _detect_existing_business_id(self, page: Page) -> Optional[str]:
        """Detecta se a conta já tem um portfólio empresarial (BM).

        Contas COM portfólio são redirecionadas para a Meta Business Suite
        autenticada — a URL carrega ``business_id=`` e NÃO existe botão
        "Criar portfólio empresarial" (procurá-lo só rende timeout). Contas
        SEM portfólio ficam no overview com o CTA de criação.

        Duas superfícies, ambas estruturais (não dependem de locale):
          1. URL corrente após o redirect pós-goto.
          2. Fallback: primeiras âncoras do menu da Suite com
             ``business_id=`` no href — cobre rotas que omitem o parâmetro
             na barra de endereço.
        """
        m = self._BUSINESS_ID_RE.search(page.url or "")
        if m:
            return m.group(1)
        try:
            anchors = page.locator('a[href*="business_id="]')
            count = await anchors.count()
            for i in range(min(count, 5)):
                href = await anchors.nth(i).get_attribute("href", timeout=1500) or ""
                m = self._BUSINESS_ID_RE.search(href)
                if m:
                    return m.group(1)
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _collect_portfolio_ids(self, page: Page) -> set[str]:
        """Coleta os ``business_id`` DISTINTOS visíveis na conta.

        Sinal estrutural e locale-agnóstico: cada portfólio empresarial é uma
        BM com seu próprio id (na URL corrente e nos hrefs do menu/seletor da
        Suite). Filtra ids-placeholder (``business_id=0`` aparece em links
        genéricos da Suite e NÃO é um portfólio — inflava a contagem e
        descartava conta boa, visto em produção 2026-06, conta Antonio).

        NÃO usar o texto 'X contas de anúncios' para contar: esse rótulo
        aparece várias vezes para UM só portfólio (card do portfólio + grupo
        'Outros ativos' + painel de detalhes + placeholder 'Procure uma conta
        de anúncios'), o que descartava contas boas por engano — uma conta com
        1 portfólio rendia contagem 4 (visto em produção 2026-06).
        """
        ids: set[str] = set()
        m = self._BUSINESS_ID_RE.search(page.url or "")
        if m:
            ids.add(m.group(1))
        try:
            anchors = page.locator('a[href*="business_id="]')
            n = await anchors.count()
            for i in range(min(n, 30)):
                href = await anchors.nth(i).get_attribute("href", timeout=800) or ""
                mm = self._BUSINESS_ID_RE.search(href)
                if mm:
                    ids.add(mm.group(1))
        except Exception:  # noqa: BLE001
            pass
        # Fonte AUTORITATIVA: o seletor 'Selecionar negócio' lista TODAS as BMs
        # da conta como cards <a href="/?business_id=…">, mas o scan de âncoras
        # acima (cap de 30, sujeito a timing/locator) às vezes só pega a BM
        # CORRENTE da URL. Varrer o HTML inteiro pelos MESMOS params business_id=
        # pega todas — a subcontagem deixava uma conta com 3 BMs passar como 1,
        # furando a regra de descarte em ≥2 e acumulando empresas alheias (run
        # 564eb886 2026-06-17: conta com Alipio+Miguel+Thiago contada como 1).
        # Mesma semântica das âncoras (param business_id = BM da conta) → não
        # infla mais que elas; zeros/placeholder são filtrados logo abaixo.
        try:
            html = await asyncio.wait_for(page.content(), timeout=5.0)
            for mm in self._BUSINESS_ID_RE.finditer(html):
                ids.add(mm.group(1))
        except Exception:  # noqa: BLE001
            pass
        # business_id=0 (e variantes só-zeros) = placeholder, não portfólio.
        ids = {i for i in ids if i.strip("0")}
        # Stash p/ o run_service decidir reuso da conta (sem 2 portfólios volta
        # ao pool). MONOTÔNICO: guarda o MÁXIMO de BMs visto no run — uma coleta
        # numa página default às vezes regride p/ a BM corrente (1), perdendo o
        # total real já visto quando o seletor 'Selecionar negócio' apareceu.
        # settle_fb_account também faz max(); aqui evitamos alimentá-lo baixo.
        self._portfolio_count = max(self._portfolio_count or 0, len(ids))
        logger.info(
            "portfolio-count: %d business_id distinto(s) detectado(s): %s "
            "(máx. no run=%d)",
            len(ids),
            sorted(ids) or "(nenhum)",
            self._portfolio_count,
        )
        return ids

    async def _count_existing_portfolios(self, page: Page) -> int:
        """Conta portfólios (BMs) DISTINTOS visíveis na conta."""
        return len(await self._collect_portfolio_ids(page))

    async def _business_id_for_name(
        self, page: Page, name: Optional[str]
    ) -> Optional[str]:
        """Acha o business_id do portfólio cujo nome bate com ``name``.

        Sinal: no menu/seletor da Suite, o nome do portfólio e seu link com
        ``business_id=`` ficam no mesmo container (âncora ou ancestral
        próximo). Usado para desambiguar QUAL portfólio é o recém-criado
        quando o diff de ids não basta (conta com vários portfólios).
        """
        if not name or not name.strip():
            return None
        target = re.sub(r"\s+", " ", name).strip().casefold()
        js = r"""
        (name) => {
          const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
          const want = norm(name);
          const re = /business_id=(\d{3,})/;
          const anchors = Array.from(document.querySelectorAll('a[href*="business_id="]'));
          for (const a of anchors) {
            // texto da âncora ou de até 3 ancestrais
            let node = a, hops = 0, txt = '';
            while (node && hops < 4) { txt += ' ' + (node.innerText || node.textContent || ''); node = node.parentElement; hops++; }
            if (norm(txt).includes(want)) {
              const m = (a.getAttribute('href') || '').match(re);
              if (m) return m[1];
            }
          }
          return null;
        }
        """
        try:
            bid = await page.evaluate(js, name)
        except Exception:  # noqa: BLE001
            return None
        if bid and bid.strip("0"):
            logger.info(
                "portfolio: business_id %s casado pelo nome %r.", bid, name
            )
            return bid
        return None

    @staticmethod
    def _norm_portfolio_name(name: Optional[str]) -> str:
        """Normaliza nome de portfólio p/ comparação (casefold + espaços)."""
        return " ".join((name or "").split()).casefold()

    async def _business_ids_via_graph(self, page: Page) -> list[dict[str, str]]:
        """Lista os portfólios da conta via Graph API (me/businesses), rodando o
        fetch DENTRO da página (mesma sessão/cookies/IP do navegador).

        Fonte AUTORITATIVA do business_id quando o DOM do /overview NÃO o expõe:
        conta NOVA cai na tela 'Unable to access Meta Business Suite' após criar
        o 1º portfólio (sem business_id na URL) — o diff fica 0×0 mesmo com o
        portfólio criado (run 2026-09, conta Joyce). Best-effort: [] se não
        houver token capturado ou a chamada falhar."""
        res = getattr(self, "last_result", None)
        token = None
        if res is not None:
            token = getattr(res, "meta_token", None) or getattr(res, "verify_token", None)
        if not token:
            return []
        try:
            data = await asyncio.wait_for(
                page.evaluate(
                    """async (tok) => {
                        const out = [];
                        let url = 'https://graph.facebook.com/v19.0/me/businesses'
                            + '?fields=id,name&limit=200&access_token='
                            + encodeURIComponent(tok);
                        for (let i = 0; i < 5 && url; i++) {
                            let r;
                            try { r = await fetch(url, { credentials: 'include' }); }
                            catch (e) { break; }
                            if (!r.ok) break;
                            const j = await r.json();
                            if (j && Array.isArray(j.data)) {
                                for (const b of j.data) {
                                    out.push({ id: String(b.id), name: b.name || '' });
                                }
                            }
                            url = (j && j.paging && j.paging.next) ? j.paging.next : null;
                        }
                        return out;
                    }""",
                    token,
                ),
                timeout=15.0,
            )
            return [
                {"id": str(b.get("id")), "name": str(b.get("name") or "")}
                for b in (data or [])
                if b.get("id")
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph me/businesses falhou (%s).", type(exc).__name__)
            return []

    async def _capture_new_business_id(
        self, page: Page, *, portfolio_name: Optional[str] = None
    ) -> Optional[str]:
        """business_id do portfólio RECÉM-criado (não o default do overview).

        Precedência:
          1. diff (depois − antes) — se exatamente 1 id novo, é ele.
          2. match por NOME (desambigua quando há vários ids novos/antigos).
          3. diff>1 sem match de nome → menor id novo (determinístico).
        NUNCA cai no _detect_existing_business_id (id default = portfólio
        ERRADO numa conta com portfólio pré-existente).
        """
        before = getattr(self, "_pre_creation_portfolio_ids", set()) or set()
        # O id do portfólio recém-criado PROPAGA com ATRASO no overview: logo
        # após 'Concluir' às vezes só o id PRÉ-EXISTENTE aparece na DOM (diff
        # vazio → id não capturado → passos 2/3 abriam o portfólio ERRADO: 0/11
        # campos + chooser de domínio ausente; visto 2026-06 em conta COM
        # portfólio prévio — o id novo só surgia ~segundos depois). Re-poll até
        # um id novo aparecer (ou o nome casar) antes de desistir.
        after = await self._collect_portfolio_ids(page)
        new_ids = after - before
        logger.info(
            "portfolio: capturando novo business_id (antes=%s, depois=%s, novos=%s)",
            sorted(before) or "(nenhum)", sorted(after) or "(nenhum)", sorted(new_ids) or "(nenhum)",
        )
        for attempt in range(8):  # 6 → 8 tentativas (mais tempo para propagar)
            if new_ids:
                break
            named_probe = await self._business_id_for_name(page, portfolio_name)
            if named_probe and named_probe not in before:
                new_ids = {named_probe}
                break
            # Aguarda mais tempo entre tentativas (2026-06-22: alguns navegadores demoram)
            await asyncio.sleep(1.5)
            # O re-poll relê o MESMO DOM; se a SPA do Business Suite ainda não
            # propagou o id novo, ele NUNCA aparece só esperando. Recarrega o
            # overview no meio do loop p/ forçar a re-busca da lista — causa do
            # 'diff vazio' em conta com portfólio prévio (run 2026-06-18 00:44).
            if attempt in (2, 5):  # 1,3 → 2,5 (mais agressivo)
                logger.info(
                    "portfolio: diff ainda vazio (tentativa %d/8) — recarregando o overview p/ a "
                    "SPA propagar o portfólio novo.",  attempt + 1,
                )
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)  # Espera mais após reload
                except Exception as exc:  # noqa: BLE001
                    logger.warning("portfolio: reload do overview falhou (%s).", exc)
            after = await self._collect_portfolio_ids(page)
            new_ids = after - before
        if len(new_ids) == 1:
            bid = next(iter(new_ids))
            logger.success(
                "portfolio: business_id NOVO identificado por diff: %s "
                "(antes=%s).", bid, sorted(before) or "(nenhum)",
            )
            return bid
        named = await self._business_id_for_name(page, portfolio_name)
        if named and (not new_ids or named in new_ids):
            return named
        if len(new_ids) > 1:
            bid = sorted(new_ids)[0]
            logger.warning(
                "portfolio: %d ids novos no diff e nome não desambiguou — "
                "usando o menor (%s). Novos=%s.",
                len(new_ids), bid, sorted(new_ids),
            )
            return bid
        if named:
            return named

        # FALLBACK 1 — Graph API (AUTORITATIVO): conta NOVA cai na tela 'Unable
        # to access Meta Business Suite' após criar o 1º portfólio, e o /overview
        # NÃO expõe o business_id na URL/DOM → diff fica 0×0 mesmo com o
        # portfólio já criado (run 2026-09, conta Joyce). me/businesses lista os
        # portfólios direto da Graph, na MESMA sessão do navegador.
        graph = await self._business_ids_via_graph(page)
        if graph:
            want = self._norm_portfolio_name(portfolio_name)
            for b in graph:
                if (
                    want
                    and self._norm_portfolio_name(b["name"]) == want
                    and b["id"] not in before
                ):
                    logger.success(
                        "portfolio: business_id NOVO via Graph (nome casou): %s",
                        b["id"],
                    )
                    return b["id"]
            fresh = [b["id"] for b in graph if b["id"] not in before]
            if len(fresh) == 1:
                logger.success(
                    "portfolio: business_id NOVO via Graph (único novo): %s",
                    fresh[0],
                )
                return fresh[0]
            if len(fresh) > 1:
                bid = sorted(fresh)[0]
                logger.warning(
                    "portfolio: Graph trouxe %d portfólios novos e o nome não "
                    "desambiguou — usando o menor (%s). Novos=%s.",
                    len(fresh), bid, sorted(fresh),
                )
                return bid

        # FALLBACK 2 — página /select/ ('Selecionar empresa'): lista TODOS os
        # portfólios como <a href="…business_id=…"> mesmo quando o /overview
        # esconde. Última tentativa antes de desistir.
        try:
            await page.goto(BUSINESS_SELECT_URL, wait_until="domcontentloaded")
            await self._organic_wait()
            sel_ids = await self._collect_portfolio_ids(page)
            sel_new = sel_ids - before
            if len(sel_new) == 1:
                bid = next(iter(sel_new))
                logger.success("portfolio: business_id NOVO via /select/: %s", bid)
                return bid
            sel_named = await self._business_id_for_name(page, portfolio_name)
            if sel_named and sel_named not in before:
                logger.success(
                    "portfolio: business_id NOVO via /select/ (nome): %s",
                    sel_named,
                )
                return sel_named
        except Exception as exc:  # noqa: BLE001
            logger.debug("portfolio: probe /select/ falhou (%s).", type(exc).__name__)

        logger.error(
            "portfolio: NÃO identifiquei o business_id do portfólio novo "
            "(diff vazio, nome não casou, Graph e /select/ vazios). Antes=%s, "
            "depois=%s. NÃO vou persistir o id default (seria o portfólio "
            "errado).",
            sorted(before) or "(nenhum)", sorted(after) or "(nenhum)",
        )
        return None

    def _persist_business_mapping(self, business_id: str, *, source: str) -> None:
        """Grava CNPJ → business_id em ``.state/business_portfolios.json``.

        Best-effort: CNPJ ausente/inválido apenas loga warning — o registro
        é inventário/idempotência, não pode derrubar o pipeline (a BM no
        Meta já existe de qualquer forma).
        """
        from utils.helpers import only_digits  # noqa: PLC0415
        from utils.business_state import save_business  # noqa: PLC0415

        raw_cnpj = (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
        cnpj_digits = only_digits(raw_cnpj)
        if len(cnpj_digits) != 14:
            logger.warning(
                "business_state: CNPJ inválido (%r) — mapeamento "
                "business_id=%s NÃO persistido.", raw_cnpj, business_id,
            )
            return
        save_business(
            cnpj_digits,
            business_id=business_id,
            portfolio_name=self._resolve_portfolio_name(),
            source=source,
        )
        logger.info(
            "business_state: CNPJ %s → business_id=%s persistido (source=%s).",
            cnpj_digits, business_id, source,
        )

    def _resolve_portfolio_name(self) -> str:
        profile = self.business_profile
        if profile and profile.portfolio_name:
            candidate = profile.portfolio_name
        else:
            candidate = (
                self.cnpj_data.nome_fantasia
                or self.cnpj_data.razao_social
                or "Novo Portfólio"
            )
        return self._humanize_person_name(candidate)

    @staticmethod
    def _sanitize_business_name(name: str) -> str:
        """Remove caracteres que o Meta rejeita no nome do portfólio.

        O formulário "Create your business portfolio" recusa nomes com
        caracteres especiais ("It can't contain special characters") e bloqueia
        o Submit em silêncio. Nomes derivados de CNPJ chegam com pontos
        (ex.: "64.434.703 WEMERSON ...") — visto em produção 2026-06 travando a
        criação. Mantém letras (com acento), dígitos e espaços; remove o resto
        e colapsa espaços.
        """
        cleaned = re.sub(r"[^\w\s]", "", name or "", flags=re.UNICODE)
        cleaned = cleaned.replace("_", "")
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _razao_social_for_form(razao: str) -> str:
        """Razão social para o CAMPO 'Razão social da empresa'.

        MANTÉM o valor oficial como vem — INCLUSIVE a raiz do CNPJ na FRENTE
        ('97.554.463 ISLENE SANTOS COSTA' fica idêntico, pedido do operador) —
        e remove APENAS número(s) puramente numérico(s) NO FIM: o CPF que a API
        às vezes anexa errado ('JADER AMADO DA SILVA 01841413119' → 'JADER AMADO
        DA SILVA'). Distinto do nome do portfólio (que é o nome próprio limpo,
        sem número nenhum — ver _humanize_person_name)."""
        tokens = (razao or "").split()
        while tokens:
            last = (
                tokens[-1].replace(".", "").replace("-", "")
                .replace("/", "").replace(",", "")
            )
            if last and last.isdigit():
                tokens.pop()
            else:
                break
        return " ".join(tokens).strip() or (razao or "")

    # Conectores que ficam em minúscula no meio de um nome próprio PT/BR
    # ("José da Silva", "Maria dos Santos"). Nunca minúsculos na 1ª posição.
    _NAME_LOWER_CONNECTORS: frozenset[str] = frozenset(
        {"de", "da", "do", "das", "dos", "e", "di", "du", "del", "la", "das"}
    )

    @staticmethod
    def _humanize_person_name(raw: str) -> str:
        """Normaliza a razão social para um nome próprio em Title Case.

        Pipeline: remove o prefixo numérico (raiz do CNPJ que vem coladado
        em MEIs, ex.: "64460952 RODRIGO ...") → remove caracteres especiais
        (Meta recusa) → Title Case com conectores PT minúsculos. Decisão do
        operador (2026-06): nome próprio limpo, SEM o número do CNPJ.

        Ex.: "64460952 RODRIGO RIBEIRO HENRIQUES" → "Rodrigo Ribeiro Henriques"
             "66.087.509 LENICE DE OLIVEIRA" → "Lenice de Oliveira"
             "JADER AMADO DA SILVA 01841413119" → "Jader Amado da Silva"
        """
        # Sanitiza (remove a pontuação que a Meta recusa) e depois descarta
        # TODOS os tokens puramente numéricos — a raiz do CNPJ pode vir na
        # FRENTE ("64460952 RODRIGO…") OU o CPF/número no FIM ("JADER AMADO DA
        # SILVA 01841413119"), conforme a fonte da API (produção 2026-09, conta
        # Jader). Antes só o prefixo era removido, e o número do fim entrava no
        # nome do portfólio E na verificação, quebrando o cadastro.
        cleaned = AutomationRunner._sanitize_business_name(raw or "")
        words = [w for w in cleaned.split() if not w.isdigit()]
        if not words:
            # Era só número? Último recurso: o sanitize cru (Meta exige nome).
            return cleaned
        out: list[str] = []
        for i, w in enumerate(words):
            low = w.lower()
            if i != 0 and low in AutomationRunner._NAME_LOWER_CONNECTORS:
                out.append(low)
            else:
                out.append(low.capitalize())
        return " ".join(out)

    async def _swap_cnpj_for_rejected_name(self) -> bool:
        """Troca o CNPJ atual por um novo do pool — MESMO perfil/conta AdsPower,
        só o CNPJ muda. Dois chamadores:

          1. Nome do portfólio rejeitado pelo Meta 2x seguidas (diálogo de
             criação continua aberto após o submit — não é glitch transitório).
          2. BM restringida pela Meta ("cadeado vermelho") logo após a
             criação (``_check_business_restriction``) — pedido do operador:
             perfil/conta continuam bons, troca-se só o CNPJ e recria.

        Descarta o CNPJ atual (``store.discard_cnpj`` — não volta ao pool,
        já comprovadamente problemático), reserva outro disponível, consulta
        os dados dele e troca ``self.cnpj_data``/``portfolio_name`` ANTES do
        caller re-tentar ``_create_business_portfolio`` do zero (a função
        renavega para o overview e reabre o diálogo, então não precisa
        limpar nenhum campo aqui — só atualizar o estado do runner).

        Best-effort: devolve False se não há CNPJ livre no pool ou se a
        consulta falhar — o caller mantém o comportamento antigo (levanta o
        erro original) nesse caso.
        """
        from core import store  # noqa: PLC0415 — import tardio evita ciclo
        from core.cnpj_lookup import fetch_cnpj_cached  # noqa: PLC0415

        old_digits = self._profile_cnpj_digits()
        # Exclude: CNPJs já tentados neste run + CNPJ atual
        exclude_set = self._cnpjs_attempted | ({old_digits} if old_digits else set())
        new_digits = store.reserve_one_cnpj(exclude=exclude_set)
        if not new_digits:
            logger.warning(
                "Nome do portfólio rejeitado 2x e NÃO há CNPJ livre no pool "
                "p/ trocar — desistindo (erro original será levantado)."
            )
            return False
        try:
            new_data = await fetch_cnpj_cached(new_digits)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Nome rejeitado 2x — troquei p/ CNPJ %s mas a consulta "
                "falhou (%s); devolvendo ao pool e desistindo.",
                new_digits, exc,
            )
            store.settle_cnpj(new_digits, created_portfolio=False)  # volta a available
            return False

        if old_digits:
            store.discard_cnpj(old_digits)

        # Rastreia o novo CNPJ para evitar tentar o mesmo múltiplas vezes
        self._cnpjs_attempted.add(new_digits)

        logger.warning(
            "CNPJ %s descartado (nome do portfólio rejeitado pelo Meta 2x) — "
            "trocando para o CNPJ %s (tentativas: %s) e refazendo a criação.",
            old_digits or "?", new_digits, len(self._cnpjs_attempted),
        )
        self.cnpj_data = new_data
        if self.business_profile is not None:
            self.business_profile.portfolio_name = (
                new_data.nome_fantasia or new_data.razao_social or None
            )
        return True

    async def _fill_verified(
        self,
        locator: Locator,
        value: str,
        *,
        field: str = "campo",
        retries: int = 1,
    ) -> bool:
        """Limpa→digita→confere→re-tenta. Núcleo anti-travamento da esteira.

        O ``_human_type`` sozinho falha em inputs controlados pelo React: o
        ``fill("")`` não zera o valor pré-preenchido (ex.: "Seu nome" já vem
        com o nome da conta), o texto novo gruda no antigo e o Meta engole o
        Submit em silêncio. Aqui a limpeza é por seleção+Delete (sobrevive ao
        React) e o valor é RELIDO via ``input_value`` — se divergir, re-tenta.
        """
        value = value or ""
        current: Optional[str] = None
        for attempt in range(retries + 1):
            try:
                await locator.click()
                # Limpeza em 3 estágios (2026-07-03): previne resíduo de texto grudado
                # que causa bounce por campo inválido.
                # Estágio 1: Ctrl+A + Delete
                await locator.press("ControlOrMeta+a")
                await locator.press("Delete")
                await asyncio.sleep(0.2)

                # Estágio 2: Verifica se esvaziou, senão chama fill("")
                try:
                    stage2_val = (await locator.input_value()).strip()
                except Exception:  # noqa: BLE001
                    stage2_val = ""
                if stage2_val:
                    await locator.fill("")
                    await asyncio.sleep(0.2)

                # Estágio 3: Verifica novamente, se ainda não esvaziou, usa Backspace repetido
                try:
                    stage3_val = (await locator.input_value()).strip()
                except Exception:  # noqa: BLE001
                    stage3_val = ""
                if stage3_val:
                    await locator.press("End")
                    for _ in range(len(stage3_val) + 5):
                        await locator.press("Backspace")
                    await asyncio.sleep(0.2)

                if attempt == 0:
                    # 1ª via: digitação humana char-a-char.
                    delay_ms = random.randint(TYPING_DELAY_MIN_MS, TYPING_DELAY_MAX_MS)
                    await locator.press_sequentially(value, delay=delay_ms)
                else:
                    # Retry: fill() do Playwright (set value + eventos
                    # input/change) — mais robusto em input controlado React.
                    await locator.fill(value)
                await self._organic_wait()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: erro ao preencher (%s).", field, exc)
                continue
            # Relê o valor REAL. NÃO confia cegamente: se não dá pra ler,
            # tenta via evaluate; valor vazio/divergente = falha (era o furo
            # que deixava submit vazio passar como sucesso — run 1781192632).
            current = None
            try:
                current = (await locator.input_value()).strip()
            except Exception:  # noqa: BLE001
                try:
                    current = ((await locator.evaluate("el => el.value")) or "").strip()
                except Exception:  # noqa: BLE001
                    current = None
            if current is not None and current == value.strip():
                return True
            logger.warning(
                "%s: valor lido %r ≠ esperado %r (tentativa %d/%d) — re-tentando.",
                field, current, value, attempt + 1, retries + 1,
            )
        logger.error(
            "%s: não foi possível cravar %r — último valor lido %r.",
            field, value, current,
        )
        return False

    # Espera entre a 1ª e a 2ª tentativa de criação do portfólio. O erro
    # Delay entre tentativas de criar portfolio.
    # 30s era muito lento (travava), 5s muito rápido (falhava).
    # 8s é o ponto ideal: rápido o suficiente mas seguro (2026-06-22).
    _PORTFOLIO_RETRY_BACKOFF_S: float = 8.0

    async def _create_business_portfolio(
        self,
        page: Page,
        result: AutomationResult,
        *,
        _sso_retry: bool = False,
        _cnpj_swapped: bool = False,
        _dup_suffix: int = 0,
    ) -> None:
        assert self.business_profile is not None  # contract via _run_business_pipeline
        logger.info("Navegando para overview do Business Suite.")
        # Retry na navegação inicial: timeouts transitórios matam a run inteira
        for nav_attempt in range(1, 4):
            try:
                await page.goto(BUSINESS_OVERVIEW_URL, wait_until="domcontentloaded")
                break  # Sucesso, sai do loop
            except PlaywrightTimeoutError as e:
                if nav_attempt < 3:
                    logger.warning(
                        "Timeout ao navegar para overview (tentativa %d/3) — retry em 5s",
                        nav_attempt
                    )
                    await asyncio.sleep(5.0)
                else:
                    logger.error("Timeout ao navegar para overview após 3 tentativas")
                    raise
        await self._organic_wait()
        # CRÍTICO: Detecta e fecha popup de "Alternar perfis" logo após acessar Business
        await self._detect_and_close_profile_modal(page, timeout_ms=5000)
        if not await self._ensure_business_session(
            page, target_url=BUSINESS_OVERVIEW_URL
        ):
            # Seguir aqui seria interagir com a LOGINPAGE: o 'Criar nova
            # conta' dela é SIGNUP (casa com o CTA regex) e o robô passaria
            # a preencher o diálogo errado (visto em produção 2026-06).
            raise LoginError(
                "Sessão do Business Manager não estabelecida (loginpage "
                "persistente após SSO) — abortando antes de interagir com "
                "a página errada. DOM em .state/debug_business_sso_failed_*."
            )
        # GUARD: conta SEM acesso ao Business Suite (sem páginas) — tela "Não é
        # possível acessar o Meta Business Suite com esta conta". Descarta a
        # conta, marca o perfil [PAGINA] e fecha (pedido do operador 2026-06).
        # Aguarda até 8s (com retry) pra página carregar antes de descartar.
        for attempt in range(1, 4):
            await asyncio.sleep(3.0)  # Aguarda 3s entre tentativas
            try:
                await self._guard_no_business_access(page, result)
                break  # Se passou (sem erro), sai do retry
            except AccountDiscardError:
                if attempt < 3:
                    logger.info("Guard no_business_access falhou (tentativa %d/3) — retry…", attempt)
                    continue
                raise
        # Política de não discriminação ("Aceito") e prompts de boas-vindas
        # podem aparecer ao entrar no Gerenciador de Anúncios, ANTES do botão
        # de criar portfólio.
        await self._dismiss_business_interstitials(page)

        portfolio_ids = await self._collect_portfolio_ids(page)
        # Snapshot PRÉ-criação: o business_id do portfólio NOVO é (depois −
        # antes). Sem isso, a captura pós-criação pegava o id DEFAULT do
        # overview (= o portfólio PRÉ-EXISTENTE do dono) e TODO o run operava
        # no portfólio errado — criou 'Paulo dos Santos Tourinho' mas seguiu
        # detalhes/domínio/WhatsApp na 'Izabel…' pré-existente (produção
        # 2026-06, conta Paulo).
        self._pre_creation_portfolio_ids = set(portfolio_ids)

        # O portfólio que NÓS criamos num run anterior (persistido com
        # source='created') não conta contra a conta: re-run reusa ele
        # DIRETO — checar o descarte antes contava o nosso e descartava
        # conta boa (visto em produção 2026-06, Antonio: dono + nosso = 2
        # → descarte indevido logo após criarmos).
        from utils.helpers import only_digits  # noqa: PLC0415
        from utils.business_state import find_business_by_cnpj  # noqa: PLC0415

        cnpj_digits = only_digits(
            (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
        )
        own_record = find_business_by_cnpj(cnpj_digits) or {}
        own_id = (
            own_record.get("business_id")
            if own_record.get("source") == "created"
            else None
        )
        if own_id and own_id in portfolio_ids:
            result.business_id = own_id
            result.portfolio_reused = True
            logger.success(
                "Portfólio criado por run anterior para este CNPJ "
                "(business_id=%s) — reusando e pulando a criação.",
                own_id,
            )
            return
        if own_id and own_id not in portfolio_ids:
            # O portfólio que NÓS criamos para este CNPJ NÃO apareceu no DOM —
            # típico em conta nova que cai na tela 'Unable to access Meta Business
            # Suite' (sem business_id na tela). Confirma via Graph que ele ainda
            # existe e reusa, em vez de criar uma 2ª cópia numa retomada
            # ('Continuar BM' na "Joyce", run 2026-09). Se a Graph não responder
            # (sem token), confia no mapeamento salvo — nós o gravamos de
            # propósito. Se a Graph responder e o id NÃO estiver lá (portfólio
            # apagado), cai para a criação normal.
            graph_ids = {b["id"] for b in await self._business_ids_via_graph(page)}
            if not graph_ids or own_id in graph_ids:
                result.business_id = own_id
                result.portfolio_created = True
                result.portfolio_reused = True
                logger.success(
                    "Portfólio criado por run anterior (business_id=%s) não "
                    "apareceu no DOM, mas %s — reusando e pulando a criação.",
                    own_id,
                    "confirmado na Graph" if graph_ids
                    else "Graph indisponível; confiando no mapeamento salvo",
                )
                return

        # Regra do operador (2026-06): conta com ≥2 portfólios ALHEIOS é
        # inutilizável (no limite do Meta, não cria outro) → DESCARTAR e ir
        # p/ a próxima. O nosso (own_id) não entra na conta.
        #
        # Mudança pedida pelo operador (2026-07): NÃO descartar mais a conta só
        # porque já tem ≥2 portfólios alheios. Algumas contas PERMITEM criar
        # mais que 2 — então SEMPRE tentamos criar um novo portfólio. O único
        # motivo válido para PARAR é o Meta RECUSAR de fato a criação (erro de
        # limite), detectado por _maybe_handle_portfolio_limit (antes de
        # preencher E depois do submit) → PortfolioLimitError, que
        # _run_business_pipeline captura e encerra o pipeline limpo
        # (result.portfolio_limit_reached=True → pode fechar).
        foreign_ids = {i for i in portfolio_ids if i != own_id}
        # Perfil AGED que já vem com o portfólio do dono (≥1 alheio): apenas
        # registra o fato. A criação do NOSSO portfólio novo segue normal (via
        # página /select/ 'Criar empresa', caminho confiável nessas contas) e o
        # fluxo de 2 BMs continua valendo — NÃO reusamos o portfólio do dono. O
        # flag só habilita o reuse-fallback DORMENTE (PORTFOLIO_REUSE_FALLBACK,
        # default OFF) caso o operador precise um dia.
        if foreign_ids and not self._profile_had_foreign_portfolio:
            self._profile_had_foreign_portfolio = True
            logger.info(
                "Perfil já vem com %d portfólio(s) do dono — criando o NOSSO "
                "portfólio novo pela página /select/ (não reusa o do dono).",
                len(foreign_ids),
            )
        if len(foreign_ids) >= 2:
            logger.info(
                "Conta já possui %d portfólio(s) alheio(s). Antes isso "
                "DESCARTAVA a conta; agora seguimos e TENTAMOS criar um novo "
                "portfólio mesmo assim — só o erro de limite do Meta interrompe.",
                len(foreign_ids),
            )

        # NÃO reusar portfólio existente. A conta (aged/comprada) chega com o
        # portfólio do dono (ex.: "Lucas Gabriel ...") — o bot deve CRIAR UM
        # NOVO portfólio com o nome da empresa do CNPJ, não editar o do dono
        # (decisão do operador 2026-06; antes a idempotência pulava a criação
        # e a etapa 2 acabava editando o portfólio errado). Só loga o id
        # pré-existente para diagnóstico e segue para a criação. O business_id
        # do NOVO portfólio é capturado em _finalize_portfolio_success.
        existing_id = await self._detect_existing_business_id(page)
        if existing_id:
            # EXCEÇÃO segura ao 'nunca reusar': se o portfólio detectado é o
            # que NÓS criamos para ESTE CNPJ num run anterior (mapeamento
            # persistido com source='created' e id batendo), reusa e pula a
            # criação. O portfólio do DONO da conta nunca entra no
            # business_state.json deste CNPJ, então a regra 'criar novo, não
            # editar o do dono' continua valendo. Evita o ciclo recriar→
            # bounce→retry (~80s) em todo re-run de calibração (visto em
            # produção 2026-06, conta Fabiano).
            from utils.helpers import only_digits  # noqa: PLC0415
            from utils.business_state import find_business_by_cnpj  # noqa: PLC0415

            cnpj_digits = only_digits(
                (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
            )
            record = find_business_by_cnpj(cnpj_digits) or {}
            if (
                record.get("source") == "created"
                and record.get("business_id") == existing_id
            ):
                result.business_id = existing_id
                result.portfolio_reused = True
                logger.success(
                    "Portfólio criado por run anterior para este CNPJ "
                    "(business_id=%s) — reusando e pulando a criação.",
                    existing_id,
                )
                return
            logger.info(
                "Portfólio pré-existente detectado (business_id=%s) — NÃO será "
                "reusado; criando portfólio novo para a empresa do CNPJ.",
                existing_id,
            )

        # ANTI-DUPLICATA (2026-09): antes de criar, pergunta na Graph
        # (me/businesses) se JÁ existe um portfólio com o NOME desta empresa
        # (CNPJ). Cobre o caso da run que criou o portfólio mas NÃO capturou o
        # business_id (bug de captura em conta nova → nada persistido): sem isto,
        # uma retomada/'Continuar BM' criaria uma 2ª "Joyce". Casa SÓ pelo NOSSO
        # nome-alvo, então nunca reusa o portfólio do dono (nome diferente). Se
        # casar, reusa aquele id e pula a criação. Best-effort: se a Graph não
        # responder (sem token), segue para a criação normal.
        target_name = self._resolve_portfolio_name()
        if target_name:
            want = self._norm_portfolio_name(target_name)
            graph = await self._business_ids_via_graph(page)
            match = next(
                (
                    b for b in graph
                    if self._norm_portfolio_name(b["name"]) == want
                ),
                None,
            )
            if match:
                result.business_id = match["id"]
                result.portfolio_created = True
                result.portfolio_reused = True
                self._persist_business_mapping(match["id"], source="created")
                logger.success(
                    "Portfólio com o nome desta empresa JÁ existe (business_id="
                    "%s, nome=%r) — reusando e pulando a criação (anti-duplicata "
                    "via Graph).",
                    match["id"], match["name"],
                )
                return

        # O Meta frequentemente já abre o diálogo de criação direto na
        # loginpage (create_business_portfolio_for_bm=1) — nesse caso NÃO
        # clica o CTA (clicaria o 'Criar nova conta' do FUNDO). Só procura
        # o botão quando o diálogo ainda não está aberto.
        if await self._portfolio_dialog_still_open(page):
            logger.info("Diálogo de criação já aberto — indo direto ao preenchimento.")
        elif await self._business_logged_in(page) and await self._open_create_via_select_page(page):
            # Caminho PRIMÁRIO em conta que já tem portfólio (aged): a página
            # /select/ tem o botão 'Criar empresa'/'Create Business' que abre o
            # diálogo de forma confiável — o seletor do header vem ofuscado e o
            # deep-link bounceia p/ loginpage nessas contas (produção 2026-09,
            # contas Malti/kicker). Só devolve True se o diálogo realmente abriu.
            logger.info("Criação iniciada via página /select/ ('Criar empresa').")
        elif await self._business_logged_in(page) and await self._open_create_via_switcher(page):
            # Home LOGADA: cria pela UI (seletor → 'Criar um portfólio
            # empresarial'), como o humano — o deep-link bounceia p/ loginpage
            # numa conta já logada (run 66a82ca8). _open_create_via_switcher só
            # devolve True se o diálogo de criação realmente abriu.
            logger.info("Criação iniciada via seletor de portfólios (home logada).")
        else:
            # /overview cai em telas diferentes por tipo de conta: contas com
            # Página abrem a HOME do Business Suite (botões 'Criar post/
            # anúncio', SEM CTA de portfólio — visto 2026-06). Em vez de caçar
            # um CTA que varia, navega à URL DETERMINÍSTICA que auto-abre o
            # diálogo de criação. CTA por texto vira só fallback de layouts
            # antigos.
            logger.info(
                "Diálogo não aberto no overview — navegando à URL determinística "
                "de criação de portfólio."
            )
            try:
                # SSO sintético ANTES de abrir a URL de criação. O diálogo
                # renderiza mesmo SEM a sessão business estabelecida (login
                # fresco): preenche normal, mas o Enviar só RECARREGA a
                # loginpage sem criar nada (visto em produção 2026-06:
                # url_antes == url_depois, diálogo re-renderizado vazio). O
                # detector "diálogo aberto = sessão OK" não discrimina os dois
                # casos — o marketing de fundo aparece em ambos. Passar por
                # login.php?next= atravessa com a sessão facebook.com ativa e
                # garante o SSO sem chooser (mesmo mecanismo da "última
                # cartada" do _ensure_business_session).
                from urllib.parse import quote  # noqa: PLC0415

                warmup = (
                    "https://www.facebook.com/login.php?next="
                    + quote(BUSINESS_CREATE_PORTFOLIO_URL, safe="")
                )
                await page.goto(warmup, wait_until="domcontentloaded")
                await self._organic_wait()
                if "create_business_portfolio_for_bm" not in (page.url or ""):
                    await page.goto(
                        BUSINESS_CREATE_PORTFOLIO_URL,
                        wait_until="domcontentloaded",
                    )
                    await self._organic_wait()
                await self._ensure_business_session(
                    page, target_url=BUSINESS_CREATE_PORTFOLIO_URL
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Navegação à URL de criação falhou (%s).", exc)

            # O diálogo pode renderizar com ATRASO após a navegação determinística
            # (SPA do Business Suite ainda hidratando — run 8 jun/2026: a checagem
            # deu False cedo e o scan do CTA estourou em 5s, falhando a run). Dá
            # algumas rodadas extras p/ o diálogo aparecer ANTES de cair no CTA.
            # Só adiciona espera quando o diálogo não abriu de imediato — runs que
            # já abrem direto (ex.: run 1) não são afetadas.
            dialog_open = await self._portfolio_dialog_still_open(page)
            for _ in range(4):
                if dialog_open:
                    break
                await self._organic_wait()
                dialog_open = await self._portfolio_dialog_still_open(page)
            if not dialog_open:
                # Fallback: CTA por texto (layouts que não auto-abrem).
                _create_re = self._PORTFOLIO_CREATE_CTA_RE
                create_btn = self._first_visible(
                    page,
                    [
                        page.get_by_role("button", name=_create_re),
                        page.get_by_role("link", name=_create_re),
                        page.locator('[role="button"]').filter(has_text=_create_re),
                        page.get_by_text(_create_re),
                    ],
                )
                try:
                    await self._robust_click(await create_btn)
                except LoginError as exc:
                    # O CTA pode não existir porque a página é o GATE de selfie
                    # de vídeo (verificação de identidade), não o overview — foi
                    # o que travou a conta 64661428. Não dá p/ gravar selfie →
                    # DESCARTA a conta (vai pra próxima), em vez de falhar como
                    # 'botão não encontrado'.
                    if await self._is_video_selfie_gate(page):
                        raise AccountDiscardError(
                            "conta exige selfie de vídeo (verificação de "
                            "identidade) — não automatizável; descartada."
                        ) from exc
                    raise UIError(
                        f"Botão 'Criar conta/Create account' não encontrado "
                        f"(nem a URL determinística abriu o diálogo): {exc}"
                    ) from exc
                await self._organic_wait()

        await self._maybe_handle_portfolio_limit(page)

        portfolio_name = self._resolve_portfolio_name()
        # Nome da EMPRESA (campo 'Nome da sua conta e empresa'): recebe o
        # sufixo anti-duplicado (' 2', ' 3', …) quando uma tentativa anterior
        # bateu em 'Você já tem um portfólio com esse nome'. Os DEMAIS campos
        # ('Seu nome', e-mail) seguem SEM sufixo (pedido do operador).
        business_name = portfolio_name
        if _dup_suffix > 0:
            business_name = f"{portfolio_name} {_dup_suffix + 1}"
            logger.info(
                "Preenchendo portfólio comercial: %r → nome da empresa=%r "
                "(sufixo anti-duplicado; mesmo CNPJ).",
                portfolio_name, business_name,
            )
        else:
            logger.info("Preenchendo portfólio comercial: %r", portfolio_name)

        # TUDO escopado ao DIÁLOGO de criação (filtrado pelo título). Sem
        # isso, get_by_label/placeholder page-wide podiam casar inputs do
        # FUNDO (signup da loginpage) e o _fill_verified digitava num campo
        # invisível — submit ia vazio e o diálogo visível ficava intacto
        # (run 1781192632). O diálogo tem exatamente 3 inputs nesta ordem:
        # 0=empresa, 1=seu nome, 2=e-mail.
        dialog = page.locator('[role="dialog"]').filter(
            has=page.get_by_text(self._PORTFOLIO_DIALOG_TITLE_RE)
        ).first
        try:
            await dialog.wait_for(state="visible", timeout=8000)
        except Exception:  # noqa: BLE001
            logger.warning("Diálogo de criação não confirmado visível — seguindo.")
        dialog_inputs = dialog.locator("input")

        # 1) Nome da conta e empresa ← nome próprio limpo.
        name_field = await self._first_visible(
            page,
            [
                dialog.get_by_label(_PORTFOLIO_NAME_LABEL_RE),
                dialog.get_by_placeholder(re.compile(r"market|empresa|business", re.I)),
                dialog_inputs.nth(0),
            ],
        )
        ok_name = await self._fill_verified(
            name_field, business_name, field="nome_empresa", retries=3
        )

        # 2) Detecta a variante com "Sobrenome" separado. Quando existe, o
        #    rótulo "Seu nome" passa a significar PRIMEIRO nome → divide o
        #    nome próprio em (primeiro, resto). Sem ela, "Seu nome" recebe o
        #    nome completo (operador: mesmo valor do nome da empresa).
        from core.api_clients import split_admin_name  # noqa: PLC0415

        sobrenome_field = None
        try:
            cand = dialog.get_by_label(re.compile(r"sobrenome|last name", re.I)).first
            if await cand.is_visible(timeout=1500):
                sobrenome_field = cand
        except Exception:  # noqa: BLE001
            sobrenome_field = None

        admin_field = await self._first_visible(
            page,
            [
                dialog.get_by_label(_PORTFOLIO_ADMIN_LABEL_RE),
                dialog.get_by_placeholder(
                    re.compile(r"first and last|nome e sobrenome|primeiro nome", re.I)
                ),
                dialog_inputs.nth(1),
            ],
        )
        if sobrenome_field is not None:
            first, last = split_admin_name(portfolio_name)
            ok_admin = await self._fill_verified(admin_field, first, field="primeiro_nome", retries=3)
            await self._organic_wait()
            ok_admin = ok_admin and await self._fill_verified(sobrenome_field, last, field="sobrenome", retries=3)
            logger.info("Variante com 'Sobrenome' detectada — nome dividido.")
        else:
            ok_admin = await self._fill_verified(admin_field, portfolio_name, field="seu_nome", retries=3)
        await self._organic_wait()

        # 3) E-mail comercial.
        email_field = await self._first_visible(
            page,
            [
                dialog.get_by_label(_PORTFOLIO_EMAIL_LABEL_RE),
                dialog.locator('input[type="email"]').first,
                dialog_inputs.nth(2),
            ],
        )
        ok_email = await self._fill_verified(
            email_field, self.business_profile.business_email, field="email_comercial"
        )

        # GUARDA: NÃO submete com campos críticos vazios — era o que gerava
        # o falso "criado" (submit vazio fecha o diálogo sem criar BM, e a
        # etapa 2 quebrava sem business_id). Aborta alto com DOM salvo.
        if not ok_name or not ok_email or not ok_admin:
            await self._dump_debug_html(page, "portfolio_fill_failed")
            result.portfolio_created = False
            raise LoginError(
                "Campos do portfólio não confirmados após digitação "
                f"(nome={ok_name}, email={ok_email}, admin={ok_admin}) — submit abortado para "
                "não criar BM vazia/falsa. DOM salvo em .state/."
            )
        await self._organic_wait()

        # CRÍTICO: o submit DEVE ser escopado ao diálogo de criação. A página
        # do Business Suite atrás do modal tem botões "Create/Criar" próprios —
        # um seletor global (e com "criar|create" primeiro) clicava no botão de
        # FUNDO em vez do "Submit" do modal, deixando o diálogo aberto sem
        # submeter (visto em produção 2026-06: campos preenchidos, modal nunca
        # fechava). Escopa ao [role=dialog] da criação e prioriza "Submit".
        # Mimetiza o humano (operador confirmou: manualmente o Enviar CRIA):
        # blur do último campo para o React COMMITAR os valores antes do
        # submit. Sem isso o bot enviava dados parciais (e-mail não commitado)
        # → o Meta recusava e bounceava p/ loginpage.
        try:
            await email_field.press("Tab")
            await self._organic_wait()
        except Exception:  # noqa: BLE001
            pass

        # O diálogo pode DESAPARECER entre o preenchimento e o submit (Meta
        # em estado 'Ocorreu um problema técnico' derruba o modal — visto em
        # produção 2026-06, conta Izabel). Sem esta guarda, o lookup do
        # submit estourava com um timeout críptico de locator.
        if not await self._portfolio_dialog_still_open(page):
            raise LoginError(
                "Diálogo de criação DESAPARECEU antes do submit — provável "
                "erro transitório do Meta ('problema técnico' na tentativa "
                "anterior). Re-rode a conta; persistindo, aguarde alguns "
                "minutos antes de tentar de novo."
            )
        create_dialog = page.locator('[role="dialog"]').filter(
            has=page.get_by_text(self._PORTFOLIO_DIALOG_TITLE_RE)
        )
        submit_loc = await self._first_visible(
            page,
            [
                create_dialog.get_by_role(
                    "button",
                    name=re.compile(r"submit|enviar|concluir|done|criar|create", re.I),
                ),
                create_dialog.locator('div[role="button"][tabindex="0"]').last,
                create_dialog.locator('button[type="submit"]'),
            ],
        )
        # Espera o 'Enviar' HABILITAR (fica aria-disabled até os campos
        # validarem — clicar desabilitado submetia vazio). Até ~10s.
        for _ in range(20):
            try:
                aria = await submit_loc.get_attribute("aria-disabled")
                enabled = await submit_loc.is_enabled()
                if enabled and aria != "true":
                    break
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning(
                "Botão 'Enviar' do portfólio não habilitou em ~10s — "
                "clicando mesmo assim (diagnóstico capturará o resultado)."
            )
        url_before = page.url
        await self._robust_click(submit_loc)

        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass

        # O 'Enviar' entra em estado de SPINNER (criação em curso). Dá tempo
        # do diálogo FECHAR (sinal de sucesso) antes de seguir — navegar
        # embora cedo demais perdia a conclusão (visto em produção 2026-06:
        # campos OK + spinner, mas o código já re-navegava). Até ~15s.
        for _ in range(15):
            try:
                if not await self._portfolio_dialog_still_open(page):
                    break
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(1.0)

        # CAMINHO FELIZ: o Meta mostra o modal "<Nome> foi criado! → Concluir".
        # Detecta, clica Concluir e captura o business_id ANTES da lógica de
        # bounce/SSO — senão o _ensure_business_session via o marketing de
        # FUNDO e disparava SSO, travando no modal de sucesso (visto em
        # produção 2026-06: portfólio criado, mas bot não clicava 'Concluir').
        if await self._finalize_portfolio_success(page, result):
            logger.success("Portfólio comercial %r criado.", portfolio_name)
            return

        # DIAGNÓSTICO: estado IMEDIATO pós-submit (URL de bounce + erro inline
        # + diálogo) ANTES de qualquer re-navegação — é o que revela por que a
        # criação não conclui na loginpage. Dump distinto do postcreate_noid
        # (que é capturado já depois do bounce).
        await self._diag_after_submit(page, url_before)

        # Pós-submit o Meta despeja em business/loginpage/?...&create_
        # business_portfolio_for_bm=1 (DESLOGADO) — reentra via SSO e volta
        # ao OVERVIEW (não à loginpage!) antes de checar limite e capturar
        # o business_id (senão tudo abaixo enxerga a página errada).
        # Se a URL atual JÁ é a loginpage, sabemos que está deslogado: força o
        # SSO (o diálogo de criação que fica aberto sobre a loginpage faria
        # _business_logged_out devolver False e o SSO seria pulado — loop de
        # bounce, produção 2026-06, conta Fabio).
        bounced_to_loginpage = "/loginpage" in (page.url or "").lower()
        if bounced_to_loginpage:
            # Limpa o diálogo de criação que ficou ABERTO sobre a loginpage —
            # senão ele cobre o botão 'Continuar com o Facebook' e o SSO não
            # consegue clicar. Volta ao chooser limpo antes de forçar o SSO.
            try:
                await page.goto(
                    BUSINESS_OVERVIEW_URL, wait_until="domcontentloaded"
                )
                await self._organic_wait()
            except Exception as exc:  # noqa: BLE001
                logger.debug("bounce: goto overview p/ limpar diálogo falhou (%s).", exc)
        if not await self._ensure_business_session(
            page, target_url=BUSINESS_OVERVIEW_URL, force=bounced_to_loginpage
        ):
            # Mesma escada de recuperação do "diálogo ainda aberto" abaixo:
            # pro operador, bounce-pra-loginpage e nome rejeitado parecem
            # IGUAIS na tela (volta pro mesmo formulário) — então tratamos
            # igual. 1ª vez: espera e refaz com o MESMO CNPJ (pode ser
            # glitch transitório de sessão). Repetindo, troca de CNPJ (mesmo
            # perfil) — se a sessão não recupera 2x seguidas com o mesmo
            # CNPJ, mais retry não muda nada; só desistir dessa run ajudaria
            # menos que tentar outro CNPJ.
            result.portfolio_created = False
            if not _sso_retry:
                logger.warning(
                    "SSO não recuperou a sessão (1ª tentativa, bounce p/ "
                    "loginpage) — aguardando %.0fs e refazendo o fluxo de "
                    "criação (tentativa 2/2).",
                    self._PORTFOLIO_RETRY_BACKOFF_S,
                )
                await asyncio.sleep(self._PORTFOLIO_RETRY_BACKOFF_S)
                return await self._create_business_portfolio(
                    page, result, _sso_retry=True, _cnpj_swapped=_cnpj_swapped, _dup_suffix=_dup_suffix
                )
            if not _cnpj_swapped and await self._swap_cnpj_for_rejected_name():
                return await self._create_business_portfolio(
                    page, result, _sso_retry=False, _cnpj_swapped=True, _dup_suffix=0
                )
            raise LoginError(
                "Submit do portfólio despejou na loginpage e o SSO não "
                "recuperou a sessão (2x) — criação não confirmada. DOM em "
                ".state/debug_business_sso_failed_*."
                + (" CNPJ já trocado 1x sem sucesso." if _cnpj_swapped else "")
            )

        # CRÍTICO: Após submit, verificar se REALMENTE criou ou se houve bounce.
        # Se estiver na loginpage AGORA, significa que o submit falhou.
        bounced_after_submit = "/loginpage" in (page.url or "").lower()
        if bounced_after_submit:
            logger.error(
                "BOUNCE após submit do portfólio: submit não foi aceito "
                "(sessão SSO expirou ou formulário inválido). Marcando como falha."
            )
            result.portfolio_created = False
            raise LoginError(
                "Portfólio não foi criado — bounce para loginpage após submit "
                "(sessão SSO expirou ou formulário com erro)."
            )

        await self._maybe_handle_portfolio_limit(page)
        # "<Portfólio> foi criado! Confirme seu endereço de email" → Concluir.
        await self._dismiss_business_interstitials(page)

        # IMPORTANTE: NÃO marcar como criado ANTES de capturar o business_id.
        # Só marca como criado se realmente conseguir capturar o ID.
        # Antes marcava como True aqui, mas se o submit falhava (bounce para
        # loginpage), ficava marcado como criado sem ID (2026-06-22).

        # Best-effort: pós-criação o Meta costuma redirecionar para a Suite
        # com business_id= na URL — captura e persiste para runs futuros.
        created_id = await self._capture_new_business_id(
            page, portfolio_name=self._resolve_portfolio_name()
        )
        if created_id:
            result.business_id = created_id
            result.portfolio_created = True  # SÓ marca como criado se tiver ID!
            self._persist_business_mapping(created_id, source="created")
        else:
            # business_id não capturado pós-criação. Captura o DOM e checa se
            # o diálogo de criação CONTINUA aberto — se sim, o Submit não foi
            # aceito (nome inválido/campo obrigatório) e a "criação" é falsa:
            # falha alto em vez de marchar pra etapa 2 sem BM real.
            await self._dump_debug_html(page, "portfolio_postcreate_noid")
            result.portfolio_created = False  # Explícito: não foi criado
            if await self._portfolio_dialog_still_open(page):
                # NOME DUPLICADO ('Você já tem um portfólio com esse nome'):
                # o nome do CNPJ já foi usado numa BM anterior. Pedido do
                # operador (2026-09): NÃO descartar a conta/CNPJ — re-tentar
                # com sufixo (' 2', ' 3', …) SÓ no nome da empresa, MESMO
                # CNPJ. Checado ANTES da rejeição/troca de CNPJ abaixo.
                name_duplicate = False
                try:
                    name_duplicate = await page.get_by_text(
                        self._PORTFOLIO_NAME_DUPLICATE_RE
                    ).first.is_visible(timeout=600)
                except Exception:  # noqa: BLE001
                    pass
                if name_duplicate and _dup_suffix < self._PORTFOLIO_DUP_MAX_RETRIES:
                    logger.warning(
                        "Nome do portfólio JÁ EXISTE nesta conta — re-tentando "
                        "com sufixo ' %d' no nome da empresa (MESMO CNPJ, conta "
                        "NÃO descartada; tentativa %d/%d).",
                        _dup_suffix + 2, _dup_suffix + 1,
                        self._PORTFOLIO_DUP_MAX_RETRIES,
                    )
                    return await self._create_business_portfolio(
                        page, result, _sso_retry=False,
                        _cnpj_swapped=_cnpj_swapped, _dup_suffix=_dup_suffix + 1,
                    )

                # 'Unable to Create Account' / 'nome não adequado' — o Meta
                # recusou o nome DESTA CONTA definitivamente. Troca o CNPJ
                # IMEDIATAMENTE, sem o retry de 30s com o mesmo nome (o erro
                # é definitivo, nenhum delay resolve). Pedido do operador
                # 2026-06-21.
                name_rejected = False
                try:
                    # Timeout: 600ms para detectar mensagem de erro visual
                    # (300ms era muito curto e causava falsos negativos).
                    name_rejected = await page.get_by_text(
                        self._PORTFOLIO_NAME_REJECT_RE
                    ).first.is_visible(timeout=600)
                except Exception:  # noqa: BLE001
                    pass
                if name_rejected and not _cnpj_swapped:
                    logger.warning(
                        "Nome do portfólio explicitamente rejeitado pelo Meta "
                        "('Unable to Create Account') — trocando CNPJ "
                        "imediatamente (sem aguardar 30s)."
                    )
                    if await self._swap_cnpj_for_rejected_name():
                        return await self._create_business_portfolio(
                            page, result, _sso_retry=False, _cnpj_swapped=True, _dup_suffix=0
                        )
                if not _sso_retry:
                    logger.warning(
                        "Submit não aceito (diálogo ainda aberto) — aguardando "
                        "%.0fs (erro transitório do Meta não some em segundos) "
                        "e refazendo o fluxo de criação (tentativa 2/2).",
                        self._PORTFOLIO_RETRY_BACKOFF_S,
                    )
                    await asyncio.sleep(self._PORTFOLIO_RETRY_BACKOFF_S)
                    return await self._create_business_portfolio(
                        page, result, _sso_retry=True, _cnpj_swapped=_cnpj_swapped, _dup_suffix=_dup_suffix
                    )
                # 2ª tentativa com o MESMO nome também falhou (diálogo ainda
                # aberto) — não é mais glitch transitório do Meta, é o nome
                # deste CNPJ sendo rejeitado. Descarta o CNPJ e tenta 1x com
                # outro do pool antes de desistir de vez.
                if not _cnpj_swapped and await self._swap_cnpj_for_rejected_name():
                    return await self._create_business_portfolio(
                        page, result, _sso_retry=False, _cnpj_swapped=True, _dup_suffix=0
                    )
                # Troca de CNPJ também falhou — ambos (conta + CNPJ) são
                # definitivamente inviáveis. Marca para descarte.
                result.account_discarded = True
                result.cnpj_discarded = True
                raise AccountDiscardError(
                    "Criação do portfólio REJEITADA (nome inválido). "
                    "Conta + CNPJ descartados — não reutilizáveis. "
                    "DOM salvo em .state/."
                    + (" CNPJ já trocado 1x sem sucesso." if _cnpj_swapped else "")
                )
            # De volta ao overview pós-SSO: se o CTA de criação AINDA está
            # lá, a conta segue sem BM — o submit foi engolido pelo bounce
            # para a loginpage (visto em produção 2026-06). Falha alto em
            # vez de marcar criado e quebrar nas etapas 2-4.
            try:
                cta_visible = await (
                    page.get_by_role("button", name=self._PORTFOLIO_CREATE_CTA_RE)
                    .first.is_visible(timeout=5000)
                )
            except Exception:  # noqa: BLE001
                cta_visible = False
            if cta_visible:
                result.portfolio_created = False
                if not _sso_retry:
                    logger.warning(
                        "Submit engolido pelo bounce (CTA ainda no overview) — "
                        "aguardando %.0fs (erro transitório do Meta não some "
                        "em segundos) e refazendo o fluxo de criação "
                        "(tentativa 2/2).",
                        self._PORTFOLIO_RETRY_BACKOFF_S,
                    )
                    await asyncio.sleep(self._PORTFOLIO_RETRY_BACKOFF_S)
                    return await self._create_business_portfolio(
                        page, result, _sso_retry=True, _cnpj_swapped=_cnpj_swapped, _dup_suffix=_dup_suffix
                    )
                if not _cnpj_swapped and await self._swap_cnpj_for_rejected_name():
                    return await self._create_business_portfolio(
                        page, result, _sso_retry=False, _cnpj_swapped=True, _dup_suffix=0
                    )
                raise LoginError(
                    "Criação do portfólio NÃO concluída — o overview ainda "
                    "oferece o CTA de criação (submit engolido pelo bounce "
                    "para business/loginpage). DOM salvo em .state/."
                    + (" CNPJ já trocado 1x sem sucesso." if _cnpj_swapped else "")
                )
            logger.warning(
                "Portfólio criado mas business_id NÃO capturado da URL/DOM — "
                "idempotência cega num re-run (DOM salvo p/ diagnóstico)."
            )
        logger.success("Portfólio comercial %r criado.", portfolio_name)

    # Heading do modal de sucesso pós-criação ("<Nome> foi criado!").
    _PORTFOLIO_CREATED_RE = re.compile(
        r"foi criad[oa]|was created|criad[oa]\s*!|created\s*!", re.I
    )

    async def _finalize_portfolio_success(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Detecta o modal de sucesso e clica 'Concluir' p/ finalizar.

        O Meta mostra "<Nome> foi criado! · Confirme seu endereço de email"
        com um botão 'Concluir' (``<div role="button">``). Devolve True se a
        criação foi confirmada (clicou Concluir + marcou portfolio_created e
        tentou capturar o business_id). False se não há modal de sucesso.
        """
        created = False
        for _ in range(6):
            try:
                if await (
                    page.get_by_text(self._PORTFOLIO_CREATED_RE)
                    .first.is_visible(timeout=1000)
                ):
                    created = True
                    break
            except Exception:  # noqa: BLE001
                pass
            await self._organic_wait()
        if not created:
            return False

        logger.success("Portfólio criado — modal de sucesso detectado.")
        result.portfolio_created = True
        # Clica 'Concluir'/'Done' (div[role=button]) para fechar o modal.
        try:
            done = await self._first_visible(
                page,
                [
                    page.get_by_role(
                        "button",
                        name=re.compile(r"concluir|done|entendi|got it|ok", re.I),
                    ),
                    page.locator('[role="dialog"] div[role="button"]').last,
                ],
            )
            await self._robust_click(done)
            await self._organic_wait()
            logger.info("Modal de sucesso finalizado ('Concluir' clicado).")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Modal de sucesso: 'Concluir' não clicado (%s) — seguindo.", exc
            )
        # Captura o business_id pós-criação (só aparece após o Concluir +
        # redirect para a Suite autenticada).
        try:
            await page.goto(BUSINESS_OVERVIEW_URL, wait_until="domcontentloaded")
            await self._organic_wait()
            await self._ensure_business_session(
                page, target_url=BUSINESS_OVERVIEW_URL
            )
            bid = await self._capture_new_business_id(
                page, portfolio_name=self._resolve_portfolio_name()
            )
            if bid:
                result.business_id = bid
                self._persist_business_mapping(bid, source="created")
                logger.success("business_id (NOVO portfólio) capturado: %s", bid)
            else:
                logger.warning(
                    "Portfólio criado, mas business_id do NOVO portfólio não "
                    "identificado (diff/nome) — idempotência cega num re-run."
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Captura de business_id pós-criação falhou (%s).", exc)
        return True

    # Tela "Não é possível acessar o Meta Business Suite com esta conta" / "não
    # tem acesso a Páginas do Facebook" — conta sem páginas, inútil p/ o fluxo.
    # SÓ frases específicas dessa tela (não casa o botão genérico "Criar Página"
    # que aparece em outros lugares).
    _NO_BUSINESS_ACCESS_RE = re.compile(
        r"n[aã]o (?:e|é) poss[ií]vel acessar o meta business suite"
        r"|n[aã]o tem acesso a p[aá]ginas do facebook"
        r"|can'?t access (?:the )?meta business suite"
        r"|doesn'?t have access to (?:any )?facebook pages",
        re.I,
    )

    async def _account_has_portfolio(self, page: Page) -> bool:
        """True se esta conta JÁ tem algum portfólio empresarial (BM).

        Usado para NÃO descartar conta que cai na tela 'Não é possível acessar o
        Meta Business Suite' (sem Página) mas que TEM um portfólio — a BM existe
        e é usável via business_id, mesmo sem Página (conta Joyce, 2026-09).
        Fontes, em ordem de custo: mapeamento que NÓS salvamos ao criar
        (source='created') → Graph me/businesses (autoritativo) → ids no DOM."""
        try:
            from utils.helpers import only_digits  # noqa: PLC0415
            from utils.business_state import find_business_by_cnpj  # noqa: PLC0415

            digits = only_digits(
                (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
            )
            rec = find_business_by_cnpj(digits) or {}
            if rec.get("source") == "created" and rec.get("business_id"):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            if await self._business_ids_via_graph(page):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            if await self._collect_portfolio_ids(page):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _guard_no_business_access(self, page: Page, result: AutomationResult) -> None:
        """Detecta a tela 'sem acesso ao Business Suite / sem páginas'. Se achar:
        descarta a conta, marca o perfil com a tag PAGINA, força fechar o perfil
        e aborta (AccountDiscardError) — o finalizador fecha o perfil e libera o
        CNPJ. Best-effort na leitura do texto; nunca trava o fluxo à toa."""
        try:
            txt = await asyncio.wait_for(
                page.evaluate("() => document.body ? document.body.innerText : ''"),
                timeout=4.0,
            )
        except Exception:  # noqa: BLE001
            return
        if not self._NO_BUSINESS_ACCESS_RE.search(txt or ""):
            return
        # A tela 'Não é possível acessar o Meta Business Suite' aparece quando a
        # conta NÃO tem PÁGINA — mas se ela JÁ tem um portfólio (BM), é
        # perfeitamente utilizável: a BM é acessada via business_id (settings/
        # ads manager), não pela home. NÃO descartar nesse caso (conta Joyce
        # 2026-09: portfólio criado, sem Página → home diz 'sem acesso', mas a
        # BM existe e funciona). Só descarta se realmente NÃO houver portfólio.
        if await self._account_has_portfolio(page):
            logger.info(
                "business: tela 'sem acesso ao Business Suite' (conta sem "
                "Página), MAS a conta JÁ tem um portfólio — NÃO descartando; "
                "seguindo para reusar a BM existente pelo business_id."
            )
            return
        email = (self.credentials.email_or_phone or "").strip()
        try:
            from core import store  # noqa: PLC0415

            discarded = store.discard_fb_account(email)
        except Exception:  # noqa: BLE001
            discarded = None
        result.account_discarded = True
        result.force_close_profile = True
        result.profile_error_tag = "PAGINA"
        result.current_step = "no_business_access"
        logger.error(
            "business: conta %s SEM acesso ao Business Suite (sem páginas) — "
            "DESCARTADA (%s); perfil marcado [PAGINA] e será FECHADO.",
            email or "?", discarded or "não estava no estoque",
        )
        raise AccountDiscardError(
            "conta sem acesso ao Business Suite (sem páginas) — descartada (PAGINA)."
        )

    async def _guard_portfolio_id(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Aborta (claro) se 1/5 marcou criado mas NÃO capturou o business_id.

        Sem isso, o passo 2 navega com business_id=None → /latest/home → falso
        "editor não abriu" (cascata da run 66533593000160). No-op quando há
        business_id (inclusive idempotência: BM já existia e foi detectada).
        Captura o DOM antes de levantar p/ diagnóstico, inclusive em
        calibração — a cascata produz lixo nos passos 2-5 nos dois modos."""
        if not (result.portfolio_created and not result.business_id):
            return
        # FALLBACK REUSO (2026-09, operador): conta AGED que já vem com o
        # portfólio do dono frequentemente BLOQUEIA/esconde o 2º portfólio — a
        # criação "conclui" (modal de sucesso) mas o id novo nunca surge no diff
        # (PortfolioIdCaptureError). Em vez de abortar e descartar a run, REUSA o
        # portfólio pré-existente para ESTA (única) BM e segue a verificação. Só
        # vale quando o perfil realmente tinha um portfólio alheio ANTES da
        # criação — senão não há o que reusar e mantemos o abort claro.
        reuse_id = await self._reuse_preexisting_portfolio_id(page)
        if reuse_id:
            result.business_id = reuse_id
            result.portfolio_reused = True
            logger.warning(
                "1/5: business_id do portfólio NOVO não capturado (Meta "
                "provavelmente bloqueou/escondeu o 2º portfólio da conta aged). "
                "FALLBACK: reusando o portfólio pré-existente (business_id=%s) "
                "para esta BM e seguindo a verificação.",
                reuse_id,
            )
            return
        result.current_step = "portfolio_no_business_id"
        try:
            await self._capture_step_failure(page, "1/5 portfolio_create")
        except Exception:  # noqa: BLE001
            pass
        raise PortfolioIdCaptureError(
            "Portfólio criado mas business_id NÃO capturado (diff vazio / nome "
            "não casou). Sem BM real a esteira cairia em /latest/home e o passo "
            "2 daria falso 'editor não abriu' — abortando em 1/5. DOM em .state/."
        )

    async def _reuse_preexisting_portfolio_id(
        self, page: Page
    ) -> Optional[str]:
        """business_id de um portfólio ALHEIO pré-existente, para reuso quando o
        portfólio novo não pôde ser capturado (conta aged com portfólio do dono).

        Só considera ids que JÁ EXISTIAM antes da criação
        (``self._pre_creation_portfolio_ids``) — nunca um id novo (se existisse,
        teria sido capturado no diff). Preferência: o portfólio 'default' que a
        Suite aponta agora (URL/menu), desde que seja um dos pré-existentes;
        senão o menor id pré-existente (determinístico). Retorna None (mantendo o
        abort) se o flag estiver desligado ou não houver portfólio pré-existente.
        """
        from config import PORTFOLIO_REUSE_FALLBACK  # noqa: PLC0415

        if not PORTFOLIO_REUSE_FALLBACK:
            return None
        pre = getattr(self, "_pre_creation_portfolio_ids", set()) or set()
        if not pre:
            return None
        try:
            current = await self._detect_existing_business_id(page)
        except Exception:  # noqa: BLE001 — best-effort
            current = None
        if current and current in pre:
            return current
        return sorted(pre)[0]

    async def _diag_after_submit(self, page: Page, url_before: str) -> None:
        """Captura a mecânica do submit do portfólio (diagnóstico).

        Loga URL antes→depois, procura erro inline e estado do diálogo, e
        salva o DOM IMEDIATO (tag portfolio_after_submit). É o que faltava
        para entender o bounce para a loginpage (criação não concluída).
        """
        url_after = page.url or ""
        logger.info(
            "DIAG submit: url_antes=%s → url_depois=%s", url_before, url_after
        )
        if "/loginpage" in url_after:
            logger.warning(
                "DIAG submit: BOUNCE para loginpage — criação NÃO autenticada "
                "(o diálogo da loginpage não cria BM sem o SSO completo)."
            )
        # Erro inline (Meta às vezes recusa nome/campo sem fechar o diálogo).
        # NÃO inclui "caracteres especiais" — é o TEXTO DE AJUDA do campo
        # (sempre presente), dava falso positivo de erro (run 1781207645).
        for pat in (
            r"n[ãa]o foi poss[íi]vel", r"tente novamente",
            r"obrigat[óo]ri", r"inv[áa]lid",
            r"something went wrong", r"couldn'?t",
        ):
            try:
                el = page.get_by_text(re.compile(pat, re.I)).first
                if await el.is_visible(timeout=600):
                    txt = (await el.inner_text())[:180]
                    logger.warning("DIAG submit: erro inline detectado: %r", txt)
                    break
            except Exception:  # noqa: BLE001
                continue
        try:
            still = await (
                page.get_by_text(self._PORTFOLIO_DIALOG_TITLE_RE)
                .first.is_visible(timeout=800)
            )
            logger.info("DIAG submit: diálogo de criação ainda aberto=%s", still)
        except Exception:  # noqa: BLE001
            pass
        await self._dump_debug_html(page, "portfolio_after_submit")

    async def _portfolio_dialog_still_open(self, page: Page) -> bool:
        """True se o DIÁLOGO de criação de portfólio está REALMENTE aberto.

        Exige um [role="dialog"] VISÍVEL com o título de criação E um input —
        não só o TEXTO do título. O detector antigo (só texto) casava 'Criar um
        portfólio empresarial' do SELETOR de portfólios na home LOGADA e dava
        falso positivo de 'diálogo já aberto' → o bot tentava preencher um
        diálogo inexistente e estourava timeout (run 66a82ca8 2026-06-17,
        conta Miguel Cole do screenshot do operador)."""
        try:
            dlg = (
                page.locator('[role="dialog"]')
                .filter(has_text=self._PORTFOLIO_DIALOG_TITLE_RE)
                .filter(has=page.locator("input"))
            )
            return await dlg.first.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

    async def _switcher_panel_open(self, page: Page) -> bool:
        """O painel do seletor de portfólios está aberto? (heading 'Portfólios
        empresariais' OU o botão 'Criar um portfólio empresarial' visível)."""
        for rx in (self._PORTFOLIO_LIST_HEADING_RE, self._PORTFOLIO_SWITCHER_CREATE_RE):
            try:
                if await page.get_by_text(rx).first.is_visible(timeout=800):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _open_create_via_select_page(self, page: Page) -> bool:
        """Cria um portfólio NOVO pela página /select/ ('Selecionar empresa'),
        clicando 'Criar empresa'/'Create Business'.

        É o caminho MAIS confiável em conta AGED que já tem portfólios: o seletor
        do header vem ofuscado (icons sem rótulo) e o deep-link
        create_business_portfolio_for_bm=1 BOUNCEIA p/ loginpage — mas o botão
        desta página abre o diálogo de criação normalmente (screenshot do
        operador 2026-09: conta com 2 portfólios + 'Create Business' habilitado).
        Devolve True se o diálogo de criação realmente abriu."""
        try:
            await page.goto(BUSINESS_SELECT_URL, wait_until="domcontentloaded")
            await self._organic_wait()
        except Exception as exc:  # noqa: BLE001
            logger.warning("/select/: navegação falhou (%s) — pulando.", exc)
            return False
        # Se a navegação bounceou p/ loginpage, a sessão business não está de pé:
        # este caminho não vai funcionar, deixa o próximo tentar o SSO.
        if "/loginpage" in (page.url or "") or "/login" in (page.url or ""):
            logger.info(
                "/select/: bounce p/ loginpage — sessão business não pronta, "
                "deixando o próximo caminho tratar o SSO."
            )
            return False
        try:
            create = await self._first_visible(
                page,
                [
                    page.get_by_role(
                        "button", name=self._PORTFOLIO_SELECT_CREATE_RE
                    ),
                    page.get_by_role(
                        "link", name=self._PORTFOLIO_SELECT_CREATE_RE
                    ),
                    page.get_by_text(self._PORTFOLIO_SELECT_CREATE_RE),
                ],
                timeout_ms=6000,
            )
        except LoginError:
            await self._dump_debug_html(page, "portfolio_select_no_button")
            logger.warning(
                "/select/: botão 'Criar empresa' não localizado — DOM salvo, "
                "caindo no próximo caminho."
            )
            return False
        await self._robust_click(create)
        await self._organic_wait()
        # O clique pode abrir o diálogo IN-APP direto OU navegar p/ o fluxo de
        # criação — confirma pelo diálogo aberto (com folga p/ a SPA hidratar).
        for _ in range(6):
            if await self._portfolio_dialog_still_open(page):
                logger.info("Diálogo de criação aberto via página /select/.")
                return True
            await self._organic_wait()
        await self._dump_debug_html(page, "portfolio_select_no_dialog")
        logger.warning(
            "/select/: 'Criar empresa' clicado mas o diálogo não abriu — DOM "
            "salvo, caindo no próximo caminho."
        )
        return False

    async def _open_create_via_switcher(self, page: Page) -> bool:
        """Na home LOGADA, abre o SELETOR de portfólios (canto sup. esq.) e clica
        'Criar um portfólio empresarial' — o fluxo IN-APP que o humano usa
        (screenshot do operador), em vez do deep-link que bounceia p/ loginpage.

        Os botões do header do Business Suite são ÍCONES sem rótulo (DOM ofuscado
        do Meta), então tenta os candidatos com aria-haspopup e confirma pela
        aparição de 'Portfólios empresariais'/'Criar um portfólio empresarial'.
        Dumpa o DOM do seletor ABERTO p/ diagnóstico + contagem futura de TODOS
        os portfólios (os ocultos atrás do seletor fechado é o que faz a conta
        ser reusada — 'MESMA conta', run 66a82ca8). Devolve True se o diálogo de
        criação abriu."""
        opened = await self._switcher_panel_open(page)
        if not opened:
            cands = page.locator('[aria-haspopup="menu"], [role="button"][aria-haspopup]')
            try:
                n = await cands.count()
            except Exception:  # noqa: BLE001
                n = 0
            for i in range(min(n, 8)):
                try:
                    await self._robust_click(cands.nth(i))
                except Exception:  # noqa: BLE001
                    continue
                # O painel do seletor é LAZY (montado só ao abrir) — POLLA por
                # alguns segundos antes de desistir. Antes checava 1× em ~800ms e
                # já dava Escape, matando o painel CERTO antes de ele renderizar
                # (run db2211ad 2026-06-17: 'seletor não abriu' com o candidato
                # certo provavelmente clicado).
                got = False
                for _ in range(4):
                    await self._organic_wait()
                    if await self._switcher_panel_open(page):
                        got = True
                        break
                if got:
                    opened = True
                    logger.info("seletor de portfólios aberto (candidato #%d).", i)
                    break
                try:
                    await page.keyboard.press("Escape")
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
        if not opened:
            await self._dump_debug_html(page, "portfolio_switcher_failed")
            logger.warning(
                "seletor de portfólios não abriu (header ofuscado) — caindo no "
                "deep-link. DOM salvo p/ diagnóstico."
            )
            return False
        await self._dump_debug_html(page, "portfolio_switcher_open")
        try:
            create = await self._first_visible(
                page,
                [
                    page.get_by_role("button", name=self._PORTFOLIO_SWITCHER_CREATE_RE),
                    page.get_by_role("link", name=self._PORTFOLIO_SWITCHER_CREATE_RE),
                    page.get_by_text(self._PORTFOLIO_SWITCHER_CREATE_RE),
                ],
                timeout_ms=3000,
            )
        except LoginError:
            logger.warning(
                "'Criar um portfólio empresarial' não localizado no seletor "
                "aberto — DOM salvo, caindo no deep-link."
            )
            return False
        await self._robust_click(create)
        await self._organic_wait()
        for _ in range(5):
            if await self._portfolio_dialog_still_open(page):
                logger.info("Diálogo de criação aberto via seletor de portfólios.")
                return True
            await self._organic_wait()
        return await self._portfolio_dialog_still_open(page)

    # Interstício de consentimento LGPD do Facebook (/privacy/consent/
    # lgpd_migrated/): aparece INTERMITENTE logo após o login em algumas contas
    # e TRAVA a navegação pro Business Suite. Wizard de 3 telas:
    #   1. "Required: Review your data settings"      → botão "Get Started"
    #   2. "Activity information from ad partners"     → "Accept and continue"
    #   3. "You're all set!"                           → "Close"
    # (relato do operador 2026-09; a Meta serve em EN mesmo com locale pt_BR.)
    _CONSENT_SIGNAL_RE = re.compile(
        r"review your data settings|revise as suas configura[çc][õo]es de dados"
        r"|activity information from ad partners"
        r"|informa[çc][õo]es de atividade (?:de|dos) parceiros"
        r"|you'?re all set|tudo (?:pronto|certo)"
        # Consentimento de COOKIES (pós-2FA, flow=user_cookie_choice_v2) —
        # "Allow the use of cookies from/by Facebook?" (produção 2026-09).
        r"|allow the use of cookies|permitir o uso de cookies"
        r"|use of cookies (?:from|by) facebook|uso de cookies (?:do|pelo) facebook",
        re.I,
    )
    # Botão de RECUSAR cookies opcionais — preferido (preserva privacidade).
    _COOKIE_DECLINE_RE = re.compile(
        r"^\s*(decline optional cookies|recusar cookies opcionais"
        r"|only allow essential cookies|permitir apenas.*essenciais"
        r"|recusar cookies n[ãa]o essenciais)\s*$",
        re.I,
    )
    # Botões que AVANÇAM/ACEITAM/FECHAM o wizard. NÃO inclui "Manage settings"
    # / "Gerenciar configurações" (abre submenu) nem "Learn more" (link).
    # 'allow all cookies'/'permitir todos os cookies' entram como ÚLTIMO recurso
    # (só se o 'recusar' não for encontrado) — evita travar na tela de cookies.
    _CONSENT_ADVANCE_RE = re.compile(
        r"^\s*(get started|come[çc]ar|comece agora"
        r"|accept and continue|aceitar e continuar|aceitar"
        r"|continue|continuar|avan[çc]ar|next|pr[óo]ximo"
        r"|close|fechar|done|conclu[íi]r|ok|got it|entendi"
        r"|allow all cookies|permitir todos os cookies)\s*$",
        re.I,
    )

    async def _dismiss_consent_flow(self, page: Page) -> None:
        """Passa pelo interstício de consentimento LGPD, se estiver na tela.

        Best-effort e não-fatal: detecta pelo URL (/privacy/consent/) ou pelo
        texto das telas e clica o botão de avançar/aceitar/fechar até o fluxo
        sumir. Quando NÃO há consentimento, sai rápido (~1s de probe)."""
        for i in range(8):
            url = (page.url or "").lower()
            on_consent_url = "/privacy/consent" in url
            has_signal = False
            try:
                has_signal = await page.get_by_text(
                    self._CONSENT_SIGNAL_RE
                ).first.is_visible(timeout=1000)
            except Exception:  # noqa: BLE001
                has_signal = False
            if not on_consent_url and not has_signal:
                if i > 0:
                    logger.info("Consentimento LGPD/cookies: fluxo concluído.")
                return
            # PREFERÊNCIA: na tela de COOKIES, clica 'Recusar cookies opcionais'
            # (preserva privacidade) ANTES de qualquer 'Permitir'. Só cai no
            # avanço genérico (que inclui 'Allow all cookies' como último
            # recurso) se o 'recusar' não estiver na tela.
            try:
                dec = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role("button", name=self._COOKIE_DECLINE_RE),
                        page.get_by_text(self._COOKIE_DECLINE_RE),
                    ],
                    rounds=1,
                )
                await self._robust_click(dec)
                logger.info(
                    "Consentimento cookies: 'Recusar cookies opcionais' "
                    "clicado (passo %d, privacidade).", i + 1,
                )
                await self._organic_wait()
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                continue
            except LoginError:
                pass  # sem botão de recusar — tenta o avanço genérico abaixo.
            try:
                btn = await self._first_visible_scan(
                    page,
                    [page.get_by_role("button", name=self._CONSENT_ADVANCE_RE)],
                    rounds=3,
                )
            except LoginError:
                # Botão ainda não renderizou — dá um respiro e re-tenta.
                await asyncio.sleep(1.5)
                continue
            try:
                label = (await btn.inner_text()).strip()[:40]
            except Exception:  # noqa: BLE001
                label = "?"
            await self._robust_click(btn)
            logger.info(
                "Consentimento LGPD: '%s' clicado (passo %d).", label, i + 1
            )
            await self._organic_wait()
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
        logger.warning(
            "Consentimento LGPD/cookies: ainda na tela após 8 cliques — "
            "seguindo mesmo assim (best-effort)."
        )

    async def _dismiss_business_interstitials(self, page: Page) -> None:
        """Fecha modais informativos da esteira (best-effort, não-fatal).

        Cobre a política de não discriminação ("Aceito"), a confirmação de
        criação do portfólio ("Concluir") e prompts de reconhecimento
        ("Entendi"/"OK"/"Agora não"). NÃO inclui "Continuar"/"Enviar" para
        não disparar ações primárias sem querer. Pode haver mais de um modal
        empilhado, então tenta algumas vezes.
        """
        # Consentimento LGPD pode reaparecer dentro da esteira (não só após o
        # login) — passa por ele antes dos modais informativos.
        await self._dismiss_consent_flow(page)
        dismiss_re = re.compile(
            r"^\s*(aceito|concluir|entendi|got it|agora n[ãa]o|not now|"
            r"ok|fechar|dispensar)\s*$",
            re.I,
        )
        for _ in range(3):
            try:
                btn = page.get_by_role("button", name=dismiss_re).first
                if await btn.is_visible(timeout=1500):
                    await self._robust_click(btn)
                    logger.info("Interstício da esteira dispensado.")
                    await self._organic_wait()
                    continue
            except Exception:  # noqa: BLE001
                pass
            break

    # business.facebook.com pode renderizar DESLOGADO (marketing "Meta for
    # Business") mesmo com a sessão web.facebook.com válida — visto em
    # produção 2026-06 logo após o submit de criação do portfólio, derrubando
    # em cascata business_details (0/12 campos) e domain_security. Duas
    # variantes observadas nos DOMs salvos: chooser de login ("Entrar nas
    # ferramentas…" + "Continuar com o Facebook") e signup ("Já tem uma
    # conta?").
    _BUSINESS_LOGGED_OUT_RE = re.compile(
        r"entrar nas ferramentas|comece a usar as ferramentas"
        r"|j[áa] tem uma conta|get started with meta"
        r"|already have an account|log in to meta for business",
        re.I,
    )
    _BUSINESS_SSO_RE = re.compile(
        r"continuar com o facebook|continue with facebook"
        r"|j[áa] tem uma conta|already have an account",
        re.I,
    )
    # Sinais de HOME LOGADO do Business Suite — ausentes na loginpage/chooser.
    # Usados p/ NÃO disparar SSO quando o Meta já redirecionou de volta p/ a
    # home autenticada (run 265dce75: home 'Miguel Cole' LOGADA — screenshot do
    # operador — tratada como 'DESLOGADO', loopando no SSO inexistente).
    _BUSINESS_LOGGED_IN_RE = re.compile(
        r"meta business suite|procurar um ativo|portf[óo]lios empresariais"
        r"|criar an[úu]ncio|criar reel|editar foto da capa|create ad",
        re.I,
    )
    # Erro transitório do Facebook ('Estamos tendo problemas em acompanhar a
    # sua solicitação' / 'Something went wrong'): a página troca o conteúdo por
    # uma tela de erro e os campos somem. Recarregar resolve (produção
    # 2026-06, conta Fabio: portfolio-count=0 e campos críticos ausentes).
    # 2026-09 (relato do operador): a Meta também serve a MESMA tela com o
    # texto 'Estamos tendo problemas PARA CONCLUIR sua solicitação' — a regex
    # antiga só cobria 'em acompanhar' e a página de domínios ficava em branco
    # sem ninguém dar o F5. Agora a preposição e o verbo são livres.
    _FB_ERROR_RE = re.compile(
        r"estamos tendo problemas"
        r"|problemas (?:em|para|ao) (?:acompanhar|concluir|processar|"
        r"completar|atender)"
        r"|(?:having|have) (?:trouble|problems) (?:completing|processing|"
        r"following)"
        r"|algo deu errado|ocorreu um erro|tente novamente mais tarde"
        r"|something went wrong|we'?re having trouble"
        r"|sorry, something went wrong|please try again",
        re.I,
    )

    async def _ensure_business_session(
        self, page: Page, target_url: Optional[str] = None, *, force: bool = False
    ) -> bool:
        """Reentra via SSO quando business.facebook.com abre deslogado.

        Na variante signup, "Já tem uma conta?" leva ao chooser de login;
        no chooser, "Continuar com o Facebook" completa o SSO com a sessão
        já autenticada — por isso o loop de até 2 cliques. Ao final,
        re-navega para ``target_url`` (o SSO costuma despejar no overview,
        não na página pedida).

        ``target_url`` deve ser a página que o CHAMADOR quer — nunca usar
        ``page.url`` cegamente: pós-submit do portfólio o Meta despeja em
        ``business/loginpage/?...`` e voltar para lá re-renderiza o chooser
        deslogado em loop (visto em produção 2026-06).

        Devolve True se a sessão está OK ao final (nunca esteve deslogada
        ou foi recuperada); False se a página CONTINUA deslogada.
        """
        # Antes de tudo: se o Facebook serviu a tela de erro transitório,
        # recarrega — senão o detector de logout e os campos veriam a tela de
        # erro no lugar do conteúdo.
        await self._recover_from_fb_error(page)

        # ANTES de qualquer SSO: se a página JÁ assentou num HOME LOGADO do
        # Business Suite (o Meta redireciona de volta p/ /latest/home autenticado
        # após o bounce de criação), NÃO há 'Continuar com o Facebook' a clicar —
        # tentar o SSO ali estoura timeout e o bot loopa 'DESLOGADO' (run
        # 265dce75; home 'Miguel Cole' do screenshot, LOGADA). Reconhece e segue.
        # Curto-circuita MESMO sob force=True (o force é p/ a loginpage com
        # diálogo por cima, não p/ a home logada).
        if await self._business_logged_in(page):
            logger.info(
                "business: HOME LOGADO do Business Suite (url=%s) — sessão OK, "
                "SSO desnecessário.",
                (page.url or "")[:90],
            )
            return True

        # force=True: pula o atalho "diálogo visível = sessão OK". Usado quando
        # o CHAMADOR já SABE que está deslogado por um sinal externo (ex.: o
        # submit do portfólio bounceou para business/loginpage). Sem isso, o
        # diálogo de criação que o Meta renderiza SOBRE a loginpage faz
        # _business_logged_out devolver False e o SSO nunca roda — o bot
        # refaz o submit na loginpage morta e bounceia em loop (produção
        # 2026-06, conta Fabio, perfil reusado).
        if not force and not await self._business_logged_out(page):
            # Inclui o caso do diálogo de criação já aberto sobre o
            # marketing — sessão OK, segue direto pro preenchimento.
            return True

        came_from = page.url or ""
        # loginpage/chooser nunca é destino válido de retorno.
        resume_url = target_url or (
            "" if "/loginpage" in came_from else came_from
        )
        logger.warning(
            "business.facebook.com abriu DESLOGADO (sessão web ok, url=%s) — "
            "tentando SSO 'Continuar com o Facebook'.",
            came_from,
        )
        for _ in range(2):
            try:
                sso = await self._first_visible(
                    page,
                    [
                        page.get_by_role("button", name=self._BUSINESS_SSO_RE),
                        page.get_by_role("link", name=self._BUSINESS_SSO_RE),
                        page.get_by_text(self._BUSINESS_SSO_RE),
                    ],
                )
                # 'Continuar com o Facebook' abre um popup OAuth (OAuthShim)
                # que fica parado esperando 'Continuar como <nome>' — sem
                # cuidar dele a página mãe NUNCA loga (causa raiz dos
                # business_sso_failed de 2026-06).
                popup: Optional[Page] = None
                try:
                    # 20s: em proxy 4G o popup OAuth demora a abrir — 6s
                    # estourava antes dele existir (run 1781133809).
                    async with page.expect_popup(timeout=50000) as pop_info:
                        await self._robust_click(sso)
                    popup = await pop_info.value
                except PlaywrightTimeoutError:
                    popup = None  # clique navegou in-page, sem popup
                if popup is None:
                    # Popup atrasado ou aberto como ABA (target=_blank):
                    # varre o contexto por páginas extras de OAuth/login.
                    popup = self._find_oauth_page(page)
                if popup is not None:
                    await self._complete_sso_popup(popup)
                else:
                    # Variante SEM popup: a confirmação 'Continuar como
                    # <nome>' renderiza na própria aba — clicar nela é o
                    # que completa o OAuth.
                    await self._confirm_continue_as(page)
                # Fecha qualquer aba orphan que ficou no contexto (bug 2026-06:
                # popup OAuth não fechava, deixava aba viva que confundia o
                # fluxo e voltava pra portfolio_create). Crítico: limpa antes
                # de continuar pra não deixar contexto poluído.
                for p in page.context.pages:
                    if p is not page and not p.is_closed():
                        logger.warning("SSO: fechando aba orphan %s", p.url)
                        try:
                            await p.close()
                        except Exception:  # noqa: BLE001
                            pass
                # Dá tempo do OAuth redirecionar para fora da loginpage —
                # re-navegar cedo demais mata a continuação no meio.
                try:
                    await page.wait_for_url(
                        lambda u: "/loginpage" not in u, timeout=15000
                    )
                except PlaywrightTimeoutError:
                    pass
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    pass
                await self._organic_wait()
            except Exception as exc:  # noqa: BLE001
                await self._dump_debug_html(page, "business_sso")
                logger.warning(
                    "SSO do Business falhou (%s) — re-navegando direto.", exc
                )
                break
            try:
                still_out = await (
                    page.get_by_text(self._BUSINESS_LOGGED_OUT_RE)
                    .first.is_visible(timeout=1500)
                )
            except Exception:  # noqa: BLE001
                still_out = False
            if not still_out:
                break
        # Telemetria de calibração: se APÓS os cliques ainda estamos na
        # loginpage, captura o DOM pós-clique ANTES do re-goto — é a única
        # evidência do que o botão SSO realmente produziu.
        if "/loginpage" in (page.url or "") and await self._business_logged_out(page):
            await self._dump_debug_html(page, "business_sso_post_click")
        try:
            if resume_url:
                await page.goto(resume_url, wait_until="domcontentloaded")
                await self._organic_wait()
        except Exception:  # noqa: BLE001
            pass
        # Verificação final: o SSO pode abrir popup/redirect que não volta
        # sozinho — se a página alvo AINDA renderiza o chooser, avisa alto
        # e salva DOM (o chamador decide se aborta).
        still_out = await self._business_logged_out(page)
        if still_out and resume_url:
            # Última cartada: SSO sintético via login.php?next= — com a
            # sessão facebook.com ativa o redirect atravessa sem chooser.
            from urllib.parse import quote  # noqa: PLC0415

            fallback = (
                "https://www.facebook.com/login.php?next="
                + quote(resume_url, safe="")
            )
            logger.info("SSO fallback via login.php?next= → %s", resume_url)
            try:
                await page.goto(fallback, wait_until="domcontentloaded")
                await self._organic_wait()
                await page.goto(resume_url, wait_until="domcontentloaded")
                await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            still_out = await self._business_logged_out(page)
        if still_out and BUSINESS_SSO_MANUAL_TIMEOUT_S > 0:
            # Escape hatch: a janela do AdsPower é visível — um clique
            # humano no chooser destrava o perfil (e o Meta lembra). Poll
            # até o operador completar ou estourar o tempo.
            logger.warning(
                "SSO automático falhou — INTERVENÇÃO MANUAL: na janela do "
                "navegador, clique em 'Continuar com o Facebook' e confirme "
                "a conta. Aguardando até %.0fs…",
                BUSINESS_SSO_MANUAL_TIMEOUT_S,
            )
            deadline = time.monotonic() + BUSINESS_SSO_MANUAL_TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                if "/loginpage" in (page.url or ""):
                    continue
                if not await self._business_logged_out(page):
                    still_out = False
                    logger.success("SSO manual concluído pelo operador.")
                    break
            if not still_out and resume_url:
                try:
                    await page.goto(resume_url, wait_until="domcontentloaded")
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
                still_out = await self._business_logged_out(page)
        if still_out:
            await self._dump_debug_html(page, "business_sso_failed")
            logger.error(
                "SSO do Business NÃO destravou a sessão — página continua "
                "deslogada após retorno a %s (DOM salvo).",
                resume_url or came_from,
            )
            return False
        return True

    async def _recover_from_fb_error(
        self, page: Page, *, max_reloads: int = 3
    ) -> bool:
        """Recarrega a página quando o Facebook mostra erro transitório.

        Detecta a tela 'Estamos tendo problemas em acompanhar a sua
        solicitação' / 'Something went wrong' (que substitui o conteúdo e faz
        os campos sumirem) e dá ``page.reload`` até ``max_reloads`` vezes,
        com pausa orgânica entre as tentativas — exatamente o que o operador
        fazia à mão. Best-effort: nunca levanta.

        Devolve True se detectou o erro em algum momento (recarregou);
        False se a página estava limpa desde o início.
        """
        recovered = False
        for attempt in range(1, max_reloads + 1):
            try:
                visible = await (
                    page.get_by_text(self._FB_ERROR_RE).first.is_visible(
                        timeout=1500
                    )
                )
            except Exception:  # noqa: BLE001
                visible = False
            if not visible:
                return recovered
            recovered = True
            logger.warning(
                "Facebook mostrou erro transitório ('Estamos tendo problemas "
                "…sua solicitação') — dando F5 (tentativa %d/%d); a tela some "
                "e o fluxo continua de onde parou.",
                attempt, max_reloads,
            )
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                logger.debug("recover_fb_error: reload falhou (%s).", exc)
            await self._organic_wait()
        # Ainda na tela de erro após todos os reloads — loga e segue (o
        # chamador trata a ausência de conteúdo como antes).
        try:
            if await page.get_by_text(self._FB_ERROR_RE).first.is_visible(
                timeout=1000
            ):
                logger.error(
                    "Facebook seguiu na tela de erro após %d reloads — "
                    "seguindo sem recuperação.", max_reloads,
                )
        except Exception:  # noqa: BLE001
            pass
        return recovered

    async def _business_logged_in(self, page: Page) -> bool:
        """True se a página é um HOME LOGADO do Business Suite (não a loginpage).

        Discriminador decisivo: a URL é /latest/...|/overview (ou traz
        business_id) e NÃO contém /loginpage|login.php; não há form de login; e a
        marca do Business Suite logado está visível. Evita o SSO INÚTIL quando o
        Meta redireciona de volta p/ a home autenticada após o bounce de criação
        — o bot via isso como 'DESLOGADO' e loopava no SSO inexistente
        ('batendo a cabeça', run 265dce75; home 'Miguel Cole' do screenshot do
        operador, comprovadamente LOGADA: title 'Meta Business Suite', 0 form de
        login). Vale MESMO sob force=True (o force é p/ a loginpage com diálogo
        por cima, NÃO p/ a home logada)."""
        url = (page.url or "").lower()
        if "business.facebook.com" not in url:
            return False
        if "/loginpage" in url or "login.php" in url:
            return False
        if not ("/latest/" in url or "/overview" in url or "business_id=" in url):
            return False
        # Form de login visível ⇒ não está logado (descarta falso positivo).
        try:
            if await page.locator(
                'input[name="pass"], input[name="email"]'
            ).first.is_visible(timeout=800):
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            return await (
                page.get_by_text(self._BUSINESS_LOGGED_IN_RE)
                .first.is_visible(timeout=1500)
            )
        except Exception:  # noqa: BLE001
            return False

    async def _business_logged_out(self, page: Page) -> bool:
        """True se a página está REALMENTE deslogada (chooser/marketing).

        CRÍTICO (run 1781189106): em ``business/loginpage/?...&create_
        business_portfolio_for_bm=1`` o Meta renderiza o DIÁLOGO de criação
        de portfólio JÁ ABERTO, com o texto de marketing ('Continuar com o
        Facebook', 'Comece a usar…') no FUNDO do SPA. O detector antigo via
        só o marketing e dava falso-positivo de logout — o bot entrava em
        loop de SSO enquanto o formulário estava ali, preenchível.

        Regra: se o diálogo de criação está visível, NÃO está deslogado.
        """
        try:
            if await (
                page.get_by_text(self._PORTFOLIO_DIALOG_TITLE_RE)
                .first.is_visible(timeout=1500)
            ):
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            return await (
                page.get_by_text(self._BUSINESS_LOGGED_OUT_RE)
                .first.is_visible(timeout=5000)
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _find_oauth_page(page: Page) -> Optional[Page]:
        """Localiza popup/aba de OAuth aberto fora do expect_popup.

        Cobre popup atrasado (proxy lento) e SSO aberto como aba nova.
        Restringe a páginas do Facebook (o AdsPower pode ter abas próprias
        no contexto, ex. start page).
        """
        candidates = []
        for p in page.context.pages:
            if p is page or p.is_closed():
                continue
            url = (p.url or "").lower()
            if "facebook.com" in url or url in ("", "about:blank"):
                candidates.append(p)
        for p in candidates:
            if any(k in (p.url or "").lower() for k in ("oauth", "login", "dialog")):
                return p
        return candidates[0] if len(candidates) == 1 else None

    # Confirmação de conta do OAuth — aparece como card in-page OU dentro
    # do popup, dependendo da variante que o Meta serve. Na aba principal
    # só vale o padrão ESTRITO: o amplo ("continuar|entrar") casaria com o
    # próprio chooser ("Continuar com o Facebook/Instagram").
    _SSO_CONTINUE_AS_STRICT_RE = re.compile(r"continuar como|continue as", re.I)
    _SSO_CONTINUE_AS_BROAD_RE = re.compile(
        r"continuar como|continue as|continuar|continue|entrar|log in", re.I
    )

    async def _confirm_continue_as(self, page: Page, *, strict: bool = True) -> bool:
        """Clica a confirmação OAuth ('Continuar como <nome>') se visível.

        Best-effort: devolve True se clicou. ``strict=False`` só é seguro
        dentro do popup OAuth, onde não existe o chooser FB/IG.
        """
        pattern = (
            self._SSO_CONTINUE_AS_STRICT_RE
            if strict
            else self._SSO_CONTINUE_AS_BROAD_RE
        )
        try:
            btn = page.get_by_role("button", name=pattern).first
            if await btn.is_visible(timeout=5000):
                await self._robust_click(btn)
                logger.info("SSO: confirmação 'Continuar como…' clicada.")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _complete_sso_popup(self, popup: Page) -> None:
        """Completa o popup OAuth do SSO ('Continuar como <nome>').

        O OAuthShim fecha após a confirmação da conta e a página mãe recarrega
        autenticada. Fecha o popup EXPLICITAMENTE após o clique para evitar
        abas orphan/contexto confuso que causam volta pro portfolio_create
        (bug 2026-06: faltava close do popup, deixava aba viva).
        Best-effort — falha aqui só loga (a página mãe re-verifica depois).
        """
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
            await self._confirm_continue_as(popup, strict=False)
            # Espera o popup fechar sozinho (normal) ou força após 3s (timeout).
            try:
                await popup.wait_for_event("close", timeout=3000)
            except PlaywrightTimeoutError:
                # Popup não fechou sozinho — força o fechamento antes de
                # retornar pra página mãe (evita aba orphan no contexto).
                try:
                    await popup.close()
                    logger.info("Popup OAuth fechado explicitamente (timeout).")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Popup SSO não concluído (%s) — página mãe re-verifica.", exc
            )
            # Se falha, tenta fechar o popup antes de sair pra não deixar aba.
            try:
                if not popup.is_closed():
                    await popup.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _maybe_handle_portfolio_limit(page: Page) -> None:
        """Detecta o erro de limite de portfólios em qualquer parte da DOM.

        Varre 4 superfícies (não exclusivas):
          1. ``body`` inteiro — texto principal renderizado.
          2. ``[role="alert"] / [role="dialog"] / [role="status"] / [aria-live]``
             — modais, toasts e regiões de notificação que o Meta usa para
             erros estilizados fora do fluxo normal.
          3. Iframes embarcados — alguns wizards do Business Suite montam
             telas de erro dentro de frames isolados.

        Quando o padrão é detectado, levanta ``PortfolioLimitError`` —
        ``_run_business_pipeline`` captura e curta-circuita o pipeline,
        impedindo o robô de seguir e quebrar com timeouts genéricos.
        """
        async def _collect_text(loc) -> str:
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                return ""
            chunks: list[str] = []
            for i in range(min(count, 8)):  # limita varredura por performance
                try:
                    t = await loc.nth(i).inner_text(timeout=1500)
                except Exception:  # noqa: BLE001
                    continue
                if t:
                    chunks.append(t)
            return "\n".join(chunks)

        scopes = [
            page.locator("body"),
            page.locator('[role="alert"]'),
            page.locator('[role="dialog"]'),
            page.locator('[role="status"]'),
            page.locator('[aria-live]'),
        ]
        bag: list[str] = []
        for scope in scopes:
            text = await _collect_text(scope)
            if text:
                bag.append(text)

        # Frames isolados (wizards/modais que montam fora do main frame).
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                t = await frame.locator("body").inner_text(timeout=1500)
            except Exception:  # noqa: BLE001
                continue
            if t:
                bag.append(t)

        combined = "\n---\n".join(bag)
        if PORTFOLIO_LIMIT_PATTERN.search(combined):
            logger.error(
                "Limite de portfólios detectado — short-circuit do pipeline. "
                "Trecho: %s", combined[:280].replace("\n", " "),
            )
            raise PortfolioLimitError(combined[:200])

    # ------------------------------------------------------------------
    # Business details
    # ------------------------------------------------------------------
    # Campos cuja ausência aborta o save (Meta valida server-side e o
    # portfólio fica em estado inválido se enviarmos parcial).
    # O diálogo real "Editar detalhes da empresa" (screenshot produção
    # 2026-06) NÃO tem campo CNPJ/tax-id, e "País" é um dropdown que já vem
    # pré-selecionado como Brasil — exigi-los abortava TODO save. Críticos
    # são só os text inputs que existem e precisam de valor.
    # 'telefone' e 'pais' são OBRIGATÓRIOS no diálogo (vazios bloqueiam o
    # Salvar) e ambos têm valor garantido (telefone = nº provisório do SMS;
    # país = Brasil) — entram como críticos p/ a falha ser ALTA e diagnosticada.
    _CRITICAL_FIELDS: frozenset[str] = frozenset(
        {"razao_social", "cidade", "uf", "telefone", "pais"}
    )

    async def _resolve_business_selector(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Resolve a tela 'Selecionar negócio' (contas com múltiplos portfólios).

        Quando a conta tem >1 BM e a URL não fixou o business_id, o Meta serve
        um seletor ('Selecionar negócio') com cards em vez da página de info — e
        o probe de 'Detalhes da empresa' nunca acha nada (5× em produção
        2026-06-17, sempre com business_id ausente). Se sabemos o business_id
        deste run (criado no passo 1), clica o card certo; se NÃO sabemos, falha
        rápido apontando a causa REAL (passo 1 não capturou o portfólio) em vez
        do enganoso 'editor não hidratou'. Best-effort: se o seletor não está
        presente, não faz nada (caminho normal)."""
        try:
            await page.get_by_text(
                re.compile(r"selecionar neg[oó]cio|select business", re.I)
            ).first.wait_for(state="visible", timeout=5000)
        except Exception:  # noqa: BLE001
            return  # seletor ausente — página de info normal
        biz = result.business_id
        if not biz:
            await self._dump_debug_html(page, "business_selector_no_bizid")
            raise BusinessDetailsError(
                "tela 'Selecionar negócio' apareceu mas o business_id deste run "
                "é desconhecido (passo 1 não capturou o portfólio) — não dá p/ "
                "escolher a empresa certa; abortando p/ não editar a BM errada."
            )
        logger.warning(
            "business_details: 'Selecionar negócio' presente — clicando o card "
            "do business_id=%s (deste run).", biz,
        )
        card = page.locator(f'a[href*="business_id={biz}"]').first
        try:
            await card.wait_for(state="visible", timeout=5000)
            await self._robust_click(card)
            await self._organic_wait()
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                pass
        except Exception as exc:  # noqa: BLE001
            await self._dump_debug_html(page, "business_selector_card_missing")
            raise BusinessDetailsError(
                f"card do business_id={biz} não encontrado no seletor "
                f"'Selecionar negócio' ({type(exc).__name__})."
            ) from exc

    async def _fill_business_details(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Preenche o form de informações da empresa.

        Mudanças vs versão anterior:
          * Mapa por chave (em vez de tupla anônima) para rastrear campos
            críticos e detectar misses precisos.
          * Patterns multilíngues estendidos (PT-BR + EN + variantes).
          * Inclui **CNPJ** e **País** (não estavam mapeados antes).
          * Digitação via ``_press_sequentially`` — sobrevive a validação
            assíncrona do Meta que costuma roubar foco mid-typing.
          * Barreira de campos críticos: se Razão Social, CNPJ, País,
            Cidade ou UF falharem, **não clica em Salvar** e marca o
            resultado como ``business_details_saved=False`` com log claro.
        """
        logger.info("Abrindo configurações de informações da empresa.")
        info_url = self._with_business_id(BUSINESS_INFO_URL, result.business_id)
        await page.goto(info_url, wait_until="domcontentloaded")
        await self._organic_wait()
        # CRÍTICO: Detecta e fecha popup de "Alternar perfis" antes de business_details
        await self._detect_and_close_profile_modal(page, timeout_ms=5000)
        if not await self._ensure_business_session(page, target_url=info_url):
            # Sem sessão, procurar campos na loginpage só rende 12 timeouts
            # (~13 min vistos em produção). Aborta o save e segue a esteira.
            result.business_details_saved = False
            logger.error(
                "business_details: sessão do Business não estabelecida — "
                "etapa pulada (sem campos para preencher na loginpage)."
            )
            raise BusinessDetailsError(
                "sessão do Business não estabelecida (loginpage) — "
                "sem campos para preencher."
            )

        # Idempotência: detalhes JÁ salvos num run anterior (flag por perfil)
        # → pula direto pro domínio. Reabrir o diálogo numa conta preenchida
        # falhava: o layout muda com os campos já com valor (país vira texto
        # selecionado, cidade/uf não casam os seletores) e o save abortava
        # deixando o diálogo aberto (visto em produção 2026-06, Antonio).
        if self._profile_flag("details_ok"):
            result.business_details_saved = True
            logger.info(
                "Detalhes da empresa já salvos em run anterior "
                "(flag details_ok) — etapa pulada."
            )
            return

        # Contas com >1 portfólio podem cair na tela 'Selecionar negócio' em vez
        # da página de info (a URL nem sempre fixa o BM). Resolve ANTES de probar
        # hidratação/editor — senão o probe nunca acha 'Detalhes da empresa' e
        # aborta enganosamente como 'não hidratou' (5× em 2026-06-17).
        await self._resolve_business_selector(page, result)

        # HIDRATAÇÃO: a tela V3 de info é um SPA pesado e renderiza o conteúdo
        # DEPOIS do networkidle. Sem esta espera, o probe de 'Editar' achava 0
        # e abortava 0/11 com a página em shimmer (conta Danilo, 2026-06) —
        # causa-raiz da cascata que derrubava WhatsApp e verificação. Espera o
        # conteúdo real; se não hidratar, recarrega 1× e espera de novo.
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            pass
        await self._organic_wait()
        if not await self._wait_business_info_hydrated(page):
            logger.warning(
                "business_details: conteúdo não hidratou em ~25s — "
                "recarregando 1× antes do probe de edição."
            )
            try:
                await page.reload(wait_until="domcontentloaded")
                await self._organic_wait()
                await self._wait_business_info_hydrated(page)
            except Exception:  # noqa: BLE001
                pass

        # Empresa JÁ VERIFICADA pelo Meta → os detalhes legais ficam TRAVADOS
        # pela plataforma: o 'Editar' de 'Detalhes da empresa' vira LINK de
        # visualização (role=link, não button) e o bot gastava ~200s clicando
        # os 'Editar' errados até abortar 0/11 (visto em produção 2026-06,
        # conta Andreia: 'Status de verificação da empresa: Verificada').
        # Nada a preencher — etapa concluída.
        if await self._business_already_verified(page):
            result.business_details_saved = True
            self._set_profile_flag("details_ok")
            logger.success(
                "Empresa JÁ VERIFICADA pelo Meta — detalhes travados pela "
                "plataforma; etapa 2 concluída sem edição."
            )
            return

        # Abre o editor de 'Detalhes da empresa' com até 3 tentativas, RECARREGANDO
        # entre elas. A 'Informações da empresa' às vezes sobe em branco/shimmer
        # (IP recém-trocado, flaky) e o editor não abre — recarregar resolve sem
        # perder o login caro (auth custa minutos + rotações). Coachmarks do tour
        # cobrem o 'Editar' em contas novas → dispensa antes de cada tentativa.
        # FAIL-FAST: se após as tentativas o editor não abriu E há <3 campos de
        # texto na página, NÃO mói os 11 campos × ~20s (180s desperdiçados, visto
        # em produção 2026-06-17 run ffe412c8) — dumpa o DOM (página ainda viva)
        # e aborta rápido com erro claro.
        editor_open = False
        nboxes_probe = 0
        for tentativa in range(3):
            await self._dismiss_onboarding_coachmarks(page)
            await self._enter_edit_mode_if_needed(page)
            editor_open = await self._business_edit_open(page, timeout_ms=5000)
            if editor_open:
                break
            try:
                nboxes_probe = await page.locator(
                    'input:visible, textarea:visible, [role="textbox"]:visible'
                ).count()
            except Exception:  # noqa: BLE001
                nboxes_probe = 0
            if nboxes_probe >= 3:
                break  # provável edição inline legítima — segue p/ o preenchimento
            if tentativa < 2:
                logger.warning(
                    "business_details: editor não abriu (%d campo(s) de texto) — "
                    "recarregando e re-tentando (%d/2).",
                    nboxes_probe, tentativa + 1,
                )
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await self._organic_wait()
                    await self._wait_business_info_hydrated(page)
                except Exception:  # noqa: BLE001
                    pass
        if not editor_open and nboxes_probe < 3:
            await self._dump_debug_html(page, "business_details_editor_nao_abriu")
            logger.error(
                "business_details: editor de 'Detalhes da empresa' NÃO abriu após "
                "3 tentativas (%d campo(s) de texto na página) — provável página "
                "sem hidratar ou 'Editar' ausente. Abortando rápido (sem moer os "
                "11 campos). DOM salvo p/ diagnóstico.",
                nboxes_probe,
            )
            raise BusinessDetailsError(
                "editor de 'Detalhes da empresa' não abriu "
                f"({nboxes_probe} campos de texto visíveis) — página não hidratou?"
            )

        cnpj = self.cnpj_data
        # O diálogo real tem um único campo "Endereço" (sem número/bairro
        # separados) — compõe "logradouro, numero" como um humano digitaria.
        # A maioria dos CNPJs traz o número (consta no documento); quando a
        # fonte devolve vazio, usa "S/N" (convenção BR) p/ não deixar o
        # endereço sem número (decisão do operador 2026-06).
        _logr = (cnpj.logradouro or "").strip()
        # REDE DE SEGURANÇA (produção 2026-09, print do operador: campo
        # 'Endereço' com "2320"): ~10% dos MEIs vêm da Receita SEM logradouro
        # (CEP genérico da cidade, -000, em que o lookup de CEP também não
        # devolve rua). Nesse caso o join abaixo produzia SÓ O NÚMERO e a Meta
        # reprovava a verificação. O core.cnpj_lookup agora tenta a rua nos
        # provedores ao vivo; se mesmo assim não vier, cai para o
        # bairro/localidade (é o que consta no próprio registro público) e, em
        # último caso, a cidade. Um logradouro só-dígitos também não vale.
        if not _logr or not any(ch.isalpha() for ch in _logr):
            _fallback = (cnpj.bairro or "").strip() or (cnpj.cidade or "").strip()
            if _fallback:
                logger.warning(
                    "business_details: CNPJ sem logradouro utilizável (%r) — "
                    "usando '%s' no campo Endereço para NÃO enviar só o "
                    "número.", _logr or None, _fallback,
                )
            else:
                logger.error(
                    "business_details: CNPJ sem logradouro E sem bairro/cidade "
                    "— o campo Endereço vai só com o número (a Meta tende a "
                    "reprovar; conferir o CNPJ na base)."
                )
            _logr = _fallback
        _num = (cnpj.numero or "").strip() or ("S/N" if _logr else "")
        endereco = ", ".join(p for p in [_logr, _num] if p)
        logger.info("business_details: endereço composto = %r", endereco)
        site = (
            f"https://{self.business_profile.domain}"
            if self.business_profile and self.business_profile.domain
            else None
        )
        # Telefone comercial: USA O NÚMERO PROVISÓRIO DO SMS (o que o bot
        # controla e onde lê o código na etapa 4), não o telefone do CNPJ —
        # senão o SMS de verificação iria para o dono do documento. É campo
        # OBRIGATÓRIO no diálogo: vazio bloqueava o Salvar (visto em produção
        # 2026-06: 'Insira um número de telefone válido', conta Paula Rios).
        # Formato internacional com '+' (o número do sms24h já traz o DDI 55).
        _sms_phone = (self.preallocated_sms_phone or "").strip()
        _sms_digits = re.sub(r"\D+", "", _sms_phone)
        telefone_value = (
            f"+{_sms_digits}" if _sms_digits else (cnpj.telefone or None)
        )
        # ESCOPO do form: quando há OUTRO diálogo aberto (ex.: o picker
        # 'Selecionar negócio'), page.get_by_label casava os rótulos do diálogo
        # ERRADO → 0/N campos e save abortado. Restringe ao diálogo de edição.
        form_scope = await self._business_details_form_scope(page)
        # (key, locators, value) — key é usada para classificação critical/optional.
        field_map: list[tuple[str, list[Locator], Optional[str]]] = [
            ("razao_social",
                self._field_locators(form_scope,
                    r"raz[aã]o social|legal name|legal business name|company name"),
                # Mantém a raiz do CNPJ na frente (oficial); tira só o CPF que a
                # API às vezes anexa no fim (conta Jader, 2026-09).
                self._razao_social_for_form(cnpj.razao_social)),
            ("nome_fantasia",
                self._field_locators(form_scope,
                    r"nome fantasia|trade name|doing business|d/b/a|nome comercial"),
                cnpj.nome_fantasia),
            # 'Identificação fiscal (EIN)' NÃO é mais preenchida aqui (decisão
            # do operador 2026-06-20): ao ATUALIZAR os dados da empresa dentro do
            # portfólio comercial não precisamos do EIN/CNPJ. O CNPJ segue sendo
            # usado na verificação de empresa (P5, _bv_fill_tax_id) — só o passo
            # de detalhes do portfólio deixa de tocar nesse campo.
            # País ANTES dos campos regionais: trocar o país RESETA cidade/
            # estado/CEP/telefone (validação por país do Meta) — selecionado
            # por último, apagava o que já tinha sido digitado e o Salvar
            # ficava desabilitado (visto em produção 2026-06: cidade/uf/cep
            # vazios com erro após 'País selecionado: Brasil.').
            ("pais",
                self._field_locators(form_scope, r"pa[ií]s|country|nation"),
                "Brasil"),
            ("logradouro",
                self._field_locators(form_scope,
                    r"logradouro|endere[cç]o|street address|\brua\b|\bavenida\b"),
                endereco),
            # 'numero' e 'bairro' NÃO têm campo próprio no diálogo real: o
            # número já vai composto no Endereço ("Rua X, 360") e bairro não
            # existe. Mantê-los no mapa custava ~20s CADA varrendo seletores
            # que nunca casam (visto em produção 2026-06) — removidos.
            ("complemento",
                self._field_locators(form_scope,
                    r"complemento|complement|address line 2|continua[cç][aã]o"),
                cnpj.complemento),
            ("cidade",
                self._field_locators(form_scope, r"cidade|city|munic[ií]pio|localidade"),
                cnpj.cidade),
            ("uf",
                self._field_locators(form_scope, r"estado|state|\buf\b|\bst\b|provincia|province|state/province|uf/estado|uf/state"),
                cnpj.uf),
            ("cep",
                self._field_locators(form_scope, r"\bcep\b|zip|postal code|postal"),
                cnpj.cep),
            ("telefone",
                self._field_locators(form_scope,
                    r"telefone|phone|\btel\b|business phone|telefone comercial"),
                telefone_value),
            # "Site da empresa" existe no diálogo real e fica marcado como
            # inválido se vazio em alguns fluxos — preenche com o domínio
            # do Worker quando disponível (mesmo usado na etapa de marca).
            ("site",
                self._field_locators(form_scope, r"site da empresa|website|\bsite\b"),
                site),
        ]

        filled: list[str] = []
        missing_critical: list[str] = []

        # Diagnóstico: o editor abriu mesmo? Conta os campos de texto VISÍVEIS
        # no escopo. 0 = editor NÃO abriu (causa-raiz do 0/11 — enter_edit_mode
        # não achou o 'Editar' certo); >0 com campos faltando = abriu, mas os
        # RÓTULOS é que não casam (próxima camada a consertar). Antes a falha
        # não distinguia os dois casos (produção 2026-06-17, Leonardo Moreira).
        try:
            scope_kind = "página-inline" if form_scope is page else "diálogo"
            nboxes = await form_scope.locator(
                'input:visible, textarea:visible, [role="textbox"]:visible'
            ).count()
            logger.info(
                "business_details: escopo=%s, %d campo(s) de texto visível(is) "
                "no editor antes de preencher.",
                scope_kind, nboxes,
            )
        except Exception:  # noqa: BLE001
            pass

        for key, locators, value in field_map:
            if not value:
                if key in self._CRITICAL_FIELDS:
                    missing_critical.append(f"{key} (valor de entrada vazio)")
                    logger.warning("Campo crítico '%s' sem valor — não há o que digitar.", key)
                continue
            try:
                # Timeout curto por candidato: o diálogo já está renderizado,
                # então campos que EXISTEM aparecem no 1º/2º candidato na hora.
                # Campos AUSENTES (cnpj, numero, bairro… não há input no
                # diálogo real) não podem gastar 85s cada (5s × 17 candidatos,
                # visto em produção) — 1.2s × candidatos resolve a esmagadora
                # lentidão da etapa 2.
                target = await self._first_visible(page, locators, timeout_ms=3000)
            except LoginError:
                if key in self._CRITICAL_FIELDS:
                    missing_critical.append(f"{key} (seletor não encontrado)")
                    logger.warning("Campo CRÍTICO '%s' não localizado na DOM — abort save.", key)
                else:
                    logger.warning("Campo opcional '%s' não localizado — pulando.", key)
                continue
            # Só digita em elementos de texto reais. Combobox custom (ex.:
            # "País", que já vem pré-selecionado como Brasil) abriria o
            # dropdown ao receber cliques/teclas e deixaria o diálogo em
            # estado imprevisível para os campos seguintes.
            try:
                tag = ((await target.evaluate("el => el.tagName")) or "").lower()
            except Exception:  # noqa: BLE001
                tag = ""
            if tag == "select":
                try:
                    await target.select_option(label=value)
                    filled.append(key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Falha ao selecionar '%s': %s", key, exc)
                await self._organic_wait()
                continue
            if tag not in {"input", "textarea"}:
                # País é um combobox custom (div/role=combobox), NÃO um
                # <select> nem <input>. Ao contrário do que assumíamos, ele
                # NÃO vem pré-selecionado como Brasil — chega vazio e
                # obrigatório ('Selecione um país no menu suspenso', visto em
                # produção 2026-06). Abre o dropdown e clica em Brasil.
                if key == "pais":
                    if await self._select_pais_brasil(page, target):
                        filled.append(key)
                    elif key in self._CRITICAL_FIELDS:
                        missing_critical.append(f"{key} (combobox não selecionou Brasil)")
                        logger.warning(
                            "País CRÍTICO: seleção de 'Brasil' falhou — abort save."
                        )
                    else:
                        logger.warning(
                            "País: seleção de 'Brasil' falhou — o Save pode "
                            "ser bloqueado pelo Meta (campo obrigatório)."
                        )
                    continue
                logger.info(
                    "Campo '%s' é '%s' (não-texto) — mantendo valor padrão do Meta.",
                    key, tag or "?",
                )
                continue
            try:
                await self._press_sequentially(target, value)
            except Exception as exc:  # noqa: BLE001
                if key in self._CRITICAL_FIELDS:
                    missing_critical.append(f"{key} (erro na digitação)")
                logger.warning("Falha ao digitar '%s': %s", key, exc)
                continue
            await self._organic_wait()
            filled.append(key)

        logger.info("Campos preenchidos com sucesso: %d / %d (%s)",
                    len(filled), len(field_map), ", ".join(filled) or "—")

        # --- Barreira de campos críticos ---------------------------------
        if missing_critical:
            result.business_details_saved = False
            # 0 campos preenchidos quase sempre = form não estava presente
            # (modo edição não ativou, ou a página não é o form esperado).
            # Captura o DOM p/ diagnóstico — esta etapa não crasha, então
            # _dump_failure_evidence não roda.
            await self._dump_debug_html(page, "business_details")
            logger.error(
                "Save ABORTADO — campos críticos faltantes ou falhos: %s "
                "(%d/%d campos preenchidos). DOM salvo p/ diagnóstico.",
                missing_critical, len(filled), len(field_map),
            )
            # Fecha o diálogo (Escape) — deixá-lo aberto contaminava o passo
            # 3 (overlay interceptando cliques na página de domínios).
            try:
                await page.keyboard.press("Escape")
                await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            raise BusinessDetailsError(
                f"campos críticos não preenchidos ({len(filled)}/{len(field_map)}): "
                f"{', '.join(missing_critical)}"
            )

        # --- Save -------------------------------------------------------
        # Escopado ao DIÁLOGO primeiro (a página de fundo tem 'Save' oculto
        # que o .first page-wide pegava) e via scan de visibilidade.
        dialog_scope = page.locator('[role="dialog"]')
        save_candidates = [
            dialog_scope.get_by_role(
                "button",
                name=re.compile(
                    r"^\s*(salvar( altera[cç][oõ]es)?|save( changes)?)\s*$", re.I
                ),
            ),
            page.get_by_role(
                "button",
                name=re.compile(
                    r"salvar altera[cç][oõ]es|save changes|salvar|save|confirmar|confirm",
                    re.I,
                ),
            ),
            page.locator('button[type="submit"]'),
        ]
        try:
            save_btn = await self._first_visible_scan(page, save_candidates, rounds=3)
            await self._robust_click(save_btn)
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except (LoginError, PlaywrightTimeoutError) as exc:
            result.business_details_saved = False
            logger.warning("Falha ao salvar informações da empresa: %s", exc)
            raise BusinessDetailsError(
                f"falha ao clicar/confirmar Salvar: {exc}"
            ) from exc
        # VERIFICAÇÃO REAL: o save só vale se o diálogo de edição FECHOU.
        # Clicar num Salvar desabilitado (validação pendente) não navega nem
        # erra — o código antigo marcava sucesso com o diálogo aberto e
        # campos inválidos (falso 'salvas', visto em produção 2026-06).
        # O clique também pode cair num RE-RENDER e não surtir efeito (visto
        # em produção 2026-06, Izabel: tudo válido + Salvar habilitado +
        # diálogo aberto) — no meio do poll, re-clica o Salvar uma vez.
        still_editing = False
        for attempt in range(8):
            try:
                still_editing = await (
                    page.get_by_label(self._BIZ_RAZAO_LABEL_RE)
                    .first.is_visible(timeout=1000)
                )
            except Exception:  # noqa: BLE001
                still_editing = False
            if not still_editing:
                break
            if attempt == 3:
                try:
                    btn = await self._first_visible_scan(
                        page, save_candidates, rounds=1
                    )
                    await self._robust_click(btn)
                    logger.info(
                        "Salvar re-clicado (diálogo ainda aberto após o 1º clique)."
                    )
                except Exception:  # noqa: BLE001
                    pass
            await self._organic_wait()
        if still_editing:
            result.business_details_saved = False
            await self._dump_debug_html(page, "business_details_save_blocked")
            logger.error(
                "Salvar NÃO concluiu — diálogo de edição continua aberto "
                "(validação pendente/botão desabilitado). DOM salvo em .state/."
            )
            # Fecha o diálogo para não contaminar a etapa seguinte.
            try:
                await page.keyboard.press("Escape")
                await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            raise BusinessDetailsError(
                "Salvar não concluiu — diálogo de edição continua aberto "
                "(validação pendente/botão desabilitado)."
            )
        result.business_details_saved = True
        self._set_profile_flag("details_ok")
        logger.success("Informações cadastrais da empresa salvas.")

    @staticmethod
    def _field_locators(scope, label_pattern: str) -> list[Locator]:
        """Constrói lista de locators para um campo identificado por label.

        ``scope`` é a Page OU um Locator de diálogo — escopar ao diálogo do form
        evita casar rótulos de OUTRO diálogo (ex.: picker 'Selecionar negócio'),
        que derrubava o passo com 0/N campos (DOM real, conta 2026-06-16).

        Estratégia em camadas (do mais robusto ao fallback):
          1. ``get_by_label`` com regex completa (cobre aria-label, label-for,
             texto adjacente — funciona em PT-BR e EN).
          2. ``get_by_placeholder`` com a mesma regex.
          3. Seletores CSS por atributo (``aria-label``, ``placeholder``,
             ``name``) usando termos **ASCII puros** extraídos das
             alternativas da regex (descarta ``\\b``, classes de caractere,
             acentos — interpolar regex em CSS quebra silenciosamente).
        """
        regex = re.compile(label_pattern, re.IGNORECASE)
        locators: list[Locator] = [
            scope.get_by_label(regex),
            scope.get_by_placeholder(regex),
        ]

        # Extrai termos ASCII puros das alternativas para usar em CSS.
        css_terms: list[str] = []
        for alt in label_pattern.split("|"):
            term = re.sub(r"[^a-zA-Z0-9 ]", "", alt).strip().lower()
            if term and len(term) >= 3 and term not in css_terms:
                css_terms.append(term)

        # 3 melhores termos × 3 atributos = até 9 fallbacks CSS.
        for term in css_terms[:3]:
            locators.extend([
                scope.locator(f'input[aria-label*="{term}" i]'),
                scope.locator(f'input[placeholder*="{term}" i]'),
                scope.locator(f'input[name*="{term}" i]'),
                # Combobox/select para campos com dropdown (país, UF).
                scope.locator(f'select[aria-label*="{term}" i]'),
                scope.locator(f'[role="combobox"][aria-label*="{term}" i]'),
            ])
        return locators

    async def _business_details_form_scope(self, page: Page):
        """Locator do DIÁLOGO do form de 'Detalhes da empresa' (ou a própria page
        se a edição for inline). Restringir a busca dos campos a este diálogo
        evita casar rótulos de OUTRO diálogo aberto na página (ex.: o picker
        'Selecionar negócio'), que derrubava o passo com 0/N campos
        (causa-raiz confirmada por DOM, conta 2026-06-16)."""
        dlg = page.locator('[role="dialog"]').filter(
            has_text=re.compile(
                r"detalhes da empresa|business details|raz[ãa]o social"
                r"|legal business name", re.I
            )
        )
        try:
            await dlg.first.wait_for(state="visible", timeout=5000)
            logger.info("business_details: campos escopados ao diálogo de edição.")
            return dlg.first
        except Exception:  # noqa: BLE001
            logger.info(
                "business_details: diálogo do form não localizado — buscando "
                "campos na página inteira (edição inline?)."
            )
            return page

    # Label do input de razão social no diálogo "Editar detalhes da empresa"
    # — é o discriminador do diálogo CERTO (a tela de leitura mostra o texto
    # 'Razão social' mas NÃO um input).
    _BIZ_RAZAO_LABEL_RE = re.compile(
        r"raz[aã]o social|legal name|legal business name|company name", re.I
    )

    _VIDEO_SELFIE_RE = re.compile(
        r"selfie de v[íi]deo|video selfie"
        r"|confirme que voc[êe] [ée] uma pessoa real"
        r"|confirm (?:that )?you(?:'re| are)? a real person",
        re.I,
    )

    async def _is_video_selfie_gate(self, page: Page) -> bool:
        """Página é o gate de 'selfie de vídeo' (verificação de identidade que
        exige GRAVAR um vídeo)? Não há como automatizar isso → a conta deve ser
        DESCARTADA (pedido do operador 2026-06; visto na conta 64661428 no passo
        1, mal classificado como 'diálogo de criação não aberto')."""
        try:
            txt = await asyncio.wait_for(
                page.evaluate("() => document.body ? document.body.innerText : ''"),
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(self._VIDEO_SELFIE_RE.search(txt or ""))

    async def _dismiss_onboarding_coachmarks(self, page: Page) -> None:
        """Fecha os coachmarks/tour de onboarding do Business Suite.

        Ao criar o 1º portfólio, o Meta abre tooltips de tour ('Edite quais
        ferramentas você vê neste painel' com 'Avançar'; 'Alternar entre seus
        perfis de usuário' com 'Concluir') que COBREM o botão 'Editar' dos
        detalhes da empresa — os cliques no 'Editar' eram interceptados e a
        etapa 2 abortava com todos os campos críticos faltando (visto em
        produção 2026-06, conta Fabiano). Best-effort: fecha no X e clica os
        botões de avançar/concluir/pular do tour em algumas passadas.
        """
        dismiss_re = re.compile(
            r"^\s*(avan[cç]ar|pr[oó]ximo|next|pular|skip|concluir|done|"
            r"entendi|got it|ok|fechar|dispensar)\s*$",
            re.I,
        )
        # Se o diálogo de EDIÇÃO já está aberto (re-run após save bloqueado),
        # o X page-wide fecharia o diálogo certo — nesse caso não há
        # coachmark cobrindo nada, sai sem clicar.
        try:
            if await (
                page.get_by_label(self._BIZ_RAZAO_LABEL_RE)
                .first.is_visible(timeout=800)
            ):
                return
        except Exception:  # noqa: BLE001
            pass
        for _ in range(8):
            acted = False
            # 1) Botão 'X' de fechar (aria-label) dos tooltips do tour.
            try:
                x = page.locator(
                    '[aria-label="Fechar"], [aria-label="Close"]'
                ).first
                if await x.is_visible(timeout=800):
                    await self._robust_click(x)
                    acted = True
                    await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            # 2) Botões do tour por texto (Avançar/Concluir/Pular…).
            try:
                btn = page.get_by_role("button", name=dismiss_re).first
                if await btn.is_visible(timeout=800):
                    await self._robust_click(btn)
                    acted = True
                    await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            if not acted:
                break

    async def _business_already_verified(self, page: Page) -> bool:
        """True se a página de informações mostra a empresa como Verificada.

        A seção 'Status de verificação da empresa' exibe o badge 'Verificada'
        (ou 'Não verificada'). Empresa verificada = detalhes legais travados
        pelo Meta — não há edição possível nem necessária.
        """
        try:
            sec = page.get_by_text(
                re.compile(
                    r"status de verifica[cç][aã]o da empresa|"
                    r"business verification status", re.I,
                )
            ).first
            if not await sec.is_visible(timeout=1500):
                return False
        except Exception:  # noqa: BLE001
            return False
        # Negativo primeiro: 'Verificada' é substring de 'Não verificada'.
        try:
            neg = page.get_by_text(
                re.compile(r"n[ãa]o verificada|not verified", re.I)
            ).first
            if await neg.is_visible(timeout=800):
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            pos = page.get_by_text(
                re.compile(r"^\s*(verificada|verified)\s*$", re.I)
            ).first
            return bool(await pos.is_visible(timeout=800))
        except Exception:  # noqa: BLE001
            return False

    async def _wait_business_info_hydrated(self, page: Page) -> bool:
        """Espera o conteúdo REAL da tela de info da empresa renderizar.

        A página V3 ('BusinessInfoV3Route') é um SPA pesado: heading, campos e
        botões 'Editar' hidratam DEPOIS do domcontentloaded/networkidle. Probar
        cedo achava 0 'Editar' e abortava 0/11 com a tela ainda em shimmer
        (visto em produção 2026-06, conta Danilo: dump com 2 spinners e 0
        campos — a razão de toda a cascata WhatsApp/verificação falhar).

        Retorna True quando aparece um sinal estável (heading 'Detalhes da
        empresa'/'Informações do portfólio', texto 'Razão social', ou um botão
        'Editar'). Mesmo padrão já usado na etapa de domínios.
        """
        try:
            await self._first_visible_scan(
                page,
                [
                    page.get_by_role(
                        "heading",
                        name=re.compile(
                            r"detalhes da empresa|informa[cç][oõ]es do portf[oó]lio"
                            r"|business details|business info", re.I,
                        ),
                    ),
                    page.get_by_text(
                        re.compile(r"raz[aã]o social|legal name", re.I)
                    ),
                    page.get_by_role(
                        "button", name=re.compile(r"editar|edit", re.I)
                    ),
                ],
                rounds=25,
                round_wait_s=1.0,
            )
            return True
        except LoginError:
            return False

    async def _business_edit_open(self, page: Page, *, timeout_ms: int = 2500) -> bool:
        """O editor de 'Detalhes da empresa' está aberto?

        Sinal primário: o INPUT de razão social ficou visível (get_by_label só
        casa CONTROLE de formulário, não o texto da tela de leitura). Sinal
        alternativo (layouts que demoram a montar o <label> do input): um
        diálogo de edição com título 'Detalhes da empresa'/'Razão social'
        contendo ao menos um campo editável (input/textarea/role=textbox).
        """
        try:
            if await (
                page.get_by_label(self._BIZ_RAZAO_LABEL_RE)
                .first.is_visible(timeout=timeout_ms)
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            dlg = page.locator('[role="dialog"]').filter(
                has_text=re.compile(
                    r"detalhes da empresa|business details|raz[ãa]o social"
                    r"|legal business name|editar detalhes",
                    re.I,
                )
            )
            box = dlg.first.locator('input, textarea, [role="textbox"]').first
            if await box.is_visible(timeout=max(800, timeout_ms // 2)):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _enter_edit_mode_if_needed(self, page: Page) -> None:
        """Abre o editor de 'Detalhes da empresa' (razão social/endereço/tel.).

        A tela 'Informações do portfólio empresarial' tem VÁRIOS botões
        'Editar' (nome do portfólio, ponto comercial, contato do perfil…). O
        certo é o da SEÇÃO 'Detalhes da empresa', que fica perto do FIM de uma
        página enorme (~2.6 MB de DOM, abaixo da dobra) — por isso o antigo
        'varre os 5 primeiros Editar' não o alcançava e a etapa abortava 0/11
        campos (DOM real, conta Leonardo Moreira 2026-06-17).

        Estratégia: (1) já em edição? sai; (2) clica o 'Editar' ANCORADO no
        heading 'Detalhes da empresa' (1º role=button após o heading, com
        scroll) — determinístico; (3) fallback: varre cada 'Editar' e ACEITA
        só o que abre o editor (loga os candidatos p/ diagnóstico).
        """
        # (1) Já em modo de edição? — probe curto, não atrasa o caminho comum.
        if await self._business_edit_open(page, timeout_ms=800):
            logger.info("enter_edit_mode: editor de 'Detalhes da empresa' já aberto.")
            return

        # (2) 'Editar' ANCORADO no heading 'Detalhes da empresa': o primeiro
        #     role=button que aparece DEPOIS do heading da seção (ver DOM real).
        anchored = page.locator(
            'xpath=(//*[@role="heading"][contains(., "Detalhes da empresa")'
            ' or contains(., "Business details")]'
            '/following::*[@role="button"])[1]'
        )
        try:
            if await anchored.count():
                try:
                    await anchored.first.scroll_into_view_if_needed(timeout=3000)
                except Exception:  # noqa: BLE001
                    pass
                await self._robust_click(anchored.first)
                await self._organic_wait()
                if await self._business_edit_open(page):
                    logger.info(
                        "enter_edit_mode: editor aberto via 'Editar' da seção "
                        "'Detalhes da empresa' (âncora por heading)."
                    )
                    return
                logger.warning(
                    "enter_edit_mode: cliquei o 'Editar' ancorado em 'Detalhes "
                    "da empresa' mas o editor não abriu — varrendo os demais."
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("enter_edit_mode: âncora por heading falhou (%s).", exc)

        # (3) Fallback: conta os 'Editar' com RE-TENTATIVA (a tela re-renderiza
        #     num shimmer e um count() único às vezes pega 0 no meio do hydrate).
        editar = page.get_by_role("button", name=re.compile(r"editar|edit", re.I))
        total = 0
        for _ in range(4):
            try:
                total = await editar.count()
            except Exception:  # noqa: BLE001
                total = 0
            if total:
                break
            await self._organic_wait()
        if total == 0:
            # WARNING (não debug): página em branco/shimmer não tem nenhum
            # 'Editar' — antes isso era invisível no log e o passo seguia cego
            # moendo 11 campos (run ffe412c8, 2026-06-17). O chamador faz o
            # fail-fast; aqui só deixamos o rastro visível.
            logger.warning(
                "enter_edit_mode: nenhum botão 'Editar' na página (não "
                "hidratou? editor não será aberto)."
            )
            return
        # Loga os nomes dos candidatos (antes só dizia 'N candidatos' — opaco).
        try:
            names = await editar.all_inner_texts()
            logger.info(
                "enter_edit_mode: %d candidato(s) 'Editar' → %s",
                total,
                " | ".join(n.strip().replace("\n", " ")[:32] for n in names) or "—",
            )
        except Exception:  # noqa: BLE001
            pass
        for i in range(min(total, 8)):
            try:
                await editar.nth(i).scroll_into_view_if_needed(timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._robust_click(editar.nth(i))
                await self._organic_wait()
            except Exception as exc:  # noqa: BLE001
                logger.debug("enter_edit_mode: 'Editar' #%d não clicável (%s).", i, exc)
                continue
            if await self._business_edit_open(page):
                logger.info(
                    "Modo de edição: editor de 'Detalhes da empresa' aberto via "
                    "'Editar' #%d (fallback).", i,
                )
                return
            # Editor errado (nome do portfólio etc.) — fecha e tenta o próximo.
            try:
                await page.keyboard.press("Escape")
                await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
        logger.warning(
            "enter_edit_mode: não encontrei o 'Editar' que abre o editor de "
            "'Detalhes da empresa' (de %d candidato(s)) — seguindo (campos "
            "podem não aparecer). DOM salvo p/ diagnóstico.",
            total,
        )
        await self._dump_debug_html(page, "enter_edit_mode_no_razao")

    # ------------------------------------------------------------------
    # Domain security
    # ------------------------------------------------------------------
    # Navegação pela barra lateral das Configurações (fallback do goto direto):
    # 'Adequação e segurança para marcas' (brand safety) → 'Domínios'.
    _BRAND_SAFETY_NAV_RE = re.compile(
        r"adequa[çc][ãa]o e seguran[çc]a para marcas|brand safety", re.I
    )
    _DOMAINS_NAV_RE = re.compile(r"^\s*dom[íi]nios\s*$|^\s*domains\s*$", re.I)

    async def _open_domains_via_sidebar(self, page: Page) -> bool:
        """Navega até a página de domínios pela barra lateral das Configurações:
        'Adequação e segurança para marcas' → 'Domínios'. Fallback robusto p/
        quando o goto direto (owned-domains) redireciona para 'Informações da
        empresa' (produção 2026-06, conta Alipio: 'veio pra cá e travou')."""
        try:
            # 'Domínios' já visível (grupo expandido)? → clica direto.
            link = await self._first_visible(
                page,
                [
                    page.get_by_role("link", name=self._DOMAINS_NAV_RE),
                    page.get_by_role("treeitem", name=self._DOMAINS_NAV_RE),
                ],
            )
            if link is None:
                grp = await self._first_visible(
                    page,
                    [
                        page.get_by_role("button", name=self._BRAND_SAFETY_NAV_RE),
                        page.get_by_role("link", name=self._BRAND_SAFETY_NAV_RE),
                        page.get_by_text(self._BRAND_SAFETY_NAV_RE),
                    ],
                )
                if grp is None:
                    logger.warning(
                        "sidebar domínios: grupo 'Adequação e segurança para "
                        "marcas' não encontrado na barra lateral."
                    )
                    return False
                await self._robust_click(grp)
                await self._organic_wait()
                link = await self._first_visible(
                    page,
                    [
                        page.get_by_role("link", name=self._DOMAINS_NAV_RE),
                        page.get_by_role("treeitem", name=self._DOMAINS_NAV_RE),
                        page.get_by_text(self._DOMAINS_NAV_RE),
                    ],
                )
            if link is None:
                logger.warning(
                    "sidebar domínios: item 'Domínios' não apareceu após expandir "
                    "'Adequação e segurança para marcas'."
                )
                return False
            await self._robust_click(link)
            await self._organic_wait()
            logger.info(
                "sidebar domínios: naveguei por 'Adequação e segurança para "
                "marcas' → 'Domínios'."
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("sidebar domínios: falha na navegação lateral (%s).", exc)
            return False

    async def _configure_domain_security(
        self, page: Page, result: AutomationResult, _suffix_attempt: int = 0
    ) -> None:
        assert self.business_profile is not None
        domain = (self.business_profile.domain or "").strip()
        if not domain:
            logger.info("Nenhum domínio informado — etapa de marca pulada.")
            return

        # Idempotência rápida: domínio verificado em run anterior (flag por
        # perfil) → nada a fazer, nem precisa abrir a página.
        if self._profile_flag("domain_verified_ok"):
            result.domain_added = True
            result.domain_verified = True
            logger.info(
                "Domínio já verificado em run anterior "
                "(flag domain_verified_ok) — etapa pulada."
            )
            return

        logger.info("Adicionando domínio %s à segurança da marca.", domain)
        domains_url = self._with_business_id(BUSINESS_DOMAINS_URL, result.business_id)
        await page.goto(domains_url, wait_until="domcontentloaded")
        await self._organic_wait()
        # 'Estamos tendo problemas para concluir sua solicitação' → F5.
        await self._recover_from_fb_error(page)

        # networkidle PRIMEIRO: espera o SPA parar de carregar antes de
        # checar popup/conteúdo. Antes ficava depois do modal+session e
        # adicionava ~6s de espera redundante.
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            pass

        # Popup "Alternar perfis" — cobre o 'Adicionar' e intercepta cliques.
        # 1.5s de graça (antes era 3s): o popup aparece em ~1s se vai aparecer.
        if not await self._detect_and_close_profile_modal(
            page, timeout_ms=6000, wait_for_appear_ms=1500
        ):
            logger.error("domain_security: popup de 'Alternar perfis' bloqueando — abortando passo 2")
            raise LoginError(
                "Popup de 'Alternar entre perfis' não foi fechado — "
                "bloqueando o acesso à página de domínio (passo 2/5)."
            )

        # Espera conteúdo real (empty-state ou heading). Se visível, CONFIRMA
        # que estamos logados — pula _ensure_business_session (economiza ~4s).
        _domains_content_locators = [
            page.get_by_text(
                re.compile(r"nenhum dom[ií]nio|no domains", re.I)
            ),
            page.get_by_role(
                "heading", name=re.compile(r"dom[ií]nios|domains", re.I)
            ),
        ]
        _content_visible = False
        try:
            await self._first_visible_scan(
                page, _domains_content_locators, rounds=12, round_wait_s=1.0,
            )
            _content_visible = True
        except LoginError:
            pass

        if not _content_visible:
            # Conteúdo não renderizou — verificar se deslogou ou erro transitório.
            if not await self._ensure_business_session(page, target_url=domains_url):
                raise LoginError(
                    "domain_security: sessão do Business não estabelecida "
                    "(loginpage persistente) — sem página de domínios para operar."
                )
            _domains_rendered = False
            if await self._recover_from_fb_error(page):
                try:
                    await self._first_visible_scan(
                        page, _domains_content_locators, rounds=12, round_wait_s=1.0,
                    )
                    _domains_rendered = True
                    logger.success(
                        "Página de domínios renderizou após o F5 da tela de "
                        "erro — seguindo de onde parou."
                    )
                except LoginError:
                    _domains_rendered = False
            if not _domains_rendered:
                logger.warning(
                    "Página de domínios não renderizou — tentando a barra "
                    "lateral 'Adequação e segurança para marcas' → 'Domínios'."
                )
            if not _domains_rendered and await self._open_domains_via_sidebar(page):
                try:
                    await self._first_visible_scan(
                        page, _domains_content_locators, rounds=15, round_wait_s=1.0,
                    )
                except LoginError:
                    logger.warning(
                        "Página de domínios ainda não renderizou após a barra "
                        "lateral — seguindo para o scan do 'Adicionar' mesmo assim."
                    )

        # 2ª passada rápida do popup (ele pode subir DEPOIS do conteúdo hidratar).
        # Reduzido: 3s timeout + 1s de graça (era 6s + 2s). O loop do
        # 'Adicionar' abaixo também checa popup antes de cada clique.
        await self._detect_and_close_profile_modal(
            page, timeout_ms=3000, wait_for_appear_ms=1000
        )
        await self._organic_wait()

        # Idempotência on-page: o domínio pode JÁ estar listado (run anterior
        # adicionou). Re-adicionar dispara o fluxo de criação de novo e o
        # Meta NEM MOSTRA a metatag — o run terminava em 'metatag não
        # encontrada' mascarando que estava tudo pronto (visto em produção
        # 2026-06, conta Andreia: domínio já Verified, bot re-adicionou).
        badge = await self._domain_badge_state(page, domain)
        if badge == "verified":
            result.domain_added = True
            result.domain_verified = True
            self._set_profile_flag("domain_verified_ok")
            logger.success(
                "Domínio %s JÁ está listado e VERIFICADO — etapa pulada.",
                domain,
            )
            return
        if badge in ("unverified", "listed"):
            result.domain_added = True
            logger.info(
                "Domínio %s já listado (badge=%s) — abrindo o painel do "
                "domínio para retomar a verificação SEM re-adicionar.",
                domain, badge,
            )
            try:
                row = await self._first_visible_scan(
                    page, [page.get_by_text(domain)], rounds=3
                )
                await self._robust_click(row)
                await self._organic_wait()
            except LoginError:
                logger.warning(
                    "Não consegui abrir o painel do domínio já listado — "
                    "verificação fica para o próximo run (sem re-adicionar)."
                )
                return
            await self._finalize_domain_verification(page, result)
            return

        # 'Adicionar' abre o chooser "O que você quer fazer?" ('Criar um
        # domínio' / 'Solicitar acesso a um domínio'). Clica e ESPERA o
        # chooser ou o diálogo de input aparecer; se não vier (clique caiu no
        # loading), re-tenta o 'Adicionar' mais uma vez.
        create_opt_re = re.compile(
            r"criar um( novo)? dom[ií]nio|create a( new)? domain", re.I
        )
        dialog = page.locator('[role="dialog"]')
        dialog_ready = False
        for attempt in range(1, 5):  # Aumentado de 3 para 5 tentativas
            # Popup 'Alternar entre seus perfis de usuário' cobre o botão
            # 'Adicionar' e intercepta o clique → checa/fecha ANTES de cada
            # tentativa. Quick-check primeiro (barato); só chama o detector
            # completo se houver sinal. Pula se um diálogo NOSSO já estiver
            # aberto — o closer clica 'Concluir' e não pode mexer nele.
            try:
                _own_dialog = await dialog.first.is_visible(timeout=300)
            except Exception:  # noqa: BLE001
                _own_dialog = False
            if not _own_dialog and await self._profile_modal_quick_check(page):
                logger.info(
                    "domain_security: popup 'Alternar perfis' na frente do "
                    "'Adicionar' (tentativa %d) — clicando 'Concluir'.", attempt,
                )
                await self._detect_and_close_profile_modal(page, timeout_ms=6000)
                await self._organic_wait()
            # Scan por VISIBILIDADE (não .first): a página tem dezenas de
            # botões 'Add...' ocultos (menus/templates EN) e o .first trava
            # no 1º match do DOM mesmo invisível — o wait_for(visible)
            # estourava com o 'Adicionar' real na tela (visto em produção
            # 2026-06, conta Antonio). Anchored primeiro (evita 'Add to
            # cart'), amplo como fallback.
            try:
                add_btn = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^\s*\+?\s*(adicionar|add)\s*$", re.I),
                        ),
                        page.get_by_role(
                            "button",
                            name=re.compile(r"adicionar dom[ií]nio|add domain", re.I),
                        ),
                        page.get_by_role(
                            "button", name=re.compile(r"adicionar|add", re.I)
                        ),
                        # Variante EMPTY-STATE ("Nenhum domínio adicionado"): o
                        # 'Adicionar' vem como LINK ou <div> clicável SEM
                        # role=button — get_by_role('button') não casa e o scan
                        # falhava com o botão NA TELA (produção 2026-06, conta
                        # Gilda: DOM salvo tinha 'Adicionar', mas só como texto).
                        # Link e texto exato cobrem essa variante.
                        page.get_by_role(
                            "link",
                            name=re.compile(r"adicionar dom[ií]nio|adicionar|add", re.I),
                        ),
                        page.get_by_text(
                            re.compile(r"^\s*\+?\s*adicionar\s*$", re.I)
                        ),
                    ],
                    rounds=8,
                )
            except LoginError:
                logger.warning(
                    "Botão 'Adicionar' não encontrado na página de domínios "
                    "(tentativa %d/2) — re-tentando.", attempt,
                )
                # Sem 'Adicionar' na tela normalmente = tela de erro
                # transitório da Meta ocupando a página → F5 e re-tenta.
                await self._recover_from_fb_error(page)
                await self._organic_wait()
                continue
            await self._robust_click(add_btn)
            await self._organic_wait()
            # Chooser presente? Clica 'Criar um domínio' (scan visível).
            try:
                create_opt = await self._first_visible_scan(
                    page, [page.get_by_text(create_opt_re)], rounds=8
                )
                await self._robust_click(create_opt)
                logger.info("Chooser de domínio: 'Criar um domínio' clicado.")
                await self._organic_wait()
                dialog_ready = True
                break
            except LoginError:
                pass
            # Sem chooser: alguns layouts abrem o diálogo de input direto.
            try:
                if await dialog.first.is_visible(timeout=5000):
                    dialog_ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "Chooser/diálogo de domínio não apareceu após 'Adicionar' "
                "(tentativa %d/5) — aguardando 5s e re-tentando.", attempt,
            )
            # Causa nº1 do clique "perdido": o popup de perfis estava por cima
            # (ou subiu logo depois do clique). Fecha antes de re-tentar.
            await self._detect_and_close_profile_modal(
                page, timeout_ms=5000, wait_for_appear_ms=1500
            )
            # Causa nº2: o clique levou à tela 'Estamos tendo problemas para
            # concluir sua solicitação' → F5 e re-tenta o 'Adicionar'.
            await self._recover_from_fb_error(page)
            await asyncio.sleep(5.0)  # Aguarda 5s entre tentativas
        if not dialog_ready:
            await self._dump_debug_html(page, "domain_chooser_missing")
            err = LoginError(
                "Etapa de domínio: o chooser/diálogo não abriu após clicar "
                "'Adicionar' 5x — DOM salvo em .state/. [RESTART_FROM_ZERO]"
            )
            # Marca para reabrir navegador e reiniciar do zero
            err.restart_from_zero = True
            raise err

        # A Meta abre um chooser "O que você quer fazer?" (SEM input): as opções
        # 'Criar um domínio' / 'Solicitar acesso a um domínio'. Numa corrida de
        # render o loop acima pode ter marcado dialog_ready pela visibilidade do
        # diálogo enquanto ele ainda era o CHOOSER — aí o campo nunca aparece e a
        # busca abaixo estoura (produção 2026-09). Garante aqui o clique em
        # 'Criar um domínio'. Usa a presença de 'Solicitar acesso' como sinal
        # de que é o chooser (só ele tem a 2ª opção) — evita casar o próprio
        # header 'Criar um domínio' do diálogo de input. Idempotente.
        # A Meta abre um chooser "O que você quer fazer?" (SEM input): opções
        # 'Criar um domínio' / 'Solicitar acesso a um domínio'. Sob CORRIDA (2+
        # runs juntas) o render atrasa e o chooser aparecia DEPOIS da janela de
        # 2500ms — o bot pulava pro input (que não existe no chooser) e estourava
        # (produção 2026-09, print do operador: chooser aberto e travado). Agora
        # um loop resiliente: a cada tentativa, se o chooser está na tela clica
        # 'Criar um domínio', e só então procura o input; 4 tentativas.
        solicitar_re = re.compile(r"solicitar acesso|request access", re.I)
        input_locators = [
            dialog.get_by_placeholder(
                re.compile(r"dom[ií]nio|domain|exemplo|example", re.I)
            ),
            dialog.locator('input[name*="domain" i]').first,
            dialog.locator('input[type="text"]').first,
            dialog.locator("input").first,
        ]
        domain_input = None
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            # Chooser presente? (só ele tem 'Solicitar acesso'.)
            chooser_visible = False
            try:
                await page.get_by_text(solicitar_re).first.wait_for(
                    state="visible", timeout=2500
                )
                chooser_visible = True
            except Exception:  # noqa: BLE001
                chooser_visible = False
            if chooser_visible:
                try:
                    create_opt = await self._first_visible_scan(
                        page, [page.get_by_text(create_opt_re)], rounds=2,
                    )
                    await self._robust_click(create_opt)
                    logger.info(
                        "domain_security: chooser 'O que você quer fazer?' na tela "
                        "— 'Criar um domínio' clicado (tentativa %d/4).", attempt + 1,
                    )
                    await self._organic_wait()
                except LoginError:
                    pass  # o scan não achou a opção; tenta o input mesmo assim
            # Input escopado ao DIÁLOGO. SEM fallback por get_by_label page-wide:
            # ele casa o LINK de navegação 'Domínios' do fundo e o clique trava
            # 30s interceptado pelo overlay (visto 2x em produção 2026-06).
            try:
                domain_input = await self._first_visible(
                    page, input_locators, timeout_ms=4000,
                )
                break  # achou o campo — segue
            except LoginError as exc:
                last_exc = exc
                if attempt < 3:
                    await asyncio.sleep(1.5)  # dá tempo do render (corrida)
        if domain_input is None:
            raise last_exc or LoginError(
                "Etapa de domínio: campo de domínio não apareceu após 4 tentativas."
            )
        await self._human_type(domain_input, domain)
        await self._organic_wait()

        confirm = self._first_visible(
            page,
            [
                dialog.get_by_role(
                    "button",
                    name=re.compile(
                        r"adicionar|add|confirmar|continuar|continue|avan[cç]ar|next",
                        re.I,
                    ),
                ),
                dialog.locator('button[type="submit"]'),
                page.get_by_role(
                    "button", name=re.compile(r"adicionar|add|confirmar|continue", re.I)
                ),
                page.locator('button[type="submit"]'),
            ],
        )
        await self._robust_click(await confirm)
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass

        # Espera o RESULTADO do Adicionar: diálogo FECHA (sucesso) ou o Meta
        # devolve erro inline ('Erro ao realizar a consulta.', visto em
        # produção 2026-06, Izabel) e o diálogo fica aberto — o código antigo
        # seguia cego pra raspagem da metatag e reportava 'metatag não
        # encontrada' mascarando a causa real. Erro transitório → backoff e
        # re-clique (até 3 tentativas); persistindo → falha com a mensagem
        # REAL do Meta.
        _domain_err_re = re.compile(
            r"erro ao realizar a consulta|n[ãa]o foi poss[ií]vel"
            r"|tente novamente|try again|something went wrong",
            re.I,
        )
        added = False
        last_err = ""
        for attempt in range(1, 4):
            outcome = None
            for _ in range(20):  # Aumentado de 10 para 20 (aguarda ~20s total)
                try:
                    visible = await dialog.first.is_visible(timeout=800)
                except Exception:  # noqa: BLE001
                    visible = False
                if not visible:
                    outcome = "closed"
                    break
                # Erro DURO (domínio já verificado por outra empresa) → aborta
                # JÁ: re-tentar 3×12s não resolve (subdomínio colidiu c/ outra
                # BM). Checado ANTES do transitório p/ não cair no backoff à toa.
                try:
                    hard_loc = dialog.get_by_text(_DOMAIN_HARD_ERR_RE).first
                    if await hard_loc.is_visible(timeout=400):
                        try:
                            last_err = ((await hard_loc.inner_text()) or "")[:160]
                        except Exception:  # noqa: BLE001
                            last_err = "domínio já verificado por outra empresa"
                        outcome = "hard"
                        break
                except Exception:  # noqa: BLE001
                    pass
                try:
                    err_loc = dialog.get_by_text(_domain_err_re).first
                    if await err_loc.is_visible(timeout=400):
                        try:
                            last_err = ((await err_loc.inner_text()) or "")[:140]
                        except Exception:  # noqa: BLE001
                            last_err = "erro inline do Meta"
                        outcome = "error"
                        break
                except Exception:  # noqa: BLE001
                    pass
                await self._organic_wait()
            if outcome == "hard":
                await self._dump_debug_html(page, "domain_already_claimed")
                # PEDIDO DO OPERADOR: em vez de descartar o CNPJ de imediato,
                # tenta um SUBDOMÍNIO NUMERADO (nome1, nome2, nome3). Cada um é
                # um domínio NOVO, então o Meta não acusa "já verificado". Só
                # descarta o CNPJ depois de esgotar as tentativas. Re-entra o
                # passo inteiro (re-navega + re-digita o novo subdomínio) por
                # recursão — o deploy do site (Cloudflare/Netlify) no
                # _finalize usa o self.business_profile.domain já atualizado.
                _MAX_SUFFIX = 3
                if _suffix_attempt < _MAX_SUFFIX:
                    _root = getattr(self, "_domain_root_slug", "") or ""
                    _zone = getattr(self, "_domain_base_zone", "") or ""
                    if not _root or not _zone:
                        try:
                            _rslug, _zone = split_subdomain(domain)
                        except Exception:  # noqa: BLE001
                            _parts = domain.split(".")
                            _rslug, _zone = _parts[0], ".".join(_parts[1:])
                        _root = re.sub(r"\d+$", "", _rslug) or _rslug
                    _next = _suffix_attempt + 1
                    _new_domain = f"{_root}{_next}.{_zone}"
                    logger.warning(
                        "Domínio '%s' já verificado por outra empresa — tentando "
                        "subdomínio numerado '%s' (tentativa %d/%d).",
                        domain, _new_domain, _next, _MAX_SUFFIX,
                    )
                    self.business_profile.domain = _new_domain
                    self._domain_root_slug = _root
                    self._domain_base_zone = _zone
                    return await self._configure_domain_security(
                        page, result, _suffix_attempt=_next,
                    )
                # Esgotou as tentativas numeradas → mantém a regra antiga (descarta).
                cnpj_digits = (self.cnpj_data.cnpj or "").strip()
                if cnpj_digits:
                    from core import store  # noqa: PLC0415
                    discarded = store.discard_cnpj(cnpj_digits)
                    logger.warning(
                        f"[DISCARD_ON_DOMAIN_COLLISION] CNPJ {cnpj_digits} DESCARTADO "
                        f"após {_MAX_SUFFIX} subdomínios numerados (base '{domain}', "
                        f"status={discarded})"
                    )
                raise DomainAlreadyClaimedError(
                    f"Domínio '{domain}' e {_MAX_SUFFIX} variações numeradas já "
                    f"foram verificados por OUTRA empresa no Meta ({last_err}). "
                    "CNPJ descartado após esgotar as tentativas de subdomínio."
                )
            if outcome == "closed":
                added = True
                break
            if attempt < 3:
                logger.warning(
                    "Domínio NÃO adicionado (%s) — aguardando 20s e "
                    "re-clicando 'Adicionar' (tentativa %d/3).",
                    last_err or "diálogo continua aberto", attempt,
                )
                await asyncio.sleep(20.0)
                try:
                    retry_btn = await self._first_visible_scan(
                        page,
                        [
                            dialog.get_by_role(
                                "button",
                                name=re.compile(r"^\s*(adicionar|add)\s*$", re.I),
                            ),
                        ],
                        rounds=2,
                    )
                    await self._robust_click(retry_btn)
                    await self._organic_wait()
                except LoginError:
                    break
        if not added:
            await self._dump_debug_html(page, "domain_add_failed")
            raise LoginError(
                f"Meta recusou a adição do domínio "
                f"({last_err or 'diálogo de criação não fechou'}) — provável "
                "erro transitório do lado do Meta; re-rode a conta em alguns "
                "minutos. DOM salvo em .state/."
            )

        result.domain_added = True
        await self._finalize_domain_verification(page, result)

    async def _note_domain_incomplete(
        self,
        result: AutomationResult,
        reason: str,
        *,
        page: Optional[Page] = None,
        tag: Optional[str] = None,
    ) -> None:
        """Verificação de domínio não concluída — registra o MOTIVO (+ evidência
        opcional) SEM matar a run.

        O fluxo é 2-pass DE PROPÓSITO: domínio não-verificado é um estado
        legítimo (a 1ª passada deploya o Worker e para no passo 4 com
        ``whatsapp_blocked_domain_unverified``; a 2ª passada vê o badge
        'verificado'). Levantar aqui quebraria isso. O que faltava era o MOTIVO
        rastreável (em vez de um warning solto) + DOM nos pontos que não salvavam
        evidência (token e deploy) — para diagnosticar a 1ª conta que travar.
        """
        result.domain_verified = False
        result.notes.append(f"dominio_incompleto: {reason}")
        if page is not None and tag:
            await self._dump_debug_html(page, tag)

    async def _finalize_domain_verification(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Painel do domínio aberto → metatag → deploy do Worker → verify.

        Usado tanto após CRIAR o domínio quanto ao RETOMAR um domínio já
        listado de run anterior (idempotência — sem re-adicionar).
        """
        assert self.business_profile is not None
        domain = (self.business_profile.domain or "").strip()
        # Após abrir o painel do domínio, o Meta mostra a verificação com
        # "Selecione uma opção". A metatag SÓ aparece no método "Adicionar uma
        # metatag" — se o método default for outro (DNS/TXT), seleciona-o.
        await self._select_metatag_verification_method(page)
        # A metatag renderiza alguns segundos APÓS a criação — o scrape único
        # rodava cedo demais e devolvia None ('Metatag não encontrada', visto
        # em produção 2026-06: a tag <meta ... content="nwawwecl…"> estava na
        # tela). Poll resolve.
        #
        # Produção 2026-09 (conta Bernadet): o DOM salvo aos ~15s NÃO tinha o
        # painel de verificação aberto (0x 'Selecione uma opção', 0x metatag)
        # — o painel/método abriu tarde e o poll curto desistiu, deixando o
        # domínio sem deploy do token (nunca verifica, nem no 2º pass). Fix:
        # poll mais longo (~30s) e RE-selecionar o método metatag a cada 5
        # rodadas, para capturar o painel que abre atrasado. Aditivo: o
        # caminho feliz ainda sai na 1ª leitura bem-sucedida.
        raw_dom = None
        for _round in range(30):
            raw_dom = await self._scrape_domain_meta_tag(page)
            if raw_dom:
                break
            if _round and _round % 5 == 0:
                await self._select_metatag_verification_method(page)
            await self._organic_wait()
        if not raw_dom:
            logger.warning(
                "Metatag de verificação não encontrada na DOM após ~15s "
                "(DOM salvo em .state/)."
            )
            await self._note_domain_incomplete(
                result,
                "metatag de verificação não encontrada após ~15s",
                page=page,
                tag="domain_metatag_missing",
            )
            return

        result.domain_meta_tag = raw_dom

        # --- Extração cirúrgica do hash alfanumérico --------------------
        token_hash = self._extract_token_hash(raw_dom)
        if not token_hash:
            logger.warning(
                "Não foi possível extrair token alfanumérico de: %r", raw_dom,
            )
            await self._note_domain_incomplete(
                result,
                f"token não extraído da metatag ({raw_dom!r})",
                page=page,
                tag="domain_token_extract_failed",
            )
            return
        logger.success("Token de verificação extraído: %s", token_hash)
        # Persiste no result para auditoria/UI — sem este campo o operador
        # precisava abrir os logs para descobrir qual token foi injetado.
        result.meta_verification_token = token_hash

        # --- CNPJ apenas-dígitos (Worker exige 14 dígitos cravados) -----
        cnpj_digits = re.sub(r"\D+", "", self.cnpj_data.cnpj or "")
        if len(cnpj_digits) != 14:
            logger.warning(
                "CNPJ inválido para embed no Worker (%r — esperava 14 dígitos).",
                cnpj_digits,
            )
            await self._note_domain_incomplete(
                result, f"CNPJ inválido para o Worker ({cnpj_digits!r})"
            )
            return

        # --- Deploy do site (Netlify Edge Function) do cliente ----------
        # Precedência: override do run_service (domínio ativo do pool, já
        # validado no gate) > pool de domínios do store. O token é o da conta
        # Netlify dona do domínio. A página servida é a MESMA do worker antigo
        # (mesmo tema + dados do CNPJ + metatag), agora numa Edge Function.
        # --- Deploy do site (host do cliente) — Cloudflare OU Netlify ----
        if self.hosting_provider == "cloudflare":
            # Cloudflare (domínio próprio, wildcard): 1 Worker multi-tenant serve
            # TODOS os subdomínios; cada CNPJ é uma entrada no mapa. O gate do
            # run_service já validou zona/nameservers antes de gastar SMS.
            from core.cloudflare_deployer import (  # noqa: PLC0415
                CloudflareDomainDeployer, CloudflareDeployerError,
            )
            from core import store as _cf_store  # noqa: PLC0415
            _cf_slug, _cf_base = split_subdomain(domain)
            _default_tmpl = _cf_store.get_setting("default_template") or "corporate"
            _cf_store.cloudflare_domain_upsert_site_entry(
                _cf_base, _cf_slug, cnpj_digits, token_hash, domain,
                template=_default_tmpl,
            )
            _cf_entries = _cf_store.cloudflare_domain_list_site_entries(_cf_base)
            _cf_dep = CloudflareDomainDeployer(
                self.cloudflare_account_id, self.cloudflare_api_token,
            )
            if not _cf_dep.enabled:
                logger.warning(
                    "Cloudflare não configurada — pulando deploy e verificação. "
                    "Cadastre domínio + Account ID + API Token no painel."
                )
                await self._note_domain_incomplete(
                    result, "Cloudflare não configurada (sem domínio/conta/token)"
                )
                return
            _cf_ok = False
            for _cf_attempt in range(1, 4):
                try:
                    await _cf_dep.publish_domain(_cf_base, _cf_entries)
                    result.site_deployed = True
                    logger.success(
                        "Site publicado no Cloudflare (domain=%s, cnpj=%s, hash=%s).",
                        domain, cnpj_digits, token_hash,
                    )
                    _cf_ok = True
                    break
                except CloudflareDeployerError as _cf_exc:
                    _cf_s = str(_cf_exc)
                    if ("429" in _cf_s or "rate" in _cf_s.lower()) and _cf_attempt < 3:
                        logger.warning(
                            "Rate limit Cloudflare (tentativa %d/3) — aguardando 20s",
                            _cf_attempt,
                        )
                        await asyncio.sleep(20.0)
                    else:
                        logger.warning("Falha no deploy do site (Cloudflare): %s", _cf_exc)
                        await self._note_domain_incomplete(
                            result,
                            f"deploy do site (Cloudflare) falhou: {_cf_exc}",
                            page=page,
                            tag="domain_deploy_failed",
                        )
                        return
            if not _cf_ok:
                return
        else:
            api_token = self.netlify_api_token
            if not api_token:
                try:
                    _pool = NetlifyDomainPool()
                    _active = _pool.active() if _pool.enabled else None
                except Exception as exc:  # noqa: BLE001
                    _active = None
                    logger.debug("Pool Netlify indisponível: %s", exc)
                if _active:
                    api_token = _active.get("api_token")

            deployer = NetlifyDeployer(api_token=api_token or "")
            if not deployer.enabled:
                logger.warning(
                    "Netlify não configurada — pulando deploy e verificação. "
                    "Cadastre um domínio + token Netlify no painel (Configurações)."
                )
                await self._note_domain_incomplete(
                    result, "Netlify não configurada (sem domínio/token)"
                )
                return
            # EMBUTE os dados do CNPJ já consultados (self.cnpj_data) no render,
            # para o site NÃO depender da consulta LIVE (que às vezes falha e
            # caía no CNPJ de exemplo, abortando o deploy — típico na 2ª BM,
            # produção 2026-09). Sem dados, fica None e o render busca live.
            empresa_json = None
            try:
                from .site_template import (  # noqa: PLC0415
                    empresa_json_from_cnpjdata,
                )
                empresa_json = empresa_json_from_cnpjdata(self.cnpj_data)
            except Exception as _emp_exc:  # noqa: BLE001
                logger.debug("empresa_json_from_cnpjdata falhou: %s", _emp_exc)
            # Retry no deploy: trata rate limit 429 de forma especial (retry em 20s)
            deploy_success = False
            for deploy_attempt in range(1, 4):
                try:
                    slug, base_domain = split_subdomain(domain)
                    if NETLIFY_WILDCARD:
                        # WILDCARD: 1 site por domínio serve todos os subdomínios (exige
                        # o recurso wildcard habilitado na conta Netlify).
                        await deployer.publish_cnpj(
                            base_domain, slug=slug, cnpj_digits=cnpj_digits, token_hash=token_hash,
                        )
                    else:
                        # PADRÃO: 1 site por CNPJ (custom_domain = o subdomínio). Funciona
                        # em qualquer plano; o Netlify cria DNS + cert por subdomínio.
                        await deployer.deploy_subdomain(
                            domain, cnpj_digits=cnpj_digits, token_hash=token_hash,
                            empresa_json=empresa_json,
                        )
                    result.site_deployed = True
                    logger.success(
                        "Site publicado no Netlify (domain=%s, cnpj=%s, hash=%s).",
                        domain, cnpj_digits, token_hash,
                    )
                    # NÃO conta no limite aqui: o site só é contabilizado quando a run
                    # conclui 100% (ver bloco finally de run()). Run que quebra no meio
                    # NÃO consome o limite do domínio (pedido do operador).
                    deploy_success = True
                    break  # Sucesso, sai do loop
                except NetlifyDeployerError as exc:
                    exc_str = str(exc)
                    # Erros TRANSITÓRIOS que valem re-tentar:
                    #  • rate limit (429) do Netlify;
                    #  • a consulta LIVE do CNPJ falhar DURANTE o render e a
                    #    página cair no CNPJ de exemplo do template ("Tente de
                    #    novo em alguns segundos"). Na 2ª BM esse hiccup da API
                    #    de CNPJ derrubava o domínio, que depois travava a
                    #    verificação por não ter domínio verificado (produção
                    #    2026-09, conta Susana — 2 BMs no mesmo perfil).
                    _is_429 = "429" in exc_str
                    _is_cnpj_render = (
                        "consulta do CNPJ falhou" in exc_str
                        or "CNPJ de exemplo do template" in exc_str
                    )
                    if (_is_429 or _is_cnpj_render) and deploy_attempt < 3:
                        _wait = 20.0 if _is_429 else 8.0
                        logger.warning(
                            "Deploy transitório (%s) — tentativa %d/3, aguardando "
                            "%.0fs e re-tentando.",
                            "rate limit 429" if _is_429 else "consulta CNPJ no render",
                            deploy_attempt, _wait,
                        )
                        await asyncio.sleep(_wait)
                    else:
                        logger.warning("Falha no deploy do site (Netlify): %s", exc)
                        await self._note_domain_incomplete(
                            result,
                            f"deploy do site (Netlify) falhou: {exc}",
                            page=page,
                            tag="domain_deploy_failed",
                        )
                        return

        # --- PROBE: a metatag está mesmo no ar? -------------------------
        # Faz o MESMO que o crawler do Meta (GET na raiz + procura a metatag
        # com o token) ANTES de pedir a verificação. O deploy do Netlify/CF
        # responder "ready" não garante que o SUBDOMÍNIO já resolve com SSL —
        # pedir verificação antes disso queima o pedido em silêncio. Janela
        # curta (METATAG_PROBE_ATTEMPTS × METATAG_PROBE_INTERVAL_S ≈ 90s);
        # o clique acontece de qualquer forma, o probe só decide se dá pra
        # AFIRMAR que o domínio está verificado no fim.
        # ESPERA LONGA (pedido do operador 2026-09): o DNS/SSL do subdomínio
        # leva minutos para propagar; a janela curta de ~90s reprovava sites
        # que subiriam logo depois e a run seguia com o domínio pendente.
        # Metade da janela total vai para o probe; o resto fica para o badge.
        deadline = time.monotonic() + DOMAIN_VERIFY_WAIT_S
        probe_attempts = METATAG_PROBE_ATTEMPTS
        if DOMAIN_VERIFY_STRICT:
            probe_attempts = max(
                METATAG_PROBE_ATTEMPTS,
                int((DOMAIN_VERIFY_WAIT_S / 2) // max(METATAG_PROBE_INTERVAL_S, 1)),
            )
        result.domain_metatag_live = await self._await_metatag_served(
            domain, token_hash,
            attempts=probe_attempts,
            interval=METATAG_PROBE_INTERVAL_S,
        )
        if not result.domain_metatag_live:
            logger.warning(
                "Metatag NÃO confirmada em https://%s/ após ~%ds — site público "
                "ainda não responde com o token (DNS/SSL propagando, deploy "
                "quebrado ou 401/404 do host). Clicando 'Verificar domínio' "
                "assim mesmo, mas SEM marcar o domínio como verificado.",
                domain, int(probe_attempts * METATAG_PROBE_INTERVAL_S),
            )

        # --- Clica "Verificar Domínio" e aguarda confirmação do Meta -----
        # O clique vem depois do probe, mas a espera do badge é a mesma: o Meta
        # verifica de forma assíncrona (crawl em background, até 72h).
        await self._click_verify_domain(page, result, deadline=deadline)

        # HONESTIDADE: só marca verificado quando (a) o badge 'Verified'
        # apareceu na linha do domínio — já tratado em _click_verify_domain —
        # ou (b) o probe provou que a metatag está pública no domínio certo
        # (aí a verificação assíncrona do Meta é questão de tempo).
        # Sem nenhum dos dois, o domínio fica NÃO verificado: o passo 4 barra a
        # criação da WABA (DomainNotVerifiedError) em vez de gerar uma conta
        # incompleta, e a run é retomada mais tarde pelo fluxo de 2 passadas.
        await self._enforce_domain_verified(page, result, domain)
        return

    async def _enforce_domain_verified(
        self, page: Page, result: AutomationResult, domain: str
    ) -> None:
        """Fecha o passo 2: só deixa a esteira seguir com o domínio verificado.

        Extraído de ``_finalize_domain_verification`` para ficar testável — é
        o ponto onde a run PARA (``DomainNotVerifiedError``, NON_RETRYABLE) em
        vez de avançar para os passos 3-5 com o domínio pendente.
        """
        if not result.domain_verified:
            if DOMAIN_VERIFY_STRICT:
                # GATE: sem domínio verificado a esteira NÃO avança. O passo 5
                # depende dele (sem domínio a Central de Segurança nem mostra
                # 'Iniciar verificação' — produção 2026-09) e seguir só queima
                # conta, CNPJ, SMS e comprovante. A run fica RETOMÁVEL: o botão
                # 'Continuar BM' repete com a MESMA conta/CNPJ/BM e o passo 2
                # detecta o badge assim que o Meta confirmar.
                await self._note_domain_incomplete(
                    result,
                    f"domínio {domain} não foi verificado pelo Meta dentro de "
                    f"~{int(DOMAIN_VERIFY_WAIT_S / 60)} min "
                    f"(metatag no ar: {'SIM' if result.domain_metatag_live else 'NÃO'})",
                    page=page,
                    tag="domain_not_verified_timeout",
                )
                msg = (
                    f"domain_security PAROU — domínio {domain} ainda NÃO "
                    f"verificado pelo Meta após ~{int(DOMAIN_VERIFY_WAIT_S / 60)} "
                    f"min de espera (metatag no ar pelo probe: "
                    f"{'SIM' if result.domain_metatag_live else 'NÃO'}). A esteira "
                    "NÃO avança sem domínio verificado — o passo 5 não encontra "
                    "'Iniciar verificação' e a run queimaria conta e CNPJ. "
                    "Use 'Continuar BM' quando o Meta confirmar (a mesma BM e o "
                    "mesmo CNPJ são reaproveitados)."
                )
                logger.error(msg)
                raise DomainNotVerifiedError(msg)
            if result.domain_metatag_live:
                logger.info(
                    "Meta não mostrou o badge 'Verified' imediatamente, mas a "
                    "metatag está COMPROVADAMENTE no ar (probe HTTP em https://%s/) "
                    "— marcando como verificado (DOMAIN_VERIFY_STRICT=false).",
                    domain,
                )
                result.domain_verified = True
            else:
                await self._note_domain_incomplete(
                    result,
                    f"metatag não confirmada no ar em https://{domain}/ "
                    f"(probe ~{int(METATAG_PROBE_ATTEMPTS * METATAG_PROBE_INTERVAL_S)}s) "
                    "e badge 'Verified' não apareceu",
                    page=page,
                    tag="domain_metatag_not_served",
                )
                logger.error(
                    "Domínio %s NÃO verificado: o site público não serviu a "
                    "metatag e o Meta não confirmou. Confira se https://%s/ abre "
                    "(HTTP 200 + <meta facebook-domain-verification>) e re-rode a "
                    "conta — a 2ª passada detecta o badge e libera o WhatsApp.",
                    domain, domain,
                )

    async def _await_metatag_served(
        self,
        domain: str,
        token_hash: str,
        *,
        attempts: int = 80,
        interval: Optional[float] = None,
    ) -> bool:
        """Poll HTTP no domínio até a metatag com o token estar no ar.

        Faz o MESMO que o crawler do Meta: GET na raiz e procura
        ``<meta name="facebook-domain-verification" content="<token>">``.
        Evita pedir verificação cedo demais (propagação do edge) e detecta
        deploy/rota quebrados sem depender da leitura da tela do Meta.

        Chamado ANTES de pedir "Verificar domínio" ao Meta: sem essa prova o
        bot pedia verificação de um site que ainda não estava no ar (ou que o
        host devolvia 401/404) e ninguém percebia.

        ``attempts``/``interval``: janela padrão do fluxo = METATAG_PROBE_ATTEMPTS
        × METATAG_PROBE_INTERVAL_S (15 × 6s ≈ 90s, ajustável no .env). Retorna
        na PRIMEIRA resposta boa — só custa tempo quando o site não está no ar.
        """
        url = f"https://{domain}/"
        needle = f'content="{token_hash}"'
        for i in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=15.0, follow_redirects=True
                ) as client:
                    resp = await client.get(
                        url, headers={"Cache-Control": "no-cache"}
                    )
                body = resp.text
                if "facebook-domain-verification" in body and needle in body:
                    logger.success(
                        "Metatag de verificação CONFIRMADA no ar em %s "
                        "(tentativa %d).", url, i,
                    )
                    return True
                logger.info(
                    "Metatag ainda não servida em %s (tentativa %d/%d, HTTP %s) "
                    "— aguardando propagação.", url, i, attempts, resp.status_code,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Probe da metatag falhou (tentativa %d/%d): %s — re-tentando.",
                    i, attempts, exc,
                )
            await asyncio.sleep(
                NETLIFY_DEPLOY_WAIT_S if interval is None else interval
            )
        return False

    @staticmethod
    def _extract_token_hash(raw_dom: str) -> Optional[str]:
        """Extrai APENAS o hash alfanumérico do meta tag scraped.

        Aceita 3 formatos:
          * Tag completa:  ``<meta name="facebook-domain-verification" content="abc123" />``
          * Variante aspas simples: ``content='abc123'``
          * Hash isolado (operador colou só o code): ``abc123def456``
        """
        if not raw_dom:
            return None
        # Tenta extrair de meta-tag completo (aspas duplas ou simples).
        m = re.search(
            r'facebook-domain-verification[^>]*content\s*=\s*["\']([A-Za-z0-9_-]+)["\']',
            raw_dom,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        # Fallback: o operador pode ter colado apenas o hash.
        stripped = raw_dom.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z0-9_-]{8,}", stripped):
            return stripped
        return None

    # Badges de status do domínio. Negativo testado ANTES do positivo:
    # 'Verified'/'verificada' é substring de 'Not Verified'/'Não verificada'.
    _DOMAIN_NEG_RE = re.compile(r"n[ãa]o verificad|not verified", re.I)
    _DOMAIN_POS_RE = re.compile(
        r"verificad[oa] com sucesso|dom[ií]nio verificado|"
        r"successfully verified|domain verified|(?<!not )\bverified\b|"
        r"\bverificad[oa]\b",
        re.I,
    )

    # Devolve o textContent do MENOR elemento visível que contém o domínio
    # E algum badge de verificação — i.e. a LINHA do domínio alvo. Containers
    # maiores (página inteira) misturam badges de OUTROS domínios e causavam
    # falso positivo (produção 2026-06, Cleber). '' = domínio na tela sem
    # badge legível; null = domínio ausente.
    _JS_DOMAIN_ROW_TEXT = """
    (dom) => {
      const els = Array.from(document.querySelectorAll('body *')).filter((e) => {
        if (e.tagName === 'SCRIPT' || e.tagName === 'STYLE') return false;
        if (e.offsetParent === null && e.getClientRects().length === 0) return false;
        return (e.textContent || '').includes(dom);
      });
      if (!els.length) return null;
      const badgeRe = /n[aã]o verificad|not verified|verificad[oa]|verified/i;
      let best = null;
      for (const e of els) {
        const t = e.textContent || '';
        if (badgeRe.test(t) && (!best || t.length < (best.textContent || '').length)) {
          best = e;
        }
      }
      // innerText (não textContent): preserva separação visual entre o
      // nome do domínio e o badge — textContent concatena 'devVerified'
      // e quebra a fronteira de palavra do regex.
      return best ? (best.innerText || best.textContent || '') : '';
    }
    """

    async def _domain_badge_state(self, page: Page, domain: str) -> str:
        """Estado do domínio na página de domínios.

        Retorna: 'absent' (não listado), 'verified', 'unverified' ou
        'listed' (na tela mas sem badge legível).
        """
        try:
            row_text = await page.evaluate(self._JS_DOMAIN_ROW_TEXT, domain)
        except Exception:  # noqa: BLE001
            return "absent"
        if row_text is None:
            return "absent"
        if self._DOMAIN_NEG_RE.search(row_text):
            return "unverified"
        if self._DOMAIN_POS_RE.search(row_text):
            return "verified"
        return "listed"

    async def _find_verify_domain_button(self, page: Page):
        """Locator do botão 'Verificar domínio' (None se ausente)."""
        try:
            return await self._first_visible(
                page,
                [
                    page.get_by_role(
                        "button",
                        name=re.compile(r"verificar dom[ií]nio|verify domain", re.I),
                    ),
                    page.get_by_text(
                        re.compile(r"verificar dom[ií]nio|verify domain", re.I)
                    ),
                ],
                timeout_ms=5000,
            )
        except LoginError:
            return None

    async def _click_verify_domain(
        self,
        page: Page,
        result: AutomationResult,
        *,
        deadline: Optional[float] = None,
    ) -> None:
        """Clica 'Verificar Domínio' e ESPERA a confirmação do Meta.

        A verificação do Meta é assíncrona: com a metatag no ar, costuma
        confirmar ao clicar, mas às vezes precisa de RE-CLIQUES após o crawl
        (visto em produção 2026-06, conta Paulo: metatag no ar, mas o badge
        seguia 'Não verificado' no poll curto). Poll generoso + re-cliques
        periódicos antes de declarar não-verificado.
        """
        dom = (self.business_profile.domain or "").strip()
        verify_btn = await self._find_verify_domain_button(page)
        if verify_btn is None:
            # Botão ausente pode significar 'já verificado' — checa o badge.
            if await self._domain_badge_state(page, dom) == "verified":
                result.domain_verified = True
                self._set_profile_flag("domain_verified_ok")
                logger.success("Domínio %s já consta verificado.", dom)
                return
            logger.warning("Botão 'Verificar domínio' não encontrado.")
            return

        await self._robust_click(verify_btn)
        logger.info("Clique em 'Verificar domínio' disparado — aguardando confirmação.")

        # Detecção HONESTA e ESCOPADA AO DOMÍNIO ALVO (produção 2026-06,
        # conta Cleber, painel com vários domínios): page-wide pegava o badge
        # "Verified" de OUTRO domínio e '\bverified\b' casava dentro de "Not
        # Verified". _domain_badge_state lê o MENOR container que tem o nome
        # completo do domínio + badge (= a linha do alvo).
        verified = False
        # Sem ``deadline``: ~40 rounds (comportamento antigo, ~1-2 min). Com
        # ``deadline`` (passo 2 em modo estrito): insiste até o relógio,
        # re-clicando 'Verificar domínio' a cada ~10 rounds — a verificação do
        # Meta é assíncrona e costuma confirmar em minutos, não em segundos.
        i = -1
        while True:
            i += 1
            if deadline is None and i >= 40:
                break
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Domínio %s: janela de espera esgotada (%d rounds).", dom, i,
                )
                break
            if await self._domain_badge_state(page, dom) == "verified":
                verified = True
                break
            # Re-clique espaçado: a cada ~10 rounds na janela curta e a cada
            # ~30 (≈1 min) na janela longa — martelar o botão por 30 min seria
            # ~90 cliques e um convite a rate-limit da Meta.
            if i and i % (10 if deadline is None else 30) == 0:
                rebtn = await self._find_verify_domain_button(page)
                if rebtn is not None:
                    await self._robust_click(rebtn)
                    logger.info(
                        "Re-clique em 'Verificar domínio' (round %d) — Meta "
                        "verifica de forma assíncrona.", i,
                    )
                if deadline is not None:
                    restante = max(0, int(deadline - time.monotonic()))
                    logger.info(
                        "Aguardando o Meta verificar %s — %dmin%02ds restantes "
                        "da janela (a esteira só avança com o domínio "
                        "verificado).", dom, restante // 60, restante % 60,
                    )
            await self._organic_wait()
        if verified:
            result.domain_verified = True
            self._set_profile_flag("domain_verified_ok")
            logger.success(
                "Domínio %s verificado pelo Meta (badge 'Verified' na linha do "
                "domínio alvo).", dom,
            )
        else:
            result.domain_verified = False
            logger.warning(
                "Meta ainda mostra '%s' como NÃO verificado (metatag no ar: %s). "
                "Se o probe confirmou, é só a verificação assíncrona do Meta "
                "(pode levar até 72h pelo aviso do painel); se NÃO confirmou, o "
                "site público não está servindo a metatag — confira o deploy.",
                dom,
                "SIM (probe HTTP confirmou)" if getattr(result, "domain_metatag_live", False)
                else "NÃO CONFIRMADA pelo probe",
            )

    async def _select_metatag_verification_method(self, page: Page) -> None:
        """Garante o método 'Adicionar uma metatag' no painel de verificação.

        O painel do domínio tem um seletor 'Selecione uma opção' cujo default
        pode ser DNS/TXT. A metatag <meta facebook-domain-verification> só
        renderiza no método metatag — best-effort: se já estiver nesse método
        (texto da metatag visível), não faz nada.
        """
        try:
            if await (
                page.get_by_text(re.compile(r"facebook-domain-verification", re.I))
                .first.is_visible(timeout=1500)
            ):
                return  # metatag já visível — método correto
        except Exception:  # noqa: BLE001
            pass
        metatag_re = re.compile(r"adicionar uma metatag|add a meta-?tag|meta-?tag", re.I)
        try:
            # Abre o seletor de método (combobox 'Selecione uma opção').
            selector = page.get_by_text(
                re.compile(r"selecione uma op[cç][aã]o|select an option", re.I)
            ).first
            if await selector.is_visible(timeout=5000):
                await self._robust_click(selector)
                await self._organic_wait()
            opt = page.get_by_text(metatag_re).first
            if await opt.is_visible(timeout=5000):
                await self._robust_click(opt)
                logger.info("Método de verificação 'Adicionar uma metatag' selecionado.")
                await self._organic_wait()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Seleção do método metatag não aplicada (%s).", exc)

    @staticmethod
    async def _scrape_domain_meta_tag(page: Page) -> Optional[str]:
        """Captura a meta-tag de verificação do Meta.

        Prioridade do payload retornado:
          1. ``meta[name="facebook-domain-verification"]`` injetado no DOM —
             devolve o atributo ``content`` puro (apenas o hash).
          2. Snippet renderizado em textarea/code/span/div — devolve o
             ``<meta ...>`` completo para o regex do extractor lidar.
        Em ambos os casos o caller passa pelo ``_extract_token_hash`` para
        normalizar; persistir o content puro acelera o caminho feliz.
        """
        try:
            tag = await page.evaluate(
                """() => {
                    const m = document.querySelector(
                        'meta[name="facebook-domain-verification"]'
                    );
                    if (m) {
                        const c = m.getAttribute('content');
                        if (c) return c;
                        return m.outerHTML;
                    }
                    // O assistente do Meta normalmente exibe o snippet
                    // dentro de um textarea/code/span para o operador copiar.
                    const sel = 'textarea, code, span, div';
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.innerText || el.value || '';
                        if (t.includes('facebook-domain-verification')) {
                            const m2 = t.match(
                                /<meta[^>]+facebook-domain-verification[^>]+>/i
                            );
                            if (m2) return m2[0];
                        }
                    }
                    return null;
                }"""
            )
            return tag
        except Exception as exc:  # noqa: BLE001
            # H3 — antes silenciava 100%; agora deixa rastro em DEBUG para que
            # o operador possa investigar quando a meta-tag de domínio "some".
            logger.debug(
                "extract_meta_tag: falhou silenciosamente err=%s: %s",
                type(exc).__name__, exc,
            )
            return None

    # ------------------------------------------------------------------
    # SMS verification
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Passo 4 — criar conta do WhatsApp Business (gera waba_id + token)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Passo 4 — Conta de anúncios (substituiu a criação da WABA em 2026-09)
    # ------------------------------------------------------------------
    async def _create_ad_account(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Cria a conta de anúncios do portfólio: nome da BM + moeda BRL.

        Fluxo (Configurações do negócio → Contas → Contas de anúncios):
          Adicionar → 'Criar uma nova conta de anúncios' → diálogo:
            • Nome da conta de anúncios = MESMO nome do portfólio (BM)
            • Fuso horário              = AD_ACCOUNT_TIMEZONE (America/Sao_Paulo)
            • Moeda                     = AD_ACCOUNT_CURRENCY (BRL)
            → Avançar
            • 'Para quem você está criando esta conta?' → 'Minha empresa'
            → Criar
            • Tela de pessoas/permissões (opcional) → fechar

        Moeda e fuso são IRREVERSÍVEIS na Meta (só criando outra conta): o
        passo ABORTA se não conseguir cravar BRL — o ``_run_business_step``
        re-tenta com a tela fresca, o que é melhor que entregar conta em USD.
        """
        ad_name = self._resolve_portfolio_name()
        result.ad_account_name = ad_name

        # Idempotência: conta de anúncios já criada em run anterior.
        if self._profile_flag("ad_account_ok"):
            logger.info(
                "Conta de anúncios já criada em run anterior (flag "
                "ad_account_ok) — etapa pulada."
            )
            result.ad_account_created = True
            return

        url = self._with_business_id(BUSINESS_AD_ACCOUNTS_URL, result.business_id)
        await page.goto(url, wait_until="domcontentloaded")
        await self._organic_wait()
        if not await self._ensure_business_session(page, target_url=url):
            raise RuntimeError(
                "ad_account: sessão do Business não estabelecida — sem página "
                "de contas de anúncios para operar."
            )

        # Hidratação do SPA (shimmer): espera o empty-state ou o heading real.
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            pass
        try:
            await self._first_visible_scan(
                page,
                [
                    page.get_by_text(
                        re.compile(
                            r"nenhuma conta de an[úu]ncios|no ad accounts", re.I
                        )
                    ),
                    page.get_by_role(
                        "heading",
                        name=re.compile(r"contas de an[úu]ncios|ad accounts", re.I),
                    ),
                ],
                rounds=10,
                round_wait_s=0.5,
            )
        except LoginError:
            logger.warning(
                "Página de contas de anúncios não renderizou conteúdo em ~20s "
                "— seguindo para o 'Adicionar' mesmo assim."
            )
        await self._organic_wait()

        # Idempotência REAL: a flag de perfil não existe em conta reaproveitada
        # ou em run que morreu depois de criar. Se o portfólio JÁ tem conta de
        # anúncios, captura o id e sai — criar outra só polui a BM.
        existing = await self._ad_existing_account_id(page, result)
        if existing:
            result.ad_account_id = existing
            result.ad_account_created = True
            self._set_profile_flag("ad_account_ok")
            logger.info(
                "Portfólio já possui conta de anúncios (act_%s) — criação "
                "pulada.", existing,
            )
            return

        # Sniffer da conta de anúncios ARMADO só aqui (ver _ad_capture_armed).
        self._ad_capture_armed = True
        try:
            estado = await self._ad_open_create_dialog(page)
            if estado == "limite":
                # A Meta só permite UMA conta por portfólio novo: ver o aviso
                # de limite é a PROVA de que a conta já existe. Antes isto
                # virava "campo Nome não encontrado" e o passo re-tentava,
                # batendo no mesmo muro 3x (produção 2026-09, conta Yasmin).
                logger.info(
                    "ad_account: a Meta avisa que o portfólio atingiu o limite "
                    "de contas de anúncios — a conta JÁ EXISTE, seguindo."
                )
            elif estado != "ok":
                await self._dump_debug_html(page, "ad_account_add_failed")
                raise RuntimeError(
                    "ad_account: não consegui abrir o diálogo de criação "
                    "('Adicionar' → 'Criar uma nova conta de anúncios') — DOM "
                    "salvo em .state/."
                )
            else:
                # Diálogo de 3 etapas (produção 2026-09, conta Barbara):
                # Detalhes → Uso → Confirmar. A ÚLTIMA exige marcar os Termos
                # Comerciais antes de 'Criar conta de anúncios' habilitar.
                await self._ad_fill_details_step(page, ad_name)
                await self._ad_owner_step(page)
                await self._ad_confirm_step(page)
                await self._ad_dismiss_people_step(page)

                # O id chega pelo sniffer (mutation da criação); dá algumas
                # janelas de espera antes de cair no fallback do DOM.
                for _ in range(10):
                    if result.ad_account_id:
                        break
                    await self._organic_wait()
                if not result.ad_account_id:
                    result.ad_account_id = await self._ad_scan_account_id(
                        page, result
                    )
        except AdAccountLimitReached as exc:
            # O muro da cota pode aparecer no meio do diálogo (campo de nome
            # ausente porque a Meta já bloqueou). Também é sucesso.
            logger.info("ad_account: %s Seguindo para a confirmação.", exc)
        finally:
            self._ad_capture_armed = False

        await self._ad_finalizar_como_criada(page, result, ad_name)

    async def _ad_finalizar_como_criada(
        self, page: Page, result: AutomationResult, ad_name: str
    ) -> None:
        """Confirma na LISTA que a conta existe e fecha o passo.

        O sucesso é a CONTA EXISTIR, não o ``act_<id>`` ter sido capturado. O
        id é telemetria: o sniffer perde com frequência (a mutation nem sempre
        volta numa resposta legível) e exigi-lo fazia o passo levantar erro
        depois de criar a conta com sucesso — as duas re-tentativas do
        ``_run_business_step`` iam criar OUTRA conta e batiam no limite da Meta
        (produção 2026-09, conta Yasmin). Só o empty-state ainda visível é
        prova de que nada foi criado; aí sim vale re-tentar.
        """
        # Guarda: id igual ao login da conta FB / business_id NÃO é conta de
        # anúncios (ver _ad_excluded_ids).
        if result.ad_account_id and result.ad_account_id in (
            self._ad_excluded_ids()
            | ({result.business_id} if result.business_id else set())
        ):
            logger.warning(
                "ad_account: id capturado (%s) é o login da conta FB / o "
                "business_id — DESCARTADO (não é conta de anúncios).",
                result.ad_account_id,
            )
            result.ad_account_id = None

        existe, id_da_lista = await self._ad_confirm_na_lista(page, result)
        if not existe:
            await self._dump_debug_html(page, "ad_account_nao_aparece_na_lista")
            raise RuntimeError(
                "ad_account: o fluxo terminou mas a lista de ativos continua "
                "no empty-state ('Nenhuma conta de anúncios') — a conta NÃO "
                "foi criada. DOM salvo em .state/."
            )
        if id_da_lista and not result.ad_account_id:
            result.ad_account_id = id_da_lista

        result.ad_account_created = True
        self._set_profile_flag("ad_account_ok")
        if result.ad_account_id:
            logger.success(
                "Conta de anúncios %r pronta — act_%s (moeda %s, fuso %s).",
                ad_name, result.ad_account_id, AD_ACCOUNT_CURRENCY,
                AD_ACCOUNT_TIMEZONE,
            )
        else:
            logger.success(
                "Conta de anúncios %r pronta (moeda %s, fuso %s). O id act_… "
                "não apareceu no sniffer nem na lista — só telemetria, o passo "
                "está concluído e a esteira segue para a verificação.",
                ad_name, AD_ACCOUNT_CURRENCY, AD_ACCOUNT_TIMEZONE,
            )

    async def _ad_confirm_na_lista(
        self, page: Page, result: AutomationResult
    ) -> "tuple[bool, Optional[str]]":
        """Recarrega a lista de ativos e responde ``(existe, id|None)``.

        O empty-state é o sinal negativo confiável (é o mesmo que o passo usa
        ANTES de criar e que funcionou em produção). Sua ausência = existe
        conta. O ``act_<id>`` vem por cima, quando algum link o expõe.
        """
        url = self._with_business_id(BUSINESS_AD_ACCOUNTS_URL, result.business_id)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self._organic_wait()
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("ad_account: recarga da lista falhou (%s).", exc)

        for _ in range(6):
            try:
                if await page.get_by_text(_AD_EMPTY_RE).first.is_visible(
                    timeout=1500
                ):
                    return (False, None)
            except Exception:  # noqa: BLE001
                pass
            act = (
                await self._ad_act_de_ancoras(page, result)
                or await self._ad_scan_account_id(page, result)
            )
            if act:
                return (True, act)
            await self._organic_wait()
        # Sem empty-state e sem act_: a conta está lá, só não expôs o id.
        return (True, None)

    def _ad_excluded_ids(self) -> set[str]:
        """Ids que NUNCA são uma conta de anúncios: o login/uid da conta FB.

        O login das contas do pool é o próprio uid numérico do Facebook, e ele
        aparece em toda página do Business Manager (``account_id``, menus,
        avatares). Sem esta lista, o passo 4 lia o uid da conta logada como se
        fosse a conta de anúncios do portfólio (produção 2026-09).
        """
        ids: set[str] = set()
        cred = getattr(self, "credentials", None)
        login = (getattr(cred, "email_or_phone", "") or "").strip()
        digits = "".join(ch for ch in login if ch.isdigit())
        if len(digits) >= 8:
            ids.add(digits)
        res = getattr(self, "last_result", None)
        biz = getattr(res, "business_id", None)
        if biz:
            ids.add(str(biz))
        return ids

    async def _ad_existing_account_id(
        self, page: Page, result: AutomationResult
    ) -> Optional[str]:
        """Detecta uma conta de anúncios JÁ EXISTENTE no portfólio.

        ESTRITO de propósito: um falso-positivo aqui faz o passo PULAR a
        criação e a run terminar sem conta nenhuma (produção 2026-09). Só dois
        sinais valem: (1) o empty-state visível manda criar, ponto final; e
        (2) um link de ativo ``…act_<id>…`` na lista. NÃO varre o HTML inteiro
        — a página do Business Manager está cheia de ids longos que não são
        contas de anúncios.
        """
        try:
            if await page.get_by_text(_AD_EMPTY_RE).first.is_visible(timeout=2500):
                logger.info(
                    "ad_account: portfólio SEM conta de anúncios (empty-state) "
                    "— seguindo para a criação."
                )
                return None
        except Exception:  # noqa: BLE001
            pass

        return await self._ad_act_de_ancoras(page, result)

    async def _ad_act_de_ancoras(
        self, page: Page, result: AutomationResult
    ) -> Optional[str]:
        """Procura ``act_<id>`` nos LINKS de ativo da lista (sinal estrutural)."""
        excl = self._ad_excluded_ids() | (
            {result.business_id} if result.business_id else set()
        )
        try:
            anchors = page.locator('a[href*="act_"]')
            n = await anchors.count()
            for i in range(min(n, 20)):
                href = await anchors.nth(i).get_attribute("href", timeout=800) or ""
                cand = extract_ad_account_id(href, exclude=excl)
                if cand:
                    return cand
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _ad_scan_account_id(
        self, page: Page, result: AutomationResult
    ) -> Optional[str]:
        """Fallback PÓS-criação: procura ``act_<id>`` na URL e no HTML.

        Só roda DEPOIS de a conta ter sido criada nesta run (o sniffer é a
        fonte primária), então varrer o HTML aqui é aceitável — mas as
        exclusões continuam valendo (login da conta FB e business_id).
        """
        excl = self._ad_excluded_ids() | ({result.business_id} if result.business_id else set())
        found = extract_ad_account_id(page.url or "", exclude=excl)
        if found:
            return found
        try:
            html = await asyncio.wait_for(page.content(), timeout=5.0)
        except Exception:  # noqa: BLE001
            return None
        return extract_ad_account_id(html, exclude=excl)

    async def _ad_limite_atingido(self, page: Page) -> bool:
        """A Meta avisa que o portfólio já bateu o limite de contas de anúncios?

        Portfólio novo tem cota de UMA conta; o aviso aparece sobre o menu
        'Adicionar' com a opção 'Criar uma nova conta de anúncios' esmaecida.
        """
        try:
            return await page.get_by_text(_AD_LIMIT_RE).first.is_visible(
                timeout=2000
            )
        except Exception:  # noqa: BLE001
            return False

    async def _ad_open_create_dialog(self, page: Page) -> str:
        """Clica 'Adicionar' → 'Criar uma nova conta de anúncios'.

        Mesma anatomia do menu da WABA: o item do dropdown só existe DEPOIS do
        clique em 'Adicionar'. Devolve ``"ok"`` (diálogo aberto), ``"limite"``
        (a Meta avisou que o portfólio já atingiu a cota — a conta JÁ EXISTE) ou
        ``"falhou"``.
        """
        create_re = re.compile(
            r"criar uma nova conta de an[úu]ncios|crie uma nova conta de "
            r"an[úu]ncios|create a new ad account",
            re.I,
        )
        dialog_re = re.compile(
            r"criar (uma )?conta de an[úu]ncios|create ad account|"
            r"nova conta de an[úu]ncios",
            re.I,
        )
        for attempt in range(1, 3):
            try:
                add_btn = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^\s*\+?\s*(adicionar|add)\s*$", re.I),
                        ),
                        page.get_by_role(
                            "button", name=re.compile(r"adicionar|add", re.I)
                        ),
                    ],
                    rounds=8,
                )
            except LoginError:
                logger.warning(
                    "ad_account: botão 'Adicionar' não encontrado "
                    "(tentativa %d/2).", attempt,
                )
                await self._organic_wait()
                continue
            await self._robust_click(add_btn)
            await self._organic_wait()
            # Cota estourada: a opção de criar vem esmaecida e a Meta explica o
            # porquê num tooltip. Sair AGORA evita 3 tentativas contra o muro.
            if await self._ad_limite_atingido(page):
                return "limite"
            try:
                create_opt = await self._first_visible_scan(
                    page, [page.get_by_text(create_re)], rounds=6
                )
                await self._robust_click(create_opt)
                logger.info("ad_account: 'Criar uma nova conta de anúncios' clicado.")
                await self._organic_wait()
            except LoginError:
                logger.warning(
                    "ad_account: opção 'Criar uma nova conta de anúncios' não "
                    "apareceu após 'Adicionar' (tentativa %d/2).", attempt,
                )
                continue
            try:
                ok = await self._first_visible(
                    page,
                    [
                        page.get_by_role("heading", name=dialog_re),
                        page.get_by_text(dialog_re),
                        page.get_by_label(
                            re.compile(
                                r"nome da conta de an[úu]ncios|ad account name",
                                re.I,
                            )
                        ),
                    ],
                    timeout_ms=4000,
                )
                if ok:
                    return "ok"
            except LoginError:
                pass
            # O diálogo não abriu: se for a cota, o tooltip está na tela.
            if await self._ad_limite_atingido(page):
                return "limite"
        return "falhou"

    async def _ad_fill_details_step(self, page: Page, ad_name: str) -> None:
        """Preenche nome + fuso + moeda e avança.

        Levanta ``RuntimeError`` se moeda ou fuso não puderem ser cravados —
        os dois são imutáveis depois de criada a conta, então re-tentar o
        passo é sempre melhor que seguir com o palpite da Meta.
        """
        dialog = page.locator('[role="dialog"]')

        # --- Nome: SEMPRE o nome do portfólio (pedido do operador) --------
        try:
            name_input = await self._first_visible(
                page,
                [
                    dialog.get_by_label(
                        re.compile(
                            r"nome da conta de an[úu]ncios|ad account name|"
                            r"nome da conta",
                            re.I,
                        )
                    ),
                    dialog.locator('input[type="text"]').first,
                ],
                timeout_ms=4000,
            )
        except LoginError as exc:
            if await self._ad_limite_atingido(page):
                raise AdAccountLimitReached(
                    "ad_account: campo de nome ausente porque a Meta bloqueou a "
                    "criação — o portfólio já atingiu a cota de contas de "
                    "anúncios (a conta já existe)."
                ) from exc
            await self._dump_debug_html(page, "ad_account_name_field_missing")
            raise RuntimeError(
                "ad_account: campo 'Nome da conta de anúncios' não encontrado "
                "— DOM salvo em .state/."
            ) from exc
        await self._human_type(name_input, ad_name)
        logger.info("ad_account: nome preenchido (%s).", ad_name)

        # --- Fuso horário -------------------------------------------------
        # Rótulo varia por idioma: "(GMT-03:00) America/Sao_Paulo",
        # "(GMT-03:00) América/São Paulo", "(GMT-03:00) Brasília". O filtro
        # digitado usa 'Paulo' (sem acento, presente em todas as variantes de
        # São Paulo) e a última tentativa varre a lista SEM digitar.
        tz_ok = await self._ad_select_dropdown(
            page,
            label_re=re.compile(r"fuso hor[áa]rio|time ?zone", re.I),
            option_re=re.compile(r"s[ãa]o[_ ]?paulo|bras[íi]lia", re.I),
            filter_text="Paulo",
            field="fuso horário",
        )
        if not tz_ok:
            await self._dump_debug_html(page, "ad_account_timezone_failed")
            raise RuntimeError(
                f"ad_account: não consegui selecionar o fuso {AD_ACCOUNT_TIMEZONE} "
                "— abortando o passo (fuso é imutável após a criação)."
            )

        # --- Moeda = BRL (pedido do operador) -----------------------------
        # O CÓDIGO ('BRL') é estável entre idiomas — "BRL - Real brasileiro"
        # (pt-BR) e "BRL - Brazilian Real" (en) casam o mesmo prefixo.
        cur = re.escape(AD_ACCOUNT_CURRENCY)
        cur_ok = await self._ad_select_dropdown(
            page,
            label_re=re.compile(r"moeda|currency", re.I),
            option_re=re.compile(rf"\b{cur}\b", re.I),
            filter_text=AD_ACCOUNT_CURRENCY,
            field="moeda",
        )
        if not cur_ok:
            await self._dump_debug_html(page, "ad_account_currency_failed")
            raise RuntimeError(
                f"ad_account: não consegui selecionar a moeda {AD_ACCOUNT_CURRENCY} "
                "— abortando o passo (moeda é imutável após a criação; melhor "
                "re-tentar que criar a conta em USD)."
            )

        if not await self._ad_click_forward(page, step_name="detalhes"):
            await self._dump_debug_html(page, "ad_account_next_failed")
            raise RuntimeError(
                "ad_account: botão 'Avançar'/'Criar' da etapa de detalhes não "
                "habilitou — DOM salvo em .state/."
            )

    async def _ad_select_dropdown(
        self,
        page: Page,
        *,
        label_re: "re.Pattern[str]",
        option_re: "re.Pattern[str]",
        filter_text: str,
        field: str,
    ) -> bool:
        """Seleciona uma opção num dropdown do diálogo (nativo ou ARIA).

        Cobre as duas anatomias que a Meta alterna: ``<select>`` nativo
        (resolve por ``<option>``) e combobox ARIA com listbox LAZY (só existe
        depois de abrir). Verifica o valor que ficou no controle — a Meta
        pré-seleciona palpites (USD/fuso do IP) e clicar não garante troca.
        """
        dialog = page.locator('[role="dialog"]')
        try:
            combo = await self._first_visible(
                page,
                [
                    dialog.get_by_label(label_re),
                    dialog.get_by_role("combobox", name=label_re),
                    dialog.locator("select").filter(has=page.locator("option")).first,
                    dialog.locator('[role="button"]').filter(has_text=label_re),
                ],
                timeout_ms=3000,
            )
        except LoginError:
            logger.warning("ad_account: dropdown de %s não encontrado.", field)
            return False

        # --- Variante <select> NATIVO ------------------------------------
        try:
            tag = ((await combo.evaluate("el => el.tagName")) or "").lower()
        except Exception:  # noqa: BLE001
            tag = ""
        if tag == "select":
            opts = combo.locator("option")
            for i in range(await opts.count()):
                text = (await opts.nth(i).inner_text()).strip()
                value = await opts.nth(i).get_attribute("value") or ""
                if option_re.search(text) or option_re.search(value):
                    await combo.select_option(value=value)
                    logger.info(
                        "ad_account: %s = %r (<select> nativo).", field, text,
                    )
                    return True
            logger.warning(
                "ad_account: <select> nativo de %s sem opção casando %s.",
                field, option_re.pattern,
            )
            return False

        # --- Combobox ARIA (listbox lazy) --------------------------------
        for attempt in range(3):
            await self._robust_click(combo)
            await self._organic_wait()
            # Nas 2 primeiras tentativas digita p/ filtrar; na última varre a
            # lista inteira (há listas do Meta que NÃO filtram por digitação).
            if attempt < 2 and filter_text:
                try:
                    await page.keyboard.type(filter_text, delay=50)
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
            try:
                opt = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role("option", name=option_re),
                        page.get_by_role("menuitem", name=option_re),
                        page.get_by_text(option_re),
                    ],
                    scan=30,
                    rounds=6,
                )
                await self._robust_click(opt)
                await self._organic_wait()
            except LoginError:
                logger.debug(
                    "ad_account: opção de %s não apareceu (tentativa %d/3) — "
                    "reabrindo o combobox.", field, attempt + 1,
                )
                try:
                    await page.keyboard.press("Escape")
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
                continue
            verdict = await self._ad_combo_matches(combo, option_re)
            if verdict is False:
                logger.warning(
                    "ad_account: cliquei a opção de %s mas o controle NÃO "
                    "ficou com ela (tentativa %d/3) — re-selecionando.",
                    field, attempt + 1,
                )
                try:
                    await page.keyboard.press("Escape")
                    await self._organic_wait()
                except Exception:  # noqa: BLE001
                    pass
                continue
            logger.info("ad_account: %s selecionada e confirmada.", field)
            return True
        return False

    @staticmethod
    async def _ad_combo_matches(combo, option_re: "re.Pattern[str]") -> Optional[bool]:
        """True/False se o valor exibido no combo casa ``option_re``.

        ``None`` quando não deu pra ler o valor (não bloqueia — alguns
        controles do Meta escondem o rótulo selecionado num nó irmão).
        """
        try:
            tag = ((await combo.evaluate("el => el.tagName")) or "").lower()
        except Exception:  # noqa: BLE001
            tag = ""
        try:
            if tag == "select":
                text = await combo.evaluate(
                    "el => (el.options[el.selectedIndex] || {}).text || ''"
                )
            else:
                text = await combo.evaluate(
                    "el => (el.value || el.innerText || el.textContent || '').trim()"
                )
        except Exception:  # noqa: BLE001
            return None
        if not text or not str(text).strip():
            return None
        return bool(option_re.search(str(text)))

    async def _ad_click_forward(
        self,
        page: Page,
        *,
        step_name: str,
        name_re: "re.Pattern[str] | None" = None,
        wait_enabled_s: float = 30.0,
    ) -> bool:
        """Clica o botão de avanço do diálogo ('Avançar'/'Continuar'/'Criar').

        Espera o botão HABILITAR: como no fluxo da WABA, o Meta usa
        ``<div role="button" aria-disabled="true">``, para o qual
        ``is_enabled()`` mente — a checagem lê ``aria-disabled``. ``name_re``
        permite mirar um rótulo específico (ex.: 'Criar conta de anúncios' na
        etapa Confirmar, que NÃO casa o padrão curto de avanço).
        """
        dialog = page.locator('[role="dialog"]')
        if name_re is None:
            name_re = re.compile(
                r"^\s*(avan[çc]ar|continuar|criar|pr[óo]xim[ao]|next|continue|"
                r"create|submit)\s*$",
                re.I,
            )
        deadline = time.monotonic() + max(wait_enabled_s, 0.0)
        while True:
            try:
                btn = await self._first_visible_scan(
                    page,
                    [
                        dialog.get_by_role("button", name=name_re),
                        page.get_by_role("button", name=name_re),
                        dialog.locator('[role="button"]').filter(has_text=name_re),
                    ],
                    rounds=2,
                    round_wait_s=0.5,
                )
            except LoginError:
                btn = None
            if btn is not None:
                try:
                    disabled = (await btn.get_attribute("aria-disabled")) == "true"
                except Exception:  # noqa: BLE001
                    disabled = False
                if not disabled:
                    await self._robust_click(btn)
                    logger.info(
                        "ad_account: avanço clicado (etapa %s).", step_name,
                    )
                    await self._organic_wait()
                    return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(1.5)

    async def _ad_owner_step(self, page: Page) -> None:
        """Etapa 'Uso' → 'Selecione quem usará esta conta' → 'Minha empresa'.

        Diálogo de 3 etapas (Detalhes → Uso → Confirmar). Após 'Avançar' na
        Detalhes, a etapa 'Uso' leva alguns segundos para renderizar. O
        detector antigo escaneava por ~2s e desistia assumindo 'variante que
        cria direto' — quando a etapa 'Uso' aparecia logo depois, o passo
        seguinte (_ad_confirm_step) procurava os Termos numa tela que ainda
        era a 'Uso' e falhava 3x (produção 2026-09, conta Bernadet: o DOM
        salvo mostrava o stepper Detalhes=completo / Uso=pendente).

        Agora esperamos a etapa 'Uso' aparecer DE FATO, e só concluímos que
        ela foi pulada se a etapa 'Confirmar' (Termos / 'Criar conta de
        anúncios') já estiver na tela. Retorna assim que resolve — não
        atrasa as variantes em que a tela aparece rápido.
        """
        own_re = re.compile(
            r"minha empresa|meu neg[óo]cio|my business|minha pr[óo]pria empresa",
            re.I,
        )
        # Marcadores da etapa 'Confirmar' — se JÁ estiverem visíveis, a etapa
        # 'Uso' realmente foi pulada por esta variante e não há o que fazer.
        confirm_re = re.compile(
            r"concordo com os termos|termos comerciais da meta|"
            r"criar conta de an[úu]ncios|i agree to the meta|create ad account",
            re.I,
        )
        dialog = page.locator('[role="dialog"]')
        deadline = time.monotonic() + 25.0
        while True:
            # 1) A opção 'Minha empresa' da etapa 'Uso' está na tela?
            try:
                opt = await self._first_visible_scan(
                    page,
                    [
                        dialog.get_by_role("radio", name=own_re),
                        dialog.get_by_text(own_re),
                        page.get_by_role("radio", name=own_re),
                        page.get_by_text(own_re),
                    ],
                    rounds=1,
                    round_wait_s=0.3,
                )
            except LoginError:
                opt = None
            if opt is not None:
                await self._robust_click(opt)
                await self._organic_wait()
                logger.info(
                    "ad_account: etapa 'Uso' → 'Minha empresa' selecionada."
                )
                await self._ad_click_forward(page, step_name="uso")
                return
            # 2) A etapa 'Confirmar' já está visível? Então a 'Uso' foi pulada.
            try:
                await self._first_visible_scan(
                    page,
                    [dialog.get_by_text(confirm_re), page.get_by_text(confirm_re)],
                    rounds=1,
                    round_wait_s=0.3,
                )
                logger.info(
                    "ad_account: etapa 'Uso' pulada nesta variante (Confirmar "
                    "já visível) — seguindo."
                )
                return
            except LoginError:
                pass
            if time.monotonic() >= deadline:
                logger.info(
                    "ad_account: etapa 'Uso' não apareceu em ~25s — seguindo "
                    "(variante que cria direto)."
                )
                return
            await asyncio.sleep(1.0)

    async def _ad_confirm_step(self, page: Page) -> None:
        """Etapa 'Confirmar': marca os Termos Comerciais e CRIA a conta.

        Observado em produção 2026-09 (conta Barbara): o diálogo tem TRÊS
        etapas — Detalhes → Uso → Confirmar. Na última, o botão 'Criar conta
        de anúncios' fica DESABILITADO até a caixa "Concordo com os Termos
        Comerciais da Meta e as Políticas de Publicidade" ser marcada. Sem
        marcar, o fluxo parava aqui com tudo preenchido e NADA criado.
        """
        confirm_re = re.compile(
            r"confirme a conta de an[úu]ncios|detalhes da conta de an[úu]ncios|"
            r"confirm .*ad account",
            re.I,
        )
        try:
            await self._first_visible_scan(
                page,
                [
                    page.get_by_role("heading", name=confirm_re),
                    page.get_by_text(confirm_re),
                ],
                rounds=6,
                round_wait_s=0.5,
            )
        except LoginError:
            logger.debug(
                "ad_account: heading da etapa 'Confirmar' não visto — seguindo "
                "para os termos mesmo assim."
            )

        if not await self._ad_check_terms_box(page):
            await self._dump_debug_html(page, "ad_account_terms_unchecked")
            raise RuntimeError(
                "ad_account: não consegui marcar a caixa dos Termos Comerciais "
                "na etapa 'Confirmar' — sem ela o botão 'Criar conta de "
                "anúncios' nunca habilita. DOM salvo em .state/."
            )

        create_re = re.compile(
            r"criar conta de an[úu]ncios|create ad account", re.I
        )
        if not await self._ad_click_forward(
            page, step_name="confirmar", name_re=create_re, wait_enabled_s=45.0
        ):
            await self._dump_debug_html(page, "ad_account_create_disabled")
            raise RuntimeError(
                "ad_account: botão 'Criar conta de anúncios' não habilitou na "
                "etapa 'Confirmar' (termos marcados?) — DOM salvo em .state/."
            )

    async def _ad_check_terms_box(self, page: Page) -> bool:
        """Marca a caixa "Concordo com os Termos Comerciais da Meta".

        Duas anatomias possíveis: ``<input type="checkbox">`` real e o
        ``<div role="checkbox" aria-checked>`` do design system da Meta. Em
        ambas, clicar no RÓTULO também alterna — é o fallback quando o clique
        no controle não pega (overlays do FB). Idempotente: se a caixa já vier
        marcada, não desmarca.
        """
        terms_re = re.compile(
            r"concordo com os termos|termos comerciais da meta|"
            r"i agree to the meta|meta commercial terms",
            re.I,
        )
        dialog = page.locator('[role="dialog"]')
        for attempt in range(1, 4):
            try:
                box = await self._first_visible_scan(
                    page,
                    [
                        dialog.get_by_role("checkbox"),
                        dialog.locator('input[type="checkbox"]'),
                        dialog.locator('[role="checkbox"]'),
                        page.get_by_role("checkbox"),
                    ],
                    scan=10,
                    rounds=6,
                    round_wait_s=0.5,
                )
            except LoginError:
                box = None

            if box is not None:
                # Já marcada (re-tentativa do passo): não desmarca.
                if await self._ad_checkbox_is_checked(box) is True:
                    logger.info("ad_account: termos já estavam marcados.")
                    return True
                await self._robust_click(box)
                await self._organic_wait()
                verdict = await self._ad_checkbox_is_checked(box)
                if verdict is True:
                    logger.info(
                        "ad_account: caixa dos Termos Comerciais marcada."
                    )
                    return True
            else:
                verdict = None

            # Fallback: clicar no TEXTO do consentimento (o <label> alterna a
            # caixa). Cobre o caso do controle real ficar sob um overlay.
            try:
                label = await self._first_visible_scan(
                    page, [page.get_by_text(terms_re)], rounds=3, round_wait_s=0.5
                )
                await self._robust_click(label)
                await self._organic_wait()
                logger.debug(
                    "ad_account: cliquei no rótulo dos termos (tentativa %d/3).",
                    attempt,
                )
            except LoginError:
                pass

            if box is not None:
                verdict = await self._ad_checkbox_is_checked(box)
                if verdict is True:
                    logger.info(
                        "ad_account: termos marcados via rótulo (tentativa %d).",
                        attempt,
                    )
                    return True
            if verdict is None and box is not None:
                # Não deu pra LER o estado (variante sem aria-checked/checked):
                # não trava o passo — quem decide é o 'Criar conta de anúncios'
                # habilitar ou não logo adiante.
                logger.warning(
                    "ad_account: estado da caixa de termos ilegível — seguindo; "
                    "o botão 'Criar conta de anúncios' dirá se pegou."
                )
                return True
        return False

    @staticmethod
    async def _ad_checkbox_is_checked(box) -> Optional[bool]:
        """True/False/None (ilegível) para o estado de uma caixa de seleção."""
        try:
            aria = await box.get_attribute("aria-checked")
        except Exception:  # noqa: BLE001
            aria = None
        if aria in ("true", "false"):
            return aria == "true"
        try:
            return bool(await box.is_checked())
        except Exception:  # noqa: BLE001
            pass
        try:
            return bool(await box.evaluate("el => !!el.checked"))
        except Exception:  # noqa: BLE001
            return None

    async def _ad_dismiss_people_step(self, page: Page) -> None:
        """Fecha a tela de pessoas/permissões que abre após a criação.

        Não atribuímos ninguém aqui: o admin do portfólio já tem acesso. O
        objetivo é só devolver a página de ativos limpa para o passo 5.
        """
        close_re = re.compile(
            r"^\s*(fechar|concluir|pular|agora n[ãa]o|close|done|skip|"
            r"not now)\s*$",
            re.I,
        )
        try:
            btn = await self._first_visible_scan(
                page,
                [
                    page.get_by_role("button", name=close_re),
                    page.locator('[aria-label="Fechar"], [aria-label="Close"]'),
                ],
                rounds=4,
                round_wait_s=0.5,
            )
        except LoginError:
            return
        try:
            await self._robust_click(btn)
            await self._organic_wait()
            logger.debug("ad_account: diálogo pós-criação fechado.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("ad_account: não consegui fechar o diálogo (%s).", exc)

    # ------------------------------------------------------------------ #
    # BM Invite — convite automático de Admin ao portfólio                #
    # (mesma lógica da extensão Fast Invite BM: token EAAI + Graph API)   #
    # ------------------------------------------------------------------ #

    _BM_EAAI_RE = re.compile(r"EAAI[A-Za-z0-9_-]+")

    _BM_ROLES_ADMIN = [
        "DEFAULT", "MANAGE", "DEVELOPER", "EMPLOYEE",
        "ASSET_MANAGE", "ASSET_VIEW", "PEOPLE_MANAGE", "PEOPLE_VIEW",
        "PARTNERS_VIEW", "PARTNERS_MANAGE", "PROFILE_MANAGE",
    ]
    _BM_ROLES_EMPLOYEE = ["EMPLOYEE"]

    _BM_AUTH_ERROR_CODES = {102, 190, 463, 467, 2500}

    async def _bm_get_eaai_token(self, page: Page) -> str:
        """Extrai token EAAI do bootloader do Facebook."""
        url = (
            "https://business.facebook.com/ajax/bootloader-endpoint/"
            "?modules=AdsCanvasComposerDialog.react&__a=1"
        )
        resp = await page.request.get(url, headers={
            "referer": "https://business.facebook.com/",
        })
        html = await resp.text()
        m = self._BM_EAAI_RE.search(html)
        if not m:
            raise ValueError("Token EAAI não encontrado no bootloader")
        return m.group(0)

    def _bm_is_auth_error(self, raw_error: dict | None) -> bool:
        if not raw_error:
            return False
        code = raw_error.get("code")
        if code and int(code) in self._BM_AUTH_ERROR_CODES:
            return True
        if raw_error.get("type") == "OAuthException":
            return True
        msg = str(raw_error.get("message", "")).lower()
        return "oauth" in msg or ("session" in msg and "expired" in msg)

    async def _bm_invite_primary(
        self, page: Page, biz: str, email: str, eaai: str, roles: list[str],
    ) -> dict:
        """API 1: POST a graph.facebook.com/v19.0/{bmId}/business_users."""
        import json as _json  # noqa: PLC0415
        url = (
            f"https://graph.facebook.com/v19.0/{biz}/business_users"
            f"?access_token={eaai}"
        )
        resp = await page.request.post(url, form={
            "brandId": biz,
            "email": email,
            "invite_origin": "BM_INVITE_USER_FLOW",
            "method": "post",
            "roles": _json.dumps(roles),
            "suppress_http_code": "1",
        }, headers={
            "origin": "https://business.facebook.com",
            "referer": "https://business.facebook.com/",
        })
        text = await resp.text()
        try:
            return _json.loads(text)
        except Exception:
            return {"error": {"message": f"Resposta não-JSON: {text[:200]}"}}

    async def _bm_invite_fallback(
        self, page: Page, biz: str, email: str, eaai: str, roles: list[str],
    ) -> dict:
        """API 2: batch POST a adsmanager-graph.facebook.com (fallback)."""
        import json as _json  # noqa: PLC0415
        inner_body = (
            f"brandId={biz}"
            f"&email={email}"
            f"&roles={_json.dumps(roles)}"
        )
        batch = _json.dumps([{
            "method": "POST",
            "relative_url": f"/v3.0/{biz}/business_users",
            "body": inner_body,
        }])
        url = (
            f"https://adsmanager-graph.facebook.com/v17.0"
            f"?access_token={eaai}&suppress_http_code=1&locale=en_US"
        )
        resp = await page.request.post(url, form={
            "batch": batch,
            "method": "post",
            "suppress_http_code": "1",
        }, headers={
            "origin": "https://business.facebook.com",
            "referer": "https://business.facebook.com/",
        })
        text = await resp.text()
        try:
            data = _json.loads(text)
        except Exception:
            return {"error": {"message": f"Resposta não-JSON: {text[:200]}"}}
        if isinstance(data, list) and data and isinstance(data[0], dict):
            body_str = data[0].get("body", "")
            try:
                return _json.loads(body_str)
            except Exception:
                pass
        return data if isinstance(data, dict) else {"error": {"message": str(data)[:200]}}

    @staticmethod
    def _bm_parse_invite_response(data: dict) -> tuple[bool, str | None]:
        """Analisa a resposta da Graph API e retorna (sucesso, erro_msg)."""
        if not isinstance(data, dict):
            return False, f"Resposta inesperada: {str(data)[:200]}"
        err = data.get("error")
        if err:
            msg = err.get("error_user_msg") or err.get("message") or str(err)
            return False, msg
        ok = bool(
            data.get("id")
            or data.get("uid")
            or data.get("success") is True
            or data.get("status") == "PENDING"
            or data.get("request_status") == "PENDING"
        )
        if ok:
            return True, None
        return False, f"Convite não confirmado: {str(data)[:200]}"

    async def _send_bm_invite(
        self, page: Page, result: "AutomationResult",
    ) -> None:
        """Envia convite de Admin ao portfólio (best-effort, com retries)."""
        if not BM_INVITE_ENABLED:
            logger.info("bm_invite: DESLIGADO (BM_INVITE_ENABLED=false).")
            return

        biz = result.business_id
        if not biz:
            logger.warning("bm_invite: business_id ausente — pulando convite.")
            return

        email = BM_INVITE_EMAIL
        roles = (
            self._BM_ROLES_EMPLOYEE
            if BM_INVITE_ROLE == "employee"
            else self._BM_ROLES_ADMIN
        )
        logger.info(
            "bm_invite: enviando convite (%s) para %s no portfólio %s…",
            BM_INVITE_ROLE, email, biz,
        )

        for attempt in range(1, BM_INVITE_MAX_RETRIES + 1):
            try:
                eaai = await self._bm_get_eaai_token(page)
                logger.info(
                    "bm_invite: token EAAI obtido (prefixo=%s, %d chars).",
                    eaai[:6], len(eaai),
                )

                # --- API 1 ---
                r1 = await self._bm_invite_primary(page, biz, email, eaai, roles)
                ok1, err1 = self._bm_parse_invite_response(r1)
                if ok1:
                    result.bm_invite_sent = True
                    result.bm_invite_email = email
                    logger.info(
                        "bm_invite: convite ENVIADO com sucesso para %s (API1, tentativa %d).",
                        email, attempt,
                    )
                    return

                if self._bm_is_auth_error(r1.get("error")):
                    logger.info("bm_invite: token expirado — renovando…")
                    eaai = await self._bm_get_eaai_token(page)
                    r1 = await self._bm_invite_primary(page, biz, email, eaai, roles)
                    ok1, err1 = self._bm_parse_invite_response(r1)
                    if ok1:
                        result.bm_invite_sent = True
                        result.bm_invite_email = email
                        logger.info(
                            "bm_invite: convite ENVIADO com sucesso para %s "
                            "(API1 retry, tentativa %d).", email, attempt,
                        )
                        return

                logger.warning("bm_invite: API1 falhou — %s. Tentando API2…", err1)

                # --- API 2 (fallback) ---
                r2 = await self._bm_invite_fallback(page, biz, email, eaai, roles)
                ok2, err2 = self._bm_parse_invite_response(r2)
                if ok2:
                    result.bm_invite_sent = True
                    result.bm_invite_email = email
                    logger.info(
                        "bm_invite: convite ENVIADO com sucesso para %s (API2, tentativa %d).",
                        email, attempt,
                    )
                    return

                if self._bm_is_auth_error(r2.get("error") if isinstance(r2, dict) else None):
                    eaai = await self._bm_get_eaai_token(page)
                    r2 = await self._bm_invite_fallback(page, biz, email, eaai, roles)
                    ok2, err2 = self._bm_parse_invite_response(r2)
                    if ok2:
                        result.bm_invite_sent = True
                        result.bm_invite_email = email
                        logger.info(
                            "bm_invite: convite ENVIADO com sucesso para %s "
                            "(API2 retry, tentativa %d).", email, attempt,
                        )
                        return

                raise ValueError(f"API1: {err1} | API2: {err2}")

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "bm_invite: tentativa %d/%d falhou: %s",
                    attempt, BM_INVITE_MAX_RETRIES, exc,
                )
                if attempt < BM_INVITE_MAX_RETRIES:
                    wait = BM_INVITE_RETRY_INTERVAL_S
                    logger.info(
                        "bm_invite: aguardando %ds (~%d min) antes da próxima tentativa…",
                        wait, wait // 60,
                    )
                    for _ in range(wait):
                        if self._assume_requested() or self._close_requested():
                            logger.warning(
                                "bm_invite: interrompido pelo operador durante espera."
                            )
                            return
                        rid = getattr(self, "run_id", None)
                        if rid and run_control.is_pause_requested(rid):
                            logger.info("bm_invite: PAUSADA — aguardando retomada.")
                            await self._await_if_paused(result)
                        await asyncio.sleep(1)

        logger.warning(
            "bm_invite: TODAS as %d tentativas falharam — "
            "continuando o fluxo normal.", BM_INVITE_MAX_RETRIES,
        )

    async def _create_whatsapp_business_account(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Cria a conta do WhatsApp Business no portfólio.

        Fluxo (mapeado em produção 2026-06, conta Vitor):
          Contas → Contas do WhatsApp → Adicionar → 'Crie uma nova conta do
          WhatsApp Business' → diálogo:
            • Detalhes: Nome de exibição (geralmente pré-preenchido) +
              Categoria='Outro' (seleção, não campo livre) + reCAPTCHA →
              Continuar
            • Telefone: 'Usar somente um nome de exibição' → Continuar
        É durante esse fluxo que o waba_id + access_token (EAAB…) trafegam no
        GraphQL — o sniffer de rede os captura (independe do Datacrazy).
        """
        # Idempotência: WABA já criada em run anterior (flag por perfil).
        if self._profile_flag("waba_ok"):
            logger.info(
                "Conta do WhatsApp já criada em run anterior (flag waba_ok) — "
                "etapa pulada."
            )
            return

        # GATE: o passo 4 EXIGE domínio verificado — é o fluxo do operador
        # ("depois de domínio verificado, entrar em contas → contas do
        # WhatsApp"). Criar a WABA com o domínio NÃO verificado gera conta
        # incompleta (produção 2026-06, conta Paulo: bot avançou pro WhatsApp
        # sem o domínio verificado). A verificação do Meta é assíncrona —
        # re-rodar a conta mais tarde RETOMA daqui: o passo 3 detecta o badge
        # 'verified' (ou a flag domain_verified_ok) e libera o WhatsApp.
        domain = (self.business_profile.domain or "").strip()
        if domain and not result.domain_verified:
            result.current_step = "whatsapp_blocked_domain_unverified"
            msg = (
                f"whatsapp_account BLOQUEADO — domínio {domain} ainda NÃO verificado "
                "pelo Meta. O passo 4 exige domínio verificado (a metatag está "
                "no ar; o Meta verifica de forma assíncrona). Re-rode a conta "
                "mais tarde — o passo 3 detecta o badge 'verified' e libera o "
                "WhatsApp."
            )
            logger.error(msg)
            # Levanta exceção terminal p/ PARAR o fluxo aqui e NÃO prosseguir
            # p/ step 5 — antes era apenas 'return', que marcava como status=ok
            # e continuava (bug: criava WA mesmo com domínio não verificado).
            raise DomainNotVerifiedError(msg)

        wa_url = self._with_business_id(BUSINESS_WHATSAPP_URL, result.business_id)
        await page.goto(wa_url, wait_until="domcontentloaded")
        await self._organic_wait()
        if not await self._ensure_business_session(page, target_url=wa_url):
            logger.error(
                "whatsapp_account: sessão do Business não estabelecida — "
                "etapa pulada (sem página de contas do WhatsApp para operar)."
            )
            return

        # Hidratação: o settings SPA mostra shimmer; espera o conteúdo real
        # (empty-state 'Nenhuma conta do WhatsApp' ou o heading) antes de clicar.
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            pass
        try:
            await self._first_visible_scan(
                page,
                [
                    page.get_by_text(
                        re.compile(
                            r"nenhuma conta do whatsapp|no whatsapp accounts", re.I
                        )
                    ),
                    page.get_by_role(
                        "heading",
                        name=re.compile(r"contas do whatsapp|whatsapp accounts", re.I),
                    ),
                ],
                rounds=10,
                round_wait_s=0.5,
            )
        except LoginError:
            logger.warning(
                "Página de contas do WhatsApp não renderizou conteúdo em ~20s — "
                "seguindo para o 'Adicionar' mesmo assim."
            )
        await self._organic_wait()

        # 1) Abrir o menu 'Adicionar' e escolher 'Crie uma nova conta'.
        if not await self._wa_open_create_dialog(page):
            await self._dump_debug_html(page, "whatsapp_add_failed")
            logger.error(
                "whatsapp_account: não consegui abrir o diálogo de criação "
                "('Adicionar' → 'Crie uma nova conta') — DOM salvo em .state/."
            )
            return

        # 2) Etapa 'Detalhes': nome de exibição + categoria 'Outro' + captcha.
        if not await self._wa_fill_details_step(page, result):
            await self._dump_debug_html(page, "whatsapp_details_failed")
            logger.error(
                "whatsapp_account: etapa 'Detalhes' não concluída — DOM salvo."
            )
            return

        # 3) Etapa 'Telefone': usar somente nome de exibição.
        await self._wa_phone_step(page)

        # 4) Espera o sniffer capturar o waba_id (a mutation de criação volta
        #    com o id). O token costuma vir junto/antes no mesmo fluxo.
        for _ in range(10):
            if result.waba_id:
                break
            await self._organic_wait()

        if result.waba_id and result.waba_id != result.business_id:
            self._set_profile_flag("waba_ok")
            logger.success(
                "Conta do WhatsApp Business criada — waba_id=%s capturado pelo "
                "sniffer.", result.waba_id,
            )
        else:
            # waba_id ausente OU == business_id (falso-positivo): NÃO marca
            # waba_ok, senão o próximo run pula o WhatsApp achando que criou.
            if result.waba_id == result.business_id:
                result.waba_id = None
                logger.warning(
                    "whatsapp: sniffer só viu o business_id do portfólio (não "
                    "um WABA real) — conta NÃO criada. waba_ok NÃO marcado."
                )
            await self._dump_debug_html(page, "whatsapp_waba_id_missing")
            logger.warning(
                "Fluxo de criação concluído, mas o sniffer não capturou o "
                "waba_id (GraphQL pode ter usado outra chave/forma) — DOM "
                "salvo em .state/ para calibração."
            )

    async def _wa_open_create_dialog(self, page: Page) -> bool:
        """Clica 'Adicionar' e 'Crie uma nova conta do WhatsApp Business'.

        Retorna True quando o diálogo de criação abre (campo Nome de exibição
        ou heading 'Criar uma conta do WhatsApp Business' visível).
        """
        create_re = re.compile(
            r"crie uma nova conta do whatsapp|create a( new)? whatsapp business",
            re.I,
        )
        dialog_re = re.compile(
            r"criar uma conta do whatsapp|create a whatsapp business account", re.I
        )
        for attempt in range(1, 3):
            try:
                add_btn = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^\s*\+?\s*(adicionar|add)\s*$", re.I),
                        ),
                        page.get_by_role(
                            "button", name=re.compile(r"adicionar|add", re.I)
                        ),
                    ],
                    rounds=8,
                )
            except LoginError:
                logger.warning(
                    "whatsapp: botão 'Adicionar' não encontrado "
                    "(tentativa %d/2).", attempt,
                )
                await self._organic_wait()
                continue
            await self._robust_click(add_btn)
            await self._organic_wait()
            # Menu suspenso: 'Crie uma nova conta do WhatsApp Business'.
            try:
                create_opt = await self._first_visible_scan(
                    page, [page.get_by_text(create_re)], rounds=6
                )
                await self._robust_click(create_opt)
                logger.info("whatsapp: 'Crie uma nova conta' clicado.")
                await self._organic_wait()
            except LoginError:
                logger.warning(
                    "whatsapp: opção 'Crie uma nova conta' não apareceu após "
                    "'Adicionar' (tentativa %d/2).", attempt,
                )
                continue
            # Diálogo aberto?
            try:
                ok = await self._first_visible(
                    page,
                    [
                        page.get_by_role("heading", name=dialog_re),
                        page.get_by_text(dialog_re),
                        page.get_by_label(
                            re.compile(r"nome de exibi[cç][aã]o|display name", re.I)
                        ),
                    ],
                    timeout_ms=4000,
                )
                if ok:
                    return True
            except LoginError:
                pass
        return False

    async def _wa_fill_details_step(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Preenche a etapa 'Detalhes' do diálogo de criação da WABA.

        Nome de exibição (preenche só se vazio), Categoria='Outro' (seleção),
        resolve o reCAPTCHA (corrida manual×2captcha) e clica 'Continuar'.
        """
        dialog = page.locator('[role="dialog"]')

        # --- Nome de exibição: preenche apenas se vier vazio --------------
        display_name = self._wa_display_name()
        try:
            name_input = await self._first_visible(
                page,
                [
                    dialog.get_by_label(
                        re.compile(r"nome de exibi[cç][aã]o|display name", re.I)
                    ),
                    dialog.locator('input[type="text"]').first,
                ],
                timeout_ms=3000,
            )
            current = (await name_input.input_value()) or ""
            if not current.strip() and display_name:
                await self._human_type(name_input, display_name)
                logger.info("whatsapp: nome de exibição preenchido (%s).", display_name)
            else:
                logger.info(
                    "whatsapp: nome de exibição já preenchido (%r) — mantido.",
                    current.strip(),
                )
        except LoginError:
            logger.warning("whatsapp: campo de nome de exibição não encontrado.")

        # --- Categoria = 'Outro' (combobox, não campo livre) -------------
        # REGRA DO OPERADOR: sempre 'Outro', NUNCA 'Restaurante'. Se não der pra
        # garantir, ABORTA o passo (o _run_business_step re-tenta com a tela
        # fresca) em vez de seguir com a categoria sugerida errada pela Meta.
        if not await self._wa_select_category_outro(page):
            raise RuntimeError(
                "whatsapp: não consegui garantir a categoria 'Outro' (nunca "
                "'Restaurante') — abortando o passo para re-tentar em vez de "
                "criar a WABA com categoria errada."
            )

        # --- reCAPTCHA 'Não sou um robô' + Continuar ----------------------
        # Marca o checkbox, tenta o 2captcha (best-effort) e ESPERA o sinal
        # REAL de conclusão: o botão 'Continuar' ficar HABILITADO (humano ou
        # 2captcha resolveu). Se CapSolver falhar na 1ª tentativa, rola a
        # página para o captcha ficar visível e troca o desafio (refresh) a
        # cada 30s, re-tentando o CapSolver a cada imagem nova.
        await self._wa_check_robot_box(page)
        await self._wa_scroll_captcha_visible(page)
        await self._wa_try_2captcha(page, result)

        if not result.captcha_solved:
            _cap_deadline = time.monotonic() + CAPTCHA_MANUAL_TIMEOUT_S
            _refresh_interval = 30.0
            _next_refresh = time.monotonic() + _refresh_interval
            while time.monotonic() < _cap_deadline:
                btn = self._wa_continue_button(page)
                try:
                    if await btn.first.is_visible(timeout=500):
                        if (await btn.first.get_attribute("aria-disabled")) != "true":
                            break
                except Exception:  # noqa: BLE001
                    pass
                if time.monotonic() >= _next_refresh:
                    logger.info("whatsapp: captcha não resolvido — trocando desafio.")
                    await self._refresh_captcha_challenge(page)
                    await self._wa_scroll_captcha_visible(page)
                    await asyncio.sleep(2.0)
                    await self._wa_try_2captcha(page, result)
                    if result.captcha_solved:
                        break
                    _next_refresh = time.monotonic() + _refresh_interval
                await asyncio.sleep(2.0)

        return await self._wa_wait_and_click_continue(
            page, step_name="Detalhes",
            wait_enabled_s=10.0 if result.captcha_solved else CAPTCHA_MANUAL_TIMEOUT_S,
        )

    def _wa_display_name(self) -> str:
        """Nome de exibição do WhatsApp: razão social/fantasia ou admin."""
        bp = self.business_profile
        cn = self.cnpj_data
        for cand in (
            getattr(cn, "nome_fantasia", None),
            getattr(cn, "razao_social", None),
            getattr(bp, "admin_name", None) if bp else None,
        ):
            if cand and str(cand).strip():
                return str(cand).strip()
        return ""

    async def _wa_category_is_outro(self, combo) -> Optional[bool]:
        """Lê a categoria atualmente selecionada no combo.

        Devolve ``True`` se é 'Outro'/'Other', ``False`` se é OUTRA coisa
        (ex.: 'Restaurante' sugerido pela Meta) e ``None`` se não deu pra ler
        (não bloqueia nesse caso). Usado para GARANTIR que nunca seguimos com a
        categoria errada (pedido do operador: sempre 'Outro', nunca
        'Restaurante')."""
        outro_re = re.compile(r"\b(outro|outros|other)\b", re.I)
        try:
            tag = ((await combo.evaluate("el => el.tagName")) or "").lower()
        except Exception:  # noqa: BLE001
            tag = ""
        text: Optional[str] = None
        try:
            if tag == "select":
                text = await combo.evaluate(
                    "el => (el.options[el.selectedIndex] || {}).text || ''"
                )
            else:
                # Combobox ARIA: o rótulo selecionado costuma ser o value do
                # input ou o innerText do controle.
                text = await combo.evaluate(
                    "el => (el.value || el.innerText || el.textContent || '').trim()"
                )
        except Exception:  # noqa: BLE001
            text = None
        if not text or not text.strip():
            return None
        return bool(outro_re.search(text))

    async def _wa_select_category_outro(self, page: Page) -> bool:
        """Abre o combobox 'Categoria' e seleciona 'Outro'.

        As opções são LAZY: o listbox apontado por aria-owns só EXISTE depois de
        abrir (clicar) o combobox — buscar antes não acha nada (achado da
        auditoria DOM 2026-06-17, debug_whatsapp_categoria_outro_missing). Abre,
        digita p/ filtrar e clica a opção EXATA; RE-TENTA (fecha c/ Escape) se o
        listbox não montou na 1ª (render transitório)."""
        dialog = page.locator('[role="dialog"]')
        outro_re = re.compile(r"^\s*outro\b|^\s*other\b", re.I)
        exact_outro = re.compile(r"^\s*(outro|outros|other)\s*$", re.I)
        try:
            combo = await self._first_visible(
                page,
                [
                    dialog.get_by_label(re.compile(r"categoria|category", re.I)),
                    dialog.get_by_role(
                        "combobox", name=re.compile(r"categoria|category", re.I)
                    ),
                    dialog.get_by_role("combobox"),
                    dialog.locator('[role="button"]').filter(
                        has_text=re.compile(r"categoria|selecione|select", re.I)
                    ),
                ],
                timeout_ms=3000,
            )
        except LoginError:
            return False

        # Variante <select> NATIVO (visto em produção 2026-06: dropdown de
        # seta simples que já vem com uma categoria "sugerida" pelo Meta
        # PRÉ-SELECIONADA, ex.: "Restaurante" — sem tratar esse caso o robô
        # seguia direto com a sugestão errada, porque o fluxo de digitar+
        # filtrar abaixo é só pra combobox ARIA/listbox, não pra <select>
        # nativo). Resolve direto pela <option> cujo texto bate Outro/Other;
        # se o elemento não for um <select> de verdade, cai pro combobox
        # ARIA de sempre.
        try:
            tag = (await combo.evaluate("el => el.tagName")) or ""
            if tag.lower() == "select":
                opts = combo.locator("option")
                matched = False
                for i in range(await opts.count()):
                    text = (await opts.nth(i).inner_text()).strip()
                    if outro_re.search(text):
                        value = await opts.nth(i).get_attribute("value")
                        await combo.select_option(value=value)
                        logger.info(
                            "whatsapp: categoria 'Outro' selecionada "
                            "(<select> nativo)."
                        )
                        matched = True
                        break
                if matched:
                    return True
                logger.warning(
                    "whatsapp: <select> nativo de categoria SEM opção "
                    "'Outro'/'Other' entre as <option> — mantendo o fluxo "
                    "de combobox ARIA como fallback."
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "whatsapp: categoria não é <select> nativo (%s) — tentando "
                "combobox ARIA (digitar+filtrar).", exc,
            )

        for attempt in range(3):
            await self._robust_click(combo)
            await self._organic_wait()
            # Digitar 'Outro' filtra a lista em muitos comboboxes do Meta; depois
            # clica a opção EXATA. Preferir role=option com nome exato — o
            # get_by_text amplo pegava a 1ª linha realçada (a conta Paulo ficou
            # com 'Viagem e transporte' por casar texto solto).
            try:
                await page.keyboard.type("Outro", delay=50)
                await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            try:
                opt = await self._first_visible_scan(
                    page,
                    [
                        page.get_by_role("option", name=exact_outro),
                        page.get_by_role("menuitem", name=exact_outro),
                        page.get_by_text(exact_outro),
                        page.get_by_text(outro_re),
                    ],
                    rounds=6,
                )
                await self._robust_click(opt)
                await self._organic_wait()
                # VERIFICA: nunca seguir com 'Restaurante' (sugestão da Meta).
                verdict = await self._wa_category_is_outro(combo)
                if verdict is False:
                    logger.warning(
                        "whatsapp: cliquei 'Outro' mas o combo NÃO ficou 'Outro' "
                        "(tentativa %d/3) — reabrindo e re-selecionando.",
                        attempt + 1,
                    )
                    try:
                        await page.keyboard.press("Escape")
                        await self._organic_wait()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                logger.info("whatsapp: categoria 'Outro' selecionada e confirmada.")
                return True
            except LoginError:
                if attempt < 2:
                    # Listbox não montou — fecha e reabre o combobox.
                    try:
                        await page.keyboard.press("Escape")
                        await self._organic_wait()
                    except Exception:  # noqa: BLE001
                        pass
                    logger.debug(
                        "whatsapp: opção 'Outro' não apareceu — reabrindo o "
                        "combobox (tentativa %d/3).", attempt + 2,
                    )
                    continue
        logger.warning(
            "whatsapp: categoria 'Outro' não encontrada após 3 tentativas — o "
            "combobox pode não ter renderizado as opções (DOM em .state se o "
            "passo dumpar adiante)."
        )
        return False

    async def _wa_check_robot_box(self, page: Page) -> None:
        """Marca o checkbox 'Não sou um robô' (iframe anchor do reCAPTCHA)."""
        try:
            for frame in page.frames:
                if re.search(r"recaptcha.*anchor|api2/anchor", frame.url or "", re.I):
                    box = frame.locator("#recaptcha-anchor, .recaptcha-checkbox")
                    if await box.first.is_visible(timeout=1500):
                        await box.first.click()
                        logger.info("whatsapp: checkbox 'Não sou um robô' marcado.")
                        await self._organic_wait()
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("whatsapp: clique no checkbox reCAPTCHA falhou (%s).", exc)

    async def _wa_scroll_captcha_visible(self, page: Page) -> None:
        """Rola a página/diálogo para que o reCAPTCHA fique visível."""
        try:
            for frame in page.frames:
                url = frame.url or ""
                if "recaptcha" not in url:
                    continue
                if "anchor" in url:
                    anchor = frame.locator("#recaptcha-anchor, .recaptcha-checkbox")
                    if await anchor.count() > 0:
                        await anchor.first.scroll_into_view_if_needed(timeout=2000)
                        return
                if "bframe" in url:
                    challenge = frame.locator(
                        "#rc-imageselect, .rc-imageselect-challenge, "
                        "#rc-audiochallenge"
                    )
                    if await challenge.count() > 0:
                        await challenge.first.scroll_into_view_if_needed(timeout=2000)
                        return
        except Exception:  # noqa: BLE001
            pass
        try:
            dialog = page.locator('[role="dialog"]')
            await dialog.evaluate("el => el.scrollTop = el.scrollHeight")
        except Exception:  # noqa: BLE001
            pass

    async def _wa_try_2captcha(self, page: Page, result: AutomationResult) -> None:
        """Tenta resolver o reCAPTCHA via 2captcha (best-effort, sem bloquear).

        Injeta o token se conseguir; NÃO declara sucesso por heurística de URL
        (o sinal de conclusão é o 'Continuar' habilitar, checado adiante).
        """
        if self.captcha_solver is None:
            return
        try:
            challenge = await self._detect_captcha_iframe(page)
            if not challenge:
                return
            _frame, sitekey, is_enterprise, origin = challenge
            token = await self.captcha_solver.solve_recaptcha_v2(
                sitekey=sitekey, page_url=origin or page.url,
                enterprise=is_enterprise,
            )
            await self._inject_recaptcha_token(page, token)
            result.captcha_solved = True
            logger.success("whatsapp: 2captcha injetou o token do reCAPTCHA.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "whatsapp: 2captcha não resolveu (%s) — aguardando resolução "
                "manual na janela do AdsPower.", exc,
            )

    def _wa_continue_button(self, page: Page):
        """Locator do 'Continuar' do diálogo (sem levantar)."""
        dialog = page.locator('[role="dialog"]')
        continue_re = re.compile(r"^\s*(continuar|continue|avan[cç]ar|next)\s*$", re.I)
        return dialog.get_by_role("button", name=continue_re)

    async def _wa_wait_and_click_continue(
        self, page: Page, *, step_name: str, wait_enabled_s: float = 0.0
    ) -> bool:
        """Espera o 'Continuar' ficar HABILITADO e clica.

        O botão fica desabilitado até o captcha/campos estarem ok — clicar
        cedo não avança. Poll do estado enabled por até ``wait_enabled_s``
        (janela em que 2captcha OU um humano pode resolver). Se nunca
        habilitar, salva o DOM do diálogo ABERTO para calibração.
        """
        btn = self._wa_continue_button(page)
        deadline = time.monotonic() + max(wait_enabled_s, 0.0)
        first = True
        while True:
            try:
                visible = await btn.first.is_visible(timeout=800)
            except Exception:  # noqa: BLE001
                visible = False
            if visible:
                try:
                    # O 'Continuar' do WhatsApp é um <div role="button"
                    # aria-disabled="true"> — is_enabled() (atributo HTML
                    # 'disabled') devolve True p/ esse div mesmo desabilitado, e
                    # o bot clicava um botão morto sem avançar (achado da
                    # auditoria DOM 2026-06-17, debug_whatsapp_continue_disabled_
                    # detalhes). Checa aria-disabled explicitamente — mesmo
                    # padrão do _click_enabled_button (linha ~7704).
                    if (await btn.first.get_attribute("aria-disabled")) == "true":
                        enabled = False
                    else:
                        enabled = await btn.first.is_enabled(timeout=500)
                except Exception:  # noqa: BLE001
                    enabled = False
                if enabled:
                    await self._robust_click(btn.first)
                    logger.info("whatsapp: 'Continuar' clicado (etapa %s).", step_name)
                    await self._organic_wait()
                    return True
            if first:
                logger.info(
                    "whatsapp: aguardando 'Continuar' habilitar (etapa %s, até "
                    "%.0fs — resolva o captcha na janela se necessário).",
                    step_name, wait_enabled_s,
                )
                first = False
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(2.0)
        await self._dump_debug_html(page, f"whatsapp_continue_disabled_{step_name.lower()}")
        logger.warning(
            "whatsapp: 'Continuar' não habilitou na etapa %s (captcha não "
            "resolvido?) — DOM do diálogo salvo em .state/.", step_name,
        )
        return False

    async def _wa_phone_step(self, page: Page) -> None:
        """Etapa 'Telefone': escolhe 'Usar somente um nome de exibição'.

        A tela Telefone só aparece APÓS o round-trip de rede da criação na
        etapa Detalhes — rounds=10 (~10-20s) expirava antes (produção 2026-06,
        conta Daniel: 'Telefone não apareceu' embora a tela exista nos prints).
        Espera ~30 rounds; casa a opção EXATA (radio sem aria-label, como em
        'tem registro') e dumpa o DOM se não achar.
        """
        only_name_exact = re.compile(
            r"^\s*(usar somente um nome de exibi[cç][aã]o|"
            r"use only a display name|display name only)\s*$", re.I,
        )
        only_name_loose = re.compile(
            r"usar somente um nome de exibi[cç][aã]o|use only a display name|"
            r"display name only", re.I,
        )
        try:
            opt = await self._first_visible_scan(
                page,
                [
                    page.get_by_text(only_name_exact),
                    page.get_by_text(only_name_loose),
                ],
                rounds=30,
            )
        except LoginError:
            await self._dump_debug_html(page, "whatsapp_phone_step_missing")
            logger.warning(
                "whatsapp: etapa 'Telefone' não apareceu em ~30 rounds — DOM "
                "salvo em .state/. A conta pode ter sido criada sem o passo "
                "Telefone, ou o seletor precisa calibrar."
            )
            return
        await self._robust_click(opt)
        logger.info("whatsapp: 'Usar somente um nome de exibição' selecionado.")
        await self._organic_wait()
        await self._wa_wait_and_click_continue(
            page, step_name="Telefone", wait_enabled_s=20.0
        )
        # Pode haver uma tela de confirmação final ('Concluir'/'Done').
        try:
            done = await self._first_visible(
                page,
                [
                    page.locator('[role="dialog"]').get_by_role(
                        "button",
                        name=re.compile(r"concluir|done|finalizar|finish", re.I),
                    ),
                    page.get_by_role(
                        "button",
                        name=re.compile(r"concluir|done|finalizar|finish", re.I),
                    ),
                ],
                timeout_ms=3000,
            )
            await self._robust_click(done)
            logger.info("whatsapp: criação finalizada ('Concluir').")
        except LoginError:
            pass

    # ------------------------------------------------------------------
    # Passo 5 — Verificação da empresa (Central de Segurança)
    # ------------------------------------------------------------------

    async def _run_business_verification(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Passo 5 — inicia a verificação da empresa na Central de Segurança.

        Fluxo (mapeado em produção 2026-06): ``settings/security`` → 'Iniciar
        verificação' → diálogo de verificação:
            Começar → País (Brasil) → Tipo='Empresa individual' → 'Tem
            registro' → CNPJ → Endereço (pré-preenchido) → Contato
            (pré-preenchido) → reCAPTCHA 'Não sou um robô' → 'Carregar
            documentos': seleciona o tipo 'CNPJ' e **PARA** (o upload do PDF
            em si fica para a próxima fatia — combinado com o operador).

        SCAFFOLD DE CALIBRAÇÃO: muitas telas vêm pré-preenchidas e o DOM real
        ainda não foi observado. Cada sub-tela é best-effort e salva DOM em
        falha (``debug_verify_*``), seguindo para capturar o máximo de telas
        por run. Os seletores serão lapidados contra os dumps das próximas
        runs.
        """
        # Marca que ALCANÇAMOS o passo 5 — ANTES dos early-returns de
        # idempotência. O teardown usa isto + KEEP_BROWSER_OPEN_ON_BV_ERROR
        # para manter o navegador aberto (debug/inspeção, ex.: ler o waba_id na
        # conta WhatsApp Business). "Alcançou o passo 5" vale mesmo quando ele
        # é PULADO por já ter concluído numa run anterior.
        self._reached_bv = True

        # Idempotência por perfil. 'completed' = submetido + código SMS
        # preenchido (fim real). 'started' = só o upload (modo manual, sem
        # auto-submit) — nesse modo o passo para após anexar e não reprocessa.
        if self._profile_flag("business_verification_completed"):
            logger.info(
                "Verificação da empresa já CONCLUÍDA em run anterior (flag "
                "business_verification_completed) — passo pulado."
            )
            return
        if self._profile_flag("business_verification_started") and not BV_AUTO_SUBMIT:
            logger.info(
                "Comprovante já anexado em run anterior e BV_AUTO_SUBMIT=off "
                "(aguardando validação manual) — passo pulado."
            )
            return

        # Gera o comprovante CNPJ (PDF) ANTES de navegar as telas — assim o
        # artefato fica pronto na saga para o upload (fatia futura). Primário:
        # render LOCAL via Playwright headless a partir da BrasilAPI; fallback:
        # Infosimples. O caminho vai para saga.artifacts['cnpj_card_path'].
        await self._bv_prepare_comprovante()

        # Loop de até 2 tentativas: a 1ª com o número SMS original, a 2ª
        # (se _BvSmsRestartNeeded) com o número da 2ª provedora. No restart
        # a página é recarregada e o fluxo inteiro refeito desde "Iniciar
        # verificação" — o PDF já foi re-gerado com o número novo dentro de
        # _verify_sms_via_api antes de levantar a exceção.
        for _bv_attempt in range(2):

            sec_url = self._with_business_id(BUSINESS_SECURITY_URL, result.business_id)
            await page.goto(sec_url, wait_until="domcontentloaded")
            await self._organic_wait()
            if not await self._ensure_business_session(page, target_url=sec_url):
                logger.error(
                    "business_verification: sessão do Business não estabelecida — "
                    "passo pulado (sem Central de Segurança para operar)."
                )
                return
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                pass
            await self._organic_wait()

            # 0) Action Needed: domínio confiável + proteção extra da conta de
            # anúncios — só na 1ª tentativa (2ª é restart de SMS, já resolveu).
            if _bv_attempt == 0:
                try:
                    await self._bv_resolve_action_needed(page)
                except Exception as _an_exc:  # noqa: BLE001
                    logger.debug("verify: _bv_resolve_action_needed falhou (%s).", _an_exc)

            # 1) 'Iniciar verificação' (botão azul da Central de Segurança).
            # Devolve a Page certa pra seguir (pode ser aba nova — ver docstring
            # de _bv_start_verification). Daqui em diante, `page` é essa Page —
            # todo o resto da função (steps 2-11) precisa operar nela, não na
            # aba original que pode ter ficado parada/morta.
            verify_page = await self._bv_start_verification(page)
            if verify_page is None:
                await self._dump_debug_html(page, "verify_start_missing")
                ineligible = await self._bv_account_ineligible(page)
                if ineligible:
                    logger.error(
                        "business_verification: conta INELEGÍVEL p/ verificação "
                        "('sua organização não precisa ser verificada'). Causa "
                        "provável: setup incompleto a montante (dados da empresa "
                        "não preenchidos, domínio não verificado ou nenhum ativo no "
                        "portfólio). Conclua os passos 2-4 (a CONTA DE ANÚNCIOS é o "
                        "ativo que habilita a verificação) antes de tentar de novo."
                    )
                else:
                    logger.error(
                        "business_verification: botão 'Iniciar verificação' não "
                        "encontrado na Central de Segurança — DOM salvo em .state/."
                    )

                # FALLBACK (pedido do operador, 2026-09): em algumas contas — só
                # ALGUMAS, o resto libera normal — o botão 'Iniciar verificação'
                # não aparece mesmo com o passo 4 (conta de anúncios) OK. Nesses
                # casos, cria a WABA para esta BM como ativo alternativo: ela
                # própria oferece um ponto de entrada de verificação (ver
                # _bv_start_verification, fallback via Contas do WhatsApp) que às
                # vezes libera quando a Central de Segurança não libera. Roda
                # tanto no caso 'inelegível' quanto no caso 'botão sumiu' — pedido
                # do operador foi "não liberou o botão de verificar", sem
                # distinguir o motivo. Guardado por flag (ligada por padrão) e por
                # result.waba_id, pra não tentar de novo se uma run anterior já
                # deixou a WABA criada.
                if WABA_FALLBACK_SE_VERIFICACAO_INDISPONIVEL and not result.waba_id:
                    logger.warning(
                        "business_verification: botão de verificar NÃO liberou "
                        "para esta conta — acionando fallback: criando a conta "
                        "do WhatsApp Business (WABA) para esta BM."
                    )
                    try:
                        await self._create_whatsapp_business_account(page, result)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "business_verification: fallback de criação da WABA "
                            "falhou (%s) — desistindo da verificação nesta run.",
                            exc,
                        )
                        return

                    # A WABA recém-criada pode expor 'Iniciar verificação' onde a
                    # Central de Segurança não expôs — tenta reabrir a partir
                    # dela (revisita a Central primeiro; _bv_start_verification
                    # já cai sozinho no fallback via Contas do WhatsApp).
                    await page.goto(sec_url, wait_until="domcontentloaded")
                    await self._organic_wait()
                    verify_page = await self._bv_start_verification(page)
                    if verify_page is None:
                        logger.error(
                            "business_verification: mesmo após criar a WABA, o "
                            "botão 'Iniciar verificação' continua indisponível "
                            "— desistindo da verificação nesta run (a WABA fica "
                            "como ativo alternativo para esta BM)."
                        )
                        return
                else:
                    return
            page = verify_page  # pode ser a mesma aba ou uma aba nova — ver acima.

            # 2-8) Diálogo de verificação até 'Contato' inclusive — qualquer uma
            # dessas telas pode devolver 'Erro ao realizar a consulta' (Central
            # não confirmou os dados contra o registro oficial). Pedido do
            # operador: terminal pra esta conta — descarta e aborta a run aqui,
            # sem gastar tempo no recaptcha/upload que vêm depois.
            try:
                # 2) Diálogo 'Verificar [empresa]' → 'Começar'. Cada _bv_advance
                # pode abrir uma aba nova (achado em produção 2026-06: o clique
                # de 'país' abria outra aba e o bot continuava preenchendo a
                # velha, parada) — por isso SEMPRE reatribui `page`.
                page = await self._bv_advance(page, "começar", wait_enabled_s=15.0)

                # 3) País (Brasil já selecionado) → Avançar.
                page = await self._bv_advance(page, "país")

                # 4) Tipo da empresa → 'Empresa individual'.
                await self._bv_select_option(
                    page, r"empresa individual|sole proprietor", step="tipo_empresa"
                )
                page = await self._bv_advance(page, "tipo_empresa")

                # 5) 'Sua empresa tem registro oficial?' → 'Tem registro'.
                await self._bv_select_option(
                    page, r"tem registro|has registration|registered", step="tem_registro"
                )
                page = await self._bv_advance(page, "tem_registro")

                # 6) Dados da empresa: CNPJ (Identificação Fiscal) + corrige nome.
                # A Meta pré-preenche 'Nome da empresa' com a razão social RAW
                # (ex.: "66.499.935 BRUNA EDUARDA DAVIES") — precisamos limpar o
                # prefixo numérico e aplicar Title Case antes de avançar.
                await self._bv_fill_tax_id(page)
                await self._bv_fix_company_name(page)
                await self._bv_fill_legal_name(page)
                page = await self._bv_advance(page, "dados_empresa")

                # 6b) 'Selecione sua empresa': a Meta consulta registros públicos
                # pelo CNPJ e pede para escolher um. SEMPRE 'Minha empresa não está
                # na lista' (regra do operador) — sem seleção o 'Avançar' fica
                # desabilitado e a run trava aqui (produção 2026-09, conta Yasmin).
                page = await self._bv_select_business_from_records(page)

                # 7) Endereço (provavelmente pré-preenchido) → Avançar.
                page = await self._bv_advance(page, "endereco")

                # 8) Contato: telefone + site (provavelmente pré-preenchido) → Avançar.
                page = await self._bv_advance(page, "contato")
            except BusinessDataQueryError as exc:
                await self._dump_debug_html(page, "verify_query_error")
                email = (self.credentials.email_or_phone or "").strip()
                try:
                    from core import store  # noqa: PLC0415

                    discarded = store.discard_fb_account(email)
                except Exception as store_exc:  # noqa: BLE001
                    discarded = None
                    logger.debug(
                        "verify: falha ao descartar conta no store (%s).", store_exc
                    )
                result.account_discarded = True
                result.current_step = "account_discarded"
                logger.error(
                    "%s Conta %s DESCARTADA (%s) — abortando a run.",
                    exc, email, discarded or "não estava no estoque",
                )
                return

            # 9) reCAPTCHA 'Não sou um robô' (corrida manual×2captcha) → upload.
            await self._bv_solve_recaptcha_until_upload(page, result)

            # 10) 'Carregar documentos': seleciona tipo = CNPJ.
            if not await self._bv_select_doc_type_cnpj(page):
                await self._dump_debug_html(page, "verify_doc_type_missing")
                logger.warning(
                    "business_verification: tela 'Carregar documentos' / tipo "
                    "'CNPJ' não confirmada — DOM salvo em .state/ para calibração."
                )
                return

            # 11) ANEXA o PDF do comprovante.
            if not await self._bv_upload_document(page):
                await self._dump_debug_html(page, "verify_upload_failed")
                logger.warning(
                    "business_verification: não consegui anexar o PDF do "
                    "comprovante — DOM salvo em .state/ para calibração."
                )
                return

            # 11b) 'Seu documento inclui o número de telefone?' → 'Sim' (nosso
            # comprovante carimba o número do sms24h, igual ao telefone da org na
            # Meta). Dispensa o 2º documento e habilita o 'Avançar'.
            await self._bv_answer_doc_phone_question(page)

            # Modo manual (BV_AUTO_SUBMIT=off): para após anexar para o operador
            # validar o documento na janela antes de submeter (submit é
            # irreversível). Marca 'started' para não re-anexar em re-runs.
            if not BV_AUTO_SUBMIT:
                self._set_profile_flag("business_verification_started")
                result.business_verification_started = True
                logger.success(
                    "business_verification: comprovante CNPJ ANEXADO — PARANDO "
                    "antes de submeter (BV_AUTO_SUBMIT=off). Valide o documento na "
                    "janela do AdsPower e submeta manualmente, ou ligue "
                    "BV_AUTO_SUBMIT=true para o fluxo completo."
                )
                return

            # 12) Fluxo automático: submeter → receber+preencher código SMS → fim.
            result.business_verification_started = True
            try:
                await self._bv_complete_verification(page, result)
            except _BvSmsRestartNeeded:
                if _bv_attempt >= 1:
                    logger.error(
                        "SMS fallback restart: já reiniciou uma vez — desistindo."
                    )
                    return
                # Troca o client SMS para a 2ª provedora (preparado em
                # _verify_sms_via_api antes de levantar a exceção).
                if hasattr(self, "_sms_restart_client") and self._sms_restart_client:
                    self.sms_client = self._sms_restart_client
                    self._sms_restart_client = None
                logger.warning(
                    "SMS fallback: 1º SMS não chegou — reiniciando verificação "
                    "desde 'Iniciar verificação' com 2º número (%s). "
                    "Recarregando a página…",
                    self.preallocated_sms_phone,
                )
                await page.reload(wait_until="domcontentloaded")
                await self._organic_wait()
                continue

            # Se chegou aqui sem exceção, o fluxo terminou (sucesso ou falha
            # tratada dentro de _bv_complete_verification) — sai do loop.
            break

    # --- Helpers do passo 5 (verify_*) ---------------------------------
    def _save_review_copy_of_comprovante(
        self,
        *,
        source_path: Path,
        run_id: str,
        sms_phone: Optional[str],
    ) -> Optional[Path]:
        """Salva uma cópia do comprovante em pasta fixa para revisão manual."""
        try:
            if not source_path.is_file():
                return None
            COMPROVANTES_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
            phone_digits = re.sub(r"\D+", "", sms_phone or "")
            phone_tag = phone_digits or "telefone_receita"
            dest = COMPROVANTES_REVIEW_DIR / (
                f"{run_id}_{phone_tag}_{source_path.name}"
            )
            shutil.copy2(source_path, dest)
            # Sidecar com metadados p/ a aba "PDFs/CNPJs" listar, editar e
            # re-renderizar (precisa do CNPJ pra buscar os dados). Best-effort.
            try:
                import json as _json  # noqa: PLC0415
                cd = getattr(self, "cnpj_data", None)
                meta = {
                    "cnpj": (getattr(cd, "cnpj", "") or "") if cd else "",
                    "razao_social": (getattr(cd, "razao_social", "") or "") if cd else "",
                    "email": (getattr(cd, "email", "") or "") if cd else "",
                    "telefone": phone_digits or ((getattr(cd, "telefone", "") or "") if cd else ""),
                    "run_id": run_id,
                }
                dest.with_suffix(dest.suffix + ".meta.json").write_text(
                    _json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as _mex:  # noqa: BLE001 — sidecar é best-effort
                logger.debug("comprovante: sidecar meta não salvo (%s).", _mex)
            return dest
        except OSError as exc:
            logger.warning(
                "comprovante: falha ao salvar cópia de revisão (%s).", exc
            )
            return None

    async def _bv_prepare_comprovante(self) -> None:
        """Gera o PDF do comprovante CNPJ e registra na saga (best-effort).

        Primário: render LOCAL (Playwright headless) a partir do CNPJData da
        BrasilAPI. Fallback (exceção/dados insuficientes): Infosimples (PDF
        oficial). O caminho final é gravado em
        ``saga.artifacts['cnpj_card_path']`` — o upload (fatia futura) LÊ daí,
        nunca faz glob. Falha total NÃO derruba o passo (segue para as telas).

        **Telefone no comprovante**: usa o número SMS24h desta run (o mesmo
        que recebe o código na verificação). ``COMPROVANTE_TELEFONE`` no
        ``.env`` só entra se não houver SMS alocado (ver ``comprovante_phone``).
        """
        if self.saga is None:
            logger.warning("comprovante: saga ausente — pulando geração do PDF.")
            return
        run_id = self.saga.run_id

        # --- Telefone no PDF (SMS24h desta run > .env > Receita) -----------
        if not should_skip_sms_acquire_for_comprovante():
            await self._ensure_sms_phone_for_comprovante()
        sms_phone = resolve_comprovante_telefone(
            sms_phone_raw=self.preallocated_sms_phone,
        )
        if not sms_phone:
            logger.warning(
                "comprovante: sem número SMS24h para carimbar no PDF — "
                "documento sairá com telefone da Receita (verificação SMS "
                "pode falhar se o cadastro Meta usar outro número)."
            )
        else:
            logger.info(
                "comprovante: telefone carimbado no PDF = %s (sms24h=%s).",
                sms_phone,
                self.preallocated_sms_phone or "?",
            )

        path: Optional[Path] = None
        # 1) Primário — render local.
        try:
            path = await render_comprovante_local(
                self.cnpj_data, run_id, telefone_override=sms_phone,
            )
            logger.success(
                "comprovante: render LOCAL (run %s) → %s (telefone=%s)",
                run_id, path, sms_phone or "<receita>",
            )
        except Exception as exc:  # noqa: BLE001 — qualquer falha → fallback
            if sms_phone:
                logger.error(
                    "comprovante: render local falhou (%s) com SMS24h=%s — "
                    "NÃO caindo no Infosimples (PDF oficial não carimba o "
                    "número alugado; verificação SMS quebraria).",
                    exc, sms_phone,
                )
                return
            logger.warning(
                "comprovante: render local falhou (%s) — fallback Infosimples.",
                exc,
            )
            # 2) Fallback — Infosimples (PDF oficial) — só sem SMS24h no PDF.
            try:
                cnpj = (self.cnpj_data.cnpj or "") if self.cnpj_data else ""
                res = await emitir_comprovante_cnpj(cnpj)
                path = Path(res.pdf_path)
                logger.success(
                    "comprovante: fallback Infosimples → %s", path
                )
            except Exception as exc2:  # noqa: BLE001
                logger.error(
                    "comprovante: render local E Infosimples falharam (%s) — "
                    "sem PDF para o upload nesta run.", exc2,
                )
                return
        if path is not None:
            self.saga.record_artifact("cnpj_card_path", str(path))
            review_copy = self._save_review_copy_of_comprovante(
                source_path=path,
                run_id=run_id,
                sms_phone=sms_phone,
            )
            if review_copy is not None:
                self.saga.record_artifact(
                    "cnpj_card_review_copy_path", str(review_copy)
                )
                logger.info(
                    "comprovante: cópia de revisão salva em %s", review_copy
                )

    async def _ensure_sms_phone_for_comprovante(self) -> None:
        """Garante que ``self.preallocated_sms_phone`` tenha um número.

        Se o número ainda não foi alocado (nem pela UI, nem pelo passo 4),
        adquire um via ``Sms24hClient.acquire_number`` e rastreia na saga
        para compensação. Best-effort: se falhar, loga e segue (o PDF sai
        com o telefone da Receita — melhor que não sair).
        """
        if (self.preallocated_sms_phone or "").strip():
            logger.info(
                "comprovante: telefone SMS já disponível (%s).",
                self.preallocated_sms_phone,
            )
            return
        if not self.sms_client.enabled:
            logger.warning(
                "comprovante: SMS24H_API_KEY não configurada — telefone do "
                "comprovante será o da Receita Federal."
            )
            return
        try:
            async with self.sms_client:
                activation_id, phone = await self.sms_client.acquire_number()
            self.preallocated_sms_phone = phone
            self.preallocated_sms_activation_id = activation_id
            logger.info(
                "comprovante: número adquirido para o PDF (id=%s, phone=%s).",
                activation_id, phone,
            )
            self._track_sms_activation(
                activation_id,
                detail=f"adquirido para comprovante CNPJ (phone={phone})",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "comprovante: falha ao adquirir número SMS (%s) — telefone "
                "do comprovante será o da Receita Federal.", exc,
            )

    def _format_sms_phone_for_comprovante(self) -> Optional[str]:
        """Formata ``preallocated_sms_phone`` para o padrão BR (XX) XXXXX-XXXX.

        Retorna ``None`` quando não há número — o render mantém o telefone
        da Receita (comportamento backward-compatible).
        """
        raw = (self.preallocated_sms_phone or "").strip()
        digits = re.sub(r"\D+", "", raw)
        if len(digits) >= 11:
            return f"({digits[-11:-9]}) {digits[-9:-4]}-{digits[-4:]}"
        if len(digits) == 10:
            return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
        return None

    # Regex do título do dialog 'Deseja sair?' que a Meta exibe quando o
    # wizard de verificação detecta uma tentativa de navegação/abandono.
    _BV_EXIT_DIALOG_RE = re.compile(
        r"deseja sair|want to leave|leave.*page|sair agora|leave now",
        re.I,
    )

    async def _dismiss_bv_exit_dialog(self, page: Page) -> bool:
        """Detecta e fecha o dialog 'Deseja sair?' do wizard de verificação.

        A Meta renderiza esse dialog React (não nativo) quando detecta que o
        wizard está prestes a ser abandonado. Ao clicar no X ou 'Cancelar'
        o wizard permanece ativo e o progresso é mantido.

        Devolve True se dispensou o dialog.
        """
        try:
            if not await page.get_by_text(self._BV_EXIT_DIALOG_RE).first.is_visible(
                timeout=200
            ):
                return False
        except Exception:  # noqa: BLE001
            return False

        logger.warning(
            "BV: dialog 'Deseja sair?' detectado — fechando para manter o "
            "wizard ativo."
        )
        for loc in [
            # X no canto do dialog 'Deseja sair?' (sem texto — identificado
            # por aria-label ou posição junto ao título).
            page.get_by_role("button", name=re.compile(r"^fechar$|^close$", re.I)),
            page.locator('[aria-label="Fechar"], [aria-label="Close"]').first,
            # 'Cancelar' = fica na página (mesma semântica do X).
            page.get_by_role("button", name=re.compile(r"^cancelar$|^cancel$", re.I)),
        ]:
            try:
                if await loc.first.is_visible(timeout=600):
                    await loc.first.click()
                    await asyncio.sleep(0.5)
                    logger.info("BV: 'Deseja sair?' fechado (Fechar/Cancelar).")
                    return True
            except Exception:  # noqa: BLE001
                continue
        # Fallback: Escape fecha a maioria dos dialogs da Meta.
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            logger.info("BV: 'Deseja sair?' fechado via Escape.")
            return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _bv_advance_button(self, page: Page):
        """Locator do botão de avanço do diálogo de verificação (sem levantar).

        Cobre 'Avançar', 'Continuar', 'Começar' e variantes EN — o mesmo
        botão primário muda de rótulo entre as telas do fluxo.

        Combina o escopo do diálogo (modal "normal", role="dialog") COM a
        página inteira — vídeo do operador confirmou que pelo menos as
        telas 'começar'/'país' do assistente (quando aberto via aba nova/
        Contas do WhatsApp) NÃO usam role="dialog" nesse contêiner: o botão
        'Avançar' ficava visível e HABILITADO na tela, mas o escopo só-
        diálogo nunca achava nada e `_bv_advance` ficava "aguardando
        habilitar" até estourar os 20s, sempre. Sem o fallback page-wide
        esses passos nunca avançavam de verdade.
        """
        dialog = page.locator('[role="dialog"]')
        adv_re = re.compile(
            r"^\s*(avan[cç]ar|continuar|continue|next|come[cç]ar|get started)\s*$",
            re.I,
        )
        return dialog.get_by_role("button", name=adv_re).or_(
            page.get_by_role("button", name=adv_re)
        )

    # "Erro ao realizar a consulta." — a Central de Verificação não confirmou
    # os dados da empresa (CNPJ/endereço/contato) contra o registro oficial.
    # Pedido do operador 2026-06: TERMINAL, nunca passa clicando de novo.
    _BV_QUERY_ERROR_RE = re.compile(
        r"erro ao realizar a consulta|error (?:performing|while performing) the query",
        re.I,
    )

    # Tela 'Selecione sua empresa' (a Meta cruza o CNPJ com registros públicos
    # e lista 0..N empresas). REGRA DO OPERADOR: SEMPRE 'Minha empresa não está
    # na lista' — escolher um registro traz endereço/telefone de terceiros, que
    # não batem com o comprovante e reprovam a verificação.
    _BV_RECORDS_SCREEN_RE = re.compile(
        r"selecione sua empresa|encontramos os seguintes itens|"
        r"registros p[úu]blicos|select your business|public records",
        re.I,
    )
    _BV_NAO_LISTADA_PATTERN = (
        r"minha empresa n[ãa]o est[áa] na lista|"
        r"my business (?:isn'?t|is not) listed|"
        r"n[ãa]o (?:vejo|encontrei) minha empresa"
    )

    async def _bv_records_screen_visible(
        self, page: Page, timeout_ms: int = 600
    ) -> bool:
        """True se a tela 'Selecione sua empresa' está na frente agora."""
        for loc in (
            page.get_by_role("heading", name=self._BV_RECORDS_SCREEN_RE),
            page.get_by_text(self._BV_RECORDS_SCREEN_RE),
        ):
            try:
                if await loc.first.is_visible(timeout=timeout_ms):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _bv_click_empresa_nao_listada(self, page: Page) -> bool:
        """Marca 'Minha empresa não está na lista' SE a tela estiver visível.

        Devolve True só quando detectou a tela E conseguiu marcar a opção —
        o chamador então segue o fluxo normal de 'Avançar'.
        """
        if not await self._bv_records_screen_visible(page):
            return False
        logger.info(
            "verify: tela 'Selecione sua empresa' detectada — marcando "
            "'Minha empresa não está na lista' (regra do operador)."
        )
        if not await self._bv_select_option(
            page, self._BV_NAO_LISTADA_PATTERN, step="selecione_empresa"
        ):
            await self._dump_debug_html(page, "verify_empresa_nao_listada_missing")
            logger.warning(
                "verify: opção 'Minha empresa não está na lista' não "
                "encontrada na tela de registros públicos — DOM salvo em "
                ".state/. Seguindo assim mesmo (o 'Avançar' pode estar travado)."
            )
            return False
        await self._organic_wait()
        return True

    async def _bv_advance(
        self, page: Page, step: str, *, wait_enabled_s: float = 20.0
    ) -> Page:
        """Espera o botão de avanço habilitar e clica (best-effort).

        Espelha ``_wa_wait_and_click_continue`` mas com prefixo 'verify' nos
        logs/dumps. Botões podem ficar desabilitados até campos/captcha ok.

        Devolve a ``Page`` em que o caller deve seguir operando — o clique
        em 'Avançar' pode abrir uma ABA NOVA (confirmado em produção
        2026-06: o dump salvo na tela 'tipo_empresa' mostrava ainda o
        título 'Selecionar um país' — o clique de 'país' tinha aberto outra
        aba e o bot continuou preenchendo a aba VELHA e parada, igual ao
        bug já corrigido em ``_bv_start_verification``, só que aqui dentro
        do próprio fluxo de avanço). Quando não abre aba nova, devolve a
        mesma ``page`` recebida — sempre re-atribua: ``page = await
        self._bv_advance(page, ...)``.

        Levanta ``BusinessDataQueryError`` se a Central mostrar 'Erro ao
        realizar a consulta' — terminal, não tem "esperar mais" que resolva
        (pedido do operador: descartar a conta direto, sem insistir)."""
        btn = self._bv_advance_button(page)
        dialog = page.locator('[role="dialog"]')
        query_err = dialog.get_by_text(self._BV_QUERY_ERROR_RE).or_(
            page.get_by_text(self._BV_QUERY_ERROR_RE)
        )
        deadline = time.monotonic() + max(wait_enabled_s, 0.0)
        announced = False
        records_handled = False
        while True:
            # Detecta e fecha 'Deseja sair?' ANTES de qualquer outra ação.
            # Esse dialog React da Meta aparece quando o wizard detecta uma
            # tentativa de navegação; fechar ele (X ou Cancelar) mantém o
            # wizard ativo e permite retomar o avanço.
            await self._dismiss_bv_exit_dialog(page)
            try:
                if await query_err.first.is_visible(timeout=200):
                    raise BusinessDataQueryError(
                        f"Central de Verificação: 'Erro ao realizar a "
                        f"consulta' na tela '{step}'."
                    )
            except BusinessDataQueryError:
                raise
            except Exception:  # noqa: BLE001
                pass
            try:
                # A Central RENDERIZA 2 elementos "Avançar" durante a
                # transição entre telas: um DUPLICADO oculto/morto
                # (aria-hidden="true" aria-disabled="true" tabindex="-1" —
                # da tela que está saindo) e o REAL (sem aria-hidden/
                # aria-disabled, tabindex="0" — da tela atual). `.first`
                # cego pegava QUALQUER um dos dois por ordem de DOM —
                # quando pegava o morto, "is_enabled()" nativo dava True
                # (o disabled é só por ARIA, não pelo atributo HTML), o bot
                # clicava um botão que não faz nada, logava "avanço
                # clicado" e a tela NUNCA avançava de verdade (achado via
                # dump real: 'país' clicado, mas o DOM seguinte ainda
                # mostrava o diálogo 'Selecionar um país' aberto). Varre
                # todos os candidatos e pula qualquer um com aria-hidden
                # ou aria-disabled = "true".
                cand_target = None
                count = await btn.count()
                for i in range(min(count, 8)):
                    cand = btn.nth(i)
                    try:
                        aria_hidden = await cand.get_attribute("aria-hidden")
                        aria_disabled = await cand.get_attribute("aria-disabled")
                        if aria_hidden == "true" or aria_disabled == "true":
                            continue
                        if not await cand.is_visible(timeout=400):
                            continue
                        if not await cand.is_enabled(timeout=300):
                            continue
                    except Exception:  # noqa: BLE001
                        continue
                    cand_target = cand
                    break
                if cand_target is not None:
                    # Antes de clicar, garante que nenhum listener beforeunload
                    # vai interceptar a navegação da SPA e exibir 'Deseja sair?'.
                    # O add_init_script cobre novas cargas; este evaluate cobre
                    # a página/popup JÁ carregada (incluindo BV popup reutilizado).
                    try:
                        await page.evaluate(self._BEFOREUNLOAD_SUPPRESS_JS)
                    except Exception:  # noqa: BLE001
                        pass
                    # Captura o aria-label ATUAL do diálogo antes de clicar.
                    # Após o clique (sem popup novo), aguardamos até este label
                    # mudar — sem isso, _bv_select_option é chamado na tela
                    # ANTERIOR pois a API da Meta pode levar vários segundos
                    # para avançar o diálogo (achado via dump 2026-06: label
                    # "Selecionar um país" presente quando tipo_empresa esperado).
                    try:
                        old_dialog_label = (
                            await dialog.get_attribute("aria-label", timeout=400) or ""
                        )
                    except Exception:  # noqa: BLE001
                        old_dialog_label = ""
                    try:
                        # A aba nova, quando abre, dispara o evento 'popup' no
                        # ATO do clique (window.open local, não depende de rede).
                        # 400ms cobre o evento na maioria dos casos — reduzido de
                        # 700ms pois o botão já está aberto.
                        async with page.expect_popup(timeout=400) as pop_info:
                            await self._robust_click(cand_target)
                        popup = await pop_info.value
                        try:
                            await popup.wait_for_load_state(
                                "domcontentloaded",
                                timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info(
                            "verify: avanço clicado (tela %s) — abriu ABA "
                            "NOVA, seguindo nela.", step,
                        )
                        await self._organic_wait()
                        return popup
                    except PlaywrightTimeoutError:
                        logger.info("verify: avanço clicado (tela %s).", step)
                        # Aguarda a transição real do diálogo. O click foi
                        # registrado mas a resposta da API Meta pode levar
                        # vários segundos — poleia até o aria-label mudar
                        # (indica que a próxima tela carregou), máx. 20s.
                        if old_dialog_label:
                            # Poll FINO (0,4s/passo) p/ pegar a transição assim
                            # que a API da Meta responde. Reduzido de 18s para 13s
                            # (tipo_empresa e dados_empresa abrem rápido em produção).
                            _t_deadline = time.monotonic() + 13.0
                            while time.monotonic() < _t_deadline:
                                await asyncio.sleep(0.4)
                                try:
                                    new_label = await dialog.get_attribute(
                                        "aria-label", timeout=500
                                    )
                                    if new_label != old_dialog_label:
                                        logger.debug(
                                            "verify: diálogo avançou '%s' → '%s' "
                                            "(tela %s).",
                                            old_dialog_label, new_label, step,
                                        )
                                        break
                                except Exception:  # noqa: BLE001
                                    break  # diálogo desapareceu / mudou
                        else:
                            await self._organic_wait()
                        return page
            except Exception:  # noqa: BLE001
                pass
            # REDE DE SEGURANÇA (regra do operador 2026-09): chegou aqui = não
            # há 'Avançar' clicável. A causa mais comum é a tela 'Selecione sua
            # empresa' ter subido FORA da ordem esperada do wizard — sem
            # nenhuma opção marcada o 'Avançar' fica desabilitado e a run
            # trava (print do operador: log já em 'contato'/reCAPTCHA com a
            # tela de registros públicos ainda aberta na frente). Marca
            # 'Minha empresa não está na lista' em QUALQUER tela e volta ao
            # loop para clicar o 'Avançar' que acabou de habilitar.
            if not records_handled and await self._bv_click_empresa_nao_listada(
                page
            ):
                records_handled = True
                logger.info(
                    "verify: tela de registros públicos resolvida durante a "
                    "tela '%s' — retomando o avanço.", step,
                )
                continue
            if not announced:
                logger.info(
                    "verify: aguardando avanço habilitar (tela %s, até %.0fs).",
                    step, wait_enabled_s,
                )
                announced = True
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.5)
        await self._dump_debug_html(page, f"verify_advance_blocked_{step}")
        logger.warning(
            "verify: botão de avanço não habilitou (tela %s) — DOM salvo.", step
        )
        return page

    async def _bv_select_business_from_records(self, page: Page) -> Page:
        """Tela 'Selecione sua empresa' → SEMPRE 'Minha empresa não está na lista'.

        Depois do CNPJ, a Meta busca registros públicos e lista de 0 a N
        empresas + a opção de nenhuma delas. Escolher um registro traz
        endereço/telefone/e-mail de terceiros (que não batem com o comprovante)
        e reprova a verificação; a regra do operador é sempre 'não está na
        lista'. Sem NENHUMA seleção o botão 'Avançar' fica desabilitado e a run
        trava nesta tela (produção 2026-09, conta Yasmin).

        Best-effort: a tela nem sempre aparece (CNPJ sem registro público
        indexado) — nesse caso devolve a mesma ``page`` sem tocar em nada.
        """
        # A consulta aos registros públicos é LENTA (a Meta bate em base
        # externa): 3s de scan (6×0,5s) perdiam a tela em produção e o wizard
        # seguia "avançando" telas que nem estavam na frente. ~12s agora.
        try:
            await self._first_visible_scan(
                page,
                [
                    page.get_by_role("heading", name=self._BV_RECORDS_SCREEN_RE),
                    page.get_by_text(self._BV_RECORDS_SCREEN_RE),
                ],
                rounds=15,
                round_wait_s=0.8,
            )
        except LoginError:
            logger.debug(
                "verify: tela 'Selecione sua empresa' não apareceu — seguindo "
                "(a rede de segurança do _bv_advance cobre se ela subir depois)."
            )
            return page

        # A frase também aparece no PARÁGRAFO de instruções ("…selecione a
        # opção \"Minha empresa não está na lista\"") — o _bv_select_option
        # tenta o casamento EXATO/ancorado primeiro, que pega a LINHA-opção e
        # não o enunciado, e sobe até o elemento que alterna o radio.
        if not await self._bv_click_empresa_nao_listada(page):
            return page
        logger.info(
            "verify: 'Minha empresa não está na lista' marcada (regra do "
            "operador — nunca usar registro público de terceiros)."
        )
        return await self._bv_advance(page, "selecione_empresa")

    async def _bv_select_option(
        self, page: Page, pattern: str, *, step: str
    ) -> bool:
        """Seleciona uma opção/radio por texto (ex.: 'Empresa individual').

        Casa a opção EXATA (ancorada) ANTES da substring: na tela 'tem
        registro' o texto curto da opção ('Tem registro') é substring da
        PERGUNTA ('Sua empresa tem registro oficial?') e da NEGATIVA ('Ainda
        não tem registro') — a substring pegava o enunciado e o radio ficava
        sem seleção, travando o Avançar (produção 2026-06, conta Sergio).
        Os radios do fluxo não têm aria-label, então clicamos o TEXTO da
        opção (a linha é um label clicável que alterna o radio).
        """
        rx = re.compile(pattern, re.I)
        exact = re.compile(rf"^\s*(?:{pattern})\s*$", re.I)
        dialog = page.locator('[role="dialog"]')
        try:
            opt = await self._first_visible_scan(
                page,
                [
                    # EXATO primeiro (não casa a pergunta nem a negativa).
                    dialog.get_by_role("radio", name=exact),
                    dialog.get_by_text(exact),
                    page.get_by_text(exact),
                    # Linha-opção como div[role=button] SEM role=radio — tela
                    # 'Escolha como confirmar sua conexão' (verificação por
                    # DOCUMENTO, produção 2026-06, conta LucioLuis): o radio é um
                    # aria-checked custom e o título traz sufixo ('Mensagem de
                    # texto (SMS)'), que o regex EXATO não casa. O alvo clicável é
                    # a LINHA inteira (role=button), cujo nome acessível inclui o
                    # título. Preferido sobre get_by_text(rx), que casava um
                    # <span> de descrição NÃO-clicável → o radio nunca alternava
                    # e o 'Avançar' ficava travado (a Meta nunca enviava o SMS).
                    dialog.get_by_role("button", name=rx),
                    page.get_by_role("button", name=rx),
                    # Fallback substring (telas onde a opção é única no texto).
                    dialog.get_by_role("radio", name=rx),
                    dialog.get_by_text(rx),
                    page.get_by_text(rx),
                ],
                rounds=8,
            )
        except LoginError:
            await self._dump_debug_html(page, f"verify_option_missing_{step}")
            logger.warning(
                "verify: opção '%s' não encontrada (tela %s) — DOM salvo.",
                pattern, step,
            )
            return False
        # O texto casado pode ser um <span> NÃO-clicável (título/descrição). O
        # alvo que ALTERNA o radio é a LINHA clicável (label / role=radio /
        # role=button) que o contém — sobe até ela antes de clicar. Sem isso o
        # clique caía no span e o radio não mudava (Avançar travado).
        target = await self._bv_clickable_row(opt)
        await self._robust_click(target)
        logger.info("verify: opção selecionada (tela %s): %s", step, pattern)
        await self._organic_wait()
        return True

    async def _bv_clickable_row(self, opt: Locator) -> Locator:
        """Resolve o elemento CLICÁVEL da linha-opção a partir do texto casado.

        Sobe pelos ancestrais até o 1º container que alterna o radio (label,
        ``role=radio`` ou ``role=button``); se nenhum existir, procura o
        ``input[type=radio]`` da mesma linha; em último caso devolve o próprio
        ``opt`` (comportamento antigo). Best-effort e sem levantar.
        """
        ancestors = (
            'xpath=ancestor-or-self::*[@role="radio"][1]',
            'xpath=ancestor-or-self::label[1]',
            'xpath=ancestor-or-self::*[@role="button"][1]',
        )
        for xp in ancestors:
            try:
                cand = opt.locator(xp)
                if await cand.count() and await cand.first.is_visible(timeout=400):
                    return cand.first
            except Exception:  # noqa: BLE001
                continue
        try:
            radio = opt.locator(
                'xpath=ancestor::*[.//input[@type="radio"]][1]'
                '//input[@type="radio"]'
            )
            if await radio.count() and await radio.first.is_visible(timeout=400):
                return radio.first
        except Exception:  # noqa: BLE001
            pass
        return opt

    async def _bv_answer_doc_phone_question(self, page: Page) -> bool:
        """Responde 'Sim' a 'Seu documento inclui o número de telefone?'.

        Na tela 'Carregar documentos' a Meta pergunta se o comprovante já
        contém o telefone da empresa. O default é 'Não', que exige um SEGUNDO
        documento (verificação de telefone) e mantém 'Avançar' desabilitado —
        foi onde travou (produção 2026-06, conta Michael).

        Nosso comprovante LOCAL já carimba o telefone do sms24h (o MESMO número
        que a Meta mostra como telefone da organização), então 'Sim' é
        verdadeiro e dispensa o 2º documento, habilitando o 'Avançar'.

        Best-effort: se a pergunta não estiver na tela (layout sem ela),
        retorna False sem derrubar o passo.
        """
        dialog = page.locator('[role="dialog"]')
        # Âncora na PERGUNTA do telefone para garantir que existe esta seção.
        question_re = re.compile(
            r"inclui o n[uú]mero de telefone|include.*phone number|"
            r"telefone da sua empresa", re.I,
        )
        try:
            if await dialog.get_by_text(question_re).first.count() == 0:
                return False
        except Exception:  # noqa: BLE001
            pass
        # 'Sim' exato (o radio não tem aria-label — clica o texto/label).
        sim_re = re.compile(r"^\s*(sim|yes)\s*$", re.I)
        try:
            opt = await self._first_visible_scan(
                page,
                [
                    dialog.get_by_role("radio", name=sim_re),
                    dialog.get_by_text(sim_re),
                ],
                rounds=4,
            )
        except LoginError:
            await self._dump_debug_html(page, "verify_doc_phone_sim_missing")
            logger.warning(
                "verify: opção 'Sim' (documento inclui telefone) não "
                "encontrada — DOM salvo. Avançar pode ficar travado."
            )
            return False
        await self._robust_click(opt)
        logger.info(
            "verify: 'Sim' marcado em 'documento inclui o telefone' "
            "(comprovante carimba o número do sms24h)."
        )
        await self._organic_wait()
        return True

    async def _bv_complete_verification(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Submete a verificação e preenche o código SMS que a Meta dispara.

        Fluxo (após o upload do comprovante):
          1. Submeter (Enviar/Concluir/Avançar) — dispara o código SMS.
          2. Receber+preencher o código via sms24h (reusa _verify_sms_via_api,
             que finaliza a ativação set_status=6 e marca result.sms_verified).
          3. Passo final simples (best-effort) — Concluir/Avançar.
          4. Marca a flag business_verification_completed.
        Best-effort com dumps por etapa — seletores calibrados contra DOM real.
        """
        # 1) Submeter o documento.
        if not await self._bv_submit_verification(page):
            await self._dump_debug_html(page, "verify_submit_failed")
            logger.warning(
                "business_verification: botão de submeter não encontrado/"
                "habilitado após o upload — DOM salvo em .state/."
            )
            return

        # 1.5) Escolher 'Mensagem de texto (SMS)', trocar o telefone pelo número
        # alugado DESTE run e clicar 'Enviar SMS' — é o que DISPARA o código.
        # Sem isso o bot ficava só esperando (a Meta nunca enviava) e, pior, o
        # número do cadastro era o de um run antigo → o SMS ia pro número errado
        # (produção 2026-06, conta Fabio: Meta→71927485750, bot→48...).
        # Best-effort: se as telas não aparecerem, segue pro polling.
        await self._bv_choose_sms_method_and_send(page)

        # 2) Receber + preencher o código SMS (Meta dispara após o submit).
        if not self.sms_client.enabled:
            logger.warning(
                "business_verification: cliente SMS desabilitado — não dá "
                "para receber o código de verificação. Etapa incompleta."
            )
            result.sms_failed_reason = "client_disabled"
            return
        ok = await self._verify_sms_via_api(page, result)
        if not ok:
            await self._dump_debug_html(page, "verify_sms_code_failed")
            result.sms_failed_reason = "timeout"  # ou "not_received" via _verify_sms_via_api
            logger.warning(
                "business_verification: código SMS não recebido/preenchido — "
                "DOM salvo em .state/ para calibração. sms_failed_reason=%s",
                result.sms_failed_reason,
            )
            return

        # 3) Telas FINAIS de conexão (Domínio → Avançar → Concluir). Só marca
        #    concluído ao chegar na tela TERMINAL ('Informações enviadas') —
        #    antes logava SUBMETIDA cedo demais e o operador clicava A/B/C na
        #    mão (prints conta Thiago 2026-06).
        finalized = await self._bv_finalize(page)
        if not finalized:
            logger.warning(
                "business_verification: código SMS OK, mas as telas finais de "
                "conexão (domínio → avançar → concluir) NÃO concluíram — a "
                "verificação pode não ter sido enviada. NÃO marcando como "
                "concluída (DOM salvo p/ calibração)."
            )
            return

        # 4) Concluído de verdade (alcançou 'Informações enviadas').
        self._set_profile_flag("business_verification_completed")
        logger.success(
            "business_verification: verificação SUBMETIDA e código SMS "
            "preenchido — empresa em verificação na Meta. 🎉"
        )
        # Robô terminou 100% do que dava p/ automatizar — a aprovação em si
        # é assíncrona (fica em análise do lado da Meta). Renomeia o perfil
        # no AdsPower p/ facilitar achar no painel o que já foi enviado.
        await self._mark_profile_in_analise()

    async def _bv_choose_sms_method_and_send(self, page: Page) -> bool:
        """Pós-submit: escolhe 'Mensagem de texto (SMS)', troca o telefone pelo
        número alugado e dispara 'Enviar SMS'.

        Telas (produção 2026-06, conta Fabio):
          1. 'Escolha como gostaria de confirmar sua conexão' → opção
             'Mensagem de texto (SMS)' → Avançar.
          2. 'Confirme o número do telefone' → editar 'Telefone da organização'
             com o número DESTE run → 'Enviar SMS'.
        Sem isso o bot só ficava no polling (a Meta nunca enviava) e o número
        do cadastro era de um run antigo → o código ia pro número errado.
        Best-effort: dumpa e devolve False se alguma tela faltar.
        """
        dialog = page.locator('[role="dialog"]')
        # 1) Método de confirmação → 'Mensagem de texto (SMS)'.
        #    Âncora no TÍTULO da opção ('Mensagem de texto'), NÃO em '\bsms\b':
        #    a DESCRIÇÃO da linha ('Enviaremos um SMS com o código…') também
        #    contém 'SMS' e o get_by_text casava esse <span> NÃO-clicável → o
        #    radio nunca alternava e o 'Avançar' ficava travado, então a Meta
        #    nunca enviava o código (produção 2026-06, conta Fabio; reincidente).
        #    'mensagem de texto' aparece SÓ no título → clica a linha certa.
        #    (Se a tela não existir nesta variante, _bv_select_option só loga.)
        #    Retry de até 3x: a tela pode demorar a renderizar após o avanço
        #    do step anterior (pedido do operador 2026-06-21: "umas vezes não
        #    seleciona o certo").
        _sms_selected = False
        for _sms_try in range(3):
            _sms_selected = await self._bv_select_option(
                page,
                r"mensagem de texto|text message",
                step="metodo_sms",
            )
            if _sms_selected:
                break
            # Não encontrou: pode ser que a tela ainda não renderizou ou
            # não existe nesta variante. Espera um pouco e tenta de novo.
            if _sms_try < 2:
                await asyncio.sleep(2)
        page = await self._bv_advance(page, "metodo_sms")
        dialog = page.locator('[role="dialog"]')  # re-resolve — `page` pode ter mudado

        # 2) Troca o telefone da organização pelo número alugado deste run.
        await self._bv_fill_org_phone(page)

        # 3) 'Enviar SMS' — dispara o código (3 camadas de tentativas).
        dialog = page.locator('[role="dialog"]')

        # Camada 1: botões específicos de SMS
        sms_re = re.compile(
            r"^\s*(enviar\s+sms|send\s+sms|enviar\s+c[oó]digo|send\s+code|"
            r"receber\s+c[oó]digo|receive\s+code|get\s+code|text\s+me)\s*$",
            re.I
        )

        # Camada 2: botões genéricos de avanço
        generic_re = re.compile(
            r"^\s*(enviar|send|continuar|continue|avan[çc]ar|next|pr[óo]ximo|"
            r"confirmar|confirm)\s*$",
            re.I
        )

        # Loop de tentativas (3x)
        for attempt in range(1, 4):
            btn = None

            # Tenta Camada 1: SMS específico
            try:
                btn = await self._first_visible(
                    page,
                    [
                        dialog.get_by_role("button", name=sms_re),
                        page.get_by_role("button", name=sms_re),
                    ],
                    timeout_ms=4000,
                )
                logger.info(
                    "verify: botão SMS encontrado (Camada 1, tentativa %d/3)", attempt
                )
            except LoginError:
                logger.debug(
                    "verify: botão SMS não encontrado na Camada 1 (tentativa %d/3)",
                    attempt,
                )
                btn = None

            # Tenta Camada 2: genérico de avanço
            if not btn:
                try:
                    btn = await self._first_visible(
                        page,
                        [
                            dialog.get_by_role("button", name=generic_re),
                            page.get_by_role("button", name=generic_re),
                        ],
                        timeout_ms=4000,
                    )
                    logger.info(
                        "verify: botão encontrado via genérico (Camada 2, tentativa %d/3)",
                        attempt,
                    )
                except LoginError:
                    logger.debug(
                        "verify: botão genérico não encontrado (Camada 2, tentativa %d/3)",
                        attempt,
                    )
                    btn = None

            # Tenta Camada 3: botão primário do CSS
            if not btn:
                try:
                    # Procura botão primário do diálogo (último)
                    candidates = await page.locator(
                        '[role="dialog"] button[type="submit"], '
                        '[role="dialog"] div[role="button"][class*="primary"], '
                        '[role="dialog"] div[aria-label][role="button"]'
                    ).all()
                    if candidates:
                        btn = candidates[-1]  # pega o último
                        logger.info(
                            "verify: botão primário encontrado via CSS (Camada 3, tentativa %d/3)",
                            attempt,
                        )
                except Exception as exc:
                    logger.debug(
                        "verify: erro ao procurar botão primário CSS (Camada 3, tentativa %d/3): %s",
                        attempt, exc,
                    )
                    btn = None

            # Se achou em qualquer camada, clica
            if btn:
                await self._robust_click(btn)
                logger.info("verify: 'Enviar SMS' clicado — Meta vai disparar o código.")
                await self._organic_wait()
                return True

            # Se não achou, aguarda antes de retry (exceto na última tentativa)
            if attempt < 3:
                logger.info("verify: nenhum botão encontrado na tentativa %d/3 — retentando em 2s", attempt)
                await asyncio.sleep(2.0)

        # Falhou em todas as 3 tentativas em todas as 3 camadas
        await self._dump_debug_html(page, "verify_enviar_sms_missing")
        logger.warning(
            "verify: botão 'Enviar SMS' não encontrado após 3 tentativas em 3 camadas — "
            "DOM salvo. O código pode não ter sido disparado."
        )
        return False

    async def _bv_fill_org_phone(self, page: Page) -> None:
        """Substitui 'Telefone da organização' pelo número sms24h DESTE run.

        O campo é editável (confirmado em produção) e é PARA ELE que a Meta
        manda o SMS. Sem trocar, a Meta usaria o número antigo do cadastro e o
        sms24h nunca receberia o código. Entra o número NACIONAL (sem o 55 — o
        'BR +55' é um seletor à parte).
        """
        digits = re.sub(r"\D+", "", self.preallocated_sms_phone or "")
        if len(digits) < 10:
            logger.warning(
                "verify: sem número sms24h pré-alocado — não troquei o "
                "telefone da organização (SMS pode ir pro número errado)."
            )
            return
        national = digits[-11:] if len(digits) >= 11 else digits  # tira o 55
        dialog = page.locator('[role="dialog"]')
        try:
            field = await self._first_visible(
                page,
                [
                    dialog.get_by_label(
                        re.compile(
                            r"telefone da organiza[çc][ãa]o|organization phone|"
                            r"telefone",
                            re.I,
                        )
                    ),
                    dialog.locator('input[type="tel"]'),
                    dialog.locator('input[inputmode="tel"]'),
                    dialog.get_by_role("textbox"),
                ],
                timeout_ms=5000,
            )
        except LoginError:
            await self._dump_debug_html(page, "verify_org_phone_field_missing")
            logger.warning(
                "verify: campo 'Telefone da organização' não encontrado — DOM "
                "salvo. SMS pode ir pro número do cadastro."
            )
            return
        # O campo vem PRÉ-PREENCHIDO e DESABILITADO com o número certo quando o
        # telefone salvo no passo 2 já é o número sms24h deste run (produção
        # 2026-06: <input disabled value='79926757548'>). Tentar clicar/preencher
        # um disabled trava 30s à toa — e o SMS já vai pro número correto.
        try:
            if not await field.is_enabled():
                current = re.sub(r"\D+", "", await field.input_value() or "")
                if current and current.endswith(national[-8:]):
                    logger.info(
                        "verify: telefone da organização já é o número sms24h "
                        "(%s, campo travado) — nada a trocar.", national,
                    )
                else:
                    logger.warning(
                        "verify: telefone da organização DESABILITADO com outro "
                        "número (%r) — re-alocando SMS24h para este número.",
                        current,
                    )
                    # CRÍTICO: Re-aloca SMS24h para o número que Meta REALMENTE vai usar
                    old_id = self.preallocated_sms_activation_id
                    old_phone = self.preallocated_sms_phone

                    if old_id:
                        try:
                            # Cancela alocação anterior (errada)
                            await self.sms_client.set_status(old_id, 8)
                            logger.info(
                                "SMS24h anterior cancelado (id=%s, phone=%s)",
                                old_id,
                                old_phone,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Erro ao cancelar SMS24h anterior: %s", exc
                            )

                    # Aguarda um pouco para SMS24h liberar a ativação no pool
                    await asyncio.sleep(2.0)

                    # Re-aloca para o número correto (com retry)
                    new_activation_id = None
                    new_phone = None
                    for attempt in range(1, 5):  # 4 tentativas em vez de 3
                        try:
                            new_activation_id, new_phone = (
                                await self.sms_client.acquire_number()
                            )
                            if new_phone and new_phone.endswith(current[-8:]):
                                logger.info(
                                    "SMS24h re-alocado com SUCESSO (tentativa %d/4): "
                                    "id=%s, phone=%s",
                                    attempt,
                                    new_activation_id,
                                    new_phone,
                                )
                                self.preallocated_sms_activation_id = new_activation_id
                                self.preallocated_sms_phone = new_phone
                                break
                            else:
                                logger.warning(
                                    "SMS24h alocado não bate (tentativa %d/4): "
                                    "pedido=%s, recebido=%s — tentando novamente.",
                                    attempt,
                                    current,
                                    new_phone,
                                )
                                # Aguarda um pouco antes de retry
                                await asyncio.sleep(1.0)
                        except Exception as exc:
                            logger.warning(
                                "Erro na re-alocação SMS24h (tentativa %d/4): %s",
                                attempt,
                                exc,
                            )
                            # Aguarda antes de retry mesmo em erro
                            if attempt < 4:
                                await asyncio.sleep(1.5)

                    if not new_phone or not new_phone.endswith(current[-8:]):
                        logger.error(
                            "FALHA: Re-alocação SMS24h não conseguiu numero correto "
                            "(querido=%s, ultido=%s). Usando número pré-registrado mesmo assim — "
                            "SMS pode não chegar.",
                            current,
                            new_phone,
                        )
                        # Fallback: volta ao número antigo se tinha (mesmo que errado)
                        # para não deixar o fluxo travado
                        if old_id and old_phone:
                            logger.info(
                                "Fallback: reutilizando ativação anterior (id=%s, phone=%s)",
                                old_id,
                                old_phone,
                            )
                            self.preallocated_sms_activation_id = old_id
                            self.preallocated_sms_phone = old_phone

                await self._organic_wait()
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            await field.fill(national)  # fill limpa o antigo e escreve o novo
            logger.info(
                "verify: telefone da organização trocado para o número sms24h "
                "(%s) — SMS virá pro número que o bot monitora.", national,
            )
        except Exception:  # noqa: BLE001 — input mascarado: limpa e digita
            try:
                await field.click()
                await field.fill("")
                await self._press_sequentially(field, national)
                logger.info(
                    "verify: telefone da organização preenchido via digitação "
                    "(%s).", national,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "verify: falha ao trocar o telefone da organização (%s) — "
                    "SMS pode ir pro número errado.", exc,
                )
        await self._organic_wait()

    async def _bv_submit_verification(
        self, page: Page, *, wait_enabled_s: float = 60.0
    ) -> bool:
        """Clica o botão que SUBMETE o documento (Enviar/Concluir/Avançar).

        Espera habilitar e clica. O 'Avançar' fica ``aria-disabled=true``
        enquanto o Meta PROCESSA o upload do PDF (sobe pro servidor + preview)
        — 25s não bastava (produção 2026-06, conta Daniel: dump mostrou
        Avançar role=button aria-disabled após o anexo). 60s dá margem.
        Espelha _bv_advance, mas cobre também 'enviar/submit/concluir'.
        """
        dialog = page.locator('[role="dialog"]')
        submit_re = re.compile(
            r"^\s*(enviar|submit|concluir|done|avan[cç]ar|continuar|continue|"
            r"next|finalizar|finish)\s*$",
            re.I,
        )
        btn = dialog.get_by_role("button", name=submit_re)
        deadline = time.monotonic() + max(wait_enabled_s, 0.0)
        announced = False
        while True:
            try:
                if await btn.first.is_visible(timeout=800) and await btn.first.is_enabled(
                    timeout=500
                ):
                    await self._robust_click(btn.first)
                    logger.info("verify: documento SUBMETIDO (botão de envio).")
                    await self._organic_wait()
                    return True
            except Exception:  # noqa: BLE001
                pass
            if not announced:
                logger.info("verify: aguardando o botão de submeter habilitar (até 25s).")
                announced = True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(1.5)

    async def _bv_dialog_has_text(self, page: Page, pattern: str) -> bool:
        """True se o diálogo ativo contém texto casando ``pattern`` (rápido)."""
        rx = re.compile(pattern, re.I)
        dialog = page.locator('[role="dialog"]')
        try:
            return await dialog.get_by_text(rx).first.is_visible(timeout=800)
        except Exception:  # noqa: BLE001
            return False

    async def _bv_any_dialog_visible(self, page: Page) -> bool:
        """True se QUALQUER diálogo modal está visível (transições da esteira)."""
        try:
            return await page.locator('[role="dialog"]').first.is_visible(
                timeout=600
            )
        except Exception:  # noqa: BLE001
            return False

    async def _bv_click_concluir(self, page: Page) -> bool:
        """Clica 'Concluir'/'Done' na tela TERMINAL ('Informações enviadas')."""
        dialog = page.locator('[role="dialog"]')
        concluir_re = re.compile(
            r"^\s*(concluir|done|finalizar|finish|ok|fechar|close)\s*$", re.I
        )
        try:
            btn = await self._first_visible(
                page,
                [
                    dialog.get_by_role("button", name=concluir_re),
                    page.get_by_role("button", name=concluir_re),
                ],
                timeout_ms=6000,
            )
        except LoginError:
            logger.info(
                "verify: 'Concluir' não encontrado na tela final (pode ter "
                "fechado sozinho)."
            )
            return False
        await self._robust_click(btn)
        logger.success(
            "verify: 'Concluir' clicado — verificação ENVIADA (análise da Meta "
            "~2 dias úteis)."
        )
        await self._organic_wait()
        return True

    async def _bv_finalize(self, page: Page) -> bool:
        """Completa as telas FINAIS de confirmação da CONEXÃO com a empresa.

        Após o código SMS, a Meta pede para confirmar a conexão com a empresa
        (prints do operador, conta Thiago 2026-06):
          (A) 'Escolha como gostaria de confirmar sua conexão com <empresa>' —
              opções 'Email' e 'Verificação de domínio'; o 'Avançar' só
              habilita após escolher. Escolhemos DOMÍNIO (já verificado no
              passo 3).
          (B) 'Verifique com o domínio da empresa' (domínio ✓ verificado) →
              Avançar.
          (C) 'Informações enviadas' (análise ~2 dias) → Concluir. ← TERMINAL.

        Antes o bot só clicava 1 botão genérico em 4s e logava SUBMETIDA cedo
        demais (o operador clicava A/B/C na mão). Agora roda uma máquina de
        estados e só devolve True ao alcançar a tela TERMINAL (ou ao diálogo
        fechar APÓS tratar ≥1 tela) — sucesso HONESTO. DOM salvo se travar.
        """
        a_re = (
            r"como.*confirmar sua conex[ãa]o|confirmar sua conex|"
            r"verifique com um documento|n[ãa]o foi poss[ií]vel encontrar sua empresa|"
            r"how.*confirm your connection|verify with document"
        )
        b_re = (
            r"verifique com o dom[íi]nio|dom[íi]nio.*verificad|"
            r"verify.*with.*domain"
        )
        terminal_re = (
            r"informa[çc][õo]es enviadas|agradecemos o envio|an[áa]lise.*dias|"
            r"reviewed within|thanks for submitting|received your"
        )
        handled_any = False
        for _ in range(14):
            # TERMINAL primeiro — 'Informações enviadas' → Concluir → FIM.
            if await self._bv_dialog_has_text(page, terminal_re):
                await self._bv_click_concluir(page)
                return True
            # (A) método de confirmação da conexão → 'Verificação de domínio'.
            if await self._bv_dialog_has_text(page, a_re):
                await self._bv_select_option(
                    page,
                    r"verifica[çc][ãa]o de dom[íi]nio|usar.*dom[íi]nio|"
                    r"domain verification|use.*domain",
                    step="conexao_metodo",
                )
                page = await self._bv_advance(page, "conexao_metodo")
                handled_any = True
                await self._organic_wait()
                continue
            # (B) confirmar com o domínio verificado → Avançar.
            if await self._bv_dialog_has_text(page, b_re):
                page = await self._bv_advance(page, "conexao_dominio")
                handled_any = True
                await self._organic_wait()
                continue
            # Sem diálogo visível: se já tratamos ≥1 tela, o fluxo concluiu.
            if not await self._bv_any_dialog_visible(page):
                if handled_any:
                    logger.success(
                        "verify: diálogo de verificação fechou após as telas de "
                        "conexão — verificação enviada."
                    )
                    return True
                await asyncio.sleep(1.2)
                continue
            # Diálogo DESCONHECIDO visível: tenta um avanço genérico e re-checa.
            if not await self._bv_advance(
                page, "conexao_desconhecida", wait_enabled_s=6.0
            ):
                await asyncio.sleep(1.0)
        await self._dump_debug_html(page, "verify_finalize_no_terminal")
        logger.warning(
            "verify: telas finais de conexão NÃO chegaram em 'Informações "
            "enviadas' em ~30s — DOM salvo p/ calibração."
        )
        return False

    async def _bv_upload_document(self, page: Page) -> bool:
        """Anexa o PDF do comprovante na tela de upload — SEM submeter.

        Lê o caminho de ``saga.artifacts['cnpj_card_path']`` (gravado por
        _bv_prepare_comprovante neste run — nunca glob). Estratégia:
          1. ``set_input_files`` direto no ``input[type=file]`` (funciona mesmo
             oculto atrás de um botão estilizado, sem abrir o diálogo do SO).
          2. Fallback: ``expect_file_chooser`` ao clicar 'Carregar/Selecionar'.
        NÃO clica em Avançar/Enviar — o operador valida o documento na janela.
        """
        pdf_path = (
            self.saga.artifacts.get("cnpj_card_path") if self.saga else None
        )
        if not pdf_path or not Path(pdf_path).is_file():
            logger.warning(
                "verify: PDF do comprovante ausente/inexistente na saga "
                "(%r) — upload pulado.", pdf_path,
            )
            return False

        dialog = page.locator('[role="dialog"]')
        # 1) input[type=file] direto (escopo diálogo, depois page-wide).
        for scope in (dialog, page):
            try:
                file_input = scope.locator('input[type="file"]')
                if await file_input.count() > 0:
                    await file_input.first.set_input_files(pdf_path)
                    logger.info(
                        "verify: comprovante anexado via input[type=file] (%s).",
                        pdf_path,
                    )
                    await self._organic_wait()
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("verify: set_input_files direto falhou (%s).", exc)

        # 2) Fallback: file chooser ao clicar no botão de upload.
        upload_btn_re = re.compile(
            r"carregar|selecionar arquivo|enviar arquivo|adicionar arquivo|"
            r"escolher arquivo|upload|choose file|add file",
            re.I,
        )
        try:
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                btn = await self._first_visible_scan(
                    page,
                    [
                        dialog.get_by_role("button", name=upload_btn_re),
                        page.get_by_role("button", name=upload_btn_re),
                        page.get_by_text(upload_btn_re),
                    ],
                    rounds=4,
                )
                await self._robust_click(btn)
            chooser = await fc_info.value
            await chooser.set_files(pdf_path)
            logger.info(
                "verify: comprovante anexado via file chooser (%s).", pdf_path
            )
            await self._organic_wait()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: não consegui anexar o PDF (%s).", exc)
            return False

    async def _bv_account_ineligible(self, page: Page) -> bool:
        """True se a Central de Segurança serviu o card de INELEGIBILIDADE.

        Meta mostra 'Sua organização não precisa ser verificada' (ineligible_
        card_v2) quando a conta não qualifica — em geral porque o setup a
        montante está incompleto (dados da empresa ausentes, domínio não
        verificado, portfólio sem ativo). NÃO é bug de seletor: o botão
        simplesmente não existe.
        """
        ineligible_re = re.compile(
            r"n[aã]o precisa ser verificada|"
            r"does(?:n't| not) need to be verified|"
            r"n[aã]o (?:est[aá] )?qualificad|not eligible|ineligible",
            re.I,
        )
        try:
            loc = page.get_by_text(ineligible_re).first
            return await loc.is_visible(timeout=5000)
        except Exception:  # noqa: BLE001
            return False

    # ── Action Needed helpers (Central de Segurança) ────────────────

    async def _bv_resolve_action_needed(self, page: Page) -> None:
        """Resolve 'Action needed' on Security Center before verification.

        Best-effort: failures log a warning and the verification proceeds.
        """
        action_re = re.compile(r"action needed|a[çc][ãa]o necess", re.I)
        try:
            el = page.get_by_text(action_re).first
            if not await el.is_visible(timeout=3000):
                return
        except Exception:  # noqa: BLE001
            return

        logger.info(
            "verify: 'Action needed' detectada na Central de Segurança "
            "— resolvendo antes de iniciar a verificação."
        )
        await self._bv_action_add_trusted_domain(page)
        await self._bv_action_ad_account_protection(page)

        try:
            await page.reload(wait_until="domcontentloaded")
            await self._organic_wait()
        except Exception:  # noqa: BLE001
            pass

    async def _bv_find_agir_near_text(
        self, page: Page, text_re: "re.Pattern[str]",
    ) -> Optional[Locator]:
        """Return the 'Agir' button in the same visual row as *text_re*."""
        agir_re = re.compile(r"^agir$", re.I)
        try:
            text_el = page.get_by_text(text_re).first
            if not await text_el.is_visible(timeout=2000):
                return None
            text_box = await text_el.bounding_box()
            if not text_box:
                return None
        except Exception:  # noqa: BLE001
            return None

        text_cy = text_box["y"] + text_box["height"] / 2
        agir_buttons = page.get_by_role("button", name=agir_re)
        count = await agir_buttons.count()
        best: Optional[Locator] = None
        best_dist = 80.0
        for i in range(count):
            btn = agir_buttons.nth(i)
            try:
                if not await btn.is_visible(timeout=500):
                    continue
                box = await btn.bounding_box()
                if not box:
                    continue
                dist = abs(box["y"] + box["height"] / 2 - text_cy)
                if dist < best_dist:
                    best_dist = dist
                    best = btn
            except Exception:  # noqa: BLE001
                continue
        return best

    async def _bv_action_add_trusted_domain(self, page: Page) -> None:
        """Action needed: add the CNPJ domain to ad-account Domain Security."""
        domain = (
            (self.business_profile.domain or "").strip()
            if self.business_profile else ""
        )
        if not domain:
            return
        domain = re.sub(r"^https?://", "", domain).rstrip("/")

        domain_text_re = re.compile(
            r"dom[ií]nio.*confi[áa]ve|adicione dom[ií]nio|"
            r"trusted.*domain|add.*trusted",
            re.I,
        )
        btn = await self._bv_find_agir_near_text(page, domain_text_re)
        if not btn:
            logger.info("verify: ação 'Domínio confiável' não encontrada — pulando.")
            return

        logger.info("verify: resolvendo 'Domínio confiável' (domain=%s).", domain)
        await self._robust_click(btn)
        await asyncio.sleep(1.5)

        input_loc = page.locator(
            'input[placeholder*="Domain" i], '
            'input[placeholder*="domínio" i], '
            'input[placeholder*="meta.com"]'
        ).first
        try:
            await input_loc.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            logger.warning("verify: modal Domain Security não abriu — pulando.")
            return

        await input_loc.fill(domain)
        await input_loc.press("Tab")
        await asyncio.sleep(2.0)

        domains_btn_re = re.compile(r"domain\(?s?\)?$", re.I)
        domains_btn = page.get_by_role("button", name=domains_btn_re).first
        try:
            await domains_btn.wait_for(state="visible", timeout=3000)
            await self._robust_click(domains_btn)
        except Exception:  # noqa: BLE001
            try:
                fb = page.locator('button, [role="button"]').filter(
                    has_text=re.compile(r"domain", re.I)
                ).first
                await self._robust_click(fb)
            except Exception:  # noqa: BLE001
                logger.warning("verify: botão 'Domain(s)' não clicou — pulando.")
                return
        await asyncio.sleep(2.0)

        close_re = re.compile(r"^fechar$|^close$", re.I)
        try:
            close_btn = page.get_by_role("button", name=close_re).first
            await close_btn.wait_for(state="visible", timeout=3000)
            await self._robust_click(close_btn)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
        logger.success("verify: domínio confiável '%s' adicionado.", domain)

    async def _bv_action_ad_account_protection(self, page: Page) -> None:
        """Action needed: set 'Proteção extra' on the ad account."""
        ad_text_re = re.compile(
            r"conta de an[uú]ncio.*seguran|n[ií]ve.*prote[çc]|"
            r"ad account.*security|protection.*level",
            re.I,
        )
        btn = await self._bv_find_agir_near_text(page, ad_text_re)
        if not btn:
            logger.info(
                "verify: ação 'Segurança da conta de anúncios' não encontrada "
                "— pulando."
            )
            return

        logger.info("verify: resolvendo 'Segurança da conta de anúncios'.")
        await self._robust_click(btn)
        await asyncio.sleep(1.5)

        dropdown_re = re.compile(r"conta de an[uú]ncio|ad account", re.I)
        dropdown = page.locator(
            '[role="combobox"], [role="listbox"], '
            '[aria-haspopup="listbox"], [aria-haspopup="true"]'
        ).first
        try:
            await dropdown.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            dropdown = page.get_by_text(dropdown_re).first
        try:
            await self._robust_click(dropdown)
        except Exception:  # noqa: BLE001
            logger.warning("verify: dropdown de conta de anúncios não clicou — pulando.")
            return
        await asyncio.sleep(1.0)

        option = page.locator(
            '[role="option"], [role="menuitem"], '
            '[role="menuitemradio"]'
        ).first
        try:
            await option.wait_for(state="visible", timeout=5000)
            await self._robust_click(option)
        except Exception:  # noqa: BLE001
            logger.warning("verify: nenhuma opção no dropdown — pulando.")
            return
        await asyncio.sleep(1.5)

        extra_re = re.compile(r"prote[çc][ãa]o extra|extra protection", re.I)
        extra_opt = page.get_by_text(extra_re).first
        try:
            await extra_opt.wait_for(state="visible", timeout=5000)
            await self._robust_click(extra_opt)
        except PlaywrightTimeoutError:
            logger.warning("verify: opção 'Proteção extra' não encontrada — pulando.")
            return
        await asyncio.sleep(0.5)

        save_re = re.compile(r"^salvar$|^save$", re.I)
        try:
            save_btn = page.get_by_role("button", name=save_re).first
            await save_btn.wait_for(state="visible", timeout=3000)
            await self._robust_click(save_btn)
        except Exception:  # noqa: BLE001
            logger.warning("verify: botão 'Salvar' não encontrado — pulando.")
            return
        await asyncio.sleep(2.0)

        concluir_re = re.compile(r"^concluir$|^done$", re.I)
        try:
            c_btn = page.get_by_role("button", name=concluir_re).first
            await c_btn.wait_for(state="visible", timeout=5000)
            await self._robust_click(c_btn)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
        logger.success("verify: 'Proteção extra' aplicada à conta de anúncios.")

    # Quantos ciclos de (procurar + recarregar a Central) antes de desistir do
    # botão 'Iniciar verificação' e cair no fallback (criar WABA). 3 ciclos com
    # scan/espera enxutos dão ~90s no MÁXIMO (pedido do operador 2026-09-27: não
    # perder tempo — se em ~2min o botão não liberar, parte pra WABA).
    _BV_START_RELOAD_CYCLES: int = 3

    async def _bv_start_verification(self, page: Page) -> Optional[Page]:
        """Abre o assistente de verificação — 'Iniciar' (1ª vez) OU
        'Continuar'/'Retomar' (verificação começada num run anterior e
        interrompida antes de concluir — pedido do operador 2026-06: tem
        que RETOMAR, não pular e seguir como se nada tivesse acontecido).

        Se nenhum dos dois aparecer na Central de Segurança, tenta o ponto
        de entrada alternativo na página de Contas do WhatsApp Business —
        quando a WABA já existe, ela também oferece um botão de iniciar/
        continuar a verificação.

        Devolve a ``Page`` onde o assistente está — pode ser uma ABA NOVA:
        'Iniciar/Continuar verificação' às vezes abre o wizard em popup em
        vez de modal na mesma aba (achado em produção 2026-06 — 4 dumps de
        diagnóstico consecutivos com HASH IDÊNTICO, zero `role="dialog"` e
        só o botão 'Iniciar verificação' ainda visível: o bot continuava
        clicando/checando na aba ANTIGA, parada, enquanto o operador via o
        assistente de verdade rodando normal em OUTRA aba). Sem detectar o
        popup, o caller seguia preenchendo uma aba morta — daí "tipo_
        empresa"/"tem_registro"/identificação fiscal" nunca eram achados e
        o botão de avanço nunca habilitava: não tinha NADA ali, o
        assistente real estava em outro lugar. Devolve ``None`` se não
        conseguiu abrir de nenhuma forma."""
        start_re = re.compile(
            r"iniciar verifica[cç][aã]o|start verification|"
            r"continuar verifica[cç][aã]o|continue verification|"
            r"retomar verifica[cç][aã]o|resume verification",
            re.I,
        )
        bare_continue_re = re.compile(r"^continuar$|^continue$", re.I)

        async def _click_and_resolve_page(locator) -> Page:
            """Clica o botão e devolve a Page certa pra seguir operando —
            a do popup se abriu um, senão a mesma de sempre."""
            try:
                async with page.expect_popup(timeout=6000) as pop_info:
                    await self._robust_click(locator)
                popup = await pop_info.value
                try:
                    await popup.wait_for_load_state(
                        "domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.info(
                    "verify: assistente abriu em ABA NOVA — bot passa a "
                    "operar nela (era a causa do 'nada encontrado' em série)."
                )
                return popup
            except PlaywrightTimeoutError:
                # Sem popup — clique já aconteceu, navegou na mesma aba.
                await self._organic_wait()
                return page

        # A CTA 'Iniciar verificação' às vezes NÃO renderiza logo após criar a
        # conta de anúncios — a Meta leva um tempo pra calcular a elegibilidade
        # (produção 2026-09, conta Suzi Martins: ausente por 22s na BM#1,
        # presente na hora na BM#2; o DOM salvo confirmou 0 ocorrências do
        # botão, não era seletor). Só esperar na MESMA página não resolve: o
        # React não re-busca a elegibilidade sozinho. Reforço: procura ~15s; se
        # não achar, RECARREGA a Central (page.reload → re-fetch da
        # elegibilidade) e tenta de novo, até N ciclos, ANTES do fallback WABA.
        start_locators = [
            page.get_by_role("button", name=start_re),
            page.get_by_role("link", name=start_re),
            page.get_by_text(start_re),
            page.locator('[role="button"]').filter(has_text=start_re),
            page.locator('a').filter(has_text=start_re),
            page.locator('div').filter(has_text=start_re).locator('button').first,
            page.locator('xpath=//button[contains(text(), "Iniciar") or contains(text(), "Continuar") or contains(text(), "Retomar")]').first,
            page.locator('xpath=//a[contains(text(), "Iniciar") or contains(text(), "Continuar") or contains(text(), "Retomar")]').first,
            page.get_by_role("button", name=bare_continue_re),
            page.get_by_role("link", name=bare_continue_re),
            page.locator('[role="button"]').filter(has_text=bare_continue_re),
        ]
        for cycle in range(1, self._BV_START_RELOAD_CYCLES + 1):
            try:
                logger.info(
                    "verify: procurando botão 'Iniciar verificação' ou "
                    "'Continuar' (ciclo %d/%d, ~9s)…",
                    cycle, self._BV_START_RELOAD_CYCLES,
                )
                btn = await self._first_visible_scan(
                    page, start_locators, rounds=6, round_wait_s=1.5,
                )
                logger.info("verify: botão encontrado! Clicando…")
                target = await _click_and_resolve_page(btn)
                logger.info(
                    "verify: assistente de verificação aberto (Central de "
                    "Segurança)."
                )
                return target
            except LoginError as e:
                if cycle < self._BV_START_RELOAD_CYCLES:
                    logger.info(
                        "verify: botão ainda não disponível (elegibilidade pode "
                        "não ter propagado) — RECARREGANDO a Central de "
                        "Segurança e aguardando (ciclo %d/%d).",
                        cycle, self._BV_START_RELOAD_CYCLES,
                    )
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass
                    await self._detect_and_close_profile_modal(page, timeout_ms=3000)
                    await asyncio.sleep(3.0)
                else:
                    logger.warning(
                        "verify: botão 'Iniciar verificação'/'Continuar' não "
                        "apareceu após %d ciclos com reload (%s) — indo ao "
                        "fallback.",
                        self._BV_START_RELOAD_CYCLES, e,
                    )

        # Fallback: ponto de entrada via Contas do WhatsApp Business. Sem
        # isso, uma verificação iniciada num run anterior (e que não mostra
        # mais 'Iniciar' na Central, só 'Continuar' em outro lugar, ou nem
        # isso) era simplesmente pulada — o run seguia pro resto do pipeline
        # sem nunca completar o passo 5 (achado em produção 2026-06).
        logger.info(
            "verify: botão de iniciar/continuar não achado na Central de "
            "Segurança — tentando o ponto de entrada via Contas do "
            "WhatsApp Business."
        )
        try:
            await page.goto(BUSINESS_WHATSAPP_URL, wait_until="domcontentloaded")
            await self._organic_wait()
            btn = await self._first_visible_scan(
                page,
                [
                    page.get_by_role("button", name=start_re),
                    page.get_by_role("link", name=start_re),
                    page.get_by_text(start_re),
                    page.get_by_role("button", name=bare_continue_re),
                    page.get_by_role("link", name=bare_continue_re),
                ],
                rounds=10,
                round_wait_s=1.0,
            )
            target = await _click_and_resolve_page(btn)
            logger.info(
                "verify: assistente de verificação aberto via Contas do "
                "WhatsApp Business (fallback)."
            )
            return target
        except LoginError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "verify: fallback via Contas do WhatsApp Business falhou (%s).",
                exc,
            )
            return None

    async def _bv_fill_tax_id(self, page: Page) -> None:
        """Preenche o CNPJ com MÁXIMA ROBUSTEZ - múltiplas estratégias agressivas.

        - 10 estratégias diferentes de localização
        - 7 diferentes formas de preenchimento
        - Retry automático até conseguir
        - Detecção e limpeza de popups antes
        """
        cnpj = (getattr(self.cnpj_data, "cnpj", "") or "").strip()
        if not cnpj:
            logger.warning("verify: CNPJ ausente no cnpj_data")
            return

        # Remove qualquer formatação existente
        cnpj_clean = re.sub(r"[^\d]", "", cnpj)
        if len(cnpj_clean) < 14:
            cnpj_clean = cnpj_clean.ljust(14, "0")

        cnpj_formatted = f"{cnpj_clean[:2]}.{cnpj_clean[2:5]}.{cnpj_clean[5:8]}/{cnpj_clean[8:12]}-{cnpj_clean[12:14]}"
        logger.info("verify: ========== INICIANDO PREENCHIMENTO DE CNPJ ==========")
        logger.info("verify: CNPJ: %s (formatado: %s)", cnpj, cnpj_formatted)

        # ========== PASSO 1: FECHAR QUALQUER POPUP ANTES ==========
        await self._detect_and_close_profile_modal(page, timeout_ms=5000)
        await asyncio.sleep(0.5)

        # ========== PASSO 2: LOCALIZAR O CAMPO COM 10 ESTRATÉGIAS ==========
        field = None
        strategies = []

        # E1: Seletor CSS exato para placeholder "Identificação Fiscal"
        strategies.append(("E1: CSS placeholder exato",
                          page.locator('input[placeholder*="Identificação Fiscal"]').first))

        # E2: get_by_placeholder com regex
        strategies.append(("E2: get_by_placeholder regex",
                          page.get_by_placeholder(re.compile(r"identifica[cç][aã]o fiscal|cnpj|registration|empresa", re.I)).first))

        # E3: Procura dentro de dialog por placeholder
        strategies.append(("E3: dialog.get_by_placeholder",
                          page.locator('[role="dialog"]').get_by_placeholder(re.compile(r"identifica|fiscal|cnpj", re.I)).first))

        # E4: XPath agressivo
        strategies.append(("E4: XPath agressivo",
                          page.locator('xpath=//input[contains(translate(@placeholder, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "identificação") or contains(@placeholder, "CNPJ")]').first))

        # E5: Procura por name/id contendo CNPJ
        strategies.append(("E5: name/id com CNPJ",
                          page.locator('input[name*="cnpj"], input[id*="cnpj"], input[name*="fiscal"], input[id*="fiscal"]').first))

        # E6: Dentro do dialog, qualquer input com aria-label
        strategies.append(("E6: dialog aria-label",
                          page.locator('[role="dialog"] input[aria-label*="fiscal"], [role="dialog"] input[aria-label*="CNPJ"]').first))

        # E7: Procura em containers com "Identificação Fiscal"
        strategies.append(("E7: dentro de container",
                          page.locator('div:has-text("Identificação Fiscal") input').first))

        # E8: Procura input que está antes de texto "Identificação"
        strategies.append(("E8: input antes de 'Identificação'",
                          page.locator('input:has-text("Identificação")').first))

        # E9: Qualquer input tipo text no dialog
        strategies.append(("E9: input[type=text] no dialog",
                          page.locator('[role="dialog"] input[type="text"]').first))

        # E10: Último recurso - qualquer input no dialog
        strategies.append(("E10: qualquer input no dialog",
                          page.locator('[role="dialog"] input').first))

        # O form 'Adicionar dados da empresa' renderiza alguns segundos APÓS o
        # avanço de 'tem_registro' — o scan único rodava cedo demais (~1s) e
        # dava 'NENHUMA estratégia encontrou' mesmo com o campo prestes a
        # aparecer (produção 2026-09, conta Suzi Quaresma: o campo só foi achado
        # ~3s depois, pelo _bv_fix_company_name). Sem CNPJ o 'Avançar' nunca
        # habilita e a run cascateia bloqueada por ~6min. Fix: POLL de até ~20s
        # re-rodando as 10 estratégias até o campo aparecer.
        deadline = time.monotonic() + 20.0
        probe = 0
        while field is None and time.monotonic() < deadline:
            probe += 1
            for strategy_name, locator in strategies:
                try:
                    if await locator.is_visible(timeout=500):
                        field = locator
                        logger.info(
                            "verify: ✅ %s FUNCIONOU! (tentativa %d)",
                            strategy_name, probe,
                        )
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "verify: %s falhou (%s)", strategy_name, type(e).__name__
                    )
            if field is None:
                await asyncio.sleep(1.5)

        if not field:
            logger.error(
                "verify: ❌ NENHUMA das 10 estratégias encontrou o campo de "
                "CNPJ após ~20s!"
            )
            await self._dump_debug_html(page, "verify_taxid_missing")
            return

        # ========== PASSO 3: PREENCHER COM ESTRATÉGIAS SIMPLES MAS EFETIVAS ==========
        success = False
        last_result = ""

        logger.info("verify: Tentando preencher CNPJ (apenas números: %s)…", cnpj_clean)

        # ESTRATÉGIA 1: JavaScript puro com NÚMEROS (melhor compatibilidade com máscaras)
        logger.info("verify: E1 - JavaScript com números puros…")
        try:
            await page.evaluate(f"""(field) => {{
                field.value = '{cnpj_clean}';
                field.dispatchEvent(new Event('input', {{ bubbles: true }}));
                field.dispatchEvent(new Event('change', {{ bubbles: true }}));
                field.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }}""", field)

            await asyncio.sleep(1.0)
            current = (await field.input_value()) or ""
            if len(current) >= 10:
                logger.info("verify: ✅ E1 FUNCIONOU! valor='%s' (%d chars)", current[:30], len(current))
                success = True
                last_result = current
        except Exception as e:
            logger.debug("verify: E1 falhou: %s", type(e).__name__)

        if success:
            logger.info("verify: ========== CNPJ PREENCHIDO COM SUCESSO ==========")
            return

        # ESTRATÉGIA 2: fill() com números puros (não deixa máscara interferir)
        logger.info("verify: E2 - fill() com números puros…")
        try:
            await field.clear()
            await asyncio.sleep(0.2)
            await field.fill(cnpj_clean)
            await asyncio.sleep(0.8)

            current = (await field.input_value()) or ""
            if len(current) >= 10:
                logger.info("verify: ✅ E2 FUNCIONOU! valor='%s' (%d chars)", current[:30], len(current))
                success = True
                last_result = current
        except Exception as e:
            logger.debug("verify: E2 falhou: %s", type(e).__name__)

        if success:
            logger.info("verify: ========== CNPJ PREENCHIDO COM SUCESSO ==========")
            return

        # ESTRATÉGIA 3: fill() com formatação (deixa Meta formatar)
        logger.info("verify: E3 - fill() com formatação…")
        try:
            await field.clear()
            await asyncio.sleep(0.2)
            await field.fill(cnpj_formatted)
            await asyncio.sleep(0.8)

            current = (await field.input_value()) or ""
            if len(current) >= 10:
                logger.info("verify: ✅ E3 FUNCIONOU! valor='%s' (%d chars)", current[:30], len(current))
                success = True
                last_result = current
        except Exception as e:
            logger.debug("verify: E3 falhou: %s", type(e).__name__)

        if success:
            logger.info("verify: ========== CNPJ PREENCHIDO COM SUCESSO ==========")
            return

        # ESTRATÉGIA 4: press_sequentially ULTRA LENTO (apenas números)
        logger.info("verify: E4 - press_sequentially lento (100ms delay)…")
        try:
            await field.clear()
            await asyncio.sleep(0.3)
            await field.click()
            await asyncio.sleep(0.1)
            await field.press_sequentially(cnpj_clean, delay=100)
            await asyncio.sleep(2.0)

            current = (await field.input_value()) or ""
            if len(current) >= 10:
                logger.info("verify: ✅ E4 FUNCIONOU! valor='%s' (%d chars)", current[:30], len(current))
                success = True
                last_result = current
        except Exception as e:
            logger.debug("verify: E4 falhou: %s", type(e).__name__)

        if success:
            logger.info("verify: ========== CNPJ PREENCHIDO COM SUCESSO ==========")
            return

        # ESTRATÉGIA 5: JavaScript com valor digitado lentamente (simula digitação)
        logger.info("verify: E5 - JavaScript com digitação simulada…")
        try:
            await field.clear()
            await asyncio.sleep(0.2)

            await page.evaluate(f"""async (field) => {{
                const value = '{cnpj_clean}';
                for (let i = 0; i < value.length; i++) {{
                    field.value = value.substring(0, i + 1);
                    field.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    await new Promise(r => setTimeout(r, 50));
                }}
                field.dispatchEvent(new Event('change', {{ bubbles: true }}));
                field.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }}""", field)

            await asyncio.sleep(1.0)
            current = (await field.input_value()) or ""
            if len(current) >= 10:
                logger.info("verify: ✅ E5 FUNCIONOU! valor='%s' (%d chars)", current[:30], len(current))
                success = True
                last_result = current
        except Exception as e:
            logger.debug("verify: E5 falhou: %s", type(e).__name__)

        # ========== VERIFICAÇÃO FINAL ==========
        if not success:
            logger.error("verify: ❌ FALHA - NENHUMA estratégia preencheu o CNPJ corretamente!")
            logger.error("verify: Última tentativa: '%s' (%d chars)", last_result[:30] if last_result else "[vazio]", len(last_result))
            await self._dump_debug_html(page, "verify_taxid_fill_failed")
        else:
            logger.info("verify: ========== CNPJ PREENCHIDO COM SUCESSO ==========")

    async def _bv_fill_legal_name(self, page: Page) -> None:
        """Preenche o campo 'Razão Social / Legal Name' se existir e estiver vazio.

        Some companies require a 'Legal Name' or 'Razão Social' field separate
        from 'Nome da Empresa' (business name). This ensures it's filled.
        """
        if not self.cnpj_data:
            return

        legal_name = self.cnpj_data.razao_social or ""
        if not legal_name.strip():
            logger.debug("verify: razão social ausente — não preenchendo legal name.")
            return

        # Remove prefixo numérico se existir
        legal_name = re.sub(r"^\d+[\.\s]*", "", legal_name).strip()
        legal_name = self._humanize_person_name(legal_name)

        if not legal_name:
            return

        dialog = page.locator('[role="dialog"]')
        legal_re = re.compile(
            r"raz[aã]o social|legal name|legal business name|nome legal", re.I
        )

        try:
            field = await self._first_visible(
                page,
                [
                    dialog.get_by_label(legal_re),
                    dialog.get_by_placeholder(legal_re),
                    page.get_by_label(legal_re),
                    page.get_by_placeholder(legal_re),
                ],
                timeout_ms=3000,
            )
        except LoginError:
            logger.debug("verify: campo 'Razão Social / Legal Name' não encontrado — pulando.")
            return

        try:
            current = (await field.input_value()) or ""
        except Exception:  # noqa: BLE001
            current = ""

        if current.strip():
            logger.info(
                "verify: 'Razão Social' já preenchida (%r) — mantida.",
                current.strip(),
            )
            return

        await self._human_type(field, legal_name)
        logger.info("verify: 'Razão Social' preenchida (%s).", legal_name)

    async def _bv_fix_company_name(self, page: Page) -> None:
        """Corrige o campo 'Nome da empresa' na tela 'Dados da empresa' do wizard BV.

        A Meta pré-preenche o campo com a razão social RAW da empresa, que
        para MEIs frequentemente inclui o prefixo numérico (raiz do CNPJ),
        ex.: "66.499.935 BRUNA EDUARDA DAVIES". O campo não pode ter esse
        número, e deve estar em Title Case. Usa ``_humanize_person_name`` —
        o mesmo pipeline que sanitiza o nome no portfólio.
        """
        if not self.cnpj_data:
            return
        correct_name = self._humanize_person_name(
            self.cnpj_data.nome_fantasia or self.cnpj_data.razao_social or ""
        )
        if not correct_name:
            return

        dialog = page.locator('[role="dialog"]')
        name_re = re.compile(r"nome da empresa|company name|business name", re.I)
        try:
            field = await self._first_visible(
                page,
                [
                    dialog.get_by_label(name_re),
                    dialog.get_by_placeholder(name_re),
                    page.get_by_label(name_re),
                    page.get_by_placeholder(name_re),
                ],
                timeout_ms=3000,
            )
        except LoginError:
            logger.debug("verify: campo 'Nome da empresa' não encontrado — pulando correção.")
            return

        try:
            current = (await field.input_value()) or ""
        except Exception:  # noqa: BLE001
            current = ""

        # REGRA DO OPERADOR (2026-09): o 'Nome da empresa' que a Meta pré-preenche
        # é a razão social oficial (ex.: '68.762.049 SUZI QUARESMA DOS SANTOS'),
        # que casa com o registro público consultado pelo CNPJ — NÃO deve ser
        # alterado (antes o bot tirava o número e mudava p/ Title Case, gerando
        # divergência do registro). Só preenche se, excepcionalmente, o campo
        # vier VAZIO.
        if current.strip():
            logger.info(
                "verify: 'Nome da empresa' já pré-preenchida (%r) — mantida "
                "como veio (regra do operador; só preenchemos o CNPJ).",
                current.strip(),
            )
            return

        logger.info(
            "verify: 'Nome da empresa' veio VAZIA — preenchendo com %r.",
            correct_name,
        )
        try:
            await field.click(click_count=3)  # Locator não tem triple_click
            await field.fill(correct_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: falha ao preencher 'Nome da empresa': %s", exc)

    async def _bv_solve_recaptcha_until_upload(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Resolve o reCAPTCHA 'Não sou um robô' e espera a tela de upload.

        Reusa os primitivos do passo 4 (``_wa_check_robot_box`` +
        ``_wa_try_2captcha``). O sinal de conclusão é a tela 'Carregar
        documentos' aparecer (humano OU 2captcha resolveu — o 1º a vir).
        """
        upload_re = re.compile(
            r"carregar documentos|upload documents|selecione um tipo de documento|"
            r"select a document type",
            re.I,
        )
        # Captcha pode nem aparecer (já estamos na tela de upload?).
        try:
            if await page.get_by_text(upload_re).first.is_visible(timeout=1500):
                return True
        except Exception:  # noqa: BLE001
            pass

        await self._wa_check_robot_box(page)
        await self._wa_try_2captcha(page, result)

        deadline = time.monotonic() + max(CAPTCHA_MANUAL_TIMEOUT_S, 0.0)
        announced = False
        while True:
            await self._dismiss_bv_exit_dialog(page)
            try:
                if await page.get_by_text(upload_re).first.is_visible(timeout=800):
                    logger.success(
                        "verify: captcha resolvido — tela 'Carregar documentos' "
                        "visível."
                    )
                    result.captcha_solved = True
                    return True
            except Exception:  # noqa: BLE001
                pass
            # 'Selecione sua empresa' (registros públicos) pode subir AQUI,
            # tarde no fluxo — DEPOIS que _bv_select_business_from_records já
            # passou (a consulta da Meta aos registros é lenta e assíncrona).
            # Sem marcar 'Minha empresa não está na lista' o 'Avançar' fica
            # desabilitado e este loop esperava a tela de upload eternamente,
            # estourando os 300s (produção 2026-09, conta Carla Taissa — DOM
            # do reCAPTCHA salvo mostrava a tela de registros na frente).
            # Marca a opção; o clique de 'Avançar' abaixo destrava e segue.
            try:
                await self._bv_click_empresa_nao_listada(page)
            except Exception:  # noqa: BLE001
                pass
            # Alguns fluxos exigem um 'Avançar' após marcar o checkbox.
            try:
                btn = self._bv_advance_button(page)
                if await btn.first.is_visible(timeout=400) and await btn.first.is_enabled(
                    timeout=300
                ):
                    await self._robust_click(btn.first)
                    await self._organic_wait()
            except Exception:  # noqa: BLE001
                pass
            if not announced:
                logger.info(
                    "verify: aguardando reCAPTCHA (corrida manual×2captcha, até "
                    "%.0fs) — resolva na janela do AdsPower se necessário.",
                    CAPTCHA_MANUAL_TIMEOUT_S,
                )
                announced = True
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(2.0)
        await self._dump_debug_html(page, "verify_recaptcha_unresolved")
        logger.warning(
            "verify: tela de upload não apareceu após o reCAPTCHA — DOM salvo."
        )
        return False

    async def _bv_select_doc_type_cnpj(self, page: Page) -> bool:
        """Abre o combobox 'tipo de documento' e seleciona CNPJ. PARA aqui."""
        dialog = page.locator('[role="dialog"]')
        upload_re = re.compile(
            r"carregar documentos|upload documents", re.I
        )
        type_re = re.compile(r"tipo de documento|document type", re.I)
        cnpj_re = re.compile(r"cadastro nacional|cnpj", re.I)
        # Confirma que estamos na tela de upload.
        try:
            await self._first_visible(
                page, [page.get_by_text(upload_re)], timeout_ms=4000
            )
        except LoginError:
            return False
        try:
            combo = await self._first_visible(
                page,
                [
                    dialog.get_by_label(type_re),
                    dialog.get_by_role("combobox", name=type_re),
                    dialog.get_by_role("combobox"),
                    dialog.locator('[role="button"]').filter(has_text=type_re),
                ],
                timeout_ms=4000,
            )
        except LoginError:
            return False
        await self._robust_click(combo)
        await self._organic_wait()
        try:
            opt = await self._first_visible_scan(
                page,
                [
                    page.get_by_role("option", name=cnpj_re),
                    page.get_by_role("menuitem", name=cnpj_re),
                    page.get_by_text(cnpj_re),
                ],
                rounds=6,
            )
            await self._robust_click(opt)
            logger.info("verify: tipo de documento 'CNPJ' selecionado.")
            await self._organic_wait()
            return True
        except LoginError:
            return False

    async def _handle_sms_verification(
        self, page: Page, result: AutomationResult
    ) -> None:
        """Conclui o desafio de SMS.

        Estratégia:
          1. Localiza o botão "Enviar código" e dispara o envio.
          2. Se o cliente de SMS estiver habilitado (provider+api_key), faz
             polling assíncrono até o código de N dígitos chegar, digita-o
             humanamente no input visível e confirma.
          3. Caso o provedor falhe (e ``SMS_API_FALLBACK_MANUAL=true``) ou
             a API esteja desabilitada, retorna ao comportamento original:
             aguarda o operador concluir no navegador.
        """
        try:
            # Timeout curto: o prompt, quando existe, já está renderizado ao
            # entrar na etapa — o default de 5s × 2 candidatos gastava 10s
            # cravados em TODO run sem prompt (a maioria).
            send_btn = await self._first_visible(
                page,
                [
                    page.get_by_role("button", name=re.compile(r"enviar c[oó]digo|send code|sms", re.I)),
                    page.get_by_text(re.compile(r"verificar por sms|verify by sms", re.I)),
                ],
                timeout_ms=5000,
            )
        except LoginError:
            logger.info("Nenhum prompt de validação por SMS detectado.")
            return

        await self._robust_click(send_btn)
        logger.info("Botão 'enviar código' clicado — preparando recepção do SMS.")

        if self.sms_client.enabled:
            ok = await self._verify_sms_via_api(page, result)
            if ok:
                return
            if not SMS_API_FALLBACK_MANUAL:
                logger.error("Falha na API de SMS e fallback manual desabilitado.")
                return
            logger.warning("Fallback para validação manual (operador no navegador).")

        await self._verify_sms_manual(page, result)

    async def _verify_sms_via_api(
        self, page: Page, result: AutomationResult
    ) -> bool:
        """Ciclo de vida completo do SMS via sms24h.org (handler_api).

        Etapas:
          1. ``acquire_number(service="fb", country="br")`` — reserva número.
          2. ``get_sms_code(activation_id)`` — loop até ``STATUS_OK:DDDDDD``.
          3. Extrai dígitos do payload via ``SMS_CODE_REGEX``.
          4. ``_press_sequentially(input, code)`` — digita preservando foco
             contra validação assíncrona do Meta.
          5. Click em "Confirmar/Verificar" + espera indicador de sucesso.
          6. **finally**: ``release(activation_id, success=...)`` rodando
             em QUALQUER caminho de saída — sucesso, erro esperado, ou
             exceção inesperada — para fechar a transação no provedor
             (cobra ou estorna saldo). Best-effort: ``release`` engole
             próprias exceções internamente para nunca mascarar a causa real.
        """
        activation_id: Optional[str] = None
        fallback_client = None
        fallback_activation_id: Optional[str] = None
        success = False
        try:
            if self.preallocated_sms_activation_id and self.preallocated_sms_phone:
                activation_id = self.preallocated_sms_activation_id
                phone = self.preallocated_sms_phone
                logger.info(
                    "Número PRÉ-ALOCADO pela UI reutilizado: id=%s, phone=%s",
                    activation_id, phone,
                )
            else:
                try:
                    activation_id, phone = await self.sms_client.acquire_number()
                except Exception as e:
                    # SMS24h inacessível ou outro erro ao alocar número
                    error_str = str(e).lower()
                    if "limited_activations" in error_str or "no_numbers" in error_str:
                        logger.warning(f"SMS24h sem números disponíveis (LIMITED_ACTIVATIONS) — abortando verificação")
                        raise InfrastructureError(
                            f"SMS24h sem números disponíveis (LIMITED_ACTIVATIONS) — tente novamente em instantes"
                        ) from e
                    raise InfrastructureError(
                        f"SMS24h falhou ao alocar número: {e}"
                    ) from e
                logger.info(
                    "Número adquirido no provedor (sms24h-compatible): id=%s, phone=%s",
                    activation_id, phone,
                )
                # Salva no runner para que o comprovante CNPJ (passo 5)
                # possa usar o mesmo número no campo Telefone do PDF.
                self.preallocated_sms_phone = phone
                self.preallocated_sms_activation_id = activation_id
                # PR4 — rastreia para compensação caso a esteira morra antes
                # do release do finally (ex.: kill do processo, crash fora
                # deste método).
                self._track_sms_activation(
                    activation_id, detail=f"adquirido na etapa 4/4 (phone={phone})"
                )
            # Se a UI exigir cadastro do número antes do envio, preenche-o.
            await self._maybe_fill_phone_field(page, phone)

            # CRÍTICO: Detecta qual número Meta REALMENTE vai usar (pode ser diferente
            # do que foi alocado) e re-aloca SMS24h para esse número.
            meta_phone = await self._detect_meta_phone_for_sms(page)
            if meta_phone and meta_phone != phone:
                logger.warning(
                    "Meta vai usar número DIFERENTE: %s (alocado %s). "
                    "Re-alocando SMS24h para o número correto.",
                    meta_phone, phone
                )
                # Cancela o número anterior
                try:
                    await self.sms_client.set_status(activation_id, 8)  # 8 = cancel
                except Exception:
                    pass

                # Aloca novo SMS24h para o número que Meta vai usar
                try:
                    # Extrai apenas dígitos
                    meta_digits = ''.join(c for c in meta_phone if c.isdigit())
                    # Tenta alocar SMS para esse número específico (re-tentativas se falhar)
                    for attempt in range(1, 4):
                        try:
                            new_id, new_phone = await self.sms_client.acquire_number()
                            new_digits = ''.join(c for c in new_phone if c.isdigit())
                            if new_digits == meta_digits:
                                activation_id = new_id
                                phone = new_phone
                                logger.success(
                                    "SMS24h re-alocado para número Meta: id=%s, phone=%s",
                                    activation_id, phone,
                                )
                                break
                        except Exception:
                            if attempt == 3:
                                logger.error(
                                    "Não consegui re-alocar SMS24h para %s — "
                                    "continuando com número anterior.", meta_phone
                                )
                except Exception as exc:
                    logger.warning("Erro ao re-alocar SMS24h: %s", exc)

            # ── Poll com timeout curto (SMS_RETRY_WAIT_S) ──────────────
            original_timeout = self.sms_client.poll_timeout_s
            self.sms_client.poll_timeout_s = SMS_RETRY_WAIT_S

            try:
                code_payload = await self._sms_poll_with_retry(
                    page, activation_id,
                )
            except _SmsFallbackNeeded:
                code_payload = None
            finally:
                self.sms_client.poll_timeout_s = original_timeout

            if code_payload is None and self.sms_fallback_option:
                # ── Fallback: 2ª provedora — REINICIAR BV ────────────
                # Em vez de tentar voltar pelo botão "Voltar" (instável),
                # prepara o 2º número e sinaliza para reiniciar toda a
                # verificação desde "Iniciar verificação" via reload da
                # página. O caller (_run_business_verification) captura
                # _BvSmsRestartNeeded e refaz o fluxo completo.
                logger.warning(
                    "Código não recebido após 2 tentativas na 1ª provedora — "
                    "preparando restart BV com fallback: %s",
                    self.sms_fallback_option,
                )
                try:
                    await self.sms_client.set_status(
                        activation_id, Sms24hClient.STATUS_CANCEL,
                    )
                except Exception:
                    pass

                restart_client = self._create_sms_client_for_option(
                    self.sms_fallback_option,
                )

                try:
                    restart_aid, restart_phone = (
                        await restart_client.acquire_number()
                    )
                except Exception as exc:
                    logger.error("Fallback SMS: falha ao adquirir número: %s", exc)
                    try:
                        await restart_client.aclose()
                    except Exception:
                        pass
                    return False

                logger.info(
                    "Fallback SMS: número adquirido id=%s phone=%s — "
                    "reiniciando BV com esse número.",
                    restart_aid, restart_phone,
                )

                self.preallocated_sms_phone = restart_phone
                self.preallocated_sms_activation_id = restart_aid
                self._sms_restart_client = restart_client
                self.sms_fallback_option = None

                await self._bv_prepare_comprovante()

                raise _BvSmsRestartNeeded()

            if code_payload is None:
                if not self.sms_fallback_option:
                    self._sms_erro2 = True
                return False

            # ── Extrai código do payload ────────────────────────────
            matches = SMS_CODE_REGEX.findall(code_payload or "")
            if not matches:
                logger.warning("Código não localizado no payload SMS: %r", code_payload)
                return False
            code = max(matches, key=len)

            active_client = fallback_client or self.sms_client
            active_aid = fallback_activation_id or activation_id

            seen_codes: set[str] = set()
            for attempt in range(1, BV_SMS_RESEND_ATTEMPTS + 1):
                seen_codes.add(code)
                logger.success(
                    "Código SMS recebido (%s) — tentativa %d/%d.",
                    code, attempt, BV_SMS_RESEND_ATTEMPTS,
                )
                if not await self._bv_fill_code(page, code):
                    return False
                if await self._bv_code_accepted(page):
                    result.sms_verified = True
                    success = True
                    if fallback_client and fallback_activation_id:
                        await fallback_client.set_status(
                            fallback_activation_id, Sms24hClient.STATUS_FINISH,
                        )
                    logger.success(
                        "Validação por SMS confirmada via sms24h (tentativa %d).",
                        attempt,
                    )
                    return True
                if attempt >= BV_SMS_RESEND_ATTEMPTS:
                    logger.warning(
                        "Código não confirmou após %d tentativa(s) — desistindo "
                        "do SMS.", attempt,
                    )
                    break
                logger.warning(
                    "Código não confirmou (diálogo ainda aberto) — clicando "
                    "'Reenviar código' e pedindo um novo código."
                )
                if not await self._bv_click_resend(page):
                    logger.warning(
                        "'Reenviar código' não encontrado — sem como re-tentar."
                    )
                    break
                rearmed = await active_client.set_status(active_aid, 3)
                if not rearmed:
                    logger.warning(
                        "Provedor recusou reenvio (status 3) — pode não entregar "
                        "um 2º código no mesmo número."
                    )
                next_code = await self._fetch_next_sms_code_on(
                    active_client, active_aid, seen_codes,
                )
                if not next_code:
                    logger.warning(
                        "Reenvio não trouxe um código NOVO — encerrando o loop."
                    )
                    break
                code = next_code

            if fallback_client and fallback_activation_id:
                await fallback_client.set_status(
                    fallback_activation_id, Sms24hClient.STATUS_CANCEL,
                )
            return False

        except _BvSmsRestartNeeded:
            # Fallback SMS ativado — reiniciar BV desde o início.
            # Cancela a ativação ORIGINAL (a do fallback fica para o restart).
            if activation_id is not None:
                try:
                    await self.sms_client.set_status(
                        activation_id, Sms24hClient.STATUS_CANCEL,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if self.saga is not None:
                        self.saga.settle_resource(
                            "sms_activation", activation_id, outcome="cancel",
                        )
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.sms_client.aclose()
            except Exception:  # noqa: BLE001
                pass
            # Zera para que o finally (que roda após re-raise) não
            # tente cancelar/fechar de novo.
            activation_id = None
            raise
        except (Sms24hError, LoginError, PlaywrightTimeoutError) as exc:
            logger.warning("Falha ao verificar SMS pela API: %s", exc)
            return False
        finally:
            # Encerramento de transação garantido em TODOS os caminhos de
            # saída — inclui retornos antecipados, exceções esperadas e
            # exceções inesperadas. ``release`` é best-effort (engole erros).
            if activation_id is not None:
                status_code = (
                    Sms24hClient.STATUS_FINISH if success
                    else Sms24hClient.STATUS_CANCEL
                )
                # set_status NUNCA levanta; o bool é o sucesso REAL no
                # provedor. False → recurso fica OPEN na saga e a
                # compensação re-tentará o cancelamento (desejado).
                released = await self.sms_client.set_status(
                    activation_id, status_code
                )
                if not released:
                    logger.warning(
                        "Release SMS NÃO confirmado (id=%s) — recurso permanece "
                        "compensável na saga; saldo pode não ter sido estornado.",
                        activation_id,
                    )
                if released:
                    logger.info(
                        "Release sms24h enviado (id=%s, success=%s, status=%s).",
                        activation_id, success, status_code,
                    )
                    # PR4 — settle SÓ quando set_status SUCEDEU (evita
                    # double-cancel) e em guarda própria: falha no settle não
                    # pode deixar o recurso OPEN após release confirmado.
                    try:
                        if self.saga is not None:
                            self.saga.settle_resource(
                                "sms_activation",
                                activation_id,
                                outcome="finish" if success else "cancel",
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("saga: settle falhou (id=%s): %s", activation_id, exc)
            # Fecha o pool de sockets do(s) cliente(s) — best-effort.
            for cl in (self.sms_client, fallback_client):
                if cl is None:
                    continue
                try:
                    await cl.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _detect_meta_phone_for_sms(self, page: Page) -> Optional[str]:
        """Detecta qual número Meta vai usar para enviar o SMS de verificação.

        Meta mostra: "Insira o código enviado para o número (48) 9917-7427"
        Extrai esse número para re-alocar SMS24h exatamente para esse número.
        """
        try:
            # Procura texto como "Insira o código enviado para o número (XX) XXXX-XXXX"
            dialog_text = await page.locator('[role="dialog"]').text_content()
            if not dialog_text:
                dialog_text = await page.text_content()

            if not dialog_text:
                return None

            # Regex para encontrar número no formato (XX) XXXX-XXXX ou variações
            matches = re.findall(r'\(\d{2}\)\s*\d{4,5}[- ]?\d{4}', dialog_text)
            if matches:
                logger.info("Meta phone detectado da página: %s", matches[0])
                return matches[0]
        except Exception as exc:
            logger.debug("Erro ao detectar número Meta: %s", exc)

        return None

    async def _bv_fill_code(self, page: Page, code: str) -> bool:
        """Acha o input do código (maxlength=6), digita e clica o 'Avançar' HABILITADO.

        Usa ``_human_type`` (keystrokes reais) pra disparar o onChange do React —
        sem isso o 'Avançar' fica desabilitado. O diálogo tem um 'Avançar' DECOY
        com aria-disabled e pode conter 'Enviar SMS'/'Continuar' residuais de
        telas anteriores no DOM — por isso clicamos via ``_bv_click_avancar``,
        que pega o 'Avançar' que de fato HABILITOU, não o 1º que casa um regex
        amplo (que era o bug: clicava o decoy e o código nunca submetia).
        Devolve False se o campo não for localizado (DOM salvo).
        """
        try:
            code_input = await self._first_visible(
                page,
                [
                    page.locator('input[autocomplete="one-time-code"]'),
                    page.locator('input[name="approvals_code"]'),
                    page.locator('input[maxlength="6"]'),
                    page.locator('input[name*="code" i]').first,
                    page.get_by_label(
                        re.compile(r"c[oó]digo de confirma|insira o c[oó]digo", re.I)
                    ),
                    page.locator('input[inputmode="numeric"]'),
                ],
            )
        except LoginError:
            await self._dump_debug_html(page, "bv_sms_code_input_missing")
            logger.warning("verify: campo do código SMS não localizado — DOM salvo.")
            return False
        await self._human_type(code_input, code)
        await self._organic_wait()
        if not await self._bv_click_avancar(page):
            logger.warning(
                "verify: nenhum 'Avançar' habilitou após digitar o código — o "
                "código pode não ter registrado ou estar incompleto."
            )
        return True

    async def _bv_click_avancar(self, page: Page) -> bool:
        """Clica o 'Avançar' HABILITADO do diálogo de código.

        Espera o botão habilitar (input controlado do React leva um tick) e
        PULA o decoy com aria-disabled='true'. Devolve True se clicou um botão
        habilitado; False se nenhum habilitou em ~8s.
        """
        dialog = page.get_by_role("dialog")
        deadline = asyncio.get_event_loop().time() + 8.0
        while asyncio.get_event_loop().time() < deadline:
            loc = dialog.get_by_role(
                "button", name=re.compile(r"avan[çc]ar|continuar|next", re.I)
            )
            try:
                n = await loc.count()
            except Exception:  # noqa: BLE001
                n = 0
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    if (await el.get_attribute("aria-disabled")) == "true":
                        continue
                    await self._robust_click(el)
                    logger.info(
                        "verify: 'Avançar' (habilitado) clicado no diálogo de código."
                    )
                    return True
                except Exception:  # noqa: BLE001
                    continue
            await asyncio.sleep(0.5)
        return False

    async def _bv_code_accepted(self, page: Page) -> bool:
        """True se o código foi ACEITO (o campo do código some).

        Usa o INPUT do código (``input[maxlength=6]`` — único e estável na
        página) como sinal, NÃO o diálogo por nome — que dava FALSO-POSITIVO:
        'locator não casou' = 'hidden' = falso 'aceito' (o bot dizia SUBMETIDA
        sem o código ter passado). Aceito = o campo sumiu (verificação
        avançou). Recusado = o campo permanece (ou erro explícito aparece).
        Espera curta (não os 600s do SMS) pra liberar o loop de reenvio.
        """
        code_input = page.locator('input[maxlength="6"]').first
        # Palavras-chave de rejeição (em qualquer posição do texto do diálogo):
        # 'incorreto', 'inválido', 'expirou', 'errado', 'tente novamente'…
        err_re = re.compile(
            r"incorret|inv[aá]lid|expir|errad|tente novamente|try again|"
            r"incorrect|invalid",
            re.I,
        )
        deadline = asyncio.get_event_loop().time() + 25.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                visible = await code_input.is_visible(timeout=1500)
            except (PlaywrightTimeoutError, Exception):  # noqa: BLE001
                visible = False
            if not visible:
                return True  # campo sumiu → verificação avançou (aceito)
            try:
                body = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception:  # noqa: BLE001
                body = ""
            if err_re.search(body or ""):
                logger.warning("verify: código REJEITADO (erro explícito na tela).")
                return False
            await asyncio.sleep(1.0)
        logger.warning(
            "verify: campo do código ainda visível após a espera — não confirmou."
        )
        return False

    async def _bv_click_resend(self, page: Page) -> bool:
        """Clica 'Reenviar código' no diálogo. Devolve True se clicou."""
        try:
            link = await self._first_visible(
                page,
                [
                    page.get_by_text(
                        re.compile(r"reenviar c[oó]digo|resend code", re.I)
                    ),
                    page.get_by_role(
                        "button",
                        name=re.compile(r"reenviar c[oó]digo|resend code", re.I),
                    ),
                ],
                timeout_ms=5000,
            )
        except LoginError:
            return False
        await self._robust_click(link)
        logger.info("verify: 'Reenviar código' clicado — aguardando novo SMS.")
        await self._organic_wait()
        return True

    async def _fetch_next_sms_code(
        self, activation_id: str, seen_codes: set[str]
    ) -> Optional[str]:
        """Busca o PRÓXIMO código no provedor, diferente dos já tentados.

        ``get_sms_code`` pode devolver o código antigo em cache; só aceita um
        que ainda não foi visto (senão o reenvio não trouxe um SMS novo).
        """
        try:
            payload = await self.sms_client.get_sms_code(activation_id)
        except Sms24hError as exc:
            logger.warning("Sem novo código após reenvio: %s", exc)
            return None
        matches = SMS_CODE_REGEX.findall(payload or "")
        if not matches:
            return None
        code = max(matches, key=len)
        return None if code in seen_codes else code

    async def _sms_poll_with_retry(
        self, page: Page, activation_id: str,
    ) -> Optional[str]:
        """Poll com até 2 tentativas (timeout SMS_RETRY_WAIT_S cada).

        1ª tentativa: poll direto.
        2ª tentativa: clica "Enviar novamente" no Facebook e re-poll.
        Levanta ``_SmsFallbackNeeded`` se ambas expirarem.
        """
        for attempt in (1, 2):
            logger.info(
                "Polling SMS a cada %.1fs (timeout %.0fs) — tentativa %d/2…",
                self.sms_client.poll_interval_s,
                self.sms_client.poll_timeout_s,
                attempt,
            )
            try:
                return await self.sms_client.get_sms_code(activation_id)
            except Sms24hTimeoutError:
                logger.warning(
                    "Timeout SMS (tentativa %d/2, %.0fs).",
                    attempt, self.sms_client.poll_timeout_s,
                )
            if attempt == 1:
                clicked = await self._bv_click_resend(page)
                if not clicked:
                    logger.warning(
                        "'Enviar novamente' não encontrado — pulando para fallback."
                    )
                    raise _SmsFallbackNeeded()
                rearmed = await self.sms_client.set_status(activation_id, 3)
                if not rearmed:
                    logger.warning(
                        "Provedor recusou reenvio (status 3) — tentando poll mesmo assim."
                    )
        raise _SmsFallbackNeeded()

    async def _fetch_next_sms_code_on(
        self, client: Any, activation_id: str, seen_codes: set[str],
    ) -> Optional[str]:
        """Igual a _fetch_next_sms_code mas aceita qualquer cliente SMS."""
        try:
            payload = await client.get_sms_code(activation_id)
        except Sms24hError as exc:
            logger.warning("Sem novo código após reenvio: %s", exc)
            return None
        matches = SMS_CODE_REGEX.findall(payload or "")
        if not matches:
            return None
        code = max(matches, key=len)
        return None if code in seen_codes else code

    def _create_sms_client_for_option(self, option: str) -> Any:
        """Cria um cliente SMS para a opção dada (facebook/sms24h/smsrush)."""
        opt = (option or "").strip().lower()
        if opt == "sms24h":
            from core.api_clients import Sms24hOrgClient  # noqa: PLC0415
            return Sms24hOrgClient()
        if opt == "smsrush":
            from core.api_clients import SmsRushClient  # noqa: PLC0415
            kw: dict[str, Any] = {}
            if self.smsrush_server:
                kw["server"] = self.smsrush_server
            return SmsRushClient(**kw)
        from config import sms_kwargs_for_option  # noqa: PLC0415
        kw2: dict[str, Any] = {}
        if self.sms_ddd:
            kw2["allowed_ddds"] = frozenset([self.sms_ddd])
        kw2.update(sms_kwargs_for_option(option))
        return Sms24hClient(**kw2)

    async def _bv_go_back_to_phone_entry(self, page: Page) -> bool:
        """Tenta voltar à tela de número de telefone no diálogo do Facebook."""
        dialog = page.locator('[role="dialog"]')
        back_re = re.compile(
            r"voltar|back|alterar n[uú]mero|change.*(number|phone)|"
            r"editar|usar outro",
            re.I,
        )
        try:
            btn = await self._first_visible(
                page,
                [
                    dialog.get_by_role("button", name=back_re),
                    dialog.get_by_text(back_re),
                    page.get_by_role("button", name=back_re),
                ],
                timeout_ms=5000,
            )
            await self._robust_click(btn)
            logger.info("verify: botão 'Voltar' clicado — retornando à tela de telefone.")
            await asyncio.sleep(2.0)
            return True
        except LoginError:
            logger.warning(
                "verify: botão 'Voltar' não encontrado — tentando via navegação."
            )
        try:
            await page.go_back(timeout=5000)
            await asyncio.sleep(2.0)
            return True
        except Exception as exc:
            logger.warning("verify: page.go_back falhou (%s).", exc)
        return False

    async def _bv_resend_sms_on_page(self, page: Page) -> bool:
        """Clica 'Enviar SMS' no diálogo atual (reuso da camada de botões)."""
        dialog = page.locator('[role="dialog"]')
        sms_re = re.compile(
            r"^\s*(enviar\s+sms|send\s+sms|enviar\s+c[oó]digo|send\s+code|"
            r"receber\s+c[oó]digo|receive\s+code|get\s+code|text\s+me)\s*$",
            re.I,
        )
        generic_re = re.compile(
            r"^\s*(enviar|send|continuar|continue|avan[çc]ar|next|pr[óo]ximo|"
            r"confirmar|confirm)\s*$",
            re.I,
        )
        for candidates_re in (sms_re, generic_re):
            try:
                btn = await self._first_visible(
                    page,
                    [
                        dialog.get_by_role("button", name=candidates_re),
                        page.get_by_role("button", name=candidates_re),
                    ],
                    timeout_ms=4000,
                )
                await self._robust_click(btn)
                logger.info("verify: 'Enviar SMS' (re)clicado na 2ª provedora.")
                await self._organic_wait()
                return True
            except LoginError:
                continue
        logger.warning("verify: botão 'Enviar SMS' não encontrado na tela de fallback.")
        return False

    @staticmethod
    def _sms_success_locator(page: Page, post_login_indicator: str) -> "Locator":
        """Locator de sucesso pós-SMS: texto de confirmação OU indicador pós-login.

        NUNCA juntar ``text=/.../i`` com CSS numa única string de seletor: o
        Playwright trata o que vier após a flag (``i, div[role=...]``) como
        flags de regex e estoura 'Invalid flags supplied to RegExp constructor'
        — bug que derrubava o passo 5 logo após o código SMS ser digitado.
        Compõe via ``.or_()`` (engine text + engine CSS, sem misturar string).
        """
        return page.get_by_text(
            re.compile(r"verificad[oa]|verified|sucesso|conclu[ií]d", re.I)
        ).or_(page.locator(post_login_indicator))

    async def _verify_sms_manual(
        self, page: Page, result: AutomationResult
    ) -> None:
        logger.warning(
            "[WAIT] Aguardando inserção do código SMS na janela do navegador pelo "
            "operador… (timeout %.0fs)", SMS_VERIFICATION_TIMEOUT_S,
        )
        try:
            await self._sms_success_locator(
                page, self.POST_LOGIN_INDICATOR
            ).first.wait_for(
                state="visible",
                timeout=int(SMS_VERIFICATION_TIMEOUT_S * 1000),
            )
            result.sms_verified = True
            logger.success("Validação por SMS confirmada (manual).")
        except PlaywrightTimeoutError:
            logger.warning("Timeout aguardando validação por SMS — operador não concluiu.")

    @staticmethod
    async def _maybe_fill_phone_field(page: Page, phone: str) -> None:
        """Preenche um campo de telefone real se a UI de SMS o exigir antes do envio.

        Tenta 3 seletores em ordem de especificidade:
        1. input[type="tel"] (mais específico)
        2. input com name/aria-label/placeholder contendo "phone" ou "telefone"
        3. Qualquer input visível em diálogo de SMS

        Valida que é um <input> antes de digitar. Se não for, apenas loga debug.
        """
        try:
            phone_input = None

            # Seletor 1: input[type="tel"] (mais específico)
            try:
                candidates = await page.locator('input[type="tel"]').all()
                if candidates:
                    for cand in candidates:
                        if await cand.is_visible(timeout=500):
                            phone_input = cand
                            logger.debug("maybe_fill_phone_field: input[type='tel'] encontrado")
                            break
            except Exception:
                pass

            # Seletor 2: input com name/aria-label/placeholder contendo phone/telefone
            if not phone_input:
                try:
                    pattern = re.compile(r"phone|telefone", re.I)
                    candidates = await page.locator(
                        'input[name*="phone"], input[aria-label*="phone"], '
                        'input[placeholder*="phone"], '
                        'input[name*="telefone"], input[aria-label*="telefone"], '
                        'input[placeholder*="telefone"]'
                    ).all()
                    if candidates:
                        for cand in candidates:
                            if await cand.is_visible(timeout=500):
                                phone_input = cand
                                logger.debug("maybe_fill_phone_field: input com phone/telefone no atributo encontrado")
                                break
                except Exception:
                    pass

            # Seletor 3: get_by_label com padrão (fallback — ÚLTIMO)
            if not phone_input:
                try:
                    phone_input = page.get_by_label(
                        re.compile(r"telefone|phone|n[uú]mero", re.I)
                    ).first
                    if await phone_input.is_visible(timeout=500):
                        logger.debug("maybe_fill_phone_field: get_by_label encontrou elemento")
                    else:
                        phone_input = None
                except Exception:
                    phone_input = None

            # Se nenhum input foi encontrado
            if not phone_input:
                logger.debug("maybe_fill_phone_field: nenhum campo de telefone encontrado — pulando.")
                return

            # Valida que é um <input> real (não um <div> ou outro elemento)
            try:
                tag_name = await phone_input.evaluate("el => el.tagName.toLowerCase()")
                if tag_name != "input":
                    logger.debug(
                        "maybe_fill_phone_field: elemento encontrado é <%s>, não <input> — "
                        "não tentando digitar.", tag_name
                    )
                    return
            except Exception as exc:
                logger.debug("maybe_fill_phone_field: erro ao validar tag: %s", exc)
                return

            # Chegou aqui = é um <input> real e visível → preenche
            await phone_input.fill("")
            await phone_input.type(phone, delay=40)
            logger.info("Telefone preenchido via API SMS: %s", phone)

        except PlaywrightTimeoutError:
            logger.debug("maybe_fill_phone_field: timeout na visibilidade — pulando.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_fill_phone_field: erro inesperado err=%s: %s",
                type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # AdsPower — observações (credenciais salvas para o próximo acesso)
    # ------------------------------------------------------------------
    def _build_credentials_remark(self) -> str:
        """Monta o texto das Observações do perfil: login/senha/2FA.

        Fica salvo no painel AdsPower para o operador reentrar na conta
        manualmente quando preciso (ex.: desafio que exige humano)."""
        seed = (self.credentials.totp_seed or "").strip() or "—"
        lines = [
            f"login: {self.credentials.email_or_phone}",
            f"senha: {self.credentials.password}",
            f"2FA: {seed}",
        ]
        # Email da conta + senha do email + recuperação (quando vieram na combolist).
        email_addr = (self.credentials.email_address or "").strip()
        email_pwd = (self.credentials.email_password or "").strip()
        rec_email = (self.credentials.recovery_email or "").strip()
        if email_addr:
            lines.append(f"email: {email_addr}")
        if email_pwd:
            lines.append(f"senha email: {email_pwd}")
        if rec_email:
            lines.append(f"email recuperação: {rec_email}")
        return "\n".join(lines)

    async def _persist_credentials_remark(self) -> None:
        """Grava as credenciais nas Observações do perfil (best-effort)."""
        if not self.adspower_user_id:
            return
        ok = await self.adspower.update_profile(
            self.adspower_user_id, remark=self._build_credentials_remark()
        )
        if ok:
            logger.info(
                "AdsPower: credenciais salvas nas Observações do perfil %s.",
                self.adspower_user_id,
            )

    async def _mark_profile_in_analise(self) -> None:
        """Renomeia o perfil no AdsPower p/ "ANALISE - <cnpj>" quando a
        verificação da empresa é SUBMETIDA à Meta — fim do que o robô faz;
        a aprovação em si é assíncrona, fica "em análise" do lado da Meta.
        Facilita achar no painel quais perfis já terminaram e estão
        esperando aprovação. Best-effort: não derruba o run se falhar."""
        if not self.adspower_user_id:
            return
        cnpj_digits = self._profile_cnpj_digits()
        if not cnpj_digits:
            return
        new_name = f"ANALISE - {cnpj_digits}"
        ok = await self.adspower.update_profile(self.adspower_user_id, name=new_name)
        if not ok:
            logger.warning(
                "AdsPower: não consegui renomear o perfil %s para %r.",
                self.adspower_user_id, new_name,
            )
            return
        # CRÍTICO: atualiza o nome em cache p/ a GUARDA anti-sobrescrita valer.
        # Sem isso, uma 2ª BM que falha depois renomeava o perfil de 'ANALISE'
        # para 'ERRO ...', apagando a marca de que JÁ tem 1 BM em análise
        # (produção 2026-09-27: BM#1 em análise, BM#2 falhou → virou ERRO GERAL).
        self.adspower_profile_name = new_name
        from utils.profiles_state import set_profile_flag  # noqa: PLC0415

        _bp = self.browser_provider_name if self.browser_provider_name != "adspower" else None
        try:
            set_profile_flag(cnpj_digits, "name", new_name, provider=_bp)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Nome local do perfil não atualizado (%s).", exc)
        logger.success(
            "AdsPower: perfil %s renomeado para %r (em análise na Meta).",
            self.adspower_user_id, new_name,
        )

    # ------------------------------------------------------------------
    # Cliques resilientes
    # ------------------------------------------------------------------
    @staticmethod
    async def _robust_click(locator: Locator) -> None:
        """Click resiliente a overlays do Facebook.

        O FB cobre botões (inclusive dentro de modais ``role=dialog``) com
        camadas ``data-visualcompletion="ignore"`` que interceptam pointer
        events e fazem o click normal estourar 30s. Cascata em ordem de
        preferência: normal → force (ignora a interceptação de viewport) →
        JS ``el.click()`` (dispara o evento direto no elemento, à prova de
        overlay). Mesma estratégia já usada no submit de 2FA.
        """
        try:
            await locator.click(timeout=8000)
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "robust_click: click normal falhou (%s) — tentando force.",
                type(exc).__name__,
            )
        try:
            await locator.click(force=True, timeout=8000)
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "robust_click: force falhou (%s) — JS click (último recurso).",
                type(exc).__name__,
            )
        await locator.evaluate("el => el.click()")

    # ------------------------------------------------------------------
    # Human-like helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _human_type(
        locator: Locator, text: str, *, click_timeout_ms: Optional[int] = None
    ) -> None:
        # click_timeout_ms encurta o clique inicial nos campos onde um modal
        # pode interceptar (login): falha rápido em vez de esperar os 30 s
        # padrão do Playwright, para o chamador fechar o modal e re-tentar.
        if click_timeout_ms is not None:
            await locator.click(timeout=click_timeout_ms)
        else:
            await locator.click()
        # Clear without losing focus.
        try:
            await locator.fill("")
        except Exception:  # noqa: BLE001
            pass
        # Reancora o foco no input ANTES de digitar. O fill("") acima dispara o
        # onChange de inputs controlados (React) e o re-render pode ROUBAR o
        # foco por um instante; se o 1º keyboard.type cair nessa janela, o
        # primeiro caractere é engolido — sintoma observado em produção:
        # "61587773573746" digitado como "1587773573746", login bate
        # account_not_found ("Encontre sua conta") e a run morre no auth.
        try:
            await locator.focus()
        except Exception:  # noqa: BLE001
            pass
        keyboard = locator.page.keyboard
        for ch in text:
            delay_ms = random.randint(TYPING_DELAY_MIN_MS, TYPING_DELAY_MAX_MS)
            await keyboard.type(ch, delay=0)
            await asyncio.sleep(delay_ms / 1000)
        # Pós-condição (auto-cura): confere se o que ficou no campo bate com o
        # pretendido. Se uma validação assíncrona moveu o foco e comeu
        # caractere(s), corrige via fill direto — perde a cadência humana
        # naquele campo, mas é melhor um valor CERTO que um login derrubado por
        # dígito perdido. Loga só os tamanhos (nunca o valor — pode ser senha).
        try:
            got = await locator.input_value()
        except Exception:  # noqa: BLE001
            got = None
        if got is not None and got != text:
            logger.warning(
                "_human_type: valor do campo divergiu do esperado "
                "(obtido len=%d, esperado len=%d) — corrigindo via fill direto.",
                len(got), len(text),
            )
            try:
                await locator.fill(text)
            except Exception:  # noqa: BLE001
                # Último recurso: re-digita amarrado ao locator (reanchora por
                # caractere, sobrevive a foco instável).
                try:
                    await locator.fill("")
                except Exception:  # noqa: BLE001
                    pass
                await locator.press_sequentially(text, delay=0)

    @staticmethod
    async def _press_sequentially(locator: Locator, text: str) -> None:
        """Digita amarrado ao locator — usado em formulários cadastrais.

        Diferente do ``_human_type`` (que envia via ``page.keyboard.type`` e
        depende do foco global), ``locator.press_sequentially`` reposiciona
        o input antes de cada caractere, sobrevivendo a validações
        assíncronas do Meta que possam mover o foco mid-typing.
        """
        await locator.click()
        try:
            await locator.fill("")
        except Exception:  # noqa: BLE001
            pass
        delay_ms = random.randint(TYPING_DELAY_MIN_MS, TYPING_DELAY_MAX_MS)
        await locator.press_sequentially(text, delay=delay_ms)

    async def _profile_modal_quick_check(self, page: Page) -> bool:
        """Checagem RÁPIDA (~300ms) do popup 'Alternar entre perfis'.

        O ``_is_profile_modal_visible`` roda 10 sinais (inclui ``page.content()``)
        e leva ~2-3s quando o popup NÃO está lá — caro demais para usar num
        loop de espera. Este aqui é um único locator combinado, só para saber
        se vale a pena chamar o detector completo.
        """
        try:
            loc = page.locator(
                '[role="dialog"]:has-text("Alternar entre seus perfis"), '
                '[role="dialog"]:has-text("Concluir"), '
                '[role="button"]:has-text("Concluir"), '
                'button:has-text("Concluir")'
            )
            return await loc.first.is_visible(timeout=300)
        except Exception:  # noqa: BLE001
            return False

    async def _detect_and_close_profile_modal(
        self,
        page: Page,
        timeout_ms: int = 5000,
        wait_for_appear_ms: int = 0,
    ) -> bool:
        """Detecta E fecha o popup de 'Alternar entre perfis' com MÁXIMA AGRESSIVIDADE.

        Polling contínuo: tenta detectar + fechar até conseguir ou timeout.
        Muito mais robusto que chamar só uma vez.

        ``wait_for_appear_ms``: janela de GRAÇA antes de declarar "não estava
        lá". O popup do Meta costuma renderizar 1-3s DEPOIS da página hidratar
        — sem essa espera a função retornava True na 1ª tentativa (popup ainda
        não existia) e o popup subia logo em seguida, cobrindo o 'Adicionar'
        da página de domínios (relato do operador 2026-09).

        Returns: True se conseguiu fechar, False se não estava lá ou falhou.

        REESCRITA 2026-07-09: Mais tentativas, mais strategies, menos mercy.
        """
        if wait_for_appear_ms > 0:
            appear_deadline = time.monotonic() + (wait_for_appear_ms / 1000.0)
            while time.monotonic() < appear_deadline:
                if await self._profile_modal_quick_check(page):
                    logger.info(
                        "_detect_and_close_profile_modal: popup apareceu na "
                        "janela de graça (%dms) — fechando.", wait_for_appear_ms,
                    )
                    break
                await asyncio.sleep(0.3)

        start_time = time.monotonic()
        attempt = 0
        max_attempts = 20  # Aumentado de ~10 para 20

        logger.info("_detect_and_close_profile_modal: iniciando detecção (%dms timeout)…", timeout_ms)

        while time.monotonic() - start_time < (timeout_ms / 1000.0) and attempt < max_attempts:
            attempt += 1
            try:
                # Detecta se o popup está visível
                is_modal_present = await self._is_profile_modal_visible(page)

                if not is_modal_present:
                    if attempt > 1:
                        logger.info("_detect_and_close_profile_modal: ✅ popup desapareceu (tentativa %d)", attempt)
                    else:
                        logger.debug("_detect_and_close_profile_modal: popup não estava presente")
                    return True  # Popup desapareceu (sucesso)

                logger.info("_detect_and_close_profile_modal: popup detectado! (tentativa %d/20)", attempt)

                # Tenta fechar com TODAS as estratégias
                if await self._try_close_profile_modal(page):
                    logger.info("_detect_and_close_profile_modal: ✅ POPUP FECHADO! (tentativa %d)", attempt)
                    await asyncio.sleep(1.0)

                    # Validação final: confirma que realmente desapareceu
                    final_check = await self._is_profile_modal_visible(page)
                    if not final_check:
                        return True
                    else:
                        logger.warning("_detect_and_close_profile_modal: popup ainda visível após click! Retentando…")
                        await asyncio.sleep(0.5)
                else:
                    logger.debug("_detect_and_close_profile_modal: _try_close_profile_modal falhou, retentando…")
                    await asyncio.sleep(0.3)

            except Exception as exc:
                logger.debug("_detect_and_close_profile_modal: erro na tentativa %d: %s", attempt, exc)
                await asyncio.sleep(0.3)

        logger.error("_detect_and_close_profile_modal: ❌ TIMEOUT/MAX ATTEMPTS (%dms / %d tentativas)", timeout_ms, attempt)
        return False

    async def _is_profile_modal_visible(self, page: Page) -> bool:
        """Detecta se o popup de 'Alternar entre perfis' está visível - SUPER ROBUSTO.

        10 sinais diferentes para máxima certeza.
        """
        # GUARD: NÃO confundir o CHOOSER de domínio ("O que você quer fazer?" —
        # 'Criar um domínio' / 'Solicitar acesso a um domínio') com o popup de
        # perfil. Sem isto o closer gastava ~76s tentando clicar 'Concluir' (que
        # NÃO existe nessa tela), estourava as 20 tentativas e derrubava a etapa
        # de domínio (produção 2026-09, conta Susana). O chooser é tratado logo
        # depois por _configure_domain_security ('Criar um domínio').
        try:
            if await page.get_by_text(
                re.compile(
                    r"o que voc[êe] quer fazer"
                    r"|criar um( novo)? dom[íi]nio"
                    r"|solicitar acesso a um dom[íi]nio"
                    r"|request access to a domain",
                    re.I,
                )
            ).first.is_visible(timeout=300):
                logger.debug(
                    "_is_profile_modal_visible: chooser de domínio na tela — "
                    "NÃO é popup de perfil (ignorando)."
                )
                return False
        except Exception:  # noqa: BLE001
            pass

        # Sinal 1: Texto exato "Alternar entre seus perfis"
        try:
            if await page.locator('text=Alternar entre seus perfis').is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S1 - texto 'Alternar'")
                return True
        except Exception:
            pass

        # Sinal 2: Texto "Veja o perfil no qual você entrou"
        try:
            if await page.locator('text=Veja o perfil no qual você entrou').is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S2 - texto 'Veja o perfil'")
                return True
        except Exception:
            pass

        # Sinal 3: Botão "Concluir" (geralmente só o popup tem esse botão isolado)
        try:
            btn = page.get_by_role("button", name="Concluir")
            if await btn.count() > 0 and await btn.first.is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S3 - botão 'Concluir'")
                return True
        except Exception:
            pass

        # Sinal 4: Dialog com "Alternar" no texto
        try:
            if await page.locator('[role="dialog"]:has-text("Alternar")').is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S4 - dialog com 'Alternar'")
                return True
        except Exception:
            pass

        # Sinal 5: Elemento contendo "Concluir" com visibilidade
        try:
            if await page.locator('button:has-text("Concluir")').first.is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S5 - button:has-text('Concluir')")
                return True
        except Exception:
            pass

        # Sinal 6: XPath agressivo
        try:
            if await page.locator('xpath=//*[contains(text(), "Alternar entre seus perfis")]').is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S6 - XPath 'Alternar'")
                return True
        except Exception:
            pass

        # Sinal 7: Procura por div com role=button e texto Concluir
        try:
            if await page.locator('[role="button"]:has-text("Concluir")').first.is_visible(timeout=300):
                logger.debug("_is_profile_modal_visible: ✅ S7 - [role=button]:has-text('Concluir')")
                return True
        except Exception:
            pass

        # Sinal 8: Locator genérico para qualquer elemento com o texto
        try:
            page_html = await asyncio.wait_for(page.content(), timeout=15)
            if "Alternar entre seus perfis" in page_html:
                logger.debug("_is_profile_modal_visible: ✅ S8 - HTML contém 'Alternar'")
                return True
        except Exception:
            pass

        # Sinal 9: Procura por elementos de perfil (avatares)
        try:
            profile_elements = await page.locator('img[alt*="profile"], img[alt*="Profile"]').all()
            if len(profile_elements) >= 2:
                # Se tem 2+ avatares e o botão Concluir, é o popup
                try:
                    if await page.get_by_role("button", name="Concluir").is_visible(timeout=200):
                        logger.debug("_is_profile_modal_visible: ✅ S9 - múltiplos avatares + Concluir")
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        # Sinal 10: Qualquer elemento que contenha o padrão do popup
        try:
            modal_text = await page.locator('[role="dialog"]').text_content()
            if modal_text and "Concluir" in modal_text and "perfil" in modal_text.lower():
                logger.debug("_is_profile_modal_visible: ✅ S10 - dialog contém 'Concluir' + 'perfil'")
                return True
        except Exception:
            pass

        logger.debug("_is_profile_modal_visible: ❌ não detectado")
        return False

    async def _try_close_profile_modal(self, page: Page) -> bool:
        """Tenta fechar o popup com 20+ estratégias BRUTAIS.

        Retorna True se conseguiu, False se não achou o botão.
        REESCRITA 2026-07-09: Máxima agressividade.
        """
        strategies = [
            # E1-E3: get_by_role variações
            ("E1: get_by_role Concluir", lambda: page.get_by_role("button", name="Concluir")),
            ("E2: get_by_role regex", lambda: page.get_by_role("button", name=re.compile(r"Concluir|Conclude|Done|Finalizar"))),
            ("E3: get_by_role button roxo", lambda: page.get_by_role("button").filter(has_text="Concluir")),

            # E4-E6: XPath variações
            ("E4: XPath exato", lambda: page.locator('xpath=//button[contains(text(), "Concluir")]')),
            ("E5: XPath wildcards", lambda: page.locator('xpath=//*[contains(text(), "Concluir") and (@role="button" or self::button)]')),
            ("E6: XPath /button", lambda: page.locator('xpath=//button[contains(., "Concluir") or contains(., "Done")]')),

            # E7-E9: Dialog-specific
            ("E7: dialog button Concluir", lambda: page.locator('[role="dialog"]').get_by_role("button", name="Concluir")),
            ("E8: dialog button roxo", lambda: page.locator('[role="dialog"] button[class*="primary"]')),
            ("E9: dialog último button", lambda: page.locator('[role="dialog"] button').last),

            # E10-E12: CSS classes
            ("E10: button[class*=primary]", lambda: page.locator('button[class*="primary"]')),
            ("E11: button[class*=Primary]", lambda: page.locator('button[class*="Primary"]')),
            ("E12: button[style*=purple]", lambda: page.locator('button[style*="purple"], button[style*="#"]')),

            # E13-E15: has-text
            ("E13: button:has-text Concluir", lambda: page.locator('button:has-text("Concluir")')),
            ("E14: [role=button]:has-text", lambda: page.locator('[role="button"]:has-text("Concluir")')),
            ("E15: div:has-text Concluir", lambda: page.locator('div:has-text("Concluir")[role="button"], div[role="button"]:has-text("Concluir")')),

            # E16-E18: Botões X/fechar
            ("E16: Close aria-label", lambda: page.locator('button[aria-label*="Close"]')),
            ("E17: Fechar aria-label", lambda: page.locator('button[aria-label*="Fechar"]')),
            ("E18: X icon", lambda: page.locator('button:has(svg)')),

            # E19-E20: Último recurso
            ("E19: qualquer button purple", lambda: page.locator('button').filter(has_text=re.compile(r"Concluir|Done|Conclude", re.I))),
            ("E20: último button visível", lambda: page.locator('[role="dialog"] button').last),
        ]

        for strategy_name, get_btn_fn in strategies:
            try:
                logger.debug("_try_close_profile_modal: tentando %s…", strategy_name)
                btn_locator = get_btn_fn()

                # Se é locator múltiplo, pega o primeiro (ou último se E20)
                if hasattr(btn_locator, 'first') and "E20" not in strategy_name:
                    btn_locator = btn_locator.first

                # Verifica visibilidade
                try:
                    if not await btn_locator.is_visible(timeout=200):
                        logger.debug("_try_close_profile_modal: %s não visível", strategy_name)
                        continue
                except Exception:
                    continue

                # Scroll pra visibilidade
                try:
                    await btn_locator.scroll_into_view_if_needed()
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

                # Clica!
                logger.info("_try_close_profile_modal: %s - clicando…", strategy_name)
                try:
                    await self._robust_click(btn_locator)
                except Exception as click_err:
                    logger.debug("_try_close_profile_modal: click falhou (%s), tentando JavaScript…", type(click_err).__name__)
                    try:
                        await btn_locator.evaluate("el => el.click()")
                    except Exception:
                        continue

                # Aguarda propagação
                await asyncio.sleep(0.5)

                # Valida que desapareceu (3 validações)
                try:
                    if not await page.locator('[role="dialog"]').is_visible(timeout=300):
                        logger.info("_try_close_profile_modal: ✅ %s FUNCIONOU! (dialog desapareceu)", strategy_name)
                        return True
                except Exception:
                    pass

                try:
                    if not await btn_locator.is_visible(timeout=200):
                        logger.info("_try_close_profile_modal: ✅ %s FUNCIONOU! (botão desapareceu)", strategy_name)
                        return True
                except Exception:
                    pass

                # Verifica se o popup text sumiu
                try:
                    if not await page.locator('text=Alternar entre seus perfis').is_visible(timeout=200):
                        logger.info("_try_close_profile_modal: ✅ %s FUNCIONOU! (texto desapareceu)", strategy_name)
                        return True
                except Exception:
                    pass

            except Exception as e:
                logger.debug("_try_close_profile_modal: %s erro: %s", strategy_name, type(e).__name__)
                continue

        logger.error("_try_close_profile_modal: ❌ NENHUMA das 20 estratégias funcionou!")
        await self._dump_debug_html(page, "profile_modal_close_failed")
        return False

    async def _close_profile_switch_modal(self, page: Page) -> None:
        """LEGADO: chamada para trás. Use _detect_and_close_profile_modal() em vez."""
        await self._detect_and_close_profile_modal(page, timeout_ms=5000)

    async def _organic_wait(self) -> None:
        delay_ms = random.randint(ORGANIC_WAIT_MIN_MS, ORGANIC_WAIT_MAX_MS)
        await asyncio.sleep(delay_ms / 1000)
