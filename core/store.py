"""Store SQLite de runs e BMs (Business Managers / WABAs).

Por que SQLite (e não os JSON de ``utils/*_state.py``):
  * **Concorrência**: o motor de execução paralela (2–3 workers) escreve
    status de runs ao mesmo tempo. JSON sem lock = last-write-wins (perda de
    dado). SQLite em modo WAL aceita N leitores + 1 escritor com
    ``busy_timeout`` — sem corrida.
  * **Queries**: o dashboard precisa de contagens/filtros (quantas BMs,
    quantas viraram 2k, taxa de sucesso). SQL faz isso direto.

Persiste o que ANTES era jogado fora: ``waba_id``/``meta_token``/
``business_id`` ficavam só no ``session_state`` do Streamlit e sumiam ao fim
do run. Agora viram registros duráveis (tabela ``bms``), base da seção de BMs.

Conexão é aberta POR CHAMADA (cada thread/worker usa a sua) — o módulo
``sqlite3`` não compartilha conexão entre threads com segurança. WAL +
``busy_timeout`` cobrem a concorrência sem precisar de pool.

Schema
------
``runs``  — uma linha por execução do pipeline (uma conta).
``bms``   — uma linha por BM/WABA criada (o ativo durável que rastreamos).

A regra do que conta como "2k" NÃO está aqui: guardamos o ``tier`` cru da
Graph API (``messaging_limit``) e a classificação fica na Fase 3, calibrada
contra o dado real.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from config import STATE_DIR

logger = logging.getLogger("meta_automation.store")

DB_PATH = STATE_DIR / "meta_automation.db"

# Status canônicos de um run (string livre no banco, mas estes são os usados).
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCESS = "success"          # fechou 100% do fluxo (portfólio→verificação)
RUN_INCOMPLETE = "incomplete"    # rodou sem erro mas parou antes do fim
RUN_FAILED = "failed"
RUN_NEEDS_ATTENTION = "needs_attention"
RUN_CANCELLED = "cancelled"
RUN_ASSUMED = "assumed"          # operador assumiu manualmente (bot parou)
RUN_PAUSED = "paused"            # aguardando ação externa para continuar

# Classificação de tier da BM (a regra do "2k" é calibrada na Fase 3).
TIER_UNKNOWN = "unknown"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT UNIQUE NOT NULL,
    cnpj            TEXT,
    label           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    step            TEXT,
    business_id     TEXT,
    waba_id         TEXT,
    -- Passo 4 (2026-09): conta de anúncios criada no portfólio (nome da BM,
    -- moeda BRL). Substituiu a WABA como ativo do passo 4.
    ad_account_id   TEXT,
    meta_token      TEXT,
    -- Prova ao vivo (_verify_on_capture): 1 = o token capturado AUTENTICOU na
    -- Graph API nesta run (método comprovado). 0 = token de tipo errado/morto
    -- ou nem chegou a checar. Sem isto a prova sumia (só ia pro log, rotacionado).
    verify_token_ok INTEGER NOT NULL DEFAULT 0,
    sms_phone            TEXT,
    profile_user_id      TEXT,
    profile_serial_number TEXT,
    proxy_host           TEXT,
    error                TEXT,
    created_at      INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_cnpj   ON runs(cnpj);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);

CREATE TABLE IF NOT EXISTS bms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     TEXT UNIQUE NOT NULL,
    cnpj            TEXT,
    portfolio_name  TEXT,
    account_email   TEXT,   -- conta FB/AdsPower que criou a BM (login); senha+2FA vêm de fb_accounts no read
    waba_id         TEXT,
    -- Conta de anúncios do portfólio (passo 4 desde 2026-09).
    ad_account_id   TEXT,
    meta_token      TEXT,
    -- 1 = meta_token é um token de System User (PERMANENTE) → o re-check consulta
    -- a Graph sem reabrir o browser. 0 = token de sessão (~1h, morto após a run);
    -- p/ pegar o flip 'aprovada' precisaria reabrir o perfil e recapturar.
    token_durable   INTEGER NOT NULL DEFAULT 0,
    tier            TEXT NOT NULL DEFAULT 'unknown',
    status_detail   TEXT,
    last_run_id     TEXT,
    last_checked_at INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bms_tier ON bms(tier);
CREATE INDEX IF NOT EXISTS idx_bms_cnpj ON bms(cnpj);

CREATE TABLE IF NOT EXISTS fb_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE NOT NULL,
    password    TEXT,
    totp_seed   TEXT,
    status      TEXT NOT NULL DEFAULT 'available',
    used_by_run TEXT,
    portfolios  INTEGER NOT NULL DEFAULT 0,
    -- Timestamp Unix até quando a conta está congelada (não será arrendada).
    -- NULL = sem congelamento. Após este timestamp a conta volta ao pool
    -- automaticamente na próxima chamada de lease_fb_account.
    frozen_until INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fbacc_status ON fb_accounts(status);

CREATE TABLE IF NOT EXISTS cnpjs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj        TEXT UNIQUE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'available',
    used_by_run TEXT,
    failed_runs INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cnpjs_status ON cnpjs(status);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);

-- Cache de consultas de CNPJ (BrasilAPI). Evita re-bater a API (rate limit
-- 429) em toda prévia/run: busca uma vez, reusa. data_json = CNPJData serializado.
CREATE TABLE IF NOT EXISTS cnpj_cache (
    cnpj       TEXT PRIMARY KEY,
    data_json  TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

-- Consumo do Cloudflare POR CREDENCIAL (account_id). Cada deploy de Worker
-- bem-sucedido = 1 "site" gasto. O cap (CLOUDFLARE_ROTATION_THRESHOLD, =10)
-- diz quantas runs ainda restam naquela conta. Persiste entre runs/restarts
-- para o operador não re-preencher credenciais "no escuro".
CREATE TABLE IF NOT EXISTS cloudflare_usage (
    account_id TEXT PRIMARY KEY,
    sites_used INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

-- Pool de credenciais Cloudflare. O bot usa sempre a primeira conta com
-- remaining > 0 (get_active_cloudflare_account). Quando uma esgota o
-- sistema já pula para a próxima sem intervenção manual.
CREATE TABLE IF NOT EXISTS cloudflare_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT UNIQUE NOT NULL,
    api_token  TEXT NOT NULL,
    cap        INTEGER NOT NULL DEFAULT 15,
    created_at INTEGER NOT NULL
);

-- Domínios Netlify (substitui a Cloudflare). Cada domínio tem: o token da
-- conta Netlify dona, a zona DNS criada, os nameservers (para configurar na
-- Hostinger), o cap (limite de sites/CNPJs definido pelo operador ao adicionar
-- o domínio) e quantos já foram gastos. O bot usa o primeiro domínio com cota
-- livre e nameservers já propagados (get_active_netlify_domain).
CREATE TABLE IF NOT EXISTS netlify_domains (
    domain       TEXT PRIMARY KEY,
    api_token    TEXT NOT NULL,
    account_slug TEXT,
    zone_id      TEXT,
    site_id      TEXT,                       -- 1 site Netlify por domínio (wildcard)
    nameservers  TEXT,                       -- JSON: lista de NS da Netlify
    cap          INTEGER NOT NULL DEFAULT 10,
    sites_used   INTEGER NOT NULL DEFAULT 0,
    dns_ok       INTEGER NOT NULL DEFAULT 0, -- 1 quando os NS já propagaram
    created_at   INTEGER NOT NULL
);

-- Mapa de subdomínios (CNPJs) por domínio. No modelo wildcard, 1 site Netlify
-- serve TODOS os subdomínios e a Edge Function escolhe o conteúdo pelo Host a
-- partir deste mapa. Cada CNPJ = 1 linha (slug -> cnpj + token do Facebook).
CREATE TABLE IF NOT EXISTS netlify_sites (
    domain     TEXT NOT NULL,
    slug       TEXT NOT NULL,
    cnpj       TEXT,
    fbtoken    TEXT,
    fqdn       TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (domain, slug)
);


-- Domínios Cloudflare (host ALTERNATIVO ao Netlify; MESMO modelo wildcard).
-- Cada domínio: account_id + api_token da conta Cloudflare dona, a zona
-- (zone_id), os nameservers da Cloudflare (pra pôr na Hostinger), o cap e o uso.
-- Só é usado quando settings.hosting_provider = 'cloudflare'. Netlify segue
-- sendo o padrão e nada aqui interfere no fluxo Netlify.
CREATE TABLE IF NOT EXISTS cloudflare_domains (
    domain       TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    api_token    TEXT NOT NULL,
    zone_id      TEXT,
    nameservers  TEXT,                       -- JSON: lista de NS da Cloudflare
    cap          INTEGER NOT NULL DEFAULT 100,
    sites_used   INTEGER NOT NULL DEFAULT 0,
    dns_ok       INTEGER NOT NULL DEFAULT 0, -- 1 quando os NS já propagaram
    created_at   INTEGER NOT NULL
);

-- Mapa de subdomínios (CNPJs) por domínio Cloudflare. 1 Worker serve TODOS os
-- subdomínios e escolhe o conteúdo pelo Host a partir deste mapa (wildcard).
CREATE TABLE IF NOT EXISTS cloudflare_domain_sites (
    domain     TEXT NOT NULL,
    slug       TEXT NOT NULL,
    cnpj       TEXT,
    fbtoken    TEXT,
    fqdn       TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (domain, slug)
);

-- Pool de proxies p/ mascarar o IP por conta (evita soft-block/derrubada). O
-- proxy é STICKY por CNPJ: a mesma conta sempre sai pelo MESMO IP (consistência
-- anti-detect, sobrevive ao 2º passe). Runs paralelas pegam IPs distintos.
CREATE TABLE IF NOT EXISTS proxies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,   -- host:port (permite duplicatas agora)
    scheme       TEXT NOT NULL DEFAULT 'http',
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL,
    username     TEXT,
    password     TEXT,
    status       TEXT NOT NULL DEFAULT 'available',  -- available | used
    used_by_cnpj TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxies_status ON proxies(status);
"""

POOL_AVAILABLE = "available"
POOL_USED = "used"
POOL_WRONG_PASSWORD = "wrong_password"  # Senha incorreta — aguardando troca do fornecedor
POOL_PENDING_DISCARD = "pending_discard"  # Erro terminal — aguardando revisão/discard manual do operador
# Proxy permanentemente ruim (soft-block de IP) — nunca mais é leaseado.
# 'ips que não passarem descarta' (operador 2026-06). lease_proxy só pega
# 'available', então 'discarded' é naturalmente pulado.
POOL_DISCARDED = "discarded"

# Lock GLOBAL (processo todo) para qualquer leitura+escrita de status de
# pool (proxies/cnpjs/fb_accounts). SQLite por si só NÃO impede a corrida:
# cada chamada abre sua PRÓPRIA conexão (``_conn()``) e o SELECT de "pegue 1
# disponível" não trava a linha — duas runs concorrentes (threads do
# JobManager) podiam SELECT o MESMO registro 'available' antes de qualquer
# UPDATE comitar, e as duas saíam crendo que pegaram aquele proxy/CNPJ/conta
# (pedido do operador 2026-06: "cada run usa um de cada, sem repetir").
# Um lock de thread no nível do Python serializa essas funções entre si —
# suficiente aqui porque é um processo único (FastAPI + ThreadPoolExecutor),
# não múltiplos processos/máquinas batendo no mesmo arquivo .db.
_POOL_LOCK = threading.Lock()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Conexão por chamada, em WAL, com row factory por nome de coluna."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_initialized = False

_QUOTA_BACKFILL_KEY = "quota_sites_used_backfilled"


