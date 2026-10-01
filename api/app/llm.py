"""All LLM calls live here: intent parsing (Groq), web discovery (DuckDuckGo), and per-page extraction (Groq)."""

from __future__ import annotations

import itertools
import json
import re
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import groq
from ddgs import DDGS

from . import config

if TYPE_CHECKING:
    from .models import DataSpec

logger = logging.getLogger("scout.llm")

# ---------------------------------------------------------------------------
# Groq client pool with key rotation
#
# GROQ_API_KEY may be a comma-separated list. Free-tier keys hit tight
# tokens-per-minute limits fast; the SDK's own retry backs off for up to
# 30s+ per call, which can stall a run for minutes and makes cancellation
# unresponsive. Instead we keep one lightweight client per key (max_retries=0,
# so 429s raise immediately) and round-robin past any key that is currently
# rate-limited.
# ---------------------------------------------------------------------------

_clients: list[groq.Groq] = []
_client_cycle = None
_lock = threading.Lock()
_cooldowns: dict[int, float] = {}  # client index -> unix time it's usable again
_cancel_event = threading.Event()  # set by pipeline to interrupt rate-limit sleeps


def request_cancel() -> None:
    """Signal that the current run is cancelled — wakes any sleeping key-rotation wait."""
    _cancel_event.set()


def clear_cancel() -> None:
    """Clear the cancel signal at the start of a new run."""
    _cancel_event.clear()


def _init_clients() -> None:
    global _clients, _client_cycle
    keys = config.GROQ_API_KEYS or ([config.GROQ_API_KEY] if config.GROQ_API_KEY else [])
    if not keys:
        raise RuntimeError("No GROQ_API_KEY configured (set it in .env)")
    _clients = [groq.Groq(api_key=k, max_retries=0) for k in keys]
    _client_cycle = itertools.cycle(range(len(_clients)))
    logger.info("llm: initialised %d Groq client(s) for key rotation", len(_clients))


def client() -> groq.Groq:
    """Return a single client (legacy callers). Prefer _chat_json for rotation."""
    with _lock:
        if not _clients:
            _init_clients()
        return _clients[0]


