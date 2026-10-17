# xExtract

Scrapes the tweets from an X user for a date range you choose, saves them in a small SQLite database, and logs every run. The next time you scrape the same user, it shows what you already have and offers to continue from where the last run stopped.

---

## 1. Requirements

- macOS / Linux with Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- An X account (preferably a **secondary** one: X can rate-limit or suspend accounts that scrape)

## 2. Installation

```bash
cd ~/xExtract
uv sync
```

Run every command below **from the project folder**, because data is saved in `./data/`.

## 3. Connect your X account (once only)

X doesn't let you read tweets without logging in, so xExtract uses your browser session cookies.

1. Log in to **https://x.com** in Chrome, Firefox or Safari.
2. Open DevTools (`Cmd+Option+I`) → **Application** tab (in Firefox, **Storage**) → **Cookies** → `https://x.com`.
3. Copy the values of the **`auth_token`** and **`ct0`** cookies.
4. Run:

```bash
uv run xextract accounts add
```

```
Your X username (the account that will make the requests): my_secondary_account
Copy each cookie value from x.com (DevTools → Application → Cookies). Input is hidden.
auth_token:          ← paste the value (nothing is shown while you paste)
ct0:
Account added.
  @my_secondary_account  active=True  last used=-
```

It asks for each value separately and doesn't show what you paste. You can also pass them as flags. Anything you leave out is asked for:

```bash
uv run xextract accounts add --username my_secondary_account --auth-token XXX --ct0 YYY
```

> Prefer the prompts: values passed as flags are saved in your shell history.

Check the configured accounts at any time with `uv run xextract accounts`. Remove one with `uv run xextract accounts remove` (it lists your accounts and asks for confirmation) or `uv run xextract accounts remove --username NAME`. You can add more than one, and they are rotated automatically to spread out the rate limits.

> If you log out of x.com in the browser, the cookies stop working. Run `accounts add` again with the new ones.

## 4. Scrape tweets

### Interactive mode (recommended)

```bash
uv run xextract
```

It asks you for everything step by step. Press Enter to accept the value shown in `[brackets]`:

```
X username (without @): nasa

History for @nasa:
  @nasa has never been scraped.

Start date (YYYY-MM-DD): 2025-01-01
End date (YYYY-MM-DD) [2026-10-07]:
Include retweets? (y/N): n

Scraping @NASA (NASA) from 2025-01-01 to 2026-10-07 — run #1
  2026-09-08 → 2026-10-07: total 112 tweets (112 new)
  2026-08-09 → 2026-09-07: total 230 tweets (230 new)
  ...
Run #1 → ok. 2140 tweets found, 2140 new.
```

### Re-scraping the same user later

xExtract remembers the last run and suggests continuing from there:

```
History for @nasa:
  Saved tweets: 2140  (from 2025-01-02 to 2026-10-07)
  Recent runs:
   #1    2026-10-07 17:47  range 2025-01-01 → 2026-10-07  ok   found=2140 new=2140

  Last completed run covered up to 2026-10-07; by default it continues from there ...

Start date (YYYY-MM-DD) [2026-10-07]:      ← Enter = only fetch what's new
```

Tweets that are already saved **are not duplicated**. Their metrics (likes, RTs, views) are updated instead.

### How it fetches tweets

1. **Profile timeline** ("Posts & replies"): the most complete source, including all replies, but X only serves the latest ~3200 posts there.
2. **X search**, in date windows, only for the older part of your range that the timeline didn't reach. Search often misses replies from smaller accounts.

### Direct mode (no prompts)

Useful for scripts or cron jobs:

```bash
uv run xextract scrape nasa --since 2025-01-01 --until 2025-06-30 --no-retweets
```

| Option | Description |
|---|---|
| `username` | Handle, with or without `@` |
| `--since YYYY-MM-DD` | Start date (inclusive) |
| `--until YYYY-MM-DD` | End date (inclusive) |
| `--retweets` / `--no-retweets` | Include the user's retweets or not |
| `--window N` | Days per search query, for the older part the timeline doesn't reach (default 30). Lower it to 7–14 if you notice missing tweets |

Any option you leave out will be asked for interactively.

### Stopping halfway

Press **Ctrl+C**. Everything downloaded so far is kept and the run is marked `interrupted`. An interrupted run doesn't count as "the last successful run", so next time it will suggest the previous start date again.

## 5. View the history

```bash
uv run xextract history          # last 20 runs, all users
uv run xextract history nasa     # only @nasa, with tweet totals
uv run xextract history --limit 50
```

Possible run statuses:

| Status | Meaning |
|---|---|
| `ok` | Completed |
| `interrupted` | Stopped with Ctrl+C (partial data saved) |
| `error` | Failed (the message appears in the history) |
| `running` | Running now, or the process was killed abruptly |

## 6. Export the tweets

```bash
uv run xextract export nasa                    # → data/nasa.csv
uv run xextract export nasa --format json      # → data/nasa.json
uv run xextract export nasa -o ~/Desktop/nasa.csv
```

Exported columns: `id, username, created_at, text, lang, url, reply_count, retweet_count, like_count, quote_count, view_count, is_reply, is_retweet, is_quote, first_run_id, last_seen_at`.

## 7. Querying the database directly

Everything is stored in `data/xextract.db` (SQLite). You can open it with [DB Browser for SQLite](https://sqlitebrowser.org/), DBeaver, or the terminal:

```bash
sqlite3 data/xextract.db
```

```sql
-- 10 most-liked tweets
SELECT created_at, like_count, text FROM tweets
WHERE username = 'nasa' ORDER BY like_count DESC LIMIT 10;

-- tweets per month
SELECT substr(created_at, 1, 7) AS month, COUNT(*) FROM tweets
WHERE username = 'nasa' GROUP BY month ORDER BY month;

-- last run per user
SELECT username, MAX(started_at), status FROM scrape_runs GROUP BY username;
```

Tables:

| Table | Contents |
|---|---|
| `tweets` | One row per tweet: text, dates, metrics, reply/RT/quote flags and the full original JSON (`raw_json`) |
| `scrape_runs` | Log of every run: user, date range, status, tweets found, new tweets, error |
| `profiles` | ID, display name and followers of each scraped user |

## 8. Files and configuration

```
xExtract/
├── data/                  ← created automatically (in .gitignore)
│   ├── xextract.db        ← your tweets and the run history
│   └── accounts.db        ← your X cookies (DO NOT share)
└── src/xextract/
    ├── cli.py             ← commands and prompts
    ├── scraper.py         ← connection to X (twscrape)
    └── db.py              ← SQLite database
```

To save data somewhere else:

```bash
XEXTRACT_DATA=~/Documents/x-data uv run xextract
```

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `No account available` / no accounts | Run `uv run xextract accounts add` |
| The account shows `active=False` or auth errors | Cookies expired: copy new `auth_token` and `ct0` and run `accounts add` again with the same handle |
| `Rate limited by X, waiting until HH:MM...` | Normal: X allows ~50 searches per 15 min per account. It resumes on its own (gives up after 1 h). Add more accounts to go faster |
| `User @... not found` | Check the handle. Private or suspended accounts can't be scraped |
| Fewer tweets than expected | The profile timeline (step 1) is complete but only reaches the latest ~3200 posts; older ones come from X search, which skips many replies. Lower `--window` (e.g. `--window 7`) to help search. Deleted tweets still count in the profile's total but can't be scraped |
| A run stuck as `running` | The process was killed abruptly. The saved tweets are fine, just run it again |

> ⚠️ Automated scraping goes against X's terms of service. Use it responsibly and with an account you don't mind losing.
