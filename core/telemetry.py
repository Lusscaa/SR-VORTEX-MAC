"""core/telemetry.py — lógica do reporter de telemetria (sidecar da instância).

Por que um SIDECAR (e não um endpoint no app principal):
  A máquina de cada operador roda atrás de NAT (sem IP público) e usa AdsPower
  LOCAL — não dá para o admin central fazer *pull*. O modelo é PUSH (saída):
  um processo separado lê o banco SQLite do app em modo SOMENTE-LEITURA e
  empurra eventos para um coletor central. Sendo um processo à parte, ele NÃO
  trava o app (FastAPI continua escrevendo no WAL) e pode cair/subir sem afetar
  a esteira de provisionamento.

Contrato de telemetria (espelhado pelo coletor / Frente 3):
  POST ``${COLLECTOR_URL}/api/ingest`` com header ``X-Instance-Token`` e JSON::

      {"instance_id", "instance_name", "event_type", "ts": <unix>, "payload": {...}}

  event_type / payload:
    * ``heartbeat``     → {counts:{cnpjs,fb_accounts,proxies,runs}, version}
    * ``run_upsert``    → a linha COMPLETA do run (todos os campos reais)
    * ``run_log``       → {run_id, lines:[{ts,level,msg}]}  (lido incremental)
    * ``pool_snapshot`` → {cnpjs:[...], fb_accounts:[...COM email/senha/2fa...],
                           proxies:[...]}  (o admin VÊ as credenciais)

Idempotência: o coletor faz upsert por (instance_id, run_id). Aqui empurramos
só DELTAS — runs cujo ``updated_at`` mudou desde o último ciclo, e linhas de
log a partir do último offset lido. O cursor (``telemetry_state.json``) só
avança DEPOIS que os eventos estão duráveis no outbox, então uma queda do
processo no meio do ciclo no máximo re-empurra (o coletor deduplica), nunca
perde.

Resiliência: se o POST falhar, o evento fica na outbox
(``.state/telemetry_outbox.json``) e é re-tentado no próximo ciclo. A conexão
SQLite é aberta com ``mode=ro`` (somente leitura) — sob WAL, leitores não
bloqueiam o escritor do app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("meta_automation.telemetry")

# Raiz do projeto: core/telemetry.py → parents[1] == diretório do app.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / ".state"
DB_PATH = STATE_DIR / "meta_automation.db"
RUNS_DIR = STATE_DIR / "runs"
OUTBOX_PATH = STATE_DIR / "telemetry_outbox.json"
CURSOR_PATH = STATE_DIR / "telemetry_state.json"

# Carrega o .env do projeto (mesmas credenciais do app: COLLECTOR_URL etc.).
# Silencioso quando python-dotenv não está instalado / não há .env.
try:  # pragma: no cover - dependência opcional
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass

# Tabelas de pool enviadas no pool_snapshot (chaves do payload == nomes reais).
POOL_TABLES = ("cnpjs", "fb_accounts", "proxies")
# Tabelas contadas no heartbeat.
COUNT_TABLES = ("cnpjs", "fb_accounts", "proxies", "runs")

# Formato do run.log (core/job_manager: "%(asctime)s %(levelname)s %(message)s"
# com datefmt "%H:%M:%S"). Ex.: "19:10:57 INFO subindo navegador".
_LOG_LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(\w+)\s+(.*)$")

# Limites defensivos.
MAX_LOG_LINES_PER_EVENT = 200    # fatia run_log em lotes (payload não explode)
MAX_OUTBOX_EVENTS = 5000         # backstop se o coletor ficar offline p/ sempre


# ---------------------------------------------------------------------------
# Configuração (vinda do ambiente)
# ---------------------------------------------------------------------------
@dataclass
class TelemetryConfig:
    collector_url: str
    instance_id: str
    instance_name: str
    instance_token: str
    interval_s: float = 15.0
    version: str = "unknown"
    runs_limit: int = 500

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        return cls(
            collector_url=os.getenv("COLLECTOR_URL", "").rstrip("/"),
            instance_id=os.getenv("INSTANCE_ID", ""),
            instance_name=os.getenv("INSTANCE_NAME", ""),
            instance_token=os.getenv("INSTANCE_TOKEN", ""),
            interval_s=float(os.getenv("TELEMETRY_INTERVAL_S", "15")),
            version=os.getenv("TELEMETRY_VERSION") or _detect_version(),
            runs_limit=int(os.getenv("TELEMETRY_RUNS_LIMIT", "500")),
        )

    def missing(self) -> list[str]:
        """Variáveis obrigatórias ausentes (instance_name é opcional)."""
        required = (
            ("COLLECTOR_URL", self.collector_url),
            ("INSTANCE_ID", self.instance_id),
            ("INSTANCE_TOKEN", self.instance_token),
        )
        return [name for name, value in required if not value]

    @property
    def ingest_url(self) -> str:
        return f"{self.collector_url}/api/ingest"


def _detect_version() -> str:
    """Versão = hash curto do git (se disponível), senão 'unknown'."""
    try:  # pragma: no cover - depende do ambiente
        import subprocess

        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Leitura SOMENTE-LEITURA do banco do app (não trava o escritor sob WAL)
# ---------------------------------------------------------------------------
def _ro_conn() -> sqlite3.Connection:
    """Conexão read-only via URI (``mode=ro``). Levanta se o DB não existir."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def fetch_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in COUNT_TABLES:
        if _table_exists(conn, t):
            counts[t] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"])
        else:
            counts[t] = 0
    return counts


