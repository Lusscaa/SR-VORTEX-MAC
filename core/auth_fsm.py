"""Máquina de estados de autenticação do Facebook (PR3 — AuthFSM).

Substitui o fluxo linear ``_perform_login``/``_handle_two_factor`` por um
loop reativo:

    SENTE o estado da tela  → classify_facebook_state (PR1, sensor)
    DESPACHA a ação          → page objects (PR2, atuadores)
    REPETE                   → até autenticar, falhar terminal ou estourar
                               os guardas anti-loop

Vantagens sobre o script linear: checkpoints/captchas/banners que aparecem
FORA da ordem esperada não derrubam o fluxo — a FSM reage ao que a tela
mostra, não ao que o roteiro previa.

Guardas anti-loop: teto global de transições + teto de visitas por estado.
Estados terminais de conta (suspensa/desativada/bloqueada/senha errada)
falham imediatamente com diagnóstico no ``AutomationResult`` (semântica do
PR1 preservada).

A FSM não conhece Playwright diretamente além da ``page`` que repassa aos
atuadores; interações de DOM e helpers de gesto humano (digitação, waits,
captcha, intervenção manual) são delegados ao ``runner``
(AutomationRunner), o que mantém a FSM testável com fakes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

import pyotp
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import (
    CAPTCHA_POST_SOLVE_CLICK_DELAY_S,
    PLAYWRIGHT_NAV_TIMEOUT_MS,
    PLAYWRIGHT_NAV_TIMEOUT_MS_PROXY,
    META_LOGIN_URL,
    PROXY_ROTATION_URL,
)

from .errors import CaptchaError, CaptchaUnsolvedError, LoginError
from .facebook_state import (
    FacebookState,
    StateDiagnosis,
    LOGIN_BOUNCED_DESCRIPTION,
    classify_facebook_state,
)
from .pages import FacebookLoginPage, FacebookTwoFactorPage

if TYPE_CHECKING:
    from playwright.async_api import Page

    from .automation import AutomationResult, AutomationRunner

logger = logging.getLogger("meta_automation.auth_fsm")

# Estados que encerram a FSM com falha imediata (diagnóstico terminal).
_TERMINAL_STATES = frozenset(
    {
        FacebookState.ACCOUNT_SUSPENDED,
        FacebookState.ACCOUNT_DISABLED,
        FacebookState.ACCOUNT_LOCKED,
        FacebookState.RATE_LIMITED,
        FacebookState.WRONG_PASSWORD,
        FacebookState.ACCOUNT_NOT_FOUND,
        FacebookState.LOGIN_CAPTCHA,
    }
)

# Erros terminais que, ao falhar, devem: marcar o NOME do perfil AdsPower com o
# motivo, FECHAR o perfil e DESCARTAR a conta (pedido do operador 2026-06).
# Mapeia estado → tag curta que vai pro nome do perfil. RATE_LIMITED e LOGIN_BOUNCED
# ficam de fora (são temporários — a conta volta ao estoque, não descarta).
_DISCARD_AND_CLOSE: dict[FacebookState, str] = {
    FacebookState.WRONG_PASSWORD: "Senha incorreta",
    # LOGIN_BOUNCED NÃO descarta — é temporário, conta volta ao estoque
    FacebookState.ACCOUNT_LOCKED: "Conta restrita/bloqueada",
    FacebookState.ACCOUNT_SUSPENDED: "Conta suspensa",
    FacebookState.ACCOUNT_DISABLED: "Conta desativada",
    FacebookState.ACCOUNT_NOT_FOUND: "Conta não encontrada",
    FacebookState.IDENTITY_SELFIE: "SELFIE",
    FacebookState.LOGIN_CAPTCHA: "CAPTCHA",
}


class AuthFSM:
    """Loop reativo de autenticação. Uma instância por tentativa de login."""

    # Teto global de iterações sense→act — backstop absoluto contra loops.
    MAX_TRANSITIONS = 20
    # Teto de visitas por estado: APÓS atingir este número, fallback manual.
    # Exemplo: MAX_VISITS[UNKNOWN]=5 permite até 5 visitas; na 6ª dispara fallback.
    # LOGIN_FORM=1: 1 submit de credenciais; se o FB voltar AO login (após o 2FA),
    # isso é bounce (soft-block/senha) → NÃO re-submete (o 2º submit só aprofunda
    # o soft-block e queima a conta — runs 2/4/10 jun/2026). Cai direto no
    # _handle_login_bounce (rotaciona IP se configurado) / fail-fast.
    MAX_VISITS: dict[FacebookState, int] = {
        FacebookState.LOGIN_FORM: 1,
        FacebookState.COOKIE_BANNER: 2,
        FacebookState.CAPTCHA: 2,
        FacebookState.TWO_FACTOR_TOTP: 3,
        FacebookState.SAVE_LOGIN_PROMPT: 3,
        FacebookState.REMEMBER_BROWSER: 3,
        FacebookState.TWO_FACTOR_APPROVAL: 2,
        FacebookState.CHECKPOINT_GENERIC: 3,
        FacebookState.UNKNOWN: 5,
    }
    # Tentativas de login antes do fail-fast por bounce. Cada tentativa
    # esgotada (submit → volta ao login) consome uma; com rotação de IP
    # configurada, gastamos as tentativas rotacionando antes de desistir.
    # 3 (era 2): mais uma chance de recuperar o bounce sozinho antes de precisar
    # do operador (rumo às 5 runs simultâneas sem intervenção).
    MAX_LOGIN_ATTEMPTS = 3
    # Tentativas extras quando "senha incorreta" é detectada. Na prática este
    # erro é falso positivo quando o modal de QR code esvaziou o campo de
    # e-mail antes do submit: Meta recebe e-mail vazio + senha → rejeita como
    # "senha incorreta". Recarregamos a tela de login e tentamos de novo antes
    # de concluir que a senha é realmente errada.
    MAX_WRONG_PASSWORD_RETRIES = 2
    # Sensoriamento: o Facebook leva 5-15s para renderizar telas pós-submit;
    # o sense() re-classifica a cada POLL até a tela "assentar" ou estourar.
    SENSE_POLL_S = 2.0
    SENSE_TIMEOUT_S = 18.0

    def __init__(
        self,
        runner: "AutomationRunner",
        page: "Page",
        result: "AutomationResult",
        sensor: Optional[Callable[["Page"], Awaitable[StateDiagnosis]]] = None,
    ) -> None:
        self.runner = runner
        self.page = page
        self.result = result
        self._sensor = sensor or classify_facebook_state
        self._visits: Counter = Counter()
        self._previous: Optional[FacebookState] = None
        # Setado por handlers cujo efeito esperado é MUDAR de tela (submit
        # de credenciais/TOTP) — o sense() então aguarda a mudança.
        self._expect_change = False
        # Intervenção manual SUCEDIDA = POST_LOGIN_INDICATOR visível; a FSM
        # encerra com sucesso direto, sem depender do classifier concordar.
        self._manual_ok = False
        # Autenticado por detecção do indicador pós-login numa página que o
        # classifier não rotulou (ex.: home pós-2FA ?checkpoint_src=any) — sem
        # esperar o operador. Mesmo sinal em que a intervenção manual confia.
        self._auth_ok = False
        # Contador de tentativas de login esgotadas (bounce). Limita o
        # retry-com-rotação a MAX_LOGIN_ATTEMPTS antes do fail-fast.
        self._login_attempts = 0
        # Guard de 1 tentativa pra recuperação do modal de QR code travando
        # o login_form (ver run() — não é o mesmo orçamento de bounce real).
        self._qr_modal_recovery_used = False
        # Contador de retries de "senha incorreta" (WRONG_PASSWORD): muitas
        # vezes é falso positivo porque o modal de QR esvaziou o e-mail.
        self._wrong_password_retries = 0
        self._proxyless_attempted = False
        self.transitions: list[str] = []

    async def _goto_login(self) -> None:
        """Abre a tela de login. Com PROXY, usa timeout MAIOR (datacenter/
        residencial é lento — o goto de 45s estourava, produção 2026-06) e
        re-tenta 1x antes de propagar o timeout."""
        # getattr defensivo: o runner pode não expor _effective_proxy_cfg (fakes/
        # testes). Sem proxy → timeout padrão.
        get_cfg = getattr(self.runner, "_effective_proxy_cfg", None)
        proxied = bool(
            getattr(self.runner, "adspower_proxy", None)
            or (callable(get_cfg) and get_cfg())
        )
        timeout_ms = PLAYWRIGHT_NAV_TIMEOUT_MS_PROXY if proxied else PLAYWRIGHT_NAV_TIMEOUT_MS
        # Escapa a aba do AdsPower (start.adspower.net) antes de navegar para o
        # Facebook. A página do AdsPower pode ter handlers de beforeunload ou
        # política de extensão que aborta navegações externas. Navegando para
        # about:blank primeiro saímos do contexto da extensão/página AdsPower.
        current_url = self.page.url or ""
        if "adspower" in current_url or current_url in ("", "about:blank"):
            try:
                await self.page.goto("about:blank", wait_until="domcontentloaded", timeout=8000)
                await asyncio.sleep(0.3)
                logger.debug("AuthFSM _goto_login: navegou para about:blank para escapar da aba AdsPower.")
            except Exception:  # noqa: BLE001
                pass

        last: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                await self.page.goto(
                    META_LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms
                )
                return
            except PlaywrightTimeoutError as exc:
                last = exc
                logger.warning(
                    "AuthFSM: goto da tela de login estourou (%dms, tentativa "
                    "%d/2%s) — re-tentando.",
                    timeout_ms, attempt, " via proxy" if proxied else "",
                )
            except Exception as exc:  # noqa: BLE001
                # net::ERR_ABORTED: Facebook redireciona antes do domcontentloaded
                # (ex.: /login.php?locale=pt_BR → www.facebook.com/login.php).
                # O redirect pode ter completado — aguarda 1.5s e verifica a URL.
                if "ERR_ABORTED" in str(exc):
                    await asyncio.sleep(1.5)
                    if "facebook.com" in (self.page.url or ""):
                        logger.debug(
                            "AuthFSM goto login: ERR_ABORTED (redirect) na tentativa "
                            "%d — página em %s, prosseguindo.", attempt, self.page.url,
                        )
                        return
                last = exc
                logger.warning(
                    "AuthFSM: goto da tela de login falhou (%s, tentativa %d/2) "
                    "— re-tentando.", type(exc).__name__, attempt,
                )
        raise last  # type: ignore[misc]  # propaga o último erro

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------
    async def run(self) -> None:
        logger.info("AuthFSM: iniciando em %s", META_LOGIN_URL)
        await self._goto_login()

        handlers: dict[FacebookState, Callable[[StateDiagnosis], Awaitable[None]]] = {
            FacebookState.LOGIN_FORM: self._handle_login_form,
            FacebookState.COOKIE_BANNER: self._handle_cookie_banner,
            FacebookState.TEMPORARY_ERROR: self._handle_temporary_error,
            FacebookState.CAPTCHA: self._handle_captcha,
            FacebookState.TWO_FACTOR_TOTP: self._handle_totp,
            FacebookState.TWO_FACTOR_APPROVAL: self._handle_two_factor_approval,
            FacebookState.CHECKPOINT_GENERIC: self._handle_manual_challenge,
            FacebookState.SAVE_LOGIN_PROMPT: self._handle_save_login_prompt,
            FacebookState.REMEMBER_BROWSER: self._handle_remember_browser,
            FacebookState.UNKNOWN: self._handle_unknown,
        }

        last_diagnosis: Optional[StateDiagnosis] = None
        for step_n in range(1, self.MAX_TRANSITIONS + 1):
            # Parada cooperativa (botões Encerrar/Assumir): checa antes de cada
            # iteração — cobre o 'fica tentando login várias vezes'.
            check_stop = getattr(self.runner, "_check_stop", None)
            if callable(check_stop):
                check_stop()
            diagnosis = await self._sense()
            last_diagnosis = diagnosis
            state = diagnosis.state
            self._visits[state] += 1
            self._trace(step_n, state)

            # WRONG_PASSWORD pode ser falso positivo: o modal de QR code às
            # vezes esvazia o campo de e-mail antes do submit, fazendo o Meta
            # rejeitar com "senha incorreta" mesmo que a senha esteja certa.
            # Recarregamos a tela de login e tentamos de novo antes de declarar
            # falha terminal. Após MAX_WRONG_PASSWORD_RETRIES tentativas extras,
            # cai no bloco _TERMINAL_STATES abaixo (falha real).
            if state is FacebookState.WRONG_PASSWORD:
                # Contador total de vezes que WRONG_PASSWORD apareceu (incluindo retries)
                total_wrong_password_attempts = self._wrong_password_retries + 1

                # Se apareceu 3x = é SOFT-BLOCK (não senha realmente incorreta)
                # Soft-block = IP temporariamente bloqueado, não erro de senha
                if total_wrong_password_attempts >= 3:
                    logger.error(
                        "AuthFSM: soft block detectado! 🚫 "
                        "'Senha incorreta' apareceu %dx — "
                        "IP bloqueado temporariamente, não é erro de senha. "
                        "Conta devolvida ao estoque — tente depois.",
                        total_wrong_password_attempts,
                    )
                    # Soft-block não descarta a conta — ela volta pro estoque
                    self._fail_login_bounced()
                    return

                # Primeira ou segunda tentativa — tenta de novo (pode ser modal QR)
                if self._wrong_password_retries < self.MAX_WRONG_PASSWORD_RETRIES:
                    self._wrong_password_retries += 1
                    logger.warning(
                        "AuthFSM: WRONG_PASSWORD detectado (tentativa %d/3) "
                        "— pode ser e-mail esvaziado pelo modal QR ou soft-block. "
                        "Recarregando tela de login e re-tentando.",
                        total_wrong_password_attempts + 1,
                    )
                    try:
                        await self.page.goto(META_LOGIN_URL, wait_until="domcontentloaded")
                    except Exception as _ge:  # noqa: BLE001
                        if "ERR_ABORTED" in str(_ge):
                            await asyncio.sleep(1.5)
                        else:
                            raise
                    self._visits[FacebookState.LOGIN_FORM] = 0
                    self._visits[FacebookState.WRONG_PASSWORD] = 0
                    self._previous = None
                    self._expect_change = False
                    continue  # volta ao topo do loop; _TERMINAL_STATES não dispara

            if state is FacebookState.IDENTITY_SELFIE:
                # Selfie/identidade por vídeo no login: não automatizável.
                # _fail (via _DISCARD_AND_CLOSE) marca o perfil, descarta a conta
                # e fecha. O CNPJ é liberado pelo finalizador (sem portfólio).
                self._fail(diagnosis, reason="selfie/identidade no login")

            if state in _TERMINAL_STATES:
                self._fail(diagnosis)

            if state is FacebookState.AUTHENTICATED:
                logger.success("AuthFSM: sessão autenticada (%d transições).", step_n)
                self.result.notes.append(
                    f"auth_fsm: autenticado em {step_n} transições "
                    f"({' > '.join(self.transitions)})"
                )
                return

            if self._visits[state] > self.MAX_VISITS.get(state, 1):
                if state is FacebookState.LOGIN_FORM:
                    # Antes de QUALQUER coisa: o modal de login por QR code
                    # ('Faça a leitura com o telefone') pode ter reaparecido
                    # e travado o preenchimento/submit (campos voltam vazios,
                    # nada foi de fato submetido) — isso NÃO é bounce real
                    # (soft-block/senha errada), é obstrução de UI. Fecha o
                    # modal e refaz o preenchimento+submit 1x (pedido do
                    # operador 2026-06) ANTES de cair na lógica de bounce —
                    # que rotacionaria IP ou desistiria pelo motivo errado.
                    # Guard de 1 tentativa: não é o orçamento de bounce real
                    # (_login_attempts) — é só pra não loopar se o modal
                    # insistir em reabrir.
                    if not self._qr_modal_recovery_used:
                        login_page = FacebookLoginPage(self.page)
                        qr_was_open = await login_page.dismiss_qr_login_modal(
                            appear_timeout_ms=2000
                        )
                        self._qr_modal_recovery_used = True
                        if qr_was_open:
                            logger.warning(
                                "AuthFSM: modal de login por QR code travou o "
                                "preenchimento (login_form revisitado sem "
                                "credenciais terem sido aceitas) — fechado, "
                                "refazendo login (1x)."
                            )
                            self._visits[state] = 0
                            self._previous = None
                            self._expect_change = False
                            continue
                    # Antes de concluir 'bounce silencioso', dá uma última chance
                    # ao FB de RENDERIZAR um erro TARDIO: o alerta de senha
                    # ('A entrada Senha é inválida.' / 'senha incorreta') ou
                    # 'Encontre sua conta' aparece ~1-2s DEPOIS do form reaparecer
                    # — o _sense já tinha devolvido LOGIN_FORM e o loop bounceava
                    # às cegas, rotacionando IP à toa e mascarando o erro real
                    # (corrida confirmada por DOM, run ffe412c8 2026-06-17).
                    late = await self._reclassify_for_late_error()
                    if late is not None and late.state in _TERMINAL_STATES:
                        self._fail(late)  # diagnóstico PRECISO (levanta LoginError)
                    # 'Login infinito': captura o DOM + alertas DESTE bounce
                    # (após 1+ tentativa) — é o snapshot que o operador pode
                    # enviar p/ diagnosticarmos a causa real da rejeição.
                    await self._capture_diag("auth_login_bounce")
                    # Credenciais submetidas e o FB voltou ao MESMO formulário
                    # sem erro = bounce (soft-block de IP ou senha incorreta).
                    # NÃO é 2FA: esperar 300s pelo operador é inútil. Rotaciona
                    # IP + nova tentativa se configurado; senão, fail-fast
                    # diagnosticado (failure_state=LOGIN_BOUNCED).
                    if await self._handle_login_bounce():
                        continue  # nova tentativa após rotação de IP
                    self._fail_login_bounced()  # levanta LoginError
                if state is FacebookState.UNKNOWN:
                    # Página não reconhecida em loop e sem indicador pós-login
                    # — captura o DOM para catalogar o sinal depois (telemetria,
                    # não-fatal) antes de cair no operador.
                    await self.runner._dump_debug_html(self.page, "unknown_loop")
                # Ação repetida sem efeito — última cartada é o operador.
                logger.warning(
                    "AuthFSM: estado %s revisitado além do limite — "
                    "fallback manual.",
                    state.value,
                )
                await self._manual_intervention()
            else:
                handler = handlers.get(state)
                if handler is None:  # pragma: no cover — estados mapeados
                    self._fail(diagnosis, reason="estado sem handler")
                self._previous = state
                try:
                    await handler(diagnosis)
                except CaptchaError as exc:
                    # Captcha esgotado numa etapa de auth: usa a classificação
                    # FINA do próprio erro (captcha_unsolved/unsupported) em vez
                    # do estado da tela (two_factor_totp) — e re-levanta o MESMO
                    # erro (já é LoginError, o run_service trata igual). NÃO cai
                    # em _fail (que sobrescreveria failure_state pelo diagnóstico).
                    self.result.failure_state = exc.failure_state
                    self.result.failure_detail = str(exc)
                    trace = " > ".join(self.transitions)
                    self.result.notes.append(
                        f"auth_fsm: {exc.failure_state}: {exc} [trace={trace}]"
                    )
                    raise
                except (LoginError, PlaywrightTimeoutError) as exc:
                    # Handler falhou (ex: campo esperado não apareceu, DOM
                    # instável durante a interação). Falha com o diagnóstico
                    # pré-handler + contexto da exceção — failure_state
                    # carimbado para a UI (semântica PR1).
                    self._fail(
                        diagnosis,
                        reason=f"handler {handler.__name__} falhou: {exc}",
                    )

            if self._manual_ok or self._auth_ok:
                # Sessão autenticada: ou o indicador pós-login apareceu numa
                # página UNKNOWN (auto, _auth_ok), ou a intervenção manual o
                # confirmou (_manual_ok). Ambos terminam com sucesso.
                via = "indicador pós-login" if self._auth_ok else "intervenção manual"
                logger.success(
                    "AuthFSM: autenticado via %s (%d transições).", via, step_n
                )
                self.result.notes.append(
                    f"auth_fsm: autenticado via {via} em {step_n} "
                    f"transições ({' > '.join(self.transitions)})"
                )
                return

        # Teto global estourado: melhor diagnóstico disponível + trace.
        assert last_diagnosis is not None
        self._fail(
            last_diagnosis,
            reason=f"FSM excedeu {self.MAX_TRANSITIONS} transições",
        )

    # ------------------------------------------------------------------
    # Sensor
    # ------------------------------------------------------------------
    async def _sense(self) -> StateDiagnosis:
        """Classifica a tela, aguardando ela 'assentar' quando necessário.

        Re-classifica enquanto: (a) o estado é UNKNOWN (página possivelmente
        em transição), ou (b) um handler sinalizou que a tela DEVE mudar
        (submit) e ela ainda mostra o estado anterior.
        """
        deadline = time.monotonic() + self.SENSE_TIMEOUT_S
        diagnosis = await self._sensor(self.page)
        while time.monotonic() < deadline:
            stale = self._expect_change and diagnosis.state is self._previous
            if diagnosis.state is not FacebookState.UNKNOWN and not stale:
                break
            # Atalho: UNKNOWN com o indicador pós-login VISÍVEL = home logada
            # sem sinais conhecidos — não há por que insistir nos 18s do teto
            # re-classificando (o handler de UNKNOWN confirma e encerra).
            # ~15s economizados por login fresco (visto em produção 2026-06:
            # 8 classificações UNKNOWN em ?checkpoint_src=any até o teto).
            if diagnosis.state is FacebookState.UNKNOWN:
                try:
                    if await (
                        self.page.locator(self.runner.POST_LOGIN_INDICATOR)
                        .first.is_visible(timeout=1000)
                    ):
                        break
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self.SENSE_POLL_S)
            diagnosis = await self._sensor(self.page)
        self._expect_change = False
        return diagnosis

    # ------------------------------------------------------------------
    # Handlers (atuadores)
    # ------------------------------------------------------------------
    async def _handle_login_form(self, _diagnosis: StateDiagnosis) -> None:
        login_page = FacebookLoginPage(self.page)
        await login_page.dismiss_cookie_banner()
        # Passo 0: garante que o form de e-mail/senha está ativo e esconde o
        # botão de trigger do QR code. Facebook exibe um pequeno botão de QR
        # no canto superior direito do form; clique acidental nele substitui
        # o form pelo QR code. Dois cenários cobertos:
        #   a) QR já está ativo (clicado antes): detecta ausência do campo de
        #      e-mail e clica em "Entrar com e-mail" para voltar.
        #   b) Form visível mas botão de QR presente: esconde-o via JS para
        #      eliminar o risco de clique acidental durante o preenchimento.
        await login_page.ensure_password_login_view()
        # Meta às vezes intercepta com o modal de QR code ('Faça a leitura com
        # o telefone') sobre a tela de login. Ele renderiza com ATRASO (~6 s) e
        # fica FIXO sobre o form, interceptando o clique nos campos — sem
        # fechar, o clique estoura 'Locator.click: Timeout 30000ms' e a run
        # morre no auth. Tentativa antecipada (caso já esteja na tela) + guarda
        # por-campo (_type_login_field), que é a rede pro render tardio: se um
        # clique for interceptado, o modal JÁ está presente (marcador casa),
        # fechamos e re-tentamos.
        await login_page.dismiss_qr_login_modal()

        # LIMPEZA: clear os campos antes de preencher (podem estar com lixo de tentativas anteriores)
        try:
            email_field = await login_page.email_field()
            await email_field.clear()
        except Exception:
            pass

        await self._type_login_field(
            login_page,
            login_page.email_field,
            self.runner.credentials.email_or_phone,
            "e-mail",
        )
        await self.runner._organic_wait()

        # LIMPEZA: clear o campo de senha também
        try:
            password_field = await login_page.password_field()
            await password_field.clear()
        except Exception:
            pass

        password = await self._type_login_field(
            login_page,
            login_page.password_field,
            self.runner.credentials.password,
            "senha",
        )
        await self.runner._organic_wait()

        # Captcha pode aparecer ANTES do submit (raro); o pós-submit é
        # detectado pelo sensor e tratado por _handle_captcha.
        await self.runner._maybe_solve_captcha(self.page, self.result)

        # Reconcilia os campos ANTES do submit. O ciclo de abrir/fechar o modal
        # de QR re-monta o form controlado (React) e pode LIMPAR um campo já
        # digitado — visto: e-mail digitado OK, modal fecha, e-mail some, login
        # submete vazio → 'Encontre sua conta' (account_not_found) mascarado de
        # conta inexistente. Garante que ambos batem com o pretendido.
        password = await self._reconcile_login_fields(login_page, password)

        await self._submit_login_form(login_page, password)
        logger.info("AuthFSM: credenciais submetidas — aguardando navegação.")
        await self._settle_after_submit()
        logger.info("AuthFSM: aguardando 20s extras para a página estabilizar…")
        await asyncio.sleep(20)

    async def _reconcile_login_fields(
        self, login_page: "FacebookLoginPage", password_field: Any
    ) -> Any:
        """Re-afirma e-mail e senha imediatamente antes do submit.

        Lê o valor atual de cada campo; se divergir do pretendido (campo limpo
        por re-render do modal de QR, p.ex.), re-preenche via ``fill`` (não
        depende de cadência humana — só precisa estar correto no submit). Loga
        só tamanhos, nunca o valor. Devolve o campo de senha p/ o submit.
        """
        ident = self.runner.credentials.email_or_phone
        pwd = self.runner.credentials.password
        email_field = await login_page.email_field()
        try:
            cur = await email_field.input_value()
        except Exception:  # noqa: BLE001
            cur = None
        if cur is not None and cur != ident:
            logger.warning(
                "Campo de e-mail divergiu antes do submit (len atual=%d, "
                "esperado=%d) — re-preenchendo (provável re-render do modal).",
                len(cur), len(ident),
            )
            try:
                await email_field.fill(ident)
            except Exception:  # noqa: BLE001
                pass
        try:
            curp = await password_field.input_value()
        except Exception:  # noqa: BLE001
            curp = None
        if curp is not None and curp != pwd:
            logger.warning(
                "Campo de senha divergiu antes do submit (len atual=%d) — "
                "re-preenchendo.",
                len(curp),
            )
            try:
                await password_field.fill(pwd)
            except Exception:  # noqa: BLE001
                # Campo pode ter sido re-montado: re-resolve e preenche.
                password_field = await login_page.password_field()
                await password_field.fill(pwd)
        return password_field

    async def _type_login_field(
        self,
        login_page: "FacebookLoginPage",
        field_getter: "Callable[[], Awaitable[Any]]",
        value: str,
        label: str,
    ) -> Any:
        """Digita num campo do login com guarda contra o modal de QR.

        O modal renderiza tarde e intercepta o clique nos campos. Com um
        timeout de clique CURTO (8 s) falhamos rápido em vez de queimar os 30 s
        padrão; aí o modal já está presente (o marcador casa), fechamos
        (Escape + X) e re-tentamos UMA vez. Devolve o locator do campo — o
        submit precisa do campo de senha.
        """
        field = await field_getter()
        try:
            await self.runner._human_type(field, value, click_timeout_ms=8000)
            return field
        except PlaywrightTimeoutError:
            logger.warning(
                "Clique no campo de %s interceptado (modal de QR?) — fechando "
                "o modal e re-tentando a digitação uma vez.",
                label,
            )
        await login_page.dismiss_qr_login_modal(appear_timeout_ms=2000)
        field = await field_getter()  # re-resolve: o DOM pode ter mudado
        await self.runner._human_type(field, value, click_timeout_ms=12000)
        return field

    async def _submit_login_form(
        self, login_page: "FacebookLoginPage", password_input: Any
    ) -> None:
        """Submete o login de forma resiliente a idioma e a variações de DOM.

        Ordem de robustez:

        1. **Enter no campo de senha** — submete o ``<form id="login_form">``
           sem depender de localizar nem de rotular o botão. Locale-agnóstico:
           contas chegam com a UI do Facebook em idiomas variados (PT, EN e
           até bengali, visto em produção 2026-06) — nessas o texto
           ``entrar|log in`` não casa, e há variantes que renderizam o submit
           como ``<input type="submit">`` ou ``<div role="button">`` em vez de
           ``<button>``.
        2. **Click no controle de submit** (input/button/role=button), com
           cascata force→JS — cobre variantes onde o Enter não submete. Se o
           Enter JÁ foi enviado, a ausência do controle NÃO é fatal: a tela
           provavelmente já navegou.
        """
        enter_sent = False
        try:
            await password_input.press("Enter")
            enter_sent = True
            logger.debug("AuthFSM login: Enter pressionado no campo de senha.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("AuthFSM login: Enter no campo de senha falhou (%s).", exc)

        await self.runner._organic_wait()

        # Enter normalmente JÁ submete o form. Dá alguns segundos para a
        # navegação ocorrer ANTES de procurar/clicar o botão — o botão
        # 'Entrar' fica aria-disabled e o click esperava os 30s de timeout
        # à toa (≈30s desperdiçados POR LOGIN, visto em produção 2026-06),
        # quando o Enter já tinha levado pro 2FA. Poll curto resolve.
        if enter_sent:
            for _ in range(6):
                if not await login_page.password_visible():
                    return
                await self.runner._organic_wait()

        try:
            submit = await login_page.submit_button()
        except Exception as exc:  # noqa: BLE001
            if enter_sent:
                logger.info(
                    "AuthFSM login: sem controle de submit visível após Enter "
                    "— assumindo submissão via Enter."
                )
                return
            raise LoginError(f"submit do login não encontrado: {exc}") from exc

        # O submit costuma estar 'visible, enabled e stable' MAS fora da
        # viewport — o click normal gasta o timeout inteiro à toa (visto em
        # produção 2026-06: input[type=submit] 'outside of the viewport').
        # Vai DIRETO de force=True (ignora o check de viewport), com timeout
        # curto, e JS click como último recurso. O Enter já submeteu, então
        # falha aqui não é fatal.
        try:
            await submit.click(force=True, timeout=3000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AuthFSM: click forçado no submit falhou (%s) — JS click.", exc)
            try:
                await submit.evaluate("el => el.click()")
            except Exception as exc2:  # noqa: BLE001
                if enter_sent:
                    logger.info(
                        "AuthFSM login: click no submit falhou mas Enter "
                        "foi enviado — seguindo."
                    )
                    return
                raise LoginError(
                    f"submit do login falhou após Enter + force + JS click: "
                    f"{exc2}"
                ) from exc2

    async def _handle_cookie_banner(self, _diagnosis: StateDiagnosis) -> None:
        await FacebookLoginPage(self.page).dismiss_cookie_banner()

    async def _handle_temporary_error(self, _diagnosis: StateDiagnosis) -> None:
        """Erro temporário do Facebook — recarrega e retenta automaticamente."""
        logger.info("AuthFSM: erro temporário detectado, recarregando a página...")
        try:
            await self.page.reload(wait_until="load")
            logger.info("AuthFSM: página recarregada com sucesso.")
            # Sinaliza que esperamos uma mudança de estado (novo ciclo)
            self._expect_change = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AuthFSM: falha ao recarregar página: {e}")
            # Tenta de novo mesmo se o reload falhou — o próximo ciclo vai reclassificar
            self._expect_change = True

    async def _handle_captcha(self, _diagnosis: StateDiagnosis) -> None:
        solved = await self.runner._maybe_solve_captcha(self.page, self.result)
        # Se a corrida (manual×2captcha) esgotou E o captcha SEGUE na tela,
        # aborta classificado em vez de re-sensar até o teto de visitas e
        # cair em mais uma espera de 300s. (Captcha que sumiu no meio →
        # _has_captcha False → segue normal.)
        if not solved and await self.runner._has_captcha(self.page):
            raise CaptchaUnsolvedError(
                "Captcha (tela dedicada) não resolvido — manual e 2captcha "
                "esgotados.",
                where="captcha",
            )
        self._expect_change = True

    async def _handle_totp(self, _diagnosis: StateDiagnosis) -> None:
        """Insere o código TOTP (lógica migrada de _handle_two_factor)."""
        # Guarda antecipado: página two_step_verification pode estar EXPIRADA/STALE
        # (URL com encrypted_context antigo) e mostrar "Esta página não está disponível
        # no momento". Nenhum input aparece, mas o loop normal ficaria preso até
        # MAX_VISITS (2× 300s de fallback manual). Detectamos o texto de erro e
        # navegamos diretamente para o login para reiniciar o fluxo.
        try:
            body_text = await self.page.inner_text("body", timeout=3000)
        except Exception:  # noqa: BLE001
            body_text = ""
        if re.search(
            r"página não está disponível|page isn'?t available|"
            r"não está disponível no momento|this page isn'?t available",
            body_text, re.I,
        ):
            logger.warning(
                "AuthFSM: página two_step_verification indisponível (URL stale/expirada) "
                "— navegando para facebook.com/login para reiniciar o login."
            )
            try:
                await self.page.goto(
                    "https://www.facebook.com/login", wait_until="domcontentloaded"
                )
            except Exception as _ge:  # noqa: BLE001
                if "ERR_ABORTED" in str(_ge):
                    await asyncio.sleep(1.5)  # aguarda redirect completar
                else:
                    raise
            # Reseta visitas do LOGIN_FORM para que o handler não seja bloqueado
            # pelo MAX_VISITS do estado anterior (já visitou 0× nesta renovação).
            self._visits[FacebookState.LOGIN_FORM] = 0
            self._expect_change = True
            await self._handle_login_form(_diagnosis)
            return

        page2fa = FacebookTwoFactorPage(self.page)
        # A classificação por URL (/two_step_verification/) cobre a entrada
        # de código E o seletor de método/push. Com seed, convergimos para a
        # tela de código (escolhendo 'app autenticador' se preciso) antes de
        # desistir; sem seed, só detectamos (manual é o destino de qualquer
        # forma, pois não há código para preencher).
        if self._has_totp_seed_source():
            totp_input = await page2fa.ensure_code_input()
        else:
            totp_input = await page2fa.detect_totp_input()
        if totp_input is None:
            # CAPTCHA disfarçado de 2FA: o Meta serve Arkose/reCAPTCHA na rota
            # /two_step_verification/authentication/ (classificada como TOTP),
            # sem input de código (visto em produção 2026-06). Se há captcha,
            # resolve pela CORRIDA manual×2captcha e DEVOLVE à FSM (re-classifica
            # → a próxima tela, ex. código TOTP, é tratada automaticamente) —
            # em vez de esperar o indicador pós-login (que nunca vinha aqui).
            if await self.runner._has_captcha(self.page):
                await self.runner._dump_debug_html(self.page, "2fa_captcha")
                logger.warning(
                    "AuthFSM: captcha na tela 2FA — corrida manual×2captcha."
                )
                solved = await self.runner._solve_captcha_race(
                    self.page, self.result
                )
                if not solved:
                    # Esgotou a corrida (manual E 2captcha) sem sair do desafio.
                    # NÃO devolve à FSM: re-sensar veria two_factor_totp de novo,
                    # estouraria MAX_VISITS e cairia em OUTRA espera de 300s
                    # (produção 2026-06: ~10min até morrer com mensagem genérica).
                    # Aborta com erro classificado — fim rápido e legível.
                    raise CaptchaUnsolvedError(
                        "Captcha na tela de 2FA não resolvido (manual e 2captcha "
                        "esgotados).",
                        captcha_type=getattr(self.result, "captcha_type", None),
                        where="two_factor_totp",
                    )
                # NUDGE pós-solve (pedido do operador 2026-09): após "Captcha
                # resolvido" (token injetado), espera um delay e clica o
                # checkbox 'Não sou um robô' — alguns fluxos do FB só avançam
                # quando o widget é marcado depois do token. Configurável por
                # CAPTCHA_POST_SOLVE_CLICK_DELAY_S (0 desliga).
                if CAPTCHA_POST_SOLVE_CLICK_DELAY_S > 0:
                    logger.info(
                        "AuthFSM: captcha resolvido — aguardando %.0fs e "
                        "clicando 'Não sou um robô' (nudge).",
                        CAPTCHA_POST_SOLVE_CLICK_DELAY_S,
                    )
                    await asyncio.sleep(CAPTCHA_POST_SOLVE_CLICK_DELAY_S)
                    try:
                        await self.runner._click_recaptcha_checkbox(self.page)
                    except Exception:  # noqa: BLE001
                        pass

                # Captcha resolvido: a tela AVANÇA de /authentication/ (captcha)
                # para /two_factor/ (código) em alguns segundos. As DUAS telas
                # têm a MESMA classificação (two_factor_totp), então devolver à
                # FSM aqui gasta uma 'visita' e estoura o MAX_VISITS ANTES de o
                # código aparecer (produção 2026-09, conta Taissa Nayara: 3
                # captchas resolvidos e nunca digitou o código). Em vez disso,
                # ESPERAMOS o input de código materializar e digitamos o TOTP na
                # MESMA passada. FB às vezes encadeia 2 desafios → re-resolve.
                if self._has_totp_seed_source():
                    for _ in range(30):  # ~60s (2s por rodada)
                        await asyncio.sleep(2.0)
                        try:
                            totp_input = await page2fa.ensure_code_input()
                        except Exception:  # noqa: BLE001
                            totp_input = None
                        if totp_input is not None:
                            logger.info(
                                "AuthFSM: pós-captcha, tela de código 2FA "
                                "apareceu — preenchendo o TOTP na mesma passada."
                            )
                            break
                        try:
                            if await self.runner._has_captcha(self.page):
                                await self.runner._solve_captcha_race(
                                    self.page, self.result
                                )
                        except Exception:  # noqa: BLE001
                            pass
                if totp_input is None:
                    # Não materializou o código no tempo — devolve à FSM (que
                    # re-sensa; se persistir, cai no fallback manual).
                    self._expect_change = True
                    return
                # totp_input encontrado → cai no fluxo de digitação abaixo.
            # BOUNCE pro login: o Meta às vezes RECARREGA o form de login
            # (email+senha) na PRÓPRIA rota /two_step_verification/ após um
            # erro transitório — a URL ainda diz 2FA, mas o DOM é o login
            # (visto em produção 2026-06, conta Cleber: inputs name=email +
            # name=pass nessa tela; é o 'voltou pro login' do manual).
            # Re-submete as credenciais em vez de cair no fallback manual.
            elif await FacebookLoginPage(self.page).password_visible(timeout_ms=1500):
                logger.warning(
                    "AuthFSM: form de LOGIN recarregado na rota 2FA (bounce "
                    "transitório do Meta) — re-submetendo credenciais."
                )
                await self._handle_login_form(_diagnosis)
                self._expect_change = True
                return
            else:
                # Página pode estar carregando (React shell vazio após
                # captcha/transição) — espera até 30s antes de desistir.
                for _retry in range(6):
                    await asyncio.sleep(5.0)
                    totp_input = await page2fa.ensure_code_input()
                    if totp_input is not None:
                        logger.info(
                            "AuthFSM 2FA: input apareceu após %ds de espera "
                            "(página lenta).", (_retry + 1) * 5,
                        )
                        break
                    if await self.runner._has_captcha(self.page):
                        logger.info("AuthFSM 2FA: captcha surgiu durante espera — resolvendo.")
                        await self.runner._solve_captcha_race(self.page, self.result)
                if totp_input is None:
                    await self.runner._dump_debug_html(self.page, "2fa_no_input")
                    logger.warning("AuthFSM: tela 2FA sem input detectável — fallback manual.")
                    await self._manual_intervention()
                    return

        current_url = self.page.url
        if (
            "/two_step_verification/" not in current_url
            and "/checkpoint/" not in current_url
        ):
            logger.warning(
                "AuthFSM 2FA: página navegou para %s durante busca do input "
                "— devolvendo à FSM para re-classificação.",
                current_url[:120],
            )
            self._expect_change = True
            return

        code = await self._resolve_totp_code()
        if code is None:
            await self._manual_intervention()
            return

        await self.runner._human_type(totp_input, code)
        await self.runner._organic_wait()
        # READ-BACK: confirma que o campo recebeu o código EXATO (digitação às
        # vezes engole/duplica o 1º char em inputs React → código errado → 2FA
        # rejeitado e 'login infinito' que parecia soft-block). Loga divergência.
        try:
            entered = (await totp_input.input_value()) or ""
            entered_digits = "".join(ch for ch in entered if ch.isdigit())
            if entered_digits != code:
                logger.warning(
                    "AuthFSM 2FA: campo divergente do código gerado! digitado=%r "
                    "(%d díg) ≠ TOTP=%s — provável bug de digitação, não senha/IP.",
                    entered, len(entered_digits), code,
                )
            else:
                logger.info("AuthFSM 2FA: código %s confirmado no campo.", code)
        except Exception:  # noqa: BLE001
            pass

        # LOG DETALHADO: qual código foi gerado e quando
        import time as time_module
        secs_left = 30 - (int(time_module.time()) % 30)
        logger.info("🔐 [DEBUG 2FA] Código confirmado: %s | Tempo restante: %ds | Horário: %s",
                   code or "NÃO GERADO", secs_left, time_module.strftime("%H:%M:%S"))

        await self._capture_diag("auth_2fa_before_submit")
        # Estratégia de submit em ordem de preferência:
        # 1. Enter no próprio input (gesto humano, submete o form sem
        #    depender do botão estar na viewport).
        # 2. Click no botão com force=True (bypassa o check de viewport
        #    que faz o Playwright travar 30s).
        # 3. JS click direto no elemento.
        submitted = False
        try:
            await totp_input.press("Enter")
            submitted = True
            logger.debug("AuthFSM 2FA: Enter pressionado no input TOTP.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("AuthFSM 2FA: Enter falhou (%s) — tentando botão.", exc)
        if not submitted:
            try:
                confirm = await FacebookTwoFactorPage(self.page).confirm_button()
                try:
                    await confirm.click(force=True, timeout=8000)
                    logger.debug("AuthFSM 2FA: click forçado no botão de submit.")
                except Exception:
                    await confirm.evaluate("el => el.click()")
                    logger.debug("AuthFSM 2FA: JS click no botão (último recurso).")
            except Exception as exc:  # noqa: BLE001
                logger.warning("AuthFSM 2FA: nenhum método de submit funcionou (%s).", exc)
        await self._settle_after_submit()
        # Aguarda a navegação ser concluída após o submit do 2FA
        # (a página pode estar recarregando/atualizando)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            logger.info("AuthFSM 2FA: navegação pós-submit concluída.")
        except Exception as e:
            logger.debug("AuthFSM 2FA: timeout aguardando navegação (%s) — continuando.", e)

        # Captura o RESULTADO do submit do 2FA: se o FB voltar ao login (bounce),
        # este DOM/alerta mostra o motivo real (código incorreto? sessão expirada?
        # 'tente novamente'?) — o cerne do 'login infinito'.
        await self._capture_diag("auth_2fa_after_submit")

    async def _handle_two_factor_approval(self, diagnosis: StateDiagnosis) -> None:
        """Push em outro dispositivo → tenta converter para app autenticador.

        Se a conta tem seed TOTP (credenciais ou AdsPower fakey), clica em
        'Try another way' e escolhe 'Authentication app' — o sense seguinte
        cai em TWO_FACTOR_TOTP e _handle_totp completa sozinho. Sem seed, ou
        se a troca falhar, cai no operador (comportamento anterior).
        """
        if not self._has_totp_seed_source():
            logger.warning(
                "AuthFSM: aprovação push sem seed TOTP disponível — operador."
            )
            await self._manual_intervention()
            return

        switched = await FacebookTwoFactorPage(self.page).switch_to_authenticator_app()
        if switched:
            logger.info(
                "AuthFSM: 2FA alternado de push para app autenticador — "
                "preenchendo código no próximo passo."
            )
            self._expect_change = True
            return

        logger.warning(
            "AuthFSM: não foi possível alternar push→autenticador (%s) — operador.",
            diagnosis.description,
        )
        await self._manual_intervention()

    def _has_totp_seed_source(self) -> bool:
        """Há origem de seed TOTP? (credencial explícita ou perfil AdsPower)."""
        return bool(
            (self.runner.credentials.totp_seed or "").strip()
            or self.runner.adspower_user_id
        )

    async def _handle_manual_challenge(self, diagnosis: StateDiagnosis) -> None:
        """Checkpoint genérico → operador."""
        logger.warning(
            "AuthFSM: desafio requer operador (%s) — %s",
            diagnosis.state.value,
            diagnosis.description,
        )
        await self._manual_intervention()

    async def _handle_save_login_prompt(self, _diagnosis: StateDiagnosis) -> None:
        """Prompt 'Salvar informações de login?' = login OK; dispensa e segue."""
        dismissed = await FacebookLoginPage(self.page).dismiss_save_login_prompt()
        if dismissed:
            logger.info("AuthFSM: prompt 'Salvar informações de login' dispensado.")
        self._expect_change = True

    async def _handle_remember_browser(self, _diagnosis: StateDiagnosis) -> None:
        """'Lembrar deste navegador?' pós-2FA = login OK; confirma e segue.

        A sessão JÁ está autenticada neste ponto (o 2FA passou). O 'lembrar
        navegador' é só um interstício opcional. Em produção 2026-06 (conta
        Fabio) o clique de confirmação deixou a página em LOAD INFINITO — ela
        nunca navegava, então a FSM re-sensava remember_browser até o teto de
        visitas e caía no operador (300s à toa).

        Por isso NÃO dependemos da página se auto-navegar: confirmamos
        (best-effort, para pular 2FA no futuro) e então vamos PROATIVAMENTE à
        home, verificando o indicador pós-login. Se ele aparece, encerramos a
        FSM como autenticada (``_auth_ok``).
        """
        confirmed = await FacebookTwoFactorPage(self.page).confirm_remember_browser()
        if confirmed:
            logger.info(
                "AuthFSM: 'lembrar navegador' confirmado — perfil pulará 2FA "
                "nos próximos logins."
            )
        if await self._confirm_authenticated_via_home():
            logger.success(
                "AuthFSM: sessão autenticada após 'lembrar navegador' "
                "(indicador pós-login presente na home)."
            )
            self._auth_ok = True
            return
        # Home não confirmou de imediato — deixa a FSM re-sensar (a navegação
        # pode estar só lenta); o teto de visitas ainda protege contra loop.
        self._expect_change = True

    async def _confirm_authenticated_via_home(self) -> bool:
        """Navega à home e checa o indicador pós-login (robusto a load infinito).

        ``goto`` cancela qualquer navegação pendente da página travada. Tudo
        best-effort com timeouts curtos — nunca levanta.
        """
        try:
            await self.page.goto(
                "https://www.facebook.com/",
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("remember_browser: goto home falhou (%s).", exc)
        try:
            return await (
                self.page.locator(self.runner.POST_LOGIN_INDICATOR)
                .first.is_visible(timeout=8000)
            )
        except Exception:  # noqa: BLE001
            return False

    async def _handle_unknown(self, _diagnosis: StateDiagnosis) -> None:
        # LGPD/cookies: quando a tela NÃO reconhecida é o interstício de
        # consentimento (/privacy/consent/ ou lgpd_migrated), dispensa clicando
        # através dele (Começar → Aceitar e continuar → Fechar) AQUI DENTRO. Sem
        # isto o AuthFSM ficava em loop de 'unknown' até o teto e caía no manual
        # de 300s (produção 2026-09, conta Tatiana). Depois de dispensar, a home
        # carrega e o indicador pós-login aparece na próxima classificação.
        _url = (self.page.url or "").lower()
        if "/privacy/consent" in _url or "lgpd_migrated" in _url:
            logger.info(
                "AuthFSM: consentimento (LGPD/cookies) em UNKNOWN — dispensando "
                "antes de seguir."
            )
            try:
                await self.runner._dismiss_consent_flow(self.page)
            except Exception:  # noqa: BLE001
                pass
            self._expect_change = True
            await asyncio.sleep(self.SENSE_POLL_S)
            return

        # Página possivelmente em transição/render lento: o indicador
        # pós-login pode aparecer sem que o classifier reconheça a tela.
        try:
            await self.page.wait_for_selector(
                self.runner.POST_LOGIN_INDICATOR, timeout=12000
            )
            # Indicador pós-login presente numa página que o classifier não
            # rotulou (ex.: home pós-2FA em web.facebook.com/?checkpoint_src=any,
            # cujo HTML não casa as regras AUTHENTICATED). É o MESMO sinal em que
            # a intervenção manual confia — então autenticamos direto, sem
            # esperar o operador 300s nem loopar até o teto de visitas.
            logger.success(
                "AuthFSM: indicador pós-login presente em página UNKNOWN — "
                "sessão autenticada."
            )
            self._auth_ok = True
            return
        except Exception:  # noqa: BLE001 — timeout ou elemento não existe
            pass

        # Sondagem oportunista: tela não reconhecida que NA VERDADE é uma
        # entrada de código 2FA (variante de checkpoint não catalogada). Se
        # há input de código E temos seed, preenche em vez de desistir — é
        # exatamente o "qualquer página diferente interrompe" que queremos
        # eliminar sem catalogar cada variação do Facebook.
        if self._has_totp_seed_source():
            totp_input = await FacebookTwoFactorPage(self.page).detect_totp_input(
                timeout_ms=4000
            )
            if totp_input is not None:
                logger.info(
                    "AuthFSM: input de código detectado em página UNKNOWN — "
                    "tratando como 2FA TOTP."
                )
                await self._handle_totp(_diagnosis)
                return

        logger.debug(
            "AuthFSM: UNKNOWN sem indicador pós-login nem input de código "
            "— aguardando nova classificação (visita %d/%d).",
            self._visits[FacebookState.UNKNOWN],
            self.MAX_VISITS.get(FacebookState.UNKNOWN, 3)
        )
        await asyncio.sleep(self.SENSE_POLL_S)

    # ------------------------------------------------------------------
    # Apoio
    # ------------------------------------------------------------------
    async def _manual_intervention(self) -> None:
        """Aguarda o operador; sucesso = indicador pós-login visível.

        ``_await_manual_intervention`` levanta LoginError no timeout, então
        chegar à linha seguinte significa sessão autenticada — o loop
        principal encerra com sucesso ao ver ``_manual_ok``.
        """
        await self.runner._await_manual_intervention(self.page, self.result)
        self._manual_ok = True

    async def _settle_after_submit(self) -> None:
        self._expect_change = True
        try:
            # Aguarda domcontentloaded E networkidle para garantir que a
            # navegação REALMENTE terminou (não é só um reload intermediário).
            # Sem networkidle, pode voltar ao login_form se o FB recarregar
            # a tela durante a transição para 2FA.
            await self.page.wait_for_load_state(
                "networkidle", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001 — timeout de load não é fatal
            # Se networkidle estourar, tenta só domcontentloaded (fallback)
            try:
                await self.page.wait_for_load_state(
                    "domcontentloaded", timeout=5000
                )
            except Exception:
                pass

    async def _resolve_totp_code(self) -> Optional[str]:
        """Resolve a seed (credenciais → AdsPower fakey) e gera o código.

        ``None`` significa "sem código utilizável" — o chamador cai no
        fallback manual (mesma semântica do fluxo antigo).
        """
        raw_seed = self.runner.credentials.totp_seed or ""
        source = "credentials.totp_seed"
        if not raw_seed and self.runner.adspower_user_id:
            # Em modo AdsPower o segredo TOTP vive no perfil (campo `fakey`);
            # pyotp.TOTP(fakey).now() reproduz o código do painel.
            try:
                fetched = await self.runner.adspower.get_totp_seed(
                    self.runner.adspower_user_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("AuthFSM: falha ao ler fakey do AdsPower: %s", exc)
                fetched = None
            if fetched:
                raw_seed = fetched
                source = f"AdsPower fakey (user_id={self.runner.adspower_user_id})"

        if not raw_seed:
            logger.warning("AuthFSM: nenhuma seed TOTP disponível — fallback manual.")
            return None
        seed = self.runner._normalize_totp_seed(raw_seed)
        if not seed:
            logger.warning("AuthFSM: seed TOTP vazia após normalização — fallback manual.")
            return None
        try:
            totp = pyotp.TOTP(seed)
            code = totp.now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AuthFSM: seed TOTP inválida (%s) — fallback manual.", exc)
            return None

        # PROTEÇÃO CRÍTICA: se código vai expirar em < 10s, esperar nova janela
        # Evita: código expirar durante preenchimento/submissão → soft-block silencioso
        secs_into = int(time.time()) % 30
        secs_remaining = 30 - secs_into
        if secs_remaining < 10:
            logger.warning(
                "AuthFSM: TOTP=%s expirando em %ds (< 10s) — aguardando nova janela.",
                code, secs_remaining,
            )
            import asyncio
            await asyncio.sleep(float(secs_remaining) + 1.0)  # Espera expirar + 1s margem
            # Gera novo código na próxima janela
            code = totp.now()
            secs_into = int(time.time()) % 30
            secs_remaining = 30 - secs_into
            logger.info("AuthFSM: novo TOTP gerado (janela=%ds/30 — %ds p/ expirar).",
                        secs_into, secs_remaining)

        # DIAGNÓSTICO DETALHADO (pedido do operador p/ o 'login infinito'): o
        # código é efêmero (30s) e são CONTAS DO PRÓPRIO operador, então logar o
        # valor + a posição na janela de 30s ajuda a distinguir as causas do 2FA
        # rejeitado: seed errada (código não casa) × relógio fora de sync (código
        # certo mas expirado) × soft-block (código certo, FB rejeita mesmo).
        logger.info(
            "AuthFSM: TOTP=%s (fonte=%s, seed_len=%d, janela=%ds/30 — %ds p/ "
            "expirar). Se o FB rejeitar este código VÁLIDO, é soft-block, não seed.",
            code, source, len(seed), secs_into, secs_remaining,
        )
        return code

    def _trace(self, step_n: int, state: FacebookState) -> None:
        self.transitions.append(state.value)
        logger.info(
            "AuthFSM #%d: %s → %s",
            step_n,
            self._previous.value if self._previous else "início",
            state.value,
        )

    async def _capture_diag(self, tag: str) -> None:
        """Captura DETALHADA p/ diagnóstico de auth (pedido do operador): loga a
        URL + TODO texto de ALERTA/erro visível na tela + dumpa o DOM com o tag
        em .state/. É o que revela POR QUE o 2FA é rejeitado / o login infinito —
        a mensagem do FB que o classificador não promove a um estado. Best-effort,
        nunca levanta (roda em caminho que já pode estar falhando)."""
        url = ""
        try:
            url = self.page.url
        except Exception:  # noqa: BLE001
            pass
        alerts: list[str] = []
        try:
            alerts = await asyncio.wait_for(
                self.page.evaluate(
                    """() => {
                        const seen = new Set(), out = [];
                        const push = (t) => { t = (t||'').trim();
                            if (t && t.length <= 200 && !seen.has(t)) { seen.add(t); out.push(t); } };
                        document.querySelectorAll('[role="alert"]').forEach(e => push(e.innerText));
                        // banners de erro comuns do FB sem role=alert
                        document.querySelectorAll('[aria-live="polite"],[aria-live="assertive"]').forEach(e => push(e.innerText));
                        return out.slice(0, 8);
                    }"""
                ),
                timeout=4.0,
            )
        except Exception:  # noqa: BLE001
            alerts = []
        logger.info(
            "AuthFSM diag [%s]: url=%s | alertas=%s",
            tag,
            (url[:100] + "…") if len(url) > 100 else (url or "?"),
            alerts or "(nenhum)",
        )
        try:
            await self.runner._dump_debug_html(self.page, tag)
        except Exception:  # noqa: BLE001
            pass

    # Janela extra p/ o FB renderizar um erro de login TARDIO antes de concluir
    # bounce. Curta de propósito: o caminho já gastou ~44s re-sentindo o form.
    _LATE_ERROR_POLLS = 2

    async def _reclassify_for_late_error(self) -> Optional[StateDiagnosis]:
        """Re-classifica algumas vezes para capturar um erro de login que o FB
        renderiza com ATRASO (o alerta entra no innerText 1-2s DEPOIS do form
        reaparecer, então o _sense já devolveu LOGIN_FORM e o loop ia bouncear
        às cegas — rotação de IP inútil mascarando 'senha inválida', run
        ffe412c8 2026-06-17). Devolve o diagnóstico se virar um estado
        != LOGIN_FORM/UNKNOWN (revelou o erro real); senão None (bounce
        silencioso de fato)."""
        for _ in range(self._LATE_ERROR_POLLS):
            await asyncio.sleep(self.SENSE_POLL_S)
            try:
                diag = await self._sensor(self.page)
            except Exception:  # noqa: BLE001
                return None
            if diag.state not in (FacebookState.LOGIN_FORM, FacebookState.UNKNOWN):
                logger.info(
                    "AuthFSM: erro de login TARDIO detectado antes do bounce: "
                    "%s (sinais=%s).",
                    diag.state.value, diag.matched or "—",
                )
                return diag
        return None

    async def _handle_login_bounce(self) -> bool:
        """Bounce de login: fecha browser, remove proxy, reabre sem proxy.

        Devolve ``True`` se reabriu sem proxy e re-navegou (o loop deve tentar
        de novo); ``False`` se já tentou proxyless ou não usa AdsPower — aí o
        chamador faz fail-fast.

        Fluxo: bounce com proxy → fecha browser → tira proxy do perfil →
        reabre → retry. Se ainda bouncea → tag CONTA BUGADA.
        """
        if self._proxyless_attempted:
            logger.warning(
                "AuthFSM: bounce APÓS retry sem proxy — marcando CONTA BUGADA.",
            )
            self._mark_account_discard("CONTA BUGADA")
            return False

        has_adspower = bool(
            getattr(self.runner, "adspower", None)
            and getattr(self.runner, "adspower_user_id", None)
        )
        if not has_adspower:
            return False

        self._proxyless_attempted = True
        self._login_attempts += 1
        logger.warning(
            "AuthFSM: bounce de login (tentativa %d/%d) — fechando browser, "
            "removendo proxy e reabrindo sem proxy.",
            self._login_attempts,
            self.MAX_LOGIN_ATTEMPTS,
        )

        try:
            new_page = await self.runner._restart_browser_without_proxy()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "AuthFSM: falha ao reabrir browser sem proxy: %s", exc,
            )
            return False

        self.page = new_page
        self.runner._active_page = new_page
        self.runner._proxyless_login = True

        try:
            await self.page.goto(META_LOGIN_URL, wait_until="domcontentloaded")
        except Exception as _ge:  # noqa: BLE001
            if "ERR_ABORTED" in str(_ge):
                await asyncio.sleep(1.5)
            else:
                raise
        self._visits[FacebookState.LOGIN_FORM] = 0
        self._previous = None
        self._expect_change = False
        return True

    _POST_LOGIN_STATES = frozenset({
        FacebookState.CAPTCHA,
        FacebookState.TWO_FACTOR_TOTP,
        FacebookState.TWO_FACTOR_APPROVAL,
        FacebookState.CHECKPOINT_GENERIC,
        FacebookState.SAVE_LOGIN_PROMPT,
        FacebookState.REMEMBER_BROWSER,
    })

    def _fail_login_bounced(self) -> None:
        """Fail-fast diagnosticado para bounce de login (sem espera de 300s)."""
        if any(s in self._POST_LOGIN_STATES for s in self._visits):
            self.result.bounce_post_auth_step = True
            logger.warning(
                "AuthFSM: bounce PÓS captcha/2FA — provável bloqueio de "
                "proxy (login aceito, bounce na verificação)."
            )
        diagnosis = StateDiagnosis(
            state=FacebookState.LOGIN_BOUNCED,
            description=LOGIN_BOUNCED_DESCRIPTION,
            terminal=False,
        )
        self._fail(diagnosis, reason="login bounced (soft-block de IP ou senha incorreta)")

    def _mark_account_discard(self, tag: str) -> None:
        """Erro terminal: marca pra revisão manual (NÃO descarta automaticamente).

        Conta fica em "pending_discard" — operador decide se descarta via painel.
        O finalizador fecha o perfil (marcando o nome com a tag) e libera o CNPJ.
        """
        email = (self.runner.credentials.email_or_phone or "").strip()
        try:
            from core import store  # noqa: PLC0415 — import tardio evita ciclo

            # Marca como pendente de discard em vez de descartar imediatamente
            store.mark_account_pending_discard(email, tag)
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao marcar conta pendente discard no store (%s).", exc)

        self.result.account_discarded = False  # NÃO foi descartada ainda
        self.result.force_close_profile = True
        self.result.profile_error_tag = tag
        logger.warning(
            "AuthFSM: erro terminal [%s] — conta %s marcada PENDENTE DE DISCARD "
            "(revisar no painel antes de descartar); perfil será marcado com o motivo e fechado.",
            tag, email or "?",
        )

    def _fail(self, diagnosis: StateDiagnosis, reason: Optional[str] = None) -> None:
        # Estado é um erro que descarta a conta + marca/fecha o perfil?
        tag = _DISCARD_AND_CLOSE.get(diagnosis.state)
        if tag:
            self._mark_account_discard(tag)
        self.result.failure_state = diagnosis.state.value
        self.result.failure_detail = diagnosis.description
        trace = " > ".join(self.transitions)
        prefix = f"{reason}. " if reason else ""
        self.result.notes.append(f"auth_fsm: {prefix}{diagnosis.summary()} [trace={trace}]")
        raise LoginError(f"{prefix}{diagnosis.summary()}")
