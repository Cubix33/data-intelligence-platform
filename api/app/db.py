"""SQLite storage for first-draft runs, records and the source ledger.

First draft deliberately uses one JSON-valued `fields`/`provenance` column per record
instead of the fully normalized FieldValue table sketched in IDEATION.md — same idea
(cell-level provenance), less plumbing, easy to migrate to Postgres + a real FieldValue
table later.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    data_spec TEXT,
    stats TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    confidence REAL NOT NULL,
    fields TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    records_found INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _local.conn = conn
    return _local.conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(run_id: str, prompt: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO runs (id, prompt, status, created_at) VALUES (?, ?, 'queued', ?)",
        (run_id, prompt, now()),
    )
    conn.commit()


def update_run(run_id: str, **fields) -> None:
    conn = get_conn()
    sets, values = [], []
    for key, value in fields.items():
        sets.append(f"{key} = ?")
        values.append(json.dumps(value) if key in ("data_spec", "stats") else value)
    values.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def get_run(run_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("data_spec"):
        d["data_spec"] = json.loads(d["data_spec"])
    if d.get("stats"):
        d["stats"] = json.loads(d["stats"])
    return d


def list_runs() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, prompt, status, created_at, finished_at FROM runs ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_record(run_id: str, entity_key: str, fields: dict, provenance: dict, confidence: float) -> bool:
    """Insert a new record, or merge into an existing one with the same entity_key.

    Returns True if a new record was created (used to count fresh finds per source).
    """
    conn = get_conn()
    existing = conn.execute(
        "SELECT id, fields, provenance, confidence FROM records WHERE run_id = ? AND entity_key = ?",
        (run_id, entity_key),
    ).fetchone()

    if existing is None:
        rec_id = f"{run_id}:{entity_key}"[:120]
        conn.execute(
            "INSERT INTO records (id, run_id, entity_key, confidence, fields, provenance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec_id, run_id, entity_key, confidence, json.dumps(fields), json.dumps(provenance), now()),
        )
        conn.commit()
        return True

    merged_fields = json.loads(existing["fields"])
    merged_prov = json.loads(existing["provenance"])
    for key, value in fields.items():
        if value in (None, ""):
            continue
        old_conf = merged_prov.get(key, {}).get("confidence", 0)
        new_conf = provenance.get(key, {}).get("confidence", 0)
        if merged_fields.get(key) in (None, "") or new_conf >= old_conf:
            prov = dict(provenance.get(key, {}))
            prior = merged_prov.get(key)
            if prior and prior.get("url") and prior.get("url") != prov.get("url"):
                prov["corroborated_by"] = prior["url"]
            merged_fields[key] = value
            merged_prov[key] = prov

    new_confidence = max(confidence, existing["confidence"])
    conn.execute(
        "UPDATE records SET fields = ?, provenance = ?, confidence = ? WHERE id = ?",
        (json.dumps(merged_fields), json.dumps(merged_prov), new_confidence, existing["id"]),
    )
    conn.commit()
    return False


def list_records(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM records WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fields"] = json.loads(d["fields"])
        d["provenance"] = json.loads(d["provenance"])
        out.append(d)
    return out


def log_source(run_id: str, url: str, domain: str, status: str, reason: str = "", records_found: int = 0) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO sources (id, run_id, url, domain, status, reason, records_found, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{run_id}:{url}"[:180], run_id, url, domain, status, reason, records_found, now()),
    )
    conn.commit()


def list_sources(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sources WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
    return [dict(r) for r in rows]
