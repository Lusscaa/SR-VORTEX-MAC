"""PipelineSaga — compensação e auditoria de falhas do pipeline (PR4).

Problema que resolve: o pipeline adquire recursos com custo financeiro
(ex.: número SMS alugado no sms24h — pré-alocado pela UI ou adquirido na
etapa 4/4). Se a esteira falhar em QUALQUER ponto antes do encerramento
da transação, o crédito fica preso no provedor. A saga:

1. RASTREIA recursos adquiridos (``track_resource``) com sua função de
   compensação (ex.: ``setStatus=8`` → cancela e estorna).
2. LIQUIDA recursos encerrados pelo fluxo normal (``settle_resource``) —
   a compensação então NÃO roda (idempotência; sem double-cancel).
3. COMPENSA na falha (``compensate_pending``): roda as compensações dos
   recursos pendentes em ordem reversa de aquisição, best-effort — uma
   compensação que falha não impede as demais nem mascara o erro raiz.
4. PERSISTE cada transição em ``.state/saga/<run_id>.json`` (crash-safe:
   reescrito a cada mudança) — trilha de auditoria para recuperação
   manual de crédito quando até a compensação falhar.

A saga NÃO substitui o ``step()`` de logging (H1); ela registra a camada
transacional: passos executados, recursos abertos, desfecho.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("meta_automation.saga")

Compensation = Callable[[], Awaitable[None]]


class StepStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ResourceStatus(str, Enum):
    OPEN = "open"                # adquirido; transação em aberto no provedor
    SETTLED = "settled"          # encerrado pelo fluxo normal (finish/cancel)
    COMPENSATED = "compensated"  # encerrado pela saga após falha
    COMPENSATION_FAILED = "compensation_failed"  # requer ação MANUAL


@dataclass
class TrackedResource:
    kind: str
    ref: str
    detail: str = ""
    status: ResourceStatus = ResourceStatus.OPEN
    outcome: str = ""
    compensate: Optional[Compensation] = None


@dataclass
class SagaStep:
    name: str
    status: StepStatus = StepStatus.RUNNING
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


class PipelineSaga:
    """Registro transacional de uma execução do pipeline."""

    def __init__(self, state_dir: Path, run_id: Optional[str] = None) -> None:
        # Sufixo aleatório: runs paralelos no mesmo segundo NÃO podem
        # sobrescrever a trilha de auditoria um do outro.
        self.run_id = run_id or (
            f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        self.state_dir = state_dir
        self.steps: list[SagaStep] = []
        self.resources: list[TrackedResource] = []
        # Artefatos produzidos no run (ex.: PDF do comprovante CNPJ). Chave
        # estável → caminho absoluto. O passo de upload LÊ daqui (nunca faz
        # glob/adivinha o arquivo), o que torna a esteira segura para vários
        # robôs rodando em paralelo (cada run_id tem seu próprio diretório).
        self.artifacts: dict[str, str] = {}
        self.outcome: str = "running"
        self.error: str = ""
        self._persist()

    # ------------------------------------------------------------------
    # Passos
    # ------------------------------------------------------------------
    def step_started(self, name: str) -> SagaStep:
        entry = SagaStep(name=name)
        self.steps.append(entry)
        self._persist()
        return entry

    def step_finished(self, entry: SagaStep, error: Optional[BaseException] = None) -> None:
        entry.finished_at = time.time()
        if error is None:
            entry.status = StepStatus.DONE
        else:
            entry.status = StepStatus.FAILED
            entry.error = f"{type(error).__name__}: {error}"
        self._persist()

    # ------------------------------------------------------------------
    # Recursos compensáveis
    # ------------------------------------------------------------------
    def track_resource(
        self,
        kind: str,
        ref: str,
        *,
        detail: str = "",
        compensate: Optional[Compensation] = None,
    ) -> None:
        """Registra recurso com transação aberta no provedor externo."""
        self.resources.append(
            TrackedResource(kind=kind, ref=ref, detail=detail, compensate=compensate)
        )
        logger.info("saga: recurso rastreado kind=%s ref=%s (%s)", kind, ref, detail)
        self._persist()

    def settle_resource(self, kind: str, ref: str, outcome: str) -> None:
        """Marca recurso como encerrado pelo fluxo normal — não compensar."""
        for res in self.resources:
            if res.kind == kind and res.ref == ref and res.status is ResourceStatus.OPEN:
                res.status = ResourceStatus.SETTLED
                res.outcome = outcome
                logger.info(
                    "saga: recurso liquidado kind=%s ref=%s outcome=%s",
                    kind, ref, outcome,
                )
                self._persist()
                return
        logger.debug("saga: settle de recurso não rastreado/já liquidado: %s/%s", kind, ref)

    @property
    def open_resources(self) -> list[TrackedResource]:
        return [r for r in self.resources if r.status is ResourceStatus.OPEN]

    async def compensate_pending(self, error: BaseException) -> None:
        """Roda compensações dos recursos ainda abertos (ordem reversa).

        Best-effort: NUNCA levanta — o erro raiz do pipeline é o que deve
        propagar. Compensação que falha vira ``compensation_failed`` no
        JSON, sinalizando recuperação manual de crédito.
        """
        self.outcome = "failed"
        self.error = f"{type(error).__name__}: {error}"
        pending = self.open_resources
        if not pending:
            self._persist()
            return
        logger.warning(
            "saga: pipeline falhou com %d recurso(s) em aberto — compensando.",
            len(pending),
        )
        for res in reversed(pending):
            if res.compensate is None:
                res.status = ResourceStatus.COMPENSATION_FAILED
                res.outcome = "sem função de compensação"
                continue
            try:
                await res.compensate()
                res.status = ResourceStatus.COMPENSATED
                res.outcome = "compensado pela saga"
                logger.info(
                    "saga: recurso compensado kind=%s ref=%s", res.kind, res.ref
                )
            except Exception as exc:  # noqa: BLE001
                res.status = ResourceStatus.COMPENSATION_FAILED
                res.outcome = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "saga: COMPENSAÇÃO FALHOU kind=%s ref=%s (%s) — "
                    "recuperação manual necessária; ver %s",
                    res.kind, res.ref, exc, self.state_path,
                )
        self._persist()

    def finish(self, success: bool) -> None:
        self.outcome = "success" if success else "failed"
        self._persist()

    # ------------------------------------------------------------------
    # Artefatos do run
    # ------------------------------------------------------------------
    def record_artifact(self, key: str, path: str) -> None:
        """Registra um artefato (caminho absoluto) produzido neste run.

        Persistido na trilha de auditoria; lido depois por chave estável
        (ex.: ``artifacts['cnpj_card_path']``) — nunca por glob/adivinhação.
        """
        self.artifacts[key] = path
        logger.info("saga: artefato registrado %s=%s", key, path)
        self._persist()

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.state_dir / f"saga_{self.run_id}.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "error": self.error,
            "steps": [
                {
                    "name": s.name,
                    "status": s.status.value,
                    "error": s.error,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                }
                for s in self.steps
            ],
            "resources": [
                {
                    "kind": r.kind,
                    "ref": r.ref,
                    "detail": r.detail,
                    "status": r.status.value,
                    "outcome": r.outcome,
                }
                for r in self.resources
            ],
            "artifacts": dict(self.artifacts),
        }

    def _persist(self) -> None:
        """Reescreve o snapshot a cada transição. Best-effort: I/O de
        auditoria jamais derruba o pipeline."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            try:
                logger.warning(
                    "saga: falha ao persistir estado: %s: %s",
                    type(exc).__name__, exc,
                )
            except Exception:  # noqa: BLE001 — nem o log pode derrubar
                pass
