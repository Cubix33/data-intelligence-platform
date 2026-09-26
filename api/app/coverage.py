"""Coverage estimation using capture–recapture statistics (Chao2).

Background
----------
We treat each search occasion (one query × one engine × one source_type) as a
*capture event*.  Each resolved entity is an *individual*.  The incidence matrix
marks which occasions found which entities.

Chao2 (Chao 1987, Biometrics 43(4)) estimates the total population size S from:

    Ŝ = S_obs + ((T−1)/T) · Q1·(Q1−1) / (2·(Q2+1))

where
    T     = number of capture occasions
    S_obs = number of distinct entities observed so far
    Q1    = entities found by exactly one occasion ("uniques")
    Q2    = entities found by exactly two occasions ("duplicates")

Coverage is then  S_obs / Ŝ.  Under heterogeneity (famous sponsors are easier to
find), Chao2 is a *lower bound* on S — the UI says "at least ~X% of what exists".

A bootstrap CI resamples the columns (occasions) with replacement 500 times,
recomputes Ŝ each time, and takes the 5th / 95th percentiles.

Public references
-----------------
Chao, A. (1987). Estimating the population size for capture-recapture data with
    unequal catchability. Biometrics 43(4), 783–791.
Burnham & Overton (1979) for bootstrap resampling of capture occasions.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Collection


# ---------------------------------------------------------------------------
# Core Chao2 computation
# ---------------------------------------------------------------------------

def _q_counts(history: dict[str, set[str]], occasion_ids: list[str]) -> tuple[int, int, int]:
    """Return (S_obs, Q1, Q2) for a given set of occasion IDs.

    history : entity_id → set of capture_ids that found it
    occasion_ids : the occasion IDs to consider (subset for bootstrap)
    """
    occasion_set = set(occasion_ids)
    counts_per_entity: list[int] = []
    for caps in history.values():
        n = len(caps & occasion_set)
        if n > 0:
            counts_per_entity.append(n)

    s_obs = len(counts_per_entity)
    freq  = Counter(counts_per_entity)
    q1    = freq[1]
    q2    = freq[2]
    return s_obs, q1, q2


def chao2(history: dict[str, set[str]], T: int) -> float:
    """Chao2 point estimate of total population size.

    Parameters
    ----------
    history : entity_id -> set of capture_ids that found it
    T       : total number of capture occasions run so far

    Returns
    -------
    Estimated total population size Ŝ (always >= S_obs).
    """
    if T < 2 or not history:
        return float(len(history))

    all_caps = list({cap for caps in history.values() for cap in caps})
    s_obs, q1, q2 = _q_counts(history, all_caps)

    if s_obs == 0:
        return 0.0

    # Chao2 bias-corrected form
    s_hat = s_obs + ((T - 1) / T) * (q1 * (q1 - 1)) / (2 * (q2 + 1))
    return max(s_hat, float(s_obs))


def bootstrap_ci(
    history: dict[str, set[str]],
    T: int,
    n_boot: int = 500,
    lo_pct: float = 5.0,
    hi_pct: float = 95.0,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap confidence interval for Ŝ.

    Resamples the *occasions* (columns) with replacement T times and recomputes
    Chao2 for each resample. Returns the (lo_pct, hi_pct) percentiles.

    Parameters
    ----------
    history  : entity_id → set of capture_ids
    T        : number of real capture occasions
    n_boot   : number of bootstrap replicates
    lo_pct   : lower percentile (default 5 → 90% CI)
    hi_pct   : upper percentile (default 95)
    seed     : RNG seed for reproducibility

    Returns
    -------
    (ci_low_s_hat, ci_high_s_hat) — CI on Ŝ, not on coverage directly.
    To get a coverage CI: (S_obs / ci_high_s_hat, S_obs / ci_low_s_hat).
    """
    if T < 2 or len(history) < 2:
        s_obs = float(len(history))
        return s_obs, s_obs

    all_caps = list({cap for caps in history.values() for cap in caps})
    rng = random.Random(seed)
    estimates: list[float] = []

    for _ in range(n_boot):
        # Resample T occasions with replacement from the real occasions
        resampled = [rng.choice(all_caps) for _ in range(T)]
        s_obs_b, q1_b, q2_b = _q_counts(history, resampled)
        if s_obs_b == 0:
            continue
        s_hat_b = s_obs_b + ((T - 1) / T) * (q1_b * (q1_b - 1)) / (2 * (q2_b + 1))
        estimates.append(max(s_hat_b, float(s_obs_b)))

    if not estimates:
        s_obs = float(len(history))
        return s_obs, s_obs

    estimates.sort()
    n = len(estimates)
    lo_idx = max(0, int(lo_pct / 100 * n) - 1)
    hi_idx = min(n - 1, int(hi_pct / 100 * n))
    return estimates[lo_idx], estimates[hi_idx]


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------

def coverage_point(history: dict[str, set[str]], T: int) -> float:
    """Point estimate of fractional coverage: S_obs / Ŝ."""
    s_hat = chao2(history, T)
    if s_hat <= 0:
        return 1.0
    return min(1.0, len(history) / s_hat)


def coverage_lower_bound(history: dict[str, set[str]], T: int, **ci_kwargs) -> float:
    """Conservative coverage estimate using the upper bound of Ŝ's CI.

    This is the value used in the stopping rule:
        if coverage_lower_bound >= target_coverage: stop

    It means: even if the true population is at the high end of our CI,
    we've still probably found at least this fraction.
    """
    _, ci_hi = bootstrap_ci(history, T, **ci_kwargs)
    if ci_hi <= 0:
        return 1.0
    return min(1.0, len(history) / ci_hi)


def coverage_summary(history: dict[str, set[str]], T: int) -> dict:
    """Return a dict suitable for SSE progress events and the dashboard."""
    s_obs = len(history)
    s_hat = chao2(history, T)
    ci_lo, ci_hi = bootstrap_ci(history, T)
    coverage_est = min(1.0, s_obs / s_hat) if s_hat > 0 else 1.0
    cov_lo       = min(1.0, s_obs / ci_hi) if ci_hi > 0 else 1.0
    cov_hi       = min(1.0, s_obs / ci_lo) if ci_lo > 0 else 1.0
    return {
        "s_obs":        s_obs,
        "s_hat":        round(s_hat, 1),
        "ci_lo_s":      round(ci_lo, 1),
        "ci_hi_s":      round(ci_hi, 1),
        "coverage_est": round(coverage_est, 4),
        "coverage_lo":  round(cov_lo, 4),
        "coverage_hi":  round(cov_hi, 4),
        "T":            T,
    }


# ---------------------------------------------------------------------------
# Marginal yield (second stopping condition)
# ---------------------------------------------------------------------------

def marginal_yield(recent_new_counts: Collection[int]) -> float:
    """Fraction of new entities found in recent captures, used as a second stopping signal.

    If the last few captures are finding very few new entities, we've probably exhausted
    the easy-to-find part of the population.

    Parameters
    ----------
    recent_new_counts : new entity counts per capture, most recent last

    Returns
    -------
    Mean new-entity rate over the provided window, as a fraction of the first count.
    Returns 1.0 (don't stop) if fewer than 2 data points.
    """
    counts = list(recent_new_counts)
    if len(counts) < 2:
        return 1.0
    window = counts[-4:]  # look at up to last 4 captures
    total  = sum(window)
    # Normalise by the average of the full history to get a relative rate
    baseline = max(1, sum(counts) / len(counts))
    return total / (len(window) * baseline)
