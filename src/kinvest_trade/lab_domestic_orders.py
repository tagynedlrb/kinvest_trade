from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .client import KisApiError
from .message_format import format_krw, format_market_korean, format_pct
from .time_utils import format_kst_korean

if TYPE_CHECKING:
    from .liquidity_lab import (
        DomesticHeldPosition,
        DomesticScanResult,
        LiquidityLabService,
        WatchTargetStatus,
    )
    from .technical_signals import MovingAverageSnapshot

_logger = logging.getLogger(__name__)


class DomesticOrderHelper:
    """Domestic buy/sell order workflows for LiquidityLab."""

    def __init__(self, service: "LiquidityLabService") -> None:
        self.service = service

    async def place_test_order(
        self,
        candidate: "DomesticScanResult",
        watch_target: "WatchTargetStatus | None" = None,
    ) -> dict:
        service = self.service
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        signal_snapshot = None if watch_target is None else watch_target.signal_snapshot
        buy_price = float(candidate.best_ask or candidate.current_price)
        config = service.config.liquidity_lab
        qty = config.domestic_test_order_qty
        if config.use_slot_sizing:
            try:
                available_krw = await service._get_domestic_available_krw()
            except KisApiError:
                available_krw = 0.0
            slot_qty = service._slot_based_qty(
                available_amount=available_krw,
                price=float(candidate.best_ask or candidate.current_price),
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_krw > 0:
                service._record_trade_skip(
                    market="domestic",
                    symbol=candidate.stock_code,
                    exchange_code=None,
                    reason="slot_budget_insufficient",
                    side="buy",
                    price=buy_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.stock_name,
                    activity_score=candidate.activity_score,
                    orderable_qty=qty,
                )
                return {
                    "skipped": True,
                    "market": "domestic",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "reason": "slot_budget_insufficient",
                    "available_krw": available_krw,
                }
        if qty <= 0:
            return {"skipped": True, "reason": "domestic_test_order_qty_zero"}
        if service.config.credentials.dry_run:
            return {
                "skipped": True,
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
            }
        order_division = "00"
        order_kind = "limit"
        submit_price = buy_price
        try:
            response = await service.client.place_cash_order(
                side="buy",
                stock_code=candidate.stock_code,
                qty=qty,
                price=submit_price,
                order_division=order_division,
            )
        except KisApiError as exc:
            error_text = str(exc)
            service._register_order_rejection(market="domestic", side="buy", error=error_text)
            service._record_trade_skip(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                reason="order_rejected",
                side="buy",
                price=buy_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.stock_name,
                activity_score=candidate.activity_score,
                orderable_qty=qty,
                error=error_text,
            )
            service._record_broker_order_event(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                side="BUY",
                order_kind=order_kind,
                requested_qty=qty,
                requested_price=submit_price,
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
                "skipped": True,
                "market": "domestic",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "order_rejected",
                "error": error_text,
            }
        service._record_broker_order_event(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
            side="BUY",
            order_kind=order_kind,
            requested_qty=qty,
            requested_price=submit_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            status="SUBMITTED",
            reason="domestic_buy",
            payload={
                "response": response,
                "order_division": order_division,
                "reference_price": buy_price,
            },
        )
        service._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("domestic"),
                    service._format_trade_symbol_label("domestic", candidate.stock_code),
                    "매수접수",
                    f"{int(candidate.best_ask or candidate.current_price):,}원",
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
            candidate.stock_code,
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        service._mark_session_owned(candidate.stock_code)
        repository = getattr(service, "repository", None)
        if repository is not None:
            commission_krw = round(buy_price * qty * service._domestic_commission_rate(), 2)
            now_iso = datetime.now(timezone.utc).isoformat()
            repository.save_cycle_log(
                logged_at=now_iso,
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                action_bias="BUY_REAL",
                action_reason="domestic_buy",
                price=buy_price,
                pnl_pct=0.0,
                realized_pnl_usd=None,
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
                net_pnl_usd=None,
                net_pnl_krw=0.0,
                commission_usd=None,
                commission_krw=commission_krw,
                is_virtual=0,
                orderable_qty=qty,
                stock_name=candidate.stock_name,
                hold_duration_min=0.0,
                entry_time=now_iso,
                exit_cooldown_remaining=0.0,
                cb_active=service._cb_active_flag(),
                pool_size=service._pool_size_for_market("domestic"),
            )
        service._persist_trade_state(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
            action_bias="BUY_REAL",
            signal_state="BUY",
            note="domestic_buy",
            holding_qty=qty,
            last_price=float(candidate.current_price),
            pnl_pct=0.0,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            signal_snapshot=signal_snapshot,
            has_position=True,
        )
        return {
            "submitted": True,
            "already_notified": True,
            "market": "domestic",
            "side": "buy",
            "candidate": asdict(candidate),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "qty": qty,
            "order_kind": order_kind,
            "order_division": order_division,
            "submit_price": submit_price,
            "reference_price": buy_price,
            "response": response,
        }

    async def place_sell_order(
        self,
        candidate: "DomesticScanResult",
        held: "DomesticHeldPosition",
        exit_reason: str,
        signal_snapshot: "MovingAverageSnapshot | None" = None,
    ) -> dict:
        service = self.service
        strategy_flag, entry_by, exit_by = service._get_strategy_labels(
            candidate.stock_code,
            signal_snapshot,
        )
        exit_by = exit_by or exit_reason
        entry_label, exit_label = service._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        if service.config.credentials.dry_run:
            return {
                "skipped": True,
                "market": "domestic",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "reason": "dry_run_enabled",
                "exit_reason": exit_reason,
            }

        sell_price = float(candidate.best_bid or candidate.current_price)
        order_spec = service._sell_order_submit_spec(
            market="domestic",
            exit_reason=exit_reason,
            reference_price=sell_price,
        )
        submit_price = int(order_spec["submit_price"])
        order_division = str(order_spec["order_division"])
        order_kind = str(order_spec["order_kind"])
        sell_qty = min(held.quantity, max(held.orderable_qty, 0))
        replacement_note = ""
        pending_sell_order = await service._find_open_domestic_order(
            symbol=candidate.stock_code,
            side="SELL",
        )
        if pending_sell_order is not None:
            pending_age_sec = service._pending_order_age_seconds(pending_sell_order)
            is_protective_exit = exit_reason in service._protective_exit_reasons()
            stale_threshold_sec = 45.0 if is_protective_exit else service._stale_exit_replace_seconds()
            if pending_age_sec < stale_threshold_sec:
                service._record_trade_skip(
                    market="domestic",
                    symbol=candidate.stock_code,
                    exchange_code=None,
                    reason="pending_exit_order",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    stock_name=candidate.stock_name,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "domestic",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_exit_order",
                }
            try:
                cancel_response = await service._cancel_open_domestic_order(
                    symbol=candidate.stock_code,
                    pending_order=pending_sell_order,
                )
            except KisApiError as exc:
                error_text = str(exc)
                service._register_order_rejection(
                    market="domestic", side="sell", error=f"pending_exit_cancel_failed: {error_text}"
                )
                service._record_trade_skip(
                    market="domestic",
                    symbol=candidate.stock_code,
                    exchange_code=None,
                    reason="pending_exit_cancel_failed",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    stock_name=candidate.stock_name,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                    error=error_text,
                )
                return {
                    "submitted": False,
                    "skipped": True,
                    "market": "domestic",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "exit_reason": exit_reason,
                    "reason": "pending_exit_cancel_failed",
                    "error": error_text,
                }
            service._record_broker_order_event(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
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
            if sell_qty <= 0:
                sell_qty = min(
                    held.quantity,
                    int(pending_sell_order.get("open_qty") or held.quantity),
                )
        if sell_qty <= 0:
            service._record_trade_skip(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                reason="no_orderable_qty",
                side="sell",
                price=sell_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                stock_name=candidate.stock_name,
                activity_score=candidate.activity_score,
                orderable_qty=held.orderable_qty,
                holding_qty=held.quantity,
            )
            return {
                "skipped": True,
                "market": "domestic",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "reason": "no_orderable_qty",
                "exit_reason": exit_reason,
            }
        pnl_pct = (sell_price - held.avg_price) / held.avg_price if held.avg_price > 0 else None
        if held.avg_price > 0 and service._is_profit_exit_reason(exit_reason):
            estimated_net_krw, _ = service._estimate_domestic_net_pnl_krw(
                entry_price=float(held.avg_price or 0.0),
                exit_price=sell_price,
                qty=sell_qty,
            )
            if estimated_net_krw <= 0:
                service._record_trade_skip(
                    market="domestic",
                    symbol=candidate.stock_code,
                    exchange_code=None,
                    reason="net_profit_below_cost",
                    side="sell",
                    price=sell_price,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    stock_name=candidate.stock_name,
                    activity_score=candidate.activity_score,
                    orderable_qty=held.orderable_qty,
                    holding_qty=held.quantity,
                )
                service._persist_trade_state(
                    market="domestic",
                    symbol=candidate.stock_code,
                    exchange_code=None,
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
                    "market": "domestic",
                    "side": "sell",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                    "reason": "net_profit_below_cost",
                    "exit_reason": exit_reason,
                }
        try:
            response = await service.client.place_cash_order(
                side="sell",
                stock_code=candidate.stock_code,
                qty=sell_qty,
                price=submit_price,
                order_division=order_division,
            )
        except KisApiError as exc:
            error_text = str(exc)
            service._set_exit_cooldown_minutes("domestic", candidate.stock_code, 10)
            service._register_order_rejection(market="domestic", side="sell", error=error_text)
            _logger.warning(
                "[SELL] domestic order_rejected %s -> 10분 쿨다운 등록 (error=%s)",
                candidate.stock_code,
                exc,
            )
            service._save_event(
                event_type="trade_skip",
                market="domestic",
                symbol=candidate.stock_code,
                detail={
                    "reason": "order_rejected",
                    "side": "sell",
                    "error": error_text[:100],
                    "cooldown_applied_min": 10,
                },
            )
            service._record_trade_skip(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                reason="order_rejected",
                side="sell",
                price=sell_price,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                stock_name=candidate.stock_name,
                activity_score=candidate.activity_score,
                orderable_qty=held.orderable_qty,
                holding_qty=held.quantity,
                error=error_text,
            )
            service._record_broker_order_event(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                side="SELL",
                order_kind=order_kind,
                requested_qty=sell_qty,
                requested_price=float(submit_price),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="REJECTED",
                reason="order_rejected",
                payload={
                    "error": error_text,
                    "order_division": order_division,
                    "reference_price": sell_price,
                },
            )
            return {
                "submitted": False,
                "skipped": True,
                "market": "domestic",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "reason": "order_rejected",
                "error": error_text,
            }

        lines = [
            "[KIS][LAB_SELL]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"시장={format_market_korean('domestic')}",
            f"종목={candidate.stock_code}",
            "구분=매도",
            f"가격={format_krw(sell_price)}",
            f"수량={sell_qty}주",
            f"매수전략={entry_label}",
            f"청산전략={exit_label}",
        ]
        if order_kind != "limit":
            lines.append(f"주문방식={'시장가' if order_kind == 'market' else order_kind}")
        if replacement_note:
            lines.append(f"참고={replacement_note}")
        if held.avg_price > 0:
            gross_pnl = (sell_price - held.avg_price) * sell_qty
            pnl_pct = (sell_price - held.avg_price) / held.avg_price
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("수익률=알수없음")
        service._record_broker_order_event(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
            side="SELL",
            order_kind=order_kind,
            requested_qty=sell_qty,
            requested_price=float(submit_price),
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status="SUBMITTED",
            reason=exit_reason,
            payload={
                "response": response,
                "order_division": order_division,
                "reference_price": sell_price,
            },
        )
        queue_parts = [
            format_market_korean("domestic"),
            service._format_trade_symbol_label("domestic", candidate.stock_code),
            "매도접수",
            format_krw(sell_price),
            f"x{sell_qty}",
            f"수익률={format_pct(pnl_pct) if held.avg_price > 0 else '-'}",
            f"매수={entry_label}",
            f"청산={exit_label}",
        ]
        if order_kind != "limit":
            queue_parts.append(f"주문={'시장가' if order_kind == 'market' else order_kind}")
        if replacement_note:
            queue_parts.append(f"참고={replacement_note}")
        service._queue_trade_notification(" ".join(queue_parts))
        await service._flush_trade_notifications(force=service._trade_notification_force_immediate())
        entry_price, entry_time_iso, hold_duration_min = service._get_entry_context(
            "domestic",
            candidate.stock_code,
            fallback_price=held.avg_price,
        )
        service._reset_strategy_position(candidate.stock_code)
        service._register_exit_cooldown("domestic", candidate.stock_code, exit_reason)
        if held.avg_price > 0:
            service._on_realised(
                market="domestic",
                gross_pnl_krw=float(gross_pnl),
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
            net_pnl_krw, sell_commission_krw = service._estimate_domestic_net_pnl_krw(
                entry_price=float(entry_price or 0.0),
                exit_price=sell_price,
                qty=sell_qty,
            )
            service.repository.save_cycle_log(
                logged_at=datetime.now(timezone.utc).isoformat(),
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                action_bias="SELL_REAL",
                action_reason=exit_reason,
                price=sell_price,
                pnl_pct=pnl_pct,
                realized_pnl_usd=None,
                realized_pnl_krw=float(gross_pnl),
                holding_qty=sell_qty,
                rsi14=signal_snapshot.rsi14 if signal_snapshot else None,
                volume_ratio=signal_snapshot.volume_ratio if signal_snapshot else None,
                intraday_momentum=signal_snapshot.intraday_momentum if signal_snapshot else None,
                intraday_bar_return=(
                    signal_snapshot.intraday_bar_return if signal_snapshot else None
                ),
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
                is_session_trade=1 if service._is_session_owned(candidate.stock_code) else 0,
                consecutive_losses=int(getattr(service, "_consecutive_losses", 0) or 0),
                hold_cycles=service._estimate_hold_cycles(candidate.stock_code),
                entry_price=entry_price,
                qty_executed=sell_qty,
                net_pnl_usd=None,
                net_pnl_krw=net_pnl_krw,
                commission_usd=None,
                commission_krw=sell_commission_krw,
                is_virtual=0,
                orderable_qty=held.orderable_qty,
                stock_name=candidate.stock_name,
                hold_duration_min=hold_duration_min,
                entry_time=entry_time_iso,
                cb_active=service._cb_active_flag(),
                pool_size=service._pool_size_for_market("domestic"),
                activity_score=candidate.activity_score,
            )
        service._persist_trade_state(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
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
            "market": "domestic",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": sell_qty,
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
