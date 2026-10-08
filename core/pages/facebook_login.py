"""Page Object da tela de login do Facebook.

Centraliza TODOS os seletores da tela de login (campos, submit, banner de
cookies) — quando o DOM do Meta mudar, este é o único arquivo a tocar.
Os métodos ``*_candidates()`` devolvem a lista de locators em ordem de
preferência (migrada verbatim de ``automation.py``); os métodos sem
sufixo resolvem o primeiro candidato visível.

A LÓGICA de fluxo (digitação humana, captcha, navegação) permanece em
``AutomationRunner._perform_login`` — aqui só vivem seletores e ações
atômicas de página.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .base import BasePage

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = logging.getLogger("meta_automation.pages")


class FacebookLoginPage(BasePage):
    def email_candidates(self) -> list["Locator"]:
        return [
            self.page.get_by_label(re.compile(r"e-?mail|telefone|phone", re.I)),
            self.page.locator('input[name="email"]'),
            self.page.locator('input[type="email"]'),
        ]

    def password_candidates(self) -> list["Locator"]:
        return [
            self.page.get_by_label(re.compile(r"senha|password", re.I)),
            self.page.locator('input[name="pass"]'),
            self.page.locator('input[type="password"]'),
        ]

    def submit_candidates(self) -> list["Locator"]:
        # Variantes do controle de submit: <button>, <input type="submit">
        # (web.facebook.com não usa <button>, visto em produção 2026-06) e
        # o div role=button. O texto 'entrar|log in' só cobre PT/EN — o
        # submit via Enter em _handle_login_form é o caminho locale-agnóstico.
        return [
            self.page.get_by_role(
                "button", name=re.compile(r"entrar|log in", re.I)
            ),
            self.page.locator('button[name="login"]'),
            self.page.locator('button[type="submit"]'),
            self.page.locator('input[type="submit"]'),
            self.page.locator('form#login_form [type="submit"]'),
        ]

    def cookie_banner_candidates(self) -> list["Locator"]:
        """Seletores para botão de aceitar cookies (várias variantes do Meta).

        Cobre: "Permitir todos os cookies", "Allow all", "Accept", etc.
        Estratégia: procura por role+nome primeiro (robusto), depois data attributes,
        depois texto, depois fallback.
        """
        return [
            # Por role + nome (mais robusto — detecta acessibilidade)
            self.page.get_by_role(
                "button",
                name=re.compile(r"permitir\s+(?:todos\s+)?os\s+cookies|allow\s+(?:all\s+)?cookies", re.I),
            ),
            self.page.get_by_role(
                "button",
                name=re.compile(r"permitir\s+todos|allow\s+all|accept\s+all|aceitar\s+todos", re.I),
            ),
            self.page.get_by_role(
                "button",
                name=re.compile(r"permitir|allow|accept|aceitar", re.I),
            ),
            # Por data attribute (data-cookiebanner é específico do Meta)
            self.page.locator('[data-cookiebanner="accept_button"]'),
            # Por texto direto (último recurso)
            self.page.get_by_text(re.compile(r"permitir\s+todos\s+os\s+cookies|allow\s+all\s+cookies", re.I)).first,
            self.page.locator('button:has-text("Permitir")').first,
            # Fallback: segundo botão em dialog (geralmente "Permitir" vs "Recusar")
            self.page.locator('div[role="dialog"] button').nth(1),
            self.page.locator('[role="dialog"] button').nth(1),
        ]

    def qr_login_modal_marker(self) -> "Locator":
        """Texto que identifica o modal de login por QR code.

        O Meta às vezes intercepta a tela de login com 'Faça a leitura com o
        telefone' (QR code para login via app). Basta fechar no X e seguir o
        login normal por e-mail/senha.
        """
        return self.page.get_by_text(
            re.compile(
                r"leitura com o telefone|scan.*(qr|code)|c[oó]digo qr",
                re.I,
            )
        )

    def qr_login_modal_close_candidates(self) -> list["Locator"]:
        """Botões 'X' que fecham o modal de QR code, em ordem de preferência."""
        return [
            self.page.get_by_role(
                "button", name=re.compile(r"fechar|close|dispensar|dismiss", re.I)
            ),
            self.page.locator('[aria-label="Fechar"], [aria-label="Close"]'),
        ]

    def qr_active_back_candidates(self) -> list["Locator"]:
        """Links/botões para voltar ao login por senha quando a view de QR está ativa.

        Quando o botão de QR trigger é clicado (acidentalmente ou não), o form
        de e-mail/senha desaparece e uma imagem de QR code é exibida no lugar.
        O Facebook mostra um link 'Entrar com e-mail ou número de telefone' ou
        similar para voltar ao form normal.
        """
        return [
            self.page.get_by_text(re.compile(
                r"entrar com (?:e-?mail|telefone|senha)|log.?in with (?:email|phone|password)|"
                r"usar e-?mail|use email|usar senha|use password|"
                r"fazer login com e-?mail",
                re.I,
            )),
            self.page.get_by_role("link", name=re.compile(
                r"e-?mail|telefone|phone|senha|password", re.I
            )),
            self.page.get_by_role("button", name=re.compile(
                r"voltar|back|return", re.I
            )),
        ]

    async def suppress_qr_trigger_button(self) -> None:
        """Remove/oculta o botão de trigger do QR code via JS (best-effort).

        Facebook exibe um pequeno botão no canto superior direito do form de
        login que, quando clicado, substitui o form de e-mail/senha por um QR
        code de leitura. Qualquer clique acidental nesse botão interrompe o
        fluxo. Este método esconde o botão pelo DOM antes de qualquer interação,
        eliminando o risco independente de layout ou idioma.
        """
        try:
            await self.page.evaluate("""
            (() => {
                const sel = 'button, a, [role="button"]';
                for (const el of document.querySelectorAll(sel)) {
                    const text  = (el.textContent || '').toLowerCase();
                    const aria  = (el.getAttribute('aria-label') || '').toLowerCase();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    if (/qr/i.test(text) || /qr/i.test(aria) || /qr/i.test(title)) {
                        el.style.display = 'none';
                        el.setAttribute('aria-hidden', 'true');
                        el.setAttribute('tabindex', '-1');
                    }
                }
            })();
            """)
        except Exception:  # noqa: BLE001
            pass

    async def ensure_password_login_view(self) -> None:
        """Garante que a view de login por e-mail/senha está ativa.

        Quando o botão de QR é clicado (acidental ou modal prévio), o campo de
        e-mail some e um QR code aparece no lugar. Detecta isso verificando a
        visibilidade do campo de e-mail; se ausente, procura e clica no link de
        retorno ao login por senha. Também oculta o botão de trigger via JS para
        prevenir novos cliques acidentais durante o preenchimento.
        """
        # 1) Esconde o botão de trigger para evitar cliques acidentais
        await self.suppress_qr_trigger_button()

        # 2) Checa se o campo de e-mail está visível (situação normal)
        try:
            email_visible = await self.page.locator(
                'input[name="email"], input[type="email"]'
            ).first.is_visible(timeout=1500)
        except (PlaywrightTimeoutError, Exception):
            email_visible = False

        if email_visible:
            return  # form de senha já visível, ok

        # 3) Campo não visível → provável que QR view está ativa.
        logger.warning(
            "LoginPage: campo de e-mail não visível após carregar — "
            "QR code provavelmente foi ativado. Tentando retornar ao login por senha."
        )
        for loc in self.qr_active_back_candidates():
            try:
                if await loc.first.is_visible(timeout=2000):
                    await loc.first.click()
                    await self.page.wait_for_timeout(1200)
                    # Esconde o trigger novamente (pode ter re-renderizado)
                    await self.suppress_qr_trigger_button()
                    logger.info("LoginPage: retornou ao login por e-mail/senha.")
                    return
            except (PlaywrightTimeoutError, Exception):
                continue
        logger.warning(
            "LoginPage: não foi possível retornar ao form de senha — "
            "o campo de e-mail pode estar oculto/inacessível."
        )

    async def dismiss_qr_login_modal(self, *, appear_timeout_ms: int = 1500) -> bool:
        """Fecha o modal de login por QR code, se presente (best-effort).

        O modal 'Faça a leitura com o telefone' costuma renderizar com ATRASO
        (1–3 s após a navegação) e, uma vez aberto, fica FIXO interceptando o
        clique no campo de e-mail — daí o login estourar 'Locator.click:
        Timeout 30000ms'. Por isso o dispatcher chama isto de novo logo ANTES
        de digitar, já com o modal presente (``appear_timeout_ms`` dá margem
        pro render tardio). Só age quando o marcador de texto do modal está
        visível — assim não clica num 'Fechar' de outro componente.

        Estratégia: **Escape** primeiro (fecha a maioria dos dialogs do Meta
        sem depender de achar o 'X' nem do idioma) e, se o marcador persistir,
        o 'X' por papel/aria-label. Devolve ``True`` se dispensou.
        """
        try:
            if not await self.qr_login_modal_marker().first.is_visible(
                timeout=appear_timeout_ms
            ):
                return False
        except (PlaywrightTimeoutError, Exception):
            return False
        # 1) Escape — universal e locale-agnóstico.
        try:
            await self.page.keyboard.press("Escape")
            if not await self._qr_modal_still_open():
                logger.info("Modal de login por QR code dispensado (Escape).")
                return True
        except Exception:  # noqa: BLE001
            pass
        # 2) Botão 'X' (fechar/close), em ordem de preferência.
        for loc in self.qr_login_modal_close_candidates():
            try:
                if await loc.first.is_visible(timeout=1500):
                    await loc.first.click()
                    logger.info("Modal de login por QR code dispensado (X).")
                    return True
            except (PlaywrightTimeoutError, Exception):
                continue
        logger.warning(
            "Modal de QR code detectado mas não consegui fechá-lo — "
            "o clique no campo de e-mail pode ser interceptado."
        )
        return False

    async def _qr_modal_still_open(self) -> bool:
        """True se o marcador do modal de QR ainda está visível."""
        try:
            return await self.qr_login_modal_marker().first.is_visible(timeout=1000)
        except (PlaywrightTimeoutError, Exception):
            return False

    async def email_field(self) -> "Locator":
        return await self.first_visible(self.email_candidates())

    async def password_field(self) -> "Locator":
        return await self.first_visible(self.password_candidates())

    async def submit_button(self) -> "Locator":
        return await self.first_visible(self.submit_candidates())

    async def password_visible(self, timeout_ms: int = 2500) -> bool:
        """True se o campo de senha ainda está visível (login não submeteu).

        Usado após o Enter para decidir se ainda é preciso clicar o botão de
        submit: se a tela já navegou, o campo sumiu e podemos seguir sem
        gastar tempo procurando um controle que não existe mais.
        """
        for loc in self.password_candidates():
            try:
                if await loc.first.is_visible(timeout=timeout_ms):
                    return True
            except (PlaywrightTimeoutError, Exception):
                continue
        return False

    async def dismiss_cookie_banner(self) -> None:
        """Aceita o banner de cookies se estiver visível (best-effort).

        Tenta vários seletores com timeout generoso (3s) pra cobrir variantes
        do Meta (HTML pode demorar, banner é lazy-loaded, etc).
        """
        # 800ms por candidato, não 3s: o banner é renderizado JUNTO com a
        # página (não é lazy) e são 6+ seletores — quando ele não existe, o
        # teto antigo custava ~21s por run só para concluir "não está aqui"
        # (medição de 12 runs, 2026-09).
        for i, loc in enumerate(self.cookie_banner_candidates()):
            try:
                if await loc.first.is_visible(timeout=800):
                    await loc.first.click()
                    logger.info(f"Banner de cookies aceito (seletor #{i}).")
                    return
            except (PlaywrightTimeoutError, Exception):
                continue
        logger.warning("Não consegui encontrar o botão de aceitar cookies.")

    def save_login_dismiss_candidates(self) -> list["Locator"]:
        return [
            self.page.get_by_role(
                "button", name=re.compile(r"agora n[ãa]o|not now", re.I)
            ),
            self.page.get_by_role(
                "button", name=re.compile(r"n[ãa]o salvar|don'?t save", re.I)
            ),
        ]

    async def dismiss_save_login_prompt(self) -> bool:
        """Dispensa o prompt pós-login 'Salvar informações de login?'.

        Devolve ``True`` se algum botão de recusa foi clicado. O prompt só
        aparece com login JÁ bem-sucedido — dispensá-lo destrava o
        indicador pós-login que pode ficar atrás do dialog.
        """
        for loc in self.save_login_dismiss_candidates():
            try:
                if await loc.first.is_visible(timeout=1500):
                    await loc.first.click()
                    return True
            except (PlaywrightTimeoutError, Exception):
                continue
        return False
