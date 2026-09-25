"""FastAPI app: create runs, poll status, list/export records."""

from __future__ import annotations

import csv
import io
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


class RunRequest(BaseModel):
    prompt: str


WEB_INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"


@app.get("/", include_in_schema=False)
def dashboard():
    if not WEB_INDEX.exists():
        raise HTTPException(404, f"dashboard file not found at {WEB_INDEX}")
    return FileResponse(WEB_INDEX)


@app.post("/api/runs")
def create_run(req: RunRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    run_id = uuid.uuid4().hex[:12]
    db.create_run(run_id, req.prompt.strip())
    thread = threading.Thread(target=run_pipeline, args=(run_id, req.prompt.strip()), daemon=True)
    thread.start()
    return {"id": run_id}


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


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    records = db.list_records(run_id)
    field_names = [f["name"] for f in (run.get("data_spec") or {}).get("fields", [])]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(field_names + ["confidence", "sources"])
    for record in records:
        row = [record["fields"].get(name, "") for name in field_names]
        sources = sorted({p.get("url") for p in record["provenance"].values() if p and p.get("url")})
        row += [record["confidence"], "; ".join(sources)]
        writer.writerow(row)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=scout_{run_id}.csv"},
    )
