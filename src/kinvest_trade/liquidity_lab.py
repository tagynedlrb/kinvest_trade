from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

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
from .lab_domestic_orders import DomesticOrderHelper
from .lab_notify import TradeNotifier
from .lab_overseas_orders import OverseasOrderHelper
from .lab_positions import UnifiedPositionTracker, VirtualPosition, VirtualTradeManager
from .lab_risk import CircuitBreakerManager
from .lab_runtime import LabRuntimeManager
from .lab_watch import WatchStateHelper
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


def _fallback_runtime_config() -> SimpleNamespace:
    return SimpleNamespace(
        liquidity_lab=SimpleNamespace(loop_interval_sec=25),
        auto_trade=SimpleNamespace(
            rsi_entry_threshold=50.0,
            min_hold_before_trend_exit=12,
        ),
    )


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
        self.cb = CircuitBreakerManager(
            self.config,
            event_hook=self._save_event,
            notify_hook=self._send_circuit_breaker_notification,
        )
        self._domestic_excluded: list[ExcludedCandidate] = []
        self._overseas_excluded: list[ExcludedCandidate] = []
        self._last_held_symbols: set[str] = set()
        self._signal_cache: dict[str, MovingAverageSnapshot | None] = {}
        self._signal_cache_updated_at: dict[str, datetime] = {}
        self._overseas_signal_failures: dict[str, int] = {}
        self._overseas_signal_suppressed_until: dict[str, datetime] = {}
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
        self.trade_notifier = TradeNotifier(
            self.notifier,
            window_seconds=self._trade_notification_window_sec,
            max_batch_size=self._trade_notification_max_batch_size,
        )
        self._session_start_logged: bool = False
        self._no_orderable_retry: dict[str, datetime] = {}
        self._exit_price_shock_guard: dict[str, dict[str, float | str]] = {}
        self._cycle_exit_reference_prices: dict[str, float] = {}
        self._recent_trade_count: int = 0
        self._recent_cycle_count: int = 0
        self._recent_order_reason_counts: dict[str, int] = {}
        self._rsi_blocked_count: int = 0
        self._last_low_trade_frequency_alert_cycle: int = 0
        self._last_trend_filter_alert_cycle: int = 0
        self.runtime = LabRuntimeManager(
            self.config,
            self.repository,
            self.notifier,
            is_effective_trade_order=self._is_effective_trade_order,
        )
        self._strategy_guard_cache: dict[str, object] = {}
        self._last_strategy_guard_blocked_keys: set[tuple[str, str]] = set()
        self.watch_state = WatchStateHelper(self)
        self.domestic_orders = DomesticOrderHelper(self)
        self.overseas_orders = OverseasOrderHelper(self)

    def _get_circuit_breaker(self) -> CircuitBreakerManager:
        cb = getattr(self, "cb", None)
        if cb is None:
            cb = CircuitBreakerManager(
                self.config,
                event_hook=self._save_event,
                notify_hook=self._send_circuit_breaker_notification,
            )
            self.cb = cb
        cb.load_state(
            consecutive_losses=int(getattr(self, "_consecutive_losses", 0) or 0),
            session_realised_krw=float(getattr(self, "_session_realised_krw", 0.0) or 0.0),
            daily_loss_date=getattr(self, "_daily_loss_date", None),
            halted_at=getattr(self, "_halted_at", None),
            daily_halted_at=getattr(self, "_daily_halted_at", None),
        )
        return cb

    def _sync_circuit_breaker_legacy_state(self, cb: CircuitBreakerManager | None = None) -> None:
        cb = cb or getattr(self, "cb", None)
        if cb is None:
            return
        snapshot = cb.snapshot()
        self._consecutive_losses = int(snapshot["consecutive_losses"])
        self._session_realised_krw = float(snapshot["session_realised_krw"])
        self._daily_loss_date = snapshot["daily_loss_date"]
        self._halted_at = snapshot["halted_at"]
        self._daily_halted_at = snapshot["daily_halted_at"]

    async def _send_circuit_breaker_notification(self, message: str) -> None:
        notifier = getattr(self, "notifier", None)
        if notifier is None or not getattr(notifier, "enabled", True):
            return
        await notifier.send(message)

    def _on_realised(
        self,
        *,
        market: str,
        gross_pnl_krw: float,
        pnl_pct: float,
    ) -> None:
        cb = self._get_circuit_breaker()
        cb.on_realised(
            market=market,
            gross_pnl_krw=gross_pnl_krw,
            pnl_pct=pnl_pct,
        )
        self._sync_circuit_breaker_legacy_state(cb)

    def _get_trade_notifier(self) -> TradeNotifier:
        notifier = getattr(self, "trade_notifier", None)
        if notifier is None:
            notifier = TradeNotifier(
                getattr(self, "notifier", None),
                window_seconds=getattr(self, "_trade_notification_window_sec", 60),
                max_batch_size=getattr(self, "_trade_notification_max_batch_size", 8),
            )
            self.trade_notifier = notifier
        notifier.set_notifier(getattr(self, "notifier", None))
        notifier.set_window_seconds(getattr(self, "_trade_notification_window_sec", 60))
        notifier.set_max_batch_size(getattr(self, "_trade_notification_max_batch_size", 8))
        notifier.load_state(
            lines=getattr(self, "_pending_trade_notifications", []),
            window_start=getattr(self, "_pending_trade_notification_started_at", None),
        )
        return notifier

    def _sync_trade_notifier_legacy_state(self, notifier: TradeNotifier | None = None) -> None:
        notifier = notifier or getattr(self, "trade_notifier", None)
        if notifier is None:
            return
        self._pending_trade_notifications = notifier.queued_lines
        self._pending_trade_notification_started_at = notifier.window_start
        self._trade_notification_window_sec = notifier.window_seconds
        self._trade_notification_max_batch_size = notifier.max_batch_size

    def _get_runtime_manager(self) -> LabRuntimeManager:
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            runtime = LabRuntimeManager(
                getattr(self, "config", _fallback_runtime_config()),
                getattr(self, "repository", None),
                getattr(self, "notifier", None),
                is_effective_trade_order=self._is_effective_trade_order,
            )
            self.runtime = runtime
        runtime.configure(
            config=getattr(self, "config", _fallback_runtime_config()),
            repository=getattr(self, "repository", None),
            notifier=getattr(self, "notifier", None),
        )
        runtime.load_state(
            cycle_no=int(getattr(self, "_cycle_count", 0) or 0),
            session_id=str(getattr(self, "_session_id", "") or ""),
            recent_trade_count=int(getattr(self, "_recent_trade_count", 0) or 0),
            recent_cycle_count=int(getattr(self, "_recent_cycle_count", 0) or 0),
            recent_order_reason_counts=getattr(self, "_recent_order_reason_counts", {}),
            rsi_blocked_count=int(getattr(self, "_rsi_blocked_count", 0) or 0),
            last_low_trade_frequency_alert_cycle=int(
                getattr(self, "_last_low_trade_frequency_alert_cycle", 0) or 0
            ),
            last_trend_filter_alert_cycle=int(
                getattr(self, "_last_trend_filter_alert_cycle", 0) or 0
            ),
            exit_cooldown=getattr(self, "_exit_cooldown", {}),
            no_orderable_retry=getattr(self, "_no_orderable_retry", {}),
            no_orderable_counts=getattr(self, "_no_orderable_counts", {}),
        )
        return runtime

    def _sync_runtime_legacy_state(self, runtime: LabRuntimeManager | None = None) -> None:
        runtime = runtime or getattr(self, "runtime", None)
        if runtime is None:
            return
        snapshot = runtime.snapshot()
        self._recent_trade_count = int(snapshot["recent_trade_count"])
        self._recent_cycle_count = int(snapshot["recent_cycle_count"])
        self._recent_order_reason_counts = dict(snapshot["recent_order_reason_counts"])
        self._rsi_blocked_count = int(snapshot["rsi_blocked_count"])
        self._last_low_trade_frequency_alert_cycle = int(
            snapshot["last_low_trade_frequency_alert_cycle"]
        )
        self._last_trend_filter_alert_cycle = int(snapshot["last_trend_filter_alert_cycle"])
        self._exit_cooldown = dict(snapshot["exit_cooldown"])
        self._no_orderable_retry = dict(snapshot["no_orderable_retry"])
        self._no_orderable_counts = dict(snapshot["no_orderable_counts"])

    def _get_watch_state_helper(self) -> WatchStateHelper:
        helper = getattr(self, "watch_state", None)
        if helper is None:
            helper = WatchStateHelper(self)
            self.watch_state = helper
        return helper

    def _get_domestic_order_helper(self) -> DomesticOrderHelper:
        helper = getattr(self, "domestic_orders", None)
        if helper is None:
            helper = DomesticOrderHelper(self)
            self.domestic_orders = helper
        return helper

    def _get_overseas_order_helper(self) -> OverseasOrderHelper:
        helper = getattr(self, "overseas_orders", None)
        if helper is None:
            helper = OverseasOrderHelper(self)
            self.overseas_orders = helper
        return helper

    def _make_watch_target_status(
        self,
        *,
        market: str,
        code: str,
        exchange_code: str | None,
        price: float,
        activity_score: float,
        signal_score: float,
        action_bias: str,
        signal_state: str,
        ma_summary: str,
        note: str,
        holding_qty: int = 0,
        signal_snapshot: MovingAverageSnapshot | None = None,
        strategy_flag: str = "",
        entry_by: str = "",
    ) -> WatchTargetStatus:
        return WatchTargetStatus(
            market=market,
            code=code,
            exchange_code=exchange_code,
            price=price,
            activity_score=activity_score,
            signal_score=signal_score,
            action_bias=action_bias,
            signal_state=signal_state,
            ma_summary=ma_summary,
            note=note,
            holding_qty=holding_qty,
            signal_snapshot=signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
        )

    def _evaluate_entry_setup(
        self,
        signal_snapshot: MovingAverageSnapshot,
        code: str,
    ):
        inverse_symbols = getattr(self.config.liquidity_lab, "inverse_etf_symbols", [])
        leveraged_symbols = getattr(self.config.liquidity_lab, "leveraged_etf_symbols", [])
        return evaluate_entry_setup(
            self.config.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=inverse_symbols,
            leveraged_etf_symbols=leveraged_symbols,
        )

    def _derive_watch_state(
        self,
        signal_snapshot: MovingAverageSnapshot,
        code: str,
    ) -> tuple[str, str]:
        inverse_symbols = getattr(self.config.liquidity_lab, "inverse_etf_symbols", [])
        leveraged_symbols = getattr(self.config.liquidity_lab, "leveraged_etf_symbols", [])
        return derive_watch_state(
            self.config.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=inverse_symbols,
            leveraged_etf_symbols=leveraged_symbols,
        )

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

    def _should_block_overseas_standalone_rsi(
        self,
        *,
        market: str,
        strategy_flag: str,
    ) -> bool:
        return (
            market == "overseas"
            and strategy_flag == "RSI"
            and bool(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_block_standalone_rsi",
                    False,
                )
            )
        )

    def _should_block_overseas_standalone_vol(
        self,
        *,
        market: str,
        strategy_flag: str,
    ) -> bool:
        return (
            market == "overseas"
            and strategy_flag == "VOL"
            and bool(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_block_standalone_vol",
                    False,
                )
            )
        )

    def _strategy_guard_blocked_keys(self) -> set[tuple[str, str]]:
        config = getattr(self.config, "liquidity_lab", object())
        if not bool(getattr(config, "strategy_guard_enabled", False)):
            return set()
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "get_recent_strategy_guard_performance"):
            return set()
        cycle_no = getattr(self, "_cycle_count", 0)
        cache = getattr(self, "_strategy_guard_cache", {})
        if cache.get("cycle_no") == cycle_no:
            return set(cache.get("blocked", set()))

        lookback_hours = max(1, int(getattr(config, "strategy_guard_lookback_hours", 48) or 48))
        min_trades = max(1, int(getattr(config, "strategy_guard_min_trades", 3) or 3))
        max_avg_net = float(
            getattr(config, "strategy_guard_max_avg_net_pnl_pct", -0.003) or -0.003
        )
        guard_markets = {
            str(market).strip().lower()
            for market in getattr(config, "strategy_guard_markets", ["overseas"])
            if str(market).strip()
        }
        guard_flags = {
            str(flag).strip().upper()
            for flag in getattr(config, "strategy_guard_strategy_flags", ["VWAP", "RSI"])
            if str(flag).strip()
        }
        after_logged_at = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
        auto_config = getattr(self.config, "auto_trade", object())
        cost_pct = max(
            0.005,
            float(getattr(auto_config, "overseas_commission_rate", 0.0025) or 0.0025) * 2,
        )
        rows = repository.get_recent_strategy_guard_performance(
            after_logged_at=after_logged_at,
            cost_pct=cost_pct,
        )
        blocked: set[tuple[str, str]] = set()
        blocked_detail: list[dict] = []
        for row in rows:
            market = str(row.get("market") or "").strip().lower()
            strategy = str(row.get("strategy_flag") or "").strip().upper()
            if not market or not strategy:
                continue
            if guard_markets and market not in guard_markets:
                continue
            if guard_flags and strategy not in guard_flags:
                continue
            trade_count = int(row.get("trade_count") or 0)
            avg_net = float(row.get("avg_net_pnl_pct") or 0.0)
            if trade_count < min_trades or avg_net > max_avg_net:
                continue
            blocked.add((market, strategy))
            blocked_detail.append(
                {
                    "market": market,
                    "strategy_flag": strategy,
                    "trade_count": trade_count,
                    "avg_net_pnl_pct": round(avg_net, 6),
                }
            )

        self._strategy_guard_cache = {
            "cycle_no": cycle_no,
            "blocked": blocked,
            "rows": rows,
        }
        previous = getattr(self, "_last_strategy_guard_blocked_keys", set())
        if blocked and blocked != previous:
            self._save_event(
                event_type="strategy_guard_active",
                detail={
                    "lookback_hours": lookback_hours,
                    "min_trades": min_trades,
                    "max_avg_net_pnl_pct": max_avg_net,
                    "blocked": blocked_detail,
                },
            )
        self._last_strategy_guard_blocked_keys = blocked
        return blocked

    def _entry_strategy_block_reason(
        self,
        *,
        market: str,
        strategy_flag: str,
    ) -> str:
        strategy = str(strategy_flag or "").strip().upper()
        market_key = str(market or "").strip().lower()
        if not strategy:
            return ""
        if self._should_block_overseas_standalone_vwap(
            market=market_key,
            strategy_flag=strategy,
        ):
            return "standalone_vwap_blocked"
        if self._should_block_overseas_standalone_rsi(
            market=market_key,
            strategy_flag=strategy,
        ):
            return "standalone_rsi_blocked"
        if self._should_block_overseas_standalone_vol(
            market=market_key,
            strategy_flag=strategy,
        ):
            return "standalone_vol_blocked"
        if (market_key, strategy) in self._strategy_guard_blocked_keys():
            return "recent_strategy_underperformance"
        return ""

    def _entry_liquidity_block_reason(
        self,
        *,
        market: str,
        signal_snapshot: MovingAverageSnapshot | None,
    ) -> str:
        """Protect overseas scalping entries from low-flow strategy signals."""
        if str(market or "").strip().lower() != "overseas":
            return ""
        if signal_snapshot is None:
            return ""
        min_ratio = float(
            getattr(
                self.config.liquidity_lab,
                "overseas_min_strategy_volume_ratio",
                0.0,
            )
            or 0.0
        )
        if min_ratio <= 0:
            return ""
        if signal_snapshot.volume_ratio < min_ratio:
            return "overseas_volume_floor"
        return ""

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
        notifier = self._get_trade_notifier()
        notifier.queue(line)
        self._sync_trade_notifier_legacy_state(notifier)

    def _trade_notification_window_seconds(self) -> int:
        notifier = self._get_trade_notifier()
        self._sync_trade_notifier_legacy_state(notifier)
        return notifier.window_seconds

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

    def _sell_order_submit_spec(
        self,
        *,
        market: str,
        exit_reason: str,
        reference_price: float,
    ) -> dict[str, object]:
        """Return KIS submit parameters while keeping analytics on reference price."""
        protective = exit_reason in self._protective_exit_reasons()
        market_key = market.strip().lower()
        if protective and market_key == "domestic":
            return {
                "order_division": "01",
                "submit_price": 0,
                "order_kind": "market",
                "reference_price": reference_price,
            }
        if protective and market_key == "overseas":
            env = str(getattr(self.config.credentials, "env", "vps") or "vps")
            if env == "prod":
                return {
                    "order_division": "01",
                    "submit_price": "0",
                    "order_kind": "market",
                    "reference_price": reference_price,
                }
            return {
                "order_division": "00",
                "submit_price": f"{reference_price:.4f}",
                "order_kind": "aggressive_limit",
                "reference_price": reference_price,
            }
        if market_key == "domestic":
            return {
                "order_division": "00",
                "submit_price": int(reference_price),
                "order_kind": "limit",
                "reference_price": reference_price,
            }
        return {
            "order_division": "00",
            "submit_price": f"{reference_price:.4f}",
            "order_kind": "limit",
            "reference_price": reference_price,
        }

    def _broker_cancel_payload(
        self,
        cancel_response: object,
        pending_order: dict,
        *,
        reference_price: float | None = None,
    ) -> dict[str, object]:
        order_price = float(
            pending_order.get("order_price")
            or self._parse_float(pending_order.get("ord_unpr"))
            or self._parse_float(pending_order.get("ft_ord_unpr3"))
            or 0.0
        )
        order_division = str(
            pending_order.get("ord_dvsn_cd")
            or pending_order.get("order_division")
            or "00"
        ).strip() or "00"
        original_order_no = str(
            pending_order.get("order_no") or pending_order.get("odno") or ""
        ).strip()
        original_order_orgno = str(
            pending_order.get("ord_gno_brno")
            or pending_order.get("krx_fwdg_ord_orgno")
            or pending_order.get("KRX_FWDG_ORD_ORGNO")
            or ""
        ).strip()
        open_qty = int(
            pending_order.get("open_qty")
            or parse_kis_number(pending_order.get("rmn_qty"))
            or parse_kis_number(pending_order.get("nccs_qty"))
            or 0
        )
        payload: dict[str, object] = {
            "response": cancel_response,
            "original_order_no": original_order_no,
            "order_division": order_division,
            "original_order_price": order_price,
            "reference_price": order_price if reference_price is None else reference_price,
            "open_qty": open_qty,
        }
        if original_order_orgno:
            payload["original_order_orgno"] = original_order_orgno
        return payload

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
        notifier = self._get_trade_notifier()
        await notifier.flush_async(force=force)
        self._sync_trade_notifier_legacy_state(notifier)

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
            self._prime_cycle_exit_reference_prices(monitored_overseas_positions)
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
            self._cycle_exit_reference_prices = {}
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
        overseas_entry_block_reason = ""
        overseas_entry_block_detail: dict[str, int] = {}
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
                open_overseas_symbols = {
                    position.symbol.strip().upper()
                    for position in monitored_overseas_positions
                    if position.symbol.strip() and position.quantity > 0
                }
                overseas_entry_block_reason = "overseas_position_cap_reached"
                overseas_entry_block_detail = {
                    "open_positions": len(open_overseas_symbols),
                    "max_positions": int(_max_os),
                }
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
            overseas_skip_reason = (
                "us_open_but_mock_session_not_supported"
                if us_open and not us_orderable_in_profile
                else overseas_entry_block_reason or "no_overseas_candidate"
            )
            overseas_order = {
                "skipped": True,
                "reason": overseas_skip_reason,
            }
            overseas_order.update(overseas_entry_block_detail)
            overseas_orders = [overseas_order]
        if overseas_orders:
            overseas_order = dict(overseas_order)
            overseas_order["batched_orders"] = overseas_orders
        if domestic_orders:
            domestic_order = dict(domestic_order)
            domestic_order["batched_orders"] = domestic_orders

        self._record_cycle_trade_frequency(
            domestic_orders=domestic_orders,
            overseas_orders=overseas_orders,
        )
        self._check_trend_filter_lost_ratio()

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
            symbol = candidate.symbol.strip().upper()
            if symbol not in held_symbols:
                suppression_reason = self._overseas_signal_suppression_reason(symbol)
                if suppression_reason:
                    excluded.append(
                        ExcludedCandidate(
                            market="overseas",
                            code=symbol,
                            reasons=[suppression_reason],
                            snapshot={
                                "symbol": symbol,
                                "exchange_code": candidate.exchange_code,
                            },
                        )
                    )
                    continue
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
            signal_snapshot = await self._get_overseas_signal_for_candidate(result)
            self._signal_cache[symbol] = signal_snapshot
            self._record_overseas_signal_result(
                result,
                signal_snapshot,
                is_held=symbol in held_symbols,
            )
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
        return await self._get_watch_state_helper().select_overseas_exit_targets(
            overseas_ranked,
            held_positions,
            max_exits=max_exits,
        )

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

    def _overseas_signal_suppression_reason(self, symbol: str) -> str:
        suppressed = getattr(self, "_overseas_signal_suppressed_until", None)
        if not suppressed:
            return ""
        key = symbol.strip().upper()
        until = suppressed.get(key)
        if until is None:
            return ""
        until = ensure_timezone(until)
        if datetime.now(timezone.utc) >= until:
            suppressed.pop(key, None)
            failures = getattr(self, "_overseas_signal_failures", None)
            if failures:
                failures.pop(key, None)
            return ""
        return "signal_unavailable_cooldown"

    def _record_overseas_signal_result(
        self,
        candidate: OverseasScanResult,
        snapshot: MovingAverageSnapshot | None,
        *,
        is_held: bool,
    ) -> None:
        symbol = candidate.symbol.strip().upper()
        if not symbol:
            return
        failures = getattr(self, "_overseas_signal_failures", None)
        if failures is None:
            failures = {}
            self._overseas_signal_failures = failures
        suppressed = getattr(self, "_overseas_signal_suppressed_until", None)
        if suppressed is None:
            suppressed = {}
            self._overseas_signal_suppressed_until = suppressed

        if snapshot is not None:
            failures.pop(symbol, None)
            suppressed.pop(symbol, None)
            return
        if is_held:
            return

        failures[symbol] = int(failures.get(symbol, 0) or 0) + 1
        threshold = max(
            1,
            int(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_signal_failure_threshold",
                    3,
                )
                or 3
            ),
        )
        if failures[symbol] < threshold:
            return

        cooldown_minutes = max(
            1,
            int(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_signal_failure_cooldown_minutes",
                    180,
                )
                or 180
            ),
        )
        until = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
        suppressed[symbol] = until
        self._save_event(
            event_type="overseas_signal_suppressed",
            market="overseas",
            symbol=symbol,
            detail={
                "reason": "signal_unavailable",
                "failures": failures[symbol],
                "threshold": threshold,
                "cooldown_minutes": cooldown_minutes,
                "activity_score": candidate.activity_score,
                "price": candidate.last_price,
            },
        )

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
        key = f"overseas:{symbol.strip().upper()}"
        cycle_refs = getattr(self, "_cycle_exit_reference_prices", {}) or {}
        cycle_reference_price = float(cycle_refs.get(key, 0.0) or 0.0)
        if cycle_reference_price > 0:
            reference_price = cycle_reference_price
        repository = getattr(self, "repository", None)
        if cycle_reference_price <= 0 and repository is not None:
            state = repository.get_lab_symbol_state("overseas", symbol)
            if state is not None:
                previous_price = float(state.get("last_price") or 0.0)
                if previous_price > 0 and (
                    reference_price <= 0
                    or abs(previous_price - last_price) / previous_price > 0.000001
                ):
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
        self._get_watch_state_helper().remember_persisted_symbol_state(state)

    def _get_persisted_symbol_state(self, market: str, symbol: str) -> dict | None:
        return self._get_watch_state_helper().get_persisted_symbol_state(market, symbol)

    def _prime_cycle_exit_reference_prices(
        self,
        overseas_positions: list[OverseasHeldPosition],
    ) -> None:
        self._get_watch_state_helper().prime_cycle_exit_reference_prices(overseas_positions)

    @staticmethod
    def _snapshot_from_payload(
        payload: dict | None,
    ) -> MovingAverageSnapshot | None:
        return self._get_watch_state_helper().snapshot_from_payload(payload)

    @staticmethod
    def _with_live_price(
        snapshot: MovingAverageSnapshot,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> MovingAverageSnapshot:
        return WatchStateHelper.with_live_price(snapshot, price=price, bid=bid, ask=ask)

    def _state_snapshot_with_live_price(
        self,
        state: dict | None,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> MovingAverageSnapshot | None:
        return self._get_watch_state_helper().state_snapshot_with_live_price(
            state,
            price=price,
            bid=bid,
            ask=ask,
        )

    async def _get_overseas_signal_for_candidate(
        self,
        candidate: OverseasScanResult,
    ) -> MovingAverageSnapshot | None:
        return await self._get_watch_state_helper().get_overseas_signal_for_candidate(candidate)

    def _persist_watch_target_state(
        self,
        watch_target: WatchTargetStatus,
        *,
        pnl_pct: float | None = None,
        exit_by: str = "",
    ) -> None:
        self._get_watch_state_helper().persist_watch_target_state(
            watch_target,
            pnl_pct=pnl_pct,
            exit_by=exit_by,
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
        self._get_watch_state_helper().persist_trade_state(
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            action_bias=action_bias,
            signal_state=signal_state,
            note=note,
            holding_qty=holding_qty,
            last_price=last_price,
            pnl_pct=pnl_pct,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            signal_snapshot=signal_snapshot,
            has_position=has_position,
        )

    def _clear_stale_lab_position_states(
        self,
        *,
        domestic_positions: list[DomesticHeldPosition],
        overseas_positions: list[OverseasHeldPosition],
        refreshed_markets: set[str],
    ) -> None:
        self._get_watch_state_helper().clear_stale_lab_position_states(
            domestic_positions=domestic_positions,
            overseas_positions=overseas_positions,
            refreshed_markets=refreshed_markets,
        )

    def _restore_strategy_contexts(
        self,
        *,
        domestic_positions: list[DomesticHeldPosition],
        overseas_positions: list[OverseasHeldPosition],
    ) -> None:
        self._get_watch_state_helper().restore_strategy_contexts(
            domestic_positions=domestic_positions,
            overseas_positions=overseas_positions,
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
        self._get_watch_state_helper().restore_strategy_position(
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            quantity=quantity,
            avg_price=avg_price,
            current_price=current_price,
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
        return self._get_watch_state_helper().build_watch_target_status(
            market=market,
            code=code,
            exchange_code=exchange_code,
            price=price,
            activity_score=activity_score,
            signal_snapshot=signal_snapshot,
            held_position=held_position,
            holding_qty=holding_qty,
        )

    def _save_cycle_log_from_watch_target(
        self,
        watch_target: WatchTargetStatus,
        *,
        pnl_pct: float | None = None,
    ) -> None:
        self._get_watch_state_helper().save_cycle_log_from_watch_target(
            watch_target,
            pnl_pct=pnl_pct,
        )

    def _select_domestic_buy_targets(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
        max_concurrent: int = 2,
    ) -> list[DomesticScanResult]:
        return self._get_watch_state_helper().select_domestic_buy_targets(
            domestic_ranked,
            watch_targets,
            max_concurrent=max_concurrent,
        )

    def _select_domestic_exit_target(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
        held_positions: list[DomesticHeldPosition],
    ) -> tuple[DomesticScanResult, DomesticHeldPosition, str, MovingAverageSnapshot | None] | None:
        return self._get_watch_state_helper().select_domestic_exit_target(
            domestic_ranked,
            watch_targets,
            held_positions,
        )

    def _select_overseas_buy_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        watch_targets: list[WatchTargetStatus],
        max_concurrent: int = 3,
        held_positions: list[OverseasHeldPosition] | None = None,
    ) -> list[OverseasScanResult]:
        return self._get_watch_state_helper().select_overseas_buy_targets(
            overseas_ranked,
            watch_targets,
            max_concurrent=max_concurrent,
            held_positions=held_positions,
        )

    @staticmethod
    def _remaining_overseas_entry_slots(
        positions: list[OverseasHeldPosition],
        *,
        max_positions: int,
    ) -> int:
        return WatchStateHelper.remaining_overseas_entry_slots(
            positions,
            max_positions=max_positions,
        )

    @staticmethod
    def _select_primary_target(
        *,
        krx_open: bool,
        us_open: bool,
        us_orderable_in_profile: bool,
        domestic_ranked: list[DomesticScanResult],
        overseas_ranked: list[OverseasScanResult],
    ) -> tuple[str, str | None, str]:
        return WatchStateHelper.select_primary_target(
            krx_open=krx_open,
            us_open=us_open,
            us_orderable_in_profile=us_orderable_in_profile,
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
        )

    async def _place_domestic_test_order(
        self,
        candidate: DomesticScanResult,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        return await self._get_domestic_order_helper().place_test_order(
            candidate,
            watch_target=watch_target,
        )

    async def _place_domestic_sell_order(
        self,
        candidate: DomesticScanResult,
        held: DomesticHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        return await self._get_domestic_order_helper().place_sell_order(
            candidate,
            held,
            exit_reason,
            signal_snapshot=signal_snapshot,
        )

    async def _place_overseas_test_order(
        self,
        candidate: OverseasScanResult,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        return await self._get_overseas_order_helper().place_test_order(
            candidate,
            watch_target=watch_target,
        )

    async def _manage_overseas_position(
        self,
        *,
        candidate: OverseasScanResult,
        held_positions: list[OverseasHeldPosition],
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        return await self._get_overseas_order_helper().manage_position(
            candidate=candidate,
            held_positions=held_positions,
            watch_target=watch_target,
        )

    async def _record_virtual_overseas_buy(
        self,
        candidate: OverseasScanResult,
        *,
        signal_snapshot: MovingAverageSnapshot | None = None,
        rejected_error: str | None = None,
        watch_target: WatchTargetStatus | None = None,
    ) -> dict:
        return await self._get_overseas_order_helper().record_virtual_buy(
            candidate,
            signal_snapshot=signal_snapshot,
            rejected_error=rejected_error,
            watch_target=watch_target,
        )

    async def _place_overseas_sell_order(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        return await self._get_overseas_order_helper().place_sell_order(
            candidate,
            held,
            exit_reason,
            signal_snapshot=signal_snapshot,
        )

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
        return await self._get_overseas_order_helper().record_virtual_sell(
            candidate,
            held,
            exit_reason,
            signal_snapshot=signal_snapshot,
            rejected_error=rejected_error,
            sell_qty_override=sell_qty_override,
        )

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
        cb = self._get_circuit_breaker()
        self._sync_circuit_breaker_legacy_state(cb)
        return int(cb.is_active)

    def _pool_size_for_market(self, market: str) -> int:
        market_key = market.strip().lower()
        if market_key == "domestic":
            return len(getattr(self, "_dynamic_domestic_codes", None) or [])
        if market_key == "overseas":
            return len(getattr(self, "_dynamic_overseas_pool", None) or [])
        return 0

    @staticmethod
    def _is_effective_trade_order(order: dict) -> bool:
        if not isinstance(order, dict) or order.get("skipped"):
            return False
        side = str(order.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            return False
        return bool(
            order.get("submitted")
            or order.get("recorded")
            or order.get("virtual")
            or order.get("broker_order_no")
            or order.get("order_id")
        )

    def _record_cycle_trade_frequency(
        self,
        *,
        domestic_orders: list[dict],
        overseas_orders: list[dict],
    ) -> None:
        runtime = self._get_runtime_manager()
        runtime.record_cycle_trade_frequency(
            domestic_orders=domestic_orders,
            overseas_orders=overseas_orders,
        )
        self._sync_runtime_legacy_state(runtime)

    def _track_rsi_threshold_blocks(self, watch_targets: list[WatchTargetStatus]) -> None:
        runtime = self._get_runtime_manager()
        runtime.track_rsi_threshold_blocks(watch_targets)
        self._sync_runtime_legacy_state(runtime)

    def _check_trend_filter_lost_ratio(self) -> None:
        runtime = self._get_runtime_manager()
        runtime.check_trend_filter_lost_ratio()
        self._sync_runtime_legacy_state(runtime)

    def _save_event(
        self,
        *,
        event_type: str,
        market: str = "",
        symbol: str = "",
        detail: dict | str = "",
        cycle_no: int | None = None,
    ) -> None:
        runtime = self._get_runtime_manager()
        runtime.save_event(
            event_type=event_type,
            market=market,
            symbol=symbol,
            detail=detail,
            cycle_no=cycle_no,
        )
        self._sync_runtime_legacy_state(runtime)

    def _cooldown_remaining_minutes(
        self,
        market: str,
        symbol: str,
    ) -> float:
        runtime = self._get_runtime_manager()
        remaining = runtime.cooldown_remaining_minutes(market, symbol)
        self._sync_runtime_legacy_state(runtime)
        return remaining

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
        runtime = self._get_runtime_manager()
        deferred = runtime.defer_no_orderable_position(
            market=market,
            symbol=symbol,
            holding_qty=holding_qty,
            orderable_qty=orderable_qty,
        )
        self._sync_runtime_legacy_state(runtime)
        return deferred

    def _no_orderable_retry_minutes(self, key: str) -> int:
        runtime = self._get_runtime_manager()
        minutes = runtime.no_orderable_retry_minutes(key)
        self._sync_runtime_legacy_state(runtime)
        return minutes

    def _track_no_orderable_stall(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
    ) -> int:
        runtime = self._get_runtime_manager()
        count = runtime.track_no_orderable_stall(
            market=market,
            symbol=symbol,
            holding_qty=holding_qty,
        )
        self._sync_runtime_legacy_state(runtime)
        return count

    def _reset_no_orderable_stall(self, market: str, symbol: str) -> None:
        runtime = self._get_runtime_manager()
        runtime.reset_no_orderable_stall(market, symbol)
        self._sync_runtime_legacy_state(runtime)

    def _is_no_orderable_retry_active(self, market: str, symbol: str) -> bool:
        runtime = self._get_runtime_manager()
        active = runtime.is_no_orderable_retry_active(market, symbol)
        self._sync_runtime_legacy_state(runtime)
        return active

    def _clear_no_orderable_retry(self, market: str, symbol: str) -> None:
        runtime = self._get_runtime_manager()
        runtime.clear_no_orderable_retry(market, symbol)
        self._sync_runtime_legacy_state(runtime)

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
        runtime = self._get_runtime_manager()
        runtime.register_exit_cooldown(market, symbol, exit_reason)
        self._sync_runtime_legacy_state(runtime)

    def _set_exit_cooldown_minutes(
        self,
        market: str,
        symbol: str,
        cooldown_minutes: int,
    ) -> None:
        runtime = self._get_runtime_manager()
        runtime.set_exit_cooldown_minutes(market, symbol, cooldown_minutes)
        self._sync_runtime_legacy_state(runtime)

    def _is_trading_halted(self) -> bool:
        cb = self._get_circuit_breaker()
        halted = cb.is_halted()
        self._sync_circuit_breaker_legacy_state(cb)
        return halted

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
