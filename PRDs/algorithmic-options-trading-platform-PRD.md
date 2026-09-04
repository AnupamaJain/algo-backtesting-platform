# Product Requirements Document
## Algorithmic Options Trading Platform (formalized spec, based on `ui-trading-system`)

| | |
|---|---|
| **Status** | Draft — reverse-engineered from existing open-source implementation |
| **Source system** |  |
| **Owner** | _(assign)_ |
| **Last updated** | _(fill in)_ |
| **Audience** | Engineering, risk/compliance reviewer, anyone picking up or re-platforming this system |

> **How to read this PRD:** Sections marked **[Confirmed]** describe behavior verified against the author's own architecture write-up and strategy deep-dives. Sections marked **[Partial]** are functionally scoped from a one-line description only — treat their acceptance criteria as a starting draft, not a spec to build against blind. Sections marked **[Proposed]** are requirements this document is introducing that don't exist yet in the source system (gaps worth closing). This distinction is preserved throughout rather than presented as uniform fact.

---

## 1. Problem statement

Retail options traders on Indian index derivatives (NIFTY, SENSEX) who run systematic strategies today either:
- trade manually and can't react fast enough to intraday chop or breakout moves, or
- rely on third-party algo platforms with limited strategy customization, opaque risk logic, and per-strategy subscription costs.

This system exists to let a single operator run **multiple independent, complementary trading strategies** against their own broker account, with a shared, auditable order-execution and risk-observation layer underneath — without paying for or trusting a black-box platform.

## 2. Goals

| Goal | Metric |
|---|---|
| Run 2+ uncorrelated strategies safely on one account | Both can run concurrently without one strategy's order flow corrupting the other's state |
| Make "no live trade fires without explicit intent" structurally true | Dry-run is the default state on every fresh install; flipping to live is a deliberate, logged action |
| Survive process crashes without losing track of positions | A strategy process restarting mid-session reconstructs its open-order state from disk, not from memory |
| Give the operator continuous visibility into real risk | Live delta/theta and P&L are visible on the dashboard without polling the broker manually |
| Make adding a new strategy cheap | A new strategy should mostly need "when do I call `place_order()`", not new infrastructure |

### Non-goals
- This is **not** a multi-tenant SaaS product — it is single-operator, single-broker-account software.
- This is **not** a backtesting or research platform — no historical simulation engine is in scope.
- This is **not** a guaranteed-profit system — strategies substitute structural risk controls for hard stop losses by design (see §6), and that tradeoff is treated as intentional, not a defect to silently fix.

## 3. Users & personas

| Persona | Description | Primary needs |
|---|---|---|
| **Operator / strategy author** | Technically fluent trader running their own capital | Configure strategy parameters, monitor live risk, kill-switch a runaway process |
| **Reviewer** | Second pair of eyes checking exposure before/after market hours | Read-only visibility into positions, unreviewed exposure, and journal history |
| **Maintainer / contributor** | Engineer extending the codebase with a new strategy | Clear extension points, shared infra they don't need to reinvent |

## 4. System overview **[Confirmed]**

A self-hosted Flask web application that (a) authenticates against Zerodha's Kite Connect API, (b) runs a set of independent trading-strategy processes, and (c) surfaces live positions, Greeks, and P&L on a dashboard. All strategies funnel order placement through one shared, retry-safe engine.

```
                    ┌─────────────────────┐
                    │   Kite (broker API, dhan, flattrade) Connect API   │  (broker)
                    └──────────┬──────────┘
                               │
      ┌────────────────────────┼────────────────────────┐
      │                flask_app.py (dashboard)          │
      │   auth · positions view · Greeks · process mgmt  │
      └───┬────────────────────┬────────────────────┬────┘
          │                    │                    │
   ┌──────▼──────┐    ┌────────▼────────┐   ┌───────▼───────┐
   │  Strategies │    │  Shared engine   │   │   Watchdogs    │
   │ (own procs) │◄──►│ common_lib.py    │◄─►│ gtt_monitor,   │
   │ Survivor,   │    │ positions_lib.py │   │ position_guard,│
   │ Wave, ...   │    │ instrument_cache │   │ trade_journal  │
   └─────────────┘    │ greeks_lib.py    │   └───────────────┘
                       └──────────────────┘
```

## 5. Functional requirements — shared platform **[Confirmed]**

