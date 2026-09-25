"""All LLM calls live here: intent parsing (Groq), web discovery (DuckDuckGo), and per-page extraction (Groq)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import groq
from ddgs import DDGS

from . import config

if TYPE_CHECKING:
    from .models import DataSpec

logger = logging.getLogger("scout.llm")

_client: groq.Groq | None = None


def client() -> groq.Groq:
    global _client
    if _client is None:
        _client = groq.Groq(api_key=config.GROQ_API_KEY)
    return _client


def _chat_json(model: str, system: str, user: str, schema: dict, schema_name: str, max_tokens: int = 4000) -> Any:
    """One structured-output chat call, returning parsed JSON."""
    response = client().chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        },
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# 1. Intent -> DataSpec
# ---------------------------------------------------------------------------

INTENT_SYSTEM = """You turn a plain-English data request into a structured data collection spec.
Design a small set of columns (5-8) that best capture what the user asked for; always make the
first field a primary identifying field (a name or title an entity is known by). Write 3-6 distinct,
high-signal web search queries that would surface pages listing or describing these entities. Keep
field names snake_case. Prefer fields that are actually likely to be stated on public web pages."""

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


# ---------------------------------------------------------------------------
# 3. Per-page extraction, with mandatory evidence quotes
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """You extract structured records from a single web page for a data collection task.
Only extract records that clearly match the entity type and satisfy the stated filters. Every field
value you output MUST be backed by a verbatim quote from the page text, placed in the matching
"evidence" slot — copy the exact wording, don't paraphrase. If a field isn't stated on the page, set
both the value and its evidence to null. Never invent values. If the page lists none of the requested
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
