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
python3 -m vriddhix.cli init        # apply migrations (first run only)
```

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
