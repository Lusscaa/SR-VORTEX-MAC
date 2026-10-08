"""Recon READ-ONLY do contexto de login num perfil AdsPower.

Diagnóstico para login que não progride (login_form → login_form sem erro):
coleta IP/geo de egresso, fingerprint de locale/timezone, cookies do
Facebook (sessão viva? datr presente?) e o estado real da página de login.
NÃO digita credenciais nem submete — zero tentativas de login gastas.

Uso:
    .venv/bin/python tools/recon_login.py [user_id]   # default: k1de8lfb

Artefatos em .state/recon/: recon_<ts>.html / .png / .json
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from core.adspower_client import AdsPowerClient  # noqa: E402

USER_ID = sys.argv[1] if len(sys.argv) > 1 else "k1de8lfb"
LOGIN_URL = "https://www.facebook.com/login.php?locale=pt_BR"
OUT_DIR = Path(__file__).resolve().parents[1] / ".state" / "recon"

JS_PROBE = """() => ({
    language: navigator.language,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    webdriver: navigator.webdriver,
    platform: navigator.platform,
    userAgent: navigator.userAgent,
})"""


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    report: dict = {"user_id": USER_ID, "ts": ts}

    client = AdsPowerClient()

    # 1. Config do perfil no painel (proxy declarado, país, etc.)
    try:
        info = await client.get_profile_info(USER_ID)
        proxy_cfg = info.get("user_proxy_config") or info.get("proxy_config") or {}
        report["profile_info"] = {
            "name": info.get("name"),
            "group_id": info.get("group_id"),
            "proxy_type": proxy_cfg.get("proxy_type"),
            "proxy_host": proxy_cfg.get("proxy_host"),
            "country": info.get("country") or info.get("ip_country"),
            "last_open_time": info.get("last_open_time"),
        }
    except Exception as exc:  # noqa: BLE001
        report["profile_info_error"] = str(exc)

    # 2. Abre o perfil e conecta via CDP
    started = await client.start(USER_ID)
    ws = started["ws_puppeteer"]
    report["cdp"] = ws

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # 3. Cookies do Facebook — nomes apenas (sem valores). c_user presente
        #    = sessão autenticada viva; datr ausente = browser "novo" para o FB.
        cookies = await context.cookies(["https://www.facebook.com", "https://web.facebook.com"])
        report["fb_cookies"] = sorted({c["name"] for c in cookies})
        report["session_alive_c_user"] = any(c["name"] == "c_user" for c in cookies)
        report["has_datr"] = any(c["name"] == "datr" for c in cookies)

        page = await context.new_page()

        # 4. Fingerprint de ambiente
        await page.goto("about:blank")
        report["js_env"] = await page.evaluate(JS_PROBE)

        # 5. IP/geo de egresso (passa pelo proxy do perfil)
        try:
            await page.goto("https://ipinfo.io/json", timeout=30000)
            body = await page.locator("body").inner_text()
            report["egress"] = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            report["egress_error"] = str(exc)
            try:
                await page.goto("https://api.ipify.org?format=json", timeout=20000)
                report["egress"] = json.loads(await page.locator("body").inner_text())
            except Exception as exc2:  # noqa: BLE001
                report["egress_fallback_error"] = str(exc2)

        # 6. Página de login — estado real, sem interagir
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)  # JS/redirects do FB assentarem
        html = await page.content()
        report["login_page"] = {
            "final_url": page.url,
            "title": await page.title(),
            "html_lang": await page.evaluate("document.documentElement.lang || null"),
            "canonical": (
                m.group(1) if (m := re.search(r'rel="canonical" href="([^"]+)"', html)) else None
            ),
            "has_email_field": await page.locator('input[name="email"]').count() > 0,
            "has_pass_field": await page.locator('input[name="pass"]').count() > 0,
            "form_action": (
                m.group(1) if (m := re.search(r'<form[^>]+action="([^"]+)"', html)) else None
            ),
            "captcha_iframes": [
                src for src in await page.locator("iframe").evaluate_all(
                    "els => els.map(e => e.src)"
                ) if src
            ],
        }
        # textos visíveis curtos no topo do form (erro renderizado?)
        try:
            report["login_page"]["visible_text_head"] = (
                await page.locator("body").inner_text()
            )[:600]
        except Exception:  # noqa: BLE001
            pass

        (OUT_DIR / f"recon_{ts}.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=OUT_DIR / f"recon_{ts}.png", full_page=False)
        await page.close()

    (OUT_DIR / f"recon_{ts}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
