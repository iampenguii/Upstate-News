"""Discord webhook posting and embed builders.

Two responsibilities:
  1. Post a payload to a webhook URL, handling Discord's rate limit (HTTP 429)
     and transient 5xx errors with bounded retries.
  2. Build a Discord embed dict for each source type (news, weather, meeting,
     Substack-proxy item).

A webhook URL is effectively a password. This module never logs the URL.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Embed colors.
COLOR_NEWS = 0x95A5A6      # neutral gray
COLOR_MEETING = 0x1F4E79   # neutral blue
COLOR_SEVERE = 0xCC0000    # red:    Extreme / Severe
COLOR_MODERATE = 0xE67E22  # orange: Moderate
COLOR_MINOR = 0xF1C40F     # yellow: Minor / Unknown

# Discord field length ceilings (the API rejects anything longer).
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_CONTENT = 2000

# How long to pause after each post, to stay well under Discord's webhook
# rate limit. Steady-state runs post only a handful of items, so this is cheap.
POST_SPACING_SECONDS = 0.7

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = html.unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def truncate(text: str, limit: int) -> str:
    """Shorten text to `limit` characters, ending with an ellipsis if cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fmt_et(iso_str: str) -> str:
    """Format an ISO 8601 timestamp as a readable Eastern-time string.

    A timestamp with no offset is assumed to be UTC. If the string cannot be
    parsed it is returned unchanged so the caller still shows something.
    """
    if not iso_str:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    eastern = parsed.astimezone(ET)
    hour = eastern.strftime("%I").lstrip("0") or "12"
    return f"{eastern.strftime('%a %b')} {eastern.day}, {hour}:{eastern.strftime('%M %p %Z')}"


def _is_http_url(value: str) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _is_iso_timestamp(value: str) -> bool:
    """Cheap check that a string looks like an ISO 8601 datetime."""
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _post(webhook_url: str, payload: dict, max_attempts: int = 4) -> None:
    """POST a payload to a Discord webhook with retry on 429 and 5xx.

    Raises RuntimeError if the post cannot be delivered. The caller treats
    that as a source error; the item is not marked seen and will be retried
    on the next run.
    """
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(webhook_url, json=payload, timeout=20)

        if 200 <= resp.status_code < 300:
            time.sleep(POST_SPACING_SECONDS)
            return

        if resp.status_code == 429:
            # Rate limited. Discord returns retry_after (seconds) in the body.
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                pass
            retry_after = min(retry_after, 60.0)
            log.warning("Discord rate limited; sleeping %.1fs", retry_after)
            time.sleep(retry_after + 0.25)
            continue

        if resp.status_code >= 500:
            log.warning(
                "Discord webhook 5xx (%s), attempt %d/%d",
                resp.status_code,
                attempt,
                max_attempts,
            )
            time.sleep(2 * attempt)
            continue

        # 4xx other than 429: a malformed payload or a dead webhook. No retry.
        raise RuntimeError(
            f"Discord webhook rejected payload: HTTP {resp.status_code} "
            f"{resp.text[:200]}"
        )

    raise RuntimeError("Discord webhook failed after retries")


def post_embed(webhook_url: str, embed: dict) -> None:
    """Post a single embed to a channel webhook."""
    _post(webhook_url, {"embeds": [embed]})


def post_text(webhook_url: str, content: str) -> None:
    """Post a plain-text message. Used for #bot-status notices and heartbeat."""
    _post(webhook_url, {"content": truncate(content, MAX_CONTENT)})


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_news_embed(item: dict, source_name: str) -> dict:
    """News RSS item -> gray embed for #news-feed."""
    embed = {
        "title": truncate(item.get("title") or "(untitled)", MAX_TITLE),
        "color": COLOR_NEWS,
        "footer": {"text": truncate(source_name, MAX_FOOTER)},
    }
    summary = strip_html(item.get("summary", ""))
    if summary:
        # Spec: first 300 chars of summary, HTML stripped.
        embed["description"] = truncate(summary, 300)
    if _is_http_url(item.get("link", "")):
        embed["url"] = item["link"]
    published = item.get("published_iso")
    if _is_iso_timestamp(published or ""):
        # Discord renders this next to the footer text.
        embed["timestamp"] = published
    return embed


