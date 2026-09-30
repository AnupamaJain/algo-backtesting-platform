# Running Pramana

Everything runs on this machine. One process serves the pages and the API;
the database is a SQLite file under `state/`.

---

## Launch it

```bash
cd ~/learningacademy/algo-backtesting-platform/algo-backtesting-platform/vriddhix
./run.sh start
```

Then open:

### **http://127.0.0.1:8787**

| | |
|---|---|
| http://127.0.0.1:8787/ | Landing — what it does, with live figures from the database |
| http://127.0.0.1:8787/app | **The workspace** — scan, open any name, read what the engines recorded |
| http://127.0.0.1:8787/learn | How each engine measures a chart, and the five quiet lies |
| http://127.0.0.1:8787/login | Sign in |
| http://127.0.0.1:8787/signup | Create an account |
| http://127.0.0.1:8787/api/docs | Browsable API reference (46 endpoints) |

`127.0.0.1` means **this machine only**. Nothing is reachable from your
network or the internet, which is the right default for something holding a
research database and account credentials.

---

## Sign in

A demo account already exists in your local database:

```
email     demo@vriddhix.test
password  research-instrument-2026
```

These work only against the SQLite file on this Mac. `state/` is gitignored,
so the account was never committed and does not exist anywhere else. Do not
reuse that password anywhere real, and create your own account at `/signup`
if you would rather.

To make another account from the command line:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a long enough passphrase","display_name":"You"}'
```

Passwords need 10 characters or more. Length is what matters — a passphrase
of ordinary words beats `P@ssw0rd!` and is easier to type. They are stored as
salted scrypt hashes; the plaintext is never written and cannot be recovered,
only reset.

---

## The trading terminal, from the same sign-in

The options dashboard is a **separate process on port 5010** that can place
real orders. Pramana cannot — it has no trading path at all. They are paired
for sign-in only; pairing them does not merge them.

```bash
./run-terminal.sh start     # from the repository root
./run-terminal.sh status    # running? paired? dry-run?
```

Signing in at `/login` and opening `/app` shows an **Open trading terminal**
button — but only for an account flagged as an operator:

```bash
python3 -m vriddhix.cli operator you@example.com            # grant
python3 -m vriddhix.cli operator you@example.com --revoke   # take it back
```

That flag is the whole point. Sign-up on the landing page is open to anyone,
so *being signed in* cannot be what admits someone to a live order book. A
non-operator never sees the button, and the endpoint behind it answers 403.

Pairing needs a shared secret both processes read, in `state/sso.env`
(gitignored, mode 600):

```bash
printf "export PRAMANA_SSO_SECRET='%s'\n" \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  > state/sso.env
chmod 600 state/sso.env
```

Both launchers source it automatically. Without it the handoff endpoint
answers 503 and the terminal's `/sso` route 404s — which is the right state
for an install that has not opted in.

It is deliberately **not** the API JWT secret. That one signs a twelve-hour
bearer token clients keep in `localStorage`; this one signs a ticket that
opens an order book. The ticket lasts 45 seconds, is single-use, and is
refused on replay — it travels in a URL, so it lands in browser history, in
any proxy log on the way, and in the `Referer` of whatever loads next.

---

## The other two apps

The workspace is one of three surfaces; `../run-all.sh` starts them all.
The header shows **Terminal** and **Lab** to operator accounts, and both go
through `/go/<target>` — a page that mints a short-lived ticket for the
target and forwards. Every app links to the others through that hub rather
than directly, because only this process holds the account.

The Lab is gated the same way the terminal is: `../run-lab.sh` sources the
shared secret in `state/sso.env`, and without it the Lab refuses every
request rather than opening by default.

**Backtesting one strategy.** On the Lab's Strategies page, every library
strategy has a **Backtest** link that pre-selects it in the run panel;
pick the market (India is priced by Flattrade with Dhan behind it) and
run. Narrowed runs write to `results_<market>/strategies/<name>/`, apart
from the full-grid baseline, because the two are not comparable: the last
gate is a multiple-comparison correction priced on how many
configurations were tried, so one strategy alone clears it more easily
than the same strategy inside the grid. RSIReversion clears the India
funnel on its own with 3 survivors and inside the full grid with 0. Both
are true.

**The Dhan token.** It lasts about a day and cannot be renewed
programmatically — `python quant_backtester/dhan_token.py --totp` mints
one from the PIN and TOTP seed in `configfile.ini`. It has also been seen
rejected (`DH-906 Invalid Token`) well before expiry; nothing in this
repository re-mints it, so the likely cause is Dhan ending the session when
another login happens, such as the Dhan app. Re-mint before the session and
treat `--check` as part of opening the terminal.

---

## Start, stop, check

```bash
./run.sh start      # start (prints the URLs)
./run.sh stop       # stop
./run.sh restart    # stop, then start
./run.sh status     # running? what does the database hold?
./run.sh logs       # follow the server log
```

`status` looks like this:

```
server: running (pid 95715) — http://127.0.0.1:8787
data  : 212 symbols · 475,835 bars · 2016-09-14 .. 2026-09-15
scans : 2,415 sessions · 813 patterns · 406 breakouts
regime: STRONG_BEAR (20.0) as of 2026-09-15
users : 2
agent : ai.vriddhix.nightly loaded (weekdays 19:00 IST)
```

To run on a different port:

```bash
VRIDDHIX_PORT=9000 ./run.sh start
```

---

## Keeping the data current

A launchd agent runs the pipeline at **19:00 IST on weekdays**, after the NSE
close. It is already loaded — `./run.sh status` confirms it on the `agent:`
line. The server does not need to be running for it to work; they are
independent.

It **catches up** rather than doing "today". If the Mac was asleep for a week,
the next run scans all five missing sessions, oldest first — order matters,
because relative-strength trend and regime hysteresis each read the previous
stored value.

To run it yourself, right now:

```bash
export PYTHONPATH=src
python3 -m vriddhix.cli backfill          # fetch new bars, scan what is missing
python3 -m vriddhix.cli backfill --no-ingest   # scan only, skip the download
```

Agent control:

```bash
launchctl list | grep vriddhix                              # loaded?
launchctl start ai.vriddhix.nightly                         # run now
tail -f state/logs/nightly.log                              # watch it
launchctl unload ~/Library/LaunchAgents/ai.vriddhix.nightly.plist   # turn off
launchctl load   ~/Library/LaunchAgents/ai.vriddhix.nightly.plist   # turn on
```

---

## The other commands

```bash
export PYTHONPATH=src