def _backfill_domain_quota_from_site_maps(conn: sqlite3.Connection) -> None:
    """Uma vez: copia o nº de sites publicados para ``sites_used``.

    DBs antigos calculavam a cota pelo COUNT do mapa; a cota passou a ser o
    campo ``sites_used`` (editável). Sem este backfill, o restante apareceria
    cheio no primeiro boot. Depois o operador ajusta à vontade — a flag
    impede o COUNT de sobrescrever o valor editado.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_QUOTA_BACKFILL_KEY,)
    ).fetchone()
    if row and str(row["value"] or "") == "1":
        return
    try:
        conn.execute(
            "UPDATE netlify_domains SET sites_used = ("
            "  SELECT COUNT(*) FROM netlify_sites s"
            "  WHERE s.domain = netlify_domains.domain"
            ") WHERE ("
            "  SELECT COUNT(*) FROM netlify_sites s"
            "  WHERE s.domain = netlify_domains.domain"
            ") > sites_used"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "UPDATE cloudflare_domains SET sites_used = ("
            "  SELECT COUNT(*) FROM cloudflare_domain_sites s"
            "  WHERE s.domain = cloudflare_domains.domain"
            ") WHERE ("
            "  SELECT COUNT(*) FROM cloudflare_domain_sites s"
            "  WHERE s.domain = cloudflare_domains.domain"
            ") > sites_used"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_QUOTA_BACKFILL_KEY, "1", int(time.time())),
    )
    logger.info("store: migração — cota de sites_used alinhada ao mapa publicado.")


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrações leves p/ DBs criados antes de mudanças de schema.

    CREATE TABLE IF NOT EXISTS não altera tabela existente — DBs antigos
    ficavam sem colunas novas (ex.: bms.status_detail, renomeada de
    messaging_limit), derrubando a UI com KeyError. Aqui adicionamos o que
    falta, idempotente."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bms)").fetchall()}
    if cols and "status_detail" not in cols:
        conn.execute("ALTER TABLE bms ADD COLUMN status_detail TEXT")
        if "messaging_limit" in cols:  # herda o dado da coluna antiga
            conn.execute(
                "UPDATE bms SET status_detail = messaging_limit "
                "WHERE status_detail IS NULL"
            )
        logger.info("store: migração — coluna bms.status_detail adicionada.")

    if cols and "account_email" not in cols:
        conn.execute("ALTER TABLE bms ADD COLUMN account_email TEXT")
        # Backfill: a conta que criou a BM é o label do último run (label=email
        # nos dois fluxos: enqueue e manual). Recupera de runs.label — só quando
        # a coluna last_run_id existe (DBs muito antigos/mínimos podem não ter) e
        # a tabela runs está disponível.
        if "last_run_id" in cols:
            try:
                conn.execute(
                    "UPDATE bms SET account_email = ("
                    "  SELECT r.label FROM runs r WHERE r.run_id = bms.last_run_id"
                    ") WHERE account_email IS NULL AND last_run_id IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass  # sem tabela runs (DB mínimo/teste) — segue sem backfill
        logger.info(
            "store: migração — coluna bms.account_email adicionada"
            "%s.",
            " + backfill via runs.label" if "last_run_id" in cols else "",
        )

    # Migração: coluna netlify_domains.site_id (modelo wildcard).
    try:
        nf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(netlify_domains)").fetchall()}
        if nf_cols and "site_id" not in nf_cols:
            conn.execute("ALTER TABLE netlify_domains ADD COLUMN site_id TEXT")
            logger.info("store: migração — coluna netlify_domains.site_id adicionada.")
    except sqlite3.OperationalError:
        pass

    # Migração: aba "Gerar Site" — empresa_json (dados EDITÁVEIS embutidos no
    # site) + source ('run' | 'manual') p/ os cards de "Meus Sites".
    for _tbl in ("cloudflare_domain_sites", "netlify_sites"):
        try:
            _sc = {r["name"] for r in conn.execute(f"PRAGMA table_info({_tbl})").fetchall()}
            if _sc and "empresa_json" not in _sc:
                conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN empresa_json TEXT")
                logger.info("store: migração — coluna %s.empresa_json adicionada.", _tbl)
            if _sc and "source" not in _sc:
                conn.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN source TEXT NOT NULL DEFAULT 'run'"
                )
                logger.info("store: migração — coluna %s.source adicionada.", _tbl)
            if _sc and "template" not in _sc:
                conn.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN template TEXT NOT NULL DEFAULT 'classic'"
                )
                logger.info("store: migração — coluna %s.template adicionada.", _tbl)
        except sqlite3.OperationalError:
            pass

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if run_cols and "sms_phone" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN sms_phone TEXT")
        logger.info("store: migração — coluna runs.sms_phone adicionada.")

    if run_cols and "verify_token_ok" not in run_cols:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN verify_token_ok INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("store: migração — coluna runs.verify_token_ok adicionada.")

    if cols and "ad_account_id" not in cols:
        conn.execute("ALTER TABLE bms ADD COLUMN ad_account_id TEXT")
        logger.info("store: migração — coluna bms.ad_account_id adicionada.")

    if run_cols and "ad_account_id" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN ad_account_id TEXT")
        logger.info("store: migração — coluna runs.ad_account_id adicionada.")

    if cols and "token_durable" not in cols:
        conn.execute(
            "ALTER TABLE bms ADD COLUMN token_durable INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("store: migração — coluna bms.token_durable adicionada.")

    if cols and "manual_status" not in cols:
        conn.execute("ALTER TABLE bms ADD COLUMN manual_status TEXT")
        logger.info("store: migração — coluna bms.manual_status adicionada.")

    cf_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(cloudflare_accounts)").fetchall()
    }
    if cf_cols and "subdomain" not in cf_cols:
        conn.execute("ALTER TABLE cloudflare_accounts ADD COLUMN subdomain TEXT")
        logger.info("store: migração — coluna cloudflare_accounts.subdomain adicionada.")

    fb_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(fb_accounts)").fetchall()
    }
    for _col in ("email_address", "email_password", "recovery_email"):
        if fb_cols and _col not in fb_cols:
            conn.execute(f"ALTER TABLE fb_accounts ADD COLUMN {_col} TEXT")
            logger.info("store: migração — coluna fb_accounts.%s adicionada.", _col)

    if run_cols and "profile_serial_number" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN profile_serial_number TEXT")
        logger.info("store: migração — coluna runs.profile_serial_number adicionada.")

    if run_cols and "proxy_host" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN proxy_host TEXT")
        logger.info("store: migração — coluna runs.proxy_host adicionada.")

    if run_cols and "cnpj2" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN cnpj2 TEXT")
        logger.info("store: migração — coluna runs.cnpj2 adicionada.")

    if run_cols and "browser_provider" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN browser_provider TEXT DEFAULT 'adspower'")
        logger.info("store: migração — coluna runs.browser_provider adicionada.")

    acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(fb_accounts)").fetchall()}
    if acc_cols and "portfolios" not in acc_cols:
        conn.execute("ALTER TABLE fb_accounts ADD COLUMN portfolios INTEGER NOT NULL DEFAULT 0")
        logger.info("store: migração — coluna fb_accounts.portfolios adicionada.")

    if acc_cols and "frozen_until" not in acc_cols:
        conn.execute("ALTER TABLE fb_accounts ADD COLUMN frozen_until INTEGER")
        logger.info("store: migração — coluna fb_accounts.frozen_until adicionada.")

    if acc_cols and "remark" not in acc_cols:
        conn.execute("ALTER TABLE fb_accounts ADD COLUMN remark TEXT")
        logger.info("store: migração — coluna fb_accounts.remark adicionada.")

    cnpj_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cnpjs)").fetchall()}
    if cnpj_cols and "failed_runs" not in cnpj_cols:
        conn.execute("ALTER TABLE cnpjs ADD COLUMN failed_runs INTEGER NOT NULL DEFAULT 0")
        logger.info("store: migração — coluna cnpjs.failed_runs adicionada.")

    if cnpj_cols and "last_error" not in cnpj_cols:
        conn.execute("ALTER TABLE cnpjs ADD COLUMN last_error TEXT")
        logger.info("store: migração — coluna cnpjs.last_error adicionada.")

    if run_cols and "remark" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN remark TEXT")
        logger.info("store: migração — coluna runs.remark adicionada.")

    _backfill_domain_quota_from_site_maps(conn)


def init_db() -> None:
    """Cria tabelas/índices (idempotente). Seguro chamar a cada importação."""
    global _initialized
    if _initialized:
        return
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    _initialized = True
    logger.debug("store: schema inicializado em %s", DB_PATH)


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
def create_run(
    *,
    cnpj: Optional[str] = None,
    label: Optional[str] = None,
    status: str = RUN_QUEUED,
    run_id: Optional[str] = None,
) -> str:
    """Cria um registro de run e retorna o ``run_id`` (gerado se não vier)."""
    init_db()
    rid = run_id or uuid.uuid4().hex
    now = _now()
    started = now if status == RUN_RUNNING else None
    with _conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, cnpj, label, status, started_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, cnpj, label, status, started, now, now),
        )
    return rid


_RUN_FIELDS = {
    "cnpj", "cnpj2", "label", "status", "step", "business_id", "waba_id",
    "ad_account_id",
    "meta_token", "verify_token_ok", "sms_phone", "profile_user_id",
    "profile_serial_number", "proxy_host", "browser_provider", "error",
    "started_at", "finished_at",
}


def update_run(run_id: str, **fields: Any) -> None:
    """Atualiza campos de um run. Ignora chaves desconhecidas (defensivo).

    Setar ``status`` para um estado terminal (success/failed/cancelled)
    preenche ``finished_at`` automaticamente se ainda não veio explícito.
    """
    init_db()
    fields = {k: v for k, v in fields.items() if k in _RUN_FIELDS}
    if not fields:
        return
    status = fields.get("status")
    if status in (RUN_SUCCESS, RUN_INCOMPLETE, RUN_FAILED, RUN_CANCELLED, RUN_ASSUMED) and "finished_at" not in fields:
        fields["finished_at"] = _now()
    if status == RUN_RUNNING and "started_at" not in fields:
        fields["started_at"] = _now()
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE runs SET {cols} WHERE run_id = ?",
            (*fields.values(), run_id),
        )


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_runs(
    *, status: Optional[str] = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Runs mais recentes primeiro, opcionalmente filtrados por status."""
    init_db()
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC "
                "LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def _get_period_where_clause(period: str = "total", specific_date: str = "") -> str:
    """Retorna cláusula WHERE para filtro de período.

    Períodos: "total" (sem filtro), "dia" (últimas 24h), "semana" (últimos 7d),
    "mês" (últimos 30d).

    specific_date: formato "YYYY-MM-DD" para filtrar um dia específico (sobrescreve period).
    """
    import time
    from datetime import datetime, timedelta

    # Se specific_date foi fornecida (formato YYYY-MM-DD), filtrar por aquele dia
    if specific_date:
        try:
            dt = datetime.strptime(specific_date, "%Y-%m-%d")
            # Timestamp do início do dia (00:00:00)
            start = int(dt.timestamp())
            # Timestamp do fim do dia (23:59:59)
            end = int((dt + timedelta(days=1)).timestamp())
            return f"AND created_at >= {start} AND created_at < {end}"
        except ValueError:
            return ""  # data inválida = sem filtro

    # Caso contrário, usar período (dia/semana/mês/total)
    now = int(time.time())

    # "dia" = 00:00:00 até 23:59:59 de hoje (não últimas 24h)
    if period == "dia":
        dt_today = datetime.fromtimestamp(now)
        # Meia-noite do início do dia de hoje
        start = int(dt_today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        # Meia-noite do início do próximo dia
        end = int((dt_today + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return f"AND created_at >= {start} AND created_at < {end}"

    periods = {
        "semana": 7 * 24 * 3600,
        "mês": 30 * 24 * 3600,
    }
    if period not in periods:
        return ""  # total = sem filtro
    threshold = now - periods[period]
    return f"AND created_at >= {threshold}"


def run_counts(period: str = "total", specific_date: str = "") -> dict[str, int]:
    """Contagem de runs por status (para os cards do dashboard).

    period: "total", "dia", "semana", "mês"
    specific_date: formato "YYYY-MM-DD" para filtrar um dia específico
    """
    init_db()
    where_clause = _get_period_where_clause(period, specific_date)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS n FROM runs WHERE 1=1 {where_clause} "
            f"GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    counts["total"] = sum(counts.values())
    return counts


# Motivo da falha → rótulo legível. Primeiro tentamos o estado carimbado no
# texto do erro (LoginError termina com "[estado=<state>]"); depois palavras-
# chave; por fim a etapa; senão "Outros".
_FAILURE_STATE_LABELS: dict[str, str] = {
    "wrong_password": "Senha incorreta",
    "login_bounced": "Login bounce (voltou ao login)",
    "identity_selfie": "Selfie / identidade",
    "account_locked": "Conta restringida/invadida",
    "account_suspended": "Conta suspensa",
    "account_disabled": "Conta desativada",
    "account_not_found": "Conta inexistente",
    "rate_limited": "Bloqueio temporário (rate limit)",
    "two_factor_totp": "2FA",
    "two_factor_approval": "2FA",
    "checkpoint_generic": "Checkpoint manual",
    "unknown": "Tela desconhecida",
}

_STATE_RE = re.compile(r"\[estado=([a-z_]+)\]", re.I)


def classify_failure(error: Optional[str], step: Optional[str] = None) -> str:
    """Devolve um rótulo legível do MOTIVO da falha, a partir do texto do erro
    (e da etapa como reforço). Determinístico — usado no relatório do painel."""
    e = (error or "").lower()
    s = (step or "").lower()
    # 1) Estado carimbado pelo AuthFSM ("[estado=wrong_password]" etc.)
    m = _STATE_RE.search(error or "")
    if m:
        st = m.group(1).lower()
        if st in _FAILURE_STATE_LABELS:
            return _FAILURE_STATE_LABELS[st]
    # 2) Palavras-chave no texto do erro.
    if "encerrada pelo operador" in e:
        return "Encerrada pelo operador"
    if "proxy_egress" in e or "illegal request line" in e or "remoteprotocolerror" in e:
        return "Proxy (sem saída/egress)"
    if "pre-flight" in e or "preflight" in e:
        return "Pré-flight reprovado"
    if "pagina" in e or "sem acesso ao business" in e or "sem páginas" in e or "sem paginas" in e:
        return "Sem acesso ao Business (página)"
    if "selfie" in e:
        return "Selfie / identidade"
    if "senha" in e and ("inv" in e or "incorret" in e):
        return "Senha incorreta"
    if "captcha" in e:
        return "Captcha não resolvido"
    if "2fa" in e or "two_factor" in e or "two-factor" in e:
        return "2FA"
    if "cloudflare" in e:
        return "Cloudflare"
    if "adspower" in e or "cdp" in e:
        return "AdsPower / navegador"
    if "timeout" in e or "playwright" in e:
        return "Timeout / instabilidade de tela"
    # 3) Etapa como reforço.
    if "auth" in s or "login" in s:
        return "Login (sem detalhe)"
    if "preflight" in s:
        return "Pré-flight reprovado"
    if s:
        return f"Parou na etapa: {step}"
    return "Outros / sem detalhe"


def failure_breakdown(status: str = RUN_FAILED, period: str = "total", specific_date: str = "") -> list[dict[str, Any]]:
    """Relatório de falhas por MOTIVO: ``[{reason, count, pct}]`` ordenado do
    maior pro menor. ``pct`` é a % sobre o total de runs com esse ``status``.
    Função fixa do painel — recalcula ao vivo a cada poll.

    period: "total", "dia", "semana", "mês"
    specific_date: formato "YYYY-MM-DD" para filtrar um dia específico
    """
    init_db()
    where_clause = _get_period_where_clause(period, specific_date)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT error, step FROM runs WHERE status = ? {where_clause}",
            (status,),
        ).fetchall()
    total = len(rows)
    if not total:
        return []
    tally: dict[str, int] = {}
    for r in rows:
        reason = classify_failure(r["error"], r["step"])
        tally[reason] = tally.get(reason, 0) + 1
    out = [
        {"reason": k, "count": v, "pct": round(v * 100 / total, 1)}
        for k, v in tally.items()
    ]
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# bms (Business Managers / WABAs)
# ---------------------------------------------------------------------------
def upsert_bm(
    *,
    business_id: str,
    cnpj: Optional[str] = None,
    portfolio_name: Optional[str] = None,
    account_email: Optional[str] = None,
    waba_id: Optional[str] = None,
    ad_account_id: Optional[str] = None,
    meta_token: Optional[str] = None,
    token_durable: Optional[int] = None,
    last_run_id: Optional[str] = None,
) -> None:
    """Insere ou atualiza uma BM por ``business_id``.

    Campos novos só sobrescrevem quando vierem não-nulos (um run que não
    capturou o token não apaga o token de um run anterior). ``token_durable``
    sinaliza que o ``meta_token`` é um System User token (permanente). ``tier`` e
    ``messaging_limit`` NÃO são tocados aqui — são responsabilidade do
    poller da Graph API (Fase 3) via :func:`set_bm_tier`.
    """
    if not business_id:
        return
    init_db()
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT business_id FROM bms WHERE business_id = ?", (business_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO bms (business_id, cnpj, portfolio_name, "
                "account_email, waba_id, ad_account_id, meta_token, "
                "token_durable, last_run_id, tier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (business_id, cnpj, portfolio_name, account_email, waba_id,
                 ad_account_id, meta_token, int(token_durable or 0), last_run_id,
                 TIER_UNKNOWN, now, now),
            )
        else:
            conn.execute(
                "UPDATE bms SET "
                "  cnpj = COALESCE(?, cnpj),"
                "  portfolio_name = COALESCE(?, portfolio_name),"
                "  account_email = COALESCE(?, account_email),"
                "  waba_id = COALESCE(?, waba_id),"
                "  ad_account_id = COALESCE(?, ad_account_id),"
                "  meta_token = COALESCE(?, meta_token),"
                "  token_durable = COALESCE(?, token_durable),"
                "  last_run_id = COALESCE(?, last_run_id),"
                "  updated_at = ? "
                "WHERE business_id = ?",
                (cnpj, portfolio_name, account_email, waba_id, ad_account_id,
                 meta_token, token_durable, last_run_id, now, business_id),
            )


