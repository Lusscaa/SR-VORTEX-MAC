"""Persistência do coletor de admin (SQLite isolado em admin/admin.db).

Este módulo NÃO depende de nada fora de admin/. Guarda a telemetria
empurrada pelas instâncias (heartbeat, runs, logs, snapshots de pool) e
serve as agregações consumidas pela API de admin.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

# admin.db vive ao lado deste arquivo por padrão; ADMIN_DB_PATH é um override
# local opcional para apontar o banco para outro caminho.
DB_PATH = os.environ.get("ADMIN_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "admin.db"
)

# Janela (segundos) dentro da qual uma instância é considerada "online".
ONLINE_WINDOW_SECONDS = 120

_lock = threading.Lock()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS instances (
                instance_id   TEXT PRIMARY KEY,
                instance_name TEXT,
                last_seen     REAL
            );

            CREATE TABLE IF NOT EXISTS runs (
                instance_id TEXT NOT NULL,
                run_id      TEXT NOT NULL,
                status      TEXT,
                ts          REAL,
                data        TEXT,                 -- JSON: linha completa do run
                PRIMARY KEY (instance_id, run_id)
            );

            CREATE TABLE IF NOT EXISTS run_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                run_id      TEXT NOT NULL,
                ts          REAL,
                level       TEXT,
                msg         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_run_logs_lookup
                ON run_logs (instance_id, run_id, id);

            CREATE TABLE IF NOT EXISTS pools (
                instance_id TEXT PRIMARY KEY,
                cnpjs       TEXT,                 -- JSON array
                fb_accounts TEXT,                 -- JSON array (inclui credenciais)
                proxies     TEXT,                 -- JSON array
                updated_at  REAL
            );

            -- ============================================================
            -- MASTER (estoque central + funcionários) — adicionado 2026-07
            -- O dono cadastra funcionários (com token), contas do Facebook e
            -- CNPJs AQUI; os clientes puxam com o token. Sentido inverso da
            -- telemetria (que continua funcionando igual acima).
            -- ============================================================

            -- Funcionários: 1 token por pessoa + config (.env) individual.
            CREATE TABLE IF NOT EXISTS workers (
                token       TEXT PRIMARY KEY,   -- o que a pessoa digita no app
                name        TEXT,               -- nome do funcionário (label)
                config      TEXT,               -- JSON: overrides de .env desta pessoa
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  REAL,
                updated_at  REAL
            );

            -- Estoque central de CNPJs. status: available|claimed|used.
            CREATE TABLE IF NOT EXISTS master_cnpjs (
                cnpj        TEXT PRIMARY KEY,    -- só dígitos
                status      TEXT NOT NULL DEFAULT 'available',
                claimed_by  TEXT,               -- token do worker que pegou
                claimed_at  REAL,
                created_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_master_cnpjs_status
                ON master_cnpjs (status);

            -- Estoque central de contas do Facebook (inclui credenciais).
            CREATE TABLE IF NOT EXISTS master_fb_accounts (
                email       TEXT PRIMARY KEY,
                password    TEXT,
                twofa       TEXT,               -- segredo 2FA (opcional)
                proxy       TEXT,               -- proxy opcional atrelado à conta
                status      TEXT NOT NULL DEFAULT 'available',
                claimed_by  TEXT,
                claimed_at  REAL,
                created_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_master_fb_status
                ON master_fb_accounts (status);
            """
        )


def _now():
    return time.time()


def _touch_instance(conn, instance_id, instance_name, ts):
    conn.execute(
        """
        INSERT INTO instances (instance_id, instance_name, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(instance_id) DO UPDATE SET
            instance_name = COALESCE(excluded.instance_name, instances.instance_name),
            last_seen     = excluded.last_seen
        """,
        (instance_id, instance_name, ts),
    )


