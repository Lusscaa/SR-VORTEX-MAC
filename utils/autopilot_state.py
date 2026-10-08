"""Estado do piloto automático (ciclos sequenciais de runs simultâneas).

RUN = quantidade de execuções simultâneas por ciclo.
PILOTO AUTOMÁTICO = quantidade de ciclos sequenciais.

Armazenado em .state/autopilot.json para persistência entre restarts."""
import json
import logging
from pathlib import Path
from config import STATE_DIR

logger = logging.getLogger("meta_automation.autopilot_state")

AUTOPILOT_PATH = STATE_DIR / "autopilot.json"

_DEFAULTS: dict = {
    "enabled": False,
    "runs_per_cycle": 1,
    "total_cycles": 3,
    "current_cycle": 0,
    "use_proxy": True,
    "proxy_type": "datacenter",
    "sms_option": None,
    "sms_ddd": None,
    "smsrush_server": None,
    "sms_fallback_option": None,
    "dual_bm": False,
    "browser_provider": "adspower",
    "waba_enabled": False,
}


def load_autopilot() -> dict:
    """Lê o estado atual."""
    if AUTOPILOT_PATH.exists():
        try:
            data = json.loads(AUTOPILOT_PATH.read_text())
            if "target" in data:
                if "total_cycles" not in data:
                    data["total_cycles"] = data["target"]
                del data["target"]
            if "runs_per_cycle" not in data:
                data["runs_per_cycle"] = 1
            if "current_cycle" not in data:
                data["current_cycle"] = 0
            return {**_DEFAULTS, **data}
        except Exception as e:
            logger.warning(f"Erro ao ler autopilot.json: {e}")
    return dict(_DEFAULTS)


def save_autopilot(
    enabled: bool,
    runs_per_cycle: int = 1,
    total_cycles: int = 3,
    current_cycle: int = 0,
    use_proxy: bool = True,
    proxy_type: str = "datacenter",
    sms_option: str | None = None,
    sms_ddd: str | None = None,
    smsrush_server: int | None = None,
    sms_fallback_option: str | None = None,
    dual_bm: bool = False,
    browser_provider: str = "adspower",
    waba_enabled: bool = False,
) -> None:
    """Salva o estado do piloto automático."""
    STATE_DIR.mkdir(exist_ok=True)
    state = {
        "enabled": enabled,
        "runs_per_cycle": max(1, int(runs_per_cycle)),
        "total_cycles": max(1, int(total_cycles)),
        "current_cycle": int(current_cycle),
        "use_proxy": use_proxy,
        "proxy_type": proxy_type,
        "sms_option": sms_option,
        "sms_ddd": sms_ddd,
        "smsrush_server": smsrush_server,
        "sms_fallback_option": sms_fallback_option,
        "dual_bm": dual_bm,
        "browser_provider": browser_provider,
        "waba_enabled": waba_enabled,
    }
    AUTOPILOT_PATH.write_text(json.dumps(state, indent=2))
    logger.info(
        "Autopilot: enabled=%s, runs_per_cycle=%d, total_cycles=%d, current_cycle=%d",
        enabled, state["runs_per_cycle"], state["total_cycles"], state["current_cycle"],
    )


def advance_cycle() -> dict:
    """Incrementa current_cycle. Desliga se todos os ciclos foram concluídos.
    Retorna o estado atualizado."""
    ap = load_autopilot()
    ap["current_cycle"] = ap.get("current_cycle", 0) + 1
    if ap["current_cycle"] > ap.get("total_cycles", 1):
        ap["enabled"] = False
        logger.info(
            "Piloto Automático: todos os %d ciclos concluídos — DESLIGANDO.",
            ap.get("total_cycles", 1),
        )
    STATE_DIR.mkdir(exist_ok=True)
    AUTOPILOT_PATH.write_text(json.dumps(ap, indent=2))
    return ap