def _chat_json(model: str, system: str, user: str, schema: dict, schema_name: str, max_tokens: int = 4000) -> Any:
    """One structured-output chat call, returning parsed JSON.

    Rotates across all configured Groq keys on rate-limit (429) errors before
    falling back to a short sleep, so one key running dry doesn't stall the
    whole capture loop (and, in turn, doesn't block a run's cancel button).
    """
    with _lock:
        if not _clients:
            _init_clients()
        n = len(_clients)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema},
    }

    last_exc: Exception | None = None
    # Rate-limited pages used to be dropped outright; wait out the cooldown and retry
    # (up to 3 rounds) so a busy key pool slows a run down instead of losing data.
    for _round in range(3):
        for _attempt in range(n * 2):  # two full passes over the key pool
            with _lock:
                idx = next(_client_cycle)
                ready_at = _cooldowns.get(idx, 0.0)
            now = time.monotonic()
            if ready_at > now and n > 1:
                continue  # skip a key still in cooldown while others are available

            try:
                response = _clients[idx].chat.completions.create(
                    model=model, temperature=0, max_tokens=max_tokens,
                    messages=messages, response_format=response_format,
                )
                return json.loads(response.choices[0].message.content)
            except groq.RateLimitError as exc:
                last_exc = exc
                # Cool this key down; Groq's error usually names a retry-after in
                # its message, but we don't parse it — a flat 15s keeps this simple.
                with _lock:
                    _cooldowns[idx] = time.monotonic() + 15.0
                logger.info("llm: key #%d rate-limited, rotating (%s)", idx, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("llm: call failed on key #%d: %s", idx, exc)
                continue

        if not isinstance(last_exc, groq.RateLimitError) or _cancel_event.is_set():
            break  # only rate limits are worth waiting out
        if "per day" in str(last_exc).lower():
            break  # daily quota (TPD) won't reset in seconds — fail fast instead of stalling the run
        if all(_cooldowns.get(i, 0.0) > time.monotonic() for i in range(n)):
            # Every key is cooling down — wait out the shortest one rather than
            # raising immediately, so a single tight loop doesn't burn the whole pool.
            wait = max(0.5, min(_cooldowns.values()) - time.monotonic())
            logger.info("llm: all %d key(s) rate-limited, waiting %.1fs", n, wait)
            _cancel_event.wait(timeout=min(wait, 15.0))

    raise last_exc or RuntimeError("Groq call failed with no configured keys")


# ---------------------------------------------------------------------------
# 1. Intent -> DataSpec
# ---------------------------------------------------------------------------

INTENT_SYSTEM = """You turn a plain-English data request into a structured data collection spec.
Design a small set of columns (5-8) that best capture what the user asked for; always make the
first field a primary identifying field (a name or title an entity is known by). Write 3-6 distinct,
high-signal web search queries that would surface pages listing or describing these entities. Keep
field names snake_case. Prefer fields that are actually likely to be stated on public web pages.

IMPORTANT — search query strategy: write plain, natural-language keyword queries (4-10 words) the way a
person would type into a search engine, e.g. 'Indian edtech startups seed funding 2024 investors' or
'open source vision language models 2025 benchmark comparison'. Do NOT use quotation marks, boolean
operators, or more than one site: filter per query (most queries should have none) — over-constrained queries
return nothing on the search engines we use. Make the queries genuinely different angles (list/roundup
articles, news announcements, official directories or leaderboards, primary sources) rather than rewordings.
Avoid job boards (LinkedIn, Glassdoor, Indeed, Wellfound, Naukri, Internshala) and login-walled sites —
they block crawlers. Always include the entity type and the key filters (year, country, topic) in the query
so results stay on-topic."""

# Groq structured outputs don't support minItems/maxItems — pydantic enforces those instead.
_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "entity": {"type": "string"},
        "summary": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "type": {"type": "string", "enum": ["string", "number", "boolean", "date", "url"]},
                    "required": {"type": "boolean"},
                },
                "required": ["name", "description", "type", "required"],
                "additionalProperties": False,
            },
        },
        "filters": {"type": "array", "items": {"type": "string"}},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "target_count": {"type": "integer"},
    },
    "required": ["entity", "summary", "fields", "filters", "search_queries", "target_count"],
    "additionalProperties": False,
}


def parse_intent(prompt: str) -> "DataSpec":
    from .models import DataSpec  # avoid a module-level cycle

    parsed = _chat_json(config.MODEL_INTENT, INTENT_SYSTEM, prompt, _INTENT_SCHEMA, "data_spec")
    return DataSpec.model_validate(parsed)


# ---------------------------------------------------------------------------
# 2. Discovery via DuckDuckGo (free, no API key)
# ---------------------------------------------------------------------------


def discover_urls(dataspec: "DataSpec") -> list[dict]:
    """Run each search query through DuckDuckGo and collect candidate URLs."""
    seen: dict[str, str] = {}
    for query in dataspec.search_queries:
        try:
            results = DDGS().text(query, max_results=config.SEARCH_RESULTS_PER_QUERY)
        except Exception as exc:  # DDG rate limits / network hiccups
            logger.warning("search failed for %r: %s", query, exc)
            continue

        for result in results:
            url = result.get("href") or result.get("url")
            title = result.get("title")
            if url and url not in seen:
                seen[url] = title or url

    return [{"url": u, "title": t} for u, t in seen.items()]


_SITE_OP = re.compile(r"\bsite:\S+", re.IGNORECASE)
_BOOL_OP = re.compile(r"\b(AND|OR|NOT)\b")
MIN_RESULTS_BEFORE_RELAX = 3


