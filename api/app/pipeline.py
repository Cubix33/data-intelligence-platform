"""Pipeline orchestrator — capture-loop edition.

Flow
----
prompt → DataSpec → [capture loop] → fetch → extract (with quotes) →
value-support check → entity resolution → upsert record + insert claim →
Chao2 coverage estimate → stop if target reached → SQLite → done

Each search occasion (query × engine × source_type) is a *capture* logged in the
`captures` table so the Chao2 estimator can compute how many entities are still missing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from urllib.parse import urlparse

from . import compliance, config, db, fetcher, llm
from .coverage import chao2, bootstrap_ci, marginal_yield
from .entity_resolution import resolve_entity_key

logger = logging.getLogger("scout.pipeline")

# Injected by main.py at startup to avoid circular imports.
# Falls back to a no-op so the pipeline works even without a server (e.g. demo.py).
def _publish_event(run_id: str, event_type: str, data: dict) -> None:  # noqa: D401
    pass


# ---------------------------------------------------------------------------
# Async helper — run coroutines from a sync background thread safely
# ---------------------------------------------------------------------------

# Each pipeline thread gets its own event loop so asyncio.run() never
# conflicts with uvicorn's main-thread loop.
_thread_loop: threading.local = threading.local()


def _run_async(coro):
    """Run an async coroutine from a sync thread using a per-thread event loop."""
    if not hasattr(_thread_loop, "loop") or _thread_loop.loop.is_closed():
        _thread_loop.loop = asyncio.new_event_loop()
    return _thread_loop.loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _verify_evidence(evidence: str | None, page_text: str) -> bool:
    """Check that the quoted evidence string appears verbatim on the page."""
    if not evidence:
        return False
    needle = re.sub(r"\s+", " ", evidence).strip().lower()
    if not needle:
        return False
    haystack = re.sub(r"\s+", " ", page_text).lower()
    return needle in haystack


def _value_in_quote(value, quote: str | None) -> bool:
    """Check that the extracted value (or all its tokens) appear inside the quote.

    Prevents cases like value="Gold", quote="Silver sponsors: Acme" passing
    just because the quote string exists on the page.
    """
    if value is None or not quote:
        return False
    v = re.sub(r"\s+", " ", str(value)).strip().lower()
    q = re.sub(r"\s+", " ", quote).lower()
    return v in q or all(tok in q for tok in re.findall(r"[a-z0-9]+", v) if tok)


def _norm_value(value, field_type: str) -> str | None:
    """Best-effort normalization to value_norm for the claims table."""
    if value is None:
        return None
    s = str(value).strip()
    if field_type == "url":
        return s.lower().rstrip("/")
    if field_type == "number":
        m = re.search(r"[\d,]+\.?\d*", s.replace(",", ""))
        return m.group() if m else s.lower()
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# Capture planner — decides what to search next
# ---------------------------------------------------------------------------

_ENGINES = ["ddg", "brave", "searxng"]
_SOURCE_TYPES = ["general", "news"]


def _next_capture_params(
    spec,
    used_combos: set[tuple],
) -> tuple[str, str, str] | None:
    """Return (query, engine, source_type) not yet tried, or None if exhausted."""
    for query in spec.search_queries:
        for engine in _ENGINES:
            for source_type in _SOURCE_TYPES:
                combo = (query, engine, source_type)
                if combo not in used_combos:
                    return combo
    return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(run_id: str, prompt: str) -> None:
    db.update_run(run_id, status="planning")
    _publish_event(run_id, "status_change", {"status": "planning"})

    # --- 1. Parse intent ---
    try:
        dataspec = llm.parse_intent(prompt)
    except Exception as exc:
        logger.exception("intent parsing failed")
        db.update_run(run_id, status="failed", error=str(exc), finished_at=db.now())
        _publish_event(run_id, "error", {"message": str(exc)})
        return

    db.update_run(run_id, data_spec=dataspec.model_dump(), status="discovering")
    _publish_event(run_id, "status_change", {"status": "discovering"})

    primary_field = dataspec.fields[0].name
    field_types = {f.name: f.type for f in dataspec.fields}

    # capture_history: entity_key -> set of capture_ids that found it (for Chao2)
    capture_history: dict[str, set[str]] = {}
    used_combos: set[tuple] = set()
    recent_new_counts: list[int] = []

    stats = {
        "urls_discovered": 0,
        "urls_skipped_compliance": 0,
        "pages_fetched": 0,
        "pages_failed": 0,
        "records_found": 0,
        "records_after_dedupe": 0,
        "captures_run": 0,
        "coverage_estimate": None,
        "coverage_ci_low": None,
        "coverage_ci_high": None,
    }

    db.update_run(run_id, status="fetching")
    _publish_event(run_id, "status_change", {"status": "fetching"})

    # --- 2. Capture loop ---
    for _cap_iter in range(config.MAX_CAPTURES_PER_RUN):

        combo = _next_capture_params(dataspec, used_combos)
        if combo is None:
            logger.info("run %s: all query/engine/source combos exhausted", run_id)
            break
        query, engine, source_type = combo
        used_combos.add(combo)

        capture_id = db.log_capture(run_id, query, engine, source_type)
        stats["captures_run"] += 1
        _publish_event(run_id, "capture_start", {
            "capture_id": capture_id, "query": query,
            "engine": engine, "source_type": source_type,
        })

        try:
            candidates = llm.discover_urls_for_query(dataspec, query, engine)
        except Exception as exc:
            logger.warning("discovery failed for %r/%r: %s", query, engine, exc)
            continue

        candidates = candidates[: config.MAX_URLS_PER_CAPTURE]
        stats["urls_discovered"] += len(candidates)
        new_in_capture = 0

        for candidate in candidates:
            url = candidate["url"]
            domain = urlparse(url).netloc

            # --- Compliance gate ---
            allowed, reason = compliance.is_allowed(url)
            if not allowed:
                db.log_source(run_id, url, domain, "skipped", reason,
                              capture_id=capture_id)
                stats["urls_skipped_compliance"] += 1
                continue

            # --- Fetch (async, safe from sync thread) ---
            try:
                chunks = _run_async(fetcher.fetch_chunks(url))
            except Exception as exc:
                logger.info("fetch error %s: %s", url, exc)
                db.log_source(run_id, url, domain, "failed",
                              "fetch error or empty page", capture_id=capture_id)
                stats["pages_failed"] += 1
                continue

            if not chunks:
                db.log_source(run_id, url, domain, "failed",
                              "empty page", capture_id=capture_id)
                stats["pages_failed"] += 1
                continue

            stats["pages_fetched"] += 1
            new_here = 0

            for chunk_text in chunks:
                # --- Extract ---
                try:
                    records = llm.extract_records(dataspec, url, chunk_text)
                except Exception as exc:
                    logger.warning("extraction failed for %s: %s", url, exc)
                    continue

                for record in records:
                    evidence = record.get("evidence", {})
                    fields: dict = {}
                    provenance: dict = {}
                    verified_count = 0

                    for field in dataspec.fields:
                        value   = record.get(field.name)
                        ev_text = evidence.get(field.name)

                        # Both checks must pass (day-1 fix)
                        quote_on_page = _verify_evidence(ev_text, chunk_text)
                        value_in_ev   = _value_in_quote(value, ev_text)
                        verified      = quote_on_page and value_in_ev

                        if not verified and value is not None:
                            value = None   # drop unverifiable value

                        if verified:
                            verified_count += 1

                        fields[field.name] = value
                        provenance[field.name] = {
                            "url":        url,
                            "evidence":   ev_text if verified else None,
                            "verified":   verified,
                            "capture_id": capture_id,
                        }

                    primary_value = fields.get(primary_field)
                    if not primary_value:
                        continue

                    entity_key    = resolve_entity_key(str(primary_value))
                    fields_sourced = round(verified_count / max(len(dataspec.fields), 1), 2)

                    is_new = db.upsert_record(run_id, entity_key, fields,
                                              provenance, fields_sourced)
                    stats["records_found"] += 1
                    if is_new:
                        new_here      += 1
                        new_in_capture += 1

                    _publish_event(run_id, "record_found", {
                        "entity_key":    entity_key,
                        "is_new":        is_new,
                        "fields_sourced": fields_sourced,
                    })

                    # Write claims (audit layer)
                    for field in dataspec.fields:
                        v   = fields.get(field.name)
                        ev  = provenance[field.name].get("evidence")
                        if v is not None or ev is not None:
                            db.insert_claim(
                                run_id=run_id,
                                entity_id=entity_key,
                                field=field.name,
                                value_raw=str(v) if v is not None else None,
                                value_norm=_norm_value(v, field_types.get(field.name, "string")),
                                source_url=url,
                                quote=ev,
                                support_score=None,
                                capture_id=capture_id,
                                extractor=f"llm:{config.MODEL_EXTRACT}",
                            )

                    # Update Chao2 capture history
                    capture_history.setdefault(entity_key, set()).add(capture_id)

            db.log_source(run_id, url, domain, "ok",
                          records_found=new_here, capture_id=capture_id)

        recent_new_counts.append(new_in_capture)

        # --- Coverage estimate after each capture ---
        T = stats["captures_run"]
        if T >= 2 and len(capture_history) >= 2:
            s_hat         = chao2(capture_history, T)
            ci_lo, ci_hi  = bootstrap_ci(capture_history, T)
            s_obs         = len(capture_history)
            coverage_est  = min(1.0, s_obs / s_hat)  if s_hat > 0 else 1.0
            coverage_lo   = min(1.0, s_obs / ci_hi)  if ci_hi > 0 else 1.0

            stats["coverage_estimate"] = round(coverage_est, 4)
            stats["coverage_ci_low"]   = round(coverage_lo,  4)
            stats["coverage_ci_high"]  = round(min(1.0, s_obs / max(ci_lo, 1)), 4)
            db.update_run(run_id, stats=stats)

            logger.info(
                "run %s: T=%d S_obs=%d Ŝ=%.1f cov=%.1f%% (lo=%.1f%%)",
                run_id, T, s_obs, s_hat, coverage_est * 100, coverage_lo * 100,
            )
            _publish_event(run_id, "coverage", {
                "s_obs":         s_obs,
                "s_hat":         round(s_hat, 1),
                "coverage_est":  round(coverage_est, 4),
                "coverage_lo":   round(coverage_lo, 4),
                "T":             T,
            })

            # Stopping rule
            target = getattr(dataspec, "target_coverage",
                             config.DEFAULT_TARGET_COVERAGE)
            if coverage_lo >= target:
                logger.info("run %s: coverage target %.0f%% reached — stopping",
                            run_id, target * 100)
                break
            if marginal_yield(recent_new_counts) < config.MIN_MARGINAL_YIELD and T >= 4:
                logger.info("run %s: marginal yield flat — stopping", run_id)
                break
        else:
            db.update_run(run_id, stats=stats)

    # --- 3. Finish ---
    stats["records_after_dedupe"] = len(db.list_records(run_id))
    db.update_run(run_id, status="done", stats=stats, finished_at=db.now())
    _publish_event(run_id, "done", {"stats": stats})

    # Close the per-thread event loop
    if hasattr(_thread_loop, "loop") and not _thread_loop.loop.is_closed():
        _thread_loop.loop.close()
