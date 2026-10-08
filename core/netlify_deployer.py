"""Netlify deployer — cria/atualiza um site por CNPJ (subdomínio) servindo a
MESMA página do worker via Netlify Edge Function (runtime Deno).

Fluxo geral (por domínio cadastrado no painel):

  1. ``ensure_dns_zone(domain)`` — cria (ou lê) a zona DNS Netlify do domínio e
     devolve os **nameservers** que o operador coloca na Hostinger. Se a zona já
     existir (Netlify recusa recriar), buscamos a existente e devolvemos os NS.
  2. ``check_domain_nameservers(domain, expected)`` — consulta os NS reais do
     domínio (DNS ao vivo) e diz se já propagou para a Netlify.
  3. Para cada CNPJ: ``deploy_subdomain("{slug}.{domain}", cnpj_digits=...,
     token_hash=...)`` — cria um site, faz deploy (ZIP com a Edge Function),
     aponta o subdomínio como custom_domain, dispara o SSL e espera o deploy.

A página servida é idêntica à do Cloudflare Worker: o template vive em
``core/site_template.py`` e é transformado em Edge Function (sem a Cache API da
Cloudflare). A metatag ``facebook-domain-verification`` é embutida por CNPJ.

O "limite" (quantos sites por domínio antes de rotacionar para o próximo) é o
``cap`` definido pelo operador ao adicionar o domínio — ver
``NetlifyDomainPool`` e as funções ``netlify_*`` no ``core/store``.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import Any, Optional

import httpx

from config import (
    NETLIFY_API_BASE,
    NETLIFY_API_TOKEN,
    NETLIFY_DEFAULT_CAP,
    NETLIFY_DEPLOY_POLL_ATTEMPTS,
    NETLIFY_CONNECT_TIMEOUT_S,
    NETLIFY_REQUEST_TIMEOUT_S,
)
from core.site_template import (
    EDGE_FUNCTION_NAME,
    SiteTemplateError,
    render_edge_function,
    render_multi_edge_function,
)
from utils.logging_utils import install_redaction
from utils.retry import http_retry

logger = logging.getLogger("meta_automation.netlify_deployer")
install_redaction(logger)  # mascara Bearer/token em logs


# Janela de tolerância a queda de rede/DNS no gate de hospedagem (antes de
# abortar a run). Total = _NET_RETRIES * _NET_RETRY_WAIT_S.
_NET_RETRIES = 3
_NET_RETRY_WAIT_S = 10

# CNPJ de EXEMPLO embutido no template (``fallbackEmpresa``): quando a consulta
# do CNPJ falha no meio do render, a página sai com os dados DESTA empresa. É o
# marcador que a guarda de conteúdo procura antes de deixar o site subir.
_TEMPLATE_FALLBACK_CNPJ = "65144276000199"
_TEMPLATE_FALLBACK_CNPJ_FMT = "65.144.276/0001-99"


class NetlifyDeployerError(RuntimeError):
    """Erro no pipeline de deploy/DNS da Netlify."""


# ---------------------------------------------------------------------------
# Helpers de domínio / slug
# ---------------------------------------------------------------------------
def slugify_label(value: str) -> str:
    """Normaliza um rótulo DNS válido (a-z, 0-9, hífen; sem hífen nas pontas)."""
    s = re.sub(r"[^a-z0-9-]", "-", (value or "").strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:63] or "site"


def split_subdomain(fqdn: str) -> tuple[str, str]:
    """``camila.meudominio.com.br`` -> ``("camila", "meudominio.com.br")``.

    Considera o rótulo mais à esquerda como o slug do CNPJ e o resto como o
    domínio-base (a zona DNS). Levanta se não houver ao menos um subdomínio.
    """
    parts = (fqdn or "").strip().lower().strip(".").split(".")
    if len(parts) < 3:
        # precisa de pelo menos slug + dominio.tld (ex.: a.b.c)
        raise NetlifyDeployerError(
            f"FQDN inválido para subdomínio de CNPJ: {fqdn!r} "
            "(esperado algo como slug.seudominio.com)."
        )
    return parts[0], ".".join(parts[1:])


# ---------------------------------------------------------------------------
# Checagem de nameservers (propagação) — DNS ao vivo
# ---------------------------------------------------------------------------
def resolve_nameservers(domain: str, retries: int = 3, retry_delay_s: float = 2.0) -> list[str]:
    """NS atuais do domínio (consulta DNS ao vivo com retry). [] se indeterminável.

    Usa dnspython se disponível; degrada para [] (status 'desconhecido') se a
    lib não estiver instalada ou a consulta falhar.

    MUDANÇA 2026-07-01: adicionado retry automático (3x com delay 2s) para
    resolver falhas transitórias de DNS quando nameservers acabam de propagar.
    Antes: falha 1x → retorna vazio → "ainda não propagaram"
    Depois: tenta 3x → aguarda 2s entre tentativas → detecção correta
    """
    import time

    domain = (domain or "").strip().strip(".").lower()
    if not domain:
        return []
    try:
        import dns.resolver  # type: ignore  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.debug("dnspython ausente — checagem de NS indisponível.")
        return []

    for attempt in range(1, retries + 1):
        try:
            answers = dns.resolver.resolve(domain, "NS")
            result = sorted({str(r.target).rstrip(".").lower() for r in answers})
            if result:
                if attempt > 1:
                    logger.info("resolve NS de %s sucesso (tentativa %d/%d)", domain, attempt, retries)
                return result
        except Exception as exc:  # noqa: BLE001
            if attempt < retries:
                logger.debug(
                    "resolve NS de %s falhou (tentativa %d/%d): %s — aguardando %ds",
                    domain, attempt, retries, type(exc).__name__, retry_delay_s
                )
                time.sleep(retry_delay_s)
            else:
                logger.debug("resolve NS de %s falhou após %d tentativas", domain, retries)

    return []


def check_domain_nameservers(
    domain: str, expected_nameservers: list[str]
) -> dict[str, Any]:
    """Compara os NS reais do domínio com os esperados (Netlify).

    Retorna ``{"propagated", "found", "expected"}``. ``propagated`` é True
    quando pelo menos um NS esperado aparece nos NS reais (a Hostinger às vezes
    devolve a lista em ordem/subset diferente). ``found == []`` => ainda não dá
    pra determinar (não propagou ou DNS sem resposta).
    """
    expected = sorted({(n or "").rstrip(".").lower() for n in (expected_nameservers or []) if n})
    found = resolve_nameservers(domain)
    propagated = bool(found and expected and any(
        any(e in f or f in e for e in expected) for f in found
    ))
    return {"propagated": propagated, "found": found, "expected": expected}


# ---------------------------------------------------------------------------
# Cliente / deployer Netlify
# ---------------------------------------------------------------------------
class NetlifyDeployer:
    """Chamadas à API Netlify para 1 token (1 conta)."""

    def __init__(
        self,
        *,
        api_token: str = NETLIFY_API_TOKEN,
        api_base: str = NETLIFY_API_BASE,
        timeout_s: float = NETLIFY_REQUEST_TIMEOUT_S,
    ) -> None:
        self.api_token = (api_token or "").strip()
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s

    @property
    def _timeout(self) -> "httpx.Timeout":
        """Conectar é rápido ou não é — ler pode demorar (deploy).

        Sem o ``connect`` curto, uma máquina sem rota até a Netlify ficava 60s
        por tentativa × 3 tentativas × N chamadas: o operador via só
        "Publicando…" girando. Agora falha em ~10s com erro claro."""
        return httpx.Timeout(
            self.timeout_s, connect=min(NETLIFY_CONNECT_TIMEOUT_S, self.timeout_s)
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_token)

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": "meta-farm/netlify-deployer",
        }
        if extra:
            h.update(extra)
        return h

    # ---- credenciais / conta ------------------------------------------
    @http_retry(exceptions=(httpx.TransportError, httpx.TimeoutException), logger=logger)
    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(
                f"{self.api_base}{path}", headers=self._headers(), **kwargs
            )

    async def test_creds(self) -> dict[str, Any]:
        """Valida o token (GET /accounts). Não lança — retorna ok/message/slug."""
        if not self.enabled:
            return {"ok": False, "message": "Token Netlify vazio.", "account_slug": None}
        try:
            r = await self._get("/accounts")
        except httpx.HTTPError as exc:
            # Falha de TRANSPORTE (DNS caiu, sem internet, VPN/proxy) NÃO é
            # token inválido — e era assim que aparecia pro operador, que ia
            # conferir token e domínio à toa. Marca ``network`` p/ quem chama
            # saber que dá pra tentar de novo.
            raw = f"{type(exc).__name__}: {exc}"
            low = str(exc).lower()
            dns = any(k in low for k in (
                "nodename nor servname", "getaddrinfo", "name or service not known",
                "temporary failure in name resolution", "no address associated",
            ))
            return {
                "ok": False,
                "network": True,
                "dns": dns,
                "message": (
                    f"sem DNS — a máquina não resolveu o host da API da Netlify "
                    f"({raw}). Internet caiu, VPN/proxy ou DNS do Mac."
                    if dns else f"Netlify inacessível: {raw}"
                ),
                "account_slug": None,
            }
        if r.status_code == 401:
            return {"ok": False, "message": "Token Netlify inválido (HTTP 401).", "account_slug": None}
        if r.status_code != 200:
            return {"ok": False, "message": f"Netlify HTTP {r.status_code}.", "account_slug": None}
        try:
            accounts = r.json()
        except Exception:  # noqa: BLE001
            accounts = []
        slug = None
        if isinstance(accounts, list) and accounts:
            slug = accounts[0].get("slug") or accounts[0].get("name")
        return {
            "ok": True,
            "message": "Token válido — conta acessível.",
            "account_slug": slug,
        }

    async def get_account_slug(self) -> Optional[str]:
        res = await self.test_creds()
        return res.get("account_slug")

    # ---- DNS zone ------------------------------------------------------
    async def find_dns_zone(self, domain: str) -> Optional[dict[str, Any]]:
        """Procura uma zona DNS já existente pelo nome do domínio (paginado)."""
        domain = domain.strip().lower().strip(".")
        page = 1
        while page <= 20:
            try:
                r = await self._get("/dns_zones", params={"page": page, "per_page": 100})
            except httpx.HTTPError:
                return None
            if r.status_code != 200:
                return None
            try:
                zones = r.json()
            except Exception:  # noqa: BLE001
                return None
            if not zones:
                break
            for z in zones:
                if (z.get("name") or "").lower() == domain:
                    return z
            if len(zones) < 100:
                break
            page += 1
        return None

    async def ensure_dns_zone(self, domain: str) -> dict[str, Any]:
        """Garante a zona DNS do domínio na Netlify e devolve os nameservers.

        Retorna ``{"zone_id", "nameservers", "name", "created"}``. Se a zona já
        existir (a Netlify recusa recriar com HTTP 422), buscamos a existente e
        devolvemos os nameservers dela — assim o operador SEMPRE vê os NS para
        configurar na Hostinger, mesmo re-adicionando um domínio já cadastrado.
        """
        domain = domain.strip().lower().strip(".")
        if not domain:
            raise NetlifyDeployerError("Domínio vazio.")

        existing = await self.find_dns_zone(domain)
        if existing:
            return {
                "zone_id": existing.get("id"),
                "nameservers": existing.get("dns_servers") or [],
                "name": existing.get("name"),
                "created": False,
            }

        account_slug = await self.get_account_slug()
        payload = {"name": domain}
        if account_slug:
            payload["account_slug"] = account_slug
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self.api_base}/dns_zones",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise NetlifyDeployerError(f"Falha ao criar zona DNS: {exc}") from exc

        if r.status_code in (200, 201):
            z = r.json()
            return {
                "zone_id": z.get("id"),
                "nameservers": z.get("dns_servers") or [],
                "name": z.get("name"),
                "created": True,
            }

        # Create recusado (ex.: 422 "already exists"/"not valid name"): a zona
        # provavelmente JÁ existe. Tenta achar de novo e devolver os NS dela.
        again = await self.find_dns_zone(domain)
        if again:
            return {
                "zone_id": again.get("id"),
                "nameservers": again.get("dns_servers") or [],
                "name": again.get("name"),
                "created": False,
            }
        raise NetlifyDeployerError(
            f"Netlify recusou criar a zona DNS (HTTP {r.status_code}) e não "
            f"encontrei uma zona existente para {domain!r}. Detalhe: {r.text[:200]}. "
            "Se a zona estiver em OUTRA conta Netlify, use o token daquela conta."
        )

    # ---- Deploy --------------------------------------------------------
    @staticmethod
    def _prerender_pages(
        *, cnpj_digits: str, token_hash: str, empresa_json: Optional[str] = None
    ) -> dict[str, str]:
        """Pré-renderiza as páginas (/, /privacidade, /termos) como HTML estático.

        Roda o MESMO código do worker no Node (que já está instalado p/ o
        frontend): busca os dados do CNPJ ao vivo e gera o HTML final, com a
        metatag de verificação embutida. Estático sempre funciona no Netlify
        (deploy de Edge Function via zip da API NÃO executa a função)."""
        import json as _json  # noqa: PLC0415
        import os  # noqa: PLC0415
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        node = shutil.which("node")
        if not node:
            raise NetlifyDeployerError(
                "Node.js não encontrado no PATH — é necessário para gerar a "
                "página do site. Rode o INSTALAR.bat (instala o Node)."
            )
        edge_js = render_edge_function(
            cnpj_digits=cnpj_digits, token_hash=token_hash, empresa_json=empresa_json,
        )
        harness = (
            'import { pathToFileURL } from "node:url";\n'
            'globalThis.Netlify = globalThis.Netlify || { env: { get: () => undefined } };\n'
            '// Blinda o fetch: erro de rede vira 503 (o worker cai no fallback e\n'
            '// a página renderiza mesmo assim, sem ficar em branco).\n'
            'const __f = globalThis.fetch;\n'
            'globalThis.fetch = async (...a) => { try { return await __f(...a); } catch (e) { return new Response("", { status: 503 }); } };\n'
            'const handler = (await import(pathToFileURL(process.argv[2]).href)).default;\n'
            'const paths = ["/", "/privacidade", "/termos"];\n'
            'const out = {};\n'
            'for (const p of paths) {\n'
            '  try { const res = await handler(new Request("https://site.local" + p), {}); out[p] = await res.text(); }\n'
            '  catch (e) { out[p] = ""; }\n'
            '}\n'
            'process.stdout.write(JSON.stringify(out));\n'
        )
        with tempfile.TemporaryDirectory() as d:
            site_path = os.path.join(d, "site.mjs")
            harness_path = os.path.join(d, "render.mjs")
            with open(site_path, "w", encoding="utf-8") as f:
                f.write(edge_js)
            with open(harness_path, "w", encoding="utf-8") as f:
                f.write(harness)
            try:
                proc = subprocess.run(
                    [node, harness_path, site_path],
                    capture_output=True, timeout=120,
                )
            except Exception as exc:  # noqa: BLE001
                raise NetlifyDeployerError(f"Falha ao rodar o Node p/ renderizar: {exc}") from exc
        if proc.returncode != 0:
            raise NetlifyDeployerError(
                f"Render no Node falhou: {proc.stderr.decode('utf-8', 'ignore')[:300]}"
            )
        try:
            pages = _json.loads(proc.stdout.decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            raise NetlifyDeployerError(f"Saída do render inválida: {exc}") from exc
        home = pages.get("/", "") or ""
        if not home.strip():
            raise NetlifyDeployerError("O render não produziu a home do site.")
        # GUARDA DO CNPJ: o template tem um ``fallbackEmpresa`` fixo (o exemplo
        # 65.144.276/0001-99, "MARIA APARECIDA DA SILVA") que ele usa quando a
        # consulta do CNPJ falha DURANTE o render. Sem esta guarda o deploy subia
        # um site com os dados de OUTRA empresa — repetidos em todos os
        # subdomínios, que é exatamente o rastro que queima domínio.
        _c = re.sub(r"\D+", "", cnpj_digits or "")
        _fmt = (
            f"{_c[:2]}.{_c[2:5]}.{_c[5:8]}/{_c[8:12]}-{_c[12:]}"
            if len(_c) == 14 else ""
        )
        if _c and _c != _TEMPLATE_FALLBACK_CNPJ and _c not in home and (
            not _fmt or _fmt not in home
        ):
            if _TEMPLATE_FALLBACK_CNPJ_FMT in home:
                raise NetlifyDeployerError(
                    "A consulta do CNPJ falhou durante a geração e a página caiu "
                    f"no CNPJ de exemplo do template ({_TEMPLATE_FALLBACK_CNPJ_FMT}) "
                    "— publicação ABORTADA para não subir um site com dados de "
                    "outra empresa. Tente de novo em alguns segundos."
                )
            logger.warning(
                "render: o CNPJ %s não aparece na home gerada (%d bytes) — "
                "conferir o template/consulta antes de confiar no conteúdo.",
                _c, len(home),
            )
        # Com token: a metatag é OBRIGATÓRIA (é o que o Meta vai buscar).
        # Sem token (``token_hash=""``): publicação em 2 etapas da aba
        # "Gerar Site" — o site sobe primeiro e a metatag entra no re-deploy.
        if token_hash and (
            "facebook-domain-verification" not in home or token_hash not in home
        ):
            raise NetlifyDeployerError(
                "O render não produziu a metatag de verificação na home."
            )
        return pages

    @staticmethod
    def _build_site_zip(
        *, cnpj_digits: str, token_hash: str, empresa_json: Optional[str] = None
    ) -> bytes:
        """ZIP do deploy: HTML ESTÁTICO pré-renderizado (/, /privacidade, /termos).

        Estático garante que o Netlify serve a página (deploy via zip da API não
        executa Edge Function). A página é gerada pelo MESMO worker, no Node."""
        pages = NetlifyDeployer._prerender_pages(
            cnpj_digits=cnpj_digits, token_hash=token_hash, empresa_json=empresa_json,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", pages.get("/") or "")
            if pages.get("/privacidade"):
                zf.writestr("privacidade/index.html", pages["/privacidade"])
            if pages.get("/termos"):
                zf.writestr("termos/index.html", pages["/termos"])
            # 404 -> mostra a home (a metatag fica disponível em qualquer rota).
            zf.writestr("404.html", pages.get("/") or "")
        return buf.getvalue()

    async def create_site(self, *, name: str) -> dict[str, Any]:
        """POST /sites — cria um site novo (nome *.netlify.app único)."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self.api_base}/sites",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"name": name},
                )
        except httpx.HTTPError as exc:
            raise NetlifyDeployerError(f"Falha ao criar site: {exc}") from exc
        if r.status_code not in (200, 201):
            raise NetlifyDeployerError(
                f"Netlify recusou criar o site (HTTP {r.status_code}): {r.text[:300]}"
            )
        return r.json()

    async def set_custom_domain(self, site_id: str, fqdn: str) -> None:
        """PATCH /sites/{id} — aponta o subdomínio como custom_domain do site."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.patch(
                    f"{self.api_base}/sites/{site_id}",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"custom_domain": fqdn},
                )
        except httpx.HTTPError as exc:
            raise NetlifyDeployerError(f"Falha ao definir custom_domain: {exc}") from exc
        if r.status_code not in (200, 201):
            raise NetlifyDeployerError(
                f"Netlify recusou o custom_domain {fqdn!r} (HTTP {r.status_code}): {r.text[:300]}"
            )

    async def publish_site(self, site_id: str) -> bool:
        """PATCH /sites/{id} — publica o site (torna públicamente acessível)."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.patch(
                    f"{self.api_base}/sites/{site_id}",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"state": "current"},
                )
        except httpx.HTTPError as exc:
            logger.warning(f"Falha ao publicar site {site_id}: {exc}")
            return False
        if r.status_code in (200, 201):
            logger.info("Site %s publicado com sucesso.", site_id)
            return True
        logger.warning(
            f"Netlify recusou publicar o site {site_id} (HTTP {r.status_code}): {r.text[:300]}"
        )
        return False

    async def delete_site(self, site_id_or_domain: str) -> bool:
        """DELETE /sites/{id} — apaga um site (aceita o id OU o domínio)."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.delete(
                    f"{self.api_base}/sites/{site_id_or_domain}",
                    headers=self._headers(),
                )
            return r.status_code in (200, 204, 404)
        except httpx.HTTPError as exc:
            logger.debug("delete_site %s falhou: %s", site_id_or_domain, exc)
            return False

    async def provision_ssl(self, site_id: str) -> bool:
        """POST /sites/{id}/ssl — dispara a emissão do certificado SSL do domínio
        custom (Let's Encrypt). Best-effort: a Netlify também provisiona sozinha,
        mas chamar aqui acelera/garante o início. Retorna True se aceitou."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self.api_base}/sites/{site_id}/ssl",
                    headers=self._headers(),
                )
            return r.status_code in (200, 201, 202)
        except httpx.HTTPError as exc:
            logger.debug("provision_ssl %s falhou: %s", site_id, exc)
            return False

    async def deploy_zip(self, site_id: str, zip_bytes: bytes) -> dict[str, Any]:
        """POST /sites/{id}/deploys com Content-Type application/zip."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self.api_base}/sites/{site_id}/deploys",
                    headers=self._headers({"Content-Type": "application/zip"}),
                    content=zip_bytes,
                )
        except httpx.HTTPError as exc:
            raise NetlifyDeployerError(f"Falha no deploy (zip): {exc}") from exc
        if r.status_code not in (200, 201):
            raise NetlifyDeployerError(
                f"Netlify recusou o deploy (HTTP {r.status_code}): {r.text[:300]}"
            )
        return r.json()

    async def wait_deploy_ready(
        self, site_id: str, deploy_id: str, *, attempts: int = NETLIFY_DEPLOY_POLL_ATTEMPTS
    ) -> bool:
        """Faz poll até o deploy ficar ``ready`` (ou esgotar tentativas)."""
        import asyncio  # noqa: PLC0415
        for i in range(1, attempts + 1):
            try:
                r = await self._get(f"/sites/{site_id}/deploys/{deploy_id}")
                state = (r.json() or {}).get("state") if r.status_code == 200 else None
            except Exception as exc:  # noqa: BLE001
                state = None
                logger.debug("poll deploy %s: %s", deploy_id, exc)
            if state == "ready":
                logger.info("Deploy %s pronto (tentativa %d).", deploy_id, i)
                return True
            if state == "error":
                raise NetlifyDeployerError(f"Deploy {deploy_id} falhou (state=error).")
            logger.debug("deploy %s state=%s (tentativa %d/%d)", deploy_id, state, i, attempts)
            await asyncio.sleep(3)
        return False

    async def deploy_subdomain(
        self, fqdn: str, *, cnpj_digits: str, token_hash: str,
        empresa_json: Optional[str] = None,
    ) -> str:
        """Fluxo completo: cria site -> deploy da edge function -> aponta domínio.

        Retorna ``https://{fqdn}``. A zona DNS do domínio-base precisa já existir
        na Netlify (ver ``ensure_dns_zone``) para o registro do subdomínio ser
        criado automaticamente.

        ``empresa_json``: dados do CNPJ já consultados no backend (formato do
        template) para EMBUTIR no render e não depender da consulta LIVE."""
        if not self.enabled:
            raise NetlifyDeployerError("Token Netlify ausente.")
        slug, base = split_subdomain(fqdn)
        try:
            zip_bytes = self._build_site_zip(
                cnpj_digits=cnpj_digits, token_hash=token_hash,
                empresa_json=empresa_json,
            )
        except SiteTemplateError as exc:
            raise NetlifyDeployerError(f"Erro ao renderizar a página: {exc}") from exc

        import secrets  # noqa: PLC0415
        site_name = f"{slugify_label(slug)}-{slugify_label(base)}-{secrets.token_hex(3)}"[:63]
        site = await self.create_site(name=site_name)
        site_id = site.get("id")
        if not site_id:
            raise NetlifyDeployerError(f"Site criado sem id: {site}")

        deploy = await self.deploy_zip(site_id, zip_bytes)
        deploy_id = deploy.get("id")
        # Aponta o subdomínio (cria o registro DNS na zona gerenciada).
        await self.set_custom_domain(site_id, fqdn)
        # Dispara a emissão do certificado SSL do subdomínio (assíncrono na
        # Netlify; chamar aqui acelera — o Meta verifica via HTTPS).
        await self.provision_ssl(site_id)
        if deploy_id:
            ready = await self.wait_deploy_ready(site_id, deploy_id)
            if not ready:
                logger.warning(
                    "Deploy %s não confirmou 'ready' a tempo — pode levar mais "
                    "alguns segundos para propagar.", deploy_id,
                )
        # Publica o site para torná-lo publicamente acessível
        await self.publish_site(site_id)
        logger.info("Site publicado: https://%s (site_id=%s)", fqdn, site_id)
        return f"https://{fqdn}"

    async def find_site_by_domain(self, fqdn: str) -> Optional[dict[str, Any]]:
        """Acha na conta Netlify o site cujo domínio é ``fqdn`` (ou None).

        Necessário para RE-DEPLOYAR sem criar site novo: dois sites não podem
        ter o mesmo ``custom_domain``, e recriar perderia o certificado SSL já
        emitido para o subdomínio."""
        fq = (fqdn or "").strip().lower().strip(".")
        if not fq:
            return None
        # Atalho: a API aceita o domínio no lugar do site_id.
        try:
            r = await self._get(f"/sites/{fq}")
            if r.status_code == 200:
                data = r.json() or {}
                if isinstance(data, dict) and data.get("id"):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.debug("find_site_by_domain(%s): atalho falhou: %s", fq, exc)
        # Varredura paginada da conta.
        for page in range(1, 11):
            try:
                r = await self._get("/sites", params={"page": page, "per_page": 100})
            except Exception as exc:  # noqa: BLE001
                logger.debug("find_site_by_domain: listagem falhou: %s", exc)
                return None
            if r.status_code != 200:
                return None
            items = r.json() or []
            if not items:
                return None
            for s in items:
                cands = {
                    (s.get("custom_domain") or "").strip().lower(),
                    (s.get("name") or "").strip().lower(),
                    (s.get("url") or "").strip().lower()
                    .replace("https://", "").replace("http://", "").rstrip("/"),
                }
                cands |= {
                    str(a or "").strip().lower() for a in (s.get("domain_aliases") or [])
                }
                if fq in cands:
                    return s
        return None

    async def redeploy_subdomain(
        self, fqdn: str, *, cnpj_digits: str, token_hash: str
    ) -> tuple[str, bool]:
        """Re-publica o site do ``fqdn`` com um novo conteúdo (ex.: metatag).

        Mantém o MESMO site Netlify — logo o mesmo subdomínio, DNS e
        certificado SSL. Se o site não existir mais, cria do zero.
        Devolve ``(url, criado_do_zero)``."""
        if not self.enabled:
            raise NetlifyDeployerError("Token Netlify ausente.")
        fq = (fqdn or "").strip().lower().strip(".")
        site = await self.find_site_by_domain(fq)
        if not site:
            logger.info(
                "redeploy: site de %s não encontrado na conta — publicando do zero.", fq
            )
            url = await self.deploy_subdomain(
                fq, cnpj_digits=cnpj_digits, token_hash=token_hash
            )
            return url, True
        site_id = site.get("id")
        try:
            zip_bytes = self._build_site_zip(
                cnpj_digits=cnpj_digits, token_hash=token_hash
            )
        except SiteTemplateError as exc:
            raise NetlifyDeployerError(f"Erro ao renderizar a página: {exc}") from exc
        deploy = await self.deploy_zip(site_id, zip_bytes)
        deploy_id = deploy.get("id")
        if deploy_id and not await self.wait_deploy_ready(site_id, deploy_id):
            logger.warning(
                "Re-deploy %s não confirmou 'ready' a tempo — pode levar mais "
                "alguns segundos para propagar.", deploy_id,
            )
        await self.publish_site(site_id)
        logger.info("Site RE-publicado: https://%s (site_id=%s)", fq, site_id)
        return f"https://{fq}", False

    # ------------------------------------------------------------------
    # MODELO WILDCARD — 1 site por domínio serve TODOS os subdomínios
    # ------------------------------------------------------------------
    @staticmethod
    def _build_multi_zip(entries: Any) -> bytes:
        """ZIP do deploy com a Edge Function MULTI-CNPJ (mapa slug->cnpj/token)."""
        edge_js = render_multi_edge_function(entries)
        netlify_toml = (
            "[[edge_functions]]\n"
            '  path = "/*"\n'
            f'  function = "{EDGE_FUNCTION_NAME}"\n'
        )
        index_html = (
            "<!DOCTYPE html><html lang=\"pt-BR\"><head><meta charset=\"UTF-8\">"
            "<title>Carregando…</title></head><body></body></html>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("netlify.toml", netlify_toml)
            zf.writestr(f"netlify/edge-functions/{EDGE_FUNCTION_NAME}.js", edge_js)
            zf.writestr("index.html", index_html)
        return buf.getvalue()

    async def ensure_domain_site(self, domain: str) -> str:
        """Garante o site ÚNICO do domínio (modelo wildcard) e devolve o site_id.

        Cria o site (se ainda não houver), aponta o apex + ``*.{domain}`` como
        domínios do site e dispara o certificado WILDCARD (o Netlify provisiona
        sozinho porque o DNS é dele). Assim TODO subdomínio nasce com HTTPS — sem
        emitir cert por CNPJ. Idempotente: reusa o site_id salvo no store.
        """
        from core import store  # noqa: PLC0415
        domain = (domain or "").strip().lower().strip(".")
        row = store.get_netlify_domain(domain)
        zone_id = (row or {}).get("zone_id") or ""
        site_id = (row or {}).get("site_id") or ""
        site: Optional[dict[str, Any]] = None

        if site_id:
            # Reusa o site existente — confirma que ainda existe e pega os dados.
            try:
                r = await self._get(f"/sites/{site_id}")
                if r.status_code == 200:
                    site = r.json()
                else:
                    site_id = ""  # sumiu — recria abaixo
            except httpx.HTTPError:
                pass  # rede instável — segue (tenta o wildcard DNS mesmo assim)

        if not site_id:
            import secrets  # noqa: PLC0415
            name = f"{slugify_label(domain)}-{secrets.token_hex(3)}"[:63]
            site = await self.create_site(name=name)
            site_id = site.get("id")
            if not site_id:
                raise NetlifyDeployerError(f"Site do domínio criado sem id: {site}")
            store.netlify_set_site_id(domain, site_id)
            logger.info("Site do domínio %s criado (site_id=%s).", domain, site_id)

        # Associa o apex ao site + dispara SSL. O domínio WILDCARD em si
        # (``*.{domain}``) NÃO é adicionável por API (a Netlify recusa) — o
        # operador adiciona UMA vez pelo PAINEL do Netlify (Domain management ->
        # add ``*.{domain}``). Estando em DNS Netlify, o registro + o cert
        # wildcard são montados automaticamente pela própria Netlify. Por isso
        # NÃO criamos o registro DNS aqui (evita conflito com o que o painel cria).
        await self._configure_wildcard_domain(site_id, domain)
        await self.provision_ssl(site_id)
        _ = (zone_id, site)  # mantidos p/ compat; DNS wildcard é via painel
        return site_id

    async def _configure_wildcard_domain(self, site_id: str, domain: str) -> bool:
        """Associa o domínio (apex) ao site. O wildcard de subdomínio NÃO é
        configurável por API (a Netlify recusa ``*.dominio`` em custom_domain e
        domain_aliases) — é uma flag que o SUPORTE da Netlify habilita por site
        em contas Pro+. Aqui só garantimos o apex como custom_domain; o roteamento
        wildcard passa a valer quando o suporte ativa a flag + o CNAME *.dominio
        (criado em ensure_wildcard_dns) propaga. Best-effort."""
        payload = {"custom_domain": domain}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.patch(
                    f"{self.api_base}/sites/{site_id}",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json=payload,
                )
            if r.status_code in (200, 201):
                logger.info("Apex %s associado ao site %s.", domain, site_id)
                return True
            logger.warning(
                "Netlify recusou associar o apex %s (HTTP %s): %s",
                domain, r.status_code, r.text[:300],
            )
        except httpx.HTTPError as exc:
            logger.warning("Erro ao associar o apex %s: %s", domain, exc)
        return False

    async def list_dns_records(self, zone_id: str) -> list[dict[str, Any]]:
        """Lista os registros DNS de uma zona (ou [] em falha)."""
        try:
            r = await self._get(f"/dns_zones/{zone_id}/dns_records")
            return r.json() if r.status_code == 200 else []
        except httpx.HTTPError:
            return []

    async def ensure_wildcard_dns(
        self, zone_id: str, domain: str, site: dict[str, Any]
    ) -> bool:
        """Garante 1 registro DNS ``*.{domain}`` apontando para o site (NETLIFY).

        Sem ele, novos subdomínios não resolvem. Idempotente: se já existir um
        registro para ``*.{domain}``, não cria de novo. Retorna True se OK/existe.
        """
        hostname = f"*.{domain}"
        for rec in await self.list_dns_records(zone_id):
            if (rec.get("hostname") or "").lower().rstrip(".") == hostname:
                return True  # já existe
        value = (site.get("default_domain")
                 or (f"{site.get('name')}.netlify.app" if site.get("name") else ""))
        if not value:
            logger.warning("Sem subdomínio netlify.app do site p/ o registro wildcard.")
            return False
        # Tenta o tipo nativo NETLIFY; se a conta/API recusar, cai p/ CNAME.
        last_err = ""
        for rec_type in ("NETLIFY", "CNAME"):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.post(
                        f"{self.api_base}/dns_zones/{zone_id}/dns_records",
                        headers=self._headers({"Content-Type": "application/json"}),
                        json={"type": rec_type, "hostname": hostname, "value": value},
                    )
                if r.status_code in (200, 201):
                    logger.info(
                        "Registro DNS wildcard criado (%s): %s -> %s",
                        rec_type, hostname, value,
                    )
                    return True
                last_err = f"{rec_type} HTTP {r.status_code}: {r.text[:160]}"
                logger.warning("Registro wildcard %s recusado: %s", rec_type, last_err)
            except httpx.HTTPError as exc:
                last_err = f"{rec_type}: {exc}"
                logger.warning("Erro ao criar registro wildcard (%s): %s", rec_type, exc)
        logger.error("Não consegui criar o registro DNS wildcard: %s", last_err)
        return False

    async def deploy_domain_map(self, site_id: str, entries: Any) -> None:
        """Redeploya o site do domínio com o mapa COMPLETO de CNPJs."""
        zip_bytes = self._build_multi_zip(entries)
        deploy = await self.deploy_zip(site_id, zip_bytes)
        deploy_id = deploy.get("id")
        if deploy_id:
            await self.wait_deploy_ready(site_id, deploy_id)

    async def publish_cnpj(
        self, domain: str, *, slug: str, cnpj_digits: str, token_hash: str
    ) -> str:
        """Publica/atualiza UM CNPJ no site wildcard do domínio.

        Serializa por domínio (lock) para que redeploys concorrentes não percam
        entradas do mapa: grava a entrada no store, reconstrói o mapa COMPLETO a
        partir do store e redeploya o site único. Retorna ``https://{slug}.{domain}``.
        """
        from core import store  # noqa: PLC0415
        if not self.enabled:
            raise NetlifyDeployerError("Token Netlify ausente.")
        domain = (domain or "").strip().lower().strip(".")
        slug = slugify_label(slug)
        fqdn = f"{slug}.{domain}"
        # valida cedo (mesma regra do template)
        if not re.fullmatch(r"\d{14}", cnpj_digits or ""):
            raise NetlifyDeployerError(f"cnpj inválido: {cnpj_digits!r}")

        # threading.Lock (não asyncio): cada job roda em seu PRÓPRIO event loop
        # (asyncio.run por job, em threads distintas), então um asyncio.Lock
        # global quebraria entre loops. O threading.Lock serializa entre threads
        # — segurar durante os awaits aqui é OK (1 job por thread/loop).
        lock = _domain_lock(domain)
        with lock:
            site_id = await self.ensure_domain_site(domain)
            store.netlify_upsert_site_entry(domain, slug, cnpj_digits, token_hash, fqdn)
            entries = store.netlify_list_site_entries(domain)
            try:
                await self.deploy_domain_map(site_id, entries)
            except SiteTemplateError as exc:
                raise NetlifyDeployerError(f"Erro ao renderizar o mapa: {exc}") from exc
            # Publica o site para torná-lo publicamente acessível após o deploy
            await self.publish_site(site_id)
        logger.info("CNPJ publicado no wildcard: https://%s (slug=%s)", fqdn, slug)
        return f"https://{fqdn}"


# Locks por domínio (serializam redeploys do mesmo site wildcard entre threads).
import threading as _threading  # noqa: E402
_DOMAIN_LOCKS_GUARD = _threading.Lock()
_DOMAIN_LOCKS: dict[str, "_threading.Lock"] = {}


def _domain_lock(domain: str) -> "_threading.Lock":
    with _DOMAIN_LOCKS_GUARD:
        lock = _DOMAIN_LOCKS.get(domain)
        if lock is None:
            lock = _threading.Lock()
            _DOMAIN_LOCKS[domain] = lock
        return lock


# ---------------------------------------------------------------------------
# Gate de prontidão — usado pelo run_service ANTES de gastar SMS/conta
# ---------------------------------------------------------------------------
async def validate_netlify_ready(
    domain: Optional[str], api_token: Optional[str]
) -> dict[str, Any]:
    """Netlify PRONTO para uma run? Checa token + zona DNS + propagação dos NS.

    Retorna ``{"ok", "detail", "nameservers"}``.
    """
    domain = (domain or "").strip().lower().strip(".")
    api_token = (api_token or "").strip()
    if not (domain and api_token):
        return {
            "ok": False,
            "detail": "Netlify NÃO configurado (domínio + token).",
            "nameservers": [],
        }
    dep = NetlifyDeployer(api_token=api_token)
    # Blip de rede/DNS não pode matar a run: 2026-08-27 a máquina ficou 23s sem
    # resolver api.netlify.com e 14 runs foram abortadas em sequência com a
    # mensagem "token Netlify inválido" — com token e domínio perfeitos.
    # Tenta de novo por ~30s (além dos ~3s de retry interno do _get) e só então
    # desiste, dizendo a verdade: é rede, não credencial.
    import asyncio  # noqa: PLC0415

    creds = await dep.test_creds()
    for _tent in range(1, _NET_RETRIES + 1):
        if creds.get("ok") or not creds.get("network"):
            break
        logger.warning(
            "Netlify inacessível (rede/DNS) — tentativa %d/%d, aguardando %ds. "
            "Isto NÃO é token inválido: %s",
            _tent, _NET_RETRIES, _NET_RETRY_WAIT_S, creds.get("message"),
        )
        await asyncio.sleep(_NET_RETRY_WAIT_S)
        creds = await dep.test_creds()
    if not creds.get("ok"):
        if creds.get("network"):
            return {
                "ok": False,
                "detail": (
                    "Netlify INACESSÍVEL pela rede após "
                    f"~{_NET_RETRIES * _NET_RETRY_WAIT_S}s — {creds.get('message')} "
                    "O TOKEN NÃO FOI RECUSADO e o domínio não tem nada a ver: "
                    "confira internet/VPN/DNS da máquina e re-dispare."
                ),
                "nameservers": [],
                "network": True,
            }
        return {"ok": False, "detail": f"token Netlify inválido: {creds.get('message')}", "nameservers": []}
    zone = await dep.find_dns_zone(domain)
    if not zone:
        return {
            "ok": False,
            "detail": (
                f"o domínio {domain!r} ainda não tem zona DNS na Netlify — "
                "adicione o domínio no painel para gerar os nameservers."
            ),
            "nameservers": [],
        }
    nameservers = zone.get("dns_servers") or []
    ns = check_domain_nameservers(domain, nameservers)
    # TOLERANTE: só bloqueia se a consulta de NS encontrou servidores e eles
    # apontam COMPROVADAMENTE para FORA da Netlify (domínio ainda no registrador).
    # Se a consulta não retornou nada (dnspython ausente, hiccup de DNS) OU já
    # bate com a Netlify, libera — a prova final é o deploy + a metatag servida.
    if ns["found"] and not ns["propagated"]:
        return {
            "ok": False,
            "detail": (
                f"os nameservers do domínio {domain!r} ainda apontam para fora da "
                f"Netlify ({', '.join(ns['found'][:4])}). Coloque os NS da Netlify "
                "na Hostinger e aguarde a propagação."
            ),
            "nameservers": nameservers,
        }
    detail = ("ok (token válido, zona DNS ativa, NS propagados)."
              if ns["propagated"] else
              "ok (token + zona DNS; propagação de NS não confirmada ao vivo, "
              "seguindo — o deploy confirma).")
    return {"ok": True, "detail": detail, "nameservers": nameservers}


# ---------------------------------------------------------------------------
# Pool de domínios — rotaciona por cap (limite por domínio definido na UI)
# ---------------------------------------------------------------------------
class NetlifyDomainPool:
    """Escolhe o domínio ativo entre os cadastrados, respeitando o cap.

    Usa o ``core.store``: pega o primeiro domínio com ``sites_used < cap``. Cada
    deploy bem-sucedido chama ``mark_used(domain)`` para incrementar o contador
    (quando o domínio enche, o próximo passa a ser usado).
    """

    def __init__(self) -> None:
        from core import store  # noqa: PLC0415
        self._store = store

    @property
    def enabled(self) -> bool:
        return bool(self._store.list_netlify_domains())

    def active(self) -> Optional[dict[str, Any]]:
        """Domínio ativo (primeiro com cota livre) ou None."""
        return self._store.get_active_netlify_domain()

    def deployer_for(self, domain_row: dict[str, Any]) -> NetlifyDeployer:
        return NetlifyDeployer(api_token=domain_row.get("api_token") or "")

    def mark_used(self, domain: str) -> int:
        return self._store.netlify_usage_increment(domain)


DEFAULT_CAP = NETLIFY_DEFAULT_CAP

__all__ = [
    "NetlifyDeployer",
    "NetlifyDeployerError",
    "NetlifyDomainPool",
    "validate_netlify_ready",
    "check_domain_nameservers",
    "resolve_nameservers",
    "split_subdomain",
    "slugify_label",
    "DEFAULT_CAP",
]
