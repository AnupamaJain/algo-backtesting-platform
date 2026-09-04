# Running the platform

Two applications share one codebase:

| | What it does | Data |
|---|---|---|
| **Research pipeline** (Layers 1–4) | Backtests strategies, validates them, stress-tests them, builds portfolios | Real market history from yfinance |
| **Trading Ops console** | Places paper trades and shows live account state | Real market quotes, simulated money |

**Nothing in this system uses mock or sample data.** Prices come from the market,
fills happen at real quotes, and every number in the UI is computed by the Python
engine and read from its output — the browser never invents a figure.

---

## 1. Install

```bash
cd algo-backtesting-platform

python3 -m venv venv
source venv/bin/activate
pip install -r quant_backtester/requirements.txt

cd quant_backtester/web
npm install
```

Requires Python 3.11+ and Node 20+.

## 2. Start the web app

```bash
cd quant_backtester/web
npm run dev          # development, hot reload
# or
npm run build && npm start   # production build
```

### → Open **http://localhost:4300**

Both commands bind port **4300**. To use a different one:

```bash
npx next dev -p 5200
```

## 3. Load real market data

The console and the research pages are empty until data exists. From the UI, open
**Data & Strategies → Generate signals**, or run it directly:

```bash
source venv/bin/activate

python quant_backtester/main.py layer1               # download 30 assets, generate signals
python quant_backtester/main.py layer2               # walk-forward + 6-gate funnel
python quant_backtester/main.py layer3 --top-n 20    # sensitivity + bootstrap
python quant_backtester/main.py layer4 --top-n 20    # regimes + portfolios

# or all four:
python quant_backtester/main.py all --top-n 20
```

The first run downloads ~15 years of daily bars for 30 symbols (a minute or two).
Data is cached in `quant_backtester/data/`, so later runs are fast and re-use it.

---

## Paper trading

The paper broker is a **real broker adapter**, not a stub. It fetches live market
quotes, fills against them across the spread, charges commission and slippage,
and keeps a durable SQLite ledger. The only simulated thing is the money.

### From the browser

Open **http://localhost:4300/console** and use the order ticket. Orders fill
immediately at the live market price and appear in positions, fills and the
audit log.

### From the command line

```bash
source venv/bin/activate

# Live quote, straight from the market feed
python quant_backtester/broker_cli.py quote --symbol SPY

# Place a paper trade at the live price
python quant_backtester/broker_cli.py place --symbol SPY --side BUY --quantity 10

# Short
python quant_backtester/broker_cli.py place --symbol TLT --side SELL --quantity 5

# Resting limit order — fills when the market reaches it
python quant_backtester/broker_cli.py place --symbol QQQ --side BUY --quantity 5 \
    --order-type LIMIT --limit-price 600

# Account, positions and live marks
python quant_backtester/broker_cli.py state

# Paged history
python quant_backtester/broker_cli.py orders --page 1 --page-size 25
python quant_backtester/broker_cli.py fills  --page 1
python quant_backtester/broker_cli.py events --page 1

# Re-check resting orders against the current market
python quant_backtester/broker_cli.py poll

# Start over (destructive)
python quant_backtester/broker_cli.py reset
```

### Where the state lives

`quant_backtester/state/paper_broker.db` — orders, fills, cash and the audit
log. Positions are **derived** by replaying fills, never stored, so the console
can only show a state consistent with what actually executed. Deleting this file
resets the account.

---

## What's in this repository

The console's **Strategies** and **Watchdogs** pages read a live inventory
(`python quant_backtester/ops_cli.py inventory`) rather than a hardcoded list,
so they always reflect what is actually on disk.

### Production — Indian markets (Zerodha Kite)

| Family | Strategies |
|---|---|
| Premium selling | Survivor — NIFTY / SENSEX / Single stock, each with a hedged (auto-margin) variant |
| Mean reversion | Wave Extractor (bracket scalper), Expiry Trade (Stochastic RSI, 3-min) |
| Exit management | Early Exit — NIFTY, Early Exit — SENSEX |
| Income | Covered Calls |
| Risk overlay | Survivor Delta Rebalance |

### Watchdogs and analytics

GTT Monitor · Position Guard · Duplicate Order Monitor · Trade Journal ·
Broker API Monitor · Tradebook Analyzer · NIFTY Contributors (CAS) ·
Instrument Cache · Notifications

