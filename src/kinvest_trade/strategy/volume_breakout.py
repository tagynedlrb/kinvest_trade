from __future__ import annotations

from typing import Optional

from ..technical_signals import MovingAverageSnapshot
from .base import Position, StrategySignal


class VolumeBreakoutStrategy:
    """
    Volume surge plus resistance breakout with trailing stop exit.
    """

    VOL_MULT = 3.0
    TRAIL_PCT = 0.015

    def evaluate(
        self,
        snapshot: MovingAverageSnapshot,
        position: Optional[Position],
    ) -> StrategySignal:
        if position is None:
            vol_ok = snapshot.volume_ratio >= self.VOL_MULT
            breakout_ok = snapshot.breakout_distance_pct >= 0
            if vol_ok and breakout_ok:
                score = min(snapshot.volume_ratio, 5.0) * 20.0
                return StrategySignal(buy=True, score=score, note="vol_breakout")
            return StrategySignal()

        trail_stop = position.peak_price * (1 - self.TRAIL_PCT)
        if snapshot.price <= trail_stop:
            return StrategySignal(sell=True, note="trail_stop")
        return StrategySignal()
