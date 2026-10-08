"""Sistema de checkpoints por passo — salva estado pra recovery sem reabrir navegador."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / ".state" / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


def get_checkpoint_file(run_id: str) -> Path:
    """Arquivo de checkpoint da run."""
    return CHECKPOINT_DIR / f"{run_id}.checkpoint.json"


def save_checkpoint(run_id: str, step_name: str, data: dict[str, Any]) -> None:
    """Salva checkpoint de um passo completado com sucesso.

    Exemplo:
        save_checkpoint("run_123", "auth", {"user_id": "abc123"})
    """
    cp_file = get_checkpoint_file(run_id)
    try:
        if cp_file.exists():
            checkpoint = json.loads(cp_file.read_text())
        else:
            checkpoint = {"run_id": run_id, "steps": {}}

        checkpoint["steps"][step_name] = {
            "completed": True,
            "data": data,
        }

        cp_file.write_text(json.dumps(checkpoint, indent=2))
        logger.info(f"Checkpoint salvo: {run_id} → {step_name}")
    except Exception as e:
        logger.error(f"Erro ao salvar checkpoint: {e}")


def get_checkpoint(run_id: str, step_name: str) -> Optional[dict[str, Any]]:
    """Recupera dados de um checkpoint anterior."""
    cp_file = get_checkpoint_file(run_id)
    if not cp_file.exists():
        return None

    try:
        checkpoint = json.loads(cp_file.read_text())
        return checkpoint.get("steps", {}).get(step_name, {}).get("data")
    except Exception as e:
        logger.error(f"Erro ao ler checkpoint: {e}")
        return None


def get_last_completed_step(run_id: str) -> Optional[str]:
    """Retorna o último passo que completou com sucesso."""
    cp_file = get_checkpoint_file(run_id)
    if not cp_file.exists():
        return None

    try:
        checkpoint = json.loads(cp_file.read_text())
        steps = checkpoint.get("steps", {})
        if steps:
            return list(steps.keys())[-1]
    except Exception as e:
        logger.error(f"Erro ao pegar último passo: {e}")

    return None


def clear_checkpoint(run_id: str) -> None:
    """Limpa checkpoint da run (quando finaliza com sucesso)."""
    cp_file = get_checkpoint_file(run_id)
    try:
        if cp_file.exists():
            cp_file.unlink()
        logger.info(f"Checkpoint limpo: {run_id}")
    except Exception as e:
        logger.error(f"Erro ao limpar checkpoint: {e}")


def delete_steps_after(run_id: str, step_name: str) -> None:
    """Deleta checkpoints de passos APÓS um passo específico.

    Exemplo: se falhar no passo 5, volta ao 4 deletando os dados do 5 em diante.
    """
    cp_file = get_checkpoint_file(run_id)
    if not cp_file.exists():
        return

    try:
        checkpoint = json.loads(cp_file.read_text())
        steps = list(checkpoint.get("steps", {}).keys())

        # Acha índice do passo
        try:
            idx = steps.index(step_name)
        except ValueError:
            return

        # Deleta passos após esse
        steps_to_delete = steps[idx+1:]
        for step in steps_to_delete:
            del checkpoint["steps"][step]

        cp_file.write_text(json.dumps(checkpoint, indent=2))
        logger.info(f"Checkpoints deletados após '{step_name}': {steps_to_delete}")
    except Exception as e:
        logger.error(f"Erro ao deletar steps: {e}")
