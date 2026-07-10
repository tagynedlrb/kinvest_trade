from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from .client import KisApiError, KisRestClient, parse_kis_number
from .config import AppConfig, OverseasCandidateConfig
from .market_sessions import (
    KST,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    is_us_regular_session,
    us_holiday_date_for_kis_session,
)
from .market_calendar import is_krx_holiday, is_nyse_holiday, market_status_summary
from .message_format import (
    format_krw,
    format_side_korean,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_usd,
)
from .adaptive_params import apply_override, compute_adaptive_override
from .momentum_policy import (
    derive_watch_state,
    detect_market_regime,
    evaluate_entry_setup,
    evaluate_exit_setup,
)
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .strategy import PriorityStrategyManager, STRATEGY_LABEL, StrategyID
from .technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    extract_price_series,
)
from .time_utils import ensure_timezone, format_kst, format_kst_korean, parse_datetime
from .tv_scanner import check_connectivity, scan_top_volume_surge

_logger = logging.getLogger(__name__)
_DEFAULT_OVERSEAS_EXCHANGE_CODES = ("NASD", "NYSE", "AMEX")


@dataclass(slots=True)
class DomesticScanResult:
    stock_code: str
    current_price: int
    best_ask: int
    best_bid: int
    spread_pct: float
    minute_change_pct: float
    intraday_turnover_krw: int
    volume_sum: int
    activity_score: float
    stock_name: str = ""


@dataclass(slots=True)
class OverseasScanResult:
    symbol: str
    exchange_code: str
    last_price: float
    bid: float
    ask: float
    spread_pct: float
    change_rate_pct: float
    volume: int
    orderable_qty: int
    fx_rate_krw: float
    activity_score: float


@dataclass(slots=True)
class UnifiedScanResult:
    market: str
    code: str
    exchange_code: str | None
    activity_score: float
    domestic: DomesticScanResult | None = None
    overseas: OverseasScanResult | None = None


@dataclass(slots=True)
class ExcludedCandidate:
    market: str
    code: str
    reasons: list[str]
    snapshot: dict


@dataclass(slots=True)
class VirtualPosition:
    market: str
    symbol: str
    exchange_code: str | None
    qty: int
    avg_price: float
    currency: str


@dataclass(slots=True)
class OverseasHeldPosition:
    symbol: str
    exchange_code: str
    quantity: int
    orderable_qty: int
    avg_price: float
    current_price: float
    pnl_pct: float
    is_virtual: bool = False


@dataclass(slots=True)
class DomesticHeldPosition:
    stock_code: str
    quantity: int
    orderable_qty: int
    avg_price: float
    current_price: float
    pnl_pct: float


@dataclass(slots=True)
class WatchTargetStatus:
    market: str
    code: str
    exchange_code: str | None
    price: float
    activity_score: float
    signal_score: float
    action_bias: str
    signal_state: str
    ma_summary: str
    note: str
    holding_qty: int = 0
    signal_snapshot: MovingAverageSnapshot | None = None
    strategy_flag: str = ""
    entry_by: str = ""


@dataclass(slots=True)
class LiquidityLabReport:
    scanned_at: str
    krx_market_open: bool
    us_market_open: bool
    us_market_session: str
    us_orderable_in_profile: bool
    primary_market: str
    primary_target: str | None
    primary_selection_reason: str
    domestic_ranked: list[DomesticScanResult]
    overseas_ranked: list[OverseasScanResult]
    domestic_excluded: list[ExcludedCandidate]
    overseas_excluded: list[ExcludedCandidate]
    domestic_positions: list[DomesticHeldPosition]
    overseas_positions: list[OverseasHeldPosition]
    watch_targets: list[WatchTargetStatus]
    estimated_api_calls_per_cycle: int
    domestic_order: dict | None
    overseas_order: dict | None

    def to_dict(self) -> dict:
        return {
            "scanned_at": self.scanned_at,
            "krx_market_open": self.krx_market_open,
            "us_market_open": self.us_market_open,
            "us_market_session": self.us_market_session,
            "us_orderable_in_profile": self.us_orderable_in_profile,
            "primary_market": self.primary_market,
            "primary_target": self.primary_target,
            "primary_selection_reason": self.primary_selection_reason,
            "domestic_ranked": [asdict(item) for item in self.domestic_ranked],
            "overseas_ranked": [asdict(item) for item in self.overseas_ranked],
            "domestic_excluded": [asdict(item) for item in self.domestic_excluded],
            "overseas_excluded": [asdict(item) for item in self.overseas_excluded],
            "domestic_positions": [asdict(item) for item in self.domestic_positions],
            "overseas_positions": [asdict(item) for item in self.overseas_positions],
            "watch_targets": [
                {key: value for key, value in asdict(item).items() if key != "signal_snapshot"}
                for item in self.watch_targets
            ],
            "estimated_api_calls_per_cycle": self.estimated_api_calls_per_cycle,
            "domestic_order": self.domestic_order,
            "overseas_order": self.overseas_order,
        }


class VirtualTradeManager:
    """
    Keep a virtual portfolio for orders rejected by the paper environment while the
    underlying market itself is open.
    """

    def __init__(self, repository: SqliteRepository) -> None:
        self.repository = repository

    def get_position(self, market: str, symbol: str) -> VirtualPosition | None:
        row = self.repository.get_virtual_position(market.strip().lower(), symbol.strip().upper())
        if row is None:
            return None
        return self._to_position(row)

    def list_positions(self, market: str | None = None) -> list[VirtualPosition]:
        positions = [self._to_position(row) for row in self.repository.list_virtual_positions()]
        if market is None:
            return positions
        market_key = market.strip().lower()
        return [position for position in positions if position.market == market_key]

    def record_buy(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        fill_price: float,
        currency: str,
        session: str,
        reason: str,
        created_at: str,
    ) -> VirtualPosition | None:
        if qty <= 0 or fill_price <= 0:
            return None

        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        existing = self.get_position(market_key, symbol_key)
        next_qty = qty if existing is None else existing.qty + qty
        avg_price = fill_price
        if existing is not None and next_qty > 0:
            avg_price = (
                existing.avg_price * existing.qty + fill_price * qty
            ) / next_qty

        self.repository.upsert_virtual_position(
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            qty=next_qty,
            avg_price=avg_price,
            currency=currency,
            opened_at=created_at,
            updated_at=created_at,
        )
        self.repository.save_virtual_order(
            created_at=created_at,
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            side="buy",
            qty=qty,
            fill_price=fill_price,
            currency=currency,
            session=session,
            reason=reason,
        )
        return self.get_position(market_key, symbol_key)

    def record_sell(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        fill_price: float,
        currency: str,
        session: str,
        reason: str,
        created_at: str,
        seed_avg_price: float | None = None,
        seed_qty: int | None = None,
    ) -> tuple[float, float]:
        if qty <= 0 or fill_price <= 0:
            return 0.0, 0.0

        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        position = self.get_position(market_key, symbol_key)
        if position is None and seed_avg_price and seed_avg_price > 0 and seed_qty and seed_qty > 0:
            position = VirtualPosition(
                market=market_key,
                symbol=symbol_key,
                exchange_code=exchange_code,
                qty=seed_qty,
                avg_price=seed_avg_price,
                currency=currency,
            )
        if position is None or position.qty <= 0:
            return 0.0, 0.0

        sell_qty = min(qty, position.qty)
        realized_pnl = (fill_price - position.avg_price) * sell_qty
        realized_pnl_pct = (
            (fill_price - position.avg_price) / position.avg_price
            if position.avg_price > 0
            else 0.0
        )
        remaining_qty = position.qty - sell_qty

        if remaining_qty > 0:
            self.repository.upsert_virtual_position(
                market=market_key,
                symbol=symbol_key,
                exchange_code=exchange_code or position.exchange_code,
                qty=remaining_qty,
                avg_price=position.avg_price,
                currency=position.currency,
                opened_at=created_at,
                updated_at=created_at,
            )
        else:
            self.repository.delete_virtual_position(market_key, symbol_key)

        self.repository.save_virtual_order(
            created_at=created_at,
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code or position.exchange_code,
            side="sell",
            qty=sell_qty,
            fill_price=fill_price,
            currency=position.currency,
            session=session,
            reason=reason,
            realized_pnl=realized_pnl,
            realized_pnl_pct=realized_pnl_pct,
        )
        return realized_pnl, realized_pnl_pct

    def performance_summary(self) -> dict:
        return self.repository.get_virtual_performance_summary()

    @staticmethod
    def _to_position(row: dict) -> VirtualPosition:
        return VirtualPosition(
            market=str(row["market"]),
            symbol=str(row["symbol"]),
            exchange_code=row.get("exchange_code"),
            qty=int(row["qty"]),
            avg_price=float(row["avg_price"]),
            currency=str(row["currency"]),
        )


@dataclass(slots=True)
class UnifiedPosition:
    market: str
    symbol: str
    exchange_code: str | None
    real_qty: int
    virtual_buy_qty: int
    virtual_sell_qty: int
    currency: str

    @property
    def total_qty(self) -> int:
        return self.real_qty + self.virtual_buy_qty - self.virtual_sell_qty


class UnifiedPositionTracker:
    """
    Track real holdings, virtual buys, and pending virtual sells in one place.
    """

    def __init__(
        self,
        repository: SqliteRepository,
        virtual_trades: VirtualTradeManager,
    ) -> None:
        self.repository = repository
        self.virtual_trades = virtual_trades

    def get_unified(
        self,
        market: str,
        symbol: str,
        real_qty: int,
        currency: str,
        exchange_code: str | None = None,
    ) -> UnifiedPosition:
        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        virtual_buy = self.virtual_trades.get_position(market_key, symbol_key)
        virtual_sell = self.repository.get_virtual_sell_pending(market_key, symbol_key)
        return UnifiedPosition(
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            real_qty=real_qty,
            virtual_buy_qty=0 if virtual_buy is None else virtual_buy.qty,
            virtual_sell_qty=0 if virtual_sell is None else int(virtual_sell["qty"]),
            currency=currency,
        )

    def apply_sell(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        sell_qty: int,
        price: float,
        currency: str,
        session: str,
        reason: str,
        real_qty: int,
        can_execute_real: bool,
        created_at: str,
        reference_avg_price: float | None = None,
    ) -> dict:
        virtual_buy = self.virtual_trades.get_position(market, symbol)
        virtual_buy_qty = 0 if virtual_buy is None else virtual_buy.qty
        virtual_buy_avg = 0.0 if virtual_buy is None else virtual_buy.avg_price

        remaining = sell_qty
        realized_pnl = 0.0
        from_virtual_buy = min(remaining, virtual_buy_qty)
        if from_virtual_buy > 0:
            sell_pnl, _ = self.virtual_trades.record_sell(
                market=market,
                symbol=symbol,
                exchange_code=exchange_code,
                qty=from_virtual_buy,
                fill_price=price,
                currency=currency,
                session=session,
                reason=reason,
                created_at=created_at,
            )
            realized_pnl += sell_pnl
            remaining -= from_virtual_buy

        if remaining <= 0:
            return {
                "realized_pnl": realized_pnl,
                "fully_virtual": True,
                "qty_from_real": 0,
                "qty_from_virtual_buy": from_virtual_buy,
                "qty_pending_real": 0,
                "virtual_buy_avg_price": virtual_buy_avg,
            }

        if can_execute_real:
            return {
                "realized_pnl": realized_pnl,
                "fully_virtual": False,
                "qty_from_real": remaining,
                "qty_from_virtual_buy": from_virtual_buy,
                "qty_pending_real": 0,
                "virtual_buy_avg_price": virtual_buy_avg,
            }

        existing = self.repository.get_virtual_sell_pending(market, symbol)
        existing_qty = 0 if existing is None else int(existing["qty"])
        existing_avg = 0.0 if existing is None else float(existing["avg_sell_price"])
        next_qty = existing_qty + remaining
        next_avg = price
        if next_qty > 0:
            next_avg = ((existing_avg * existing_qty) + (price * remaining)) / next_qty
        self.repository.upsert_virtual_sell_pending(
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            qty=next_qty,
            avg_sell_price=next_avg,
            currency=currency,
            updated_at=created_at,
        )
        pending_realized_pnl = 0.0
        pending_realized_pnl_pct = 0.0
        if reference_avg_price is not None and reference_avg_price > 0:
            pending_realized_pnl = (price - reference_avg_price) * remaining
            pending_realized_pnl_pct = (price - reference_avg_price) / reference_avg_price
        self.repository.save_virtual_order(
            created_at=created_at,
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            side="sell",
            qty=remaining,
            fill_price=price,
            currency=currency,
            session=session,
            reason=reason,
            realized_pnl=pending_realized_pnl,
            realized_pnl_pct=pending_realized_pnl_pct,
        )
        return {
            "realized_pnl": realized_pnl + pending_realized_pnl,
            "fully_virtual": True,
            "qty_from_real": remaining,
            "qty_from_virtual_buy": from_virtual_buy,
            "qty_pending_real": remaining,
            "virtual_buy_avg_price": virtual_buy_avg,
        }

    def get_pending_settlement(self, market: str, symbol: str) -> tuple[int, float] | None:
        row = self.repository.get_virtual_sell_pending(market, symbol)
        if row is None:
            return None
        qty = int(row["qty"])
        if qty <= 0:
            return None
        return qty, float(row["avg_sell_price"])

    def settle(
        self,
        *,
        market: str,
        symbol: str,
        real_qty_after_settlement: int,
    ) -> None:
        del real_qty_after_settlement
        self.repository.delete_virtual_sell_pending(market, symbol)