def fetch_runs(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Runs mais recentemente atualizados primeiro (linha completa)."""
    if not _table_exists(conn, "runs"):
        return []
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_pool(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Snapshot completo dos pools (com credenciais — o admin precisa vê-las)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for t in POOL_TABLES:
        if _table_exists(conn, t):
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            out[t] = [dict(r) for r in rows]
        else:
            out[t] = []
    return out


def parse_log_line(line: str) -> dict[str, Any]:
    m = _LOG_LINE_RE.match(line)
    if m:
        return {"ts": m.group(1), "level": m.group(2), "msg": m.group(3)}
    return {"ts": None, "level": "RAW", "msg": line}


def _hash_pool(pool: dict[str, Any]) -> str:
    blob = json.dumps(pool, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cursor durável (deltas) — telemetry_state.json
# ---------------------------------------------------------------------------
@dataclass
class Cursor:
    runs: dict[str, int] = field(default_factory=dict)        # run_id -> updated_at
    log_offsets: dict[str, int] = field(default_factory=dict)  # run_id -> byte offset
    pool_hash: Optional[str] = None

    @classmethod
    def load(cls) -> "Cursor":
        try:
            data = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
            return cls(
                runs={str(k): int(v) for k, v in (data.get("runs") or {}).items()},
                log_offsets={str(k): int(v) for k, v in (data.get("log_offsets") or {}).items()},
                pool_hash=data.get("pool_hash"),
            )
        except Exception:  # noqa: BLE001 - arquivo ausente/corrompido = começa do zero
            return cls()

    def save(self) -> None:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "runs": self.runs,
                "log_offsets": self.log_offsets,
                "pool_hash": self.pool_hash,
            }),
            encoding="utf-8",
        )
        tmp.replace(CURSOR_PATH)  # atômico


# ---------------------------------------------------------------------------
# Outbox de retry — telemetry_outbox.json
# ---------------------------------------------------------------------------
class Outbox:
    """Fila persistente de eventos pendentes de envio (FIFO, ordem preservada)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        # Resolve no momento da chamada (não como default de função) — assim o
        # caminho acompanha OUTBOX_PATH do módulo, o que permite testar com um
        # diretório temporário e mantém um único ponto de verdade.
        self.path = path if path is not None else OUTBOX_PATH
        self.events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.events = data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            self.events = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.events), encoding="utf-8")
        tmp.replace(self.path)  # atômico

    def extend(self, evs: list[dict[str, Any]]) -> None:
        self.events.extend(evs)
        if len(self.events) > MAX_OUTBOX_EVENTS:
            drop = len(self.events) - MAX_OUTBOX_EVENTS
            logger.warning("outbox no teto (%d) — descartando %d eventos mais antigos",
                           MAX_OUTBOX_EVENTS, drop)
            self.events = self.events[drop:]

    def __len__(self) -> int:
        return len(self.events)


