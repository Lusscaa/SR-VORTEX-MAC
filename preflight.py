#!/usr/bin/env python3
"""Checagem pré-execução do meta_automation.

Reporta em PT-BR, com ✓/✗/⚠, o estado do ambiente ANTES de subir o painel
ou rodar a esteira:

  (a) .env existe e as variáveis OBRIGATÓRIAS estão preenchidas;
  (b) AdsPower responde em ADSPOWER_API_URL (GET simples, timeout curto);
  (c) banco SQLite acessível/gravável (.state/meta_automation.db);
  (d) Playwright instalado (módulo + navegador Chromium).

Sai com código != 0 se algo CRÍTICO faltar (itens marcados [crítico]).
Itens não-críticos viram avisos (⚠) e NÃO derrubam o preflight — o app
sobe degradado (sem AdsPower / sem captcha etc.).

Usa apenas a biblioteca padrão: roda mesmo sem as dependências instaladas
e mesmo sem o AdsPower aberto (reporta ✗ no item, não quebra).
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_DIR = BASE_DIR / ".state"
DB_PATH = STATE_DIR / "meta_automation.db"

# Credenciais do painel: sem elas /api/login levanta erro em core/auth.py.
REQUIRED_VARS = ("PANEL_USERNAME", "PANEL_PASSWORD", "PANEL_AUTH_SECRET")

# Cores ANSI (degradam p/ texto puro quando a saída não é um TTY).
_TTY = sys.stdout.isatty()
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
BOLD = "\033[1m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""

_critical_failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, *, critical: bool = True) -> None:
    mark = f"{RED}✗{RESET}"
    print(f"  {mark} {msg}")
    if critical:
        _critical_failures.append(msg)


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parser .env minimalista (sem dependência de python-dotenv).

    Suporta ``CHAVE=valor``, comentários ``#`` e aspas simples/duplas.
    """
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split(" #", 1)[0].strip()  # comentário inline
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def env_value(file_vars: dict[str, str], name: str) -> str:
    """Valor efetivo: prioriza o env do processo (shell/CI), cai pro .env."""
    return (os.getenv(name) or file_vars.get(name, "")).strip()


# --------------------------------------------------------------------------- #
# (a) .env + obrigatórias                                                      #
# --------------------------------------------------------------------------- #
def check_env() -> dict[str, str]:
    section("[a] Configuração (.env + variáveis obrigatórias)")
    file_vars: dict[str, str] = {}
    if ENV_PATH.exists():
        ok(f".env encontrado em {ENV_PATH}")
        file_vars = parse_env_file(ENV_PATH)
    else:
        fail(
            ".env NÃO encontrado — copie de .env.example: cp .env.example .env  [crítico]"
        )

    for name in REQUIRED_VARS:
        val = env_value(file_vars, name)
        if val and not val.lower().startswith(("troque", "gere-")):
            ok(f"{name} preenchida")
        elif val:
            fail(f"{name} ainda com valor-placeholder — defina um valor real  [crítico]")
        else:
            fail(f"{name} ausente/vazia  [crítico]")
    return file_vars


# --------------------------------------------------------------------------- #
# (b) AdsPower                                                                 #
# --------------------------------------------------------------------------- #
def check_adspower(file_vars: dict[str, str]) -> None:
    section("[b] AdsPower (Local API)")
    base = env_value(file_vars, "ADSPOWER_API_URL") or "http://127.0.0.1:50325"
    url = base.rstrip("/") + "/status"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            code = resp.getcode()
        if code == 200:
            ok(f"AdsPower respondeu em {base} (HTTP {code})")
        else:
            warn(f"AdsPower respondeu HTTP {code} em {base} — verifique se está logado")
    except urllib.error.HTTPError as exc:
        # Respondeu (logo está no ar), só não no /status — suficiente p/ saber que subiu.
        ok(f"AdsPower respondeu em {base} (HTTP {exc.code})")
    except (urllib.error.URLError, OSError) as exc:
        fail(
            f"AdsPower NÃO respondeu em {base} ({exc}). Abra o AdsPower e faça login. "
            "[não-crítico: o painel sobe, mas a esteira não roda]",
            critical=False,
        )


# --------------------------------------------------------------------------- #
# (c) Banco SQLite                                                             #
# --------------------------------------------------------------------------- #
def check_db() -> None:
    section("[c] Banco de dados (SQLite)")
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        try:
            conn.execute("SELECT 1")
            cur = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            )
            n_tables = cur.fetchone()[0]
        finally:
            conn.close()
        if n_tables:
            ok(f"DB acessível em {DB_PATH} ({n_tables} tabelas)")
        else:
            warn(
                f"DB acessível em {DB_PATH}, mas SEM tabelas — rode o init "
                "(veja README_SETUP.md)"
            )
    except (sqlite3.Error, OSError) as exc:
        fail(f"DB inacessível em {DB_PATH} ({exc})  [crítico]")


# --------------------------------------------------------------------------- #
# (d) Playwright                                                               #
# --------------------------------------------------------------------------- #
def check_playwright() -> None:
    section("[d] Playwright")
    if importlib.util.find_spec("playwright") is None:
        fail(
            "módulo playwright NÃO instalado — rode: pip install -r requirements.txt  [crítico]"
        )
        return
    ok("módulo playwright instalado")

    # Navegador Chromium: procura no cache padrão do Playwright.
    cache_root = Path(
        os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
        or (Path.home() / "Library" / "Caches" / "ms-playwright")
    )
    # Em Linux o cache fica em ~/.cache/ms-playwright; tenta ambos.
    candidates = [cache_root, Path.home() / ".cache" / "ms-playwright"]
    found = any(
        c.exists() and any(c.glob("chromium-*")) for c in candidates if c
    )
    if found:
        ok("navegador Chromium do Playwright encontrado")
    else:
        warn(
            "Chromium do Playwright não encontrado — rode: playwright install chromium "
            "[não-crítico aqui, mas a esteira falha sem ele]"
        )


# --------------------------------------------------------------------------- #
def main() -> int:
    print(f"{BOLD}== Preflight meta_automation =={RESET}")
    file_vars = check_env()
    check_adspower(file_vars)
    check_db()
    check_playwright()

    section("Resumo")
    if _critical_failures:
        print(f"  {RED}{BOLD}{len(_critical_failures)} item(ns) crítico(s) faltando:{RESET}")
        for msg in _critical_failures:
            print(f"    {RED}•{RESET} {msg}")
        print(f"\n  {RED}Preflight FALHOU — corrija o(s) item(ns) acima antes de subir.{RESET}")
        return 1
    print(f"  {GREEN}{BOLD}Tudo certo nos itens críticos.{RESET} (Avisos ⚠ não bloqueiam.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
