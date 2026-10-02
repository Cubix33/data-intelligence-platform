"""Deterministic value normalisation for extracted fields.

The extractor returns values exactly as written on the page ("Jan 20", "671B",
"10 July 2025"). That makes the same fact look different across sources, so
corroboration and conflict detection fail and the exported dataset is messy.

These helpers turn ``date`` fields into ISO 8601 and ``number`` fields into plain
numbers. They are conservative: if a value is ambiguous or unparseable it is returned
unchanged — we never invent a missing year, day or magnitude.
"""

from __future__ import annotations

import calendar
import re

_MONTHS = {
    name.lower(): i
    for i, name in enumerate(calendar.month_name) if name
} | {
    name.lower(): i
    for i, name in enumerate(calendar.month_abbr) if name
} | {"sept": 9}

_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

_ISO_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")
# "10 July 2025", "10th July, 2025", "10 July"
_DMY = re.compile(rf"^(\d{{1,2}}){_ORD}\s+({_MONTH_RE})\.?,?(?:\s+(\d{{4}}))?$", re.I)
# "July 10, 2025", "July 10th", "Jul 10"
_MDY = re.compile(rf"^({_MONTH_RE})\.?\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?$", re.I)
# "May 2024", "May, 2024"
_MY = re.compile(rf"^({_MONTH_RE})\.?,?\s+(\d{{4}})$", re.I)


def _iso(year: int, month: int, day: int | None = None) -> str | None:
    if not (1 <= month <= 12):
        return None
    if day is None:
        return f"{year:04d}-{month:02d}"
    if not (1 <= day <= calendar.monthrange(year, month)[1]):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def single_year(text: str | None) -> int | None:
    """The one distinct 4-digit year mentioned in ``text``, or None if zero or several."""
    years = {int(y) for y in _YEAR_RE.findall(text or "")}
    return years.pop() if len(years) == 1 else None


def normalize_date(value, year_hint: int | None = None) -> str:
    """Return ``value`` as ISO 8601 (YYYY-MM-DD, YYYY-MM or YYYY) when it is unambiguous.

    ``year_hint`` is only used when the text has a day and month but no year
    ("Jan 20"); without a hint such values are returned unchanged.
    Numeric forms like "01/02/2025" are ambiguous (d/m vs m/d) and are left alone.
    """
    if not isinstance(value, str):
        return value
    s = re.sub(r"\s+", " ", value).strip()
    if not s:
        return value

    if m := _ISO_FULL.match(s):
        out = _iso(int(m[1]), int(m[2]), int(m[3]))
        return out or value
    if m := _ISO_MONTH.match(s):
        return _iso(int(m[1]), int(m[2])) or value
    if _YEAR_ONLY.match(s):
        return s

    if m := _DMY.match(s):
        day, month, year = int(m[1]), _MONTHS[m[2].lower()], m[3]
    elif m := _MDY.match(s):
        month, day, year = _MONTHS[m[1].lower()], int(m[2]), m[3]
    elif m := _MY.match(s):
        return _iso(int(m[2]), _MONTHS[m[1].lower()]) or value
    else:
        return value

    year_i = int(year) if year else year_hint
    if year_i is None:
        return value
    return _iso(year_i, month, day) or value


_MAGNITUDE = {
    "k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5,
    "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "tn": 1e12, "trillion": 1e12,
}
_FILLER = re.compile(
    r"\b(?:usd|inr|rs\.?|dollars?|rupees?|parameters?|params?|approx\.?|approximately|about|around|over|nearly)\b|[~≈$₹€£]",
    re.I,
)
_NUMBER = re.compile(r"^(\d[\d,]*(?:\.\d+)?)\s*([a-z]+)?$", re.I)


def normalize_number(value):
    """Return ``value`` as an int/float, expanding magnitude suffixes ("671B" -> 671000000000).

    Anything that is not a single number with an optional magnitude word — ranges, lists
    ("7B/70B"), percentages, text — is returned unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return value
    s = _FILLER.sub(" ", value.lower())
    s = re.sub(r"\s+", " ", s).strip()
    m = _NUMBER.match(s)
    if not m:
        return value
    suffix = (m[2] or "").lower()
    if suffix and suffix not in _MAGNITUDE:
        return value
    try:
        num = float(m[1].replace(",", "")) * _MAGNITUDE.get(suffix, 1.0)
    except ValueError:
        return value
    return int(round(num)) if abs(num - round(num)) < 1e-6 else num


def normalize_value(value, field_type: str, year_hint: int | None = None):
    """Normalise ``value`` according to the spec's field type; other types pass through."""
    if value is None or value == "":
        return value
    if field_type == "date":
        return normalize_date(value, year_hint)
    if field_type == "number":
        return normalize_number(value)
    return value