def set_bm_tier(
    business_id: str, *, tier: str, detail: Optional[str] = None
) -> None:
    """Grava a classificação ('2k'=verificada) + o status cru da Graph API.

    ``tier`` ∈ {'2k','not_2k','unknown'}; ``detail`` é o ``verification_status``
    cru retornado pela Graph (ex.: 'verified', 'pending', 'rejected')."""
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE bms SET tier = ?, status_detail = ?, last_checked_at = ?,"
            " updated_at = ? WHERE business_id = ?",
            (tier, detail, _now(), _now(), business_id),
        )


def set_bm_manual_status(business_id: str, status: Optional[str]) -> None:
    """Grava o status MANUAL do operador (rótulo visual escolhido no painel:
    'analise', 'aprovada1', 'aprovada2', '2k', 'banida'). Fica SEPARADO do
    ``tier`` automático da Graph — o 'Atualizar verificação' não o sobrescreve.
    ``status`` vazio/None limpa o override (volta ao automático)."""
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE bms SET manual_status = ?, updated_at = ? WHERE business_id = ?",
            ((status or None), _now(), business_id),
        )


def get_bm(business_id: str) -> Optional[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM bms WHERE business_id = ?", (business_id,)
        ).fetchone()
    return dict(row) if row else None


def list_bms(
    *, tier: Optional[str] = None, limit: int = 500, success_only: bool = False
) -> list[dict[str, Any]]:
    """Lista BMs (mais recentes primeiro).

    ``success_only=True`` devolve: (a) todas as BMs de runs 100%
    (``status = success``), e (b) BMs de runs ``incomplete`` que tenham
    ``meta_token`` (progresso real — o bot logou e capturou token de
    sessão). Stubs sem token (só portfolio criado) ficam ocultas."""
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if tier:
        clauses.append("tier = ?")
        params.append(tier)
    if success_only:
        clauses.append(
            "(last_run_id IN (SELECT run_id FROM runs WHERE status = ?)"
            " OR (last_run_id IN (SELECT run_id FROM runs WHERE status = ?)"
            "     AND meta_token IS NOT NULL AND meta_token != ''))"
        )
        params.extend([RUN_SUCCESS, RUN_INCOMPLETE])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM bms{where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def bm_counts(*, success_only: bool = False, period: str = "total", specific_date: str = "") -> dict[str, int]:
    """Contagem de BMs por tier (cards: total / 2k / não-2k / desconhecido).

    ``success_only`` espelha o filtro de :func:`list_bms` para o card do
    dashboard bater com a seção de BMs (runs que não crasharam).

    period: "total", "dia", "semana", "mês"
    specific_date: formato "YYYY-MM-DD" para filtrar um dia específico
    """
    init_db()
    where, params = "", []
    period_where = _get_period_where_clause(period, specific_date)
    if success_only:
        where = (
            " WHERE (last_run_id IN (SELECT run_id FROM runs WHERE status = ?"
            f" {period_where})"
            " OR (last_run_id IN (SELECT run_id FROM runs WHERE status = ?"
            f" {period_where})"
            "     AND meta_token IS NOT NULL AND meta_token != ''))"
        )
        params = [RUN_SUCCESS, RUN_INCOMPLETE]
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT tier, COUNT(*) AS n FROM bms{where} GROUP BY tier", params
        ).fetchall()
    counts = {r["tier"]: r["n"] for r in rows}
    counts["total"] = sum(counts.values())
    return counts


# ---------------------------------------------------------------------------
# pools — CNPJs e contas do Facebook (uploads em lote, pareados na fila)
# ---------------------------------------------------------------------------
def add_cnpjs(cnpjs: list[str]) -> int:
    """Adiciona CNPJs ao pool (dedup por valor). Retorna quantos entraram."""
    init_db()
    now = _now()
    added = 0
    with _conn() as conn:
        for raw in cnpjs:
            c = (raw or "").strip()
            if not c:
                continue
            try:
                conn.execute(
                    "INSERT INTO cnpjs (cnpj, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (c, POOL_AVAILABLE, now, now),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass  # já no pool
    return added


def add_fb_accounts(accounts: list[dict[str, Any]]) -> int:
    """Adiciona contas FB ao pool. **Upsert por email**: conta nova é inserida;
    conta que JÁ existe tem as credenciais atualizadas e é REATIVADA (volta a
    ``available``, descongelada) — assim re-colar um lote não perde as que já
    estavam no pool. Retorna quantas foram gravadas (novas + reativadas).

    Só reescreve um campo de credencial quando a linha nova traz valor (evita
    apagar senha/2FA já cadastrada se a re-colagem vier sem esse campo)."""
    init_db()
    now = _now()
    written = 0
    with _conn() as conn:
        for a in accounts:
            email = (a.get("email") or "").strip()
            if not email:
                continue
            cur = conn.execute(
                "INSERT INTO fb_accounts (email, password, totp_seed,"
                " email_address, email_password, recovery_email, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(email) DO UPDATE SET"
                "   password       = COALESCE(NULLIF(excluded.password, ''), fb_accounts.password),"
                "   totp_seed      = COALESCE(NULLIF(excluded.totp_seed, ''), fb_accounts.totp_seed),"
                "   email_address  = COALESCE(NULLIF(excluded.email_address, ''), fb_accounts.email_address),"
                "   email_password = COALESCE(NULLIF(excluded.email_password, ''), fb_accounts.email_password),"
                "   recovery_email = COALESCE(NULLIF(excluded.recovery_email, ''), fb_accounts.recovery_email),"
                "   status         = ?,"
                "   frozen_until   = NULL,"
                "   used_by_run    = NULL,"
                "   updated_at     = ?",
                (email, a.get("password"), a.get("totp_seed"),
                 a.get("email_address"), a.get("email_password"),
                 a.get("recovery_email"), POOL_AVAILABLE, now, now,
                 POOL_AVAILABLE, now),
            )
            if cur.rowcount:
                written += 1
    return written


def get_fb_account(email: str) -> Optional[dict[str, Any]]:
    """Conta FB por e-mail (login). Fonte única das credenciais (senha/2FA).

    Usada para enriquecer a view de BMs com login/senha/2FA da conta que criou
    a BM, SEM duplicar segredo na tabela bms (bms só guarda account_email)."""
    e = (email or "").strip()
    if not e:
        return None
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM fb_accounts WHERE email = ? LIMIT 1", (e,)
        ).fetchone()
    return dict(row) if row else None


