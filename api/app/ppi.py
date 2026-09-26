"""Prediction-Powered Inference (PPI) — accuracy audit for Scout.

Background
----------
We want to estimate the fraction of cells that are correct.  Checking every cell
is expensive (needs a human).  The verifier has scored every cell automatically,
but its scores aren't perfectly calibrated.

PPI (Angelopoulos et al. 2023, arXiv 2301.09633) combines:
  - f_all : verifier scores on ALL N cells (cheap to get)
  - y_lab, f_lab : human labels + verifier scores on a small random sample of n cells

to produce an unbiased estimate of accuracy θ with a confidence interval that
shrinks as the verifier improves:

    θ̂  = mean(f_all) + mean(y_lab − f_lab)
    SE² = Var(f_all)/N + Var(y_lab − f_lab)/n
    CI  = θ̂ ± z · SE

Key properties:
  - Unbiased regardless of verifier quality (the correction term y − f fixes bias).
  - Interval is never wider than a pure human-only interval (using n labels alone).
  - Updates live: every additional label tightens the interval.

Demo value
----------
Show this panel updating on screen as a judge labels 10 cells.  The accuracy
interval shrinks visibly — that's a strong live demo of statistical rigour.

References
----------
Angelopoulos et al. (2023). Prediction-Powered Inference (arXiv 2301.09633).
"""

from __future__ import annotations

import math
import logging
import random
from typing import NamedTuple

logger = logging.getLogger("scout.ppi")


# ---------------------------------------------------------------------------
# Core PPI formula
# ---------------------------------------------------------------------------

class PPIResult(NamedTuple):
    theta_hat: float          # point estimate of accuracy
    ci_low:    float          # lower 95% confidence bound
    ci_high:   float          # upper 95% confidence bound
    n_cells:   int            # total cells scored by verifier
    n_labeled: int            # human-labelled cells used
    se:        float          # standard error


def ppi_accuracy(
    f_all:  list[float],
    f_lab:  list[float],
    y_lab:  list[int],
    z: float = 1.96,
) -> PPIResult:
    """Compute the PPI accuracy estimate and confidence interval.

    Parameters
    ----------
    f_all  : verifier support_score for every cell in the run (0..1), length N
    f_lab  : verifier support_score for the labelled subset, length n
    y_lab  : human label for the labelled subset (1=correct, 0=wrong), length n
    z      : z-score for the desired confidence level (1.96 → 95%)

    Returns
    -------
    PPIResult with point estimate and CI.

    Notes
    -----
    Falls back to a plain mean(y_lab) estimate when f_all is empty or
    f_lab/y_lab are mismatched.
    """
    N = len(f_all)
    n = len(y_lab)

    if N == 0:
        return PPIResult(0.0, 0.0, 0.0, 0, 0, 0.0)

    if n == 0:
        # No labels yet — return verifier mean with no correction
        mu_f = sum(f_all) / N
        var_f = sum((x - mu_f) ** 2 for x in f_all) / max(N - 1, 1)
        se = math.sqrt(var_f / N)
        return PPIResult(
            theta_hat=round(mu_f, 4),
            ci_low=round(max(0.0, mu_f - z * se), 4),
            ci_high=round(min(1.0, mu_f + z * se), 4),
            n_cells=N, n_labeled=0, se=round(se, 6),
        )

    if len(f_lab) != n:
        logger.warning("ppi_accuracy: f_lab and y_lab length mismatch (%d vs %d)", len(f_lab), n)
        n = min(len(f_lab), n)
        f_lab = f_lab[:n]
        y_lab = y_lab[:n]

    # PPI point estimate: imputed mean + correction
    mu_f   = sum(f_all) / N
    rect   = [yi - fi for yi, fi in zip(y_lab, f_lab)]   # correction terms
    mu_r   = sum(rect) / n
    theta  = mu_f + mu_r

    # Variance terms
    var_f = sum((x - mu_f) ** 2 for x in f_all) / max(N - 1, 1)
    var_r = sum((x - mu_r) ** 2 for x in rect)  / max(n  - 1, 1)
    se    = math.sqrt(var_f / N + var_r / n)

    return PPIResult(
        theta_hat=round(max(0.0, min(1.0, theta)), 4),
        ci_low=round(max(0.0, theta - z * se), 4),
        ci_high=round(min(1.0, theta + z * se), 4),
        n_cells=N, n_labeled=n, se=round(se, 6),
    )


# ---------------------------------------------------------------------------
# Run-level helper (pulls from DB and runs PPI)
# ---------------------------------------------------------------------------

def ppi_accuracy_for_run(run_id: str) -> dict:
    """Compute the PPI accuracy estimate for all claims in a run.

    Reads verifier scores from the claims table and human labels from
    human_labels, then returns a dict suitable for the /api/runs/{id}/accuracy
    endpoint.
    """
    from . import db as _db

    claims = _db.list_claims(run_id)
    labels_rows = _db.list_human_labels(run_id)

    if not claims:
        return {
            "theta_hat": None,
            "ci_low": None,
            "ci_high": None,
            "n_cells": 0,
            "n_labeled": 0,
            "se": None,
            "message": "No claims found for this run.",
        }

    # All verifier scores (None -> 0.5 neutral)
    f_all = [c.get("support_score") if c.get("support_score") is not None else 0.5
             for c in claims]

    # Build labelled subset
    label_map = {row["claim_id"]: row["correct"] for row in labels_rows}
    claim_id_map = {c["id"]: c for c in claims}

    f_lab: list[float] = []
    y_lab: list[int]   = []
    for claim_id, correct in label_map.items():
        claim = claim_id_map.get(claim_id)
        if claim:
            score = claim.get("support_score")
            f_lab.append(score if score is not None else 0.5)
            y_lab.append(int(correct))

    result = ppi_accuracy(f_all, f_lab, y_lab)

    return {
        "theta_hat":  result.theta_hat,
        "ci_low":     result.ci_low,
        "ci_high":    result.ci_high,
        "n_cells":    result.n_cells,
        "n_labeled":  result.n_labeled,
        "se":         result.se,
        "message":    (
            f"Estimated accuracy: {result.theta_hat:.1%} "
            f"(95% CI: {result.ci_low:.1%}–{result.ci_high:.1%}) "
            f"from {result.n_labeled} labelled / {result.n_cells} total cells"
        ),
    }


# ---------------------------------------------------------------------------
# Sample selector — pick n cells to send to the audit UI
# ---------------------------------------------------------------------------

def sample_for_audit(run_id: str, n: int = 30, seed: int = 42) -> list[dict]:
    """Return n randomly sampled claims from a run, weighted toward uncertain cells.

    Cells where the verifier score is near 0.5 (uncertain) are more valuable to
    label because they shift the correction term the most.  We use a stratified
    sample: ~50% from the [0.3, 0.7] uncertainty band, ~50% random.
    """
    from . import db as _db

    claims = _db.list_claims(run_id)
    if not claims:
        return []

    rng = random.Random(seed)

    uncertain = [c for c in claims
                 if c.get("support_score") is not None
                 and 0.3 <= c["support_score"] <= 0.7]
    certain   = [c for c in claims if c not in uncertain]

    n_uncertain = min(len(uncertain), n // 2)
    n_certain   = min(len(certain),   n - n_uncertain)

    sampled = (
        rng.sample(uncertain, n_uncertain) +
        rng.sample(certain,   n_certain)
    )
    rng.shuffle(sampled)
    return sampled[:n]
