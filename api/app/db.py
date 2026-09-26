"""SQLite storage for Scout runs, records, claims, captures, and the source ledger.

Schema notes
------------
- `records` is the display layer: one row per resolved entity, merged fields + provenance JSON.
- `claims` is the audit layer: every individual (value, source, quote) triple, kept forever.
- `captures` logs each search occasion (query × engine × source_type) for the Chao2 estimator.
- `human_labels` holds spot-check labels used by the PPI accuracy audit.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL,
    data_spec   TEXT,
    stats       TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    entity_key   TEXT NOT NULL,
    fields_sourced REAL NOT NULL,
    fields       TEXT NOT NULL,
    provenance   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    query       TEXT,
    engine      TEXT,
    source_type TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    field         TEXT NOT NULL,
    value_raw     TEXT,
    value_norm    TEXT,
    source_url    TEXT NOT NULL,
    quote         TEXT,
    support_score REAL,
    capture_id    TEXT NOT NULL,
    extractor     TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_labels (
    claim_id   TEXT PRIMARY KEY,
    correct    INTEGER,
    labeler    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    url            TEXT NOT NULL,
    domain         TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT,
    records_found  INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _local.conn = conn
    return _local.conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Normalization helper (used by corroboration comparison and claims)
# ---------------------------------------------------------------------------

def _norm(v) -> str:
    """Whitespace-collapsed, lowercased string for value comparison."""
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Records (display layer — one merged row per entity)
# ---------------------------------------------------------------------------

def upsert_record(
    run_id: str,
    entity_key: str,
    fields: dict,
    provenance: dict,
    fields_sourced: float,
) -> bool:
    """Insert a new record, or merge into an existing one with the same entity_key.

    Corroboration logic (fixed):
    - A second source agrees  → mark corroborated_by (✓✓)
    - A second source disagrees → record conflicts_with and keep the first value
      until truth discovery resolves it.

    Returns True if a new record was created.
    """
    conn = get_conn()
    existing = conn.execute(
        "SELECT id, fields, provenance, fields_sourced FROM records "
        "WHERE run_id = ? AND entity_key = ?",
        (run_id, entity_key),
    ).fetchone()

    if existing is None:
        rec_id = f"{run_id}:{entity_key}"[:120]
        conn.execute(
            "INSERT INTO records "
            "(id, run_id, entity_key, fields_sourced, fields, provenance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec_id, run_id, entity_key, fields_sourced,
             json.dumps(fields), json.dumps(provenance), now()),
        )
        conn.commit()
        return True

    merged_fields = json.loads(existing["fields"])
    merged_prov   = json.loads(existing["provenance"])

    for key, value in fields.items():
        if value in (None, ""):
            continue

        prior_val = merged_fields.get(key)
        prior_prov = merged_prov.get(key) or {}
        new_prov = dict(provenance.get(key, {}))

        # --- No prior value: just write ---
        if prior_val in (None, ""):
            merged_fields[key] = value
            merged_prov[key]   = new_prov
            continue

        # --- Prior value exists from a different URL ---
        if prior_prov.get("url") and prior_prov.get("url") != new_prov.get("url"):
            if _norm(prior_val) == _norm(value):
                # Real agreement between sources → corroborate
                new_prov["corroborated_by"] = prior_prov["url"]
                merged_fields[key] = value
                merged_prov[key]   = new_prov
            else:
                # Disagreement → flag conflict, keep the earlier (more trusted) value
                # Truth discovery in truth.py will resolve this later.
                existing_conflicts = prior_prov.get("conflicts", [])
                existing_conflicts.append({"url": new_prov.get("url"), "value": value})
                prior_prov["conflicts"] = existing_conflicts
                merged_prov[key] = prior_prov
                # Do NOT overwrite merged_fields[key] — keep the first value for now.
        # else: same URL re-submitting — ignore silently

    new_fields_sourced = max(fields_sourced, existing["fields_sourced"])
    conn.execute(
        "UPDATE records SET fields = ?, provenance = ?, fields_sourced = ? WHERE id = ?",
        (json.dumps(merged_fields), json.dumps(merged_prov),
         new_fields_sourced, existing["id"]),
    )
    conn.commit()
    return False


def list_records(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM records WHERE run_id = ? ORDER BY created_at", (run_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fields"]     = json.loads(d["fields"])
        d["provenance"] = json.loads(d["provenance"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Captures (one row per search occasion: query × engine × source_type)
# ---------------------------------------------------------------------------

def log_capture(run_id: str, query: str, engine: str, source_type: str = "web") -> str:
    """Insert a capture occasion and return its ID."""
    cap_id = uuid.uuid4().hex[:16]
    conn = get_conn()
    conn.execute(
        "INSERT INTO captures (id, run_id, query, engine, source_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cap_id, run_id, query, engine, source_type, now()),
    )
    conn.commit()
    return cap_id


def list_captures(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM captures WHERE run_id = ? ORDER BY created_at", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Claims (audit layer — every individual value + source + quote triple)
# ---------------------------------------------------------------------------

def insert_claim(
    run_id: str,
    entity_id: str,
    field: str,
    value_raw: str | None,
    value_norm: str | None,
    source_url: str,
    quote: str | None,
    support_score: float | None,
    capture_id: str,
    extractor: str,
) -> str:
    """Insert one claim and return its ID."""
    claim_id = uuid.uuid4().hex
    conn = get_conn()
    conn.execute(
        "INSERT INTO claims "
        "(id, run_id, entity_id, field, value_raw, value_norm, source_url, quote, "
        " support_score, capture_id, extractor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (claim_id, run_id, entity_id, field, value_raw, value_norm,
         source_url, quote, support_score, capture_id, extractor, now()),
    )
    conn.commit()
    return claim_id


def list_claims(run_id: str, entity_id: str | None = None, field: str | None = None) -> list[dict]:
    conn = get_conn()
    q = "SELECT * FROM claims WHERE run_id = ?"
    params: list = [run_id]
    if entity_id:
        q += " AND entity_id = ?"
        params.append(entity_id)
    if field:
        q += " AND field = ?"
        params.append(field)
    q += " ORDER BY created_at"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


# ---------------------------------------------------------------------------
# Human labels (for PPI accuracy audit)
# ---------------------------------------------------------------------------

def upsert_human_label(claim_id: str, correct: bool, labeler: str = "human") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO human_labels (claim_id, correct, labeler, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(claim_id) DO UPDATE SET correct=excluded.correct, labeler=excluded.labeler",
        (claim_id, int(correct), labeler, now()),
    )
    conn.commit()


def list_human_labels(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT hl.* FROM human_labels hl "
        "JOIN claims c ON c.id = hl.claim_id "
        "WHERE c.run_id = ? ORDER BY hl.created_at",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sources / ledger
# ---------------------------------------------------------------------------

def log_source(
    run_id: str, url: str, domain: str, status: str,
    reason: str = "", records_found: int = 0,
    capture_id: str = "",
) -> None:
    """Log a source fetch attempt.

    The same URL can be visited in multiple capture occasions (different queries
    both find the same page).  We use capture_id in the PK so each visit gets
    its own row, but fall back to a timestamp suffix when capture_id is empty.
    """
    import uuid as _uuid
    suffix = capture_id or _uuid.uuid4().hex[:8]
    row_id = f"{run_id}:{suffix}:{url}"[:220]
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sources "
        "(id, run_id, url, domain, status, reason, records_found, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, run_id, url, domain, status, reason, records_found, now()),
    )
    conn.commit()


def list_sources(run_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sources WHERE run_id = ? ORDER BY created_at", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]