### Research — backtester library

RSI Snapback · Bollinger Reversion · Keltner Reversion · MA Crossover ·
Donchian Breakout · Simple Momentum

> The production modules are **installed and complete but not running**. They
> are Kite Connect implementations and need Zerodha credentials plus a running
> dashboard process. The console distinguishes *idle* (never run) from
> *missing files*, and `tests/test_inventory.py` fails if the inventory ever
> claims a module that is not on disk.

---

## Flattrade (Indian markets)

Flattrade covers **NSE / BSE / NFO / MCX only**. It cannot price SPY, QQQ,
AAPL or BTC-USD, so it uses its own universe of Indian symbols
(`config/universe_india.yaml`) rather than the US one.

### Automated login (recommended)

Flattrade tokens expire daily. With a password and TOTP seed configured, the
platform logs in **by itself** — no browser, no manual step, safe to run from
cron at 09:00.

Add two secrets to your environment (never to a tracked file):

```bash
export FLATTRADE_PASSWORD='your-login-password'
export FLATTRADE_TOTP_SECRET='YOUR-AUTHENTICATOR-SEED'   # base32, from Flattrade 2FA setup
```

The TOTP seed is the string behind the QR code when you set up your
authenticator app. With those set, everything is automatic:

```bash
python quant_backtester/main.py layer1 \
    --universe-config quant_backtester/config/universe_india.yaml
```

If the stored token is missing or from a previous day, the platform mints a
fresh one silently and caches it. You can also force a refresh:

```bash
python quant_backtester/flattrade_token.py     # logs in automatically
```

**How it works:** the same `POST /ftauth` call the login page makes — SHA256
password, a freshly generated TOTP in the second-factor field — returning a
redirect that carries the request code, which is exchanged for a token. Your
password is hashed before it leaves the process and is never written to disk.

Accounts still on PAN/DOB instead of TOTP can set `FLATTRADE_SECOND_FACTOR`
instead, though that value is static and therefore weaker.

To require a manual step deliberately, set `auto_login: false` under the
`flattrade` broker in `config/broker.yaml`.

### Browser fallback

For first-time setup, or if you have no TOTP seed:

```bash
python quant_backtester/flattrade_token.py --browser
python quant_backtester/flattrade_token.py --code <request_code>   # paste it yourself
```

The listener binds dual-stack (IPv4 **and** IPv6) on the port from your
`redirect_uri` — `localhost` resolves to `::1` first on macOS, so an
IPv4-only listener silently never sees the redirect.

App credentials come from `[flattrade]` in `configfile.ini`, or from
`FLATTRADE_API_KEY` / `FLATTRADE_API_SECRET` / `FLATTRADE_CLIENT_ID` in the
environment, which take precedence.

### Where to put the secrets

`configfile.ini` is **git-ignored**, so `[flattrade]` is a valid place for
them. Environment variables take precedence if you prefer those.

Key names are alias-tolerant — all of these are recognised in `[flattrade]`:

| Credential | Accepted keys |
|---|---|
| TOTP seed | `totp_secret`, `FLATTRADE_TOTP`, `FLATTRADE_TOTP_SECRET`, `totp` |
| Password | `password`, `FLATTRADE_PASSWORD` |
| Client ID | `client_id`, `FLATTRADE_CLIENT_ID`, `uid`, `user_id` |

Environment variables must be **`FLATTRADE_`-prefixed**. Bare names like `PWD`
or `UID` are deliberately ignored: `PWD` is the shell's working directory, and
reading it as a password would send a filesystem path to the broker.

> If you commit this repo somewhere new, confirm `configfile.ini` is still
> ignored before pushing.

### Fetching Indian data

```bash
python quant_backtester/main.py layer1 \
    --universe-config quant_backtester/config/universe_india.yaml
```

Bars land in `quant_backtester/data_india/`, kept separate from the US cache so
the two universes never mix. Layers 2-4 then run unchanged — the pipeline does
not know or care which market the data came from.

> Flattrade's EOD series is already adjusted for corporate actions, so the
> loader sets `Adj Close` equal to `Close`. That makes its back-adjustment step
> a no-op instead of adjusting twice.

### Paper trading on NSE

A paper account that fills against **real Flattrade prices** with simulated
money — the Indian equivalent of the default paper broker:

