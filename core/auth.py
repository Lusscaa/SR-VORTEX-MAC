"""Autenticação do painel — login de 1 operador por instância.

Contrato (outras frentes dependem disto):
  * Env: ``PANEL_USERNAME``, ``PANEL_PASSWORD``, ``PANEL_AUTH_SECRET``.
  * Token HS256 (PyJWT) com ``exp`` de 12h, ``sub`` = username.
  * Toda rota ``/api/*`` exige ``Authorization: Bearer <token>``,
    exceto ``/api/login`` e ``/api/health``.

Este módulo é puro (sem FastAPI) — só credenciais e token. O enforcement
HTTP (middleware) fica no ``api_server``.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt  # PyJWT
from dotenv import load_dotenv

# Garante que o .env esteja carregado mesmo se ``config`` ainda não foi
# importado (idempotente; não sobrescreve env já presente no processo).
load_dotenv()

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=12)


class AuthError(Exception):
    """Credencial/token inválido ou auth mal configurada."""


def _env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise AuthError(
            f"{name} não definido no .env — configure as credenciais do painel "
            "(PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET)."
        )
    return val


def verify_credentials(username: str, password: str) -> bool:
    """Compara username/senha com o .env em tempo constante (anti-timing)."""
    try:
        exp_user, exp_pass = _env("PANEL_USERNAME"), _env("PANEL_PASSWORD")
    except AuthError:
        raise
    ok_user = hmac.compare_digest((username or "").encode(), exp_user.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), exp_pass.encode())
    return ok_user and ok_pass


def create_token(username: str) -> tuple[str, int]:
    """Emite um JWT assinado. Retorna (token, expires_in_segundos)."""
    secret = _env("PANEL_AUTH_SECRET")
    now = datetime.now(timezone.utc)
    exp = now + TOKEN_TTL
    payload = {"sub": username, "iat": now, "exp": exp}
    token = jwt.encode(payload, secret, algorithm=ALGORITHM)
    return token, int(TOKEN_TTL.total_seconds())


def decode_token(token: str) -> dict:
    """Valida assinatura + expiração. Levanta ``AuthError`` se inválido."""
    secret = _env("PANEL_AUTH_SECRET")
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError(f"token inválido: {exc}") from exc


def token_from_header(header: Optional[str]) -> Optional[str]:
    """Extrai o token de ``Authorization: Bearer <token>``."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