def get_pool_cnpj_set() -> set[str]:
    """Retorna conjunto de todos os CNPJs no pool (qualquer status)."""
    init_db()
    with _conn() as conn:
        rows = conn.execute("SELECT cnpj FROM cnpjs").fetchall()
    return {r["cnpj"] for r in rows}


def list_cnpjs(*, status: Optional[str] = None, limit: int = 100000) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cnpjs WHERE status = ? ORDER BY id LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cnpjs ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def list_fb_accounts(*, status: Optional[str] = None, limit: int = 1000) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM fb_accounts WHERE status = ? ORDER BY id LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fb_accounts ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def pool_counts() -> dict[str, int]:
    """Disponíveis/usados de cada pool (para o painel de lote)."""
    init_db()
    with _conn() as conn:
        c = conn.execute(
            "SELECT status, COUNT(*) n FROM cnpjs GROUP BY status"
        ).fetchall()
        a = conn.execute(
            "SELECT status, COUNT(*) n FROM fb_accounts GROUP BY status"
        ).fetchall()
        p = conn.execute(
            "SELECT status, COUNT(*) n FROM proxies GROUP BY status"
        ).fetchall()
    cm = {r["status"]: r["n"] for r in c}
    am = {r["status"]: r["n"] for r in a}
    pm = {r["status"]: r["n"] for r in p}
    return {
        "cnpjs_available": cm.get(POOL_AVAILABLE, 0),
        "cnpjs_used": cm.get(POOL_USED, 0),
        "accounts_available": am.get(POOL_AVAILABLE, 0),
        "accounts_used": am.get(POOL_USED, 0),
        "proxies_available": pm.get(POOL_AVAILABLE, 0),
        "proxies_used": pm.get(POOL_USED, 0),
    }


# Limite de portfólios (BMs) por conta FB.
# Após atingir esse número, a conta é marcada como USED e NUNCA volta ao pool.
# 1 = descarta após 1 portfolio (nunca reutiliza a mesma conta).
FB_ACCOUNT_MAX_PORTFOLIOS = 1


def settle_fb_account(email: str, *, portfolios: Optional[int] = None, run_failed: bool = False) -> Optional[str]:
    """Decide o destino da conta ao FIM de um run (reuso vs consumida).

    Regra do operador: conta sem FB_ACCOUNT_MAX_PORTFOLIOS volta ao pool p/ rodar
    de novo; só fica ``used`` quando atinge esse limite.

    NOVO COMPORTAMENTO:
    - Se run_failed=True: conta mantém status=USED (não volta ao pool)
    - Se run_failed=False: aplica lógica normal (reuso ou consumo)

    Args:
        email: Email da conta FB
        portfolios: Total de portfólios DETECTADOS neste run (None = não chegou no passo 1)
        run_failed: True se a run FALHOU

    Devolve o novo status, ou None se a conta não está no pool (ex.: Manual com
    credenciais digitadas à mão). No-op fora do pool."""
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id, portfolios FROM fb_accounts WHERE lower(email) = ? LIMIT 1",
            (e,),
        ).fetchone()
        if not row:
            return None  # conta não veio do estoque — nada a fazer

        # Se a run FALHOU, conta fica USED permanentemente (não volta ao pool)
        if run_failed:
            status = POOL_USED
            conn.execute(
                "UPDATE fb_accounts SET status = ?, used_by_run = NULL, updated_at = ? "
                "WHERE id = ?",
                (status, _now(), row["id"]),
            )
            logger.info(f"store: FB Account {e}: falha detectada, mantém USED (não volta ao pool)")
            return status

        # Se a run SUCEDEU (run_failed=False), aplica lógica normal
        # Conta de portfólios: usa o detectado no run; senão preserva o salvo.
        n = portfolios if portfolios is not None else int(row["portfolios"] or 0)
        n = max(n, int(row["portfolios"] or 0))  # nunca regride o contador
        status = POOL_USED if n >= FB_ACCOUNT_MAX_PORTFOLIOS else POOL_AVAILABLE
        conn.execute(
            "UPDATE fb_accounts SET status = ?, portfolios = ?, used_by_run = NULL, updated_at = ? "
            "WHERE id = ?",
            (status, n, _now(), row["id"]),
        )
    return status


def pool_drain_projection() -> dict[str, Any]:
    """Projeção de esgotamento dos pools com base no consumo das últimas 24h."""
    init_db()
    now = _now()
    h24 = now - 86400
    with _conn() as conn:
        runs_24h = conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE created_at >= ?", (h24,)
        ).fetchone()["n"]
        success_24h = conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE status = 'success' AND created_at >= ?",
            (h24,),
        ).fetchone()["n"]
    counts = pool_counts()
    rate = max(success_24h, 1)
    acct_h = round((counts["accounts_available"] / rate) * 24, 1) if success_24h else None
    cnpj_h = round((counts["cnpjs_available"] / rate) * 24, 1) if success_24h else None
    return {
        "runs_24h": runs_24h,
        "success_24h": success_24h,
        "accounts_hours_left": acct_h,
        "cnpjs_hours_left": cnpj_h,
    }


def get_accounts_by_status(status: str) -> list[dict[str, Any]]:
    """Retorna todas as contas com um status específico (com motivo se houver)."""
    init_db()
    with _POOL_LOCK, _conn() as conn:
        rows = conn.execute(
            "SELECT id, email, status, remark FROM fb_accounts WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_frozen_accounts() -> list[dict[str, Any]]:
    """Retorna contas atualmente congeladas (frozen_until > now)."""
    init_db()
    now = _now()
    with _POOL_LOCK, _conn() as conn:
        rows = conn.execute(
            "SELECT id, email, status, remark, frozen_until FROM fb_accounts "
            "WHERE frozen_until IS NOT NULL AND frozen_until > ? ORDER BY frozen_until",
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_soft_blocked_accounts() -> list[dict[str, Any]]:
    """Retorna contas que tiveram soft-block (login_bounced) em runs recentes."""
    init_db()
    with _POOL_LOCK, _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.id, a.email, a.status, a.remark, a.frozen_until, "
            "  r.error AS last_error, r.created_at AS run_date "
            "FROM fb_accounts a "
            "JOIN runs r ON r.label = a.email "
            "WHERE (r.error LIKE '%login_bounced%' OR r.error LIKE '%soft%block%' "
            "  OR r.error LIKE '%bounced%') "
            "  AND r.created_at > ? "
            "ORDER BY r.created_at DESC",
            (_now() - 7 * 86400,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_account_pending_discard(email: Optional[str], reason: str = "") -> Optional[str]:
    """Marca a conta como PENDENTE DE DISCARD — operador revisa e decide descartar manualmente.

    Conta não volta ao pool (lease_fb_account só pega 'available'). Armazenamos
    o motivo no campo 'remark'.
    Devolve o novo status, ou None se a conta não está no pool. No-op fora dele."""
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()
        if not row:
            return None
        # Armazena o motivo no campo remark (já existe na tabela)
        remark = f"[PENDING_DISCARD] {reason}" if reason else "[PENDING_DISCARD]"
        conn.execute(
            "UPDATE fb_accounts SET status = ?, remark = ?, updated_at = ? WHERE id = ?",
            (POOL_PENDING_DISCARD, remark, _now(), row["id"]),
        )
    return POOL_PENDING_DISCARD


def discard_fb_account(email: Optional[str]) -> Optional[str]:
    """Marca a conta como DESCARTADA — não volta ao pool e nunca mais é
    arrendada (lease_fb_account só pega 'available'). Para contas com senha
    genuinamente RUIM: 'A entrada Senha é inválida' confirmado em 2 IPs ⇒ é a
    conta, não o IP (operador 2026-06-17) — re-arrendá-la só queima runs.
    Devolve o novo status, ou None se a conta não está no pool. No-op fora dele."""
    e = (email or "").strip().lower()
    if not e:
        logger.warning(f"[DISCARD_FB] Email vazio recebido")
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        # DEBUG: Log do email sendo procurado
        logger.info(f"[DISCARD_FB] Procurando email: {repr(e)}")

        # Primeiro tenta exata
        row = conn.execute(
            "SELECT id, status FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()

        if not row:
            # DEBUG: Mostra alguns emails no banco para comparar
            similar = conn.execute(
                "SELECT email, status FROM fb_accounts WHERE lower(email) LIKE ? LIMIT 5",
                (f"{e[:3]}%",)
            ).fetchall()
            logger.warning(f"[DISCARD_FB] ✗ Email não encontrado: {repr(e)}. Similares no banco: {[dict(r) for r in similar]}")
            return None

        logger.info(f"[DISCARD_FB] ✓ Encontrado — id={row['id']}, status atual={row['status']}")
        conn.execute(
            "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_DISCARDED, _now(), row["id"]),
        )
        logger.info(f"[DISCARD_FB] ✓ Atualizado para {POOL_DISCARDED}")
    return POOL_DISCARDED


_FREEZE_HOURS_DEFAULT = 3 / 60  # 3min — cooldown mínimo, não trava autopilot


def freeze_fb_account(email: Optional[str], *, hours: int = _FREEZE_HOURS_DEFAULT) -> Optional[str]:
    """Congela a conta por ``hours`` horas — não é arrendada até o fim do período.

    A conta mantém o ``status`` atual (available/used). Ao expirar,
    ``lease_fb_account`` a pega novamente sem nenhuma ação extra. Contas com
    status=discarded/used permanecem sem efeito prático (lease já as ignora).
    Devolve o status resultante ou None se a conta não está no estoque.
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    until = _now() + hours * 3600
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id, status FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()
        if not row:
            return None
        # Reseta status para available se estava used (run não 100%) para que
        # volte ao pool após o congelamento.
        new_status = POOL_AVAILABLE if row["status"] == POOL_USED else row["status"]
        conn.execute(
            "UPDATE fb_accounts SET status = ?, frozen_until = ?, updated_at = ? WHERE id = ?",
            (new_status, until, _now(), row["id"]),
        )
        return new_status


def mark_fb_account_wrong_password(email: Optional[str]) -> Optional[str]:
    """Marca a conta como tendo senha incorreta — aguardando troca do fornecedor.

    A conta é movida para status 'wrong_password' e não será mais arrendada.
    Devolve o status resultante ou None se a conta não está no estoque.
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_WRONG_PASSWORD, _now(), row["id"]),
        )
    return POOL_WRONG_PASSWORD


def force_release_fb_account(email: Optional[str]) -> Optional[dict[str, Any]]:
    """Libera a conta de volta a 'available' INDEPENDENTE do status atual —
    exceto se estiver DISCARDED (senha permanentemente ruim: não revive).

    Usado pra REINICIAR uma run TRAVADA OU quando o operador clica "Voltar Conta"
    após um erro temporário (sem isto a conta ficava 'used' presa pra sempre,
    sem nenhuma run rodando de verdade nela).

    NOVO: também libera contas com status WRONG_PASSWORD (o operador confirmou
    que a senha foi trocada no fornecedor).

    Devolve o registro COMPLETO (com password/totp), ou None se a conta não
    existe no pool ou está DESCARTADA (não dá pra reusar)."""
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()
        if not row:
            logger.warning(f"force_release: conta {e} não existe no estoque")
            return None
        if row["status"] == POOL_DISCARDED:
            logger.warning(f"force_release: conta {e} foi DESCARTADA — não pode reviver")
            return None
        if row["status"] == POOL_PENDING_DISCARD:
            logger.warning(f"force_release: conta {e} está PENDING_DISCARD — não pode reviver (erro terminal)")
            return None
        # Libera contas com qualquer outro status: USED, WRONG_PASSWORD, etc.
        conn.execute(
            "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_AVAILABLE, _now(), row["id"]),
        )
        logger.info(f"force_release: conta {e} liberada (status anterior: {row['status']})")
        d = dict(row)
        d["status"] = POOL_AVAILABLE
        return d


def settle_cnpj(cnpj: str, *, created_portfolio: bool, run_failed: bool = False, run_100pct: bool = False, error_msg: Optional[str] = None) -> Optional[str]:
    """Decide o destino do CNPJ ao FIM de um run.

    REGRA ABSOLUTA: JAMAIS reutilizar o mesmo CNPJ.

    Uma vez que um CNPJ é arrendado do pool e usado em uma run (independentemente de resultado),
    ele é DESCARTADO PERMANENTEMENTE. Não volta ao pool — nunca mais é usado.

    Args:
        cnpj: CNPJ a processar
        created_portfolio: Um portfólio foi criado/reusado para este CNPJ
        run_failed: True se a run FALHOU (antes/depois de criar portfólio)
        run_100pct: True se a run completou 100% (business + verificação)
        error_msg: Mensagem de erro para rastreamento

    Behavior:
    - SEMPRE descarta (regra: jamais reutilizar)
    - Não importa se criou portfolio, se falhou, ou se foi 100%

    Devolve o novo status, ou None se o CNPJ não está no pool."""
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM cnpjs WHERE cnpj = ? LIMIT 1", (digits,)
        ).fetchone()
        if not row:
            return None  # CNPJ não veio do estoque — nada a fazer

        # Destino do CNPJ ao fim do run:
        #  - criou/reusou BM ou fechou 100% → DESCARTA permanente (jamais reusar).
        #  - falhou ANTES de criar a BM → NÃO volta ao pool sozinho. Fica SEGURADO
        #    (used), igual às contas (settle_fb_account), e só retorna via botão
        #    'Recuperar Conta + CNPJ' (restore_cnpj). Pedido do operador (2026-09):
        #    nada volta ao pool automaticamente — só quando ele aperta o botão.
        #    (Antes voltava a 'available' sozinho; virava corrida com runs
        #    concorrentes enquanto o perfil ficava aberto p/ intervenção manual.)
        if created_portfolio or run_100pct:
            status = POOL_DISCARDED
            logger.warning(
                f"[SETTLE_CNPJ] CNPJ {digits} DESCARTADO PERMANENTEMENTE "
                f"(run_failed={run_failed}, created_portfolio={created_portfolio}, "
                f"100pct={run_100pct}, error={error_msg})"
            )
        else:
            status = POOL_USED
            logger.info(
                f"[SETTLE_CNPJ] CNPJ {digits} SEGURADO (used, run_failed="
                f"{run_failed}, sem BM) — NÃO volta ao pool sozinho; use o botão "
                f"'Recuperar Conta + CNPJ' para devolvê-lo."
            )
        conn.execute(
            "UPDATE cnpjs SET status = ?, used_by_run = NULL, failed_runs = 0, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (status, _now(), row["id"]),
        )
    return status


def discard_cnpj(cnpj: str) -> Optional[str]:
    """Marca o CNPJ como DESCARTADO — não volta ao pool e nunca mais é
    reservado (``reserve_one_cnpj``/``reserve_pairs`` só pegam 'available').

    Para CNPJ cujo nome derivado foi REJEITADO pelo Meta na criação do
    portfólio (2x seguidas, não é glitch transitório) — re-tentar o mesmo
    CNPJ só queima runs. Devolve o novo status, ou None se o CNPJ não está
    no pool. No-op fora dele."""
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM cnpjs WHERE cnpj = ? LIMIT 1", (digits,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET status = ?, used_by_run = NULL, updated_at = ? "
            "WHERE id = ?",
            (POOL_DISCARDED, _now(), row["id"]),
        )
    return POOL_DISCARDED


