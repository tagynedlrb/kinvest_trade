from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum


class StrategyID(IntEnum):
    VWAP_PULLBACK = 1
    VOLUME_BREAKOUT = 2
    RSI_MACD = 3


STRATEGY_LABEL: dict[StrategyID, str] = {
    StrategyID.VWAP_PULLBACK: "VWAP",
    StrategyID.VOLUME_BREAKOUT: "VOL",
    StrategyID.RSI_MACD: "RSI",
}


@dataclass(slots=True)
class StrategySignal:
    buy: bool = False
    sell: bool = False
    score: float = 0.0
    note: str = ""


@dataclass(slots=True)
class Position:
    symbol: str
    entry_price: float
    entry_time: datetime
    triggered_by: frozenset[StrategyID]
    peak_price: float = 0.0

    def update_peak(self, price: float) -> None:
        if price > self.peak_price:
            self.peak_price = price

    @property
    def flag(self) -> str:
        return "+".join(STRATEGY_LABEL[strategy] for strategy in sorted(self.triggered_by))

    @property
    def entry_by(self) -> str:
        if not self.triggered_by:
            return ""
        first = min(self.triggered_by)
        return STRATEGY_LABEL[first]
