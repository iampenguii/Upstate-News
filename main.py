#!/usr/bin/env python3
"""Upstate SC feed bot: poll entry point.

Runs once per invocation. GitHub Actions calls it every 15 minutes. On each
run it fetches every configured source, posts new items to the matching
Discord channel via webhook, and records state in SQLite so nothing is posted
twice.

Design rule: never crash the whole run. Every fetcher is wrapped in
try/except. A failure is logged, counted, and surfaced to #bot-status if it
has been sustained; the run then proceeds to the next source.

Usage:
    python main.py            normal run
    python main.py --seed     record current feed items as seen WITHOUT
                              posting them. Run this once on first deploy so
                              the bot does not dump every outlet's full
                              backlog into Discord at once. See the README.

The same seed behavior is available through the env var SEED_MODE (truthy),
which is how the GitHub Actions workflow_dispatch input wires it in.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv

import discord_post
from fetchers.civicclerk import CivicClerkHTTPError, fetch_civicclerk
from fetchers.nws import fetch_nws_alerts
from fetchers.rss import extract_meeting, fetch_feed, matches_keywords
from state import State

log = logging.getLogger("main")

CONFIG_PATH = "config.yaml"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Log to stdout (captured by Actions) and to a local logfile."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(os.environ.get("LOG_PATH", "feed-bot.log")))
    except OSError:
        # A read-only filesystem should not stop the run; stdout still works.
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_webhooks(config: dict) -> dict:
    """Map each channel to its webhook URL from the environment.

    A missing webhook is a configuration error, not a feed error, so the bot
    exits loudly rather than running half-configured. GitHub emails the repo
    owner on a failed workflow, and the weekly heartbeat will also show the
    gap.
    """
    webhooks = {}
    missing = []
    for channel, env_var in config["channels"].items():
        value = os.environ.get(env_var)
        if not value:
            missing.append(env_var)
        webhooks[channel] = value
    if missing:
        log.error("Missing required webhook env var(s): %s", ", ".join(missing))
        log.error(
            "Set them in .env for local runs, or as repository Secrets for "
            "GitHub Actions. See the README."
        )
        sys.exit(1)
    return webhooks


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def handle_source_failure(
    state: State,
    source: str,
    error: Exception,
    status_webhook: str,
    threshold_hours: int,
    immediate_detail: str | None = None,
) -> None:
    """Record a source failure and, when warranted, notify #bot-status.

    Two notice paths:
      1. immediate_detail: if provided and this is the first failure of the
         streak, post the detail right away. Used for CivicClerk, whose
         response details are worth surfacing the moment it starts blocking.
      2. sustained outage: once a source has been failing for longer than
         threshold_hours, post a notice. Re-posts every threshold_hours while
         the outage continues, so a multi-day outage does not go quiet after
         one message.
    """
    record = state.record_failure(source, str(error))
    now = datetime.now(timezone.utc)

    if immediate_detail and record["consecutive_failures"] == 1:
        try:
            discord_post.post_text(
                status_webhook,
                f":warning: `{source}` fetch failed.\n{immediate_detail}",
            )
        except Exception as post_err:  # noqa: BLE001
            log.error("Could not post immediate failure notice: %s", post_err)

    first_failure = datetime.fromisoformat(record["first_failure_at"])
    hours_down = (now - first_failure).total_seconds() / 3600.0
    last_alerted = record["last_alerted_at"]

    over_threshold = hours_down >= threshold_hours
    due_to_realert = last_alerted is None or (
        (now - datetime.fromisoformat(last_alerted)).total_seconds() / 3600.0
        >= threshold_hours
    )

    if over_threshold and due_to_realert:
        try:
            discord_post.post_text(
                status_webhook,
                f":rotating_light: Feed `{source}` has been failing for "
                f"{hours_down:.0f}h "
                f"({record['consecutive_failures']} consecutive runs).\n"
                f"Last error: {str(error)[:500]}",
            )
            state.set_alerted(source, now.isoformat())
        except Exception as post_err:  # noqa: BLE001
            log.error("Could not post sustained-failure notice: %s", post_err)


def _oldest_first(items: list) -> list:
    """Sort items oldest-first so a channel reads chronologically.

    Items with no published date sort first (empty string).
    """
    return sorted(items, key=lambda i: i.get("published_iso") or "")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class RunStats:
    """Mutable counters accumulated across one run."""

    def __init__(self) -> None:
        self.polled = 0
        self.ok = 0
        self.failed = 0
        self.news = 0
        self.weather = 0
        self.meeting = 0


def run(seed: bool = False) -> None:
    config = load_config()
    webhooks = resolve_webhooks(config)
    settings = config["settings"]

    db_path = os.environ.get("STATE_DB_PATH", settings["database_path"])
    state = State(db_path)

    threshold = settings["failure_alert_hours"]
    cap = settings["max_posts_per_source"]
    status_webhook = webhooks["status"]
    stats = RunStats()

    if seed:
        log.info(
            "SEED MODE: feed items will be recorded as seen but NOT posted "
            "to Discord."
        )

    # --- 1. Local news RSS -> #news-feed ------------------------------------
    for feed in config["news_feeds"]:
        source = f"news:{feed['name']}"
        stats.polled += 1
        try:
            items = fetch_feed(feed["url"])
            fresh = [
                i for i in items if not state.is_seen("news_items", i["guid"])
            ]
            fresh = _oldest_first(fresh)[:cap]
            for item in fresh:
                if not seed:
                    discord_post.post_embed(
                        webhooks["news"],
                        discord_post.build_news_embed(item, feed["name"]),
                    )
                    stats.news += 1
                state.mark_seen(
                    "news_items", item["guid"], feed["name"], item["title"]
                )
            state.record_success(source)
            stats.ok += 1
            log.info("%s: %d new item(s)", source, len(fresh))
        except Exception as err:  # noqa: BLE001
            stats.failed += 1
            log.exception("%s failed: %s", source, err)
            handle_source_failure(state, source, err, status_webhook, threshold)

    # --- 2. NWS active alerts -> #weather-alerts ----------------------------
    source = "nws"
    stats.polled += 1
    try:
        alerts = fetch_nws_alerts(
            config["nws"]["endpoint"],
            config["nws"]["user_agent"],
            config["nws"]["counties"],
        )
        fresh = [
            a
            for a in alerts
            if a["guid"] and not state.is_seen("weather_alerts", a["guid"])
        ][:cap]
        for alert in fresh:
            if not seed:
                discord_post.post_embed(
                    webhooks["weather"], discord_post.build_weather_embed(alert)
                )
                stats.weather += 1
            state.mark_seen(
                "weather_alerts",
                alert["guid"],
                "NWS",
                alert.get("headline") or alert.get("event"),
            )
        state.record_success(source)
        stats.ok += 1
        log.info("nws: %d new alert(s)", len(fresh))
    except Exception as err:  # noqa: BLE001
        stats.failed += 1
        log.exception("nws failed: %s", err)
        handle_source_failure(state, source, err, status_webhook, threshold)

    # --- 3a / 3b. CivicPlus RSS calendars -> #government-watch --------------
    for feed in config["civicplus_feeds"]:
        source = f"civicplus:{feed['name']}"
        stats.polled += 1
        try:
            raw_items: list = []
            for cid in feed["cids"]:
                raw_items.extend(
                    fetch_feed(feed["feed_template"].format(cid=cid))
                )
            # De-duplicate across CIDs by GUID before checking state.
            seen_guids: set = set()
            unique_items = []
            for item in raw_items:
                if item["guid"] in seen_guids:
                    continue
                seen_guids.add(item["guid"])
                unique_items.append(item)

            fresh = [
                extract_meeting(i)
                for i in unique_items
                if not state.is_seen("meetings", i["guid"])
            ][:cap]
            for meeting in fresh:
                if not seed:
                    discord_post.post_embed(
                        webhooks["government"],
                        discord_post.build_meeting_embed(
                            meeting, feed["jurisdiction"]
                        ),
                    )
                    stats.meeting += 1
                state.mark_seen(
                    "meetings", meeting["guid"], feed["name"], meeting["title"]
                )
            state.record_success(source)
            stats.ok += 1
            log.info("%s: %d new meeting(s)", source, len(fresh))
        except Exception as err:  # noqa: BLE001
            stats.failed += 1
            log.exception("%s failed: %s", source, err)
            handle_source_failure(state, source, err, status_webhook, threshold)

    # --- 3c. CivicClerk OData -> #government-watch -------------------------
    cc = config["civicclerk"]
    source = f"civicclerk:{cc['name']}"
    stats.polled += 1
    try:
        meetings = fetch_civicclerk(cc)
        fresh = [
            m for m in meetings if not state.is_seen("meetings", m["guid"])
        ][:cap]
        for meeting in fresh:
            if not seed:
                discord_post.post_embed(
                    webhooks["government"],
                    discord_post.build_meeting_embed(meeting, cc["jurisdiction"]),
                )
                stats.meeting += 1
            state.mark_seen(
                "meetings", meeting["guid"], cc["name"], meeting["title"]
            )
        state.record_success(source)
        stats.ok += 1
        log.info("%s: %d new meeting(s)", source, len(fresh))
    except CivicClerkHTTPError as err:
        stats.failed += 1
        log.error("CivicClerk HTTP error: %s", err.summary())
        detail = (
            f"CivicClerk returned HTTP {err.status}. This endpoint may be "
            f"rate-limiting or blocking non-browser clients.\n"
            f"Body (first 500 chars): ```{err.body_snippet}```"
        )
        handle_source_failure(
            state, source, err, status_webhook, threshold, immediate_detail=detail
        )
    except Exception as err:  # noqa: BLE001
        stats.failed += 1
        log.exception("CivicClerk failed: %s", err)
        handle_source_failure(state, source, err, status_webhook, threshold)

    # --- 3d. Substack proxy -> #government-watch ---------------------------
    sp = config["substack_proxy"]
    source = f"substack:{sp['name']}"
    stats.polled += 1
    try:
        items = fetch_feed(sp["url"])
        # Only meeting-related posts pass; the Council Chair's newsletter
        # covers more than meetings.
        relevant = [i for i in items if matches_keywords(i, sp["keywords"])]
        fresh = [
            i for i in relevant if not state.is_seen("substack_items", i["guid"])
        ]
        fresh = _oldest_first(fresh)[:cap]
        for item in fresh:
            if not seed:
                discord_post.post_embed(
                    webhooks["government"],
                    discord_post.build_substack_embed(item, sp["jurisdiction"]),
                )
                stats.meeting += 1
            state.mark_seen(
                "substack_items", item["guid"], sp["name"], item["title"]
            )
        state.record_success(source)
        stats.ok += 1
        log.info("%s: %d new item(s) after keyword filter", source, len(fresh))
    except Exception as err:  # noqa: BLE001
        stats.failed += 1
        log.exception("%s failed: %s", source, err)
        handle_source_failure(state, source, err, status_webhook, threshold)

    # --- record the run and prune old state --------------------------------
    state.record_run(
        stats.polled,
        stats.ok,
        stats.failed,
        stats.news,
        stats.weather,
        stats.meeting,
    )
    pruned = state.prune(settings["prune_days"])
    log.info(
        "Run complete. polled=%d ok=%d failed=%d | posted news=%d weather=%d "
        "meeting=%d | pruned %d old row(s)",
        stats.polled,
        stats.ok,
        stats.failed,
        stats.news,
        stats.weather,
        stats.meeting,
        pruned,
    )
    state.close()


def main() -> None:
    setup_logging()
    load_dotenv()  # no-op when there is no .env (the GitHub Actions case)
    seed = "--seed" in sys.argv or os.environ.get("SEED_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        run(seed=seed)
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001
        # A failure here is outside the per-source guards (config load, DB
        # open). Log it and exit non-zero so the Actions run is marked failed.
        log.exception("Fatal error: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
