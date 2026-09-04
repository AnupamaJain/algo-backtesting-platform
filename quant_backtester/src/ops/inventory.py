"""Inventory of every strategy and operational module in this repository.

The repo holds two distinct systems that had no single view over them:

  * **Production** — the Indian options/equity trading modules described in
    `algorithmic-options-trading-platform-PRD.md`: Survivor, Wave Extractor,
    Expiry Trade, Early Exit, Covered Calls, plus the watchdogs that guard
    them. Written against a vendor SDK originally, they now reach whichever
    broker is configured through src/broker/legacy.py — one seam, no rewrite.
  * **Research** — the six naked strategies the backtester sweeps.

The console previously listed only the research six, which made the platform
look far emptier than it is.

Every entry is *declared* here but *verified* against disk at read time. A
module that has been renamed or deleted shows as missing rather than being
silently rendered as if it existed — a strategy list that lies about what is
installed is worse than no list.
"""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Production modules live at the repo root; the backtester lives one level in.
REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class FileStateSource:
    """A directory of run artifacts, for modules that persist files not rows.

    The Survivor strategies write `survivor_state_<pid>.json` per process.
    Without this the inventory had no state source for them at all, so
    `has_activity` was False by default — and the console reported "never
    run" as though it had checked, when it had not looked anywhere.
    """

    directory: str               # relative to repo root
    pattern: str = "*.json"

    def read(self) -> dict:
        path = REPO_ROOT.parent / self.directory
        if not path.is_dir():
            return {"available": False, "rows": 0, "last_activity": None}
        files = sorted(path.glob(self.pattern))
        if not files:
            return {"available": True, "rows": 0, "last_activity": None}
        newest = max(f.stat().st_mtime for f in files)
        return {
            "available": True,
            "rows": len(files),
            "last_activity": datetime.fromtimestamp(newest).isoformat(timespec="seconds"),
        }


@dataclass(frozen=True)
class HeartbeatStateSource:
    """A job inside watchdog_runner.py's shared heartbeat file.

    The watchdogs run as three imported functions inside one consolidated
    process rather than as `python <module>.py` each, specifically so
    starting them does not require booting flask_app.py (which also
    registers an order-placing strategy blueprint). Process-table matching
    on the module's own filename therefore never finds them running, and
    their DB tables record VIOLATIONS FOUND, not "the check ran" — an
    all-clear check leaves both looking exactly like "never run". This
    reads the runner's heartbeat instead, which is written every pass
    regardless of whether anything was flagged.
    """

    job_key: str
    file: str = "watchdog_heartbeat.json"

    def read(self) -> dict:
        path = REPO_ROOT / "quant_backtester" / "state" / self.file
        if not path.exists():
            return {"available": False, "rows": 0, "last_activity": None}
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            return {"available": False, "rows": 0, "last_activity": None}
        job = (data.get("jobs") or {}).get(self.job_key)
        if job is None:
            return {"available": True, "rows": 0, "last_activity": None}
        return {
            "available": True,
            "rows": 1 if job.get("ok") else 0,
            "last_activity": data.get("last_pass"),
        }


@dataclass(frozen=True)
class StateSource:
    """A SQLite table that reflects a module's runtime activity."""

    db: str                      # path relative to repo root
    table: str
    timestamp_column: str | None = None
    # Restricts which rows count as evidence of THIS module's activity, not
    # just any row in a table other modules also write to. Used by
    # survivor_delta_rebalance: survivor_events rows come from every Survivor
    # process, but only rows with a delta_at_*_anchor value were actually a
    # rebalance decision, not a routine PE/CE trigger.
    where: str | None = None

    def read(self) -> dict:
        path = REPO_ROOT / self.db
        if not path.exists():
            return {"available": False, "rows": 0, "last_activity": None}
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                clause = f" WHERE {self.where}" if self.where else ""
                rows = conn.execute(
                    f"SELECT COUNT(*) FROM {self.table}{clause}"
                ).fetchone()[0]
                last = None
                if self.timestamp_column and rows:
                    last = conn.execute(
                        f"SELECT MAX({self.timestamp_column}) FROM {self.table}{clause}"
                    ).fetchone()[0]
                return {"available": True, "rows": int(rows), "last_activity": last}
            finally:
                conn.close()
        except sqlite3.Error:
            # A table that does not exist yet is normal on a fresh install.
            return {"available": False, "rows": 0, "last_activity": None}


