from __future__ import annotations

from typing import Optional

from ..technical_signals import MovingAverageSnapshot
from .base import Position, StrategySignal


class VWAPPullbackStrategy:
    """
    VWAP pullback entry with simple target / VWAP break exits.
    """

    VWAP_TOLERANCE = 0.008
    TARGET_PCT = 0.020
    STOP_VWAP_PCT = 0.008

    def evaluate(
        self,
        snapshot: MovingAverageSnapshot,
        position: Optional[Position],
    ) -> StrategySignal:
        vwap = snapshot.vwap
        rsi = snapshot.rsi14

        if vwap is None or vwap <= 0:
            return StrategySignal()

        near_vwap = abs(snapshot.price - vwap) / vwap <= self.VWAP_TOLERANCE

        if position is None:
            rsi_ok = rsi is not None and 35.0 <= rsi <= 62.0
            if near_vwap and rsi_ok:
                return StrategySignal(
                    buy=True,
                    score=50.0 + (55.0 - rsi),
                    note="vwap_pullback",
                )
            return StrategySignal()

        gain = (snapshot.price - position.entry_price) / position.entry_price
        below_vwap = snapshot.price < vwap * (1 - self.STOP_VWAP_PCT)
        if gain >= self.TARGET_PCT or below_vwap:
            reason = "target_hit" if gain >= self.TARGET_PCT else "vwap_break"
            return StrategySignal(sell=True, note=reason)
        return StrategySignal()

    def is_watching(self, snapshot: MovingAverageSnapshot) -> bool:
        vwap = snapshot.vwap
        if vwap is None or vwap <= 0:
            return False
        near_vwap = abs(snapshot.price - vwap) / vwap <= self.VWAP_TOLERANCE * 2
        rsi_range = snapshot.rsi14 is not None and 30.0 <= snapshot.rsi14 <= 70.0
        return near_vwap or rsi_range