# ---------------------------------------------------------------------------
# Cliente HTTP do coletor
# ---------------------------------------------------------------------------
class CollectorClient:
    def __init__(self, cfg: TelemetryConfig, timeout: float = 10.0) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def send(self, event: dict[str, Any]) -> bool:
        """POST de UM evento. True se 2xx; False em erro de rede/HTTP."""
        import requests  # import tardio: dependência só do sidecar

        try:
            resp = requests.post(
                self.cfg.ingest_url,
                json=event,
                headers={"X-Instance-Token": self.cfg.instance_token},
                timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001 - rede caiu, coletor offline etc.
            logger.debug("falha de rede no POST (%s): %s", event.get("event_type"), e)
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("coletor respondeu HTTP %s para %s",
                       resp.status_code, event.get("event_type"))
        return False


# ---------------------------------------------------------------------------
# Reporter — orquestra coleta de deltas, outbox e drenagem
# ---------------------------------------------------------------------------
def make_event(cfg: TelemetryConfig, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": cfg.instance_id,
        "instance_name": cfg.instance_name,
        "event_type": event_type,
        "ts": int(time.time()),
        "payload": payload,
    }


@dataclass
class CycleResult:
    collected: int
    sent: int
    pending: int


class Reporter:
    def __init__(self, cfg: TelemetryConfig, client: Optional[CollectorClient] = None) -> None:
        self.cfg = cfg
        self.client = client or CollectorClient(cfg)
        self.cursor = Cursor.load()
        self.outbox = Outbox()

    # -- coleta de deltas ---------------------------------------------------
    def collect_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            conn = _ro_conn()
        except sqlite3.OperationalError as e:
            logger.warning("DB indisponível (%s) — ciclo sem coleta", e)
            return events
        try:
            counts = fetch_counts(conn)
            runs = fetch_runs(conn, self.cfg.runs_limit)
            pool = fetch_pool(conn)
        finally:
            conn.close()

        # heartbeat (todo ciclo)
        events.append(make_event(self.cfg, "heartbeat",
                                  {"counts": counts, "version": self.cfg.version}))

        # run_upsert — só runs cujo updated_at mudou desde o último push
        for run in runs:
            rid = run.get("run_id")
            if not rid:
                continue
            upd = int(run.get("updated_at") or 0)
            if self.cursor.runs.get(rid) != upd:
                events.append(make_event(self.cfg, "run_upsert", run))
                self.cursor.runs[rid] = upd

        # run_log — leitura incremental do log de cada run recente
        for run in runs:
            rid = run.get("run_id")
            if rid:
                events.extend(self._read_log_events(rid))

        # pool_snapshot — só quando o pool muda (hash)
        ph = _hash_pool(pool)
        if ph != self.cursor.pool_hash:
            events.append(make_event(self.cfg, "pool_snapshot", pool))
            self.cursor.pool_hash = ph

        return events

    def _read_log_events(self, run_id: str) -> list[dict[str, Any]]:
        """Lê o run.log a partir do último offset; fatia em lotes de linhas."""
        path = RUNS_DIR / run_id / "run.log"
        if not path.exists():
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        offset = self.cursor.log_offsets.get(run_id, 0)
        if offset > size:  # log truncado/recriado — relê do início
            offset = 0
        if size <= offset:
            return []
        try:
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return []
        self.cursor.log_offsets[run_id] = offset + len(data)
        text = data.decode("utf-8", errors="replace")
        lines = [parse_log_line(ln) for ln in text.splitlines() if ln.strip()]
        events: list[dict[str, Any]] = []
        for i in range(0, len(lines), MAX_LOG_LINES_PER_EVENT):
            batch = lines[i:i + MAX_LOG_LINES_PER_EVENT]
            events.append(make_event(self.cfg, "run_log",
                                     {"run_id": run_id, "lines": batch}))
        return events

    # -- ciclo --------------------------------------------------------------
    def run_cycle(self) -> CycleResult:
        events = self.collect_events()
        if events:
            # Durabilidade primeiro: grava os eventos no outbox e SÓ ENTÃO
            # avança o cursor. Se cair entre as duas gravações, no pior caso
            # re-empurramos no próximo boot (o coletor deduplica por run_id).
            self.outbox.extend(events)
            self.outbox.save()
            self.cursor.save()
        sent = self._drain()
        return CycleResult(collected=len(events), sent=sent, pending=len(self.outbox))

    def _drain(self) -> int:
        """Tenta enviar a outbox em ordem; para no 1º erro (preserva FIFO)."""
        if not self.outbox.events:
            return 0
        sent = 0
        remaining: list[dict[str, Any]] = []
        blocked = False
        for ev in self.outbox.events:
            if blocked:
                remaining.append(ev)
                continue
            if self.client.send(ev):
                sent += 1
            else:
                blocked = True
                remaining.append(ev)
        self.outbox.events = remaining
        self.outbox.save()
        return sent
