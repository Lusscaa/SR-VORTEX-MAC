#!/usr/bin/env python3
"""telemetry_reporter.py — sidecar standalone de telemetria da instância.

Lê o banco SQLite do app em modo SOMENTE-LEITURA e empurra eventos (heartbeat,
run_upsert, run_log, pool_snapshot) para um coletor central via HTTP POST. NÃO
importa/edita o app principal (api_server / store / job_manager) — só observa o
DB e os logs em ``.state/``. Toda a lógica vive em :mod:`core.telemetry`.

Uso::

    COLLECTOR_URL=https://coletor.exemplo/  \\
    INSTANCE_ID=op-01 INSTANCE_NAME="Sócio 1" INSTANCE_TOKEN=segredo \\
    python telemetry_reporter.py            # loop infinito (default 15s)

    python telemetry_reporter.py --once     # um ciclo só (debug/teste)

Env:
    COLLECTOR_URL, INSTANCE_ID, INSTANCE_NAME, INSTANCE_TOKEN  (obrigatórios,
        exceto INSTANCE_NAME), TELEMETRY_INTERVAL_S (default 15),
        TELEMETRY_VERSION, TELEMETRY_RUNS_LIMIT (default 500).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from core.telemetry import Reporter, TelemetryConfig

logger = logging.getLogger("meta_automation.telemetry_reporter")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sidecar de telemetria (push).")
    parser.add_argument("--once", action="store_true",
                        help="executa um único ciclo e sai (debug/teste)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="log em nível DEBUG")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = TelemetryConfig.from_env()
    missing = cfg.missing()
    if missing:
        logger.error("variáveis de ambiente ausentes: %s", ", ".join(missing))
        return 2

    reporter = Reporter(cfg)
    logger.info(
        "reporter iniciado | coletor=%s instancia=%s (%s) intervalo=%ss versao=%s",
        cfg.ingest_url, cfg.instance_id, cfg.instance_name or "-",
        cfg.interval_s, cfg.version,
    )

    while True:
        try:
            res = reporter.run_cycle()
            logger.info("ciclo: %d coletados, %d enviados, %d pendentes (outbox)",
                        res.collected, res.sent, res.pending)
        except Exception:  # noqa: BLE001 - um ciclo ruim não derruba o loop
            logger.exception("erro inesperado no ciclo")
        if args.once:
            return 0
        try:
            time.sleep(cfg.interval_s)
        except KeyboardInterrupt:
            logger.info("interrompido — encerrando")
            return 0


if __name__ == "__main__":
    sys.exit(main())
