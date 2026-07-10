from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..technical_signals import MovingAverageSnapshot
from .base import Position, STRATEGY_LABEL, StrategyID, StrategySignal

logger = logging.getLogger(__name__)

PRIORITY_ORDER = [
    StrategyID.VWAP_PULLBACK,
    StrategyID.VOLUME_BREAKOUT,
    StrategyID.RSI_MACD,
]


@dataclass(slots=True)
class StrategyResult:
    signal: str
    flag: str
    entry_by: str
    exit_by: str
    pnl_pct: float | None
    triggered_by: frozenset[StrategyID] = field(default_factory=frozenset)


class PriorityStrategyManager:
    """
    Priority-based entry routing plus strategy-scoped exit metadata.
    """

    def __init__(self, auto_trade_config: object | None = None) -> None:
        from .rsi_macd import RSIMACDStrategy
        from .volume_breakout import VolumeBreakoutStrategy
        from .vwap_pullback import VWAPPullbackStrategy

        self._auto_trade_config = auto_trade_config
        self._strategies: dict[StrategyID, object] = {
            StrategyID.VWAP_PULLBACK: VWAPPullbackStrategy(),
            StrategyID.VOLUME_BREAKOUT: VolumeBreakoutStrategy(),
            StrategyID.RSI_MACD: RSIMACDStrategy(),
        }
        self.position: Optional[Position] = None

    def evaluate(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot,
        *,
        commit: bool = True,
    ) -> StrategyResult:
        signals: dict[StrategyID, StrategySignal] = {}
        for strategy_id, strategy in self._strategies.items():
            signals[strategy_id] = strategy.evaluate(snapshot, self.position)  # type: ignore[attr-defined]

        if self.position is None:
            return self._check_entry(symbol, snapshot, signals, commit=commit)
        return self._check_exit(symbol, snapshot, signals, commit=commit)

    def open_position(
        self,
        *,
        symbol: str,
        entry_price: float,
        triggered_by: frozenset[StrategyID],
        entry_time: datetime | None = None,
    ) -> None:
        if not triggered_by:
            return
        opened_at = entry_time or datetime.now(timezone.utc)
        self.position = Position(
            symbol=symbol,
            entry_price=entry_price,
            entry_time=opened_at,
            triggered_by=triggered_by,
            peak_price=entry_price,
        )

    def reset(self) -> None:
        self.position = None

    def buy_score(self, snapshot: MovingAverageSnapshot) -> float:
        """Sum the scores of strategies that emit BUY on this snapshot."""
        signals: dict[StrategyID, StrategySignal] = {}
        for strategy_id, strategy in self._strategies.items():
            signals[strategy_id] = strategy.evaluate(snapshot, None)  # type: ignore[attr-defined]
        triggered = self._filter_triggered_entry_ids(
            frozenset(
                strategy_id for strategy_id, signal in signals.items() if signal.buy
            ),
            snapshot,
        )
        return sum(signals[strategy_id].score for strategy_id in triggered)

    def _check_entry(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot,
        signals: dict[StrategyID, StrategySignal],
        *,
        commit: bool,
    ) -> StrategyResult:
        triggered = self._filter_triggered_entry_ids(
            frozenset(
            strategy_id for strategy_id, signal in signals.items() if signal.buy
            ),
            snapshot,
        )
        if not triggered:
            watching_ids = [
                strategy_id
                for strategy_id in PRIORITY_ORDER
                if hasattr(self._strategies[strategy_id], "is_watching")
                and self._strategies[strategy_id].is_watching(snapshot)  # type: ignore[attr-defined]
            ]
            monitoring_flag = (
                "+".join(STRATEGY_LABEL[strategy_id] for strategy_id in watching_ids)
                if watching_ids
                else ""
            )
            return StrategyResult("HOLD", monitoring_flag, "", "", None)

        preview_position = Position(
            symbol=symbol,
            entry_price=snapshot.price,
            entry_time=datetime.now(timezone.utc),
            triggered_by=triggered,
            peak_price=snapshot.price,
        )
        if commit:
            self.position = preview_position
            logger.info(
                "[ENTRY] %s flag=%s entry_by=%s price=%.2f",
                symbol,
                preview_position.flag,
                preview_position.entry_by,
                snapshot.price,
            )
        return StrategyResult(
            signal="BUY",
            flag=preview_position.flag,
            entry_by=preview_position.entry_by,
            exit_by="",
            pnl_pct=None,
            triggered_by=triggered,
        )

    def _filter_triggered_entry_ids(
        self,
        triggered: frozenset[StrategyID],
        snapshot: MovingAverageSnapshot,
    ) -> frozenset[StrategyID]:
        if not triggered:
            return triggered
        config = self._auto_trade_config
        filtered = set(triggered)
        if filtered == {StrategyID.VWAP_PULLBACK}:
            vwap = snapshot.vwap
            min_above_pct = float(
                getattr(config, "vwap_min_price_above_pct", 0.0) or 0.0
            )
            if vwap is None or vwap <= 0:
                filtered.clear()
            else:
                above_pct = (snapshot.price - vwap) / vwap
                if above_pct < min_above_pct:
                    filtered.clear()
        if filtered == {StrategyID.RSI_MACD}:
            rsi = snapshot.rsi14
            entry_threshold = float(
                getattr(config, "rsi_entry_threshold", 50.0) or 50.0
            )
            if rsi is None or rsi > entry_threshold:
                filtered.clear()
        return frozenset(filtered)

    def _check_exit(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot,
        signals: dict[StrategyID, StrategySignal],
        *,
        commit: bool,
    ) -> StrategyResult:
        position = self.position
        assert position is not None
        if commit:
            position.update_peak(snapshot.price)
        peak_price = max(position.peak_price, snapshot.price)

        for strategy_id in sorted(position.triggered_by):
            if signals[strategy_id].sell:
                pnl = (snapshot.price - position.entry_price) / position.entry_price * 100
                exit_label = STRATEGY_LABEL[strategy_id]
                if commit:
                    logger.info(
                        "[EXIT] %s flag=%s exit_by=%s pnl=%.2f%%",
                        symbol,
                        position.flag,
                        exit_label,
                        pnl,
                    )
                    self.position = None
                else:
                    position.peak_price = peak_price
                return StrategyResult(
                    signal="SELL",
                    flag=position.flag,
                    entry_by=position.entry_by,
                    exit_by=exit_label,
                    pnl_pct=round(pnl, 2),
                    triggered_by=position.triggered_by,
                )
        if not commit:
            position.peak_price = peak_price
        return StrategyResult(
            "HOLD",
            position.flag,
            position.entry_by,
            "",
            None,
            triggered_by=position.triggered_by,
        )
