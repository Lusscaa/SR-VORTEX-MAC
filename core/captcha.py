"""Async 2captcha client for reCAPTCHA resolution."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import (
    TWO_CAPTCHA_BASE_URL,
    TWO_CAPTCHA_POLL_INTERVAL_S,
    TWO_CAPTCHA_TIMEOUT_S,
)
from .errors import CaptchaUnsupportedError, LoginError

logger = logging.getLogger(__name__)

# Respostas do 2captcha que significam "não dá para resolver este captcha"
# (vs. um erro transitório). ERROR_CAPTCHA_UNSOLVABLE foi o visto em produção
# 2026-06 na rota /two_step_verification/authentication/ do Meta — reCAPTCHA
# que o método padrão userrecaptcha não cobre (provável Enterprise).
_UNSOLVABLE_MARKERS = frozenset(
    {
        "ERROR_CAPTCHA_UNSOLVABLE",
        "ERROR_UNSUPPORTED_CAPTCHA_TYPE",
        "ERROR_BAD_PARAMETERS",
    }
)


class TwoCaptchaSolver:
    """Async client for the 2captcha in.php / res.php endpoints."""

    name = "2captcha"

    def __init__(self, api_key: str, base_url: str = TWO_CAPTCHA_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def solve_recaptcha_v2(
        self, *, sitekey: str, page_url: str, invisible: bool = False,
        enterprise: bool = False,
    ) -> str:
        if not self.api_key:
            raise LoginError("2captcha API key not configured.")
        async with httpx.AsyncClient(timeout=30) as client:
            task_id = await self._submit(
                client, sitekey, page_url, invisible, enterprise
            )
            logger.info("2captcha task submitted (id=%s)", task_id)
            return await self._poll(client, task_id)

    async def _submit(
        self,
        client: httpx.AsyncClient,
        sitekey: str,
        page_url: str,
        invisible: bool,
        enterprise: bool = False,
    ) -> str:
        params = {
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        if invisible:
            params["invisible"] = 1
        if enterprise:
            # reCAPTCHA Enterprise (rota /enterprise/anchor): sem este flag o
            # 2captcha trata como v2 comum e o Google rejeita ("Invalid domain
            # for site key" / insolúvel). Doc 2captcha: enterprise=1.
            params["enterprise"] = 1
        r = await client.get(f"{self.base_url}/in.php", params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 1:
            raise LoginError(f"2captcha submit failed: {data}")
        return str(data["request"])

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + TWO_CAPTCHA_TIMEOUT_S
        params = {"key": self.api_key, "action": "get", "id": task_id, "json": 1}
        while True:
            await asyncio.sleep(TWO_CAPTCHA_POLL_INTERVAL_S)
            r = await client.get(f"{self.base_url}/res.php", params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == 1:
                return str(data["request"])
            req = str(data.get("request") or "")
            if req != "CAPCHA_NOT_READY":
                if req in _UNSOLVABLE_MARKERS:
                    # Insolúvel/não-suportado: sinaliza para o chamador abortar o
                    # auto e seguir só manual, em vez de tratar como erro genérico.
                    raise CaptchaUnsupportedError(
                        f"2captcha não resolve este captcha: {req}",
                        provider="2captcha",
                    )
                raise LoginError(f"2captcha error: {data}")
            if asyncio.get_event_loop().time() > deadline:
                raise LoginError("2captcha solve timed out.")