def restore_cnpj(cnpj: str) -> Optional[str]:
    """Devolve um CNPJ ao pool (status=available), desfazendo um descarte/uso.
    Usado pelo botão 'Recuperar Conta + CNPJ' quando a run falhou sem criar BM.
    Devolve o novo status, ou None se o CNPJ não está no estoque."""
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM cnpjs WHERE cnpj = ? LIMIT 1", (digits,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET status = ?, used_by_run = NULL, failed_runs = 0, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (POOL_AVAILABLE, _now(), row["id"]),
        )
    return POOL_AVAILABLE


def mark_cnpj_in_use(cnpj: str, run_id: str) -> bool:
    """BLOQUEIO IMEDIATO: marca CNPJ como em uso neste run ESPECÍFICO.

    Se o run morrer/travar no meio, o CNPJ fica marcado com used_by_run=run_id,
    bloqueando qualquer reutilização (reserve_pairs só pega status=available).

    Garante que o CNPJ nunca é servido 2x simultaneamente ou após crash.
    Retorna True se conseguiu marcar, False se o CNPJ não existe.
    """
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return False
    run_id_clean = (run_id or "").strip()
    if not run_id_clean:
        return False
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM cnpjs WHERE cnpj = ? LIMIT 1", (digits,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE cnpjs SET used_by_run = ?, updated_at = ? WHERE id = ?",
            (run_id_clean, _now(), row["id"]),
        )
    return True


def reserve_one_cnpj(exclude: Optional[set[str]] = None) -> Optional[str]:
    """Reserva UM CNPJ disponível do pool (marca ``used``) e devolve só o
    valor (string de dígitos). Pula os de ``exclude`` (ex.: o CNPJ que
    acabou de ser descartado neste mesmo run). ``None`` se o pool esgotou.

    Usado na troca de CNPJ em pleno run (nome do portfólio rejeitado pelo
    Meta) — diferente de ``reserve_pairs``, não toca em ``fb_accounts``."""
    excl = {"".join(ch for ch in (c or "") if ch.isdigit()) for c in (exclude or set())}
    excl.discard("")
    init_db()
    with _POOL_LOCK, _conn() as conn:
        if excl:
            ph = ",".join("?" * len(excl))
            row = conn.execute(
                "SELECT id, cnpj FROM cnpjs WHERE status = ? "
                f"AND cnpj NOT IN ({ph}) ORDER BY id LIMIT 1",
                (POOL_AVAILABLE, *excl),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, cnpj FROM cnpjs WHERE status = ? ORDER BY id LIMIT 1",
                (POOL_AVAILABLE,),
            ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_USED, _now(), row["id"]),
        )
        return row["cnpj"]


def get_cnpj_by_digits(digits: str) -> Optional[dict[str, Any]]:
    """Pega o CNPJ por dígitos SEM marcar como USED (usado após reserve_one_cnpj).

    Reserve_one_cnpj já marca como USED, então essa função só lê o objeto."""
    d = "".join(ch for ch in (digits or "") if ch.isdigit())
    if not d or len(d) != 14:
        return None
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM cnpjs WHERE cnpj = ? LIMIT 1", (d,)
        ).fetchone()
    return dict(row) if row else None


def take_cnpj(cnpj: str) -> Optional[dict[str, Any]]:
    """Reserva (marca ``used``) e devolve o CNPJ do estoque por valor —
    espelha ``take_fb_account``. ``None`` se não existir no pool."""
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM cnpjs WHERE cnpj = ? LIMIT 1", (digits,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_USED, _now(), row["id"]),
        )
        d = dict(row)
        d["status"] = POOL_USED
        return d


def force_release_cnpj(cnpj: str) -> Optional[dict[str, Any]]:
    """NUNCA libera CNPJ de volta ao pool — regra absoluta.

    Um CNPJ uma vez usado é DESCARTADO PERMANENTEMENTE e nunca mais reutilizado.
    Esta função retorna None sempre (não libera nada).

    Histórico: antes era usada para "reiniciar runs travadas", mas isso violava
    a regra absoluta "jamais reutilize o mesmo CNPJ". Agora desabilitada.

    Se um run ficar travada, use OUTRO CNPJ — não reutilize o antigo.
    """
    digits = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not digits:
        return None

    # REGRA ABSOLUTA: CNPJ NUNCA volta ao pool
    logger.warning(
        f"[FORCE_RELEASE_CNPJ_BLOCKED] Tentativa de liberar CNPJ {digits} "
        f"foi BLOQUEADA. Um CNPJ uma vez usado é DESCARTADO PERMANENTEMENTE."
    )
    return None  # ← NUNCA libera


def take_fb_account(email: str) -> Optional[dict[str, Any]]:
    """Reserva (marca ``used``) e devolve a conta do estoque por e-mail — p/ o
    Manual importar uma conta sem o operador re-digitar senha/2FA. Devolve o
    registro COMPLETO (com password/totp). ``None`` se não existir."""
    e = (email or "").strip().lower()
    if not e:
        return None
    init_db()
    with _POOL_LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM fb_accounts WHERE lower(email) = ? LIMIT 1", (e,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_USED, _now(), row["id"]),
        )
        d = dict(row)
        d["status"] = POOL_USED
        return d


def reserve_pairs(n: int) -> list[dict[str, Any]]:
    """Reserva até ``n`` pares (1 conta + 1 CNPJ disponíveis), marca USED.

    Pareamento 1:1 por ordem de chegada (qualquer conta serve p/ qualquer
    CNPJ). Limitado a min(n, contas_disp, cnpjs_disp). Atômico sob lock do
    SQLite — dois enfileiramentos simultâneos não pegam o mesmo recurso.
    Retorna ``[{"account": {...}, "cnpj": {...}}, …]``.

    GARANTIA: nenhum CNPJ é devolvido 2x simultaneamente (idempotência).
    Usa transação SQLite com rollback em caso de violação.
    """
    init_db()
    pairs: list[dict[str, Any]] = []
    if n <= 0:
        return pairs
    with _POOL_LOCK, _conn() as conn:
        # Força SERIALIZABLE para evitar phantom reads em race conditions extremas
        conn.isolation_level = "IMMEDIATE"  # Lock de escrita imediato
        try:
            accs = conn.execute(
                "SELECT * FROM fb_accounts WHERE status = ? ORDER BY id LIMIT ?",
                (POOL_AVAILABLE, n),
            ).fetchall()
            cnps = conn.execute(
                "SELECT * FROM cnpjs WHERE status = ? ORDER BY id LIMIT ?",
                (POOL_AVAILABLE, n),
            ).fetchall()
            now = _now()
            reserved_cnpjs = set()
            for acc, cnp in zip(accs, cnps):
                cnpj_str = cnp["cnpj"]
                # Check de duplicação: nunca devolva o mesmo CNPJ 2x
                if cnpj_str in reserved_cnpjs:
                    logger.error(
                        f"[RESERVE_PAIRS_GUARD] CNPJ {cnpj_str} já foi reservado "
                        f"nesta chamada de reserve_pairs — possível race condition!"
                    )
                    conn.rollback()
                    raise RuntimeError(
                        f"Duplicação de CNPJ detectada em reserve_pairs: {cnpj_str}"
                    )
                reserved_cnpjs.add(cnpj_str)

                conn.execute(
                    "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
                    (POOL_USED, now, acc["id"]),
                )
                conn.execute(
                    "UPDATE cnpjs SET status = ?, updated_at = ? WHERE id = ?",
                    (POOL_USED, now, cnp["id"]),
                )
                pairs.append({"account": dict(acc), "cnpj": dict(cnp)})
            conn.commit()
        except Exception as e:
            logger.error(f"[RESERVE_PAIRS] Erro durante reserva: {e}")
            conn.rollback()
            raise
        finally:
            conn.isolation_level = None  # Restaura default

    logger.info(f"[RESERVE_PAIRS] Reservados {len(pairs)} par(es) — "
                f"contas: {[p['account']['email'] for p in pairs]}, "
                f"cnpjs: {[p['cnpj']['cnpj'] for p in pairs]}")
    return pairs


def lease_fb_account(
    exclude_emails: Optional[set[str]] = None,
) -> Optional[dict[str, Any]]:
    """Arrenda UMA conta FB disponível (status=available), marca ``used`` e a
    devolve completa (email/password/totp_seed). Pula as de ``exclude_emails``
    (contas que já bouncearam neste run). ``None`` se o pool esgotou.

    Usado na rotação de CONTA pós login_bounced/senha-incorreta: o operador
    constatou que esses bounces são de IP e a conta TAMBÉM pode ficar marcada —
    então troca de conta (a anterior volta ao pool via settle_fb_account)."""
    excl = {(e or "").strip().lower() for e in (exclude_emails or set()) if e}
    init_db()
    now = _now()
    with _POOL_LOCK, _conn() as conn:
        if excl:
            ph = ",".join("?" * len(excl))
            row = conn.execute(
                "SELECT * FROM fb_accounts WHERE status = ? "
                "AND (frozen_until IS NULL OR frozen_until <= ?) "
                f"AND lower(email) NOT IN ({ph}) ORDER BY id LIMIT 1",
                (POOL_AVAILABLE, now, *excl),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fb_accounts WHERE status = ? "
                "AND (frozen_until IS NULL OR frozen_until <= ?) ORDER BY id LIMIT 1",
                (POOL_AVAILABLE, now),
            ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE fb_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (POOL_USED, _now(), row["id"]),
        )
        d = dict(row)
        d["status"] = POOL_USED
        return d


