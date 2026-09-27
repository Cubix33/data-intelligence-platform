"""Jev (TypeSafe AI System One) client — the decision layer.

Jev never writes text. It only judges values the LLM (Groq) already
extracted: yes/no gates (`noul`), and pick-one judgements (`choice`).
Every call is one HTTP POST that can score a whole batch of questions
in parallel server-side.

A run must never fail because Jev is unavailable — every caller in
pipeline.py treats `JevUnavailable` as "skip this gate" and falls
back to the pre-Jev behaviour (see config.SCOUT_JEV_ENABLED and the
per-caller fallbacks in pipeline.py / verifier.py).
"""

from __future__ import annotations

import logging
import time

import httpx

from . import config

logger = logging.getLogger("scout.jev")

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevUnavailable(Exception):
    """Raised when Jev cannot be reached or errors — callers must fall back."""


def ask(state: dict, questions: dict) -> dict:
    """POST one System One request and return `{"answers": {...}, "usage": {...}}`.

    Raises JevUnavailable on connection errors, timeouts, 429s or 5xxs so
    callers can fall back without crashing the run.
    """
    if not config.JEV_API_KEY:
        raise JevUnavailable("no JEV_API_KEY / TYPESAFE_API_KEY configured")

    payload = {"model": config.JEV_MODEL, "state": state, "questions": questions}
    started = time.monotonic()
    try:
        resp = httpx.post(
            _ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {config.JEV_API_KEY}"},
            timeout=config.JEV_TIMEOUT_S,
        )
    except httpx.RequestError as exc:
        raise JevUnavailable(f"Jev connection error: {exc}") from exc

    if resp.status_code == 429 or resp.status_code >= 500:
        raise JevUnavailable(f"Jev returned {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        # Client error (bad request) — not a "Jev is down" situation, but we
        # still don't want to crash a run over a malformed question set.
        raise JevUnavailable(f"Jev rejected request ({resp.status_code}): {resp.text[:200]}")

    latency_ms = round((time.monotonic() - started) * 1000)
    data = resp.json()
    data["latency_ms"] = latency_ms
    logger.debug("jev.ask: %d questions in %dms", len(questions), latency_ms)
    return data


def noul_of(answer: dict | None) -> float | None:
    """Extract the noul probability from an answer, or None if missing/malformed."""
    if not answer or answer.get("type") != "noul":
        return None
    try:
        return float(answer["noul"])
    except (KeyError, TypeError, ValueError):
        return None


def choice_of(answer: dict | None) -> tuple[str | None, dict[str, float]]:
    """Extract (choice, probabilities) from a choice answer."""
    if not answer or answer.get("type") != "choice":
        return None, {}
    return answer.get("choice"), answer.get("probabilities") or {}


# ---------------------------------------------------------------------------
# J1: page relevance gate — called once per fetched chunk, before extraction
# ---------------------------------------------------------------------------

def page_gate(wanted_entity: str, wanted_filters: list[str], page_url: str, page_text: str) -> float:
    """Return the probability that `page_text` names a `wanted_entity` worth extracting.

    Raises JevUnavailable if Jev can't be reached — the caller (pipeline.py)
    treats that as "let the page through" so a Jev outage never drops data.
    """
    state = {
        "wanted_entity": wanted_entity,
        "wanted_filters": wanted_filters,
        "page_url": page_url,
        "page_text": page_text[:4000],
    }
    questions = {
        "lists_entities": {
            "type": "noul",
            "instructions": (
                "Does `page_text` name one or more specific `wanted_entity` (by name) that "
                "could satisfy `wanted_filters`? Navigation text, ads or a generic article "
                "about the topic without named entities is false."
            ),
        }
    }
    data = ask(state, questions)
    noul = noul_of(data.get("answers", {}).get("lists_entities"))
    if noul is None:
        raise JevUnavailable("page_gate: malformed answer")
    return noul


# ---------------------------------------------------------------------------
# J3: filter check — called once per (page, record-batch) before upsert
# ---------------------------------------------------------------------------

def filter_check(records: list[dict], filters: list[str]) -> list[list[float]]:
    """Return per-record, per-filter noul scores: result[i][j] for records[i]/filters[j].

    `records[i]` must have "evidence" (the text supporting it). Raises
    JevUnavailable on failure — the caller then skips the filter check
    entirely for this batch (records pass through unfiltered).
    """
    if not records or not filters:
        return [[] for _ in records]

    state = {"records": records, "filters": filters}
    questions = {}
    for i in range(len(records)):
        for j in range(len(filters)):
            questions[f"r{i}_f{j}"] = {
                "type": "noul",
                "instructions": f"Does the evidence for `records[{i}]` satisfy the filter `filters[{j}]`?",
            }

    data = ask(state, questions)
    answers = data.get("answers", {})
    out: list[list[float]] = []
    for i in range(len(records)):
        row = []
        for j in range(len(filters)):
            noul = noul_of(answers.get(f"r{i}_f{j}"))
            row.append(0.5 if noul is None else noul)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# J2: batched claim support — replaces one DeBERTa forward pass per claim
# ---------------------------------------------------------------------------

def claim_support_batch(claims: list[dict]) -> list[float]:
    """Score a batch of claims in one call. Each claim needs entity, field,
    field_description, value, quote (and optionally page_context).

    Returns a support probability per claim, same order as input. Raises
    JevUnavailable on failure — caller falls back to DeBERTa or a neutral 0.5.
    """
    if not claims:
        return []

    state = {"claims": claims}
    questions = {}
    for i in range(len(claims)):
        questions[f"c{i}"] = {
            "type": "noul",
            "instructions": (
                f"Does `claims[{i}].quote` state that the `claims[{i}].field_description` of "
                f"`claims[{i}].entity` is `claims[{i}].value`? Formatting differences (commas vs "
                "line breaks, case) do not matter; missing or different facts do."
            ),
        }

    data = ask(state, questions)
    answers = data.get("answers", {})
    out = []
    for i in range(len(claims)):
        noul = noul_of(answers.get(f"c{i}"))
        out.append(0.5 if noul is None else noul)
    return out
