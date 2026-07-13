from dataclasses import replace
from pathlib import Path

from kinvest_trade.config import load_app_config
from kinvest_trade.indicators import compute_rsi
from kinvest_trade.momentum_policy import (
    detect_market_regime,
    _pullback_ready,
    evaluate_entry_setup,
    evaluate_exit_setup,
)
from kinvest_trade.technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    compute_vwap,
)


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
        0.01,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "time_exit_profit"


def test_time_exit_profit_does_not_fire_below_commission_floor() -> None:
    # Regression test: a position flat at ~0% gross pnl past max_hold_cycles used
    # to be flagged "sell, time_exit_profit" here and then get rejected downstream
    # (net_profit_below_cost) on every single cycle once fees were netted out —
    # an order-then-block pattern. The exit decision itself must now require
    # enough gross pnl to clear the round-trip commission floor before treating
    # the position as sellable, so it never gets proposed as a sell in the first
    # place when it wouldn't clear costs.
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(intraday_momentum=-0.0001),
        0.0005,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason != "time_exit_profit"


def test_momentum_loss_cut_requires_two_of_three_conditions() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=99.8,
            minute_ma_slow=100.0,
            intraday_momentum=0.001,
            intraday_bar_return=0.001,
            atr_pct=0.001,
        ),
        -0.006,
        drawdown_from_peak=0.0,
        hold_cycles=1,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason == "hold"


