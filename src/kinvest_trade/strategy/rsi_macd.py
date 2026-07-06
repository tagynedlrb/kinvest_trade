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
            macd_ok = snapshot.macd_golden or (
                snapshot.macd_line is not None
                and snapshot.macd_signal is not None
                and snapshot.macd_line > snapshot.macd_signal
                and snapshot.macd_line > 0
            )
            if macd_ok and rsi_ok:
                score = 40.0 + max(0.0, 50.0 - (rsi or 50.0))
                return StrategySignal(buy=True, score=score, note="macd_golden")
            return StrategySignal()

        rsi_sell = rsi is not None and rsi >= self.RSI_SELL
        if snapshot.macd_dead or rsi_sell:
            reason = "macd_dead" if snapshot.macd_dead else "rsi_overbought"
            return StrategySignal(sell=True, note=reason)
        return StrategySignal()

    def is_watching(self, snapshot: MovingAverageSnapshot) -> bool:
        """Treat RSI strategy as active only when MACD is already constructive."""
        return snapshot.macd_golden or (
            snapshot.macd_line is not None
            and snapshot.macd_signal is not None
            and snapshot.macd_line > snapshot.macd_signal
        )
