"""National Weather Service active-alerts fetcher.

Pulls https://api.weather.gov/alerts/active?area=SC and filters the result
down to the seven Upstate counties the chapter covers, matched by SAME code
against the geocode.SAME field on each alert.

NWS requires a User-Agent with a contact address; a request without one is
rejected. The address is set in config.yaml.

Alert de-duplication note: NWS issues a fresh alert id when an alert is
genuinely updated (severity change, area expansion). The bot keys on that id,
so an update posts as a new embed rather than editing the old one. That is
intended; see the README.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def fetch_nws_alerts(endpoint: str, user_agent: str, counties: dict) -> list:
    """Fetch active NWS alerts and keep only those touching our counties.

    Args:
        endpoint:   the alerts/active URL (area=SC).
        user_agent: User-Agent header value, including a contact address.
        counties:   mapping of SAME code -> county name, e.g.
                    {"045045": "Greenville", ...}.

    Returns a list of alert dicts. Raises on HTTP or JSON errors; the caller
    treats a raised exception as a source failure.
    """
    resp = requests.get(
        endpoint,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        },
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geocode = props.get("geocode") or {}
        same_codes = geocode.get("SAME") or []

        # Keep the alert only if at least one of its SAME codes is one of ours.
        matched = [counties[code] for code in same_codes if code in counties]
        if not matched:
            continue

        # properties.id is the canonical alert identifier (a URN). The
        # feature-level id is a fetchable URL for the same alert.
        alert_id = props.get("id") or feature.get("id")
        if not alert_id:
            log.warning("NWS alert with no id; skipping")
            continue

        results.append(
            {
                "guid": alert_id,
                "headline": props.get("headline"),
                "event": props.get("event"),
                "severity": props.get("severity"),
                # onset/ends are fallbacks when effective/expires are absent.
                "effective": props.get("effective") or props.get("onset"),
                "expires": props.get("expires") or props.get("ends"),
                "counties": sorted(set(matched)),
                "summary": props.get("description") or "",
                "url": feature.get("id") or props.get("id"),
            }
        )

    log.info("NWS: %d active alert(s) touching our counties", len(results))
    return results