### 5.1 Authentication
- **FR-1.1**: System must authenticate the operator to the local dashboard via a Flask gatekeeper login, independent of broker auth.
- **FR-1.2**: System must complete a Kite Connect OAuth flow to obtain a broker `access_token`, and persist it for the trading day.
- **FR-1.3**: Access tokens expire daily (broker-side, ~6 AM IST); the system must detect an expired/missing token and route the operator back through re-auth rather than silently failing order calls.
- **FR-1.4**: Restart recovery — if the dashboard process restarts within the same trading day, it must recover the cached token from `instruments.db` rather than forcing re-login.

### 5.2 Order placement (shared by every strategy)
- **FR-2.1**: All order placement, regardless of which strategy initiates it, must go through a single shared function (`common_lib.place_order()`), not duplicated per-strategy logic.
- **FR-2.2**: Every broker API call within order placement must be wrapped in retry-with-backoff, so a transient network failure does not silently drop an order attempt.
- **FR-2.3**: A single configuration flag (`[safety] live_trading`) must gate whether an order actually reaches the broker. When `false`:
  - the full order (symbol, side, qty, price, strategy origin) must still be logged as if it were sent,
  - the function must return a defined sentinel value (`-1`) rather than a fabricated order ID, so strategy logic has a clean "no fill" code path.
- **FR-2.4**: `live_trading` must default to `false` on every fresh install.
- **FR-2.5**: Every successful (or dry-run-logged) order must be persisted to disk (`save_executed_order()`) before the placing function returns, and reloadable on restart (`load_todays_orders()`).
- **FR-2.6**: Order-state updates must be tracked via two independent paths: a WebSocket callback (`on_order_update()`) for low-latency UI updates, and a polling loop (interval: 180s) that is the **authoritative** source of truth for strategy decision-making.
  - **Acceptance criterion**: a `COMPLETE` WebSocket callback arriving before its corresponding `OPEN` callback (observed with GTT-triggered orders) must not cause the strategy to lose track of the fill — the poll loop must reconcile it correctly.

### 5.3 Instrument & lot-size resolution
- **FR-3.1**: Symbol → instrument token → lot size must be resolved through a central cache (`instrument_cache.py`), never hardcoded per strategy.
- **FR-3.2**: The cache must refresh automatically once daily (9 AM IST) via a full re-sync into a staging table, then an atomic swap into the live table.
- **FR-3.3**: A manual "Sync Instruments" trigger must be available on the dashboard, and must not be able to race the automatic daily sync against the same staging table (mutex-guarded).

### 5.4 Position & risk visibility
- **FR-4.1**: Dashboard must display live positions pulled from `KiteConnect.positions()`.
- **FR-4.2**: Each position must display computed Greeks (at minimum delta, theta), via a pricing-model facade (`greeks_lib.py`) that can be backed by an embedded Black-Scholes implementation or a faster external library (config-selected).

### 5.5 Process management
- **FR-5.1**: Continuously-running strategies (Wave Extractor, Survivor) must run as independent OS processes, not threads inside the dashboard's process.
- **FR-5.2**: The dashboard must track each spawned strategy process by PID.
- **FR-5.3**: The dashboard must refuse to spawn a second instance of a strategy against a symbol that already has one running.
- **FR-5.4**: Each strategy process must write its own log file, independent of the dashboard's log.

### 5.6 Notifications **[Confirmed]**
- **FR-6.1**: Order events must be able to trigger a Telegram notification.
- **FR-6.2**: Order events must be able to trigger a browser Web Push notification.
- **FR-6.3**: A new strategy calling the shared order-placement path must receive notification support automatically, without additional wiring.

---

## 6. Functional requirements — strategies

### 6.1 Survivor (breakout hedge) **[Confirmed]**

**Purpose:** Defend an existing option book when price breaks out and trends, rather than mean-reverting.

