from __future__ import annotations

import dataclasses
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .momentum_policy import detect_market_regime
from .technical_signals import MovingAverageSnapshot
from .time_utils import ensure_timezone, parse_datetime

if TYPE_CHECKING:
    from .liquidity_lab import (
        DomesticScanResult,
        DomesticHeldPosition,
        LiquidityLabService,
        OverseasHeldPosition,
        OverseasScanResult,
        WatchTargetStatus,
    )

_logger = logging.getLogger(__name__)


class WatchStateHelper:
    """Persisted watch-state, cached signal fallback, and strategy restore helpers."""

    def __init__(self, service: "LiquidityLabService") -> None:
        self.service = service

    def remember_persisted_symbol_state(self, state: dict | None) -> None:
        if not state:
            return
        market = str(state.get("market", "") or "").strip()
        symbol = str(state.get("symbol", "") or "").strip().upper()
        if not market or not symbol:
            return
        cache = getattr(self.service, "_persisted_symbol_state", None)
        if cache is None:
            cache = {}
            self.service._persisted_symbol_state = cache
        cache[(market, symbol)] = state

    def get_persisted_symbol_state(self, market: str, symbol: str) -> dict | None:
        key = (market, symbol.strip().upper())
        cache = getattr(self.service, "_persisted_symbol_state", None)
        if cache is None:
            cache = {}
            self.service._persisted_symbol_state = cache
        cached = cache.get(key)
        if cached is not None:
            return cached
        repository = getattr(self.service, "repository", None)
        if repository is None:
            return None
        state = repository.get_lab_symbol_state(market, symbol.strip().upper())
        if state is not None:
            self.remember_persisted_symbol_state(state)
        return state

    def prime_cycle_exit_reference_prices(
        self,
        overseas_positions: list["OverseasHeldPosition"],
    ) -> None:
        refs: dict[str, float] = {}
        repository = getattr(self.service, "repository", None)
        if repository is None:
            self.service._cycle_exit_reference_prices = refs
            return

        for position in overseas_positions:
            symbol = position.symbol.strip().upper()
            if not symbol:
                continue
            state = repository.get_lab_symbol_state("overseas", symbol)
            if state is None:
                continue
            try:
                last_price = float(state.get("last_price") or 0.0)
            except (TypeError, ValueError):
                last_price = 0.0
            if last_price <= 0:
                continue
            refs[f"overseas:{symbol}"] = last_price
            self.remember_persisted_symbol_state(state)

        self.service._cycle_exit_reference_prices = refs

    @staticmethod
    def snapshot_from_payload(payload: dict | None) -> MovingAverageSnapshot | None:
        if not payload:
            return None
        try:
            return MovingAverageSnapshot(**payload)
        except TypeError:
            return None

    @staticmethod
    def with_live_price(
        snapshot: MovingAverageSnapshot,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> MovingAverageSnapshot:
        spread_pct = snapshot.spread_pct
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid_price = (bid + ask) / 2
            if mid_price > 0:
                spread_pct = (ask - bid) / mid_price
        return dataclasses.replace(
            snapshot,
            price=price,
            spread_pct=spread_pct,
        )

    def state_snapshot_with_live_price(
        self,
        state: dict | None,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> MovingAverageSnapshot | None:
        snapshot = self.snapshot_from_payload(
            state.get("snapshot_json") if state else None
        )
        if snapshot is None:
            return None
        return self.with_live_price(snapshot, price=price, bid=bid, ask=ask)

    async def get_overseas_signal_for_candidate(
        self,
        candidate: "OverseasScanResult",
    ) -> MovingAverageSnapshot | None:
        service = self.service
        symbol = candidate.symbol.upper()
        now_utc = datetime.now(timezone.utc)
        auto_trade_cfg = getattr(service.config, "auto_trade", None)
        refresh_sec = max(
            5,
            int(getattr(auto_trade_cfg, "intraday_chart_refresh_sec", 60) or 60),
        )
        cached = service._signal_cache.get(symbol)
        updated_map = getattr(service, "_signal_cache_updated_at", None)
        if updated_map is None:
            updated_map = {}
            service._signal_cache_updated_at = updated_map
        cached_at = updated_map.get(symbol)
        if (
            cached is not None
            and cached_at is not None
            and (now_utc - cached_at).total_seconds() < refresh_sec
        ):
            return self.with_live_price(
                cached,
                price=candidate.last_price,
                bid=candidate.bid,
                ask=candidate.ask,
            )

        snapshot = await service._load_overseas_signal(candidate)
        if snapshot is not None:
            service._signal_cache[symbol] = snapshot
            updated_map[symbol] = now_utc
            return snapshot

        if cached is not None:
            fallback = self.with_live_price(
                cached,
                price=candidate.last_price,
                bid=candidate.bid,
                ask=candidate.ask,
            )
            service._signal_cache[symbol] = fallback
            updated_map[symbol] = now_utc
            return fallback

        state = self.get_persisted_symbol_state("overseas", symbol)
        fallback = self.state_snapshot_with_live_price(
            state,
            price=candidate.last_price,
            bid=candidate.bid,
            ask=candidate.ask,
        )
        if fallback is not None:
            service._signal_cache[symbol] = fallback
            updated_map[symbol] = now_utc
            _logger.info(
                "overseas_signal_fallback_used symbol=%s source=persisted_state",
                symbol,
            )
        return fallback

    def persist_watch_target_state(
        self,
        watch_target: "WatchTargetStatus",
        *,
        pnl_pct: float | None = None,
        exit_by: str = "",
    ) -> None:
        service = self.service
        repository = getattr(service, "repository", None)
        if repository is None:
            return
        symbol = watch_target.code.strip().upper()
        manager = getattr(service, "_strategy_managers", {}).get(symbol)
        entry_price = None
        peak_price = None
        if manager is not None and manager.position is not None:
            entry_price = float(manager.position.entry_price)
            peak_price = float(manager.position.peak_price)
        state = self.get_persisted_symbol_state(watch_target.market, symbol) or {}
        strategy_flag = watch_target.strategy_flag or str(state.get("strategy_flag", "") or "")
        entry_by_value = watch_target.entry_by or str(state.get("entry_by", "") or "")
        signal_state = watch_target.signal_state or str(state.get("signal_state", "") or "")
        action_bias = watch_target.action_bias or str(state.get("action_bias", "") or "")
        note = watch_target.note or str(state.get("note", "") or "")
        snapshot_payload = (
            asdict(watch_target.signal_snapshot)
            if watch_target.signal_snapshot is not None
            else state.get("snapshot_json")
        )
        has_position = 1 if watch_target.holding_qty > 0 else 0
        updated_at = datetime.now(timezone.utc).isoformat()
        repository.upsert_lab_symbol_state(
            market=watch_target.market,
            symbol=symbol,
            exchange_code=watch_target.exchange_code,
            action_bias=action_bias,
            signal_state=signal_state,
            note=note,
            strategy_flag=strategy_flag,
            entry_by=entry_by_value,
            exit_by=exit_by,
            holding_qty=watch_target.holding_qty,
            last_price=watch_target.price,
            pnl_pct=pnl_pct,
            entry_price=entry_price,
            peak_price=peak_price,
            has_position=has_position,
            snapshot_json=snapshot_payload,
            updated_at=updated_at,
        )
        self.remember_persisted_symbol_state(
            {
                "market": watch_target.market,
                "symbol": symbol,
                "exchange_code": watch_target.exchange_code,
                "action_bias": action_bias,
                "signal_state": signal_state,
                "note": note,
                "strategy_flag": strategy_flag,
                "entry_by": entry_by_value,
                "exit_by": exit_by,
                "holding_qty": watch_target.holding_qty,
                "last_price": watch_target.price,
                "pnl_pct": pnl_pct,
                "entry_price": entry_price,
                "peak_price": peak_price,
                "has_position": has_position,
                "snapshot_json": snapshot_payload,
                "updated_at": updated_at,
            }
        )

    def persist_trade_state(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        action_bias: str,
        signal_state: str,
        note: str,
        holding_qty: int,
        last_price: float | None,
        pnl_pct: float | None,
        strategy_flag: str,
        entry_by: str,
        exit_by: str = "",
        signal_snapshot: MovingAverageSnapshot | None = None,
        has_position: bool,
    ) -> None:
        service = self.service
        repository = getattr(service, "repository", None)
        if repository is None:
            return
        manager = getattr(service, "_strategy_managers", {}).get(symbol.strip().upper())
        entry_price = None
        peak_price = None
        if manager is not None and manager.position is not None:
            entry_price = float(manager.position.entry_price)
            peak_price = float(manager.position.peak_price)
        elif last_price is not None and has_position:
            entry_price = float(last_price)
            peak_price = float(last_price)
        payload = asdict(signal_snapshot) if signal_snapshot is not None else None
        repository.upsert_lab_symbol_state(
            market=market,
            symbol=symbol.strip().upper(),
            exchange_code=exchange_code,
            action_bias=action_bias,
            signal_state=signal_state,
            note=note,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            holding_qty=holding_qty,
            last_price=last_price,
            pnl_pct=pnl_pct,
            entry_price=entry_price,
            peak_price=peak_price,
            has_position=1 if has_position else 0,
            snapshot_json=payload,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.remember_persisted_symbol_state(
            repository.get_lab_symbol_state(market, symbol.strip().upper())
        )

    def clear_stale_lab_position_states(
        self,
        *,
        domestic_positions: list["DomesticHeldPosition"],
        overseas_positions: list["OverseasHeldPosition"],
        refreshed_markets: set[str],
    ) -> None:
        service = self.service
        repository = getattr(service, "repository", None)
        if repository is None or not refreshed_markets:
            return
        active_keys: set[tuple[str, str]] = set()
        if "domestic" in refreshed_markets:
            active_keys.update(
                ("domestic", position.stock_code.strip())
                for position in domestic_positions
                if position.quantity > 0 and position.stock_code.strip()
            )
        if "overseas" in refreshed_markets:
            active_keys.update(
                ("overseas", position.symbol.strip().upper())
                for position in overseas_positions
                if position.quantity > 0 and position.symbol.strip()
            )
        cleared = repository.clear_stale_lab_positions(
            markets=refreshed_markets,
            active_keys=active_keys,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        if not cleared:
            return
        persisted = getattr(service, "_persisted_symbol_state", None)
        if persisted is not None:
            for row in cleared:
                persisted.pop(
                    (
                        str(row.get("market", "")).strip().lower(),
                        str(row.get("symbol", "")).strip().upper(),
                    ),
                    None,
                )
        service._save_event(
            event_type="lab_position_state_cleanup",
            detail={
                "count": len(cleared),
                "markets": sorted(refreshed_markets),
                "symbols": [
                    f"{row.get('market')}:{row.get('symbol')}"
                    for row in cleared[:30]
                ],
            },
        )

    def restore_strategy_contexts(
        self,
        *,
        domestic_positions: list["DomesticHeldPosition"],
        overseas_positions: list["OverseasHeldPosition"],
    ) -> None:
        for position in domestic_positions:
            self.restore_strategy_position(
                market="domestic",
                symbol=position.stock_code,
                exchange_code=None,
                quantity=position.quantity,
                avg_price=position.avg_price,
                current_price=position.current_price,
            )
        for position in overseas_positions:
            self.restore_strategy_position(
                market="overseas",
                symbol=position.symbol.upper(),
                exchange_code=position.exchange_code,
                quantity=position.quantity,
                avg_price=position.avg_price,
                current_price=position.current_price,
            )

    def restore_strategy_position(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        quantity: int,
        avg_price: float,
        current_price: float,
    ) -> None:
        del exchange_code
        service = self.service
        if quantity <= 0:
            return
        manager = service._get_strategy_manager(symbol)
        if manager.position is not None:
            return
        state = self.get_persisted_symbol_state(market, symbol)
        if state is None:
            return
        strategy_flag = str(state.get("strategy_flag", "") or "")
        entry_by = str(state.get("entry_by", "") or "")
        triggered = service._decode_strategy_ids(strategy_flag, entry_by)
        if not triggered:
            return
        entry_price = float(state.get("entry_price") or avg_price or current_price or 0.0)
        if entry_price <= 0:
            entry_price = max(float(avg_price or 0.0), float(current_price or 0.0))
        entry_time = parse_datetime(str(state.get("updated_at", "") or ""))
        manager.open_position(
            symbol=symbol.strip().upper(),
            entry_price=entry_price,
            triggered_by=triggered,
            entry_time=entry_time,
        )
        if manager.position is not None:
            restored_peak = float(state.get("peak_price") or 0.0)
            manager.position.peak_price = max(
                restored_peak,
                manager.position.entry_price,
                float(current_price or 0.0),
            )

    def save_cycle_log_from_watch_target(
        self,
        watch_target: "WatchTargetStatus",
        *,
        pnl_pct: float | None = None,
    ) -> None:
        service = self.service
        signal_snapshot = watch_target.signal_snapshot
        exit_by = ""
        if signal_snapshot is not None:
            _, _, exit_by = service._get_strategy_labels(
                watch_target.code,
                signal_snapshot,
            )
        service.repository.save_cycle_log(
            logged_at=datetime.now(timezone.utc).isoformat(),
            market=watch_target.market,
            symbol=watch_target.code,
            exchange_code=watch_target.exchange_code,
            action_bias=watch_target.action_bias,
            action_reason=watch_target.note or watch_target.action_bias,
            price=watch_target.price,
            pnl_pct=pnl_pct,
            holding_qty=watch_target.holding_qty,
            rsi14=signal_snapshot.rsi14 if signal_snapshot else None,
            volume_ratio=signal_snapshot.volume_ratio if signal_snapshot else None,
            intraday_momentum=signal_snapshot.intraday_momentum if signal_snapshot else None,
            intraday_bar_return=signal_snapshot.intraday_bar_return if signal_snapshot else None,
            minute_ma_fast=signal_snapshot.minute_ma_fast if signal_snapshot else None,
            minute_ma_slow=signal_snapshot.minute_ma_slow if signal_snapshot else None,
            activity_score=watch_target.activity_score,
            cycle_no=getattr(service, "_cycle_count", 0),
            session_id=getattr(service, "_session_id", ""),
            strategy_flag=watch_target.strategy_flag,
            entry_by=watch_target.entry_by,
            exit_by=exit_by,
            is_session_trade=0,
            vwap=signal_snapshot.vwap if signal_snapshot else None,
            macd_line=signal_snapshot.macd_line if signal_snapshot else None,
            macd_signal=signal_snapshot.macd_signal if signal_snapshot else None,
            macd_golden=int(signal_snapshot.macd_golden) if signal_snapshot else None,
            breakout_distance_pct=(
                signal_snapshot.breakout_distance_pct if signal_snapshot else None
            ),
            atr=signal_snapshot.atr if signal_snapshot else None,
            spread_pct=signal_snapshot.spread_pct if signal_snapshot else None,
            consecutive_losses=int(getattr(service, "_consecutive_losses", 0) or 0),
        )
        self.persist_watch_target_state(
            watch_target,
            pnl_pct=pnl_pct,
            exit_by=exit_by,
        )

    def build_watch_target_status(
        self,
        *,
        market: str,
        code: str,
        exchange_code: str | None,
        price: float,
        activity_score: float,
        signal_snapshot: MovingAverageSnapshot | None,
        held_position: "OverseasHeldPosition | DomesticHeldPosition | None" = None,
        holding_qty: int = 0,
    ) -> "WatchTargetStatus":
        service = self.service
        if signal_snapshot is None:
            persisted = self.get_persisted_symbol_state(market, code)
            fallback_snapshot = self.state_snapshot_with_live_price(
                persisted,
                price=price,
            )
            if fallback_snapshot is not None:
                if held_position is not None:
                    existing_flag = str(persisted.get("strategy_flag", "") or "")
                    existing_entry_by = str(persisted.get("entry_by", "") or "")
                    exit_setup = service._build_exit_setup(
                        fallback_snapshot,
                        held_position.pnl_pct,
                        holding_qty,
                        symbol=code,
                        take_profit_override=(
                            getattr(service.config.liquidity_lab, "overseas_take_profit_pct", None)
                            if market == "overseas"
                            else None
                        ),
                    )
                    if exit_setup.action in {"sell", "sell_partial"}:
                        return service._make_watch_target_status(
                            market=market,
                            code=code,
                            exchange_code=exchange_code,
                            price=price,
                            activity_score=activity_score,
                            signal_score=0.0,
                            action_bias="SELL",
                            signal_state="SELL_READY",
                            ma_summary=service._ma_relation_summary(fallback_snapshot),
                            note=exit_setup.reason,
                            holding_qty=holding_qty,
                            signal_snapshot=fallback_snapshot,
                            strategy_flag=existing_flag,
                            entry_by=existing_entry_by,
                        )
                    return service._make_watch_target_status(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="HOLD",
                        signal_state="HOLD",
                        ma_summary=service._ma_relation_summary(fallback_snapshot),
                        note=f"{exit_setup.note}|stale_signal_cache",
                        holding_qty=holding_qty,
                        signal_snapshot=fallback_snapshot,
                        strategy_flag=existing_flag,
                        entry_by=existing_entry_by,
                    )
                strategy_result = service._get_strategy_manager(code).evaluate(
                    code,
                    fallback_snapshot,
                    commit=False,
                )
                signal_state, note = service._derive_watch_state(
                    fallback_snapshot,
                    code,
                )
                block_reason = service._entry_strategy_block_reason(
                    market=market,
                    strategy_flag=strategy_result.flag,
                )
                if block_reason and (strategy_result.signal == "BUY" or signal_state == "BUY"):
                    return service._make_watch_target_status(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="WAIT",
                        signal_state="WAIT",
                        ma_summary=service._ma_relation_summary(fallback_snapshot),
                        note=f"[{strategy_result.flag or '-'}] {block_reason}|stale_signal_cache",
                        holding_qty=holding_qty,
                        signal_snapshot=fallback_snapshot,
                        strategy_flag=strategy_result.flag,
                        entry_by=strategy_result.entry_by,
                    )
                if strategy_result.signal == "BUY" or signal_state == "BUY":
                    return service._make_watch_target_status(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="WAIT",
                        signal_state="WAIT",
                        ma_summary=service._ma_relation_summary(fallback_snapshot),
                        note=f"[{strategy_result.flag or 'CACHE'}] stale_signal_cache_buy_blocked",
                        holding_qty=holding_qty,
                        signal_snapshot=fallback_snapshot,
                        strategy_flag=strategy_result.flag
                        or str(persisted.get("strategy_flag", "") or ""),
                        entry_by=strategy_result.entry_by
                        or str(persisted.get("entry_by", "") or ""),
                    )
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias=signal_state,
                    signal_state=signal_state,
                    ma_summary=service._ma_relation_summary(fallback_snapshot),
                    note=f"{note}|stale_signal_cache",
                    holding_qty=holding_qty,
                    signal_snapshot=fallback_snapshot,
                    strategy_flag=strategy_result.flag or str(persisted.get("strategy_flag", "") or ""),
                    entry_by=strategy_result.entry_by or str(persisted.get("entry_by", "") or ""),
                )
            return service._make_watch_target_status(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=0.0,
                action_bias="WAIT",
                signal_state="WARMUP",
                ma_summary="-",
                note="signal_unavailable",
                holding_qty=holding_qty,
                signal_snapshot=signal_snapshot,
                strategy_flag="" if persisted is None else str(persisted.get("strategy_flag", "") or ""),
                entry_by="" if persisted is None else str(persisted.get("entry_by", "") or ""),
            )

        existing_flag, existing_entry_by, _ = service._get_strategy_labels(code, signal_snapshot)
        if held_position is not None:
            exit_setup = service._build_exit_setup(
                signal_snapshot,
                held_position.pnl_pct,
                holding_qty,
                symbol=code,
                take_profit_override=(
                    getattr(service.config.liquidity_lab, "overseas_take_profit_pct", None)
                    if market == "overseas"
                    else None
                ),
            )
            if exit_setup.action in {"sell", "sell_partial"}:
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias="SELL",
                    signal_state="SELL_READY",
                    ma_summary=service._ma_relation_summary(signal_snapshot),
                    note=exit_setup.reason,
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=existing_flag,
                    entry_by=existing_entry_by,
                )
            return service._make_watch_target_status(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=0.0,
                action_bias="HOLD",
                signal_state="HOLD",
                ma_summary=service._ma_relation_summary(signal_snapshot),
                note=exit_setup.note,
                holding_qty=holding_qty,
                signal_snapshot=signal_snapshot,
                strategy_flag=existing_flag,
                entry_by=existing_entry_by,
            )

        entry_setup = service._evaluate_entry_setup(signal_snapshot, code)
        strategy_result = service._get_strategy_manager(code).evaluate(
            code,
            signal_snapshot,
            commit=False,
        )
        if strategy_result.signal == "BUY":
            block_reason = service._entry_strategy_block_reason(
                market=market,
                strategy_flag=strategy_result.flag,
            )
            if block_reason:
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=entry_setup.score,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=service._ma_relation_summary(signal_snapshot),
                    note=f"[{strategy_result.flag or '-'}] {block_reason}",
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_result.flag,
                    entry_by=strategy_result.entry_by,
                )
            liquidity_block_reason = service._entry_liquidity_block_reason(
                market=market,
                signal_snapshot=signal_snapshot,
            )
            if liquidity_block_reason:
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=entry_setup.score,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=service._ma_relation_summary(signal_snapshot),
                    note=f"[{strategy_result.flag or '-'}] {liquidity_block_reason}",
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_result.flag,
                    entry_by=strategy_result.entry_by,
                )
            if (
                market == "overseas"
                and strategy_result.flag in {"VWAP", "RSI"}
                and not entry_setup.ready
            ):
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=entry_setup.score,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=service._ma_relation_summary(signal_snapshot),
                    note=f"[{strategy_result.flag}] confirm_wait:{entry_setup.reason}",
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_result.flag,
                    entry_by=strategy_result.entry_by,
                )
            remaining_cooldown = service._cooldown_remaining_minutes(market, code)
            if remaining_cooldown > 0:
                remaining_min = max(1, int(remaining_cooldown))
                service._save_event(
                    event_type="cooldown_blocked",
                    market=market,
                    symbol=code,
                    detail={
                        "reason": "reentry_cooldown",
                        "remaining_min": remaining_min,
                    },
                )
                return service._make_watch_target_status(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=service._ma_relation_summary(signal_snapshot),
                    note=f"재진입대기 {remaining_min}분",
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag="",
                    entry_by="",
                )
            combined_score = (
                service._get_strategy_manager(code).buy_score(signal_snapshot)
                + entry_setup.score
            )
            return service._make_watch_target_status(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=round(combined_score, 2),
                action_bias="BUY",
                signal_state="BUY",
                ma_summary=service._ma_relation_summary(signal_snapshot),
                note=(
                    f"[{strategy_result.flag}] {entry_setup.reason}"
                    if entry_setup.ready
                    else f"[{strategy_result.flag}] strategy_buy_signal"
                ),
                holding_qty=holding_qty,
                signal_snapshot=signal_snapshot,
                strategy_flag=strategy_result.flag,
                entry_by=strategy_result.entry_by,
            )
        signal_state, note = service._derive_watch_state(signal_snapshot, code)
        return service._make_watch_target_status(
            market=market,
            code=code,
            exchange_code=exchange_code,
            price=price,
            activity_score=activity_score,
            signal_score=entry_setup.score,
            action_bias=signal_state,
            signal_state=signal_state,
            ma_summary=service._ma_relation_summary(signal_snapshot),
            note=note,
            holding_qty=holding_qty,
            signal_snapshot=signal_snapshot,
            strategy_flag=strategy_result.flag,
            entry_by=strategy_result.entry_by,
        )

    def select_domestic_buy_targets(
        self,
        domestic_ranked: list["DomesticScanResult"],
        watch_targets: list["WatchTargetStatus"],
        max_concurrent: int = 2,
    ) -> list["DomesticScanResult"]:
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic" and watch_target.action_bias == "BUY"
        ]
        if not ready_targets or max_concurrent <= 0:
            return []
        ready_targets.sort(
            key=lambda item: (item.signal_score, item.activity_score),
            reverse=True,
        )
        selected: list[DomesticScanResult] = []
        seen: set[str] = set()
        for watch_target in ready_targets:
            if len(selected) >= max_concurrent:
                break
            code = watch_target.code
            if code in seen:
                continue
            candidate = candidate_map.get(code)
            if candidate is None:
                continue
            selected.append(candidate)
            seen.add(code)
        return selected

    def select_domestic_exit_target(
        self,
        domestic_ranked: list["DomesticScanResult"],
        watch_targets: list["WatchTargetStatus"],
        held_positions: list["DomesticHeldPosition"],
    ) -> tuple["DomesticScanResult", "DomesticHeldPosition", str, MovingAverageSnapshot | None] | None:
        service = self.service
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        held_map = {position.stock_code: position for position in held_positions}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic"
            and watch_target.action_bias == "SELL"
            and watch_target.code in held_map
            and held_map[watch_target.code].quantity > 0
            and service._cooldown_remaining_minutes("domestic", watch_target.code) <= 0
        ]
        for held in held_positions:
            if held.orderable_qty <= 0:
                service._track_no_orderable_stall(
                    market="domestic",
                    symbol=held.stock_code,
                    holding_qty=held.quantity,
                )
                service._defer_no_orderable_position(
                    market="domestic",
                    symbol=held.stock_code,
                    holding_qty=held.quantity,
                    orderable_qty=held.orderable_qty,
                )
            else:
                service._clear_no_orderable_retry("domestic", held.stock_code)
                service._reset_no_orderable_stall("domestic", held.stock_code)
        if not ready_targets:
            return None
        best_target = min(
            ready_targets,
            key=lambda item: held_map.get(item.code).pnl_pct if item.code in held_map else 0.0,
        )
        candidate = candidate_map.get(best_target.code)
        held = held_map.get(best_target.code)
        if candidate is None or held is None:
            return None
        return candidate, held, best_target.note, None

    def select_overseas_buy_targets(
        self,
        overseas_ranked: list["OverseasScanResult"],
        watch_targets: list["WatchTargetStatus"],
        max_concurrent: int = 3,
        held_positions: list["OverseasHeldPosition"] | None = None,
    ) -> list["OverseasScanResult"]:
        service = self.service
        candidate_map = {candidate.symbol.upper(): candidate for candidate in overseas_ranked}
        held_symbols: set[str] = set()
        if held_positions:
            held_symbols = {
                held.symbol.upper()
                for held in held_positions
                if getattr(held, "quantity", 0) > 0
            }
        virtual_manager = getattr(service, "virtual_trades", None)
        if virtual_manager is not None:
            for position in virtual_manager.list_positions("overseas"):
                if position.qty > 0:
                    held_symbols.add(position.symbol.upper())
        service._track_rsi_threshold_blocks(watch_targets)
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "overseas"
            and watch_target.action_bias == "BUY"
            and watch_target.code.upper() not in held_symbols
            and not service._entry_strategy_block_reason(
                market=watch_target.market,
                strategy_flag=watch_target.strategy_flag,
            )
        ]
        if not ready_targets or max_concurrent <= 0:
            return []
        inverse_set = {
            symbol.upper()
            for symbol in getattr(service.config.liquidity_lab, "inverse_etf_symbols", [])
        }

        def sort_key(item: "WatchTargetStatus") -> tuple[int, float, float]:
            snapshot = item.signal_snapshot
            prefer_inverse = bool(
                snapshot is not None
                and detect_market_regime(snapshot) == "bear"
                and item.code.upper() in inverse_set
            )
            return (0 if prefer_inverse else 1, -item.signal_score, -item.activity_score)

        ready_targets.sort(key=sort_key)
        selected: list[OverseasScanResult] = []
        seen: set[str] = set()
        for watch_target in ready_targets:
            symbol = watch_target.code.upper()
            if symbol in seen:
                continue
            candidate = candidate_map.get(symbol)
            if candidate is None:
                continue
            selected.append(candidate)
            seen.add(symbol)
            if len(selected) >= max_concurrent:
                break
        return selected

    @staticmethod
    def remaining_overseas_entry_slots(
        positions: list["OverseasHeldPosition"],
        *,
        max_positions: int,
    ) -> int:
        open_symbols = {
            position.symbol.strip().upper()
            for position in positions
            if position.symbol.strip() and position.quantity > 0
        }
        return max(0, int(max_positions) - len(open_symbols))

    @staticmethod
    def select_primary_target(
        *,
        krx_open: bool,
        us_open: bool,
        us_orderable_in_profile: bool,
        domestic_ranked: list["DomesticScanResult"],
        overseas_ranked: list["OverseasScanResult"],
    ) -> tuple[str, str | None, str]:
        if krx_open and domestic_ranked:
            return "domestic", domestic_ranked[0].stock_code, "highest_current_activity_in_open_market"
        if us_orderable_in_profile and overseas_ranked:
            return "overseas", overseas_ranked[0].symbol, "highest_current_activity_in_open_market"
        if krx_open:
            return "domestic", None, "krx_open_but_no_candidate"
        if us_orderable_in_profile:
            return "overseas", None, "us_open_but_no_candidate"
        if us_open:
            return "none", None, "us_open_but_mock_session_not_supported"
        return "none", None, "no_supported_market_open"
