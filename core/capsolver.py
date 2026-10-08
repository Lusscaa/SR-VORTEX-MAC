"""Async CapSolver client — reCAPTCHA v2 / v2 Enterprise.

CapSolver cobre reCAPTCHA **Enterprise** (e Arkose/FunCaptcha), que o método
``userrecaptcha`` do 2captcha NÃO resolve — observação 2026-06: o Meta serve na
rota ``/two_step_verification/authentication/`` um reCAPTCHA que o 2captcha
devolve como ``ERROR_CAPTCHA_UNSOLVABLE``.

Interface compatível com ``TwoCaptchaSolver`` (``solve_recaptcha_v2``), para o
runner trocar de provedor sem tocar no fluxo. API CapSolver:
``createTask`` → ``getTaskResult`` (poll). Doc: https://docs.capsolver.com
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import (
    CAPSOLVER_BASE_URL,
    CAPSOLVER_POLL_INTERVAL_S,
    CAPSOLVER_RECAPTCHA_ENTERPRISE,
    CAPSOLVER_TIMEOUT_S,
)
from .errors import CaptchaUnsupportedError, LoginError

logger = logging.getLogger(__name__)

# errorCode/status do CapSolver que significam "não dá para resolver" (vs erro
# transitório) — sinaliza para abortar o auto e seguir só manual.
_UNSOLVABLE_MARKERS = frozenset(
    {
        "ERROR_CAPTCHA_UNSOLVABLE",
        "ERROR_CAPTCHA_UNSUPPORTED",
        "ERROR_INVALID_TASK_DATA",
        "ERROR_TASKID_INVALID",
    }
)

# Erros de CHAVE/CONTA (não adianta re-tentar; só troca de chave/saldo resolve).
_KEY_ERROR_MARKERS = frozenset(
    {
        "ERROR_KEY_DOES_NOT_EXIST",
        "ERROR_KEY_DENIED_ACCESS",
        "ERROR_KEY_DENIED_LOW_BALANCE",
        "ERROR_ZERO_BALANCE",
    }
)


class CapSolverClient:
    """Cliente async do CapSolver (createTask/getTaskResult)."""

    name = "CapSolver"

    def __init__(
        self,
        api_key: str,
        base_url: str = CAPSOLVER_BASE_URL,
        *,
        enterprise: bool = CAPSOLVER_RECAPTCHA_ENTERPRISE,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.enterprise = enterprise

    async def solve_recaptcha_v2(
        self, *, sitekey: str, page_url: str, invisible: bool = False,
        enterprise: bool = False,
    ) -> str:
        if not self.api_key:
            raise LoginError("CapSolver API key not configured.")
        # enterprise per-call (detectado pelo iframe '/enterprise/') OU o default
        # global da conta — qualquer um liga o task type Enterprise. Antes o
        # 2FA/WhatsApp dependia só do flag global; com a detecção por iframe o
        # provedor recebe o tipo certo sempre.
        use_enterprise = enterprise or self.enterprise
        task_type = (
            "ReCaptchaV2EnterpriseTaskProxyLess"
            if use_enterprise
            else "ReCaptchaV2TaskProxyLess"
        )
        task: dict[str, Any] = {
            "type": task_type,
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
        if invisible:
            task["isInvisible"] = True
        # sitekey/url são PÚBLICOS (não-secretos) — logamos para diagnosticar
        # rejeições "Invalid domain/site key" (causa nº1 de cair p/ manual).
        logger.info(
            "CapSolver: criando task type=%s enterprise=%s sitekey=%r url=%r",
            task_type, use_enterprise, sitekey, page_url,
        )
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                task_id = await self._create_task(client, task)
            except CaptchaUnsupportedError:
                logger.warning(
                    "CapSolver RECUSOU a task na criação (type=%s sitekey=%r url=%r) "
                    "— provedor não aceita ESTES dados (sitekey/URL provavelmente "
                    "errados), NÃO é falta de crédito.",
                    task_type, sitekey, page_url,
                )
                raise
            logger.info("CapSolver task criada (id=%s, type=%s)", task_id, task_type)
            return await self._poll(client, task_id)

    async def _create_task(
        self, client: httpx.AsyncClient, task: dict[str, Any]
    ) -> str:
        r = await client.post(
            f"{self.base_url}/createTask",
            json={"clientKey": self.api_key, "task": task},
        )
        data = self._parse_response(r, "createTask")
        self._raise_on_error(data)
        task_id = data.get("taskId")
        if not task_id:
            raise LoginError(f"CapSolver createTask sem taskId: {data}")
        return str(task_id)

    @staticmethod
    def _parse_response(r: httpx.Response, endpoint: str) -> dict[str, Any]:
        """Lê o JSON do CapSolver MESMO em 4xx.

        O CapSolver devolve o erro real no corpo (errorCode/errorDescription)
        com HTTP 400/401 — ex.: 'ERROR_KEY_DOES_NOT_EXIST',
        'ERROR_KEY_DENIED_ACCESS'. Fazer ``raise_for_status()`` cru escondia
        isso atrás de um genérico '400 Bad Request' e o operador ficava sem
        saber que era CHAVE inválida/negada. Aqui o corpo é preservado para
        ``_raise_on_error`` classificar; só cai no status HTTP se não vier JSON.
        """
        try:
            return r.json()
        except ValueError:
            raise LoginError(
                f"CapSolver {endpoint}: resposta não-JSON "
                f"(HTTP {r.status_code}): {(r.text or '')[:200]}"
            )

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + CAPSOLVER_TIMEOUT_S
        while True:
            await asyncio.sleep(CAPSOLVER_POLL_INTERVAL_S)
            r = await client.post(
                f"{self.base_url}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
            )
            data = self._parse_response(r, "getTaskResult")
            self._raise_on_error(data)
            if data.get("status") == "ready":
                token = (data.get("solution") or {}).get("gRecaptchaResponse")
                if not token:
                    raise LoginError(f"CapSolver pronto mas sem token: {data}")
                return str(token)
            # status == 'processing' (ou ausente) → continua o poll.
            if asyncio.get_event_loop().time() > deadline:
                raise LoginError("CapSolver solve timed out.")

    @staticmethod
    def _raise_on_error(data: dict[str, Any]) -> None:
        """errorId != 0 → classifica insolúvel (Unsupported) vs erro genérico."""
        if data.get("errorId") in (0, None):
            return
        code = str(data.get("errorCode") or "")
        desc = data.get("errorDescription") or data
        if code in _UNSOLVABLE_MARKERS:
            raise CaptchaUnsupportedError(
                f"CapSolver não resolve este captcha: {code} ({desc})",
                provider="capsolver",
            )
        # Erros de CHAVE/CONTA: mensagem acionável (o operador só resolve
        # trocando a CAPSOLVER_API_KEY ou o saldo/status da conta).
        if code in _KEY_ERROR_MARKERS:
            raise LoginError(
                f"CapSolver: CHAVE inválida/negada ({code}: {desc}). Confira "
                f"CAPSOLVER_API_KEY no .env (sem '#' e sem espaços) e o "
                f"saldo/status da conta em capsolver.com."
            )
        raise LoginError(f"CapSolver error: {code or desc}")
