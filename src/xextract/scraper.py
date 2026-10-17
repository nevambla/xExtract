"""Fetches tweets from X through twscrape (X's internal GraphQL API, using your own X account cookies)."""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from loguru import logger
from twscrape import API, set_log_level

set_log_level("WARNING")

# Max time to wait for X's rate limit to reset (~15 min per window) before giving up.
RATE_LIMIT_WAIT = 60 * 60


def _wait_message(record) -> str:
    """Rewrites twscrape's rate-limit log lines into something readable."""
    msg = record["message"]
    if m := re.search(r"Next available at (\S+)", msg):
        return f"  Rate limited by X, waiting until {m.group(1)}...\n"
    if msg.startswith("Continuing"):
        return "  Rate limit lifted, continuing.\n"
    return ""


# twscrape logs rate-limit waits at INFO, which we otherwise hide; surface just those.
logger.add(
    sys.stderr,
    level="INFO",
    format=_wait_message,
    filter=lambda r: r["function"] == "get_for_queue_or_wait" and r["level"].name == "INFO",
)


@dataclass
class Profile:
    user_id: str
    username: str
    display_name: str
    followers: int | None
    created: date


@dataclass
class TimelineState:
    oldest: date | None = None      # oldest own post the timeline returned
    reached_since: bool = False     # True if the timeline went back past the requested start date
    dates: list[date] = field(default_factory=list)  # dates of every own post the timeline returned

    def frontier(self, max_gap_days: int = 30) -> date | None:
        """Oldest date the timeline covers continuously.

        The timeline can include a few much older posts (a pinned post, or the user's own old posts shown
        as thread context). Taking the plain minimum would make search skip the years in between, so walk
        back from the newest post and stop at the first gap longer than `max_gap_days`.
        """
        if not self.dates:
            return None
        ds = sorted(set(self.dates), reverse=True)
        edge = ds[0]
        for d in ds[1:]:
            if (edge - d).days > max_gap_days:
                break
            edge = d
        return edge


def date_windows(since: date, until: date, days: int) -> list[tuple[date, date]]:
    """Split [since, until] (both inclusive) into windows, newest first.

    X search silently drops results on long ranges, so querying in smaller windows is far more complete.
    """
    windows = []
    end = until
    while end >= since:
        start = max(since, end - timedelta(days=days - 1))
        windows.append((start, end))
        end = start - timedelta(days=1)
    return windows


class TwscrapeScraper:
    def __init__(self, accounts_db: Path):
        accounts_db.parent.mkdir(parents=True, exist_ok=True)
        # Raises at once if there's no active account; waits if they're only rate-limited.
        self.api = API(str(accounts_db), raise_when_no_account=True, wait_timeout=RATE_LIMIT_WAIT)

    async def add_cookie_account(self, username: str, cookies: str) -> None:
        await self.api.pool.add_account_cookies(username, cookies)

    async def remove_account(self, username: str) -> None:
        await self.api.pool.delete_accounts(username)

    async def accounts(self) -> list[dict]:
        return await self.api.pool.accounts_info()

    async def profile(self, username: str) -> Profile | None:
        u = await self.api.user_by_login(username)
        if u is None:
            return None
        return Profile(str(u.id), u.username, u.displayname, u.followersCount, u.created.date())

    async def timeline(
        self, user_id: str, since: date, until: date, state: TimelineState, *, include_retweets: bool = False
    ) -> AsyncIterator:
        """Yields the user's own posts and replies in [since, until] from their profile timeline.

        Far more complete than search (which drops many replies), but X only serves the latest
        ~3200 posts here. `state` records how far back it got, so search can cover the rest.
        """
        older_in_a_row = 0
        async for t in self.api.user_tweets_and_replies(int(user_id)):
            if str(t.user.id) != user_id:  # conversation context: other people's tweets in threads
                continue
            if not include_retweets and t.retweetedTweet is not None:
                continue
            day = t.date.date()
            state.oldest = min(state.oldest or day, day)
            state.dates.append(day)
            if day < since:
                # A pinned post can be old, so only stop after several older posts in a row.
                older_in_a_row += 1
                if older_in_a_row >= 5:
                    state.reached_since = True
                    return
                continue
            older_in_a_row = 0
            if day <= until:
                yield t

    async def search(
        self, username: str, since: date, until: date, *, include_retweets: bool = False
    ) -> AsyncIterator:
        """Yields tweets posted by `username` between `since` and `until` (inclusive) via X search."""
        # X search's `until:` is exclusive, so add a day to make our range inclusive.
        q = f"from:{username} since:{since.isoformat()} until:{(until + timedelta(days=1)).isoformat()}"
        if include_retweets:
            q += " include:nativeretweets"
        async for t in self.api.search(q, kv={"product": "Latest"}):
            yield t