python3 -m vriddhix.cli status      # what is in the database
python3 -m vriddhix.cli ingest      # fetch bars and recompute features
python3 -m vriddhix.cli scan        # scan one date (default: the latest bar)
python3 -m vriddhix.cli backtests   # run whatever backtests are queued
python3 -m vriddhix.cli revalidate  # re-run quality rules over stored bars
python3 -m vriddhix.cli repair-regime  # rebuild the regime chain (dry run)
python3 -m vriddhix.cli init        # apply migrations (first run only)
```

`repair-regime` exists because regime is not a per-day fact. Hysteresis makes
each label depend on the one before it, so the stored sequence is a chain —
and a session inserted into the middle of it leaves every label after that
point computed against a predecessor that is no longer its predecessor.

That happens for a real reason: NSE holds occasional **weekend sessions** —
Budget Day, Muhurat trading, disaster-recovery drills — and when the nightly
catch-up discovers one long after the fact, it lands mid-chain. 2024-01-20
arrived that way and was labelled `STRONG_BULL` on a score of 78.3 while the
session before it, scoring 78.4, was `BULL`.

The repair replays the chain through the engine's own `classify` and
`apply_hysteresis`, so it cannot drift from what the scanner does. Scores are
never rewritten — only labels, which are the part that depends on the chain.
It is a dry run unless you pass `--apply`:

```bash
python3 -m vriddhix.cli repair-regime            # what would change
python3 -m vriddhix.cli repair-regime --apply    # change it
```

The nightly job now runs this automatically whenever it scans a date older
than its last completed scan, and logs that it did.

`revalidate` exists because a rule added today would otherwise only ever
apply to data arriving tomorrow, leaving the stored decade unexamined by it.
It re-runs every quality check over bars already in the database, without
re-fetching, and replaces the previous findings rather than adding to them.
Nothing is corrected — a guessed adjustment factor is its own silent
corruption, so a bad bar is flagged and left where it is.

It currently finds six `BAD_PRICE_SPAN` bars: NIFTYBEES, BANKBEES and
GOLDBEES each printed a decimal-shifted level for two sessions in December
2019 (GOLDBEES at ₹0.34 against neighbours at ₹33.60). Those bars are
excluded from the benchmark curve on the landing page. YESBANK's 56% fall on
the RBI moratorium is *not* flagged, because it was real.

A backtest is created through the API and then picked up by the worker:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/backtests \
  -H 'Content-Type: application/json' \
  -d '{"name":"VCP 70+","start_date":"2017-01-01","end_date":"2026-09-11",
       "config":{"entry":{"field":"vcp_score","cmp":"gte","value":70}}}'

python3 -m vriddhix.cli backtests
```