def relax_query(query: str) -> str:
    """Strip search operators that over-constrain the engine (quotes, site:, AND/OR/NOT, -term)."""
    q = _SITE_OP.sub(" ", query)
    q = q.replace('"', " ").replace("“", " ").replace("”", " ")
    q = _BOOL_OP.sub(" ", q)
    q = re.sub(r"(^|\s)-\w+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def discover_urls_for_query(dataspec: "DataSpec", query: str, engine: str = "ddg") -> list[dict]:
    """Search ``query``; if the engine returns too little, retry with a relaxed (operator-free) query."""
    found = _discover_once(dataspec, query, engine)
    if len(found) < MIN_RESULTS_BEFORE_RELAX:
        relaxed = relax_query(query)
        if relaxed and relaxed != query:
            logger.info("search: %d result(s) for %r — retrying relaxed %r", len(found), query, relaxed)
            seen = {c["url"] for c in found}
            found += [c for c in _discover_once(dataspec, relaxed, engine) if c["url"] not in seen]
    return found


def _discover_once(dataspec: "DataSpec", query: str, engine: str = "ddg") -> list[dict]:
    """Run a single search query on the specified engine and return candidate URLs.

    Supported engines:
      - "ddg"     : DuckDuckGo (always available, no key needed)
      - "brave"   : Brave Search API (requires BRAVE_API_KEY in config)
      - "searxng" : Self-hosted SearXNG instance (requires SEARXNG_URL in config)

    Falls back to DuckDuckGo if the requested engine isn't configured.
    """
    engine = engine.lower()
    seen: dict[str, str] = {}

    if engine == "brave" and config.BRAVE_API_KEY:
        _discover_brave(query, seen)
    elif engine == "searxng" and config.SEARXNG_URL:
        _discover_searxng(query, seen)
    else:
        if engine not in ("ddg",):
            logger.debug("engine %r not configured, falling back to ddg", engine)
        _discover_ddg(query, seen)

    return [{"url": u, "title": t} for u, t in seen.items()]


def _discover_ddg(query: str, seen: dict) -> None:
    # ddgs scrapes DuckDuckGo and fails transiently (rate limits, timeouts) from cloud IPs;
    # one backed-off retry recovers most of those. "No results found" is final, not retried.
    for attempt in range(2):
        try:
            results = DDGS().text(query, max_results=config.SEARCH_RESULTS_PER_QUERY)
        except Exception as exc:  # noqa: BLE001
            if "no results" in str(exc).lower() or attempt == 1:
                logger.warning("DuckDuckGo search failed for %r: %s", query, exc)
                return
            time.sleep(2.0)
            continue
        for r in results:
            url = r.get("href") or r.get("url")
            if url and url not in seen:
                seen[url] = r.get("title") or url
        return


def _discover_brave(query: str, seen: dict) -> None:
    """Brave Search API (https://api.search.brave.com/res/v1/web/search)."""
    try:
        import httpx as _httpx
        resp = _httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": config.SEARCH_RESULTS_PER_QUERY},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": config.BRAVE_API_KEY,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        for r in resp.json().get("web", {}).get("results", []):
            url = r.get("url")
            if url and url not in seen:
                seen[url] = r.get("title") or url
    except Exception as exc:
        logger.warning("Brave search failed for %r: %s", query, exc)
        _discover_ddg(query, seen)   # graceful fallback


def _discover_searxng(query: str, seen: dict) -> None:
    """SearXNG JSON API."""
    try:
        import httpx as _httpx
        resp = _httpx.get(
            config.SEARXNG_URL.rstrip("/") + "/search",
            params={"q": query, "format": "json",
                    "engines": "google,bing,duckduckgo",
                    "count": config.SEARCH_RESULTS_PER_QUERY},
            timeout=10.0,
        )
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            url = r.get("url")
            if url and url not in seen:
                seen[url] = r.get("title") or url
    except Exception as exc:
        logger.warning("SearXNG search failed for %r: %s", query, exc)
        _discover_ddg(query, seen)


_REPLAN_SYSTEM = """You are improving a web search plan that is not surfacing relevant pages.
Given the data request, the queries already tried, and the entities found so far, write 3 NEW plain
natural-language search queries (4-10 words, no quotation marks, no site: or boolean operators) that approach
the topic from a different angle: list/roundup articles, news, official directories or leaderboards, primary
sources. Keep the entity type and key filters (year, country, topic) in each query. Never repeat a tried query."""

_REPLAN_SCHEMA = {
    "type": "object",
    "properties": {"search_queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["search_queries"],
    "additionalProperties": False,
}


def replan_queries(dataspec: "DataSpec", tried: list[str], found_entities: list[str]) -> list[str]:
    """Ask the intent model for fresh queries when a capture surfaced nothing new."""
    tried_lines = "\n".join(f"- {q}" for q in tried[-12:])
    user = "\n".join([
        f"Request: {dataspec.summary}",
        f"Entity: {dataspec.entity}",
        f"Filters: {'; '.join(dataspec.filters) or '(none)'}",
        f"Queries already tried:\n{tried_lines}",
        f"Entities found so far: {', '.join(found_entities[:20]) or '(none)'}",
    ])
    parsed = _chat_json(config.MODEL_INTENT, _REPLAN_SYSTEM, user, _REPLAN_SCHEMA, "replan", max_tokens=600)
    seen = {q.lower() for q in tried}
    out: list[str] = []
    for q in parsed.get("search_queries", []):
        q = relax_query(str(q))
        if q and q.lower() not in seen:
            out.append(q)
            seen.add(q.lower())
    return out[:3]


# ---------------------------------------------------------------------------
# 3. Per-page extraction, with mandatory evidence quotes
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """You extract structured records from a single web page for a data collection task.
Only extract records that clearly match the entity type and satisfy the stated filters. Every field
value you output MUST be backed by a verbatim quote from the page text, placed in the matching
"evidence" slot — copy the exact wording, don't paraphrase. If a field isn't stated on the page, set
both the value and its evidence to null. Never invent values. Use the entity's short canonical name as its primary value (no taglines, slogans, or
parenthetical asides). When a page lists many values for one entity over time (e.g. every historical version
or past editions), return only the current / most recent one unless the filters ask for history. If the page lists none of the requested
entities, return an empty records list and set page_is_relevant to false."""


def _nullable(json_type: str) -> dict:
    return {"anyOf": [{"type": json_type}, {"type": "null"}]}


def _extraction_schema(dataspec: "DataSpec") -> dict:
    field_props: dict = {}
    evidence_props: dict = {}
    required_fields: list[str] = []
    json_type_map = {"number": "number", "boolean": "boolean"}

    for f in dataspec.fields:
        json_type = json_type_map.get(f.type, "string")
        field_props[f.name] = _nullable(json_type)
        evidence_props[f.name] = _nullable("string")
        required_fields.append(f.name)

    return {
        "type": "object",
        "properties": {
            "page_is_relevant": {"type": "boolean"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        **field_props,
                        "evidence": {
                            "type": "object",
                            "properties": evidence_props,
                            "required": required_fields,
                            "additionalProperties": False,
                        },
                    },
                    "required": required_fields + ["evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["page_is_relevant", "records"],
        "additionalProperties": False,
    }


def extract_records(dataspec: "DataSpec", url: str, page_text: str) -> list[dict]:
    fields_desc = "\n".join(
        f"- {f.name} ({f.type}{', required' if f.required else ''}): {f.description}" for f in dataspec.fields
    )
    filters_desc = "\n".join(f"- {f}" for f in dataspec.filters) or "(none)"
    user_content = (
        f"Entity to extract: {dataspec.entity}\n\n"
        f"Fields:\n{fields_desc}\n\n"
        f"Filters:\n{filters_desc}\n\n"
        f"Page URL: {url}\n\n"
        f"Page text:\n{page_text}"
    )

    parsed = _chat_json(
        config.MODEL_EXTRACT,
        EXTRACT_SYSTEM,
        user_content,
        _extraction_schema(dataspec),
        "extraction_result",
    )
    return parsed.get("records", []) if parsed.get("page_is_relevant") else []
