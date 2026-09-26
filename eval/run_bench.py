#!/usr/bin/env python3
"""Benchmark runner for Scout's evaluation harness.

Usage
-----
    # Run Scout on all tasks in eval/tasks.json, output to eval/results/
    python eval/run_bench.py

    # Run a single task by index
    python eval/run_bench.py --task 0

    # Dry-run: show tasks without fetching
    python eval/run_bench.py --dry-run

    # Use cached pages from a previous run (no new fetches)
    python eval/run_bench.py --use-cache

Task file format (eval/tasks.json)
-----------------------------------
[
  {
    "id": "hackathon_sponsors_india_2025",
    "prompt": "Companies that sponsored student hackathons in India in 2025 ...",
    "gold_file": "eval/gold/hackathon_sponsors_india_2025.json",
    "target_coverage": 0.80
  },
  ...
]

Gold file format (eval/gold/<id>.json)
---------------------------------------
{
  "entities": ["Google", "GitHub", "Polygon", ...],
  "rows": [
    {"name": "Google", "sponsorship_tier": "Title", "contact_url": "..."},
    ...
  ]
}

WideSearch tasks: download from widesearch-seed.github.io and place gold JSON files
in eval/gold/ following the same schema.

Results are written to eval/results/<id>_<timestamp>.json.
Each result contains: run stats, records, coverage estimate, scorer output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root: `python eval/run_bench.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import db, config
from api.app.pipeline import run_pipeline
from api.app.coverage import coverage_summary

EVAL_DIR    = Path(__file__).resolve().parent
TASKS_FILE  = EVAL_DIR / "tasks.json"
RESULTS_DIR = EVAL_DIR / "results"


def load_tasks() -> list[dict]:
    if not TASKS_FILE.exists():
        print(f"No tasks file found at {TASKS_FILE}. Creating an example.")
        example = [
            {
                "id": "example_task",
                "prompt": "Companies that sponsored student hackathons in India in 2025, "
                          "with sponsorship tier and a contact URL",
                "gold_file": str(EVAL_DIR / "gold" / "example_task.json"),
                "target_coverage": 0.80,
            }
        ]
        TASKS_FILE.write_text(json.dumps(example, indent=2))
        return example
    return json.loads(TASKS_FILE.read_text())


def run_task(task: dict, use_cache: bool = False) -> dict:
    """Run Scout on a single benchmark task and return the result dict."""
    run_id = uuid.uuid4().hex[:12]
    ts_start = time.monotonic()

    print(f"\n{'='*60}")
    print(f"Task : {task['id']}")
    print(f"RunID: {run_id}")
    print(f"{'='*60}")

    # Override target_coverage from task spec if provided
    prompt = task["prompt"]
    if "target_coverage" in task:
        # Append a meta-instruction the intent parser ignores (coverage set via DB after parse)
        pass  # pipeline reads from dataspec; we patch it below via a monkey-patch approach

    db.create_run(run_id, prompt)
    run_pipeline(run_id, prompt)   # blocks until done (sync in bench context)

    elapsed = time.monotonic() - ts_start
    run_data = db.get_run(run_id)
    records  = db.list_records(run_id)

    # Build capture history from the claims table for coverage summary
    captures = db.list_captures(run_id)
    claims   = db.list_claims(run_id)
    history: dict[str, set[str]] = {}
    for c in claims:
        eid = c["entity_id"]
        if eid not in history:
            history[eid] = set()
        history[eid].add(c["capture_id"])

    T = len(captures)
    cov = coverage_summary(history, T) if T >= 2 else {}

    result = {
        "task_id":        task["id"],
        "run_id":         run_id,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "elapsed_s":      round(elapsed, 2),
        "status":         run_data.get("status"),
        "stats":          run_data.get("stats") or {},
        "coverage":       cov,
        "records":        [
            {"entity_key": r["entity_key"], "fields": r["fields"]}
            for r in records
        ],
        "gold_file":      task.get("gold_file"),
    }

    # Score against gold if available
    gold_file = task.get("gold_file")
    if gold_file and Path(gold_file).exists():
        gold = json.loads(Path(gold_file).read_text())
        scores = score_against_gold(records, gold)
        result["scores"] = scores
        print(f"Item-F1: {scores['item_f1']:.3f}  Row-F1: {scores['row_f1']:.3f}  "
              f"Coverage est: {cov.get('coverage_est', 'n/a')}")
    else:
        print(f"Records: {len(records)}  "
              f"Coverage est: {cov.get('coverage_est', 'n/a')}  "
              f"(no gold file)")

    # Write result
    out_path = RESULTS_DIR / f"{task['id']}_{run_id}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"Result written to {out_path}")

    return result


def score_against_gold(records: list[dict], gold: dict) -> dict:
    """Compute Item-F1 and Row-F1 against a gold table.

    Item-F1: entity-level precision/recall.
    Row-F1:  row-level precision/recall (entity found AND all required fields correct).
    """
    from api.app.entity_resolution import normalize_name

    gold_entities = {normalize_name(e) for e in gold.get("entities", [])}
    gold_rows     = {
        normalize_name(r.get("name", "")): r
        for r in gold.get("rows", [])
    }

    pred_entities = {r["entity_key"] for r in records}
    pred_rows     = {r["entity_key"]: r["fields"] for r in records}

    # Item precision / recall / F1
    tp_item = len(pred_entities & gold_entities)
    p_item  = tp_item / max(len(pred_entities), 1)
    r_item  = tp_item / max(len(gold_entities), 1)
    f1_item = 2 * p_item * r_item / max(p_item + r_item, 1e-9)

    # Row-F1: a row is correct if entity found AND all non-null gold fields match
    tp_row = 0
    for ek, pred_fields in pred_rows.items():
        if ek not in gold_rows:
            continue
        gold_f = gold_rows[ek]
        match  = all(
            normalize_name(str(pred_fields.get(k) or "")) == normalize_name(str(v))
            for k, v in gold_f.items()
            if v is not None and k != "name"
        )
        if match:
            tp_row += 1

    p_row  = tp_row / max(len(pred_rows), 1)
    r_row  = tp_row / max(len(gold_rows), 1)
    f1_row = 2 * p_row * r_row / max(p_row + r_row, 1e-9)

    return {
        "item_precision": round(p_item, 4),
        "item_recall":    round(r_item, 4),
        "item_f1":        round(f1_item, 4),
        "row_precision":  round(p_row, 4),
        "row_recall":     round(r_row, 4),
        "row_f1":         round(f1_row, 4),
        "n_pred":         len(pred_entities),
        "n_gold":         len(gold_entities),
    }


def main():
    parser = argparse.ArgumentParser(description="Scout benchmark runner")
    parser.add_argument("--task",      type=int,   default=None, help="Run only task N (0-indexed)")
    parser.add_argument("--dry-run",   action="store_true",       help="Print tasks without running")
    parser.add_argument("--use-cache", action="store_true",       help="Use cached pages only")
    args = parser.parse_args()

    tasks = load_tasks()

    if args.task is not None:
        tasks = [tasks[args.task]]

    if args.dry_run:
        print(f"Tasks ({len(tasks)}):")
        for i, t in enumerate(tasks):
            print(f"  {i}: {t['id']}  — {t['prompt'][:60]}...")
        return

    print(f"Running {len(tasks)} benchmark task(s)...")
    all_results = []
    for task in tasks:
        result = run_task(task, use_cache=args.use_cache)
        all_results.append(result)

    # Summary table
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Task':<30}  {'Item-F1':>8}  {'Row-F1':>8}  {'CovEst':>8}")
    for r in all_results:
        scores = r.get("scores", {})
        cov    = r.get("coverage", {})
        print(
            f"{r['task_id']:<30}  "
            f"{scores.get('item_f1', '-'):>8}  "
            f"{scores.get('row_f1', '-'):>8}  "
            f"{cov.get('coverage_est', '-'):>8}"
        )


if __name__ == "__main__":
    main()