Results come back at `/api/v1/backtests/<id>/metrics`, always carrying the
survivorship mode the run was defined against.

---

## Running the tests

```bash
cd ~/learningacademy/algo-backtesting-platform/algo-backtesting-platform/vriddhix
source ../venv/bin/activate
export PYTHONPATH=src
pytest -q                 # 451 tests, about 30 seconds
```

The schema is built by running the migrations, so a migration that has
drifted from the models fails here rather than in a deployment.

---

## If something is wrong

**Port already in use.** `./run.sh stop`, then start again. Or pick another
port with `VRIDDHIX_PORT=9000 ./run.sh start`.

**"no virtualenv".** The venv lives one directory up, at
`algo-backtesting-platform/venv`. Recreate it with
`python3 -m venv venv && venv/bin/pip install -r vriddhix/requirements.txt`.

**Pages load but the numbers look old.** That is working as intended — stale
data is shown with its age rather than hidden, and the banner says so. Run
`python3 -m vriddhix.cli backfill` to refresh.

**A scan says "BIASED UNIVERSE".** Also intended. Index membership is
backfilled from today's constituent list, so historical results may overstate
performance through survivorship bias. The warning travels with the numbers
into every run and every backtest payload rather than living in a document
nobody reads.

**Server will not start.** `tail -20 state/logs/serve.log` — `run.sh` prints
the same lines when a start times out.

---

## The landing page

Two sections are driven by files rather than by the database:

**`config/testimonials.yaml`** — quotes shown under "What people using it
say". An entry renders only when `verified: true`, which means a real,
identifiable person said it about this software and agreed to it being
published with their name. The file ships empty, so the section does not
appear at all. It is written this way because a fabricated quote on a
financial product is a false statement about someone's experience of
something that affects money, and in India that sits inside SEBI's
advertising rules, which cover research and analytics products and not only
advice.

**The equity comparison** comes from the most recent completed backtest run.
To change what it shows, queue a different one:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/backtests \
  -H 'Content-Type: application/json' \
  -d '{"name":"VCP 70+ · 10y","start_date":"2016-10-01","end_date":"2026-09-17",
       "config":{"entry":{"field":"vcp_score","cmp":"gte","value":70}}}'

python3 -m vriddhix.cli backtests
```

The section disappears when no run has completed. It is never drawn from
placeholder data — an invented equity curve is the most misleading graphic a
research tool could show.

---

## Two things worth knowing before you read any number

**Nothing here is advice.** Scores and grades classify what a chart has
measurably done — prior trend, contraction quality, volume behaviour,
relative strength. They are not predictions and not recommendations. The
ledger records what followed similar setups historically, failures included;
that is a record of the past, not a claim about the future.

**This universe is 209 NSE F&O-eligible names, with a survivorship warning.**
Membership is backfilled from today's constituent list, so every hit rate,
failure rate and backtest figure is a measurement of the names that are
liquid *today* — not of the market as it stood in 2016.
