"""Global configuration for meta_automation."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_boot_logger = logging.getLogger("meta_automation.config")

# Carrega .env do diretório do projeto (silencioso quando o pacote não está
# instalado, permitindo que o app rode em ambientes onde as envs já vêm do
# shell — CI, containers etc.).
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

# --- Paths -----------------------------------------------------------------
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

STATE_DIR = BASE_DIR / ".state"
STATE_DIR.mkdir(exist_ok=True)

STORAGE_STATE_PATH = Path(
    # ``or`` (não os.getenv 2 args): o python-dotenv SETA a env var mesmo
    # quando a linha no .env é "KEY=" (valor vazio) — os.getenv(key, default)
    # só cai no default quando a var está AUSENTE, não quando está vazia. Com
    # 2 args, "vazio = usa o padrão" (documentado no .env.example) NUNCA
    # acontecia de fato: a env ficava "" e isso virava Path(""), CWD errado.
    os.getenv("STORAGE_STATE_PATH") or (STATE_DIR / "storage_state.json")
)
SCREENSHOTS_DIR = STATE_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Instrumentação de banda (Passo 0 — medir, não chutar). Modo SÓ-LOG: quando
# ON, contabiliza bytes por (resource_type, domínio) e emite tabela + JSON por
# run. NUNCA aborta/altera tráfego. Default OFF → zero impacto em produção.
# Ver AutomationRunner._install_bandwidth_meter / _flush_bandwidth_report.
MEASURE_ASSET_BYTES = os.getenv("MEASURE_ASSET_BYTES", "false").lower() == "true"
BANDWIDTH_DIR = BASE_DIR / "artifacts" / "bandwidth"  # mkdir lazy (só quando grava)
COMPROVANTES_REVIEW_DIR = BASE_DIR / "artifacts" / "comprovantes_review"

# --- OCR -------------------------------------------------------------------
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "por")

# --- Uploads ---------------------------------------------------------------
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_UPLOAD_SIZE_MB = 10

# --- Playwright ------------------------------------------------------------
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "30000"))
PLAYWRIGHT_NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "60000"))
# Teto do wait_for_load_state("networkidle"). O Facebook mantém long-poll e
# conexões vivas, então o "idle" (500ms sem rede) quase NUNCA acontece: com o
# teto de navegação (60s) cada uma dessas esperas virava timeout cheio. O
# código já segue em frente no timeout — o teto curto só evita o desperdício.
NETWORKIDLE_TIMEOUT_MS = int(os.getenv("NETWORKIDLE_TIMEOUT_MS", "6000"))
# Diálogo de erro no fim de uma run falha: janela para o operador escolher
# 'voltar' antes de encerrar. 0 desliga (encerra direto).
ERROR_DIALOG_TIMEOUT_S = float(os.getenv("ERROR_DIALOG_TIMEOUT_S", "15"))
# Navegação via PROXY costuma ser bem mais lenta (datacenter/residencial) — o
# goto da tela de login estourava os 45s (produção 2026-06, proxy 185.14.238.15).
# Timeout maior só quando o run usa proxy.
PLAYWRIGHT_NAV_TIMEOUT_MS_PROXY = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS_PROXY", "120000"))
# ``or`` em vez de 2 args — ver comentário em STORAGE_STATE_PATH (.env "KEY="
# vazio não cai no default com os.getenv(key, default)).
PLAYWRIGHT_USER_AGENT = os.getenv("PLAYWRIGHT_USER_AGENT") or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PLAYWRIGHT_VIEWPORT = {"width": 1366, "height": 820}

# --- Locale ----------------------------------------------------------------
TARGET_LOCALE = os.getenv("TARGET_LOCALE", "pt_BR")
TARGET_LOCALE_HEADER = os.getenv("TARGET_LOCALE_HEADER", "pt-BR")
TARGET_TIMEZONE = os.getenv("TARGET_TIMEZONE", "America/Sao_Paulo")
# Página USF "Idioma e região" (Meta 2025+). A URL antiga
# (settings?tab=language) redireciona para cá, mas perde o ?locale= no
# caminho — apontar direto preserva o parâmetro de renderização.
LANGUAGE_PREFERENCES_URL = os.getenv(
    "LANGUAGE_PREFERENCES_URL",
    "https://www.facebook.com/settings/?tab=language_and_region",
)

# --- Human behavior --------------------------------------------------------
# --- Ritmo do bot (perfil AGRESSIVO, pedido do operador 2026-09) -----------
# A digitação e as pausas continuam ALEATÓRIAS (é a variação, não a lentidão,
# que disfarça o padrão), só que num ritmo de "humano rápido". Medição de 12
# runs: o ritmo antigo (100ms/char, 0,9s de pausa média) custava ~3,5 min por
# run de ~13 min. Se a Meta começar a restringir BMs, o caminho de volta é
# subir estes 4 números no .env — nada mais precisa mudar.
TYPING_DELAY_MIN_MS = int(os.getenv("TYPING_DELAY_MIN_MS", "8"))
TYPING_DELAY_MAX_MS = int(os.getenv("TYPING_DELAY_MAX_MS", "25"))
ORGANIC_WAIT_MIN_MS = int(os.getenv("ORGANIC_WAIT_MIN_MS", "100"))
ORGANIC_WAIT_MAX_MS = int(os.getenv("ORGANIC_WAIT_MAX_MS", "300"))

# --- Auth flow URLs / selectors -------------------------------------------
# ?locale= força o idioma da tela de login (deslogada), que o Facebook
# escolhe por geolocalização do IP — sem isso, proxies que saem fora do
# Brasil renderizam o login em idiomas inesperados (bengali visto em
# produção 2026-06), quebrando os seletores por texto. O submit via Enter
# em _handle_login_form cobre o caso de o idioma ainda assim não casar.
# ``or`` em vez de os.getenv(key, default) nestas 3: ver comentário em
# STORAGE_STATE_PATH acima — "KEY=" vazio no .env NÃO cai no default com a
# forma de 2 args (a env existe, só que vazia). Foi o bug real (2026-06):
# POST_LOGIN_INDICATOR vinha "" e o Playwright recebia um seletor CSS vazio
# (ou, pior, o comentário inline do .env, por um bug separado de parsing do
# python-dotenv com linhas "KEY=   # comentário" — ver .env.example).
META_LOGIN_URL = os.getenv("META_LOGIN_URL") or (
    f"https://www.facebook.com/login.php?locale={TARGET_LOCALE}"
)
META_HOME_URL = os.getenv("META_HOME_URL") or "https://www.facebook.com/"
POST_LOGIN_INDICATOR = os.getenv("POST_LOGIN_INDICATOR") or (
    'div[role="navigation"][aria-label],a[aria-label="Sua conta"]'
    ',a[aria-label="Your account"]'
)

# --- 2captcha --------------------------------------------------------------
TWO_CAPTCHA_API_KEY = os.getenv("TWO_CAPTCHA_API_KEY", "")
TWO_CAPTCHA_BASE_URL = os.getenv("TWO_CAPTCHA_BASE_URL", "https://2captcha.com")
TWO_CAPTCHA_POLL_INTERVAL_S = float(os.getenv("TWO_CAPTCHA_POLL_INTERVAL_S", "5"))
TWO_CAPTCHA_TIMEOUT_S = float(os.getenv("TWO_CAPTCHA_TIMEOUT_S", "180"))
# CapSolver — provedor que cobre reCAPTCHA Enterprise (e Arkose/FunCaptcha), que
# o método padrão do 2captcha NÃO resolve (observação 2026-06: o Meta serve na
# rota /authentication/ um reCAPTCHA que o 2captcha devolve ERROR_CAPTCHA_UNSOLVABLE).
# Quando CAPSOLVER_API_KEY está setada, o runner prefere o CapSolver ao 2captcha.
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")
CAPSOLVER_BASE_URL = os.getenv("CAPSOLVER_BASE_URL", "https://api.capsolver.com")
CAPSOLVER_POLL_INTERVAL_S = float(os.getenv("CAPSOLVER_POLL_INTERVAL_S", "3"))
CAPSOLVER_TIMEOUT_S = float(os.getenv("CAPSOLVER_TIMEOUT_S", "180"))
# O reCAPTCHA do Meta no 2FA é (provavelmente) Enterprise — o CapSolver usa um
# tipo de task distinto. Default True; ponha "false" se descobrir que é v2 comum.
CAPSOLVER_RECAPTCHA_ENTERPRISE = os.getenv("CAPSOLVER_RECAPTCHA_ENTERPRISE", "true").lower() == "true"
# Captcha: janela máxima para a RESOLUÇÃO MANUAL na corrida manual×2captcha.
# Rodando várias instâncias, o operador pode resolver qualquer uma na janela
# do AdsPower enquanto o 2captcha tenta sozinho — o primeiro a concluir
# destrava o bot. 0 desliga a espera manual (só 2captcha).
CAPTCHA_MANUAL_TIMEOUT_S = float(os.getenv("CAPTCHA_MANUAL_TIMEOUT_S", "300"))

# NUDGE pós-captcha (DESLIGADO por padrão — 0): clicar 'Não sou um robô' depois
# do token do CapSolver RE-ABRE o desafio de imagens ("selecione os ônibus") e
# invalida a resolução, entrando em loop (produção 2026-09, conta Samira). O
# token do CapSolver já resolve o captcha INTEIRO — não precisa clicar. Deixe 0.
# (Só religue com >0 se descobrir um fluxo que realmente exija o clique.)
CAPTCHA_POST_SOLVE_CLICK_DELAY_S = float(
    os.getenv("CAPTCHA_POST_SOLVE_CLICK_DELAY_S", "0")
)

# REFRESH do desafio: se o captcha NÃO for resolvido neste intervalo (s), clica
# o botão ↻ ('obter novo desafio') do reCAPTCHA para trocar por um desafio mais
# fácil — o solver/extensão então tenta o novo (pedido do operador 2026-09: um
# desafio de imagens difícil não saía; o refresh manual destravou). 0 desliga.
CAPTCHA_REFRESH_INTERVAL_S = float(
    os.getenv("CAPTCHA_REFRESH_INTERVAL_S", "25")
)

# --- Manual SMS fallback ---------------------------------------------------
SMS_MANUAL_TIMEOUT_S = float(os.getenv("SMS_MANUAL_TIMEOUT_S", "300"))

# --- SMS reception API (numero-virtual.com) --------------------------------
# Mantemos os nomes ``SMS24H_*`` como aliases de compatibilidade para não
# quebrar a esteira enquanto a troca de provedor é feita gradualmente.
NUMERO_VIRTUAL_API_KEY = (
    os.getenv("NUMERO_VIRTUAL_API_KEY")
    or os.getenv("SMS24H_API_KEY", "")
)
NUMERO_VIRTUAL_BASE_URL = os.getenv(
    "NUMERO_VIRTUAL_BASE_URL",
    os.getenv("SMS24H_BASE_URL", "https://app.numero-virtual.com/api/v1"),
).rstrip("/")
NUMERO_VIRTUAL_SERVICE_ID = int(
    os.getenv("NUMERO_VIRTUAL_SERVICE_ID", "60009")
)
NUMERO_VIRTUAL_SERVICE_CODE = os.getenv(
    "NUMERO_VIRTUAL_SERVICE_CODE",
    os.getenv("SMS24H_SERVICE_CODE", "ot"),
)
NUMERO_VIRTUAL_SERVICE_NAME = os.getenv(
    "NUMERO_VIRTUAL_SERVICE_NAME", "Outros apps/Site"
)
NUMERO_VIRTUAL_COUNTRY_ID = int(
    os.getenv("NUMERO_VIRTUAL_COUNTRY_ID", "24")
)
NUMERO_VIRTUAL_COUNTRY_CODE = os.getenv(
    "NUMERO_VIRTUAL_COUNTRY_CODE",
    os.getenv("SMS24H_COUNTRY_CODE", "br"),
)
NUMERO_VIRTUAL_TARGET_PRICE = int(
    os.getenv("NUMERO_VIRTUAL_TARGET_PRICE", "273")
)
NUMERO_VIRTUAL_PURCHASE_API_ID = int(
    os.getenv("NUMERO_VIRTUAL_PURCHASE_API_ID", "3")
)

# --- SMS24H.org (protocolo sms-activate.ru) --------------------------------
SMS24H_ORG_API_KEY = os.getenv("SMS24H_ORG_API_KEY", "")
SMS24H_ORG_BASE_URL = os.getenv(
    "SMS24H_ORG_BASE_URL",
    "https://api.sms24h.org/stubs/handler_api",
).rstrip("/")
SMS24H_ORG_SERVICE = os.getenv("SMS24H_ORG_SERVICE", "fb")
SMS24H_ORG_COUNTRY = os.getenv("SMS24H_ORG_COUNTRY", "73")

# --- SMSRush (smsrush.com.br) — API REST JSON --------------------------------
SMSRUSH_API_TOKEN = os.getenv("SMSRUSH_API_TOKEN", "")
SMSRUSH_BASE_URL = os.getenv(
    "SMSRUSH_BASE_URL",
    "https://api.smsrush.com.br/api/v1",
).rstrip("/")
SMSRUSH_SERVICE_ID = int(os.getenv("SMSRUSH_SERVICE_ID", "42"))

# Presets de compra de SMS (numero-virtual) escolhíveis por run no painel
# (botões na aba Disparar). Às vezes chega SMS de uma opção e não da outra —
# por isso o operador escolhe a fonte ANTES da run:
#   * 'facebook' → serviço Facebook / Vivo, Opção 1 (~R$1,30).
#   * 'other_2'  → 'Outros apps/Site', operadora aleatória, Opção 2 (~R$1,80).
#   * 'other'    → 'Outros apps/Site', operadora aleatória, Opção 3 (~R$2,76).
# None/ausente → usa o que estiver no .env/config (NUMERO_VIRTUAL_*).
SMS_PURCHASE_OPTIONS: dict = {
    "facebook": {
        "service_id": 60033, "service_name": "Facebook",
        "service_code": "fb",
        "purchase_api_id": 1, "target_price": 130,
    },
    "other_2": {
        "service_id": 60009, "service_name": "Outros apps/Site",
        "service_code": "ot",
        "purchase_api_id": 2, "target_price": 180,
    },
    "other": {
        "service_id": 60009, "service_name": "Outros apps/Site",
        "service_code": "ot",
        "purchase_api_id": 3, "target_price": 276,
    },
}


def sms_kwargs_for_option(option) -> dict:
    """kwargs pro Sms24hClient dado o preset ('facebook'|'other'|'other_2').
    {} = usa o default do .env/config (nada é sobrescrito)."""
    return dict(SMS_PURCHASE_OPTIONS.get((option or "").strip().lower(), {}))

# Escalonamento entre runs da MESMA leva (Disparar/Piloto): cada run começa
# ``RUN_STAGGER_S`` segundos DEPOIS da anterior (run 0 = 0s, run 1 = 25s, …).
# Evita a corrida no login/2FA/captcha do Facebook quando várias runs sobem no
# mesmo instante (produção 2026-09: 2 runs juntas davam bounce/timeout). 0 = sem
# escalonamento (todas ao mesmo tempo).
RUN_STAGGER_S = float(os.getenv("RUN_STAGGER_S", "25"))

# Aquecimento do perfil após o login (parece humano antes de criar BM: rola o
# feed, curte 1 post, passa no Marketplace, vê alguns segundos de Reels). Ajuda
# na reputação/soft-block. Best-effort — nunca derruba a run.
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "true").lower() == "true"
WARMUP_REELS_SECONDS = float(os.getenv("WARMUP_REELS_SECONDS", "8"))
# Compartilhar 1 post no aquecimento? Default OFF (share é mais arriscado/cria
# post público). Curtir já é feito sempre que WARMUP_ENABLED.
WARMUP_SHARE = os.getenv("WARMUP_SHARE", "false").lower() == "true"

# Contas AGED/compradas chegam com o portfólio do dono. O Meta frequentemente
# BLOQUEIA/esconde um 2º portfólio nessas contas: a criação "conclui" (modal de
# sucesso) mas o business_id novo nunca surge no diff → PortfolioIdCaptureError
# e a run era descartada em 1/5 (produção 2026-09, contas Malti). Com este flag
# ligado, quando o perfil JÁ TEM ≥1 portfólio do dono o bot:
#   1) faz APENAS 1 BM neste perfil (pula a 2ª BM, devolve o 2º CNPJ ao pool);
#   2) se a criação do portfólio novo não capturar o id, REUSA o portfólio
#      pré-existente para essa única BM e segue a verificação (em vez de abortar).
# Perfil ZERADO (sem portfólio do dono) segue o fluxo normal de 2 BMs.
PORTFOLIO_REUSE_FALLBACK = (
    os.getenv("PORTFOLIO_REUSE_FALLBACK", "false").lower() == "true"
)

# Rotação de IP do proxy MÓVEL antes de cada run: o bot bate neste link (o
# "Link de Rotação" do provedor) → o modem troca de IP → a conta loga num IP
# fresco. Só faz sentido com 1 proxy e runs SEQUENCIAIS/escalonadas (com várias
# concorrentes no MESMO proxy, rotacionar troca o IP de todas). Vazio = desligado.
PROXY_ROTATION_URL = os.getenv("PROXY_ROTATION_URL", "")
PROXY_ROTATION_WAIT_S = float(os.getenv("PROXY_ROTATION_WAIT_S", "8"))
NUMERO_VIRTUAL_CUSTOMER_SESSION = os.getenv(
    "NUMERO_VIRTUAL_CUSTOMER_SESSION", ""
)
NUMERO_VIRTUAL_PRICE_STRICT = os.getenv(
    "NUMERO_VIRTUAL_PRICE_STRICT", "false"
).lower() == "true"

SMS24H_API_KEY = NUMERO_VIRTUAL_API_KEY
SMS24H_BASE_URL = NUMERO_VIRTUAL_BASE_URL
SMS24H_SERVICE_CODE = NUMERO_VIRTUAL_SERVICE_CODE
SMS24H_COUNTRY_CODE = NUMERO_VIRTUAL_COUNTRY_CODE
SMS24H_OPERATOR = os.getenv("SMS24H_OPERATOR", "")


def _parse_ddd_set(raw: str) -> frozenset[str]:
    """Converte "11-19" / "11,19,21" / "11-13,21" em {'11','12',...} (2 díg.).

    Faixas (``a-b``) e itens soltos, separados por vírgula. Valor vazio →
    conjunto vazio = SEM filtro (aceita qualquer DDD).
    """
    out: set[str] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                for n in range(int(lo), int(hi) + 1):
                    out.add(f"{n:02d}")
            except ValueError:
                continue
        else:
            out.add(part.zfill(2))
    return frozenset(out)


# DDDs aceitos para o número alugado. Vazio = sem filtro (aceita qualquer DDD
# que o numero-virtual devolver). O número é virtual e serve para qualquer
# estado — o operador quer usar exatamente o que a API retornar.
SMS24H_ALLOWED_DDDS = _parse_ddd_set(os.getenv("SMS24H_ALLOWED_DDDS", ""))
SMS24H_DDD_MAX_ATTEMPTS = int(os.getenv("SMS24H_DDD_MAX_ATTEMPTS", "35"))

# Polling do gateway SMS: período entre consultas e timeout total da espera.
SMS_POLL_INTERVAL_S = float(os.getenv("SMS_POLL_INTERVAL_S", "5"))
SMS_POLL_TIMEOUT_S = float(os.getenv("SMS_POLL_TIMEOUT_S", "180"))

# Tempo de espera por tentativa de SMS no fluxo de recuperação (retry + fallback
# para 2ª provedora). Cada etapa (1ª espera, reenvio, 2ª provedora) aguarda este
# valor. Ajustável via .env sem recompilar.
SMS_RETRY_WAIT_S = float(os.getenv("SMS_RETRY_WAIT_S", "120"))

# Quando True, cai para validação manual se o gateway SMS falhar / key ausente.
SMS_API_FALLBACK_MANUAL = os.getenv("SMS_API_FALLBACK_MANUAL", "true").lower() == "true"

# Regex genérica para extrair o código do payload SMS. Cobre 4-10 dígitos
# (Facebook = 6, alguns providers entregam payloads com 9 dígitos como o
# telefone do remetente embaralhado). O caller deve usar ``findall`` e pegar
# a sequência mais longa para evitar matches parciais.
SMS_CODE_REGEX = re.compile(r"\b(\d{4,10})\b")

# --- AdsPower (Antidetect Browser via Local API) ----------------------------
# Local API roda em 127.0.0.1:50325 por padrão; pode ser sobrescrita via env.
ADSPOWER_API_URL = os.getenv("ADSPOWER_API_URL", "http://127.0.0.1:50325")
ADSPOWER_START_PATH = os.getenv("ADSPOWER_START_PATH", "/api/v1/browser/start")
ADSPOWER_STOP_PATH = os.getenv("ADSPOWER_STOP_PATH", "/api/v1/browser/stop")
ADSPOWER_TIMEOUT_S = float(os.getenv("ADSPOWER_TIMEOUT_S", "30"))
# Timeout para o connect_over_cdp no browser que o AdsPower abriu. O AdsPower
# pode reportar 'iniciado' mas o CDP travar no boot (porta ocupada, ws inválido);
# sem teto+classificação a exceção escapava sem fallback (observação 2026-06).
ADSPOWER_CDP_CONNECT_TIMEOUT_MS = int(os.getenv("ADSPOWER_CDP_CONNECT_TIMEOUT_MS", "30000"))
# Quando True, falhas no AdsPower fazem fallback para launch direto (com proxy do env).
ADSPOWER_FALLBACK_DIRECT = os.getenv("ADSPOWER_FALLBACK_DIRECT", "false").lower() == "true"

# Chave global da conta AdsPower. Em builds recentes da Local API, é exigida
# como query-param `api_key=` ou header Authorization. Em builds antigos
# (local-only, sem auth) ela é apenas ignorada — passar é sempre seguro.
ADSPOWER_API_KEY = os.getenv("ADSPOWER_API_KEY", "")
# Nome do grupo onde os perfis serão criados. Default "farm-conta". Mude para
# separar perfis por MacBook/operador (ex.: "farm-macbook-2", "farm-produção").
ADSPOWER_GROUP_NAME = os.getenv("ADSPOWER_GROUP_NAME", "farm-conta")

# --- ixBrowser (Antidetect Browser via Local API v2) --------------------------
IXBROWSER_API_URL = os.getenv("IXBROWSER_API_URL", "http://127.0.0.1:53200")
IXBROWSER_TIMEOUT_S = float(os.getenv("IXBROWSER_TIMEOUT_S", "30"))
IXBROWSER_GROUP_NAME = os.getenv("IXBROWSER_GROUP_NAME", "farm-conta")

# --- Rotação de IP 4G / proxy móvel (pré-boot do navegador) -----------------
# URL HTTP que dispara o reset do modem 4G / endpoint de rotação do provedor.
# Em branco → rotação é PULADA silenciosamente, fluxo segue normal.
PROXY_ROTATION_URL = os.getenv("PROXY_ROTATION_URL", "")
# Tempo (s) que esperamos a rede estabilizar após o reset antes de abrir o
# navegador. Modems 4G costumam levar 8–15s para renegociar IP.
PROXY_ROTATION_COOLDOWN_S = float(os.getenv("PROXY_ROTATION_COOLDOWN_S", "15"))
# Timeout (s) da requisição de reset — não bloqueia o pipeline indefinidamente
# se o endpoint do modem ficar inacessível.
PROXY_ROTATION_TIMEOUT_S = float(os.getenv("PROXY_ROTATION_TIMEOUT_S", "20"))

# CONSUMO DO PROXY NO POOL (pedido do operador 2026-09):
#   * IP FIXO (datacenter/1-IP-por-proxy): o proxy é CONSUMIDO — fica 'used'
#     vinculado ao CNPJ e NÃO volta ao pool (dois runs nunca compartilham o
#     mesmo IP fixo).
#   * IP ROTATIVO (móvel/4G com link de rotação): o proxy é MANTIDO — durante o
#     run fica 'used' (evita 2 runs no MESMO canal ao mesmo tempo), mas ao FIM
#     do run VOLTA AO POOL (available), pois a rotação entrega um IP fresco na
#     próxima vez. Assim o pool cicla e não drena.
# Default: rotativo quando há link de rotação configurado; senão fixo.
PROXY_POOL_ROTATING = (
    os.getenv(
        "PROXY_POOL_ROTATING",
        "true" if (PROXY_ROTATION_URL or "").strip() else "false",
    ).lower()
    == "true"
)

# --- Proxy dedicado (modo fallback / sem AdsPower) --------------------------
# Usado quando o perfil NÃO é gerenciado pelo AdsPower. Quando o user_id do
# AdsPower é informado, o proxy do perfil já vem aplicado e estes campos
# devem ficar vazios.
PROXY_SERVER = os.getenv("PROXY_SERVER", "")          # ex.: "http://1.2.3.4:8000"
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")

# --- Business Suite (Module 3) ---------------------------------------------
BUSINESS_OVERVIEW_URL = os.getenv(
    "BUSINESS_OVERVIEW_URL", "https://business.facebook.com/overview"
)
# Entrada DETERMINÍSTICA do fluxo de criação de portfólio. O /overview cai
# em telas diferentes por tipo de conta (Home do Business Suite quando a
# conta tem Página — visto em produção 2026-06, sem CTA de criar). Esta URL
# auto-abre o diálogo "Crie seu portfólio empresarial" independente do tipo
# de conta (create_business_portfolio_for_bm=1).
# ``or`` em vez de 2 args — ver comentário em STORAGE_STATE_PATH (.env "KEY="
# vazio não cai no default com os.getenv(key, default); era o caso real
# deste e do POST_LOGIN_INDICATOR, ambos "vazio = usa padrão" no .env.example).
BUSINESS_CREATE_PORTFOLIO_URL = os.getenv("BUSINESS_CREATE_PORTFOLIO_URL") or (
    "https://business.facebook.com/business/loginpage/?login_options[0]=FB"
    "&login_options[1]=IG&login_options[2]=SSO"
    "&config_ref=biz_login_tool_flavor_mbs&create_business_portfolio_for_bm=1"
)
# Página "Selecionar empresa" (business.facebook.com/select/): lista TODOS os
# portfólios da conta + o botão "Criar empresa"/"Create Business". É o caminho
# CONFIÁVEL de criar um portfólio NOVO numa conta que JÁ tem outros (aged) —
# o seletor do header vem ofuscado e o deep-link bounceia p/ loginpage, mas
# este botão abre o diálogo de criação normalmente (screenshot do operador
# 2026-09, conta com 2 portfólios + "Create Business" habilitado).
BUSINESS_SELECT_URL = os.getenv(
    "BUSINESS_SELECT_URL", "https://business.facebook.com/select/"
)
BUSINESS_INFO_URL = os.getenv(
    "BUSINESS_INFO_URL", "https://business.facebook.com/settings/info"
)
BUSINESS_DOMAINS_URL = os.getenv(
    "BUSINESS_DOMAINS_URL", "https://business.facebook.com/settings/owned-domains"
)
# Quando o SSO automático do chooser (business/loginpage) falha, o pipeline
# aguarda o operador clicar 'Continuar com o Facebook' na janela visível do
# AdsPower por até N segundos antes de abortar. 0 desliga o fallback.
BUSINESS_SSO_MANUAL_TIMEOUT_S = float(
    os.getenv("BUSINESS_SSO_MANUAL_TIMEOUT_S", "180")
)
BUSINESS_SECURITY_URL = os.getenv(
    "BUSINESS_SECURITY_URL", "https://business.facebook.com/settings/security"
)
# Página de contas do WhatsApp do portfólio (Contas → Contas do WhatsApp).
# Passo 4: criar uma conta do WhatsApp Business — é durante esse fluxo que o
# waba_id + access_token trafegam no GraphQL e o sniffer os captura.
BUSINESS_WHATSAPP_URL = os.getenv(
    "BUSINESS_WHATSAPP_URL",
    "https://business.facebook.com/settings/whatsapp-business-accounts",
)

# ---------------------------------------------------------------------------
# Passo 4 — CONTA DE ANÚNCIOS (substituiu a criação da WABA)
# ---------------------------------------------------------------------------
# Pedido do operador (2026-09): o passo 4 NÃO cria mais conta do WhatsApp.
# Em vez disso cria uma CONTA DE ANÚNCIOS no portfólio, com o MESMO nome da BM
# e moeda BRL. O fluxo da WABA continua íntegro no código, apenas DESLIGADO por
# este flag — CRIAR_WABA=true no .env reativa o passo antigo (e, com ele, a
# captura de waba_id/token e o registro no Datacrazy, que dependem da WABA).
CRIAR_WABA = os.getenv("CRIAR_WABA", "false").lower() == "true"

# Fallback do passo 5 (verificação da empresa): em ALGUMAS contas — não todas
# — o botão 'Iniciar verificação' simplesmente não libera na Central de
# Segurança mesmo com a conta de anúncios criada corretamente (passo 4 OK).
# Quando isso acontece, e SÓ nesse caso, a esteira cria a WABA (WhatsApp
# Business Account) para aquela BM como ativo alternativo e tenta reabrir a
# verificação a partir dela (a WABA também expõe um ponto de entrada para
# 'Iniciar verificação'). Independente de CRIAR_WABA (que controla se a WABA
# é criada por PADRÃO no passo 4b) — este flag só cria a WABA quando o botão
# de verificar não libera. Ligado por padrão;
# WABA_FALLBACK_SE_VERIFICACAO_INDISPONIVEL=false desativa e mantém o
# comportamento antigo (loga e desiste da verificação para aquela conta).
WABA_FALLBACK_SE_VERIFICACAO_INDISPONIVEL = (
    os.getenv("WABA_FALLBACK_SE_VERIFICACAO_INDISPONIVEL", "true").lower() == "true"
)

# Página de contas de anúncios do portfólio (Contas → Contas de anúncios).
BUSINESS_AD_ACCOUNTS_URL = os.getenv(
    "BUSINESS_AD_ACCOUNTS_URL",
    "https://business.facebook.com/settings/ad-accounts",
)
# Moeda da conta de anúncios. 'BRL' casa tanto "BRL - Real brasileiro" (pt-BR)
# quanto "BRL - Brazilian Real" (en) na lista do Meta — a seleção procura o
# CÓDIGO, que é estável entre idiomas.
AD_ACCOUNT_CURRENCY = os.getenv("AD_ACCOUNT_CURRENCY", "BRL")
# Fuso horário da conta de anúncios. O rótulo do Meta varia por idioma
# ("(GMT-03:00) Brasília", "America/Sao_Paulo") — o matcher cobre as variantes.
AD_ACCOUNT_TIMEZONE = os.getenv("AD_ACCOUNT_TIMEZONE", "America/Sao_Paulo")

PORTFOLIO_LIMIT_PATTERN = re.compile(
    r"limite|exceeded|máximo|cannot create|n[aã]o pode criar|too many",
    re.IGNORECASE,
)
# "Cadeado vermelho" — a Meta RESTRINGE a BM logo após a criação (pedido do
# operador 2026-06). Padrão best-effort (texto varia por idioma/experimento da
# Meta) — calibrar com DOM real salvo em .state/ quando bater um caso novo.
BUSINESS_RESTRICTED_PATTERN = re.compile(
    r"restrit[ao]|restricted|acesso limitado|limited access|"
    r"foi suspens[ao]|account.{0,15}suspended|"
    r"n[aã]o pode acessar este recurso|don't have permission",
    re.IGNORECASE,
)
# CADEADO de ANÚNCIO (operador 2026-09): a Meta às vezes trava o portfólio
# recém-criado — "You can't use this business portfolio to advertise" /
# "You can't use the business <nome> to create ads" (aparece na conta de
# anúncios E em domínios). Distinto da restrição dura acima. Quando bate na 1ª
# BM, a esteira ABANDONA essa BM e cria/roda a próxima no MESMO perfil (a conta
# continua boa; só aquele portfólio queimou). Padrão específico p/ não dar
# falso-positivo com texto genérico de anúncios.
BUSINESS_LOCKED_PATTERN = re.compile(
    r"can'?t use this business.{0,25}to advertise"
    r"|can'?t use the business.{0,60}to create ads"
    r"|n[ãa]o pode usar (este|esse|o) (portf[óo]lio|neg[óo]cio).{0,45}(anunciar|criar an[úu]ncios)"
    r"|n[ãa]o (é|e) poss[íi]vel usar.{0,60}(para|pra) (anunciar|criar an[úu]ncios)",
    re.IGNORECASE,
)
SMS_VERIFICATION_TIMEOUT_S = float(os.getenv("SMS_VERIFICATION_TIMEOUT_S", "600"))

# Passo 5: após anexar o comprovante CNPJ, SUBMETER a verificação à Meta e
# preencher o código SMS que a Meta dispara. True = fluxo automático completo
# (default). False = para após o upload para validação humana do documento
# antes de submeter (submit é irreversível).
BV_AUTO_SUBMIT = os.getenv("BV_AUTO_SUBMIT", "true").lower() == "true"
# Passo 5: quantas vezes preencher o código SMS antes de desistir. Se o código
# não confirmar (chegou tarde / foi rejeitado e o diálogo segue aberto), o bot
# clica "Reenviar código", pede um NOVO código ao provedor (status 3 re-arma a
# ativação) e re-tenta. >=1; default 3. O sms24h entrega 1 código por
# solicitação — cada reenvio é uma solicitação nova.
BV_SMS_RESEND_ATTEMPTS = max(1, int(os.getenv("BV_SMS_RESEND_ATTEMPTS", "3")))
# Debug do passo 5: quando ON e a verificação da empresa NÃO concluir (erro ou
# inelegível), o navegador + perfil AdsPower ficam ABERTOS no fim da run (e o
# número SMS não é cancelado por exceção) — pra inspecionar a tela e finalizar
# na mão. Default OFF: em produção/lote isso vazaria janelas e slots AdsPower.
# Fechar o perfil quando a run conclui 100%? Pedido do operador 2026-08:
# o navegador só fecha quando ELE clica em "Encerrar" — nem erro nem sucesso
# fecham sozinhos. Ligue (=true) se quiser que o sucesso feche o perfil.
FECHAR_PERFIL_NO_SUCESSO = (
    os.getenv("FECHAR_PERFIL_NO_SUCESSO", "false").lower() == "true"
)

KEEP_BROWSER_OPEN_ON_BV_ERROR = (
    os.getenv("KEEP_BROWSER_OPEN_ON_BV_ERROR", "false").lower() == "true"
)

# --- Cloudflare Workers API -------------------------------------------------
# O código JavaScript do Worker vive embutido em core/cloudflare_deployer.py
# (raw string `_WORKER_TEMPLATE`) — não há mais arquivo de template separado.
CLOUDFLARE_API_BASE = os.getenv(
    "CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4"
)
CLOUDFLARE_COMPATIBILITY_DATE = os.getenv(
    "CLOUDFLARE_COMPATIBILITY_DATE", "2024-12-01"
)
CLOUDFLARE_REQUEST_TIMEOUT_S = float(os.getenv("CLOUDFLARE_REQUEST_TIMEOUT_S", "30"))

# Nome do Worker script (mesmo em todas as contas do pool — só o id/token muda).
# Aceita CLOUDFLARE_SCRIPT_NAME (novo) ou CLOUDFLARE_WORKER_NAME (legado).
CLOUDFLARE_SCRIPT_NAME = os.getenv(
    "CLOUDFLARE_SCRIPT_NAME", os.getenv("CLOUDFLARE_WORKER_NAME", "")
)

# Override singular — caminho LEGADO, ainda suportado para retro-compat e para
# o operador colar credenciais ad-hoc na sidebar do app sem mexer no pool.
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")


def _csv_list(raw: str) -> list[str]:
    """Parse CSV env list, descartando espaços e itens vazios."""
    if not raw:
        return []
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


# --- Pool de contas Cloudflare (rotação automática) ------------------------
# Duas listas indexadas por posição — Account ID N pareado com API Token N.
# Quando ambas têm pelo menos 1 item E mesmo tamanho, o pool é ativado e
# CloudflareAccountPool rotaciona a cada CLOUDFLARE_ROTATION_THRESHOLD
# deploys bem-sucedidos.
CLOUDFLARE_ACCOUNT_IDS = _csv_list(os.getenv("CLOUDFLARE_ACCOUNT_IDS", ""))
CLOUDFLARE_API_TOKENS = _csv_list(os.getenv("CLOUDFLARE_API_TOKENS", ""))
CLOUDFLARE_ROTATION_THRESHOLD = int(os.getenv("CLOUDFLARE_ROTATION_THRESHOLD", "10"))
CLOUDFLARE_ROTATION_STATE_PATH = Path(
    os.getenv(
        "CLOUDFLARE_ROTATION_STATE_PATH",
        STATE_DIR / "cloudflare_metrics.json",
    )
)

# Espera de propagação entre o PUT do Worker e o clique em "Verificar
# domínio" no Meta Business Suite. Aceita CLOUDFLARE_DEPLOY_WAIT_S (novo)
# ou PROXY_WAIT_S (legado).
CLOUDFLARE_DEPLOY_WAIT_S = float(
    os.getenv("CLOUDFLARE_DEPLOY_WAIT_S", os.getenv("PROXY_WAIT_S", "15"))
)
# Alias legado mantido para imports antigos.
PROXY_WAIT_S = CLOUDFLARE_DEPLOY_WAIT_S

# --- Netlify (hospedagem dos sites por CNPJ) --------------------------------
# Substitui a Cloudflare como provedor de hospedagem. Cada CNPJ vira um
# subdomínio ({slug}.{seu-dominio}) servido por uma Netlify Edge Function que
# renderiza a MESMA página do worker (mesmo tema + dados do CNPJ + metatag de
# verificação do Facebook). O domínio, o token Netlify e o limite (cap) de
# sites por domínio são cadastrados no painel (tabela netlify_domains no store).
NETLIFY_API_BASE = os.getenv("NETLIFY_API_BASE", "https://api.netlify.com/api/v1")
# Token padrão (fallback) — o normal é cadastrar o token junto do domínio na UI.
NETLIFY_API_TOKEN = os.getenv("NETLIFY_API_TOKEN", "")
NETLIFY_REQUEST_TIMEOUT_S = float(os.getenv("NETLIFY_REQUEST_TIMEOUT_S", "60"))
# Timeout só do HANDSHAKE (abrir a conexão). Separado do de leitura porque
# deploy é lento (60s ok) mas CONECTAR não: sem isto, uma máquina que não
# alcança api.netlify.com ficava 60s pendurada por tentativa (3 tentativas
# por chamada) e o botão "Publicar" girava por minutos sem dizer nada.
NETLIFY_CONNECT_TIMEOUT_S = float(os.getenv("NETLIFY_CONNECT_TIMEOUT_S", "10"))
# Limite padrão de sites por domínio quando o operador não informa um na UI.
NETLIFY_DEFAULT_CAP = int(os.getenv("NETLIFY_DEFAULT_CAP", "10"))
# Espera de propagação entre o deploy e o clique em "Verificar domínio".
NETLIFY_DEPLOY_WAIT_S = float(
    os.getenv("NETLIFY_DEPLOY_WAIT_S", os.getenv("PROXY_WAIT_S", "15"))
)
# Tentativas de poll do estado do deploy (state == "ready").
NETLIFY_DEPLOY_POLL_ATTEMPTS = int(os.getenv("NETLIFY_DEPLOY_POLL_ATTEMPTS", "30"))
# PROBE da metatag: antes de pedir "Verificar domínio" ao Meta, o bot faz o
# MESMO que o crawler dele — GET https://<subdominio>/ e procura a metatag com
# o token. Sem isso o bot pedia verificação de um site que ainda não estava no
# ar (DNS/SSL propagando) e marcava "verificado" sem ninguém ter conferido.
# Janela padrão: 15 tentativas × 6s ≈ 90s.
METATAG_PROBE_ATTEMPTS = int(os.getenv("METATAG_PROBE_ATTEMPTS", "15"))
METATAG_PROBE_INTERVAL_S = float(os.getenv("METATAG_PROBE_INTERVAL_S", "6"))

# --- Passo 2: ESPERAR o domínio verificar antes de avançar -----------------
# Pedido do operador (2026-09): a esteira NÃO deve seguir para os passos 3-5
# com o domínio pendente — sem domínio verificado o passo 5 não encontra
# 'Iniciar verificação' e a run queima conta, CNPJ e comprovante à toa.
# Janela TOTAL (segundos) em que o passo 2 fica insistindo: metade para o
# probe HTTP da metatag (DNS/SSL do Netlify/Cloudflare propagando) e o resto
# re-clicando 'Verificar domínio' até o Meta mostrar o badge.
DOMAIN_VERIFY_WAIT_S = float(os.getenv("DOMAIN_VERIFY_WAIT_S", "180"))  # 3 min
# true = domínio NÃO verificado ao fim da janela ABORTA a run no passo 2
# (retomável pelo botão 'Continuar BM'). false = comportamento antigo, que
# seguia otimista quando o probe via a metatag no ar.
DOMAIN_VERIFY_STRICT = os.getenv("DOMAIN_VERIFY_STRICT", "true").lower() == "true"
# Modo de hospedagem:
#   False (padrão) → 1 SITE POR CNPJ (funciona em qualquer plano; cada
#       subdomínio é um site próprio com custom_domain — o Netlify cria o DNS e
#       o certificado por subdomínio em ~1-2 min, sem passo manual).
#   True → WILDCARD (1 site por domínio servindo *.dominio). SÓ funciona se o
#       Netlify tiver habilitado o recurso "wildcard subdomains" na sua conta
#       (Pro + flag liberada pelo suporte) — senão dá "Site not found".
NETLIFY_WILDCARD = os.getenv("NETLIFY_WILDCARD", "false").strip().lower() in {
    "1", "true", "yes", "on", "sim",
}

# ---------------------------------------------------------------------------
# Datacrazy — plataforma de registro de WABA + token capturados pelo sniffer.
# Endpoint e API key ficam como placeholder; quando o cliente confirmar o
# contrato (POST JSON simples vs. preenchimento via browser) basta preencher
# DATACRAZY_ENDPOINT no .env.
# ---------------------------------------------------------------------------
DATACRAZY_ENDPOINT = os.getenv("DATACRAZY_ENDPOINT", "")
DATACRAZY_API_KEY = os.getenv("DATACRAZY_API_KEY", "")
DATACRAZY_TIMEOUT_S = float(os.getenv("DATACRAZY_TIMEOUT_S", "30"))

# ---------------------------------------------------------------------------
# BrasilAPI — consulta CNPJ na Receita (substitui o OCR como fonte primária).
# Endpoint público, sem autenticação. Retorna razão social, endereço, sócios,
# CNAEs e capital social. Para MEI a `razao_social` traz "CNPJ_DIGITS NOME".
# ---------------------------------------------------------------------------
BRASILAPI_BASE_URL = os.getenv(
    "BRASILAPI_BASE_URL", "https://brasilapi.com.br/api/cnpj/v1"
)
BRASILAPI_TIMEOUT_S = float(os.getenv("BRASILAPI_TIMEOUT_S", "20"))

# ---------------------------------------------------------------------------
# InvertTexto / Uorak — API de e-mail temporário usada para gerar o e-mail
# comercial do cliente automaticamente. Token vem do painel; sem token o
# cliente cai para preenchimento manual.
# ---------------------------------------------------------------------------
INVERTTEXTO_API_TOKEN = os.getenv("INVERTTEXTO_API_TOKEN", "")
# Default sem subpath /v1 — alguns deploys do painel devolvem [Errno 8]/DNS
# quando a URL inclui caminho legado. O cliente concatena a rota canônica.
INVERTTEXTO_BASE_URL = os.getenv(
    "INVERTTEXTO_BASE_URL", "https://api.inverttexto.com"
)
INVERTTEXTO_TIMEOUT_S = float(os.getenv("INVERTTEXTO_TIMEOUT_S", "30"))

# Domínio público fixo Uorak para fallback local (slug@uorak.com) — não bate
# em API alguma; é o domínio captador da própria InvertTexto/Uorak.
UORAK_EMAIL_DOMAIN = os.getenv("UORAK_EMAIL_DOMAIN", "uorak.com")

# ---------------------------------------------------------------------------
# Infosimples — emissão do Cartão CNPJ (Comprovante de Inscrição e de Situação
# Cadastral) da Receita Federal.
#
# A Infosimples é um provedor LICENCIADO que consulta o portal cnpjreva por nós
# e devolve, em `site_receipts`, o PDF do comprovante OFICIAL capturado da
# Receita — é esse PDF autêntico que alimenta o upload na Meta. O captcha
# (hCaptcha) é resolvido do lado deles; nada de captcha/navegador do nosso lado.
#
#   * INFOSIMPLES_API_TOKEN     — token do painel (obrigatório; sem ele o
#                                  fluxo de emissão fica desabilitado).
#   * INFOSIMPLES_BASE_URL      — raiz da API v2.
#   * INFOSIMPLES_CNPJ_PATH     — rota da consulta Receita Federal / CNPJ.
#   * INFOSIMPLES_TIMEOUT_S     — timeout HTTP do nosso lado (conexão/leitura).
#   * INFOSIMPLES_SERVER_TIMEOUT_S — quanto a Infosimples pode levar tentando o
#                                  portal antes de desistir (param `timeout`).
# ---------------------------------------------------------------------------
INFOSIMPLES_API_TOKEN = os.getenv("INFOSIMPLES_API_TOKEN", "")
INFOSIMPLES_BASE_URL = os.getenv(
    "INFOSIMPLES_BASE_URL", "https://api.infosimples.com/api/v2"
)
INFOSIMPLES_CNPJ_PATH = os.getenv(
    "INFOSIMPLES_CNPJ_PATH", "/consultas/receita-federal/cnpj"
)
# Timeout HTTP local precisa ser MAIOR que o server-side: a Infosimples pode
# segurar a conexão aberta enquanto resolve o captcha e raspa o portal.
INFOSIMPLES_SERVER_TIMEOUT_S = float(os.getenv("INFOSIMPLES_SERVER_TIMEOUT_S", "300"))
INFOSIMPLES_TIMEOUT_S = float(os.getenv("INFOSIMPLES_TIMEOUT_S", "330"))
# Backfill do lookup (número do endereço que a BrasilAPI dá como "S/N"): timeout
# CURTO — a prévia é interativa, não pode segurar minutos. Se a Infosimples não
# responder a tempo, mantém "S/N" e o operador preenche/edita na mão.
INFOSIMPLES_BACKFILL_TIMEOUT_S = float(os.getenv("INFOSIMPLES_BACKFILL_TIMEOUT_S", "30"))

# ---------------------------------------------------------------------------
# Cloudflare Workers — subdomínio público (workers.dev) da conta operadora.
# Usado para derivar a URL pública previsível imediatamente após a busca CNPJ,
# sem precisar consultar a API. Ex.: <slug>.perfilprofissional.workers.dev.
# ---------------------------------------------------------------------------
CLOUDFLARE_WORKERS_SUBDOMAIN = os.getenv(
    "CLOUDFLARE_WORKERS_SUBDOMAIN", "perfilprofissional"
)

# ---------------------------------------------------------------------------
# Boot self-check — registra no log se chaves críticas vieram do .env.
# Ajuda a diagnosticar UI mostrando "API_KEY não configurada" quando o
# arquivo .env existe mas a chave está faltando/typo.
# ---------------------------------------------------------------------------
def _boot_self_check() -> None:
    checks = {
        "NUMERO_VIRTUAL_API_KEY": NUMERO_VIRTUAL_API_KEY,
        "INVERTTEXTO_API_TOKEN": INVERTTEXTO_API_TOKEN,
        "NETLIFY_API_TOKEN": NETLIFY_API_TOKEN,
        "INFOSIMPLES_API_TOKEN": INFOSIMPLES_API_TOKEN,
    }
    for name, value in checks.items():
        status = "OK" if value else "AUSENTE"
        if value:
            _boot_logger.info("[config] %s carregada (%s)", name, status)
        else:
            _boot_logger.warning("[config] %s %s — fluxo dependente ficará desabilitado", name, status)


_boot_self_check()

# --- Pre-flight (L2) --------------------------------------------------------
# Valida ambiente (credenciais, cookies, proxy, AdsPower, saldo SMS) antes de
# navegar. Desligue com PREFLIGHT_ENABLED=false para depuração.
PREFLIGHT_ENABLED = os.getenv("PREFLIGHT_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# --- Locale ----------------------------------------------------------------
# Forçar a UI para pt-BR (idioma + formato de data/hora/números) antes da
# esteira, MESMO quando o idioma detectado é en. Default TRUE: o fluxo do
# cliente exige pt-BR (a tela de informações da empresa só expõe os campos
# brasileiros — razão social, CEP, cidade, estado — em contexto pt-BR/Brasil).
# Idiomas FORA de pt/en (bengali visto em produção 2026-06) disparam a
# normalização AUTOMATICAMENTE, independente desta flag.
FORCE_LOCALE = os.getenv("FORCE_LOCALE", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# --- Recon / calibração ----------------------------------------------------
# Modo de RECONHECIMENTO: após autenticar+locale, o bot percorre TODAS as
# telas de negócio (overview, detalhes da empresa, domínios) abrindo cada
# diálogo "Editar" e salvando DOM + screenshot de cada uma em .state/, SEM
# tentar preencher/submeter e SEM fail-fast. Um único run rende o DOM de
# todos os passos de uma vez — acelera a calibração de seletores. Default
# FALSE (não altera a esteira normal).
RECON_DUMP = os.getenv("RECON_DUMP", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Modo CALIBRAÇÃO: a esteira NÃO aborta no primeiro passo que falha — cada
# passo de negócio (detalhes, domínio, SMS) que estourar salva DOM +
# screenshot e a esteira SEGUE para o próximo. Um run revela vários erros
# de uma vez (acelera os fixes). Default FALSE: produção mantém fail-fast.
CALIBRATION_MODE = os.getenv("CALIBRATION_MODE", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
