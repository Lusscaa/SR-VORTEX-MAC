"""Infra comum dos Page Objects.

``first_visible`` reproduz exatamente a semântica do antigo
``AutomationRunner._first_visible``: tenta cada candidato em ordem,
devolve o primeiro que ficar visível, levanta ``LoginError`` se nenhum
aparecer. ``AutomationRunner._first_visible`` agora delega para cá.
"""

from __future__ import annotations

import os

import logging
from typing import TYPE_CHECKING

from ..errors import LoginError, UIError

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

logger = logging.getLogger("meta_automation.pages")

# Teto POR CANDIDATO da busca de elementos. Telas com muitos candidatos (o
# form de detalhes tem 17) gastavam 5s em CADA um que não existe — ~85s numa
# tela só. 2s cobre a hidratação real do SPA; quem precisa de mais passa
# ``timeout_ms`` explícito (o fluxo já faz isso onde importa).
FIRST_VISIBLE_TIMEOUT_MS = int(os.getenv("FIRST_VISIBLE_TIMEOUT_MS", "2000"))


async def first_visible(
    locators: list["Locator"],
    timeout_ms: int = FIRST_VISIBLE_TIMEOUT_MS,
) -> "Locator":
    """Devolve o primeiro locator da lista que ficar visível.

    Cada candidato tem ``timeout_ms`` para aparecer; a ordem da lista é a
    ordem de preferência. Levanta ``LoginError`` se nenhum ficar visível.
    """
    last_exc: Exception | None = None
    for loc in locators:
        try:
            target = loc.first if hasattr(loc, "first") else loc
            await target.wait_for(state="visible", timeout=timeout_ms)
            return target
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    # UIError (não LoginError): "elemento não visível" é glitch de UI / render
    # lento — TRANSITÓRIO, não é culpa da conta. UIError é subclasse de
    # LoginError (handlers com `except LoginError` seguem pegando), mas o settle
    # NÃO descarta a conta (evita perder conta boa por travada de tela).
    raise UIError(f"Nenhum localizador visível encontrado: {last_exc}")


class BasePage:
    """Page Object mínimo: guarda a ``page`` e expõe ``first_visible``."""

    def __init__(self, page: "Page") -> None:
        self.page = page

    async def first_visible(
        self,
        locators: list["Locator"],
        timeout_ms: int = FIRST_VISIBLE_TIMEOUT_MS,
    ) -> "Locator":
        return await first_visible(locators, timeout_ms)
