"""CivicClerk OData fetcher for City of Greenville meetings.

The City of Greenville publishes meetings through a CivicClerk portal whose
backing API (https://greenvillesc.api.civicclerk.com/v1/Events) returns JSON,
not RSS. The API is undocumented.

The portal's JavaScript bundle checks an X-Bypass-Recaptcha-Secret header,
which signals that the API may rate-limit or block clients that do not look
like the portal's own front end. The mitigations here:

  - send a browser-like User-Agent;
  - send Origin and Referer headers matching the portal;
  - poll no more often than the 15-minute schedule already enforces;
  - log every response status, and on any non-2xx response capture the status,
    headers, and first 500 characters of the body so the failure is legible
    in #bot-status rather than silent.

This fetcher is the most fragile source in the bot. The README documents a
manual fallback for when it breaks.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class CivicClerkHTTPError(Exception):
    """Raised when CivicClerk returns a non-2xx response.

    Carries the status, headers, and a body snippet so main.py can post a
    legible notice to #bot-status instead of a bare stack trace.
    """

    def __init__(self, status: int, headers: dict, body_snippet: str):
        self.status = status
        self.headers = headers
        self.body_snippet = body_snippet
        super().__init__(f"CivicClerk returned HTTP {status}")

    def summary(self) -> str:
        return (
            f"status={self.status}; "
            f"headers={self.headers}; "
            f"body[:500]={self.body_snippet}"
        )


def _format_location(loc) -> str | None:
    """Flatten a CivicClerk eventLocation into a readable one-line string.

    The API returns eventLocation as a nested object, for example:
        {"address1": "206 S. Main Street",
         "address2": "Greenville City Hall - Council Chambers",
         "city": "Greenville", "state": "SC", "zipCode": "29601"}
    It may also be absent, empty, or (defensively) a plain string.
    """
    if not loc:
        return None
    if isinstance(loc, str):
        return loc.strip() or None
    if not isinstance(loc, dict):
        return None

    pieces = [
        str(loc.get(key)).strip()
        for key in ("address1", "address2")
        if loc.get(key) and str(loc.get(key)).strip()
    ]
    city_state = ", ".join(
        str(loc.get(key)).strip()
        for key in ("city", "state")
        if loc.get(key) and str(loc.get(key)).strip()
    )
    zip_code = str(loc.get("zipCode") or "").strip()
    if city_state and zip_code:
        city_state = f"{city_state} {zip_code}"
    elif zip_code:
        city_state = zip_code
    if city_state:
        pieces.append(city_state)
    return ", ".join(pieces) or None


def fetch_civicclerk(cfg: dict) -> list:
    """Fetch upcoming City of Greenville meetings from the CivicClerk OData API.

    Args:
        cfg: the `civicclerk` block from config.yaml (url, portal, user_agent).

    Returns a list of meeting dicts shaped like the CivicPlus meeting dicts so
    discord_post.build_meeting_embed can handle both.

    Raises:
        CivicClerkHTTPError: on any non-2xx response.
        requests.RequestException: on connection/timeout errors.
    """
    portal = cfg["portal"].rstrip("/")
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": "application/json",
        # Origin and Referer make the request look like it came from the
        # portal's own front end, which the API appears to expect.
        "Origin": portal,
        "Referer": portal + "/",
    }

    resp = requests.get(cfg["url"], headers=headers, timeout=25)

    # Required by spec: log every response status for this fragile source.
    log.info("CivicClerk response status: %s", resp.status_code)

    if not (200 <= resp.status_code < 300):
        body_snippet = resp.text[:500]
        # Full detail to the logfile / Actions log.
        log.error(
            "CivicClerk non-2xx response. status=%s headers=%s body[:500]=%s",
            resp.status_code,
            dict(resp.headers),
            body_snippet,
        )
        raise CivicClerkHTTPError(
            resp.status_code, dict(resp.headers), body_snippet
        )

    data = resp.json()
    events = data.get("value", [])

    results = []
    for event in events:
        event_id = event.get("id")
        if event_id is None:
            continue

        # The OData schema is undocumented and field names may shift. Try the
        # likely keys for each value and fall back gracefully.
        name = (
            event.get("eventName")
            or event.get("name")
            or event.get("title")
            or "City of Greenville meeting"
        )
        start = (
            event.get("startDateTime")
            or event.get("eventDate")
            or event.get("start")
        )
        location = _format_location(event.get("eventLocation"))

        # Portal event page. CivicClerk portals route /event/<id>/overview.
        link = f"{portal}/event/{event_id}/overview"

        results.append(
            {
                "guid": f"civicclerk:{event_id}",
                "title": name,
                "link": link,
                "when_iso": start,
                "when_text": None,
                "where": location,
                "published_iso": start,
            }
        )

    log.info("CivicClerk: %d event(s) returned", len(results))
    return results
