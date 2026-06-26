from __future__ import annotations

from .config import RiskConfig, StrategyConfig
from .models import MarketSnapshot, PositionState, Signal, SignalType


class ConservativeMomentumStrategy:
    name = "conservative_momentum_v0"

    def __init__(self, config: StrategyConfig, risk: RiskConfig) -> None:
        self.config = config
        self.risk = risk

    def evaluate_entry(
        self, snapshot: MarketSnapshot, position: PositionState | None
    ) -> Signal:
        reasons: list[str] = []

        if position and position.qty > 0:
            return Signal(SignalType.HOLD, self.name, snapshot.stock_code, 0.0, "already holding")

        if snapshot.current_price <= snapshot.vwap:
            reasons.append("price_below_vwap")

        if not snapshot.high_breakout:
            reasons.append("no_breakout")

        if not self.config.rsi_min <= snapshot.rsi14 <= self.config.rsi_max:
            reasons.append("rsi_out_of_range")

        if snapshot.volume_ratio_1m < self.config.min_volume_ratio:
            reasons.append("volume_too_low")

        if snapshot.spread_pct > self.config.max_spread_pct:
            reasons.append("spread_too_wide")

        if snapshot.recent_turnover_krw < self.config.min_recent_turnover_krw:
            reasons.append("turnover_too_low")

        if abs(snapshot.ret_1m) > self.config.max_ret_1m:
            reasons.append("ret_1m_too_large")

        if snapshot.ret_3m > self.config.max_ret_3m:
            reasons.append("ret_3m_too_large")

        if reasons:
            return Signal(
                SignalType.HOLD,
                self.name,
                snapshot.stock_code,
                0.0,
                ",".join(reasons),
            )

        score = 50.0
        score += min(snapshot.volume_ratio_1m, 3.0) * 10.0
        score += max(snapshot.rsi14 - self.config.rsi_min, 0.0)
        score += max(snapshot.ret_3m * 100.0, 0.0)

        return Signal(
            SignalType.BUY,
            self.name,
            snapshot.stock_code,
            round(score, 2),
            "vwap_breakout_with_liquidity",
            metadata={
                "rsi14": snapshot.rsi14,
                "volume_ratio_1m": snapshot.volume_ratio_1m,
                "spread_pct": snapshot.spread_pct,
            },
        )

    def evaluate_exit(self, snapshot: MarketSnapshot, position: PositionState | None) -> Signal:
        if position is None or position.qty <= 0:
            return Signal(SignalType.HOLD, self.name, snapshot.stock_code, 0.0, "no_position")

        pnl_pct = position.pnl_pct(snapshot.current_price)

        if pnl_pct <= -self.risk.emergency_exit_if_position_loss_gt:
            return Signal(
                SignalType.SELL,
                self.name,
                snapshot.stock_code,
                100.0,
                "emergency_stop_loss",
                metadata={"pnl_pct": pnl_pct},
            )

        if pnl_pct <= -self.risk.stop_loss_pct:
            return Signal(
                SignalType.SELL,
                self.name,
                snapshot.stock_code,
                90.0,
                "stop_loss",
                metadata={"pnl_pct": pnl_pct},
            )

        if pnl_pct >= self.risk.take_profit_1_pct:
            return Signal(
                SignalType.SELL,
                self.name,
                snapshot.stock_code,
                70.0,
                "take_profit",
                metadata={"pnl_pct": pnl_pct},
            )

        if snapshot.current_price < snapshot.vwap and snapshot.ret_1m < 0:
            return Signal(
                SignalType.SELL,
                self.name,
                snapshot.stock_code,
                60.0,
                "vwap_loss_of_support",
                metadata={"pnl_pct": pnl_pct},
            )

        return Signal(
            SignalType.HOLD,
            self.name,
            snapshot.stock_code,
            0.0,
            "hold_position",
            metadata={"pnl_pct": pnl_pct},
        )

