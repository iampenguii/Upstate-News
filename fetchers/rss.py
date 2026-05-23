"""Generic RSS / Atom fetcher.

Used for three of the bot's source types:
  - local news outlets   (WYFF, WSPA, Greenville Journal, GVLtoday)
  - the Substack proxy   (Greenville County, via the Council Chair newsletter)
  - CivicPlus calendars  (Spartanburg County, City of Spartanburg)

The feed bytes are fetched with `requests` (a browser-like User-Agent, an
explicit timeout) and then handed to `feedparser`. Letting `requests` do the
HTTP gives cleaner error handling and dodges feeds that reject feedparser's
default User-Agent.

Everything here returns plain dicts. No Discord, no SQLite.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import requests

log = logging.getLogger(__name__)

# A neutral, honest User-Agent. Not pretending to be a browser; these feeds
# are public RSS and do not need that.
DEFAULT_UA = "upstate-feed-bot/1.0 (+https://github.com/, RSS reader)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Best-effort date/time extraction for CivicPlus calendar descriptions.
_DATE_RE = re.compile(
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)")
_LOCATION_RE = re.compile(
    r"(?:Location|Address|Where|Place)\s*[:\-]\s*(.+)", re.IGNORECASE
)


def _strip(text: str) -> str:
    """Local minimal HTML stripper (keeps this module independent of discord_post)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _struct_to_iso(struct_time) -> str | None:
    """Convert a feedparser time struct (UTC) to an ISO 8601 string."""
    if not struct_time:
        return None
    try:
        epoch = calendar.timegm(struct_time)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def _normalize(entry) -> dict:
    """Normalize one feedparser entry into the dict shape the bot expects."""
    link = entry.get("link", "") or ""
    # GUID precedence: explicit id/guid, then link, then title. Something
    # stable per item so de-duplication works.
    guid = entry.get("id") or entry.get("guid") or link or entry.get("title", "")

    summary = entry.get("summary") or entry.get("description") or ""
    if not summary and entry.get("content"):
        try:
            summary = entry["content"][0].get("value", "")
        except (IndexError, AttributeError, TypeError):
            summary = ""

    published_iso = _struct_to_iso(
        entry.get("published_parsed") or entry.get("updated_parsed")
    )

    return {
        "guid": guid,
        "title": (entry.get("title") or "").strip(),
        "link": link,
        "summary": summary,
        "published_iso": published_iso,
    }


def fetch_feed(url: str, user_agent: str = DEFAULT_UA, timeout: int = 20) -> list:
    """Fetch and parse one RSS/Atom feed.

    Returns a list of normalized item dicts. Raises on HTTP errors or on a
    feed that both fails to parse and yields no entries; the caller treats a
    raised exception as a source failure.
    """
    resp = requests.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
        timeout=timeout,
    )
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    # feedparser sets `bozo` for any irregularity, including harmless ones.
    # Only treat it as fatal when it also produced zero entries.
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(
            f"feed did not parse and has no entries: {url} "
            f"({parsed.get('bozo_exception')})"
        )

    return [_normalize(e) for e in parsed.entries]


def matches_keywords(item: dict, keywords: list) -> bool:
    """True if any keyword appears in the item's title or summary.

    Case-insensitive substring match. Used to filter the Substack proxy down
    to meeting-related posts.
    """
    haystack = (
        (item.get("title") or "") + " " + _strip(item.get("summary") or "")
    ).lower()
    return any(kw.lower() in haystack for kw in keywords)


def extract_meeting(item: dict) -> dict:
    """Turn a CivicPlus calendar RSS item into a meeting dict.

    CivicPlus calendar feeds vary in how they format the event date, time,
    and location inside the item description. This does best-effort regex
    extraction. When parsing fails the embed still links to the authoritative
    agenda page, so a missed date is a cosmetic problem, not a data-loss one.
    """
    text = _strip(item.get("summary", ""))

    date_match = _DATE_RE.search(text)
    time_match = _TIME_RE.search(text)
    when_text = None
    if date_match and time_match:
        when_text = f"{date_match.group(1)} at {time_match.group(1)}"
    elif date_match:
        when_text = date_match.group(1)
    elif time_match:
        when_text = time_match.group(1)
    elif text:
        # Last resort: show the opening of the description.
        when_text = text[:140]

    where = None
    loc_match = _LOCATION_RE.search(text)
    if loc_match:
        where = loc_match.group(1).strip()[:300]

    return {
        "guid": item["guid"],
        "title": item.get("title") or "Meeting",
        "link": item.get("link", ""),
        "when_iso": None,         # CivicPlus RSS gives no clean machine date
        "when_text": when_text,
        "where": where,
        "published_iso": item.get("published_iso"),
    }
