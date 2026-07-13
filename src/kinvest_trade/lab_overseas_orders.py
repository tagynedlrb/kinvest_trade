from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .client import KisApiError
from .market_sessions import get_us_trading_session, is_us_orderable_session_for_env
from .message_format import format_market_korean, format_pct, format_usd
from .time_utils import format_kst, format_kst_korean

_logger = logging.getLogger(__name__)

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
            service._register_order_rejection(market="overseas", side="buy", error=error_text)
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

    async def place_sell_order(
        self,
        candidate: "OverseasScanResult",
        held: "OverseasHeldPosition",
        exit_reason: str,
        signal_snapshot: "MovingAverageSnapshot | None" = None,
    ) -> dict:
        service = self.service
        strategy_flag, entry_by, exit_by = service._get_strategy_labels(candidate.symbol, signal_snapshot)
        exit_by = exit_by or exit_reason
        entry_label, exit_label = service._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        tracker = service._get_position_tracker()
        unified = None
        target_sell_qty = min(held.quantity, max(held.orderable_qty, 0))
        real_sell_qty = target_sell_qty
        if tracker is not None:
            unified = tracker.get_unified(
                market="overseas",
                symbol=candidate.symbol,
                real_qty=0 if held.is_virtual else held.quantity,
                currency="USD",
                exchange_code=candidate.exchange_code,
            )
            if held.is_virtual:
                target_sell_qty = max(0, unified.total_qty)
                real_sell_qty = 0
            else:
                target_sell_qty = max(
                    0,
                    min(
                        unified.total_qty,
                        max(held.orderable_qty, 0) + unified.virtual_buy_qty,
                    ),
                )
                real_sell_qty = max(0, target_sell_qty - min(target_sell_qty, unified.virtual_buy_qty))

        if held.is_virtual:
            return await service._record_virtual_overseas_sell(
                candidate,
                held,
                exit_reason,
                signal_snapshot=signal_snapshot,
                sell_qty_override=target_sell_qty,
            )
        if service.config.credentials.dry_run:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "reason": "dry_run_enabled",
                "exit_reason": exit_reason,
            }
        if target_sell_qty <= 0:
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="no_orderable_qty",
                side="sell",
                price=candidate.last_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=held.orderable_qty,
                holding_qty=held.quantity,
            )
            return {
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "reason": "no_orderable_qty",
                "exit_reason": exit_reason,
            }
        now = datetime.now(timezone.utc)
        session = get_us_trading_session(now)
        created_at = format_kst(now) or now.isoformat()
        if real_sell_qty <= 0:
            return await service._record_virtual_overseas_sell(
                candidate,
                held,
                exit_reason,
                signal_snapshot=signal_snapshot,
                sell_qty_override=target_sell_qty,
            )
        if not is_us_orderable_session_for_env(now, service.config.credentials.env):
            if is_us_orderable_session_for_env(now, "prod"):
                # A real account could trade right now; the mock/paper profile
                # just can't submit orders outside its regular-session window.
                # Record the exit as a virtual sell so the strategy isn't stuck
                # waiting on a stale position, then settle it against the real
                # position once the mock session opens (see
                # _reconcile_pending_virtual_sells).
                return await service._record_virtual_overseas_sell(
                    candidate,
                    held,
                    exit_reason,
                    signal_snapshot=signal_snapshot,
                    rejected_error="session_not_orderable_in_profile",
                    sell_qty_override=target_sell_qty,
                )
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="session_not_orderable_in_profile",
                side="sell",
                price=candidate.last_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=held.orderable_qty,
                holding_qty=held.quantity,
            )
            service._persist_trade_state(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                action_bias="SELL",
                signal_state="SELL",
                note=f"{exit_reason}|session_not_orderable_in_profile",
                holding_qty=held.quantity,
                last_price=candidate.last_price,
                pnl_pct=held.pnl_pct,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                signal_snapshot=signal_snapshot,
                has_position=True,
            )
            return {
                "submitted": False,
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "reason": "session_not_orderable_in_profile",
                "session": session,
            }
        sell_price = service._overseas_sell_order_price(candidate, exit_reason=exit_reason)
        order_spec = service._sell_order_submit_spec(
            market="overseas",
            exit_reason=exit_reason,
            reference_price=sell_price,
        )
        submit_price = str(order_spec["submit_price"])
        order_division = str(order_spec["order_division"])
        order_kind = str(order_spec["order_kind"])
        pnl_pct = (sell_price - held.avg_price) / held.avg_price if held.avg_price > 0 else None
        if held.avg_price > 0 and service._is_profit_exit_reason(exit_reason):
            auto_trade_cfg = getattr(service.config, "auto_trade", None)
            fx_rate = getattr(auto_trade_cfg, "usd_krw_fallback_rate", 1380.0)
            estimated_net_usd, _, _, _ = service._estimate_overseas_net_pnl(
                entry_price=float(held.avg_price or 0.0),
                exit_price=sell_price,
                qty=real_sell_qty,
                fx_rate=fx_rate,
            )
            if estimated_net_usd <= 0:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="net_profit_below_cost",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                service._persist_trade_state(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    action_bias="HOLD",
                    signal_state="HOLD",
                    note="net_profit_below_cost",
                    holding_qty=held.quantity,
                    last_price=sell_price,
                    pnl_pct=pnl_pct,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    signal_snapshot=signal_snapshot,
                    has_position=True,
                )
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "reason": "net_profit_below_cost",
                    "exit_reason": exit_reason,
                }
        replacement_note = ""
        conflicting_buy_order = await service._find_conflicting_overseas_order(
            symbol=candidate.symbol,
            side="SELL",
            exchange_code=candidate.exchange_code,
        )
        if conflicting_buy_order is not None:
            conflicting_age_sec = service._pending_order_age_seconds(conflicting_buy_order, now=now)
            if exit_reason not in service._protective_exit_reasons() and conflicting_age_sec < 30:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_conflicting_buy_order",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_conflicting_buy_order",
                }
            try:
                cancel_response = await service._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=conflicting_buy_order,
                )
            except KisApiError as exc:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_conflicting_buy_cancel_failed",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_conflicting_buy_cancel_failed",
                    "error": str(exc),
                }
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="BUY",
                order_kind="cancel",
                requested_qty=int(conflicting_buy_order.get("open_qty") or 0),
                requested_price=float(conflicting_buy_order.get("order_price") or 0.0),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="CANCELED",
                reason="conflicting_pending_buy_cleared",
                payload=cancel_response if isinstance(cancel_response, dict) else {"response": cancel_response},
            )
            replacement_note = "미체결 매수 취소 후 재매도"
        pending_sell_order = await service._find_open_overseas_order(
            symbol=candidate.symbol,
            side="SELL",
            exchange_code=candidate.exchange_code,
        )
        if pending_sell_order is not None:
            pending_age_sec = service._pending_order_age_seconds(pending_sell_order, now=now)
            is_protective_exit = exit_reason in service._protective_exit_reasons()
            stale_threshold_sec = 45.0 if is_protective_exit else service._stale_exit_replace_seconds()
            if pending_age_sec < stale_threshold_sec:
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_exit_order",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_exit_order",
                }
            try:
                cancel_response = await service._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=pending_sell_order,
                )
            except KisApiError as exc:
                service._register_order_rejection(
                    market="overseas", side="sell", error=f"pending_exit_cancel_failed: {exc}"
                )
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="pending_exit_cancel_failed",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_exit_cancel_failed",
                    "error": str(exc),
                }
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="SELL",
                order_kind="cancel",
                requested_qty=int(pending_sell_order.get("open_qty") or 0),
                requested_price=float(pending_sell_order.get("order_price") or 0.0),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="CANCELED",
                reason="stale_exit_replace",
                payload=service._broker_cancel_payload(
                    cancel_response,
                    pending_sell_order,
                    reference_price=sell_price,
                ),
            )
            replacement_note = "미체결 매도 정정 후 재주문"

        try:
            response = await service.client.place_overseas_order_for_current_session(
                side="sell",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=real_sell_qty,
                price=submit_price,
                order_division=order_division,
            )
        except KisApiError as exc:
            error_text = str(exc)
            if service._is_mock_us_balance_missing_error(str(exc)):
                service._defer_no_orderable_position(
                    market="overseas",
                    symbol=candidate.symbol,
                    holding_qty=held.quantity,
                    orderable_qty=0,
                )
                service._record_trade_skip(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    reason="no_orderable_qty",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.symbol,
                    activity_score=candidate.activity_score,
                    orderable_qty=0,
                    holding_qty=held.quantity,
                    error=error_text,
                )
                service._record_broker_order_event(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    side="SELL",
                    order_kind=order_kind,
                    requested_qty=real_sell_qty,
                    requested_price=float(submit_price),
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    status="REJECTED",
                    reason="no_orderable_qty",
                    payload={
                        "error": error_text,
                        "order_division": order_division,
                        "reference_price": sell_price,
                    },
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "overseas",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "no_orderable_qty",
                    "error": error_text,
                }
            reject_reason = (
                "session_not_orderable_in_profile"
                if service._is_mock_us_session_blocked_error(error_text)
                or not is_us_orderable_session_for_env(
                    datetime.now(timezone.utc),
                    service.config.credentials.env,
                )
                else "order_rejected"
            )
            if reject_reason == "session_not_orderable_in_profile" and is_us_orderable_session_for_env(
                datetime.now(timezone.utc), "prod"
            ):
                # KIS rejected the live submission specifically because the
                # mock/paper profile can't order outside its regular-session
                # window, even though a real account could trade right now.
                # Record it as a virtual sell instead of leaving the strategy
                # stuck retrying the same rejected order every cycle.
                return await service._record_virtual_overseas_sell(
                    candidate,
                    held,
                    exit_reason,
                    signal_snapshot=signal_snapshot,
                    rejected_error=error_text,
                    sell_qty_override=target_sell_qty,
                )
            if reject_reason == "order_rejected":
                service._set_exit_cooldown_minutes("overseas", candidate.symbol, 20)
                service._register_order_rejection(
                    market="overseas", side="sell", error=error_text
                )
                _logger.warning(
                    "[SELL] order_rejected %s -> 20분 쿨다운 등록 (error=%s)",
                    candidate.symbol,
                    exc,
                )
                service._save_event(
                    event_type="trade_skip",
                    market="overseas",
                    symbol=candidate.symbol,
                    detail={
                        "reason": "order_rejected",
                        "side": "sell",
                        "error": error_text[:100],
                        "cooldown_applied_min": 20,
                    },
                )
            service._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason=reject_reason,
                side="sell",
                price=sell_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.symbol,
                activity_score=candidate.activity_score,
                orderable_qty=held.orderable_qty,
                holding_qty=held.quantity,
                error=error_text,
            )
            service._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="SELL",
                order_kind=order_kind,
                requested_qty=real_sell_qty,
                requested_price=float(submit_price),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="REJECTED",
                reason=reject_reason,
                payload={
                    "error": error_text,
                    "order_division": order_division,
                    "reference_price": sell_price,
                },
            )
            return {
                "submitted": False,
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "reason": reject_reason,
                "error": error_text,
            }
        existing_pending = service.repository.get_virtual_sell_pending("overseas", candidate.symbol)
        if existing_pending is not None and tracker is not None:
            tracker.settle(
                market="overseas",
                symbol=candidate.symbol,
                real_qty_after_settlement=0,
            )

        sell_result = (
            tracker.apply_sell(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                sell_qty=target_sell_qty,
                price=sell_price,
                currency="USD",
                session=session,
                reason=exit_reason,
                real_qty=held.quantity,
                can_execute_real=True,
                created_at=created_at,
                reference_avg_price=held.avg_price,
            )
            if tracker is not None
            else {
                "qty_from_real": real_sell_qty,
                "qty_from_virtual_buy": 0,
            }
        )

        lines = [
            "[KIS][LAB_SELL]",
            f"시각={format_kst_korean(now)}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol}",
            "구분=매도",
            f"가격={format_usd(sell_price)}",
            f"수량={int(sell_result.get('qty_from_real', real_sell_qty) or real_sell_qty)}주",
            f"매수전략={entry_label}",
            f"청산전략={exit_label}",
        ]
        if order_kind != "limit":
            order_label = "시장가" if order_kind == "market" else "공격지정가"
            lines.append(f"주문방식={order_label}")
        if replacement_note:
            lines.append(f"참고={replacement_note}")
        virtual_closed_qty = int(sell_result.get("qty_from_virtual_buy", 0) or 0)
        if virtual_closed_qty > 0:
            lines.append(f"참고=가상매수 {virtual_closed_qty}주 우선 차감")
        if held.avg_price > 0:
            gross_pnl = (sell_price - held.avg_price) * int(
                sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty
            )
            pnl_pct = (sell_price - held.avg_price) / held.avg_price
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("수익률=알수없음")
        service._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="SELL",
            order_kind=order_kind,
            requested_qty=real_sell_qty,
            requested_price=float(submit_price),
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status="SUBMITTED",
            reason=exit_reason,
            payload={
                "response": response,
                "sell_result": sell_result,
                "requested_qty": target_sell_qty,
                "order_division": order_division,
                "reference_price": sell_price,
            },
        )
        queue_parts = [
            format_market_korean("overseas"),
            candidate.symbol,
            "매도접수",
            format_usd(sell_price),
            f"x{int(sell_result.get('qty_from_real', real_sell_qty) or real_sell_qty)}",
            f"수익률={format_pct(pnl_pct) if held.avg_price > 0 else '-'}",
            f"매수={entry_label}",
            f"청산={exit_label}",
        ]
        if order_kind != "limit":
            order_label = "시장가" if order_kind == "market" else "공격지정가"
            queue_parts.append(f"주문={order_label}")
        service._queue_trade_notification(" ".join(queue_parts))
        await service._flush_trade_notifications(force=service._trade_notification_force_immediate())
        entry_price, entry_time_iso, hold_duration_min = service._get_entry_context(
            "overseas",
            candidate.symbol,
            fallback_price=held.avg_price,
        )
        service._reset_strategy_position(candidate.symbol)
        service._register_exit_cooldown("overseas", candidate.symbol, exit_reason)
        if held.avg_price > 0:
            real_qty_sold = int(sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty)
            auto_trade_cfg = getattr(service.config, "auto_trade", None)
            fx_rate = getattr(auto_trade_cfg, "usd_krw_fallback_rate", 1380.0)
            gross_pnl_usd = (sell_price - held.avg_price) * real_qty_sold
            gross_pnl_krw = gross_pnl_usd * fx_rate
            service._on_realised(
                market="overseas",
                gross_pnl_krw=float(gross_pnl_krw),
                pnl_pct=float(pnl_pct),
            )
            if service._is_trading_halted():
                _logger.warning(
                    "[CB] 서킷브레이커 발동 consecutive=%d session_pnl=%.0f",
                    service._consecutive_losses,
                    service._session_realised_krw,
                )
                notifier = getattr(service, "notifier", None)
                if notifier is not None and getattr(notifier, "enabled", True):
                    asyncio.create_task(
                        notifier.send(
                            f"⛔ 서킷브레이커 발동\n"
                            f"연속손절 {service._consecutive_losses}회 | "
                            f"세션손익 {service._session_realised_krw:+,.0f}원\n"
                            f"신규 매수를 중단합니다."
                        )
                    )
            if entry_price is None:
                entry_price = float(held.avg_price or 0.0)
            net_pnl_usd, net_pnl_krw, sell_commission_usd, sell_commission_krw = (
                service._estimate_overseas_net_pnl(
                    entry_price=float(entry_price or 0.0),
                    exit_price=sell_price,
                    qty=real_qty_sold,
                    fx_rate=fx_rate,
                )
            )
            service.repository.save_cycle_log(
                logged_at=datetime.now(timezone.utc).isoformat(),
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                action_bias="SELL_REAL",
                action_reason=exit_reason,
                price=sell_price,
                pnl_pct=pnl_pct,
                realized_pnl_usd=gross_pnl_usd,
                realized_pnl_krw=gross_pnl_krw,
                holding_qty=real_qty_sold,
                rsi14=signal_snapshot.rsi14 if signal_snapshot else None,
                volume_ratio=signal_snapshot.volume_ratio if signal_snapshot else None,
                intraday_momentum=signal_snapshot.intraday_momentum if signal_snapshot else None,
                intraday_bar_return=signal_snapshot.intraday_bar_return if signal_snapshot else None,
                minute_ma_fast=signal_snapshot.minute_ma_fast if signal_snapshot else None,
                minute_ma_slow=signal_snapshot.minute_ma_slow if signal_snapshot else None,
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
                exit_by=exit_by,
                is_session_trade=1 if service._is_session_owned(candidate.symbol) else 0,
                consecutive_losses=int(getattr(service, "_consecutive_losses", 0) or 0),
                hold_cycles=service._estimate_hold_cycles(candidate.symbol),
                entry_price=entry_price,
                qty_executed=real_qty_sold,
                net_pnl_usd=net_pnl_usd,
                net_pnl_krw=net_pnl_krw,
                commission_usd=sell_commission_usd,
                commission_krw=round(sell_commission_usd * fx_rate, 2),
                is_virtual=0,
                orderable_qty=held.orderable_qty,
                stock_name=candidate.symbol,
                hold_duration_min=hold_duration_min,
                entry_time=entry_time_iso,
                cb_active=service._cb_active_flag(),
                pool_size=service._pool_size_for_market("overseas"),
                activity_score=candidate.activity_score,
            )
        service._persist_trade_state(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            action_bias="SELL_REAL",
            signal_state="SELL_READY",
            note=exit_reason,
            holding_qty=0,
            last_price=sell_price,
            pnl_pct=pnl_pct if held.avg_price > 0 else None,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            signal_snapshot=signal_snapshot,
            has_position=False,
        )

        return {
            "submitted": True,
            "already_notified": True,
            "market": "overseas",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": int(sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty),
            "requested_qty": target_sell_qty,
            "exit_reason": exit_reason,
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "exit_by": exit_by,
            "order_kind": order_kind,
            "order_division": order_division,
            "submit_price": submit_price,
            "reference_price": sell_price,
            "replacement_note": replacement_note,
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
