#!/usr/bin/env python3
"""Weekly heartbeat for the Upstate SC feed bot.

A push-only bot is invisible when nothing happens. A quiet week and a dead bot
look identical in Discord. The heartbeat fixes that: every Monday morning it
posts a summary of the last seven days to #bot-status. If the heartbeat stops
appearing, the bot is down. If it appears but the numbers are wrong, something
is also wrong. See the README.

Run by .github/workflows/heartbeat.yml, separately from the 15-minute poller.

Daylight-saving handling: GitHub Actions cron is UTC only. 09:00 America/
New_York is 13:00 UTC under EDT and 14:00 UTC under EST. The workflow fires at
both times; this script checks the actual Eastern hour and posts only at 09:00
ET, so exactly one of the two invocations posts. The HEARTBEAT_FORCE env var
(wired to the workflow_dispatch "force" input) bypasses that gate for manual
testing.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

import discord_post
from state import State

log = logging.getLogger("heartbeat")

ET = ZoneInfo("America/New_York")
CONFIG_PATH = "config.yaml"


def _is_truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _humanize_age(iso_str: str) -> str:
    """Render the age of an ISO timestamp as a short string, e.g. '31h' or '4d'."""
    try:
        start = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return "unknown"
    delta = datetime.now(timezone.utc) - start
    hours = delta.total_seconds() / 3600.0
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def build_summary(state: State, lookback_days: int) -> str:
    """Build the heartbeat message from the runs table."""
    since = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).isoformat()
    runs = state.runs_since(since)

    if not runs:
        # No runs recorded at all. This is the exact silent-failure case the
        # heartbeat exists to catch.
        return (
            f":rotating_light: **Heartbeat ({lookback_days}-day)**\n"
            f"No poll runs recorded in the last {lookback_days} days. The "
            f"poller workflow may be disabled, failing at startup, or losing "
            f"its state cache. Check the repository's Actions tab."
        )

    total_runs = len(runs)
    successful = sum(1 for r in runs if r["sources_failed"] == 0)
    transient_errors = sum(r["sources_failed"] for r in runs)
    news = sum(r["news_posted"] for r in runs)
    weather = sum(r["weather_posted"] for r in runs)
    meetings = sum(r["meeting_posted"] for r in runs)
    # Source count from the most recent run (config can change over time).
    n_sources = runs[-1]["sources_polled"]

    lines = [
        f":white_check_mark: **Heartbeat (last {lookback_days} days)**",
        (
            f"Polled {n_sources} sources every 15 min. "
            f"{successful} of {total_runs} runs completed with no source "
            f"errors; {transient_errors} transient source errors total."
        ),
        f"Posted {news} news / {weather} weather / {meetings} meeting items.",
    ]

    # Surface any source currently in a failure streak. A heartbeat that says
    # "all good" while a feed is dark would defeat the purpose.
    failing = state.get_failures()
    if failing:
        parts = [
            f"`{row['source']}` (down {_humanize_age(row['first_failure_at'])})"
            for row in failing
        ]
        lines.append(":warning: Currently failing: " + ", ".join(parts))

    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    load_dotenv()

    force = _is_truthy(os.environ.get("HEARTBEAT_FORCE", "")) or "--force" in sys.argv
    now_et = datetime.now(ET)
    if not force and now_et.hour != 9:
        log.info(
            "Eastern hour is %02d:00, not 09:00. The heartbeat workflow fires "
            "two crons to cover daylight saving; this invocation is the wrong "
            "one and exits without posting.",
            now_et.hour,
        )
        return

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    status_var = config["channels"]["status"]
    webhook = os.environ.get(status_var)
    if not webhook:
        log.error("No status webhook (%s) set; cannot post heartbeat.", status_var)
        sys.exit(1)

    db_path = os.environ.get("STATE_DB_PATH", config["settings"]["database_path"])
    lookback = config["settings"].get("heartbeat_lookback_days", 7)

    state = State(db_path)
    try:
        message = build_summary(state, lookback)
        discord_post.post_text(webhook, message)
        log.info("Heartbeat posted to #bot-status.")
    finally:
        state.close()


if __name__ == "__main__":
    main()
