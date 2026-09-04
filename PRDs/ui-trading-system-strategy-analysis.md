# Strategy & Architecture Analysis: `Raahi-Bhushan/ui-trading-system`

**Repo:** https://github.com/Raahi-Bhushan/ui-trading-system
**Related strategy source repo:** https://github.com/Raahi-Bhushan/trading-algo
**Author's technical write-ups:** https://raahibhushan.substack.com

## How this document was built (read this first)

Two source types were used, and they're kept distinct throughout:

1. **The author's own architecture write-up** ("I Open-Sourced My Zerodha Algo-Trading Dashboard") — this names real files, real functions, and describes real data flow: `common_lib.py`, `positions_lib.py`, `instrument_cache.py`, `greeks_lib.py`, `retry_with_backoff()`, `save_executed_order()` / `load_todays_orders()`, `place_order()`, the WebSocket-vs-polling reconciliation design, the process-per-symbol model. This is first-party and treated as ground truth.
2. **Two long-form strategy deep-dives** (Survivor, Wave Extractor) where the author walks through worked examples with real numbers and parameter names.

**What this document does NOT contain:** line-by-line source code. GitHub's raw file content for this repo could not be retrieved through available tools (permission errors on every direct blob fetch, even for files visible in the repo's own listing) — so nothing below is copied or paraphrased from actual `.py` source lines. Every diagram and mechanism below is reconstructed from the author's own descriptions of what that code does, not from reading the code myself. Where the repo's file tree gives a name but no public description exists (Expiry Trade internals, Covered Calls internals, several ops modules), that gap is called out explicitly rather than filled in with a guess.

---

## 1. System-level architecture (applies to every strategy)

This is the substrate every strategy below is built on top of — worth understanding first because none of the individual strategies re-implement it.

```
AUTH
  Browser → Flask gatekeeper login → Kite Connect OAuth
    → access_token → Flask session + instruments.db (restart recovery)

ORDER PLACEMENT (every strategy funnels through this same path)
  strategy process → common_lib.place_order()
    → retry_with_backoff() wraps every Kite API call
    → [safety] live_trading config gate
        - false → order logged in full, NOT sent to Zerodha, returns sentinel -1
        - true  → order sent to Kite Connect API
    → save_executed_order() persists state to a JSON file
    → on_order_update() WebSocket callback updates that JSON state
    → a 180-second polling loop is the AUTHORITATIVE source of truth
      (WebSocket is a fast-path optimization only — see reconciliation note below)

POSITIONS
  KiteConnect.positions() → positions_lib.py → greeks_lib.py (Black-Scholes or
    opengreeks, config-selected) → dashboard (delta/theta per position)

INSTRUMENTS
  daily 9 AM IST → instrument_cache.sync_instruments()
    → SQLite staging table → atomic rename/swap → live table
    (guarded by threading.Lock() so a background sync and a manual
     "Sync Instruments" click can't race on the same staging table)
```

**Process model:** Strategies that trade continuously — **Wave Extractor** and **Survivor** — run as **separate OS processes**, not threads inside the Flask app. One process per symbol, each writing its own log file. The dashboard tracks each by **PID** and refuses to spawn a second scraper for a symbol that already has one running (prevents accidental duplicate bots on the same instrument).

**State persistence pattern used by every strategy:** `save_executed_order()` / `load_todays_orders()` — orders placed today are written to a JSON file and reloaded on restart, so a strategy process that crashes and restarts mid-session doesn't lose track of what it already has open. This is also why new strategies are described as cheap to add: "look at `place_order_at_nifty.py` or `ticker_single_scraper_new.py` as templates ... a new strategy is mostly 'when should I call `place_order()`', not new infrastructure."

**A documented reconciliation bug/fix worth knowing about:** WebSocket order-update callbacks don't always arrive in the order you'd expect — a `COMPLETE` callback can arrive *before* the corresponding `OPEN` callback, especially for GTT-fired orders. A state machine that assumes `OPEN` always comes first will silently drop completions. The author's fix was architectural: the 180-second polling loop is the source of truth for order state; the WebSocket only accelerates how fast the dashboard *notices* a change, it doesn't drive the state machine.

