#!/usr/bin/env python3
"""Scorer and calibration-plot generator for Scout benchmark results.

Usage
-----
    # Score all result files in eval/results/
    python eval/score.py

    # Score a specific result file
    python eval/score.py --result eval/results/my_task_abc123.json

    # Print coverage calibration error across all scored tasks
    python eval/score.py --calibration

    # Generate a calibration plot (requires matplotlib)
    python eval/score.py --calibration --plot

Output metrics
--------------
Per-task:
  Item-F1, Item-Precision, Item-Recall
  Row-F1, Row-Precision, Row-Recall
  Coverage estimate, true coverage (if gold available), calibration error

Aggregate:
  Mean Item-F1, Mean Row-F1
  Coverage Calibration Error (CCE): mean |estimated − true| across tasks
  Calibration plot: estimated coverage (x) vs true coverage (y), one point per task
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EVAL_DIR    = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"


# ---------------------------------------------------------------------------
# True coverage (from gold file)
# ---------------------------------------------------------------------------

def true_coverage(result: dict) -> float | None:
    """Compute true coverage = n_found / n_gold_entities."""
    gold_file = result.get("gold_file")
    if not gold_file or not Path(gold_file).exists():
        return None

    gold = json.loads(Path(gold_file).read_text())
    n_gold = len(gold.get("entities", []))
    if n_gold == 0:
        return None

    scores = result.get("scores", {})
    n_pred_correct = round(scores.get("item_recall", 0) * n_gold)
    return n_pred_correct / n_gold


# ---------------------------------------------------------------------------
# Load and score
# ---------------------------------------------------------------------------

def load_results(result_dir: Path) -> list[dict]:
    results = []
    for f in sorted(result_dir.glob("*.json")):
        try:
            results.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"Warning: could not load {f}: {e}")
    return results


def print_table(results: list[dict]) -> None:
    """Print a formatted summary table."""
    header = (
        f"{'Task':<32}  {'Item-F1':>7}  {'Row-F1':>7}  "
        f"{'CovEst':>7}  {'CovTrue':>8}  {'CalErr':>7}"
    )
    print(header)
    print("-" * len(header))

    for r in results:
        scores = r.get("scores", {})
        cov    = r.get("coverage", {})
        cov_est  = cov.get("coverage_est")
        cov_true = true_coverage(r)
        cal_err  = abs(cov_est - cov_true) if cov_est is not None and cov_true is not None else None

        print(
            f"{r.get('task_id', '?'):<32}  "
            f"{scores.get('item_f1', '-'):>7}  "
            f"{scores.get('row_f1', '-'):>7}  "
            f"{cov_est if cov_est is not None else '-':>7}  "
            f"{cov_true if cov_true is not None else '-':>8}  "
            f"{cal_err if cal_err is not None else '-':>7}"
        )


def aggregate_metrics(results: list[dict]) -> dict:
    item_f1s = [r["scores"]["item_f1"] for r in results if "scores" in r]
    row_f1s  = [r["scores"]["row_f1"]  for r in results if "scores" in r]

    cal_errors = []
    for r in results:
        cov_est  = r.get("coverage", {}).get("coverage_est")
        cov_true = true_coverage(r)
        if cov_est is not None and cov_true is not None:
            cal_errors.append(abs(cov_est - cov_true))

    return {
        "n_tasks":       len(results),
        "mean_item_f1":  round(sum(item_f1s) / len(item_f1s), 4) if item_f1s else None,
        "mean_row_f1":   round(sum(row_f1s)  / len(row_f1s),  4) if row_f1s  else None,
        "cce":           round(sum(cal_errors) / len(cal_errors), 4) if cal_errors else None,
        "n_cce_tasks":   len(cal_errors),
    }


# ---------------------------------------------------------------------------
# Calibration plot
# ---------------------------------------------------------------------------

def calibration_plot(results: list[dict], save_path: Path | None = None) -> None:
    """Plot estimated coverage (x) vs true coverage (y).

    A perfectly calibrated estimator lies on the y=x diagonal.
    Points below the diagonal: over-estimated coverage (bad — we stopped too soon).
    Points above the diagonal: under-estimated (conservative — fine, just wasteful).
    """
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except ImportError:
        print("matplotlib not installed — cannot generate plot. pip install matplotlib")
        return

    xs, ys, labels = [], [], []
    for r in results:
        cov_est  = r.get("coverage", {}).get("coverage_est")
        cov_true = true_coverage(r)
        if cov_est is not None and cov_true is not None:
            xs.append(cov_est)
            ys.append(cov_true)
            labels.append(r.get("task_id", "?")[:20])

    if not xs:
        print("No tasks with both estimated and true coverage — cannot plot.")
        return

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(xs, ys, zorder=3)
    for x, y, label in zip(xs, ys, labels):
        ax.annotate(label, (x, y), textcoords="offset points", xytext=(5, 5), fontsize=7)

    # Perfect calibration diagonal
    ax.plot([0, 1], [0, 1], "k--", linewidth=1, label="Perfect calibration (y=x)")

    # Shaded region: over-estimated (below diagonal) — potentially stopped too early
    ax.fill_between([0, 1], [0, 0], [0, 1], alpha=0.08, color="red",
                    label="Over-estimated (stopped too early)")
    ax.fill_between([0, 1], [0, 1], [1, 1], alpha=0.08, color="green",
                    label="Under-estimated (conservative)")

    cce = sum(abs(x - y) for x, y in zip(xs, ys)) / len(xs)
    ax.set_xlabel("Estimated coverage (Chao2 lower bound)")
    ax.set_ylabel("True coverage (gold set)")
    ax.set_title(f"Coverage Calibration Plot — CCE = {cce:.3f} (n={len(xs)} tasks)")
    ax.legend(fontsize=8)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150)
        print(f"Plot saved to {save_path}")
    else:
        plt.show()


# ---------------------------------------------------------------------------
# Ablation table builder
# ---------------------------------------------------------------------------

def build_ablation_table(result_groups: dict[str, list[dict]]) -> None:
    """Print an ablation table: one column per system version, rows are metrics.

    Parameters
    ----------
    result_groups : { "v0 (baseline)": [results], "v1 (+bug fixes)": [...], ... }
    """
    versions = list(result_groups.keys())
    print(f"\n{'Metric':<25}" + "".join(f"  {v[:18]:>18}" for v in versions))
    print("-" * (25 + len(versions) * 20))

    for metric in ["mean_item_f1", "mean_row_f1", "cce"]:
        row = f"{metric:<25}"
        for v in versions:
            agg = aggregate_metrics(result_groups[v])
            val = agg.get(metric)
            row += f"  {val if val is not None else '-':>18}"
        print(row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scout benchmark scorer")
    parser.add_argument("--result",      type=str, default=None,
                        help="Path to a single result JSON file")
    parser.add_argument("--calibration", action="store_true",
                        help="Print coverage calibration error")
    parser.add_argument("--plot",        action="store_true",
                        help="Generate calibration plot (requires matplotlib)")
    parser.add_argument("--plot-out",    type=str, default=None,
                        help="Save calibration plot to this path instead of showing it")
    args = parser.parse_args()

    if args.result:
        results = [json.loads(Path(args.result).read_text())]
    else:
        results = load_results(RESULTS_DIR)

    if not results:
        print(f"No result files found in {RESULTS_DIR}. Run run_bench.py first.")
        return

    print_table(results)

    agg = aggregate_metrics(results)
    print(f"\nAggregate ({agg['n_tasks']} tasks):")
    print(f"  Mean Item-F1 : {agg['mean_item_f1']}")
    print(f"  Mean Row-F1  : {agg['mean_row_f1']}")
    if args.calibration or agg["cce"] is not None:
        print(f"  CCE          : {agg['cce']}  ({agg['n_cce_tasks']} tasks with gold)")

    if args.plot:
        save = Path(args.plot_out) if args.plot_out else None
        calibration_plot(results, save_path=save)


if __name__ == "__main__":
    main()
