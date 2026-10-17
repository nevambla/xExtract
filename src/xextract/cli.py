"""Interactive CLI: `xextract` asks for a user and a date range, scrapes, and logs the run."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .db import Store, row_to_export
from .scraper import TimelineState, TwscrapeScraper, date_windows

DATA_DIR = Path(os.environ.get("XEXTRACT_DATA", Path.cwd() / "data"))
BATCH = 50


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def ask_secret(prompt: str) -> str:
    while not (value := getpass.getpass(f"{prompt}: ").strip()):
        print(f"  {prompt} can't be empty.")
    return value


def ask_date(prompt: str, default: date | None = None) -> date:
    while True:
        value = ask(prompt + " (YYYY-MM-DD)", default.isoformat() if default else None)
        try:
            return date.fromisoformat(value)
        except ValueError:
            print("  Invalid date, use the YYYY-MM-DD format.")


def ask_yes(prompt: str, default: bool = False) -> bool:
    value = ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not value else value in ("y", "yes")


def clean_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def print_history(store: Store, username: str, limit: int = 5) -> None:
    runs = store.runs(username, limit)
    if not runs:
        print(f"  @{username} has never been scraped.")
        return
    stats = store.tweet_stats(username)
    print(f"  Saved tweets: {stats['n']}  (from {(stats['oldest'] or '-')[:10]} to {(stats['newest'] or '-')[:10]})")
    print("  Recent runs:")
    for r in runs:
        print(
            f"   #{r['id']:<4} {r['started_at'][:16].replace('T', ' ')}  "
            f"range {r['since_date']} → {r['until_date']}  "
            f"{r['status']:<11} found={r['tweets_found']} new={r['tweets_new']}"
            + (f"  error: {r['error']}" if r["error"] else "")
        )


# --- commands ---------------------------------------------------------------


async def cmd_scrape(args: argparse.Namespace, store: Store, scraper: TwscrapeScraper) -> int:
    username = clean_handle(args.username or ask("X username (without @)"))
    if not username:
        print("A username is required.")
        return 1

    print(f"\nHistory for @{username}:")
    print_history(store, username)

    # Default "since": continue from the last successful run, otherwise ask from scratch.
    last = store.last_successful_run(username)
    default_since = date.fromisoformat(last["until_date"]) if last else None
    if last:
        print(f"\n  Last completed run covered up to {last['until_date']}; "
              "by default it continues from there (that day is re-scraped in case it was incomplete).")

    print()
    since = date.fromisoformat(args.since) if args.since else ask_date("Start date", default_since)
    until = date.fromisoformat(args.until) if args.until else ask_date("End date", date.today())
    if since > until:
        print("The start date is after the end date.")
        return 1
    include_rts = args.retweets if args.retweets is not None else ask_yes("Include retweets?")

    profile = await scraper.profile(username)
    if profile is None:
        print(f"User @{username} not found (or the account is private/suspended).")
        return 1
    store.upsert_profile(username, profile.user_id, profile.display_name, profile.followers)
    if since < profile.created:
        print(f"@{profile.username} was created on {profile.created}; starting from there.")
        since = profile.created
        if since > until:
            print("The account didn't exist yet in that range.")
            return 1

    run_id = store.start_run(username, since.isoformat(), until.isoformat())
    print(f"\nScraping @{profile.username} ({profile.display_name}) from {since} to {until} — run #{run_id}")

    seen: set[int] = set()
    new = 0

    async def consume(tweets) -> None:
        nonlocal new
        batch = []
        async for tweet in tweets:
            if tweet.id in seen:  # X repeats tweets across pages and sources
                continue
            seen.add(tweet.id)
            batch.append(tweet)
            if len(batch) >= BATCH:
                new += store.save_tweets(username, run_id, batch)
                batch.clear()
        if batch:
            new += store.save_tweets(username, run_id, batch)

    status, error = "ok", None
    try:
        # 1) Profile timeline: the most complete source, but only reaches the latest ~3200 posts.
        tl = TimelineState()
        if args.search_only:
            frontier = until  # skip the timeline and search the whole range (used to fill gaps in older history)
        else:
            print("  Reading profile timeline...")
            await consume(scraper.timeline(profile.user_id, since, until, tl, include_retweets=include_rts))
            frontier = tl.frontier()
            print(f"  Profile timeline: {len(seen)} tweets ({new} new)"
                  + (f", continuous back to {frontier}" if frontier else ""))

        # 2) Search, only for the older part of the range the timeline didn't cover continuously.
        if not tl.reached_since or (frontier and frontier > since):
            search_until = min(until, frontier) if frontier else until
            print(f"  Searching older tweets {since} → {search_until}...")
            for w_since, w_until in date_windows(since, search_until, args.window):
                await consume(scraper.search(username, w_since, w_until, include_retweets=include_rts))
                print(f"  {w_since} → {w_until}: total {len(seen)} tweets ({new} new)")
    except (KeyboardInterrupt, asyncio.CancelledError):
        status = "interrupted"
        print("\nInterrupted: everything downloaded so far has been saved.")
    except Exception as e:  # noqa: BLE001 — record any failure in the run log
        status, error = "error", f"{type(e).__name__}: {e}"
        print(f"\nError: {error}")
    finally:
        store.finish_run(run_id, status, error)

    print(f"\nRun #{run_id} → {status}. {len(seen)} tweets found, {new} new.")
    return 0 if status == "ok" else 1


async def cmd_history(args: argparse.Namespace, store: Store, _scraper) -> int:
    if args.username:
        username = clean_handle(args.username)
        print(f"History for @{username}:")
        print_history(store, username, args.limit)
        return 0
    runs = store.runs(limit=args.limit)
    if not runs:
        print("No runs recorded.")
    for r in runs:
        print(f"#{r['id']:<4} @{r['username']:<20} {r['started_at'][:16].replace('T', ' ')}  "
              f"{r['since_date']} → {r['until_date']}  {r['status']:<11} new={r['tweets_new']}")
    return 0


async def cmd_export(args: argparse.Namespace, store: Store, _scraper) -> int:
    username = clean_handle(args.username)
    rows = [row_to_export(r) for r in store.tweets_for(username)]
    if not rows:
        print(f"No tweets saved for @{username}.")
        return 1
    out = Path(args.output or DATA_DIR / f"{username}.{args.format}")
    if args.format == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    print(f"{len(rows)} tweets exported to {out}")
    return 0


async def cmd_accounts(args: argparse.Namespace, _store, scraper: TwscrapeScraper) -> int:
    if args.action == "add":
        username = args.username or ask("Your X username (the account that will make the requests)")
        cookies = args.cookies
        if not cookies:
            if not (args.auth_token and args.ct0):
                print("Copy each cookie value from x.com (DevTools → Application → Cookies). Input is hidden.")
            auth_token = args.auth_token or ask_secret("auth_token")
            ct0 = args.ct0 or ask_secret("ct0")
            cookies = f"auth_token={auth_token}; ct0={ct0}"
        await scraper.add_cookie_account(clean_handle(username), cookies)
        print("Account added.")
    elif args.action == "remove":
        names = {a["username"].lower(): a["username"] for a in await scraper.accounts()}
        if not names:
            print("No accounts configured.")
            return 0
        username = clean_handle(args.username or ask(f"Account to remove ({', '.join('@' + n for n in names.values())})"))
        if username not in names:
            print(f"No account @{username} configured.")
            return 1
        if not ask_yes(f"Remove @{names[username]} and its saved cookies?"):
            print("Cancelled.")
            return 0
        await scraper.remove_account(names[username])
        print(f"Account @{names[username]} removed. Your scraped tweets and history are untouched.")
    accounts = await scraper.accounts()
    if not accounts:
        print("No accounts configured. Add one with: xextract accounts add")
    for a in accounts:
        print(f"  @{a['username']}  active={a['active']}  last used={a.get('last_used') or '-'}  {'' if a['error_msg'] in (None, 'None') else a['error_msg']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xextract", description="Scrape an X user's tweets and keep a log of every run.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scrape", help="Scrape tweets (interactive if any input is missing)")
    s.add_argument("username", nargs="?")
    s.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    s.add_argument("--until", help="YYYY-MM-DD (inclusive)")
    s.add_argument("--retweets", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--window", type=int, default=30, help="days per search query (default 30)")
    s.add_argument("--search-only", action="store_true",
                   help="skip the profile timeline and search the whole range (fills gaps in older history)")

    h = sub.add_parser("history", help="Show past runs")
    h.add_argument("username", nargs="?")
    h.add_argument("--limit", type=int, default=20)

    e = sub.add_parser("export", help="Export saved tweets")
    e.add_argument("username")
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    e.add_argument("-o", "--output")

    a = sub.add_parser("accounts", help="Manage the X accounts used for scraping")
    a.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    a.add_argument("--username")
    a.add_argument("--auth-token")
    a.add_argument("--ct0")
    a.add_argument("--cookies", help="full cookie string, e.g. 'auth_token=...; ct0=...'")
    return p


COMMANDS = {"scrape": cmd_scrape, "history": cmd_history, "export": cmd_export, "accounts": cmd_accounts}


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd is None:  # bare `xextract` → interactive scrape
        args = build_parser().parse_args(["scrape"])

    store = Store(DATA_DIR / "xextract.db")
    scraper = TwscrapeScraper(DATA_DIR / "accounts.db")
    try:
        code = asyncio.run(COMMANDS[args.cmd](args, store, scraper))
    except KeyboardInterrupt:
        code = 130
    finally:
        store.close()
    sys.exit(code)
