from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger("scout.fetch")


def fetch_text(url: str) -> str | None:
    """Fetch a page and return its trimmed, script/style-free text, or None on failure."""
    try:
        resp = httpx.get(
            url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("fetch failed for %s: %s", url, exc)
        return None

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = "\n".join(lines)
    return cleaned[: config.MAX_PAGE_CHARS] if cleaned else None
