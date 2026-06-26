from __future__ import annotations

from dataclasses import dataclass

from .config import AutoTradeConfig
from .technical_signals import MovingAverageSnapshot


@dataclass(slots=True)
class EntrySetup:
    ready: bool
    reason: str
    state: str
    note: str
    score: float = 0.0
    urgent: bool = False


@dataclass(slots=True)
class ExitSetup:
    action: str
    reason: str
    state: str
    note: str


def evaluate_entry_setup(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> EntrySetup:
    if snapshot.spread_pct > config.max_spread_pct:
        return EntrySetup(False, "spread_too_wide", "SPREAD", "spread")
    if not snapshot.has_required_context:
        return EntrySetup(False, "building_signal_context", "WARMUP", "warmup")
    if not trend_filter_ok(snapshot):
        return EntrySetup(False, "trend_filter_off", "FILTER", _note(snapshot))
    if snapshot.rsi14 is not None and snapshot.rsi14 > config.max_entry_rsi14:
        return EntrySetup(False, "entry_rsi_too_high", "OVERHEAT", _note(snapshot))

    volume_ready = snapshot.volume_ratio >= config.volume_spike_ratio
    momentum_ready = (
        snapshot.intraday_momentum >= config.min_intraday_momentum_pct
        or snapshot.intraday_bar_return >= config.min_bar_return_pct
    )
    breakout_ready = _breakout_ready(config, snapshot)
    band_breakout_ready = _band_breakout_ready(config, snapshot)
    extension_too_large = (
        snapshot.breakout_distance_pct > config.max_breakout_extension_pct
        if snapshot.breakout_distance_pct > 0
        else False
    )

    if extension_too_large:
        return EntrySetup(False, "breakout_too_extended", "CHASE", _note(snapshot))
    if not volume_ready:
        return EntrySetup(False, "volume_not_expanded", "SETUP", _note(snapshot))
    if not momentum_ready:
        return EntrySetup(False, "momentum_not_ready", "SETUP", _note(snapshot))

    fast_track = (
        snapshot.volume_ratio >= config.volume_spike_ratio * 2.0
        and snapshot.intraday_bar_return >= config.min_bar_return_pct * 2.0
    )
    if fast_track:
        score = (
            min(snapshot.volume_ratio, 5.0) * 30.0
            + max(snapshot.intraday_bar_return, 0.0) * 5000.0
        )
        return EntrySetup(
            True,
            "volume_momentum_fast_entry",
            "BUY_READY",
            _note(snapshot),
            score=round(score, 2),
            urgent=True,
        )

    if not (breakout_ready or band_breakout_ready):
        state = "SPIKE" if volume_ready else "SETUP"
        return EntrySetup(False, "no_breakout_signal", state, _note(snapshot))

    score = (
        min(snapshot.volume_ratio, 4.0) * 25.0
        + max(snapshot.intraday_momentum, 0.0) * 5000.0
        + max(snapshot.intraday_bar_return, 0.0) * 4000.0
        + max((snapshot.rsi14 or 50.0) - 45.0, 0.0)
    )
    urgent = (
        snapshot.volume_ratio >= (config.volume_spike_ratio * 1.5)
        or snapshot.intraday_bar_return >= (config.min_bar_return_pct * 2.0)
    )
    reason = "volume_breakout_entry" if breakout_ready else "band_breakout_entry"
    return EntrySetup(True, reason, "BUY_READY", _note(snapshot), score=round(score, 2), urgent=urgent)


def evaluate_scale_in_setup(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
    *,
    pnl_pct: float,
    position_qty: int,
    partial_exit_done: bool,
) -> EntrySetup:
    if not config.allow_scale_in:
        return EntrySetup(False, "scale_in_disabled", "HOLD", _note(snapshot))
    if position_qty <= 0:
        return EntrySetup(False, "no_position", "HOLD", _note(snapshot))
    if pnl_pct < config.scale_in_profit_trigger_pct:
        return EntrySetup(False, "scale_in_profit_not_ready", "HOLD", _note(snapshot))
    if partial_exit_done:
        return EntrySetup(False, "scale_in_after_partial_exit_blocked", "HOLD", _note(snapshot))
    if not trend_filter_ok(snapshot):
        return EntrySetup(False, "scale_in_trend_filter_off", "HOLD", _note(snapshot))
    if snapshot.volume_ratio < config.scale_in_volume_ratio:
        return EntrySetup(False, "scale_in_volume_too_low", "HOLD", _note(snapshot))
    if not (_breakout_ready(config, snapshot) or _band_breakout_ready(config, snapshot)):
        return EntrySetup(False, "scale_in_no_breakout", "HOLD", _note(snapshot))
    if snapshot.breakout_distance_pct > config.max_breakout_extension_pct:
        return EntrySetup(False, "scale_in_breakout_too_extended", "HOLD", _note(snapshot))

    score = (
        min(snapshot.volume_ratio, 4.0) * 20.0
        + max(snapshot.intraday_momentum, 0.0) * 4000.0
        + (pnl_pct * 2000.0)
    )
    return EntrySetup(
        True,
        "momentum_scale_in",
        "BUY_READY",
        _note(snapshot),
        score=round(score, 2),
        urgent=False,
    )


def evaluate_exit_setup(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
    pnl_pct: float,
    *,
    drawdown_from_peak: float,
    hold_cycles: int,
    position_qty: int,
    partial_exit_done: bool,
) -> ExitSetup:
    if not snapshot.has_required_context:
        return ExitSetup("hold", "building_signal_context", "WARMUP", "warmup")

    hard_stop = max(config.hard_stop_loss_pct, snapshot.atr_pct * config.atr_hard_stop_multiplier)
    soft_stop = max(config.stop_loss_pct, snapshot.atr_pct * config.atr_soft_stop_multiplier)
    trailing_stop = max(
        config.trailing_stop_pct,
        snapshot.atr_pct * config.atr_trailing_stop_multiplier,
    )
    note = _note(snapshot)

    if pnl_pct <= -hard_stop:
        return ExitSetup("sell", "atr_hard_stop", "SELL_READY", note)
    if pnl_pct <= -soft_stop and (
        snapshot.price < (snapshot.minute_ma_slow or snapshot.price + 1.0)
        or snapshot.intraday_momentum < 0
        or snapshot.intraday_bar_return < 0
    ):
        return ExitSetup("sell", "momentum_loss_cut", "SELL_READY", note)
    if pnl_pct < 0 and not trend_filter_ok(snapshot) and snapshot.intraday_momentum <= 0:
        return ExitSetup("sell", "trend_filter_lost", "SELL_READY", note)

    if (
        config.allow_partial_exit
        and position_qty > 1
        and not partial_exit_done
        and pnl_pct >= config.take_profit_pct
        and (
            (snapshot.rsi14 is not None and snapshot.rsi14 >= config.partial_exit_rsi14)
            or snapshot.volume_ratio <= config.volume_fade_ratio
            or drawdown_from_peak <= -(trailing_stop * 0.5)
        )
    ):
        return ExitSetup("sell_partial", "partial_profit_lock", "SELL_READY", note)

    if pnl_pct >= config.full_take_profit_pct and (
        drawdown_from_peak <= -trailing_stop
        or snapshot.volume_ratio <= config.volume_fade_ratio
        or snapshot.price < (snapshot.minute_ma_fast or snapshot.price)
        or (snapshot.rsi14 is not None and snapshot.rsi14 >= config.partial_exit_rsi14 + 4.0)
    ):
        return ExitSetup("sell", "breakout_exhaustion_exit", "SELL_READY", note)

    if hold_cycles >= config.max_hold_cycles and pnl_pct > 0 and snapshot.intraday_momentum <= 0:
        return ExitSetup("sell", "time_exit", "SELL_READY", note)

    if snapshot.volume_ratio >= config.volume_spike_ratio and trend_filter_ok(snapshot):
        return ExitSetup("hold", "trend_holding", "HOLD", note)
    return ExitSetup("hold", "hold", "HOLD", note)


def derive_watch_state(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> tuple[str, str]:
    entry = evaluate_entry_setup(config, snapshot)
    if entry.ready:
        return entry.state, entry.reason
    return entry.state, entry.note if entry.reason == "trend_filter_off" else entry.reason


def trend_filter_ok(snapshot: MovingAverageSnapshot) -> bool:
    return snapshot.daily_trend_up and snapshot.intraday_trend_up


def _breakout_ready(config: AutoTradeConfig, snapshot: MovingAverageSnapshot) -> bool:
    if snapshot.breakout_level is None or snapshot.breakout_level <= 0:
        return False
    threshold = snapshot.breakout_level * (1.0 + config.breakout_entry_pct)
    return snapshot.price >= threshold


def _band_breakout_ready(config: AutoTradeConfig, snapshot: MovingAverageSnapshot) -> bool:
    if snapshot.bollinger_upper is None or snapshot.bollinger_upper <= 0:
        return False
    threshold = snapshot.bollinger_upper * (1.0 + config.bollinger_breakout_buffer_pct)
    return snapshot.price >= threshold


def _note(snapshot: MovingAverageSnapshot) -> str:
    return (
        f"vr={snapshot.volume_ratio:.1f}x "
        f"mom={snapshot.intraday_momentum * 100:+.2f}%"
    )