class LiquidityLabService:
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
        self.virtual_trades = VirtualTradeManager(repository)
        self.position_tracker = UnifiedPositionTracker(repository, self.virtual_trades)
        self._domestic_excluded: list[ExcludedCandidate] = []
        self._overseas_excluded: list[ExcludedCandidate] = []
        self._last_held_symbols: set[str] = set()
        self._signal_cache: dict[str, MovingAverageSnapshot | None] = {}
        self._signal_cache_updated_at: dict[str, datetime] = {}
        self._cycle_count: int = 0
        self._session_id: str = uuid.uuid4().hex[:12]
        self._wait_cycles: dict[str, int] = {}
        self._exit_cooldown: dict[str, datetime] = {}
        self._vol_history: dict[str, deque] = {}
        self._vol_history_maxlen: int = 12
        self._dynamic_domestic_codes: list[str] | None = None
        self._dynamic_domestic_names: dict[str, str] = {}
        self._domestic_scan_cycle_count: int = 0
        self._dynamic_overseas_pool: list[dict[str, str]] | None = None
        self._awaiting_relist: bool = False
        self._manual_overseas_pool: list[dict[str, str]] | None = None
        self._overseas_scan_cycle_count: int = 0
        self._overseas_balance_cache: dict = {}
        self._domestic_balance_cache: dict = {}
        self._overseas_relist_schedule: list[tuple[int, int]] = self._parse_relist_schedule(
            getattr(self.config.liquidity_lab, "overseas_relist_schedule_kst", "")
        )
        self._last_relist_kst: tuple[int, int] | None = None
        self._tv_available: bool = False
        self._last_tv_scan_used_fallback: bool = False
        self._consecutive_losses: int = 0
        self._session_realised_krw: float = 0.0
        self._daily_loss_date: date | None = None
        self._halted_at: datetime | None = None
        self._daily_halted_at: datetime | None = None
        self._tv_diagnostic_ran: bool = False
        self._last_holiday_notice_key: tuple[bool, bool, str] | None = None
        self._session_owned_symbols: set[str] = set()
        self._strategy_managers: dict[str, PriorityStrategyManager] = {}
        self._persisted_symbol_state: dict[tuple[str, str], dict] = {}
        self._domestic_fluctuation_rank_disabled: bool = False
        self._pending_trade_notifications: list[str] = []
        self._pending_trade_notification_started_at: datetime | None = None
        self._trade_notification_window_sec: int = 60
        self._trade_notification_max_batch_size: int = 8
        self._session_start_logged: bool = False
        self._no_orderable_retry: dict[str, datetime] = {}
        self._exit_price_shock_guard: dict[str, dict[str, float | str]] = {}

    def _get_position_tracker(self) -> UnifiedPositionTracker | None:
        tracker = getattr(self, "position_tracker", None)
        if tracker is not None:
            return tracker
        repository = getattr(self, "repository", None)
        virtual_trades = getattr(self, "virtual_trades", None)
        if repository is None or virtual_trades is None:
            return None
        tracker = UnifiedPositionTracker(repository, virtual_trades)
        self.position_tracker = tracker
        return tracker

    async def _get_overseas_available_usd(
        self,
        *,
        symbol: str,
        exchange_code: str,
        price: float,
    ) -> float:
        if price <= 0:
            return 0.0
        possible = await self.client.get_overseas_possible_order(
            symbol=symbol,
            exchange_code=exchange_code,
            price=f"{price:.4f}",
        )
        raw = possible.get("raw", {}) or {}
        # Prefer fields that KIS exposes as immediately orderable foreign cash.
        # Some simulation responses also include larger pre-exchange or max
        # theoretical amounts (for example frcr_ord_psbl_amt1), so cap the
        # usable budget by the actual max orderable quantity when available.
        direct_amounts = [
            self._parse_float(possible.get("cash_available")),
            self._parse_float(raw.get("ord_psbl_frcr_amt")),
            self._parse_float(raw.get("ord_psbl_frcr_amt_wcrc")),
            self._parse_float(raw.get("ovrs_ord_psbl_amt")),
            self._parse_float(raw.get("echm_af_ord_psbl_amt")),
            self._parse_float(raw.get("frcr_dncl_amt_2")),
        ]
        result = max(direct_amounts)
        if result <= 0:
            result = max(
                self._parse_float(raw.get("frcr_ord_psbl_amt1")),
                self._parse_float(possible.get("overseas_max_order_amount")),
            )
        qty_candidates = [
            self._parse_float(possible.get("max_order_quantity")),
            self._parse_float(raw.get("max_ord_psbl_qty")),
            self._parse_float(raw.get("ord_psbl_qty")),
            self._parse_float(raw.get("echm_af_ord_psbl_qty")),
        ]
        positive_qty = [qty for qty in qty_candidates if qty > 0]
        if positive_qty:
            quantity_cap_amount = min(positive_qty) * price
            if quantity_cap_amount > 0:
                result = min(result, quantity_cap_amount) if result > 0 else quantity_cap_amount
        self._last_overseas_available_usd = result
        self._last_overseas_available_usd_at = datetime.now(timezone.utc)
        return result

    async def _get_domestic_available_krw(self) -> float:
        cycle = getattr(self, "_cycle_count", 0)
        cache = getattr(self, "_domestic_balance_cache", {})
        if cache.get("cycle") == cycle and cache.get("data"):
            balance = cache["data"]
        else:
            try:
                balance = await self.client.get_balance()
                self._domestic_balance_cache = {
                    "cycle": cycle,
                    "data": balance,
                }
            except KisApiError as exc:
                _logger.warning("domestic_balance_fetch_failed error=%s", exc)
                return 0.0
        try:
            summary = balance.get("summary", {}) or {}
            result = max(
                self._parse_float(summary.get("ord_psbl_cash")),
                self._parse_float(summary.get("dnca_tot_amt")),
            )
            if result <= 0:
                _logger.warning(
                    "domestic_krw_balance_zero balance_keys=%s",
                    list(summary.keys()),
                )
            return result
        except Exception as exc:  # noqa: BLE001
            _logger.warning("domestic_balance_parse_failed error=%s", exc)
            return 0.0

    def _slot_based_qty(
        self,
        *,
        available_amount: float,
        price: float,
        max_budget: float | None = None,
    ) -> int:
        config = self.config.liquidity_lab
        if available_amount <= 0 or price <= 0:
            return 0
        slot_max_pct = max(float(config.slot_max_pct), 0.0)
        slot_entry_pct = max(float(config.slot_entry_pct), 0.0)
        if slot_max_pct <= 0 or slot_entry_pct <= 0:
            return 0
        budget = available_amount * slot_entry_pct
        if max_budget is not None:
            budget = min(budget, max(0.0, float(max_budget)))
        return max(int(math.floor(budget / price)), 0)

    def _open_virtual_overseas_notional(self) -> float:
        manager = getattr(self, "virtual_trades", None)
        if manager is None:
            return 0.0
        return sum(
            max(0, position.qty) * max(0.0, position.avg_price)
            for position in manager.list_positions("overseas")
        )

    def _remaining_virtual_overseas_budget(self, available_usd: float) -> float:
        if available_usd <= 0:
            return 0.0
        max_exposure_pct = max(
            0.0,
            float(getattr(self.config.liquidity_lab, "max_virtual_exposure_pct", 1.0)),
        )
        max_exposure = available_usd * max_exposure_pct
        return max(0.0, max_exposure - self._open_virtual_overseas_notional())

    def _should_block_overseas_standalone_vwap(
        self,
        *,
        market: str,
        strategy_flag: str,
    ) -> bool:
        return (
            market == "overseas"
            and strategy_flag == "VWAP"
            and bool(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_block_standalone_vwap",
                    False,
                )
            )
        )

    @staticmethod
    def _extract_broker_order_no(response: object) -> str:
        if not isinstance(response, dict):
            return ""
        nested_response = response.get("response")
        if isinstance(nested_response, dict):
            nested_value = LiquidityLabService._extract_broker_order_no(nested_response)
            if nested_value:
                return nested_value
        output = response.get("output")
        if isinstance(output, dict):
            for key in ("ODNO", "odno", "ORD_NO", "ord_no"):
                value = output.get(key)
                if value:
                    return str(value)
        for key in ("ODNO", "odno", "ORD_NO", "ord_no"):
            value = response.get(key)
            if value:
                return str(value)
        return ""

    def _record_broker_order_event(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        side: str,
        order_kind: str,
        requested_qty: int,
        requested_price: float | None,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        status: str = "",
        reason: str = "",
        is_virtual: bool = False,
        payload: dict | None = None,
    ) -> None:
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        broker_order_no = self._extract_broker_order_no(payload)
        repository.save_broker_order_event(
            created_at=datetime.now(timezone.utc).isoformat(),
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            side=side.upper(),
            order_kind=order_kind,
            requested_qty=requested_qty,
            requested_price=requested_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status=status,
            reason=reason,
            broker_order_no=broker_order_no or None,
            is_virtual=1 if is_virtual else 0,
            payload=payload,
        )

    def _queue_trade_notification(self, line: str) -> None:
        if not line:
            return
        queue = getattr(self, "_pending_trade_notifications", None)
        if queue is None:
            queue = []
            self._pending_trade_notifications = queue
        if not queue:
            self._pending_trade_notification_started_at = datetime.now(timezone.utc)
        queue.append(line)

    def _trade_notification_window_seconds(self) -> int:
        value = getattr(self, "_trade_notification_window_sec", 60)
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 60

    def _trade_notification_force_immediate(self) -> bool:
        return self._trade_notification_window_seconds() <= 0

    def _overseas_buy_order_price(self, candidate: OverseasScanResult) -> float:
        return float(candidate.ask or candidate.last_price)

    def _overseas_sell_order_price(
        self,
        candidate: OverseasScanResult,
        *,
        exit_reason: str,
    ) -> float:
        protective_reasons = {
            "stop_loss",
            "atr_hard_stop",
            "momentum_loss_cut",
            "trend_filter_lost",
            "time_exit_loss",
        }
        if exit_reason in protective_reasons:
            return float(candidate.bid or candidate.last_price)
        return float(candidate.bid or candidate.last_price or candidate.ask)

    def _overseas_order_history_exchange_param(self, exchange_code: str) -> str:
        env = str(getattr(self.config.credentials, "env", "vps") or "vps")
        if env != "prod":
            return ""
        return exchange_code

    @staticmethod
    def _parse_overseas_order_history_timestamp(row: dict) -> datetime | None:
        ord_dt = str(row.get("dmst_ord_dt") or row.get("ord_dt") or "").strip()
        ord_tmd = str(row.get("thco_ord_tmd") or row.get("ord_tmd") or "").strip()
        if not ord_dt or not ord_tmd:
            return None
        ord_tmd = ord_tmd.zfill(6)[:6]
        try:
            parsed = datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=KST).astimezone(timezone.utc)

    async def _list_open_overseas_orders(
        self,
        *,
        symbol: str,
        exchange_code: str,
    ) -> list[dict]:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        start_date = (now_kst - timedelta(days=1)).strftime("%Y%m%d")
        end_date = now_kst.strftime("%Y%m%d")
        env = str(getattr(self.config.credentials, "env", "vps") or "vps")
        side_filter = "00"
        fill_filter = "00" if env != "prod" else "02"
        try:
            history = await self.client.get_overseas_order_history(
                symbol="" if env != "prod" else symbol.upper(),
                start_date=start_date,
                end_date=end_date,
                side_filter=side_filter,
                fill_filter=fill_filter,
                exchange_code=self._overseas_order_history_exchange_param(exchange_code),
                sort_sqn="DS",
            )
        except Exception:
            return []
        results: list[dict] = []
        for row in history.get("orders", []):
            row_symbol = str(row.get("pdno") or row.get("ovrs_pdno") or "").strip().upper()
            if row_symbol != symbol.upper():
                continue
            open_qty = parse_kis_number(row.get("nccs_qty"))
            if open_qty <= 0:
                continue
            result = dict(row)
            result["open_qty"] = open_qty
            result["order_no"] = str(row.get("odno") or "").strip()
            result["order_price"] = self._parse_float(row.get("ft_ord_unpr3"))
            result["created_at"] = self._parse_overseas_order_history_timestamp(row)
            results.append(result)
        results.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return results

    async def _find_open_overseas_order(
        self,
        *,
        symbol: str,
        side: str,
        exchange_code: str,
    ) -> dict | None:
        side_code = "01" if side.upper() == "SELL" else "02"
        for row in await self._list_open_overseas_orders(symbol=symbol, exchange_code=exchange_code):
            if str(row.get("sll_buy_dvsn_cd") or "").strip() == side_code:
                return row
        return None

    async def _find_conflicting_overseas_order(
        self,
        *,
        symbol: str,
        side: str,
        exchange_code: str,
    ) -> dict | None:
        conflicting_side = "02" if side.upper() == "SELL" else "01"
        for row in await self._list_open_overseas_orders(symbol=symbol, exchange_code=exchange_code):
            if str(row.get("sll_buy_dvsn_cd") or "").strip() == conflicting_side:
                return row
        return None

    async def _cancel_open_overseas_order(
        self,
        *,
        symbol: str,
        exchange_code: str,
        pending_order: dict,
    ) -> dict:
        order_no = str(pending_order.get("order_no") or "").strip()
        if not order_no:
            raise KisApiError("pending_overseas_order_missing_order_no")
        qty = int(pending_order.get("open_qty") or 0)
        if qty <= 0:
            raise KisApiError("pending_overseas_order_missing_open_qty")
        return await self.client.revise_or_cancel_overseas_order(
            symbol=symbol,
            exchange_code=exchange_code,
            original_order_no=order_no,
            rvse_cncl_dvsn_cd="02",
            qty=qty,
            price="0",
        )

    @staticmethod
    def _parse_domestic_order_history_timestamp(row: dict) -> datetime | None:
        ord_dt = str(row.get("ord_dt") or "").strip()
        ord_tmd = str(row.get("ord_tmd") or "").strip()
        if not ord_dt or not ord_tmd:
            return None
        ord_tmd = ord_tmd.zfill(6)[:6]
        try:
            parsed = datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=KST).astimezone(timezone.utc)

    def _parse_open_domestic_order_rows(
        self,
        rows: list[dict],
        *,
        symbol: str,
    ) -> list[dict]:
        parsed: list[dict] = []
        target_symbol = symbol.strip().upper()
        for row in rows:
            row_symbol = str(row.get("pdno") or "").strip().upper()
            if target_symbol and row_symbol != target_symbol:
                continue
            open_qty = parse_kis_number(row.get("rmn_qty"))
            if open_qty <= 0:
                order_qty = parse_kis_number(row.get("ord_qty"))
                filled_qty = parse_kis_number(row.get("tot_ccld_qty"))
                canceled_qty = parse_kis_number(row.get("cncl_cfrm_qty"))
                rejected_qty = parse_kis_number(row.get("rjct_qty"))
                open_qty = max(0, order_qty - filled_qty - canceled_qty - rejected_qty)
            if open_qty <= 0:
                continue
            if str(row.get("cncl_yn", "") or "").strip().upper() == "Y":
                continue
            item = dict(row)
            item["open_qty"] = open_qty
            item["symbol"] = row_symbol
            item["order_no"] = str(row.get("odno") or "").strip()
            item["order_price"] = self._parse_float(row.get("ord_unpr"))
            item["created_at"] = self._parse_domestic_order_history_timestamp(row)
            parsed.append(item)
        parsed.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return parsed

    async def _list_open_domestic_orders(self, *, symbol: str) -> list[dict]:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        trade_date = now_kst.strftime("%Y%m%d")
        try:
            history = await self.client.get_domestic_order_history(
                symbol=symbol.strip().upper(),
                start_date=trade_date,
                end_date=trade_date,
                side_filter="00",
                fill_filter="02",
                query_order="00",
                query_type="00",
                exchange_code="KRX",
            )
        except Exception:
            return []
        return self._parse_open_domestic_order_rows(history.get("orders", []), symbol=symbol)

    async def _find_open_domestic_order(self, *, symbol: str, side: str) -> dict | None:
        side_code = "01" if side.upper() == "SELL" else "02"
        for row in await self._list_open_domestic_orders(symbol=symbol):
            side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
            row_side = str(row.get("sll_buy_dvsn_cd") or "").strip()
            if row_side == side_code:
                return row
            if side.upper() == "SELL" and side_name == "매도":
                return row
            if side.upper() == "BUY" and side_name == "매수":
                return row
        return None

    async def _cancel_open_domestic_order(
        self,
        *,
        symbol: str,
        pending_order: dict,
    ) -> dict:
        order_no = str(pending_order.get("order_no") or pending_order.get("odno") or "").strip()
        orgno = str(
            pending_order.get("ord_gno_brno")
            or pending_order.get("krx_fwdg_ord_orgno")
            or pending_order.get("KRX_FWDG_ORD_ORGNO")
            or ""
        ).strip()
        qty = int(pending_order.get("open_qty") or parse_kis_number(pending_order.get("rmn_qty")))
        if not order_no:
            raise KisApiError("pending_domestic_order_missing_order_no")
        if not orgno:
            raise KisApiError("pending_domestic_order_missing_orgno")
        if qty <= 0:
            raise KisApiError("pending_domestic_order_missing_open_qty")
        order_division = str(pending_order.get("ord_dvsn_cd") or "00").strip() or "00"
        exchange_code = str(
            pending_order.get("excg_id_dvsn_cd")
            or pending_order.get("EXCG_ID_DVSN_CD")
            or "KRX"
        ).strip() or "KRX"
        return await self.client.revise_or_cancel_domestic_order(
            krx_order_orgno=orgno,
            original_order_no=order_no,
            order_division=order_division,
            rvse_cncl_dvsn_cd="02",
            qty=0,
            price=0,
            qty_all_order_yn="Y",
            exchange_code=exchange_code,
        )

    @staticmethod
    def _pending_order_age_seconds(pending_order: dict | None, *, now: datetime | None = None) -> float:
        if pending_order is None:
            return 0.0
        created_at = pending_order.get("created_at")
        if not isinstance(created_at, datetime):
            return 0.0
        ref = now or datetime.now(timezone.utc)
        return max((ref - created_at).total_seconds(), 0.0)

    @staticmethod
    def _protective_exit_reasons() -> set[str]:
        return {
            "stop_loss",
            "atr_hard_stop",
            "momentum_loss_cut",
            "trend_filter_lost",
            "time_exit_loss",
        }

    def _format_domestic_symbol_label(self, stock_code: str) -> str:
        code = str(stock_code or "").strip().upper()
        if not code:
            return "-"
        name = str(getattr(self, "_dynamic_domestic_names", {}).get(code, "") or "").strip()
        return f"{code}({name})" if name else code

    def _format_trade_symbol_label(self, market: str, code: str) -> str:
        if str(market).strip().lower() == "domestic":
            return self._format_domestic_symbol_label(code)
        return str(code or "").strip().upper() or "-"

    def _get_domestic_stock_name(self, stock_code: str, *sources: object) -> str:
        code = str(stock_code or "").strip().upper()
        if not code:
            return ""
        name_map = getattr(self, "_dynamic_domestic_names", {})
        if code in name_map and str(name_map.get(code) or "").strip():
            return str(name_map[code]).strip()
        for source in sources:
            if not isinstance(source, dict):
                continue
            for field_name in ("hts_kor_isnm", "name", "prdt_name", "stck_shrn_iscd_name"):
                value = str(source.get(field_name, "") or "").strip()
                if value:
                    return value
        return ""

    async def _flush_trade_notifications(self, *, force: bool = False) -> None:
        queue = getattr(self, "_pending_trade_notifications", None)
        if not queue:
            return
        now = datetime.now(timezone.utc)
        started_at = getattr(self, "_pending_trade_notification_started_at", None) or now
        batch_size = len(queue)
        age_sec = max((now - started_at).total_seconds(), 0.0)
        if (
            not force
            and age_sec < float(self._trade_notification_window_seconds())
            and batch_size < int(getattr(self, "_trade_notification_max_batch_size", 8) or 8)
        ):
            return
        lines = [
            "[KIS][거래알림]",
            f"시각={format_kst_korean(now)}",
            f"건수={batch_size}",
            *queue,
        ]
        try:
            await self.notifier.send("\n".join(lines))
        finally:
            self._pending_trade_notifications = []
            self._pending_trade_notification_started_at = None

    @staticmethod
    def _display_trade_action(action_raw: str, action_text: str, *, skip_count: int = 0) -> str:
        if action_raw == "WAIT" and skip_count > 0:
            return "주문거부"
        mapping = {
            "BUY": "매수접수",
            "SELL": "매도접수",
            "VIRTUAL_BUY": "가상매수",
            "VIRTUAL_SELL": "가상매도",
        }
        return mapping.get(action_raw, action_text)

    async def flush_pending_trade_notifications(self, *, force: bool = True) -> None:
        await self._flush_trade_notifications(force=force)

    @staticmethod
    def _parse_relist_schedule(schedule_text: str) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for token in str(schedule_text or "").split(","):
            text = token.strip()
            if not text or ":" not in text:
                continue
            hour_text, minute_text = text.split(":", 1)
            try:
                hour = int(hour_text)
                minute = int(minute_text)
            except ValueError:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                result.append((hour, minute))
        return result

    @staticmethod
    def _coerce_overseas_candidate(item: object) -> OverseasCandidateConfig:
        if isinstance(item, OverseasCandidateConfig):
            return item
        if hasattr(item, "symbol") and hasattr(item, "exchange_code"):
            return OverseasCandidateConfig(
                symbol=str(getattr(item, "symbol", "")),
                exchange_code=str(getattr(item, "exchange_code", "NASD")),
            )
        if isinstance(item, dict):
            return OverseasCandidateConfig(
                symbol=str(item.get("symbol", "")),
                exchange_code=str(item.get("exchange_code", "NASD")),
            )
        return OverseasCandidateConfig(symbol="", exchange_code="NASD")

    def _active_overseas_pool(
        self,
        held_positions: list | None = None,
        held_symbols: set[str] | None = None,
        held_symbol_map: dict[str, str] | None = None,
    ) -> list[OverseasCandidateConfig]:
        raw_pool: list = (
            getattr(self, "_manual_overseas_pool", None)
            or getattr(self, "_dynamic_overseas_pool", None)
            or []
        )
        candidates = [
            candidate
            for candidate in (self._coerce_overseas_candidate(item) for item in raw_pool)
            if candidate.symbol.strip()
        ]
        existing_symbols = {candidate.symbol.upper() for candidate in candidates}
        if held_positions:
            for position in held_positions:
                symbol = ""
                exchange_code = "NASD"
                if hasattr(position, "symbol"):
                    symbol = str(getattr(position, "symbol", "")).strip().upper()
                    exchange_code = str(getattr(position, "exchange_code", "NASD") or "NASD").strip().upper()
                else:
                    symbol = str(position).strip().upper()
                if symbol and symbol not in existing_symbols:
                    candidates.append(
                        self._coerce_overseas_candidate(
                            {
                                "symbol": symbol,
                                "exchange_code": exchange_code,
                            }
                        )
                    )
                    existing_symbols.add(symbol)
        if held_symbols:
            for symbol in held_symbols:
                symbol_upper = str(symbol).strip().upper()
                if symbol_upper and symbol_upper not in existing_symbols:
                    candidates.append(
                        self._coerce_overseas_candidate(
                            {
                                "symbol": symbol_upper,
                                "exchange_code": (
                                    (held_symbol_map or {}).get(symbol_upper, "NASD")
                                ),
                            }
                        )
                    )
                    existing_symbols.add(symbol_upper)
        return [candidate for candidate in candidates if candidate.symbol.strip()]

    def _known_overseas_exchange_codes(
        self,
        held_positions: list[OverseasHeldPosition] | None = None,
    ) -> set[str]:
        exchange_codes = {
            candidate.exchange_code.upper()
            for candidate in self._active_overseas_pool(held_positions=held_positions or [])
            if candidate.exchange_code.strip()
        }
        if held_positions:
            for position in held_positions:
                exchange_code = str(getattr(position, "exchange_code", "") or "").strip().upper()
                if exchange_code:
                    exchange_codes.add(exchange_code)
        if not exchange_codes:
            exchange_codes = set(_DEFAULT_OVERSEAS_EXCHANGE_CODES)
        return exchange_codes

    @staticmethod
    def _scan_result_from_overseas_position(position: OverseasHeldPosition) -> OverseasScanResult:
        current_price = float(position.current_price or position.avg_price or 0.0)
        return OverseasScanResult(
            symbol=position.symbol.upper(),
            exchange_code=(position.exchange_code or "NASD").upper(),
            last_price=current_price,
            bid=current_price,
            ask=current_price,
            spread_pct=0.0,
            change_rate_pct=0.0,
            volume=0,
            orderable_qty=max(position.orderable_qty, position.quantity),
            fx_rate_krw=0.0,
            activity_score=0.0,
        )

    async def _ensure_tv_diagnostics(self) -> None:
        if getattr(self, "_tv_available", False):
            return
        if getattr(self, "_tv_diagnostic_ran", False):
            return
        self._tv_diagnostic_ran = True
        ll_cfg = self.config.liquidity_lab
        if not getattr(ll_cfg, "tv_scan_enabled", True):
            _logger.info("[TV] tv_scan_enabled=False")
            self._tv_available = False
            return
        client = getattr(self.client, "_client", None)
        if client is None:
            _logger.warning("[TV] shared_http_client_missing")
            self._tv_available = False
            return
        self._tv_available = await check_connectivity(client)
        notifier = getattr(self, "notifier", None)
        if notifier is not None and getattr(notifier, "enabled", True):
            try:
                await notifier.send(
                    "✅ TradingView Scanner 접근 가능 — 해외 동적 풀 활성화"
                    if self._tv_available
                    else "⚠️ TradingView Scanner 접근 불가 — 기존 relist 방식 유지"
                )
            except Exception:  # noqa: BLE001
                _logger.debug("tv_diagnostic_notify_failed", exc_info=True)

    async def _scan_tv_dynamic_pool(
        self,
        *,
        min_rel_volume: float | None = None,
    ) -> list[dict[str, str]]:
        client = getattr(self.client, "_client", None)
        if client is None:
            return []
        ll_cfg = self.config.liquidity_lab
        return await scan_top_volume_surge(
            client=client,
            top_n=max(1, getattr(ll_cfg, "tv_top_n", 30)),
            min_rel_volume=(
                float(min_rel_volume)
                if min_rel_volume is not None
                else float(getattr(ll_cfg, "tv_min_rel_volume", 2.0))
            ),
            min_price_usd=float(getattr(ll_cfg, "tv_min_price_usd", 5.0)),
            min_volume=int(getattr(ll_cfg, "tv_min_volume", 500_000)),
            min_market_cap=float(getattr(ll_cfg, "tv_min_market_cap", 3e8)),
            max_market_cap=float(getattr(ll_cfg, "tv_max_market_cap", 2e12)),
            max_change_pct=float(getattr(ll_cfg, "tv_max_change_pct", 20.0)),
        )

    async def _scan_tv_dynamic_pool_with_fallback(self) -> list[dict[str, str]]:
        ll_cfg = self.config.liquidity_lab
        target_n = max(1, getattr(ll_cfg, "tv_top_n", 30))
        min_fallback_n = max(1, int(target_n * 0.3))
        tv_rows = await self._scan_tv_dynamic_pool()
        if tv_rows and len(tv_rows) >= min_fallback_n:
            self._last_tv_scan_used_fallback = False
            return tv_rows

        fallback_rel_vol = max(
            1.0,
            float(getattr(ll_cfg, "tv_min_rel_volume", 2.0)) * 0.6,
        )
        _logger.info(
            "[TV] 결과 부족 (%s개 < %s) -> min_rel_volume=%.1f 완화 재시도",
            len(tv_rows),
            min_fallback_n,
            fallback_rel_vol,
        )
        fallback_rows = await self._scan_tv_dynamic_pool(
            min_rel_volume=fallback_rel_vol,
        )
        self._last_tv_scan_used_fallback = bool(fallback_rows)
        return fallback_rows or tv_rows or []

    async def _refresh_overseas_dynamic_pool(self) -> None:
        manual_pool = getattr(self, "_manual_overseas_pool", None)
        if manual_pool:
            if getattr(self, "_tv_available", False):
                tv_rows = await self._scan_tv_dynamic_pool_with_fallback()
                if tv_rows:
                    self._manual_overseas_pool = None
                    self._dynamic_overseas_pool = list(tv_rows)
                    self._awaiting_relist = False
                    self._save_event(
                        event_type="tv_scan",
                        market="overseas",
                        detail={
                            "pool_size": len(tv_rows),
                            "threshold": float(
                                getattr(self.config.liquidity_lab, "tv_min_rel_volume", 2.0)
                            ),
                            "fallback_used": bool(
                                getattr(self, "_last_tv_scan_used_fallback", False)
                            ),
                        },
                    )
                    preview = ", ".join(row["symbol"] for row in tv_rows[:5])
                    _logger.info(
                        "[TV] 수동 풀 자동 해제 -> TV 동적 풀 복귀 (%s개) [%s]",
                        len(tv_rows),
                        preview,
                    )
                    notifier = getattr(self, "notifier", None)
                    if notifier is not None and getattr(notifier, "enabled", True):
                        try:
                            await notifier.send(
                                "✅ TV 동적 풀 자동 복귀\n"
                                "수동 relist 해제 -> TV 스캔 결과 적용\n"
                                f"대표: {preview} (총 {len(tv_rows)}개)"
                            )
                        except Exception:  # noqa: BLE001
                            _logger.debug("tv_auto_restore_notify_failed", exc_info=True)
                    return

            self._dynamic_overseas_pool = list(manual_pool)
            self._awaiting_relist = False
            self._save_event(
                event_type="pool_refresh",
                market="overseas",
                detail={"pool_size": len(manual_pool), "source": "manual"},
            )
            _logger.info("overseas_manual_pool_override count=%s", len(manual_pool))
            return

        if getattr(self, "_tv_available", False):
            tv_rows = await self._scan_tv_dynamic_pool_with_fallback()
            if tv_rows:
                self._dynamic_overseas_pool = list(tv_rows)
                self._awaiting_relist = False
                self._save_event(
                    event_type="tv_scan",
                    market="overseas",
                    detail={
                        "pool_size": len(tv_rows),
                        "threshold": float(
                            getattr(self.config.liquidity_lab, "tv_min_rel_volume", 2.0)
                        ),
                        "fallback_used": bool(
                            getattr(self, "_last_tv_scan_used_fallback", False)
                        ),
                    },
                )
                preview = ", ".join(row["symbol"] for row in tv_rows[:5])
                _logger.info(
                    "[TV] 해외 동적 풀 갱신: %s개 -> [%s]",
                    len(tv_rows),
                    preview,
                )
                return
            _logger.warning("[TV] scan_result_empty; will retry next rescan cycle")

        self._dynamic_overseas_pool = []
        if not getattr(self, "_awaiting_relist", False):
            self._awaiting_relist = True
            self._save_event(
                event_type="tv_scan",
                market="overseas",
                detail={"pool_size": 0, "threshold": float(getattr(self.config.liquidity_lab, "tv_min_rel_volume", 2.0)), "fallback_used": bool(getattr(self, "_last_tv_scan_used_fallback", False))},
            )
            _logger.warning("[풀] 해외 동적 풀 없음 — relist 요청")
            notifier = getattr(self, "notifier", None)
            if notifier is not None and getattr(notifier, "enabled", True):
                try:
                    await notifier.send(
                        "⚠️ 해외 종목 풀이 비어 있습니다.\n"
                        "TV Scanner 접근 불가 + 수동 목록 없음.\n\n"
                        "아래 명령으로 직접 지정해주세요:\n"
                        "/lab_relist NVDA TSLA AMD PLTR COIN"
                    )
                except Exception:  # noqa: BLE001
                    _logger.debug("relist_notify_failed", exc_info=True)

    async def _apply_holiday_overrides(self, now_utc: datetime) -> tuple[bool, bool]:
        nyse_date = us_holiday_date_for_kis_session(now_utc)
        krx_date = now_utc.astimezone(KST).date()
        nyse_holiday = bool(
            getattr(self.config, "skip_holiday_overseas", True) and is_nyse_holiday(nyse_date)
        )
        krx_holiday = bool(
            getattr(self.config, "skip_holiday_domestic", True) and is_krx_holiday(krx_date)
        )
        notice_key = (
            nyse_holiday,
            krx_holiday,
            now_utc.astimezone(KST).strftime("%Y-%m-%d"),
        )
        if (nyse_holiday or krx_holiday) and notice_key != getattr(self, "_last_holiday_notice_key", None):
            self._last_holiday_notice_key = notice_key
            notifier = getattr(self, "notifier", None)
            if notifier is not None and getattr(notifier, "enabled", True):
                lines = [
                    "📅 휴장일 감지 — 스캔 중단",
                    market_status_summary(nyse_date=nyse_date, krx_date=krx_date),
                    "",
                    f"해외 스캔 {'중단' if nyse_holiday else '유지'} | 국내 스캔 {'중단' if krx_holiday else '유지'}",
                    "다음 영업일에 자동으로 재개됩니다.",
                ]
                try:
                    await notifier.send("\n".join(lines))
                except Exception:  # noqa: BLE001
                    _logger.debug("holiday_notice_send_failed", exc_info=True)
            _logger.info(
                "holiday_skip_detected krx_holiday=%s nyse_holiday=%s",
                krx_holiday,
                nyse_holiday,
            )
        elif not nyse_holiday and not krx_holiday:
            self._last_holiday_notice_key = None
        return krx_holiday, nyse_holiday

    def _surge_bonus_from_ratio(self, surge_ratio: float) -> float:
        strong = float(getattr(self.config.liquidity_lab, "vol_surge_threshold_strong", 5.0))
        mild = float(getattr(self.config.liquidity_lab, "vol_surge_threshold_mild", 3.0))
        if surge_ratio >= 10.0:
            return 15.0
        if surge_ratio >= strong:
            return 8.0
        if surge_ratio >= mild:
            return 3.0
        return 0.0

    def _record_volume_and_get_surge_ratio(
        self,
        symbol: str,
        acml_vol: int,
        now_utc: datetime | None = None,
    ) -> float:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        history_map = getattr(self, "_vol_history", None)
        if history_map is None:
            history_map = {}
            self._vol_history = history_map
        history_maxlen = int(getattr(self, "_vol_history_maxlen", 12))
        if symbol not in history_map:
            history_map[symbol] = deque(maxlen=history_maxlen)
        history = history_map[symbol]
        history.append((now_utc, acml_vol))
        if len(history) < 3:
            return 1.0

        deltas: list[float] = []
        items = list(history)
        for index in range(1, len(items)):
            prev_vol = items[index - 1][1]
            curr_vol = items[index][1]
            deltas.append(float(max(0, curr_vol - prev_vol)))
        if not deltas:
            return 1.0
        current_delta = deltas[-1]
        past_deltas = deltas[:-1]
        avg_past = sum(past_deltas) / len(past_deltas) if past_deltas else 0.0
        if avg_past <= 0:
            return 1.0
        return current_delta / avg_past

    async def _refresh_domestic_dynamic_pool(self) -> None:
        ll_cfg = self.config.liquidity_lab
        try:
            vol_rows = await self.client.get_domestic_volume_rank(
                market_code="J",
                top_n=ll_cfg.domestic_dynamic_top_n,
                min_price_krw=ll_cfg.domestic_dynamic_min_price_krw,
                min_volume=ll_cfg.domestic_dynamic_min_volume,
            )
            if getattr(self, "_domestic_fluctuation_rank_disabled", False):
                flu_rows = []
            else:
                try:
                    flu_rows = await self.client.get_domestic_fluctuation_rank(
                        market_code="J",
                        top_n=max(1, ll_cfg.domestic_dynamic_top_n // 2),
                        min_price_krw=ll_cfg.domestic_dynamic_min_price_krw,
                        min_volume=ll_cfg.domestic_dynamic_min_volume,
                    )
                except Exception as exc:  # noqa: BLE001
                    if "404" in str(exc):
                        self._domestic_fluctuation_rank_disabled = True
                        _logger.warning(
                            "domestic_fluctuation_rank_disabled error=%s",
                            exc,
                        )
                        flu_rows = []
                    else:
                        raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("domestic_dynamic_scan_failed error=%s", exc)
            return

        seen: set[str] = set()
        codes: list[str] = []
        name_map: dict[str, str] = {}
        for row in [*vol_rows, *flu_rows]:
            code = str(row.get("stock_code", "")).strip()
            name = str(row.get("hts_kor_isnm", "") or row.get("name", "")).strip()
            if code and name:
                name_map[code] = name
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        if not codes:
            self._dynamic_domestic_names = {}
            self._save_event(
                event_type="pool_refresh",
                market="domestic",
                detail={"pool_size": 0, "top_names": []},
            )
            return
        self._dynamic_domestic_codes = codes
        self._dynamic_domestic_names = name_map
        top_names = [
            str(row.get("hts_kor_isnm", "") or row.get("name", "")).strip()
            for row in vol_rows[:5]
            if row.get("hts_kor_isnm") or row.get("name")
        ]
        self._save_event(
            event_type="pool_refresh",
            market="domestic",
            detail={"pool_size": len(codes), "top_names": top_names[:5]},
        )
        _logger.info("domestic_dynamic_pool_refreshed count=%s", len(codes))
        notifier = getattr(self, "notifier", None)
        if notifier is not None and getattr(notifier, "enabled", True):
            try:
                await notifier.send(
                    f"🔄 [국내 동적 풀 갱신] {len(codes)}종목\n거래량 상위: {', '.join(top_names)}"
                )
            except Exception:  # noqa: BLE001
                _logger.debug("domestic_dynamic_pool_notify_failed", exc_info=True)

    async def _maybe_send_overseas_relist_alert(
        self,
        now_utc: datetime,
        *,
        nyse_holiday: bool = False,
    ) -> None:
        if nyse_holiday:
            return
        now_kst = now_utc.astimezone(KST)
        current_hm = (now_kst.hour, now_kst.minute)
        schedule = getattr(self, "_overseas_relist_schedule", None)
        if schedule is None:
            schedule = self._parse_relist_schedule(
                getattr(self.config.liquidity_lab, "overseas_relist_schedule_kst", "")
            )
            self._overseas_relist_schedule = schedule
        if current_hm not in schedule:
            return
        if current_hm == getattr(self, "_last_relist_kst", None):
            return
        self._last_relist_kst = current_hm
        notifier = getattr(self, "notifier", None)
        if notifier is None or not getattr(notifier, "enabled", True):
            return
        pool = (
            getattr(self, "_manual_overseas_pool", None)
            or self._dynamic_overseas_pool
            or []
        )
        await notifier.send(
            "\n".join(
                [
                    f"⏰ [자동 relist 알림] {now_kst.strftime('%H:%M')} KST",
                    f"현재 감시 풀: {len(pool)}종목",
                    "교체: /lab_relist PLTR NVDA AMD ...",
                    "유지: 무시",
                ]
            )
        )

    async def run(self) -> LiquidityLabReport:
        try:
            return await self._run_cycle()
        except (
            KisApiError,
            httpx.ConnectTimeout,
            httpx.NetworkError,
            httpx.ReadTimeout,
        ) as exc:
            _logger.warning(
                "[CYCLE] 일시적 네트워크/API 오류 - 사이클 스킵 (error=%s)",
                exc,
            )
            self._save_event(
                event_type="session_crash",
                detail={
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
            now = datetime.now(timezone.utc)
            return LiquidityLabReport(
                scanned_at=format_kst(now) or "",
                krx_market_open=False,
                us_market_open=False,
                us_market_session="",
                us_orderable_in_profile=False,
                primary_market="none",
                primary_target=None,
                primary_selection_reason="network_error",
                domestic_ranked=[],
                overseas_ranked=[],
                domestic_excluded=[],
                overseas_excluded=[],
                domestic_positions=[],
                overseas_positions=[],
                watch_targets=[],
                estimated_api_calls_per_cycle=0,
                domestic_order=None,
                overseas_order=None,
            )

    async def _run_cycle(self) -> LiquidityLabReport:
        now = datetime.now(timezone.utc)
        self._cycle_count = getattr(self, "_cycle_count", 0) + 1
        if not getattr(self, "_session_start_logged", False):
            self._session_start_logged = True
            self._save_event(
                event_type="session_start",
                detail={
                    "profile": getattr(
                        self.config.credentials,
                        "profile_name",
                        getattr(self.config.credentials, "env", ""),
                    )
                },
            )
        await self._ensure_tv_diagnostics()
        krx_holiday, nyse_holiday = await self._apply_holiday_overrides(now)
        await self._maybe_send_overseas_relist_alert(now, nyse_holiday=nyse_holiday)
        krx_open = is_krx_regular_session(now) and not krx_holiday
        us_open = is_us_regular_session(now) and not nyse_holiday
        us_session = get_us_trading_session(now)
        us_orderable_in_profile = is_us_orderable_session_for_env(
            now,
            self.config.credentials.env,
        ) and not nyse_holiday

        if not krx_open and not us_open:
            return LiquidityLabReport(
                scanned_at=format_kst(now) or "",
                krx_market_open=False,
                us_market_open=False,
                us_market_session=us_session,
                us_orderable_in_profile=False,
                primary_market="none",
                primary_target=None,
                primary_selection_reason="market_holiday" if (krx_holiday or nyse_holiday) else "no_supported_market_open",
                domestic_ranked=[],
                overseas_ranked=[],
                domestic_excluded=[],
                overseas_excluded=[],
                domestic_positions=[],
                overseas_positions=[],
                watch_targets=[],
                estimated_api_calls_per_cycle=0,
                domestic_order={"skipped": True, "reason": "market_closed"},
                overseas_order={"skipped": True, "reason": "market_closed"},
            )

        refreshed_position_markets: set[str] = set()
        domestic_ranked = await self.scan_domestic() if krx_open else []
        domestic_positions = (
            await self._load_domestic_positions(domestic_ranked)
            if krx_open
            else []
        )
        domestic_balance_cache = getattr(self, "_domestic_balance_cache", {})
        if (
            krx_open
            and domestic_balance_cache.get("cycle") == getattr(self, "_cycle_count", 0)
            and domestic_balance_cache.get("data")
        ):
            refreshed_position_markets.add("domestic")
        if us_open:
            overseas_ranked, held_symbols_cache = await self.scan_overseas()
            overseas_positions = await self._load_overseas_positions(
                overseas_ranked,
                held_symbols_cache=held_symbols_cache,
            )
            if us_orderable_in_profile:
                await self._reconcile_pending_virtual_sells(
                    overseas_positions=overseas_positions,
                )
            virtual_overseas_positions = self._load_virtual_overseas_positions(overseas_ranked)
            monitored_overseas_positions = [
                *overseas_positions,
                *virtual_overseas_positions,
            ]
            overseas_balance_cache = getattr(self, "_overseas_balance_cache", {})
            if (
                overseas_balance_cache.get("cycle") == getattr(self, "_cycle_count", 0)
                and overseas_balance_cache.get("data")
            ):
                refreshed_position_markets.add("overseas")
        else:
            overseas_ranked = []
            overseas_positions = []
            monitored_overseas_positions = []
        self._clear_stale_lab_position_states(
            domestic_positions=domestic_positions,
            overseas_positions=monitored_overseas_positions,
            refreshed_markets=refreshed_position_markets,
        )
        self._restore_strategy_contexts(
            domestic_positions=domestic_positions,
            overseas_positions=monitored_overseas_positions,
        )
        watch_targets = await self._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_positions=domestic_positions,
            overseas_positions=monitored_overseas_positions,
            krx_open=krx_open,
            us_open=us_open,
        )
        domestic_watch_targets = [
            watch_target for watch_target in watch_targets if watch_target.market == "domestic"
        ]
        overseas_watch_targets = [
            watch_target for watch_target in watch_targets if watch_target.market == "overseas"
        ]
        domestic_watch_map = {watch_target.code: watch_target for watch_target in domestic_watch_targets}
        overseas_watch_map = {watch_target.code: watch_target for watch_target in overseas_watch_targets}
        overseas_exit_targets = (
            await self._select_overseas_exit_targets(
                overseas_ranked,
                monitored_overseas_positions,
                max_exits=5,
            )
            if us_open
            else []
        )
        overseas_exit_target = overseas_exit_targets[0] if overseas_exit_targets else None
        domestic_exit_target = (
            self._select_domestic_exit_target(
                domestic_ranked,
                domestic_watch_targets,
                domestic_positions,
            )
            if krx_open
            else None
        )
        if self._is_trading_halted():
            domestic_buy_targets = []
            domestic_buy_target = None
            overseas_buy_targets = []
            overseas_buy_target = None
            _logger.info(
                "[CB] 서킷브레이커 활성 — 이번 사이클 매수 스킵"
                " (consecutive=%d, session_pnl=%.0f)",
                getattr(self, "_consecutive_losses", 0),
                getattr(self, "_session_realised_krw", 0.0),
            )
        else:
            config_ll = self.config.liquidity_lab
            domestic_buy_targets = self._select_domestic_buy_targets(
                domestic_ranked,
                domestic_watch_targets,
                max_concurrent=getattr(config_ll, "max_concurrent_domestic_orders", 2),
            )
            domestic_buy_target = domestic_buy_targets[0] if domestic_buy_targets else None
            _max_os = getattr(config_ll, "max_concurrent_overseas_orders", 20)
            remaining_overseas_slots = self._remaining_overseas_entry_slots(
                monitored_overseas_positions,
                max_positions=_max_os,
            )
            if remaining_overseas_slots <= 0:
                overseas_buy_targets = []
                overseas_buy_target = None
            else:
                overseas_buy_targets = self._select_overseas_buy_targets(
                    overseas_ranked,
                    overseas_watch_targets,
                    max_concurrent=remaining_overseas_slots,
                    held_positions=monitored_overseas_positions,
                )
                overseas_buy_target = overseas_buy_targets[0] if overseas_buy_targets else None
        domestic_order: dict = {"skipped": True, "reason": "no_action"}
        overseas_order: dict = {"skipped": True, "reason": "no_action"}
        domestic_orders: list[dict] = []
        overseas_orders: list[dict] = []

        if domestic_exit_target is not None:
            exit_candidate, held, exit_reason, exit_signal = domestic_exit_target
            domestic_order = await self._place_domestic_sell_order(
                exit_candidate,
                held,
                exit_reason,
                exit_signal,
            )
            domestic_orders = [domestic_order]
        elif domestic_buy_targets and krx_open:
            for buy_candidate in domestic_buy_targets:
                domestic_orders.append(
                    await self._place_domestic_test_order(
                        buy_candidate,
                        watch_target=domestic_watch_map.get(buy_candidate.stock_code),
                    )
                )
            domestic_order = domestic_orders[0]
        else:
            domestic_orders = [domestic_order]

        if overseas_exit_targets:
            for exit_candidate, exit_position, exit_reason, exit_signal in overseas_exit_targets:
                _order = await self._place_overseas_sell_order(
                    exit_candidate,
                    exit_position,
                    exit_reason,
                    signal_snapshot=exit_signal,
                )
                overseas_orders.append(_order)
            overseas_order = overseas_orders[0]
        elif overseas_buy_targets and us_orderable_in_profile:
            for buy_candidate in overseas_buy_targets:
                overseas_orders.append(
                    await self._manage_overseas_position(
                        candidate=buy_candidate,
                        held_positions=overseas_positions,
                        watch_target=overseas_watch_map.get(buy_candidate.symbol),
                    )
                )
            overseas_order = overseas_orders[0]
        elif overseas_buy_targets and us_open and not us_orderable_in_profile:
            for buy_candidate in overseas_buy_targets:
                overseas_orders.append(
                    await self._record_virtual_overseas_buy(
                        buy_candidate,
                        watch_target=overseas_watch_map.get(buy_candidate.symbol),
                    )
                )
            overseas_order = overseas_orders[0]
        else:
            overseas_order = {
                "skipped": True,
                "reason": (
                    "us_open_but_mock_session_not_supported"
                    if us_open and not us_orderable_in_profile
                    else "no_overseas_candidate"
                ),
            }
            overseas_orders = [overseas_order]
        if overseas_orders:
            overseas_order = dict(overseas_order)
            overseas_order["batched_orders"] = overseas_orders
        if domestic_orders:
            domestic_order = dict(domestic_order)
            domestic_order["batched_orders"] = domestic_orders

        domestic_active = any(not order.get("skipped", False) for order in domestic_orders)
        overseas_active = any(not order.get("skipped", False) for order in overseas_orders)
        if domestic_active and overseas_active:
            domestic_code = (
                domestic_order.get("candidate", {}).get("stock_code")
                or (domestic_buy_target.stock_code if domestic_buy_target is not None else None)
            )
            overseas_code = (
                overseas_order.get("candidate", {}).get("symbol")
                or (overseas_buy_target.symbol if overseas_buy_target is not None else None)
            )
            primary_market = "both"
            primary_target = "+".join(
                [code for code in [domestic_code, overseas_code] if code]
            ) or None
            primary_reason = "dual_market_active"
        elif domestic_active:
            primary_market = "domestic"
            if domestic_exit_target is not None:
                exit_candidate, _, exit_reason, _ = domestic_exit_target
                primary_target = exit_candidate.stock_code
                primary_reason = f"existing_position_{exit_reason}"
            elif domestic_buy_target is not None:
                primary_target = domestic_buy_target.stock_code
                primary_reason = "watchlist_buy_signal"
            else:
                primary_target = domestic_watch_targets[0].code if domestic_watch_targets else None
                primary_reason = "domestic_active"
        elif overseas_active:
            primary_market = "overseas"
            if overseas_exit_target is not None:
                exit_candidate, _, exit_reason, _ = overseas_exit_target
                primary_target = exit_candidate.symbol
                primary_reason = f"existing_position_{exit_reason}"
            elif overseas_buy_target is not None:
                primary_target = overseas_buy_target.symbol
                primary_reason = "watchlist_buy_signal"
            else:
                primary_target = overseas_watch_targets[0].code if overseas_watch_targets else None
                primary_reason = "overseas_active"
        elif krx_open and us_open and watch_targets:
            primary_market = "both"
            primary_target = None
            primary_reason = "both_waiting"
        elif krx_open and domestic_watch_targets:
            primary_market = "domestic"
            primary_target = domestic_watch_targets[0].code
            primary_reason = "watchlist_wait"
        elif us_open and overseas_watch_targets:
            primary_market = "overseas"
            primary_target = overseas_watch_targets[0].code
            primary_reason = "watchlist_wait"
        elif us_open and not us_orderable_in_profile:
            primary_market = "none"
            primary_target = None
            primary_reason = "us_open_but_mock_session_not_supported"
        elif krx_open:
            primary_market = "domestic"
            primary_target = None
            primary_reason = "krx_open_but_no_candidate"
        elif us_open:
            primary_market = "overseas" if us_orderable_in_profile else "none"
            primary_target = None
            primary_reason = (
                "us_open_but_no_candidate"
                if us_orderable_in_profile
                else "us_open_but_mock_session_not_supported"
            )
        else:
            primary_market = "none"
            primary_target = None
            primary_reason = "no_supported_market_open"

        report = LiquidityLabReport(
            scanned_at=format_kst(now) or "",
            krx_market_open=krx_open,
            us_market_open=us_open,
            us_market_session=us_session,
            us_orderable_in_profile=us_orderable_in_profile,
            primary_market=primary_market,
            primary_target=primary_target,
            primary_selection_reason=primary_reason,
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_excluded=self._domestic_excluded,
            overseas_excluded=self._overseas_excluded,
            domestic_positions=domestic_positions,
            overseas_positions=overseas_positions,
            watch_targets=watch_targets,
            estimated_api_calls_per_cycle=self._estimate_api_calls_per_cycle(
                krx_open=krx_open,
                us_open=us_open,
                include_domestic_order=bool(domestic_exit_target or domestic_buy_target),
                include_overseas_order=bool(overseas_exit_target or overseas_buy_target),
            ),
            domestic_order=domestic_order,
            overseas_order=overseas_order,
        )
        await self._send_summary(report)
        return report

    async def scan_domestic(self) -> list[DomesticScanResult]:
        config = self.config.liquidity_lab
        if getattr(config, "domestic_dynamic_scan", False):
            self._domestic_scan_cycle_count = getattr(self, "_domestic_scan_cycle_count", 0) + 1
            if (
                getattr(self, "_dynamic_domestic_codes", None) is None
                or self._domestic_scan_cycle_count >= max(1, config.domestic_dynamic_rescan_cycles)
            ):
                self._domestic_scan_cycle_count = 0
                await self._refresh_domestic_dynamic_pool()
        active_codes = (
            list(getattr(self, "_dynamic_domestic_codes", None))
            if getattr(self, "_dynamic_domestic_codes", None)
            else list(config.domestic_candidates)
        )
        quote_results: list[DomesticScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for stock_code in active_codes:
            try:
                candidate = await self._scan_single_domestic_quote(stock_code)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            reasons = self._domestic_quote_speculative_reasons(candidate)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=stock_code,
                        reasons=reasons,
                        snapshot=asdict(candidate),
                    )
                )
            else:
                quote_results.append(candidate)
            await asyncio.sleep(0.05)
        self._domestic_excluded = excluded
        if not quote_results:
            return []

        ll_cfg = self.config.liquidity_lab
        threshold = getattr(ll_cfg, "max_wait_cycles_before_penalty", 15)
        decay = getattr(ll_cfg, "wait_penalty_decay", 0.07)
        wait_cycles = getattr(self, "_wait_cycles", None)
        if wait_cycles is None:
            wait_cycles = {}
            self._wait_cycles = wait_cycles

        def _domestic_effective_score(result: DomesticScanResult) -> float:
            key = f"domestic:{result.stock_code.upper()}"
            wait_count = wait_cycles.get(key, 0)
            excess = max(0, wait_count - threshold)
            penalty = max(0.2, 1.0 - excess * decay)
            return result.activity_score * penalty

        quote_results.sort(key=_domestic_effective_score, reverse=True)
        refine_n = min(len(quote_results), max(config.unified_scan_top_n, 3))
        refined: list[DomesticScanResult] = []
        for candidate in quote_results[:refine_n]:
            try:
                full_candidate = await self._scan_single_domestic(candidate.stock_code)
            except Exception:
                refined.append(candidate)
                await asyncio.sleep(0.05)
                continue
            reasons = self._domestic_speculative_reasons(full_candidate)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=full_candidate.stock_code,
                        reasons=reasons,
                        snapshot=asdict(full_candidate),
                    )
                )
            else:
                refined.append(full_candidate)
            await asyncio.sleep(0.05)

        self._domestic_excluded = excluded
        remaining = quote_results[refine_n:]
        return sorted(refined + remaining, key=_domestic_effective_score, reverse=True)

    async def _scan_single_domestic_quote(self, stock_code: str) -> DomesticScanResult:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        stock_name = self._get_domestic_stock_name(stock_code, current, orderbook)
        intraday_turnover = int(current.get("turnover_krw", 0) or 0)
        acml_vol = int(current.get("volume", 0) or 0)
        spread_pct = float(orderbook.get("spread_pct", 0.0) or 0.0)
        liquidity_score = math.log10(max(intraday_turnover, 1)) * 8.0
        spread_penalty = spread_pct * 3000.0
        turnover_surge_bonus = 0.0
        if intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 3:
            turnover_surge_bonus = 4.0
        elif intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 1.5:
            turnover_surge_bonus = 2.0
        surge_ratio = self._record_volume_and_get_surge_ratio(stock_code, acml_vol)
        surge_bonus = self._surge_bonus_from_ratio(surge_ratio)

        activity_score = liquidity_score + turnover_surge_bonus + surge_bonus - spread_penalty
        return DomesticScanResult(
            stock_code=stock_code,
            current_price=int(current["current_price"]),
            best_ask=int(orderbook["best_ask"]),
            best_bid=int(orderbook["best_bid"]),
            spread_pct=spread_pct,
            minute_change_pct=0.0,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=acml_vol,
            activity_score=round(activity_score, 4),
            stock_name=stock_name,
        )

    async def scan_overseas(self) -> tuple[list[OverseasScanResult], set[str]]:
        """
        Scan the overseas universe in a single pass per cycle.

        Step 1: fetch quotes for all candidates and compute activity score.
        Step 2: select top-N activity symbols plus any held symbol for signal loading.
        Step 3: load chart-based signals only for that reduced set and cache them.
        """
        config = self.config.liquidity_lab
        quote_results: list[OverseasScanResult] = []
        excluded: list[ExcludedCandidate] = []
        self._overseas_scan_cycle_count = getattr(self, "_overseas_scan_cycle_count", 0) + 1
        if (
            getattr(self, "_dynamic_overseas_pool", None) is None
            or self._overseas_scan_cycle_count
            >= max(1, int(getattr(config, "overseas_rescan_cycles", 20)))
        ):
            self._overseas_scan_cycle_count = 0
            self._tv_diagnostic_ran = False
            await self._refresh_overseas_dynamic_pool()

        held_symbol_map = await self._get_held_symbol_map()
        virtual_symbols = self._get_virtual_held_symbols()
        active_overseas_pool = self._active_overseas_pool(
            held_symbol_map=held_symbol_map,
            held_symbols=set(held_symbol_map.keys()) | virtual_symbols,
        )
        held_symbols = set(held_symbol_map.keys()) | virtual_symbols
        for candidate in active_overseas_pool:
            try:
                scan_result = await self._scan_single_overseas(candidate)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            reasons = self._overseas_speculative_reasons(scan_result)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="overseas",
                        code=candidate.symbol,
                        reasons=reasons,
                        snapshot=asdict(scan_result),
                    )
                )
            else:
                quote_results.append(scan_result)
            await asyncio.sleep(0.05)

        self._overseas_excluded = excluded
        if not quote_results:
            # Keep held symbols and existing cached signals alive when quote fetches
            # temporarily fail so exit/watch logic can continue using last-good
            # balance data and persisted signal context.
            if not held_symbols:
                self._signal_cache.clear()
                updated_map = getattr(self, "_signal_cache_updated_at", None)
                if updated_map is not None:
                    updated_map.clear()
            return [], held_symbols

        ll_cfg = self.config.liquidity_lab
        threshold = getattr(ll_cfg, "max_wait_cycles_before_penalty", 15)
        decay = getattr(ll_cfg, "wait_penalty_decay", 0.07)
        wait_cycles = getattr(self, "_wait_cycles", None)
        if wait_cycles is None:
            wait_cycles = {}
            self._wait_cycles = wait_cycles

        def _effective_score(result: OverseasScanResult) -> float:
            key = f"overseas:{result.symbol.upper()}"
            wait_count = wait_cycles.get(key, 0)
            excess = max(0, wait_count - threshold)
            penalty = max(0.2, 1.0 - excess * decay)
            return result.activity_score * penalty

        quote_results.sort(key=_effective_score, reverse=True)
        # held_symbols is already assigned above (before pool scan).
        top_n = max(1, config.overseas_scan_top_n)

        signal_symbols: set[str] = set()
        for result in quote_results:
            symbol = result.symbol.upper()
            if symbol in held_symbols:
                signal_symbols.add(symbol)

        remaining_slots = max(0, top_n - len(signal_symbols))
        for result in quote_results:
            if remaining_slots <= 0:
                break
            symbol = result.symbol.upper()
            if symbol in signal_symbols:
                continue
            signal_symbols.add(symbol)
            remaining_slots -= 1

        for result in quote_results:
            symbol = result.symbol.upper()
            if symbol not in signal_symbols:
                continue
            self._signal_cache[symbol] = await self._get_overseas_signal_for_candidate(result)
            await asyncio.sleep(0.05)

        for symbol in list(self._signal_cache.keys()):
            if symbol not in signal_symbols:
                del self._signal_cache[symbol]
                updated_map = getattr(self, "_signal_cache_updated_at", None)
                if updated_map is not None:
                    updated_map.pop(symbol, None)

        return quote_results, held_symbols

    async def _get_held_symbols(self) -> set[str]:
        """
        Return overseas symbols currently held.

        On API failure, fall back to the previous cycle cache so exit scans still include
        existing positions.
        """
        try:
            cycle = getattr(self, "_cycle_count", 0)
            cache = getattr(self, "_overseas_balance_cache", {})
            if cache.get("cycle") == cycle:
                cached = cache.get("data", {})
                held: set[str] = set(self._get_virtual_held_symbols())
                for balance in cached.values():
                    for row in balance.get("positions", []):
                        qty = parse_kis_number(row.get("ovrs_cblc_qty"))
                        if qty <= 0:
                            continue
                        symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                        if symbol:
                            held.add(symbol)
                self._last_held_symbols = held
                return held

            exchange_codes = self._known_overseas_exchange_codes()
            held: set[str] = set(self._get_virtual_held_symbols())
            raw_balances: dict[str, dict] = {}
            for exchange_code in sorted(exchange_codes):
                balance = await self.client.get_overseas_balance(
                    exchange_code=exchange_code,
                    currency_code="USD",
                )
                raw_balances[exchange_code] = balance
                for row in balance.get("positions", []):
                    qty = parse_kis_number(row.get("ovrs_cblc_qty"))
                    if qty <= 0:
                        continue
                    symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                    if symbol:
                        held.add(symbol)
            self._overseas_balance_cache = {
                "cycle": cycle,
                "data": raw_balances,
            }
            self._last_held_symbols = held
            return held
        except Exception:
            return self._last_held_symbols or self._get_virtual_held_symbols()

    async def _get_held_symbol_map(self) -> dict[str, str]:
        """
        Return current overseas holdings as symbol -> exchange_code mapping.

        Reuses the balance cache populated by `_get_held_symbols()` to avoid
        extra API calls. Virtual holdings default to NASD when no exchange
        context exists.
        """
        _ = await self._get_held_symbols()
        cache = getattr(self, "_overseas_balance_cache", {})
        result: dict[str, str] = {}
        for balance in cache.get("data", {}).values():
            for row in balance.get("positions", []):
                qty = parse_kis_number(row.get("ovrs_cblc_qty"))
                if qty <= 0:
                    continue
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                raw_exch = str(row.get("ovrs_excg_cd", "")).strip().upper()
                if symbol:
                    result[symbol] = raw_exch or "NASD"
        for sym in self._get_virtual_held_symbols():
            symbol = sym.upper()
            if symbol not in result:
                result[symbol] = "NASD"
        return result

    def _get_virtual_held_symbols(self) -> set[str]:
        manager = getattr(self, "virtual_trades", None)
        if manager is None:
            return set()
        return {
            position.symbol.upper()
            for position in manager.list_positions("overseas")
            if position.qty > 0
        }

    async def _scan_single_domestic(self, stock_code: str) -> DomesticScanResult:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        stock_name = self._get_domestic_stock_name(stock_code, current, orderbook)
        target_date = datetime.now(timezone.utc).astimezone(KST).strftime("%Y%m%d")
        bars = await self.client.get_time_daily_chart(
            stock_code=stock_code,
            target_date=target_date,
            market_code=self.config.trading.market_code,
        )
        limited_bars = bars[: min(8, len(bars))]
        closes = [parse_kis_number(row.get("stck_prpr")) for row in limited_bars]
        volumes = [parse_kis_number(row.get("cntg_vol")) for row in limited_bars]
        earliest = closes[-1] if closes else 0
        latest = closes[0] if closes else current["current_price"]
        minute_change_pct = 0.0 if earliest <= 0 else (latest - earliest) / earliest
        intraday_turnover = int(current.get("turnover_krw", 0) or 0)
        volume_sum = sum(volumes)
        spread_pct = float(orderbook.get("spread_pct", 0.0) or 0.0)
        liquidity_score = math.log10(max(intraday_turnover, 1)) * 8.0
        volume_score = math.log10(max(volume_sum, 1)) * 4.0
        momentum_score = minute_change_pct * 300.0
        spread_penalty = spread_pct * 3000.0
        turnover_surge_bonus = 0.0
        if intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 3:
            turnover_surge_bonus = 4.0
        elif intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 1.5:
            turnover_surge_bonus = 2.0
        activity_score = (
            liquidity_score
            + volume_score
            + momentum_score
            + turnover_surge_bonus
            - spread_penalty
        )
        return DomesticScanResult(
            stock_code=stock_code,
            current_price=int(current["current_price"]),
            best_ask=int(orderbook["best_ask"]),
            best_bid=int(orderbook["best_bid"]),
            spread_pct=spread_pct,
            minute_change_pct=minute_change_pct,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=volume_sum,
            activity_score=round(activity_score, 4),
            stock_name=stock_name,
        )

    async def _scan_single_overseas(
        self,
        candidate: OverseasCandidateConfig,
    ) -> OverseasScanResult:
        quote = await self.client.get_overseas_price(candidate.symbol, candidate.exchange_code)
        last_price = self._parse_float(quote.get("last_price"))
        bid = self._parse_float(quote.get("bid"))
        ask = self._parse_float(quote.get("ask"))
        volume = parse_kis_number(quote.get("volume"))
        change_rate = self._parse_float(quote.get("change_rate"))
        mid_price = (bid + ask) / 2 if bid > 0 and ask > 0 else float(last_price)
        spread_pct = 0.0
        if bid > 0 and ask > 0 and mid_price > 0:
            spread_pct = (ask - bid) / mid_price
        liquidity_score = math.log10(max(volume, 1)) * 6.0
        momentum_score = change_rate * 200.0
        spread_penalty = spread_pct * 2500.0
        volume_surge_bonus = 0.0
        if volume >= self.config.liquidity_lab.overseas_min_volume * 5:
            volume_surge_bonus = 3.0
        elif volume >= self.config.liquidity_lab.overseas_min_volume * 2:
            volume_surge_bonus = 1.5
        surge_ratio = self._record_volume_and_get_surge_ratio(candidate.symbol.upper(), int(volume))
        surge_bonus = self._surge_bonus_from_ratio(surge_ratio)
        tight_spread_bonus = 1.0 if spread_pct < 0.001 else 0.0
        activity_score = (
            liquidity_score
            + momentum_score
            + volume_surge_bonus
            + surge_bonus
            + tight_spread_bonus
            - spread_penalty
        )
        return OverseasScanResult(
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            change_rate_pct=change_rate,
            volume=volume,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=round(activity_score, 4),
        )

    async def _load_overseas_positions(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_symbols_cache: set[str] | None = None,
    ) -> list[OverseasHeldPosition]:
        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        exchange_codes = (
            {item.exchange_code.upper() for item in overseas_ranked if item.exchange_code.strip()}
            or self._known_overseas_exchange_codes()
        )
        positions_by_key: dict[tuple[str, str], OverseasHeldPosition] = {}
        cycle = getattr(self, "_cycle_count", 0)
        cache = getattr(self, "_overseas_balance_cache", {})
        if cache.get("cycle") == cycle and cache.get("data"):
            balance_map = cache["data"]
        else:
            balance_map: dict[str, dict] = {}
            for exchange_code in sorted(exchange_codes):
                try:
                    balance = await self.client.get_overseas_balance(
                        exchange_code=exchange_code,
                        currency_code="USD",
                    )
                    balance_map[exchange_code] = balance
                except Exception:
                    continue
            self._overseas_balance_cache = {
                "cycle": cycle,
                "data": balance_map,
            }

        for exchange_code, balance in balance_map.items():
            for row in balance.get("positions", []):
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                if not symbol:
                    continue
                row_exchange_code = str(row.get("ovrs_excg_cd", "")).strip().upper() or exchange_code
                quantity = parse_kis_number(row.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue
                orderable_qty = parse_kis_number(row.get("ord_psbl_qty"))
                avg_price = self._parse_float(row.get("pchs_avg_pric"))
                quote = quote_map.get(symbol)
                if avg_price <= 0:
                    continue
                current_price = (
                    quote.last_price
                    if quote is not None
                    else max(
                        self._parse_float(row.get("ovrs_now_pric")),
                        self._parse_float(row.get("ovrs_now_pric1")),
                        self._parse_float(row.get("now_pric2")),
                        self._parse_float(row.get("last_price")),
                    )
                )
                if current_price <= 0:
                    current_price = avg_price
                pnl_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
                positions_by_key[(symbol, row_exchange_code)] = OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=row_exchange_code,
                    quantity=quantity,
                    orderable_qty=orderable_qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    is_virtual=False,
                )

        return list(positions_by_key.values())

    def _load_virtual_overseas_positions(
        self,
        overseas_ranked: list[OverseasScanResult],
    ) -> list[OverseasHeldPosition]:
        manager = getattr(self, "virtual_trades", None)
        if manager is None:
            return []

        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        positions: list[OverseasHeldPosition] = []
        for position in manager.list_positions("overseas"):
            symbol = position.symbol.upper()
            if position.qty <= 0:
                continue
            quote = quote_map.get(symbol)
            exchange_code = str(position.exchange_code or "").strip().upper()
            current_price = 0.0
            if quote is not None:
                current_price = float(quote.last_price)
                if not exchange_code:
                    exchange_code = str(quote.exchange_code or "").strip().upper()
            else:
                persisted = self._get_persisted_symbol_state("overseas", symbol)
                if persisted is not None:
                    current_price = self._parse_float(persisted.get("last_price"))
                    if not exchange_code:
                        exchange_code = str(
                            persisted.get("exchange_code", "") or ""
                        ).strip().upper()
            if current_price <= 0:
                current_price = float(position.avg_price)
            if not exchange_code:
                exchange_code = "NASD"
            pnl_pct = (
                (current_price - position.avg_price) / position.avg_price
                if position.avg_price > 0
                else 0.0
            )
            positions.append(
                OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=exchange_code,
                    quantity=position.qty,
                    orderable_qty=position.qty,
                    avg_price=position.avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    is_virtual=True,
                )
            )
        return positions

    async def _load_domestic_positions(
        self,
        domestic_ranked: list[DomesticScanResult],
    ) -> list[DomesticHeldPosition]:
        quote_map = {item.stock_code: item for item in domestic_ranked}
        try:
            balance = await self.client.get_balance()
            self._domestic_balance_cache = {
                "cycle": getattr(self, "_cycle_count", 0),
                "data": balance,
            }
        except Exception:
            return []

        positions: list[DomesticHeldPosition] = []
        rows = balance.get("positions", []) or balance.get("output1", [])
        for row in rows:
            qty = int(parse_kis_number(row.get("hldg_qty")))
            if qty <= 0:
                continue
            stock_code = str(row.get("pdno", "")).strip()
            if not stock_code:
                continue
            avg_price = self._parse_float(row.get("pchs_avg_pric"))
            orderable_qty = int(parse_kis_number(row.get("ord_psbl_qty")) or qty)
            quote = quote_map.get(stock_code)
            if quote is not None:
                current_price = quote.current_price
            else:
                current_price = next(
                    (
                        price
                        for price in (
                            self._parse_float(row.get("prpr")),
                            self._parse_float(row.get("stck_prpr")),
                            self._parse_float(row.get("now_pric")),
                            self._parse_float(row.get("last_price")),
                        )
                        if price > 0
                    ),
                    avg_price,
                )
            pnl_pct = (
                (current_price - avg_price) / avg_price
                if avg_price > 0
                else 0.0
            )
            positions.append(
                DomesticHeldPosition(
                    stock_code=stock_code,
                    quantity=qty,
                    orderable_qty=orderable_qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                )
            )
        return positions

    async def _select_overseas_exit_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
        *,
        max_exits: int = 10,
    ) -> list[tuple[OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot | None]]:
        if not held_positions:
            return []

        config = self.config.liquidity_lab
        tracker = self._get_position_tracker()
        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        real_by_symbol: dict[str, OverseasHeldPosition] = {
            held.symbol.upper(): held
            for held in held_positions
            if not held.is_virtual
        }

        results: list[
            tuple[
                tuple[int, float],
                OverseasScanResult,
                OverseasHeldPosition,
                str,
                MovingAverageSnapshot | None,
            ]
        ] = []

        symbols_to_check = set(real_by_symbol.keys())
        virtual_manager = getattr(self, "virtual_trades", None)
        if virtual_manager is not None:
            for position in virtual_manager.list_positions("overseas"):
                symbols_to_check.add(position.symbol.upper())

        for symbol in symbols_to_check:
            quote = quote_map.get(symbol)
            real = real_by_symbol.get(symbol)
            if quote is None:
                fallback = real or next(
                    (position for position in held_positions if position.symbol.upper() == symbol),
                    None,
                )
                if fallback is None:
                    continue
                quote = self._scan_result_from_overseas_position(fallback)

            pending = None if tracker is None else tracker.get_pending_settlement("overseas", symbol)
            already_pending_qty = 0 if pending is None else pending[0]
            remaining_real_orderable = max(0, (real.orderable_qty if real else 0) - already_pending_qty)
            if (
                remaining_real_orderable <= 0
                and real is not None
                and self._is_no_orderable_retry_active("overseas", symbol)
            ):
                self._track_no_orderable_stall(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=real.quantity,
                )
                continue

            avg_price = 0.0
            pnl_pct = 0.0
            virtual_buy = None if virtual_manager is None else virtual_manager.get_position("overseas", symbol)
            if real is not None and real.avg_price > 0:
                avg_price = real.avg_price
                pnl_pct = real.pnl_pct
            else:
                if virtual_buy is not None and virtual_buy.avg_price > 0:
                    avg_price = virtual_buy.avg_price
                    pnl_pct = (quote.last_price - avg_price) / avg_price

            is_virtual_only = real is None and virtual_buy is not None
            effective_orderable = remaining_real_orderable
            if (
                effective_orderable <= 0
                and real is not None
                and real.quantity > already_pending_qty
            ):
                if self._cooldown_remaining_minutes("overseas", symbol) > 0:
                    self._track_no_orderable_stall(
                        market="overseas",
                        symbol=symbol,
                        holding_qty=real.quantity,
                    )
                    continue
                # KIS orderable_qty reflects unsettled state inconsistently, so
                # fall back to held quantity and let the actual sell API decide.
                effective_orderable = max(0, real.quantity - already_pending_qty)
                if effective_orderable > 0:
                    _logger.warning(
                        "[EXIT] orderable_qty=0이지만 holding_qty=%d → 실주문 시도",
                        real.quantity,
                    )
            if avg_price <= 0:
                continue
            if not is_virtual_only and effective_orderable <= 0:
                self._track_no_orderable_stall(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=0 if real is None else real.quantity,
                )
                self._defer_no_orderable_position(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=0 if real is None else real.quantity,
                    orderable_qty=effective_orderable,
                )
                continue
            effective_orderable = (
                (virtual_buy.qty if virtual_buy is not None else 0)
                if is_virtual_only
                else effective_orderable
            )
            self._clear_no_orderable_retry("overseas", symbol)
            self._reset_no_orderable_stall("overseas", symbol)

            exit_reason: str | None = None
            priority: tuple[int, float] | None = None
            signal_snapshot = getattr(self, "_signal_cache", {}).get(symbol)
            strategy_flag, entry_by, exit_by = self._get_strategy_labels(symbol, signal_snapshot)
            if self._overseas_exit_price_guard_reason(
                symbol=symbol,
                quote=quote,
                avg_price=avg_price,
                holding_qty=(
                    virtual_buy.qty
                    if is_virtual_only and virtual_buy
                    else real.quantity
                    if real
                    else 0
                ),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
            ):
                continue
            if pnl_pct <= -config.overseas_stop_loss_pct:
                exit_reason = "stop_loss"
                priority = (0, pnl_pct)
            elif pnl_pct >= config.overseas_take_profit_pct:
                exit_reason = "take_profit"
                priority = (1, -pnl_pct)

            if exit_reason is None or priority is None:
                continue

            held_for_return = OverseasHeldPosition(
                symbol=symbol,
                exchange_code=real.exchange_code if real else quote.exchange_code,
                quantity=real.quantity if real else 0,
                orderable_qty=effective_orderable,
                avg_price=avg_price,
                current_price=quote.last_price,
                pnl_pct=pnl_pct,
                is_virtual=(real is None),
            )
            results.append((priority, quote, held_for_return, exit_reason, None))

        already_exiting = {item[2].symbol.upper() for item in results}
        for symbol, held in real_by_symbol.items():
            if symbol in already_exiting:
                continue
            quote = quote_map.get(symbol)
            if quote is None:
                continue
            pending = None if tracker is None else tracker.get_pending_settlement("overseas", symbol)
            already_pending_qty = 0 if pending is None else pending[0]
            remaining_real_orderable = max(0, held.orderable_qty - already_pending_qty)
            if (
                remaining_real_orderable <= 0
                and self._is_no_orderable_retry_active("overseas", symbol)
            ):
                self._track_no_orderable_stall(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=held.quantity,
                )
                continue
            effective_orderable = remaining_real_orderable
            if effective_orderable <= 0 and held.quantity > already_pending_qty:
                if self._cooldown_remaining_minutes("overseas", symbol) > 0:
                    self._track_no_orderable_stall(
                        market="overseas",
                        symbol=symbol,
                        holding_qty=held.quantity,
                    )
                    continue
                effective_orderable = max(0, held.quantity - already_pending_qty)
                if effective_orderable > 0:
                    _logger.warning(
                        "[EXIT] orderable_qty=0이지만 holding_qty=%d → 실주문 시도",
                        held.quantity,
                    )
            if effective_orderable <= 0:
                self._track_no_orderable_stall(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=held.quantity,
                )
                self._defer_no_orderable_position(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=held.quantity,
                    orderable_qty=effective_orderable,
                )
                continue
            self._clear_no_orderable_retry("overseas", symbol)
            self._reset_no_orderable_stall("overseas", symbol)
            signal_snapshot = getattr(self, "_signal_cache", {}).get(held.symbol.upper())
            if signal_snapshot is None:
                continue
            strategy_flag, entry_by, exit_by = self._get_strategy_labels(symbol, signal_snapshot)
            if self._overseas_exit_price_guard_reason(
                symbol=symbol,
                quote=quote,
                avg_price=held.avg_price,
                holding_qty=held.quantity,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
            ):
                continue
            should_exit, exit_reason = self._should_exit_overseas_position(signal_snapshot, held)
            if not should_exit:
                continue
            held_copy = OverseasHeldPosition(
                symbol=held.symbol,
                exchange_code=held.exchange_code,
                quantity=held.quantity,
                orderable_qty=effective_orderable,
                avg_price=held.avg_price,
                current_price=held.current_price,
                pnl_pct=held.pnl_pct,
                is_virtual=False,
            )
            results.append(((2, held.pnl_pct), quote, held_copy, exit_reason, signal_snapshot))

        if not results:
            return []

        results.sort(key=lambda item: item[0])
        return [
            (quote, held, exit_reason, signal_snapshot)
            for _, quote, held, exit_reason, signal_snapshot in results[:max_exits]
        ]

    async def _select_overseas_exit_target(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> tuple[OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot | None] | None:
        selected = await self._select_overseas_exit_targets(
            overseas_ranked,
            held_positions,
            max_exits=1,
        )
        return selected[0] if selected else None

    def _domestic_speculative_reasons(self, candidate: DomesticScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        if candidate.current_price < config.domestic_min_price_krw:
            reasons.append("low_price_krw")
        if candidate.intraday_turnover_krw < config.domestic_min_intraday_turnover_krw:
            reasons.append("thin_intraday_turnover")
        if candidate.volume_sum < config.domestic_min_volume_sum:
            reasons.append("thin_recent_volume")
        if candidate.spread_pct > config.domestic_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    def _domestic_quote_speculative_reasons(self, candidate: DomesticScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        if candidate.current_price < config.domestic_min_price_krw:
            reasons.append("low_price_krw")
        if candidate.intraday_turnover_krw < config.domestic_min_intraday_turnover_krw:
            reasons.append("thin_intraday_turnover")
        if candidate.spread_pct > config.domestic_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    def _overseas_speculative_reasons(self, candidate: OverseasScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        structured_reason = self._overseas_structured_symbol_reason(candidate.symbol)
        if structured_reason:
            reasons.append(structured_reason)
        if candidate.last_price < config.overseas_min_price_usd:
            reasons.append("low_price_usd")
        if candidate.volume < config.overseas_min_volume:
            reasons.append("thin_volume")
        if candidate.spread_pct > config.overseas_max_spread_pct:
            reasons.append("wide_spread")
        approx_daily_turnover = candidate.last_price * candidate.volume
        min_daily_turnover = config.overseas_min_price_usd * config.overseas_min_volume
        if approx_daily_turnover < min_daily_turnover:
            reasons.append("thin_turnover")
        return reasons

    @staticmethod
    def _overseas_structured_symbol_reason(symbol: str) -> str:
        normalized = symbol.strip().upper().replace(".", "").replace("-", "")
        if len(normalized) >= 5 and normalized.endswith("U"):
            return "structured_unit_symbol"
        warrant_suffixes = ("WTS", "WS", "WT", "W", "RT", "R")
        if len(normalized) >= 5 and normalized.endswith(warrant_suffixes):
            return "structured_warrant_or_right_symbol"
        return ""

    def _overseas_exit_price_guard_reason(
        self,
        *,
        symbol: str,
        quote: OverseasScanResult,
        avg_price: float,
        holding_qty: int,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
    ) -> str | None:
        """
        Guard exits from one-off bad overseas quotes.

        A real crash should still exit on the next confirmed cycle. The first
        anomalous print is logged and skipped so stale/daytime quotes do not
        erase virtual positions with fabricated PnL.
        """
        last_price = float(quote.last_price or 0.0)
        if last_price <= 0:
            return "invalid_exit_price"

        if quote.bid > 0 and quote.ask > 0:
            mid_price = (quote.bid + quote.ask) / 2.0
            mid_mismatch_pct = abs(last_price - mid_price) / mid_price if mid_price > 0 else 0.0
            max_mid_mismatch = float(
                getattr(self.config.liquidity_lab, "overseas_exit_mid_mismatch_pct", 0.03)
            )
            if mid_mismatch_pct >= max_mid_mismatch:
                reason = f"price_mid_mismatch:{mid_mismatch_pct:.1%}"
                self._record_trade_skip(
                    market="overseas",
                    symbol=symbol,
                    exchange_code=quote.exchange_code,
                    reason=reason,
                    side="sell",
                    price=last_price,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    stock_name=symbol,
                    activity_score=quote.activity_score,
                    holding_qty=holding_qty,
                )
                return reason

        reference_price = float(avg_price or 0.0)
        repository = getattr(self, "repository", None)
        if repository is not None:
            state = repository.get_lab_symbol_state("overseas", symbol)
            if state is not None:
                previous_price = float(state.get("last_price") or 0.0)
                if previous_price > 0:
                    reference_price = previous_price

        if reference_price <= 0:
            return None

        shock_pct = (last_price - reference_price) / reference_price
        shock_threshold = float(
            getattr(self.config.liquidity_lab, "overseas_exit_price_shock_pct", 0.20)
        )
        if abs(shock_pct) <= shock_threshold:
            guard = getattr(self, "_exit_price_shock_guard", None)
            if guard is not None:
                guard.pop(f"overseas:{symbol.strip().upper()}", None)
            return None

        guard = getattr(self, "_exit_price_shock_guard", None)
        if guard is None:
            guard = {}
            self._exit_price_shock_guard = guard
        key = f"overseas:{symbol.strip().upper()}"
        previous = guard.get(key)
        confirm_pct = float(
            getattr(self.config.liquidity_lab, "overseas_exit_price_shock_confirm_pct", 0.02)
        )
        if previous is not None:
            previous_price = float(previous.get("price", 0.0) or 0.0)
            if previous_price > 0 and abs(last_price - previous_price) / reference_price <= confirm_pct:
                guard.pop(key, None)
                self._save_event(
                    event_type="trade_guard",
                    market="overseas",
                    symbol=symbol,
                    detail={
                        "reason": "price_shock_confirmed",
                        "reference_price": reference_price,
                        "last_price": last_price,
                        "shock_pct": shock_pct,
                    },
                )
                return None

        guard[key] = {
            "price": last_price,
            "reference_price": reference_price,
            "shock_pct": shock_pct,
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        reason = f"price_shock_confirm:{shock_pct:+.1%}"
        self._record_trade_skip(
            market="overseas",
            symbol=symbol,
            exchange_code=quote.exchange_code,
            reason=reason,
            side="sell",
            price=last_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            stock_name=symbol,
            activity_score=quote.activity_score,
            holding_qty=holding_qty,
        )
        return reason

    async def _build_domestic_watch_targets(
        self,
        domestic_ranked: list[DomesticScanResult],
        held_positions: list[DomesticHeldPosition],
        top_n: int | None = None,
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        held_map = {position.stock_code: position for position in held_positions}
        watch_limit = (
            top_n
            if top_n is not None
            else self.config.liquidity_lab.unified_watch_top_n
        )
        for candidate in domestic_ranked[:watch_limit]:
            signal_snapshot = await self._load_domestic_signal(candidate)
            held = held_map.get(candidate.stock_code)
            watch_targets.append(
                self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=0 if held is None else held.quantity,
                )
            )
            await asyncio.sleep(0.05)

        watched_codes = {watch_target.code for watch_target in watch_targets}
        for held in held_positions:
            if held.stock_code in watched_codes:
                continue
            candidate = next(
                (item for item in domestic_ranked if item.stock_code == held.stock_code),
                None,
            )
            if candidate is None:
                continue
            signal_snapshot = await self._load_domestic_signal(candidate)
            watch_targets.append(
                self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=held.quantity,
                )
            )
            await asyncio.sleep(0.05)
        return watch_targets

    async def _build_unified_watch_targets(
        self,
        *,
        domestic_ranked: list[DomesticScanResult],
        overseas_ranked: list[OverseasScanResult],
        domestic_positions: list[DomesticHeldPosition],
        overseas_positions: list[OverseasHeldPosition],
        krx_open: bool,
        us_open: bool,
    ) -> list[WatchTargetStatus]:
        unified: list[UnifiedScanResult] = []
        if krx_open:
            for candidate in domestic_ranked:
                unified.append(
                    UnifiedScanResult(
                        market="domestic",
                        code=candidate.stock_code,
                        exchange_code=None,
                        activity_score=candidate.activity_score,
                        domestic=candidate,
                    )
                )
        if us_open:
            for candidate in overseas_ranked:
                unified.append(
                    UnifiedScanResult(
                        market="overseas",
                        code=candidate.symbol.upper(),
                        exchange_code=candidate.exchange_code,
                        activity_score=candidate.activity_score,
                        overseas=candidate,
                    )
                )
            ranked_symbols = {candidate.symbol.upper() for candidate in overseas_ranked}
            for position in overseas_positions:
                symbol = position.symbol.upper()
                if symbol in ranked_symbols:
                    continue
                unified.append(
                    UnifiedScanResult(
                        market="overseas",
                        code=symbol,
                        exchange_code=position.exchange_code,
                        activity_score=0.0,
                        overseas=self._scan_result_from_overseas_position(position),
                    )
                )
                ranked_symbols.add(symbol)

        unified.sort(key=lambda item: item.activity_score, reverse=True)

        held_domestic_codes = {position.stock_code for position in domestic_positions}
        held_overseas_codes = {
            position.symbol.upper()
            for position in overseas_positions
        }
        selected: list[UnifiedScanResult] = []
        selected_keys: set[tuple[str, str]] = set()

        for item in unified:
            key = item.code.upper()
            is_held = (
                item.market == "domestic" and key in held_domestic_codes
            ) or (
                item.market == "overseas" and key in held_overseas_codes
            )
            pair = (item.market, key)
            if is_held and pair not in selected_keys:
                selected.append(item)
                selected_keys.add(pair)

        remaining_slots = max(0, self.config.liquidity_lab.unified_watch_top_n)
        for item in unified:
            if remaining_slots <= 0:
                break
            pair = (item.market, item.code.upper())
            if pair in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(pair)
            remaining_slots -= 1

        domestic_held_map = {position.stock_code: position for position in domestic_positions}
        overseas_held_map: dict[str, OverseasHeldPosition] = {}
        for position in overseas_positions:
            symbol = position.symbol.upper()
            existing = overseas_held_map.get(symbol)
            if existing is None or (existing.is_virtual and not position.is_virtual):
                overseas_held_map[symbol] = position

        tracker = self._get_position_tracker()
        watch_targets: list[WatchTargetStatus] = []
        for item in selected:
            if item.market == "domestic" and item.domestic is not None:
                candidate = item.domestic
                signal_snapshot = await self._load_domestic_signal(candidate)
                if signal_snapshot is None:
                    await asyncio.sleep(0.05)
                    continue
                held = domestic_held_map.get(candidate.stock_code)
                watch_target = self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=0 if held is None else held.quantity,
                )
                watch_targets.append(watch_target)
                self._save_cycle_log_from_watch_target(
                    watch_target,
                    pnl_pct=None if held is None else held.pnl_pct,
                )
                await asyncio.sleep(0.05)
                continue

            if item.market == "overseas" and item.overseas is not None:
                candidate = item.overseas
                symbol = candidate.symbol.upper()
                signal_snapshot = self._signal_cache.get(symbol)
                held = overseas_held_map.get(symbol)
                holding_qty = 0
                if tracker is not None:
                    unified_position = tracker.get_unified(
                        market="overseas",
                        symbol=symbol,
                        real_qty=0 if held is None or held.is_virtual else held.quantity,
                        currency="USD",
                        exchange_code=candidate.exchange_code,
                    )
                    holding_qty = max(0, unified_position.total_qty)
                elif held is not None:
                    holding_qty = held.quantity
                watch_target = self._build_watch_target_status(
                    market="overseas",
                    code=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=holding_qty,
                )
                watch_targets.append(watch_target)
                self._save_cycle_log_from_watch_target(
                    watch_target,
                    pnl_pct=None if held is None else held.pnl_pct,
                )
        wait_cycles = getattr(self, "_wait_cycles", None)
        if wait_cycles is None:
            wait_cycles = {}
            self._wait_cycles = wait_cycles
        for wt in watch_targets:
            key = f"{wt.market}:{wt.code.upper()}"
            if wt.action_bias == "WAIT":
                wait_cycles[key] = wait_cycles.get(key, 0) + 1
            else:
                wait_cycles.pop(key, None)

        active_keys = {f"{wt.market}:{wt.code.upper()}" for wt in watch_targets}
        stale_keys = [key for key in wait_cycles if key not in active_keys]
        for key in stale_keys:
            del wait_cycles[key]
        return watch_targets

    async def _build_overseas_watch_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        tracker = self._get_position_tracker()
        held_map: dict[str, OverseasHeldPosition] = {}
        for position in held_positions:
            symbol = position.symbol.upper()
            existing = held_map.get(symbol)
            if existing is None or (existing.is_virtual and not position.is_virtual):
                held_map[symbol] = position
        cached_symbols = set(self._signal_cache.keys())

        for candidate in overseas_ranked:
            symbol = candidate.symbol.upper()
            if symbol not in cached_symbols:
                continue
            signal_snapshot = self._signal_cache.get(symbol)
            held = held_map.get(symbol)
            holding_qty = 0
            if tracker is not None:
                unified = tracker.get_unified(
                    market="overseas",
                    symbol=symbol,
                    real_qty=0 if held is None or held.is_virtual else held.quantity,
                    currency="USD",
                    exchange_code=candidate.exchange_code,
                )
                holding_qty = max(0, unified.total_qty)
            elif held is not None:
                holding_qty = held.quantity
            watch_targets.append(
                self._build_watch_target_status(
                    market="overseas",
                    code=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=holding_qty,
                )
            )
        return watch_targets

    def _remember_persisted_symbol_state(self, state: dict | None) -> None:
        if not state:
            return
        market = str(state.get("market", "") or "").strip()
        symbol = str(state.get("symbol", "") or "").strip().upper()
        if not market or not symbol:
            return
        cache = getattr(self, "_persisted_symbol_state", None)
        if cache is None:
            cache = {}
            self._persisted_symbol_state = cache
        cache[(market, symbol)] = state

    def _get_persisted_symbol_state(self, market: str, symbol: str) -> dict | None:
        key = (market, symbol.strip().upper())
        cache = getattr(self, "_persisted_symbol_state", None)
        if cache is None:
            cache = {}
            self._persisted_symbol_state = cache
        cached = cache.get(key)
        if cached is not None:
            return cached
        repository = getattr(self, "repository", None)
        if repository is None:
            return None
        state = repository.get_lab_symbol_state(market, symbol.strip().upper())
        if state is not None:
            self._remember_persisted_symbol_state(state)
        return state

    @staticmethod
    def _snapshot_from_payload(
        payload: dict | None,
    ) -> MovingAverageSnapshot | None:
        if not payload:
            return None
        try:
            return MovingAverageSnapshot(**payload)
        except TypeError:
            return None

    @staticmethod
    def _with_live_price(
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

    def _state_snapshot_with_live_price(
        self,
        state: dict | None,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> MovingAverageSnapshot | None:
        snapshot = self._snapshot_from_payload(
            state.get("snapshot_json") if state else None
        )
        if snapshot is None:
            return None
        return self._with_live_price(snapshot, price=price, bid=bid, ask=ask)

    async def _get_overseas_signal_for_candidate(
        self,
        candidate: OverseasScanResult,
    ) -> MovingAverageSnapshot | None:
        symbol = candidate.symbol.upper()
        now_utc = datetime.now(timezone.utc)
        auto_trade_cfg = getattr(self.config, "auto_trade", None)
        refresh_sec = max(
            5,
            int(getattr(auto_trade_cfg, "intraday_chart_refresh_sec", 60) or 60),
        )
        cached = self._signal_cache.get(symbol)
        updated_map = getattr(self, "_signal_cache_updated_at", None)
        if updated_map is None:
            updated_map = {}
            self._signal_cache_updated_at = updated_map
        cached_at = updated_map.get(symbol)
        if (
            cached is not None
            and cached_at is not None
            and (now_utc - cached_at).total_seconds() < refresh_sec
        ):
            return self._with_live_price(
                cached,
                price=candidate.last_price,
                bid=candidate.bid,
                ask=candidate.ask,
            )

        snapshot = await self._load_overseas_signal(candidate)
        if snapshot is not None:
            self._signal_cache[symbol] = snapshot
            updated_map[symbol] = now_utc
            return snapshot

        if cached is not None:
            fallback = self._with_live_price(
                cached,
                price=candidate.last_price,
                bid=candidate.bid,
                ask=candidate.ask,
            )
            self._signal_cache[symbol] = fallback
            updated_map[symbol] = now_utc
            return fallback

        state = self._get_persisted_symbol_state("overseas", symbol)
        fallback = self._state_snapshot_with_live_price(
            state,
            price=candidate.last_price,
            bid=candidate.bid,
            ask=candidate.ask,
        )
        if fallback is not None:
            self._signal_cache[symbol] = fallback
            updated_map[symbol] = now_utc
            _logger.info(
                "overseas_signal_fallback_used symbol=%s source=persisted_state",
                symbol,
            )
        return fallback

    def _persist_watch_target_state(
        self,
        watch_target: WatchTargetStatus,
        *,
        pnl_pct: float | None = None,
        exit_by: str = "",
    ) -> None:
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        symbol = watch_target.code.strip().upper()
        manager = getattr(self, "_strategy_managers", {}).get(symbol)
        entry_price = None
        peak_price = None
        if manager is not None and manager.position is not None:
            entry_price = float(manager.position.entry_price)
            peak_price = float(manager.position.peak_price)
        state = self._get_persisted_symbol_state(watch_target.market, symbol) or {}
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
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._remember_persisted_symbol_state(
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
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _persist_trade_state(
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
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        manager = getattr(self, "_strategy_managers", {}).get(symbol.strip().upper())
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
        self._remember_persisted_symbol_state(
            repository.get_lab_symbol_state(market, symbol.strip().upper())
        )

    def _clear_stale_lab_position_states(
        self,
        *,
        domestic_positions: list[DomesticHeldPosition],
        overseas_positions: list[OverseasHeldPosition],
        refreshed_markets: set[str],
    ) -> None:
        repository = getattr(self, "repository", None)
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
        persisted = getattr(self, "_persisted_symbol_state", None)
        if persisted is not None:
            for row in cleared:
                persisted.pop(
                    (
                        str(row.get("market", "")).strip().lower(),
                        str(row.get("symbol", "")).strip().upper(),
                    ),
                    None,
                )
        self._save_event(
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

    def _restore_strategy_contexts(
        self,
        *,
        domestic_positions: list[DomesticHeldPosition],
        overseas_positions: list[OverseasHeldPosition],
    ) -> None:
        for position in domestic_positions:
            self._restore_strategy_position(
                market="domestic",
                symbol=position.stock_code,
                exchange_code=None,
                quantity=position.quantity,
                avg_price=position.avg_price,
                current_price=position.current_price,
            )
        for position in overseas_positions:
            symbol = position.symbol.upper()
            self._restore_strategy_position(
                market="overseas",
                symbol=symbol,
                exchange_code=position.exchange_code,
                quantity=position.quantity,
                avg_price=position.avg_price,
                current_price=position.current_price,
            )

    def _restore_strategy_position(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        quantity: int,
        avg_price: float,
        current_price: float,
    ) -> None:
        if quantity <= 0:
            return
        manager = self._get_strategy_manager(symbol)
        if manager.position is not None:
            return
        state = self._get_persisted_symbol_state(market, symbol)
        if state is None:
            return
        strategy_flag = str(state.get("strategy_flag", "") or "")
        entry_by = str(state.get("entry_by", "") or "")
        triggered = self._decode_strategy_ids(strategy_flag, entry_by)
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

    def _build_watch_target_status(
        self,
        *,
        market: str,
        code: str,
        exchange_code: str | None,
        price: float,
        activity_score: float,
        signal_snapshot: MovingAverageSnapshot | None,
        held_position: OverseasHeldPosition | DomesticHeldPosition | None = None,
        holding_qty: int = 0,
    ) -> WatchTargetStatus:
        if signal_snapshot is None:
            persisted = self._get_persisted_symbol_state(market, code)
            fallback_snapshot = self._state_snapshot_with_live_price(
                persisted,
                price=price,
            )
            if fallback_snapshot is not None:
                if held_position is not None:
                    existing_flag = str(persisted.get("strategy_flag", "") or "")
                    existing_entry_by = str(persisted.get("entry_by", "") or "")
                    exit_setup = self._build_exit_setup(
                        fallback_snapshot,
                        held_position.pnl_pct,
                        holding_qty,
                        symbol=code,
                        take_profit_override=(
                            getattr(self.config.liquidity_lab, "overseas_take_profit_pct", None)
                            if market == "overseas"
                            else None
                        ),
                    )
                    if exit_setup.action in {"sell", "sell_partial"}:
                        return WatchTargetStatus(
                            market=market,
                            code=code,
                            exchange_code=exchange_code,
                            price=price,
                            activity_score=activity_score,
                            signal_score=0.0,
                            action_bias="SELL",
                            signal_state="SELL_READY",
                            ma_summary=self._ma_relation_summary(fallback_snapshot),
                            note=exit_setup.reason,
                            holding_qty=holding_qty,
                            signal_snapshot=fallback_snapshot,
                            strategy_flag=existing_flag,
                            entry_by=existing_entry_by,
                        )
                    return WatchTargetStatus(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="HOLD",
                        signal_state="HOLD",
                        ma_summary=self._ma_relation_summary(fallback_snapshot),
                        note=f"{exit_setup.note}|stale_signal_cache",
                        holding_qty=holding_qty,
                        signal_snapshot=fallback_snapshot,
                        strategy_flag=existing_flag,
                        entry_by=existing_entry_by,
                    )
                strategy_result = self._get_strategy_manager(code).evaluate(
                    code,
                    fallback_snapshot,
                    commit=False,
                )
                signal_state, note = derive_watch_state(
                    self.config.auto_trade,
                    fallback_snapshot,
                    symbol=code,
                    inverse_etf_symbols=getattr(self.config.liquidity_lab, "inverse_etf_symbols", []),
                    leveraged_etf_symbols=getattr(self.config.liquidity_lab, "leveraged_etf_symbols", []),
                )
                if (
                    self._should_block_overseas_standalone_vwap(
                        market=market,
                        strategy_flag=strategy_result.flag,
                    )
                    and (strategy_result.signal == "BUY" or signal_state == "BUY")
                ):
                    return WatchTargetStatus(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="WAIT",
                        signal_state="WAIT",
                        ma_summary=self._ma_relation_summary(fallback_snapshot),
                        note="[VWAP] standalone_vwap_blocked|stale_signal_cache",
                        holding_qty=holding_qty,
                        signal_snapshot=fallback_snapshot,
                        strategy_flag=strategy_result.flag,
                        entry_by=strategy_result.entry_by,
                    )
                return WatchTargetStatus(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias=signal_state,
                    signal_state=signal_state,
                    ma_summary=self._ma_relation_summary(fallback_snapshot),
                    note=f"{note}|stale_signal_cache",
                    holding_qty=holding_qty,
                    signal_snapshot=fallback_snapshot,
                    strategy_flag=strategy_result.flag or str(persisted.get("strategy_flag", "") or ""),
                    entry_by=strategy_result.entry_by or str(persisted.get("entry_by", "") or ""),
                )
            return WatchTargetStatus(
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

        existing_flag, existing_entry_by, _ = self._get_strategy_labels(code, signal_snapshot)
        if held_position is not None:
            exit_setup = self._build_exit_setup(
                signal_snapshot,
                held_position.pnl_pct,
                holding_qty,
                symbol=code,
                take_profit_override=(
                    getattr(self.config.liquidity_lab, "overseas_take_profit_pct", None)
                    if market == "overseas"
                    else None
                ),
            )
            if exit_setup.action in {"sell", "sell_partial"}:
                return WatchTargetStatus(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias="SELL",
                    signal_state="SELL_READY",
                    ma_summary=self._ma_relation_summary(signal_snapshot),
                    note=exit_setup.reason,
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=existing_flag,
                    entry_by=existing_entry_by,
                )
            return WatchTargetStatus(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=0.0,
                action_bias="HOLD",
                signal_state="HOLD",
                ma_summary=self._ma_relation_summary(signal_snapshot),
                note=exit_setup.note,
                holding_qty=holding_qty,
                signal_snapshot=signal_snapshot,
                strategy_flag=existing_flag,
                entry_by=existing_entry_by,
            )

        inverse_symbols = getattr(self.config.liquidity_lab, "inverse_etf_symbols", [])
        leveraged_symbols = getattr(self.config.liquidity_lab, "leveraged_etf_symbols", [])
        entry_setup = evaluate_entry_setup(
            self.config.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=inverse_symbols,
            leveraged_etf_symbols=leveraged_symbols,
        )
        strategy_result = self._get_strategy_manager(code).evaluate(
            code,
            signal_snapshot,
            commit=False,
        )
        if strategy_result.signal == "BUY":
            if self._should_block_overseas_standalone_vwap(
                market=market,
                strategy_flag=strategy_result.flag,
            ):
                return WatchTargetStatus(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=entry_setup.score,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=self._ma_relation_summary(signal_snapshot),
                    note="[VWAP] standalone_vwap_blocked",
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
                return WatchTargetStatus(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=entry_setup.score,
                    action_bias="WAIT",
                    signal_state="WAIT",
                    ma_summary=self._ma_relation_summary(signal_snapshot),
                    note=f"[{strategy_result.flag}] confirm_wait:{entry_setup.reason}",
                    holding_qty=holding_qty,
                    signal_snapshot=signal_snapshot,
                    strategy_flag=strategy_result.flag,
                    entry_by=strategy_result.entry_by,
                )
            exit_cooldown = getattr(self, "_exit_cooldown", None)
            if exit_cooldown is None:
                exit_cooldown = {}
                self._exit_cooldown = exit_cooldown
            cooldown_key = f"{market}:{code.upper()}"
            cooldown_until = exit_cooldown.get(cooldown_key)
            now_utc = datetime.now(timezone.utc)
            if cooldown_until is not None:
                cooldown_until = ensure_timezone(cooldown_until)
                exit_cooldown[cooldown_key] = cooldown_until
                if now_utc < cooldown_until:
                    remaining_min = max(
                        1,
                        int((cooldown_until - now_utc).total_seconds() / 60),
                    )
                    self._save_event(
                        event_type="cooldown_blocked",
                        market=market,
                        symbol=code,
                        detail={
                            "reason": "reentry_cooldown",
                            "remaining_min": remaining_min,
                        },
                    )
                    return WatchTargetStatus(
                        market=market,
                        code=code,
                        exchange_code=exchange_code,
                        price=price,
                        activity_score=activity_score,
                        signal_score=0.0,
                        action_bias="WAIT",
                        signal_state="WAIT",
                        ma_summary=self._ma_relation_summary(signal_snapshot),
                        note=f"재진입대기 {remaining_min}분",
                        holding_qty=holding_qty,
                        signal_snapshot=signal_snapshot,
                        strategy_flag="",
                        entry_by="",
                    )
                del exit_cooldown[cooldown_key]
            combined_score = (
                self._get_strategy_manager(code).buy_score(signal_snapshot)
                + entry_setup.score
            )
            return WatchTargetStatus(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=round(combined_score, 2),
                action_bias="BUY",
                signal_state="BUY",
                ma_summary=self._ma_relation_summary(signal_snapshot),
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
        signal_state, note = derive_watch_state(
            self.config.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=inverse_symbols,
            leveraged_etf_symbols=leveraged_symbols,
        )
        return WatchTargetStatus(
            market=market,
            code=code,
            exchange_code=exchange_code,
            price=price,
            activity_score=activity_score,
            signal_score=entry_setup.score,
            action_bias=signal_state,
            signal_state=signal_state,
            ma_summary=self._ma_relation_summary(signal_snapshot),
            note=note,
            holding_qty=holding_qty,
            signal_snapshot=signal_snapshot,
            strategy_flag=strategy_result.flag,
            entry_by=strategy_result.entry_by,
        )

    def _save_cycle_log_from_watch_target(
        self,
        watch_target: WatchTargetStatus,
        *,
        pnl_pct: float | None = None,
    ) -> None:
        signal_snapshot = watch_target.signal_snapshot
        exit_by = ""
        if signal_snapshot is not None:
            _, _, exit_by = self._get_strategy_labels(
                watch_target.code,
                signal_snapshot,
            )
        self.repository.save_cycle_log(
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
            cycle_no=getattr(self, "_cycle_count", 0),
            session_id=getattr(self, "_session_id", ""),
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
            consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
        )
        self._persist_watch_target_state(
            watch_target,
            pnl_pct=pnl_pct,
            exit_by=exit_by,
        )

    def _select_domestic_buy_targets(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
        max_concurrent: int = 2,
    ) -> list[DomesticScanResult]:
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

    def _select_domestic_exit_target(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
        held_positions: list[DomesticHeldPosition],
    ) -> tuple[DomesticScanResult, DomesticHeldPosition, str, MovingAverageSnapshot | None] | None:
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        held_map = {position.stock_code: position for position in held_positions}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic"
            and watch_target.action_bias == "SELL"
            and watch_target.code in held_map
            and held_map[watch_target.code].quantity > 0
            and self._cooldown_remaining_minutes("domestic", watch_target.code) <= 0
        ]
        for held in held_positions:
            if held.orderable_qty <= 0:
                self._track_no_orderable_stall(
                    market="domestic",
                    symbol=held.stock_code,
                    holding_qty=held.quantity,
                )
                self._defer_no_orderable_position(
                    market="domestic",
                    symbol=held.stock_code,
                    holding_qty=held.quantity,
                    orderable_qty=held.orderable_qty,
                )
            else:
                self._clear_no_orderable_retry("domestic", held.stock_code)
                self._reset_no_orderable_stall("domestic", held.stock_code)
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

    def _select_overseas_buy_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        watch_targets: list[WatchTargetStatus],
        max_concurrent: int = 3,
        held_positions: list[OverseasHeldPosition] | None = None,
    ) -> list[OverseasScanResult]:
        candidate_map = {candidate.symbol.upper(): candidate for candidate in overseas_ranked}
        held_symbols: set[str] = set()
        if held_positions:
            held_symbols = {
                held.symbol.upper()
                for held in held_positions
                if getattr(held, "quantity", 0) > 0
            }
        virtual_manager = getattr(self, "virtual_trades", None)
        if virtual_manager is not None:
            for position in virtual_manager.list_positions("overseas"):
                if position.qty > 0:
                    held_symbols.add(position.symbol.upper())
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "overseas"
            and watch_target.action_bias == "BUY"
            and watch_target.code.upper() not in held_symbols
            and not self._should_block_overseas_standalone_vwap(
                market=watch_target.market,
                strategy_flag=watch_target.strategy_flag,
            )
        ]
        if not ready_targets or max_concurrent <= 0:
            return []
        inverse_set = {
            symbol.upper()
            for symbol in getattr(self.config.liquidity_lab, "inverse_etf_symbols", [])
        }

        def sort_key(item: WatchTargetStatus) -> tuple[int, float, float]:
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
    def _remaining_overseas_entry_slots(
        positions: list[OverseasHeldPosition],
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
    def _select_primary_target(
        *,
        krx_open: bool,
        us_open: bool,
        us_orderable_in_profile: bool,
        domestic_ranked: list[DomesticScanResult],
        overseas_ranked: list[OverseasScanResult],
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

    async def _place_domestic_test_order(
        self,
        candidate: DomesticScanResult,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        signal_snapshot = None if watch_target is None else watch_target.signal_snapshot
        buy_price = float(candidate.best_ask or candidate.current_price)
        config = self.config.liquidity_lab
        qty = config.domestic_test_order_qty
        if config.use_slot_sizing:
            try:
                available_krw = await self._get_domestic_available_krw()
            except KisApiError:
                available_krw = 0.0
            slot_qty = self._slot_based_qty(
                available_amount=available_krw,
                price=float(candidate.best_ask or candidate.current_price),
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_krw > 0:
                self._record_trade_skip(
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
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
            }
        try:
            response = await self.client.place_cash_order(
                side="buy",
                stock_code=candidate.stock_code,
                qty=qty,
                price=candidate.best_ask or candidate.current_price,
                order_division="00",
            )
        except KisApiError as exc:
            error_text = str(exc)
            self._record_trade_skip(
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
            self._record_broker_order_event(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                side="BUY",
                order_kind="limit",
                requested_qty=qty,
                requested_price=float(candidate.best_ask or candidate.current_price),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                status="REJECTED",
                reason="order_rejected",
                payload={"error": error_text},
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
        self._record_broker_order_event(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
            side="BUY",
            order_kind="limit",
            requested_qty=qty,
            requested_price=float(candidate.best_ask or candidate.current_price),
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            status="SUBMITTED",
            reason="domestic_buy",
            payload=response if isinstance(response, dict) else {"response": response},
        )
        self._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("domestic"),
                    self._format_trade_symbol_label("domestic", candidate.stock_code),
                    "매수접수",
                    f"{int(candidate.best_ask or candidate.current_price):,}원",
                    f"x{qty}",
                    f"전략={strategy_flag or '-'}",
                    f"주도={entry_by or '-'}",
                ]
            )
        )
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        self._commit_strategy_entry(
            candidate.stock_code,
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        self._mark_session_owned(candidate.stock_code)
        repository = getattr(self, "repository", None)
        if repository is not None:
            commission_krw = round(buy_price * qty * self._domestic_commission_rate(), 2)
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
                intraday_bar_return=signal_snapshot.intraday_bar_return if signal_snapshot else None,
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
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
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
                cb_active=self._cb_active_flag(),
                pool_size=self._pool_size_for_market("domestic"),
            )
        self._persist_trade_state(
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
            "response": response,
        }

    async def _place_domestic_sell_order(
        self,
        candidate: DomesticScanResult,
        held: DomesticHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        strategy_flag, entry_by, exit_by = self._get_strategy_labels(candidate.stock_code, signal_snapshot)
        exit_by = exit_by or exit_reason
        entry_label, exit_label = self._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        if self.config.credentials.dry_run:
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
        sell_qty = min(held.quantity, max(held.orderable_qty, 0))
        replacement_note = ""
        pending_sell_order = await self._find_open_domestic_order(
            symbol=candidate.stock_code,
            side="SELL",
        )
        if pending_sell_order is not None:
            pending_age_sec = self._pending_order_age_seconds(pending_sell_order)
            if exit_reason not in self._protective_exit_reasons() or pending_age_sec < 45:
                self._record_trade_skip(
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
                cancel_response = await self._cancel_open_domestic_order(
                    symbol=candidate.stock_code,
                    pending_order=pending_sell_order,
                )
            except KisApiError as exc:
                error_text = str(exc)
                self._record_trade_skip(
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
            self._record_broker_order_event(
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
                payload=cancel_response if isinstance(cancel_response, dict) else {"response": cancel_response},
            )
            replacement_note = "미체결 매도 정정 후 재주문"
            if sell_qty <= 0:
                sell_qty = min(
                    held.quantity,
                    int(pending_sell_order.get("open_qty") or held.quantity),
                )
        if sell_qty <= 0:
            self._record_trade_skip(
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
        if held.avg_price > 0 and self._is_profit_exit_reason(exit_reason):
            estimated_net_krw, _ = self._estimate_domestic_net_pnl_krw(
                entry_price=float(held.avg_price or 0.0),
                exit_price=sell_price,
                qty=sell_qty,
            )
            if estimated_net_krw <= 0:
                self._record_trade_skip(
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
                self._persist_trade_state(
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
            response = await self.client.place_cash_order(
                side="sell",
                stock_code=candidate.stock_code,
                qty=sell_qty,
                price=int(sell_price),
                order_division="00",
            )
        except KisApiError as exc:
            error_text = str(exc)
            self._set_exit_cooldown_minutes("domestic", candidate.stock_code, 10)
            _logger.warning(
                "[SELL] domestic order_rejected %s -> 10분 쿨다운 등록 (error=%s)",
                candidate.stock_code,
                exc,
            )
            self._save_event(
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
            self._record_trade_skip(
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
            self._record_broker_order_event(
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                side="SELL",
                order_kind="limit",
                requested_qty=sell_qty,
                requested_price=sell_price,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="REJECTED",
                reason="order_rejected",
                payload={"error": error_text},
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
        if replacement_note:
            lines.append(f"참고={replacement_note}")
        if held.avg_price > 0:
            gross_pnl = (sell_price - held.avg_price) * sell_qty
            pnl_pct = (sell_price - held.avg_price) / held.avg_price
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("수익률=알수없음")
        self._record_broker_order_event(
            market="domestic",
            symbol=candidate.stock_code,
            exchange_code=None,
            side="SELL",
            order_kind="limit",
            requested_qty=sell_qty,
            requested_price=sell_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status="SUBMITTED",
            reason=exit_reason,
            payload=response if isinstance(response, dict) else {"response": response},
        )
        queue_parts = [
            format_market_korean("domestic"),
            self._format_trade_symbol_label("domestic", candidate.stock_code),
            "매도접수",
            format_krw(sell_price),
            f"x{sell_qty}",
            f"수익률={format_pct(pnl_pct) if held.avg_price > 0 else '-'}",
            f"매수={entry_label}",
            f"청산={exit_label}",
        ]
        if replacement_note:
            queue_parts.append(f"참고={replacement_note}")
        self._queue_trade_notification(" ".join(queue_parts))
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        entry_price, entry_time_iso, hold_duration_min = self._get_entry_context(
            "domestic",
            candidate.stock_code,
            fallback_price=held.avg_price,
        )
        self._reset_strategy_position(candidate.stock_code)
        self._register_exit_cooldown("domestic", candidate.stock_code, exit_reason)
        if held.avg_price > 0:
            self._session_realised_krw = getattr(self, "_session_realised_krw", 0.0) + float(gross_pnl)
            if gross_pnl < 0:
                self._consecutive_losses = getattr(self, "_consecutive_losses", 0) + 1
            else:
                self._consecutive_losses = 0
            if self._is_trading_halted():
                _logger.warning(
                    "[CB] 서킷브레이커 발동 consecutive=%d session_pnl=%.0f",
                    self._consecutive_losses,
                    self._session_realised_krw,
                )
                notifier = getattr(self, "notifier", None)
                if notifier is not None and getattr(notifier, "enabled", True):
                    asyncio.create_task(
                        notifier.send(
                            f"⛔ 서킷브레이커 발동\n"
                            f"연속손절 {self._consecutive_losses}회 | "
                            f"세션손익 {self._session_realised_krw:+,.0f}원\n"
                            f"신규 매수를 중단합니다."
                        )
                    )
            if entry_price is None:
                entry_price = float(held.avg_price or 0.0)
            net_pnl_krw, sell_commission_krw = self._estimate_domestic_net_pnl_krw(
                entry_price=float(entry_price or 0.0),
                exit_price=sell_price,
                qty=sell_qty,
            )
            self.repository.save_cycle_log(
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
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                is_session_trade=1 if self._is_session_owned(candidate.stock_code) else 0,
                consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
                hold_cycles=self._estimate_hold_cycles(candidate.stock_code),
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
                cb_active=self._cb_active_flag(),
                pool_size=self._pool_size_for_market("domestic"),
                activity_score=candidate.activity_score,
            )
        self._persist_trade_state(
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
            "replacement_note": replacement_note,
            "response": response,
        }

    async def _place_overseas_test_order(
        self,
        candidate: OverseasScanResult,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        signal_snapshot = self._signal_cache.get(candidate.symbol.upper())
        if signal_snapshot is None and watch_target is not None:
            signal_snapshot = watch_target.signal_snapshot
        if signal_snapshot is None:
            signal_snapshot = await self._load_overseas_signal(candidate)
            self._signal_cache[candidate.symbol.upper()] = signal_snapshot
        if signal_snapshot is None:
            self._record_trade_skip(
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

        # strategy 레이어(게이트 1)에서 이미 BUY 판단이 완료된 상태.
        # watch_target.action_bias == "BUY" 인 종목만 이 함수에 도달하므로
        # momentum_policy 재검사(이중 게이트)는 제거한다.
        strategy_flag = ""
        entry_by = ""
        buy_reason = "strategy_buy_signal"
        if watch_target is not None:
            strategy_flag = watch_target.strategy_flag
            entry_by = watch_target.entry_by
            buy_reason = watch_target.note or buy_reason
        if not strategy_flag or not entry_by:
            strategy_flag, entry_by, _ = self._get_strategy_labels(candidate.symbol, signal_snapshot)
        if self._should_block_overseas_standalone_vwap(
            market="overseas",
            strategy_flag=strategy_flag,
        ):
            self._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="standalone_vwap_blocked",
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
                "reason": "standalone_vwap_blocked",
            }

        config = self.config.liquidity_lab
        qty = config.overseas_test_order_qty
        buy_price = self._overseas_buy_order_price(candidate)
        if config.use_slot_sizing:
            try:
                available_usd = await self._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=buy_price,
                )
            except KisApiError:
                available_usd = 0.0
            slot_qty = self._slot_based_qty(
                available_amount=available_usd,
                price=buy_price,
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_usd > 0:
                self._record_trade_skip(
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
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "side": "buy",
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
            }
        conflicting_sell_order = await self._find_conflicting_overseas_order(
            symbol=candidate.symbol,
            side="BUY",
            exchange_code=candidate.exchange_code,
        )
        if conflicting_sell_order is not None:
            conflicting_age_sec = self._pending_order_age_seconds(conflicting_sell_order)
            if conflicting_age_sec < 60:
                self._record_trade_skip(
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
                cancel_response = await self._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=conflicting_sell_order,
                )
            except KisApiError as exc:
                self._record_trade_skip(
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
            self._record_broker_order_event(
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
                payload=cancel_response if isinstance(cancel_response, dict) else {"response": cancel_response},
            )
        pending_buy_order = await self._find_open_overseas_order(
            symbol=candidate.symbol,
            side="BUY",
            exchange_code=candidate.exchange_code,
        )
        if pending_buy_order is not None:
            pending_age_sec = self._pending_order_age_seconds(pending_buy_order)
            if pending_age_sec < 120:
                self._record_trade_skip(
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
                await self._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=pending_buy_order,
                )
            except KisApiError as exc:
                self._record_trade_skip(
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
        try:
            response = await self.client.place_overseas_order_for_current_session(
                side="buy",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=qty,
                price=f"{buy_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            error_text = str(exc)
            if self._is_mock_us_session_blocked_error(str(exc)):
                return await self._record_virtual_overseas_buy(
                    candidate,
                    signal_snapshot=signal_snapshot,
                    rejected_error=error_text,
                )
            self._record_trade_skip(
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
            self._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="BUY",
                order_kind="limit",
                requested_qty=qty,
                requested_price=buy_price,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                status="REJECTED",
                reason="order_rejected",
                payload={"error": error_text},
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
            }
        repository = getattr(self, "repository", None)
        if repository is not None:
            commission_usd = round(
                buy_price * qty * self._overseas_commission_rate(),
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
                intraday_bar_return=signal_snapshot.intraday_bar_return if signal_snapshot else None,
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
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
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
                cb_active=self._cb_active_flag(),
                pool_size=self._pool_size_for_market("overseas"),
            )
        self._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="BUY",
            order_kind="limit",
            requested_qty=qty,
            requested_price=buy_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            status="SUBMITTED",
            reason=buy_reason,
            payload=response if isinstance(response, dict) else {"response": response},
        )
        self._queue_trade_notification(
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
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        self._commit_strategy_entry(
            candidate.symbol,
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        self._persist_trade_state(
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
        self._mark_session_owned(candidate.symbol)
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
            "response": response,
        }

    async def _manage_overseas_position(
        self,
        *,
        candidate: OverseasScanResult,
        held_positions: list[OverseasHeldPosition],
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        config = self.config.liquidity_lab
        tracker = self._get_position_tracker()
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

        return await self._place_overseas_test_order(candidate, watch_target=watch_target)

    async def _record_virtual_overseas_buy(
        self,
        candidate: OverseasScanResult,
        *,
        signal_snapshot: MovingAverageSnapshot | None = None,
        rejected_error: str | None = None,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        config = self.config.liquidity_lab
        qty = int(config.overseas_test_order_qty)
        snapshot = signal_snapshot or self._signal_cache.get(candidate.symbol.upper())
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        if snapshot is not None and (not strategy_flag or not entry_by):
            strategy_flag, entry_by, _ = self._get_strategy_labels(candidate.symbol, snapshot)
        if self._should_block_overseas_standalone_vwap(
            market="overseas",
            strategy_flag=strategy_flag,
        ):
            self._record_trade_skip(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                reason="standalone_vwap_blocked",
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
                "reason": "standalone_vwap_blocked",
            }
        if config.use_slot_sizing:
            try:
                available_usd = await self._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                )
            except KisApiError:
                available_usd = 0.0
            remaining_virtual_budget = self._remaining_virtual_overseas_budget(available_usd)
            if remaining_virtual_budget <= 0:
                virtual_notional_usd = self._open_virtual_overseas_notional()
                self._record_trade_skip(
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
            slot_qty = self._slot_based_qty(
                available_amount=available_usd,
                price=candidate.last_price,
                max_budget=remaining_virtual_budget,
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_usd > 0:
                self._record_trade_skip(
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
        position = self.virtual_trades.record_buy(
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

        lines = [
            "[KIS][VIRTUAL_TRADE]",
            f"시각={format_kst_korean(now)}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol} (virtual)",
            "구분=가상매수",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={qty}주",
            f"전략={strategy_flag or '-'}",
            f"주도={entry_by or '-'}",
        ]
        if rejected_error:
            lines.append(f"참고={rejected_error}")
        self._record_broker_order_event(
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
        self._queue_trade_notification(
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
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        self._commit_strategy_entry(
            candidate.symbol,
            snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        self._persist_trade_state(
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

    async def _place_overseas_sell_order(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        strategy_flag, entry_by, exit_by = self._get_strategy_labels(candidate.symbol, signal_snapshot)
        exit_by = exit_by or exit_reason
        entry_label, exit_label = self._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        tracker = self._get_position_tracker()
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
            return await self._record_virtual_overseas_sell(
                candidate,
                held,
                exit_reason,
                signal_snapshot=signal_snapshot,
                sell_qty_override=target_sell_qty,
            )
        if self.config.credentials.dry_run:
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
            self._record_trade_skip(
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
            return await self._record_virtual_overseas_sell(
                candidate,
                held,
                exit_reason,
                signal_snapshot=signal_snapshot,
                sell_qty_override=target_sell_qty,
            )
        if not is_us_orderable_session_for_env(now, self.config.credentials.env):
            self._record_trade_skip(
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
            self._persist_trade_state(
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
        sell_price = self._overseas_sell_order_price(candidate, exit_reason=exit_reason)
        pnl_pct = (sell_price - held.avg_price) / held.avg_price if held.avg_price > 0 else None
        if held.avg_price > 0 and self._is_profit_exit_reason(exit_reason):
            auto_trade_cfg = getattr(self.config, "auto_trade", None)
            fx_rate = getattr(auto_trade_cfg, "usd_krw_fallback_rate", 1380.0)
            estimated_net_usd, _, _, _ = self._estimate_overseas_net_pnl(
                entry_price=float(held.avg_price or 0.0),
                exit_price=sell_price,
                qty=real_sell_qty,
                fx_rate=fx_rate,
            )
            if estimated_net_usd <= 0:
                self._record_trade_skip(
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
                self._persist_trade_state(
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
        conflicting_buy_order = await self._find_conflicting_overseas_order(
            symbol=candidate.symbol,
            side="SELL",
            exchange_code=candidate.exchange_code,
        )
        if conflicting_buy_order is not None:
            conflicting_age_sec = self._pending_order_age_seconds(conflicting_buy_order, now=now)
            if exit_reason not in self._protective_exit_reasons() and conflicting_age_sec < 30:
                self._record_trade_skip(
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
                cancel_response = await self._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=conflicting_buy_order,
                )
            except KisApiError as exc:
                self._record_trade_skip(
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
            self._record_broker_order_event(
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
        pending_sell_order = await self._find_open_overseas_order(
            symbol=candidate.symbol,
            side="SELL",
            exchange_code=candidate.exchange_code,
        )
        if pending_sell_order is not None:
            pending_age_sec = self._pending_order_age_seconds(pending_sell_order, now=now)
            if exit_reason not in self._protective_exit_reasons() or pending_age_sec < 45:
                self._record_trade_skip(
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
                cancel_response = await self._cancel_open_overseas_order(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    pending_order=pending_sell_order,
                )
            except KisApiError as exc:
                self._record_trade_skip(
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
            self._record_broker_order_event(
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
                payload=cancel_response if isinstance(cancel_response, dict) else {"response": cancel_response},
            )
            replacement_note = "미체결 매도 정정 후 재주문"

        try:
            response = await self.client.place_overseas_order_for_current_session(
                side="sell",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=real_sell_qty,
                price=f"{sell_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            error_text = str(exc)
            if self._is_mock_us_balance_missing_error(str(exc)):
                self._defer_no_orderable_position(
                    market="overseas",
                    symbol=candidate.symbol,
                    holding_qty=held.quantity,
                    orderable_qty=0,
                )
                self._record_trade_skip(
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
                self._record_broker_order_event(
                    market="overseas",
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    side="SELL",
                    order_kind="limit",
                    requested_qty=real_sell_qty,
                    requested_price=sell_price,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=exit_by,
                    status="REJECTED",
                    reason="no_orderable_qty",
                    payload={"error": error_text},
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
                if self._is_mock_us_session_blocked_error(error_text)
                or not is_us_orderable_session_for_env(
                    datetime.now(timezone.utc),
                    self.config.credentials.env,
                )
                else "order_rejected"
            )
            if reject_reason == "order_rejected":
                self._set_exit_cooldown_minutes("overseas", candidate.symbol, 20)
                _logger.warning(
                    "[SELL] order_rejected %s -> 20분 쿨다운 등록 (error=%s)",
                    candidate.symbol,
                    exc,
                )
                self._save_event(
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
            self._record_trade_skip(
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
            self._record_broker_order_event(
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                side="SELL",
                order_kind="limit",
                requested_qty=real_sell_qty,
                requested_price=sell_price,
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                status="REJECTED",
                reason=reject_reason,
                payload={"error": error_text},
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
        existing_pending = self.repository.get_virtual_sell_pending("overseas", candidate.symbol)
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
        self._record_broker_order_event(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            side="SELL",
            order_kind="limit",
            requested_qty=real_sell_qty,
            requested_price=sell_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            status="SUBMITTED",
            reason=exit_reason,
            payload={
                "response": response,
                "sell_result": sell_result,
                "requested_qty": target_sell_qty,
            },
        )
        self._queue_trade_notification(
            " ".join(
                [
                    format_market_korean("overseas"),
                    candidate.symbol,
                    "매도접수",
                    format_usd(sell_price),
                    f"x{int(sell_result.get('qty_from_real', real_sell_qty) or real_sell_qty)}",
                    f"수익률={format_pct(pnl_pct) if held.avg_price > 0 else '-'}",
                    f"매수={entry_label}",
                    f"청산={exit_label}",
                ]
            )
        )
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        entry_price, entry_time_iso, hold_duration_min = self._get_entry_context(
            "overseas",
            candidate.symbol,
            fallback_price=held.avg_price,
        )
        self._reset_strategy_position(candidate.symbol)
        self._register_exit_cooldown("overseas", candidate.symbol, exit_reason)
        if held.avg_price > 0:
            real_qty_sold = int(sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty)
            auto_trade_cfg = getattr(self.config, "auto_trade", None)
            fx_rate = getattr(auto_trade_cfg, "usd_krw_fallback_rate", 1380.0)
            gross_pnl_usd = (sell_price - held.avg_price) * real_qty_sold
            gross_pnl_krw = gross_pnl_usd * fx_rate
            self._session_realised_krw = getattr(self, "_session_realised_krw", 0.0) + float(gross_pnl_krw)
            if gross_pnl_krw < 0:
                self._consecutive_losses = getattr(self, "_consecutive_losses", 0) + 1
            else:
                self._consecutive_losses = 0
            if self._is_trading_halted():
                _logger.warning(
                    "[CB] 서킷브레이커 발동 consecutive=%d session_pnl=%.0f",
                    self._consecutive_losses,
                    self._session_realised_krw,
                )
                notifier = getattr(self, "notifier", None)
                if notifier is not None and getattr(notifier, "enabled", True):
                    asyncio.create_task(
                        notifier.send(
                            f"⛔ 서킷브레이커 발동\n"
                            f"연속손절 {self._consecutive_losses}회 | "
                            f"세션손익 {self._session_realised_krw:+,.0f}원\n"
                            f"신규 매수를 중단합니다."
                        )
                    )
            if entry_price is None:
                entry_price = float(held.avg_price or 0.0)
            net_pnl_usd, net_pnl_krw, sell_commission_usd, sell_commission_krw = (
                self._estimate_overseas_net_pnl(
                    entry_price=float(entry_price or 0.0),
                    exit_price=sell_price,
                    qty=real_qty_sold,
                    fx_rate=fx_rate,
                )
            )
            self.repository.save_cycle_log(
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
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
                exit_by=exit_by,
                is_session_trade=1 if self._is_session_owned(candidate.symbol) else 0,
                consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
                hold_cycles=self._estimate_hold_cycles(candidate.symbol),
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
                cb_active=self._cb_active_flag(),
                pool_size=self._pool_size_for_market("overseas"),
                activity_score=candidate.activity_score,
            )
        self._persist_trade_state(
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
            "replacement_note": replacement_note,
            "response": response,
        }

    async def _record_virtual_overseas_sell(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        *,
        signal_snapshot: MovingAverageSnapshot | None = None,
        rejected_error: str | None = None,
        sell_qty_override: int | None = None,
    ) -> dict:
        strategy_flag, entry_by, exit_by = self._get_strategy_labels(candidate.symbol, signal_snapshot)
        entry_label, exit_label = self._build_sell_strategy_labels(
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            exit_reason=exit_reason,
        )
        tracker = self._get_position_tracker()
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

        lines = [
            "[KIS][VIRTUAL_TRADE]",
            f"시각={format_kst_korean(now)}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol} (virtual)",
            "구분=가상매도",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={sell_qty}주",
            f"매수전략={entry_label}",
            f"청산전략={exit_label}",
            f"수익률={format_pct(realized_pnl_pct)}",
        ]
        if closed_virtual_buy_qty > 0:
            lines.append(f"가상매수차감={closed_virtual_buy_qty}주")
        if pending_real_qty > 0:
            lines.append(f"실보유정산대기={pending_real_qty}주")
        if rejected_error:
            lines.append("참고=실매도거부를 가상체결로 전환")
        self._record_broker_order_event(
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
        self._queue_trade_notification(
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
        await self._flush_trade_notifications(force=self._trade_notification_force_immediate())
        self._reset_strategy_position(candidate.symbol)
        self._register_exit_cooldown("overseas", candidate.symbol, exit_reason)
        self._persist_trade_state(
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

    async def _load_overseas_signal(
        self,
        candidate: OverseasScanResult,
    ) -> MovingAverageSnapshot | None:
        try:
            daily_rows = await self.client.get_overseas_daily_prices(
                candidate.symbol,
                candidate.exchange_code,
                adjusted_price=True,
            )
            minute_rows = await self.client.get_overseas_minute_chart(
                candidate.symbol,
                candidate.exchange_code,
                interval_minutes=self.config.auto_trade.intraday_bar_minutes,
                include_previous_day=True,
                record_count=max(
                    self.config.auto_trade.intraday_slow_window + 8,
                    self.config.auto_trade.breakout_lookback_bars + 6,
                    self.config.auto_trade.bollinger_window + 4,
                    self.config.auto_trade.atr_window + 4,
                    40,
                ),
            )
        except KisApiError as exc:
            _logger.warning(
                "overseas_signal_load_failed symbol=%s exchange=%s error=%s",
                candidate.symbol,
                candidate.exchange_code,
                exc,
            )
            return None

        daily_series = extract_price_series(daily_rows, close_fields=("clos", "close", "last"))
        minute_series = extract_price_series(
            minute_rows,
            close_fields=("last", "clos", "close"),
            high_fields=("high",),
            low_fields=("low",),
            volume_fields=("evol", "volume"),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        if (
            len(daily_closes) < self.config.auto_trade.daily_slow_window
            or len(minute_closes) < self.config.auto_trade.intraday_slow_window
        ):
            return None

        return build_moving_average_snapshot(
            price=candidate.last_price,
            bid=candidate.bid,
            ask=candidate.ask,
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=self.config.auto_trade.daily_fast_window,
            daily_slow_window=self.config.auto_trade.daily_slow_window,
            intraday_fast_window=self.config.auto_trade.intraday_fast_window,
            intraday_slow_window=self.config.auto_trade.intraday_slow_window,
            volatility_window=self.config.auto_trade.volatility_window,
            momentum_window=self.config.auto_trade.momentum_window,
            volume_window=self.config.auto_trade.volume_window,
            rsi_period=self.config.auto_trade.rsi_period,
            breakout_lookback_bars=self.config.auto_trade.breakout_lookback_bars,
            bollinger_window=self.config.auto_trade.bollinger_window,
            bollinger_stddev=self.config.auto_trade.bollinger_stddev,
            atr_window=self.config.auto_trade.atr_window,
        )

    async def _load_domestic_signal(
        self,
        candidate: DomesticScanResult,
    ) -> MovingAverageSnapshot | None:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        target_date = now_kst.strftime("%Y%m%d")
        start_date = (now_kst - timedelta(days=200)).strftime("%Y%m%d")
        try:
            daily_rows = await self.client.get_daily_chart(
                stock_code=candidate.stock_code,
                start_date=start_date,
                end_date=target_date,
                market_code=self.config.trading.market_code,
            )
            minute_rows = await self.client.get_time_daily_chart(
                stock_code=candidate.stock_code,
                target_date=target_date,
                market_code=self.config.trading.market_code,
                include_previous="Y",
            )
        except KisApiError:
            return None

        daily_series = extract_price_series(
            daily_rows,
            close_fields=("stck_clpr", "stck_prpr"),
        )
        minute_series = extract_price_series(
            minute_rows,
            close_fields=("stck_prpr",),
            high_fields=("stck_hgpr",),
            low_fields=("stck_lwpr",),
            volume_fields=("cntg_vol",),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        if (
            len(daily_closes) < self.config.auto_trade.daily_slow_window
            or len(minute_closes) < self.config.auto_trade.intraday_slow_window
        ):
            return None

        return build_moving_average_snapshot(
            price=float(candidate.current_price),
            bid=float(candidate.best_bid),
            ask=float(candidate.best_ask),
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=self.config.auto_trade.daily_fast_window,
            daily_slow_window=self.config.auto_trade.daily_slow_window,
            intraday_fast_window=self.config.auto_trade.intraday_fast_window,
            intraday_slow_window=self.config.auto_trade.intraday_slow_window,
            volatility_window=self.config.auto_trade.volatility_window,
            momentum_window=self.config.auto_trade.momentum_window,
            volume_window=self.config.auto_trade.volume_window,
            rsi_period=self.config.auto_trade.rsi_period,
            breakout_lookback_bars=self.config.auto_trade.breakout_lookback_bars,
            bollinger_window=self.config.auto_trade.bollinger_window,
            bollinger_stddev=self.config.auto_trade.bollinger_stddev,
            atr_window=self.config.auto_trade.atr_window,
        )

    def _should_exit_overseas_position(
        self,
        snapshot: MovingAverageSnapshot,
        held: OverseasHeldPosition,
    ) -> tuple[bool, str]:
        return self._should_exit_position(
            snapshot,
            held.pnl_pct,
            symbol=held.symbol,
            take_profit_override=getattr(
                self.config.liquidity_lab,
                "overseas_take_profit_pct",
                None,
            ),
        )

    def _should_exit_position(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
        *,
        symbol: str = "",
        take_profit_override: float | None = None,
    ) -> tuple[bool, str]:
        exit_setup = self._build_exit_setup(
            snapshot,
            pnl_pct,
            1,
            symbol=symbol,
            take_profit_override=take_profit_override,
        )
        return exit_setup.action in {"sell", "sell_partial"}, exit_setup.reason

    @staticmethod
    def _is_mock_us_session_blocked_error(message: str) -> bool:
        return (
            "미국주식 주간거래는 제공하지 않습니다" in message
            or "KIS mock currently supports US order tests only during the US regular session" in message
            or "does not support US daytime trading" in message
        )

    @staticmethod
    def _is_mock_us_balance_missing_error(message: str) -> bool:
        return (
            "모의투자 잔고내역이 없습니다" in message
            or "mock balance not found" in message.lower()
        )

    async def _reconcile_pending_virtual_sells(
        self,
        *,
        overseas_positions: list[OverseasHeldPosition],
    ) -> None:
        pending_rows = self.repository.list_virtual_sell_pending(market="overseas")
        if not pending_rows:
            return

        tracker = self._get_position_tracker()
        real_by_symbol = {
            position.symbol.upper(): position
            for position in overseas_positions
            if not position.is_virtual
        }
        virtual_manager = getattr(self, "virtual_trades", None)

        for row in pending_rows:
            symbol = str(row["symbol"]).upper()
            pending_qty = int(row["qty"])
            pending_avg_price = float(row["avg_sell_price"])
            exchange_code = row.get("exchange_code")
            currency = str(row["currency"])

            real = real_by_symbol.get(symbol)
            virtual_buy = (
                None
                if virtual_manager is None
                else virtual_manager.get_position("overseas", symbol)
            )
            if real is None and virtual_buy is None:
                self.repository.delete_virtual_sell_pending("overseas", symbol)
                self._persist_trade_state(
                    market="overseas",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    action_bias="HOLD",
                    signal_state="HOLD",
                    note="orphan_virtual_sell_pending_cleared",
                    holding_qty=0,
                    last_price=pending_avg_price,
                    pnl_pct=None,
                    strategy_flag="",
                    entry_by="",
                    has_position=False,
                )
                self._save_event(
                    event_type="virtual_pending_cleanup",
                    market="overseas",
                    symbol=symbol,
                    detail={
                        "reason": "orphan_virtual_sell_pending",
                        "qty": pending_qty,
                        "avg_sell_price": pending_avg_price,
                    },
                )
                continue

            real_qty = 0 if real is None else real.quantity
            orderable_qty = 0 if real is None else real.orderable_qty
            settle_qty = min(pending_qty, orderable_qty)
            if settle_qty > 0 and real is not None:
                try:
                    await self.client.place_overseas_order_for_current_session(
                        side="sell",
                        symbol=symbol,
                        exchange_code=(exchange_code or real.exchange_code),
                        qty=settle_qty,
                        price=f"{real.current_price:.4f}",
                        order_division="00",
                    )
                except KisApiError:
                    continue

                realized_pnl = (pending_avg_price - real.avg_price) * settle_qty
                pnl_pct = (
                    (pending_avg_price - real.avg_price) / real.avg_price
                    if real.avg_price > 0
                    else 0.0
                )
                await self.notifier.send(
                    "\n".join(
                        [
                            "[KIS][VIRTUAL_SETTLED]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            f"시장={format_market_korean('overseas')}",
                            f"종목={symbol}",
                            "구분=정산매도",
                            f"수량={settle_qty}주",
                            f"가상매도가={format_usd(pending_avg_price)}",
                            f"매입가={format_usd(real.avg_price)}",
                            f"손익={format_usd(realized_pnl)}",
                            f"수익률={format_pct(pnl_pct)}",
                            "참고=거래불가 세션 중 가상매도 건이 실제 매도로 정산됨",
                        ]
                    )
                )
                real.quantity = max(0, real.quantity - settle_qty)
                real.orderable_qty = max(0, real.orderable_qty - settle_qty)

            remaining_pending_qty = max(0, pending_qty - settle_qty)
            if remaining_pending_qty <= 0:
                if tracker is not None:
                    tracker.settle(
                        market="overseas",
                        symbol=symbol,
                        real_qty_after_settlement=max(0, real_qty - settle_qty),
                    )
                else:
                    self.repository.delete_virtual_sell_pending("overseas", symbol)
            elif settle_qty > 0:
                self.repository.upsert_virtual_sell_pending(
                    market="overseas",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    qty=remaining_pending_qty,
                    avg_sell_price=pending_avg_price,
                    currency=currency,
                    updated_at=format_kst(datetime.now(timezone.utc)),
                )

    async def _send_summary(self, report: LiquidityLabReport) -> None:
        await self._flush_trade_notifications(force=False)
        action = self._build_action_summary(report)
        skip_count, skip_top_reasons = self._summarize_skipped_orders(report)
        if action["action_raw"] in {"WAIT", "VIRTUAL_BUY", "VIRTUAL_SELL"} and skip_count <= 0:
            return
        overseas_order = report.overseas_order or {}
        domestic_order = report.domestic_order or {}
        submitted_order = (
            overseas_order
            if overseas_order.get("submitted")
            else domestic_order
            if domestic_order.get("submitted")
            else None
        )
        # Some execution paths already send an immediate fill notification.
        # Real overseas buys do not, so do not blanket-suppress BUY/SELL here.
        # Instead, skip only the paths that explicitly mark themselves as already notified.
        if submitted_order and submitted_order.get("already_notified") and skip_count <= 0:
            return
        session_note = ""
        if report.primary_market == "overseas" and not report.us_orderable_in_profile:
            session_note = " (거래불가 세션)"
        primary_market_key = (
            report.primary_market
            if report.primary_market in {"domestic", "overseas"}
            else "overseas"
        )
        action_market_key = str(action.get("market_raw") or "").strip().lower()
        display_market_key = (
            action_market_key
            if action_market_key in {"domestic", "overseas"}
            else primary_market_key
        )
        display_target = str(action.get("symbol_label") or "").strip() or self._format_trade_symbol_label(
            primary_market_key, report.primary_target or "-"
        )
        if submitted_order and submitted_order.get("already_notified") and skip_count > 0:
            lines = [
                "[KIS][거래알림]",
                f"시각={self._format_report_time(report.scanned_at)}",
                f"시장={format_market_korean(display_market_key)}{session_note}",
                f"종목={display_target}",
                "동작=추가미실행",
                f"미실행={skip_count}건 ({skip_top_reasons})",
            ]
            await self.notifier.send("\n".join(lines))
            return
        lines = [
            "[KIS][거래알림]",
            f"시각={self._format_report_time(report.scanned_at)}",
            f"시장={format_market_korean(display_market_key)}{session_note}",
            f"종목={display_target}",
            f"동작={self._display_trade_action(action['action_raw'], action['action'], skip_count=skip_count)}",
            f"가격={action['price']}",
            f"수량={action['qty']}",
        ]
        if action["action_raw"] == "BUY":
            lines.append(f"전략={action.get('strategy_flag', '-')}")
            lines.append(f"주도={action.get('entry_by', '-')}")
        elif action["action_raw"] in {"SELL", "SELL_REJECTED", "VIRTUAL_SELL"}:
            entry_label, exit_label = self._build_sell_strategy_labels(
                strategy_flag=str(action.get("strategy_flag", "") or ""),
                entry_by=str(action.get("entry_by", "") or ""),
                exit_by=str(action.get("exit_by", "") or ""),
                exit_reason=(
                    str(
                        action.get("exit_reason")
                        or action.get("reason_raw")
                        or ""
                    )
                ),
            )
            lines.append(f"매수전략={entry_label}")
            lines.append(f"청산전략={exit_label}")
            if action.get("pnl_text", "-") != "-":
                lines.append(f"수익률={action['pnl_text']}")
            else:
                lines.append(f"사유={action['reason']}")
        else:
            lines.append(f"지표={action['indicator']}")
            lines.append(f"사유={action['reason']}")
        if skip_count > 0:
            lines.append(f"주문거부={skip_count}건 ({skip_top_reasons})")
        if action.get("replacement_note"):
            lines.append(f"참고={action['replacement_note']}")
        if action["action_raw"] == "SELL_REJECTED":
            lines.append("참고=주문이 거부되어 실제로 체결되지 않았습니다")
        await self.notifier.send("\n".join(lines))

    def _iter_leaf_orders(self, order_result: dict | None):
        if not order_result:
            return
        batched_orders = order_result.get("batched_orders")
        if isinstance(batched_orders, list) and batched_orders:
            for item in batched_orders:
                yield from self._iter_leaf_orders(item)
            return
        yield order_result

    def _summarize_skipped_orders(self, report: LiquidityLabReport) -> tuple[int, str]:
        reason_counts: dict[str, int] = {}
        ignored_reasons = {
            "",
            "no_action",
            "market_closed",
            "no_overseas_candidate",
            "krx_open_but_no_candidate",
            "us_open_but_no_candidate",
            "us_open_but_mock_session_not_supported",
        }
        for root in (report.domestic_order or {}, report.overseas_order or {}):
            for order in self._iter_leaf_orders(root):
                if order.get("submitted"):
                    continue
                if not order.get("skipped") and not order.get("error"):
                    continue
                reason = str(order.get("reason") or order.get("error") or "unknown")
                if reason in ignored_reasons:
                    continue
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if not reason_counts:
            return 0, "-"
        total = sum(reason_counts.values())
        top_reasons = ", ".join(
            format_reason_korean(reason)
            for reason, _count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
        )
        return total, top_reasons or "-"

    def _build_action_summary(self, report: LiquidityLabReport) -> dict[str, str]:
        overseas_order = self._select_representative_order(report.overseas_order)
        domestic_order = self._select_representative_order(report.domestic_order)
        if overseas_order and overseas_order.get("submitted"):
            return self._format_order_summary(overseas_order, currency="USD")
        if domestic_order and domestic_order.get("submitted"):
            return self._format_order_summary(domestic_order, currency="KRW")
        if report.primary_market == "overseas" and overseas_order and (
            overseas_order.get("skipped") or overseas_order.get("error")
        ):
            return self._format_order_summary(overseas_order, currency="USD")
        if report.primary_market == "domestic" and domestic_order and (
            domestic_order.get("skipped") or domestic_order.get("error")
        ):
            return self._format_order_summary(domestic_order, currency="KRW")
        return {
            "action_raw": "WAIT",
            "action": format_side_korean("WAIT"),
            "price": "-",
            "qty": "-",
            "indicator": "-",
            "reason": report.primary_selection_reason,
        }

    def _select_representative_order(self, order_result: dict | None) -> dict | None:
        if not order_result:
            return None
        leaves = list(self._iter_leaf_orders(order_result))
        if not leaves:
            return order_result
        for leaf in leaves:
            if leaf.get("submitted"):
                return leaf
        for leaf in leaves:
            if leaf.get("error") or leaf.get("skipped"):
                return leaf
        return leaves[0]

    def _format_order_summary(self, order: dict, *, currency: str) -> dict[str, str]:
        candidate = order.get("candidate") or {}
        held = order.get("held_position") or {}
        signal_snapshot = order.get("signal_snapshot") or {}
        market = str(order.get("market") or "").strip().lower()
        if market not in {"domestic", "overseas"}:
            market = "domestic" if candidate.get("stock_code") or held.get("stock_code") else "overseas"
        symbol = str(
            candidate.get("stock_code")
            or candidate.get("symbol")
            or held.get("stock_code")
            or held.get("symbol")
            or "-"
        ).strip().upper() or "-"
        side = str(order.get("side", "wait")).upper()
        if order.get("virtual") and side == "BUY":
            action = "VIRTUAL_BUY"
        elif order.get("virtual") and side == "SELL":
            action = "VIRTUAL_SELL"
        else:
            action = side if side not in {"HOLD", "WAIT"} else "WAIT"
        if order.get("skipped"):
            reason_raw = str(order.get("reason") or "")
            action = "WAIT"
            if side == "BUY" and str(order.get("reason")) == "dry_run_enabled":
                action = "BUY_SETUP"
            elif side == "SELL" and str(order.get("reason")) == "dry_run_enabled":
                action = "SELL_SETUP"
            elif side == "SELL" and reason_raw in {
                "session_not_orderable_in_profile",
                "order_rejected",
                "no_orderable_qty",
            }:
                action = "SELL_REJECTED"
        price_value = candidate.get("last_price") or candidate.get("current_price") or held.get("current_price")
        qty_value = order.get("qty") or held.get("quantity") or "-"

        indicator_parts: list[str] = []
        if signal_snapshot:
            snapshot = MovingAverageSnapshot(**signal_snapshot)
            if snapshot.rsi14 is not None:
                indicator_parts.append(f"RSI {snapshot.rsi14:.1f}")
            if snapshot.volume_ratio > 0:
                indicator_parts.append(f"거래량 {snapshot.volume_ratio:.1f}x")
            if snapshot.minute_ma_fast and snapshot.minute_ma_slow:
                relation = "상방" if snapshot.minute_ma_fast >= snapshot.minute_ma_slow else "하방"
                indicator_parts.append(f"분봉 {relation}")
        elif "pnl_pct" in held:
            indicator_parts.append(f"손익 {float(held['pnl_pct']) * 100:+.2f}%")
        elif "change_rate_pct" in candidate:
            indicator_parts.append(f"등락 {float(candidate['change_rate_pct']):+.2f}%")
        elif "minute_change_pct" in candidate:
            indicator_parts.append(f"등락 {float(candidate['minute_change_pct']) * 100:+.2f}%")

        if price_value in (None, "", "-"):
            price = "-"
        elif currency == "USD":
            price = f"${float(price_value):.4f}"
        else:
            price = f"{int(float(price_value)):,}원"
        pnl_text = "-"
        if side == "SELL" and "pnl_pct" in held:
            pnl_text = format_pct(float(held["pnl_pct"]))
        elif side == "SELL" and order.get("realized_pnl_pct") is not None:
            pnl_text = format_pct(float(order["realized_pnl_pct"]))

        return {
            "action_raw": action,
            "action": format_side_korean(action),
            "price": price,
            "qty": str(qty_value),
            "indicator": ", ".join(indicator_parts) if indicator_parts else "-",
            "pnl_text": pnl_text,
            "reason": format_reason_korean(
                str(
                    order.get("exit_reason")
                    or order.get("reason")
                    or order.get("error")
                    or "watching"
                )
            ),
            "reason_raw": str(
                order.get("exit_reason")
                or order.get("reason")
                or order.get("error")
                or "watching"
            ),
            "market_raw": market,
            "symbol_raw": symbol,
            "symbol_label": self._format_trade_symbol_label(market, symbol),
            "strategy_flag": str(order.get("strategy_flag") or "-"),
            "entry_by": str(order.get("entry_by") or "-"),
            "exit_by": str(order.get("exit_by") or "-"),
            "replacement_note": str(order.get("replacement_note") or ""),
        }

    def _get_strategy_manager(self, symbol: str) -> PriorityStrategyManager:
        key = symbol.strip().upper()
        managers = getattr(self, "_strategy_managers", None)
        if managers is None:
            managers = {}
            self._strategy_managers = managers
        manager = managers.get(key)
        if manager is None:
            manager = PriorityStrategyManager(getattr(self.config, "auto_trade", None))
            managers[key] = manager
        return manager

    def _decode_strategy_ids(
        self,
        strategy_flag: str,
        entry_by: str,
    ) -> frozenset[StrategyID]:
        reverse_map = {label: strategy_id for strategy_id, label in STRATEGY_LABEL.items()}
        labels = [token.strip() for token in strategy_flag.split("+") if token.strip()]
        if not labels and entry_by:
            labels = [entry_by]
        triggered = [reverse_map[label] for label in labels if label in reverse_map]
        return frozenset(triggered)

    def _get_session_owned_symbols(self) -> set[str]:
        owned = getattr(self, "_session_owned_symbols", None)
        if owned is None:
            owned = set()
            self._session_owned_symbols = owned
        return owned

    def _mark_session_owned(self, symbol: str) -> None:
        if symbol.strip():
            self._get_session_owned_symbols().add(symbol.strip().upper())

    def _is_session_owned(self, symbol: str) -> bool:
        return symbol.strip().upper() in self._get_session_owned_symbols()

    def _build_sell_strategy_labels(
        self,
        *,
        strategy_flag: str,
        entry_by: str,
        exit_by: str,
        exit_reason: str,
    ) -> tuple[str, str]:
        entry_label = strategy_flag or "-"
        if entry_by and entry_by != strategy_flag:
            entry_label += f" (주도:{entry_by})"

        exit_reason_korean = format_reason_korean(exit_reason) if exit_reason else ""
        exit_strategy_korean = format_reason_korean(exit_by) if exit_by else ""
        if exit_strategy_korean and exit_reason_korean and exit_by != exit_reason:
            exit_label = f"{exit_strategy_korean}·{exit_reason_korean}"
        elif exit_strategy_korean:
            exit_label = exit_strategy_korean
        else:
            exit_label = exit_reason_korean or "-"
        return entry_label, exit_label

    def _commit_strategy_entry(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot | None,
        *,
        strategy_flag: str,
        entry_by: str,
    ) -> None:
        if snapshot is None:
            return
        manager = self._get_strategy_manager(symbol)
        preview = manager.evaluate(symbol, snapshot, commit=False)
        triggered = preview.triggered_by
        if not triggered:
            triggered = self._decode_strategy_ids(strategy_flag, entry_by)
        if not triggered and entry_by:
            triggered = self._decode_strategy_ids("", entry_by)
        if triggered:
            manager.open_position(
                symbol=symbol.strip().upper(),
                entry_price=snapshot.price,
                triggered_by=triggered,
            )

    def _reset_strategy_position(self, symbol: str) -> None:
        manager = getattr(self, "_strategy_managers", {}).get(symbol.strip().upper())
        if manager is not None:
            manager.reset()

    def _get_strategy_labels(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot | None,
    ) -> tuple[str, str, str]:
        manager = getattr(self, "_strategy_managers", {}).get(symbol.strip().upper())
        if manager is None:
            if snapshot is None:
                return "", "", ""
            preview = self._get_strategy_manager(symbol).evaluate(symbol, snapshot, commit=False)
            return preview.flag, preview.entry_by, preview.exit_by

        if manager.position is None:
            if snapshot is None:
                return "", "", ""
            preview = manager.evaluate(symbol, snapshot, commit=False)
            return preview.flag, preview.entry_by, preview.exit_by

        flag = manager.position.flag
        entry_by = manager.position.entry_by
        exit_by = ""
        if snapshot is not None:
            preview = manager.evaluate(symbol, snapshot, commit=False)
            exit_by = preview.exit_by
        return flag, entry_by, exit_by

    def _estimate_hold_cycles(self, symbol: str) -> int:
        manager = getattr(self, "_strategy_managers", {}).get(symbol.strip().upper())
        if manager is None or manager.position is None:
            return 0
        loop_interval_sec = max(
            1,
            int(getattr(self.config.liquidity_lab, "loop_interval_sec", 25) or 25),
        )
        elapsed_sec = max(
            0.0,
            (datetime.now(timezone.utc) - ensure_timezone(manager.position.entry_time)).total_seconds(),
        )
        return max(0, int(elapsed_sec // loop_interval_sec))

    def _commission_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        return float(getattr(auto_trade, "commission_rate", 0.0025) or 0.0025)

    def _domestic_commission_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        legacy = float(getattr(auto_trade, "commission_rate", 0.0025) or 0.0025)
        return float(getattr(auto_trade, "domestic_commission_rate", 0.00015) or legacy)

    def _overseas_commission_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        legacy = float(getattr(auto_trade, "commission_rate", 0.0025) or 0.0025)
        return float(getattr(auto_trade, "overseas_commission_rate", legacy) or legacy)

    def _domestic_sell_tax_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        return float(getattr(auto_trade, "domestic_sell_tax_rate", 0.0) or 0.0)

    def _sec_fee_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        return float(getattr(auto_trade, "sec_fee_rate", 0.0000206) or 0.0)

    def _fx_fee_rate(self) -> float:
        auto_trade = getattr(self.config, "auto_trade", None)
        return float(getattr(auto_trade, "fx_fee_rate", 0.0) or 0.0)

    def _estimate_domestic_net_pnl_krw(
        self,
        *,
        entry_price: float,
        exit_price: float,
        qty: int,
    ) -> tuple[float, float]:
        gross = (exit_price - entry_price) * qty
        buy_fee = entry_price * qty * self._domestic_commission_rate()
        sell_fee = exit_price * qty * self._domestic_commission_rate()
        sell_tax = exit_price * qty * self._domestic_sell_tax_rate()
        return round(gross - buy_fee - sell_fee - sell_tax, 2), round(
            sell_fee + sell_tax,
            2,
        )

    def _estimate_overseas_net_pnl(
        self,
        *,
        entry_price: float,
        exit_price: float,
        qty: int,
        fx_rate: float,
    ) -> tuple[float, float, float, float]:
        gross_usd = (exit_price - entry_price) * qty
        buy_commission = entry_price * qty * self._overseas_commission_rate()
        sell_commission = exit_price * qty * self._overseas_commission_rate()
        sec_fee = exit_price * qty * self._sec_fee_rate()
        total_fee_usd = buy_commission + sell_commission + sec_fee
        fx_fee_krw = (
            (entry_price * qty + exit_price * qty) * fx_rate * self._fx_fee_rate()
        )
        net_usd = gross_usd - total_fee_usd
        net_krw = net_usd * fx_rate - fx_fee_krw
        return (
            round(net_usd, 6),
            round(net_krw, 2),
            round(sell_commission + sec_fee, 6),
            round((sell_commission + sec_fee) * fx_rate, 2),
        )

    @staticmethod
    def _is_profit_exit_reason(exit_reason: str) -> bool:
        return exit_reason in {
            "time_exit_profit",
            "marginal_profit_exit",
            "partial_profit_lock",
            "take_profit",
            "breakout_exhaustion_exit",
        }

    def _cb_active_flag(self) -> int:
        return int(
            getattr(self, "_halted_at", None) is not None
            or getattr(self, "_daily_halted_at", None) is not None
        )

    def _pool_size_for_market(self, market: str) -> int:
        market_key = market.strip().lower()
        if market_key == "domestic":
            return len(getattr(self, "_dynamic_domestic_codes", None) or [])
        if market_key == "overseas":
            return len(getattr(self, "_dynamic_overseas_pool", None) or [])
        return 0

    def _save_event(
        self,
        *,
        event_type: str,
        market: str = "",
        symbol: str = "",
        detail: dict | str = "",
        cycle_no: int | None = None,
    ) -> None:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "save_event"):
            return
        repository.save_event(
            event_type=event_type,
            market=market,
            symbol=symbol,
            detail=detail,
            cycle_no=getattr(self, "_cycle_count", 0) if cycle_no is None else cycle_no,
            session_id=getattr(self, "_session_id", ""),
        )

    def _cooldown_remaining_minutes(
        self,
        market: str,
        symbol: str,
    ) -> float:
        exit_cooldown = getattr(self, "_exit_cooldown", None) or {}
        cooldown_until = exit_cooldown.get(f"{market}:{symbol.strip().upper()}")
        if cooldown_until is None:
            return 0.0
        remaining = (
            ensure_timezone(cooldown_until) - datetime.now(timezone.utc)
        ).total_seconds() / 60
        return max(0.0, round(remaining, 2))

    def _get_entry_context(
        self,
        market: str,
        symbol: str,
        *,
        fallback_price: float | None = None,
    ) -> tuple[float | None, str | None, float | None]:
        manager = getattr(self, "_strategy_managers", {}).get(symbol.strip().upper())
        entry_price: float | None = None
        entry_time_iso: str | None = None
        hold_duration_min: float | None = None
        if manager is not None and manager.position is not None:
            entry_price = float(manager.position.entry_price)
            entry_dt = ensure_timezone(manager.position.entry_time)
            entry_time_iso = entry_dt.isoformat()
            hold_duration_min = round(
                max(
                    0.0,
                    (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60,
                ),
                2,
            )
        else:
            persisted = self._get_persisted_symbol_state(market, symbol)
            if persisted is not None:
                raw_entry_price = persisted.get("entry_price")
                if raw_entry_price is not None:
                    try:
                        entry_price = float(raw_entry_price)
                    except (TypeError, ValueError):
                        entry_price = None
                parsed = parse_datetime(str(persisted.get("updated_at", "") or ""))
                if parsed is not None:
                    entry_dt = ensure_timezone(parsed)
                    entry_time_iso = entry_dt.isoformat()
                    hold_duration_min = round(
                        max(
                            0.0,
                            (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60,
                        ),
                        2,
                    )
        if entry_price is None and fallback_price is not None and fallback_price > 0:
            entry_price = float(fallback_price)
        return entry_price, entry_time_iso, hold_duration_min

    def _defer_no_orderable_position(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
        orderable_qty: int,
    ) -> bool:
        retry_map = getattr(self, "_no_orderable_retry", None)
        if retry_map is None:
            retry_map = {}
            self._no_orderable_retry = retry_map
        key = f"{market}:{symbol.strip().upper()}"
        now = datetime.now(timezone.utc)
        retry_until = retry_map.get(key)
        if retry_until is not None and now <= ensure_timezone(retry_until):
            return True
        retry_map[key] = now + timedelta(minutes=5)
        self._save_event(
            event_type="trade_skip",
            market=market,
            symbol=symbol,
            detail={
                "reason": "no_orderable_qty",
                "holding_qty": holding_qty,
                "orderable_qty": orderable_qty,
                "note": "T+2 pending or API delay",
            },
        )
        return True

    def _track_no_orderable_stall(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
    ) -> int:
        counts = getattr(self, "_no_orderable_counts", None)
        if counts is None:
            counts = {}
            self._no_orderable_counts = counts
        key = f"{market}:{symbol.strip().upper()}"
        count = int(counts.get(key, 0) or 0) + 1
        counts[key] = count
        if count == 30:
            notifier = getattr(self, "notifier", None)
            if notifier is not None and getattr(notifier, "enabled", True):
                loop_interval_sec = max(
                    1,
                    int(getattr(self.config.liquidity_lab, "loop_interval_sec", 25) or 25),
                )
                duration_min = max(1, int((count * loop_interval_sec) // 60))
                asyncio.create_task(
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

    def _reset_no_orderable_stall(self, market: str, symbol: str) -> None:
        counts = getattr(self, "_no_orderable_counts", None)
        if not counts:
            return
        counts.pop(f"{market}:{symbol.strip().upper()}", None)

    def _is_no_orderable_retry_active(self, market: str, symbol: str) -> bool:
        retry_map = getattr(self, "_no_orderable_retry", None) or {}
        retry_until = retry_map.get(f"{market}:{symbol.strip().upper()}")
        if retry_until is None:
            return False
        return datetime.now(timezone.utc) <= ensure_timezone(retry_until)

    def _clear_no_orderable_retry(self, market: str, symbol: str) -> None:
        retry_map = getattr(self, "_no_orderable_retry", None)
        if not retry_map:
            return
        retry_map.pop(f"{market}:{symbol.strip().upper()}", None)

    def _record_trade_skip(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        reason: str,
        side: str,
        price: float | None = None,
        signal_snapshot: MovingAverageSnapshot | None = None,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        stock_name: str = "",
        activity_score: float | None = None,
        orderable_qty: int | None = None,
        holding_qty: int = 0,
        error: str | None = None,
        extra_detail: dict | None = None,
    ) -> None:
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        repository.save_cycle_log(
            logged_at=datetime.now(timezone.utc).isoformat(),
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            action_bias="SKIP",
            action_reason=f"{side}:{reason}",
            price=price,
            pnl_pct=None,
            holding_qty=holding_qty,
            rsi14=signal_snapshot.rsi14 if signal_snapshot else None,
            volume_ratio=signal_snapshot.volume_ratio if signal_snapshot else None,
            intraday_momentum=signal_snapshot.intraday_momentum if signal_snapshot else None,
            intraday_bar_return=signal_snapshot.intraday_bar_return if signal_snapshot else None,
            minute_ma_fast=signal_snapshot.minute_ma_fast if signal_snapshot else None,
            minute_ma_slow=signal_snapshot.minute_ma_slow if signal_snapshot else None,
            activity_score=activity_score,
            cycle_no=getattr(self, "_cycle_count", 0),
            session_id=getattr(self, "_session_id", ""),
            strategy_flag=strategy_flag,
            entry_by=entry_by,
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
            consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
            orderable_qty=orderable_qty,
            stock_name=stock_name,
            exit_cooldown_remaining=self._cooldown_remaining_minutes(market, symbol),
            cb_active=self._cb_active_flag(),
            pool_size=self._pool_size_for_market(market),
        )
        detail = {
            "reason": reason,
            "side": side,
            "rsi14": round(signal_snapshot.rsi14, 2)
            if signal_snapshot and signal_snapshot.rsi14 is not None
            else None,
            "volume_ratio": round(signal_snapshot.volume_ratio, 3)
            if signal_snapshot and signal_snapshot.volume_ratio is not None
            else None,
            "exit_cooldown_remaining": self._cooldown_remaining_minutes(market, symbol),
            "cb_active": self._cb_active_flag(),
        }
        if error:
            detail["error"] = error[:160]
        if extra_detail:
            detail.update(extra_detail)
        self._save_event(
            event_type="trade_skip",
            market=market,
            symbol=symbol,
            detail=detail,
        )

    def _register_exit_cooldown(
        self,
        market: str,
        symbol: str,
        exit_reason: str,
    ) -> None:
        if exit_reason in ("stop_loss", "atr_hard_stop"):
            cooldown_minutes = 25
        elif exit_reason in ("momentum_loss_cut", "trend_filter_lost"):
            cooldown_minutes = 12
        elif exit_reason == "marginal_profit_exit":
            cooldown_minutes = 15
        else:
            cooldown_minutes = 8
        self._set_exit_cooldown_minutes(market, symbol, cooldown_minutes)

    def _set_exit_cooldown_minutes(
        self,
        market: str,
        symbol: str,
        cooldown_minutes: int,
    ) -> None:
        exit_cooldown = getattr(self, "_exit_cooldown", None)
        if exit_cooldown is None:
            exit_cooldown = {}
            self._exit_cooldown = exit_cooldown
        exit_cooldown[f"{market}:{symbol.strip().upper()}"] = (
            datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
        )

    def _is_trading_halted(self) -> bool:
        kst_today = datetime.now(timezone.utc).astimezone(KST).date()
        if getattr(self, "_daily_loss_date", None) != kst_today:
            self._daily_loss_date = kst_today
            self._session_realised_krw = 0.0
            self._daily_halted_at = None
            _logger.info("[CB] KST 날짜 전환 → daily_loss 초기화 (date=%s)", kst_today)

        risk = getattr(self.config, "risk", None)
        if risk is None:
            return False

        consecutive_losses = int(getattr(self, "_consecutive_losses", 0) or 0)
        max_consecutive = int(getattr(risk, "max_consecutive_losses", 0) or 0)
        if max_consecutive > 0 and consecutive_losses >= max_consecutive:
            cooldown_minutes = int(
                getattr(risk, "circuit_breaker_cooldown_minutes", 0) or 0
            )
            consecutive_blocked = True
            if cooldown_minutes > 0:
                halted_at = getattr(self, "_halted_at", None)
                if halted_at is None:
                    self._halted_at = datetime.now(timezone.utc)
                    self._save_event(
                        event_type="cb_fired",
                        detail={
                            "consecutive_losses": consecutive_losses,
                            "type": "consecutive",
                        },
                    )
                else:
                    elapsed_minutes = (
                        datetime.now(timezone.utc) - ensure_timezone(halted_at)
                    ).total_seconds() / 60
                    if elapsed_minutes >= cooldown_minutes:
                        _logger.info(
                            "[CB] 서킷브레이커 자동 해제 (%.0f분 경과)",
                            elapsed_minutes,
                        )
                        self._consecutive_losses = 0
                        self._halted_at = None
                        self._save_event(
                            event_type="cb_released",
                            detail={
                                "elapsed_min": round(elapsed_minutes, 1),
                                "trigger": "auto_cooldown",
                                "type": "consecutive",
                            },
                        )
                        notifier = getattr(self, "notifier", None)
                        if notifier is not None and getattr(notifier, "enabled", True):
                            try:
                                loop = asyncio.get_running_loop()
                            except RuntimeError:
                                loop = None
                            if loop is not None:
                                loop.create_task(
                                    notifier.send(
                                        f"✅ 서킷브레이커 자동 해제\n"
                                        f"쿨다운 {cooldown_minutes}분 완료 → 매수 재개"
                                    )
                                )
                        consecutive_blocked = False
                    else:
                        return True
            elif cooldown_minutes <= 0:
                return True
            if consecutive_blocked:
                return True

        daily_limit = float(getattr(risk, "daily_loss_limit_pct", 0.0) or 0.0)
        session_realised_krw = float(getattr(self, "_session_realised_krw", 0.0) or 0.0)
        if daily_limit > 0 and session_realised_krw < 0:
            try:
                # operating_capital_krw: 실제 운용 자본 (risk config)
                # fallback: 500만원 (보수적 기본값)
                est_capital = float(
                    getattr(self.config.risk, "operating_capital_krw", 0) or 5_000_000
                )
                if est_capital > 0 and abs(session_realised_krw) / est_capital > daily_limit:
                    cooldown_min = int(
                        getattr(risk, "circuit_breaker_cooldown_minutes", 0) or 0
                    )
                    if cooldown_min > 0:
                        daily_halted_at = getattr(self, "_daily_halted_at", None)
                        if daily_halted_at is None:
                            self._daily_halted_at = datetime.now(timezone.utc)
                            self._save_event(
                                event_type="cb_fired",
                                detail={
                                    "daily_loss_limit_pct": daily_limit,
                                    "session_realised_krw": round(session_realised_krw, 2),
                                    "type": "daily_limit",
                                },
                            )
                        else:
                            elapsed = (
                                datetime.now(timezone.utc) - ensure_timezone(daily_halted_at)
                            ).total_seconds() / 60
                            if elapsed >= cooldown_min:
                                _logger.info("[CB] daily_limit 자동 해제 (%.0f분 경과)", elapsed)
                                self._session_realised_krw = 0.0
                                self._daily_halted_at = None
                                self._save_event(
                                    event_type="cb_released",
                                    detail={
                                        "elapsed_min": round(elapsed, 1),
                                        "trigger": "auto_cooldown",
                                        "type": "daily_limit",
                                    },
                                )
                                notifier = getattr(self, "notifier", None)
                                if notifier is not None and getattr(notifier, "enabled", True):
                                    try:
                                        loop = asyncio.get_running_loop()
                                    except RuntimeError:
                                        loop = None
                                    if loop is not None:
                                        loop.create_task(
                                            notifier.send(
                                                f"✅ 일일손실한도 CB 자동 해제\n"
                                                f"쿨다운 {cooldown_min}분 완료 → 매수 재개"
                                            )
                                        )
                                return False
                    return True
            except Exception:
                pass

        return False

    def _ma_relation_summary(self, snapshot: MovingAverageSnapshot) -> str:
        auto = self.config.auto_trade
        if not snapshot.has_required_context:
            return "-"
        daily_relation = ">" if (snapshot.daily_ma_fast or 0) >= (snapshot.daily_ma_slow or 0) else "<"
        minute_relation = ">" if (snapshot.minute_ma_fast or 0) >= (snapshot.minute_ma_slow or 0) else "<"
        return (
            f"{auto.daily_fast_window}d{daily_relation}{auto.daily_slow_window}d "
            f"{auto.intraday_fast_window}{minute_relation}{auto.intraday_slow_window}"
        )

    def _estimate_api_calls_per_cycle(
        self,
        *,
        krx_open: bool,
        us_open: bool,
        domestic_watch_count: int | None = None,
        overseas_watch_count: int | None = None,
        include_domestic_order: bool | None = None,
        include_domestic_paper: bool | None = None,
        include_overseas_order: bool,
    ) -> int:
        if include_domestic_order is None:
            include_domestic_order = bool(include_domestic_paper)
        estimated_calls = 0
        config = self.config.liquidity_lab
        if krx_open:
            active_domestic_codes = (
                list(getattr(self, "_dynamic_domestic_codes", None))
                if getattr(self, "_dynamic_domestic_codes", None)
                else list(config.domestic_candidates)
            )
            domestic_candidates = len(active_domestic_codes)
            refine_n = min(
                domestic_candidates,
                max(config.unified_watch_top_n, 3),
            )
            estimated_calls += domestic_candidates * 2
            estimated_calls += refine_n
            estimated_calls += 1
            if include_domestic_order:
                estimated_calls += 1
        if us_open:
            active_overseas_candidates = self._active_overseas_pool()
            n_candidates = len(active_overseas_candidates)
            top_n = max(config.unified_scan_top_n, 1)
            estimated_calls += n_candidates
            estimated_calls += min(top_n, n_candidates) * 2
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in active_overseas_candidates
            }
            estimated_calls += len(exchange_codes)
        if krx_open and us_open:
            estimated_calls += min(
                len(
                    list(getattr(self, "_dynamic_domestic_codes", None))
                    if getattr(self, "_dynamic_domestic_codes", None)
                    else list(config.domestic_candidates)
                ),
                config.unified_watch_top_n,
            )
        if include_overseas_order:
            estimated_calls += 1
        return estimated_calls

    @staticmethod
    def _format_report_time(value: str) -> str:
        if not value:
            return "-"
        parts = value.split()
        if len(parts) >= 2:
            date_part = parts[0]
            time_part = parts[1]
            try:
                year, month, day = [int(chunk) for chunk in date_part.split("-")]
                hour, minute, _ = [int(chunk) for chunk in time_part.split(":")]
            except ValueError:
                return value
            return f"{month}월 {day}일 {hour:02d}:{minute:02d}"
        return value

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)

    def _build_exit_setup(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
        position_qty: int,
        *,
        symbol: str = "",
        take_profit_override: float | None = None,
    ):
        config = self.config.auto_trade
        if take_profit_override is not None:
            config = dataclasses.replace(config, take_profit_pct=take_profit_override)
        return evaluate_exit_setup(
            config,
            snapshot,
            pnl_pct,
            drawdown_from_peak=0.0,
            hold_cycles=self._estimate_hold_cycles(symbol) if symbol else 0,
            position_qty=position_qty,
            partial_exit_done=False,
        )
