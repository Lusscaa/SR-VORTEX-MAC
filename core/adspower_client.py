"""AdsPower Local API client — antidetect browser profile management."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from config import (
    ADSPOWER_API_KEY,
    ADSPOWER_API_URL,
    ADSPOWER_GROUP_NAME,
    ADSPOWER_START_PATH,
    ADSPOWER_STOP_PATH,
    ADSPOWER_TIMEOUT_S,
)

logger = logging.getLogger(__name__)


# Tags de status no NOME do perfil (erro de login / encerramento pelo operador).
# SEMPRE prefixo (antes de todo o nome) — pedido do operador 2026-06.
_PROFILE_TAG_LEAD_RE = re.compile(r"^\s*(?:\[(?:ERRO|ENCERRADA)[^\]]*\]\s*)+", re.I)
_PROFILE_TAG_TRAIL_RE = re.compile(r"\s*(?:\[(?:ERRO|ENCERRADA)[^\]]*\]\s*)+$", re.I)


def build_tagged_profile_name(
    base: Optional[str], label: str, stamp: Optional[str] = None
) -> str:
    """Prefixa o nome do perfil com ``[<label> dd/mm HH:MM]``.

    Remove qualquer marca ``[ERRO …]``/``[ENCERRADA …]`` anterior — esteja ela
    no início OU no fim (formato antigo, sufixo) — pra não empilhar, e coloca a
    nova SEMPRE no início. ``label`` ex.: ``"ERRO SENHA"``, ``"ENCERRADA"``."""
    base = (base or "").strip()
    base_clean = _PROFILE_TAG_LEAD_RE.sub("", base)
    base_clean = _PROFILE_TAG_TRAIL_RE.sub("", base_clean).strip()
    stamp = stamp or datetime.now().strftime("%d/%m %H:%M")
    prefix = f"[{label} {stamp}]"
    return f"{prefix} {base_clean}".strip()


# ---------------------------------------------------------------------------
# Throttle GLOBAL (processo todo) das chamadas à API local do AdsPower.
#
# Cada run cria sua PRÓPRIA instância de AdsPowerClient (run_service.py), então
# um lock por instância não coordenaria nada entre runs concorrentes. Por isso
# o estado aqui é a nível de MÓDULO + ``threading.Lock`` (não ``asyncio.Lock``:
# cada run roda num event loop próprio, numa thread própria do JobManager —
# locks de asyncio não atravessam loops diferentes).
#
# Pedido do operador (2026-06): com MAIS de 2 runs simultâneas batendo
# create_profile/start ao mesmo tempo, o AdsPower devolve "too many requests".
# Acima desse limiar, espaça as chamadas em pelo menos 5s entre si; com 1-2
# runs, não atrasa nada.
_throttle_lock = threading.Lock()
_active_adspower_runs = 0
_next_call_at = 0.0  # time.monotonic() — próxima chamada só sai daqui
ADSPOWER_THROTTLE_THRESHOLD = 1   # entra em vigor com MAIS de 1 run concorrente
ADSPOWER_THROTTLE_MIN_GAP_S = 3.0  # intervalo mínimo entre aberturas de perfil (segundos) — evita soft-block do FB


def adspower_run_started() -> None:
    """Chamar 1x quando uma run COMEÇA a usar o AdsPower (AutomationRunner.run)."""
    global _active_adspower_runs
    with _throttle_lock:
        _active_adspower_runs += 1


def adspower_run_finished() -> None:
    """Chamar 1x quando a run termina (sempre em par com ``adspower_run_started``)."""
    global _active_adspower_runs
    with _throttle_lock:
        _active_adspower_runs = max(0, _active_adspower_runs - 1)


async def _throttle_adspower_call() -> None:
    """Aguarda o necessário p/ respeitar o intervalo mínimo entre chamadas,
    só quando há mais de ``ADSPOWER_THROTTLE_THRESHOLD`` runs concorrentes.
    Best-effort: nunca levanta."""
    global _next_call_at
    with _throttle_lock:
        if _active_adspower_runs <= ADSPOWER_THROTTLE_THRESHOLD:
            return
        now = time.monotonic()
        wait = max(0.0, _next_call_at - now)
        _next_call_at = max(now, _next_call_at) + ADSPOWER_THROTTLE_MIN_GAP_S
    if wait > 0:
        logger.debug(
            "AdsPower: %d runs concorrentes — aguardando %.1fs antes da "
            "próxima chamada (evita 'too many requests').",
            _active_adspower_runs, wait,
        )
        await asyncio.sleep(wait)


class AdsPowerError(RuntimeError):
    """Erro genérico da API local do AdsPower."""


class AdsPowerProfileNotFound(AdsPowerError):
    """Profile ID não existe no AdsPower local — geralmente o operador colou
    o valor errado no campo (ex.: CNPJ, API Key, ou ID de outro AdsPower).
    """


class AdsPowerClient:
    """Cliente da Local API do AdsPower.

    Métodos públicos:
      * ``verify()`` → ping na API com o token atual; retorna True/False.
      * ``list_profiles(group_id=None)`` → lista perfis da conta.
      * ``create_profile(name, ...)`` → cria perfil novo e devolve user_id.
      * ``start(user_id)`` → abre o perfil; retorna endpoint Puppeteer (CDP).
      * ``stop(user_id)`` → encerra o perfil (libera o slot).

    No fluxo "Modo Criar" o caller chama ``create_profile`` → ``start`` →
    Playwright via CDP. O AdsPower injeta proxy + fingerprint do perfil; não
    se passa ``proxy`` ao Playwright nem se reutiliza ``storage_state.json``.
    """

    def __init__(
        self,
        base_url: str = ADSPOWER_API_URL,
        *,
        start_path: str = ADSPOWER_START_PATH,
        stop_path: str = ADSPOWER_STOP_PATH,
        timeout_s: float = ADSPOWER_TIMEOUT_S,
        api_key: str = ADSPOWER_API_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.start_url = f"{self.base_url}{start_path}"
        self.stop_url = f"{self.base_url}{stop_path}"
        self.timeout_s = timeout_s
        self.api_key = api_key
        self._default_group_id: Optional[str] = None  # cache pós-list_groups

    def _auth_headers(self) -> dict[str, str]:
        """Header opcional — builds recentes da Local API exigem Authorization."""
        return {"Authorization": self.api_key} if self.api_key else {}

    def _with_auth(self, params: dict[str, Any]) -> dict[str, Any]:
        """Anexa ``api_key`` ao query string quando configurado.

        Algumas versões aceitam só query; outras só header. Mandar nos dois
        canais é defensivo — endpoints que não consomem ignoram silenciosamente.
        """
        if self.api_key and "api_key" not in params:
            return {**params, "api_key": self.api_key}
        return params

    async def verify(self) -> dict[str, Any]:
        """Ping rápido na API local. Retorna ``{"ok": bool, "message": str,
        "profile_count": int|None}``. Tenta ``/api/v1/user/list?page_size=1``.
        """
        url = f"{self.base_url}/api/v1/user/list"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.get(
                    url,
                    params=self._with_auth({"page_size": 1, "page": 1}),
                    headers=self._auth_headers(),
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"API local inacessível: {exc}", "profile_count": None}
        if data.get("code") != 0:
            return {
                "ok": False,
                "message": f"API rejeitou: {data.get('msg') or data}",
                "profile_count": None,
            }
        payload = data.get("data") or {}
        total = payload.get("total")
        return {
            "ok": True,
            "message": "API conectada" + (f" · {total} perfis" if isinstance(total, int) else ""),
            "profile_count": total if isinstance(total, int) else None,
        }

    async def list_groups(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """``GET /api/v1/group/list`` — lista de grupos da conta (com retry automático).

        Cada item: ``{"group_id": str, "group_name": str, ...}``. Algumas
        versões devolvem ``data.list``, outras ``data`` direto — tratamos os
        dois shapes.
        """
        import asyncio
        url = f"{self.base_url}/api/v1/group/list"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    r = await client.get(
                        url,
                        params=self._with_auth({"page_size": page_size, "page": 1}),
                        headers=self._auth_headers(),
                    )
                    r.raise_for_status()
                    data = r.json()

                if data.get("code") != 0:
                    msg = data.get('msg') or str(data)
                    # AggregateError é erro temporário — retry
                    if "AggregateError" in msg and attempt < max_retries - 1:
                        logger.warning(f"list_groups AggregateError (tentativa {attempt+1}/{max_retries}) — aguardando 1s e tentando novamente")
                        await asyncio.sleep(1)
                        continue
                    raise AdsPowerError(f"list_groups falhou: {msg}")

                payload = data.get("data") or {}
                if isinstance(payload, list):
                    return payload
                return payload.get("list") or []
            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    logger.warning(f"list_groups timeout (tentativa {attempt+1}/{max_retries}) — aguardando 1s e tentando novamente")
                    await asyncio.sleep(1)
                    continue
                raise AdsPowerError(f"list_groups timeout após {max_retries} tentativas")

        raise AdsPowerError("list_groups falhou após retries")

    async def create_group(self, name: str) -> str:
        """``POST /api/v1/group/create`` — cria grupo, retorna ``group_id``."""
        url = f"{self.base_url}/api/v1/group/create"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(
                url,
                params=self._with_auth({}),
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"group_name": name},
            )
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 0:
            raise AdsPowerError(
                f"create_group falhou: {data.get('msg') or data}"
            )
        gid = str((data.get("data") or {}).get("group_id") or "").strip()
        if not gid:
            raise AdsPowerError(f"create_group sem group_id: {data!r}")
        logger.success("AdsPower: grupo '%s' criado (group_id=%s)", name, gid)
        return gid

    async def ensure_group(self, name: str = "farm-conta") -> str:
        """Procura grupo pelo nome; cria se não existir.

        Estratégia em 3 fases (descoberta empírica via probe):
          1. Se ``name`` já existe na conta → retorna.
          2. Senão, tenta ``create_group(name)``.
          3. Se create_group falhar com "permission denied" (token sem write
             permission — comum em team-members), faz **fallback para o
             primeiro grupo existente** da conta. Pior caso: usa o grupo
             legado que o operador já tem no painel.
        """
        groups = await self.list_groups()
        for g in groups:
            if str(g.get("group_name") or "").strip() == name:
                gid = str(g.get("group_id") or "").strip()
                if gid:
                    return gid
        try:
            return await self.create_group(name)
        except AdsPowerError as exc:
            msg = str(exc).lower()
            if "permission" not in msg and "auth" not in msg:
                raise
            # Token sem permissão de criar grupos — usa o primeiro existente.
            if not groups:
                raise AdsPowerError(
                    "Conta AdsPower sem nenhum grupo e o token atual não tem "
                    "permissão para criar grupos. Crie um grupo manualmente "
                    "no painel ou gere um API Token com permissão de escrita."
                ) from exc
            fallback = groups[0]
            gid = str(fallback.get("group_id") or "").strip()
            if not gid:
                raise AdsPowerError(
                    f"Primeiro grupo sem id válido: {fallback!r}"
                ) from exc
            logger.warning(
                "AdsPower: create_group falhou por permissão — usando grupo "
                "existente '%s' (id=%s). Para criar grupo próprio, peça API "
                "Token com permissão de escrita no painel.",
                fallback.get("group_name") or "<sem nome>", gid,
            )
            return gid

    async def resolve_default_group_id(self) -> str:
        """Cacheia o group_id apropriado para criar perfis.

        Tenta ``ensure_group(ADSPOWER_GROUP_NAME)`` que tem fallback embutido para o
        primeiro grupo existente quando o token não pode criar.
        """
        if self._default_group_id:
            return self._default_group_id
        gid = await self.ensure_group(ADSPOWER_GROUP_NAME)
        self._default_group_id = gid
        logger.info("AdsPower default group resolvido: id=%s (grupo=%s)", gid, ADSPOWER_GROUP_NAME)
        return gid

    async def get_profile_info(self, user_id: str) -> dict[str, Any]:
        """Devolve o registro completo de um perfil (inclui ``fakey``, ``username``,
        ``password``, ``domain_name``, ``remark``…). Usa ``GET /api/v1/user/list``
        filtrando pelo ``user_id`` — não há endpoint /info dedicado em todas as
        builds do AdsPower.
        """
        url = f"{self.base_url}/api/v1/user/list"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.get(
                url,
                params=self._with_auth({"user_id": user_id, "page_size": 1, "page": 1}),
                headers=self._auth_headers(),
            )
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 0:
            raise AdsPowerError(f"get_profile_info falhou: {data.get('msg') or data}")
        items = (data.get("data") or {}).get("list") or []
        if not items:
            raise AdsPowerError(f"Perfil user_id={user_id!r} não encontrado.")
        return items[0]

    async def get_totp_seed(self, user_id: str) -> Optional[str]:
        """Extrai o segredo TOTP (``fakey``) do perfil. Retorna ``None`` quando
        o perfil não tem 2FA cadastrado.

        AdsPower armazena o segredo base32 no campo ``fakey`` e renderiza o
        código TOTP em tempo real no painel; nada além de ``pyotp.TOTP(fakey)``
        precisa rodar para reproduzir o mesmo código.
        """
        info = await self.get_profile_info(user_id)
        fakey = (info.get("fakey") or "").strip()
        return fakey or None

    async def list_profiles(
        self, *, group_id: Optional[str] = None, page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Lista perfis. Retorna a lista bruta ``data.list``."""
        params: dict[str, Any] = {"page_size": page_size, "page": 1}
        if group_id:
            params["group_id"] = group_id
        url = f"{self.base_url}/api/v1/user/list"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.get(
                url, params=self._with_auth(params), headers=self._auth_headers(),
            )
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 0:
            raise AdsPowerError(f"list_profiles falhou: {data.get('msg') or data}")
        return (data.get("data") or {}).get("list") or []

    async def active_browser_count(self) -> Optional[int]:
        """Nº de navegadores AdsPower ABERTOS agora (GET /browser/local-active).

        Usado só pra enriquecer o diagnóstico quando o start não devolve ws —
        o limite de navegadores abertos do plano é a suspeita nº 1. Retorna
        None se a chamada falhar (versão sem o endpoint, etc.)."""
        url = f"{self.base_url}/api/v1/browser/local-active"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.get(
                    url, params=self._with_auth({}), headers=self._auth_headers(),
                )
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if data.get("code") != 0:
            return None
        return len((data.get("data") or {}).get("list") or [])

    async def _safe_active_count(self) -> Optional[int]:
        try:
            return await self.active_browser_count()
        except Exception:  # noqa: BLE001
            return None

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
    ) -> dict[str, Any]:
        """Cria perfil novo com fingerprint pt-BR.

        ``proxy`` (do pool): ``{scheme, host, port, username, password}`` →
        vira ``user_proxy_config`` (o IP fica gravado no perfil e persiste).

        Se NÃO houver proxy no pool, NÃO envia user_proxy_config — deixa
        o AdsPower usar seu proxy padrão/rotativo configurado nas settings.

        Quando ``group_id`` for ``None`` ou ``"0"`` (default antigo), resolve
        automaticamente o primeiro grupo da conta — a API rejeita group_id=0
        em contas onde o grupo padrão foi deletado/arquivado.

        Retorna ``{"user_id": str, "raw": dict}``. Caller persiste o user_id
        no mapping CNPJ ↔ user_id antes de chamar ``start``.
        """
        await _throttle_adspower_call()
        gid = (group_id or "").strip()
        if not gid or gid == "0":
            gid = await self.resolve_default_group_id()

        # Constrói user_proxy_config (AdsPower EXIGE sempre)
        if proxy and (proxy.get("host") and proxy.get("port")):
            # Tem proxy no pool: usa esse
            user_proxy_config: dict[str, Any] = {
                "proxy_soft": "other",
                "proxy_type": (proxy.get("scheme") or "http"),
                "proxy_host": str(proxy["host"]),
                "proxy_port": str(proxy["port"]),
                **({"proxy_user": proxy["username"]} if proxy.get("username") else {}),
                **({"proxy_password": proxy["password"]} if proxy.get("password") else {}),
            }
        else:
            # SEM proxy no pool: volta ao "no_proxy" (padrão original)
            user_proxy_config = {"proxy_soft": "no_proxy"}

        url = f"{self.base_url}/api/v1/user/create"
        payload: dict[str, Any] = {
            "name": name,
            "group_id": gid,
            # "remark" = campo Observações no painel AdsPower (login/senha/2FA).
            **({"remark": remark} if remark else {}),
            "user_proxy_config": user_proxy_config,
            "fingerprint_config": {
                "automatic_timezone": 0,
                "timezone": timezone,
                "language": languages or ["pt-BR", "pt"],
                "language_switch": 1,
                "webrtc": "disabled",
                "ua": user_agent or (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            # Cache de recursos estáticos do Facebook (velocidade + menos detecção)
            "static_fb_resources": 1,  # 1 = ativo, 0 = inativo
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(
                url,
                params=self._with_auth({}),
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 0:
            raise AdsPowerError(
                f"AdsPower create_profile falhou: {data.get('msg') or data}"
            )
        new_id = (data.get("data") or {}).get("id")
        if not new_id:
            raise AdsPowerError(
                f"AdsPower create_profile sem id na resposta: {data!r}"
            )
        logger.success("AdsPower create_profile OK — name=%s id=%s", name, new_id)
        return {"user_id": new_id, "raw": data.get("data") or {}}

    async def update_profile(
        self,
        user_id: str,
        *,
        remark: Optional[str] = None,
        name: Optional[str] = None,
        user_proxy_config: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Atualiza campos de um perfil existente (Observações/remark, nome, proxy).

        ``name`` renomeia o perfil no painel (mesmo campo usado em
        ``create_profile``) — usado, por ex., para marcar "ANALISE - <cnpj>"
        quando o run termina e a empresa fica esperando aprovação da Meta.

        ``user_proxy_config`` altera o proxy do perfil — ex.:
        ``{"proxy_soft": "no_proxy"}`` para remover proxy (rede local).

        Best-effort: devolve True/False; NUNCA levanta — gravar credenciais
        no painel é conveniência, não pode derrubar o provisionamento.
        """
        if not user_id:
            return False
        fields: dict[str, Any] = {"user_id": user_id}
        if remark is not None:
            fields["remark"] = remark
        if name is not None:
            fields["name"] = name
        if user_proxy_config is not None:
            fields["user_proxy_config"] = user_proxy_config
        if len(fields) == 1:  # nada além do user_id
            return False
        await _throttle_adspower_call()
        url = f"{self.base_url}/api/v1/user/update"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(
                    url,
                    params=self._with_auth({}),
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    json=fields,
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AdsPower update_profile falhou (id=%s): %s", user_id, exc)
            return False
        if data.get("code") != 0:
            logger.warning(
                "AdsPower update_profile recusado (id=%s): %s",
                user_id, data.get("msg") or data,
            )
            return False
        return True

    async def start(
        self,
        user_id: str,
        *,
        headless: bool = False,
        open_tabs: bool = False,
    ) -> dict[str, Any]:
        """Abre o perfil e retorna ``{"ws_puppeteer": str, "raw": dict}``."""
        await _throttle_adspower_call()
        params = self._with_auth({
            "user_id": user_id,
            # Flags suportadas pelo AdsPower (variam por versão; ajuste conforme docs).
            "headless": 1 if headless else 0,
            "open_tabs": 1 if open_tabs else 0,
        })

        async def _attempt() -> dict[str, Any]:
            # A API local do AdsPower engasga sob carga (timeout httpx com
            # str VAZIA, que vazava cru e matava o pipeline antes do boot —
            # visto em produção 2026-06, preflight adspower a 4s). Retry 3×
            # com backoff; persistindo, vira AdsPowerError DESCRITIVO (com o
            # tipo da exceção), que o caller trata (fallback/mensagem limpa).
            last_exc: Optional[Exception] = None
            for i in range(3):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                        r = await client.get(
                            self.start_url,
                            params=params,
                            headers=self._auth_headers(),
                        )
                        r.raise_for_status()
                        return r.json()
                except httpx.HTTPError as exc:
                    last_exc = exc
                    logger.warning(
                        "AdsPower start: API local não respondeu "
                        "(%s: %s) — tentativa %d/3.",
                        type(exc).__name__, exc or "sem detalhe", i + 1,
                    )
                    await asyncio.sleep(1.5 * (i + 1))
            raise AdsPowerError(
                f"AdsPower API não respondeu ao start após 3 tentativas "
                f"({type(last_exc).__name__}: {last_exc or 'timeout sem detalhe'}) "
                f"— a API local pode estar sobrecarregada; feche perfis "
                f"abertos ou reinicie o AdsPower."
            ) from last_exc

        def _extract_ws(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            payload = data.get("data") or {}
            ws = payload.get("ws") or {}
            return (ws.get("puppeteer") or ws.get("selenium") or "", payload)

        data = await _attempt()
        if data.get("code") != 0:
            msg = (data.get("msg") or "").strip()
            if "not exist" in msg.lower() or "does not exist" in msg.lower():
                raise AdsPowerProfileNotFound(
                    f"Profile ID '{user_id}' não existe no AdsPower local. "
                    f"Verifique em 'Browser Profiles' do painel — IDs reais são "
                    f"curtos (ex.: 'k1198abc'), não CNPJ nem API Key."
                )
            raise AdsPowerError(
                f"AdsPower start falhou (user_id={user_id}): {msg or data}"
            )
        ws_puppeteer, payload = _extract_ws(data)

        # AdsPower devolve code 0 com ws VAZIO em casos: (a) o browser ainda
        # está subindo — cold start de PERFIL NOVO passa de 10s (fingerprint +
        # cache); (b) o perfil JÁ está aberto preso (run anterior). Poll longo
        # cobre (a); stop+restart + poll cobre (b).
        async def _poll_for_ws(rounds: int, wait_s: float) -> None:
            nonlocal ws_puppeteer, payload
            r = 0
            while not ws_puppeteer and r < rounds:
                r += 1
                logger.info(
                    "AdsPower start sem ws (user_id=%s) — aguardando o browser "
                    "subir, %d/%d.", user_id, r, rounds,
                )
                await asyncio.sleep(wait_s)
                data2 = await _attempt()
                if data2.get("code") == 0:
                    ws_puppeteer, payload = _extract_ws(data2)

        await _poll_for_ws(rounds=5, wait_s=2.5)  # ~12s p/ cold start

        if not ws_puppeteer:
            logger.warning(
                "AdsPower start ainda sem ws (user_id=%s) — provável perfil "
                "preso aberto; forçando stop + restart.", user_id,
            )
            await self.stop(user_id)
            await asyncio.sleep(2.0)
            data = await _attempt()
            if data.get("code") == 0:
                ws_puppeteer, payload = _extract_ws(data)
            await _poll_for_ws(rounds=4, wait_s=2.5)  # ~10s pós-restart

        if not ws_puppeteer:
            active = await self._safe_active_count()
            hint = (
                f" Há {active} navegador(es) AdsPower aberto(s) agora."
                if active is not None else ""
            )
            raise AdsPowerError(
                f"AdsPower criou o perfil mas NÃO abriu o navegador (endpoint "
                f"ws vazio mesmo após poll + restart, user_id={user_id}).{hint} "
                f"Causas prováveis: limite de navegadores ABERTOS do seu plano "
                f"AdsPower atingido, RAM insuficiente p/ subir o browser, ou o "
                f"app travou — feche perfis abertos no AdsPower e/ou reinicie o "
                f"app, depois tente de novo. Resposta crua: {payload!r}"
            )
        return {"ws_puppeteer": ws_puppeteer, "raw": payload}

    async def stop(self, user_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.get(
                    self.stop_url,
                    params=self._with_auth({"user_id": user_id}),
                    headers=self._auth_headers(),
                )
                r.raise_for_status()
                data = r.json()
                if data.get("code") != 0:
                    msg = str(data.get("msg") or "")
                    # "User_id is not open" = perfil já está fechado → não é erro.
                    if "is not open" in msg.lower() or "not open" in msg.lower():
                        logger.debug(
                            "AdsPower stop: perfil já fechado (user_id=%s).", user_id,
                        )
                        return
                    logger.warning(
                        "AdsPower stop devolveu code != 0 (user_id=%s): %s",
                        user_id, data,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Falha ao encerrar perfil AdsPower (%s): %s", user_id, exc)

    async def delete_profile(self, user_id: str) -> bool:
        """EXCLUI o perfil do AdsPower (POST /api/v1/user/delete).

        Usado p/ apagar perfis de runs que falharam/foram encerradas (manter no
        painel do AdsPower só os 'em análise'). Best-effort: para o perfil antes
        (delete recusa perfil aberto), nunca levanta — devolve True/False."""
        if not user_id:
            return False
        # AdsPower recusa deletar um perfil ABERTO — fecha antes (idempotente).
        try:
            await self.stop(user_id)
        except Exception:  # noqa: BLE001
            pass
        await _throttle_adspower_call()
        url = f"{self.base_url}/api/v1/user/delete"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(
                    url,
                    params=self._with_auth({}),
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    json={"user_ids": [user_id]},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AdsPower delete_profile falhou (id=%s): %s", user_id, exc)
            return False
        if data.get("code") != 0:
            logger.warning(
                "AdsPower delete_profile recusado (id=%s): %s",
                user_id, data.get("msg") or data,
            )
            return False
        logger.info("AdsPower: perfil %s EXCLUÍDO (run com erro/encerrada).", user_id)
        return True
