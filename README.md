# Wizz Watch — local cheap-flight alerts on your PC

Watches Wizzair for round-trip fares under a price you set (default **€50**, regular
price — not the Wizz Club price) and pops a Windows notification when it finds one.
Everything runs on your machine; nothing runs in Claude.

## Files
- `wizz_watch.py` — the watcher
- `config.json` — your settings (price, route URL, interval, etc.)
- `run_wizz_watch.bat` — one-click run, for Windows Task Scheduler
- `last_response.json` — raw API data, only written in `--debug` mode
- `seen.json` — remembers recent alerts so the same deal doesn't nag you hourly

## One-time setup
Open a terminal (PowerShell or cmd) in this folder and run:

    python -m pip install -r requirements.txt
    python -m playwright install chromium

(You need Python 3.9+ installed: https://www.python.org/downloads/ — tick
"Add Python to PATH" during install.)

## First run — IMPORTANT
Run once in debug mode so we can confirm the price fields are read correctly:

    python wizz_watch.py --debug

This saves `last_response.json` and prints the fares it parsed. **Send me that
`last_response.json` file** and I'll tighten the parser to Wizzair's exact field
names for regular vs. Club price. The generic parser works, but one look at the
real data makes it bulletproof.

## Normal use
Single check:

    python wizz_watch.py

Run forever, checking every hour (keep the window open):

    python wizz_watch.py --loop

## Run automatically every hour (recommended)
Use Windows Task Scheduler instead of leaving a window open:

1. Open **Task Scheduler** → **Create Task** (not "Basic Task").
2. **General**: name it `Wizz Watch`. Tick "Run whether user is logged on or not"
   is optional; for toast notifications to show, "Run only when user is logged on"
   is best.
3. **Triggers** → New → "On a schedule" → Daily → **Repeat task every 1 hour** for
   "Indefinitely".
4. **Actions** → New → Program/script: browse to `run_wizz_watch.bat` in this folder.
5. Save. It now checks hourly in the background and toasts you on a deal.

(If you prefer, the `--loop` mode does the same thing without Task Scheduler — just
leave the script running.)

## Settings (config.json)
- `fare_finder_url` — the Wizzair fare-finder page to watch. Change route/dates here
  by copying a new URL from your browser's address bar on wizzair.com.
- `max_price` — alert threshold (default 50).
- `currency` — only alert when the fare is in this currency (default "EUR"). If your
  results come back in another currency, change this to match.
- `use_club_price` — false = watch the regular price (what you asked for).
- `headless` — true runs the browser invisibly. If wizzair blocks headless runs,
  set this to false (a browser window will flash open each check).
- `interval_minutes` — used by `--loop` mode.
- `renotify_after_hours` — don't re-alert the same deal within this many hours.

## If it stops finding anything
Wizzair occasionally changes its anti-bot setup or page layout. If captures start
failing, set `"headless": false` first. If it still fails, send me a fresh
`last_response.json` (or the console output) and I'll adjust.

## Round-trip matching (updated)
The watcher now pairs each destination's outbound (home->X) and return (X->home)
legs into a round trip and decides with `match_mode` in config.json:

- `both_legs` (default) — alert only when EACH direction is under `max_price`
  (your "both ways under €50"). A cheap outbound with an expensive return is skipped.
- `total` — alert when outbound + return combined is under `max_price`.
- `per_leg` — alert if either single leg is under `max_price`.

Set `home_station` to your home airport code (default `TLV`). Each alert shows
both legs with date, time and flight number (when the API provides them) plus the
round-trip total. Times/flight numbers depend on the API fields — run once with
`--debug` and send `last_response.json` so those can be confirmed.
