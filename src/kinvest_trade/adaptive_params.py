from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .config import AutoTradeConfig
from .technical_signals import MovingAverageSnapshot


@dataclass(slots=True)
class AdaptiveOverride:
    """Per-cycle overrides computed from the latest market snapshot."""

    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    hard_stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_cycles: int | None = None
    volume_spike_ratio: float | None = None
    min_intraday_momentum_pct: float | None = None


def compute_adaptive_override(
    config: AutoTradeConfig,
    snapshot: MovingAverageSnapshot,
) -> AdaptiveOverride:
    """Derive dynamic trading thresholds from volatility, flow, and momentum."""

    atr_pct = snapshot.atr_pct
    volume_ratio = snapshot.volume_ratio
    momentum = snapshot.intraday_momentum

    take_profit = None
    stop_loss = None
    hard_stop = None
    trailing = None
    max_hold = None
    spike_ratio = None
    min_momentum = None

    high_vol = atr_pct > config.stop_loss_pct * 1.5
    low_vol = atr_pct < config.stop_loss_pct * 0.5
    strong_flow = volume_ratio >= 3.0
    strong_trend = momentum >= config.min_intraday_momentum_pct * 3.0
    reverse_trend = momentum <= 0.0

    if high_vol:
        take_profit = atr_pct * 1.5
        stop_loss = atr_pct * 0.8
        hard_stop = atr_pct * 1.6
        trailing = atr_pct * 0.6
    elif low_vol:
        take_profit = config.take_profit_pct * 0.7

    if strong_flow:
        spike_ratio = config.volume_spike_ratio * 0.8

    if strong_trend:
        max_hold = max(1, int(config.max_hold_cycles * 1.5))
    elif reverse_trend:
        max_hold = max(1, int(config.max_hold_cycles * 0.5))

    if high_vol and strong_flow:
        min_momentum = config.min_intraday_momentum_pct * 0.5

    return AdaptiveOverride(
        take_profit_pct=take_profit,
        stop_loss_pct=stop_loss,
        hard_stop_loss_pct=hard_stop,
        trailing_stop_pct=trailing,
        max_hold_cycles=max_hold,
        volume_spike_ratio=spike_ratio,
        min_intraday_momentum_pct=min_momentum,
    )


def apply_override(config: AutoTradeConfig, override: AdaptiveOverride) -> AutoTradeConfig:
    """Apply non-null adaptive overrides to a copied AutoTradeConfig."""

    changes: dict[str, float | int] = {}
    if override.take_profit_pct is not None:
        changes["take_profit_pct"] = override.take_profit_pct
    if override.stop_loss_pct is not None:
        changes["stop_loss_pct"] = override.stop_loss_pct
    if override.hard_stop_loss_pct is not None:
        changes["hard_stop_loss_pct"] = override.hard_stop_loss_pct
    if override.trailing_stop_pct is not None:
        changes["trailing_stop_pct"] = override.trailing_stop_pct
    if override.max_hold_cycles is not None:
        changes["max_hold_cycles"] = override.max_hold_cycles
    if override.volume_spike_ratio is not None:
        changes["volume_spike_ratio"] = override.volume_spike_ratio
    if override.min_intraday_momentum_pct is not None:
        changes["min_intraday_momentum_pct"] = override.min_intraday_momentum_pct
    if not changes:
        return config
    return dataclasses.replace(config, **changes)