**Dry-run gate:** Every fresh install has `live_trading = false`. Every strategy's call into `place_order()` still executes its full logic and logging — the *only* thing dry-run changes is whether the Kite Connect API call actually fires. Orders return `-1` as a sentinel in dry-run so strategy code has a clean "no fill" path rather than tracking a phantom order.

---

## 2. Survivor — trend-following / breakout hedge algo

**Files:** `place_order_at_nifty.py`, `place_order_at_nifty_with_buy_auto.py`, `place_order_at_sensex.py`, `place_order_at_sensex_with_buy_auto.py`, `place_order_at_stock.py`, `place_order_at_stock_with_buy_auto.py`, `survivor_delta_rebalance.py`, `survivor_events_db.py`, `positions_lib.py` / `sensex_positions_lib.py`

**Purpose:** Defends an existing option book when the market breaks out and *keeps running* instead of mean-reverting — built after the author took heavy losses fighting trending markets while positioned for reversion.

### Process/module architecture

```
place_order_at_nifty.py  (or _sensex / _stock variant)
    │
    ├─ reads support/resistance levels from CLI args
    ├─ subscribes to live price via Kite WebSocket (through common_lib)
    ├─ on each tick:
    │     is price beyond resistance + n·gap?  → sell PE, strike = spot − y
    │     is price beyond support   − n·gap?  → sell CE, strike = spot + y
    │     (checks min_price_to_sell floor first; walks strike inward if premium too low)
    │
    ├─ calls common_lib.place_order() for the sell leg
    ├─ save_executed_order() persists the fill to JSON state
    └─ on reversal by reset_gap points, re-anchors that side's trigger level closer to spot

survivor_delta_rebalance.py
    │  (separate script — runs against the CURRENT open book, not new triggers)
    ├─ reads live positions via positions_lib.py
    ├─ computes net delta across the book (via greeks_lib.py)
    └─ rebalances / adjusts legs to bring portfolio delta back toward the target band
       — this is the "delta-based rebalancing" named in the README feature table

survivor_events_db.py
    └─ persistence layer logging Survivor's trigger events (separate from the
       generic trade_journal.py, specific to this strategy's own event history)
```

### Entry logic (from the author's worked example)
```
python3.8 place_order_at_nifty.py NIFTY25724 300:300 NFO 15:15 90:90 300:300 25140:24928 <TOKEN>
```
| Arg | Meaning |
|---|---|
| `NIFTY25724` | expiry-tagged symbol prefix |
| `300:300` (1st) | strike distance (y) — PE/CE sold 300 pts OTM |
| `15:15` | gap (n) — every 15-pt move triggers one execution |
| `90:90` | reset gap — reversal distance that re-anchors the trigger level |
| `300:300` (2nd) | quantity per execution |
| `25140:24928` | resistance:support that must break before the algo activates |

### Stop loss / Target
No hard-coded SL/target in the trigger logic itself — this is a **discretionary, author-guided** exit:
- Exit an ITM leg at ~**30–50% loss** (50% same-day, tighten to 30% if carried over).
- Take profit when premium decays below **₹20** *and* profit is **>50%**.
- The continuous re-triggering itself is described as giving ~**100–150 points of backtrace tolerance** before a position turns into a real loss — i.e., the rolling behavior *substitutes for* a stop loss rather than sitting alongside one.