# --------------------------------------------------------------------------- #
# Ingestão                                                                     #
# --------------------------------------------------------------------------- #
def ingest(instance_id, instance_name, event_type, ts, payload):
    """Aplica um evento de telemetria. ts é o relógio da instância; usamos o
    relógio do coletor para last_seen (online) e como fallback de ordenação."""
    payload = payload or {}
    server_ts = _now()
    with _lock, _connect() as conn:
        # Qualquer evento conta como sinal de vida.
        _touch_instance(conn, instance_id, instance_name, server_ts)

        if event_type == "heartbeat":
            pass  # _touch_instance já registrou o last_seen

        elif event_type == "run_upsert":
            run_id = str(payload.get("run_id") or payload.get("id") or "")
            if not run_id:
                return {"ok": False, "error": "run_upsert sem run_id"}
            status = payload.get("status")
            conn.execute(
                """
                INSERT INTO runs (instance_id, run_id, status, ts, data)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, run_id) DO UPDATE SET
                    status = excluded.status,
                    ts     = excluded.ts,
                    data   = excluded.data
                """,
                (instance_id, run_id, status, ts or server_ts, json.dumps(payload)),
            )

        elif event_type == "run_log":
            run_id = str(payload.get("run_id") or payload.get("id") or "")
            if not run_id:
                return {"ok": False, "error": "run_log sem run_id"}
            # Aceita uma linha única ou um lote de linhas.
            lines = payload.get("lines")
            if lines is None:
                lines = [{
                    "ts": payload.get("ts", ts or server_ts),
                    "level": payload.get("level", "info"),
                    "msg": payload.get("msg", ""),
                }]
            conn.executemany(
                "INSERT INTO run_logs (instance_id, run_id, ts, level, msg) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        instance_id,
                        run_id,
                        ln.get("ts", ts or server_ts),
                        ln.get("level", "info"),
                        ln.get("msg", ""),
                    )
                    for ln in lines
                ],
            )

        elif event_type == "pool_snapshot":
            # Substitui o snapshot inteiro da instância.
            conn.execute(
                """
                INSERT INTO pools (instance_id, cnpjs, fb_accounts, proxies, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    cnpjs       = excluded.cnpjs,
                    fb_accounts = excluded.fb_accounts,
                    proxies     = excluded.proxies,
                    updated_at  = excluded.updated_at
                """,
                (
                    instance_id,
                    json.dumps(payload.get("cnpjs", [])),
                    json.dumps(payload.get("fb_accounts", [])),
                    json.dumps(payload.get("proxies", [])),
                    server_ts,
                ),
            )

        else:
            return {"ok": False, "error": f"event_type desconhecido: {event_type}"}

    return {"ok": True}


# --------------------------------------------------------------------------- #
# Consultas de admin                                                           #
# --------------------------------------------------------------------------- #
def list_instances():
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT instance_id, instance_name, last_seen FROM instances ORDER BY instance_name"
        ).fetchall()

        out = []
        for r in rows:
            iid = r["instance_id"]
            run_count = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE instance_id = ?", (iid,)
            ).fetchone()["n"]
            pool = conn.execute(
                "SELECT cnpjs, fb_accounts, proxies FROM pools WHERE instance_id = ?",
                (iid,),
            ).fetchone()

            def _len(field):
                if not pool or not pool[field]:
                    return 0
                try:
                    return len(json.loads(pool[field]))
                except (ValueError, TypeError):
                    return 0

            last_seen = r["last_seen"] or 0
            out.append({
                "instance_id": iid,
                "instance_name": r["instance_name"],
                "last_seen": last_seen,
                "counts": {
                    "cnpjs": _len("cnpjs"),
                    "fb_accounts": _len("fb_accounts"),
                    "proxies": _len("proxies"),
                    "runs": run_count,
                },
                "online": (now - last_seen) <= ONLINE_WINDOW_SECONDS,
            })
        return out


def list_runs(instance_id, status=None):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT data FROM runs WHERE instance_id = ? AND status = ? ORDER BY ts DESC",
                (instance_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT data FROM runs WHERE instance_id = ? ORDER BY ts DESC",
                (instance_id,),
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]


def get_run_log(instance_id, run_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, level, msg FROM run_logs WHERE instance_id = ? AND run_id = ? ORDER BY id",
            (instance_id, run_id),
        ).fetchall()
        return {"lines": [{"ts": r["ts"], "level": r["level"], "msg": r["msg"]} for r in rows]}


