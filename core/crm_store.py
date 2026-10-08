"""Persistência do CRM (leads, tags, disparos, mensagens, conexões de BM).

Compartilha o MESMO SQLite do :mod:`core.store` (reusa ``_conn``/``_now``/
``_POOL_LOCK``) — cria as tabelas do CRM sob demanda com ``CREATE TABLE IF NOT
EXISTS`` (mesma disciplina do ``_migrate`` do store, sem tocar no schema dele).

Tabelas:
  * ``crm_leads``     — contatos p/ disparo (phone único, nome, tags JSON, notas)
  * ``crm_bm_conn``   — número Cloud API escolhido por BM (phone_number_id)
  * ``crm_messages``  — log de mensagens (out/in) por lead + BM (chat + status)
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from core import store

logger = logging.getLogger("meta_automation.crm_store")

_CRM_LOCK = threading.Lock()
_crm_ready = False


def only_phone_digits(raw: str) -> str:
    """Normaliza um telefone para SÓ dígitos (E.164 sem '+')."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def normalize_br_phone(raw: str) -> str:
    """Normaliza um telefone para o formato que a Cloud API exige (E.164 sem '+').

    A Cloud API precisa do CÓDIGO DO PAÍS. Um número BR digitado sem o 55 (ex.:
    '11982120535' ou '(11) 98212-0535') falha silenciosamente no envio. Como este
    CRM é focado em BR, assumimos que um número de 10/11 dígitos é BR sem o país:
      * 10 ou 11 dígitos (DDD + número, sem país) → prefixa '55'.
      * qualquer outro tamanho (13 já com 55, ou internacional 12+) → mantém.
    Obs.: um número de OUTRO país com 11 dígitos seria tratado como BR — aceitável
    aqui porque os leads são brasileiros; números já com DDI (12+ díg.) passam
    intactos.
    """
    d = only_phone_digits(raw)
    if len(d) in (10, 11) and not d.startswith("55"):
        return "55" + d
    return d