### Margin / hedge management — the `_with_buy_auto` variant
```
place_order_at_nifty_with_buy_auto.py
    │
    ├─ before adding a new sell leg, evaluates:
    │     (a) buy back an existing decayed leg → lock in profit, free margin
    │     (b) buy a cheaper option against the NEW sell → forms a spread
    ├─ chooses based on which is cheaper / more margin-efficient at that moment
    └─ places both legs via common_lib.place_order()
```
This exists specifically because **naked selling into a large one-sided move can exceed available margin** — running a spread instead of a naked short reduces the margin requirement, at the cost of capping some premium upside. The plain (`place_order_at_nifty.py`) variant is naked-only and relies purely on the reset-gap/manual-exit discipline for risk control.

### Squareoff / schedule
Author runs this **Monday–Wednesday only** as an adjustment overlay on that week's book — no automated end-of-day squareoff is described; exits follow the manual thresholds above.

### Delta hedge relationship to Wave
`survivor_delta_rebalance.py` and Wave Extractor's own delta gate (see below) both reference **portfolio-level delta** — this is the mechanism that lets the author run Survivor and Wave *together* as an intentional hedge pair (Wave profits in chop, Survivor profits/offsets in trend) rather than each strategy managing risk in total isolation.

---

## 3. Wave Extractor — range/theta-positive bracket scalper

**File:** `ticker_single_scraper_new.py` (one process per symbol)

### Process architecture (from the author's step-by-step walkthrough)
```
ticker_single_scraper_new.py  <symbol_gap> <cooloff> <OPTION_SYMBOL> <buy_qty:sell_qty> <TOKEN>
    │
    ├─ on launch: places a resting BRACKET around current price
    │     BUY  @ price − gap
    │     SELL @ price + gap
    │
    ├─ LOOP (guarded — only fires when zero orders are pending):
    │   1. wait for one side of the bracket to fill (via common_lib / WebSocket)
    │   2. on fill: cancel the OTHER pending order immediately
    │   3. update net position (running buys − sells)
    │   4. consult multiplier_scale:
    │        net short → widen buy_gap  (e.g. ×1.3–1.7)
    │        net long  → widen sell_gap
    │   5. cool-off pause (~5 sec) — circuit breaker against rapid re-fire
    │   6. before placing the new bracket, check portfolio option delta
    │        (check_changes_in_restrictions) — if the account is already too
    │        directionally exposed, the corresponding side of the new bracket
    │        is suppressed even though local logic wants it
    │   7. place new bracket around the new price, repeat
    │
    └─ runs continuously for the life of the process (effectively all session)
```

### Worked example (NIFTY futures @ 23,000, 10-pt gap)
1. Launch → BUY @ 22,990, SELL @ 23,010 placed.
2. Price dips to 22,990 → BUY fills. Net position now long.
3. Pending SELL @ 23,010 cancelled instantly.
4. `multiplier_scale` widens the *next* buy_gap (e.g. 10 → 13 pts) since the bot is now net long and shouldn't be eager to add more on dips.
5. 5-second cool-off.
6. New bracket at 22,990: SELL @ 23,000 (10 pts above), BUY @ 22,977 (13 pts below, widened).
7. Repeat.

### Stop loss / Target
**None, by design.** Risk is structural: `multiplier_scale` makes the bracket progressively harder to fill against the bot as a directional position builds, and the portfolio delta check is a hard gate that can block a leg outright regardless of local bracket logic.

### Position sizing
Fixed quantity per leg, set at process launch (example given: `75:75`). Size itself doesn't change — only the **gap distance** adapts.

