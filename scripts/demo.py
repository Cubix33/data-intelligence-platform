#!/usr/bin/env python
"""Run the Scout pipeline end-to-end from the terminal — no server needed.

Usage:
    python scripts/demo.py "Find companies that sponsored student hackathons in India in 2025"

Requires GROQ_API_KEY to be set (in the environment or in a .env file at the repo root).
"""
from __future__ import annotations

import csv
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app import db  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    prompt = sys.argv[1]

    run_id = uuid.uuid4().hex[:8]
    db.create_run(run_id, prompt)
    print(f"Run {run_id}: {prompt}\n")

    start = time.time()
    run_pipeline(run_id, prompt)
    elapsed = time.time() - start

    run = db.get_run(run_id)
    if run["status"] != "done":
        print(f"Run failed: {run.get('error')}")
        raise SystemExit(1)

    dataspec = run["data_spec"]
    field_names = [f["name"] for f in dataspec["fields"]]
    records = db.list_records(run_id)

    print(f"Entity: {dataspec['entity']}  ({dataspec['summary']})")
    print(f"Columns: {', '.join(field_names)}")
    print(f"Stats: {run['stats']}")
    print(f"Elapsed: {elapsed:.1f}s\n")

    widths = [max(len(name), 14) for name in field_names]
    header = " | ".join(name.ljust(w) for name, w in zip(field_names, widths))
    print(header)
    print("-" * len(header))
    for record in records:
        cells = [
            str(record["fields"].get(name, "") or "")[: widths[i]].ljust(widths[i])
            for i, name in enumerate(field_names)
        ]
        print(" | ".join(cells) + f"  (confidence {record['confidence']})")

    if not records:
        print("(no records found — try a broader prompt, or check the source log below)")

    print("\nSources:")
    for source in db.list_sources(run_id):
        print(f"  [{source['status']:8s}] {source['url']}  {source['reason'] or ''}")

    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"scout_{run_id}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(field_names + ["confidence"])
        for record in records:
            writer.writerow([record["fields"].get(name, "") for name in field_names] + [record["confidence"]])
    print(f"\nSaved: {csv_path}")


if __name__ == "__main__":
    main()
