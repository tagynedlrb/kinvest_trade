from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .client import KisApiError, KisRestClient, parse_kis_number
from .config import AppConfig, OverseasCandidateConfig
from .market_sessions import (
    KST,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    is_us_regular_session,
)
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
from .paper import PaperTradingService, PaperRunState
from .repository import SqliteRepository
from .strategy import PriorityStrategyManager, STRATEGY_LABEL, StrategyID
from .technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    extract_price_series,
)
from .time_utils import format_kst, format_kst_korean

_logger = logging.getLogger(__name__)


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
    paper_run: dict | None
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
            "paper_run": self.paper_run,
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
        self._cycle_count: int = 0
        self._session_id: str = uuid.uuid4().hex[:12]
        self._strategy_managers: dict[str, PriorityStrategyManager] = {}

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
        return max(
            self._parse_float(possible.get("cash_available")),
            self._parse_float(raw.get("ord_psbl_frcr_amt_wcrc")),
            self._parse_float(raw.get("frcr_ord_psbl_amt1")),
            self._parse_float(raw.get("frcr_dncl_amt_2")),
        )

    async def _get_domestic_available_krw(self) -> float:
        try:
            balance = await self.client.get_balance()
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
        except KisApiError as exc:
            _logger.warning("domestic_balance_fetch_failed error=%s", exc)
            return 0.0

    def _slot_based_qty(self, *, available_amount: float, price: float) -> int:
        config = self.config.liquidity_lab
        if available_amount <= 0 or price <= 0:
            return 0
        slot_max_pct = max(float(config.slot_max_pct), 0.0)
        slot_entry_pct = max(float(config.slot_entry_pct), 0.0)
        if slot_max_pct <= 0 or slot_entry_pct <= 0:
            return 0
        budget = available_amount * min(slot_entry_pct, slot_max_pct)
        return max(int(math.floor(budget / price)), 0)

    async def run(self) -> LiquidityLabReport:
        now = datetime.now(timezone.utc)
        self._cycle_count = getattr(self, "_cycle_count", 0) + 1
        krx_open = is_krx_regular_session(now)
        us_open = is_us_regular_session(now)
        us_session = get_us_trading_session(now)
        us_orderable_in_profile = is_us_orderable_session_for_env(
            now,
            self.config.credentials.env,
        )

        if not krx_open and not us_open:
            return LiquidityLabReport(
                scanned_at=format_kst(now) or "",
                krx_market_open=False,
                us_market_open=False,
                us_market_session=us_session,
                us_orderable_in_profile=False,
                primary_market="none",
                primary_target=None,
                primary_selection_reason="no_supported_market_open",
                domestic_ranked=[],
                overseas_ranked=[],
                domestic_excluded=[],
                overseas_excluded=[],
                domestic_positions=[],
                overseas_positions=[],
                watch_targets=[],
                estimated_api_calls_per_cycle=0,
                paper_run={"skipped": True, "reason": "market_closed"},
                domestic_order={"skipped": True, "reason": "market_closed"},
                overseas_order={"skipped": True, "reason": "market_closed"},
            )

        domestic_ranked = await self.scan_domestic() if krx_open else []
        domestic_positions = (
            await self._load_domestic_positions(domestic_ranked)
            if krx_open
            else []
        )
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
        else:
            overseas_ranked = []
            overseas_positions = []
            monitored_overseas_positions = []
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
        overseas_exit_target = (
            await self._select_overseas_exit_target(overseas_ranked, monitored_overseas_positions)
            if us_open
            else None
        )
        domestic_exit_target = (
            self._select_domestic_exit_target(
                domestic_ranked,
                domestic_watch_targets,
                domestic_positions,
            )
            if krx_open
            else None
        )
        config_ll = self.config.liquidity_lab
        domestic_buy_targets = self._select_domestic_buy_targets(
            domestic_ranked,
            domestic_watch_targets,
            max_concurrent=getattr(config_ll, "max_concurrent_domestic_orders", 2),
        )
        domestic_buy_target = domestic_buy_targets[0] if domestic_buy_targets else None
        overseas_buy_targets = self._select_overseas_buy_targets(
            overseas_ranked,
            overseas_watch_targets,
            max_concurrent=getattr(config_ll, "max_concurrent_overseas_orders", 3),
        )
        overseas_buy_target = overseas_buy_targets[0] if overseas_buy_targets else None
        paper_summary = {"skipped": True, "reason": "paper_test_removed_for_speed"}
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

        if overseas_exit_target is not None:
            exit_candidate, exit_position, exit_reason, exit_signal = overseas_exit_target
            overseas_order = await self._place_overseas_sell_order(
                exit_candidate,
                exit_position,
                exit_reason,
                signal_snapshot=exit_signal,
            )
            overseas_orders = [overseas_order]
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
            paper_run=paper_summary,
            domestic_order=domestic_order,
            overseas_order=overseas_order,
        )
        await self._send_summary(report)
        return report

    async def scan_domestic(self) -> list[DomesticScanResult]:
        config = self.config.liquidity_lab
        quote_results: list[DomesticScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for stock_code in config.domestic_candidates:
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

        quote_results.sort(key=lambda item: item.activity_score, reverse=True)
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
        return sorted(refined + remaining, key=lambda item: item.activity_score, reverse=True)

    async def _scan_single_domestic_quote(self, stock_code: str) -> DomesticScanResult:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        intraday_turnover = int(current.get("turnover_krw", 0) or 0)
        spread_pct = float(orderbook.get("spread_pct", 0.0) or 0.0)
        liquidity_score = math.log10(max(intraday_turnover, 1)) * 8.0
        spread_penalty = spread_pct * 3000.0
        turnover_surge_bonus = 0.0
        if intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 3:
            turnover_surge_bonus = 4.0
        elif intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 1.5:
            turnover_surge_bonus = 2.0

        activity_score = liquidity_score + turnover_surge_bonus - spread_penalty
        return DomesticScanResult(
            stock_code=stock_code,
            current_price=int(current["current_price"]),
            best_ask=int(orderbook["best_ask"]),
            best_bid=int(orderbook["best_bid"]),
            spread_pct=spread_pct,
            minute_change_pct=0.0,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=0,
            activity_score=round(activity_score, 4),
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

        for candidate in config.overseas_candidates:
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
            self._signal_cache.clear()
            return [], set()

        quote_results.sort(key=lambda item: item.activity_score, reverse=True)
        held_symbols = await self._get_held_symbols()
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
            self._signal_cache[symbol] = await self._load_overseas_signal(result)
            await asyncio.sleep(0.05)

        for symbol in list(self._signal_cache.keys()):
            if symbol not in signal_symbols:
                del self._signal_cache[symbol]

        return quote_results, held_symbols

    async def _get_held_symbols(self) -> set[str]:
        """
        Return overseas symbols currently held.

        On API failure, fall back to the previous cycle cache so exit scans still include
        existing positions.
        """
        try:
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in self.config.liquidity_lab.overseas_candidates
            }
            held: set[str] = set(self._get_virtual_held_symbols())
            for exchange_code in sorted(exchange_codes):
                balance = await self.client.get_overseas_balance(
                    exchange_code=exchange_code,
                    currency_code="USD",
                )
                for row in balance.get("positions", []):
                    qty = int(float(str(row.get("ovrs_cblc_qty", 0) or 0)))
                    if qty <= 0:
                        continue
                    symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                    if symbol:
                        held.add(symbol)
            self._last_held_symbols = held
            return held
        except Exception:
            return self._last_held_symbols or self._get_virtual_held_symbols()

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
        momentum_score = change_rate * 2.5
        spread_penalty = spread_pct * 2500.0
        volume_surge_bonus = 0.0
        if volume >= self.config.liquidity_lab.overseas_min_volume * 5:
            volume_surge_bonus = 3.0
        elif volume >= self.config.liquidity_lab.overseas_min_volume * 2:
            volume_surge_bonus = 1.5
        tight_spread_bonus = 1.0 if spread_pct < 0.001 else 0.0
        activity_score = (
            liquidity_score
            + momentum_score
            + volume_surge_bonus
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
        if not overseas_ranked:
            return []

        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        candidate_symbols = set(quote_map.keys())
        exchange_codes = {item.exchange_code.upper() for item in overseas_ranked}
        positions_by_key: dict[tuple[str, str], OverseasHeldPosition] = {}

        for exchange_code in sorted(exchange_codes):
            try:
                balance = await self.client.get_overseas_balance(
                    exchange_code=exchange_code,
                    currency_code="USD",
                )
            except Exception:
                continue

            for row in balance.get("positions", []):
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                if symbol not in candidate_symbols:
                    continue
                row_exchange_code = str(row.get("ovrs_excg_cd", "")).strip().upper() or exchange_code
                quantity = parse_kis_number(row.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue
                orderable_qty = parse_kis_number(row.get("ord_psbl_qty"))
                avg_price = self._parse_float(row.get("pchs_avg_pric"))
                quote = quote_map.get(symbol)
                if quote is None or avg_price <= 0:
                    continue
                current_price = quote.last_price
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
        if not overseas_ranked or manager is None:
            return []

        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        positions: list[OverseasHeldPosition] = []
        for position in manager.list_positions("overseas"):
            quote = quote_map.get(position.symbol.upper())
            if quote is None or position.qty <= 0:
                continue
            pnl_pct = (
                (quote.last_price - position.avg_price) / position.avg_price
                if position.avg_price > 0
                else 0.0
            )
            positions.append(
                OverseasHeldPosition(
                    symbol=position.symbol.upper(),
                    exchange_code=(position.exchange_code or quote.exchange_code).upper(),
                    quantity=position.qty,
                    orderable_qty=position.qty,
                    avg_price=position.avg_price,
                    current_price=quote.last_price,
                    pnl_pct=pnl_pct,
                    is_virtual=True,
                )
            )
        return positions

    async def _load_domestic_positions(
        self,
        domestic_ranked: list[DomesticScanResult],
    ) -> list[DomesticHeldPosition]:
        if not domestic_ranked:
            return []

        quote_map = {item.stock_code: item for item in domestic_ranked}
        try:
            balance = await self.client.get_balance()
        except Exception:
            return []

        positions: list[DomesticHeldPosition] = []
        rows = balance.get("positions", []) or balance.get("output1", [])
        for row in rows:
            qty = int(float(str(row.get("hldg_qty", 0) or 0)))
            if qty <= 0:
                continue
            stock_code = str(row.get("pdno", "")).strip()
            if not stock_code:
                continue
            avg_price = self._parse_float(row.get("pchs_avg_pric"))
            orderable_qty = int(float(str(row.get("ord_psbl_qty", qty) or qty)))
            quote = quote_map.get(stock_code)
            current_price = quote.current_price if quote is not None else avg_price
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

    async def _select_overseas_exit_target(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> tuple[OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot | None] | None:
        if not overseas_ranked or not held_positions:
            return None

        config = self.config.liquidity_lab
        tracker = self._get_position_tracker()
        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        real_by_symbol: dict[str, OverseasHeldPosition] = {}
        for held in held_positions:
            if held.is_virtual:
                continue
            real_by_symbol[held.symbol.upper()] = held

        symbols_to_check: set[str] = set(real_by_symbol.keys())
        virtual_manager = getattr(self, "virtual_trades", None)
        if virtual_manager is not None:
            for position in virtual_manager.list_positions("overseas"):
                symbols_to_check.add(position.symbol.upper())

        selected: tuple[
            tuple[int, float],
            OverseasScanResult,
            OverseasHeldPosition,
            str,
            MovingAverageSnapshot | None,
        ] | None = None

        for symbol in symbols_to_check:
            quote = quote_map.get(symbol)
            if quote is None:
                continue

            real = real_by_symbol.get(symbol)
            real_qty = 0 if real is None else real.quantity
            unified = (
                tracker.get_unified(
                    market="overseas",
                    symbol=symbol,
                    real_qty=real_qty,
                    currency="USD",
                    exchange_code=quote.exchange_code,
                )
                if tracker is not None
                else UnifiedPosition(
                    market="overseas",
                    symbol=symbol,
                    exchange_code=quote.exchange_code,
                    real_qty=real_qty,
                    virtual_buy_qty=0,
                    virtual_sell_qty=0,
                    currency="USD",
                )
            )
            if unified.total_qty <= 0:
                continue
            pending = None if tracker is None else tracker.get_pending_settlement("overseas", symbol)
            already_pending_qty = 0 if pending is None else pending[0]

            virtual_buy = None if virtual_manager is None else virtual_manager.get_position("overseas", symbol)
            avg_price = 0.0
            pnl_pct = 0.0
            remaining_real_orderable = 0
            if real is not None and real.avg_price > 0:
                avg_price = real.avg_price
                pnl_pct = real.pnl_pct
                remaining_real_orderable = max(0, real.orderable_qty - already_pending_qty)
            elif virtual_buy is not None and virtual_buy.avg_price > 0:
                avg_price = virtual_buy.avg_price
                pnl_pct = (
                    (quote.last_price - avg_price) / avg_price
                    if avg_price > 0
                    else 0.0
                )

            remaining_total = unified.total_qty
            if remaining_total <= 0:
                continue
            if remaining_real_orderable <= 0 and unified.virtual_buy_qty <= 0:
                continue
            if avg_price <= 0:
                continue

            exit_reason: str | None = None
            priority: tuple[int, float] | None = None
            if pnl_pct <= -config.overseas_stop_loss_pct:
                exit_reason = "stop_loss"
                priority = (0, pnl_pct)
            elif pnl_pct >= config.overseas_take_profit_pct:
                exit_reason = "take_profit"
                priority = (1, -pnl_pct)

            if exit_reason is None or priority is None:
                continue

            held_for_return = (
                OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=real.exchange_code,
                    quantity=real.quantity,
                    orderable_qty=remaining_real_orderable,
                    avg_price=real.avg_price,
                    current_price=quote.last_price,
                    pnl_pct=pnl_pct,
                    is_virtual=False,
                )
                if real is not None
                else OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=quote.exchange_code,
                    quantity=remaining_total,
                    orderable_qty=remaining_total,
                    avg_price=avg_price,
                    current_price=quote.last_price,
                    pnl_pct=pnl_pct,
                    is_virtual=True,
                )
            )
            if selected is None or priority < selected[0]:
                selected = (priority, quote, held_for_return, exit_reason, None)

        if selected is not None:
            _, quote, held, exit_reason, signal_snapshot = selected
            return quote, held, exit_reason, signal_snapshot

        signal_candidates: list[tuple[float, OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot]] = []
        for symbol, held in real_by_symbol.items():
            quote = quote_map.get(symbol)
            if quote is None:
                continue
            pending = None if tracker is None else tracker.get_pending_settlement("overseas", symbol)
            already_pending_qty = 0 if pending is None else pending[0]
            remaining_real_orderable = max(0, held.orderable_qty - already_pending_qty)
            if remaining_real_orderable <= 0:
                continue
            signal_snapshot = self._signal_cache.get(held.symbol.upper())
            if signal_snapshot is None:
                continue
            should_exit, exit_reason = self._should_exit_overseas_position(signal_snapshot, held)
            if not should_exit:
                continue
            signal_candidates.append(
                (
                    held.pnl_pct,
                    quote,
                    OverseasHeldPosition(
                        symbol=held.symbol,
                        exchange_code=held.exchange_code,
                        quantity=held.quantity,
                        orderable_qty=remaining_real_orderable,
                        avg_price=held.avg_price,
                        current_price=held.current_price,
                        pnl_pct=held.pnl_pct,
                        is_virtual=False,
                    ),
                    exit_reason,
                    signal_snapshot,
                )
            )

        if not signal_candidates:
            return None

        signal_candidates.sort(key=lambda item: item[0])
        _, quote, held, exit_reason, signal_snapshot = signal_candidates[0]
        return quote, held, exit_reason, signal_snapshot

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

        unified.sort(key=lambda item: item.activity_score, reverse=True)

        held_domestic_codes = {position.stock_code for position in domestic_positions}
        held_overseas_codes = {
            position.symbol.upper()
            for position in overseas_positions
            if not position.is_virtual
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
            )

        existing_flag, existing_entry_by, _ = self._get_strategy_labels(code, signal_snapshot)
        if held_position is not None:
            exit_setup = self._build_exit_setup(signal_snapshot, held_position.pnl_pct, holding_qty)
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
                note=f"[{strategy_result.flag}] {entry_setup.reason}",
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
            action_bias="WAIT",
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
        )

    def _select_domestic_buy_target(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
    ) -> DomesticScanResult | None:
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic" and watch_target.action_bias == "BUY"
        ]
        if not ready_targets:
            return None
        best_target = max(
            ready_targets,
            key=lambda item: (item.signal_score, item.activity_score),
        )
        return candidate_map.get(best_target.code)

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
            if watch_target.market == "domestic" and watch_target.action_bias == "SELL"
        ]
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

    def _select_overseas_buy_target(
        self,
        overseas_ranked: list[OverseasScanResult],
        watch_targets: list[WatchTargetStatus],
    ) -> OverseasScanResult | None:
        candidate_map = {candidate.symbol.upper(): candidate for candidate in overseas_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "overseas" and watch_target.action_bias == "BUY"
        ]
        if not ready_targets:
            return None
        best_target = max(
            ready_targets,
            key=lambda item: (item.signal_score, item.activity_score),
        )
        return candidate_map.get(best_target.code.upper())

    def _select_overseas_buy_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        watch_targets: list[WatchTargetStatus],
        max_concurrent: int = 3,
    ) -> list[OverseasScanResult]:
        candidate_map = {candidate.symbol.upper(): candidate for candidate in overseas_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "overseas" and watch_target.action_bias == "BUY"
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

    async def _run_domestic_paper_test(self, watchlist: list[str]) -> PaperRunState:
        service = PaperTradingService(self.config, self.client, self.repository, self.notifier)
        return await service.run(
            iterations=self.config.liquidity_lab.domestic_paper_iterations,
            interval_sec=self.config.liquidity_lab.domestic_paper_interval_sec,
            watchlist_override=watchlist,
        )

    async def _place_domestic_test_order(
        self,
        candidate: DomesticScanResult,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        signal_snapshot = None if watch_target is None else watch_target.signal_snapshot
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
            return {
                "submitted": False,
                "skipped": True,
                "market": "domestic",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "order_rejected",
                "error": str(exc),
            }
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][LIQUIDITY_LAB]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"시장={format_market_korean('domestic')}",
                    f"종목={candidate.stock_code}",
                    f"동작={format_side_korean('buy')}",
                    f"가격={int(candidate.best_ask or candidate.current_price):,}원",
                    f"수량={qty}주",
                    f"전략={strategy_flag or '-'}",
                    f"주도={entry_by or '-'}",
                ]
            )
        )
        self._commit_strategy_entry(
            candidate.stock_code,
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        repository = getattr(self, "repository", None)
        if repository is not None:
            repository.save_cycle_log(
                logged_at=datetime.now(timezone.utc).isoformat(),
                market="domestic",
                symbol=candidate.stock_code,
                exchange_code=None,
                action_bias="BUY_REAL",
                action_reason="domestic_buy",
                price=float(candidate.current_price),
                pnl_pct=0.0,
                realized_pnl_usd=None,
                realized_pnl_krw=0.0,
                holding_qty=qty,
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
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
        try:
            sell_qty = min(held.quantity, max(held.orderable_qty, 0))
            response = await self.client.place_cash_order(
                side="sell",
                stock_code=candidate.stock_code,
                qty=sell_qty,
                price=int(sell_price),
                order_division="00",
            )
        except KisApiError as exc:
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
                "error": str(exc),
            }

        lines = [
            "[KIS][LAB_SELL]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"시장={format_market_korean('domestic')}",
            f"종목={candidate.stock_code}",
            "구분=매도",
            f"가격={format_krw(sell_price)}",
            f"수량={sell_qty}주",
            f"진입전략={strategy_flag or '-'}",
            f"청산트리거={exit_by or format_reason_korean(exit_reason)}",
        ]
        if held.avg_price > 0:
            gross_pnl = (sell_price - held.avg_price) * sell_qty
            pnl_pct = (sell_price - held.avg_price) / held.avg_price
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("수익률=알수없음")
        await self.notifier.send("\n".join(lines))
        self._reset_strategy_position(candidate.stock_code)
        if held.avg_price > 0:
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
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
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
            return {
                "skipped": True,
                "market": "overseas",
                "side": "wait",
                "candidate": asdict(candidate),
                "reason": "signal_snapshot_unavailable",
            }

        should_buy, buy_reason = self._should_buy_overseas_candidate(
            signal_snapshot,
            symbol=candidate.symbol,
        )
        strategy_flag = ""
        entry_by = ""
        if watch_target is not None:
            strategy_flag = watch_target.strategy_flag
            entry_by = watch_target.entry_by
        if not strategy_flag or not entry_by:
            strategy_flag, entry_by, _ = self._get_strategy_labels(candidate.symbol, signal_snapshot)
        if not should_buy:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "wait",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
                "reason": buy_reason,
            }

        config = self.config.liquidity_lab
        qty = config.overseas_test_order_qty
        if config.use_slot_sizing:
            try:
                available_usd = await self._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                )
            except KisApiError:
                available_usd = 0.0
            slot_qty = self._slot_based_qty(
                available_amount=available_usd,
                price=candidate.last_price,
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_usd > 0:
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
        try:
            response = await self.client.place_overseas_order_for_current_session(
                side="buy",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=qty,
                price=f"{candidate.last_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            if self._is_mock_us_session_blocked_error(str(exc)):
                return await self._record_virtual_overseas_buy(
                    candidate,
                    signal_snapshot=signal_snapshot,
                    rejected_error=str(exc),
                )
            return {
                "submitted": False,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
                "qty": qty,
                "error": str(exc),
            }
        repository = getattr(self, "repository", None)
        if repository is not None:
            repository.save_cycle_log(
                logged_at=datetime.now(timezone.utc).isoformat(),
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                action_bias="BUY_REAL",
                action_reason=buy_reason,
                price=candidate.last_price,
                pnl_pct=0.0,
                realized_pnl_usd=0.0,
                realized_pnl_krw=0.0,
                holding_qty=qty,
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
            )
        self._commit_strategy_entry(
            candidate.symbol,
            signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )
        return {
            "submitted": True,
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
        if config.use_slot_sizing:
            try:
                available_usd = await self._get_overseas_available_usd(
                    symbol=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                )
            except KisApiError:
                available_usd = 0.0
            slot_qty = self._slot_based_qty(
                available_amount=available_usd,
                price=candidate.last_price,
            )
            if slot_qty > 0:
                qty = slot_qty
            elif available_usd > 0:
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
        snapshot = signal_snapshot or self._signal_cache.get(candidate.symbol.upper())
        strategy_flag = "" if watch_target is None else watch_target.strategy_flag
        entry_by = "" if watch_target is None else watch_target.entry_by
        if snapshot is not None and (not strategy_flag or not entry_by):
            strategy_flag, entry_by, _ = self._get_strategy_labels(candidate.symbol, snapshot)
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
            "구분=매수 (virtual)",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={qty}주",
            f"전략={strategy_flag or '-'}",
            f"주도={entry_by or '-'}",
        ]
        if rejected_error:
            lines.append(f"참고={rejected_error}")
        await self.notifier.send("\n".join(lines))
        self._commit_strategy_entry(
            candidate.symbol,
            snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
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

        try:
            response = await self.client.place_overseas_order_for_current_session(
                side="sell",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=real_sell_qty,
                price=f"{candidate.last_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            is_session_blocked = self._is_mock_us_session_blocked_error(str(exc))
            if is_session_blocked and is_us_regular_session(datetime.now(timezone.utc)):
                return await self._record_virtual_overseas_sell(
                    candidate,
                    held,
                    exit_reason,
                    signal_snapshot=signal_snapshot,
                    rejected_error=str(exc),
                    sell_qty_override=target_sell_qty,
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
                "reason": "session_not_orderable_in_profile" if is_session_blocked else "order_rejected",
                "error": str(exc),
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
                price=candidate.last_price,
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
            f"가격={format_usd(candidate.last_price)}",
            f"수량={int(sell_result.get('qty_from_real', real_sell_qty) or real_sell_qty)}주",
            f"진입전략={strategy_flag or '-'}",
            f"청산트리거={exit_by or format_reason_korean(exit_reason)}",
        ]
        virtual_closed_qty = int(sell_result.get("qty_from_virtual_buy", 0) or 0)
        if virtual_closed_qty > 0:
            lines.append(f"참고=가상매수 {virtual_closed_qty}주 우선 차감")
        if held.avg_price > 0:
            gross_pnl = (candidate.last_price - held.avg_price) * int(
                sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty
            )
            pnl_pct = (candidate.last_price - held.avg_price) / held.avg_price
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("수익률=알수없음")
        await self.notifier.send("\n".join(lines))
        self._reset_strategy_position(candidate.symbol)
        if held.avg_price > 0:
            real_qty_sold = int(sell_result.get("qty_from_real", real_sell_qty) or real_sell_qty)
            auto_trade_cfg = getattr(self.config, "auto_trade", None)
            fx_rate = getattr(auto_trade_cfg, "usd_krw_fallback_rate", 1380.0)
            gross_pnl_usd = (candidate.last_price - held.avg_price) * real_qty_sold
            gross_pnl_krw = gross_pnl_usd * fx_rate
            self.repository.save_cycle_log(
                logged_at=datetime.now(timezone.utc).isoformat(),
                market="overseas",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                action_bias="SELL_REAL",
                action_reason=exit_reason,
                price=candidate.last_price,
                pnl_pct=pnl_pct,
                realized_pnl_usd=gross_pnl_usd,
                realized_pnl_krw=gross_pnl_krw,
                holding_qty=real_qty_sold,
                cycle_no=getattr(self, "_cycle_count", 0),
                session_id=getattr(self, "_session_id", ""),
                strategy_flag=strategy_flag,
                entry_by=entry_by,
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
            "구분=매도 (virtual)",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={sell_qty}주",
            f"진입전략={strategy_flag or '-'}",
            f"청산트리거={exit_by or format_reason_korean(exit_reason)}",
            f"수익률={format_pct(realized_pnl_pct)}",
        ]
        if closed_virtual_buy_qty > 0:
            lines.append(f"가상매수차감={closed_virtual_buy_qty}주")
        if pending_real_qty > 0:
            lines.append(f"실보유정산대기={pending_real_qty}주")
        if rejected_error:
            lines.append("참고=실매도거부를 가상체결로 전환")
        await self.notifier.send("\n".join(lines))
        self._reset_strategy_position(candidate.symbol)
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
        except KisApiError:
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
        macd_min = self.config.auto_trade.macd_min_bars
        required_minute_bars = max(
            self.config.auto_trade.intraday_slow_window,
            macd_min,
        )
        if (
            len(daily_closes) < self.config.auto_trade.daily_slow_window
            or len(minute_closes) < required_minute_bars
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

    def _should_buy_overseas_candidate(
        self,
        snapshot: MovingAverageSnapshot,
        symbol: str = "",
    ) -> tuple[bool, str]:
        return self._should_buy_signal(symbol, snapshot)

    def _should_buy_signal(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot,
    ) -> tuple[bool, str]:
        _override = compute_adaptive_override(self.config.auto_trade, snapshot)
        effective_config = apply_override(self.config.auto_trade, _override)
        inverse_symbols = getattr(self.config.liquidity_lab, "inverse_etf_symbols", [])
        leveraged_symbols = getattr(self.config.liquidity_lab, "leveraged_etf_symbols", [])
        entry_setup = evaluate_entry_setup(
            effective_config,
            snapshot,
            symbol=symbol,
            inverse_etf_symbols=inverse_symbols,
            leveraged_etf_symbols=leveraged_symbols,
        )
        return entry_setup.ready, entry_setup.reason

    def _should_exit_overseas_position(
        self,
        snapshot: MovingAverageSnapshot,
        held: OverseasHeldPosition,
    ) -> tuple[bool, str]:
        return self._should_exit_position(snapshot, held.pnl_pct)

    def _should_exit_position(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
    ) -> tuple[bool, str]:
        exit_setup = self._build_exit_setup(snapshot, pnl_pct, 1)
        return exit_setup.action in {"sell", "sell_partial"}, exit_setup.reason

    @staticmethod
    def _is_mock_us_session_blocked_error(message: str) -> bool:
        return (
            "미국주식 주간거래는 제공하지 않습니다" in message
            or "KIS mock currently supports US order tests only during the US regular session" in message
            or "does not support US daytime trading" in message
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

        for row in pending_rows:
            symbol = str(row["symbol"]).upper()
            pending_qty = int(row["qty"])
            pending_avg_price = float(row["avg_sell_price"])
            exchange_code = row.get("exchange_code")
            currency = str(row["currency"])

            real = real_by_symbol.get(symbol)
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

            if tracker is not None:
                tracker.settle(
                    market="overseas",
                    symbol=symbol,
                    real_qty_after_settlement=max(0, real_qty - settle_qty),
                )

    async def _send_summary(self, report: LiquidityLabReport) -> None:
        action = self._build_action_summary(report)
        if action["action_raw"] in {"WAIT", "VIRTUAL_BUY", "VIRTUAL_SELL"}:
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
        if submitted_order and submitted_order.get("already_notified"):
            return
        session_note = ""
        if report.primary_market == "overseas" and not report.us_orderable_in_profile:
            session_note = " (거래불가 세션)"
        lines = [
            "[KIS][LIQUIDITY_LAB]",
            f"시각={self._format_report_time(report.scanned_at)}",
            f"시장={format_market_korean(report.primary_market)}{session_note}",
            f"종목={report.primary_target or '-'}",
            f"동작={action['action']}",
            f"가격={action['price']}",
            f"수량={action['qty']}",
        ]
        if action["action_raw"] == "BUY":
            lines.append(f"전략={action.get('strategy_flag', '-')}")
            lines.append(f"주도={action.get('entry_by', '-')}")
        elif action["action_raw"] in {"SELL", "SELL_REJECTED", "VIRTUAL_SELL"}:
            lines.append(f"진입전략={action.get('strategy_flag', '-')}")
            lines.append(f"청산트리거={action.get('exit_by', '-')}")
            if action.get("pnl_text", "-") != "-":
                lines.append(f"수익률={action['pnl_text']}")
            else:
                lines.append(f"사유={action['reason']}")
        else:
            lines.append(f"지표={action['indicator']}")
            lines.append(f"사유={action['reason']}")
        if action["action_raw"] == "SELL_REJECTED":
            lines.append("참고=주문이 거부되어 실제로 체결되지 않았습니다")
        await self.notifier.send("\n".join(lines))

    def _build_action_summary(self, report: LiquidityLabReport) -> dict[str, str]:
        overseas_order = report.overseas_order or {}
        domestic_order = report.domestic_order or {}
        if overseas_order.get("submitted"):
            return self._format_order_summary(overseas_order, currency="USD")
        if domestic_order.get("submitted"):
            return self._format_order_summary(domestic_order, currency="KRW")
        if report.primary_market == "overseas" and (
            overseas_order.get("skipped") or overseas_order.get("error")
        ):
            return self._format_order_summary(overseas_order, currency="USD")
        if report.primary_market == "domestic" and (
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

    def _format_order_summary(self, order: dict, *, currency: str) -> dict[str, str]:
        candidate = order.get("candidate") or {}
        held = order.get("held_position") or {}
        signal_snapshot = order.get("signal_snapshot") or {}
        side = str(order.get("side", "wait")).upper()
        if order.get("virtual") and side == "BUY":
            action = "VIRTUAL_BUY"
        elif order.get("virtual") and side == "SELL":
            action = "VIRTUAL_SELL"
        else:
            action = side if side not in {"HOLD", "WAIT"} else "WAIT"
        if order.get("skipped"):
            action = "WAIT"
            if side == "BUY" and str(order.get("reason")) == "dry_run_enabled":
                action = "BUY_SETUP"
            elif side == "SELL" and str(order.get("reason")) == "dry_run_enabled":
                action = "SELL_SETUP"
            elif side == "SELL" and order.get("error"):
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
            "strategy_flag": str(order.get("strategy_flag") or "-"),
            "entry_by": str(order.get("entry_by") or "-"),
            "exit_by": str(order.get("exit_by") or "-"),
        }

    def _get_strategy_manager(self, symbol: str) -> PriorityStrategyManager:
        key = symbol.strip().upper()
        managers = getattr(self, "_strategy_managers", None)
        if managers is None:
            managers = {}
            self._strategy_managers = managers
        manager = managers.get(key)
        if manager is None:
            manager = PriorityStrategyManager()
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
            domestic_candidates = len(config.domestic_candidates)
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
            n_candidates = len(config.overseas_candidates)
            top_n = max(config.unified_scan_top_n, 1)
            estimated_calls += n_candidates
            estimated_calls += min(top_n, n_candidates) * 2
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in config.overseas_candidates
            }
            estimated_calls += len(exchange_codes)
        if krx_open and us_open:
            estimated_calls += min(len(config.domestic_candidates), config.unified_watch_top_n)
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
    ):
        return evaluate_exit_setup(
            self.config.auto_trade,
            snapshot,
            pnl_pct,
            drawdown_from_peak=0.0,
            hold_cycles=0,
            position_qty=position_qty,
            partial_exit_done=False,
        )