def test_momentum_loss_cut_triggers_when_two_conditions_align() -> None:
    result = evaluate_exit_setup(
        _build_config(),
        _snapshot(
            price=99.8,
            minute_ma_slow=100.0,
            intraday_momentum=-0.001,
            intraday_bar_return=0.001,
            atr_pct=0.001,
        ),
        -0.006,
        drawdown_from_peak=0.0,
        hold_cycles=1,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "momentum_loss_cut"


def test_marginal_profit_exit_triggers() -> None:
    result = evaluate_exit_setup(
        replace(_build_config(), max_hold_cycles=100),
        _snapshot(
            volume_ratio=0.7,
            intraday_momentum=-0.0002,
        ),
        0.012,
        drawdown_from_peak=0.0,
        hold_cycles=30,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "marginal_profit_exit"


def test_trend_filter_lost_deferred_when_pullback_still_valid() -> None:
    config = replace(
        _build_config(),
        trend_require_price_above_slow=False,
    )
    snapshot = _snapshot(
        price=100.82,
        daily_ma_fast=100.0,
        daily_ma_slow=100.5,
        minute_ma_fast=101.0,
        minute_ma_slow=100.8,
        intraday_momentum=-0.001,
        intraday_bar_return=0.0004,
        volume_ratio=1.4,
        rsi14=55.0,
        breakout_level=101.2,
        breakout_distance_pct=-0.00375,
        minute_gap_slow_pct=0.0002,
        fast_above_slow=True,
    )

    assert _pullback_ready(config, snapshot) is True

    result = evaluate_exit_setup(
        config,
        snapshot,
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=1,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason == "hold"


def test_trend_filter_lost_suppressed_when_hold_cycles_low() -> None:
    config = replace(_build_config(), min_hold_before_trend_exit=3)
    result = evaluate_exit_setup(
        config,
        _snapshot(
            price=99.7,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.6,
            minute_ma_slow=100.0,
            intraday_momentum=-0.001,
            intraday_bar_return=-0.0002,
            volume_ratio=1.1,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=2,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"
    assert result.reason == "hold"


def test_trend_filter_lost_fires_after_min_hold_cycles() -> None:
    config = replace(_build_config(), min_hold_before_trend_exit=3)
    result = evaluate_exit_setup(
        config,
        _snapshot(
            price=99.7,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.6,
            minute_ma_slow=100.0,
            intraday_momentum=-0.001,
            intraday_bar_return=-0.0002,
            volume_ratio=1.1,
        ),
        -0.003,
        drawdown_from_peak=0.0,
        hold_cycles=4,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "trend_filter_lost"


def test_hard_stop_still_fires_during_hold_protection() -> None:
    config = replace(_build_config(), min_hold_before_trend_exit=3)
    result = evaluate_exit_setup(
        config,
        _snapshot(
            price=98.0,
            daily_ma_fast=98.5,
            daily_ma_slow=100.0,
            minute_ma_fast=99.0,
            minute_ma_slow=100.0,
            intraday_momentum=-0.001,
            intraday_bar_return=-0.001,
            atr_pct=0.004,
        ),
        -0.02,
        drawdown_from_peak=0.0,
        hold_cycles=1,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "sell"
    assert result.reason == "atr_hard_stop"


def test_marginal_profit_exit_requires_minimum_pnl() -> None:
    config = replace(_build_config(), max_hold_cycles=100)
    low_profit = evaluate_exit_setup(
        config,
        _snapshot(volume_ratio=0.7, intraday_momentum=-0.0002),
        0.008,
        drawdown_from_peak=0.0,
        hold_cycles=10,
        position_qty=1,
        partial_exit_done=False,
    )
    enough_profit = evaluate_exit_setup(
        config,
        _snapshot(volume_ratio=0.7, intraday_momentum=-0.0002),
        0.012,
        drawdown_from_peak=0.0,
        hold_cycles=30,
        position_qty=1,
        partial_exit_done=False,
    )

    assert low_profit.action == "hold"
    assert enough_profit.reason == "marginal_profit_exit"


def test_marginal_profit_exit_requires_min_hold_cycles() -> None:
    config = _build_config()
    result = evaluate_exit_setup(
        config,
        _snapshot(volume_ratio=0.7, intraday_momentum=-0.0002),
        0.012,
        drawdown_from_peak=0.0,
        hold_cycles=5,
        position_qty=1,
        partial_exit_done=False,
    )

    assert result.action == "hold"


def test_marginal_profit_exit_respects_commission_floor() -> None:
    config = replace(
        _build_config(),
        max_hold_cycles=100,
        take_profit_pct=0.009,
        commission_rate=0.0025,
    )
    below_floor = evaluate_exit_setup(
        config,
        _snapshot(volume_ratio=0.7, intraday_momentum=-0.0002),
        0.0075,
        drawdown_from_peak=0.0,
        hold_cycles=30,
        position_qty=1,
        partial_exit_done=False,
    )
    above_floor = evaluate_exit_setup(
        config,
        _snapshot(volume_ratio=0.7, intraday_momentum=-0.0002),
        0.0085,
        drawdown_from_peak=0.0,
        hold_cycles=30,
        position_qty=1,
        partial_exit_done=False,
    )

    assert below_floor.action == "hold"
    assert above_floor.reason == "marginal_profit_exit"


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
        pullback_rsi_high=60.0,
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
            rsi14=76.0,
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


def test_pullback_ready_with_relaxed_rsi() -> None:
    config = replace(_build_config(), pullback_rsi_high=70.0)
    result = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.4,
            rsi14=67.0,
            intraday_bar_return=0.002,
            volume_ratio=1.0,
        ),
    )

    assert result is True


def test_pullback_ready_with_wider_distance() -> None:
    config = replace(_build_config(), pullback_distance_upper_pct=0.012)
    result = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=101.0,
            rsi14=55.0,
            intraday_bar_return=0.002,
            volume_ratio=1.0,
        ),
    )

    assert result is True


def test_pullback_ready_with_low_volume() -> None:
    config = replace(_build_config(), pullback_min_volume_ratio=0.8)
    result = _pullback_ready(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.4,
            rsi14=55.0,
            intraday_bar_return=0.002,
            volume_ratio=0.9,
        ),
    )

    assert result is True


def test_evaluate_entry_uses_prefilter_factor() -> None:
    config = replace(
        _build_config(),
        volume_spike_ratio=1.5,
        volume_spike_ratio_prefilter_factor=0.5,
        pullback_min_volume_ratio=0.8,
    )
    result = evaluate_entry_setup(
        config,
        _snapshot(
            minute_ma_fast=100.0,
            minute_ma_slow=98.0,
            price=100.3,
            rsi14=55.0,
            intraday_momentum=0.0015,
            intraday_bar_return=0.001,
            volume_ratio=0.8,
            breakout_level=100.8,
            breakout_distance_pct=-0.0049,
        ),
    )

    assert result.reason != "volume_low"


def test_inverse_etf_enters_when_daily_trend_down() -> None:
    config = _build_config()
    result = evaluate_entry_setup(
        config,
        _snapshot(
            daily_ma_fast=98.0,
            daily_ma_slow=100.0,
            minute_ma_fast=100.0,
            minute_ma_slow=99.0,
            price=100.8,
            rsi14=54.0,
            intraday_bar_return=0.0012,
            volume_ratio=1.3,
        ),
        symbol="SQQQ",
        inverse_etf_symbols=["SQQQ", "SOXS", "UVXY", "SPXU"],
        leveraged_etf_symbols=["TQQQ", "SOXL"],
    )

    assert result.ready is True


def test_normal_stock_blocked_when_daily_trend_down() -> None:
    result = evaluate_entry_setup(
        _build_config(),
        _snapshot(
            daily_ma_fast=98.0,
            daily_ma_slow=100.0,
            minute_ma_fast=100.0,
            minute_ma_slow=99.0,
            price=100.8,
            rsi14=74.0,
            intraday_bar_return=0.0012,
            volume_ratio=1.3,
        ),
        symbol="NVDA",
        inverse_etf_symbols=["SQQQ", "SOXS", "UVXY", "SPXU"],
        leveraged_etf_symbols=["TQQQ", "SOXL"],
    )

    assert result.ready is False
    assert result.reason == "trend_down"


def test_rsi_period_7_in_snapshot() -> None:
    minute_closes = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101]
    snapshot = build_moving_average_snapshot(
        price=110.0,
        bid=109.9,
        ask=110.1,
        daily_closes=[130, 129, 128, 127, 126, 125, 124, 123, 122, 121, 120, 119, 118, 117, 116, 115, 114, 113, 112, 111, 110],
        minute_closes=minute_closes,
        minute_highs=minute_closes,
        minute_lows=minute_closes,
        minute_volumes=[1000.0] * len(minute_closes),
        daily_fast_window=5,
        daily_slow_window=10,
        intraday_fast_window=3,
        intraday_slow_window=5,
        volatility_window=3,
        momentum_window=3,
        volume_window=5,
        rsi_period=7,
        breakout_lookback_bars=3,
        bollinger_window=3,
        bollinger_stddev=2.0,
        atr_window=3,
    )

    assert snapshot.rsi14 == compute_rsi(minute_closes, 7)


def test_vwap_computed() -> None:
    assert compute_vwap([100.0, 102.0, 101.0], [1000.0, 2000.0, 1500.0]) == 455500.0 / 4500.0


def test_vwap_uses_typical_price_when_high_low_available() -> None:
    typical = compute_vwap(
        [100.0, 102.0, 98.0],
        [1000.0, 2000.0, 1500.0],
        [103.0, 105.0, 101.0],
        [98.0, 100.0, 96.0],
    )
    close_only = compute_vwap([100.0, 102.0, 98.0], [1000.0, 2000.0, 1500.0])

    assert typical is not None
    assert close_only is not None
    assert typical != close_only


def test_detect_market_regime() -> None:
    assert detect_market_regime(_snapshot(price=100.4, minute_ma_slow=100.2)) == "bull"
    assert detect_market_regime(
        _snapshot(
            daily_ma_fast=98.0,
            daily_ma_slow=100.0,
            minute_ma_fast=99.0,
            minute_ma_slow=100.0,
            price=98.5,
        )
    ) == "bear"
