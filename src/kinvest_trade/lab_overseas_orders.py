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

    async def place_test_order(
        self,
        candidate: "OverseasScanResult",
        watch_target: "WatchTargetStatus | None" = None,
    ) -> dict:
        service = self.service
        signal_snapshot = service._signal_cache.get(candidate.symbol.upper())
        if signal_snapshot is None and watch_target is not None:
            signal_snapshot = watch_target.signal_snapshot
        if signal_snapshot is None:
            signal_snapshot = await service._load_overseas_signal(candidate)
            service._signal_cache[candidate.symbol.upper()] = signal_snapshot
        if signal_snapshot is None:
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="signal_snapshot_unavailable",
                side="buy",
                price=candidate.last_price,
                strategy_flag="" if watch_target is None else watch_target.strategy_flag,
                entry_by="" if watch_target is None else watch_target.entry_by,
                activity_score=candidate.activity_score,
                orderable_qty=candidate.orderable_qty,
            )
            return {
                "skipped": True,
                "market": "overseas",
                "side": "wait",
                "candidate": asdict(candidate),
                "reason": "signal_snapshot_unavailable",
            }

        strategy_flag = ""
        entry_by = ""
        buy_reason = "strategy_buy_signal"
        if watch_target is not None:
            strategy_flag = watch_target.strategy_flag
            entry_by = watch_target.entry_by
            buy_reason = watch_target.note or buy_reason
        if not strategy_flag or not entry_by:
            strategy_flag, entry_by, _ = service._get_strategy_labels(
                candidate.symbol,
                signal_snapshot,
            )
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
                signal_snapshot=signal_snapshot,
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
                "signal_snapshot": asdict(signal_snapshot),
                "reason": block_reason,
            }

        liquidity_block_reason = service._entry_liquidity_block_reason(
            market="overseas",
            signal_snapshot=signal_snapshot,
        )
        if liquidity_block_reason:
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason=liquidity_block_reason,
                side="buy",
                price=candidate.last_price,
                signal_snapshot=signal_snapshot,
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
                "signal_snapshot": asdict(signal_snapshot),
                "reason": liquidity_block_reason,
            }

        config = service.config.liquidity_lab
        qty = config.overseas_test_order_qty
        buy_price = service._overseas_buy_order_price(candidate)
        if config.use_slot_sizing:
            try:
                available_usd = await service._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=buy_price,
                )
            except KisApiError:
                available_usd = 0.0
            slot_qty = service._slot_based_qty(
                available_amount=available_usd,
                price=buy_price,
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
                    signal_snapshot=signal_snapshot,
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
                    "signal_snapshot": asdict(signal_snapshot),
                    "reason": "slot_budget_insufficient",
                    "available_usd": available_usd,
                }
        if qty <= 0:
            return {"skipped": True, "reason": "overseas_test_order_qty_zero"}
        if service.config.credentials.dry_run:
            return {
                "skipped": True,
                "side": "buy",
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
            }
        conflicting_sell_order = await service._find_conflicting_overseas_order(
            symbol=candidate.symbol,
            side="BUY",
            exchange_code=candidate.exchange_code,
        )
        if conflicting_sell_order is not None:
            conflicting_age_sec = service._pending_order_age_seconds(conflicting_sell_order)
            if conflicting_age_sec < 60:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_conflicting_sell_order",
                    side="buy",
                    price=buy_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "signal_snapshot": asdict(signal_snapshot),
                    "qty": qty,
                    "reason": "pending_conflicting_sell_order",
                }
            try:
                cancel_response = await service._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=conflicting_sell_order,
                )
            except KisApiError as exc:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_conflicting_sell_cancel_failed",
                    side="buy",
                    price=buy_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "signal_snapshot": asdict(signal_snapshot),
                    "qty": qty,
                    "reason": "pending_conflicting_sell_cancel_failed",
                    "error": str(exc),
                }
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="SELL",
                order_kind="cancel",
                requested_qty=int(conflicting_sell_order.get("open_qty") or 0),
                requested_price=float(conflicting_sell_order.get("order_price") or 0.0),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                status="CANCELED",
                reason="conflicting_pending_sell_cleared",
                payload=service._broker_cancel_payload(
                    cancel_response,
                    conflicting_sell_order,
                    reference_price=buy_price,
                ),
            )
        pending_buy_order = await service._find_open_overseas_order(
            symbol=candidate.symbol,
            side="BUY",
            exchange_code=candidate.exchange_code,
        )
        if pending_buy_order is not None:
            pending_age_sec = service._pending_order_age_seconds(pending_buy_order)
            if pending_age_sec < 120:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_buy_order",
                    side="buy",
                    price=buy_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "signal_snapshot": asdict(signal_snapshot),
                    "qty": qty,
                    "reason": "pending_buy_order",
                }
            try:
                cancel_response = await service._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=pending_buy_order,
                )
            except KisApiError as exc:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_buy_cancel_failed",
                    side="buy",
                    price=buy_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=candidate.orderable_qty,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "signal_snapshot": asdict(signal_snapshot),
                    "qty": qty,
                    "reason": "pending_buy_cancel_failed",
                    "error": str(exc),
                }
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="BUY",
                order_kind="cancel",
                requested_qty=int(pending_buy_order.get("open_qty") or 0),
                requested_price=float(pending_buy_order.get("order_price") or 0.0),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                status="CANCELED",
                reason="stale_buy_replace",
                payload=service._broker_cancel_payload(
                    cancel_response,
                    pending_buy_order,
                    reference_price=buy_price,
                ),
            )
        order_division = "00"
        order_kind = "limit"
        submit_price = f"{buy_price:.4f}"
        try:
            response = await service.client.place_overseas_order_for_current_session(
                side="buy",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=qty,
                price=submit_price,
                order_division=order_division,
            )
        except KisApiError as exc:
            error_text = str(exc)
            if service._is_mock_us_session_blocked_error(str(exc)):
                return await self.record_virtual_buy(
                    candidate,
                    signal_snapshot=signal_snapshot,
                    rejected_error=error_text,
                )
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="order_rejected",
                side="buy",
                price=buy_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=candidate.orderable_qty,
                error=error_text,
            )
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="BUY",
                order_kind=order_kind,
                requested_qty=qty,
                requested_price=buy_price,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                status="REJECTED",
                reason="order_rejected",
                payload={
                    "error": error_text,
                    "order_division": order_division,
                    "reference_price": buy_price,
                },
            )
            return {
                "submitted": False,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
                "qty": qty,
                "reason": "order_rejected",
                "error": error_text,
                "order_kind": order_kind,
                "order_division": order_division,
                "submit_price": submit_price,
                "reference_price": buy_price,
            }
        repository = getattr(service, "repository", None)
        if repository is not None:
            commission_usd = round(
                buy_price * qty * service._overseas_commission_rate(),
                6,
            )
            commission_krw = round(commission_usd * float(candidate.fx_rate_krw or 0.0), 2)
            now_iso = datetime.now(timezone.utc).isoformat()
            repository.save_cycle_log(
                logged_at=now_iso,
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                action_bias="BUY_REAL",
                action_reason=buy_reason,
                price=buy_price,
                pnl_pct=0.0,
                realized_pnl_usd=0.0,
                realized_pnl_krw=0.0,
                holding_qty=qty,
                rsi14=signal_snapshot.rsi14 if signal_snapshot else None,
                volume_ratio=signal_snapshot.volume_ratio if signal_snapshot else None,
                intraday_momentum=signal_snapshot.intraday_momentum if signal_snapshot else None,
                intraday_bar_return=(
                    signal_snapshot.intraday_bar_return if signal_snapshot else None
                ),
                minute_ma_fast=signal_snapshot.minute_ma_fast if signal_snapshot else None,
                minute_ma_slow=signal_snapshot.minute_ma_slow if signal_snapshot else None,
                activity_score=candidate.activity_score,
                vwap=signal_snapshot.vwap if signal_snapshot else None,
                macd_line=signal_snapshot.macd_line if signal_snapshot else None,
                macd_signal=signal_snapshot.macd_signal if signal_snapshot else None,
                macd_golden=int(signal_snapshot.macd_golden) if signal_snapshot else None,
                breakout_distance_pct=(
                    signal_snapshot.breakout_distance_pct if signal_snapshot else None
                ),
                atr=signal_snapshot.atr if signal_snapshot else None,
                spread_pct=signal_snapshot.spread_pct if signal_snapshot else None,
                cycle_no=getattr(service, "_cycle_count", 0),
                session_id=getattr(service, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                consecutive_losses=int(getattr(service, "_consecutive_losses", 0) or 0),
                entry_price=buy_price,
                qty_executed=qty,
                net_pnl_usd=0.0,
                net_pnl_krw=0.0,
                commission_usd=commission_usd,
                commission_krw=commission_krw,
                is_virtual=0,
                orderable_qty=candidate.orderable_qty,
                stock_name=candidate.symbol,
                hold_duration_min=0.0,
                entry_time=now_iso,
                exit_cooldown_remaining=0.0,
                cb_active=service._cb_active_flag(),
                pool_size=service._pool_size_for_market("overseas"),
            )
        service._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="BUY",
            order_kind=order_kind,
            requested_qty=qty,
            requested_price=buy_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            status="SUBMITTED",
            reason=buy_reason,
            payload={
                "response": response,
                "order_division": order_division,
                "reference_price": buy_price,
            },
        )
        service._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("overseas"),
                    candidate.symbol,
                    "매수접수",
                    format_usd(buy_price),
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
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        service._persist_trade_state(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            action_bias="BUY_REAL",
            signal_state="BUY",
            note=buy_reason,
            holding_qty=qty,
            last_price=buy_price,
            pnl_pct=0.0,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            signal_snapshot=signal_snapshot,
            has_position=True,
        )
        service._mark_session_owned(candidate.symbol)
        return {
            "submitted": True,
            "already_notified": True,
            "market": "overseas",
            "side": "buy",
            "candidate": asdict(candidate),
            "signal_snapshot": asdict(signal_snapshot),
            "qty": qty,
            "reason": buy_reason,
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "order_kind": order_kind,
            "order_division": order_division,
            "submit_price": submit_price,
            "reference_price": buy_price,
            "response": response,
        }

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
