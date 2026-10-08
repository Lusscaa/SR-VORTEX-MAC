"""Classificador de estado de página do Facebook.

Responde "em que tela o fluxo de login parou?" para que falhas sejam
reportadas com causa (senha incorreta, conta suspensa, 2FA pendente…)
em vez do genérico "Login flow finished without authenticated session".

Arquitetura em duas camadas:

- ``classify_snapshot(url, title, body_text, html)`` — núcleo PURO,
  testável sem Playwright. Recebe um snapshot da página e devolve um
  ``StateDiagnosis``.
- ``classify_facebook_state(page)`` — wrapper async best-effort que
  colhe o snapshot via Playwright e delega ao núcleo. NUNCA levanta
  exceção: diagnóstico é observabilidade, não pode derrubar o pipeline.

Heurística de matching:

- Frases visíveis são buscadas no ``innerText`` (não no HTML bruto) para
  evitar falso-positivo com strings embutidas em bundles JS do Facebook.
- Texto é normalizado (casefold + remoção de acentos) para que "Você" e
  "voce" batam no mesmo padrão ASCII.
- Sinais estruturais (inputs, iframes, data-attributes) são buscados no
  HTML bruto.
- As regras são avaliadas em ordem de prioridade — a primeira que casar
  vence. Estados terminais de conta (suspensa/desativada/bloqueada) vêm
  primeiro; sinais definitivos de sucesso em seguida; desafios (captcha,
  2FA, checkpoint genérico — todos vivem em /checkpoint/) por último.

Frases e seletores catalogados em FACEBOOK_LOGIN_PATTERNS.md (recon
2026-06; PT-BR e EN, UI 2024-2026).
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # evita dependência dura de Playwright no núcleo puro
    from playwright.async_api import Page

logger = logging.getLogger("meta_automation.facebook_state")


class FacebookState(str, Enum):
    AUTHENTICATED = "authenticated"
    SAVE_LOGIN_PROMPT = "save_login_prompt"
    REMEMBER_BROWSER = "remember_browser"
    ACCOUNT_SUSPENDED = "account_suspended"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_LOCKED = "account_locked"
    RATE_LIMITED = "rate_limited"
    WRONG_PASSWORD = "wrong_password"
    ACCOUNT_NOT_FOUND = "account_not_found"
    # Verificação de identidade por SELFIE/FOTO de vídeo no login — exige gravar
    # vídeo/enviar foto, não automatizável. Operador: descartar a conta e seguir.
    IDENTITY_SELFIE = "identity_selfie"
    # Captcha de TEXTO no login/2FA ("Insira os caracteres que você vê") — sem
    # 2captcha exige operador. Pedido do operador: tratar como selfie (descartar
    # a conta e apagar o perfil na hora, sem ficar tentando).
    LOGIN_CAPTCHA = "login_captcha"
    CAPTCHA = "captcha"
    TWO_FACTOR_APPROVAL = "two_factor_approval"
    TWO_FACTOR_TOTP = "two_factor_totp"
    CHECKPOINT_GENERIC = "checkpoint_generic"
    COOKIE_BANNER = "cookie_banner"
    LOGIN_FORM = "login_form"
    # Atribuído PELA AuthFSM, nunca pelo classificador stateless: a tela de um
    # bounce de login é IDÊNTICA à do primeiro acesso (mesmo /login + name="pass")
    # — só a FSM sabe que houve um submit que voltou ao formulário. Não existe
    # _Rule que produza este estado; a tela de login segue como LOGIN_FORM.
    LOGIN_BOUNCED = "login_bounced"
    # Erro temporário do Facebook ("Estamos tendo problemas...") — recarrega a página
    # e retenta automaticamente até sair do erro.
    TEMPORARY_ERROR = "temporary_error"
    UNKNOWN = "unknown"


@dataclass
class StateDiagnosis:
    state: FacebookState
    description: str
    terminal: bool
    matched: list[str] = field(default_factory=list)
    url: str = ""
    title: str = ""

    def summary(self) -> str:
        """Linha única para logs e mensagens de exceção."""
        return f"{self.description} [estado={self.state.value}]"


# Descrição legível para o operador quando a AuthFSM detecta um bounce de login
# (credenciais submetidas, mas o Facebook re-renderizou o formulário sem erro).
LOGIN_BOUNCED_DESCRIPTION = (
    "Credenciais submetidas, mas o Facebook voltou ao formulário de login sem "
    "mensagem de erro — soft-block silencioso (tipicamente reputação de IP) ou "
    "senha incorreta. Verifique a senha e/ou troque o IP/proxy do perfil."
)


@dataclass(frozen=True)
class _Rule:
    state: FacebookState
    description: str
    terminal: bool
    # Padrões ASCII minúsculos, casados contra texto normalizado.
    text: tuple[str, ...] = ()
    # Padrões casados contra a URL (case-insensitive).
    url: tuple[str, ...] = ()
    # Padrões casados contra o HTML bruto (case-insensitive).
    html: tuple[str, ...] = ()
    # Se a URL casar qualquer um destes, a regra é desqualificada — usado
    # para impedir que sinais fracos (ex.: nav residual) casem em telas de
    # login/checkpoint.
    exclude_url: tuple[str, ...] = ()


def _normalize(text: str) -> str:
    """casefold + remove acentos + apóstrofo tipográfico → ASCII simples."""
    text = text.replace("’", "'")
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Ordem importa: primeira regra que casar vence. Estados terminais de conta
# (alta especificidade textual) no topo; sucesso em seguida; desafios que
# compartilham a URL /checkpoint/ ordenados do mais específico (captcha com
# iframe) ao mais genérico (checkpoint sem sinal distintivo).
_RULES: tuple[_Rule, ...] = (
    _Rule(
        state=FacebookState.ACCOUNT_SUSPENDED,
        description=(
            "Conta suspensa pelo Facebook — requer apelação manual; "
            "a automação não pode prosseguir com esta conta."
        ),
        terminal=True,
        text=(
            r"suspendemos sua conta",
            r"(a sua|sua|esta) conta foi suspensa",
            r"conta esta suspensa",
            r"we('ve| have)? suspended your account",
            r"(your|this) account (has been|is|was) suspended",
        ),
    ),
    _Rule(
        state=FacebookState.ACCOUNT_DISABLED,
        description=(
            "Conta desativada pelo Facebook — irrecuperável via automação."
        ),
        terminal=True,
        text=(
            r"(a sua|sua|esta) conta foi desativada",
            r"desativamos sua conta",
            r"conta (esta|foi) desabilitada",
            r"(your|this) account (has been|is|was) disabled",
            r"we disabled your account",
            r"account disabled",
        ),
        html=(r"/request_login_review",),
    ),
    _Rule(
        state=FacebookState.ACCOUNT_LOCKED,
        description=(
            "Conta bloqueada pelo Facebook (verificação de segurança) — "
            "requer desbloqueio manual."
        ),
        terminal=True,
        text=(
            r"sua conta (foi|esta) bloqueada",
            r"bloqueamos sua conta",
            r"your account (has been|is|was) locked",
            r"we('ve| have)? locked your account",
            r"account has been locked",
            # Conta RESTRINGIDA por possível invasão (tela 'confirme que esta
            # conta é sua para desbloqueá-la' / 'Restringimos sua conta ... pode
            # ter sido invadida') — terminal, descarta (operador 2026-06). Só
            # TEXTO (sem url /checkpoint/, que casaria 2FA/captcha por engano).
            r"restringimos sua conta",
            r"confirme que esta conta e sua para desbloque",
            r"pode ter sido invadida",
            r"we restricted your account",
            r"confirm (that )?this account is yours to unlock",
            r"may have been (hacked|compromised)",
        ),
    ),
    _Rule(
        state=FacebookState.RATE_LIMITED,
        description=(
            "Facebook bloqueou temporariamente novas tentativas (excesso de "
            "tentativas de login) — aguarde antes de tentar novamente."
        ),
        terminal=True,
        text=(
            r"voce tentou entrar muitas vezes",
            r"muitas tentativas",
            r"temporariamente bloquead[oa]",
            r"bloquead[oa] temporariamente",
            r"you('re| are|'ve been| have been) temporarily blocked",
            r"temporarily (blocked|locked)",
            r"too many (login )?attempts",
        ),
    ),
    _Rule(
        state=FacebookState.WRONG_PASSWORD,
        description=(
            "Senha incorreta — verifique as credenciais cadastradas para "
            "esta conta."
        ),
        terminal=True,
        text=(
            r"senha que voce (inseriu|digitou) esta incorreta",
            r"senha incorreta",
            r"senha\s+[ei]\s+invalida",
            r"verifique sua senha",
            r"password (that )?you('ve| have)? entered is incorrect",
            r"incorrect password",
            r"wrong password",
            r"check your password",
        ),
    ),
    _Rule(
        state=FacebookState.ACCOUNT_NOT_FOUND,
        description=(
            "E-mail/telefone não corresponde a nenhuma conta — verifique o "
            "identificador de login cadastrado."
        ),
        terminal=True,
        # NÃO casar 'encontre sua conta'/'find your account' soltos: são o CTA
        # do link de recuperação (<a href="/login/identify">Encontre sua conta e
        # entre.</a>) que existe SEMPRE na home DESLOGADA — casava como falso
        # positivo num bounce de 2FA (volta à home), matando o run como terminal
        # em vez de rotacionar (run 4d1e31ba 2026-06-17). O caso GENUÍNO é pego
        # pela URL /login/identify + pelas frases de erro específicas abaixo.
        text=(
            r"nao corresponde a nenhuma conta",
            r"nao esta associado a nenhuma conta",
            r"nao encontramos (a |uma |sua |nenhuma )?conta",
            r"nao foi possivel encontrar sua conta",
            r"did(n't| not) match any account",
            r"isn'?t associated with any account",
            r"no account found",
            r"account not found",
            r"we can'?t find the account",
            r"we couldn'?t find (an |your )?account",
        ),
        url=(r"/login/identify",),
    ),
    _Rule(
        state=FacebookState.IDENTITY_SELFIE,
        description=(
            "Verificação de identidade por SELFIE/vídeo — o Facebook exige "
            "gravar um vídeo ou enviar uma foto sua. Não automatizável; a conta "
            "é descartada e a run segue para a próxima."
        ),
        terminal=True,
        text=(
            r"selfie de video",
            r"video selfie",
            r"tire uma selfie",
            r"grave (um|uma) (video|selfie)",
            r"envie (uma foto sua|uma selfie)",
            r"confirme que voce e uma pessoa real",
            r"precisamos confirmar que voce e (uma )?(pessoa )?real",
            r"confirme sua identidade com (um )?video",
            r"verificacao (de identidade )?por video",
            r"confirm (that )?you('?re| are)? a real person",
            r"take a (video )?selfie",
            r"record a (short )?(video|selfie)",
            r"upload a (photo|video) of yourself",
            r"we need to confirm you('?re| are) a real person",
        ),
    ),
    _Rule(
        state=FacebookState.LOGIN_CAPTCHA,
        description=(
            "Captcha de texto no login/2FA ('Insira os caracteres que você "
            "vê') — sem 2captcha exige operador. Tratado como terminal: a conta "
            "é descartada e o perfil apagado (pedido do operador 2026-06)."
        ),
        terminal=True,
        text=(
            r"insira os caracteres que voce ve",
            r"digite os caracteres que voce ve",
            r"este login esta sendo feito por um humano",
            r"enter the characters you see",
            r"type the characters you see",
            r"confirm (that )?this login is being (done|made) by a human",
        ),
    ),
    _Rule(
        state=FacebookState.SAVE_LOGIN_PROMPT,
        description=(
            "Login bem-sucedido — Facebook exibiu o prompt 'Salvar suas "
            "informações de login?' e o fluxo parou antes de dispensá-lo."
        ),
        terminal=False,
        text=(
            r"salvar (suas )?informacoes de login",
            r"save your login info",
            r"memorizar este navegador",
            r"remember this browser",
        ),
        html=(r'name="save_device"',),
    ),
    _Rule(
        state=FacebookState.REMEMBER_BROWSER,
        description=(
            "Login + 2FA OK — Facebook pede 'Lembrar deste navegador?'. Basta "
            "confirmar para concluir (e o perfil pula 2FA nos próximos logins)."
        ),
        terminal=False,
        text=(
            r"confiar neste (dispositivo|navegador|aparelho)",
            r"lembrar deste navegador",
            r"trust this (device|browser)",
            r"salvar este navegador",
        ),
        # Sinal estável: a página React de /two_factor/remember_browser/
        # frequentemente não expõe texto reconhecível no primeiro paint
        # (visto em produção 2026-06: classifier em loop UNKNOWN).
        url=(r"/two_factor/remember_browser/",),
    ),
    _Rule(
        state=FacebookState.AUTHENTICATED,
        description=(
            "Sessão aparenta estar autenticada (indicador pós-login presente "
            "no DOM). Se o login foi reportado como falho, o seletor "
            "POST_LOGIN_INDICATOR pode estar desatualizado ou o elemento "
            "não ficou visível a tempo."
        ),
        terminal=False,
        html=(
            r'aria-label="sua conta"',
            r'aria-label="your account"',
        ),
        # Telas de login/checkpoint podem conter nav residual com esses
        # aria-labels — nelas, sessão ativa NÃO é a conclusão correta.
        exclude_url=(r"/checkpoint/", r"/login"),
    ),
    _Rule(
        state=FacebookState.CAPTCHA,
        description="Captcha/verificação de segurança exibido e não resolvido.",
        terminal=False,
        text=(
            r"nao (e|sou) um robo",
            r"not a robot",
            r"security check",
            r"complete a verificacao de seguranca",
        ),
        html=(
            r"google\.com/recaptcha",
            r'iframe[^>]*src="[^"]*recaptcha',
            r"hcaptcha\.com",
            r'name="h-captcha-response"',
        ),
    ),
    _Rule(
        state=FacebookState.TWO_FACTOR_APPROVAL,
        description=(
            "Facebook pede aprovação do login em outro dispositivo "
            "(notificação push) — não há campo de código TOTP nesta tela."
        ),
        terminal=False,
        text=(
            r"confira suas notificacoes",
            r"check your notifications",
            r"aprov[a-z]*[^.\n]{0,60}outro (dispositivo|aparelho)",
            r"approve[^.\n]{0,60}(another|your other) (device|phone)",
            r"voce recebera uma notificacao",
            r"you('ll| will) get a notification",
            r"enviamos uma notificacao",
            r"notificacao enviada",
            r"we sent a notification",
            r"notification sent",
        ),
    ),
    _Rule(
        state=FacebookState.TWO_FACTOR_TOTP,
        description=(
            "Tela de código 2FA presente — o código TOTP não foi aceito ou "
            "não chegou a ser submetido."
        ),
        terminal=False,
        text=(
            r"app(licativo)? de autenticacao",
            r"authentication app",
            r"authenticator app",
            r"autenticacao de dois fatores",
            r"two-?factor authentication",
            r"codigo de autenticacao",
            r"insira o codigo",
            r"codigo de login",
            r"enter (the |your )?(login |security |authentication |verification )?code",
            r"6[-\s]digit",
            r"6 digitos",
        ),
        html=(
            r'name="approvals_code"',
            r'autocomplete="one-time-code"',
        ),
        # UI nova de 2FA (2025+): o Facebook redireciona para
        # /two_step_verification/authentication/?...&flow=pre_authentication
        # — página React que pode não expor nenhum texto/atributo conhecido
        # no primeiro paint. A URL é o sinal estável (visto em produção
        # 2026-06: classifier devolvia UNKNOWN em loop até o teto).
        url=(r"/two_step_verification/",),
    ),
    _Rule(
        state=FacebookState.CHECKPOINT_GENERIC,
        description=(
            "Checkpoint de verificação de identidade — requer intervenção "
            "manual na conta."
        ),
        terminal=False,
        text=(
            r"confirm(e|ar) sua identidade",
            r"confirm your identity",
            r"verificacao de seguranca",
            r"security verification",
            r"perguntas de seguranca",
            r"security questions",
        ),
        url=(r"/checkpoint/",),
    ),
    _Rule(
        state=FacebookState.COOKIE_BANNER,
        description=(
            "Banner de cookies ainda visível — pode estar bloqueando "
            "interações com a página."
        ),
        terminal=False,
        # Só texto VISÍVEL: o atributo data-cookiebanner persiste no DOM
        # (oculto) depois do dismiss e classificaria errado por HTML.
        text=(
            r"permitir todos os cookies",
            r"allow all cookies",
        ),
    ),
    _Rule(
        state=FacebookState.TEMPORARY_ERROR,
        description=(
            "Erro temporário do Facebook ('Estamos tendo problemas...') — "
            "recarrega a página e retenta automaticamente."
        ),
        terminal=False,
        text=(
            r"estamos tendo problemas para concluir sua solicitacao",
            r"estamos tendo problemas",
            r"we're having trouble completing your request",
            r"having trouble",
            r"sorry, something went wrong",
        ),
    ),
    _Rule(
        state=FacebookState.LOGIN_FORM,
        description=(
            "Ainda na tela de login (campo de senha visível) — credenciais "
            "não submetidas ou rejeitadas sem mensagem de erro."
        ),
        terminal=False,
        url=(r"/login",),
        html=(r'name="pass"',),
    ),
)

_UNKNOWN_DESCRIPTION = (
    "Estado da página não reconhecido — consulte o screenshot/HTML de "
    "evidência salvos pela autópsia de falha."
)


def classify_snapshot(
    url: str,
    title: str = "",
    body_text: str = "",
    html: str = "",
) -> StateDiagnosis:
    """Classifica um snapshot de página em um ``FacebookState``.

    Núcleo puro: sem I/O, sem Playwright — recebe strings, devolve
    diagnóstico. ``body_text`` deve ser o texto VISÍVEL da página
    (``document.body.innerText``); ``html`` é o markup bruto.
    """
    norm_text = _normalize(f"{title}\n{body_text}")
    html_lower = html.casefold()
    url_lower = url.casefold()

    for rule in _RULES:
        if any(re.search(p, url_lower) for p in rule.exclude_url):
            continue
        matched: list[str] = []
        for pattern in rule.text:
            if re.search(pattern, norm_text):
                matched.append(f"text:{pattern}")
        for pattern in rule.url:
            if re.search(pattern, url_lower):
                matched.append(f"url:{pattern}")
        for pattern in rule.html:
            if re.search(pattern, html_lower):
                matched.append(f"html:{pattern}")
        if matched:
            return StateDiagnosis(
                state=rule.state,
                description=rule.description,
                terminal=rule.terminal,
                matched=matched,
                url=url,
                title=title,
            )

    return StateDiagnosis(
        state=FacebookState.UNKNOWN,
        description=_UNKNOWN_DESCRIPTION,
        terminal=False,
        url=url,
        title=title,
    )


# Teto por leitura de snapshot: em CDP/AdsPower com tab detached, cada
# chamada Playwright pode pendurar até o timeout default (30s). O diagnóstico
# roda em caminhos que já estão falhando — não pode adicionar 90s de espera.
_SNAPSHOT_READ_TIMEOUT_S = 5.0


async def classify_facebook_state(page: "Page") -> StateDiagnosis:
    """Colhe snapshot da página e classifica. Best-effort: nunca levanta.

    Cada captura (url/título/texto/html) tem seu próprio guarda e teto de
    tempo — em modo CDP/AdsPower uma delas pode falhar ou pendurar (tab
    detached) sem invalidar as outras. Se tudo falhar, devolve UNKNOWN com
    o que tiver.
    """
    url = ""
    title = ""
    body_text = ""
    html = ""
    try:
        url = page.url
    except Exception as exc:  # noqa: BLE001
        logger.debug("classify: leitura de url falhou: %s", exc)
    try:
        title = await asyncio.wait_for(
            page.title(), timeout=_SNAPSHOT_READ_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("classify: leitura de title falhou: %s", exc)
    try:
        body_text = await asyncio.wait_for(
            page.evaluate("() => document.body ? document.body.innerText : ''"),
            timeout=_SNAPSHOT_READ_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("classify: leitura de innerText falhou: %s", exc)
    try:
        html = await asyncio.wait_for(
            page.content(), timeout=_SNAPSHOT_READ_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("classify: leitura de content falhou: %s", exc)

    try:
        diagnosis = classify_snapshot(url, title, body_text, html)
    except Exception as exc:  # noqa: BLE001
        # Diagnóstico jamais pode derrubar o pipeline que está falhando.
        logger.warning("classify: núcleo falhou inesperadamente: %s", exc)
        diagnosis = StateDiagnosis(
            state=FacebookState.UNKNOWN,
            description=_UNKNOWN_DESCRIPTION,
            terminal=False,
            url=url,
            title=title,
        )
    logger.info(
        "Estado da página classificado: %s (sinais=%s, url=%s)",
        diagnosis.state.value,
        diagnosis.matched or "nenhum",
        # URLs do Facebook carregam contexts criptografados de 1KB+ — trunca
        # para o log; a URL completa vive em StateDiagnosis.url.
        (url[:120] + "…") if len(url) > 120 else (url or "?"),
    )
    return diagnosis
