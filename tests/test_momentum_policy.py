from dataclasses import replace
from pathlib import Path

from kinvest_trade.config import load_app_config
from kinvest_trade.momentum_policy import (
    _pullback_ready,
    evaluate_entry_setup,
    evaluate_exit_setup,
)
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
            volume_ratio=0.7,
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


def test_time_exit_does_not_trigger_on_loss_with_only_one_condition() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=99.4,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.1,
            minute_ma_slow=99.5,
            intraday_momentum=0.0008,
            volume_ratio=1.2,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason == "hold"


def test_time_exit_triggers_on_loss_with_two_conditions() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=99.4,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.1,
            minute_ma_slow=99.5,
            intraday_momentum=0.0008,
            volume_ratio=0.7,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "time_exit_loss"


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


def test_rsi_85_blocks_entry() -> None:
    result = evaluate_entry_setup(
        _build_config(),
        _snapshot(
            rsi14=86.0,
            volume_ratio=2.0,
            intraday_momentum=0.002,
            intraday_bar_return=0.001,
            price=100.3,
            breakout_level=100.1,
        ),
    )

    assert result.ready is False
    assert result.reason == "entry_rsi_too_high"


def test_rsi_71_does_not_block_entry_with_new_threshold() -> None:
    config = replace(_build_config(), max_entry_rsi14=75.0, volume_spike_ratio=1.1)
    result = evaluate_entry_setup(
        config,
        _snapshot(
            rsi14=71.0,
            volume_ratio=1.3,
            intraday_momentum=0.002,
            intraday_bar_return=0.0009,
            price=100.25,
            breakout_level=100.1,
        ),
    )

    assert result.ready is True


def test_breakout_proximity_entry() -> None:
    config = replace(
        _build_config(),
        breakout_proximity_pct=0.98,
        volume_spike_ratio=1.1,
        trend_require_price_above_slow=False,
    )
    breakout_level = 100.0
    result = evaluate_entry_setup(
        config,
        _snapshot(
            price=breakout_level * 0.99,
            breakout_level=breakout_level,
            breakout_distance_pct=-0.01,
            volume_ratio=1.2,
            intraday_momentum=0.0016,
            intraday_bar_return=0.0009,
            rsi14=62.0,
        ),
    )

    assert result.ready is True
    assert result.reason == "breakout_proximity_entry"


def test_fast_track_vr_2_0_multiplier() -> None:
    config = replace(
        _build_config(),
        volume_spike_ratio=1.2,
        min_bar_return_pct=0.0004,
    )
    result = evaluate_entry_setup(
        config,
        _snapshot(
            volume_ratio=2.4,
            intraday_bar_return=0.0013,
            intraday_momentum=0.001,
            rsi14=60.0,
        ),
    )

    assert result.ready is True
    assert result.reason == "volume_momentum_fast_entry"


def test_has_required_context_without_slow_ma() -> None:
    snapshot = _snapshot(minute_ma_slow=None)

    assert snapshot.has_required_context is True


def test_pullback_ready_returns_true_for_valid_pullback() -> None:
    result = _pullback_ready(
        _build_config(),
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.5,
            rsi14=50.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )

    assert result is True


def test_pullback_blocked_when_price_too_far_above_ma() -> None:
    result = _pullback_ready(
        _build_config(),
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=103.0,
            rsi14=50.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )

    assert result is False


def test_pullback_blocked_when_rsi_overbought() -> None:
    result = _pullback_ready(
        _build_config(),
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.2,
            rsi14=70.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )

    assert result is False


def test_pullback_is_first_priority_in_evaluate_entry() -> None:
    config = replace(_build_config(), volume_spike_ratio=1.5)
    result = evaluate_entry_setup(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.3,
            breakout_level=100.8,
            breakout_distance_pct=-0.0049,
            rsi14=50.0,
            intraday_momentum=0.0015,
            intraday_bar_return=0.001,
            volume_ratio=1.6,
        ),
    )

    assert result.ready is True
    assert result.reason == "pullback_entry"


def test_pullback_ready_respects_config_rsi_bounds() -> None:
    config = replace(_build_config(), pullback_rsi_low=40.0, pullback_rsi_high=60.0)

    blocked = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.2,
            rsi14=61.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )
    allowed = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.2,
            rsi14=59.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )

    assert blocked is False
    assert allowed is True


def test_pullback_ready_respects_config_distance() -> None:
    config = replace(
        _build_config(),
        pullback_distance_lower_pct=0.010,
        pullback_distance_upper_pct=0.005,
    )

    blocked = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=98.5,
            rsi14=50.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )
    allowed = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=99.2,
            rsi14=50.0,
            intraday_bar_return=0.002,
            volume_ratio=1.8,
        ),
    )

    assert blocked is False
    assert allowed is True


def test_pullback_ready_respects_config_volume() -> None:
    config = replace(_build_config(), pullback_min_volume_ratio=2.0)
    result = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.2,
            rsi14=50.0,
            intraday_bar_return=0.002,
            volume_ratio=1.5,
        ),
    )

    assert result is False
