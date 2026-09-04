"""Live trading engine: signal generation, position sizing, risk gates."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.broker import PaperBroker, Side, StaticQuotes
from quant_backtester.src.broker.service import OrderService, SafetySettings
from quant_backtester.src.broker.store import BrokerStore
from quant_backtester.src.live.engine import (
    PositionSizer,
    RiskLimits,
    RiskManager,
    Signal,
    SignalGenerator,
    SizedOrder,
    SizingPolicy,
    TradingEngine,
)


def _ohlcv(n: int = 400, seed: int = 1, volatility: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-01", periods=n)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, volatility, n))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * (1 + volatility),
            "Low": close * (1 - volatility),
            "Close": close,
            "Volume": 1_000_000,
        },
        index=dates,
    )


def _signal(symbol="SPY", target=1, price=100.0, strategy="RSIReversion") -> Signal:
    return Signal(
        symbol=symbol, strategy=strategy, target=target, price=price, as_of=datetime.now()
    )


def _order(**kw) -> SizedOrder:
    base = dict(
        symbol="SPY", strategy="s", side=Side.BUY, quantity=10.0, price=100.0,
        target_position=10.0, current_position=0.0, risk_amount=1000.0, stop_distance=5.0,
    )
    base.update(kw)
    return SizedOrder(**base)


# ==========================================================================
# Signal generation
# ==========================================================================


def test_signal_uses_the_last_closed_bar():
    """Live signals must come from the same place the backtest reads them —
    the most recent completed bar, never a forming one."""
    data = _ohlcv()
    signal = SignalGenerator().generate("SPY", "RSIReversion", {"rsi_period": 14}, data)

    assert signal is not None
    assert signal.as_of == data.index[-1].to_pydatetime()
    assert signal.price == pytest.approx(data["Close"].iloc[-1])
    assert signal.target in (-1, 0, 1)


def test_insufficient_history_yields_no_signal():
    """A half-warmed indicator is worse than no signal: it looks authoritative
    while being computed from nothing."""
    assert SignalGenerator(min_bars=260).generate(
        "SPY", "SimpleMomentum", {"lookback_period": 252}, _ohlcv(n=50)
    ) is None


def test_a_broken_strategy_does_not_stop_the_rest():
    generator = SignalGenerator()
    assert generator.generate("SPY", "NoSuchStrategy", {}, _ohlcv()) is None
    assert generator.generate("SPY", "RSIReversion", {"rsi_period": 14}, _ohlcv()) is not None


def test_generate_all_skips_symbols_without_data():
    deployed = pd.DataFrame([
        {"symbol": "SPY", "strategy": "RSIReversion", "params": {"rsi_period": 14}},
        {"symbol": "NOPE", "strategy": "RSIReversion", "params": {"rsi_period": 14}},
    ])
    signals = SignalGenerator().generate_all(deployed, {"SPY": _ohlcv()})
    assert [s.symbol for s in signals] == ["SPY"]


def test_params_survive_a_csv_round_trip():
    """Deployed sets arrive from CSV, where the params dict is a string."""
    deployed = pd.DataFrame([
        {"symbol": "SPY", "strategy": "RSIReversion", "params": "{'rsi_period': 21}"}
    ])
    signals = SignalGenerator().generate_all(deployed, {"SPY": _ohlcv()})
    assert signals and signals[0].params == {"rsi_period": 21}


# ==========================================================================
# Position sizing
# ==========================================================================


def test_size_is_risk_based_not_capital_based():
    """The whole point: a volatile asset gets FEWER units for the same risk.

    Equal dollar sizing would hand the volatile name several times the risk.
    """
    sizer = PositionSizer(SizingPolicy(risk_per_trade=0.01, max_position_pct=1.0))
    calm, wild = _ohlcv(volatility=0.005, seed=2), _ohlcv(volatility=0.05, seed=2)

    calm_order = sizer.size(_signal(price=float(calm["Close"].iloc[-1])), 100_000, calm)
    wild_order = sizer.size(_signal(price=float(wild["Close"].iloc[-1])), 100_000, wild)

    assert calm_order and wild_order
    assert calm_order.quantity > wild_order.quantity


def test_risk_amount_is_the_configured_fraction_of_equity():
    sizer = PositionSizer(SizingPolicy(risk_per_trade=0.02))
    order = sizer.size(_signal(), 50_000, _ohlcv())
    assert order and order.risk_amount == pytest.approx(1_000.0)


def test_position_cap_binds_before_risk_does():
    """With a generous risk budget the concentration cap must still hold."""
    sizer = PositionSizer(SizingPolicy(risk_per_trade=0.50, max_position_pct=0.10))
    data = _ohlcv()
    price = float(data["Close"].iloc[-1])
    order = sizer.size(_signal(price=price), 100_000, data)

    assert order
    assert order.quantity * price <= 100_000 * 0.10 * 1.001


def test_sizing_nets_against_the_existing_position():
    """Orders express the DELTA to target, not the target itself — otherwise
    every pass would double the book."""
    sizer = PositionSizer(SizingPolicy(max_position_pct=1.0))
    data = _ohlcv()
    full = sizer.size(_signal(), 100_000, data, current_position=0.0)
    partial = sizer.size(_signal(), 100_000, data, current_position=full.quantity / 2)

    assert partial.quantity == pytest.approx(full.quantity / 2, rel=0.01)
    assert partial.target_position == pytest.approx(full.target_position)


def test_no_order_when_already_at_target():
    sizer = PositionSizer()
    data = _ohlcv()
    order = sizer.size(_signal(), 100_000, data)
    assert sizer.size(_signal(), 100_000, data, current_position=order.target_position) is None


def test_flat_signal_closes_an_open_position():
    sizer = PositionSizer()
    order = sizer.size(_signal(target=0), 100_000, _ohlcv(), current_position=50)
    assert order and order.side is Side.SELL and order.target_position == 0


def test_short_signal_produces_a_negative_target():
    sizer = PositionSizer()
    order = sizer.size(_signal(target=-1), 100_000, _ohlcv())
    assert order and order.target_position < 0 and order.side is Side.SELL


def test_zero_equity_cannot_size():
    assert PositionSizer().size(_signal(), 0.0, _ohlcv()) is None


# ==========================================================================
# Risk gates
# ==========================================================================


def _evaluate(order, **kw):
    defaults = dict(equity=100_000.0, cash=100_000.0, positions={}, prices={"SPY": 100.0})
    defaults.update(kw)
    return RiskManager(kw.pop("limits", None) or RiskLimits()).evaluate(order, **defaults)


def test_order_within_limits_is_approved():
    assert _evaluate(_order()).approved


def test_position_count_limit_blocks_a_new_symbol():
    manager = RiskManager(RiskLimits(max_positions=2))
    decision = manager.evaluate(
        _order(symbol="NEW", target_position=10),
        equity=100_000, cash=100_000,
        positions={"A": 10, "B": 10}, prices={"A": 100, "B": 100, "NEW": 100},
    )
    assert not decision.approved and "maximum" in decision.reason


def test_position_limit_does_not_block_an_existing_symbol():
    """Adding to a position you already hold does not increase the count."""
    manager = RiskManager(RiskLimits(max_positions=2))
    decision = manager.evaluate(
        _order(symbol="A", target_position=20),
        equity=100_000, cash=100_000,
        positions={"A": 10, "B": 10}, prices={"A": 100, "B": 100},
    )
    assert decision.approved


def test_concentration_cap_blocks_an_oversized_position():
    manager = RiskManager(RiskLimits(max_position_pct=0.10))
    decision = manager.evaluate(
        _order(target_position=500, price=100),  # 50,000 = 50% of equity
        equity=100_000, cash=100_000, positions={}, prices={"SPY": 100},
    )
    assert not decision.approved and "cap" in decision.reason


def test_gross_exposure_is_checked_on_the_projected_book():
    """Checking the CURRENT book would let each order pass individually while
    the total quietly breached the limit."""
    manager = RiskManager(RiskLimits(max_gross_exposure=1.0, max_position_pct=1.0))
    decision = manager.evaluate(
        _order(symbol="C", target_position=500, price=100),
        equity=100_000, cash=100_000,
        positions={"A": 400, "B": 300}, prices={"A": 100, "B": 100, "C": 100},
    )
    assert not decision.approved and "gross exposure" in decision.reason


def test_cash_buffer_blocks_spending_the_last_of_the_cash():
    manager = RiskManager(RiskLimits(min_cash_buffer_pct=0.10, max_position_pct=1.0))
    decision = manager.evaluate(
        _order(quantity=95, price=100, target_position=95),
        equity=100_000, cash=9_500, positions={}, prices={"SPY": 100},
    )
    assert not decision.approved and "available" in decision.reason


def test_short_selling_can_be_disabled():
    manager = RiskManager(RiskLimits(allow_short=False))
    decision = manager.evaluate(
        _order(side=Side.SELL, target_position=-10),
        equity=100_000, cash=100_000, positions={}, prices={"SPY": 100},
    )
    assert not decision.approved and "short" in decision.reason


def test_daily_loss_halt_blocks_new_risk():
    manager = RiskManager(RiskLimits(max_daily_loss_pct=0.05))
    decision = manager.evaluate(
        _order(), equity=100_000, cash=100_000, positions={}, prices={"SPY": 100},
        day_pnl_pct=-0.06,
    )
    assert not decision.approved and "halt" in decision.reason


def test_daily_loss_halt_still_allows_closing():
    """A limit that stops you exiting makes risk worse, not better."""
    manager = RiskManager(RiskLimits(max_daily_loss_pct=0.05))
    decision = manager.evaluate(
        _order(side=Side.SELL, target_position=0.0, current_position=100.0),
        equity=100_000, cash=100_000, positions={"SPY": 100.0}, prices={"SPY": 100},
        day_pnl_pct=-0.20,
    )
    assert decision.approved


def test_closing_is_exempt_from_the_exposure_limit():
    manager = RiskManager(RiskLimits(max_gross_exposure=0.01))
    decision = manager.evaluate(
        _order(side=Side.SELL, target_position=0.0, current_position=500.0),
        equity=100_000, cash=100_000, positions={"SPY": 500.0}, prices={"SPY": 100},
    )
    assert decision.approved


# ==========================================================================
# End to end
# ==========================================================================


@pytest.fixture
def engine(tmp_path) -> TradingEngine:
    prices = {"SPY": 100.0, "QQQ": 200.0}
    broker = PaperBroker(
        BrokerStore(tmp_path / "live.db"), StaticQuotes(prices),
        config={"starting_cash": 100_000.0, "universe": list(prices)},
    )
    return TradingEngine(
        service=OrderService(broker, BrokerStore(tmp_path / "live.db"),
                             SafetySettings(duplicate_window_seconds=0)),
        sizer=PositionSizer(SizingPolicy(risk_per_trade=0.01)),
        risk=RiskManager(RiskLimits()),
    )


@pytest.fixture
def deployed() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "SPY", "strategy": "RSIReversion", "params": {"rsi_period": 14}},
        {"symbol": "QQQ", "strategy": "MACrossover",
         "params": {"fast_period": 20, "slow_period": 50}},
    ])


def test_dry_run_places_nothing(engine, deployed):
    data = {"SPY": _ohlcv(seed=3), "QQQ": _ohlcv(seed=4)}
    run = engine.run(deployed, data, dry_run=True)
    assert run.placed == []
    assert engine._service.adapter.get_positions() == []


def test_live_run_places_approved_orders(engine, deployed):
    data = {"SPY": _ohlcv(seed=3), "QQQ": _ohlcv(seed=4)}
    run = engine.run(deployed, data, dry_run=False)
    summary = run.summary()

    assert summary["placed"] == summary["approved"]
    assert len(engine._service.adapter.get_positions()) == summary["placed"]


def test_a_second_pass_does_not_double_the_book(engine, deployed):
    """Orders target a position, not an increment. Running twice with the same
    signals must be a no-op, or a scheduled loop would compound its own book
    every cycle."""
    data = {"SPY": _ohlcv(seed=3), "QQQ": _ohlcv(seed=4)}
    engine.run(deployed, data)
    after_first = {p.symbol: p.quantity for p in engine._service.adapter.get_positions()}

    engine.run(deployed, data)
    after_second = {p.symbol: p.quantity for p in engine._service.adapter.get_positions()}

    assert after_first == after_second


def test_run_summary_accounts_for_every_signal(engine, deployed):
    run = engine.run(deployed, {"SPY": _ohlcv(seed=3), "QQQ": _ohlcv(seed=4)}, dry_run=True)
    summary = run.summary()
    assert summary["approved"] + summary["blocked"] == summary["actionable"]
    assert summary["actionable"] <= summary["signals"]


def test_engine_describes_its_own_policy(engine):
    described = engine.describe()
    assert "risk_per_trade" in described["sizing"]
    assert "max_gross_exposure" in described["risk"]


# -- netting contradictory configurations -------------------------------


def _sig(symbol, target, strategy="BBReversion", **params):
    from datetime import datetime

    from quant_backtester.src.live.engine import Signal

    return Signal(
        symbol=symbol, strategy=strategy, target=target,
        price=100.0, as_of=datetime(2026, 9, 1), params=params,
    )


def test_opposing_configurations_on_one_symbol_cancel_out():
    """Two settings of the same strategy disagreeing must not open a long AND
    a short in the same instrument."""
    from quant_backtester.src.live.engine import net_signals_by_symbol

    netted, conflicts = net_signals_by_symbol([
        _sig("ICICIBANK-EQ", 1, period=20),
        _sig("ICICIBANK-EQ", -1, period=14),
    ])

    assert netted == []
    assert "ICICIBANK-EQ" in conflicts
    assert "FLAT" in conflicts["ICICIBANK-EQ"]


def test_agreeing_configurations_produce_one_signal():
    from quant_backtester.src.live.engine import net_signals_by_symbol

    netted, conflicts = net_signals_by_symbol([
        _sig("SBIN-EQ", 1, period=20),
        _sig("SBIN-EQ", 1, period=14),
    ])

    assert len(netted) == 1 and netted[0].target == 1
    assert conflicts == {}


def test_a_majority_wins_and_is_still_reported_as_a_conflict():
    from quant_backtester.src.live.engine import net_signals_by_symbol

    netted, conflicts = net_signals_by_symbol([
        _sig("INFY-EQ", 1, period=20),
        _sig("INFY-EQ", 1, period=30),
        _sig("INFY-EQ", -1, period=14),
    ])

    assert len(netted) == 1 and netted[0].target == 1
    # It resolved, but the disagreement is still surfaced.
    assert "INFY-EQ" in conflicts


def test_unrelated_symbols_are_untouched():
    from quant_backtester.src.live.engine import net_signals_by_symbol

    netted, conflicts = net_signals_by_symbol([
        _sig("A-EQ", 1), _sig("B-EQ", -1), _sig("C-EQ", 0),
    ])

    assert {s.symbol for s in netted} == {"A-EQ", "B-EQ"}
    assert conflicts == {}


def test_the_representative_signal_keeps_a_real_strategy_and_params():
    from quant_backtester.src.live.engine import net_signals_by_symbol

    netted, _ = net_signals_by_symbol([
        _sig("TCS-EQ", 1, period=30), _sig("TCS-EQ", 1, period=20),
    ])

    assert netted[0].strategy == "BBReversion"
    assert netted[0].params.get("period") in (20, 30)
