"""Persistência local de perfis AdsPower indexada por CNPJ.

Schema em ``.state/adspower_profiles.json``::

    {
        "12345678000100": {
            "user_id": "k1198abc",
            "name": "joaodasilva_12345678000100",
            "created_at": 1717891200,
            "last_used_at": 1717891200,
            "status": "active"
        }
    }

Idempotência: ao criar um perfil novo, o caller PRIMEIRO chama
``find_profile_by_cnpj``; se já existe, reusa o ``user_id`` antigo. Isso
impede duplicação de slots no plano AdsPower (que tem limite).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from config import STATE_DIR
from utils.helpers import only_digits

logger = logging.getLogger("meta_automation.profiles_state")

PROFILES_PATH = STATE_DIR / "adspower_profiles.json"
IXBROWSER_PROFILES_PATH = STATE_DIR / "ixbrowser_profiles.json"


def _path_for_provider(provider: str | None = None) -> Path:
    if provider == "ixbrowser":
        return IXBROWSER_PROFILES_PATH
    return PROFILES_PATH

# Lock de processo: com 2–3 workers paralelos criando perfis ao mesmo tempo, o
# read-modify-write do JSON corria (last-write-wins apagava o registro do
# outro). RLock serializa os ciclos compostos (load→muta→save).
_LOCK = threading.RLock()


def _load_raw(provider: str | None = None) -> dict[str, Any]:
    p = _path_for_provider(provider)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("profiles_state: arquivo corrompido (%s) — começando vazio.", exc)
        return {}


def _save_raw(data: dict[str, Any], provider: str | None = None) -> None:
    p = _path_for_provider(provider)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def find_profile_by_cnpj(cnpj: str, *, provider: str | None = None) -> Optional[dict[str, Any]]:
    """Retorna o registro do perfil já criado para esse CNPJ, ou None."""
    key = only_digits(cnpj or "")
    if len(key) != 14:
        return None
    return _load_raw(provider).get(key)


def save_profile(
    cnpj: str,
    *,
    user_id: str,
    name: str,
    status: str = "active",
    email: Optional[str] = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Persiste/atualiza o registro do perfil. Retorna o registro salvo.

    ``email`` é a conta do Facebook dona da sessão deste perfil — usado para
    detectar troca de conta no mesmo CNPJ (ver :func:`delete_profile`)."""
    key = only_digits(cnpj or "")
    if len(key) != 14:
        raise ValueError(f"CNPJ inválido para persistência: {cnpj!r}")
    with _LOCK:
        data = _load_raw(provider)
        existing = data.get(key, {})
        now = int(time.time())
        record = {
            **existing,
            "user_id": user_id,
            "name": name,
            "status": status,
            "email": (email or "").strip().lower() or existing.get("email"),
            "created_at": existing.get("created_at", now),
            "last_used_at": now,
        }
        data[key] = record
        _save_raw(data, provider)
        return record


def delete_profile(cnpj: str, *, provider: str | None = None) -> Optional[dict[str, Any]]:
    """Remove o registro local do perfil (ex.: a conta mudou para esse CNPJ).

    Retorna o registro removido (ou None). NÃO apaga o perfil no multi-login — só
    o mapping local; o caller loga o ``user_id`` órfão para limpeza manual no
    painel."""
    key = only_digits(cnpj or "")
    with _LOCK:
        data = _load_raw(provider)
        rec = data.pop(key, None)
        if rec is not None:
            _save_raw(data, provider)
        return rec


def set_profile_flag(cnpj: str, flag: str, value: Any, *, provider: str | None = None) -> None:
    """Grava um flag arbitrário no registro do perfil (ex.: locale_ok).

    No-op se o perfil ainda não existe — flags são metadados de conveniência
    (caches de 'já fiz isso neste perfil'), nunca podem criar registro órfão.
    """
    key = only_digits(cnpj or "")
    with _LOCK:
        data = _load_raw(provider)
        if key in data:
            data[key][flag] = value
            _save_raw(data, provider)


def touch_profile(cnpj: str, *, provider: str | None = None) -> None:
    """Atualiza ``last_used_at`` sem alterar outros campos."""
    key = only_digits(cnpj or "")
    with _LOCK:
        data = _load_raw(provider)
        if key in data:
            data[key]["last_used_at"] = int(time.time())
            _save_raw(data, provider)


def list_profiles(*, provider: str | None = None) -> dict[str, dict[str, Any]]:
    """Retorna todo o mapa CNPJ → registro."""
    return _load_raw(provider)
