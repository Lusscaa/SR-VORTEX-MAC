"""ixBrowser Local API v2 client — antidetect browser profile management.

Drop-in alternative to AdsPowerClient. Same method signatures and return
formats so automation.py can swap providers without code changes.

Local API docs: https://www.ixbrowser.com/doc/v2/local-api/en
Default base URL: http://127.0.0.1:53200/api/v2/
All endpoints are POST with JSON body; no auth required.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

import httpx

from config import (
    IXBROWSER_API_URL,
    IXBROWSER_GROUP_NAME,
    IXBROWSER_TIMEOUT_S,
)
from core.adspower_client import AdsPowerError, AdsPowerProfileNotFound

logger = logging.getLogger(__name__)


class IXBrowserError(AdsPowerError):
    """Erro genérico da Local API do ixBrowser.

    Herda de AdsPowerError para que os catches existentes em automation.py
    (except AdsPowerError) capturem erros do ixBrowser sem alteração."""


class IXBrowserProfileNotFound(AdsPowerProfileNotFound):
    """Profile ID não existe no ixBrowser."""


class IXBrowserClient:
    """Cliente da Local API v2 do ixBrowser.

    Interface compatível com AdsPowerClient — mesmos nomes de métodos e
    formatos de retorno para que automation.py funcione sem alterações.
    """

    def __init__(
        self,
        base_url: str = IXBROWSER_API_URL,
        *,
        timeout_s: float = IXBROWSER_TIMEOUT_S,
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._default_group_id: Optional[str] = None

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload or {},
            )
            r.raise_for_status()
            return r.json()

    async def verify(self) -> dict[str, Any]:
        """Ping na API. Retorna {"ok": bool, "message": str, "profile_count": int|None}."""
        try:
            data = await self._post("/api/v2/profile-list", {"page": 1, "limit": 1})
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"API local inacessível: {exc}", "profile_count": None}
        except Exception as exc:
            return {"ok": False, "message": f"Erro inesperado: {exc}", "profile_count": None}
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            return {
                "ok": False,
                "message": f"API rejeitou: {err.get('message') or data}",
                "profile_count": None,
            }
        total = (data.get("data") or {}).get("total")
        return {
            "ok": True,
            "message": "API conectada" + (f" · {total} perfis" if isinstance(total, int) else ""),
            "profile_count": total if isinstance(total, int) else None,
        }

    async def list_groups(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """Lista grupos. Retorna formato compatível com AdsPower:
        [{"group_id": str, "group_name": str}, ...]"""
        max_retries = 1
        for attempt in range(max_retries):
            try:
                data = await self._post("/api/v2/group-list", {"page": 1, "limit": page_size})
                err = data.get("error") or {}
                if err.get("code", -1) != 0:
                    msg = err.get("message") or str(data)
                    if attempt < max_retries - 1:
                        logger.warning("ixBrowser list_groups erro (tentativa %d/%d): %s", attempt + 1, max_retries, msg)
                        await asyncio.sleep(1)
                        continue
                    raise IXBrowserError(f"list_groups falhou: {msg}")
                items = (data.get("data") or {}).get("data") or []
                return [{"group_id": str(g.get("id") or ""), "group_name": g.get("title") or ""} for g in items]
            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    logger.warning("ixBrowser list_groups timeout (tentativa %d/%d)", attempt + 1, max_retries)
                    await asyncio.sleep(1)
                    continue
                raise IXBrowserError(f"list_groups timeout após {max_retries} tentativas")
        raise IXBrowserError("list_groups falhou após retries")

    async def create_group(self, name: str) -> str:
        """Cria grupo, retorna group_id como string."""
        data = await self._post("/api/v2/group-create", {"title": name, "sort": 0})
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            raise IXBrowserError(f"create_group falhou: {err.get('message') or data}")
        gid = data.get("data")
        if not gid:
            raise IXBrowserError(f"create_group sem group_id: {data!r}")
        logger.info("ixBrowser: grupo '%s' criado (group_id=%s)", name, gid)
        return str(gid)

    async def ensure_group(self, name: str = "farm-conta") -> str:
        """Procura grupo pelo nome; cria se não existir."""
        groups = await self.list_groups()
        for g in groups:
            if (g.get("group_name") or "").strip() == name:
                gid = (g.get("group_id") or "").strip()
                if gid:
                    return gid
        try:
            return await self.create_group(name)
        except IXBrowserError as exc:
            msg = str(exc).lower()
            if "permission" not in msg and "auth" not in msg:
                raise
            if not groups:
                raise IXBrowserError(
                    "Conta ixBrowser sem nenhum grupo e sem permissão para criar. "
                    "Crie um grupo manualmente no ixBrowser."
                ) from exc
            fallback = groups[0]
            gid = (fallback.get("group_id") or "").strip()
            if not gid:
                raise IXBrowserError(f"Primeiro grupo sem id válido: {fallback!r}") from exc
            logger.warning(
                "ixBrowser: create_group falhou — usando grupo existente '%s' (id=%s).",
                fallback.get("group_name") or "<sem nome>", gid,
            )
            return gid

    async def resolve_default_group_id(self) -> str:
        if self._default_group_id:
            return self._default_group_id
        try:
            gid = await self.ensure_group(IXBROWSER_GROUP_NAME)
        except (IXBrowserError, Exception) as exc:
            logger.warning("ixBrowser: ensure_group falhou (%s) — usando group_id=0 (padrão)", exc)
            self._default_group_id = "0"
            return "0"
        self._default_group_id = gid
        logger.info("ixBrowser default group resolvido: id=%s (grupo=%s)", gid, IXBROWSER_GROUP_NAME)
        return gid

    async def get_profile_info(self, user_id: str) -> dict[str, Any]:
        """Busca info de um perfil. Retorna dict com campos compatíveis."""
        pid = int(user_id)
        data = await self._post("/api/v2/profile-list", {"profile_id": pid, "page": 1, "limit": 1})
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            raise IXBrowserError(f"get_profile_info falhou: {err.get('message') or data}")
        items = (data.get("data") or {}).get("data") or []
        if not items:
            raise IXBrowserError(f"Perfil profile_id={user_id!r} não encontrado.")
        raw = items[0]
        return {
            "user_id": str(raw.get("profile_id") or raw.get("id") or ""),
            "name": raw.get("name") or "",
            "remark": raw.get("note") or "",
            "username": raw.get("username") or "",
            "password": raw.get("password") or "",
            "fakey": raw.get("tfa_secret") or "",
            "raw": raw,
        }

    async def list_profiles(
        self, *, group_id: Optional[str] = None, page_size: int = 100,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page": 1, "limit": page_size}
        if group_id:
            payload["group_id"] = int(group_id)
        data = await self._post("/api/v2/profile-list", payload)
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            raise IXBrowserError(f"list_profiles falhou: {err.get('message') or data}")
        items = (data.get("data") or {}).get("data") or []
        return [
            {
                "user_id": str(p.get("profile_id") or p.get("id") or ""),
                "name": p.get("name") or "",
                "serial_number": str(p.get("profile_id") or ""),
            }
            for p in items
        ]

    async def create_profile(
        self,
        *,
        name: str,
        group_id: Optional[str] = None,
        timezone: str = "America/Sao_Paulo",
        languages: Optional[list[str]] = None,
        user_agent: Optional[str] = None,
        remark: Optional[str] = None,
        proxy: Optional[dict[str, Any]] = None,
        os_type: str = "android",
    ) -> dict[str, Any]:
        """Cria perfil. Retorna {"user_id": str, "raw": dict} (compatível com AdsPower)."""
        gid = (group_id or "").strip()
        if not gid or gid == "0":
            gid = await self.resolve_default_group_id()

        proxy_config: dict[str, Any] = {}
        if proxy and proxy.get("host") and proxy.get("port"):
            scheme = (proxy.get("scheme") or "http").lower()
            proxy_config = {
                "proxy_mode": 2,
                "proxy_type": scheme if scheme in ("http", "https", "socks5") else "http",
                "proxy_ip": str(proxy["host"]),
                "proxy_port": str(proxy["port"]),
                **({"proxy_user": proxy["username"]} if proxy.get("username") else {}),
                **({"proxy_password": proxy["password"]} if proxy.get("password") else {}),
            }
        else:
            proxy_config = {"proxy_mode": 2, "proxy_type": "direct"}

        lang = (languages or ["pt-BR"])[0]
        is_mobile = os_type.lower() in ("android", "mobile")
        fingerprint_config: dict[str, Any] = {
            "timezone_type": "2",
            "timezone": timezone,
            "language_type": "2",
            "language": lang,
            "webrtc": "5",
            "canvas_type": "1",
            "audio_context": "1",
            "webgl_data_type": "1",
        }
        if is_mobile:
            fingerprint_config["hardware_concurrency"] = str(random.choice([4, 6, 8]))
            fingerprint_config["device_memory"] = str(random.choice([3, 4, 6, 8]))
            fingerprint_config["ua_type"] = "1"
            fingerprint_config["ua_system_type"] = "1"
            android_ver = random.choice([11, 12, 13, 14])
            models = [
                "SM-G991B", "SM-G996B", "SM-G998B",
                "SM-S901B", "SM-S906B", "SM-S908B",
                "SM-A536B", "SM-A546B", "SM-A736B",
                "SM-F926U1", "SM-F936B", "SM-F721B",
                "Pixel 6", "Pixel 7", "Pixel 7 Pro", "Pixel 8",
                "moto g(60)", "moto g82 5G",
            ]
            model = random.choice(models)
            chrome_ver = random.choice([120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131])
            ua = (
                f"Mozilla/5.0 (Linux; Android {android_ver}; {model}) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{chrome_ver}.0.0.0 Mobile Safari/537.36"
            )
            fingerprint_config["ua_info"] = ua
        else:
            fingerprint_config["hardware_concurrency"] = str(random.choice([2, 4, 6, 8, 12, 16]))
            fingerprint_config["device_memory"] = str(random.choice([2, 4, 8, 16]))
        if user_agent:
            fingerprint_config["ua_info"] = user_agent

        preference_config: dict[str, Any] = {
            "cookies_backup": "1",
            "block_restore_pages": "1",
            "block_notification_pages": "1",
            "block_popup_blocking": "1",
            "load_profile_info_page": "0",
        }

        payload: dict[str, Any] = {
            "name": name,
            "group_id": int(gid),
            "site_id": 1,
            "proxy_config": proxy_config,
            "fingerprint_config": fingerprint_config,
            "preference_config": preference_config,
        }
        if is_mobile:
            payload["platform_id"] = 2
        if remark:
            payload["note"] = remark[:200]
        data = await self._post("/api/v2/profile-create", payload)
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            raise IXBrowserError(f"ixBrowser create_profile falhou: {err.get('message') or data}")
        new_id = data.get("data")
        if not new_id:
            raise IXBrowserError(f"ixBrowser create_profile sem id na resposta: {data!r}")
        logger.info("ixBrowser create_profile OK — name=%s id=%s", name, new_id)
        return {"user_id": str(new_id), "raw": data.get("data") or {}}

    async def update_profile(
        self, user_id: str, *, remark: Optional[str] = None, name: Optional[str] = None
    ) -> bool:
        """Atualiza perfil. Best-effort, nunca levanta."""
        if not user_id:
            return False
        payload: dict[str, Any] = {"profile_id": int(user_id)}
        if remark is not None:
            payload["note"] = remark[:200]
        if name is not None:
            payload["name"] = name
        if len(payload) == 1:
            return False
        try:
            data = await self._post("/api/v2/profile-update", payload)
        except Exception as exc:
            logger.warning("ixBrowser update_profile falhou (id=%s): %s", user_id, exc)
            return False
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            logger.warning("ixBrowser update_profile recusado (id=%s): %s", user_id, err.get("message") or data)
            return False
        return True

    async def start(
        self,
        user_id: str,
        *,
        headless: bool = False,
        open_tabs: bool = False,
    ) -> dict[str, Any]:
        """Abre perfil. Retorna {"ws_puppeteer": str, "raw": dict} (compatível com AdsPower)."""
        pid = int(user_id)
        payload: dict[str, Any] = {
            "profile_id": pid,
            "load_profile_info_page": False,
            "cookies_backup": True,
            "args": ["--disable-extension-welcome-page"],
        }

        last_exc: Optional[Exception] = None
        for i in range(3):
            try:
                data = await self._post("/api/v2/profile-open", payload)
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "ixBrowser start: API não respondeu (%s: %s) — tentativa %d/3.",
                    type(exc).__name__, exc or "sem detalhe", i + 1,
                )
                await asyncio.sleep(1.5 * (i + 1))
        else:
            raise IXBrowserError(
                f"ixBrowser API não respondeu ao start após 3 tentativas "
                f"({type(last_exc).__name__}: {last_exc or 'timeout'}) "
                f"— verifique se o ixBrowser está aberto."
            ) from last_exc

        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            code = err.get("code", 0)
            msg = (err.get("message") or "").strip()
            if code == 2007 or "not exist" in msg.lower() or "does not exist" in msg.lower():
                raise IXBrowserProfileNotFound(
                    f"Profile ID '{user_id}' não existe no ixBrowser. "
                    f"Verifique no painel do ixBrowser."
                )
            raise IXBrowserError(f"ixBrowser start falhou (profile_id={user_id}): {msg or data}")

        result = data.get("data") or {}
        ws = result.get("ws") or ""

        if not ws:
            for r in range(5):
                logger.info("ixBrowser start sem ws (profile_id=%s) — aguardando %d/5.", user_id, r + 1)
                await asyncio.sleep(2.5)
                try:
                    data2 = await self._post("/api/v2/profile-open", payload)
                except Exception:
                    continue
                err2 = data2.get("error") or {}
                if err2.get("code", -1) == 0:
                    result = data2.get("data") or {}
                    ws = result.get("ws") or ""
                    if ws:
                        break

        if not ws:
            logger.warning("ixBrowser start sem ws (profile_id=%s) — forçando close + reopen.", user_id)
            await self.stop(user_id)
            await asyncio.sleep(2.0)
            try:
                data3 = await self._post("/api/v2/profile-open", payload)
                err3 = data3.get("error") or {}
                if err3.get("code", -1) == 0:
                    result = data3.get("data") or {}
                    ws = result.get("ws") or ""
            except Exception:
                pass
            if not ws:
                for r in range(4):
                    await asyncio.sleep(2.5)
                    try:
                        data4 = await self._post("/api/v2/profile-open", payload)
                        err4 = data4.get("error") or {}
                        if err4.get("code", -1) == 0:
                            result = data4.get("data") or {}
                            ws = result.get("ws") or ""
                            if ws:
                                break
                    except Exception:
                        continue

        if not ws:
            raise IXBrowserError(
                f"ixBrowser abriu o perfil mas NÃO retornou ws (profile_id={user_id}). "
                f"Feche perfis abertos no ixBrowser e tente novamente. "
                f"Resposta: {result!r}"
            )
        return {"ws_puppeteer": ws, "raw": result}

    async def stop(self, user_id: str) -> None:
        """Fecha perfil. Best-effort, nunca levanta."""
        try:
            data = await self._post("/api/v2/profile-close", {"profile_id": int(user_id)})
            err = data.get("error") or {}
            code = err.get("code", -1)
            if code != 0:
                msg = err.get("message") or ""
                if code == 1009 or "not found" in msg.lower():
                    logger.debug("ixBrowser stop: perfil já fechado (profile_id=%s).", user_id)
                    return
                logger.warning("ixBrowser stop code != 0 (profile_id=%s): %s", user_id, err)
        except Exception as exc:
            logger.warning("Falha ao fechar perfil ixBrowser (%s): %s", user_id, exc)

    async def delete_profile(self, user_id: str) -> bool:
        """Exclui perfil. Best-effort, retorna True/False."""
        if not user_id:
            return False
        try:
            await self.stop(user_id)
        except Exception:
            pass
        try:
            data = await self._post("/api/v2/profile-delete", {"profile_id": int(user_id)})
        except Exception as exc:
            logger.warning("ixBrowser delete_profile falhou (id=%s): %s", user_id, exc)
            return False
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            logger.warning("ixBrowser delete_profile recusado (id=%s): %s", user_id, err.get("message") or data)
            return False
        logger.info("ixBrowser: perfil %s EXCLUÍDO.", user_id)
        return True

    async def active_browser_count(self) -> Optional[int]:
        """Nº de browsers abertos via API."""
        try:
            data = await self._post("/api/v2/profile-opened-list", {"page": 1, "limit": 100})
        except Exception:
            return None
        err = data.get("error") or {}
        if err.get("code", -1) != 0:
            return None
        items = (data.get("data") or {}).get("data") or []
        return len(items)

    async def aclose(self) -> None:
        """Noop — compatibilidade com callers que chamam aclose()."""