def _ensure() -> None:
    """Cria as tabelas do CRM uma vez por processo."""
    global _crm_ready
    if _crm_ready:
        return
    store.init_db()
    with _CRM_LOCK, store._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_leads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                phone        TEXT NOT NULL UNIQUE,
                name         TEXT,
                tags         TEXT NOT NULL DEFAULT '[]',
                notes        TEXT,
                source       TEXT,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                last_contacted_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_bm_conn (
                business_id     TEXT PRIMARY KEY,
                phone_number_id TEXT,
                display_phone   TEXT,
                verified_name   TEXT,
                connected_at    INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_messages (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_phone     TEXT NOT NULL,
                business_id    TEXT,
                phone_number_id TEXT,
                direction      TEXT NOT NULL DEFAULT 'out',
                template_name  TEXT,
                body           TEXT,
                status         TEXT,
                wamid          TEXT,
                error          TEXT,
                created_at     INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_msg_lead ON crm_messages(lead_phone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_msg_created ON crm_messages(created_at)"
        )
    _crm_ready = True


def _parse_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    try:
        val = json.loads(raw or "[]")
        return [str(t).strip() for t in val if str(t).strip()]
    except (ValueError, TypeError):
        return []


def _lead_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = _parse_tags(d.get("tags"))
    return d


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
def add_leads(
    leads: list[dict[str, Any]], *, source: str = "manual"
) -> dict[str, int]:
    """Insere/atualiza leads por telefone. Cada item: {phone, name?, tags?}.

    Telefone é normalizado p/ dígitos. Se já existe, MESCLA tags e atualiza o
    nome quando vier não-vazio. Devolve {added, updated, invalid}.
    """
    _ensure()
    added = updated = invalid = 0
    now = store._now()
    with _CRM_LOCK, store._conn() as conn:
        for item in leads or []:
            phone = normalize_br_phone(str(item.get("phone") or ""))
            if len(phone) < 10:  # muito curto p/ ser telefone válido
                invalid += 1
                continue
            name = (str(item.get("name") or "").strip()) or None
            tags = _parse_tags(item.get("tags"))
            row = conn.execute(
                "SELECT id, name, tags FROM crm_leads WHERE phone = ?", (phone,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO crm_leads (phone, name, tags, source, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (phone, name, json.dumps(tags), source, now, now),
                )
                added += 1
            else:
                merged = sorted(set(_parse_tags(row["tags"]) + tags))
                conn.execute(
                    "UPDATE crm_leads SET name = COALESCE(?, name), tags = ?, "
                    "updated_at = ? WHERE id = ?",
                    (name, json.dumps(merged), now, row["id"]),
                )
                updated += 1
    return {"added": added, "updated": updated, "invalid": invalid}


def list_leads(
    *, tag: Optional[str] = None, search: Optional[str] = None, limit: int = 5000
) -> list[dict[str, Any]]:
    """Lista leads (mais recentes primeiro), opcionalmente filtrando por tag
    (LIKE no JSON) e por texto (telefone OU nome)."""
    _ensure()
    clauses: list[str] = []
    params: list[Any] = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if search:
        clauses.append("(phone LIKE ? OR name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with store._conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM crm_leads{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_lead_row(r) for r in rows]


def update_lead(
    lead_id: int,
    *,
    name: Optional[str] = None,
    tags: Optional[list[str]] = None,
    notes: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Atualiza nome/tags/notas de um lead. Campos None não mudam (tags=[] limpa)."""
    _ensure()
    now = store._now()
    with _CRM_LOCK, store._conn() as conn:
        row = conn.execute(
            "SELECT * FROM crm_leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if not row:
            return None
        new_name = name if name is not None else row["name"]
        new_tags = (
            json.dumps(sorted(set(str(t).strip() for t in tags if str(t).strip())))
            if tags is not None
            else row["tags"]
        )
        new_notes = notes if notes is not None else row["notes"]
        conn.execute(
            "UPDATE crm_leads SET name = ?, tags = ?, notes = ?, updated_at = ? "
            "WHERE id = ?",
            (new_name, new_tags, new_notes, now, lead_id),
        )
        row = conn.execute(
            "SELECT * FROM crm_leads WHERE id = ?", (lead_id,)
        ).fetchone()
    return _lead_row(row)


def delete_leads(ids: list[int]) -> int:
    """Remove leads por id. Devolve quantos foram removidos."""
    _ensure()
    if not ids:
        return 0
    with _CRM_LOCK, store._conn() as conn:
        cur = conn.execute(
            f"DELETE FROM crm_leads WHERE id IN ({','.join('?' * len(ids))})",
            [int(i) for i in ids],
        )
        return cur.rowcount


def all_tags() -> list[dict[str, Any]]:
    """Todas as tags distintas com contagem de leads (ordenado por uso)."""
    _ensure()
    counts: dict[str, int] = {}
    with store._conn() as conn:
        rows = conn.execute("SELECT tags FROM crm_leads").fetchall()
    for r in rows:
        for t in _parse_tags(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    return [
        {"tag": t, "count": c}
        for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def leads_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    _ensure()
    if not ids:
        return []
    with store._conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM crm_leads WHERE id IN ({','.join('?' * len(ids))})",
            [int(i) for i in ids],
        ).fetchall()
    return [_lead_row(r) for r in rows]


def touch_lead_contacted(phone: str) -> None:
    _ensure()
    with _CRM_LOCK, store._conn() as conn:
        conn.execute(
            "UPDATE crm_leads SET last_contacted_at = ? WHERE phone = ?",
            (store._now(), only_phone_digits(phone)),
        )


# ---------------------------------------------------------------------------
# Conexão de BM (número Cloud API escolhido)
# ---------------------------------------------------------------------------
def set_bm_connection(
    business_id: str,
    *,
    phone_number_id: Optional[str],
    display_phone: Optional[str] = None,
    verified_name: Optional[str] = None,
) -> None:
    _ensure()
    now = store._now()
    with _CRM_LOCK, store._conn() as conn:
        exists = conn.execute(
            "SELECT business_id FROM crm_bm_conn WHERE business_id = ?",
            (business_id,),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE crm_bm_conn SET phone_number_id = ?, display_phone = ?, "
                "verified_name = ?, updated_at = ? WHERE business_id = ?",
                (phone_number_id, display_phone, verified_name, now, business_id),
            )
        else:
            conn.execute(
                "INSERT INTO crm_bm_conn (business_id, phone_number_id, "
                "display_phone, verified_name, connected_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (business_id, phone_number_id, display_phone, verified_name, now, now),
            )


def get_bm_connection(business_id: str) -> Optional[dict[str, Any]]:
    _ensure()
    with store._conn() as conn:
        row = conn.execute(
            "SELECT * FROM crm_bm_conn WHERE business_id = ?", (business_id,)
        ).fetchone()
    return dict(row) if row else None


def list_bm_connections() -> dict[str, dict[str, Any]]:
    _ensure()
    with store._conn() as conn:
        rows = conn.execute("SELECT * FROM crm_bm_conn").fetchall()
    return {r["business_id"]: dict(r) for r in rows}


def bm_for_phone_number_id(phone_number_id: str) -> Optional[str]:
    """business_id da BM cujo número Cloud API é ``phone_number_id`` (webhook)."""
    _ensure()
    if not phone_number_id:
        return None
    with store._conn() as conn:
        row = conn.execute(
            "SELECT business_id FROM crm_bm_conn WHERE phone_number_id = ? LIMIT 1",
            (phone_number_id,),
        ).fetchone()
    return row["business_id"] if row else None


# ---------------------------------------------------------------------------
# Mensagens (chat + log de disparo)
# ---------------------------------------------------------------------------
def log_message(
    *,
    lead_phone: str,
    business_id: Optional[str],
    phone_number_id: Optional[str] = None,
    direction: str = "out",
    template_name: Optional[str] = None,
    body: Optional[str] = None,
    status: Optional[str] = None,
    wamid: Optional[str] = None,
    error: Optional[str] = None,
) -> int:
    _ensure()
    with _CRM_LOCK, store._conn() as conn:
        cur = conn.execute(
            "INSERT INTO crm_messages (lead_phone, business_id, phone_number_id, "
            "direction, template_name, body, status, wamid, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                only_phone_digits(lead_phone), business_id, phone_number_id,
                direction, template_name, body, status, wamid, error, store._now(),
            ),
        )
        return int(cur.lastrowid)


def update_status_by_wamid(wamid: str, status: str) -> int:
    """Atualiza o status de uma mensagem enviada pelo wamid (delivered/read/
    failed), vindo do webhook de status. Devolve nº de linhas afetadas."""
    _ensure()
    if not wamid:
        return 0
    with _CRM_LOCK, store._conn() as conn:
        cur = conn.execute(
            "UPDATE crm_messages SET status = ? WHERE wamid = ?",
            (status, wamid),
        )
        return cur.rowcount


def record_incoming(
    *, lead_phone: str, business_id: Optional[str], phone_number_id: Optional[str],
    body: Optional[str], wamid: Optional[str] = None, name: Optional[str] = None,
) -> int:
    """Registra uma mensagem RECEBIDA (webhook). Cria/atualiza o lead pelo
    telefone (para o nome do WhatsApp aparecer no chat) e loga a mensagem."""
    _ensure()
    phone = only_phone_digits(lead_phone)
    if phone:
        add_leads([{"phone": phone, "name": name, "tags": ["respondeu"]}], source="whatsapp")
    return log_message(
        lead_phone=phone, business_id=business_id, phone_number_id=phone_number_id,
        direction="in", body=body, status="received", wamid=wamid,
    )


def list_messages(lead_phone: str, *, limit: int = 500) -> list[dict[str, Any]]:
    _ensure()
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM crm_messages WHERE lead_phone = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (only_phone_digits(lead_phone), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_chats(*, limit: int = 500) -> list[dict[str, Any]]:
    """Uma linha por lead que já recebeu/enviou mensagem, com a última msg,
    contagem e o nome do lead (join em crm_leads). Mais recente primeiro."""
    _ensure()
    with store._conn() as conn:
        rows = conn.execute(
            """
            SELECT m.lead_phone,
                   COUNT(*)                AS total,
                   MAX(m.created_at)       AS last_at,
                   (SELECT body FROM crm_messages m2
                     WHERE m2.lead_phone = m.lead_phone
                     ORDER BY m2.created_at DESC LIMIT 1) AS last_body,
                   (SELECT status FROM crm_messages m3
                     WHERE m3.lead_phone = m.lead_phone
                     ORDER BY m3.created_at DESC LIMIT 1) AS last_status,
                   (SELECT name FROM crm_leads l WHERE l.phone = m.lead_phone) AS name
            FROM crm_messages m
            GROUP BY m.lead_phone
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def messages_stats() -> dict[str, int]:
    """Contagens p/ o dashboard do CRM."""
    _ensure()
    with store._conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM crm_messages").fetchone()["c"]
        sent = conn.execute(
            "SELECT COUNT(*) c FROM crm_messages WHERE direction='out' AND error IS NULL"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) c FROM crm_messages WHERE error IS NOT NULL"
        ).fetchone()["c"]
        leads = conn.execute("SELECT COUNT(*) c FROM crm_leads").fetchone()["c"]
    return {"messages": total, "sent": sent, "failed": failed, "leads": leads}