@dataclass(frozen=True)
class ModuleDef:
    """One strategy or operational module."""

    key: str
    name: str
    kind: str                    # production | research
    category: str                # strategy | watchdog | analytics
    family: str                  # mean_reversion | trend | premium_selling | ...
    market: str                  # IN | US
    summary: str
    detail: str = ""
    modules: tuple[str, ...] = ()          # paths relative to repo root
    entry: str = ""                        # script or class that starts it
    parameters: tuple[str, ...] = ()
    state: StateSource | None = None
    requires: tuple[str, ...] = ()         # external dependencies, e.g. kite
    dashboard: str = ""                    # legacy Flask route, if any
    prd_ref: str = ""

    # -- verified facts ---------------------------------------------------

    def installed(self) -> bool:
        return all((REPO_ROOT / m).exists() for m in self.modules)

    def missing_modules(self) -> list[str]:
        return [m for m in self.modules if not (REPO_ROOT / m).exists()]

    def size_lines(self) -> int:
        total = 0
        for module in self.modules:
            path = REPO_ROOT / module
            if path.is_file():
                try:
                    total += sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
            elif path.is_dir():
                for py in path.rglob("*.py"):
                    try:
                        total += sum(1 for _ in py.open("r", encoding="utf-8", errors="ignore"))
                    except OSError:
                        continue
        return total

    def _evidence(self, state, backtests) -> str:
        """Human-readable account of where activity was looked for."""
        if backtests is not None:
            return (
                f"{backtests['configurations']:,} walk-forward configurations "
                f"across {backtests['symbols']} symbols"
            )
        if self.state is None:
            return "no state source declared for this module"
        if isinstance(self.state, HeartbeatStateSource):
            if state and state.get("last_activity"):
                return f"watchdog_runner heartbeat: last pass {state['last_activity']}"
            return "watchdog_runner has not reported a pass for this job"
        where = getattr(self.state, "db", None) or getattr(self.state, "directory", "?")
        what = getattr(self.state, "table", None) or getattr(self.state, "pattern", "")
        if state and not state.get("available"):
            return f"{where} does not exist"
        rows = (state or {}).get("rows", 0)
        return f"{where} -> {what}: {rows} rows"

    def to_dict(self, running: set[str]) -> dict:
        state = self.state.read() if self.state else None
        # A library strategy has no state file of its own — its activity is
        # the backtests it has been through. Without this, every registered
        # strategy reported "Idle" (i.e. never run) while sitting on hundreds
        # of walk-forward results.
        backtests = _backtest_activity().get(self.entry) if self.kind == "research" else None
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "category": self.category,
            "family": self.family,
            "market": self.market,
            "summary": self.summary,
            "detail": self.detail,
            "modules": list(self.modules),
            "entry": self.entry,
            "parameters": list(self.parameters),
            "requires": list(self.requires),
            "dashboard": self.dashboard,
            "prd_ref": self.prd_ref,
            "installed": self.installed(),
            "missing_modules": self.missing_modules(),
            "lines": self.size_lines(),
            "running": any(self.entry and self.entry in proc for proc in running),
            "state": state,
            "backtests": backtests,
            # "never run" is a real, useful distinction from "broken".
            "has_activity": bool(state and state.get("rows")) or bool(backtests),
            # Whether anything was actually inspected. Without this the
            # console reported "never run" for modules whose state location
            # was simply never declared — asserting a fact it had not
            # checked, which is worse than admitting ignorance.
            "activity_checked": bool(state is not None or backtests is not None),
            # What was inspected to reach that conclusion. A status claim the
            # reader cannot audit invites exactly the question "why does it
            # say that?" — so the evidence travels with the claim.
            "activity_evidence": self._evidence(state, backtests),
        }


# ==========================================================================
# PRODUCTION — Indian options / equity.
#
# These reach whichever broker config/broker.yaml selects, through the
# BrokerAdapter abstraction. Legacy call sites go via src/broker/legacy.py.
# ==========================================================================

