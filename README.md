# Indian Markets Toolkit

Three self-hosted tools for Indian equity and index markets, in one
repository: a **live options-trading dashboard**, a **strategy backtester**,
and **VriddhiX**, a pattern-research platform that measures chart structure
and records what happened next.

Everything runs on your own machine against your own broker account. Nothing
is a hosted service, and nothing here is investment advice.

> ### ⚠️ Read this first
>
> **Part of this software can place real orders with real money.** It is
> experimental, may contain bugs, and may not behave as intended. The authors
> accept **no responsibility for any financial loss** caused by using it.
>
> Fresh installs start in **dry-run mode** — orders are simulated and nothing
> reaches the broker. Read **[DISCLAIMER.md](DISCLAIMER.md)** in full before
> you change that.
>
> The research platform (VriddhiX) has **no trading path at all**. It reads
> market data and writes measurements; it cannot place an order.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Market](https://img.shields.io/badge/market-NSE%20%C2%B7%20BSE-orange)

---

## What's in here

| | | |
|---|---|---|
| 🔬 | **[VriddhiX](vriddhix/)** | Pattern research: VCP, market structure, regime, relative strength. Detects, scores, and tracks outcomes across a decade. **Read-only — cannot trade.** |
| 📊 | **[quant_backtester](quant_backtester/)** | Strategy backtesting over Indian equities, with broker adapters for Dhan and Flattrade. |
| 📈 | **UI Trading System** (repo root) | Flask dashboard for live NIFTY/SENSEX options trading on Zerodha Kite. Positions, Greeks, GTT monitoring, automated strategies. **Trades real money.** |

They share a Python environment but are otherwise independent — you can run
any one without the others.

---

## 🔬 VriddhiX — pattern research

A research instrument, not a prediction machine. It finds chart structure in
NSE equities, places each setup in market and sector context, scores it, and
then records what actually happened next — including every time it was wrong.

```bash
cd vriddhix
pip install -r requirements.txt
export PYTHONPATH=src

python -m vriddhix.cli init                            # apply migrations
python -m vriddhix.cli bootstrap --universe nse_fno.yaml   # 209 NSE F&O names
python -m vriddhix.cli ingest                          # 10 years of daily bars
python -m vriddhix.cli backfill                        # run every engine
./run.sh start                                         # http://127.0.0.1:8787
```

Full guide: **[vriddhix/RUNNING.md](vriddhix/RUNNING.md)** ·
Design docs: **[vriddhix/docs/](vriddhix/docs/)** (10 specifications)

**What it measures**

| Engine | Finds |
|---|---|
| VCP | Volatility contraction bases — prior trend, contractions, pivot, breakout |
| Market structure | Swing highs and lows, BOS, CHoCH, order blocks, liquidity sweeps |
| Fair value gaps | Three-candle imbalances, tracked to mitigation |
| Market regime | Five weighted components with hysteresis, or an explicit refusal |
| Relative strength | Percentile rank across the universe, blended over four horizons |
| Sector rotation | Equal-weight aggregation into leading/improving/weakening/lagging |

**Four commitments it is built around**

- **One implementation of every rule.** The live scanner, the backtester and
  the historical X-Ray call the same function objects — there is no second
  implementation to drift from. A test walks the syntax tree to enforce it.
- **Nothing may see the future.** A value dated *t* depends only on bars dated
  *≤ t*, verified by truncating history and demanding identical output. A
  look-ahead bug never crashes; it produces a backtest that looks excellent
  and means nothing.
- **It says what it does not know.** An unranked sector reads `null`, not `0`.
  Absence is never quietly rendered as a bearish number.
- **Failures are kept.** Failed breakouts stay in the ledger with their reason
  and no view drops them by default. A hit rate over survivors is the single
  most flattering lie a research tool can tell.

436 tests, including no-look-ahead and engine-purity suites.

---

## 📊 quant_backtester — strategy backtesting

Replays strategies over Indian equity history with realistic costs — STT,
stamp duty, exchange and SEBI charges, GST — and broker adapters for Dhan and
Flattrade.

```bash
cd quant_backtester
python -m pytest -q
```

See **[quant_backtester/RUNNING.md](quant_backtester/RUNNING.md)**.

---

## 📈 UI Trading System — live options trading

> **This is the part that can lose money.** Read
> [DISCLAIMER.md](DISCLAIMER.md) before enabling live mode.

A Flask dashboard for NIFTY and SENSEX options on
[Zerodha Kite Connect](https://kite.trade).

| Module | What it does |
|---|---|
| **Positions** | Live option positions with delta/theta Greeks and margin |
| **Wave Extractor** | Gap trading: linked BUY+SELL pairs re-placed as price waves move |
| **Survivor** | Single-leg index strategy with delta-based rebalancing |
| **Expiry Trade** | Expiry-day strategy on 3-minute candles with Stochastic RSI |
| **Early Exit** | Pre-market fair-value GTT exit orders |
| **GTT Monitor** | Watches triggers, detects duplicates, suppresses stale orders |
| **Position Guard** | Flags unreviewed long exposure across positions, orders and GTTs |
| **Trade Journal** | FIFO pairing, per-algo P&L attribution, broker reconciliation |
| **Covered Calls** | Sell OTM calls against held equity |
| **Notifications** | Telegram bot and Web Push for fills, margin and system events |

```bash
pip install -r requirements.txt
cp configfile.ini.example configfile.ini   # then fill in your own keys
python setup_wizard.py                     # guided configuration
python flask_app.py                        # http://127.0.0.1:5000
```

Starts in **dry-run mode**. Orders are simulated until you explicitly enable
live trading.

---

## Setup

**Requirements** — Python 3.11+ (3.12 tested), and a broker account for
whichever parts you use:

| Component | Needs |
|---|---|
| VriddhiX | Nothing. Market data comes from yfinance. |
| quant_backtester | Dhan or Flattrade credentials for live data; historical CSVs work offline |
| UI Trading System | A Zerodha [Kite Connect](https://developers.kite.trade/) subscription (paid) |

```bash
git clone https://github.com/AnupamaJain/algo-backtesting-platform.git
cd algo-backtesting-platform

python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Credentials

**Never commit your credentials.** `configfile.ini` is gitignored; the
repository ships `configfile.ini.example` with placeholders only.

```bash
cp configfile.ini.example configfile.ini
```

Then fill in the sections you need. Broker tokens are short-lived and are
written to gitignored paths under `state/`:

```bash
python quant_backtester/dhan_token.py --check   # status and expiry
python quant_backtester/dhan_token.py --totp    # mint a fresh 24h token
```

Files that must never reach the repository — all already ignored:

```
configfile.ini              broker API keys, secrets, PINs, TOTP seeds
Dependencies/               instrument dumps and live access tokens
*/state/                    databases, session tokens, signing keys
```

---

## Project layout

```
.
├── vriddhix/            🔬 pattern research platform (self-contained)
│   ├── src/vriddhix/       domain · db · data · engines · services · jobs · api
│   ├── docs/               10 design specifications
│   ├── tests/              436 tests
│   └── RUNNING.md          start/stop, sign in, scheduling
│
├── quant_backtester/    📊 strategy backtesting + broker adapters
│
├── templates/           📈 UI Trading System — Flask views
├── notifications/          Telegram and Web Push
├── position_guard/         exposure checks
├── covered_calls/          covered-call workflow
├── flask_app.py            the dashboard entry point
│
├── configfile.ini.example  copy to configfile.ini and fill in
└── DISCLAIMER.md           read before enabling live trading
```

---

## Testing

```bash
# VriddhiX
cd vriddhix && PYTHONPATH=src python -m pytest -q     # 436 tests

# Backtester
cd quant_backtester && python -m pytest -q

# Trading system
python -m pytest tests -q
```

VriddhiX builds its test schema by running the Alembic migrations, so a
migration that has drifted from the models fails in the suite rather than in
a deployment.

---

## Contributing

Issues and pull requests are welcome. For anything touching order placement,
please describe how you tested it in dry-run mode first.

---

## Licence

[MIT](LICENSE).

## Disclaimer

This software is provided for **research and educational purposes**. It is
not investment advice, and no part of it is a recommendation to buy or sell
any security.

Scores, grades and backtest results describe measured historical behaviour.
They are not predictions. Past performance does not indicate future results.
Backtests that cannot resolve point-in-time index membership say so in their
own output, because a result built on today's constituent list overstates
what was achievable at the time.

Trading and investing carry risk of loss. You are responsible for your own
decisions. Consider your circumstances and consult a SEBI-registered adviser
before acting on anything produced by this software. See
**[DISCLAIMER.md](DISCLAIMER.md)** for the full text.
