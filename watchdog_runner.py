#!/usr/bin/env python3
"""Standalone runner for the watchdogs and observability jobs — nothing else.

    python watchdog_runner.py                  # run until stopped
    python watchdog_runner.py --once            # one pass of every job, then exit
    python watchdog_runner.py --status           # what last ran, and when

WHY THIS EXISTS SEPARATELY FROM flask_app.py. The watchdog jobs are wired as
APScheduler jobs inside `notifications.scheduler.init_scheduler()`, but that
function is only ever called from `flask_app.py` — a 4,400-line process that
also registers `covered_calls_bp`, an options-selling strategy that places
real orders. Starting the console to get the watchdogs running would start
that too.

This runner imports exactly six functions -- the read-only monitors and
observability jobs -- and schedules them at sensible intervals. No blueprint,
no HTTP server, no order-placing strategy code is imported, and every job
here was read before being wired in, not assumed safe from its name:

    GTT Monitor              every 10 min   detects GTT/position mismatches
    Position Guard           every  5 min   flags unwanted long exposure
    Duplicate Order Monitor  every  5 min   flags near-duplicate orders
    Instrument Sync          every 60 min   resolves the configured universe
                                             to real tokens/tick sizes
    CAS Tracker              hourly, idempotent  starts its background threads
    Notifications Purge      every 24 hrs   deletes old, non-active notices

Plus nine notifications/scheduler.py jobs that were blocked by the same
Kite-only token gate (login/session check, expiry-day short-position alert,
daily P&L, journal reconciliation, EOD candle fetch, margin/delta/ITM-loss
checks, copy-trade margin check) -- each read before being added, and each
only ever reads positions/orders/margins and dispatches a notification.

Deliberately excluded: scalping auto-start/stop (starts an actual strategy
loop, not a check) and the zone/delta-snapshot jobs (depend on
`swing_levels`/`delta_live_tracker`, modules not present in this checkout --
a real gap, not something to paper over by skipping silently).

Every job here reads and reports; none places, modifies or cancels an order.
`live_trading = false` stays enforced by the shim regardless -- these jobs
would be paper-safe either way, since none of them touches order flow.

ON INSTRUMENT SYNC. Flattrade has no bulk instrument-dump endpoint the way
Kite or Dhan's scrip master do -- only per-symbol lookup. So this syncs the
CONFIGURED UNIVERSE (the symbols this platform actually trades), each
resolved to its real numeric token and tick size, not a full exchange
catalogue. instrument_cache's own atomic swap and staleness check
(needs_sync()) do the rest, unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("watchdog_runner")

HEARTBEAT_PATH = ROOT / "quant_backtester" / "state" / "watchdog_heartbeat.json"

JOBS = [
    ("gtt_monitor", 600, "GTT / position mismatch check"),
    ("position_guard", 300, "unwanted long-exposure check"),
    ("duplicate_order_monitor", 300, "duplicate-order check"),
    ("instrument_sync", 3600, "resolve the configured universe"),
    ("cas_tracker", 3600, "CAS tracker background threads (idempotent)"),
    ("notifications_purge", 86400, "delete old non-active notifications"),
    # -- notifications/scheduler.py jobs, migrated off the Kite-only token
    # gate (see instrument_cache.get_kite_token()). Each read here and
    # verified to only ever read positions/orders/margins and dispatch a
    # notification -- none places, modifies or cancels an order.
    ("kite_login_check", 1200, "alerts if the broker session has lapsed"),
    ("early_exit_required", 1200, "flags open shorts on an expiry day"),
    ("daily_pnl_summary", 86400, "end-of-day P&L notification"),
    ("reconcile_journal", 86400, "compares broker fills against the local journal"),
    ("fetch_candles_eod", 86400, "downloads end-of-day candles"),
    ("nifty_margin_check", 3600, "next-expiry margin adequacy check"),
    ("nifty_delta_check", 900, "next-expiry delta exposure check"),
    ("nifty_itm_loss_check", 900, "next-expiry ITM loss check"),
    ("copytrade_margin_check", 900, "copy-trading account margin check"),
]


def _run_gtt_monitor() -> dict:
    from gtt_monitor import run_gtt_monitor_check

    mismatches = run_gtt_monitor_check(force=True)
    return {"mismatches": len(mismatches)}


def _run_position_guard() -> dict:
    from position_guard.detector import check_and_notify_position_guard

    check_and_notify_position_guard()
    return {}


def _run_duplicate_order_monitor() -> dict:
    from duplicate_order_monitor import check_and_notify_duplicates

    check_and_notify_duplicates()
    return {}


def _run_instrument_sync() -> dict:
    """Resolve the configured universe to real tokens/tick sizes.

    instrument_cache owns staleness checking and the atomic table swap;
    this only supplies the (adapter-backed) client and lets it decide
    whether a sync is actually due.
    """
    import common_lib
    import instrument_cache

    if not instrument_cache.needs_sync():
        return {"synced": False, "reason": "not due"}
    ok = instrument_cache.sync_instruments(common_lib.kite)
    return {"synced": ok}


def _run_cas_tracker() -> dict:
    """Start the CAS tracker's background threads, once.

    init_cas_tracker() is idempotent -- internal _monitor_started /
    _live_check_started guards make a repeat call a no-op -- so scheduling
    it hourly costs nothing after the first real start and self-heals if
    the threads ever died without the guard flags resetting.
    """
    from cas_tracker.blueprint import init_cas_tracker

    init_cas_tracker()
    return {"started": True}


def _run_notifications_purge() -> dict:
    from notifications.database import purge_old_non_active_notifications

    deleted = purge_old_non_active_notifications(days=30)
    return {"deleted": deleted}


def _scheduler_job(name: str):
    """Wrap one notifications.scheduler._job_* function.

    Those functions already never raise (each catches and logs internally,
    by their own docstrings) and return None -- there is nothing to surface
    beyond "it ran", so that is what is reported.
    """
    def run() -> dict:
        from notifications import scheduler as sch

        getattr(sch, name)()
        return {"ran": True}

    return run


RUNNERS = {
    "gtt_monitor": _run_gtt_monitor,
    "position_guard": _run_position_guard,
    "duplicate_order_monitor": _run_duplicate_order_monitor,
    "instrument_sync": _run_instrument_sync,
    "cas_tracker": _run_cas_tracker,
    "notifications_purge": _run_notifications_purge,
    "kite_login_check": _scheduler_job("_job_kite_login_check"),
    "early_exit_required": _scheduler_job("_job_early_exit_required"),
    "daily_pnl_summary": _scheduler_job("_job_daily_pnl_summary"),
    "reconcile_journal": _scheduler_job("_job_reconcile_journal"),
    "fetch_candles_eod": _scheduler_job("_job_fetch_candles_eod"),
    "nifty_margin_check": _scheduler_job("_job_nifty_margin_check"),
    "nifty_delta_check": _scheduler_job("_job_nifty_delta_check"),
    "nifty_itm_loss_check": _scheduler_job("_job_nifty_itm_loss_check"),
    "copytrade_margin_check": _scheduler_job("_job_copytrade_margin_check"),
}


def _write_heartbeat(state: dict) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_PATH.write_text(json.dumps(state, indent=1))


def _read_heartbeat() -> dict:
    if not HEARTBEAT_PATH.exists():
        return {}
    try:
        return json.loads(HEARTBEAT_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def run_all(once: bool = False) -> dict:
    """One pass of every job. Never lets one job's failure skip the rest."""
    results = {}
    for key, _interval, label in JOBS:
        started = time.monotonic()
        try:
            detail = RUNNERS[key]()
            results[key] = {
                "ok": True, "detail": detail,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "ran_at": datetime.now().isoformat(timespec="seconds"),
            }
            logger.info("%s (%s): ok %s", key, label, detail)
        except Exception as exc:  # noqa: BLE001 - one job must not stop the others
            results[key] = {"ok": False, "error": str(exc)}
            logger.error("%s (%s) FAILED: %s", key, label, exc, exc_info=True)
    return results


