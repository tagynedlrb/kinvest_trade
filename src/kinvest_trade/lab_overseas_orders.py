from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .client import KisApiError
from .market_sessions import get_us_trading_session
from .message_format import format_market_korean, format_pct, format_usd
from .time_utils import format_kst, format_kst_korean

if TYPE_CHECKING:
    from .liquidity_lab import (
        LiquidityLabService,
        OverseasHeldPosition,
        OverseasScanResult,
        WatchTargetStatus,
    )
    from .technical_signals import MovingAverageSnapshot


class OverseasOrderHelper:
    """Overseas position routing and virtual order workflows for LiquidityLab."""

    def __init__(self, service: "LiquidityLabService") -> None:
        self.service = service

    async def manage_position(
        self,
        *,
        candidate: "OverseasScanResult",
        held_positions: list["OverseasHeldPosition"],
        watch_target: "WatchTargetStatus | None" = None,
    ) -> dict:
        service = self.service
        config = service.config.liquidity_lab
        tracker = service._get_position_tracker()
        real_held = next(
            (
                item
                for item in held_positions
                if item.symbol.upper() == candidate.symbol.upper() and not item.is_virtual
            ),
            None,
        )
        display_held = real_held or next(
            (
                item
                for item in held_positions
                if item.symbol.upper() == candidate.symbol.upper()
            ),
            None,
        )
        unified = (
            tracker.get_unified(
                market="overseas",
                symbol=candidate.symbol,
                real_qty=0 if real_held is None else real_held.quantity,
                currency="USD",
                exchange_code=candidate.exchange_code,
            )
            if tracker is not None
            else None
        )

        if display_held is not None:
            if real_held is not None and real_held.orderable_qty <= 0:
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "hold",
                    "candidate": asdict(candidate),
                    "held_position": asdict(display_held),
                    "reason": "pending_exit_order",
                }
            total_qty = display_held.quantity if unified is None else unified.total_qty
            if total_qty >= config.overseas_max_position_qty:
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "hold",
                    "candidate": asdict(candidate),
                    "held_position": asdict(display_held),
                    "reason": "already_holding_max_qty_waiting_for_exit",
                }

        return await service._place_overseas_test_order(candidate, watch_target=watch_target)

    async def record_virtual_buy(
        self,
        candidate: "OverseasScanResult",
        *,
        signal_snapshot: "MovingAverageSnapshot | None" = None,
        rejected_error: str | None = None,
        watch_target: "WatchTargetStatus | None" = None,
    ) -> dict:
        service = self.service
        config = service.config.liquidity_lab
        qty = int(config.overseas_test_order_qty)
        snapshot = signal_snapshot or service._signal_cache.get(candidate.symbol.upper())
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        if snapshot is not None and (not strategy_flag or not entry_by):
            strategy_flag, entry_by, _ = service._get_strategy_labels(candidate.symbol, snapshot)
        block_reason = service._entry_strategy_block_reason(
            market="overseas",
            strategy_flag=strategy_flag,
        )
        if block_reason:
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason=block_reason,
                side="buy",
                price=candidate.last_price,
                signal_snapshot=snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=candidate.orderable_qty,
            )
            return {
                "skipped": True,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": block_reason,
            }
        liquidity_block_reason = service._entry_liquidity_block_reason(
            market="overseas",
            signal_snapshot=snapshot,
        )
        if liquidity_block_reason:
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason=liquidity_block_reason,
                side="buy",
                price=candidate.last_price,
                signal_snapshot=snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=candidate.orderable_qty,
                extra_detail={
                    "min_volume_ratio": float(
                        getattr(
                            service.config.liquidity_lab,
                            "overseas_min_strategy_volume_ratio",
                            0.0,
                        )
                        or 0.0
                    ),
                },
            )
            return {
                "skipped": True,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": liquidity_block_reason,
            }
        if config.use_slot_sizing:
            try:
                available_usd = await service._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                )
            except KisApiError:
                available_usd = 0.0
            remaining_virtual_budget = service._remaining_virtual_overseas_budget(available_usd)
            if remaining_virtual_budget <= 0:
                virtual_notional_usd = service._open_virtual_overseas_notional()
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="virtual_exposure_limit",
                    side="buy",
                    price=candidate.last_price,
                    signal_snapshot=snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                    extra_detail={
                        "available_usd": round(available_usd, 4),
                        "virtual_notional_usd": round(virtual_notional_usd, 4),
                        "remaining_virtual_budget": round(remaining_virtual_budget, 4),
                    },
                )
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "reason": "virtual_exposure_limit",
                    "available_usd": available_usd,
                    "virtual_notional_usd": virtual_notional_usd,
                }
            slot_qty = service._slot_based_qty(
                available_amount=available_usd,
                price=candidate.last_price,
                max_budget=remaining_virtual_budget,
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_usd > 0:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="slot_budget_insufficient",
                    side="buy",
                    price=candidate.last_price,
                    signal_snapshot=snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                    extra_detail={
                        "available_usd": round(available_usd, 4),
                        "remaining_virtual_budget": round(remaining_virtual_budget, 4),
                    },
                )
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "reason": "slot_budget_insufficient",
                    "available_usd": available_usd,
                }
        if qty <= 0:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "overseas_test_order_qty_zero",
            }

        now = datetime.now(timezone.utc)
        session = get_us_trading_session(now)
        created_at = format_kst(now) or now.isoformat()
        position = service.virtual_trades.record_buy(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            qty=qty,
            fill_price=candidate.last_price,
            currency="USD",
            session=session,
            reason="session_not_orderable_in_profile",
            created_at=created_at,
        )
        if position is None:
            return {
                "submitted": False,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "virtual_buy_record_failed",
            }

        service._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="BUY",
            order_kind="virtual_limit",
            requested_qty=qty,
            requested_price=candidate.last_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            status="RECORDED",
            reason="session_not_orderable_in_profile",
            is_virtual=True,
            payload={
                "rejected_error": rejected_error,
                "virtual_position": asdict(position),
            },
        )
        service._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("overseas"),
                    f"{candidate.symbol}(가상)",
                    "가상매수",
                    format_usd(candidate.last_price),
                    f"x{qty}",
                    f"전략={strategy_flag or '-'}",
                    f"주도={entry_by or '-'}",
                ]
            )
        )
        await service._flush_trade_notifications(
            force=service._trade_notification_force_immediate()
        )
        service._commit_strategy_entry(
            candidate.symbol,
            snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        service._persist_trade_state(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            action_bias="VIRTUAL_BUY",
            signal_state="BUY",
            note="session_not_orderable_in_profile",
            holding_qty=qty,
            last_price=candidate.last_price,
            pnl_pct=0.0,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            signal_snapshot=snapshot,
            has_position=True,
        )
        return {
            "submitted": True,
            "already_notified": True,
            "virtual": True,
            "market": "overseas",
            "side": "buy",
            "candidate": asdict(candidate),
            "signal_snapshot": None if snapshot is None else asdict(snapshot),
            "qty": qty,
            "reason": "session_not_orderable_in_profile",
            "session": session,
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "virtual_position": asdict(position),
        }

    async def record_virtual_sell(
        self,
        candidate: "OverseasScanResult",
        held: "OverseasHeldPosition",
        exit_reason: str,
        *,
        signal_snapshot: "MovingAverageSnapshot | None" = None,
        rejected_error: str | None = None,
        sell_qty_override: int | None = None,
    ) -> dict:
        service = self.service
        strategy_flag, entry_by, exit_by = service._get_strategy_labels(
            candidate.symbol,
            signal_snapshot,
        )
        entry_label, exit_label = service._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        tracker = service._get_position_tracker()
        sell_qty = (
            sell_qty_override
            if sell_qty_override is not None
            else min(held.quantity, max(held.orderable_qty, 0))
        )
        if sell_qty <= 0:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "reason": "no_orderable_qty",
                "virtual": True,
            }

        now = datetime.now(timezone.utc)
        session = get_us_trading_session(now)
        created_at = format_kst(now) or now.isoformat()
        sell_result = (
            tracker.apply_sell(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                sell_qty=sell_qty,
                price=candidate.last_price,
                currency="USD",
                session=session,
                reason=exit_reason,
                real_qty=0 if held.is_virtual else held.quantity,
                can_execute_real=False,
                created_at=created_at,
                reference_avg_price=held.avg_price,
            )
            if tracker is not None
            else {
                "realized_pnl": 0.0,
                "qty_from_real": sell_qty,
                "qty_from_virtual_buy": 0,
                "qty_pending_real": sell_qty,
            }
        )
        realized_pnl = float(sell_result.get("realized_pnl", 0.0) or 0.0)
        realized_pnl_pct = (
            (candidate.last_price - held.avg_price) / held.avg_price
            if held.avg_price > 0
            else 0.0
        )
        pending_real_qty = int(sell_result.get("qty_pending_real", 0) or 0)
        closed_virtual_buy_qty = int(sell_result.get("qty_from_virtual_buy", 0) or 0)

        service._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="SELL",
            order_kind="virtual_limit",
            requested_qty=sell_qty,
            requested_price=candidate.last_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status="RECORDED",
            reason="session_not_orderable_in_profile" if rejected_error else exit_reason,
            is_virtual=True,
            payload={
                "rejected_error": rejected_error,
                "sell_result": sell_result,
                "held_position": asdict(held),
            },
        )
        service._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("overseas"),
                    f"{candidate.symbol}(가상)",
                    "가상매도",
                    format_usd(candidate.last_price),
                    f"x{sell_qty}",
                    f"수익률={format_pct(realized_pnl_pct)}",
                    f"매수={entry_label}",
                    f"청산={exit_label}",
                ]
            )
        )
        await service._flush_trade_notifications(
            force=service._trade_notification_force_immediate()
        )
        service._reset_strategy_position(candidate.symbol)
        service._register_exit_cooldown("overseas", candidate.symbol, exit_reason)
        service._persist_trade_state(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            action_bias="VIRTUAL_SELL",
            signal_state="SELL_READY",
            note="session_not_orderable_in_profile" if rejected_error else exit_reason,
            holding_qty=max(0, pending_real_qty),
            last_price=candidate.last_price,
            pnl_pct=realized_pnl_pct,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            signal_snapshot=signal_snapshot,
            has_position=pending_real_qty > 0,
        )
        return {
            "submitted": True,
            "already_notified": True,
            "virtual": True,
            "market": "overseas",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": sell_qty,
            "exit_reason": exit_reason,
            "reason": "session_not_orderable_in_profile" if rejected_error else exit_reason,
            "session": session,
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "exit_by": exit_by,
            "qty_pending_real": pending_real_qty,
            "qty_from_virtual_buy": closed_virtual_buy_qty,
        }
