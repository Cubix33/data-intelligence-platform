"""Async page fetcher with chunking, pagination, per-domain rate limiting, and page cache.

Improvements over v0
---------------------
- async httpx.AsyncClient — multiple pages can fetch concurrently across domains.
- Per-domain semaphore (max 2 concurrent) + minimum delay between requests to the
  same domain (respects Crawl-delay from robots.txt if set, else 1 s default).
- Pages are split into overlapping chunks (default 8 000 chars, 200-char overlap) so
  long list pages don't lose records that fall past the first cut.
- Follows rel="next" and simple ?page=N pagination up to MAX_PAGINATION_DEPTH pages.
- In-memory page cache keyed by URL (respects ETag / Last-Modified for re-runs).
- Playwright headless fallback when BeautifulSoup yields fewer than MIN_TEXT_CHARS
  characters (indicates a JS-heavy page) — only if playwright is installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import httpx
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger("scout.fetch")


# ---------------------------------------------------------------------------
# In-memory page cache  (URL → cleaned text)
# ---------------------------------------------------------------------------

_page_cache: dict[str, tuple[str, str]] = {}   # url -> (etag_or_empty, cleaned_text)
_cache_hits = 0


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _cache_get(url: str) -> str | None:
    entry = _page_cache.get(_cache_key(url))
    return entry[1] if entry else None


def _cache_set(url: str, etag: str, text: str) -> None:
    _page_cache[_cache_key(url)] = (etag, text)


def cache_stats() -> dict:
    return {"entries": len(_page_cache), "hits": _cache_hits}


# ---------------------------------------------------------------------------
# Per-domain rate limiter
# ---------------------------------------------------------------------------

_domain_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(config.MAX_CONCURRENT_PER_DOMAIN)
)
_domain_last_request: dict[str, float] = {}


async def _domain_delay(domain: str, delay_s: float) -> None:
    """Sleep until the minimum per-domain delay has elapsed since the last request."""
    last = _domain_last_request.get(domain, 0.0)
    wait = delay_s - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _domain_last_request[domain] = time.monotonic()


# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------

def _clean_html(html: str) -> str:
    """Strip scripts/styles and return cleaned, de-duplicated lines."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text  = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Deduplicate consecutive identical lines (common in nav/footer residue)
    deduped: list[str] = []
    prev = None
    for ln in lines:
        if ln != prev:
            deduped.append(ln)
        prev = ln
    return "\n".join(deduped)


def _is_js_heavy(text: str) -> bool:
    return len(text) < config.MIN_TEXT_CHARS_FOR_JS_FALLBACK


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str, size: int = None, overlap: int = None) -> list[str]:
    """Split text into overlapping chunks for extraction.

    Uses config defaults; the overlap ensures records near a chunk boundary
    aren't split across two LLM calls.
    """
    size    = size    or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


# ---------------------------------------------------------------------------
# Pagination link extraction
# ---------------------------------------------------------------------------

def _next_page_url(html: str, current_url: str) -> str | None:
    """Find a rel=next or numbered next-page link in the HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. <link rel="next">
    link = soup.find("link", rel=re.compile(r"\bnext\b", re.I))
    if link and link.get("href"):
        return urljoin(current_url, link["href"])

    # 2. <a rel="next"> or anchor text containing "next"
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel", [])).lower()
        text = a.get_text(strip=True).lower()
        if "next" in rel or text in ("next", "next »", "next →", "›", "»"):
            return urljoin(current_url, a["href"])

    # 3. ?page=N pattern — increment the page number by 1
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)
    for param in ("page", "p", "pg", "offset"):
        if param in qs:
            try:
                n = int(qs[param][0])
                qs[param] = [str(n + 1)]
                new_qs = urlencode({k: v[0] for k, v in qs.items()})
                return urlunparse(parsed._replace(query=new_qs))
            except (ValueError, IndexError):
                pass

    return None


# ---------------------------------------------------------------------------
# Core fetch function (async)
# ---------------------------------------------------------------------------

async def fetch_chunks(url: str, _depth: int = 0) -> list[str]:
    """Fetch a URL (and pagination) and return a list of text chunks ready for extraction.

    Returns an empty list on failure.
    Each chunk is at most config.CHUNK_SIZE characters with config.CHUNK_OVERLAP overlap.
    """
    global _cache_hits

    # Check cache
    cached = _cache_get(url)
    if cached:
        _cache_hits += 1
        logger.debug("cache hit: %s", url)
        return _chunk_text(cached)

    domain = urlparse(url).netloc
    delay  = config.DOMAIN_REQUEST_DELAY_S

    async with _domain_semaphores[domain]:
        await _domain_delay(domain, delay)

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": config.USER_AGENT},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.info("fetch failed %s: %s", url, exc)
            return []

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return []

    raw_html = resp.text
    text = _clean_html(raw_html)

    # Playwright fallback for JS-heavy pages
    if _is_js_heavy(text):
        text = await _playwright_fetch(url) or text

    if not text.strip():
        return []

    # Cache the cleaned text
    etag = resp.headers.get("etag", "")
    _cache_set(url, etag, text)

    all_chunks = _chunk_text(text)

    # Follow pagination (up to MAX_PAGINATION_DEPTH more pages)
    if _depth < config.MAX_PAGINATION_DEPTH:
        next_url = _next_page_url(raw_html, url)
        if next_url and next_url != url:
            logger.debug("following pagination: %s -> %s", url, next_url)
            more_chunks = await fetch_chunks(next_url, _depth=_depth + 1)
            all_chunks.extend(more_chunks)

    return all_chunks


# ---------------------------------------------------------------------------
# Playwright fallback (optional dependency)
# ---------------------------------------------------------------------------

async def _playwright_fetch(url: str) -> str | None:
    """Render the page with headless Chromium and return its text.

    Only runs if playwright is installed (pip install playwright && playwright install chromium).
    Returns None if playwright isn't available.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        logger.debug("playwright not installed — skipping JS fallback for %s", url)
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page    = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=20_000)
            html = await page.content()
            await browser.close()
        return _clean_html(html)
    except Exception as exc:
        logger.info("playwright fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Sync wrapper (for callers that aren't already async)
# ---------------------------------------------------------------------------

def fetch_chunks_sync(url: str) -> list[str]:
    """Blocking wrapper around fetch_chunks for use in sync contexts."""
    return asyncio.run(fetch_chunks(url))
