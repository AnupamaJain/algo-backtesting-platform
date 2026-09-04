"""The repository inventory must describe what is actually on disk.

A strategy list that claims a module exists when it does not is worse than no
list: it makes the console confidently wrong. These tests assert the declared
inventory against the filesystem, so a rename or deletion fails here rather
than silently rendering a phantom strategy in the UI.
"""

from __future__ import annotations

import pytest

from quant_backtester.src.ops.inventory import (
    ALL_MODULES,
    PRODUCTION,
    RESEARCH,
    REPO_ROOT,
    WATCHDOGS,
    build_inventory,
)


def test_every_declared_module_exists_on_disk():
    missing = {m.key: m.missing_modules() for m in ALL_MODULES if not m.installed()}
    assert not missing, f"inventory references files that do not exist: {missing}"


def test_module_keys_are_unique():
    keys = [m.key for m in ALL_MODULES]
    assert len(keys) == len(set(keys))


def test_prd_strategies_are_all_present():
    """Every strategy named in the platform PRD must appear in the inventory —
    this is what caught the console listing only the research six."""
    keys = {m.key for m in PRODUCTION}
    for expected in (
        "survivor_nifty",
        "survivor_sensex",
        "survivor_stock",
        "wave_extractor",
        "expiry_trade",
        "early_exit_nifty",
        "covered_calls",
        "survivor_delta_rebalance",
    ):
        assert expected in keys, f"{expected} is described in the PRD but missing from inventory"


def test_prd_watchdogs_are_all_present():
    keys = {m.key for m in WATCHDOGS}
    for expected in (
        "gtt_monitor",
        "position_guard",
        "duplicate_order_monitor",
        "trade_journal",
        "kite_api_monitor",
    ):
        assert expected in keys, f"{expected} is described in the PRD but missing from inventory"


def test_research_strategies_match_the_registry():
    """The research entries must match the classes the backtester can build."""
    from quant_backtester.src.strategies import STRATEGY_REGISTRY

    declared = {m.entry for m in RESEARCH}
    assert declared == set(STRATEGY_REGISTRY)


def test_production_modules_declare_their_broker_dependency():
    """These reach a broker, and the UI must say so. The dependency is on
    *a configured broker* rather than a named vendor — which one is a
    config decision, so naming one here would re-couple the inventory to it."""
    for module in PRODUCTION:
        if module.key == "survivor_delta_rebalance" or module.category == "strategy":
            assert "broker" in module.requires, (
                f"{module.key} should declare its broker dependency"
            )


def test_inventory_reports_line_counts():
    inventory = build_inventory()
    strategies = [m for m in inventory["modules"] if m["kind"] == "production"]
    assert all(m["lines"] > 0 for m in strategies)


def test_inventory_summary_counts_match_the_module_list():
    inventory = build_inventory()
    production = [
        m for m in inventory["modules"]
        if m["kind"] == "production" and m["category"] == "strategy"
    ]
    assert inventory["summary"]["production_strategies"]["total"] == len(production)


def test_inventory_is_json_serializable():
    import json

    json.dumps(build_inventory(), default=str)


def test_state_source_handles_a_missing_database():
    """A fresh checkout has no state databases; that must read as empty rather
    than raising."""
    from quant_backtester.src.ops.inventory import StateSource

    state = StateSource("does/not/exist.db", "nothing").read()
    assert state == {"available": False, "rows": 0, "last_activity": None}


def test_never_run_is_distinguishable_from_broken():
    """'Installed but idle' and 'files missing' are different states, and the
    console renders them differently."""
    inventory = build_inventory()
    for module in inventory["modules"]:
        assert module["installed"] is True
        # No activity in a fresh checkout, but that is not a failure.
        assert module["has_activity"] in (True, False)


def test_never_run_is_only_claimed_when_something_was_checked():
    """Reporting "never run" for a module whose state location was never
    declared asserts a fact nobody looked up. The console must be able to
    tell "we looked and found nothing" from "we had nowhere to look"."""
    from quant_backtester.src.ops.inventory import build_inventory

    inventory = build_inventory()
    for module in inventory["modules"]:
        assert "activity_checked" in module, f"{module['key']} lacks activity_checked"
        if module["has_activity"]:
            assert module["activity_checked"], (
                f"{module['key']} reports activity without having been checked"
            )


def test_survivor_strategies_declare_where_they_persist():
    """The Survivor variants write survivor_state_<pid>.json. Without a
    declared source the console reported them as "never run" while never
    having looked anywhere.

    survivor_delta_rebalance is not a separate process -- it is the
    delta-rebalancing feature inside place_order_at_nifty.py/
    place_order_at_sensex.py -- but its activity is still real and
    checkable: those scripts log CE_ANCHOR_REBALANCE/PE_ANCHOR_REBALANCE
    rows to the same survivor_events.db, which is what its state source
    filters for.
    """
    from quant_backtester.src.ops.inventory import PRODUCTION

    persisting = [m for m in PRODUCTION if m.key.startswith("survivor_") and m.category == "strategy"]
    assert persisting, "expected Survivor strategies in the inventory"
    undeclared = [m.key for m in persisting if m.state is None]
    assert not undeclared, f"no state source declared for {undeclared}"


