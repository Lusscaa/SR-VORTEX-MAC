"""Exceções compartilhadas do pipeline.

Vivem em módulo próprio para que ``core/pages/`` possa levantá-las sem
importar ``core/automation.py`` (que importa ``core/pages/`` — ciclo).
``automation.py`` re-exporta ``LoginError``, então ``from core.automation
import LoginError`` e ``from core import LoginError`` continuam válidos.
"""

from __future__ import annotations


class LoginError(RuntimeError):
    """Raised when the runner cannot reach an authenticated state."""


class PreflightError(RuntimeError):
    """Raised when pre-flight checks (L2) reprovam o ambiente.

    Mensagem carrega o sumário das falhas críticas — a UI exibe direto.
    """


class CdpConnectionError(LoginError):
    """O AdsPower iniciou mas o ``connect_over_cdp`` não conectou.

    Browser travado no boot, ``ws_endpoint`` inválido ou porta ocupada. Era
    uma exceção crua do Playwright que escapava sem classificação nem
    fallback (observação 2026-06). Subclasse de ``LoginError`` → caminho
    de tratamento preservado.
    """

    failure_state: str = "adspower_cdp"


class CaptchaError(LoginError):
    """Base para falhas de captcha (login/2FA, WABA, verificação).

    Subclasse de ``LoginError`` DE PROPÓSITO: o tratamento de auth já
    captura ``LoginError`` e encerra a run com diagnóstico — manter a
    herança preserva esse caminho. O que muda é a CLASSIFICAÇÃO fina
    (``failure_state``) para a UI/telemetria e os metadados
    (``captcha_type``/``provider``/``where``), em vez de uma string de
    timeout genérica.

    Observado em produção 2026-06: o Meta serve captcha (reCAPTCHA, e às
    vezes Arkose/FunCaptcha) NA tela de 2FA em ``/two_step_verification/
    authentication/``; quando ninguém resolve e o provedor não cobre o
    tipo, a run morria depois de ~10 min de esperas empilhadas e sem
    classificação. Estes tipos existem para FALHAR RÁPIDO e CLARO.
    """

    #: estampado em ``AutomationResult.failure_state`` pelo runner.
    failure_state: str = "captcha"

    def __init__(
        self,
        message: str,
        *,
        captcha_type: "str | None" = None,
        provider: "str | None" = None,
        where: "str | None" = None,
    ) -> None:
        super().__init__(message)
        self.captcha_type = captcha_type  # 'recaptcha'|'hcaptcha'|'arkose'|'funcaptcha'|None
        self.provider = provider  # ex.: '2captcha'
        self.where = where  # ex.: 'two_factor_totp'|'waba'|'business_verification'


class CaptchaUnsolvedError(CaptchaError):
    """Captcha não resolvido no prazo — manual E automático esgotados.

    Levantado quando a corrida ``manual×2captcha`` estoura o timeout sem
    sair do desafio. Aborta a etapa em vez de empilhar mais uma espera.
    """

    failure_state = "captcha_unsolved"


class CaptchaUnsupportedError(CaptchaError):
    """O provedor declarou o captcha insolúvel/não-suportado.

    Ex.: 2captcha devolve ``ERROR_CAPTCHA_UNSOLVABLE`` — típico de
    Arkose/FunCaptcha, que o solver de reCAPTCHA v2 não cobre. Sinaliza
    para encurtar/abortar a corrida cedo (não esperar o manual inteiro à
    toa quando não há operador).
    """

    failure_state = "captcha_unsupported"


class DomainAlreadyClaimedError(LoginError):
    """O domínio já foi verificado por outra empresa no Meta.

    Não é culpa da conta — é culpa do CNPJ/subdomínio que entrou em conflito
    com outra Business Manager anterior. A conta pode ser reutilizada com outro
    CNPJ e outro subdomínio (mudando o nome admin para gerar novo subdomínio).

    Este erro permite retry com novo CNPJ — NÃO descarta a conta.
    """

    failure_state: str = "domain_already_claimed"


class InfrastructureError(LoginError):
    """Erro de infraestrutura/conectividade — não é culpa da conta.

    Exemplos: SMS inacessível, AdsPower API inacessível, rede offline, timeout.
    A conta pode ser reutilizada quando a infraestrutura voltar.

    Este erro permite retry — NÃO descarta a conta.
    """

    failure_state: str = "infrastructure_error"


class UIError(LoginError):
    """Erro de UI/Playwright — elemento não encontrado ou timeout de carregamento.

    Exemplos: botão não encontrado, campo timeout, página não carregou.
    Não é culpa da conta — pode ser glitch transitório de carregamento.
    A conta pode ser reutilizada.

    Este erro permite retry — NÃO descarta a conta.
    """

    failure_state: str = "ui_error"
