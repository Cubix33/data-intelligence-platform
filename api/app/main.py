"""FastAPI app: create runs, stream progress over SSE, poll status, list/export records,
submit human labels for the PPI accuracy audit.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import db
from .pipeline import run_pipeline

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Scout - AI Data Intelligence Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"


# ---------------------------------------------------------------------------
# SSE progress bus
# A simple in-process pub/sub: pipeline threads push events, SSE streams pull them.
# ---------------------------------------------------------------------------

_sse_queues: dict[str, list[asyncio.Queue]] = {}   # run_id -> list of subscriber queues
_sse_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Cancellation flags
# A simple thread-safe set of run IDs that have been requested to cancel.
# The pipeline loop checks this and exits early.
# ---------------------------------------------------------------------------

_cancelled_runs: set[str] = set()
_cancelled_lock = threading.Lock()


def is_run_cancelled(run_id: str) -> bool:
    with _cancelled_lock:
        return run_id in _cancelled_runs


def _request_cancel(run_id: str) -> None:
    with _cancelled_lock:
        _cancelled_runs.add(run_id)


def publish_event(run_id: str, event_type: str, data: dict) -> None:
    """Called from background pipeline thread to broadcast an SSE event."""
    payload = json.dumps({"type": event_type, **data})
    with _sse_lock:
        queues = _sse_queues.get(run_id, [])
    for q in queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow consumer — skip rather than block the pipeline


def _subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    with _sse_lock:
        _sse_queues.setdefault(run_id, []).append(q)
    return q


def _unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    with _sse_lock:
        queues = _sse_queues.get(run_id, [])
        if q in queues:
            queues.remove(q)


# Inject publish_event into the pipeline module so it can emit SSE events
# without a circular import.
from . import pipeline as _pipeline_mod  # noqa: E402
_pipeline_mod._publish_event = publish_event  # type: ignore[attr-defined]
_pipeline_mod._is_run_cancelled = is_run_cancelled  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard():
    if not WEB_INDEX.exists():
        raise HTTPException(404, f"dashboard not found at {WEB_INDEX}")
    return FileResponse(WEB_INDEX)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    prompt: str
    target_coverage: float = 0.80


@app.post("/api/runs")
def create_run(req: RunRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    run_id = uuid.uuid4().hex[:12]
    db.create_run(run_id, req.prompt.strip())
    thread = threading.Thread(
        target=run_pipeline,
        args=(run_id, req.prompt.strip()),
        daemon=True,
    )
    thread.start()
    return {"id": run_id}


@app.delete("/api/runs/{run_id}")
def cancel_run(run_id: str):
    """Request cancellation of an active run.

    Sets a flag the pipeline loop checks on each capture iteration.
    Returns immediately; the run status transitions to 'cancelled'
    asynchronously once the pipeline sees the flag.
    """
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run["status"] in ("done", "failed", "cancelled"):
        return {"ok": True, "status": run["status"]}
    _request_cancel(run_id)
    return {"ok": True, "status": "cancelling"}


@app.get("/api/runs")
def list_runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    run["records"] = db.list_records(run_id)
    run["sources"] = db.list_sources(run_id)
    return run


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """Server-Sent Events stream for a run.

    Event types emitted:
      - status_change   { status: str }
      - capture_start   { capture_id, query, engine, source_type }
      - record_found    { entity_key, is_new: bool, fields_sourced: float }
      - coverage        { s_obs, s_hat, coverage_est, coverage_lo, T }
      - done            { stats }
      - error           { message }
    """
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    q = _subscribe(run_id)

    async def event_stream():
        try:
            # Immediately send current state for clients connecting mid-run
            current = db.get_run(run_id)
            if current:
                yield f"data: {json.dumps({'type': 'status_change', 'status': current['status']})}\n\n"

            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {payload}\n\n"
                    data = json.loads(payload)
                    if data.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            _unsubscribe(run_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Claims and human labels (PPI audit)
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/claims")
def list_claims(run_id: str, entity_id: str | None = None, field: str | None = None):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return db.list_claims(run_id, entity_id=entity_id, field=field)


class LabelRequest(BaseModel):
    claim_id: str
    correct: bool
    labeler: str = "human"


@app.post("/api/labels")
def submit_label(req: LabelRequest):
    db.upsert_human_label(req.claim_id, req.correct, req.labeler)
    return {"ok": True}


@app.get("/api/runs/{run_id}/labels")
def get_labels(run_id: str):
    return db.list_human_labels(run_id)


# PPI accuracy estimate endpoint
@app.get("/api/runs/{run_id}/accuracy")
def get_accuracy(run_id: str):
    """Return the current PPI accuracy estimate for a run."""
    try:
        from .ppi import ppi_accuracy_for_run
        return ppi_accuracy_for_run(run_id)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    records    = db.list_records(run_id)
    field_names = [f["name"] for f in (run.get("data_spec") or {}).get("fields", [])]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(field_names + ["fields_sourced", "sources", "has_conflict"])

    for record in records:
        row = [record["fields"].get(name, "") for name in field_names]
        prov = record["provenance"]
        sources = sorted({p.get("url") for p in prov.values() if p and p.get("url")})
        has_conflict = any(p.get("conflicts") for p in prov.values() if p)
        row += [record["fields_sourced"], "; ".join(sources), "yes" if has_conflict else ""]
        writer.writerow(row)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=scout_{run_id}.csv"},
    )
