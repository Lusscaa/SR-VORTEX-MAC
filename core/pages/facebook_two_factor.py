"""Page Object da tela de 2FA (checkpoint) do Facebook.

Centraliza os seletores do input TOTP e do botão de confirmação —
migrados verbatim de ``AutomationRunner._detect_totp_input`` e do bloco
de submit de ``_handle_two_factor``. A LÓGICA do fluxo 2FA (geração de
código, cascata Enter→click→JS, fallback manual) permanece em
``automation.py``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .base import BasePage

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = logging.getLogger("meta_automation.pages")

# O Facebook tipicamente leva 5-15s após o submit do login para renderizar
# a tela 2FA — um único wait_for combinado com este teto evita desistir
# antes do form aparecer.
TOTP_DETECT_TIMEOUT_MS = 15000


class FacebookTwoFactorPage(BasePage):
    def totp_input_candidates(self) -> list["Locator"]:
        # Cobertura: desktop checkpoint, mobile checkpoint, "Two-factor",
        # "Confirme sua identidade", "Go to your authentication app" (TOTP UI
        # nova do Facebook 2025+, sem name/aria-label/placeholder no input —
        # identificável só pelo <label for> ou pela proximidade do h2).
        page = self.page
        return [
            page.locator('input[name="approvals_code"]'),
            page.locator('input[autocomplete="one-time-code"]'),
            page.locator('input[id^="checkpoint"][type="text"]'),
            # TOTP novo (2025+) na rota /two_step_verification/authentication/:
            # input React com id="_r_N_", autocomplete="off", type="text" e
            # <label for="_r_N_">Código</label> (DOM salvo em produção 2026-06).
            page.locator('input[id^="_r_"][autocomplete="off"][type="text"]'),
            page.locator('input[id*="code" i]'),
            page.locator('input[aria-label*="c[oó]digo" i]'),
            page.locator('input[aria-label*="code" i]'),
            page.locator('input[placeholder*="c[oó]digo" i]'),
            page.locator('input[placeholder*="6" i][inputmode="numeric"]'),
            page.get_by_label("Code", exact=True),
            page.get_by_label("Código", exact=True),
            page.get_by_label(
                re.compile(r"c[oó]digo|code|verifica|two-?factor|2fa", re.I)
            ),
            page.get_by_role(
                "textbox", name=re.compile(r"c[oó]digo|code|verifica", re.I)
            ),
            page.locator('input[inputmode="numeric"]'),
        ]

    def confirm_candidates(self) -> list["Locator"]:
        return [
            self.page.get_by_role(
                "button",
                name=re.compile(
                    r"continuar|continue|enviar|submit|verificar|verify|"
                    r"pr[oó]ximo|next|confirmar",
                    re.I,
                ),
            ),
            self.page.locator('input[type="submit"]'),
            self.page.locator('button[type="submit"]'),
            self.page.locator('[role="button"][tabindex="0"]'),
        ]

    async def detect_totp_input(
        self, timeout_ms: int = TOTP_DETECT_TIMEOUT_MS
    ) -> Optional["Locator"]:
        """Detecta o input de código 2FA, ou ``None`` se não aparecer.

        Combina todos os candidatos via ``Locator.or_`` e usa um ÚNICO
        ``wait_for`` — preserva o timing da implementação original.
        ``timeout_ms`` menor serve para sondagem rápida (ex.: probe em
        páginas UNKNOWN sem pagar os 15s do fluxo 2FA normal).
        """
        candidates = self.totp_input_candidates()
        combined = candidates[0]
        for extra in candidates[1:]:
            combined = combined.or_(extra)

        target = combined.first
        try:
            await target.wait_for(state="visible", timeout=timeout_ms)
            return target
        except PlaywrightTimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("detect_totp_input: erro inesperado em wait_for: %s", exc)
            return None

    async def confirm_button(self) -> "Locator":
        return await self.first_visible(self.confirm_candidates())

    # ------------------------------------------------------------------
    # "Try another way" → app autenticador
    #
    # Quando o Facebook escolhe 2FA por aprovação em outro dispositivo
    # (push para o celular), há um botão "Try another way" que abre o menu
    # de métodos. Se a conta tem app autenticador (seed TOTP no AdsPower
    # `fakey`), selecionar "Authentication app" leva à tela de código — que
    # o bot SABE preencher. Converte um desafio não-automatizável (push) no
    # caminho automatizável (TOTP).
    # ------------------------------------------------------------------
    def try_another_way_candidates(self) -> list["Locator"]:
        text = re.compile(
            r"try another way|try a different way|use another method|"
            r"tentar (outr[ao]|de outra)|outro m[eé]todo|usar outra forma",
            re.I,
        )
        return [
            self.page.get_by_role("button", name=text),
            self.page.locator('button[name="try_another_way"]'),
            self.page.get_by_text(text),
        ]

    def authenticator_option_candidates(self) -> list["Locator"]:
        # FB renderiza as opções como role=radio (divs React) ou input radio.
        text = re.compile(
            r"authenticat(ion|or) app|app(licativo)? de autentica[çc]", re.I
        )
        return [
            self.page.get_by_role("radio", name=text),
            self.page.get_by_label(text),
            self.page.get_by_text(text),
        ]

    async def switch_to_authenticator_app(self) -> bool:
        """Push → app autenticador. Devolve True se caiu na tela de código.

        Best-effort em 4 passos; qualquer passo que falhe aborta com False
        (o chamador então cai no operador). NÃO levanta.
        """
        try:
            btn = await self.first_visible(
                self.try_another_way_candidates(), timeout_ms=6000
            )
            await btn.click()
        except Exception as exc:  # noqa: BLE001
            logger.debug("switch_to_authenticator: 'try another way' ausente (%s)", exc)
            return False

        try:
            opt = await self.first_visible(
                self.authenticator_option_candidates(), timeout_ms=6000
            )
            await opt.click()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "switch_to_authenticator: opção 'authentication app' ausente (%s)", exc
            )
            return False

        # Botão de continuar é opcional — alguns fluxos React avançam sozinhos.
        try:
            cont = await self.first_visible(self.confirm_candidates(), timeout_ms=3000)
            await cont.click()
        except Exception:  # noqa: BLE001
            logger.debug("switch_to_authenticator: sem 'continuar' explícito — seguindo.")

        # Confirma que a tela de código TOTP materializou.
        return (await self.detect_totp_input()) is not None

    async def select_authenticator_directly(self, timeout_ms: int = 3000) -> bool:
        """Clica numa opção 'app autenticador' JÁ visível na tela.

        Diferente de ``switch_to_authenticator_app`` (que primeiro abre o
        menu via 'try another way'), aqui a tela já é o seletor de método —
        as opções estão à mostra. Devolve True se clicou numa opção. NÃO
        levanta: qualquer ausência vira False.
        """
        try:
            opt = await self.first_visible(
                self.authenticator_option_candidates(), timeout_ms=timeout_ms
            )
            await opt.click()
        except Exception as exc:  # noqa: BLE001
            logger.debug("select_authenticator_directly: opção ausente (%s)", exc)
            return False

        # 'Continuar' é opcional — alguns fluxos React avançam sozinhos.
        try:
            cont = await self.first_visible(self.confirm_candidates(), timeout_ms=2500)
            await cont.click()
        except Exception:  # noqa: BLE001
            logger.debug("select_authenticator_directly: sem 'continuar' explícito.")
        return True

    async def ensure_code_input(self) -> Optional["Locator"]:
        """Converge para a tela de código TOTP a partir de QUALQUER landing
        do fluxo ``/two_step_verification/`` e devolve o input (ou None).

        A regra de classificação usa a URL ``/two_step_verification/`` como
        sinal estável, mas essa URL cobre TRÊS telas distintas:

        * (a) entrada de código — a única com input;
        * (b) seletor de método ('escolha uma forma de confirmar');
        * (c) aprovação push ('confira suas notificações').

        Só (a) tem o input que ``_handle_totp`` sabe preencher. Para (b)/(c)
        é preciso escolher 'app autenticador' antes — senão o handler via
        apenas a ausência do input e caía direto no operador (visto em
        produção 2026-06: conta com 2FA por app autenticador, cuja tela de
        método não casava com o texto da regra APPROVAL, ia para manual).
        """
        found = await self.detect_totp_input()
        if found is not None:
            return found
        if not self._is_on_2fa_url():
            logger.debug("ensure_code_input: página saiu do 2FA após detect — abortando.")
            return None
        # (b) seletor de método já à mostra → clica 'app autenticador'.
        if await self.select_authenticator_directly():
            found = await self.detect_totp_input()
            if found is not None:
                return found
        if not self._is_on_2fa_url():
            logger.debug("ensure_code_input: página saiu do 2FA após select — abortando.")
            return None
        # (c) push/approval → 'try another way' → 'app autenticador'.
        if await self.switch_to_authenticator_app():
            found = await self.detect_totp_input()
            if found is not None:
                return found
        if not self._is_on_2fa_url():
            logger.debug("ensure_code_input: página saiu do 2FA após switch — abortando.")
            return None
        # (d) fallback AGNÓSTICO DE IDIOMA. Contas farmadas renderizam o 2FA
        # no idioma nativo da conta (produção 2026-06: bengali — heading
        # 'আপনার প্রমাণীকরণ অ্যাপে যান', label 'কোড'), sem name/aria/placeholder
        # reconhecíveis pelos seletores pt/en. Como já estamos na URL
        # /two_step_verification/ e a tela de código tem UM único input de
        # texto (+ submit), pegá-lo direto é seguro e independe do idioma.
        return await self._lone_text_code_input()

    def _is_on_2fa_url(self) -> bool:
        url = self.page.url
        return "/two_step_verification/" in url or "/checkpoint/" in url

    async def _lone_text_code_input(
        self, timeout_ms: int = 4000
    ) -> Optional["Locator"]:
        """Último recurso: o único input de texto da tela de código 2FA.

        NÃO entra em ``totp_input_candidates`` (usado também para sondar
        páginas UNKNOWN) porque um ``input[type=text]`` genérico daria
        falso-positivo em telas com campo de busca/e-mail. Aqui só é chamado
        de dentro de ``ensure_code_input``, já confirmados na URL de 2FA.
        """
        if not self._is_on_2fa_url():
            logger.debug(
                "_lone_text_code_input: URL não é 2FA (%s) — ignorando.",
                self.page.url[:80],
            )
            return None
        candidates = [
            # Variante React nova vista em produção (sem name/aria/placeholder).
            self.page.locator('input[autocomplete="off"][type="text"]'),
            # Genérico: qualquer input visível que não seja botão/oculto.
            self.page.locator(
                'input:not([type="hidden"]):not([type="submit"])'
                ':not([type="button"]):not([type="checkbox"])'
            ),
        ]
        for loc in candidates:
            try:
                target = loc.first
                await target.wait_for(state="visible", timeout=timeout_ms)
                logger.info(
                    "AuthFSM 2FA: input de código achado por fallback agnóstico "
                    "de idioma (tela em idioma não-pt/en)."
                )
                return target
            except PlaywrightTimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("_lone_text_code_input: %s", exc)
                continue
        return None

    # ------------------------------------------------------------------
    # "Lembrar deste navegador?" — interstício pós-2FA bem-sucedido
    # ------------------------------------------------------------------
    def remember_browser_candidates(self) -> list["Locator"]:
        text = re.compile(
            r"continuar|continue|confiar|trust|lembrar|remember|"
            r"salvar|next|pr[oó]ximo|seguir",
            re.I,
        )
        return [
            self.page.get_by_role("button", name=text),
            self.page.locator('button[type="submit"]'),
            self.page.locator('[role="button"][tabindex="0"]'),
        ]

    async def confirm_remember_browser(self) -> bool:
        """Clica o botão afirmativo de 'lembrar navegador'. True se clicou.

        Confirmar (em vez de 'não salvar') faz o perfil PULAR 2FA nos
        próximos logins — exatamente o que queremos para reuso de perfil.
        """
        try:
            btn = await self.first_visible(
                self.remember_browser_candidates(), timeout_ms=6000
            )
            await btn.click()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("confirm_remember_browser: botão ausente (%s)", exc)
            return False
