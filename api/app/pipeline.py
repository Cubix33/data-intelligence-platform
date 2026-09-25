"""The DAG executor, first-draft edition: intent -> discover -> fetch -> extract -> dedupe.

Runs synchronously in a background thread per run (see main.py). Later phases can swap
this for the Redis/arq worker queue described in IDEATION.md without touching the DB
schema or the API contract.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from . import compliance, config, db, fetcher, llm

logger = logging.getLogger("scout.pipeline")


def _slugify(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip().lower()
    value = re.sub(r"[^a-z0-9 ]", "", value)
    return value[:80] or "unknown"


def _verify_evidence(evidence: str | None, page_text: str) -> bool:
    if not evidence:
        return False
    needle = re.sub(r"\s+", " ", evidence).strip().lower()
    if not needle:
        return False
    haystack = re.sub(r"\s+", " ", page_text).lower()
    return needle in haystack


def run_pipeline(run_id: str, prompt: str) -> None:
    db.update_run(run_id, status="planning")
    try:
        dataspec = llm.parse_intent(prompt)
    except Exception as exc:  # noqa: BLE001 - surface any failure on the run record
        logger.exception("intent parsing failed")
        db.update_run(run_id, status="failed", error=str(exc), finished_at=db.now())
        return

    db.update_run(run_id, data_spec=dataspec.model_dump(), status="discovering")

    try:
        candidates = llm.discover_urls(dataspec)
    except Exception as exc:  # noqa: BLE001
        logger.exception("discovery failed")
        db.update_run(run_id, status="failed", error=str(exc), finished_at=db.now())
        return

    candidates = candidates[: config.MAX_URLS_PER_RUN]
    db.update_run(run_id, status="fetching")

    primary_field = dataspec.fields[0].name
    stats = {
        "urls_discovered": len(candidates),
        "urls_skipped_compliance": 0,
        "pages_fetched": 0,
        "pages_failed": 0,
        "records_found": 0,
        "records_after_dedupe": 0,
    }

    for candidate in candidates:
        url = candidate["url"]
        domain = urlparse(url).netloc

        allowed, reason = compliance.is_allowed(url)
        if not allowed:
            db.log_source(run_id, url, domain, "skipped", reason)
            stats["urls_skipped_compliance"] += 1
            continue

        page_text = fetcher.fetch_text(url)
        if not page_text:
            db.log_source(run_id, url, domain, "failed", "fetch error or empty page")
            stats["pages_failed"] += 1
            continue

        stats["pages_fetched"] += 1

        try:
            records = llm.extract_records(dataspec, url, page_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("extraction failed for %s: %s", url, exc)
            db.log_source(run_id, url, domain, "failed", f"extraction error: {exc}")
            continue

        new_here = 0
        for record in records:
            evidence = record.get("evidence", {})
            fields: dict = {}
            provenance: dict = {}
            verified_count = 0

            for field in dataspec.fields:
                value = record.get(field.name)
                ev_text = evidence.get(field.name)
                verified = _verify_evidence(ev_text, page_text)
                if verified:
                    verified_count += 1
                elif value is not None:
                    # claimed a value with no matching quote on the page — drop it rather
                    # than trust an unverifiable claim
                    value = None
                fields[field.name] = value
                provenance[field.name] = {
                    "url": url,
                    "evidence": ev_text if verified else None,
                    "verified": verified,
                    "confidence": 1.0 if verified else 0.0,
                }

            primary_value = fields.get(primary_field)
            if not primary_value:
                continue  # nothing to key or dedupe on

            confidence = round(verified_count / max(len(dataspec.fields), 1), 2)
            entity_key = _slugify(str(primary_value))
            is_new = db.upsert_record(run_id, entity_key, fields, provenance, confidence)
            stats["records_found"] += 1
            if is_new:
                new_here += 1

        db.log_source(run_id, url, domain, "ok", records_found=new_here)

    stats["records_after_dedupe"] = len(db.list_records(run_id))
    db.update_run(run_id, status="done", stats=stats, finished_at=db.now())