### Margin
No margin-specific handling described (unlike Survivor's `_with_buy_auto`) — this trades naked futures/option brackets, so margin is whatever the broker requires for the resulting position at any point in the cycle.

---

## 4. Expiry Trade

**File:** `expiry_trade_lib.py`
**README description:** Expiry-day strategy on 3-minute candles using Stochastic RSI signals.

**What's confirmed:** it's a `_lib.py` module (imported by a runner, following the same pattern as Early Exit's `_lib.py` files), intraday by definition (same-day expiry), signal-driven off a technical indicator (Stochastic RSI) rather than the level-breakout logic Survivor uses.

**What's not publicly available:** entry/exit thresholds, position sizing, SL/target, and squareoff timing. No Substack deep-dive exists for this module, and the source itself wasn't retrievable through available tools. This should be read directly from `expiry_trade_lib.py` and the repo's own `docs/` site before relying on any assumption about how it behaves.

---

## 5. Early Exit

**Files:** `early_exit_lib.py` (NIFTY), `early_exit_sensex_lib.py` (SENSEX)
**README description:** Pre-market fair-value GTT exit orders for NIFTY/SENSEX options.

### Inferred architecture (from module naming + system-level facts we do know)
```
early_exit_lib.py / early_exit_sensex_lib.py
    │  (runs pre-market, before the session opens)
    ├─ pulls current open option positions (positions_lib.py)
    ├─ computes theoretical fair value per position
    │     — likely via greeks_lib.py's Black-Scholes facade, since that's the
    │       only pricing model named anywhere in the repo's architecture
    ├─ places a GTT (Good-Till-Triggered) exit order at that fair-value level
    └─ gtt_monitor.py subsequently watches these GTTs for triggers/duplicates
       and suppresses stale ones when the market is closed
```
This is an **exit-management layer that sits on top of** whatever opened the position (Survivor, Wave, or a manual trade) — not a standalone entry strategy. It's the one module in the repo that automates a decay-based take-profit mechanically rather than leaving it to discretion.

---

## 6. Covered Calls

**Folder:** `covered_calls/` (a sub-package, not a single file — distinct from the strategy scripts living at repo root)
**README description:** Sell OTM calls against held equity to earn premium.

**What's confirmed:** it's structured as its own module directory rather than a flat script, suggesting more internal organization (likely separate files for strike selection, roll logic, and assignment handling) than the single-file strategies above — but the individual file names inside `covered_calls/` weren't retrievable to confirm.

**Type:** Positional by nature — sold against an existing equity holding, not opened and closed same-day.

**Not publicly available:** strike-selection distance from spot, roll timing/triggers, and assignment handling.

---

## 7. Supporting infrastructure modules (not strategies, but load-bearing for all of them)

| File | Role (per architecture write-up / README) |
|---|---|
| `common_lib.py` | Core engine: order placement/cancellation with `retry_with_backoff()`, gap management, delta calculations, order-state persistence (`save_executed_order`/`load_todays_orders`) — every strategy above imports this |
| `instrument_cache.py` | Resolves symbols → instrument tokens and lot sizes via daily SQLite sync; strategies never hardcode tokens because they change after corporate actions, and lot sizes change too |
| `positions_lib.py` / `sensex_positions_lib.py` | Wraps `KiteConnect.positions()`, feeds Greeks calculation and Survivor's delta rebalancer |
| `greeks_lib.py` | Facade over an embedded Black-Scholes implementation or the faster `opengreeks` package, config-selected — used for both dashboard display and Survivor's delta rebalancing |
| `gtt_monitor.py` | Watches GTT triggers (including those placed by Early Exit), detects duplicates, suppresses stale GTTs when market is closed |
| `duplicate_order_monitor.py` | Separate guard against double-firing orders, distinct from the dashboard's PID-based double-spawn protection for strategy processes |
| `position_guard/` | Flags any symbol with unreviewed long exposure across live positions, open orders, and pending GTTs — a manual-review risk gate, not automated risk closure |
| `trade_journal.py` / `trade_journal_db.py` | FIFO buy/sell pairing, per-algo P&L attribution, reconciled against Zerodha's own tradebook (`tradebook_analyzer.py`) |
| `kite_api_monitor.py` | Logs every Kite API call with caller, latency, errors — operational reliability, not trade risk |
| `notifications/` | Telegram bot + browser Web Push, wired into the same order-event path every strategy already emits into — new strategies get alerting "for free" |

---

## 8. Cross-cutting summary

| Aspect | Survivor | Wave Extractor | Expiry Trade | Early Exit | Covered Calls |
|---|---|---|---|---|---|
| **Entry trigger** | Support/resistance break + n-pt increments | Continuous resting bracket orders | Stochastic RSI on 3-min candles (mechanics not public) | N/A — exit-only module | Not public |
| **Position sizing** | Fixed qty per trigger (CLI-set) | Fixed qty per leg (CLI-set); gap adapts, not size | Not public | N/A | Not public |
| **Stop loss** | None hard-coded; author's manual 30–50% rule | None; structural via `multiplier_scale` gap widening | Not public | N/A | Not public |
| **Target** | None hard-coded; premium <₹20 and >50% profit | Implicit — opposite bracket leg IS the target | Not public | Fair-value GTT level (this module's whole purpose) | Not public |
| **Trailing** | Reset-gap re-anchors trigger level on reversal | `multiplier_scale` widens losing side's gap | Not public | N/A | Not public |
| **Hedge** | Explicit — `_with_buy_auto` variant forms spreads or closes legs | Portfolio delta gate blocks over-exposed adds | Not public | N/A | Inherent to the strategy (short call against long stock) |
| **Margin handling** | Addressed directly (spread reduces requirement) | Not addressed — naked bracket margin | Not public | N/A | Not public |
| **Schedule** | Mon–Wed adjustment overlay | Continuous/intraday, all session | Expiry day only (intraday) | Pre-market, daily | Positional, held against equity |
| **Squareoff** | Manual, per author's thresholds | Not automated at strategy level | Not public | N/A (it IS the exit mechanism) | Not public |

---

## 9. Caveats

1. **No raw source was read for this analysis.** Every mechanism above is reconstructed from the author's own public descriptions (architecture post + two strategy deep-dives) plus the repo's file tree — not from inspecting `.py` file contents directly, which repeated tool attempts couldn't retrieve. Treat file/function names as accurate (they're taken directly from the author's own text), but treat any described *internal logic* for Expiry Trade and Covered Calls specifically as unconfirmed — those two have no public deep-dive at all.
2. **The repo's own `docs/` site** (referenced in the README, hosted separately) is described as containing the full configuration reference, architecture doc, and per-module guides — that would be the authoritative source for exact current parameters, and should be checked directly before configuring or running anything.
3. **Code drifts from blog posts.** The deep-dives are dated July–November 2025 (Wave) and the architecture post is July 2026; the repo continues to change.
4. **No hard stop loss on Survivor or Wave.** Both substitute structural/behavioral risk controls (rolling triggers, adaptive gap widening, portfolio delta gating) for a conventional price-based stop order. That's a deliberate design choice, but it means risk is bounded by process discipline and portfolio-level checks, not by an order resting in the market.
5. **Repo's own warning stands:** this software can trade real money, is explicitly described as experimental and possibly buggy, ships in dry-run mode by default, and the author accepts no responsibility for financial losses. `DISCLAIMER.md` should be read in full before enabling live trading.

---

## 10. Full repository & runtime architecture — a teaching walkthrough

Use this section as a script if you're explaining the project to someone else from scratch. It's organized as: **what's in the repo → what each piece is for → how they connect at runtime → a full day traced step by step.** Everything here is grounded in the repo's actual file tree and the author's own architecture description (Section 1) — nothing invented.

### 10.1 The repo, folder by folder

```
ui-trading-system/
├── .github/workflows/        CI automation (see 10.2 — exact jobs unconfirmed)
├── covered_calls/            Covered Calls strategy, as its own sub-package
├── docs/                     Source for the hosted documentation site (mkdocs)
├── mibian/                   Vendored Black-Scholes options-pricing library
├── notifications/            Telegram bot + browser Web Push integration
├── position_guard/           Unreviewed-exposure risk-flagging module
├── templates/                Flask/Jinja2 HTML templates — the dashboard's UI
├── tests/                    pytest suite (real SQLite, no DB mocking)
├── vendor/pykiteconnect/     Vendored copy of Zerodha's Kite Connect SDK
│
├── flask_app.py              Entry point — the web server / dashboard process
├── setup_wizard.py           First-run onboarding: collects API keys, writes configfile.ini
├── common_lib.py             Core order engine — everything below calls into this
├── instrument_cache.py       Symbol → token/lot-size resolver, SQLite-backed
├── positions_lib.py / sensex_positions_lib.py   Live position fetch + shaping
├── greeks_lib.py             Black-Scholes / opengreeks facade
├── gtt_monitor.py            GTT trigger watcher
├── duplicate_order_monitor.py, kite_api_monitor.py   Ops/safety watchers
├── trade_journal.py / trade_journal_db.py / tradebook_analyzer.py   P&L accounting
├── survivor_events_db.py     Survivor-specific event log
│
├── place_order_at_nifty.py, _sensex.py, _stock.py    Survivor (naked)
├── place_order_at_*_with_buy_auto.py                 Survivor (hedged variant)
├── survivor_delta_rebalance.py                       Survivor's delta rebalancer
├── ticker_single_scraper_new.py                      Wave Extractor
├── expiry_trade_lib.py                               Expiry Trade
├── early_exit_lib.py / early_exit_sensex_lib.py       Early Exit
│
├── configfile.ini.example    Template config (real config is git-ignored, holds secrets)
├── requirements.txt / pyproject.toml   Python dependencies + packaging
├── mkdocs.yml                 Docs-site build config
├── firebase.json              Hosting config — the docs site is served via Firebase Hosting
├── .pre-commit-config.yaml    Local git-hook linting/formatting, run before each commit
├── DISCLAIMER.md, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, LICENSE (MIT)
```

**How to narrate this to someone new:** split it into three buckets —
1. **"The brain and its memory"** — `flask_app.py` (dashboard), `common_lib.py` / `instrument_cache.py` / `positions_lib.py` / `greeks_lib.py` (shared engine every strategy depends on).
2. **"The traders"** — every `place_order_at_*.py`, `ticker_single_scraper_new.py`, `expiry_trade_lib.py`, `early_exit_*.py`, `covered_calls/` — these are the actual decision-makers, and each one is a thin layer of *strategy logic* sitting on top of bucket 1's infrastructure.
3. **"The watchdogs"** — `gtt_monitor.py`, `position_guard/`, `duplicate_order_monitor.py`, `kite_api_monitor.py`, `trade_journal*.py` — nothing here places trades; they observe, log, and flag.

That three-bucket framing is the fastest way to get someone oriented before diving into any single file.

### 10.2 CI/CD — `.github/workflows/`
The folder exists in the repo (confirmed from the file tree) but its exact job definitions weren't retrievable through available tools, so **don't state specific CI steps as fact**. What can be said with confidence from the surrounding files: the presence of `.pre-commit-config.yaml`, a `tests/` directory built for `pytest`, and `pyproject.toml` packaging strongly suggests a standard Python CI shape (lint/format check → `pytest tests/` → possibly a package build step) triggered on push/PR — but confirm the actual workflow file before teaching it as fact.

### 10.3 The runtime architecture, explained as a story

This is the same flow as Section 1, but narrated as a sequence rather than a diagram — better for walking someone through it live.

**Step 1 — First run.** You clone the repo, `pip install -r requirements.txt`, run `python flask_app.py`. Nothing works yet because there's no `configfile.ini`. Flask detects this and serves the **setup wizard** instead of the dashboard. You paste in your Kite Connect API key/secret and choose a dashboard login. On submit, the wizard writes `configfile.ini` and restarts the *same process* in place via `os.execv` — not a fresh process spawn, so if you're running this under systemd, it doesn't even register as a restart event.

**Step 2 — Every trading morning.** Zerodha invalidates access tokens daily around 6 AM IST, so the first thing every day is: open the dashboard, click **Connect Kite**, complete the Kite Connect OAuth flow. The resulting `access_token` is cached for the rest of the day — restarts within the same day don't need a fresh login. At 9 AM IST, `instrument_cache.sync_instruments()` runs automatically, refreshing the symbol→token→lot-size table into a staging SQLite table and atomically swapping it live. This matters because a strategy launched with a stale token/lot-size would compute the wrong quantity or fail to resolve a symbol entirely.

**Step 3 — Launching a strategy.** Say you start Wave Extractor on NIFTY. The dashboard spawns `ticker_single_scraper_new.py` as its own **OS process**, records its **PID**, and will refuse to spawn a second one for the same symbol. That process immediately places its first bracket (BUY/SELL pair) via `common_lib.place_order()`.

**Step 4 — An order's life.** `place_order()` wraps the Kite API call in `retry_with_backoff()` (so a transient network blip doesn't silently drop an order attempt), checks the `[safety] live_trading` flag (dry-run vs. real), and on success calls `save_executed_order()` to persist the fill to a JSON state file. From here there are *two* paths watching for the order to complete: the WebSocket `on_order_update()` callback (fast, but occasionally arrives out of order — a `COMPLETE` can beat the `OPEN` event), and a 180-second polling loop that is the actual source of truth the strategy trusts. This dual-path design exists specifically because the author hit real bugs trusting the WebSocket alone.

**Step 5 — The strategy reacts to its own fill.** For Wave Extractor: cancel the sibling order, recompute net position, consult `multiplier_scale` to widen the losing side's next gap, wait out the cool-off, check the account's portfolio delta gate, place the next bracket. For Survivor: check if price has moved another `n`-point increment past the trigger level; if so, sell the next PE/CE leg (or, in the hedged variant, decide whether to close an old leg or hedge the new one).

**Step 6 — Meanwhile, the watchdogs.** `gtt_monitor.py` is independently checking any GTT orders in flight (including ones placed by Early Exit pre-market) for triggers or staleness. `position_guard/` is scanning the whole account for exposure nobody has explicitly reviewed. `trade_journal.py` is FIFO-pairing every buy/sell as it completes, attributing P&L back to whichever algo generated it, and periodically reconciling against Zerodha's own tradebook via `tradebook_analyzer.py`. None of these place or modify trades — they only observe and, where relevant (GTT monitor), manage the lifecycle of orders another module created.

**Step 7 — You, watching the dashboard.** The Flask UI is polling `positions_lib.py` (which calls `KiteConnect.positions()`) and running each position through `greeks_lib.py` to show live delta/theta. This is a *read path* — it doesn't feed back into any strategy's decisions directly; the strategies compute their own view of what they need (Survivor's delta rebalancer is the one exception, since it deliberately reads current positions to decide its next adjustment).

### 10.4 A one-paragraph summary, for the elevator-pitch version

> It's a single Flask app that's really two things stitched together: a dashboard for *watching* your Zerodha options book (positions, Greeks, P&L), and a set of small, independent trading bots — Survivor, Wave Extractor, Expiry Trade, Early Exit, Covered Calls — that each run as their own OS process, all funneling every single order through one shared, retry-safe order-placement function (`common_lib.place_order()`), all gated by one dry-run switch, and all watched over by a separate layer of monitors (GTT, duplicate-order, position-exposure, P&L journal) that never place a trade themselves — they just make sure nothing silently goes wrong.

### 10.5 Suggested teaching order

If you're presenting this live, this sequencing tends to land better than going file-by-file:

1. Start with the **elevator pitch** (10.4) so everyone has the shape before the detail.
2. Walk the **three-bucket framing** (10.1) — brain/memory, traders, watchdogs.
3. Trace **one order's full life** (Step 4 above) — this is the single most important flow in the whole system, since every strategy funnels through it.
4. Pick **one strategy** to go deep on (Wave Extractor's worked example in Section 3 is the most concrete and visual — a literal price-and-order walkthrough).
5. End with the **dry-run/live gate** — it's the one concept that matters most if anyone in the room might actually run this.
