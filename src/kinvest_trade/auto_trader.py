from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .auto_trade_math import (
    TradeFeeEstimate,
    estimate_capital_gains_tax_krw,
    estimate_fx_fee_krw,
    estimate_trade_fees,
)
from .adaptive_params import AdaptiveOverride, apply_override, compute_adaptive_override
from .client import KisApiError, KisRestClient
from .config import AppConfig
from .market_sessions import is_us_orderable_session_for_env
from .message_format import format_pct, format_reason_korean, format_side_korean, format_usd
from .momentum_policy import (
    evaluate_entry_setup,
    evaluate_exit_setup,
    evaluate_scale_in_setup,
)
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    extract_price_series,
)
from .time_utils import format_kst, format_kst_korean

StrategySnapshot = MovingAverageSnapshot


@dataclass(slots=True)
class TradeDecision:
    side: str | None
    qty: int
    reason: str


@dataclass(slots=True)
class AutoPosition:
    qty: int = 0
    avg_price: float = 0.0
    avg_fx_rate_krw: float = 0.0
    entry_fees_usd: float = 0.0
    entry_fx_fees_krw: float = 0.0
    opened_at: datetime | None = None
    hold_cycles: int = 0
    peak_price: float = 0.0
    scale_in_count: int = 0
    partial_exit_count: int = 0
    last_buy_cycle: int = 0
    last_sell_cycle: int = 0


@dataclass(slots=True)
class RealizedBreakdown:
    gross_pnl_usd: float = 0.0
    net_pnl_usd: float = 0.0
    net_pnl_krw: float = 0.0
    fees_usd: float = 0.0
    fx_pnl_krw: float = 0.0
    estimated_tax_delta_krw: float = 0.0


@dataclass(slots=True)
class AutoTradeSummary:
    run_id: int
    decision_count: int
    skip_count: int
    action_count: int
    buy_count: int
    sell_count: int
    realized_pnl_usd: float
    realized_pnl_net_usd: float
    realized_pnl_net_krw: float
    estimated_tax_krw: float
    fees_total_usd: float
    fx_pnl_krw: float
    last_price: float
    final_position_qty: int
    completion_reason: str


