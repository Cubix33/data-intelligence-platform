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

import ast
import asyncio
import json
import logging
import re
import threading
from urllib.parse import urlparse

from . import compliance, config, db, fetcher, llm
from .coverage import chao2, bootstrap_ci, marginal_yield
from .entity_resolution import match_existing_key, resolve_entity_key
from .normalize import normalize_value, single_year
from . import verifier as verifier_mod
from . import truth as truth_mod
from . import jev as jev_mod

logger = logging.getLogger("scout.pipeline")

# Injected by main.py at startup to avoid circular imports.
# Falls back to a no-op so the pipeline works even without a server (e.g. demo.py).
def _publish_event(run_id: str, event_type: str, data: dict) -> None:  # noqa: D401
    pass


def _is_run_cancelled(run_id: str) -> bool:  # noqa: D401
    """Injected by main.py. Falls back to never-cancel for CLI usage."""
    return False


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
    if v and re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", q):
        return True
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    v_tokens = re.findall(r"[a-z0-9]+", v)
    if v_tokens and all(tok in q_tokens for tok in v_tokens):
        return True
    # Same quantity written differently: "4000000" vs "$4 million", "4M" vs "4 mn".
    v_nums = _numbers(v)
    return len(v_nums) == 1 and any(abs(n - v_nums[0]) <= 0.005 * max(abs(n), 1e-9) for n in _numbers(q))


_MAGNITUDE = {
    "k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5,
    "m": 1e6, "mn": 1e6, "million": 1e6, "cr": 1e7, "crore": 1e7, "crores": 1e7,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|mn|m|million|crores?|cr|bn|b|billion)?(?![a-z])")


def _numbers(text: str) -> list[float]:
    """Every number in ``text`` with its magnitude word applied ("4.5 mn" -> 4500000.0)."""
    out = []
    for digits, mag in _NUMBER.findall(text.lower()):
        try:
            out.append(float(digits.replace(",", "")) * _MAGNITUDE.get(mag, 1.0))
        except ValueError:
            continue
    return out


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

def _available_engines() -> list[str]:
    """Only engines that will actually run — an unconfigured engine silently falls back to DDG,
    which would repeat the same search and fake an independent Chao2 capture occasion."""
    engines = ["ddg"]
    if config.BRAVE_API_KEY:
        engines.append("brave")
    if config.SEARXNG_URL:
        engines.append("searxng")
    return engines


# "news" never changed the search itself, so it only duplicated captures.
_SOURCE_TYPES = ["general"]
MAX_REPLANS = 3
MIN_CAPTURES_FOR_STOP = 3


def _next_capture_params(
    spec,
    used_combos: set[tuple],
) -> tuple[str, str, str] | None:
    """Return (query, engine, source_type) not yet tried, or None if exhausted."""
    for query in spec.search_queries:
        for engine in _available_engines():
            for source_type in _SOURCE_TYPES:
                combo = (query, engine, source_type)
                if combo not in used_combos:
                    return combo
    return None


# ---------------------------------------------------------------------------
# Error formatting helper
# ---------------------------------------------------------------------------

