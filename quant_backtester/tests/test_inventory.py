"""Tests for src/ops/inventory.py.

build_inventory() runs _research_modules() at module import time and
introspects the live STRATEGY_REGISTRY. A strategy whose __init__ breaks
its signature would silently kill ops_cli.py with no test catching it.
These tests guard against that and verify the inventory contract.
"""
from __future__ import annotations

import pytest

from quant_backtester.src.ops.inventory import (
    ALL_MODULES,
    PRODUCTION,
    RESEARCH,
    WATCHDOGS,
    ModuleDef,
    build_inventory,
)
from quant_backtester.src.strategies import STRATEGY_REGISTRY


# ==========================================================================
# Module registry contract
# ==========================================================================


class TestModuleDeclarations:
    def test_production_modules_non_empty(self):
        assert len(PRODUCTION) > 0

    def test_watchdog_modules_non_empty(self):
        assert len(WATCHDOGS) > 0

    def test_research_modules_match_registry(self):
        """Every strategy in STRATEGY_REGISTRY must appear as a research module."""
        research_entries = {m.entry for m in RESEARCH}
        for name in STRATEGY_REGISTRY:
            assert name in research_entries, (
                f"Strategy '{name}' is in STRATEGY_REGISTRY but missing from "
                "the research inventory. Add it or check _research_modules()."
            )

    def test_research_count_matches_registry(self):
        assert len(RESEARCH) == len(STRATEGY_REGISTRY)

    def test_all_modules_combines_all_three(self):
        assert len(ALL_MODULES) == len(PRODUCTION) + len(WATCHDOGS) + len(RESEARCH)

    def test_module_def_required_fields(self):
        for module in ALL_MODULES:
            assert module.key, f"Module has empty key: {module}"
            assert module.name, f"Module '{module.key}' has empty name"
            assert module.kind in ("production", "research"), (
                f"Module '{module.key}' has unknown kind '{module.kind}'"
            )
            assert module.category, f"Module '{module.key}' has empty category"

    def test_no_duplicate_keys(self):
        keys = [m.key for m in ALL_MODULES]
        seen = set()
        duplicates = []
        for k in keys:
            if k in seen:
                duplicates.append(k)
            seen.add(k)
        assert not duplicates, f"Duplicate module keys: {duplicates}"

    def test_research_modules_have_parameters(self):
        """Every research module must have at least one declared parameter."""
        for module in RESEARCH:
            assert module.parameters, (
                f"Research module '{module.key}' has no parameters — "
                "check that its __init__ signature is readable."
            )


# ==========================================================================
# build_inventory() output contract
# ==========================================================================


class TestBuildInventory:
    @pytest.fixture(scope="class")
    def inventory(self):
        return build_inventory()

    def test_returns_dict_with_required_keys(self, inventory):
        assert "modules" in inventory
        assert "summary" in inventory
        assert "generated_at" in inventory
        assert "repo_root" in inventory

    def test_modules_list_non_empty(self, inventory):
        assert len(inventory["modules"]) > 0

    def test_every_module_has_installed_field(self, inventory):
        for m in inventory["modules"]:
            assert "installed" in m, f"Module '{m.get('key')}' missing 'installed' field"
            assert isinstance(m["installed"], bool)

    def test_every_module_has_running_field(self, inventory):
        for m in inventory["modules"]:
            assert "running" in m, f"Module '{m.get('key')}' missing 'running' field"
            assert isinstance(m["running"], bool)

    def test_every_module_has_has_activity_field(self, inventory):
        for m in inventory["modules"]:
            assert "has_activity" in m

    def test_summary_keys_present(self, inventory):
        summary = inventory["summary"]
        for key in ("production_strategies", "watchdogs", "analytics", "research_strategies"):
            assert key in summary, f"Summary missing key '{key}'"

    def test_summary_counts_are_non_negative(self, inventory):
        for section, counts in inventory["summary"].items():
            for metric, value in counts.items():
                assert value >= 0, (
                    f"summary[{section!r}][{metric!r}] = {value} (negative is impossible)"
                )

    def test_research_summary_total_matches_registry(self, inventory):
        assert inventory["summary"]["research_strategies"]["total"] == len(STRATEGY_REGISTRY)

    def test_generated_at_is_iso_string(self, inventory):
        from datetime import datetime
        # Should not raise.
        datetime.fromisoformat(inventory["generated_at"])

    def test_module_dict_shape(self, inventory):
        """Spot-check one research module against the TypeScript OpsModule shape."""
        research = [m for m in inventory["modules"] if m["kind"] == "research"]
        assert research, "No research modules in inventory"
        m = research[0]
        for field in ("key", "name", "kind", "category", "family", "market", "summary",
                      "installed", "running", "has_activity", "parameters"):
            assert field in m, f"Research module missing field '{field}'"


# ==========================================================================
# Robustness: registry changes don't crash the inventory
# ==========================================================================


class TestInventoryRobustness:
    def test_build_inventory_is_idempotent(self):
        """Two successive calls must return the same module count."""
        inv1 = build_inventory()
        inv2 = build_inventory()
        assert len(inv1["modules"]) == len(inv2["modules"])

    def test_all_strategy_families_recognized(self):
        """Every family used in research modules must be a known string (not empty)."""
        for module in RESEARCH:
            assert module.family, (
                f"Research module '{module.key}' has empty family — "
                "set the `family` class attribute on the strategy class."
            )

    def test_module_to_dict_does_not_raise(self):
        """to_dict() must not raise for any declared module, running or not."""
        running = set()  # pretend nothing is running
        for module in ALL_MODULES:
            result = module.to_dict(running)
            assert isinstance(result, dict)