def build_weather_embed(alert: dict) -> dict:
    """NWS alert -> severity-colored embed for #weather-alerts."""
    severity = alert.get("severity") or "Unknown"
    if severity in ("Extreme", "Severe"):
        color = COLOR_SEVERE
    elif severity == "Moderate":
        color = COLOR_MODERATE
    else:  # Minor, Unknown, or anything unexpected
        color = COLOR_MINOR

    counties = ", ".join(alert.get("counties") or []) or "Upstate SC"
    summary = strip_html(alert.get("summary", ""))
    description = truncate(summary, 600)
    if description:
        description += "\n\n"
    description += f"**Affected counties:** {counties}"

    title = alert.get("headline") or alert.get("event") or "NWS Alert"
    embed = {
        "title": truncate(title, MAX_TITLE),
        "description": truncate(description, MAX_DESCRIPTION),
        "color": color,
        "fields": [
            {"name": "Severity", "value": severity, "inline": True},
            {
                "name": "Effective",
                "value": fmt_et(alert.get("effective")),
                "inline": True,
            },
            {
                "name": "Expires",
                "value": fmt_et(alert.get("expires")),
                "inline": True,
            },
        ],
    }
    if _is_http_url(alert.get("url", "")):
        embed["url"] = alert["url"]
    return embed


def _meeting_when(meeting: dict) -> str:
    """Resolve the human-readable 'When' value for a meeting embed.

    CivicClerk gives a real ISO timestamp; CivicPlus gives a best-effort
    string parsed out of the RSS description.
    """
    if meeting.get("when_iso"):
        return fmt_et(meeting["when_iso"])
    if meeting.get("when_text"):
        return meeting["when_text"]
    return "See linked agenda"


def build_meeting_embed(meeting: dict, jurisdiction: str) -> dict:
    """Government meeting -> neutral-blue embed for #government-watch."""
    embed = {
        "title": truncate(meeting.get("title") or "Meeting", MAX_TITLE),
        "color": COLOR_MEETING,
        "fields": [
            {
                "name": "When",
                "value": truncate(_meeting_when(meeting), MAX_FIELD_VALUE),
                "inline": False,
            }
        ],
        "footer": {"text": truncate(jurisdiction, MAX_FOOTER)},
    }
    where = meeting.get("where")
    if where:
        embed["fields"].append(
            {
                "name": "Where",
                "value": truncate(where, MAX_FIELD_VALUE),
                "inline": False,
            }
        )
    if _is_http_url(meeting.get("link", "")):
        embed["url"] = meeting["link"]
    published = meeting.get("published_iso")
    if _is_iso_timestamp(published or ""):
        embed["timestamp"] = published
    return embed


def build_substack_embed(item: dict, jurisdiction: str) -> dict:
    """Substack-proxy item -> neutral-blue embed for #government-watch.

    Looks like a news embed but is colored as a meeting item and footers the
    jurisdiction, so readers see in-channel that this is a proxy source.
    """
    embed = {
        "title": truncate(item.get("title") or "(untitled)", MAX_TITLE),
        "color": COLOR_MEETING,
        "footer": {"text": truncate(jurisdiction, MAX_FOOTER)},
    }
    summary = strip_html(item.get("summary", ""))
    if summary:
        embed["description"] = truncate(summary, 300)
    if _is_http_url(item.get("link", "")):
        embed["url"] = item["link"]
    published = item.get("published_iso")
    if _is_iso_timestamp(published or ""):
        embed["timestamp"] = published
    return embed
