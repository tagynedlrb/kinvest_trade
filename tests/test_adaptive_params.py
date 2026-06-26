from dataclasses import replace
from pathlib import Path

from kinvest_trade.adaptive_params import apply_override, compute_adaptive_override
from kinvest_trade.config import load_app_config
from kinvest_trade.technical_signals import MovingAverageSnapshot


def _build_config():
    project_root = Path(__file__).resolve().parents[1]
    return load_app_config(project_root / "config" / "fixed_config.json").auto_trade


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
        atr=0.3,
        atr_pct=0.003,
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


def test_high_volatility_override_uses_atr_based_targets() -> None:
    config = replace(_build_config(), stop_loss_pct=0.003)
    snapshot = _snapshot(atr_pct=config.stop_loss_pct * 2.0)

    override = compute_adaptive_override(config, snapshot)

    assert override.take_profit_pct == snapshot.atr_pct * 1.5
    assert override.stop_loss_pct == snapshot.atr_pct * 0.8


def test_strong_flow_override_relaxes_volume_spike_ratio() -> None:
    config = _build_config()
    snapshot = _snapshot(volume_ratio=4.0)

    override = compute_adaptive_override(config, snapshot)

    assert override.volume_spike_ratio == config.volume_spike_ratio * 0.8


def test_reverse_momentum_override_shortens_max_hold_cycles() -> None:
    config = _build_config()
    snapshot = _snapshot(intraday_momentum=-0.01)

    override = compute_adaptive_override(config, snapshot)

    assert override.max_hold_cycles == int(config.max_hold_cycles * 0.5)


def test_high_volatility_and_strong_flow_relaxes_min_momentum_requirement() -> None:
    config = replace(_build_config(), stop_loss_pct=0.003)
    snapshot = _snapshot(
        atr_pct=config.stop_loss_pct * 2.0,
        volume_ratio=4.0,
    )

    override = compute_adaptive_override(config, snapshot)

    assert override.min_intraday_momentum_pct == config.min_intraday_momentum_pct * 0.5


def test_normal_case_returns_no_override() -> None:
    config = _build_config()
    snapshot = _snapshot(
        atr_pct=config.stop_loss_pct,
        volume_ratio=1.5,
        intraday_momentum=config.min_intraday_momentum_pct,
    )

    override = compute_adaptive_override(config, snapshot)

    assert override.take_profit_pct is None
    assert override.stop_loss_pct is None
    assert override.hard_stop_loss_pct is None
    assert override.trailing_stop_pct is None
    assert override.max_hold_cycles is None
    assert override.volume_spike_ratio is None
    assert override.min_intraday_momentum_pct is None


def test_apply_override_returns_replaced_dataclass() -> None:
    config = _build_config()
    snapshot = _snapshot(atr_pct=config.stop_loss_pct * 2.0, volume_ratio=4.0)

    override = compute_adaptive_override(config, snapshot)
    effective = apply_override(config, override)

    assert effective.take_profit_pct == snapshot.atr_pct * 1.5
    assert effective.volume_spike_ratio == config.volume_spike_ratio * 0.8
    assert effective is not config
