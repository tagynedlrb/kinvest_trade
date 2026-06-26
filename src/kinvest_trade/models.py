from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class SignalType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(slots=True)
class MarketSnapshot:
    stock_code: str
    current_price: int
    reference_price: int
    vwap: float
    rsi14: float
    ret_1m: float
    ret_3m: float
    spread_pct: float
    recent_turnover_krw: int
    volume_ratio_1m: float
    high_breakout: bool
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class PositionState:
    stock_code: str
    qty: int
    avg_price: int
    scaled_in_steps: int = 0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def pnl_pct(self, current_price: int) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (current_price - self.avg_price) / self.avg_price


@dataclass(slots=True)
class AccountState:
    equity_krw: int
    available_cash_krw: int
    daily_pnl_pct: float
    open_positions: int
    consecutive_losses: int
    trading_enabled: bool = True


@dataclass(slots=True)
class Signal:
    signal_type: SignalType
    strategy_name: str
    stock_code: str
    score: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderIntent:
    stock_code: str
    side: str
    qty: int
    price: int | None
    strategy_name: str
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