def get_pools(instance_id):
    with _connect() as conn:
        pool = conn.execute(
            "SELECT cnpjs, fb_accounts, proxies FROM pools WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        if not pool:
            return {"cnpjs": [], "fb_accounts": [], "proxies": []}

        def _load(field):
            try:
                return json.loads(pool[field]) if pool[field] else []
            except (ValueError, TypeError):
                return []

        return {
            "cnpjs": _load("cnpjs"),
            "fb_accounts": _load("fb_accounts"),  # inclui credenciais, por design
            "proxies": _load("proxies"),
        }


# --------------------------------------------------------------------------- #
# MASTER — funcionários (workers)                                             #
# --------------------------------------------------------------------------- #
def _only_digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def create_worker(token, name, config=None):
    """Cadastra (ou atualiza) um funcionário pelo token. config = dict de .env."""
    now = _now()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO workers (token, name, config, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                name       = excluded.name,
                config     = excluded.config,
                updated_at = excluded.updated_at
            """,
            (token, name, json.dumps(config or {}), now, now),
        )
    return {"ok": True, "token": token, "name": name}


def set_worker_config(token, config):
    """Substitui a config (.env) de um funcionário."""
    now = _now()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE workers SET config = ?, updated_at = ? WHERE token = ?",
            (json.dumps(config or {}), now, token),
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": "worker não encontrado"}
    return {"ok": True}


def set_worker_active(token, active):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE workers SET active = ?, updated_at = ? WHERE token = ?",
            (1 if active else 0, _now(), token),
        )
    return {"ok": True}


def get_worker(token):
    with _connect() as conn:
        r = conn.execute(
            "SELECT token, name, config, active, created_at, updated_at "
            "FROM workers WHERE token = ?",
            (token,),
        ).fetchone()
    if not r:
        return None
    try:
        config = json.loads(r["config"]) if r["config"] else {}
    except (ValueError, TypeError):
        config = {}
    return {
        "token": r["token"],
        "name": r["name"],
        "config": config,
        "active": bool(r["active"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def list_workers():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, name, active, created_at, updated_at FROM workers "
            "ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            tok = r["token"]
            cn = conn.execute(
                "SELECT COUNT(*) n FROM master_cnpjs WHERE claimed_by = ?", (tok,)
            ).fetchone()["n"]
            ac = conn.execute(
                "SELECT COUNT(*) n FROM master_fb_accounts WHERE claimed_by = ?", (tok,)
            ).fetchone()["n"]
            out.append({
                "token": tok,
                "name": r["name"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "claimed": {"cnpjs": cn, "fb_accounts": ac},
            })
        return out


# --------------------------------------------------------------------------- #
# MASTER — estoque de CNPJs                                                    #
# --------------------------------------------------------------------------- #
def add_cnpjs(cnpjs):
    """Adiciona CNPJs ao estoque (ignora duplicados). Retorna quantos entraram."""
    now = _now()
    added = 0
    with _lock, _connect() as conn:
        for c in cnpjs or []:
            d = _only_digits(c)
            if len(d) != 14:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO master_cnpjs (cnpj, status, created_at) "
                "VALUES (?, 'available', ?)",
                (d, now),
            )
            added += cur.rowcount
    return {"ok": True, "added": added}


def list_master_cnpjs(status=None, limit=1000):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM master_cnpjs WHERE status = ? ORDER BY created_at LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM master_cnpjs ORDER BY created_at LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def claim_cnpj(worker_token):
    """Entrega 1 CNPJ disponível ao worker (atômico). None se acabou."""
    now = _now()
    with _lock, _connect() as conn:
        r = conn.execute(
            "SELECT cnpj FROM master_cnpjs WHERE status = 'available' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not r:
            return None
        conn.execute(
            "UPDATE master_cnpjs SET status='claimed', claimed_by=?, claimed_at=? "
            "WHERE cnpj=?",
            (worker_token, now, r["cnpj"]),
        )
        return {"cnpj": r["cnpj"]}


# --------------------------------------------------------------------------- #
# MASTER — estoque de contas do Facebook                                      #
# --------------------------------------------------------------------------- #
def add_fb_accounts(accounts):
    """accounts = lista de dicts {email,password,twofa?,proxy?}. Ignora dup."""
    now = _now()
    added = 0
    with _lock, _connect() as conn:
        for a in accounts or []:
            email = (a.get("email") or "").strip().lower()
            if not email:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO master_fb_accounts "
                "(email, password, twofa, proxy, status, created_at) "
                "VALUES (?, ?, ?, ?, 'available', ?)",
                (email, a.get("password") or "", a.get("twofa") or "",
                 a.get("proxy") or "", now),
            )
            added += cur.rowcount
    return {"ok": True, "added": added}


def list_master_fb_accounts(status=None, limit=1000):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM master_fb_accounts WHERE status = ? "
                "ORDER BY created_at LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM master_fb_accounts ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def claim_fb_account(worker_token):
    """Entrega 1 conta disponível ao worker (atômico). None se acabou."""
    now = _now()
    with _lock, _connect() as conn:
        r = conn.execute(
            "SELECT email, password, twofa, proxy FROM master_fb_accounts "
            "WHERE status = 'available' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not r:
            return None
        conn.execute(
            "UPDATE master_fb_accounts SET status='claimed', claimed_by=?, claimed_at=? "
            "WHERE email=?",
            (worker_token, now, r["email"]),
        )
        return {"email": r["email"], "password": r["password"],
                "twofa": r["twofa"], "proxy": r["proxy"]}
