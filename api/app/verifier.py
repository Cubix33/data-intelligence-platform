"""Value-support verifier — Pillar B.

Replaces the old "quote exists on page" check with a trained NLI scorer that
estimates the probability that a quote actually supports a field value.

Model
-----
Off-the-shelf: cross-encoder/nli-deberta-v3-large from HuggingFace.
  Premise   : quote + ~300 chars surrounding context from the page
  Hypothesis: "The {field_description} of {entity} is {value}."
  Output    : entailment probability (0..1)

The model is loaded lazily on first use and cached in the process.  Set
SCOUT_VERIFIER_ENABLED=false to skip (e.g. on machines without PyTorch).

Fine-tuning (Phase 2 / GPU plan)
---------------------------------
Once 200–400 human-labelled cells exist, fine-tune on:
  Positives : cells the 120B extractor produced that a human confirmed correct.
  Hard negatives : swapped values, wrong fields, perturbed numbers/dates,
                   quotes from neighbouring rows on the same page.
Replace the model path with the fine-tuned checkpoint.

Conformal threshold calibration
---------------------------------
Use Learn then Test (2110.01052) or Conformal Risk Control (2208.02814) on
the human-labelled calibration set to pick threshold τ such that among accepted
cells the error rate stays ≤ α (e.g. 5%) with probability ≥ 1−δ (e.g. 90%).
See conformal_threshold() below for the basic implementation.

References
----------
He et al. DeBERTa (2006.03654).
Angelopoulos & Bates. Learn then Test (2110.01052).
Angelopoulos et al. Conformal Risk Control (2208.02814).
Mohri & Hashimoto. Conformal Factuality (2402.10978).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    pass

logger = logging.getLogger("scout.verifier")

_model = None   # lazy-loaded cross-encoder


def _load_model():
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
        logger.info("loading NLI verifier model: %s", config.VERIFIER_MODEL)
        _model = CrossEncoder(config.VERIFIER_MODEL)
        logger.info("verifier model loaded")
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — verifier disabled. "
            "pip install sentence-transformers to enable."
        )
        _model = None
    except Exception as exc:
        logger.warning("verifier model failed to load (%s) — disabled", exc)
        _model = None
    return _model


# ---------------------------------------------------------------------------
# Public scoring API
# ---------------------------------------------------------------------------

def score_support(
    entity: str,
    field_name: str,
    field_description: str,
    value: str,
    quote: str,
    page_context: str = "",
) -> float:
    """Return the probability (0..1) that the quote supports the field value.

    Parameters
    ----------
    entity           : resolved entity name (e.g. "Google")
    field_name       : field key (e.g. "sponsorship_tier")
    field_description: human-readable description from DataSpec
    value            : extracted value (e.g. "Gold")
    quote            : verbatim quote from the page
    page_context     : up to 300 chars of surrounding text (improves accuracy)

    Returns
    -------
    Probability in [0, 1]. Returns 0.5 (neutral) if the model is unavailable.
    """
    if not config.VERIFIER_ENABLED:
        return 0.5

    model = _load_model()
    if model is None:
        return 0.5

    premise    = _build_premise(quote, page_context)
    hypothesis = _build_hypothesis(entity, field_description, value)

    try:
        # CrossEncoder returns shape (n_pairs, n_labels): [contradiction, neutral, entailment]
        scores = model.predict([(premise, hypothesis)])
        import numpy as np  # type: ignore
        probs = _softmax(scores[0])
        entailment_idx = 2   # standard NLI label order for DeBERTa cross-encoders
        return float(probs[entailment_idx])
    except Exception as exc:
        logger.warning("verifier predict failed: %s", exc)
        return 0.5


def score_claims_batch(claims: list[dict], entity_name: str, field_description: str) -> list[float]:
    """Score a batch of claims for the same (entity, field) in one forward pass.

    Much faster than calling score_support per claim when many claims share
    the same field (e.g. scoring all claims for a run at once).

    Parameters
    ----------
    claims          : list of claim dicts with 'value_raw' and 'quote' keys
    entity_name     : resolved entity name
    field_description: DataSpec field description

    Returns
    -------
    List of support probabilities, same order as claims.
    """
    if not config.VERIFIER_ENABLED:
        return [0.5] * len(claims)

    model = _load_model()
    if model is None:
        return [0.5] * len(claims)

    pairs = []
    for c in claims:
        premise    = _build_premise(c.get("quote") or "", "")
        hypothesis = _build_hypothesis(
            entity_name, field_description, c.get("value_raw") or ""
        )
        pairs.append((premise, hypothesis))

    try:
        scores_batch = model.predict(pairs)
        import numpy as np  # type: ignore
        return [float(_softmax(s)[2]) for s in scores_batch]
    except Exception as exc:
        logger.warning("verifier batch predict failed: %s", exc)
        return [0.5] * len(claims)


# ---------------------------------------------------------------------------
# Conformal threshold calibration
# ---------------------------------------------------------------------------

def conformal_threshold(
    scores: list[float],
    labels: list[int],
    alpha: float = 0.05,
    delta: float = 0.10,
) -> float:
    """Pick the accept threshold τ using Learn then Test (simple version).

    Among cells with score >= τ, the empirical error rate should be ≤ α
    with probability ≥ 1−δ on the calibration set.

    Parameters
    ----------
    scores : verifier probabilities on labelled cells (0..1)
    labels : 1 = correct, 0 = wrong (human judgement)
    alpha  : target error rate (e.g. 0.05 = 5%)
    delta  : failure probability (e.g. 0.10 = 90% coverage guarantee)

    Returns
    -------
    Threshold τ ∈ [0, 1].  Cells with score >= τ are "verified".
    """
    if len(scores) != len(labels) or not scores:
        logger.warning("conformal_threshold: empty or mismatched inputs, returning default")
        return config.VERIFIER_THRESHOLD

    import math

    # Sort by score descending; find the lowest τ such that error rate ≤ α
    # adjusted for finite-sample Hoeffding bound (Learn then Test style).
    n = len(scores)
    pairs = sorted(zip(scores, labels), key=lambda x: -x[0])

    best_tau = 1.0
    for i, (tau, _) in enumerate(pairs):
        accepted = [(s, y) for s, y in pairs if s >= tau]
        if not accepted:
            continue
        n_acc    = len(accepted)
        errors   = sum(1 for _, y in accepted if y == 0)
        err_rate = errors / n_acc
        # Hoeffding-style upper bound on true error rate
        margin   = math.sqrt(math.log(1 / delta) / (2 * n_acc))
        if err_rate + margin <= alpha:
            best_tau = tau
            break

    logger.info(
        "conformal_threshold: τ=%.3f (n=%d, alpha=%.2f, delta=%.2f)",
        best_tau, n, alpha, delta,
    )
    return best_tau


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_premise(quote: str, context: str) -> str:
    """Combine quote + surrounding context into the NLI premise."""
    quote   = re.sub(r"\s+", " ", quote or "").strip()
    context = re.sub(r"\s+", " ", context or "").strip()
    if context:
        return f"{context[:300]} ... {quote}"[:600]
    return quote[:600]


def _build_hypothesis(entity: str, field_description: str, value: str) -> str:
    """Build the NLI hypothesis sentence."""
    return f"The {field_description} of {entity} is {value}."


def _softmax(logits) -> list[float]:
    import math
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    s = sum(exps)
    return [e / s for e in exps]