def format_error(exc: Exception) -> str:
    """Dynamically extracts a clean, human-readable error message without hardcoding."""
    if exc is None:
        return "An unknown error occurred."

    # 1. Check for standard structured body on modern API/SDK exceptions (Groq, OpenAI, etc.)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            failed_gen = err.get("failed_generation")
            if msg and isinstance(msg, str):
                if failed_gen and isinstance(failed_gen, str) and failed_gen.strip():
                    clean_msg = msg.replace(" See 'failed_generation' for more details.", "").strip()
                    return f"{clean_msg} ({failed_gen.strip()})" if clean_msg else failed_gen.strip()
                return msg.strip()
        elif isinstance(err, str) and err.strip():
            return err.strip()
        if "message" in body and isinstance(body["message"], str):
            return body["message"].strip()

    # 2. Check for HTTP response JSON (requests, httpx)
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "json"):
        try:
            data = response.json()
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict) and isinstance(err.get("message"), str):
                    return err["message"].strip()
                if isinstance(data.get("message"), str):
                    return data["message"].strip()
        except Exception:
            pass

    # 3. Handle cases where the exception string itself contains stringified dict/JSON
    raw = str(exc).strip()
    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        candidate = raw[start:end]
        parsed = None
        try:
            parsed = json.loads(candidate)
        except Exception:
            try:
                parsed = ast.literal_eval(candidate)
            except Exception:
                pass
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                failed_gen = err.get("failed_generation")
                if msg and isinstance(msg, str):
                    if failed_gen and isinstance(failed_gen, str) and failed_gen.strip():
                        clean_msg = msg.replace(" See 'failed_generation' for more details.", "").strip()
                        return f"{clean_msg} ({failed_gen.strip()})" if clean_msg else failed_gen.strip()
                    return msg.strip()
            elif isinstance(err, str) and err.strip():
                return err.strip()
            if "message" in parsed and isinstance(parsed["message"], str):
                return parsed["message"].strip()

    return raw or "An unexpected pipeline error occurred."


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
        error_msg = format_error(exc)
        db.update_run(run_id, status="failed", error=error_msg, finished_at=db.now())
        _publish_event(run_id, "error", {"message": error_msg})
        return

    db.update_run(run_id, data_spec=dataspec.model_dump(), status="discovering")
    _publish_event(run_id, "status_change", {"status": "discovering"})

    primary_field = dataspec.fields[0].name
    field_types = {f.name: f.type for f in dataspec.fields}
    # A date written without a year ("Jan 20") borrows the year from the spec's filters
    # ("release_year:2025") only when they name exactly one year.
    filter_year = single_year(" ".join(dataspec.filters))

    # capture_history: entity_key -> set of capture_ids that found it (for Chao2)
    capture_history: dict[str, set[str]] = {}
    used_combos: set[tuple] = set()
    recent_new_counts: list[int] = []
    replans_done = 0
    seen_keys: set[str] = set()

    def _entity_key(primary_value) -> str:
        key = match_existing_key(resolve_entity_key(str(primary_value)), seen_keys)
        seen_keys.add(key)
        return key

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
        "s_hat": None,
        "jev_chunks_gated": 0,
        "jev_chunks_total": 0,
        "jev_records_dropped": 0,
    }

    db.update_run(run_id, status="fetching")
    _publish_event(run_id, "status_change", {"status": "fetching"})

    def _stop_if_cancelled() -> bool:
        """Return True (after writing final state) if the user requested cancellation.

        Checked both between captures and between URLs within a capture — a capture
        can involve many pages, each running a synchronous CPU verifier pass per
        claim, so checking only once per capture left "Stop Searching" unresponsive
        for minutes on a large page.
        """
        if not _is_run_cancelled(run_id):
            return False
        logger.info("run %s: cancellation requested — stopping", run_id)
        stats["records_after_dedupe"] = len(db.list_records(run_id))
        db.update_run(run_id, status="cancelled", stats=stats, finished_at=db.now())
        _publish_event(run_id, "status_change", {"status": "cancelled"})
        _publish_event(run_id, "done", {"stats": stats})
        return True

    # --- 2. Capture loop ---
    for _cap_iter in range(config.MAX_CAPTURES_PER_RUN):

        if _stop_if_cancelled():
            return

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
        gated_at_start = stats["jev_chunks_gated"]
        chunks_at_start = stats["jev_chunks_total"]

        for candidate in candidates:
            if _stop_if_cancelled():
                return

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

            if _stop_if_cancelled():
                return

            stats["pages_fetched"] += 1
            new_here = 0

            for chunk_text in chunks:
                # --- J1: Jev page relevance gate (before spending a Groq call) ---
                stats["jev_chunks_total"] += 1
                if config.SCOUT_JEV_ENABLED:
                    try:
                        noul = jev_mod.page_gate(
                            wanted_entity=dataspec.entity,
                            wanted_filters=dataspec.filters,
                            page_url=url,
                            page_text=chunk_text,
                        )
                        if noul < config.JEV_PAGE_GATE:
                            stats["jev_chunks_gated"] += 1
                            db.log_source(run_id, url, domain, "skipped",
                                          f"jev page gate ({noul:.2f})", capture_id=capture_id)
                            continue
                    except jev_mod.JevUnavailable as exc:
                        logger.debug("jev page_gate unavailable, extracting anyway: %s", exc)

                # --- Extract ---
                try:
                    records = llm.extract_records(dataspec, url, chunk_text)
                except Exception as exc:
                    logger.warning("extraction failed for %s: %s", url, exc)
                    continue

                if _stop_if_cancelled():
                    return

                if not records:
                    continue

                # --- Build fields/provenance for every record in this chunk first,
                # so J3 (filter check) and J2 (claim support) can run as one
                # batched Jev call per chunk instead of per-record/per-claim.
                built: list[dict] = []
                for record in records:
                    evidence = record.get("evidence", {})
                    fields: dict = {}
                    provenance: dict = {}
                    verified_count = 0

                    for field in dataspec.fields:
                        value   = record.get(field.name)
                        ev_text = evidence.get(field.name)

                        quote_on_page = _verify_evidence(ev_text, chunk_text)
                        value_in_ev   = _value_in_quote(value, ev_text)
                        verified      = quote_on_page and value_in_ev

                        # Keep all extracted values regardless of verification;
                        # unverified fields are flagged in the provenance drawer.
                        # Nulling secondary fields (e.g. URL) was dropping values
                        # that live in HTML attributes, not visible text.

                        if verified:
                            verified_count += 1

                        # Normalise only after verification, which needs the page's own wording.
                        year_hint = single_year(ev_text) or filter_year
                        norm_value = normalize_value(value, field_types.get(field.name, "string"), year_hint)

                        fields[field.name] = norm_value
                        provenance[field.name] = {
                            "url":        url,
                            "evidence":   ev_text if verified else None,
                            "verified":   verified,
                            "capture_id": capture_id,
                        }
                        if norm_value != value:
                            provenance[field.name]["raw_value"] = value

                    primary_value = fields.get(primary_field)
                    if not primary_value:
                        continue

                    built.append({
                        "primary_value": primary_value,
                        "fields": fields,
                        "provenance": provenance,
                    })

                if not built:
                    continue

                # --- J3: Jev filter check, batched across every record in this chunk ---
                if config.SCOUT_JEV_ENABLED and dataspec.filters:
                    jev_records = [
                        {**b["fields"], "evidence": chunk_text[:1500]} for b in built
                    ]
                    try:
                        scores = jev_mod.filter_check(jev_records, dataspec.filters)
                        keep: list[dict] = []
                        for b, row in zip(built, scores):
                            if row and min(row) < config.JEV_FILTER_DROP:
                                stats["jev_records_dropped"] += 1
                                continue
                            b_uncertain = any(config.JEV_FILTER_DROP <= s < 0.5 for s in row)
                            # Every provenance value must stay a per-field dict (main.py's
                            # export_csv and truth.py's resolve_run_conflicts both iterate
                            # provenance.values() assuming that) — so filter scores are
                            # attached to the primary field's provenance, not top-level.
                            b["provenance"][primary_field]["jev_filters"] = dict(zip(dataspec.filters, row))
                            b["filter_uncertain"] = b_uncertain
                            keep.append(b)
                        built = keep
                    except jev_mod.JevUnavailable as exc:
                        logger.debug("jev filter_check unavailable, skipping: %s", exc)

                if not built:
                    continue

                # --- J2: Jev/DeBERTa claim support, batched across the chunk's claims ---
                claim_plan: list[tuple[dict, str, str | None]] = []  # (built_rec, field_name, ev)
                for b in built:
                    for field in dataspec.fields:
                        v  = b["fields"].get(field.name)
                        ev = b["provenance"][field.name].get("evidence")
                        if v is not None or ev is not None:
                            claim_plan.append((b, field.name, ev))

                support_scores: list[float | None] = [None] * len(claim_plan)
                scoreable = [
                    (i, b, fname, ev) for i, (b, fname, ev) in enumerate(claim_plan)
                    if ev is not None and b["fields"].get(fname) is not None
                ]
                if scoreable:
                    # Jev needs a field_description per claim; DeBERTa's batch fn takes
                    # one shared field_description, so only mix same-field claims there.
                    if config.SCOUT_VERIFIER == "jev":
                        field_desc = {f.name: f.description for f in dataspec.fields}
                        try:
                            scores = jev_mod.claim_support_batch([
                                {
                                    "entity": str(b["primary_value"]),
                                    "field": fname,
                                    "field_description": field_desc.get(fname, fname),
                                    "value": str(b["provenance"][fname].get("raw_value", b["fields"][fname])),
                                    "quote": ev,
                                }
                                for _, b, fname, ev in scoreable
                            ])
                        except jev_mod.JevUnavailable as exc:
                            logger.debug("jev claim_support unavailable, falling back: %s", exc)
                            scores = None
                        if scores is not None:
                            for (i, *_), s in zip(scoreable, scores):
                                support_scores[i] = s
                    if all(support_scores[i] is None for i, *_ in scoreable):
                        # Jev disabled/unavailable — fall back to per-field DeBERTa batches
                        by_field: dict[str, list[tuple[int, dict, str]]] = {}
                        for i, b, fname, ev in scoreable:
                            by_field.setdefault(fname, []).append((i, b, ev))
                        for fname, items in by_field.items():
                            field_description = next(f.description for f in dataspec.fields if f.name == fname)
                            claims_batch = [{"value_raw": b["provenance"][fname].get("raw_value", b["fields"][fname]), "quote": ev} for i, b, ev in items]
                            try:
                                scores = verifier_mod.score_claims_batch(
                                    claims_batch, entity_name="", field_description=field_description,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("deberta batch scoring failed for %s: %s", fname, exc)
                                scores = [0.5] * len(items)
                            for (i, _b, _ev), s in zip(items, scores):
                                support_scores[i] = s

                for idx, (b, fname, ev) in enumerate(claim_plan):
                    v = b["fields"].get(fname)
                    db.insert_claim(
                        run_id=run_id,
                        entity_id=_entity_key(b["primary_value"]),
                        field=fname,
                        value_raw=str(v) if v is not None else None,
                        value_norm=_norm_value(v, field_types.get(fname, "string")),
                        source_url=url,
                        quote=ev,
                        support_score=support_scores[idx],
                        capture_id=capture_id,
                        extractor=f"llm:{config.MODEL_EXTRACT}",
                    )

                # --- Upsert records now that filtering + scoring is done ---
                for b in built:
                    primary_value = b["primary_value"]
                    fields = b["fields"]
                    provenance = b["provenance"]
                    entity_key = _entity_key(primary_value)
                    verified_count = sum(1 for f in dataspec.fields if provenance[f.name]["verified"])
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
                        "filter_uncertain": b.get("filter_uncertain", False),
                    })

                    # Update Chao2 capture history
                    capture_history.setdefault(entity_key, set()).add(capture_id)

            db.log_source(run_id, url, domain, "ok",
                          records_found=new_here, capture_id=capture_id)

        recent_new_counts.append(new_in_capture)

        # --- Adaptive re-plan: a capture that surfaced nothing new means the queries are off.
        # Ask the model for fresh angles instead of recombining the same bad queries. ---
        # J1 gating nearly every chunk is an even clearer "off-topic results" signal.
        chunks_here = stats["jev_chunks_total"] - chunks_at_start
        mostly_gated = chunks_here > 0 and (stats["jev_chunks_gated"] - gated_at_start) >= 0.9 * chunks_here
        if (new_in_capture == 0 or mostly_gated) and replans_done < MAX_REPLANS:
            replans_done += 1
            try:
                fresh = llm.replan_queries(
                    dataspec, list(dataspec.search_queries),
                    [r["entity_key"] for r in db.list_records(run_id)],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("run %s: replan failed: %s", run_id, exc)
                fresh = []
            if fresh:
                logger.info("run %s: replanned with %s", run_id, fresh)
                # Try the fresh queries next, ahead of the remaining (likely similar) originals.
                pos = dataspec.search_queries.index(query) + 1
                dataspec.search_queries[pos:pos] = fresh
                _publish_event(run_id, "replan", {"queries": fresh})

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
            stats["s_hat"]             = round(s_hat, 1)
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
            enough_evidence = (
                T >= MIN_CAPTURES_FOR_STOP
                and s_obs >= min(dataspec.target_count, 5)
            )
            if coverage_lo >= target and enough_evidence:
                logger.info("run %s: coverage target %.0f%% reached — stopping",
                            run_id, target * 100)
                break
            if marginal_yield(recent_new_counts) < config.MIN_MARGINAL_YIELD and T >= 4:
                logger.info("run %s: marginal yield flat — stopping", run_id)
                break
        else:
            db.update_run(run_id, stats=stats)

    # --- 3. Resolve conflicts (pillar C — Knowledge-Based Trust) ---
    try:
        resolved = truth_mod.resolve_run_conflicts(run_id)
        stats["conflicts_resolved"] = sum(resolved.values())
    except Exception as exc:  # noqa: BLE001
        logger.warning("run %s: conflict resolution failed: %s", run_id, exc)
        stats["conflicts_resolved"] = 0

    # --- 4. Finish ---
    stats["records_after_dedupe"] = len(db.list_records(run_id))
    if stats["records_after_dedupe"] == 0:
        gated = stats["jev_chunks_gated"]
        total = stats["jev_chunks_total"]
        error_msg = (
            "Scout found no usable records. "
            f"{stats['urls_discovered']} pages were found over {stats['captures_run']} searches, "
            f"but {gated} of {total} page sections were judged irrelevant to your request"
            if total else
            f"Scout found no pages it could read ({stats['urls_discovered']} found, "
            f"{stats['pages_failed']} failed to load)"
        ) + ". Try rephrasing with a more specific topic, place, or year."
        db.update_run(run_id, status="failed", stats=stats, error=error_msg, finished_at=db.now())
        _publish_event(run_id, "error", {"message": error_msg})
        return
    db.update_run(run_id, status="done", stats=stats, finished_at=db.now())
    _publish_event(run_id, "done", {"stats": stats})

    # Close the per-thread event loop
    if hasattr(_thread_loop, "loop") and not _thread_loop.loop.is_closed():
        _thread_loop.loop.close()