# ---------------------------------------------------------------------------
# settings — credenciais de infra inseridas no painel (não no .env)
# ---------------------------------------------------------------------------
def get_settings() -> dict[str, str]:
    """Todas as settings como dict {key: value}."""
    init_db()
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    return get_settings().get(key, default)


def set_settings(values: dict[str, Optional[str]]) -> None:
    """Upsert de settings. Valores None/'' são ignorados (não apaga o que já há)."""
    init_db()
    now = _now()
    with _conn() as conn:
        for key, val in values.items():
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, val.strip(), now),
            )


# ---------------------------------------------------------------------------
# cloudflare_usage — quantos "sites" (deploys de Worker) cada conta CF já gastou
# ---------------------------------------------------------------------------
def cf_usage_get(account_id: Optional[str]) -> int:
    """Sites já gastos pela credencial CF (0 se nunca usou / id vazio)."""
    acct = (account_id or "").strip()
    if not acct:
        return 0
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT sites_used FROM cloudflare_usage WHERE account_id = ?",
            (acct,),
        ).fetchone()
    return int(row["sites_used"]) if row else 0


def cf_usage_increment(account_id: Optional[str]) -> int:
    """Marca +1 site gasto na credencial CF; retorna o novo total (0 se id vazio)."""
    acct = (account_id or "").strip()
    if not acct:
        return 0
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cloudflare_usage (account_id, sites_used, updated_at) "
            "VALUES (?, 1, ?) ON CONFLICT(account_id) DO UPDATE SET "
            "sites_used = sites_used + 1, updated_at = excluded.updated_at",
            (acct, now),
        )
        row = conn.execute(
            "SELECT sites_used FROM cloudflare_usage WHERE account_id = ?",
            (acct,),
        ).fetchone()
    return int(row["sites_used"]) if row else 0


def cf_usage_reset(account_id: Optional[str]) -> None:
    """Zera o contador da credencial (operador limpou os Workers na conta CF)."""
    acct = (account_id or "").strip()
    if not acct:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cloudflare_usage (account_id, sites_used, updated_at) "
            "VALUES (?, 0, ?) ON CONFLICT(account_id) DO UPDATE SET "
            "sites_used = 0, updated_at = excluded.updated_at",
            (acct, _now()),
        )


# ---------------------------------------------------------------------------
# cloudflare_accounts — pool de credenciais (multi-conta)
# ---------------------------------------------------------------------------

def add_cloudflare_account(
    account_id: str, api_token: str, cap: int = 15, subdomain: str = ""
) -> None:
    """Adiciona (ou atualiza token/cap/subdomínio de) uma credencial Cloudflare.

    ``subdomain``: o subdomínio workers.dev da conta (o que vem antes de
    .workers.dev). Usado quando o token não consegue ler esse dado pela API —
    aí o operador informa o subdomínio real da conta aqui."""
    acct = account_id.strip()
    tok = api_token.strip()
    sub = (subdomain or "").strip().lower()
    if not acct or not tok:
        raise ValueError("account_id e api_token são obrigatórios.")
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cloudflare_accounts (account_id, api_token, cap, subdomain, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
            "api_token = excluded.api_token, cap = excluded.cap, "
            # só sobrescreve o subdomínio se um novo foi informado (não apaga o salvo)
            "subdomain = COALESCE(NULLIF(excluded.subdomain, ''), cloudflare_accounts.subdomain)",
            (acct, tok, max(1, int(cap)), sub, now),
        )


def list_cloudflare_accounts() -> list[dict[str, Any]]:
    """Lista todas as credenciais com o consumo atual de cada uma."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account_id, api_token, cap, subdomain, created_at FROM cloudflare_accounts ORDER BY created_at"
        ).fetchall()
        usage_rows = conn.execute(
            "SELECT account_id, sites_used FROM cloudflare_usage"
        ).fetchall()
    usage_map = {r["account_id"]: r["sites_used"] for r in usage_rows}
    result = []
    for r in rows:
        acct = r["account_id"]
        used = usage_map.get(acct, 0)
        cap = r["cap"]
        result.append({
            "account_id": acct,
            "api_token": r["api_token"],   # enviado mascarado pela API
            "cap": cap,
            "subdomain": (r["subdomain"] if "subdomain" in r.keys() else None) or "",
            "sites_used": used,
            "remaining": max(0, cap - used),
            "created_at": r["created_at"],
        })
    return result


def delete_cloudflare_account(account_id: str) -> None:
    """Remove uma credencial Cloudflare do pool (não apaga o uso histórico)."""
    acct = (account_id or "").strip()
    if not acct:
        return
    init_db()
    with _conn() as conn:
        conn.execute("DELETE FROM cloudflare_accounts WHERE account_id = ?", (acct,))


def get_active_cloudflare_account() -> Optional[dict[str, str]]:
    """Retorna a primeira credencial do pool com remaining > 0, ou None.

    "Ativa" = primeira a ser adicionada (ORDER BY created_at) que ainda tem
    slots disponíveis. Quando o operador adicionar uma 2ª conta, ela entra na
    fila e só é usada quando a 1ª esgotar."""
    accounts = list_cloudflare_accounts()
    for a in accounts:
        if a["remaining"] > 0:
            return {
                "account_id": a["account_id"],
                "api_token": a["api_token"],
                "subdomain": a.get("subdomain") or "",
            }
    return None


def get_cloudflare_accounts_for_runs(num_runs: int) -> list[dict[str, Any]]:
    """Retorna uma lista de contas Cloudflare para completar o número de runs.

    **Distribuição automática**: se a 1ª conta tem 4 slots e você quer 5 runs,
    retorna [conta1 (4 slots), conta2 (1 slot)]. Cada dict tem:
    - account_id, api_token, subdomain, slots_to_use (quantos slots usar dessa conta)

    Usa as contas em ordem de criação (FIFO). Se não houver slots suficientes,
    retorna as que conseguir (operador verá o aviso "limite atingido").
    """
    if num_runs <= 0:
        return []

    accounts = list_cloudflare_accounts()
    result = []
    remaining_runs = num_runs

    for a in accounts:
        if remaining_runs <= 0:
            break
        if a["remaining"] <= 0:
            continue

        # Usa quantos slots essa conta pode fornecer (até o que sobra)
        slots_to_use = min(a["remaining"], remaining_runs)
        result.append({
            "account_id": a["account_id"],
            "api_token": a["api_token"],
            "subdomain": a.get("subdomain") or "",
            "slots_to_use": slots_to_use,
            "remaining": a["remaining"],
        })
        remaining_runs -= slots_to_use

    return result


# ---------------------------------------------------------------------------
# netlify_domains — domínios + token + cap (limite) + uso + nameservers
# ---------------------------------------------------------------------------
def add_netlify_domain(
    domain: str,
    api_token: str,
    *,
    cap: int = 10,
    account_slug: str = "",
    zone_id: str = "",
    site_id: str = "",
    nameservers: Optional[list[str]] = None,
    dns_ok: bool = False,
) -> None:
    """Adiciona (ou atualiza) um domínio Netlify.

    ``cap`` é o limite de sites/CNPJs naquele domínio antes de rotacionar para o
    próximo. ``nameservers`` é a lista de NS da Netlify (mostrada ao operador
    para configurar na Hostinger). Não zera ``sites_used`` em update."""
    dom = (domain or "").strip().lower().strip(".")
    tok = (api_token or "").strip()
    if not dom or not tok:
        raise ValueError("domain e api_token são obrigatórios.")
    ns_json = json.dumps(nameservers or [])
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO netlify_domains "
            "(domain, api_token, account_slug, zone_id, site_id, nameservers, cap, "
            " sites_used, dns_ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET "
            "api_token = excluded.api_token, "
            "account_slug = COALESCE(NULLIF(excluded.account_slug,''), netlify_domains.account_slug), "
            "zone_id = COALESCE(NULLIF(excluded.zone_id,''), netlify_domains.zone_id), "
            "site_id = COALESCE(NULLIF(excluded.site_id,''), netlify_domains.site_id), "
            "nameservers = excluded.nameservers, "
            "cap = excluded.cap, "
            "dns_ok = excluded.dns_ok",
            (dom, tok, (account_slug or "").strip(), (zone_id or "").strip(),
             (site_id or "").strip(), ns_json, max(1, int(cap)),
             1 if dns_ok else 0, now),
        )


def list_netlify_domains() -> list[dict[str, Any]]:
    """Lista os domínios com consumo, cota restante e nameservers.

    A cota usa ``sites_used`` do domínio (incrementa em slug novo; o operador
    pode ajustar os restantes na UI). O mapa de sites publicados continua
    separado — editar a cota não apaga sites já no ar.
    """
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT domain, api_token, account_slug, zone_id, site_id, nameservers, cap, "
            "sites_used, dns_ok, created_at FROM netlify_domains ORDER BY created_at"
        ).fetchall()
    result = []
    for r in rows:
        try:
            ns = json.loads(r["nameservers"] or "[]")
        except Exception:  # noqa: BLE001
            ns = []
        cap = int(r["cap"])
        used = max(0, int(r["sites_used"] or 0))
        keys = r.keys()
        result.append({
            "domain": r["domain"],
            "api_token": r["api_token"],   # a API mascara antes de enviar à UI
            "account_slug": r["account_slug"] or "",
            "zone_id": r["zone_id"] or "",
            "site_id": (r["site_id"] if "site_id" in keys else None) or "",
            "nameservers": ns,
            "cap": cap,
            "sites_used": used,
            "remaining": max(0, cap - used),
            "dns_ok": bool(r["dns_ok"]),
            "created_at": r["created_at"],
        })
    return result


def get_netlify_domain(domain: str) -> Optional[dict[str, Any]]:
    """Devolve 1 domínio (com token em claro) ou None."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return None
    for d in list_netlify_domains():
        if d["domain"] == dom:
            return d
    return None


def delete_netlify_domain(domain: str) -> None:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute("DELETE FROM netlify_domains WHERE domain = ?", (dom,))


def netlify_usage_increment(domain: str) -> int:
    """Marca +1 site gasto no domínio; retorna o novo total (0 se vazio)."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return 0
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE netlify_domains SET sites_used = sites_used + 1 WHERE domain = ?",
            (dom,),
        )
        row = conn.execute(
            "SELECT sites_used FROM netlify_domains WHERE domain = ?", (dom,)
        ).fetchone()
    return int(row["sites_used"]) if row else 0


def netlify_usage_reset(domain: str) -> None:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE netlify_domains SET sites_used = 0 WHERE domain = ?", (dom,)
        )
        conn.commit()  # Força o commit explícito


def netlify_set_quota(
    domain: str, *, remaining: Optional[int] = None, cap: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """Ajusta cota do domínio. ``remaining`` = vagas ainda livres; ``cap`` = teto.

    Continua incrementando em slug novo. Editar remaining não apaga sites no ar.
    """
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return None
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT cap, sites_used FROM netlify_domains WHERE domain = ?", (dom,)
        ).fetchone()
        if not row:
            return None
        new_cap = max(1, int(cap)) if cap is not None else int(row["cap"])
        if remaining is not None:
            rem = max(0, int(remaining))
            used = max(0, new_cap - rem)
        else:
            used = max(0, int(row["sites_used"] or 0))
            if used > new_cap:
                used = new_cap
        conn.execute(
            "UPDATE netlify_domains SET cap = ?, sites_used = ? WHERE domain = ?",
            (new_cap, used, dom),
        )
    return get_netlify_domain(dom)


def netlify_set_dns_ok(domain: str, ok: bool) -> None:
    """Marca se os nameservers do domínio já propagaram (libera para runs)."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE netlify_domains SET dns_ok = ? WHERE domain = ?",
            (1 if ok else 0, dom),
        )


