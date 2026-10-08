"""Sweep de recuperação de crédito SMS órfão (orphaned activations).

Problema: PipelineSaga persiste um snapshot JSON a cada transição.
Se o processo morre antes de compensate_pending rodar (kill -9, OOM),
um número SMS fica com status == "open" no JSON (nunca foi cancelado no
provedor) e o crédito vaza.

Solução: scan do diretório saga/ no boot, rewrite dos JSONs que têm
resources com status="open" para tentar setStatus=CANCEL retrasado
(best-effort).

USO RECOMENDADO: chamar run_recovery() UMA VEZ no boot do Streamlit
(antes de pedir input), ex. em app.py::

    from core.saga_recovery import run_recovery
    recovery_report = run_recovery()
    if recovery_report.failed_refs:
        st.warning(f"Crédito órfão detectado: {recovery_report.summary()}")
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.api_clients import Sms24hClient
from config import STATE_DIR

logger = logging.getLogger("meta_automation.saga_recovery")


@dataclass
class RecoveryReport:
    """Resultado da varredura de recuperação."""
    scanned: int = 0               # arquivos JSON lidos
    recovered: int = 0             # recursos compensados com sucesso
    failed: int = 0                # set_status retornou False (manual review)
    skipped: int = 0               # arquivos corrompidos / outcome=success
    failed_refs: list[str] = field(default_factory=list)  # refs para ação manual

    def summary(self) -> str:
        """One-liner de resumo para logging/UI."""
        return (
            f"scanned={self.scanned} recovered={self.recovered} "
            f"failed={self.failed} skipped={self.skipped}"
        )


async def recover_orphaned_activations(
    saga_dir: Path,
    *,
    sms_client: Optional[Sms24hClient] = None,
) -> RecoveryReport:
    """Varre saga_dir/saga_*.json e retenta setStatus=CANCEL em abertos.

    Para cada arquivo JSON:
    1. Tolera JSON corrompido (log + skip)
    2. Pula outcome="success" (nada a fazer)
    3. Para cada resource com kind="sms_activation" e status="open":
       - Chama sms_client.set_status(ref, STATUS_CANCEL)
       - Se True: rewrite do JSON com status="compensated"
       - Se False: mantém "open", flags ref para manual recovery

    Idempotente: recursos já settled/compensated/compensation_failed
    são automaticamente pulados (status != "open").

    Args:
        saga_dir: diretório contendo saga_*.json (ex. STATE_DIR / "saga")
        sms_client: cliente SMS injetado (default constrói novo se None)

    Returns:
        RecoveryReport com contagens e refs que precisam ação manual.
    """
    report = RecoveryReport()
    created_client = False

    try:
        if sms_client is None:
            sms_client = Sms24hClient()
            created_client = True

        if not sms_client.enabled:
            logger.warning(
                "saga_recovery: SMS24H_API_KEY não configurada — "
                "recuperação de crédito órfão desabilitada."
            )
            return report

        # Glob dos arquivos de saga
        saga_files = sorted(saga_dir.glob("saga_*.json")) if saga_dir.exists() else []
        if not saga_files:
            logger.info("saga_recovery: nenhum arquivo saga encontrado em %s", saga_dir)
            return report

        logger.info(
            "saga_recovery: iniciando varredura de %d arquivo(s) em %s",
            len(saga_files), saga_dir,
        )

        for saga_file in saga_files:
            report.scanned += 1

            # Parse JSON (tolera corrompidos)
            try:
                data = json.loads(saga_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "saga_recovery: JSON inválido %s (%s) — skipping",
                    saga_file.name, exc,
                )
                report.skipped += 1
                continue

            # Pula runs bem-sucedidas
            if data.get("outcome") == "success":
                logger.debug(
                    "saga_recovery: %s outcome=success — nada a fazer",
                    saga_file.name,
                )
                report.skipped += 1
                continue

            # Procura recursos SMS abertos
            resources = data.get("resources", [])
            modified = False

            for res in resources:
                if (
                    res.get("kind") == "sms_activation"
                    and res.get("status") == "open"
                ):
                    ref = res.get("ref", "?")
                    logger.info(
                        "saga_recovery: tentando recuperar %s/%s",
                        saga_file.name, ref,
                    )

                    # Chama set_status(ref, CANCEL) — nunca levanta
                    success = await sms_client.set_status(ref, Sms24hClient.STATUS_CANCEL)

                    if success:
                        # Rewrite do JSON
                        res["status"] = "compensated"
                        res["outcome"] = "recovered by saga_recovery"
                        logger.info(
                            "saga_recovery: %s/%s compensado com sucesso",
                            saga_file.name, ref,
                        )
                        report.recovered += 1
                        modified = True
                    else:
                        # set_status falhou — deixa open, flags para manual
                        logger.error(
                            "saga_recovery: setStatus falhou para %s/%s — "
                            "recuperação manual necessária",
                            saga_file.name, ref,
                        )
                        report.failed += 1
                        report.failed_refs.append(ref)

            # Persiste mudanças
            if modified:
                try:
                    saga_file.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    logger.info("saga_recovery: %s rewritten com sucesso", saga_file.name)
                except OSError as exc:
                    logger.error(
                        "saga_recovery: falha ao reescrever %s: %s",
                        saga_file.name, exc,
                    )

        logger.info("saga_recovery: varredura completa — %s", report.summary())

    finally:
        if created_client:
            await sms_client.aclose()

    return report


def run_recovery(saga_dir: Optional[Path] = None) -> RecoveryReport:
    """Wrapper síncrono para recover_orphaned_activations.

    Args:
        saga_dir: diretório de sagas (default: STATE_DIR / "saga")

    Returns:
        RecoveryReport com resultado da varredura.
    """
    if saga_dir is None:
        saga_dir = STATE_DIR / "saga"

    return asyncio.run(recover_orphaned_activations(saga_dir))


if __name__ == "__main__":
    # CLI shim: python -m core.saga_recovery
    report = run_recovery()
    print(f"Recovery report: {report.summary()}")
    if report.failed_refs:
        print(f"Refs needing manual action: {report.failed_refs}")