```bash
python quant_backtester/broker_cli.py quote --symbol RELIANCE-EQ --broker paper_india
python quant_backtester/broker_cli.py place --symbol TCS-EQ --side BUY --quantity 5 --broker paper_india
python quant_backtester/broker_cli.py state --broker paper_india
```

The two paper accounts keep **separate ledgers** (`paper_broker.db` and
`paper_india_broker.db`), so US and NSE positions never mix. Make it the
default with `active: paper_india` in `config/broker.yaml`.

> Outside NSE hours Flattrade reports bid/ask as `0.00`. The platform treats
> that as *no book* rather than a real quote, and fills against the last
> traded price instead — a zero reaching the fill path would price a trade at
> nothing.

### Trading through Flattrade

Set `active: flattrade` in `config/broker.yaml`. **This is a real broker** —
`is_simulated` is false, so every order sits behind the `live_trading` gate,
which is off by default.

---

## Safety

The console shows one of three modes, and states it on the order ticket itself:

| Mode | Meaning |
|---|---|
| **PAPER** | Simulated broker. Orders execute at live prices with fake money. |
| **DRY-RUN** | Real broker configured, but `live_trading: false`. Orders are logged, never sent. |
| **LIVE** | Real broker, real money. |

`live_trading` is **false by default** in `config/broker.yaml` and must be turned
on deliberately. Because the paper broker cannot move real money, the gate does
not block it — blocking simulation would defeat the point of having it.

Three more gates apply to every order, above every adapter, so a new broker
integration cannot forget them:

- **Max order value** — a fat-finger ceiling (default 50,000).
- **Duplicate suppression** — identical intent inside 30s is blocked; pass
  `--force` to override.
- **Retry policy** — rate limits and outages are retried with backoff;
  rejections never are.

Adjust all of these in `quant_backtester/config/broker.yaml`.

> **Never point this at a live broker without reading the adapter code first.**
> Only the paper adapter is implemented and tested. The `zerodha` and `dhan`
> entries in the config are reference stubs with no implementation behind them.

---

## Configuration

Every threshold is editable from the UI (each page carries its own settings at
the bottom) and written back to YAML with comments preserved:

| File | Controls |
|---|---|
| `config/universe.yaml` | Asset universe, date range |
| `config/strategy_grid.yaml` | Parameter sweep per strategy |
| `config/backtest.yaml` | Costs, walk-forward windows, the six funnel gates |
| `config/robustness.yaml` | Sensitivity offsets, bootstrap settings |
| `config/regime.yaml` | HMM detection, risk gates, capital routing |
| `config/broker.yaml` | Active broker, safety gates, paper account |

Credentials are **never** stored in config. Files name the environment variable
holding each secret; the value is read at runtime and fails loudly if unset.

---

## Tests

```bash
source venv/bin/activate
cd quant_backtester
python -m pytest tests/ -q          # 248 tests
```

Notable suites:

- `test_broker_contract.py` — runs the same contract against **every** adapter,
  so a new broker inherits the whole suite.
- `test_order_service.py` — safety gates: dry-run, duplicates, retry policy.
- `test_backtest_lookahead.py` — proves the engine cannot see the future.
- `test_costs_and_calendar.py` — regression tests for two bugs found in audit.

---

## Ports and processes

| Thing | Where |
|---|---|
| Web app | http://localhost:4300 |
| Server log | `/tmp/strategy-lab.log` when started with `nohup` |
| Paper account | `quant_backtester/state/paper_broker.db` |
| Market data cache | `quant_backtester/data/*.csv` |
| Pipeline output | `quant_backtester/results/` |

Stop a background server with `pkill -f "next start"`.

---

## Troubleshooting

**Console says "Broker layer unreachable"** — the web app shells out to
`quant_backtester/broker_cli.py` using `venv/bin/python`. Check the venv exists
and that the CLI runs on its own:

```bash
python quant_backtester/broker_cli.py state
```

**Quotes show "stale"** — the live feed was unreachable (outside market hours or
no network), so positions are marked against the last cached close. The prices
are still real, just not current; the UI labels them rather than pretending.

**Pages are empty** — no pipeline run yet. Run `main.py layer1` first.

**"Nothing has cleared the gauntlet"** on the Strategies page is a *result*, not
an error. Only strategies surviving both the validation funnel and the
robustness tests are offered for deployment.