def show_status() -> int:
    beat = _read_heartbeat()
    if not beat:
        print("No watchdog runner has ever run.")
        return 1

    last = datetime.fromisoformat(beat["last_pass"])
    age = (datetime.now() - last).total_seconds()
    print(f"last pass  : {beat['last_pass']}  ({age:.0f}s ago)")
    stale = age > max(i for _, i, _ in JOBS) * 2
    print(f"status     : {'STALE — nothing is watching' if stale else 'running'}")
    print()
    for key, _interval, label in JOBS:
        result = beat.get("jobs", {}).get(key, {})
        if not result:
            print(f"  {key:26} never run")
        elif result.get("ok"):
            print(f"  {key:26} ok   {result.get('detail')}  ({label})")
        else:
            print(f"  {key:26} FAILED: {result.get('error', '')[:60]}")
    return 1 if stale else 0


def watch(once: bool) -> int:
    stopping = {"now": False}

    def stop(signum, _frame):  # noqa: ARG001
        logger.warning("Signal %s received — stopping after this pass.", signum)
        stopping["now"] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_run = {key: 0.0 for key, _i, _l in JOBS}
    logger.info("Watchdog runner started (%s jobs, Ctrl-C to stop)", len(JOBS))

    while not stopping["now"]:
        now = time.monotonic()
        due = [key for key, interval, _l in JOBS if now - last_run[key] >= interval]
        if due or not any(last_run.values()):
            results = {}
            for key, _interval, label in JOBS:
                if key in due or last_run[key] == 0.0:
                    started = time.monotonic()
                    try:
                        detail = RUNNERS[key]()
                        results[key] = {
                            "ok": True, "detail": detail,
                            "duration_ms": round((time.monotonic() - started) * 1000),
                        }
                        logger.info("%s: ok %s", key, detail)
                    except Exception as exc:  # noqa: BLE001
                        results[key] = {"ok": False, "error": str(exc)}
                        logger.error("%s FAILED: %s", key, exc, exc_info=True)
                    last_run[key] = time.monotonic()
            _write_heartbeat({
                "last_pass": datetime.now().isoformat(timespec="seconds"),
                "jobs": results,
            })

        if once:
            break
        for _ in range(50):  # 5s in 100ms slices, so Ctrl-C is responsive
            if stopping["now"]:
                break
            time.sleep(0.1)

    logger.info("Watchdog runner stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.status:
        return show_status()
    return watch(args.once)


if __name__ == "__main__":
    raise SystemExit(main())