def netlify_set_site_id(domain: str, site_id: str) -> None:
    """Salva o id do site Netlify (único por domínio, modelo wildcard)."""
    dom = (domain or "").strip().lower().strip(".")
    sid = (site_id or "").strip()
    if not dom or not sid:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE netlify_domains SET site_id = ? WHERE domain = ?", (sid, dom)
        )


def netlify_upsert_site_entry(
    domain: str, slug: str, cnpj: str, fbtoken: str, fqdn: str = ""
) -> bool:
    """Insere/atualiza 1 CNPJ no mapa do domínio. Retorna True se era NOVO slug."""
    dom = (domain or "").strip().lower().strip(".")
    sl = (slug or "").strip().lower()
    if not dom or not sl:
        return False
    init_db()
    now = _now()
    with _conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM netlify_sites WHERE domain = ? AND slug = ?", (dom, sl)
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO netlify_sites (domain, slug, cnpj, fbtoken, fqdn, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain, slug) DO UPDATE SET "
            "cnpj = excluded.cnpj, fbtoken = excluded.fbtoken, "
            "fqdn = excluded.fqdn, updated_at = excluded.updated_at",
            (dom, sl, (cnpj or "").strip(), (fbtoken or "").strip(),
             (fqdn or "").strip(), now),
        )
        if not existed:
            conn.execute(
                "UPDATE netlify_domains SET sites_used = sites_used + 1 WHERE domain = ?",
                (dom,),
            )
    return not existed


def netlify_list_site_entries(domain: str) -> list[dict[str, Any]]:
    """Mapa de CNPJs do domínio: lista de {slug, cnpj, token, fqdn}."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return []
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT slug, cnpj, fbtoken, fqdn FROM netlify_sites "
            "WHERE domain = ? ORDER BY slug", (dom,)
        ).fetchall()
    return [
        {"slug": r["slug"], "cnpj": r["cnpj"] or "",
         "token": r["fbtoken"] or "", "fqdn": r["fqdn"] or ""}
        for r in rows
    ]


def netlify_delete_site_entry(domain: str, slug: str) -> None:
    """Remove 1 CNPJ (slug) do mapa do domínio."""
    dom = (domain or "").strip().lower().strip(".")
    sl = (slug or "").strip().lower()
    if not dom or not sl:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "DELETE FROM netlify_sites WHERE domain = ? AND slug = ?", (dom, sl)
        )


def netlify_count_sites(domain: str) -> int:
    """Quantos CNPJs (slugs) já publicados no domínio."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return 0
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM netlify_sites WHERE domain = ?", (dom,)
        ).fetchone()
    return int(row["n"]) if row else 0


def get_active_netlify_domain() -> Optional[dict[str, Any]]:
    """Primeiro domínio com cota livre (ou None).

    Ordem de criação (FIFO): quando o 1º domínio enche (sites_used >= cap), o
    próximo passa a ser usado automaticamente. NÃO exige mais o flag dns_ok
    (que ficava travado em False) — a prontidão real (NS/zona) é checada AO VIVO
    no run_service (validate_netlify_ready), que só bloqueia se os NS apontarem
    comprovadamente para fora da Netlify."""
    for d in list_netlify_domains():
        if d["remaining"] > 0:
            return d
    return None


def get_netlify_domains_for_runs(num_runs: int) -> list[dict[str, Any]]:
    """Distribui ``num_runs`` entre os domínios com cota livre (FIFO).

    Cada item: {domain, api_token, slots_to_use, remaining}. A prontidão de NS é
    validada ao vivo no run_service, não aqui (evita travar por flag desatualizado)."""
    if num_runs <= 0:
        return []
    result = []
    remaining_runs = num_runs
    for d in list_netlify_domains():
        if remaining_runs <= 0:
            break
        if d["remaining"] <= 0:
            continue
        slots = min(d["remaining"], remaining_runs)
        result.append({
            "domain": d["domain"],
            "api_token": d["api_token"],
            "slots_to_use": slots,
            "remaining": d["remaining"],
        })
        remaining_runs -= slots
    return result


# ---------------------------------------------------------------------------
# proxies — pool de IPs por conta (mascara o IP; evita soft-block)
# ---------------------------------------------------------------------------
def add_proxies(proxies: list[dict[str, Any]]) -> int:
    """Adiciona proxies ao pool (permite duplicatas). Retorna quantas entraram.
    Cada item: {label, scheme, host, port, username, password}."""
    init_db()
    now = _now()
    added = 0
    with _conn() as conn:
        for p in proxies:
            try:
                label = (p.get("label") or "").strip()
                host = (p.get("host") or "").strip()
                port = p.get("port")
                if not (label and host and port):
                    continue
                scheme = (p.get("scheme") or "http").lower()
                if scheme not in {"http", "https", "socks5"}:
                    scheme = "http"
                conn.execute(
                    "INSERT INTO proxies (label, scheme, host, port, username, "
                    "password, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (label, scheme, host, int(port),
                     p.get("username"), p.get("password"), POOL_AVAILABLE, now, now),
                )
                added += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"add_proxies: erro ao adicionar {p}: {e}")
                continue
    return added


def list_proxies(*, status: Optional[str] = None, limit: int = 1000) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM proxies WHERE status = ? ORDER BY id LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proxies ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def _proxy_is_br(username: Optional[str]) -> bool:
    """Proxy com saída geográfica no Brasil? (webshare: username 'huqcplys-BR-<n>').

    IPs estrangeiros (worldwide, sem '-BR-') fazem o Facebook servir o 2FA em
    idioma estrangeiro + soft-block silencioso (login_bounced) — diagnosticado
    nas runs 2/4/10 de jun/2026 (a tela 2FA caiu no 'fallback agnóstico de
    idioma'). Por isso preferimos SEMPRE saída BR."""
    return "-br-" in (username or "").lower()


def lease_proxy(
    cnpj: str, exclude_usernames: Optional[set[str]] = None
) -> Optional[dict[str, Any]]:
    """Devolve um proxy para o CNPJ — STICKY: o mesmo CNPJ sempre pega o MESMO
    IP (consistência anti-detect; sobrevive ao 2º passe). Marca ``used`` e o
    vincula ao CNPJ. Atômico sob o lock do SQLite — runs paralelas não pegam o
    mesmo IP. ``None`` se o pool estiver vazio/esgotado.

    PREFERE saída BR (ver :func:`_proxy_is_br`): se o pool tem proxies BR, só
    entrega BR; e se um CNPJ estava preso (sticky) a um IP estrangeiro de um lote
    anterior, RE-LEASEIA um BR (corrige a causa nº1 de login_bounced). Só cai em
    não-BR quando NÃO há nenhum BR disponível (pools não-webshare/datacenter).

    ``exclude_usernames``: usernames (IPs) a NÃO entregar — usado na rotação de IP
    pós login_bounced, p/ pegar OUTRO IP que não os que já bouncearam neste run.
    No Webshare todos os IPs compartilham host:port; o username é a identidade do
    IP, então a exclusão é por username."""
    c = (cnpj or "").strip()
    excl = {u for u in (exclude_usernames or set()) if u}
    init_db()
    with _POOL_LOCK, _conn() as conn:
        if excl:
            ec = " AND username NOT IN (" + ",".join("?" * len(excl)) + ")"
            ep = list(excl)
        else:
            ec, ep = "", []
        # Há BR livre (fora os excluídos)? Decide se forçamos BR ou aceitamos qualquer.
        br_available = conn.execute(
            "SELECT 1 FROM proxies WHERE status = ? AND lower(username) LIKE '%-br-%'"
            + ec + " LIMIT 1",
            (POOL_AVAILABLE, *ep),
        ).fetchone() is not None
        if c:
            row = conn.execute(
                "SELECT * FROM proxies WHERE used_by_cnpj = ? LIMIT 1", (c,)
            ).fetchone()
            if row:
                # Reusa o IP sticky — EXCETO se estiver na blocklist deste run
                # (rotação pós-bounce), ou se for estrangeiro e houver BR livre:
                # aí solta e pega um BR abaixo (corrige CNPJs presos a IP worldwide
                # de lotes antigos → 2FA estrangeiro + soft-block).
                if (row["username"] not in excl
                        and (_proxy_is_br(row["username"]) or not br_available)):
                    return dict(row)  # já tinha um IP (BR, ou não há BR) — reusa
                conn.execute(
                    "UPDATE proxies SET status = ?, used_by_cnpj = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (POOL_AVAILABLE, _now(), row["id"]),
                )
        # Novo lease: BR primeiro; só não-BR se não houver nenhum BR disponível.
        row = None
        if br_available:
            row = conn.execute(
                "SELECT * FROM proxies WHERE status = ? AND lower(username) LIKE '%-br-%'"
                + ec + " ORDER BY id LIMIT 1",
                (POOL_AVAILABLE, *ep),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM proxies WHERE status = ?" + ec + " ORDER BY id LIMIT 1",
                (POOL_AVAILABLE, *ep),
            ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE proxies SET status = ?, used_by_cnpj = ?, updated_at = ? "
            "WHERE id = ?",
            (POOL_USED, c or None, _now(), row["id"]),
        )
        d = dict(row)
        d["status"] = POOL_USED
        d["used_by_cnpj"] = c or None
        return d


def discard_proxy(username: Optional[str]) -> None:
    """Marca um proxy (IP) como DESCARTADO — não volta ao pool e nunca mais é
    leaseado. 'ips que não passarem descarta' (operador 2026-06): o username é a
    identidade do IP no Webshare. Limpa o vínculo sticky junto. No-op se vazio."""
    u = (username or "").strip()
    if not u:
        return
    init_db()
    with _POOL_LOCK, _conn() as conn:
        conn.execute(
            "UPDATE proxies SET status = ?, used_by_cnpj = NULL, updated_at = ? "
            "WHERE username = ?",
            (POOL_DISCARDED, _now(), u),
        )


def release_proxy(cnpj: str) -> None:
    """Libera o(s) proxy(s) vinculado(s) ao CNPJ de volta ao pool (available)."""
    c = (cnpj or "").strip()
    if not c:
        return
    init_db()
    with _POOL_LOCK, _conn() as conn:
        conn.execute(
            "UPDATE proxies SET status = ?, used_by_cnpj = NULL, updated_at = ? "
            "WHERE used_by_cnpj = ?",
            (POOL_AVAILABLE, _now(), c),
        )


# ---------------------------------------------------------------------------
# Exclusão manual do pool (painel: selecionar e apagar — CNPJs/contas/proxies)
# ---------------------------------------------------------------------------
def delete_cnpjs(cnpjs: list[str]) -> int:
    """Apaga CNPJs do pool por valor (qualquer status). Retorna quantos
    foram removidos de fato — itens já ausentes/duplicados não contam."""
    digits = {("".join(ch for ch in (c or "") if ch.isdigit())) for c in cnpjs}
    digits.discard("")
    if not digits:
        return 0
    init_db()
    with _POOL_LOCK, _conn() as conn:
        ph = ",".join("?" * len(digits))
        cur = conn.execute(f"DELETE FROM cnpjs WHERE cnpj IN ({ph})", tuple(digits))
        return cur.rowcount or 0


def delete_fb_accounts(emails: list[str]) -> int:
    """Apaga contas FB do pool por e-mail (qualquer status). Retorna quantas
    foram removidas de fato."""
    vals = {(e or "").strip().lower() for e in emails}
    vals.discard("")
    if not vals:
        return 0
    init_db()
    with _POOL_LOCK, _conn() as conn:
        logger.info(f"[DELETE_ACCOUNTS] Tentando deletar {len(vals)} conta(s): {list(vals)[:5]}")
        ph = ",".join("?" * len(vals))
        cur = conn.execute(
            f"DELETE FROM fb_accounts WHERE lower(email) IN ({ph})", tuple(vals)
        )
        deleted = cur.rowcount or 0
        logger.info(f"[DELETE_ACCOUNTS] Resultado: {deleted} conta(s) deletada(s)")
        return deleted