class SoxlAutoTrader:
    """Liquidity and momentum based overseas auto trader."""

    def __init__(
        self,
        config: AppConfig,
        client: KisRestClient,
        repository: SqliteRepository,
        notifier: TelegramNotifier,
    ) -> None:
        self.config = config
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.position = AutoPosition()
        self.flat_cycles = 0
        self.loop_count = 0
        self.last_exit_cycle = 0
        self.last_fx_rate_krw = config.auto_trade.usd_krw_fallback_rate
        self.last_available_usd: float = 0.0
        self._daily_closes: list[float] = []
        self._minute_closes: list[float] = []
        self._minute_highs: list[float] = []
        self._minute_lows: list[float] = []
        self._minute_volumes: list[float] = []
        self._daily_refreshed_at: datetime | None = None
        self._intraday_refreshed_at: datetime | None = None
        self._last_adaptive_override = AdaptiveOverride()

    async def run(self) -> AutoTradeSummary:
        auto = self.config.auto_trade
        cleaned_runs = self.repository.abort_stale_auto_trade_runs(
            older_than_minutes=auto.stale_run_grace_minutes,
            reason="auto-marked ABORTED because a newer auto-run started after the grace window.",
        )
        run_id = self.repository.create_auto_trade_run(
            mode=auto.mode,
            profile=self.config.credentials.profile_name,
            symbol=auto.symbol,
            exchange_code=auto.exchange_code,
            max_actions=auto.max_actions_per_run,
            notes=(
                "volume breakout momentum policy"
                if cleaned_runs <= 0
                else f"volume breakout momentum policy; stale_runs_aborted={cleaned_runs}"
            ),
        )

        await self._sync_startup_position()
        await self._send_start_message(run_id)

        action_count = 0
        decision_count = 0
        skip_count = 0
        buy_count = 0
        sell_count = 0
        realized_pnl_usd = 0.0
        realized_pnl_net_usd = 0.0
        realized_pnl_net_krw = 0.0
        fees_total_usd = 0.0
        fx_pnl_krw = 0.0
        estimated_tax_krw = 0.0
        last_price = 0.0
        action_limit = auto.max_actions_per_run if auto.max_actions_per_run > 0 else None
        decision_limit = auto.max_decision_cycles_per_run if auto.max_decision_cycles_per_run > 0 else None

        run_status = "FINISHED"
        run_notes = ""
        completion_reason = "manual_stop_or_market_close"
        try:
            while True:
                now = datetime.now(timezone.utc)
                if not is_us_orderable_session_for_env(now, self.config.credentials.env):
                    completion_reason = "market_closed"
                    break
                if action_limit is not None and action_count >= action_limit:
                    completion_reason = "max_actions_reached"
                    break
                if decision_limit is not None and decision_count >= decision_limit:
                    completion_reason = "max_decision_cycles_reached"
                    break

                quote = await self.client.get_overseas_price(auto.symbol, auto.exchange_code)
                last_price = self._parse_float(quote.get("last_price"))
                bid = self._parse_float(quote.get("bid"))
                ask = self._parse_float(quote.get("ask"))

                self.loop_count += 1
                decision_count += 1
                snapshot = await self._build_strategy_snapshot(
                    last_price=last_price,
                    bid=bid,
                    ask=ask,
                    captured_at=now,
                )
                decision = self._decide_action(snapshot)

                if decision.side is None or decision.qty <= 0:
                    skip_count += 1
                    if (
                        self._last_adaptive_override.take_profit_pct is not None
                        or self._last_adaptive_override.volume_spike_ratio is not None
                    ):
                        self.repository.save_heartbeat(
                            "ADAPTIVE_OVERRIDE",
                            (
                                f"run_id={run_id} "
                                f"tp={self._last_adaptive_override.take_profit_pct} "
                                f"sl={self._last_adaptive_override.stop_loss_pct} "
                                f"vspike={self._last_adaptive_override.volume_spike_ratio} "
                                f"hold={self._last_adaptive_override.max_hold_cycles} "
                                f"atr={snapshot.atr_pct:.4f} "
                                f"vr={snapshot.volume_ratio:.2f}"
                            ),
                        )
                    self.repository.save_heartbeat(
                        "AUTO_TRADE_SKIP",
                        (
                            f"run_id={run_id} turn={decision_count}/{self._limit_label(decision_limit)} "
                            f"symbol={auto.symbol} price={last_price:.4f} "
                            f"regime={snapshot.regime} reason={decision.reason}"
                        ),
                    )
                    await asyncio.sleep(auto.poll_interval_sec)
                    continue

                fx_rate_krw, max_buy_qty = await self._refresh_fx_context(last_price)
                executed_qty = decision.qty
                if decision.side == "buy":
                    executed_qty = min(decision.qty, max_buy_qty or decision.qty)
                    executed_qty = min(executed_qty, max(auto.max_position_qty - self.position.qty, 0))
                else:
                    executed_qty = min(decision.qty, self.position.qty)

                if executed_qty <= 0:
                    skip_count += 1
                    self.repository.save_heartbeat(
                        "AUTO_TRADE_SKIP",
                        (
                            f"run_id={run_id} turn={decision_count}/{self._limit_label(decision_limit)} "
                            f"side={decision.side.upper()} symbol={auto.symbol} "
                            f"reason=qty_capped_to_zero original_qty={decision.qty}"
                        ),
                    )
                    await asyncio.sleep(auto.poll_interval_sec)
                    continue

                try:
                    response = await self.client.place_overseas_order_for_current_session(
                        side=decision.side,
                        symbol=auto.symbol,
                        exchange_code=auto.exchange_code,
                        qty=executed_qty,
                        price=f"{last_price:.4f}",
                        order_division="00",
                        now_utc=now,
                    )
                except KisApiError as exc:
                    if (
                        "미국주식 주간거래는 제공하지 않습니다" in str(exc)
                        or "KIS mock currently supports US order tests only during the US regular session" in str(exc)
                    ):
                        skip_count += 1
                        self.repository.save_heartbeat(
                            "AUTO_TRADE_SKIP",
                            (
                                f"run_id={run_id} turn={decision_count}/{self._limit_label(decision_limit)} "
                                f"symbol={auto.symbol} reason=mock_us_session_not_supported"
                            ),
                        )
                        await asyncio.sleep(auto.poll_interval_sec)
                        continue
                    if decision.side == "sell" and "모의투자 잔고내역이 없습니다" in str(exc):
                        self.repository.save_heartbeat(
                            "AUTO_TRADE_RETRY",
                            (
                                f"run_id={run_id} side=SELL symbol={auto.symbol} "
                                "reason=waiting_broker_position_sync"
                            ),
                        )
                        await asyncio.sleep(auto.poll_interval_sec)
                        continue
                    raise

                broker_order_no = (
                    (response.get("output") or {}).get("ODNO")
                    if isinstance(response, dict)
                    else None
                )
                fee_estimate = estimate_trade_fees(
                    side=decision.side,
                    qty=executed_qty,
                    price=last_price,
                    commission_rate=auto.commission_rate,
                    sec_fee_rate=auto.sec_fee_rate,
                )

                action_count += 1
                realized = RealizedBreakdown()
                estimated_tax_before = estimated_tax_krw
                avg_price_before_fill = 0.0
                hold_cycles_before_fill = 0
                if decision.side == "buy":
                    buy_count += 1
                    self._apply_buy_fill(
                        qty=executed_qty,
                        price=last_price,
                        fx_rate_krw=fx_rate_krw,
                        fee_estimate=fee_estimate,
                    )
                    self.flat_cycles = 0
                else:
                    sell_count += 1
                    avg_price_before_fill = self.position.avg_price
                    hold_cycles_before_fill = self.position.hold_cycles
                    realized = self._apply_sell_fill(
                        qty=executed_qty,
                        price=last_price,
                        fx_rate_krw=fx_rate_krw,
                        fee_estimate=fee_estimate,
                        cumulative_net_pnl_krw_before_tax=realized_pnl_net_krw,
                    )
                    realized_pnl_usd += realized.gross_pnl_usd
                    realized_pnl_net_usd += realized.net_pnl_usd
                    realized_pnl_net_krw += realized.net_pnl_krw
                    fees_total_usd += realized.fees_usd
                    fx_pnl_krw += realized.fx_pnl_krw
                    estimated_tax_krw = estimate_capital_gains_tax_krw(
                        cumulative_net_pnl_krw=realized_pnl_net_krw,
                        annual_tax_free_allowance_krw=auto.annual_tax_free_allowance_krw,
                        capital_gains_tax_rate=auto.capital_gains_tax_rate,
                    )
                    realized.estimated_tax_delta_krw = estimated_tax_krw - estimated_tax_before
                    if self.position.qty <= 0:
                        self.flat_cycles = 0
                        self.last_exit_cycle = self.loop_count

                self.repository.save_auto_trade_action(
                    run_id=run_id,
                    action_no=action_count,
                    created_at=now.isoformat(),
                    side=decision.side.upper(),
                    symbol=auto.symbol,
                    qty=executed_qty,
                    price=last_price,
                    reason=decision.reason,
                    broker_order_no=broker_order_no,
                    status="FILLED",
                    realized_pnl_usd=realized.gross_pnl_usd,
                    realized_pnl_net_usd=realized.net_pnl_usd,
                    realized_pnl_net_krw=realized.net_pnl_krw,
                    fees_usd=realized.fees_usd,
                    fx_rate_krw=fx_rate_krw,
                    fx_pnl_krw=realized.fx_pnl_krw,
                    estimated_tax_delta_krw=realized.estimated_tax_delta_krw,
                    raw_payload=response,
                )
                self.repository.save_heartbeat(
                    "AUTO_TRADE_FILL",
                    (
                        f"run_id={run_id} action={action_count}/{self._limit_label(action_limit)} "
                        f"side={decision.side.upper()} symbol={auto.symbol} "
                        f"qty={executed_qty} price={last_price:.4f} reason={decision.reason}"
                    ),
                )
                if auto.telegram_notify_each_fill:
                    await self._send_fill_message(
                        run_id=run_id,
                        action_count=action_count,
                        side=decision.side.upper(),
                        qty=executed_qty,
                        price=last_price,
                        reason=decision.reason,
                        realized=realized,
                        cumulative_pnl_net_krw=realized_pnl_net_krw,
                        snapshot=snapshot,
                        captured_at=now,
                        avg_price_before_fill=avg_price_before_fill,
                        hold_cycles_before_fill=hold_cycles_before_fill,
                    )

                await asyncio.sleep(auto.poll_interval_sec)
            run_notes = json.dumps(
                {
                    "symbol": auto.symbol,
                    "exchange_code": auto.exchange_code,
                    "final_position_qty": self.position.qty,
                    "final_avg_price": self.position.avg_price,
                    "final_avg_fx_rate_krw": self.position.avg_fx_rate_krw,
                    "decision_count": decision_count,
                    "skip_count": skip_count,
                    "completion_reason": completion_reason,
                    "action_limit": action_limit,
                    "decision_limit": decision_limit,
                    "strategy": (
                        f"daily_ma{auto.daily_fast_window}/{auto.daily_slow_window}"
                        f"_volume_spike{auto.volume_spike_ratio}"
                        f"_breakout{auto.breakout_lookback_bars}"
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            run_status = "FAILED"
            run_notes = json.dumps(
                {
                    "symbol": auto.symbol,
                    "exchange_code": auto.exchange_code,
                    "final_position_qty": self.position.qty,
                    "decision_count": decision_count,
                    "skip_count": skip_count,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
            raise
        finally:
            self.repository.finish_auto_trade_run(
                run_id=run_id,
                status=run_status,
                realized_pnl_usd=realized_pnl_usd,
                realized_pnl_net_usd=realized_pnl_net_usd,
                realized_pnl_net_krw=realized_pnl_net_krw,
                fees_total_usd=fees_total_usd,
                fx_pnl_krw=fx_pnl_krw,
                estimated_tax_krw=estimated_tax_krw,
                notes=run_notes,
            )

        summary = AutoTradeSummary(
            run_id=run_id,
            decision_count=decision_count,
            skip_count=skip_count,
            action_count=action_count,
            buy_count=buy_count,
            sell_count=sell_count,
            realized_pnl_usd=realized_pnl_usd,
            realized_pnl_net_usd=realized_pnl_net_usd,
            realized_pnl_net_krw=realized_pnl_net_krw,
            estimated_tax_krw=estimated_tax_krw,
            fees_total_usd=fees_total_usd,
            fx_pnl_krw=fx_pnl_krw,
            last_price=last_price,
            final_position_qty=self.position.qty,
            completion_reason=completion_reason,
        )
        await self._send_final_message(summary)
        return summary

    async def _sync_startup_position(self) -> None:
        auto = self.config.auto_trade
        balance = await self.client.get_overseas_balance(
            exchange_code=auto.exchange_code,
            currency_code=auto.currency_code,
        )
        for row in balance.get("positions", []):
            if str(row.get("ovrs_pdno", "")).strip().upper() != auto.symbol.upper():
                continue

            qty = int(float(str(row.get("ovrs_cblc_qty", "0") or "0")))
            avg_price = self._parse_float(row.get("pchs_avg_pric"))
            if qty > 0:
                self.position.qty = qty
                if avg_price > 0:
                    self.position.avg_price = avg_price
                else:
                    self.position.avg_price = 0.0
                    self.repository.save_heartbeat(
                        "POSITION_AVG_PRICE_FALLBACK",
                        (
                            f"symbol={auto.symbol.upper()} "
                            "avg_price_from_broker=0 fallback_unavailable"
                        ),
                    )
                self.position.avg_fx_rate_krw = self.last_fx_rate_krw
                self.position.opened_at = datetime.now(timezone.utc)
                self.position.hold_cycles = 0
                self.position.peak_price = avg_price if avg_price > 0 else 0.0
            return

    async def _build_strategy_snapshot(
        self,
        *,
        last_price: float,
        bid: float,
        ask: float,
        captured_at: datetime,
    ) -> StrategySnapshot:
        await self._refresh_chart_context(captured_at)
        return build_moving_average_snapshot(
            price=last_price,
            bid=bid,
            ask=ask,
            daily_closes=self._daily_closes,
            minute_closes=self._minute_closes,
            minute_highs=self._minute_highs,
            minute_lows=self._minute_lows,
            minute_volumes=self._minute_volumes,
            daily_fast_window=self.config.auto_trade.daily_fast_window,
            daily_slow_window=self.config.auto_trade.daily_slow_window,
            intraday_fast_window=self.config.auto_trade.intraday_fast_window,
            intraday_slow_window=self.config.auto_trade.intraday_slow_window,
            volatility_window=self.config.auto_trade.volatility_window,
            momentum_window=self.config.auto_trade.momentum_window,
            volume_window=self.config.auto_trade.volume_window,
            breakout_lookback_bars=self.config.auto_trade.breakout_lookback_bars,
            bollinger_window=self.config.auto_trade.bollinger_window,
            bollinger_stddev=self.config.auto_trade.bollinger_stddev,
            atr_window=self.config.auto_trade.atr_window,
        )

    async def _refresh_chart_context(self, captured_at: datetime) -> None:
        auto = self.config.auto_trade
        daily_due = (
            self._daily_refreshed_at is None
            or (captured_at - self._daily_refreshed_at).total_seconds()
            >= auto.daily_chart_refresh_sec
            or len(self._daily_closes) < auto.daily_slow_window
        )
        intraday_due = (
            self._intraday_refreshed_at is None
            or (captured_at - self._intraday_refreshed_at).total_seconds()
            >= auto.intraday_chart_refresh_sec
            or len(self._minute_closes) < auto.intraday_slow_window
        )

        if daily_due:
            try:
                rows = await self.client.get_overseas_daily_prices(
                    auto.symbol,
                    auto.exchange_code,
                    base_date="",
                    adjusted_price=True,
                )
                series = extract_price_series(rows, close_fields=("clos", "close", "last"))
                if series.closes:
                    self._daily_closes = series.closes
                    self._daily_refreshed_at = captured_at
            except KisApiError:
                pass

        if intraday_due:
            try:
                rows = await self.client.get_overseas_minute_chart(
                    auto.symbol,
                    auto.exchange_code,
                    interval_minutes=auto.intraday_bar_minutes,
                    include_previous_day=True,
                    record_count=max(
                        auto.intraday_slow_window + 8,
                        auto.min_history_points,
                        auto.breakout_lookback_bars + 6,
                        auto.bollinger_window + 4,
                        auto.atr_window + 4,
                        40,
                    ),
                )
                series = extract_price_series(
                    rows,
                    close_fields=("last", "clos", "close"),
                    high_fields=("high",),
                    low_fields=("low",),
                    volume_fields=("evol", "volume"),
                )
                if series.closes:
                    self._minute_closes = series.closes
                    self._minute_highs = series.highs
                    self._minute_lows = series.lows
                    self._minute_volumes = series.volumes
                    self._intraday_refreshed_at = captured_at
            except KisApiError:
                pass

    def _decide_action(self, snapshot: StrategySnapshot) -> TradeDecision:
        _override = compute_adaptive_override(self.config.auto_trade, snapshot)
        self._last_adaptive_override = _override
        auto = apply_override(self.config.auto_trade, _override)

        if snapshot.spread_pct > auto.max_spread_pct:
            return TradeDecision(None, 0, "spread_too_wide")
        if not snapshot.has_required_context:
            return TradeDecision(None, 0, "warmup_context")

        if self.position.qty <= 0:
            self.flat_cycles += 1
            entry_setup = evaluate_entry_setup(auto, snapshot)
            cooldown_block = (
                self.last_exit_cycle > 0
                and (self.loop_count - self.last_exit_cycle) < auto.force_reentry_after_cycles
            )
            if cooldown_block and not entry_setup.ready:
                return TradeDecision(None, 0, "reentry_cooldown")
            if not entry_setup.ready:
                return TradeDecision(None, 0, entry_setup.reason)

            qty = self._determine_buy_qty(
                auto=auto,
                snapshot=snapshot,
                scale_in=False,
                urgent=entry_setup.urgent,
            )
            if not self._entry_has_sufficient_edge(
                auto=auto,
                snapshot=snapshot,
                qty=qty,
                target_reason=entry_setup.reason,
            ):
                return TradeDecision(None, 0, "entry_edge_too_small")
            return TradeDecision("buy", qty, entry_setup.reason)

        self.position.hold_cycles += 1
        self.position.peak_price = max(self.position.peak_price, snapshot.price)
        pnl_pct = 0.0
        if self.position.avg_price > 0:
            pnl_pct = (snapshot.price - self.position.avg_price) / self.position.avg_price
        drawdown_from_peak = self._drawdown_from_peak(snapshot.price)
        exit_setup = evaluate_exit_setup(
            auto,
            snapshot,
            pnl_pct,
            drawdown_from_peak=drawdown_from_peak,
            hold_cycles=self.position.hold_cycles,
            position_qty=self.position.qty,
            partial_exit_done=self.position.partial_exit_count > 0,
        )
        if exit_setup.action == "sell":
            return TradeDecision("sell", self.position.qty, exit_setup.reason)
        if exit_setup.action == "sell_partial":
            return TradeDecision(
                "sell",
                self._determine_sell_qty(full_exit=False),
                exit_setup.reason,
            )

        if (
            auto.allow_scale_in
            and self.position.qty < auto.max_position_qty
            and (self.loop_count - self.position.last_buy_cycle) >= auto.scale_in_cooldown_cycles
        ):
            scale_in_setup = evaluate_scale_in_setup(
                auto,
                snapshot,
                pnl_pct=pnl_pct,
                position_qty=self.position.qty,
                partial_exit_done=self.position.partial_exit_count > 0,
            )
            if scale_in_setup.ready:
                qty = self._determine_buy_qty(
                    auto=auto,
                    snapshot=snapshot,
                    scale_in=True,
                    urgent=False,
                )
                if self._entry_has_sufficient_edge(
                    auto=auto,
                    snapshot=snapshot,
                    qty=qty,
                    target_reason=scale_in_setup.reason,
                ):
                    return TradeDecision("buy", qty, scale_in_setup.reason)

        return TradeDecision(None, 0, exit_setup.reason)

    def _determine_buy_qty(
        self,
        *,
        auto,
        snapshot: StrategySnapshot,
        scale_in: bool,
        urgent: bool,
    ) -> int:
        remaining = max(auto.max_position_qty - self.position.qty, 0)
        if remaining <= 0:
            return 0

        if auto.use_slot_sizing and snapshot.price > 0:
            available_usd = self.last_available_usd
            if available_usd > 0:
                slot_budget = available_usd * auto.slot_max_pct
                size_pct = auto.slot_scale_in_pct if scale_in else auto.slot_entry_pct
                if not scale_in:
                    if snapshot.volume_ratio >= auto.volume_spike_ratio * 1.5:
                        size_pct = min(size_pct * 1.5, auto.slot_max_pct * 0.5)
                    if urgent:
                        size_pct = min(size_pct * 2.0, auto.slot_max_pct * 0.5)
                slot_qty = max(1, int(slot_budget * size_pct / snapshot.price))
                return max(1, min(slot_qty, remaining))

        if scale_in:
            return max(1, min(remaining, math.ceil(max(self.position.qty, 1) / 2)))

        base_qty = auto.quantity
        if snapshot.volume_ratio >= (auto.volume_spike_ratio * 1.5):
            base_qty += 1
        if snapshot.intraday_momentum >= (auto.min_intraday_momentum_pct * 2.0):
            base_qty += 1
        if urgent:
            base_qty += 1

        return max(1, min(base_qty, remaining))

    def _determine_sell_qty(self, *, full_exit: bool) -> int:
        auto = self.config.auto_trade
        if full_exit or not auto.allow_partial_exit or self.position.qty <= 1:
            return self.position.qty
        return max(1, math.ceil(self.position.qty / 2))

    def _entry_has_sufficient_edge(
        self,
        *,
        auto,
        snapshot: StrategySnapshot,
        qty: int,
        target_reason: str,
    ) -> bool:
        if qty <= 0 or snapshot.price <= 0:
            return False

        target_pct = self._target_profit_pct_for_reason(
            auto=auto,
            reason=target_reason,
            snapshot=snapshot,
        )
        gross_reward_usd = snapshot.price * qty * target_pct
        roundtrip_fees_usd = self._estimate_roundtrip_fees_usd(price=snapshot.price, qty=qty)
        gross_risk_usd = snapshot.price * qty * self._soft_break_band_pct(snapshot, auto=auto)

        if gross_reward_usd <= 0 or gross_risk_usd <= 0:
            return False

        reward_cost_ratio = (
            gross_reward_usd / roundtrip_fees_usd if roundtrip_fees_usd > 0 else 999.0
        )
        reward_risk_ratio = gross_reward_usd / gross_risk_usd

        if reward_cost_ratio < auto.min_expected_reward_cost_ratio:
            self.repository.save_heartbeat(
                "EDGE_FAIL_COST",
                (
                    f"symbol={auto.symbol} price={snapshot.price:.4f} qty={qty} "
                    f"tp_pct={target_pct:.4f} fee={roundtrip_fees_usd:.4f} "
                    f"reward/cost={reward_cost_ratio:.3f} "
                    f"required={auto.min_expected_reward_cost_ratio} reason={target_reason}"
                ),
            )
            return False

        if reward_risk_ratio < auto.min_expected_reward_risk_ratio:
            self.repository.save_heartbeat(
                "EDGE_FAIL_RISK",
                (
                    f"symbol={auto.symbol} price={snapshot.price:.4f} qty={qty} "
                    f"tp_pct={target_pct:.4f} risk={gross_risk_usd:.4f} "
                    f"reward/risk={reward_risk_ratio:.3f} "
                    f"required={auto.min_expected_reward_risk_ratio} reason={target_reason}"
                ),
            )
            return False
        return True

    def _target_profit_pct_for_reason(
        self,
        *,
        auto,
        reason: str,
        snapshot: StrategySnapshot,
    ) -> float:
        reward_from_atr = snapshot.atr_pct * max(auto.atr_trailing_stop_multiplier, 1.0)
        reward_from_breakout = max(snapshot.breakout_distance_pct, 0.0) * 0.5
        if reason == "momentum_scale_in":
            return max(auto.take_profit_pct * 0.8, reward_from_atr, reward_from_breakout)
        return max(auto.take_profit_pct, reward_from_atr, reward_from_breakout)

    def _estimate_roundtrip_fees_usd(self, *, price: float, qty: int) -> float:
        auto = self.config.auto_trade
        buy_fees = estimate_trade_fees(
            side="buy",
            qty=qty,
            price=price,
            commission_rate=auto.commission_rate,
            sec_fee_rate=auto.sec_fee_rate,
        )
        sell_fees = estimate_trade_fees(
            side="sell",
            qty=qty,
            price=price,
            commission_rate=auto.commission_rate,
            sec_fee_rate=auto.sec_fee_rate,
        )
        return buy_fees.total_fees_usd + sell_fees.total_fees_usd

    async def _refresh_fx_context(self, last_price: float) -> tuple[float, int]:
        auto = self.config.auto_trade
        fx_rate = self.last_fx_rate_krw or auto.usd_krw_fallback_rate
        max_buy_qty = auto.max_position_qty
        self.last_available_usd = 0.0

        try:
            possible = await self.client.get_overseas_possible_order(
                symbol=auto.symbol,
                exchange_code=auto.exchange_code,
                price=f"{last_price:.4f}",
            )
        except KisApiError:
            return fx_rate, max_buy_qty

        raw = possible.get("raw", {}) if isinstance(possible, dict) else {}
        parsed_fx_rate = self._parse_float(raw.get("exrt"))
        if parsed_fx_rate > 0:
            fx_rate = parsed_fx_rate
            self.last_fx_rate_krw = parsed_fx_rate

        parsed_max_qty = self._parse_int(
            raw.get("max_ord_psbl_qty") or possible.get("max_order_quantity")
        )
        if parsed_max_qty > 0:
            max_buy_qty = parsed_max_qty

        parsed_available = self._parse_float(
            raw.get("ord_psbl_frcr_amt_wcrc") or raw.get("frcr_ord_psbl_amt1")
        )
        if parsed_available > 0 and fx_rate > 0:
            self.last_available_usd = parsed_available / fx_rate
        elif parsed_max_qty > 0 and last_price > 0:
            self.last_available_usd = parsed_max_qty * last_price

        return fx_rate, max_buy_qty

    def _apply_buy_fill(
        self,
        *,
        qty: int,
        price: float,
        fx_rate_krw: float,
        fee_estimate: TradeFeeEstimate,
    ) -> None:
        existing_notional = self.position.avg_price * self.position.qty
        new_notional = price * qty
        total_qty = self.position.qty + qty
        total_notional = existing_notional + new_notional

        if total_qty > 0:
            self.position.avg_price = total_notional / total_qty

        if total_notional > 0 and self.position.qty > 0:
            self.position.avg_fx_rate_krw = (
                (existing_notional * self.position.avg_fx_rate_krw)
                + (new_notional * fx_rate_krw)
            ) / total_notional
        else:
            self.position.avg_fx_rate_krw = fx_rate_krw

        self.position.qty = total_qty
        self.position.entry_fees_usd += fee_estimate.total_fees_usd
        self.position.entry_fx_fees_krw += estimate_fx_fee_krw(
            notional_usd=fee_estimate.notional_usd,
            fx_rate_krw=fx_rate_krw,
            fx_fee_rate=self.config.auto_trade.fx_fee_rate,
        )
        self.position.opened_at = self.position.opened_at or datetime.now(timezone.utc)
        self.position.hold_cycles = 0
        self.position.peak_price = max(self.position.peak_price, price)
        self.position.scale_in_count += 1
        self.position.partial_exit_count = 0
        self.position.last_buy_cycle = self.loop_count

    def _apply_sell_fill(
        self,
        *,
        qty: int,
        price: float,
        fx_rate_krw: float,
        fee_estimate: TradeFeeEstimate,
        cumulative_net_pnl_krw_before_tax: float,
    ) -> RealizedBreakdown:
        auto = self.config.auto_trade
        qty_before = self.position.qty
        qty_sold = min(qty, qty_before)
        if qty_sold <= 0 or qty_before <= 0:
            return RealizedBreakdown()

        weight = qty_sold / qty_before
        entry_fee_alloc_usd = self.position.entry_fees_usd * weight
        entry_fx_fee_alloc_krw = self.position.entry_fx_fees_krw * weight
        cost_basis_usd = self.position.avg_price * qty_sold

        gross_pnl_usd = (price - self.position.avg_price) * qty_sold
        fees_usd = entry_fee_alloc_usd + fee_estimate.total_fees_usd
        net_pnl_usd = gross_pnl_usd - fees_usd
        fx_pnl_krw = (cost_basis_usd + entry_fee_alloc_usd) * (
            fx_rate_krw - self.position.avg_fx_rate_krw
        )
        sell_fx_fee_krw = estimate_fx_fee_krw(
            notional_usd=fee_estimate.notional_usd,
            fx_rate_krw=fx_rate_krw,
            fx_fee_rate=auto.fx_fee_rate,
        )
        net_pnl_krw = (
            (net_pnl_usd * fx_rate_krw)
            + fx_pnl_krw
            - entry_fx_fee_alloc_krw
            - sell_fx_fee_krw
        )

        self.position.qty -= qty_sold
        self.position.entry_fees_usd -= entry_fee_alloc_usd
        self.position.entry_fx_fees_krw -= entry_fx_fee_alloc_krw
        if self.position.qty <= 0:
            self.position = AutoPosition()
        else:
            self.position.partial_exit_count += 1
            self.position.last_sell_cycle = self.loop_count

        estimated_tax_after = estimate_capital_gains_tax_krw(
            cumulative_net_pnl_krw=cumulative_net_pnl_krw_before_tax + net_pnl_krw,
            annual_tax_free_allowance_krw=auto.annual_tax_free_allowance_krw,
            capital_gains_tax_rate=auto.capital_gains_tax_rate,
        )
        estimated_tax_before = estimate_capital_gains_tax_krw(
            cumulative_net_pnl_krw=cumulative_net_pnl_krw_before_tax,
            annual_tax_free_allowance_krw=auto.annual_tax_free_allowance_krw,
            capital_gains_tax_rate=auto.capital_gains_tax_rate,
        )

        return RealizedBreakdown(
            gross_pnl_usd=gross_pnl_usd,
            net_pnl_usd=net_pnl_usd,
            net_pnl_krw=net_pnl_krw,
            fees_usd=fees_usd,
            fx_pnl_krw=fx_pnl_krw - entry_fx_fee_alloc_krw - sell_fx_fee_krw,
            estimated_tax_delta_krw=estimated_tax_after - estimated_tax_before,
        )

    async def _send_start_message(self, run_id: int) -> None:
        auto = self.config.auto_trade
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][AUTO_TRADE_START]",
                    f"time={format_kst(datetime.now(timezone.utc))}",
                    f"run_id={run_id}",
                    f"symbol={auto.symbol}",
                    f"profile={self.config.credentials.profile_name}",
                    (
                        f"strategy=vol>{auto.volume_spike_ratio:.1f}x breakout({auto.breakout_lookback_bars}) "
                        f"with {auto.daily_fast_window}d/{auto.daily_slow_window}d filter"
                    ),
                    f"interval={auto.poll_interval_sec}s",
                    "until=manual_stop_or_market_close",
                ]
            )
        )

    async def _send_fill_message(
        self,
        *,
        run_id: int,
        action_count: int,
        side: str,
        qty: int,
        price: float,
        reason: str,
        realized: RealizedBreakdown,
        cumulative_pnl_net_krw: float,
        snapshot: StrategySnapshot,
        captured_at: datetime,
        avg_price_before_fill: float = 0.0,
        hold_cycles_before_fill: int = 0,
    ) -> None:
        auto = self.config.auto_trade
        lines = [
            "[KIS][AUTO_TRADE]",
            f"시각={format_kst_korean(captured_at)}",
            f"종목={auto.symbol}",
            f"동작={format_side_korean(side)}",
            f"가격=${price:.4f}",
            f"수량={qty}주",
            f"지표=RSI {snapshot.rsi14:.1f}, 거래량 {snapshot.volume_ratio:.1f}x",
            f"사유={format_reason_korean(reason)}",
        ]
        if side == "SELL":
            avg_price = avg_price_before_fill
            hold_sec = hold_cycles_before_fill * auto.poll_interval_sec
            hold_min = hold_sec // 60
            hold_rem = hold_sec % 60
            hold_str = f"{hold_min}m{hold_rem:02d}s"

            if avg_price > 0:
                pnl_pct = (price - avg_price) / avg_price
                lines.append(f"매입가=${avg_price:.4f}")
                lines.append(f"수익률={format_pct(pnl_pct)}")
            else:
                lines.append("매입가=알수없음")
                lines.append("수익률=알수없음")

            lines.append(f"손익={format_usd(realized.net_pnl_usd)}")
            lines.append(f"총손익={format_usd(realized.gross_pnl_usd)}")
            lines.append(f"원화손익={realized.net_pnl_krw:.0f}원")
            lines.append(f"누적손익={cumulative_pnl_net_krw:.0f}원")
            lines.append(f"보유시간={hold_str}")
        await self.notifier.send("\n".join(lines))

    async def _send_final_message(self, summary: AutoTradeSummary) -> None:
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][AUTO_TRADE_DONE]",
                    f"time={format_kst(datetime.now(timezone.utc))}",
                    f"run_id={summary.run_id}",
                    f"trades={summary.action_count} (buy={summary.buy_count}, sell={summary.sell_count}, skip={summary.skip_count})",
                    f"net_pnl={summary.realized_pnl_net_krw:.0f} KRW",
                    f"fees={summary.fees_total_usd:.4f} USD",
                    f"tax_est={summary.estimated_tax_krw:.0f} KRW",
                    f"final_qty={summary.final_position_qty}",
                    f"reason={summary.completion_reason}",
                ]
            )
        )

    def _soft_break_band_pct(self, snapshot: StrategySnapshot, *, auto=None) -> float:
        auto = auto or self.config.auto_trade
        return max(
            auto.stop_loss_pct,
            snapshot.atr_pct * auto.atr_soft_stop_multiplier,
        )

    def _drawdown_from_peak(self, price: float) -> float:
        if self.position.peak_price <= 0 or price <= 0:
            return 0.0
        return (price - self.position.peak_price) / self.position.peak_price

    @staticmethod
    def _limit_label(limit: int | None) -> str:
        return "unbounded" if limit is None else str(limit)

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)

    @staticmethod
    def _parse_int(value: object) -> int:
        if value is None:
            return 0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0
        return int(float(text))
