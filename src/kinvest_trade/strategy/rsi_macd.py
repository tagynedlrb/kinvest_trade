from __future__ import annotations

from typing import Optional

from ..technical_signals import MovingAverageSnapshot
from .base import Position, StrategySignal


class RSIMACDStrategy:
    """
    RSI plus MACD momentum confirmation.
    """

    RSI_BUY = 50.0
    RSI_SELL = 70.0

    def evaluate(
        self,
        snapshot: MovingAverageSnapshot,
        position: Optional[Position],
    ) -> StrategySignal:
        rsi = snapshot.rsi14

        if position is None:
            rsi_ok = rsi is not None and rsi <= self.RSI_BUY
            if snapshot.macd_golden and rsi_ok:
                return StrategySignal(buy=True, score=40.0, note="macd_golden")
            return StrategySignal()

        rsi_sell = rsi is not None and rsi >= self.RSI_SELL
        if snapshot.macd_dead or rsi_sell:
            reason = "macd_dead" if snapshot.macd_dead else "rsi_overbought"
            return StrategySignal(sell=True, note=reason)
        return StrategySignal()
