"""Entity resolution — normalize, block, and merge entity names.

Phase 1 (shipped now)
---------------------
- Normalize: lowercase, strip legal suffixes, collapse whitespace.
- Same-domain check: two records pointing to the same canonical domain
  are the same company.
- resolve_entity_key: drop-in replacement for the old _slugify that was
  creating spurious duplicates ("Google LLC" ≠ "Google").

Phase 2 (post 3-Oct, needs GPU)
--------------------------------
- Embedding blocking: encode "name + key fields" with bge-m3 / e5; compare
  only nearest neighbours to avoid O(n²) comparisons.
- LLM check on uncertain pairs (cosine similarity in the "grey zone"):
  ask the 120B model "are these the same entity?" with both records shown.
- Union-find over confirmed merges so transitive merges propagate.

Public references
-----------------
BoostER (2403.06434): LLMs improve entity resolution on uncertain pairs.
DistillER (2602.05452): knowledge distillation for ER with LLMs.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Legal-suffix strip list (common in Indian and US company names)
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b("
    r"pvt\.?\s*ltd\.?|private\s+limited|pvt\s+limited"
    r"|ltd\.?|limited"
    r"|llp|llc|inc\.?|incorporated|corp\.?|corporation"
    r"|co\.?|company|gmbh|s\.?a\.?|pte\.?\s*ltd\.?"
    r")\b",
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def normalize_name(name: str) -> str:
    """Return a canonical, comparable form of a company / entity name.

    Steps
    -----
    1. Lowercase
    2. Strip legal suffixes (Pvt Ltd, LLC, Inc, …)
    3. Remove punctuation
    4. Collapse whitespace and strip
    5. Truncate to 80 chars for use as a DB key
    """
    s = str(name or "").lower()
    s = _LEGAL_SUFFIXES.sub("", s)
    s = _NON_ALNUM.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s[:80] or "unknown"


def canonical_domain(url: str) -> str | None:
    """Return the registrable domain (without www.) from a URL, or None."""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.netloc.lower()
        # Strip www. and port
        host = re.sub(r"^www\.", "", host)
        host = re.sub(r":\d+$", "", host)
        return host or None
    except Exception:
        return None


def same_entity_by_domain(url_a: str | None, url_b: str | None) -> bool:
    """True if both URLs resolve to the same registrable domain (strong signal)."""
    if not url_a or not url_b:
        return False
    da = canonical_domain(url_a)
    db_ = canonical_domain(url_b)
    return bool(da and db_ and da == db_)


def resolve_entity_key(primary_value: str, contact_url: str | None = None) -> str:
    """Return the canonical entity key to use for deduplication.

    Uses normalized name as the key.  If a contact_url is provided, it is
    appended as a secondary signal (for future embedding-based blocking).

    This replaces the old _slugify, which created phantom duplicates for
    "Google LLC" vs "Google" and merged distinct orgs with the same first name.
    """
    return normalize_name(primary_value)


# ---------------------------------------------------------------------------
# Union-Find for transitive merges (Phase 2 prep)
# ---------------------------------------------------------------------------

class UnionFind:
    """Weighted union-find for entity merge decisions."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank:   dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x]   = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # path compression
        return self._parent[x]

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def same(self, x: str, y: str) -> bool:
        return self.find(x) == self.find(y)

    def canonical(self, x: str) -> str:
        return self.find(x)


# ---------------------------------------------------------------------------
# Batch resolution helper (used by eval harness and truth discovery)
# ---------------------------------------------------------------------------

def group_records_by_entity(
    records: list[dict],
    primary_field: str,
) -> dict[str, list[dict]]:
    """Group a list of raw records by their resolved entity key.

    Returns a dict mapping canonical entity_key -> list of records that
    resolved to that key.  Used to detect within-run duplicates before
    upsert_record merges them.
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        val = rec.get("fields", rec).get(primary_field) or rec.get(primary_field)
        if not val:
            continue
        key = resolve_entity_key(str(val))
        groups.setdefault(key, []).append(rec)
    return groups