| ID | Requirement |
|---|---|
| FR-S1 | Operator must be able to configure a support level and a resistance level per run, per instrument (NIFTY, SENSEX, or a single stock). |
| FR-S2 | On a confirmed break of resistance, followed by a further `n`-point move (configurable "gap"), the system must sell a PE at a strike `y` points OTM (configurable "strike distance"). |
| FR-S3 | Symmetric behavior on a confirmed break of support: sell a CE at `y` points OTM after an `n`-point continuation move. |
| FR-S4 | Each further `n`-point continuation in the same direction must trigger another leg at a re-centered strike, maintaining the `y`-point OTM distance. |
| FR-S5 | Before selling, the system must check the option's premium against a configurable floor (`min_price_to_sell`); if below floor, it must walk the strike inward until premium clears the floor. |
| FR-S6 | Each side's trigger level must reset (re-anchor closer to current price) if price reverses by a configurable `reset_gap`, enabling faster re-engagement on the next leg of the same direction without waiting for a full retrace. |
| FR-S7 | Position size per trigger must be a fixed, configurable quantity — not dynamically scaled by volatility or capital. |
| FR-S8 | **No system-enforced stop loss or target.** Exit decisions are explicitly out of automated scope; documented operator guidance (30–50% loss threshold on ITM legs, take-profit below ₹20 premium and >50% profit) exists as a manual playbook, not code. |
| FR-S9 | A hedged variant must exist where, before adding a new sell leg, the system evaluates closing a decayed existing leg (lock profit, free margin) vs. buying a cheaper option against the new sell (form a spread, reduce margin requirement) — and selects automatically based on which is more capital-efficient at that moment. |
| FR-S10 | A separate delta-rebalancing job (`survivor_delta_rebalance.py`) must be able to read the current book, compute net portfolio delta, and adjust/rebalance legs toward a target band — independent of new-trigger logic. |
| FR-S11 | Strategy is intended for operator-scheduled use on a subset of the trading week (documented pattern: Mon–Wed), not as an always-on process — the system should not force a different schedule, but should not assume 24/7 operation either. |

### 6.2 Wave Extractor (range/theta-positive bracket scalper) **[Confirmed]**

**Purpose:** Harvest intraday chop by continuously selling into small moves and buying back on reversion.

| ID | Requirement |
|---|---|
| FR-W1 | On launch, the system must place a resting bracket of two orders around current price: BUY at `price − gap`, SELL at `price + gap` (configurable gap). |
| FR-W2 | On either side filling, the sibling order must be cancelled immediately (no naked resting order left behind). |
| FR-W3 | System must track net position (cumulative buys − sells) continuously. |
| FR-W4 | A configurable multiplier (`multiplier_scale`) must widen the gap on the side that would increase directional exposure: net-short widens the buy gap; net-long widens the sell gap. Size of each leg is not affected — only gap distance. |
| FR-W5 | A configurable cool-off period (documented default ~5s) must elapse after any fill before the next bracket is placed. |
| FR-W6 | The bracket-placement loop must only fire when zero orders are currently pending for that symbol (no order stacking). |
| FR-W7 | Before placing a new bracket leg, the system must check portfolio-level option delta (`check_changes_in_restrictions`) and suppress the leg if it would push the account beyond a configured directional-exposure limit — this check must be able to override local bracket logic. |
| FR-W8 | Position size per leg must be a fixed, configurable quantity, independently settable for BUY vs. SELL side. |
| FR-W9 | **No system-enforced stop loss or target.** Risk is bounded structurally (gap widening + delta gate), by design — not a gap to be filled with a bolt-on stop order without re-evaluating the strategy's core premise. |
| FR-W10 | Must run as a continuous, always-on process for the duration of the trading session (not a scheduled/periodic job). |

### 6.3 Expiry Trade **[Partial]**
| ID | Requirement | Confidence |
|---|---|---|
| FR-E1 | Strategy must generate entry/exit signals from Stochastic RSI computed on 3-minute candles. | Confirmed (naming + description only) |
| FR-E2 | Strategy must be scoped to same-day expiry instruments. | Confirmed (implied by name) |
| FR-E3 | Entry/exit thresholds, position sizing, stop loss, and target | **Undocumented** — must be extracted from `expiry_trade_lib.py` directly or specified fresh before this can be built/rebuilt to spec |

### 6.4 Early Exit **[Partial → mostly Confirmed by inference]**
| ID | Requirement |
|---|---|
| FR-EE1 | Pre-market (before session open), system must compute a theoretical fair value for open NIFTY/SENSEX option positions, using the shared Greeks/pricing facade. |
| FR-EE2 | System must place a GTT (Good-Till-Triggered) exit order at that fair-value level for each eligible position. |
| FR-EE3 | This module is an exit-management layer over positions opened by any strategy or manual trade — it must not require the position to have been opened by a specific strategy. |
| FR-EE4 | GTTs placed by this module must be visible to and manageable by the GTT Monitor watchdog (§6.6). |

### 6.5 Covered Calls **[Partial]**
| ID | Requirement | Confidence |
|---|---|---|
| FR-C1 | Strategy must sell OTM calls against an existing equity holding. | Confirmed (description only) |
| FR-C2 | Strike-selection distance, roll timing/triggers, assignment handling | **Undocumented** — needs direct source review or fresh specification |