def delete_proxies(labels: list[str]) -> int:
    """Apaga proxies do pool por ``label`` (host:porta — a chave de dedup;
    qualquer status). Retorna quantos foram removidos de fato."""
    vals = {(l or "").strip() for l in labels}
    vals.discard("")
    if not vals:
        return 0
    init_db()
    with _POOL_LOCK, _conn() as conn:
        ph = ",".join("?" * len(vals))
        cur = conn.execute(f"DELETE FROM proxies WHERE label IN ({ph})", tuple(vals))
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# cnpj_cache (consultas BrasilAPI memorizadas — evita 429 em prévia/run)
# ---------------------------------------------------------------------------
def get_cnpj_cache(cnpj: str) -> Optional[str]:
    """JSON do CNPJData cacheado (ou None). ``cnpj`` deve vir só com dígitos."""
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT data_json FROM cnpj_cache WHERE cnpj = ?", (cnpj,)
        ).fetchone()
    return row["data_json"] if row else None


def set_cnpj_cache(cnpj: str, data_json: str) -> None:
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cnpj_cache (cnpj, data_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cnpj) DO UPDATE SET data_json = excluded.data_json, "
            "fetched_at = excluded.fetched_at",
            (cnpj, data_json, now),
        )


# ---------------------------------------------------------------------------
# cloudflare_domains — domínios + conta + token + cap + uso + nameservers.
# Host ALTERNATIVO ao Netlify (mesmo modelo wildcard). Só entra em uso quando
# settings.hosting_provider = 'cloudflare'. Espelha as funções netlify_*.
# ---------------------------------------------------------------------------
def get_hosting_provider() -> str:
    """Provedor de hospedagem ativo: 'netlify' (padrão) ou 'cloudflare'."""
    v = (get_setting("hosting_provider", "netlify") or "netlify").strip().lower()
    return "cloudflare" if v == "cloudflare" else "netlify"


def set_hosting_provider(provider: str) -> None:
    v = (provider or "").strip().lower()
    set_settings({"hosting_provider": "cloudflare" if v == "cloudflare" else "netlify"})


def add_cloudflare_domain(
    domain: str,
    account_id: str,
    api_token: str,
    *,
    cap: int = 100,
    zone_id: str = "",
    nameservers=None,
    dns_ok: bool = False,
) -> None:
    """Adiciona (ou atualiza) um domínio Cloudflare. Não zera sites_used em update."""
    dom = (domain or "").strip().lower().strip(".")
    acct = (account_id or "").strip()
    tok = (api_token or "").strip()
    if not dom or not acct or not tok:
        raise ValueError("domain, account_id e api_token são obrigatórios.")
    ns_json = json.dumps(nameservers or [])
    init_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cloudflare_domains "
            "(domain, account_id, api_token, zone_id, nameservers, cap, "
            " sites_used, dns_ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET "
            "account_id = excluded.account_id, "
            "api_token = excluded.api_token, "
            "zone_id = COALESCE(NULLIF(excluded.zone_id,''), cloudflare_domains.zone_id), "
            "nameservers = excluded.nameservers, "
            "cap = excluded.cap, "
            "dns_ok = excluded.dns_ok",
            (dom, acct, tok, (zone_id or "").strip(), ns_json,
             max(1, int(cap)), 1 if dns_ok else 0, now),
        )


def list_cloudflare_domains():
    """Lista domínios Cloudflare com uso, cota restante e nameservers.

    Cota = ``sites_used`` do domínio (editável na UI), não o COUNT do mapa.
    """
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT domain, account_id, api_token, zone_id, nameservers, cap, "
            "sites_used, dns_ok, created_at FROM cloudflare_domains ORDER BY created_at"
        ).fetchall()
    result = []
    for r in rows:
        try:
            ns = json.loads(r["nameservers"] or "[]")
        except Exception:
            ns = []
        cap = int(r["cap"])
        used = max(0, int(r["sites_used"] or 0))
        result.append({
            "domain": r["domain"],
            "account_id": r["account_id"],
            "api_token": r["api_token"],
            "zone_id": r["zone_id"] or "",
            "nameservers": ns,
            "cap": cap,
            "sites_used": used,
            "remaining": max(0, cap - used),
            "dns_ok": bool(r["dns_ok"]),
            "created_at": r["created_at"],
        })
    return result


def get_cloudflare_domain(domain: str):
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return None
    for d in list_cloudflare_domains():
        if d["domain"] == dom:
            return d
    return None


def delete_cloudflare_domain(domain: str) -> None:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute("DELETE FROM cloudflare_domains WHERE domain = ?", (dom,))
        conn.execute("DELETE FROM cloudflare_domain_sites WHERE domain = ?", (dom,))


def cloudflare_domain_set_dns_ok(domain: str, ok: bool) -> None:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE cloudflare_domains SET dns_ok = ? WHERE domain = ?",
            (1 if ok else 0, dom),
        )


def cloudflare_domain_set_zone_id(domain: str, zone_id: str) -> None:
    dom = (domain or "").strip().lower().strip(".")
    zid = (zone_id or "").strip()
    if not dom or not zid:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE cloudflare_domains SET zone_id = ? WHERE domain = ?", (zid, dom)
        )


def cloudflare_domain_usage_reset(domain: str) -> None:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return
    init_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE cloudflare_domains SET sites_used = 0 WHERE domain = ?", (dom,)
        )
        conn.execute(
            "DELETE FROM cloudflare_domain_sites WHERE domain = ?", (dom,)
        )
        conn.commit()


def cloudflare_set_quota(
    domain: str, *, remaining: Optional[int] = None, cap: Optional[int] = None
):
    """Ajusta cota do domínio Cloudflare (remaining/cap). Não apaga o mapa de sites."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return None
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT cap, sites_used FROM cloudflare_domains WHERE domain = ?", (dom,)
        ).fetchone()
        if not row:
            return None
        new_cap = max(1, int(cap)) if cap is not None else int(row["cap"])
        if remaining is not None:
            rem = max(0, int(remaining))
            used = max(0, new_cap - rem)
        else:
            used = max(0, int(row["sites_used"] or 0))
            if used > new_cap:
                used = new_cap
        conn.execute(
            "UPDATE cloudflare_domains SET cap = ?, sites_used = ? WHERE domain = ?",
            (new_cap, used, dom),
        )
    return get_cloudflare_domain(dom)


def cloudflare_domain_upsert_site_entry(
    domain: str, slug: str, cnpj: str, fbtoken: str, fqdn: str = "",
    *, empresa_json: Optional[str] = None, source: Optional[str] = None,
    template: Optional[str] = None,
) -> bool:
    """Insere/atualiza 1 CNPJ no mapa do domínio. True se era slug NOVO.

    ``empresa_json`` (opcional): dados EDITÁVEIS do site já em JSON (aba Gerar
    Site) — quando presente, o worker os embute em vez de fazer lookup LIVE.
    ``source`` ('run'|'manual'): origem do site p/ os cards de Meus Sites; em
    UPDATE, só sobrescreve se um valor for passado (preserva a origem).
    ``template`` ('classic'|'professional'): tema visual do site."""
    dom = (domain or "").strip().lower().strip(".")
    sl = (slug or "").strip().lower()
    if not dom or not sl:
        return False
    init_db()
    now = _now()
    ej = empresa_json if (empresa_json and empresa_json.strip()) else None
    # 'source' é NOT NULL: a run (que não passa source) é sempre origem 'run'.
    # Só a aba Gerar Site passa 'manual'. Nunca deixar NULL (dava IntegrityError).
    src = (source or "").strip().lower() or "run"
    tmpl = (template or "").strip().lower() or "classic"
    with _conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM cloudflare_domain_sites WHERE domain = ? AND slug = ?",
            (dom, sl),
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO cloudflare_domain_sites "
            "(domain, slug, cnpj, fbtoken, fqdn, updated_at, empresa_json, source, template) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain, slug) DO UPDATE SET "
            "cnpj = excluded.cnpj, fbtoken = excluded.fbtoken, "
            "fqdn = excluded.fqdn, updated_at = excluded.updated_at, "
            "empresa_json = COALESCE(excluded.empresa_json, cloudflare_domain_sites.empresa_json), "
            "source = COALESCE(excluded.source, cloudflare_domain_sites.source), "
            "template = excluded.template",
            (dom, sl, (cnpj or "").strip(), (fbtoken or "").strip(),
             (fqdn or "").strip(), now, ej, src, tmpl),
        )
        if not existed:
            conn.execute(
                "UPDATE cloudflare_domains SET sites_used = sites_used + 1 WHERE domain = ?",
                (dom,),
            )
    return not existed


def cloudflare_domain_list_site_entries(domain: str):
    """Mapa de CNPJs do domínio: lista de {slug, cnpj, token, fqdn, empresa, source, template}."""
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return []
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT slug, cnpj, fbtoken, fqdn, empresa_json, source, template "
            "FROM cloudflare_domain_sites WHERE domain = ? ORDER BY slug", (dom,)
        ).fetchall()
    return [
        {"slug": r["slug"], "cnpj": r["cnpj"] or "",
         "token": r["fbtoken"] or "", "fqdn": r["fqdn"] or "",
         "empresa": r["empresa_json"] or "", "source": r["source"] or "run",
         "template": r["template"] or "classic"}
        for r in rows
    ]


def cloudflare_domain_list_all_sites():
    """TODOS os sites Cloudflare (qualquer domínio) p/ a aba Meus Sites.

    Devolve lista de dicts com slug, cnpj, fqdn, url, token, empresa_json,
    source, template e updated_at — mais novo primeiro."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT domain, slug, cnpj, fbtoken, fqdn, empresa_json, source, "
            "template, updated_at FROM cloudflare_domain_sites ORDER BY updated_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        fqdn = r["fqdn"] or f"{r['slug']}.{r['domain']}"
        out.append({
            "domain": r["domain"], "slug": r["slug"], "cnpj": r["cnpj"] or "",
            "token": r["fbtoken"] or "", "fqdn": fqdn,
            "url": f"https://{fqdn}", "empresa_json": r["empresa_json"] or "",
            "source": r["source"] or "run", "template": r["template"] or "classic",
            "updated_at": r["updated_at"],
        })
    return out


def cloudflare_domain_remove_site_entry(domain: str, slug: str) -> bool:
    """Remove 1 site do mapa do domínio (decrementa o contador). True se removeu."""
    dom = (domain or "").strip().lower().strip(".")
    sl = (slug or "").strip().lower()
    if not dom or not sl:
        return False
    init_db()
    with _conn() as conn:
        n = conn.execute(
            "DELETE FROM cloudflare_domain_sites WHERE domain = ? AND slug = ?",
            (dom, sl),
        ).rowcount
        if n:
            conn.execute(
                "UPDATE cloudflare_domains SET sites_used = MAX(0, sites_used - 1) "
                "WHERE domain = ?", (dom,),
            )
    return bool(n)


def cloudflare_domain_count_sites(domain: str) -> int:
    dom = (domain or "").strip().lower().strip(".")
    if not dom:
        return 0
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cloudflare_domain_sites WHERE domain = ?", (dom,)
        ).fetchone()
    return int(row["n"]) if row else 0


def get_active_cloudflare_domain():
    """Primeiro domínio Cloudflare com cota livre (ou None)."""
    for d in list_cloudflare_domains():
        if d["remaining"] > 0:
            return d
    return None


def get_cloudflare_domains_for_runs(num_runs: int):
    """Distribui num_runs entre os domínios Cloudflare com cota livre (FIFO)."""
    if num_runs <= 0:
        return []
    result = []
    remaining_runs = num_runs
    for d in list_cloudflare_domains():
        if remaining_runs <= 0:
            break
        if d["remaining"] <= 0:
            continue
        slots = min(d["remaining"], remaining_runs)
        result.append({
            "domain": d["domain"],
            "account_id": d["account_id"],
            "api_token": d["api_token"],
            "slots_to_use": slots,
            "remaining": d["remaining"],
        })
        remaining_runs -= slots
    return result
