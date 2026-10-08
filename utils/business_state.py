"""Persistência local de portfólios empresariais (BMs) indexada por CNPJ.

Schema em ``.state/business_portfolios.json``::

    {
        "12345678000100": {
            "business_id": "3882927188515522",
            "portfolio_name": "Empresa Exemplo LTDA",
            "source": "created",
            "created_at": 1717891200,
            "last_used_at": 1717891200
        }
    }

``source`` distingue como o mapeamento foi obtido:
  * ``"created"``  — o robô criou o portfólio neste run.
  * ``"detected"`` — a conta já tinha portfólio (URL da Suite com
    ``business_id=``) e o robô apenas capturou o ID.

Idempotência: espelha o padrão de ``utils/profiles_state.py`` (AdsPower).
Antes de criar um portfólio, o runner detecta se a conta já tem um; se sim,
captura o ``business_id`` e persiste aqui — o registro serve de inventário
das BMs provisionadas e destrava re-runs de contas que já passaram da etapa 1.
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

logger = logging.getLogger("meta_automation.business_state")

BUSINESSES_PATH = STATE_DIR / "business_portfolios.json"

# Lock de processo: serializa o read-modify-write do JSON sob workers paralelos
# (mesma razão de utils/profiles_state.py).
_LOCK = threading.RLock()


def _load_raw() -> dict[str, Any]:
    if not BUSINESSES_PATH.exists():
        return {}
    try:
        return json.loads(BUSINESSES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("business_state: arquivo corrompido (%s) — começando vazio.", exc)
        return {}


def _save_raw(data: dict[str, Any]) -> None:
    BUSINESSES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUSINESSES_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def find_business_by_cnpj(cnpj: str) -> Optional[dict[str, Any]]:
    """Retorna o registro da BM já mapeada para esse CNPJ, ou None."""
    key = only_digits(cnpj or "")
    if len(key) != 14:
        return None
    return _load_raw().get(key)


def save_business(
    cnpj: str,
    *,
    business_id: str,
    portfolio_name: str,
    source: str = "created",
) -> dict[str, Any]:
    """Persiste/atualiza o mapeamento CNPJ → business_id. Retorna o registro."""
    key = only_digits(cnpj or "")
    if len(key) != 14:
        raise ValueError(f"CNPJ inválido para persistência: {cnpj!r}")
    with _LOCK:
        data = _load_raw()
        existing = data.get(key, {})
        now = int(time.time())
        record = {
            "business_id": business_id,
            "portfolio_name": portfolio_name,
            "source": source,
            "created_at": existing.get("created_at", now),
            "last_used_at": now,
        }
        data[key] = record
        _save_raw(data)
        return record


def touch_business(cnpj: str) -> None:
    """Atualiza ``last_used_at`` sem alterar outros campos."""
    key = only_digits(cnpj or "")
    with _LOCK:
        data = _load_raw()
        if key in data:
            data[key]["last_used_at"] = int(time.time())
            _save_raw(data)


def list_businesses() -> dict[str, dict[str, Any]]:
    """Retorna todo o mapa CNPJ → registro."""
    return _load_raw()
