from __future__ import annotations

from ..momentum_policy import evaluate_entry_setup
from ..technical_signals import MovingAverageSnapshot
from .base import Position, StrategyID, StrategySignal


class MomentumContinuationStrategy:
    """Explicit fallback for a confirmed setup with no legacy strategy entry."""

    def __init__(self, config: object | None) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: MovingAverageSnapshot,
        position: Position | None,
    ) -> StrategySignal:
        config = self.config
        if config is None:
            return StrategySignal()
        if position is not None:
            if StrategyID.MOMENTUM not in position.triggered_by:
                return StrategySignal()
            gain = snapshot.price / position.entry_price - 1
            if gain <= -config.hard_stop_loss_pct or gain >= config.take_profit_pct:
                return StrategySignal(sell=True, note="momentum_price_exit")
            return StrategySignal()
        if not getattr(config, "entry_momentum_fallback_enabled", False):
            return StrategySignal()
        if not (
            snapshot.intraday_trend_up
            and snapshot.vwap is not None
            and snapshot.vwap > 0
            and snapshot.price >= snapshot.vwap
            and snapshot.intraday_momentum >= config.min_intraday_momentum_pct
            and snapshot.intraday_bar_return >= config.min_bar_return_pct
            and snapshot.breakout_distance_pct <= config.max_breakout_extension_pct
        ):
            return StrategySignal()
        setup = evaluate_entry_setup(config, snapshot)
        return StrategySignal(buy=setup.ready, score=setup.score, note=setup.reason)
