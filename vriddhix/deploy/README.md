# Scheduling the nightly backfill

One job — `vriddhix backfill` — fetches whatever bars are new and then scans
every trading session that has bars but no completed scan.

It **catches up**; it does not do "today". If the Mac was asleep for a week,
the next run scans all five missing sessions, oldest first. A scheduler that
only ever processes the current date leaves a hole in the research database
for every day it did not run, and a hole in regime history stays invisible
until someone charts it months later and finds the line stops.

Order is not negotiable. RS trend and regime hysteresis each read the
previously stored value, so replaying dates out of order measures every day
against a future baseline.

## launchd (installed)

`ai.vriddhix.nightly.plist` runs the job at **19:00 IST on weekdays** — after
the 15:30 NSE close, with settling time. The machine runs on IST, so the
times need no conversion.

```bash
cp deploy/ai.vriddhix.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ai.vriddhix.nightly.plist

launchctl list | grep vriddhix          # registered?
launchctl start ai.vriddhix.nightly     # run now, don't wait for 19:00
tail -f state/logs/nightly.log          # watch it
launchctl unload ~/Library/LaunchAgents/ai.vriddhix.nightly.plist   # stop
```

`RunAtLoad` is **false** on purpose: loading the agent should not kick off a
multi-minute scan as a side effect. If the Mac is asleep at 19:00, launchd
runs the job on wake and the catch-up logic covers the missed sessions.

## The resident alternative

```bash
vriddhix backfill --schedule --hour 19 --minute 0
```

Stays in the foreground on an APScheduler weekday cron. Both paths call the
same `run_once`, so they cannot disagree about what a nightly run means. Use
launchd on a laptop that sleeps; use `--schedule` inside a container or
alongside a process supervisor.

## The catch-up cap

A run scans at most `--max-catchup` sessions (default 15) and reports what it
left. The cap exists so a machine that was off for a month does not silently
start a multi-hour job at 19:00 with nobody watching.

When the cap bites it drops the **oldest** pending sessions, never the newest
— the recent ones are what anybody is actually looking at tonight. Each night
therefore scans today first and then works backwards through history, so the
archive fills in on its own.

To replay everything in one go (hours, for a decade of history):

```bash
vriddhix backfill --since 2016-12-12 --max-catchup 0
```

The job prints that exact command, with the right date, whenever the cap bites.

## Safe to re-run

Ingestion is delete-then-insert per symbol, and context persistence replaces a
date's rows rather than appending. Running the job twice over the same window
changes nothing.

## Provider order matters

`config/vriddhix.yaml` lists `providers: [yfinance, csv_cache]` — live source
first, on-disk cache as the fallback.

The reverse order looks harmless and is not. The cache is a point-in-time
snapshot nobody refreshes, so leading with it makes every nightly run
re-import whatever it froze. The run still reports success, the newest bars
survive because the overwrite is range-scoped, and the body of history quietly
reverts to the older, sparser copy. The cache should answer when the network
is down, not by default.

## Broker token

The nightly job uses yfinance and does **not** need a Dhan token. The token is
for `quant_backtester`, lasts 24 hours, and is refreshed with:

```bash
python quant_backtester/dhan_token.py --check    # status and expiry
python quant_backtester/dhan_token.py --totp     # mint a fresh 24h token
```

Never commit `Dependencies/token_*.txt` or `state/dhan_token.json`.
