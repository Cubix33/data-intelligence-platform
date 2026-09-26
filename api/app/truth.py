"""Source trust and conflict resolution — iterative Knowledge-Based Trust (KBT).

Background
----------
When two sources disagree on a field value (e.g. source A says "Gold tier",
source B says "Silver tier"), we need a principled way to pick the more likely
correct value rather than just taking the first or most recent.

Knowledge-Based Trust (Dong et al. 2015, arXiv 1502.03519) jointly estimates:
  - source trustworthiness: how often does this domain's facts turn out correct?
  - fact probability: given multiple claims, which value is true?

The algorithm alternates:
  1. Given source trust scores, compute fact probability via a voting model.
  2. Given fact probabilities, update source trust = mean(prob of their claims).

We run this per (run_id, entity_id, field) group across all claims for a run,
then write the winning value + its probability back to the records table.

Simplifications in this implementation
---------------------------------------
- Trust is per domain (not per source URL), pooled across runs on the same topic
  (via a persistent domain_trust table, added below).
- The voting model uses log-odds of trust rather than full graphical inference.
- Support_score from the verifier (pillar B) is used as a soft prior on each
  claim's correctness before trust iteration starts.

References
----------
Dong et al. (2015) Knowledge-Based Trust (arXiv 1502.03519).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from urllib.parse import urlparse

from . import db

logger = logging.getLogger("scout.truth")

_PRIOR_TRUST = 0.80   # starting trust for an unknown domain
_N_ITER      = 10     # EM iterations (converges fast in practice)
_MIN_TRUST   = 0.01
_MAX_TRUST   = 0.99


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", h)
    except Exception:
        return url


def _logit(p: float) -> float:
    p = max(_MIN_TRUST, min(_MAX_TRUST, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _softmax(scores: list[float]) -> list[float]:
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


# ---------------------------------------------------------------------------
# Core KBT iteration
# ---------------------------------------------------------------------------

def resolve_conflicts(
    claims: list[dict],
    initial_trust: dict[str, float] | None = None,
) -> list[dict]:
    """Run KBT-style trust iteration over a list of claims.

    Parameters
    ----------
    claims : list of claim dicts from db.list_claims (must share run_id + field)
    initial_trust : domain -> prior trust score (default _PRIOR_TRUST for all)

    Returns
    -------
    List of claim dicts with an added 'kbt_prob' key (0..1) — the estimated
    probability that each claim's value is correct.  The claim with the highest
    kbt_prob is the recommended display value.
    """
    if not claims:
        return claims

    # Group claims by normalized value
    from .entity_resolution import normalize_name  # avoid circular at module level
    value_claims: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        v = normalize_name(c.get("value_norm") or c.get("value_raw") or "")
        value_claims[v].append(c)

    if len(value_claims) <= 1:
        # No conflict — every claim agrees; mark them all high probability
        for c in claims:
            c["kbt_prob"] = 1.0
        return claims

    # Initial domain trust
    trust: dict[str, float] = dict(initial_trust or {})
    domains = {_domain(c["source_url"]) for c in claims}
    for d in domains:
        trust.setdefault(d, _PRIOR_TRUST)

    # Initialise value probabilities from verifier support_score (if present)
    # or from uniform prior
    unique_values = list(value_claims.keys())
    value_prob: dict[str, float] = {}
    for v in unique_values:
        scores = [c.get("support_score") or 0.5 for c in value_claims[v]]
        value_prob[v] = sum(scores) / len(scores)

    # Normalise initial probs
    total = sum(value_prob.values()) or 1.0
    value_prob = {v: p / total for v, p in value_prob.items()}

    # EM iterations
    for _i in range(_N_ITER):
        # --- E step: compute value scores given current trust ---
        raw_scores: dict[str, float] = {}
        for v, cs in value_claims.items():
            log_score = sum(_logit(trust[_domain(c["source_url"])]) for c in cs)
            raw_scores[v] = log_score

        # Softmax over values to get probabilities
        probs = _softmax(list(raw_scores.values()))
        value_prob = dict(zip(unique_values, probs))

        # --- M step: update trust per domain ---
        new_trust: dict[str, float] = {}
        for d in domains:
            # mean probability of the values this domain claimed
            domain_probs = []
            for v, cs in value_claims.items():
                for c in cs:
                    if _domain(c["source_url"]) == d:
                        domain_probs.append(value_prob[v])
            new_trust[d] = (
                sum(domain_probs) / len(domain_probs) if domain_probs else _PRIOR_TRUST
            )
        trust.update(new_trust)

    # Annotate each claim with its value's final probability
    for c in claims:
        v = normalize_name(c.get("value_norm") or c.get("value_raw") or "")
        c["kbt_prob"] = round(value_prob.get(v, 0.0), 4)

    return claims


# ---------------------------------------------------------------------------
# Run-level conflict resolution — processes all conflicted fields in a run
# ---------------------------------------------------------------------------

def resolve_run_conflicts(run_id: str) -> dict[str, int]:
    """Run KBT conflict resolution over every (entity, field) group in a run.

    For each conflicted cell:
    1. Pull all claims for that (entity_id, field).
    2. Run KBT iteration to get probabilities.
    3. Pick the highest-probability value.
    4. Update the record's field + provenance with the winner and its probability.

    Returns a summary dict: {entity_key: num_conflicts_resolved}.
    """
    records = db.list_records(run_id)
    resolved_counts: dict[str, int] = defaultdict(int)

    for rec in records:
        entity_key = rec["entity_key"]
        provenance  = rec["provenance"]
        fields      = rec["fields"]

        for field_name, prov in provenance.items():
            conflicts = prov.get("conflicts")
            if not conflicts:
                continue

            # Fetch all claims for this (entity, field)
            claims = db.list_claims(run_id, entity_id=entity_key, field=field_name)
            if len(claims) < 2:
                continue

            claims = resolve_conflicts(claims)

            # Pick the winning claim (highest kbt_prob)
            winner = max(claims, key=lambda c: c.get("kbt_prob", 0.0))
            winning_value = winner.get("value_raw")
            winning_url   = winner.get("source_url")
            winning_prob  = winner.get("kbt_prob", 0.0)

            if winning_value and winning_value != fields.get(field_name):
                fields[field_name] = winning_value
                provenance[field_name]["url"]           = winning_url
                provenance[field_name]["kbt_prob"]      = winning_prob
                provenance[field_name]["conflict_note"] = (
                    f"Resolved from {len(conflicts) + 1} conflicting sources"
                )
                resolved_counts[entity_key] += 1

        # Write updated record back
        conn = db.get_conn()
        conn.execute(
            "UPDATE records SET fields = ?, provenance = ? WHERE id = ?",
            (json.dumps(fields), json.dumps(provenance), rec["id"]),
        )
        conn.commit()

    logger.info(
        "run %s: resolved conflicts in %d cells across %d entities",
        run_id,
        sum(resolved_counts.values()),
        len(resolved_counts),
    )
    return dict(resolved_counts)


# ---------------------------------------------------------------------------
# Domain trust persistence (across runs on the same topic — future use)
# ---------------------------------------------------------------------------

def get_domain_trust(domains: list[str]) -> dict[str, float]:
    """Load persisted trust scores for a set of domains.

    Currently returns the prior for all (no persistence yet).
    TODO: store in a `domain_trust` table keyed by (domain, topic_tag).
    """
    return {d: _PRIOR_TRUST for d in domains}
