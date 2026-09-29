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

from . import config, ssrf

logger = logging.getLogger("scout.compliance")

DENYLIST = {
    # Login-walled social/professional networks
    "linkedin.com", "www.linkedin.com", "in.linkedin.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    # Job boards that block crawlers via robots.txt or require login
    "glassdoor.com", "glassdoor.co.in",
    "wellfound.com",
    "startup.jobs",
    "remoterocketship.com",
    "indeed.com",
    "naukri.com",
    "internshala.com",
}


class _FailOpenRobotParser(urllib.robotparser.RobotFileParser):
    """RobotFileParser that fails open on 401/403.

    The stdlib default sets disallow_all=True when robots.txt returns 401/403,
    treating an inaccessible robots.txt as a full crawl ban. We treat it as
    unknown and allow the fetch — the site chose not to serve a robots.txt.
    """

    def error_code(self, code: int) -> None:  # type: ignore[override]
        if code in (401, 403):
            self.allow_all = True   # fail open
        elif code >= 400:
            self.allow_all = True
        else:
            super().error_code(code)  # type: ignore[misc]


@functools.lru_cache(maxsize=256)
def _parser_for(domain: str, scheme: str) -> _FailOpenRobotParser:
    rp = _FailOpenRobotParser()
    robots_url = f"{scheme}://{domain}/robots.txt"
    rp.set_url(robots_url)
    try:
        response = ssrf.safe_get_sync(robots_url)
        if response.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(response.text.splitlines())
    except ssrf.UnsafeDestination as exc:
        logger.warning("blocked unsafe robots.txt destination %s: %s", robots_url, exc)
        rp.disallow_all = True
    except Exception:  # noqa: BLE001 - preserve best-effort behavior for network failures
        logger.info("could not read robots.txt for %s, failing open", domain)
        rp.allow_all = True
    return rp


def is_allowed(url: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is only set when allowed is False."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    host = (parsed.hostname or "").lower().rstrip(".")

    if not domain or parsed.scheme.lower() not in ("http", "https"):
        return False, "not a fetchable http(s) URL"

    if any(host == d or host.endswith("." + d) for d in DENYLIST):
        return False, "domain is on the deny list (login-walled / ToS-sensitive)"

    try:
        ssrf.validate_url_sync(url)
    except ssrf.UnsafeDestination as exc:
        return False, f"unsafe destination: {exc}"

    rp = _parser_for(domain, parsed.scheme)
    try:
        allowed = rp.can_fetch(config.USER_AGENT, url)
    except Exception:  # noqa: BLE001
        allowed = True  # fail open if robots.txt itself couldn't be parsed
    return (allowed, "" if allowed else "blocked by robots.txt")
