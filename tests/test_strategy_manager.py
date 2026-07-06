from __future__ import annotations

from kinvest_trade.indicators import compute_macd
from kinvest_trade.strategy import PriorityStrategyManager
from kinvest_trade.technical_signals import MovingAverageSnapshot


def _snapshot(**overrides) -> MovingAverageSnapshot:
    payload = dict(
        price=100.0,
        spread_pct=0.001,
        daily_ma_fast=101.0,
        daily_ma_slow=99.0,
        minute_ma_fast=100.5,
        minute_ma_slow=100.2,
        prev_minute_ma_fast=100.1,
        prev_minute_ma_slow=100.0,
        rsi14=48.0,
        intraday_volatility=0.001,
        intraday_momentum=0.001,
        intraday_bar_return=0.0006,
        volume_last=4000.0,
        volume_avg=1000.0,
        volume_ratio=4.0,
        breakout_level=99.9,
        breakdown_level=99.0,
        breakout_distance_pct=0.001,
        atr=0.2,
        atr_pct=0.002,
        bollinger_basis=100.0,
        bollinger_upper=100.4,
        bollinger_lower=99.6,
        daily_gap_fast_pct=-0.01,
        daily_gap_slow_pct=0.01,
        minute_gap_slow_pct=0.002,
        fast_above_slow=True,
        crossed_up=False,
        crossed_down=False,
        regime="bull",
        vwap=100.0,
        macd_line=0.5,
        macd_signal=0.4,
        macd_golden=True,
        macd_dead=False,
    )
    payload.update(overrides)
    return MovingAverageSnapshot(**payload)


def test_compute_macd_returns_values_for_long_series() -> None:
    prices = [float(value) for value in range(1, 60)]

    macd_line, signal_line, golden_cross, dead_cross = compute_macd(prices)

    assert macd_line is not None
    assert signal_line is not None
    assert golden_cross in {True, False}
    assert dead_cross in {True, False}


def test_priority_strategy_manager_preview_buy_includes_flag_and_entry_by() -> None:
    manager = PriorityStrategyManager()

    result = manager.evaluate("SOXL", _snapshot(), commit=False)

    assert result.signal == "BUY"
    assert result.flag == "VWAP+VOL+RSI"
    assert result.entry_by == "VWAP"
    assert manager.position is None


def test_priority_strategy_manager_buy_score_sums_buy_signals() -> None:
    manager = PriorityStrategyManager()

    score = manager.buy_score(_snapshot())

    assert score > 0
    assert score == 179.0


def test_priority_strategy_manager_hold_returns_monitoring_flag() -> None:
    manager = PriorityStrategyManager()

    result = manager.evaluate(
        "SOXL",
        _snapshot(
            vwap=105.0,
            volume_ratio=1.7,
            breakout_distance_pct=-0.01,
            rsi14=52.0,
            macd_golden=False,
            macd_line=0.2,
            macd_signal=0.1,
        ),
        commit=False,
    )

    assert result.signal == "HOLD"
    assert result.flag == "VWAP+VOL+RSI"
    assert result.entry_by == ""
    assert manager.position is None


def test_priority_strategy_manager_sell_uses_triggered_strategy_exit() -> None:
    manager = PriorityStrategyManager()
    manager.open_position(
        symbol="SOXL",
        entry_price=100.0,
        triggered_by=result_triggered(),
    )

    result = manager.evaluate(
        "SOXL",
        _snapshot(
            price=98.0,
            vwap=100.5,
            macd_golden=False,
            macd_dead=True,
            rsi14=72.0,
        ),
        commit=False,
    )

    assert result.signal == "SELL"
    assert result.flag == "VWAP+VOL+RSI"
    assert result.entry_by == "VWAP"
    assert result.exit_by == "VWAP"


def result_triggered():
    preview = PriorityStrategyManager().evaluate("SOXL", _snapshot(), commit=False)
    return preview.triggered_by
