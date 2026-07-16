from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from .config import AppConfig
from .time_utils import ensure_timezone

if TYPE_CHECKING:
    from .liquidity_lab import WatchTargetStatus
    from .notifier import TelegramNotifier
    from .repository import SqliteRepository

_logger = logging.getLogger(__name__)

EffectiveOrderHook = Callable[[dict], bool]


class LabRuntimeManager:
    """Runtime diagnostics, cooldowns, and event bookkeeping for LiquidityLab."""

    def __init__(
        self,
        config: AppConfig | object,
        repository: "SqliteRepository | None",
        notifier: "TelegramNotifier | None",
        *,
        is_effective_trade_order: EffectiveOrderHook,
    ) -> None:
        self._config = config
        self._repository = repository
        self._notifier = notifier
        self._is_effective_trade_order = is_effective_trade_order
        self._cycle_no: int = 0
        self._session_id: str = ""
        self.recent_trade_count: int = 0
        self.recent_cycle_count: int = 0
        self.recent_order_reason_counts: dict[str, int] = {}
        self.rsi_blocked_count: int = 0
        self.last_low_trade_frequency_alert_cycle: int = 0
        self.last_trend_filter_alert_cycle: int = 0
        self.exit_cooldown: dict[str, datetime] = {}
        self.no_orderable_retry: dict[str, datetime] = {}
        self.no_orderable_counts: dict[str, int] = {}
        self.symbol_loss_streak: dict[str, int] = {}

    def configure(
        self,
        *,
        config: AppConfig | object | None = None,
        repository: "SqliteRepository | None" = None,
        notifier: "TelegramNotifier | None" = None,
    ) -> None:
        if config is not None:
            self._config = config
        self._repository = repository
        self._notifier = notifier

    def load_state(
        self,
        *,
        cycle_no: int,
        session_id: str,
        recent_trade_count: int,
        recent_cycle_count: int,
        recent_order_reason_counts: dict[str, int] | None,
        rsi_blocked_count: int,
        last_low_trade_frequency_alert_cycle: int,
        last_trend_filter_alert_cycle: int,
        exit_cooldown: dict[str, datetime] | None,
        no_orderable_retry: dict[str, datetime] | None,
        no_orderable_counts: dict[str, int] | None,
        symbol_loss_streak: dict[str, int] | None = None,
    ) -> None:
        self._cycle_no = int(cycle_no)
        self._session_id = str(session_id or "")
        self.recent_trade_count = int(recent_trade_count)
        self.recent_cycle_count = int(recent_cycle_count)
        self.recent_order_reason_counts = dict(recent_order_reason_counts or {})
        self.rsi_blocked_count = int(rsi_blocked_count)
        self.last_low_trade_frequency_alert_cycle = int(last_low_trade_frequency_alert_cycle)
        self.last_trend_filter_alert_cycle = int(last_trend_filter_alert_cycle)
        self.exit_cooldown = dict(exit_cooldown or {})
        self.no_orderable_retry = dict(no_orderable_retry or {})
        self.no_orderable_counts = dict(no_orderable_counts or {})
        self.symbol_loss_streak = dict(symbol_loss_streak or {})

    def snapshot(self) -> dict[str, object]:
        return {
            "recent_trade_count": self.recent_trade_count,
            "recent_cycle_count": self.recent_cycle_count,
            "recent_order_reason_counts": dict(self.recent_order_reason_counts),
            "rsi_blocked_count": self.rsi_blocked_count,
            "last_low_trade_frequency_alert_cycle": self.last_low_trade_frequency_alert_cycle,
            "last_trend_filter_alert_cycle": self.last_trend_filter_alert_cycle,
            "exit_cooldown": dict(self.exit_cooldown),
            "no_orderable_retry": dict(self.no_orderable_retry),
            "no_orderable_counts": dict(self.no_orderable_counts),
            "symbol_loss_streak": dict(self.symbol_loss_streak),
        }

    def record_cycle_trade_frequency(
        self,
        *,
        domestic_orders: list[dict],
        overseas_orders: list[dict],
    ) -> None:
        self.recent_cycle_count += 1
        reason_counts = dict(self.recent_order_reason_counts)
        all_orders = [*domestic_orders, *overseas_orders]
        for market, orders in (
            ("domestic", domestic_orders),
            ("overseas", overseas_orders),
        ):
            for order in orders:
                if not isinstance(order, dict):
                    continue
                side = str(order.get("side") or "none").strip().lower() or "none"
                if self._is_effective_trade_order(order):
                    reason_key = f"{market}:trade:{side}"
                else:
                    reason = str(order.get("reason") or "inactive").strip() or "inactive"
                    reason_key = f"{market}:skip:{reason[:80]}"
                reason_counts[reason_key] = int(reason_counts.get(reason_key, 0)) + 1
        self.recent_order_reason_counts = reason_counts
        trade_count = sum(1 for order in all_orders if self._is_effective_trade_order(order))
        self.recent_trade_count += trade_count
        if self.recent_cycle_count < 50:
            return
        trade_ratio = self.recent_trade_count / max(self.recent_cycle_count, 1)
        top_reasons = dict(
            sorted(
                reason_counts.items(),
                key=lambda item: (-int(item[1]), str(item[0])),
            )[:8]
        )
        if trade_ratio < 0.01:
            _logger.warning(
                "[FREQ] 최근 %d사이클 매매율 %.1f%% (trade_count=%d, reasons=%s)",
                self.recent_cycle_count,
                trade_ratio * 100.0,
                self.recent_trade_count,
                top_reasons,
            )
            self.save_event(
                event_type="low_trade_frequency",
                detail={
                    "cycle_count": self.recent_cycle_count,
                    "trade_count": self.recent_trade_count,
                    "ratio": round(trade_ratio, 4),
                    "top_reasons": top_reasons,
                },
            )
            self._maybe_notify_low_trade_frequency(
                cycle_count=self.recent_cycle_count,
                trade_count=self.recent_trade_count,
                trade_ratio=trade_ratio,
                top_reasons=top_reasons,
            )
        self.recent_trade_count = 0
        self.recent_cycle_count = 0
        self.recent_order_reason_counts = {}

    def _maybe_notify_low_trade_frequency(
        self,
        *,
        cycle_count: int,
        trade_count: int,
        trade_ratio: float,
        top_reasons: dict[str, int],
    ) -> None:
        notifier = self._notifier
        if notifier is None:
            return
        cycle_no = self._cycle_no
        last_alert_cycle = self.last_low_trade_frequency_alert_cycle
        if cycle_no > 0 and last_alert_cycle > 0 and cycle_no - last_alert_cycle < 200:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        self.last_low_trade_frequency_alert_cycle = (
            cycle_no if cycle_no > 0 else last_alert_cycle + 200
        )
        top_reason_text = ", ".join(
            f"{reason} {count}회" for reason, count in list(top_reasons.items())[:3]
        )
        loop.create_task(
            notifier.send(
                "\n".join(
                    [
                        "⚠️ 매매 빈도 낮음",
                        f"범위=최근 {cycle_count}사이클",
                        f"매매={trade_count}건 비율={trade_ratio * 100:.1f}%",
                        f"주요원인={top_reason_text or '-'}",
                        "확인=/lab_status /lab_report compare 2026-07-10",
                    ]
                )
            )
        )

    def track_rsi_threshold_blocks(self, watch_targets: list["WatchTargetStatus"]) -> None:
        rsi_threshold = float(
            getattr(getattr(self._config, "auto_trade", object()), "rsi_entry_threshold", 50.0)
            or 50.0
        )
        for watch_target in watch_targets:
            if watch_target.market != "overseas":
                continue
            if watch_target.holding_qty > 0 or watch_target.action_bias == "BUY":
                continue
            snapshot = watch_target.signal_snapshot
            rsi14 = snapshot.rsi14 if snapshot is not None else None
            if rsi14 is None or rsi14 <= rsi_threshold:
                continue
            strategy_flag = str(watch_target.strategy_flag or watch_target.note or "")
            if "RSI" not in strategy_flag:
                continue
            self.rsi_blocked_count += 1
            if self.rsi_blocked_count % 20 == 0:
                _logger.info(
                    "[RSI] 차단 누적 %d건 (최근 rsi=%.1f, threshold=%.1f)",
                    self.rsi_blocked_count,
                    rsi14,
                    rsi_threshold,
                )
                self.save_event(
                    event_type="rsi_threshold_blocked",
                    detail={
                        "blocked_count": self.rsi_blocked_count,
                        "symbol": watch_target.code,
                        "rsi14": round(float(rsi14), 2),
                        "threshold": round(float(rsi_threshold), 2),
                        "strategy_flag": watch_target.strategy_flag,
                        "action_bias": watch_target.action_bias,
                    },
                )

    def check_trend_filter_lost_ratio(self) -> None:
        cycle_no = self._cycle_no
        if cycle_no <= 0 or cycle_no % 200 != 0:
            return
        repository = self._repository
        if repository is None or not hasattr(repository, "get_sell_reason_counts"):
            return
        after_logged_at = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = repository.get_sell_reason_counts(after_logged_at=after_logged_at)
        total = sum(int(row.get("cnt") or 0) for row in rows)
        trend_filter_lost = sum(
            int(row.get("cnt") or 0)
            for row in rows
            if "trend_filter" in str(row.get("action_reason") or "")
        )
        if total <= 5:
            return
        ratio = trend_filter_lost / total
        if ratio <= 0.50:
            return
        if self.last_trend_filter_alert_cycle == cycle_no:
            return
        self.last_trend_filter_alert_cycle = cycle_no
        _logger.warning(
            "[TREND] trend_filter_lost 비율 %.0f%% (%d/%d)",
            ratio * 100.0,
            trend_filter_lost,
            total,
        )
        self.save_event(
            event_type="trend_filter_lost_ratio_high",
            detail={
                "trend_filter_lost": trend_filter_lost,
                "total_sell_real": total,
                "ratio": round(ratio, 4),
                "min_hold_before_trend_exit": getattr(
                    getattr(self._config, "auto_trade", object()),
                    "min_hold_before_trend_exit",
                    12,
                ),
            },
        )
        notifier = self._notifier
        if notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            notifier.send(
                "\n".join(
                    [
                        "⚠️ trend_filter_lost 비율 경고",
                        f"비율={ratio * 100:.0f}% ({trend_filter_lost}/{total}건)",
                        "범위=최근 24시간 SELL_REAL",
                        "확인=min_hold_before_trend_exit/추세청산 조건",
                    ]
                )
            )
        )

    def save_event(
        self,
        *,
        event_type: str,
        market: str = "",
        symbol: str = "",
        detail: dict | str = "",
        cycle_no: int | None = None,
    ) -> None:
        repository = self._repository
        if repository is None or not hasattr(repository, "save_event"):
            return
        repository.save_event(
            event_type=event_type,
            market=market,
            symbol=symbol,
            detail=detail,
            cycle_no=self._cycle_no if cycle_no is None else cycle_no,
            session_id=self._session_id,
        )

    def cooldown_remaining_minutes(self, market: str, symbol: str) -> float:
        cooldown_until = self.exit_cooldown.get(f"{market}:{symbol.strip().upper()}")
        if cooldown_until is None:
            return 0.0
        remaining = (ensure_timezone(cooldown_until) - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0.0, round(remaining, 2))

    def defer_no_orderable_position(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
        orderable_qty: int,
    ) -> bool:
        key = f"{market}:{symbol.strip().upper()}"
        now = datetime.now(timezone.utc)
        retry_until = self.no_orderable_retry.get(key)
        if retry_until is not None and now <= ensure_timezone(retry_until):
            return True
        retry_minutes = self.no_orderable_retry_minutes(key)
        self.no_orderable_retry[key] = now + timedelta(minutes=retry_minutes)
        self.save_event(
            event_type="trade_skip",
            market=market,
            symbol=symbol,
            detail={
                "reason": "no_orderable_qty",
                "holding_qty": holding_qty,
                "orderable_qty": orderable_qty,
                "note": "T+2 pending or API delay",
                "retry_after_min": retry_minutes,
            },
        )
        return True

    def no_orderable_retry_minutes(self, key: str) -> int:
        count = int(self.no_orderable_counts.get(key, 0) or 0)
        if count >= 120:
            return 60
        if count >= 30:
            return 20
        return 5

    def track_no_orderable_stall(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
    ) -> int:
        key = f"{market}:{symbol.strip().upper()}"
        count = int(self.no_orderable_counts.get(key, 0) or 0) + 1
        self.no_orderable_counts[key] = count
        if count == 30:
            notifier = self._notifier
            if notifier is not None and getattr(notifier, "enabled", True):
                liquidity_config = getattr(self._config, "liquidity_lab", object())
                loop_interval_sec = max(
                    1,
                    int(getattr(liquidity_config, "loop_interval_sec", 25) or 25),
                )
                duration_min = max(1, int((count * loop_interval_sec) // 60))
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        notifier.send(
                            "\n".join(
                                [
                                    "⚠️ orderable_qty=0 장기지속",
                                    f"종목={symbol.strip().upper()}",
                                    f"지속={duration_min}분",
                                    f"보유={holding_qty}주",
                                    "참고=자본 동결 가능성, KIS 잔고/미체결 확인 필요",
                                ]
                            )
                        )
                    )
        return count

    def reset_no_orderable_stall(self, market: str, symbol: str) -> None:
        self.no_orderable_counts.pop(f"{market}:{symbol.strip().upper()}", None)

    def is_no_orderable_retry_active(self, market: str, symbol: str) -> bool:
        retry_until = self.no_orderable_retry.get(f"{market}:{symbol.strip().upper()}")
        if retry_until is None:
            return False
        return datetime.now(timezone.utc) <= ensure_timezone(retry_until)

    def clear_no_orderable_retry(self, market: str, symbol: str) -> None:
        self.no_orderable_retry.pop(f"{market}:{symbol.strip().upper()}", None)

    def register_exit_cooldown(
        self,
        market: str,
        symbol: str,
        exit_reason: str,
        *,
        pnl_pct: float | None = None,
    ) -> None:
        if exit_reason in ("stop_loss", "atr_hard_stop"):
            cooldown_minutes = 25
        elif exit_reason in ("momentum_loss_cut", "trend_filter_lost"):
            cooldown_minutes = 12
        elif exit_reason == "marginal_profit_exit":
            cooldown_minutes = 15
        else:
            cooldown_minutes = 8

        key = f"{market}:{symbol.strip().upper()}"
        is_loss = pnl_pct is not None and pnl_pct < 0
        streak = (self.symbol_loss_streak.get(key, 0) + 1) if is_loss else 0
        self.symbol_loss_streak[key] = streak

        # Repeatedly re-entering the same symbol right after it whipsaws out
        # rarely fixes anything -- the same noisy setup tends to repeat. Once
        # a symbol has lost 2+ times in a row without an intervening win,
        # escalate its re-entry cooldown well past the reason-specific default
        # so the scanner spends that time on a fresh candidate instead.
        if streak >= 3:
            cooldown_minutes = max(cooldown_minutes, 180)
        elif streak == 2:
            cooldown_minutes = max(cooldown_minutes, 60)

        if streak >= 2:
            self.save_event(
                event_type="symbol_loss_streak_cooldown",
                market=market,
                symbol=symbol,
                detail={
                    "streak": streak,
                    "exit_reason": exit_reason,
                    "cooldown_minutes": cooldown_minutes,
                },
            )
        self.set_exit_cooldown_minutes(market, symbol, cooldown_minutes)

    def set_exit_cooldown_minutes(
        self,
        market: str,
        symbol: str,
        cooldown_minutes: int,
    ) -> None:
        self.exit_cooldown[f"{market}:{symbol.strip().upper()}"] = (
            datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
        )
