from dataclasses import replace
from pathlib import Path

from kinvest_trade.config import load_app_config
from kinvest_trade.momentum_policy import evaluate_exit_setup
from kinvest_trade.technical_signals import MovingAverageSnapshot


def _build_config():
    project_root = Path(__file__).resolve().parents[1]
    return replace(load_app_config(project_root / "config" / "fixed_config.json").auto_trade, max_hold_cycles=10)


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
        rsi14=55.0,
        intraday_volatility=0.001,
        intraday_momentum=0.001,
        intraday_bar_return=0.0006,
        volume_last=1500.0,
        volume_avg=1000.0,
        volume_ratio=1.5,
        breakout_level=100.1,
        breakdown_level=99.4,
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
        regime="range",
    )
    payload.update(overrides)
    return MovingAverageSnapshot(**payload)


def test_time_exit_triggers_on_loss_with_trend_gone() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=99.0,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.1,
            minute_ma_slow=99.5,
            intraday_momentum=0.0005,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "time_exit_loss"


def test_time_exit_does_not_trigger_on_loss_with_trend_intact() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=100.4,
            daily_ma_fast=101.0,
            daily_ma_slow=99.0,
            minute_ma_fast=100.5,
            minute_ma_slow=100.2,
            intraday_momentum=0.001,
            volume_ratio=0.8,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason == "hold"


def test_time_exit_forced_after_extended_hold_on_loss() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=100.4,
            daily_ma_fast=101.0,
            daily_ma_slow=99.0,
            minute_ma_fast=100.5,
            minute_ma_slow=100.2,
            intraday_momentum=0.001,
            volume_ratio=0.8,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=15,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "time_exit_forced"


def test_time_exit_profit_still_works() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(intraday_momentum=-0.0001),
        0.002,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "time_exit_profit"