### 6.6 Watchdog / operations modules **[Confirmed]**
| ID | Requirement |
|---|---|
| FR-O1 | GTT Monitor must watch all GTT orders (from any source), detect duplicates, and suppress stale GTTs when the market is closed. |
| FR-O2 | Position Guard must flag any symbol with exposure (open position, open order, or pending GTT) that hasn't been explicitly reviewed by the operator — read-only flag, not an automated close. |
| FR-O3 | Duplicate Order Monitor must independently guard against the same order firing twice, separate from process-level PID de-duplication (§5.5). |
| FR-O4 | Trade Journal must FIFO-pair every buy/sell, attribute realized P&L to the originating strategy, and periodically reconcile against the broker's own tradebook. |
| FR-O5 | Kite API Monitor must log every broker API call with caller identity, latency, and error state, for operational debugging — not a risk control. |

---

## 7. Non-functional requirements

| Category | Requirement |
|---|---|
| **Safety** | Dry-run must be the default and must require an explicit, restart-triggering config change to disable. |
| **Resilience** | Any strategy process crashing and restarting mid-session must not lose track of orders placed earlier that session. |
| **Data integrity** | Instrument cache swaps must be atomic; a reader must never observe a partially-written table. |
| **Concurrency safety** | A manual sync action and a scheduled sync must not be able to corrupt shared state (must be mutex-guarded). |
| **Auditability** | Every order — dry-run or live — must be logged with enough detail to reconstruct what would have/did happen. |
| **Extensibility** | Adding a new strategy should require, at minimum: symbol/instrument resolution (existing), order placement (existing), and new decision logic only — not new persistence, retry, or notification infrastructure. |
| **Observability** | Operator must be able to see, without leaving the dashboard, current positions, live Greeks, and any unreviewed exposure flagged by Position Guard. |

## 8. Data model (informal) **[Confirmed, high-level]**

| Entity | Store | Notes |
|---|---|---|
| Instruments (symbol, token, lot size) | SQLite, staged + swapped daily | Source of truth for all sizing math |
| Today's executed/pending orders | JSON file per process (or shared, TBD by implementation) | Reload path on process restart |
| Trade journal (FIFO pairs, P&L per algo) | SQLite / DB (`trade_journal_db.py`) | Reconciled against broker tradebook |
| Survivor trigger events | Separate event log (`survivor_events_db.py`) | Strategy-specific, not generic journal |
| Config | `configfile.ini` (git-ignored; `.example` template tracked) | Holds API keys, `live_trading` flag |

## 9. Out of scope (explicitly)
- Historical backtesting / simulation engine
- Multi-broker support (Kite Connect only, currently)
- Multi-account / multi-tenant operation
- Mobile-native app (dashboard is web-only)
- Automated compliance/regulatory filing

## 10. Open questions / risks

| # | Question | Why it matters |
|---|---|---|
| 1 | What are Expiry Trade's actual entry/exit thresholds? | Cannot be specified, tested, or safely modified without this |
| 2 | What are Covered Calls' strike/roll rules? | Same as above |
| 3 | Should a system-level (not strategy-level) hard kill-switch exist, independent of each strategy's own logic? | Currently, "no stop loss" is a *strategy* design choice; there's no evidence of an account-wide circuit breaker beyond Wave's delta gate |
| 4 | Where does regulatory responsibility for automated order placement (SEBI algo rules) get enforced — is that a product requirement or purely an operator responsibility? | Currently entirely disclaimed to the operator; worth an explicit decision if this were productized further |
| 5 | Is `docs/` (the hosted documentation site) the actual source of truth that should supersede this PRD? | This PRD was built without direct access to that site or to raw source files — should be reconciled against it |

## 11. Success metrics (proposed, since none are defined in source material) **[Proposed]**
- Zero incidents of a strategy process losing open-order state after a crash/restart, over a full trading month.
- Zero duplicate-order incidents caught in production that weren't caught by Duplicate Order Monitor first.
- 100% of live-mode order activations are traceable to an explicit, timestamped config change (no silent flips).
- Time to add and deploy a new strategy skeleton (using existing shared infra): target under 1 day for an engineer familiar with the codebase.

## 12. Appendix — glossary
- **GTT**: Good-Till-Triggered order type (Zerodha-specific) — a conditional order that fires when a trigger price is hit.
- **Theta-positive**: A position or strategy that benefits from time decay (selling premium rather than buying it).
- **Delta**: Sensitivity of an option's price to a ₹1 move in the underlying; used here as the primary portfolio-level directional-risk gauge.
- **Dry-run**: Mode in which strategy logic executes fully but no order reaches the broker.
