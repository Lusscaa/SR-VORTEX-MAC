"""Retry helpers — decorator único para chamadas HTTP externas (H2).

Wrapper fino sobre ``tenacity``: padroniza 3 tentativas, backoff exponencial
(1s → 2s → 4s, teto 8s) e logging em WARNING antes de cada nova tentativa.
Só re-tenta erros transitórios (``httpx.HTTPError`` e seus filhos —
ConnectError, ReadTimeout, RemoteProtocolError, status 5xx via raise_for_status).
4xx NÃO são re-tentados (são erros do nosso lado).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_DEFAULT_LOGGER = logging.getLogger("meta_automation.retry")


def http_retry(
    *,
    attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 8.0,
    logger: logging.Logger | None = None,
    exceptions: tuple[type[BaseException], ...] = (httpx.HTTPError,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decora uma corrotina async que faz chamadas HTTP.

    Retorna a função original com retry transparente: assinatura idêntica,
    sucesso devolvido como esperado, falha após N tentativas re-propaga a
    última exceção. Cada retry é logado em WARNING.

    Uso::

        @http_retry()
        async def fetch(self, ...):
            r = await client.get(...)
            r.raise_for_status()
            return r.json()
    """
    log = logger or _DEFAULT_LOGGER

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            retrying = AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential(multiplier=min_wait, max=max_wait),
                retry=retry_if_exception_type(exceptions),
                reraise=True,
                before_sleep=before_sleep_log(log, logging.WARNING),
            )
            async for attempt in retrying:
                with attempt:
                    return await func(*args, **kwargs)

        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(func, "__name__", "wrapper")
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator
