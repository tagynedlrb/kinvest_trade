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
    symbol: str = "",
    inverse_etf_symbols: list[str] | None = None,
    leveraged_etf_symbols: list[str] | None = None,
) -> EntrySetup:
    if snapshot.spread_pct > config.max_spread_pct:
        return EntrySetup(False, "spread_too_wide", "SKIP", "spread")
    if not snapshot.has_required_context:
        return EntrySetup(False, "warmup_context", "WARMUP", "warmup")
    inverse_symbols = inverse_etf_symbols or getattr(config, "inverse_etf_symbols", [])
    leveraged_symbols = leveraged_etf_symbols or getattr(config, "leveraged_etf_symbols", [])
    symbol_upper = symbol.upper()
    is_inverse = symbol_upper in {value.upper() for value in inverse_symbols}
    is_leveraged = symbol_upper in {value.upper() for value in leveraged_symbols}
    effective_rsi_max = config.max_entry_rsi14
    if is_inverse or is_leveraged:
        effective_rsi_max = min(config.max_entry_rsi14 + 15.0, 90.0)
    if snapshot.rsi14 is not None and snapshot.rsi14 > effective_rsi_max:
        return EntrySetup(False, "entry_rsi_too_high", "SKIP", _note(snapshot))
    prefilter_factor = max(config.volume_spike_ratio_prefilter_factor, 0.0)
    if snapshot.volume_ratio < config.volume_spike_ratio * prefilter_factor:
        return EntrySetup(False, "volume_low", "WAIT", _note(snapshot))

    if is_inverse or is_leveraged:
        if not (
            snapshot.minute_ma_fast is not None
            and snapshot.minute_ma_slow is not None
            and snapshot.minute_ma_fast >= snapshot.minute_ma_slow
        ):
            return EntrySetup(False, "trend_down", "WAIT", _note(snapshot))
        fast_track = (
            snapshot.volume_ratio >= config.volume_spike_ratio * 1.5
            and snapshot.intraday_bar_return >= config.min_bar_return_pct * 2.0
        )
        if fast_track:
            score = min(snapshot.volume_ratio, 5.0) * 30.0
            return EntrySetup(
                True,
                "volume_momentum_fast_entry",
                "BUY",
                _note(snapshot),
                score=round(score, 2),
                urgent=True,
            )
        if _pullback_ready(config, snapshot):
            score = min(snapshot.volume_ratio, 4.0) * 25.0
            return EntrySetup(
                True,
                "pullback_entry",
                "BUY",
                _note(snapshot),
                score=round(score, 2),
                urgent=False,
            )
        return EntrySetup(False, "setup_not_ready", "WAIT", _note(snapshot))

    if not snapshot.daily_trend_up:
        return EntrySetup(False, "trend_down", "WAIT", _note(snapshot))

    fast_track = (
        snapshot.volume_ratio >= config.volume_spike_ratio * 2.0
        and snapshot.intraday_bar_return >= config.min_bar_return_pct * 3.0
    )
    if fast_track:
        score = (
            min(snapshot.volume_ratio, 5.0) * 30.0
            + max(snapshot.intraday_bar_return, 0.0) * 5000.0
        )
        return EntrySetup(
            True,
            "volume_momentum_fast_entry",
            "BUY",
            _note(snapshot),
            score=round(score, 2),
            urgent=True,
        )

    if _pullback_ready(config, snapshot):
        score = (
            min(snapshot.volume_ratio, 4.0) * 25.0
            + max(snapshot.intraday_momentum, 0.0) * 5000.0
            + max(snapshot.intraday_bar_return, 0.0) * 4000.0
        )
        return EntrySetup(
            True,
            "pullback_entry",
            "BUY",
            _note(snapshot),
            score=round(score, 2),
            urgent=False,
        )

    if snapshot.volume_ratio < config.volume_spike_ratio:
        return EntrySetup(False, "volume_low", "WAIT", _note(snapshot))

    extension_too_large = (
        snapshot.breakout_distance_pct > config.max_breakout_extension_pct
        if snapshot.breakout_distance_pct > 0
        else False
    )
    if extension_too_large:
        return EntrySetup(False, "chasing", "SKIP", _note(snapshot))

    if not _trend_filter_ok_adaptive(config, snapshot):
        return EntrySetup(False, "trend_down", "WAIT", _note(snapshot))

    momentum_ready = (
        snapshot.intraday_momentum >= config.min_intraday_momentum_pct
        or snapshot.intraday_bar_return >= config.min_bar_return_pct
    )
    if not momentum_ready:
        return EntrySetup(False, "momentum_weak", "WAIT", _note(snapshot))

    breakout_ready = _breakout_ready(config, snapshot)
    band_breakout_ready = _band_breakout_ready(config, snapshot)
    proximity_ready = _breakout_proximity_ready(config, snapshot)
    if not (breakout_ready or band_breakout_ready or proximity_ready):
        return EntrySetup(False, "near_breakout", "READY", _note(snapshot))

    score = (
        min(snapshot.volume_ratio, 4.0) * 25.0
        + max(snapshot.intraday_momentum, 0.0) * 5000.0
        + max(snapshot.intraday_bar_return, 0.0) * 4000.0
        + max((snapshot.rsi14 or 50.0) - 45.0, 0.0)
    )
    urgent = snapshot.volume_ratio >= (config.volume_spike_ratio * 1.5)
    if breakout_ready:
        reason = "volume_breakout_entry"
    elif band_breakout_ready:
        reason = "band_breakout_entry"
    else:
        reason = "breakout_proximity_entry"
    return EntrySetup(True, reason, "BUY", _note(snapshot), score=round(score, 2), urgent=urgent)


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
    if pnl_pct <= -soft_stop:
        price_below_ma = snapshot.price < (snapshot.minute_ma_slow or snapshot.price + 1.0)
        momentum_negative = snapshot.intraday_momentum < 0
        bar_negative = snapshot.intraday_bar_return < 0
        if sum([price_below_ma, momentum_negative, bar_negative]) >= 2:
            return ExitSetup("sell", "momentum_loss_cut", "SELL_READY", note)
    if pnl_pct < 0 and not trend_filter_ok(snapshot) and snapshot.intraday_momentum <= 0:
        if not _pullback_ready(config, snapshot):
            if hold_cycles >= config.min_hold_before_trend_exit:
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
    marginal_threshold = config.take_profit_pct * 0.7
    min_hold_ok = hold_cycles >= getattr(config, "min_hold_before_marginal_exit", 10)
    small_profit = marginal_threshold <= pnl_pct < config.take_profit_pct
    volume_fading = snapshot.volume_ratio <= (config.volume_fade_ratio or 0.8)
    momentum_fading = snapshot.intraday_momentum <= 0
    if small_profit and volume_fading and momentum_fading and min_hold_ok:
        return ExitSetup("sell", "marginal_profit_exit", "SELL_READY", note)

    if hold_cycles >= config.max_hold_cycles:
        if pnl_pct >= 0 and snapshot.intraday_momentum <= 0:
            return ExitSetup("sell", "time_exit_profit", "SELL_READY", note)
        if pnl_pct < 0:
            trend_lost = not trend_filter_ok(snapshot)
            momentum_gone = snapshot.intraday_momentum <= 0
            volume_dying = snapshot.volume_ratio <= (config.volume_fade_ratio or 0.8)
            conditions_met = sum([trend_lost, momentum_gone, volume_dying])
            if conditions_met >= 2:
                return ExitSetup("sell", "time_exit_loss", "SELL_READY", note)
            if hold_cycles >= int(config.max_hold_cycles * 1.5):
                return ExitSetup("sell", "time_exit_forced", "SELL_READY", note)

    if snapshot.volume_ratio >= config.volume_spike_ratio and trend_filter_ok(snapshot):
        return ExitSetup("hold", "trend_holding", "HOLD", note)
    return ExitSetup("hold", "hold", "HOLD", note)


def derive_watch_state(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
    symbol: str = "",
    inverse_etf_symbols: list[str] | None = None,
    leveraged_etf_symbols: list[str] | None = None,
) -> tuple[str, str]:
    entry = evaluate_entry_setup(
        config,
        snapshot,
        symbol=symbol,
        inverse_etf_symbols=inverse_etf_symbols,
        leveraged_etf_symbols=leveraged_etf_symbols,
    )
    return entry.state, entry.reason


def detect_market_regime(snapshot: MovingAverageSnapshot) -> str:
    if snapshot.daily_trend_up and snapshot.intraday_trend_up:
        return "bull"
    if not snapshot.daily_trend_up and not snapshot.intraday_trend_up:
        return "bear"
    return "neutral"


def trend_filter_ok(snapshot: MovingAverageSnapshot) -> bool:
    return snapshot.daily_trend_up and snapshot.intraday_trend_up


def _trend_filter_ok_adaptive(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> bool:
    if config.trend_require_price_above_slow:
        return trend_filter_ok(snapshot)

    daily_ok = bool(
        snapshot.daily_ma_fast is not None
        and snapshot.daily_ma_slow is not None
        and snapshot.daily_ma_fast >= snapshot.daily_ma_slow
    )
    intraday_ok = bool(
        snapshot.minute_ma_fast is not None
        and snapshot.minute_ma_slow is not None
        and snapshot.minute_ma_fast >= snapshot.minute_ma_slow
    )
    return daily_ok and intraday_ok


def _pullback_ready(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> bool:
    fast = snapshot.minute_ma_fast
    slow = snapshot.minute_ma_slow
    if fast is None or slow is None or fast <= 0 or fast < slow:
        return False

    distance_pct = (snapshot.price - fast) / fast
    if not (
        -config.pullback_distance_lower_pct
        <= distance_pct
        <= config.pullback_distance_upper_pct
    ):
        return False

    rsi = snapshot.rsi14
    if rsi is not None and not (config.pullback_rsi_low <= rsi <= config.pullback_rsi_high):
        return False

    if snapshot.intraday_bar_return < 0:
        return False
    # Pullbacks usually form on lighter volume, so only reject when activity
    # falls below the relaxed floor configured for this setup.
    if config.pullback_min_volume_ratio > 0:
        if snapshot.volume_ratio < config.pullback_min_volume_ratio:
            return False
    return True


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


def _breakout_proximity_ready(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> bool:
    if snapshot.breakout_level is None or snapshot.breakout_level <= 0:
        return False
    proximity_threshold = snapshot.breakout_level * config.breakout_proximity_pct
    return snapshot.price >= proximity_threshold


def _note(snapshot: MovingAverageSnapshot) -> str:
    return (
        f"vr={snapshot.volume_ratio:.1f}x "
        f"mom={snapshot.intraday_momentum * 100:+.2f}%"
    )
