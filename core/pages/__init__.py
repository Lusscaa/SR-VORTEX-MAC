"""Page Objects do Facebook — seletores centralizados (PR2).

Quando o DOM do Meta mudar, os ajustes ficam restritos a este pacote;
``automation.py`` consome os page objects sem conhecer seletores.
"""

from .base import BasePage, first_visible
from .facebook_login import FacebookLoginPage
from .facebook_two_factor import FacebookTwoFactorPage

__all__ = [
    "BasePage",
    "FacebookLoginPage",
    "FacebookTwoFactorPage",
    "first_visible",
]