SURVIVOR_DETAIL = (
    "Defends an existing option book when price breaks out and trends rather "
    "than mean-reverting. On a confirmed break of support or resistance plus a "
    "further n-point move, it sells an option n points OTM, re-centring the "
    "strike on each further continuation. Deliberately has no system-enforced "
    "stop loss: risk is bounded structurally, and exits are a documented "
    "operator playbook rather than code."
)

PRODUCTION: tuple[ModuleDef, ...] = (
    ModuleDef(
        key="survivor_nifty",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — NIFTY",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="Breakout hedge on NIFTY: sells OTM options as price trends away.",
        detail=SURVIVOR_DETAIL,
        modules=("place_order_at_nifty.py",),
        entry="place_order_at_nifty.py",
        parameters=("support", "resistance", "gap", "strike_distance", "min_price_to_sell", "reset_gap", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S1…S8",
    ),
    ModuleDef(
        key="survivor_nifty_hedged",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — NIFTY (hedged)",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="Survivor with automatic margin optimisation before each new leg.",
        detail=(
            "Before adding a sell leg it compares closing a decayed existing leg "
            "against buying a cheaper option to form a spread, and picks whichever "
            "is more capital-efficient at that moment."
        ),
        modules=("place_order_at_nifty_with_buy_auto.py",),
        entry="place_order_at_nifty_with_buy_auto.py",
        parameters=("support", "resistance", "gap", "strike_distance", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S9",
    ),
    ModuleDef(
        key="survivor_sensex",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — SENSEX",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="Breakout hedge on SENSEX.",
        detail=SURVIVOR_DETAIL,
        modules=("place_order_at_sensex.py",),
        entry="place_order_at_sensex.py",
        parameters=("support", "resistance", "gap", "strike_distance", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S1…S8",
    ),
    ModuleDef(
        key="survivor_sensex_hedged",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — SENSEX (hedged)",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="SENSEX breakout hedge with margin optimisation.",
        modules=("place_order_at_sensex_with_buy_auto.py",),
        entry="place_order_at_sensex_with_buy_auto.py",
        parameters=("support", "resistance", "gap", "strike_distance", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S9",
    ),
    ModuleDef(
        key="survivor_stock",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — Single stock",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="Breakout hedge on an individual F&O stock.",
        modules=("place_order_at_stock.py",),
        entry="place_order_at_stock.py",
        parameters=("symbol", "support", "resistance", "gap", "strike_distance", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S1…S8",
    ),
    ModuleDef(
        key="survivor_stock_hedged",
        state=FileStateSource("survivor_status", "survivor_state_*.json"),
        name="Survivor — Single stock (hedged)",
        kind="production",
        category="strategy",
        family="premium_selling",
        market="IN",
        summary="Single-stock breakout hedge with margin optimisation.",
        modules=("place_order_at_stock_with_buy_auto.py",),
        entry="place_order_at_stock_with_buy_auto.py",
        parameters=("symbol", "support", "resistance", "gap", "strike_distance", "quantity"),
        requires=("broker",),
        dashboard="/survivor",
        prd_ref="FR-S9",
    ),
    ModuleDef(
        key="wave_extractor",
        # ticker_single_scraper_new.py writes status_<symbol>_<pid>.json to
        # the repo-parent status/ dir via write_status_to_file() -- without
        # this, has_activity defaulted to False and the console reported
        # "Not tracked" regardless of whether the scraper had ever run,
        # the same gap FileStateSource already closed for the Survivor
        # strategies above.
        state=FileStateSource("status", "status_*_*.json"),
        name="Wave Extractor",
        kind="production",
        category="strategy",
        family="mean_reversion",
        market="IN",
        summary="Theta-positive bracket scalper that harvests intraday chop.",
        detail=(
            "Rests a BUY at price−gap and a SELL at price+gap. When either side "
            "fills the sibling is cancelled immediately, so no naked resting "
            "order is ever left behind. A configurable multiplier widens the gap "
            "on whichever side would increase directional exposure, and a "
            "portfolio-level delta check can suppress a leg entirely — that gate "
            "overrides the local bracket logic."
        ),
        modules=("ticker_single_scraper_new.py", "common_lib.py"),
        entry="ticker_single_scraper_new.py",
        parameters=("symbol", "gap", "multiplier_scale", "buy_quantity", "sell_quantity", "cool_off_time"),
        requires=("broker",),
        dashboard="/wave_extractor",
        prd_ref="FR-W1…W10",
    ),
    ModuleDef(
        key="expiry_trade",
        name="Expiry Trade",
        kind="production",
        category="strategy",
        family="mean_reversion",
        market="IN",
        summary="Stochastic-RSI signals on 3-minute candles, scoped to same-day expiry.",
        detail=(
            "Generates entries and exits from Stochastic RSI computed on "
            "3-minute candles for same-day-expiry instruments."
        ),
        modules=("expiry_trade_lib.py",),
        entry="expiry_trade_lib.py",
        parameters=("symbol", "stoch_rsi_period", "oversold", "overbought", "quantity"),
        requires=("broker",),
        dashboard="/expiry_trade",
        prd_ref="FR-E1…E3",
    ),
    ModuleDef(
        key="early_exit_nifty",
        name="Early Exit — NIFTY",
        kind="production",
        category="strategy",
        family="exit_management",
        market="IN",
        summary="Pre-market GTT exits placed at each position's theoretical fair value.",
        detail=(
            "Before the session opens it computes a theoretical fair value for "
            "open index option positions using the shared Greeks facade, then "
            "places a GTT exit at that level. It manages positions opened by any "
            "strategy or by hand — it does not require the position to have come "
            "from a particular source."
        ),
        modules=("early_exit_lib.py",),
        entry="early_exit_lib.py",
        parameters=("gtt_trigger_buffer_pct", "volatility", "interest_rate"),
        requires=("broker",),
        dashboard="/early_exit",
        prd_ref="FR-EE1…EE4",
    ),
    ModuleDef(
        key="early_exit_sensex",
        name="Early Exit — SENSEX",
        kind="production",
        category="strategy",
        family="exit_management",
        market="IN",
        summary="Fair-value GTT exits for SENSEX option positions.",
        modules=("early_exit_sensex_lib.py",),
        entry="early_exit_sensex_lib.py",
        parameters=("gtt_trigger_buffer_pct", "volatility", "interest_rate"),
        requires=("broker",),
        dashboard="/early_exit_sensex",
        prd_ref="FR-EE1…EE4",
    ),
    ModuleDef(
        key="covered_calls",
        name="Covered Calls",
        kind="production",
        category="strategy",
        family="income",
        market="IN",
        summary="Sells OTM calls against existing equity holdings.",
        modules=("covered_calls",),
        entry="covered_calls_lib.py",
        parameters=("strike_distance", "min_premium", "roll_trigger"),
        state=StateSource("covered_calls/pending_orders.db", "pending_orders"),
        requires=("broker",),
        dashboard="/covered_calls",
        prd_ref="FR-C1…C2",
    ),
    ModuleDef(
        key="survivor_delta_rebalance",
        # Not a standalone process -- survivor_delta_rebalance.py has no
        # __main__, just the pricing math (compute_expiry_delta_at_spot,
        # find_spot_for_target_delta) that place_order_at_nifty.py and
        # place_order_at_sensex.py import and call when launched with
        # delta_rebalancing enabled. Its activity shows up in THEIR shared
        # survivor_events.db as CE_ANCHOR_REBALANCE / PE_ANCHOR_REBALANCE
        # rows, which is what this state source actually counts -- not
        # every Survivor event, only the ones that were a rebalance.
        # StateSource resolves `db` as REPO_ROOT / db, with REPO_ROOT the
        # inner repo checkout -- survivor_status/ lives one level above that
        # (same place _write_survivor_state() in place_order_at_nifty.py
        # writes it), hence the leading "..".
        state=StateSource(
            "../survivor_status/survivor_events.db", "survivor_events",
            timestamp_column="event_time",
            where="event_type IN ('CE_ANCHOR_REBALANCE', 'PE_ANCHOR_REBALANCE')",
        ),
        name="Survivor Delta Rebalance",
        kind="production",
        category="strategy",
        family="risk_overlay",
        market="IN",
        summary="Rebalances net portfolio delta toward a target band.",
        detail=(
            "Reads the current book, computes net portfolio delta and adjusts "
            "legs toward the configured band. Runs independently of new-trigger "
            "logic, so it can correct drift without opening fresh positions. Not "
            "a separate process -- this is the delta-rebalancing feature inside "
            "place_order_at_nifty.py/place_order_at_sensex.py, launched with "
            "the delta_rebalancing flag."
        ),
        modules=("survivor_delta_rebalance.py",),
        entry="survivor_delta_rebalance.py",
        parameters=("min_nifty_delta", "max_nifty_delta", "min_bank_nifty_delta", "max_bank_nifty_delta"),
        requires=("broker",),
        prd_ref="FR-S10",
    ),
)


# ==========================================================================
# WATCHDOGS AND ANALYTICS
# ==========================================================================

WATCHDOGS: tuple[ModuleDef, ...] = (
    ModuleDef(
        key="gtt_monitor",
        name="GTT Monitor",
        kind="production",
        category="watchdog",
        family="order_safety",
        market="IN",
        summary="Watches every GTT order, detects duplicates, suppresses stale triggers.",
        detail=(
            "Covers GTTs from any source, not just ones this platform placed, "
            "and suppresses stale triggers while the market is closed."
        ),
        modules=("gtt_monitor.py",),
        entry="gtt_monitor.py",
        state=HeartbeatStateSource("gtt_monitor"),
        requires=("broker",),
        prd_ref="FR-O1",
    ),
    ModuleDef(
        key="position_guard",
        name="Position Guard",
        kind="production",
        category="watchdog",
        family="exposure",
        market="IN",
        summary="Flags any symbol with unreviewed exposure.",
        detail=(
            "Flags open positions, open orders or pending GTTs the operator has "
            "not explicitly reviewed. Read-only by design: it raises a flag, it "
            "never closes anything automatically."
        ),
        modules=("position_guard",),
        entry="position_guard/detector.py",
        state=HeartbeatStateSource("position_guard"),
        requires=("broker",),
        dashboard="/position_guard",
        prd_ref="FR-O2",
    ),
    ModuleDef(
        key="duplicate_order_monitor",
        name="Duplicate Order Monitor",
        kind="production",
        category="watchdog",
        family="order_safety",
        market="IN",
        summary="Independent guard against the same order firing twice.",
        detail=(
            "Deliberately separate from process-level PID de-duplication, so a "
            "single failure mode cannot disable both."
        ),
        modules=("duplicate_order_monitor.py",),
        entry="duplicate_order_monitor.py",
        state=HeartbeatStateSource("duplicate_order_monitor"),
        requires=("broker",),
        dashboard="/duplicate-orders",
        prd_ref="FR-O3",
    ),
    ModuleDef(
        key="trade_journal",
        name="Trade Journal",
        kind="production",
        category="analytics",
        family="reporting",
        market="IN",
        summary="FIFO-pairs every buy and sell, attributing realized P&L per strategy.",
        detail=(
            "Pairs fills FIFO, attributes realized P&L to the originating "
            "strategy, and periodically reconciles against the broker's own "
            "tradebook rather than trusting its own arithmetic."
        ),
        modules=("trade_journal.py", "trade_journal_db.py"),
        entry="trade_journal.py",
        requires=("broker",),
        dashboard="/trade_journal",
        prd_ref="FR-O4",
    ),
    ModuleDef(
        key="kite_api_monitor",
        name="Broker API Monitor",
        kind="production",
        category="analytics",
        family="observability",
        market="IN",
        summary="Logs every broker API call with caller, latency and error state.",
        detail="An operational debugging tool, explicitly not a risk control.",
        modules=("kite_api_monitor.py",),
        entry="kite_api_monitor.py",
        state=StateSource("api_monitor.db", "api_calls"),
        dashboard="/api-monitor",
        prd_ref="FR-O5",
    ),
    ModuleDef(
        key="tradebook_analyzer",
        name="Tradebook Analyzer",
        kind="production",
        category="analytics",
        family="reporting",
        market="IN",
        summary="Post-trade analysis over the broker tradebook.",
        modules=("tradebook_analyzer.py",),
        entry="tradebook_analyzer.py",
        requires=("broker",),
        dashboard="/tradebook-analysis",
    ),
    ModuleDef(
        key="cas_tracker",
        name="NIFTY Contributors (CAS)",
        kind="production",
        category="analytics",
        family="market_structure",
        market="IN",
        summary="Tracks which constituents are driving the index.",
        modules=("cas_tracker",),
        entry="cas_tracker/index_calculator.py",
        # snapshots accumulate only once its poll loop has been running a
        # while; the heartbeat says whether the loop itself is alive, which
        # is available from the first pass rather than after a delay.
        state=HeartbeatStateSource("cas_tracker"),
        dashboard="/nifty-contributors",
    ),
    ModuleDef(
        key="instrument_cache",
        name="Instrument Cache",
        kind="production",
        category="analytics",
        family="reference_data",
        market="IN",
        summary="Symbol → token → lot size, refreshed daily with an atomic swap.",
        detail=(
            "Syncs into a staging table then swaps atomically, so a reader can "
            "never observe a half-written instrument table."
        ),
        modules=("instrument_cache.py",),
        entry="instrument_cache.py",
        state=StateSource("instruments.db", "instruments"),
        dashboard="/admin/instruments",
        prd_ref="FR-3.1…3.3",
    ),
    ModuleDef(
        key="notifications",
        name="Notifications",
        kind="production",
        category="analytics",
        family="alerting",
        market="IN",
        summary="Telegram and browser web-push alerts on order events.",
        detail=(
            "Any strategy using the shared order path gets notifications "
            "automatically, with no extra wiring."
        ),
        modules=("notifications",),
        entry="notifications/service.py",
        # The notifications table only fills when something is actually
        # dispatched — a quiet day with nothing to flag looks identical to
        # the purge job never having run. The heartbeat distinguishes them.
        state=HeartbeatStateSource("notifications_purge"),
        prd_ref="FR-6.1…6.3",
    ),
)


# ==========================================================================
# RESEARCH — the backtester's strategy library
# ==========================================================================

#: One-line descriptions for the research strategies. The LIST comes from the
#: registry, not from here — a strategy added to the registry appears
#: automatically, with a neutral description if none is written yet.
RESEARCH_SUMMARIES: dict[str, str] = {
    "RSIReversion": "Buys when RSI crosses below oversold, flips short above overbought.",
    "BBReversion": "Buys below the lower Bollinger band, flips short above the upper.",
    "KeltnerReversion": "Reversion against an ATR-based envelope.",
    "ZScoreReversion": "Fades statistically extreme deviations from a rolling mean.",
    "WilliamsRReversion": "Williams %R oversold/overbought reversion.",
    "CCIReversion": "Fades CCI extremes away from the typical price.",
    "MACrossover": "Long while the fast average leads the slow one.",
    "MACDTrend": "Long while the MACD line leads its signal line.",
    "SupertrendFollow": "Follows the ATR-banded Supertrend direction.",
    "ADXTrend": "Trades direction only when ADX says the trend is strong enough.",
    "SimpleMomentum": "Long if price is above where it sat N days ago.",
    "ROCMomentum": "Trades trailing return past a dead band, avoiding noise around zero.",
    "DualMomentum": "Requires a short and a long lookback to agree.",
    "TurtleBreakout": "Turtle system: enter on an N-day breakout, exit on a shorter channel.",
    "VolatilityBreakout": "Enters when price travels more than N x ATR from the prior close.",
    "SqueezeBreakout": "Trades the expansion that follows a Bollinger squeeze.",
    "VolatilityRegime": "Holds risk only while realized volatility is subdued.",
    "VolatilityMeanReversion": "Buys after a volatility spike starts to exhaust.",
    "StructureBreak": "Trades market structure: higher highs versus lower lows.",
    "InsideBarBreakout": "An inside bar marks compression; trades the break of its range.",
    "EngulfingReversal": "Engulfing candles against the prevailing trend.",
}


def _research_modules() -> tuple[ModuleDef, ...]:
    """Build the research inventory from the live strategy registry.

    Derived rather than hand-listed: the console previously showed six
    strategies because a separate list drifted from the code.
    """
    from ..strategies import STRATEGY_REGISTRY

    modules = []
    for name, cls in STRATEGY_REGISTRY.items():
        signature = __import__("inspect").signature(cls.__init__)
        params = tuple(p for p in signature.parameters if p != "self")
        modules.append(
            ModuleDef(
                key=_snake_case(name),
                name=_display_name(name),
                kind="research",
                category="strategy",
                family=getattr(cls, "family", "unclassified"),
                market="US",
                summary=RESEARCH_SUMMARIES.get(name, "Registered backtester strategy."),
                modules=("quant_backtester/src/strategies.py",),
                entry=name,
                parameters=params,
            )
        )
    return tuple(modules)


def _backtest_activity() -> dict:
    """How many configurations of each strategy have been walk-forward tested.

    Read from the Layer 2 artifact rather than tracked separately, so the
    console cannot drift from what was actually run. Cached per process: the
    inventory asks once per strategy and the file is the same every time.
    """
    global _BACKTEST_ACTIVITY
    if _BACKTEST_ACTIVITY is not None:
        return _BACKTEST_ACTIVITY

    _BACKTEST_ACTIVITY = {}
    configs = REPO_ROOT / "quant_backtester" / "results" / "layer2" / "all_configurations.csv"
    if not configs.exists():
        return _BACKTEST_ACTIVITY

    try:
        import pandas as pd

        frame = pd.read_csv(configs)
        if "strategy" not in frame.columns:
            return _BACKTEST_ACTIVITY

        survivors: set = set()
        survivor_path = configs.parent / "survivors.csv"
        if survivor_path.exists():
            try:
                surv = pd.read_csv(survivor_path)
                if "strategy" in surv.columns:
                    survivors = set(surv["strategy"])
            except Exception:  # noqa: BLE001 - an empty survivors file is normal
                pass

        for name, group in frame.groupby("strategy"):
            best = group["oos_sharpe"].max() if "oos_sharpe" in group.columns else None
            _BACKTEST_ACTIVITY[str(name)] = {
                "configurations": int(len(group)),
                "symbols": int(group["symbol"].nunique()) if "symbol" in group.columns else 0,
                "best_oos_sharpe": round(float(best), 3) if best is not None and best == best else None,
                "survivors": int(sum(1 for s in survivors if s == name)),
            }
    except Exception as exc:  # noqa: BLE001 - the console must render regardless
        logger.debug("Could not read backtest activity: %s", exc)

    return _BACKTEST_ACTIVITY


_BACKTEST_ACTIVITY: dict | None = None


def _snake_case(name: str) -> str:
    import re as _re

    return _re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _display_name(name: str) -> str:
    """CamelCase to words, keeping acronyms intact.

    A naive split on every capital turns RSIReversion into "R S I Reversion".
    Splitting only at a lower-to-upper boundary, or before the last capital of
    an acronym run, gives "RSI Reversion" and "MACD Trend".
    """
    import re as _re

    return _re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)


RESEARCH: tuple[ModuleDef, ...] = _research_modules()


ALL_MODULES: tuple[ModuleDef, ...] = PRODUCTION + WATCHDOGS + RESEARCH


def running_processes() -> set[str]:
    """Command lines of currently running Python processes.

    Used to report whether a strategy is actually live rather than merely
    installed. `ps` keeps this dependency-free.
    """
    try:
        output = subprocess.run(
            ["ps", "-eo", "command"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return set()
    return {line.strip() for line in output.splitlines() if "python" in line.lower()}


def build_inventory() -> dict:
    """The full inventory, with every declared fact verified against disk."""
    running = running_processes()
    entries = [m.to_dict(running) for m in ALL_MODULES]

    def summarize(kind: str, category: str | None = None) -> dict:
        subset = [
            e for e in entries
            if e["kind"] == kind and (category is None or e["category"] == category)
        ]
        return {
            "total": len(subset),
            "installed": sum(1 for e in subset if e["installed"]),
            "running": sum(1 for e in subset if e["running"]),
            "with_activity": sum(1 for e in subset if e["has_activity"]),
        }

    return {
        "generated_at": datetime.now().isoformat(),
        "repo_root": str(REPO_ROOT),
        "modules": entries,
        "summary": {
            "production_strategies": summarize("production", "strategy"),
            "watchdogs": summarize("production", "watchdog"),
            "analytics": summarize("production", "analytics"),
            "research_strategies": summarize("research", "strategy"),
        },
    }
