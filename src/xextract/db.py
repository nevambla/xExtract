"""SQLite storage: tweets, profiles and a log of every scrape run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    username      TEXT PRIMARY KEY,          -- lowercase handle, no @
    user_id       TEXT,
    display_name  TEXT,
    followers     INTEGER,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tweets (
    id             TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    created_at     TEXT NOT NULL,            -- ISO 8601, UTC
    text           TEXT,
    lang           TEXT,
    url            TEXT,
    reply_count    INTEGER,
    retweet_count  INTEGER,
    like_count     INTEGER,
    quote_count    INTEGER,
    view_count     INTEGER,
    is_reply       INTEGER NOT NULL DEFAULT 0,
    is_retweet     INTEGER NOT NULL DEFAULT 0,
    is_quote       INTEGER NOT NULL DEFAULT 0,
    raw_json       TEXT,
    first_run_id   INTEGER REFERENCES scrape_runs(id),
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tweets_user_date ON tweets(username, created_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    since_date       TEXT NOT NULL,          -- inclusive, YYYY-MM-DD
    until_date       TEXT NOT NULL,          -- inclusive, YYYY-MM-DD
    status           TEXT NOT NULL,          -- running | ok | interrupted | error
    tweets_found     INTEGER NOT NULL DEFAULT 0,
    tweets_new       INTEGER NOT NULL DEFAULT 0,
    oldest_tweet_at  TEXT,
    newest_tweet_at  TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON scrape_runs(username, started_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- profiles -----------------------------------------------------------

    def upsert_profile(self, username: str, user_id: str, display_name: str, followers: int | None) -> None:
        self.conn.execute(
            """INSERT INTO profiles (username, user_id, display_name, followers, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 user_id = excluded.user_id, display_name = excluded.display_name,
                 followers = excluded.followers, updated_at = excluded.updated_at""",
            (username, user_id, display_name, followers, utcnow()),
        )
        self.conn.commit()

    # --- runs ---------------------------------------------------------------

    def start_run(self, username: str, since: str, until: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scrape_runs (username, started_at, since_date, until_date, status) VALUES (?, ?, ?, ?, 'running')",
            (username, utcnow(), since, until),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE scrape_runs SET
                 finished_at = ?, status = ?, error = ?,
                 tweets_found    = (SELECT COUNT(*) FROM tweets WHERE username = scrape_runs.username
                                    AND last_seen_at >= scrape_runs.started_at
                                    AND date(created_at) BETWEEN scrape_runs.since_date AND scrape_runs.until_date),
                 tweets_new      = (SELECT COUNT(*) FROM tweets WHERE first_run_id = scrape_runs.id),
                 oldest_tweet_at = (SELECT MIN(created_at) FROM tweets WHERE username = scrape_runs.username
                                    AND last_seen_at >= scrape_runs.started_at
                                    AND date(created_at) BETWEEN scrape_runs.since_date AND scrape_runs.until_date),
                 newest_tweet_at = (SELECT MAX(created_at) FROM tweets WHERE username = scrape_runs.username
                                    AND last_seen_at >= scrape_runs.started_at
                                    AND date(created_at) BETWEEN scrape_runs.since_date AND scrape_runs.until_date)
               WHERE id = ?""",
            (utcnow(), status, error, run_id),
        )
        self.conn.commit()

    def runs(self, username: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if username:
            q = "SELECT * FROM scrape_runs WHERE username = ? ORDER BY id DESC LIMIT ?"
            return self.conn.execute(q, (username, limit)).fetchall()
        return self.conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def last_successful_run(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scrape_runs WHERE username = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (username,),
        ).fetchone()

    # --- tweets -------------------------------------------------------------

    def save_tweets(self, username: str, run_id: int, tweets: list) -> int:
        """Upsert a batch of twscrape Tweet objects. Returns how many were new."""
        now = utcnow()
        new = 0
        for t in tweets:
            exists = self.conn.execute("SELECT 1 FROM tweets WHERE id = ?", (str(t.id),)).fetchone()
            if not exists:
                new += 1
            self.conn.execute(
                """INSERT INTO tweets (id, username, created_at, text, lang, url, reply_count, retweet_count,
                                       like_count, quote_count, view_count, is_reply, is_retweet, is_quote,
                                       raw_json, first_run_id, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     text = excluded.text, reply_count = excluded.reply_count,
                     retweet_count = excluded.retweet_count, like_count = excluded.like_count,
                     quote_count = excluded.quote_count, view_count = excluded.view_count,
                     raw_json = excluded.raw_json, last_seen_at = excluded.last_seen_at""",
                (
                    str(t.id), username, t.date.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    t.rawContent, t.lang, t.url, t.replyCount, t.retweetCount, t.likeCount,
                    t.quoteCount, t.viewCount, int(t.inReplyToTweetId is not None),
                    int(t.retweetedTweet is not None), int(t.quotedTweet is not None),
                    t.json(), run_id, now,
                ),
            )
        self.conn.commit()
        return new

    def tweet_stats(self, username: str) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest FROM tweets WHERE username = ?",
            (username,),
        ).fetchone()

    def tweets_for(self, username: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tweets WHERE username = ? ORDER BY created_at DESC", (username,)
        ).fetchall()


def row_to_export(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("raw_json", None)
    return d


def raw(row: sqlite3.Row) -> dict:
    return json.loads(row["raw_json"]) if row["raw_json"] else {}