def test_a_module_that_persists_nothing_is_not_claimed_to_have_never_run():
    """expiry_trade only exists as Flask routes over an in-memory
    ExpiryTradeSystem -- no standalone script, no file, no db row -- so the
    console must say "not tracked" rather than asserting it never ran."""
    from quant_backtester.src.ops.inventory import build_inventory

    inventory = build_inventory()
    module = next(
        m for m in inventory["modules"] if m["key"] == "expiry_trade"
    )
    assert module["activity_checked"] is False
    assert module["has_activity"] is False


def test_file_state_source_counts_run_artifacts(tmp_path, monkeypatch):
    from quant_backtester.src.ops import inventory as inv

    monkeypatch.setattr(inv, "REPO_ROOT", tmp_path / "quant_backtester")
    runs = tmp_path / "survivor_status"
    runs.mkdir()
    source = inv.FileStateSource("survivor_status", "survivor_state_*.json")

    assert source.read()["rows"] == 0          # directory exists, no runs yet
    (runs / "survivor_state_123.json").write_text("{}")
    result = source.read()
    assert result["rows"] == 1 and result["last_activity"] is not None


def test_file_state_source_reports_a_missing_directory_as_unavailable(tmp_path, monkeypatch):
    from quant_backtester.src.ops import inventory as inv

    monkeypatch.setattr(inv, "REPO_ROOT", tmp_path / "quant_backtester")
    source = inv.FileStateSource("nowhere", "*.json")
    assert source.read() == {"available": False, "rows": 0, "last_activity": None}


def test_every_status_carries_the_evidence_behind_it():
    """A status claim the reader cannot audit invites "why does it say
    that?". The evidence travels with the claim so the UI can show it."""
    from quant_backtester.src.ops.inventory import build_inventory

    for module in build_inventory()["modules"]:
        evidence = module.get("activity_evidence")
        assert evidence, f"{module['key']} has no evidence for its status"


def test_evidence_names_the_location_that_was_checked():
    from quant_backtester.src.ops.inventory import build_inventory

    modules = {m["name"]: m for m in build_inventory()["modules"]}
    monitor = modules["Broker API Monitor"]
    assert "api_monitor.db" in monitor["activity_evidence"]
    assert "api_calls" in monitor["activity_evidence"]


def test_an_untracked_module_says_so_rather_than_naming_a_file():
    """expiry_trade persists nothing (see the test above) -- genuinely
    untracked, unlike Wave Extractor which now declares status_*_*.json
    via FileStateSource, or survivor_delta_rebalance which now declares
    survivor_events.db filtered to its own rebalance events."""
    from quant_backtester.src.ops.inventory import build_inventory

    modules = {m["key"]: m for m in build_inventory()["modules"]}
    assert "no state source" in modules["expiry_trade"]["activity_evidence"]


def test_heartbeat_state_reflects_the_watchdog_runners_own_file(tmp_path, monkeypatch):
    """The watchdogs run inside one consolidated process (watchdog_runner.py)
    so starting them does not require booting flask_app.py, which also
    registers an order-placing strategy blueprint. Process-table matching on
    each module's own filename never finds them running as a result — this
    is the alternative evidence."""
    import json

    from quant_backtester.src.ops import inventory as inv

    state_dir = tmp_path / "quant_backtester" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(inv, "REPO_ROOT", tmp_path)

    source = inv.HeartbeatStateSource("gtt_monitor")
    assert source.read() == {"available": False, "rows": 0, "last_activity": None}

    (state_dir / "watchdog_heartbeat.json").write_text(json.dumps({
        "last_pass": "2026-09-03T13:51:13",
        "jobs": {"gtt_monitor": {"ok": True, "detail": {"mismatches": 0}}},
    }))
    result = source.read()
    assert result["available"] is True
    assert result["rows"] == 1
    assert result["last_activity"] == "2026-09-03T13:51:13"


def test_heartbeat_reports_a_failed_job_as_no_rows(tmp_path, monkeypatch):
    import json

    from quant_backtester.src.ops import inventory as inv

    state_dir = tmp_path / "quant_backtester" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(inv, "REPO_ROOT", tmp_path)
    (state_dir / "watchdog_heartbeat.json").write_text(json.dumps({
        "last_pass": "2026-09-03T13:51:13",
        "jobs": {"gtt_monitor": {"ok": False, "error": "boom"}},
    }))
    assert inv.HeartbeatStateSource("gtt_monitor").read()["rows"] == 0


def test_all_three_watchdogs_share_the_one_heartbeat_file():
    """They run in one process; each must read its own job key from the
    same shared file rather than expecting a file of its own."""
    from quant_backtester.src.ops.inventory import WATCHDOGS

    watchdogs = {"gtt_monitor", "position_guard", "duplicate_order_monitor"}
    found = {m.key: m.state for m in WATCHDOGS if m.key in watchdogs}
    assert set(found) == watchdogs
    for key, state in found.items():
        assert state.file == "watchdog_heartbeat.json"
        assert state.job_key == key


def test_cas_tracker_and_notifications_use_the_shared_heartbeat_too():
    """Same architectural problem as the three watchdogs: their own tables
    record findings (snapshots, dispatched notifications) rather than
    whether the job ran, so a quiet day looks identical to never running."""
    from quant_backtester.src.ops.inventory import ALL_MODULES, HeartbeatStateSource

    by_key = {m.key: m for m in ALL_MODULES}
    assert isinstance(by_key["cas_tracker"].state, HeartbeatStateSource)
    assert by_key["cas_tracker"].state.job_key == "cas_tracker"
    assert isinstance(by_key["notifications"].state, HeartbeatStateSource)
    assert by_key["notifications"].state.job_key == "notifications_purge"
