"""The Compliance Gate: the one thing every fetch has to pass through.

First draft is intentionally simple — a robots.txt check plus a small deny list for
login-walled / ToS-sensitive domains we should never scrape directly. This is the seed
of the "Source Ledger" feature described in IDEATION.md; every decision made here is
logged by the pipeline so it shows up in the UI.
"""

from __future__ import annotations

import functools
import logging
import urllib.robotparser
from urllib.parse import urlparse

from . import config

logger = logging.getLogger("scout.compliance")

DENYLIST = {"linkedin.com", "www.linkedin.com", "facebook.com", "instagram.com"}


@functools.lru_cache(maxsize=256)
def _parser_for(domain: str, scheme: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{scheme}://{domain}/robots.txt")
    try:
        rp.read()
    except Exception:  # noqa: BLE001 - robots.txt fetch is best-effort
        logger.info("could not read robots.txt for %s", domain)
    return rp


def is_allowed(url: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is only set when allowed is False."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    if not domain or not parsed.scheme.startswith("http"):
        return False, "not a fetchable http(s) URL"

    if any(domain == d or domain.endswith("." + d) for d in DENYLIST):
        return False, "domain is on the deny list (login-walled / ToS-sensitive)"

    rp = _parser_for(domain, parsed.scheme)
    try:
        allowed = rp.can_fetch(config.USER_AGENT, url)
    except Exception:  # noqa: BLE001
        allowed = True  # fail open if robots.txt itself couldn't be parsed
    return (allowed, "" if allowed else "blocked by robots.txt")
