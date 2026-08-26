from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from .auto_trade_math import (
    DOMESTIC_COST_CALCULATION_VERSION,
    OVERSEAS_COST_CALCULATION_VERSION,
    estimate_domestic_trade_costs,
)
from .client import KisApiError, KisRestClient, parse_kis_number
from .config import AppConfig, CorporateActionDefinition, OverseasCandidateConfig
from .execution_reconciler import BrokerExecutionReconciler
from .inverse_policy import (
    INVERSE_BENCHMARK_ALIGNMENT_VERSION,
    InverseRegimeDecision,
    evaluate_inverse_regime,
)
from .market_sessions import (
    KST,
    NEW_YORK,
    get_us_trading_session,
    is_krx_execution_reconcile_window,
    is_krx_regular_session,
    is_us_execution_reconcile_window,
    is_us_orderable_session_for_env,
    is_us_regular_session,
    minutes_until_regular_session_close,
    seconds_until_us_session_transition,
    us_holiday_date_for_kis_session,
)
from .market_calendar import is_krx_holiday, is_nyse_holiday, market_status_summary
from .market_policy import (
    MarketPolicyRegistry,
    MomentumMarketPolicy,
    get_market_strategy_guard_policy,
    normalize_market_name,
)
from .market_regime import MarketRegimeCollector
from .lab_domestic_orders import DomesticOrderHelper
from .lab_notify import TradeNotifier
from .lab_overseas_orders import OverseasOrderHelper
from .lab_positions import UnifiedPositionTracker, VirtualTradeManager
from .lab_risk import CircuitBreakerManager
from .lab_runtime import LabRuntimeManager
from .lab_watch import WatchStateHelper
from .message_format import (
    format_domestic_symbol_label,
    format_side_korean,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_usd,
)
from .momentum_policy import (
    EntrySetup,
    derive_watch_state,
    evaluate_entry_setup,
    evaluate_exit_setup,
    evaluate_inverse_regime_trend_breakout_setup,
)
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .sector_context import (
    build_domestic_sector_context,
    build_overseas_sector_context,
)
from .strategy import PriorityStrategyManager, STRATEGY_LABEL, StrategyID
from .technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    chart_bar_elapsed_seconds,
    extract_price_series,
    filter_latest_session_rows,
)
from .time_utils import ensure_timezone, format_kst, format_kst_korean, parse_datetime
from .tv_scanner import check_connectivity, scan_top_volume_surge

_logger = logging.getLogger(__name__)
_DEFAULT_OVERSEAS_EXCHANGE_CODES = ("NASD", "NYSE", "AMEX")
_MIN_VPS_US_FULL_SCAN_WINDOW_SEC = 120
_EXECUTION_RECONCILE_POST_CLOSE_GRACE_MIN = 30
_VIRTUAL_SELL_SETTLEMENT_ROLE = "virtual_sell_settlement"
_DEDICATED_INVERSE_ENTRY_FORMULAS = frozenset(
    {
        "regime_trend_breakout_v1",
        "us_regime_trend_breakout_v1",
    }
)


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
    product_type: str = ""
    sector_name: str = ""
    etf_nav: float | None = None
    etf_nav_deviation_pct: float | None = None
    etf_tracking_multiplier: float | None = None
    etf_metadata_available: bool = False


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
    decision_reason: str = ""
    is_virtual: bool | None = None


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
    overseas_scan_scope: str = "none"

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
            "overseas_scan_scope": self.overseas_scan_scope,
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
        self.market_policy_registry = MarketPolicyRegistry(self.config)
        self._domestic_excluded: list[ExcludedCandidate] = []
        self._overseas_excluded: list[ExcludedCandidate] = []
        self._last_held_symbols: set[str] = set()
        self._signal_cache: dict[str, MovingAverageSnapshot | None] = {}
        self._signal_cache_updated_at: dict[str, datetime] = {}
        self._overseas_signal_failures: dict[str, int] = {}
        self._overseas_signal_suppressed_until: dict[str, datetime] = {}
        self._overseas_signal_unavailable_details: dict[str, dict] = {}
        self._repeated_skip_notify_last: dict[tuple[str, str, str], datetime] = {}
        self._cycle_count: int = 0
        self._session_id: str = uuid.uuid4().hex[:12]
        self._wait_cycles: dict[str, int] = {}
        self._exit_cooldown: dict[str, datetime] = {}
        self._symbol_loss_streak: dict[str, int] = {}
        self._vol_history: dict[str, deque] = {}
        self._vol_history_maxlen: int = 12
        self._dynamic_domestic_codes: list[str] | None = None
        self._dynamic_domestic_names: dict[str, str] = {}
        self._domestic_scan_cycle_count: int = 0
        self._dynamic_overseas_pool: list[dict[str, object]] | None = None
        self._awaiting_relist: bool = False
        self._manual_overseas_pool: list[dict[str, str]] | None = None
        self._overseas_scan_cycle_count: int = 0
        self._overseas_balance_cache: dict = {}
        self._domestic_balance_cache: dict = {}
        self._vps_open_overseas_order_snapshot_key: tuple[str, int] | None = None
        self._vps_open_overseas_order_snapshot: list[dict] = []
        self._domestic_quote_cache: dict[str, DomesticScanResult] = {}
        self._domestic_quote_cache_cycle: int = -1
        self._domestic_minute_chart_cache: dict[str, list[dict]] = {}
        self._domestic_minute_chart_cache_cycle: int = -1
        self._domestic_inverse_etf_cache: dict[
            str,
            tuple[datetime, dict],
        ] = {}
        self._daily_chart_cache: dict[
            tuple[str, str, str],
            tuple[datetime, list[dict]],
        ] = {}
        self._overseas_scan_scope: str = "full"
        self._last_non_orderable_full_scan_at: datetime | None = None
        self._last_logged_overseas_scan_scope: str = ""
        self._last_overseas_scan_candidate_count: int = 0
        self._overseas_relist_schedule: list[tuple[int, int]] = self._parse_relist_schedule(
            getattr(self.config.liquidity_lab, "overseas_relist_schedule_kst", "")
        )
        self._last_us_transition_guard_key: tuple[str, str] | None = None
        self._last_execution_reconcile_defer_key: tuple[object, ...] | None = None
        self._last_relist_kst: tuple[int, int] | None = None
        self._tv_available: bool = False
        self._last_tv_scan_used_fallback: bool = False
        self._consecutive_losses: int = 0
        self._consecutive_losses_by_market: dict[str, int] = {
            "domestic": 0,
            "overseas": 0,
        }
        self._session_realised_krw: float = 0.0
        self._session_realised_krw_overseas: float = 0.0
        self._daily_loss_date: date | None = None
        self._halted_at: datetime | None = None
        self._halted_at_by_market: dict[str, datetime] = {}
        self._daily_halted_at: datetime | None = None
        self._tv_diagnostic_ran: bool = False
        self._last_holiday_notice_key: tuple[bool, bool, str] | None = None
        self._session_owned_symbols: set[str] = set()
        self._session_owned_symbols_loaded_for_session: str = ""
        self._strategy_managers: dict[str, PriorityStrategyManager] = {}
        self._persisted_symbol_state: dict[tuple[str, str], dict] = {}
        self._domestic_fluctuation_rank_disabled: bool = (
            str(
                getattr(
                    getattr(self.config, "credentials", None),
                    "env",
                    "prod",
                )
            ).strip().lower()
            != "prod"
        )
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
        self._stop_loss_confirm_guard: dict[str, dict[str, float | str]] = {}
        self._cycle_exit_reference_prices: dict[str, float] = {}
        self._recent_trade_count: int = 0
        self._recent_cycle_count: int = 0
        self._recent_order_reason_counts: dict[str, int] = {}
        self._recent_trade_count_by_market: dict[str, int] = {}
        self._recent_cycle_count_by_market: dict[str, int] = {}
        self._recent_order_reason_counts_by_market: dict[str, dict[str, int]] = {}
        self._rsi_blocked_count: int = 0
        self._last_low_trade_frequency_alert_cycle: int = 0
        self._last_low_trade_frequency_alert_cycle_by_market: dict[str, int] = {}
        self._last_trend_filter_alert_cycle: int = 0
        self.runtime = LabRuntimeManager(
            self.config,
            self.repository,
            self.notifier,
            is_effective_trade_order=self._is_effective_trade_order,
        )
        self._strategy_guard_cache: dict[str, object] = {}
        self._last_strategy_guard_blocked_keys: set[tuple[str, str]] = set()
        self._confirmed_risk_state_restored = False
        self._confirmed_symbol_loss_state_restored = False
        self.watch_state = WatchStateHelper(self)
        self.domestic_orders = DomesticOrderHelper(self)
        self.overseas_orders = OverseasOrderHelper(self)
        self.market_regime_collector = MarketRegimeCollector(
            self.client,
            self.repository,
        )
        self.execution_reconciler = BrokerExecutionReconciler(self)
        self._last_execution_reconcile_at: datetime | None = None
        self._execution_reconcile_interval_sec = 20
        self._inverse_regime_notice_keys: set[
            tuple[str, str, str, str, str]
        ] = set()
        self._inverse_observation_keys: set[
            tuple[str, str, str, str, str, str]
        ] = set()
        self._post_fill_balance_notice_keys: set[str] = set()
        self._post_submit_balance_notice_keys: set[str] = set()
        self._corporate_action_notice_keys: set[tuple[str, str, str]] = set()

    def _get_circuit_breaker(self) -> CircuitBreakerManager:
        cb = getattr(self, "cb", None)
        if cb is None:
            cb = CircuitBreakerManager(
                self.config,
                event_hook=self._save_event,
                notify_hook=self._send_circuit_breaker_notification,
            )
            self.cb = cb
        consecutive_losses = int(getattr(self, "_consecutive_losses", 0) or 0)
        losses_by_market = getattr(self, "_consecutive_losses_by_market", None)
        if (
            isinstance(losses_by_market, dict)
            and consecutive_losses > 0
            and not any(int(value or 0) > 0 for value in losses_by_market.values())
        ):
            losses_by_market = None
        halted_at_by_market = getattr(self, "_halted_at_by_market", None)
        cb.load_state(
            consecutive_losses=consecutive_losses,
            consecutive_losses_by_market=(
                dict(losses_by_market) if isinstance(losses_by_market, dict) else None
            ),
            session_realised_krw=float(getattr(self, "_session_realised_krw", 0.0) or 0.0),
            session_realised_krw_overseas=float(
                getattr(self, "_session_realised_krw_overseas", 0.0) or 0.0
            ),
            daily_loss_date=getattr(self, "_daily_loss_date", None),
            halted_at=getattr(self, "_halted_at", None),
            halted_at_by_market=(
                dict(halted_at_by_market)
                if isinstance(halted_at_by_market, dict) and halted_at_by_market
                else None
            ),
            daily_halted_at=getattr(self, "_daily_halted_at", None),
        )
        return cb

    def _sync_circuit_breaker_legacy_state(self, cb: CircuitBreakerManager | None = None) -> None:
        cb = cb or getattr(self, "cb", None)
        if cb is None:
            return
        snapshot = cb.snapshot()
        self._consecutive_losses = int(snapshot["consecutive_losses"])
        self._consecutive_losses_by_market = dict(
            snapshot["consecutive_losses_by_market"]  # type: ignore[arg-type]
        )
        self._session_realised_krw = float(snapshot["session_realised_krw"])
        self._session_realised_krw_overseas = float(
            snapshot["session_realised_krw_overseas"]
        )
        self._daily_loss_date = snapshot["daily_loss_date"]
        self._halted_at = snapshot["halted_at"]
        self._halted_at_by_market = dict(
            snapshot["halted_at_by_market"]  # type: ignore[arg-type]
        )
        self._daily_halted_at = snapshot["daily_halted_at"]

    def _reconcile_confirmed_risk_day_pnl(
        self,
        now: datetime | None = None,
        *,
        restore_consecutive: bool = False,
    ) -> dict[str, object]:
        """Rebuild the shared risk-day PnL from broker-confirmed sell fills."""
        current = ensure_timezone(now or datetime.now(timezone.utc))
        cb = self._get_circuit_breaker()
        risk_day = cb.current_risk_day(current)
        risk_day_start = cb.current_risk_day_start(current)
        previous = cb.snapshot()
        try:
            summary = self.repository.get_session_pnl_summary(
                include_virtual=False,
                after_logged_at=risk_day_start.isoformat(),
                include_non_session_real=True,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("confirmed_risk_day_pnl_reconcile_failed")
            result = {
                "reconciled": False,
                "risk_day": risk_day.isoformat(),
                "risk_day_start": risk_day_start.isoformat(),
                "error": str(exc)[:200],
            }
            self._save_event(
                event_type="risk_day_pnl_reconcile_failed",
                detail=result,
            )
            return result

        real = summary.get("real") or {}
        by_market = {
            str(market): float(stats.get("total_pnl_krw") or 0.0)
            for market, stats in real.items()
            if isinstance(stats, dict)
        }
        total_pnl_krw = sum(by_market.values())
        overseas_pnl_krw = float(by_market.get("overseas", 0.0))
        previous_risk_day = previous.get("daily_loss_date")
        day_changed = previous_risk_day != risk_day
        restored_streaks: dict[str, int] | None = None
        restored_halted_at: dict[str, datetime] | None = None
        if restore_consecutive:
            restored_streaks, restored_halted_at = (
                self._confirmed_consecutive_loss_state(current)
            )
        cb.load_state(
            consecutive_losses_by_market=restored_streaks,
            session_realised_krw=total_pnl_krw,
            session_realised_krw_overseas=overseas_pnl_krw,
            daily_loss_date=risk_day,
            halted_at_by_market=restored_halted_at,
            daily_halted_at=(
                None if day_changed else previous.get("daily_halted_at")
            ),
            overseas_cb_active=(
                False
                if day_changed
                else bool(previous.get("overseas_cb_active", False))
            ),
        )
        daily_limit_halted = cb.is_daily_halted(current)
        self._sync_circuit_breaker_legacy_state(cb)
        trade_count = sum(
            int(stats.get("trade_count") or 0)
            for stats in real.values()
            if isinstance(stats, dict)
        )
        result = {
            "reconciled": True,
            "risk_day": risk_day.isoformat(),
            "risk_day_start": risk_day_start.isoformat(),
            "previous_risk_day": (
                previous_risk_day.isoformat()
                if isinstance(previous_risk_day, date)
                else str(previous_risk_day or "")
            ),
            "trade_count": trade_count,
            "total_pnl_krw": round(total_pnl_krw, 2),
            "overseas_pnl_krw": round(overseas_pnl_krw, 2),
            "daily_limit_halted": daily_limit_halted,
            "by_market": {
                market: round(value, 2)
                for market, value in sorted(by_market.items())
            },
        }
        if restore_consecutive:
            result["consecutive_losses_by_market"] = dict(
                restored_streaks or {}
            )
            result["consecutive_halted_until_by_market"] = {
                market: (
                    halted_at
                    + timedelta(
                        minutes=self._market_risk_value(
                            market,
                            "circuit_breaker_cooldown_minutes",
                            int(
                                getattr(
                                    self.config.risk,
                                    "circuit_breaker_cooldown_minutes",
                                    0,
                                )
                                or 0
                            ),
                        )
                    )
                ).isoformat()
                for market, halted_at in (restored_halted_at or {}).items()
            }
            self._save_event(
                event_type="cb_state_restored",
                detail={
                    "risk_day": risk_day.isoformat(),
                    "consecutive_losses_by_market": dict(
                        restored_streaks or {}
                    ),
                    "halted_markets": sorted(
                        (restored_halted_at or {}).keys()
                    ),
                },
            )
        self._save_event(
            event_type="risk_day_pnl_reconciled",
            detail=result,
        )
        return result

    def _reconcile_confirmed_symbol_loss_state(
        self,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Backfill trailing per-symbol net-loss streaks for legacy state."""
        current = ensure_timezone(now or datetime.now(timezone.utc))
        try:
            outcomes = self.repository.get_recent_confirmed_sell_risk_outcomes(
                limit=5000,
                cost_pct=0.005,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("confirmed_symbol_loss_state_reconcile_failed")
            result = {
                "reconciled": False,
                "error": str(exc)[:200],
            }
            self._save_event(
                event_type="symbol_loss_streak_state_restore_failed",
                detail=result,
            )
            return result

        streaks: dict[str, int] = {}
        latest_loss_at: dict[str, datetime] = {}
        resolved_keys: set[str] = set()
        for row in outcomes:
            market = str(row.get("market") or "").strip().lower()
            symbol = str(row.get("symbol") or "").strip().upper()
            if market not in {"domestic", "overseas"} or not symbol:
                continue
            if str(row.get("action_reason") or "") == (
                _VIRTUAL_SELL_SETTLEMENT_ROLE
            ):
                continue
            key = f"{market}:{symbol}"
            if key in resolved_keys:
                continue
            net_pnl_pct = float(row.get("net_pnl_pct") or 0.0)
            if net_pnl_pct >= 0:
                resolved_keys.add(key)
                continue
            streaks[key] = min(3, streaks.get(key, 0) + 1)
            if key not in latest_loss_at:
                occurred_at = parse_datetime(str(row.get("logged_at") or ""))
                if occurred_at is not None:
                    latest_loss_at[key] = ensure_timezone(occurred_at)

        runtime = self._get_runtime_manager()
        for key, streak in streaks.items():
            runtime.symbol_loss_streak[key] = max(
                int(runtime.symbol_loss_streak.get(key, 0) or 0),
                streak,
            )

        extended_cooldowns: dict[str, str] = {}
        for key, streak in streaks.items():
            if streak < 2 or key not in latest_loss_at:
                continue
            market, symbol = key.split(":", 1)
            cooldown_minutes = 180 if streak >= 3 else 60
            runtime.set_exit_cooldown_minutes(
                market,
                symbol,
                cooldown_minutes,
                started_at=latest_loss_at[key],
                observed_at=current,
            )
            cooldown_until = runtime.exit_cooldown.get(key)
            if (
                cooldown_until is not None
                and ensure_timezone(cooldown_until) > current
            ):
                extended_cooldowns[key] = ensure_timezone(
                    cooldown_until
                ).isoformat()

        self._sync_runtime_legacy_state(runtime)
        result = {
            "reconciled": True,
            "source": "confirmed_sell_ledger_one_time_backfill",
            "streaks": {
                key: int(value)
                for key, value in sorted(streaks.items())
                if value > 0
            },
            "extended_cooldowns": dict(sorted(extended_cooldowns.items())),
        }
        self._save_event(
            event_type="symbol_loss_streak_state_restored",
            detail=result,
        )
        return result

    def _market_risk_value(
        self,
        market: str,
        field_name: str,
        fallback: int,
    ) -> int:
        policies = getattr(self.config, "market_policies", None)
        definition = (
            getattr(policies, str(market).strip().lower(), None)
            if policies is not None
            else None
        )
        configured = getattr(definition, field_name, None)
        return fallback if configured is None else int(configured)

    def _confirmed_consecutive_loss_state(
        self,
        current: datetime,
    ) -> tuple[dict[str, int], dict[str, datetime]]:
        outcomes = self.repository.get_recent_confirmed_sell_risk_outcomes(
            limit=1000,
            cost_pct=0.005,
        )
        persisted_halts, released_at_by_market = (
            self._persisted_consecutive_breaker_state(current)
        )
        streaks: dict[str, int] = {
            market: 0 for market in released_at_by_market
        }
        trigger_at: dict[str, datetime] = {}
        base_threshold = int(
            getattr(self.config.risk, "max_consecutive_losses", 0) or 0
        )
        base_cooldown = int(
            getattr(
                self.config.risk,
                "circuit_breaker_cooldown_minutes",
                0,
            )
            or 0
        )
        for row in reversed(outcomes):
            market = str(row.get("market") or "").strip().lower()
            if not market:
                continue
            occurred_at = parse_datetime(str(row.get("logged_at") or ""))
            released_at = released_at_by_market.get(market)
            if (
                occurred_at is not None
                and released_at is not None
                and ensure_timezone(occurred_at) <= released_at
            ):
                continue
            active_since = trigger_at.get(market)
            if active_since is not None and occurred_at is not None:
                cooldown_minutes = self._market_risk_value(
                    market,
                    "circuit_breaker_cooldown_minutes",
                    base_cooldown,
                )
                if (
                    cooldown_minutes > 0
                    and ensure_timezone(occurred_at)
                    >= active_since + timedelta(minutes=cooldown_minutes)
                ):
                    streaks[market] = 0
                    trigger_at.pop(market, None)
            net_pnl_pct = float(row.get("net_pnl_pct") or 0.0)
            if net_pnl_pct >= 0:
                streaks[market] = 0
                trigger_at.pop(market, None)
                continue
            streaks[market] = streaks.get(market, 0) + 1
            threshold = self._market_risk_value(
                market,
                "max_consecutive_losses",
                base_threshold,
            )
            if threshold > 0 and streaks[market] == threshold:
                if occurred_at is not None:
                    trigger_at[market] = ensure_timezone(occurred_at)

        active_halts: dict[str, datetime] = {}
        for market, count in list(streaks.items()):
            threshold = self._market_risk_value(
                market,
                "max_consecutive_losses",
                base_threshold,
            )
            started_at = trigger_at.get(market)
            if threshold <= 0 or count < threshold or started_at is None:
                continue
            cooldown_minutes = self._market_risk_value(
                market,
                "circuit_breaker_cooldown_minutes",
                base_cooldown,
            )
            if (
                cooldown_minutes > 0
                and current
                >= started_at + timedelta(minutes=cooldown_minutes)
            ):
                streaks[market] = 0
                continue
            active_halts[market] = started_at
        for market, (count, started_at) in persisted_halts.items():
            streaks[market] = count
            active_halts[market] = started_at
        return streaks, active_halts

    def _persisted_consecutive_breaker_state(
        self,
        current: datetime,
    ) -> tuple[dict[str, tuple[int, datetime]], dict[str, datetime]]:
        list_events = getattr(self.repository, "list_event_log", None)
        if not callable(list_events):
            return {}, {}
        latest: dict[str, tuple[datetime, str, int]] = {}
        latest_release: dict[str, datetime] = {}
        current_at = ensure_timezone(current)
        for event_type in ("cb_fired", "cb_released"):
            for row in list_events(event_type=event_type, limit=1000):
                detail = row.get("detail")
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except (TypeError, ValueError):
                        continue
                if not isinstance(detail, dict):
                    continue
                if str(detail.get("type") or "") != "consecutive":
                    continue
                market = str(detail.get("market") or "").strip().lower()
                occurred_at = parse_datetime(str(row.get("logged_at") or ""))
                if not market or occurred_at is None:
                    continue
                timestamp = ensure_timezone(occurred_at)
                if timestamp > current_at:
                    continue
                if event_type == "cb_released":
                    previous_release = latest_release.get(market)
                    if previous_release is None or timestamp > previous_release:
                        latest_release[market] = timestamp
                previous = latest.get(market)
                if previous is None or timestamp > previous[0]:
                    latest[market] = (
                        timestamp,
                        event_type,
                        max(0, int(detail.get("consecutive_losses") or 0)),
                    )

        active: dict[str, tuple[int, datetime]] = {}
        base_cooldown = int(
            getattr(
                self.config.risk,
                "circuit_breaker_cooldown_minutes",
                0,
            )
            or 0
        )
        for market, (started_at, event_type, count) in latest.items():
            if event_type != "cb_fired":
                continue
            cooldown_minutes = self._market_risk_value(
                market,
                "circuit_breaker_cooldown_minutes",
                base_cooldown,
            )
            if (
                cooldown_minutes > 0
                and current_at
                >= started_at + timedelta(minutes=cooldown_minutes)
            ):
                continue
            active[market] = (count, started_at)
        return active, latest_release

    async def _send_circuit_breaker_notification(self, message: str) -> None:
        notifier = getattr(self, "notifier", None)
        if notifier is None or not getattr(notifier, "enabled", True):
            return
        await notifier.send(message)

    def _on_realised(
        self,
        *,
        market: str,
        net_pnl_krw: float,
        net_pnl_pct: float,
        include_session_pnl: bool = True,
    ) -> None:
        cb = self._get_circuit_breaker()
        cb.on_realised(
            market=market,
            realized_pnl_krw=net_pnl_krw,
            pnl_pct=net_pnl_pct,
            include_session_pnl=include_session_pnl,
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
            symbol_loss_streak=getattr(self, "_symbol_loss_streak", {}),
            recent_trade_count_by_market=getattr(
                self,
                "_recent_trade_count_by_market",
                {},
            ),
            recent_cycle_count_by_market=getattr(
                self,
                "_recent_cycle_count_by_market",
                {},
            ),
            recent_order_reason_counts_by_market=getattr(
                self,
                "_recent_order_reason_counts_by_market",
                {},
            ),
            last_low_trade_frequency_alert_cycle_by_market=getattr(
                self,
                "_last_low_trade_frequency_alert_cycle_by_market",
                {},
            ),
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
        self._recent_trade_count_by_market = dict(
            snapshot["recent_trade_count_by_market"]
        )
        self._recent_cycle_count_by_market = dict(
            snapshot["recent_cycle_count_by_market"]
        )
        self._recent_order_reason_counts_by_market = {
            str(market): dict(counts)
            for market, counts in dict(
                snapshot["recent_order_reason_counts_by_market"]
            ).items()
        }
        self._rsi_blocked_count = int(snapshot["rsi_blocked_count"])
        self._last_low_trade_frequency_alert_cycle = int(
            snapshot["last_low_trade_frequency_alert_cycle"]
        )
        self._last_low_trade_frequency_alert_cycle_by_market = dict(
            snapshot["last_low_trade_frequency_alert_cycle_by_market"]
        )
        self._last_trend_filter_alert_cycle = int(snapshot["last_trend_filter_alert_cycle"])
        self._exit_cooldown = dict(snapshot["exit_cooldown"])
        self._no_orderable_retry = dict(snapshot["no_orderable_retry"])
        self._no_orderable_counts = dict(snapshot["no_orderable_counts"])
        self._symbol_loss_streak = dict(snapshot["symbol_loss_streak"])

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
        decision_reason: str = "",
        is_virtual: bool | None = None,
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
            decision_reason=decision_reason,
            is_virtual=is_virtual,
        )

    def _evaluate_entry_setup(
        self,
        signal_snapshot: MovingAverageSnapshot,
        code: str,
        market: str = "overseas",
        *,
        now: datetime | None = None,
    ):
        result, inverse_decision, entry_formula, etf_metadata = (
            self._entry_setup_for_policy(
                signal_snapshot,
                code,
                market,
                now=now,
            )
        )
        if self._is_inverse_symbol(market, code):
            raw_volume_ratio = (
                signal_snapshot.volume_last / signal_snapshot.volume_avg
                if signal_snapshot.volume_avg > 0
                else 0.0
            )
            volume_projection_multiplier = (
                signal_snapshot.volume_ratio / raw_volume_ratio
                if raw_volume_ratio > 0
                else 1.0
            )
            self._record_inverse_observation(
                event_type=(
                    "inverse_product_ready"
                    if result.ready
                    else "inverse_product_blocked"
                ),
                market=market,
                symbol=code,
                reason=result.reason,
                now=now or datetime.now(timezone.utc),
                detail={
                    "state": result.state,
                    "score": result.score,
                    "entry_formula": entry_formula,
                    "regime_reason": (
                        inverse_decision.reason
                        if inverse_decision is not None
                        else "inverse_policy_unavailable"
                    ),
                    "regime_eligible": (
                        inverse_decision.eligible
                        if inverse_decision is not None
                        else False
                    ),
                    "benchmark_return_pct": (
                        inverse_decision.benchmark_return_pct
                        if inverse_decision is not None
                        else None
                    ),
                    "etf_metadata": etf_metadata,
                    "price": signal_snapshot.price,
                    "spread_pct": signal_snapshot.spread_pct,
                    "volume_ratio": signal_snapshot.volume_ratio,
                    "volume_ratio_raw": raw_volume_ratio,
                    "volume_projection_multiplier": volume_projection_multiplier,
                    "intraday_momentum": signal_snapshot.intraday_momentum,
                    "intraday_bar_return": signal_snapshot.intraday_bar_return,
                    "intraday_trend_up": signal_snapshot.intraday_trend_up,
                    "minute_ma_fast": signal_snapshot.minute_ma_fast,
                    "minute_ma_slow": signal_snapshot.minute_ma_slow,
                    "breakout_distance_pct": (
                        signal_snapshot.breakout_distance_pct
                    ),
                    "rsi14": signal_snapshot.rsi14,
                },
            )
        return result

    def _inverse_entry_formula(self, market: str) -> str:
        policy = self._get_market_policy(market)
        if policy.auto_trade is None:
            raise RuntimeError(f"{market} market policy requires auto_trade configuration")
        return str(
            getattr(
                policy.auto_trade,
                "inverse_entry_formula",
                "strategy_consensus_v1",
            )
            or "strategy_consensus_v1"
        ).strip().lower()

    def _uses_dedicated_inverse_entry_formula(
        self,
        market: str,
        code: str,
    ) -> bool:
        return (
            self._is_inverse_symbol(market, code)
            and self._inverse_entry_formula(market)
            in _DEDICATED_INVERSE_ENTRY_FORMULAS
        )

    def _cached_domestic_inverse_etf_metadata(
        self,
        code: str,
    ) -> dict:
        cache = getattr(self, "_domestic_inverse_etf_cache", {}) or {}
        cached = cache.get(str(code).strip())
        if (
            isinstance(cached, tuple)
            and len(cached) == 2
            and isinstance(cached[1], dict)
        ):
            return dict(cached[1])
        return {}

    def _entry_setup_for_policy(
        self,
        signal_snapshot: MovingAverageSnapshot,
        code: str,
        market: str,
        *,
        now: datetime | None = None,
    ) -> tuple[
        EntrySetup,
        InverseRegimeDecision | None,
        str,
        dict,
    ]:
        policy = self._get_market_policy(market)
        if policy.auto_trade is None:
            raise RuntimeError(f"{market} market policy requires auto_trade configuration")
        observation_now = now or datetime.now(timezone.utc)
        inverse_decision = self._inverse_regime_decision(
            market,
            code,
            now=observation_now,
        )
        entry_formula = self._inverse_entry_formula(market)
        etf_metadata = (
            self._cached_domestic_inverse_etf_metadata(code)
            if normalize_market_name(market) == "domestic"
            and self._is_inverse_symbol(market, code)
            else {}
        )
        if self._is_inverse_symbol(market, code):
            if entry_formula in _DEDICATED_INVERSE_ENTRY_FORMULAS:
                result = evaluate_inverse_regime_trend_breakout_setup(
                    policy.auto_trade,
                    signal_snapshot,
                    regime_eligible=bool(
                        inverse_decision is not None
                        and inverse_decision.eligible
                    ),
                    benchmark_return_pct=(
                        inverse_decision.benchmark_return_pct
                        if inverse_decision is not None
                        else None
                    ),
                    etf_metadata=etf_metadata,
                )
                return (
                    result,
                    inverse_decision,
                    entry_formula,
                    etf_metadata,
                )
            if entry_formula != "strategy_consensus_v1":
                return (
                    EntrySetup(
                        False,
                        "inverse_entry_formula_unknown",
                        "WAIT",
                        "policy",
                    ),
                    inverse_decision,
                    entry_formula,
                    etf_metadata,
                )

        result = evaluate_entry_setup(
            policy.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=policy.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=policy.auto_trade.leveraged_etf_symbols,
            inverse_regime_eligible=(
                inverse_decision.eligible
                if inverse_decision is not None
                else None
            ),
        )
        return result, inverse_decision, entry_formula, etf_metadata

    def _derive_watch_state(
        self,
        signal_snapshot: MovingAverageSnapshot,
        code: str,
        market: str = "overseas",
    ) -> tuple[str, str]:
        if self._uses_dedicated_inverse_entry_formula(market, code):
            result, _, _, _ = self._entry_setup_for_policy(
                signal_snapshot,
                code,
                market,
            )
            return result.state, result.reason
        policy = self._get_market_policy(market)
        if policy.auto_trade is None:
            raise RuntimeError(
                f"{market} market policy requires auto_trade configuration"
            )
        inverse_decision = self._inverse_regime_decision(market, code)
        return derive_watch_state(
            policy.auto_trade,
            signal_snapshot,
            symbol=code,
            inverse_etf_symbols=policy.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=policy.auto_trade.leveraged_etf_symbols,
            inverse_regime_eligible=(
                inverse_decision.eligible
                if inverse_decision is not None
                else None
            ),
        )

    def _is_inverse_symbol(self, market: str, symbol: str) -> bool:
        policy = self._get_market_policy(market)
        if policy.auto_trade is None:
            return False
        inverse_symbols = {
            str(value).strip().upper()
            for value in policy.auto_trade.inverse_etf_symbols
            if str(value).strip()
        }
        return str(symbol).strip().upper() in inverse_symbols

    def _is_leveraged_symbol(self, market: str, symbol: str) -> bool:
        policy = self._get_market_policy(market)
        if policy.auto_trade is None:
            return False
        leveraged_symbols = {
            str(value).strip().upper()
            for value in policy.auto_trade.leveraged_etf_symbols
            if str(value).strip()
        }
        return str(symbol).strip().upper() in leveraged_symbols

    @staticmethod
    def _market_session_date(market: str, now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timezone_for_market = KST if normalize_market_name(market) == "domestic" else NEW_YORK
        return current.astimezone(timezone_for_market).date().isoformat()

    def _market_regime_context(
        self,
        market: str,
        *,
        now: datetime | None = None,
        inverse_benchmark_code: str = "",
    ) -> dict:
        market_key = normalize_market_name(market)
        inverse_code = str(inverse_benchmark_code).strip().upper()
        current = ensure_timezone(now or datetime.now(timezone.utc))
        expected_session_date = self._market_session_date(
            market_key,
            current,
        )
        unavailable = {
            "available": False,
            "market": market_key,
            "expected_session_date": expected_session_date,
        }
        if inverse_code:
            unavailable["benchmark_code"] = inverse_code
        repository = getattr(self, "repository", None)
        if repository is None:
            return {**unavailable, "reason": "repository_unavailable"}
        if inverse_code:
            regime = repository.get_inverse_benchmark_regime(
                market_key,
                inverse_code,
                expected_session_date,
            )
        else:
            regime = repository.get_market_regime(
                market_key,
                expected_session_date,
            )
        if regime is None:
            return {**unavailable, "reason": "same_session_regime_missing"}

        captured_at = parse_datetime(regime.get("captured_at"))
        observation_age_sec = None
        if captured_at is not None:
            captured_at = ensure_timezone(captured_at).astimezone(timezone.utc)
            observation_age_sec = max(
                0,
                int((current.astimezone(timezone.utc) - captured_at).total_seconds()),
            )
        observation_id = None
        observation_lookup = getattr(
            repository,
            (
                "get_inverse_benchmark_observation_at"
                if inverse_code
                else "get_market_regime_observation_at"
            ),
            None,
        )
        if callable(observation_lookup):
            if inverse_code:
                observation = observation_lookup(
                    market_key,
                    inverse_code,
                    current,
                    session_date=expected_session_date,
                )
            else:
                observation = observation_lookup(
                    market_key,
                    current,
                    session_date=expected_session_date,
                )
            if (
                observation is not None
                and str(observation.get("captured_at") or "")
                == str(regime.get("captured_at") or "")
            ):
                observation_id = int(observation.get("id") or 0) or None

        high_price = self._parse_optional_float(regime.get("high_price"))
        low_price = self._parse_optional_float(regime.get("low_price"))
        close_price = self._parse_optional_float(regime.get("close_price"))
        previous_close = self._parse_optional_float(
            regime.get("previous_close")
        )
        session_low_return_pct = None
        benchmark_rebound_from_low_pct = None
        session_range_position = None
        if (
            previous_close is not None
            and previous_close > 0
            and low_price is not None
            and low_price > 0
        ):
            session_low_return_pct = round(
                (low_price / previous_close - 1.0) * 100.0,
                8,
            )
            if close_price is not None and close_price > 0:
                benchmark_rebound_from_low_pct = round(
                    (close_price - low_price) / previous_close * 100.0,
                    8,
                )
        if (
            high_price is not None
            and low_price is not None
            and close_price is not None
            and high_price > low_price
        ):
            session_range_position = round(
                min(
                    1.0,
                    max(0.0, (close_price - low_price) / (high_price - low_price)),
                ),
                8,
            )

        return {
            "available": True,
            "market": market_key,
            "session_date": expected_session_date,
            "benchmark_code": str(regime.get("benchmark_code") or ""),
            "benchmark_name": str(regime.get("benchmark_name") or ""),
            "source": str(regime.get("source") or ""),
            "captured_at": regime.get("captured_at"),
            "observation_id": observation_id,
            "observation_age_sec": observation_age_sec,
            "is_final": int(regime.get("is_final") or 0),
            "open_price": regime.get("open_price"),
            "high_price": regime.get("high_price"),
            "low_price": regime.get("low_price"),
            "close_price": regime.get("close_price"),
            "previous_close": regime.get("previous_close"),
            "return_pct": regime.get("return_pct"),
            "volume": regime.get("volume"),
            "turnover": regime.get("turnover"),
            "volume_ratio_20": regime.get("volume_ratio_20"),
            "range_pct": regime.get("range_pct"),
            "range_ratio_20": regime.get("range_ratio_20"),
            "trend_regime": str(regime.get("trend_regime") or "unknown"),
            "activity_regime": str(
                regime.get("activity_regime") or "unknown"
            ),
            "volatility_regime": str(
                regime.get("volatility_regime") or "unknown"
            ),
            "regime_key": str(
                regime.get("regime_key")
                or "unknown|unknown|unknown"
            ),
            "sample_days": int(regime.get("sample_days") or 0),
            "calculation_version": str(
                regime.get("calculation_version") or ""
            ),
            "minutes_to_regular_close": minutes_until_regular_session_close(
                market_key,
                current,
            ),
            "session_low_return_pct": session_low_return_pct,
            "benchmark_rebound_from_low_pct": (
                benchmark_rebound_from_low_pct
            ),
            "session_range_position": session_range_position,
        }

    def _entry_sector_context(
        self,
        market: str,
        symbol: str,
        *,
        sector_name: str = "",
    ) -> dict[str, object]:
        market_key = normalize_market_name(market)
        if market_key == "domestic":
            return build_domestic_sector_context(sector_name)
        pool_rows = (
            getattr(self, "_manual_overseas_pool", None)
            or getattr(self, "_dynamic_overseas_pool", None)
            or []
        )
        return build_overseas_sector_context(symbol, pool_rows)

    def _inverse_regime_decision(
        self,
        market: str,
        symbol: str = "",
        *,
        now: datetime | None = None,
    ) -> InverseRegimeDecision | None:
        market_key = normalize_market_name(market)
        if symbol and not self._is_inverse_symbol(market_key, symbol):
            return None
        policy = self._get_market_policy(market_key)
        if policy.auto_trade is None:
            return None
        repository = getattr(self, "repository", None)
        symbol_key = str(symbol).strip().upper()
        benchmark_profile = (
            policy.inverse_benchmarks.get(symbol_key)
            if symbol_key
            else None
        )
        unavailable_reason = ""
        if benchmark_profile is not None:
            benchmark_context = {
                "benchmark_code": benchmark_profile.benchmark_code,
                "benchmark_name": benchmark_profile.benchmark_name,
                "source": benchmark_profile.source,
            }
            if not benchmark_profile.available:
                regime = benchmark_context
                unavailable_reason = (
                    benchmark_profile.unavailable_reason
                    or "inverse_exact_benchmark_unavailable"
                )
            else:
                regime = (
                    repository.get_inverse_benchmark_regime(
                        market_key,
                        benchmark_profile.benchmark_code,
                    )
                    if repository is not None
                    and hasattr(
                        repository,
                        "get_inverse_benchmark_regime",
                    )
                    else None
                )
                if regime is None:
                    regime = benchmark_context
                    unavailable_reason = "inverse_benchmark_regime_missing"
        elif symbol_key and policy.inverse_require_symbol_benchmark:
            regime = {}
            unavailable_reason = "inverse_symbol_benchmark_mapping_missing"
        else:
            regime = (
                repository.get_market_regime(market_key)
                if repository is not None
                else None
            )
        return evaluate_inverse_regime(
            policy.auto_trade,
            regime,
            expected_session_date=self._market_session_date(market_key, now),
            benchmark_unavailable_reason=unavailable_reason,
        )

    def _record_inverse_observation(
        self,
        *,
        event_type: str,
        market: str,
        reason: str,
        symbol: str = "",
        now: datetime | None = None,
        detail: dict | None = None,
    ) -> bool:
        market_key = normalize_market_name(market)
        symbol_key = str(symbol).strip().upper()
        expected_session_date = self._market_session_date(market_key, now)
        observation_version = str(
            (detail or {}).get("observation_version")
            or (detail or {}).get("entry_formula")
            or ""
        )
        observation_key = (
            expected_session_date,
            str(event_type),
            market_key,
            symbol_key,
            str(reason),
            observation_version,
        )
        observation_keys = getattr(self, "_inverse_observation_keys", None)
        if observation_keys is None:
            observation_keys = set()
            self._inverse_observation_keys = observation_keys
        if observation_key in observation_keys:
            return False
        repository = getattr(self, "repository", None)
        try:
            already_recorded = bool(
                repository is not None
                and hasattr(repository, "has_inverse_policy_observation")
                and repository.has_inverse_policy_observation(
                    event_type=event_type,
                    market=market_key,
                    symbol=symbol_key,
                    expected_session_date=expected_session_date,
                    reason=reason,
                    observation_version=observation_version,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[INVERSE] observation_lookup_failed market=%s symbol=%s error=%s",
                market_key,
                symbol_key,
                exc,
            )
            already_recorded = False
        observation_keys.add(observation_key)
        if already_recorded:
            return False
        payload = {
            **(detail or {}),
            "reason": str(reason),
            "expected_session_date": expected_session_date,
            "observation_version": observation_version,
        }
        try:
            self._save_event(
                event_type=event_type,
                market=market_key,
                symbol=symbol_key,
                detail=payload,
            )
        except Exception as exc:  # noqa: BLE001
            observation_keys.discard(observation_key)
            _logger.warning(
                "[INVERSE] observation_save_failed market=%s symbol=%s error=%s",
                market_key,
                symbol_key,
                exc,
            )
            return False
        return True

    def _observe_inverse_regime(
        self,
        market: str,
        *,
        market_open: bool,
        now: datetime | None = None,
    ) -> bool:
        market_key = normalize_market_name(market)
        try:
            decision = self._inverse_regime_decision(market_key, now=now)
        except Exception as exc:  # noqa: BLE001
            return self._record_inverse_observation(
                event_type="inverse_regime_observed",
                market=market_key,
                reason="inverse_policy_unavailable",
                now=now,
                detail={
                    "market_open": bool(market_open),
                    "error_type": type(exc).__name__,
                },
            )
        if decision is None:
            return self._record_inverse_observation(
                event_type="inverse_regime_observed",
                market=market_key,
                reason="inverse_policy_unavailable",
                now=now,
                detail={"market_open": bool(market_open)},
            )
        reason = decision.reason if market_open else "inverse_market_closed"
        return self._record_inverse_observation(
            event_type="inverse_regime_observed",
            market=market_key,
            reason=reason,
            now=now,
            detail={
                "market_open": bool(market_open),
                "regime_reason": decision.reason,
                "eligible": decision.eligible,
                "execution_mode": decision.execution_mode,
                "observed_session_date": decision.session_date,
                "benchmark_code": decision.benchmark_code,
                "benchmark": decision.benchmark_name,
                "benchmark_source": decision.benchmark_source,
                "benchmark_return_pct": decision.benchmark_return_pct,
                "regime_key": decision.regime_key,
            },
        )

    def _inverse_entry_block_reason(
        self,
        market: str,
        symbol: str,
        *,
        now: datetime | None = None,
    ) -> str:
        decision = self._inverse_regime_decision(market, symbol, now=now)
        if decision is None:
            return ""
        if not decision.eligible:
            return decision.reason
        if decision.execution_mode != "live":
            return "inverse_shadow_mode"
        if self._uses_dedicated_inverse_entry_formula(market, symbol):
            return "inverse_dedicated_live_unvalidated"
        return ""

    def _inverse_entry_size_multiplier(
        self,
        market: str,
        symbol: str,
    ) -> float:
        if not self._is_inverse_symbol(market, symbol):
            return 1.0
        policy = self._get_market_policy(market)
        configured_multiplier = getattr(
            policy.auto_trade,
            "inverse_slot_multiplier",
            0.25,
        )
        return min(
            1.0,
            max(
                0.0,
                0.25
                if configured_multiplier is None
                else float(configured_multiplier),
            ),
        )

    def _record_inverse_shadow_entry(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        price: float,
        signal_snapshot: MovingAverageSnapshot,
        strategy_flag: str,
        entry_by: str,
        entry_reason: str,
        now: datetime | None = None,
    ) -> bool:
        market_key = normalize_market_name(market)
        decision = self._inverse_regime_decision(
            market_key,
            symbol,
            now=now,
        )
        if (
            decision is None
            or not decision.eligible
            or decision.execution_mode != "shadow"
            or price <= 0
        ):
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        policy = self._get_market_policy(market_key)
        benchmark_profile = policy.inverse_benchmarks.get(
            str(symbol).strip().upper()
        )
        exact_benchmark_aligned = bool(
            benchmark_profile is not None
            and benchmark_profile.available
            and decision.benchmark_code
            == benchmark_profile.benchmark_code
            and decision.benchmark_source
            == benchmark_profile.source
        )
        spread_pct = max(0.0, float(signal_snapshot.spread_pct or 0.0))
        entry_price = float(price) * (1.0 + min(spread_pct, 0.05) / 2.0)
        commission_rate = (
            self._domestic_commission_rate()
            if market_key == "domestic"
            else self._overseas_commission_rate()
        )
        entry_market_regime = self._market_regime_context(
            market_key,
            now=current,
        )
        entry_inverse_benchmark_context = self._market_regime_context(
            market_key,
            now=current,
            inverse_benchmark_code=decision.benchmark_code,
        )
        inserted = self.repository.open_inverse_shadow_trade(
            opened_at=current.astimezone(timezone.utc).isoformat(),
            market=market_key,
            symbol=symbol,
            exchange_code=exchange_code,
            entry_session_date=decision.session_date,
            policy_id=policy.policy_id,
            entry_price=entry_price,
            entry_spread_pct=spread_pct,
            commission_rate=commission_rate,
            benchmark_name=decision.benchmark_name,
            benchmark_return_pct=decision.benchmark_return_pct,
            benchmark_regime_key=decision.regime_key,
            entry_reason=entry_reason,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            context={
                "signal_snapshot": asdict(signal_snapshot),
                "reference_price": float(price),
                "execution_mode": "shadow",
                "entry_formula": self._inverse_entry_formula(market_key),
                "benchmark_alignment_version": (
                    INVERSE_BENCHMARK_ALIGNMENT_VERSION
                    if exact_benchmark_aligned
                    else ""
                ),
                "inverse_benchmark": asdict(decision),
                "entry_inverse_benchmark_context": (
                    entry_inverse_benchmark_context
                ),
                "inverse_benchmark_profile": (
                    asdict(benchmark_profile)
                    if benchmark_profile is not None
                    else {}
                ),
                "entry_market_regime": entry_market_regime,
                "inverse_exit_policy": {
                    "take_profit_pct": float(
                        getattr(
                            policy.auto_trade,
                            "inverse_take_profit_pct",
                            0.025,
                        )
                        or 0.025
                    ),
                    "stop_loss_pct": float(
                        getattr(
                            policy.auto_trade,
                            "inverse_stop_loss_pct",
                            0.008,
                        )
                        or 0.008
                    ),
                    "trailing_activation_net_pct": float(
                        getattr(
                            policy.auto_trade,
                            "inverse_trailing_activation_net_pct",
                            0.0,
                        )
                        or 0.0
                    ),
                    "trailing_drawdown_pct": float(
                        getattr(
                            policy.auto_trade,
                            "inverse_trailing_drawdown_pct",
                            0.0,
                        )
                        or 0.0
                    ),
                },
                "etf_metadata": (
                    self._cached_domestic_inverse_etf_metadata(symbol)
                    if market_key == "domestic"
                    else {}
                ),
            },
        )
        if inserted:
            self._save_event(
                event_type="inverse_shadow_opened",
                market=market_key,
                symbol=symbol,
                detail={
                    "policy_id": policy.policy_id,
                    "entry_price": round(entry_price, 8),
                    "benchmark_code": decision.benchmark_code,
                    "benchmark": decision.benchmark_name,
                    "benchmark_source": decision.benchmark_source,
                    "benchmark_return_pct": decision.benchmark_return_pct,
                    "entry_reason": entry_reason,
                    "minutes_to_regular_close": (
                        entry_inverse_benchmark_context.get(
                            "minutes_to_regular_close"
                        )
                    ),
                    "benchmark_rebound_from_low_pct": (
                        entry_inverse_benchmark_context.get(
                            "benchmark_rebound_from_low_pct"
                        )
                    ),
                    "session_range_position": (
                        entry_inverse_benchmark_context.get(
                            "session_range_position"
                        )
                    ),
                },
            )
        return inserted

    def _update_inverse_shadow_trade(
        self,
        *,
        market: str,
        symbol: str,
        price: float,
        signal_snapshot: MovingAverageSnapshot | None,
        now: datetime | None = None,
    ) -> str:
        market_key = normalize_market_name(market)
        if not self._is_inverse_symbol(market_key, symbol) or price <= 0:
            return ""
        trade = self.repository.get_open_inverse_shadow_trade(
            market_key,
            symbol,
        )
        if trade is None:
            return ""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        policy = self._get_market_policy(market_key).auto_trade
        spread_pct = (
            max(0.0, float(signal_snapshot.spread_pct or 0.0))
            if signal_snapshot is not None
            else max(0.0, float(trade.get("entry_spread_pct") or 0.0))
        )
        exit_price = float(price) * (1.0 - min(spread_pct, 0.05) / 2.0)
        entry_price = float(trade.get("entry_price") or 0.0)
        if entry_price <= 0 or exit_price <= 0:
            return ""
        commission_rate = max(
            0.0,
            float(trade.get("commission_rate") or 0.0),
        )
        gross_pnl_pct = (exit_price - entry_price) / entry_price
        entry_cost = entry_price * (1.0 + commission_rate)
        exit_proceeds = exit_price * (1.0 - commission_rate)
        net_pnl_pct = (
            (exit_proceeds - entry_cost) / entry_cost
            if entry_cost > 0
            else gross_pnl_pct
        )
        hold_cycles = int(trade.get("hold_cycles") or 0) + 1
        peak_price = max(float(trade.get("peak_price") or entry_price), exit_price)
        trough_price = min(
            float(trade.get("trough_price") or entry_price),
            exit_price,
        )
        take_profit = max(
            0.0,
            float(getattr(policy, "inverse_take_profit_pct", 0.025) or 0.025),
        )
        stop_loss = max(
            0.0,
            float(getattr(policy, "inverse_stop_loss_pct", 0.008) or 0.008),
        )
        hard_stop = max(
            stop_loss,
            float(
                getattr(policy, "inverse_hard_stop_loss_pct", 0.012)
                or 0.012
            ),
        )
        max_hold_cycles = max(
            1,
            int(getattr(policy, "inverse_max_hold_cycles", 48) or 48),
        )
        trailing_activation_net = max(
            0.0,
            float(
                getattr(
                    policy,
                    "inverse_trailing_activation_net_pct",
                    0.0,
                )
                or 0.0
            ),
        )
        trailing_drawdown = max(
            0.0,
            float(
                getattr(
                    policy,
                    "inverse_trailing_drawdown_pct",
                    0.0,
                )
                or 0.0
            ),
        )
        peak_exit_proceeds = peak_price * (1.0 - commission_rate)
        peak_net_pnl_pct = (
            (peak_exit_proceeds - entry_cost) / entry_cost
            if entry_cost > 0
            else (peak_price - entry_price) / entry_price
        )
        drawdown_from_peak = (
            (exit_price - peak_price) / peak_price if peak_price > 0 else 0.0
        )
        trailing_profit_armed = bool(
            trailing_activation_net > 0
            and trailing_drawdown > 0
            and peak_net_pnl_pct >= trailing_activation_net
        )
        exit_reason = ""
        exit_benchmark_decision: InverseRegimeDecision | None = None
        if net_pnl_pct <= -hard_stop:
            exit_reason = "inverse_hard_stop"
        elif net_pnl_pct <= -stop_loss:
            exit_reason = "inverse_stop_loss"
        elif net_pnl_pct >= take_profit:
            exit_reason = "inverse_take_profit"
        elif (
            trailing_profit_armed
            and drawdown_from_peak <= -trailing_drawdown
        ):
            exit_reason = "inverse_trailing_profit_lock"
        elif (
            self._market_session_date(market_key, current)
            != str(trade.get("entry_session_date") or "")
        ):
            exit_reason = "inverse_session_rollover"
        elif hold_cycles >= max_hold_cycles:
            exit_reason = "inverse_time_exit"
        else:
            exit_benchmark_decision = self._inverse_regime_decision(
                market_key,
                symbol,
                now=current,
            )
            if (
                exit_benchmark_decision is not None
                and exit_benchmark_decision.session_date
                == str(trade.get("entry_session_date") or "")
                and exit_benchmark_decision.benchmark_return_pct is not None
                and exit_benchmark_decision.benchmark_return_pct > -0.3
            ):
                exit_reason = "inverse_benchmark_recovered"

        now_iso = current.astimezone(timezone.utc).isoformat()
        updated_context = None
        if exit_reason:
            if exit_benchmark_decision is None:
                exit_benchmark_decision = self._inverse_regime_decision(
                    market_key,
                    symbol,
                    now=current,
                )
            existing_context = trade.get("context_json")
            updated_context = (
                dict(existing_context)
                if isinstance(existing_context, dict)
                else {}
            )
            updated_context["exit_market_regime"] = self._market_regime_context(
                market_key,
                now=current,
            )
            updated_context["exit_inverse_benchmark"] = (
                asdict(exit_benchmark_decision)
                if exit_benchmark_decision is not None
                else {}
            )
            if (
                exit_benchmark_decision is not None
                and exit_benchmark_decision.benchmark_code
            ):
                updated_context["exit_inverse_benchmark_context"] = (
                    self._market_regime_context(
                        market_key,
                        now=current,
                        inverse_benchmark_code=(
                            exit_benchmark_decision.benchmark_code
                        ),
                    )
                )
            if signal_snapshot is not None:
                updated_context["exit_signal_snapshot"] = asdict(
                    signal_snapshot
                )
            updated_context["inverse_trailing_state"] = {
                "activation_net_pct": trailing_activation_net,
                "drawdown_limit_pct": trailing_drawdown,
                "peak_net_pnl_pct": peak_net_pnl_pct,
                "drawdown_from_peak": drawdown_from_peak,
                "armed": trailing_profit_armed,
            }
        updated = self.repository.update_inverse_shadow_trade(
            int(trade["id"]),
            updated_at=now_iso,
            hold_cycles=hold_cycles,
            peak_price=peak_price,
            trough_price=trough_price,
            last_price=exit_price,
            closed_at=now_iso if exit_reason else None,
            exit_price=exit_price if exit_reason else None,
            gross_pnl_pct=gross_pnl_pct if exit_reason else None,
            net_pnl_pct=net_pnl_pct if exit_reason else None,
            exit_reason=exit_reason,
            context=updated_context,
        )
        if updated and exit_reason:
            self._save_event(
                event_type="inverse_shadow_closed",
                market=market_key,
                symbol=symbol,
                detail={
                    "exit_reason": exit_reason,
                    "entry_price": round(entry_price, 8),
                    "exit_price": round(exit_price, 8),
                    "gross_pnl_pct": round(gross_pnl_pct, 8),
                    "net_pnl_pct": round(net_pnl_pct, 8),
                    "peak_net_pnl_pct": round(peak_net_pnl_pct, 8),
                    "drawdown_from_peak": round(drawdown_from_peak, 8),
                    "trailing_profit_armed": trailing_profit_armed,
                    "hold_cycles": hold_cycles,
                },
            )
        return exit_reason

    def _open_inverse_shadow_symbols(
        self,
        market: str,
    ) -> set[str]:
        market_key = normalize_market_name(market)
        list_open = getattr(
            getattr(self, "repository", None),
            "list_open_inverse_shadow_trades",
            None,
        )
        if not callable(list_open):
            return set()
        try:
            rows = list_open(market=market_key)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[INVERSE] open_shadow_lookup_failed market=%s error=%s",
                market_key,
                exc,
            )
            return set()
        return {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }

    def _active_inverse_symbols(
        self,
        market: str,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        market_key = normalize_market_name(market)
        open_symbols = sorted(self._open_inverse_shadow_symbols(market_key))
        policy = self._get_market_policy(market_key)
        if policy.auto_trade is None:
            return open_symbols
        configured_symbols = [
            str(value).strip().upper()
            for value in policy.auto_trade.inverse_etf_symbols
            if str(value).strip()
        ]
        symbols: list[str] = []
        notice_keys = getattr(self, "_inverse_regime_notice_keys", None)
        if notice_keys is None:
            notice_keys = set()
            self._inverse_regime_notice_keys = notice_keys
        for symbol in dict.fromkeys(configured_symbols):
            decision = self._inverse_regime_decision(
                market_key,
                symbol,
                now=now,
            )
            if decision is None:
                continue
            self._record_inverse_observation(
                event_type="inverse_symbol_regime_observed",
                market=market_key,
                symbol=symbol,
                reason=decision.reason,
                now=now,
                detail={
                    "observation_version": "symbol_benchmark_v1",
                    "eligible": decision.eligible,
                    "execution_mode": decision.execution_mode,
                    "benchmark_code": decision.benchmark_code,
                    "benchmark_name": decision.benchmark_name,
                    "benchmark_source": decision.benchmark_source,
                    "benchmark_return_pct": decision.benchmark_return_pct,
                    "regime_key": decision.regime_key,
                },
            )
            if not decision.eligible:
                continue
            symbols.append(symbol)
            notice_key = (
                market_key,
                symbol,
                decision.session_date,
                decision.execution_mode,
                decision.benchmark_code,
            )
            if notice_key not in notice_keys:
                notice_keys.add(notice_key)
                self._save_event(
                    event_type="inverse_regime_active",
                    market=market_key,
                    symbol=symbol,
                    detail={
                        "mode": decision.execution_mode,
                        "benchmark_code": decision.benchmark_code,
                        "benchmark": decision.benchmark_name,
                        "benchmark_source": decision.benchmark_source,
                        "benchmark_return_pct": (
                            decision.benchmark_return_pct
                        ),
                        "regime_key": decision.regime_key,
                        "live_orders_enabled": (
                            decision.execution_mode == "live"
                        ),
                    },
                )
        symbols = list(dict.fromkeys([*symbols, *open_symbols]))
        return symbols

    def _get_market_policy_registry(self) -> MarketPolicyRegistry:
        registry = getattr(self, "market_policy_registry", None)
        if registry is None:
            registry = MarketPolicyRegistry(self.config)
            self.market_policy_registry = registry
        return registry

    def _get_market_policy(self, market: str) -> MomentumMarketPolicy:
        return self._get_market_policy_registry().for_market(market)

    def _effective_corporate_action(
        self,
        market: str,
        symbol: str,
        *,
        now: datetime | None = None,
    ) -> CorporateActionDefinition | None:
        if not hasattr(self, "config"):
            return None
        market_key = normalize_market_name(market)
        action = self._get_market_policy(market_key).corporate_actions.get(
            symbol.strip().upper()
        )
        if action is None or action.status != "effective":
            return None
        current = ensure_timezone(now or datetime.now(timezone.utc))
        local_timezone = KST if market_key == "domestic" else NEW_YORK
        if current.astimezone(local_timezone).date() < date.fromisoformat(
            action.effective_date
        ):
            return None
        return action

    @staticmethod
    def _corporate_action_snapshot(
        action: CorporateActionDefinition,
    ) -> dict:
        return asdict(action)

    def _fresh_overseas_real_quantities(
        self,
    ) -> tuple[dict[str, int], set[str]] | None:
        cache = getattr(self, "_overseas_balance_cache", {})
        cycle = int(getattr(self, "_cycle_count", 0) or 0)
        data = cache.get("data")
        if (
            cache.get("cycle") != cycle
            or not isinstance(data, dict)
            or not data
        ):
            return None

        quantities_by_key: dict[tuple[str, str], int] = {}
        covered_exchanges: set[str] = set()
        for requested_exchange, balance in data.items():
            exchange_key = str(requested_exchange or "").strip().upper()
            if exchange_key:
                covered_exchanges.add(exchange_key)
            if not isinstance(balance, dict):
                continue
            for row in balance.get("positions", []):
                quantity = int(parse_kis_number(row.get("ovrs_cblc_qty")))
                if quantity <= 0:
                    continue
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                if not symbol:
                    continue
                exchange_code = (
                    str(row.get("ovrs_excg_cd", "")).strip().upper()
                    or exchange_key
                )
                key = (symbol, exchange_code)
                quantities_by_key[key] = max(
                    quantities_by_key.get(key, 0),
                    quantity,
                )

        quantities_by_symbol: dict[str, int] = {}
        for (symbol, _), quantity in quantities_by_key.items():
            quantities_by_symbol[symbol] = (
                quantities_by_symbol.get(symbol, 0) + quantity
            )
        return quantities_by_symbol, covered_exchanges

    def _record_corporate_action_notice_once(
        self,
        *,
        notice_type: str,
        symbol: str,
        action: CorporateActionDefinition,
        event_type: str,
        detail: dict,
    ) -> bool:
        keys = getattr(self, "_corporate_action_notice_keys", None)
        if keys is None:
            keys = set()
            self._corporate_action_notice_keys = keys
        key = (notice_type, symbol.strip().upper(), action.effective_date)
        if key in keys:
            return False
        keys.add(key)
        self._save_event(
            event_type=event_type,
            market="overseas",
            symbol=symbol,
            detail=detail,
        )
        return True

    async def _send_corporate_action_notification(self, message: str) -> None:
        notifier = getattr(self, "notifier", None)
        if notifier is None or not getattr(notifier, "enabled", True):
            return
        try:
            await notifier.send(message)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "corporate_action_notification_failed",
                exc_info=True,
            )

    def _clear_corporate_action_symbol_state(
        self,
        *,
        market: str,
        symbol: str,
        updated_at: str,
    ) -> None:
        market_key = normalize_market_name(market)
        symbol_key = symbol.strip().upper()
        prefixed_key = f"{market_key}:{symbol_key}"
        for attribute, keys in (
            ("_signal_cache", (symbol_key,)),
            ("_signal_cache_updated_at", (symbol_key,)),
            ("_overseas_signal_failures", (symbol_key,)),
            ("_overseas_signal_suppressed_until", (symbol_key,)),
            ("_overseas_signal_unavailable_details", (symbol_key,)),
            ("_wait_cycles", (prefixed_key, symbol_key)),
            ("_exit_cooldown", (prefixed_key, symbol_key)),
            ("_no_orderable_retry", (prefixed_key, symbol_key)),
            ("_exit_price_shock_guard", (prefixed_key, symbol_key)),
            ("_stop_loss_confirm_guard", (prefixed_key, symbol_key)),
            ("_cycle_exit_reference_prices", (prefixed_key, symbol_key)),
        ):
            mapping = getattr(self, attribute, None)
            if not isinstance(mapping, dict):
                continue
            for key in keys:
                mapping.pop(key, None)
        persisted = getattr(self, "_persisted_symbol_state", None)
        if isinstance(persisted, dict):
            persisted.pop((market_key, symbol_key), None)
        last_held = getattr(self, "_last_held_symbols", None)
        if isinstance(last_held, set):
            last_held.discard(symbol_key)
        repository = getattr(self, "repository", None)
        clear_state = getattr(
            repository,
            "clear_lab_symbol_position_state",
            None,
        )
        if callable(clear_state):
            clear_state(
                market=market_key,
                symbol=symbol_key,
                note="corporate_action_cash_settlement",
                updated_at=updated_at,
            )
        self._reset_strategy_position(symbol_key, market_key)

    async def _reconcile_effective_overseas_corporate_actions(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        policy = self._get_market_policy("overseas")
        if not policy.corporate_actions:
            return []
        current = ensure_timezone(now or datetime.now(timezone.utc))
        actions = {
            symbol: action
            for symbol, action in policy.corporate_actions.items()
            if self._effective_corporate_action(
                "overseas",
                symbol,
                now=current,
            )
            is not None
        }
        if not actions:
            return []

        fresh_balances = self._fresh_overseas_real_quantities()
        if fresh_balances is None:
            for symbol, action in actions.items():
                virtual_position = self.virtual_trades.get_position(
                    "overseas",
                    symbol,
                )
                if virtual_position is None:
                    continue
                self._record_corporate_action_notice_once(
                    notice_type="deferred_stale_balance",
                    symbol=symbol,
                    action=action,
                    event_type="corporate_action_reconcile_deferred",
                    detail={
                        "reason": "fresh_broker_balance_required",
                        "action": self._corporate_action_snapshot(action),
                        "virtual_qty": virtual_position.qty,
                    },
                )
            return []

        real_quantities, covered_exchanges = fresh_balances
        settled_symbols: list[str] = []
        for symbol, action in actions.items():
            real_qty = int(real_quantities.get(symbol, 0) or 0)
            virtual_position = self.virtual_trades.get_position(
                "overseas",
                symbol,
            )
            if real_qty > 0:
                is_new_notice = self._record_corporate_action_notice_once(
                    notice_type="real_position_review",
                    symbol=symbol,
                    action=action,
                    event_type="corporate_action_real_position_review_required",
                    detail={
                        "reason": "broker_cash_settlement_requires_reconciliation",
                        "real_qty": real_qty,
                        "virtual_qty": (
                            0
                            if virtual_position is None
                            else virtual_position.qty
                        ),
                        "auto_settled": False,
                        "action": self._corporate_action_snapshot(action),
                    },
                )
                if is_new_notice:
                    await self._send_corporate_action_notification(
                        "\n".join(
                            [
                                "[KIS] 미국장 기업행동 실보유 검토 필요",
                                (
                                    f"종목={symbol} 실보유={real_qty}주 "
                                    f"유형={action.action_type}"
                                ),
                                (
                                    f"공식 현금대가=${action.cash_consideration:.2f} "
                                    f"효력일={action.effective_date}"
                                ),
                                "자동정산하지 않음: 증권사 현금입금과 잔고변경을 별도 대조해야 합니다.",
                            ]
                        )
                    )
                continue
            if virtual_position is None or virtual_position.qty <= 0:
                continue

            exchange_code = str(
                virtual_position.exchange_code or "NASD"
            ).strip().upper()
            if exchange_code not in covered_exchanges:
                self._record_corporate_action_notice_once(
                    notice_type="deferred_exchange_balance",
                    symbol=symbol,
                    action=action,
                    event_type="corporate_action_reconcile_deferred",
                    detail={
                        "reason": "listing_exchange_balance_not_refreshed",
                        "required_exchange": exchange_code,
                        "covered_exchanges": sorted(covered_exchanges),
                        "virtual_qty": virtual_position.qty,
                        "action": self._corporate_action_snapshot(action),
                    },
                )
                continue
            pending = self.repository.get_virtual_sell_pending(
                "overseas",
                symbol,
            )
            if pending is not None and int(pending.get("qty", 0) or 0) > 0:
                self._record_corporate_action_notice_once(
                    notice_type="pending_sell_review",
                    symbol=symbol,
                    action=action,
                    event_type="corporate_action_pending_sell_review_required",
                    detail={
                        "reason": "existing_virtual_sell_pending",
                        "pending_qty": int(pending.get("qty", 0) or 0),
                        "virtual_qty": virtual_position.qty,
                        "auto_settled": False,
                        "action": self._corporate_action_snapshot(action),
                    },
                )
                continue

            created_at = format_kst(current) or current.isoformat()
            realized_pnl, realized_pnl_pct = self.virtual_trades.record_sell(
                market="overseas",
                symbol=symbol,
                exchange_code=exchange_code,
                qty=virtual_position.qty,
                fill_price=action.cash_consideration,
                currency=action.currency,
                session="corporate_action",
                reason="corporate_action_cash_settlement",
                created_at=created_at,
                excluded_from_performance=True,
                exclude_reason="corporate_action_cash_settlement",
            )
            self._clear_corporate_action_symbol_state(
                market="overseas",
                symbol=symbol,
                updated_at=created_at,
            )
            detail = {
                "action": self._corporate_action_snapshot(action),
                "qty": virtual_position.qty,
                "avg_price": round(virtual_position.avg_price, 8),
                "cash_consideration": action.cash_consideration,
                "gross_cash_value": round(
                    action.cash_consideration * virtual_position.qty,
                    8,
                ),
                "realized_pnl": round(realized_pnl, 8),
                "realized_pnl_pct": round(realized_pnl_pct, 8),
                "broker_real_qty": 0,
                "performance_excluded": True,
                "exclude_reason": "corporate_action_cash_settlement",
                "auto_settled": True,
            }
            self._save_event(
                event_type="virtual_corporate_action_settled",
                market="overseas",
                symbol=symbol,
                detail=detail,
            )
            settled_symbols.append(symbol)
            await self._send_corporate_action_notification(
                "\n".join(
                    [
                        "[KIS] 미국장 기업행동 가상포지션 정산",
                        (
                            f"종목={symbol} 수량={virtual_position.qty}주 "
                            f"평단=${virtual_position.avg_price:.2f}"
                        ),
                        (
                            f"공식 현금대가=${action.cash_consideration:.2f} "
                            f"손익=${realized_pnl:+.2f}"
                        ),
                        "전략매도 아님: 정책 성과 집계에서 제외했습니다.",
                    ]
                )
            )
        return settled_symbols

    def _effective_virtual_corporate_action_symbols(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        manager = getattr(self, "virtual_trades", None)
        if manager is None:
            return []
        return sorted(
            {
                position.symbol.strip().upper()
                for position in manager.list_positions("overseas")
                if (
                    position.qty > 0
                    and self._effective_corporate_action(
                        "overseas",
                        position.symbol,
                        now=now,
                    )
                    is not None
                )
            }
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

    def _strategy_guard_min_final_sessions(self, market: str) -> int:
        try:
            return get_market_strategy_guard_policy(
                self.config,
                market,
            ).min_final_sessions
        except (AttributeError, KeyError, RuntimeError, ValueError):
            return 3

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

        guard_markets = {
            str(market).strip().lower()
            for market in getattr(config, "strategy_guard_markets", ["overseas"])
            if str(market).strip()
        }
        now = datetime.now(timezone.utc)
        evaluation_markets = sorted(
            guard_markets or {"domestic", "overseas"}
        )
        guard_policies = {
            market: get_market_strategy_guard_policy(self.config, market)
            for market in evaluation_markets
        }
        rows: list[dict] = []
        for market, guard_policy in guard_policies.items():
            rows.extend(
                repository.get_recent_strategy_guard_performance(
                    after_logged_at=(
                        now
                        - timedelta(hours=guard_policy.lookback_hours)
                    ).isoformat(),
                    cost_pct=guard_policy.fallback_cost_pct,
                    market=market,
                )
            )
        current_market_regimes = {
            market: self._market_regime_context(market, now=now)
            for market in sorted(guard_markets)
        }
        blocked: set[tuple[str, str]] = set()
        blocked_detail: list[dict] = []
        rolling_blocked: dict[tuple[str, str], dict] = {}
        for row in rows:
            market = str(row.get("market") or "").strip().lower()
            strategy = str(row.get("strategy_flag") or "").strip().upper()
            if not market or not strategy:
                continue
            guard_policy = guard_policies.get(market)
            if guard_policy is None:
                continue
            if (
                guard_policy.strategy_flags
                and strategy not in guard_policy.strategy_flags
            ):
                continue
            trade_count = int(row.get("trade_count") or 0)
            avg_net = float(row.get("avg_net_pnl_pct") or 0.0)
            weighted_net = float(
                row.get("capital_weighted_net_pnl_pct")
                if row.get("capital_weighted_net_pnl_pct") is not None
                else avg_net
            )
            if (
                trade_count < guard_policy.min_trades
                or (
                    avg_net > guard_policy.max_avg_net_pnl_pct
                    and weighted_net
                    > guard_policy.max_capital_weighted_net_pnl_pct
                )
            ):
                continue
            rolling_blocked[(market, strategy)] = row

        state_methods_available = all(
            callable(getattr(repository, method, None))
            for method in (
                "activate_strategy_guard_state",
                "list_active_strategy_guard_states",
                "release_strategy_guard_state",
                "count_final_market_regimes",
            )
        )
        active_states = (
            repository.list_active_strategy_guard_states()
            if state_methods_available
            else []
        )
        active_by_key = {
            (
                str(row.get("market") or "").strip().lower(),
                str(row.get("strategy_flag") or "").strip().upper(),
            ): row
            for row in active_states
        }
        newly_activated: set[tuple[str, str]] = set()
        for key, row in rolling_blocked.items():
            market, strategy = key
            state = active_by_key.get(key)
            if state is None and state_methods_available:
                last_trade_at = parse_datetime(row.get("last_trade_at"))
                activation_session_date = self._market_session_date(
                    market,
                    last_trade_at or now,
                )
                state = repository.activate_strategy_guard_state(
                    market=market,
                    strategy_flag=strategy,
                    activated_at=now,
                    activation_session_date=activation_session_date,
                    trigger_trade_count=int(row.get("trade_count") or 0),
                    trigger_avg_net_pnl_pct=float(
                        row.get("avg_net_pnl_pct") or 0.0
                    ),
                    trigger_capital_weighted_net_pnl_pct=float(
                        row.get("capital_weighted_net_pnl_pct")
                        if row.get("capital_weighted_net_pnl_pct") is not None
                        else row.get("avg_net_pnl_pct") or 0.0
                    ),
                )
                active_by_key[key] = state
                newly_activated.add(key)

            activation_session_date = str(
                (state or {}).get("activation_session_date")
                or self._market_session_date(market, now)
            )
            min_final_sessions = self._strategy_guard_min_final_sessions(market)
            final_sessions = (
                repository.count_final_market_regimes(
                    market=market,
                    start_date=activation_session_date,
                )
                if state_methods_available
                else 0
            )
            blocked.add(key)
            blocked_detail.append(
                {
                    "market": market,
                    "strategy_flag": strategy,
                    "trade_count": int(row.get("trade_count") or 0),
                    "avg_net_pnl_pct": round(
                        float(row.get("avg_net_pnl_pct") or 0.0),
                        6,
                    ),
                    "capital_weighted_net_pnl_pct": round(
                        float(
                            row.get("capital_weighted_net_pnl_pct")
                            if row.get("capital_weighted_net_pnl_pct")
                            is not None
                            else row.get("avg_net_pnl_pct") or 0.0
                        ),
                        6,
                    ),
                    "retention_source": "rolling_performance",
                    "activation_session_date": activation_session_date,
                    "final_sessions": final_sessions,
                    "min_final_sessions": min_final_sessions,
                    "lookback_hours": guard_policies[
                        market
                    ].lookback_hours,
                    "min_trades": guard_policies[market].min_trades,
                    "max_avg_net_pnl_pct": guard_policies[
                        market
                    ].max_avg_net_pnl_pct,
                    "max_capital_weighted_net_pnl_pct": guard_policies[
                        market
                    ].max_capital_weighted_net_pnl_pct,
                }
            )

        released_detail: list[dict] = []
        if state_methods_available:
            for key, state in active_by_key.items():
                if key in rolling_blocked:
                    continue
                market, strategy = key
                guard_policy = guard_policies.get(market)
                if guard_policy is None:
                    try:
                        guard_policy = get_market_strategy_guard_policy(
                            self.config,
                            market,
                        )
                    except ValueError:
                        guard_policy = None
                activation_session_date = str(
                    state.get("activation_session_date") or ""
                )
                recovery_trade_count = 0
                recovery_avg_net_pnl_pct = 0.0
                recovery_capital_weighted_net_pnl_pct = 0.0
                if (
                    (guard_markets and market not in guard_markets)
                    or guard_policy is None
                    or (
                        guard_policy.strategy_flags
                        and strategy not in guard_policy.strategy_flags
                    )
                ):
                    release_reason = "guard_scope_removed"
                    final_sessions = repository.count_final_market_regimes(
                        market=market,
                        start_date=activation_session_date,
                    )
                    should_release = True
                    min_final_sessions = self._strategy_guard_min_final_sessions(
                        market
                    )
                else:
                    min_final_sessions = self._strategy_guard_min_final_sessions(
                        market
                    )
                    final_sessions = repository.count_final_market_regimes(
                        market=market,
                        start_date=activation_session_date,
                    )
                    if guard_policy.release_requires_recovery:
                        recovery_rows = (
                            repository.get_recent_strategy_guard_performance(
                                after_logged_at=str(
                                    state.get("activated_at") or ""
                                ),
                                cost_pct=guard_policy.fallback_cost_pct,
                                market=market,
                            )
                        )
                        recovery_row = next(
                            (
                                row
                                for row in recovery_rows
                                if str(
                                    row.get("strategy_flag") or ""
                                ).strip().upper()
                                == strategy
                            ),
                            None,
                        )
                        recovery_trade_count = int(
                            (recovery_row or {}).get("trade_count") or 0
                        )
                        recovery_avg_net_pnl_pct = float(
                            (recovery_row or {}).get("avg_net_pnl_pct") or 0.0
                        )
                        recovery_capital_weighted_net_pnl_pct = float(
                            (recovery_row or {}).get(
                                "capital_weighted_net_pnl_pct"
                            )
                            if (recovery_row or {}).get(
                                "capital_weighted_net_pnl_pct"
                            )
                            is not None
                            else recovery_avg_net_pnl_pct
                        )
                    recovery_confirmed = (
                        not guard_policy.release_requires_recovery
                        or (
                            recovery_trade_count
                            >= guard_policy.release_min_trades
                            and recovery_avg_net_pnl_pct
                            > guard_policy.release_min_avg_net_pnl_pct
                            and recovery_capital_weighted_net_pnl_pct
                            > guard_policy.release_min_capital_weighted_net_pnl_pct
                        )
                    )
                    should_release = (
                        final_sessions >= min_final_sessions
                        and recovery_confirmed
                    )
                    release_reason = (
                        "post_activation_recovery_confirmed"
                        if guard_policy.release_requires_recovery
                        else "minimum_final_sessions_observed"
                    )

                if should_release:
                    if repository.release_strategy_guard_state(
                        market=market,
                        strategy_flag=strategy,
                        released_at=now,
                        release_reason=release_reason,
                    ):
                        released_detail.append(
                            {
                                "market": market,
                                "strategy_flag": strategy,
                                "activation_session_date": activation_session_date,
                                "final_sessions": final_sessions,
                                "min_final_sessions": min_final_sessions,
                                "reason": release_reason,
                                "recovery_trade_count": recovery_trade_count,
                                "recovery_avg_net_pnl_pct": round(
                                    recovery_avg_net_pnl_pct,
                                    6,
                                ),
                                "recovery_capital_weighted_net_pnl_pct": round(
                                    recovery_capital_weighted_net_pnl_pct,
                                    6,
                                ),
                            }
                        )
                    continue

                blocked.add(key)
                blocked_detail.append(
                    {
                        "market": market,
                        "strategy_flag": strategy,
                        "trade_count": int(
                            state.get("trigger_trade_count") or 0
                        ),
                        "avg_net_pnl_pct": round(
                            float(
                                state.get("trigger_avg_net_pnl_pct") or 0.0
                            ),
                            6,
                        ),
                        "capital_weighted_net_pnl_pct": round(
                            float(
                                state.get(
                                    "trigger_capital_weighted_net_pnl_pct"
                                )
                                if state.get(
                                    "trigger_capital_weighted_net_pnl_pct"
                                )
                                is not None
                                else state.get("trigger_avg_net_pnl_pct") or 0.0
                            ),
                            6,
                        ),
                        "retention_source": (
                            "recovery_evidence_hold"
                            if (
                                final_sessions >= min_final_sessions
                                and guard_policy.release_requires_recovery
                            )
                            else "minimum_final_session_hold"
                        ),
                        "activation_session_date": activation_session_date,
                        "final_sessions": final_sessions,
                        "min_final_sessions": min_final_sessions,
                        "release_requires_recovery": (
                            guard_policy.release_requires_recovery
                        ),
                        "recovery_trade_count": recovery_trade_count,
                        "recovery_min_trades": (
                            guard_policy.release_min_trades
                        ),
                        "recovery_avg_net_pnl_pct": round(
                            recovery_avg_net_pnl_pct,
                            6,
                        ),
                        "recovery_capital_weighted_net_pnl_pct": round(
                            recovery_capital_weighted_net_pnl_pct,
                            6,
                        ),
                        "recovery_min_avg_net_pnl_pct": (
                            guard_policy.release_min_avg_net_pnl_pct
                        ),
                        "recovery_min_capital_weighted_net_pnl_pct": (
                            guard_policy.release_min_capital_weighted_net_pnl_pct
                        ),
                    }
                )

        self._strategy_guard_cache = {
            "cycle_no": cycle_no,
            "blocked": blocked,
            "rows": rows,
            "blocked_detail": blocked_detail,
            "released_detail": released_detail,
            "current_market_regimes": current_market_regimes,
        }
        previous = getattr(self, "_last_strategy_guard_blocked_keys", set())
        should_emit_active = (
            bool(newly_activated)
            if state_methods_available
            else bool(blocked and blocked != previous)
        )
        if should_emit_active:
            self._save_event(
                event_type="strategy_guard_active",
                detail={
                    "market_policies": {
                        market: {
                            "lookback_hours": policy.lookback_hours,
                            "min_trades": policy.min_trades,
                            "max_avg_net_pnl_pct": (
                                policy.max_avg_net_pnl_pct
                            ),
                            "max_capital_weighted_net_pnl_pct": (
                                policy.max_capital_weighted_net_pnl_pct
                            ),
                            "strategy_flags": sorted(
                                policy.strategy_flags
                            ),
                            "min_final_sessions": (
                                policy.min_final_sessions
                            ),
                            "release_requires_recovery": (
                                policy.release_requires_recovery
                            ),
                            "release_min_trades": (
                                policy.release_min_trades
                            ),
                            "release_min_avg_net_pnl_pct": (
                                policy.release_min_avg_net_pnl_pct
                            ),
                            "release_min_capital_weighted_net_pnl_pct": (
                                policy.release_min_capital_weighted_net_pnl_pct
                            ),
                            "fallback_cost_pct": (
                                policy.fallback_cost_pct
                            ),
                        }
                        for market, policy in guard_policies.items()
                    },
                    "blocked": blocked_detail,
                    "current_market_regimes": current_market_regimes,
                },
            )
        for released in released_detail:
            self._save_event(
                event_type="strategy_guard_released",
                market=str(released["market"]),
                detail=released,
            )
        self._last_strategy_guard_blocked_keys = blocked
        return blocked

    def _entry_strategy_raw_block_reason(
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

    def _strategy_guard_probe_context(
        self,
        *,
        market: str,
        strategy_flag: str,
        block_reason: str = "",
        now: datetime | None = None,
    ) -> dict[str, object]:
        market_key = normalize_market_name(market)
        strategy = str(strategy_flag or "").strip().upper()
        original_block_reason = block_reason or self._entry_strategy_raw_block_reason(
            market=market_key,
            strategy_flag=strategy,
        )
        detail: dict[str, object] = {
            "admitted": False,
            "market": market_key,
            "strategy_flag": strategy,
            "guard_reason": original_block_reason,
        }
        if original_block_reason != "recent_strategy_underperformance":
            detail["reason"] = "unsupported_guard_reason"
            return detail

        policy = self._get_market_policy(market_key).auto_trade
        if policy is None or not bool(
            getattr(policy, "strategy_guard_probe_enabled", False)
        ):
            detail["reason"] = "probe_disabled"
            return detail
        configured_flags = {
            str(flag).strip().upper()
            for flag in getattr(
                policy,
                "strategy_guard_probe_strategy_flags",
                [],
            )
            if str(flag).strip()
        }
        if strategy not in configured_flags:
            detail["reason"] = "strategy_not_configured"
            return detail
        credentials = getattr(self.config, "credentials", object())
        if str(getattr(credentials, "env", "")).strip().lower() != "vps":
            detail["reason"] = "paper_environment_required"
            return detail

        max_entries = max(
            0,
            int(
                getattr(
                    policy,
                    "strategy_guard_probe_max_entries_per_session",
                    0,
                )
                or 0
            ),
        )
        if max_entries <= 0:
            detail["reason"] = "session_limit_disabled"
            return detail
        max_submissions = max(
            max_entries,
            int(
                getattr(
                    policy,
                    "strategy_guard_probe_max_submissions_per_session",
                    0,
                )
                or max_entries
            ),
        )

        current = ensure_timezone(now or datetime.now(timezone.utc))
        regime = self._market_regime_context(market_key, now=current)
        detail["entry_market_regime"] = regime
        if not bool(regime.get("available")):
            detail["reason"] = "same_session_regime_required"
            return detail
        regime_age = regime.get("observation_age_sec")
        max_age_sec = max(
            1,
            int(
                getattr(
                    policy,
                    "strategy_guard_probe_regime_max_age_sec",
                    600,
                )
                or 600
            ),
        )
        if regime_age is None or int(regime_age) > max_age_sec:
            detail["reason"] = "fresh_regime_required"
            detail["regime_max_age_sec"] = max_age_sec
            return detail
        benchmark_return_pct = self._parse_optional_float(regime.get("return_pct"))
        benchmark_floor_pct = float(
            getattr(
                policy,
                "strategy_guard_probe_benchmark_floor_pct",
                0.0,
            )
            or 0.0
        )
        detail["benchmark_floor_pct"] = benchmark_floor_pct
        if (
            benchmark_return_pct is None
            or benchmark_return_pct < benchmark_floor_pct
        ):
            detail["reason"] = "benchmark_floor_not_met"
            return detail

        session_date = self._market_session_date(market_key, current)
        repository = getattr(self, "repository", None)
        usage_reader = getattr(
            repository,
            "get_strategy_guard_probe_usage",
            None,
        )
        counter = getattr(
            repository,
            "count_strategy_guard_probe_submissions",
            None,
        )
        if callable(usage_reader):
            usage = usage_reader(
                market=market_key,
                session_date=session_date,
            )
        elif callable(counter):
            submitted_count = int(
                counter(market=market_key, session_date=session_date) or 0
            )
            usage = {
                "submission_attempts": submitted_count,
                "effective_entries": submitted_count,
                "filled_entries": 0,
                "open_entries": submitted_count,
                "virtual_entries": 0,
                "no_fill_finalized": 0,
            }
        else:
            detail["reason"] = "persistent_counter_unavailable"
            return detail
        submitted_count = int(usage.get("submission_attempts") or 0)
        effective_entries = int(usage.get("effective_entries") or 0)
        detail.update(
            {
                "session_date": session_date,
                "submitted_count": submitted_count,
                "submission_attempts": submitted_count,
                "effective_entries": effective_entries,
                "filled_entries": int(usage.get("filled_entries") or 0),
                "open_entries": int(usage.get("open_entries") or 0),
                "virtual_entries": int(usage.get("virtual_entries") or 0),
                "no_fill_finalized": int(usage.get("no_fill_finalized") or 0),
                "max_entries_per_session": max_entries,
                "max_submissions_per_session": max_submissions,
            }
        )
        if effective_entries >= max_entries:
            detail["reason"] = "session_limit_reached"
            return detail
        if submitted_count >= max_submissions:
            detail["reason"] = "submission_limit_reached"
            return detail

        detail["admitted"] = True
        detail["reason"] = "paper_probe_admitted"
        detail["slot_multiplier"] = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(
                        policy,
                        "strategy_guard_probe_slot_multiplier",
                        0.10,
                    )
                    or 0.0
                ),
            ),
        )
        return detail

    @staticmethod
    def _strategy_guard_probe_qty(qty: int, context: dict[str, object]) -> int:
        if qty <= 0 or not bool(context.get("admitted")):
            return qty
        multiplier = max(0.0, min(1.0, float(context.get("slot_multiplier") or 0.0)))
        if multiplier <= 0:
            return 0
        return max(1, int(qty * multiplier))

    def _record_strategy_guard_probe_submission(
        self,
        *,
        market: str,
        symbol: str,
        qty: int,
        context: dict[str, object],
        is_virtual: bool,
    ) -> None:
        self._save_event(
            event_type="strategy_guard_probe_submitted",
            market=market,
            symbol=symbol,
            detail={
                **context,
                "qty": qty,
                "is_virtual": is_virtual,
            },
        )

    def _entry_strategy_block_reason(
        self,
        *,
        market: str,
        strategy_flag: str,
    ) -> str:
        block_reason = self._entry_strategy_raw_block_reason(
            market=market,
            strategy_flag=strategy_flag,
        )
        if not block_reason:
            return ""
        probe = self._strategy_guard_probe_context(
            market=market,
            strategy_flag=strategy_flag,
            block_reason=block_reason,
        )
        return "" if bool(probe.get("admitted")) else block_reason

    def _entry_formula_block_reason(
        self,
        *,
        market: str,
        symbol: str,
        signal_snapshot: MovingAverageSnapshot | None,
        strategy_flag: str = "",
    ) -> str:
        if self._effective_corporate_action(market, symbol) is not None:
            return "corporate_action_effective"
        if not hasattr(self, "config"):
            return ""
        market_key = normalize_market_name(market)
        policy = self._get_market_policy(market_key)
        definition = policy.definition
        auto_trade = policy.auto_trade
        if auto_trade is None:
            return ""

        is_inverse = self._is_inverse_symbol(market_key, symbol)
        if not is_inverse:
            benchmark_floor_value = getattr(
                definition,
                "entry_benchmark_floor_pct",
                None,
            )
            require_market_regime = bool(
                getattr(
                    definition,
                    "entry_require_same_session_regime",
                    False,
                )
            ) or benchmark_floor_value is not None
            if require_market_regime:
                regime = self._market_regime_context(market_key)
                if not bool(regime.get("available")):
                    return "entry_market_regime_unavailable"
                age_sec = regime.get("observation_age_sec")
                max_age_sec = max(
                    1,
                    int(
                        getattr(
                            definition,
                            "entry_regime_max_age_sec",
                            600,
                        )
                    ),
                )
                if age_sec is None or int(age_sec) > max_age_sec:
                    return "entry_market_regime_stale"
                if benchmark_floor_value is not None:
                    benchmark_return_pct = self._parse_optional_float(
                        regime.get("return_pct")
                    )
                    if benchmark_return_pct is None:
                        return "entry_market_regime_unavailable"
                    if benchmark_return_pct < float(benchmark_floor_value):
                        return "entry_benchmark_below_floor"

            post_cb_reason, _ = self._post_cb_reentry_regime_gate(
                market_key,
            )
            if post_cb_reason:
                return post_cb_reason

            strategy = str(strategy_flag or "").strip().upper()
            confirmation_flags = {
                str(value).strip().upper()
                for value in getattr(
                    auto_trade,
                    "entry_confirmation_strategy_flags",
                    [],
                )
                if str(value).strip()
            }
            if strategy and strategy in confirmation_flags:
                if signal_snapshot is None:
                    return "strategy_confirmation_signal_unavailable"
                entry_setup = self._evaluate_entry_setup(
                    signal_snapshot,
                    symbol,
                    market_key,
                )
                if not entry_setup.ready:
                    return f"strategy_confirmation_{entry_setup.reason}"
        if signal_snapshot is None:
            return ""

        if (
            bool(
                getattr(
                    auto_trade,
                    "leveraged_require_dual_trend_confirmation",
                    False,
                )
            )
            and self._is_leveraged_symbol(market_key, symbol)
            and not (
                signal_snapshot.daily_trend_up
                and signal_snapshot.intraday_trend_up
            )
        ):
            return "leveraged_trend_unconfirmed"
        return ""

    def _post_cb_reentry_regime_gate(
        self,
        market: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict]:
        market_key = normalize_market_name(market)
        policy = self._get_market_policy(market_key)
        definition = policy.definition
        floor_value = getattr(
            definition,
            "post_cb_reentry_benchmark_floor_pct",
            None,
        )
        max_fires_value = getattr(
            definition,
            "post_cb_max_fires_per_session",
            None,
        )
        if floor_value is None and max_fires_value is None:
            return "", {"enabled": False, "market": market_key}

        current = ensure_timezone(now or datetime.now(timezone.utc))
        use_cache = now is None
        cycle_no = int(getattr(self, "_cycle_count", 0) or 0)
        cb = self._get_circuit_breaker()
        snapshot = cb.snapshot()
        released_by_market = snapshot.get(
            "last_cb_released_at_by_market",
            {},
        )
        released_at = (
            released_by_market.get(market_key)
            if isinstance(released_by_market, dict)
            else None
        )
        released_at_text = (
            ensure_timezone(released_at).isoformat()
            if isinstance(released_at, datetime)
            else None
        )
        cache = getattr(self, "_post_cb_reentry_gate_cache", {})
        if (
            use_cache
            and cache.get("cycle_no") == cycle_no
            and market_key in cache.get("markets", {})
        ):
            cached = dict(cache["markets"][market_key])
            if cached.get("released_at") == released_at_text:
                return str(cached.get("reason") or ""), cached

        floor_pct = None if floor_value is None else float(floor_value)
        max_fires = (
            None
            if max_fires_value is None
            else max(1, int(max_fires_value))
        )
        max_age_sec = max(
            1,
            int(
                getattr(
                    definition,
                    "post_cb_reentry_regime_max_age_sec",
                    600,
                )
                or 600
            ),
        )
        detail = {
            "enabled": True,
            "market": market_key,
            "reason": "",
            "benchmark_floor_pct": floor_pct,
            "regime_max_age_sec": max_age_sec,
            "max_fires_per_session": max_fires,
            "released_at": released_at_text,
        }
        if not isinstance(released_at, datetime):
            result = ("", detail)
        elif self._market_session_date(
            market_key,
            ensure_timezone(released_at),
        ) != self._market_session_date(market_key, current):
            result = ("", detail)
        else:
            breaker_session = (
                self._same_session_consecutive_breaker_fire_summary(
                    market_key,
                    now=current,
                )
            )
            detail["breaker_session"] = breaker_session
            session_loss_fire_count = int(
                breaker_session.get(
                    "session_loss_fire_count",
                    breaker_session["fire_count"],
                )
            )
            session_stop_exempted = bool(
                max_fires is not None
                and int(breaker_session["fire_count"]) >= max_fires
                and session_loss_fire_count < max_fires
            )
            detail["session_stop_exempted"] = session_stop_exempted
            if session_stop_exempted:
                exemption_key = (
                    market_key,
                    str(breaker_session.get("session_date") or ""),
                    tuple(breaker_session.get("event_ids") or []),
                )
                logged_exemptions = getattr(
                    self,
                    "_post_cb_session_stop_exemptions_logged",
                    set(),
                )
                if exemption_key not in logged_exemptions:
                    logged_exemptions.add(exemption_key)
                    self._post_cb_session_stop_exemptions_logged = (
                        logged_exemptions
                    )
                    self._save_event(
                        event_type="post_cb_cross_session_streak_exempted",
                        market=market_key,
                        detail={
                            "reason": (
                                "same_session_loss_streak_below_threshold"
                            ),
                            "max_fires_per_session": max_fires,
                            "breaker_session": breaker_session,
                        },
                    )
            if (
                max_fires is not None
                and session_loss_fire_count >= max_fires
            ):
                detail["reason"] = "post_cb_session_loss_limit_reached"
            elif floor_pct is not None:
                regime = self._market_regime_context(
                    market_key,
                    now=current,
                )
                detail["market_regime"] = regime
                age_sec = regime.get("observation_age_sec")
                benchmark_return_pct = self._parse_optional_float(
                    regime.get("return_pct")
                )
                if not bool(regime.get("available")):
                    detail["reason"] = "post_cb_regime_unavailable"
                elif age_sec is None or int(age_sec) > max_age_sec:
                    detail["reason"] = "post_cb_regime_stale"
                elif benchmark_return_pct is None:
                    detail["reason"] = "post_cb_regime_unavailable"
                elif benchmark_return_pct < floor_pct:
                    detail["reason"] = "post_cb_benchmark_not_recovered"
            result = (str(detail["reason"]), detail)

        if use_cache:
            if cache.get("cycle_no") != cycle_no:
                cache = {"cycle_no": cycle_no, "markets": {}}
            cache.setdefault("markets", {})[market_key] = dict(result[1])
            self._post_cb_reentry_gate_cache = cache
        return result

    def _same_session_consecutive_breaker_fire_summary(
        self,
        market: str,
        *,
        now: datetime | None = None,
    ) -> dict:
        market_key = normalize_market_name(market)
        current = ensure_timezone(now or datetime.now(timezone.utc))
        session_date = self._market_session_date(market_key, current)
        fire_events: list[tuple[datetime, int]] = []
        list_events = getattr(self.repository, "list_event_log", None)
        if callable(list_events):
            for row in list_events(event_type="cb_fired", limit=1000):
                detail = row.get("detail")
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except (TypeError, ValueError):
                        continue
                if not isinstance(detail, dict):
                    continue
                if str(detail.get("type") or "") != "consecutive":
                    continue
                if (
                    str(detail.get("market") or "").strip().lower()
                    != market_key
                ):
                    continue
                occurred_at = parse_datetime(str(row.get("logged_at") or ""))
                if occurred_at is None:
                    continue
                timestamp = ensure_timezone(occurred_at)
                if timestamp > current:
                    continue
                if (
                    self._market_session_date(market_key, timestamp)
                    != session_date
                ):
                    continue
                event_id = int(row.get("id") or 0)
                fire_events.append((timestamp, event_id))
        fire_events.sort(key=lambda item: (item[0], item[1]))

        base_threshold = int(
            getattr(self.config.risk, "max_consecutive_losses", 0) or 0
        )
        threshold = self._market_risk_value(
            market_key,
            "max_consecutive_losses",
            base_threshold,
        )
        session_outcomes: list[tuple[datetime, float]] = []
        outcomes_available = False
        outcome_reader = getattr(
            self.repository,
            "get_recent_confirmed_sell_risk_outcomes",
            None,
        )
        if callable(outcome_reader):
            try:
                outcomes = outcome_reader(limit=5000, cost_pct=0.005)
                outcomes_available = True
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "post_cb_session_loss_outcome_lookup_failed market=%s",
                    market_key,
                )
                outcomes = []
            for row in outcomes:
                if str(row.get("market") or "").strip().lower() != market_key:
                    continue
                if int(row.get("is_session_trade") or 0) != 1:
                    continue
                occurred_at = parse_datetime(str(row.get("logged_at") or ""))
                if occurred_at is None:
                    continue
                timestamp = ensure_timezone(occurred_at)
                if timestamp > current:
                    continue
                if self._market_session_date(market_key, timestamp) != session_date:
                    continue
                session_outcomes.append(
                    (timestamp, float(row.get("net_pnl_pct") or 0.0))
                )
        session_outcomes.sort(key=lambda item: item[0])

        fire_details: list[dict[str, object]] = []
        session_loss_fire_count = 0
        for fired_at, event_id in fire_events:
            streak = 0
            observed_count = 0
            for occurred_at, net_pnl_pct in session_outcomes:
                if occurred_at > fired_at:
                    break
                observed_count += 1
                streak = streak + 1 if net_pnl_pct < 0 else 0
            evidence_available = outcomes_available and observed_count > 0
            qualifies = (
                streak >= threshold
                if evidence_available and threshold > 0
                else True
            )
            if qualifies:
                session_loss_fire_count += 1
            fire_details.append(
                {
                    "event_id": event_id,
                    "fired_at": fired_at.isoformat(),
                    "same_session_outcome_count": observed_count,
                    "same_session_loss_streak": streak,
                    "threshold": threshold,
                    "qualifies_for_session_stop": qualifies,
                    "qualification_basis": (
                        "confirmed_session_outcomes"
                        if evidence_available
                        else "fail_closed_missing_session_outcomes"
                    ),
                }
            )

        fired_at_values = [item[0].isoformat() for item in fire_events]
        event_ids = [item[1] for item in fire_events if item[1] > 0]
        return {
            "market": market_key,
            "session_date": session_date,
            "fire_count": len(fire_events),
            "session_loss_fire_count": session_loss_fire_count,
            "cross_session_fire_count": (
                len(fire_events) - session_loss_fire_count
            ),
            "fired_at": fired_at_values,
            "event_ids": event_ids,
            "fire_details": fire_details,
        }

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
        execution_context: dict | None = None,
        replacement_for_order_no: str = "",
    ) -> dict | None:
        if (
            str(market).strip().lower() == "overseas"
            and not is_virtual
            and str(status).strip().upper()
            in {"SUBMITTED", "CANCELED", "CANCELLED"}
        ):
            self._invalidate_vps_open_overseas_order_snapshot()
        repository = getattr(self, "repository", None)
        if repository is None:
            return None
        broker_order_no = self._extract_broker_order_no(payload)
        created_at = datetime.now(timezone.utc).isoformat()
        event_payload = dict(payload or {})
        if execution_context is not None:
            event_payload["execution_context"] = execution_context
        if replacement_for_order_no:
            event_payload["replacement_for_order_no"] = replacement_for_order_no
        broker_event_id = repository.save_broker_order_event(
            created_at=created_at,
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
            payload=event_payload,
        )
        if (
            str(status).strip().upper() != "SUBMITTED"
            or is_virtual
            or str(order_kind).strip().lower() == "cancel"
            or not broker_order_no
        ):
            return None
        context = execution_context or {}
        return repository.save_broker_order_execution(
            broker_event_id=broker_event_id,
            created_at=created_at,
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            side=side,
            broker_order_no=broker_order_no,
            requested_qty=requested_qty,
            requested_price=requested_price,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            reason=reason,
            session_id=str(
                context.get("session_id")
                or getattr(self, "_session_id", "")
                or ""
            ),
            cycle_no=int(
                context.get("cycle_no")
                or getattr(self, "_cycle_count", 0)
                or 0
            ),
            is_session_trade=int(context.get("is_session_trade") or 0),
            entry_price=self._parse_optional_float(context.get("entry_price")),
            entry_time=str(context.get("entry_time") or "") or None,
            hold_duration_min=self._parse_optional_float(
                context.get("hold_duration_min")
            ),
            context=context,
            replacement_for_order_no=replacement_for_order_no,
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

    def _invalidate_vps_open_overseas_order_snapshot(self) -> None:
        self._vps_open_overseas_order_snapshot_key = None
        self._vps_open_overseas_order_snapshot = []

    @staticmethod
    def _overseas_session_date(now_utc: datetime | None = None) -> str:
        current = now_utc or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(NEW_YORK).strftime("%Y%m%d")

    async def _load_vps_open_overseas_order_snapshot(self) -> list[dict]:
        session_date = self._overseas_session_date()
        cache_key = (
            session_date,
            int(getattr(self, "_cycle_count", 0) or 0),
        )
        if (
            getattr(
                self,
                "_vps_open_overseas_order_snapshot_key",
                None,
            )
            == cache_key
        ):
            return list(
                getattr(
                    self,
                    "_vps_open_overseas_order_snapshot",
                    [],
                )
            )

        history = await self.client.get_overseas_order_history(
            symbol="",
            start_date=session_date,
            end_date=session_date,
            side_filter="00",
            fill_filter="00",
            exchange_code="",
            sort_sqn="DS",
            order_date="",
            order_branch_no="",
            order_no="",
            paginate=True,
            max_pages=10,
        )
        results: list[dict] = []
        for row in history.get("orders", []):
            row_symbol = str(
                row.get("pdno") or row.get("ovrs_pdno") or ""
            ).strip().upper()
            open_qty = parse_kis_number(row.get("nccs_qty"))
            if not row_symbol or open_qty <= 0:
                continue
            result = dict(row)
            result["symbol"] = row_symbol
            result["exchange_code"] = str(
                row.get("ovrs_excg_cd") or ""
            ).strip().upper()
            result["open_qty"] = open_qty
            result["order_no"] = str(row.get("odno") or "").strip()
            result["order_price"] = self._parse_float(
                row.get("ft_ord_unpr3")
            )
            result["created_at"] = (
                self._parse_overseas_order_history_timestamp(row)
            )
            results.append(result)
        results.sort(
            key=lambda item: item.get("created_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        self._vps_open_overseas_order_snapshot_key = cache_key
        self._vps_open_overseas_order_snapshot = results
        return list(results)

    async def _list_open_overseas_orders(
        self,
        *,
        symbol: str,
        exchange_code: str,
    ) -> list[dict]:
        env = str(getattr(self.config.credentials, "env", "vps") or "vps")
        try:
            if env == "prod":
                history = await self.client.get_overseas_open_orders(
                    exchange_code=exchange_code,
                    sort_sqn="DS",
                    paginate=True,
                    max_pages=10,
                )
            else:
                rows = await self._load_vps_open_overseas_order_snapshot()
                return [
                    row
                    for row in rows
                    if str(row.get("symbol") or "").strip().upper()
                    == symbol.upper()
                ]
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[ORDERS] 해외 미체결 조회 실패 - 주문 보류 (symbol=%s, error=%s)",
                symbol,
                exc,
            )
            self._save_event(
                event_type="maintenance_skip",
                market="overseas",
                symbol=symbol,
                detail={
                    "reason": "open_overseas_order_lookup_failed",
                    "error": str(exc)[:200],
                },
            )
            raise KisApiError(
                f"open_overseas_order_lookup_failed: {exc}"
            ) from exc
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

    async def _open_overseas_orders_by_side(
        self,
        *,
        symbol: str,
        exchange_code: str,
    ) -> dict[str, dict | None]:
        orders: dict[str, dict | None] = {"BUY": None, "SELL": None}
        for row in await self._list_open_overseas_orders(
            symbol=symbol,
            exchange_code=exchange_code,
        ):
            side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
            side = "SELL" if side_code == "01" else "BUY" if side_code == "02" else ""
            if side and orders[side] is None:
                orders[side] = row
        return orders

    async def _find_open_overseas_order(
        self,
        *,
        symbol: str,
        side: str,
        exchange_code: str,
    ) -> dict | None:
        orders = await self._open_overseas_orders_by_side(
            symbol=symbol,
            exchange_code=exchange_code,
        )
        return orders["SELL" if side.upper() == "SELL" else "BUY"]

    async def _find_conflicting_overseas_order(
        self,
        *,
        symbol: str,
        side: str,
        exchange_code: str,
    ) -> dict | None:
        orders = await self._open_overseas_orders_by_side(
            symbol=symbol,
            exchange_code=exchange_code,
        )
        return orders["BUY" if side.upper() == "SELL" else "SELL"]

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
        except Exception as exc:  # noqa: BLE001
            # See the matching comment in _list_open_overseas_orders: a failed
            # lookup must not silently read as "no pending order" without at
            # least surfacing that the check itself couldn't be verified.
            _logger.warning(
                "[ORDERS] 국내 미체결 조회 실패 - 조회결과 없음으로 처리됨 (symbol=%s, error=%s)",
                symbol,
                exc,
            )
            self._save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol=symbol,
                detail={
                    "reason": "open_domestic_order_lookup_failed",
                    "error": str(exc)[:200],
                },
            )
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

    def _stale_exit_replace_seconds(self) -> float:
        risk = getattr(self.config, "risk", None)
        minutes = float(getattr(risk, "stale_exit_replace_minutes", 15) or 15)
        return max(45.0, minutes * 60.0)

    def _format_domestic_symbol_label(self, stock_code: str) -> str:
        code = str(stock_code or "").strip().upper()
        if not code:
            return "-"
        name = str(getattr(self, "_dynamic_domestic_names", {}).get(code, "") or "").strip()
        return format_domestic_symbol_label(code, name)

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
            return "매매미실행"
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

    def _fresh_daily_chart_rows(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str = "",
        now: datetime,
        refresh_sec: int,
    ) -> list[dict] | None:
        cache = getattr(self, "_daily_chart_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._daily_chart_cache = cache
        key = (
            str(market).strip().lower(),
            str(symbol).strip().upper(),
            str(exchange_code).strip().upper(),
        )
        cached = cache.get(key)
        if not isinstance(cached, tuple) or len(cached) != 2:
            return None
        captured_at, rows = cached
        if not isinstance(captured_at, datetime) or not isinstance(rows, list):
            return None
        age_sec = (ensure_timezone(now) - ensure_timezone(captured_at)).total_seconds()
        if age_sec < 0 or age_sec >= max(1, int(refresh_sec)):
            return None
        return rows

    def _store_daily_chart_rows(
        self,
        *,
        market: str,
        symbol: str,
        rows: list[dict],
        captured_at: datetime,
        exchange_code: str = "",
    ) -> None:
        cache = getattr(self, "_daily_chart_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._daily_chart_cache = cache
        key = (
            str(market).strip().lower(),
            str(symbol).strip().upper(),
            str(exchange_code).strip().upper(),
        )
        cache[key] = (ensure_timezone(captured_at), list(rows))

    def _overseas_scan_scope_for_cycle(
        self,
        *,
        now: datetime,
        krx_open: bool,
        us_open: bool,
        us_orderable_in_profile: bool,
    ) -> str:
        if not us_open:
            return "none"
        if us_orderable_in_profile:
            return "full"

        auto = self._get_market_policy("overseas").auto_trade
        full_scan_interval_sec = max(
            60,
            int(getattr(auto, "intraday_bar_minutes", 5) or 5) * 60,
        )
        last_full_scan = getattr(
            self,
            "_last_non_orderable_full_scan_at",
            None,
        )
        if (
            last_full_scan is None
            or (
                ensure_timezone(now) - ensure_timezone(last_full_scan)
            ).total_seconds()
            >= full_scan_interval_sec
        ):
            return "full"
        return "monitored"

    @staticmethod
    def _overseas_inverse_exchange_code(symbol: str) -> str:
        return {
            "SQQQ": "NASD",
            "SOXS": "AMEX",
            "SPXU": "AMEX",
        }.get(str(symbol).strip().upper(), "NASD")

    def _monitored_overseas_pool(
        self,
        *,
        held_symbol_map: dict[str, str],
        open_inverse_shadow_symbols: set[str],
    ) -> list[OverseasCandidateConfig]:
        monitored_exchange_codes = {
            str(symbol).strip().upper(): str(exchange_code or "NASD").strip().upper()
            for symbol, exchange_code in held_symbol_map.items()
            if str(symbol).strip()
        }
        for symbol in open_inverse_shadow_symbols:
            symbol_upper = str(symbol).strip().upper()
            if symbol_upper:
                monitored_exchange_codes.setdefault(
                    symbol_upper,
                    self._overseas_inverse_exchange_code(symbol_upper),
                )
        return [
            OverseasCandidateConfig(
                symbol=symbol,
                exchange_code=exchange_code or "NASD",
            )
            for symbol, exchange_code in sorted(monitored_exchange_codes.items())
        ]

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
    ) -> list[dict[str, object]]:
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

    async def _scan_tv_dynamic_pool_with_fallback(self) -> list[dict[str, object]]:
        ll_cfg = self.config.liquidity_lab
        target_n = max(1, getattr(ll_cfg, "tv_top_n", 30))
        min_fallback_n = max(1, int(target_n * 0.3))
        primary_rel_vol = float(getattr(ll_cfg, "tv_min_rel_volume", 2.0))
        tv_rows = await self._scan_tv_dynamic_pool()
        if tv_rows and len(tv_rows) >= min_fallback_n:
            self._last_tv_scan_used_fallback = False
            self._last_tv_scan_diagnostics = {
                "primary_threshold": primary_rel_vol,
                "primary_count": len(tv_rows),
                "fallback_attempted": False,
                "fallback_threshold": None,
                "fallback_count": None,
                "minimum_target_count": min_fallback_n,
                "selected_count": len(tv_rows),
                "selected_source": "primary",
            }
            return tv_rows

        fallback_rel_vol = max(
            1.0,
            primary_rel_vol * 0.6,
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
        if fallback_rows and len(fallback_rows) >= len(tv_rows):
            selected_rows = fallback_rows
            selected_source = "fallback"
        elif tv_rows:
            selected_rows = tv_rows
            selected_source = "primary"
        else:
            selected_rows = []
            selected_source = "none"
        self._last_tv_scan_used_fallback = selected_source == "fallback"
        self._last_tv_scan_diagnostics = {
            "primary_threshold": primary_rel_vol,
            "primary_count": len(tv_rows),
            "fallback_attempted": True,
            "fallback_threshold": fallback_rel_vol,
            "fallback_count": len(fallback_rows),
            "minimum_target_count": min_fallback_n,
            "selected_count": len(selected_rows),
            "selected_source": selected_source,
        }
        return selected_rows

    def _tv_scan_event_detail(
        self,
        tv_rows: list[dict[str, object]],
    ) -> dict[str, object]:
        diagnostics = dict(getattr(self, "_last_tv_scan_diagnostics", {}) or {})
        return {
            "pool_size": len(tv_rows),
            "symbols": [str(row.get("symbol") or "") for row in tv_rows],
            "threshold": float(
                getattr(self.config.liquidity_lab, "tv_min_rel_volume", 2.0)
            ),
            "fallback_used": bool(
                getattr(self, "_last_tv_scan_used_fallback", False)
            ),
            **diagnostics,
        }

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
                        detail=self._tv_scan_event_detail(tv_rows),
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
                    detail=self._tv_scan_event_detail(tv_rows),
                )
                preview = ", ".join(row["symbol"] for row in tv_rows[:5])
                _logger.info(
                    "[TV] 해외 동적 풀 갱신: %s개 -> [%s]",
                    len(tv_rows),
                    preview,
                )
                return
            _logger.warning("[TV] scan_result_empty; will retry next rescan cycle")

        static_candidates = getattr(self.config.liquidity_lab, "overseas_candidates", [])
        if static_candidates:
            static_pool = [
                {"symbol": candidate.symbol.upper(), "exchange_code": candidate.exchange_code}
                for candidate in static_candidates
                if candidate.symbol.strip()
            ]
            if static_pool:
                self._dynamic_overseas_pool = static_pool
                self._awaiting_relist = False
                self._save_event(
                    event_type="pool_refresh",
                    market="overseas",
                    detail={"pool_size": len(static_pool), "source": "static_fallback"},
                )
                _logger.warning(
                    "[풀] TV 스캔 불가 -> config.liquidity_lab.overseas_candidates 정적 폴백 사용 (%s개)",
                    len(static_pool),
                )
                return

        self._dynamic_overseas_pool = []
        if not getattr(self, "_awaiting_relist", False):
            self._awaiting_relist = True
            self._save_event(
                event_type="tv_scan",
                market="overseas",
                detail=self._tv_scan_event_detail([]),
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

    def _domestic_dynamic_product_exclusion_reason(
        self,
        stock_code: str,
        stock_name: str,
    ) -> str:
        code = str(stock_code or "").strip().upper()
        name = str(stock_name or "").strip().upper()
        policy = self._get_market_policy("domestic")
        auto_trade = policy.auto_trade
        inverse_symbols = {
            str(value).strip().upper()
            for value in getattr(auto_trade, "inverse_etf_symbols", [])
            if str(value).strip()
        }
        approved_leveraged_symbols = {
            str(value).strip().upper()
            for value in getattr(
                auto_trade,
                "dynamic_pool_approved_leveraged_symbols",
                [],
            )
            if str(value).strip()
        }
        if "인버스" in name or "INVERSE" in name:
            return (
                "inverse_requires_regime_activation"
                if code in inverse_symbols
                else "unapproved_inverse_product"
            )
        if (
            "레버리지" in name
            or "LEVERAGE" in name
            or "LEVERAGED" in name
        ) and code not in approved_leveraged_symbols:
            return "unapproved_leveraged_product"
        return ""

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
        excluded_seen: set[str] = set()
        codes: list[str] = []
        name_map: dict[str, str] = {}
        structured_excluded: list[dict[str, str]] = []
        for row in [*vol_rows, *flu_rows]:
            code = str(row.get("stock_code", "")).strip()
            name = str(row.get("hts_kor_isnm", "") or row.get("name", "")).strip()
            exclusion_reason = self._domestic_dynamic_product_exclusion_reason(
                code,
                name,
            )
            if code and exclusion_reason:
                if code not in excluded_seen:
                    excluded_seen.add(code)
                    structured_excluded.append(
                        {
                            "code": code,
                            "name": name,
                            "reason": exclusion_reason,
                        }
                    )
                continue
            if code and name:
                name_map[code] = name
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        if not codes:
            self._dynamic_domestic_codes = []
            self._dynamic_domestic_names = {}
            self._save_event(
                event_type="pool_refresh",
                market="domestic",
                detail={
                    "pool_size": 0,
                    "top_names": [],
                    "structured_excluded_count": len(structured_excluded),
                    "structured_excluded": structured_excluded,
                },
            )
            return
        self._dynamic_domestic_codes = codes
        self._dynamic_domestic_names = name_map
        top_names: list[str] = []
        for row in vol_rows:
            code = str(row.get("stock_code", "")).strip()
            name = str(
                row.get("hts_kor_isnm", "") or row.get("name", "")
            ).strip()
            if code in seen and name:
                top_names.append(name)
            if len(top_names) >= 5:
                break
        self._save_event(
            event_type="pool_refresh",
            market="domestic",
            detail={
                "pool_size": len(codes),
                "top_names": top_names,
                "structured_excluded_count": len(structured_excluded),
                "structured_excluded": structured_excluded,
            },
        )
        _logger.info("domestic_dynamic_pool_refreshed count=%s", len(codes))
        notifier = getattr(self, "notifier", None)
        if notifier is not None and getattr(notifier, "enabled", True):
            try:
                await notifier.send(
                    f"🔄 [국내 동적 풀 갱신] {len(codes)}종목\n"
                    f"거래량 상위: {', '.join(top_names)}\n"
                    f"미승인 구조화상품 제외: {len(structured_excluded)}종목"
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

    async def _refresh_market_regimes(self, now: datetime) -> None:
        collector = getattr(self, "market_regime_collector", None)
        client = getattr(self, "client", None)
        repository = getattr(self, "repository", None)
        if collector is None:
            if client is None or repository is None:
                return
            collector = MarketRegimeCollector(client, repository)
            self.market_regime_collector = collector
        else:
            # Telegram control opens a fresh KIS client for every cycle. Keep
            # the long-lived collector attached to that current client.
            if client is not None:
                collector.client = client
            if repository is not None:
                collector.repository = repository
        try:
            await collector.refresh_if_due(now)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[REGIME] collector_failed error=%s", exc)
        refresh_inverse = getattr(
            collector,
            "refresh_inverse_benchmarks_if_due",
            None,
        )
        if not callable(refresh_inverse):
            return
        try:
            domestic_policy = self._get_market_policy("domestic")
            overseas_policy = self._get_market_policy("overseas")
            await refresh_inverse(
                [
                    *domestic_policy.inverse_benchmarks.values(),
                    *overseas_policy.inverse_benchmarks.values(),
                ],
                now,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[INVERSE_BENCHMARK] collector_failed error=%s",
                exc,
            )

    def _get_execution_reconciler(self) -> BrokerExecutionReconciler:
        reconciler = getattr(self, "execution_reconciler", None)
        if reconciler is None:
            reconciler = BrokerExecutionReconciler(self)
            self.execution_reconciler = reconciler
        return reconciler

    async def _reconcile_broker_executions(
        self,
        now: datetime,
        *,
        force: bool = False,
    ) -> dict[str, int]:
        last_attempt = getattr(self, "_last_execution_reconcile_at", None)
        interval_sec = max(
            5,
            int(getattr(self, "_execution_reconcile_interval_sec", 20) or 20),
        )
        if (
            not force
            and last_attempt is not None
            and (now - last_attempt).total_seconds() < interval_sec
        ):
            return {
                "pending": 0,
                "matched": 0,
                "missing": 0,
                "finalized": 0,
                "no_fill": 0,
                "terminal_followups": 0,
                "expired_day_orders": 0,
                "failed_markets": 0,
            }
        repository = getattr(self, "repository", None)
        if repository is None:
            return {}
        pending_executions = repository.list_unfinalized_broker_executions(limit=1000)
        if not pending_executions:
            return {}
        pending_markets = {
            str(row.get("market") or "").strip().lower()
            for row in pending_executions
            if str(row.get("market") or "").strip()
        }
        eligible_markets = set(pending_markets)
        if not force:
            grace_minutes = _EXECUTION_RECONCILE_POST_CLOSE_GRACE_MIN
            eligible_markets = {
                market
                for market in pending_markets
                if (
                    market == "domestic"
                    and is_krx_execution_reconcile_window(
                        now,
                        post_close_grace_minutes=grace_minutes,
                    )
                )
                or (
                    market == "overseas"
                    and is_us_execution_reconcile_window(
                        now,
                        self.config.credentials.env,
                        post_close_grace_minutes=grace_minutes,
                    )
                )
            }
            deferred_markets = pending_markets - eligible_markets
            if deferred_markets:
                pending_by_market = {
                    market: sum(
                        1
                        for row in pending_executions
                        if str(row.get("market") or "").strip().lower() == market
                    )
                    for market in sorted(deferred_markets)
                }
                defer_key = (
                    now.astimezone(KST).date().isoformat(),
                    get_us_trading_session(now),
                    tuple(sorted(deferred_markets)),
                    tuple(
                        sorted(
                            int(row.get("id") or 0)
                            for row in pending_executions
                            if str(row.get("market") or "").strip().lower()
                            in deferred_markets
                        )
                    ),
                )
                if defer_key != getattr(
                    self,
                    "_last_execution_reconcile_defer_key",
                    None,
                ):
                    self._last_execution_reconcile_defer_key = defer_key
                    self._save_event(
                        event_type="execution_reconcile_deferred",
                        detail={
                            "deferred_markets": sorted(deferred_markets),
                            "pending_by_market": pending_by_market,
                            "us_session": get_us_trading_session(now),
                            "profile": self.config.credentials.env,
                            "post_close_grace_minutes": grace_minutes,
                        },
                    )
            if not eligible_markets:
                return {
                    "pending": len(pending_executions),
                    "matched": 0,
                    "missing": 0,
                    "finalized": 0,
                    "no_fill": 0,
                    "terminal_followups": 0,
                    "expired_day_orders": 0,
                    "failed_markets": 0,
                }
        self._last_execution_reconcile_at = now
        return await self._get_execution_reconciler().reconcile(
            now=now,
            force=force,
            markets=eligible_markets,
        )

    @staticmethod
    def _execution_signal_value(
        context: dict,
        key: str,
    ) -> float | None:
        snapshot = context.get("signal_snapshot")
        if not isinstance(snapshot, dict):
            return None
        value = snapshot.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_virtual_sell_settlement_context(context: object) -> bool:
        return (
            isinstance(context, dict)
            and str(context.get("execution_role") or "").strip()
            == _VIRTUAL_SELL_SETTLEMENT_ROLE
        )

    def _find_unfinalized_virtual_sell_settlement(
        self,
        symbol: str,
    ) -> dict | None:
        symbol_key = symbol.strip().upper()
        for execution in self.repository.list_unfinalized_broker_executions(
            market="overseas",
            limit=1000,
        ):
            if str(execution.get("symbol") or "").strip().upper() != symbol_key:
                continue
            if str(execution.get("side") or "").strip().upper() != "SELL":
                continue
            if self._is_virtual_sell_settlement_context(
                execution.get("context_json")
            ):
                return execution
        return None

    def _virtual_settlement_retry_policy(self) -> dict[str, int]:
        try:
            auto_trade = self._get_market_policy("overseas").auto_trade
        except (AttributeError, KeyError, RuntimeError, ValueError):
            auto_trade = None
        return {
            "stale_order_minutes": max(
                1,
                int(
                    getattr(
                        auto_trade,
                        "virtual_settlement_stale_order_minutes",
                        5,
                    )
                    or 5
                ),
            ),
            "retry_cooldown_minutes": max(
                1,
                int(
                    getattr(
                        auto_trade,
                        "virtual_settlement_retry_cooldown_minutes",
                        15,
                    )
                    or 15
                ),
            ),
            "max_submissions_per_session": max(
                1,
                int(
                    getattr(
                        auto_trade,
                        "virtual_settlement_max_submissions_per_session",
                        3,
                    )
                    or 3
                ),
            ),
            "aggressive_after_sessions": max(
                1,
                int(
                    getattr(
                        auto_trade,
                        "virtual_settlement_aggressive_after_sessions",
                        2,
                    )
                    or 2
                ),
            ),
            "aggressive_limit_bps": max(
                0,
                int(
                    getattr(
                        auto_trade,
                        "virtual_settlement_aggressive_limit_bps",
                        50,
                    )
                    or 0
                ),
            ),
        }

    def _virtual_settlement_retry_gate(
        self,
        *,
        symbol: str,
        now: datetime,
    ) -> tuple[bool, dict]:
        current = ensure_timezone(now)
        session_date = self._market_session_date("overseas", current)
        policy = self._virtual_settlement_retry_policy()
        usage = self.repository.get_virtual_settlement_submission_usage(
            market="overseas",
            symbol=symbol,
            session_date=session_date,
        )
        submission_count = int(usage.get("submission_count") or 0)
        detail: dict[str, object] = {
            "session_date": session_date,
            "submission_count": submission_count,
            **policy,
        }
        if submission_count >= policy["max_submissions_per_session"]:
            detail["reason"] = "session_submission_limit"
            return False, detail

        last_submitted_at = parse_datetime(
            str(usage.get("last_submitted_at") or "")
        )
        if last_submitted_at is not None:
            retry_after = ensure_timezone(last_submitted_at) + timedelta(
                minutes=policy["retry_cooldown_minutes"]
            )
            if current < retry_after:
                detail.update(
                    {
                        "reason": "retry_cooldown",
                        "last_submitted_at": ensure_timezone(
                            last_submitted_at
                        ).isoformat(),
                        "retry_after_at": retry_after.isoformat(),
                    }
                )
                return False, detail
        detail["reason"] = "allowed"
        return True, detail

    def _record_virtual_settlement_deferred(
        self,
        *,
        symbol: str,
        detail: dict,
    ) -> bool:
        key = (
            str(detail.get("session_date") or ""),
            symbol.upper(),
            str(detail.get("reason") or ""),
            int(detail.get("submission_count") or 0),
        )
        emitted = getattr(self, "_virtual_settlement_defer_event_keys", set())
        if key in emitted:
            return False
        emitted.add(key)
        self._virtual_settlement_defer_event_keys = emitted
        self._save_event(
            event_type="virtual_pending_settlement_deferred",
            market="overseas",
            symbol=symbol,
            detail={**detail, "pending_preserved": True},
        )
        return True

    async def _cancel_stale_virtual_sell_settlement(
        self,
        *,
        execution: dict,
        exchange_code: str,
        now: datetime | None = None,
    ) -> bool:
        current = ensure_timezone(now or datetime.now(timezone.utc))
        created_at = parse_datetime(str(execution.get("created_at") or ""))
        if created_at is None:
            return False
        execution_group_id = str(execution.get("execution_group_id") or "")
        if execution_group_id in getattr(
            self,
            "_virtual_settlement_cancel_requested",
            set(),
        ):
            return False
        age_sec = max(
            0.0,
            (current - ensure_timezone(created_at)).total_seconds(),
        )
        stale_after_sec = (
            self._virtual_settlement_retry_policy()["stale_order_minutes"]
            * 60.0
        )
        if age_sec < stale_after_sec:
            return False

        symbol = str(execution.get("symbol") or "").strip().upper()
        try:
            pending_order = await self._find_open_overseas_order(
                symbol=symbol,
                side="SELL",
                exchange_code=exchange_code,
            )
        except KisApiError as exc:
            self._save_event(
                event_type="virtual_pending_settlement_cancel_skipped",
                market="overseas",
                symbol=symbol,
                detail={
                    "reason": "open_order_lookup_failed",
                    "broker_order_no": execution.get("broker_order_no"),
                    "age_sec": round(age_sec, 3),
                    "stale_after_sec": stale_after_sec,
                    "error": str(exc)[:200],
                },
            )
            return False
        if pending_order is None:
            return False
        expected_order_no = self.repository.normalize_broker_order_no(
            execution.get("broker_order_no")
        )
        open_order_no = self.repository.normalize_broker_order_no(
            pending_order.get("order_no")
        )
        if not expected_order_no or open_order_no != expected_order_no:
            self._save_event(
                event_type="virtual_pending_settlement_cancel_skipped",
                market="overseas",
                symbol=symbol,
                detail={
                    "reason": "open_sell_order_number_mismatch",
                    "expected_order_no": expected_order_no,
                    "open_order_no": open_order_no,
                    "age_sec": round(age_sec, 3),
                },
            )
            return False
        try:
            response = await self._cancel_open_overseas_order(
                symbol=symbol,
                exchange_code=exchange_code,
                pending_order=pending_order,
            )
        except KisApiError as exc:
            self._save_event(
                event_type="virtual_pending_settlement_cancel_failed",
                market="overseas",
                symbol=symbol,
                detail={
                    "broker_order_no": execution.get("broker_order_no"),
                    "age_sec": round(age_sec, 3),
                    "error": str(exc)[:200],
                },
            )
            return False

        self._record_broker_order_event(
            market="overseas",
            symbol=symbol,
            exchange_code=exchange_code,
            side="SELL",
            order_kind="cancel",
            requested_qty=int(pending_order.get("open_qty") or 0),
            requested_price=float(pending_order.get("order_price") or 0.0),
            status="CANCELED",
            reason="stale_virtual_sell_settlement",
            payload=self._broker_cancel_payload(response, pending_order),
        )
        requested = getattr(
            self,
            "_virtual_settlement_cancel_requested",
            set(),
        )
        requested.add(execution_group_id)
        self._virtual_settlement_cancel_requested = requested
        self._save_event(
            event_type="virtual_pending_settlement_cancel_submitted",
            market="overseas",
            symbol=symbol,
            detail={
                "execution_group_id": execution_group_id,
                "broker_order_no": execution.get("broker_order_no"),
                "open_qty": int(pending_order.get("open_qty") or 0),
                "age_sec": round(age_sec, 3),
                "pending_preserved_until_history_confirmation": True,
            },
        )
        return True

    async def _apply_confirmed_virtual_sell_settlement(
        self,
        *,
        first: dict,
        context: dict,
        execution_group_id: str,
        fill_price: float,
        filled_qty: int,
        target_qty: int,
        confirmed_at: datetime,
    ) -> bool:
        market = str(first.get("market") or "").strip().lower()
        symbol = str(first.get("symbol") or "").strip().upper()
        pending = self.repository.get_virtual_sell_pending(market, symbol)
        pending_qty = 0 if pending is None else int(pending.get("qty") or 0)
        settled_qty = min(max(0, filled_qty), max(0, pending_qty))
        unmatched_fill_qty = max(0, filled_qty - settled_qty)
        pending_avg_price = float(
            (
                pending.get("avg_sell_price")
                if pending is not None
                else context.get("virtual_sell_avg_price")
            )
            or 0.0
        )
        strategy_flag = str(
            first.get("strategy_flag")
            or context.get("strategy_flag")
            or (pending or {}).get("strategy_flag")
            or ""
        )
        entry_by = str(
            first.get("entry_by")
            or context.get("entry_by")
            or (pending or {}).get("entry_by")
            or ""
        )
        entry_reason = str(
            context.get("entry_reason")
            or (pending or {}).get("entry_reason")
            or ""
        )
        entry_time = str(
            first.get("entry_time")
            or context.get("entry_time")
            or (pending or {}).get("entry_time")
            or ""
        ) or None
        remaining_qty = max(0, pending_qty - settled_qty)

        if pending is not None and settled_qty > 0:
            if remaining_qty <= 0:
                self.repository.delete_virtual_sell_pending(market, symbol)
            else:
                self.repository.upsert_virtual_sell_pending(
                    market=market,
                    symbol=symbol,
                    exchange_code=(
                        pending.get("exchange_code")
                        or first.get("exchange_code")
                    ),
                    qty=remaining_qty,
                    avg_sell_price=pending_avg_price,
                    currency=str(pending.get("currency") or "USD"),
                    updated_at=format_kst(confirmed_at),
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    entry_reason=entry_reason,
                    entry_time=entry_time,
                )

        entry_price = self._parse_optional_float(context.get("entry_price")) or 0.0
        virtual_realized_pnl = (
            (pending_avg_price - entry_price) * settled_qty
            if entry_price > 0 and pending_avg_price > 0
            else 0.0
        )
        actual_realized_pnl = (
            (fill_price - entry_price) * settled_qty
            if entry_price > 0
            else 0.0
        )
        settlement_slippage = (
            (fill_price - pending_avg_price) * settled_qty
            if pending_avg_price > 0
            else 0.0
        )
        pnl_pct = (
            (pending_avg_price - entry_price) / entry_price
            if entry_price > 0 and pending_avg_price > 0
            else 0.0
        )
        logged_at = str(
            first.get("fill_recorded_at")
            or first.get("updated_at")
            or confirmed_at.astimezone(timezone.utc).isoformat()
        )
        if entry_price <= 0:
            self.repository.finalize_broker_execution_group(
                execution_group_id,
                finalized_at=confirmed_at.astimezone(timezone.utc).isoformat(),
            )
            self._save_event(
                event_type="virtual_pending_settlement_accounting_failed",
                market=market,
                symbol=symbol,
                detail={
                    "reason": "missing_entry_price",
                    "execution_group_id": execution_group_id,
                    "filled_qty": filled_qty,
                    "settled_qty": settled_qty,
                    "pending_preserved_qty": remaining_qty,
                },
            )
            return True

        fx_rate = float(context.get("fx_rate") or 1380.0)
        gross_pnl_usd = (fill_price - entry_price) * filled_qty
        gross_pnl_krw = gross_pnl_usd * fx_rate
        (
            net_pnl_usd,
            net_pnl_krw,
            sell_fee_usd,
            sell_fee_krw,
        ) = self._estimate_overseas_net_pnl(
            entry_price=entry_price,
            exit_price=fill_price,
            qty=filled_qty,
            fx_rate=fx_rate,
        )
        account_pnl_pct = (fill_price - entry_price) / entry_price
        inserted = self.repository.save_cycle_log(
            logged_at=logged_at,
            market=market,
            symbol=symbol,
            exchange_code=first.get("exchange_code"),
            action_bias="SELL_REAL",
            action_reason=_VIRTUAL_SELL_SETTLEMENT_ROLE,
            price=fill_price,
            pnl_pct=account_pnl_pct,
            realized_pnl_usd=gross_pnl_usd,
            realized_pnl_krw=gross_pnl_krw,
            holding_qty=filled_qty,
            cycle_no=int(first.get("cycle_no") or 0),
            net_pnl_usd=net_pnl_usd,
            net_pnl_krw=net_pnl_krw,
            commission_usd=sell_fee_usd,
            commission_krw=sell_fee_krw,
            session_id="",
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=_VIRTUAL_SELL_SETTLEMENT_ROLE,
            is_session_trade=0,
            entry_price=entry_price,
            qty_executed=filled_qty,
            is_virtual=0,
            orderable_qty=int(context.get("orderable_qty") or filled_qty),
            stock_name=str(context.get("stock_name") or symbol),
            cost_calculation_version=OVERSEAS_COST_CALCULATION_VERSION,
            entry_time=entry_time,
            execution_group_id=execution_group_id,
        )
        if not inserted:
            return False

        execution_at = ensure_timezone(parse_datetime(logged_at) or confirmed_at)
        confirmation_delay_sec = max(
            0.0,
            (confirmed_at - execution_at).total_seconds(),
        )
        cb = self._get_circuit_breaker()
        was_halted = bool(self._is_trading_halted(market))
        daily_was_halted = cb.daily_halted_at is not None
        same_risk_day = (
            cb.current_risk_day(execution_at)
            == cb.current_risk_day(confirmed_at)
        )
        risk_controls_replayed = (
            same_risk_day
            or confirmation_delay_sec
            <= _EXECUTION_RECONCILE_POST_CLOSE_GRACE_MIN * 60
        )
        entry_notional_krw = entry_price * filled_qty * fx_rate
        net_pnl_pct = (
            net_pnl_krw / entry_notional_krw
            if entry_notional_krw > 0
            else account_pnl_pct
        )
        if risk_controls_replayed:
            self._on_realised(
                market=market,
                net_pnl_krw=net_pnl_krw,
                net_pnl_pct=net_pnl_pct,
                include_session_pnl=same_risk_day,
            )
        self._reconcile_confirmed_risk_day_pnl(confirmed_at)
        is_halted = self._is_trading_halted(market)
        daily_is_halted = cb.daily_halted_at is not None
        consecutive_losses = self._consecutive_losses_for_market(market)
        self.repository.update_cycle_log_execution_risk(
            execution_group_id,
            consecutive_losses=consecutive_losses,
            cb_active=int(is_halted),
        )
        if daily_is_halted and not daily_was_halted:
            await self._send_circuit_breaker_notification(
                "\n".join(
                    [
                        "일일손실한도 발동",
                        (
                            f"리스크일={cb.current_risk_day(confirmed_at).isoformat()} | "
                            f"확정순손익 {self._session_realised_krw:+,.0f}원"
                        ),
                        "국장·미장 신규 매수를 07:00 KST 전환까지 중단합니다.",
                    ]
                )
            )
        elif is_halted and not was_halted:
            await self._send_circuit_breaker_notification(
                "\n".join(
                    [
                        "서킷브레이커 발동",
                        (
                            f"시장={format_market_korean(market)} | "
                            f"연속손절 {consecutive_losses}회 | "
                            f"세션손익 {self._session_realised_krw:+,.0f}원"
                        ),
                        f"{format_market_korean(market)} 신규 매수만 중단합니다.",
                    ]
                )
            )
        self._save_event(
            event_type="virtual_pending_settlement_confirmed",
            market=market,
            symbol=symbol,
            detail={
                "execution_group_id": execution_group_id,
                "broker_order_no": first.get("broker_order_no"),
                "filled_qty": filled_qty,
                "target_qty": target_qty,
                "settled_qty": settled_qty,
                "unmatched_fill_qty": unmatched_fill_qty,
                "remaining_pending_qty": remaining_qty,
                "avg_fill_price": round(fill_price, 8),
                "virtual_sell_avg_price": round(pending_avg_price, 8),
                "entry_price": round(entry_price, 8),
                "virtual_realized_pnl_usd": round(virtual_realized_pnl, 6),
                "actual_realized_pnl_usd": round(actual_realized_pnl, 6),
                "actual_net_pnl_usd": round(net_pnl_usd, 6),
                "actual_net_pnl_krw": round(net_pnl_krw, 2),
                "settlement_slippage_usd": round(settlement_slippage, 6),
                "performance_recorded_at_virtual_exit": True,
                "account_risk_recorded_at_settlement": True,
                "strategy_owned_sell_real": False,
                "strategy_flag": strategy_flag,
                "entry_by": entry_by,
                "entry_reason": entry_reason,
                "entry_time": entry_time,
                "risk_controls_replayed": risk_controls_replayed,
            },
        )
        if settled_qty <= 0:
            return True

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][VIRTUAL_SETTLED]",
                    f"시각={format_kst_korean(confirmed_at)}",
                    f"시장={format_market_korean(market)}",
                    f"종목={symbol}",
                    "구분=정산매도 체결확정",
                    f"체결수량={settled_qty}주",
                    f"실제체결가={format_usd(fill_price)}",
                    f"가상매도가={format_usd(pending_avg_price)}",
                    f"매입가={format_usd(entry_price)}",
                    f"전략손익={format_usd(virtual_realized_pnl)}",
                    f"전략수익률={format_pct(pnl_pct)}",
                    f"계좌순손익={format_usd(net_pnl_usd)}",
                    f"정산슬리피지={format_usd(settlement_slippage)}",
                    f"남은정산대기={remaining_qty}주",
                    "참고=KIS 주문체결내역 확인 후 체결수량만 정산함",
                ]
            )
        )
        return True

    async def _apply_confirmed_execution_group(
        self,
        executions: list[dict],
        *,
        filled_qty: int,
        filled_amount: float,
        target_qty: int,
        reconciled_at: datetime | None = None,
    ) -> bool:
        rows = sorted(
            executions,
            key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)),
        )
        if not rows or filled_qty <= 0:
            return False
        filled_rows = [
            row for row in rows if int(row.get("filled_qty") or 0) > 0
        ]
        # A replacement group can contain an older canceled intent and the
        # later order that actually closed the position. Attribute the trade
        # to the latest filled order so its reason and signal context are not
        # inherited from an unfilled predecessor.
        first = max(
            filled_rows,
            key=lambda row: (
                str(row.get("fill_recorded_at") or ""),
                str(row.get("created_at") or ""),
                int(row.get("id") or 0),
            ),
        )
        context = (
            first.get("context_json")
            if isinstance(first.get("context_json"), dict)
            else {}
        )
        market = str(first.get("market") or "").strip().lower()
        symbol = str(first.get("symbol") or "").strip().upper()
        side = str(first.get("side") or "").strip().upper()
        execution_group_id = str(first.get("execution_group_id") or "")
        fill_price = (
            float(filled_amount) / int(filled_qty)
            if filled_amount > 0 and filled_qty > 0
            else sum(
                float(row.get("avg_fill_price") or 0.0)
                * int(row.get("filled_qty") or 0)
                for row in rows
            )
            / max(1, filled_qty)
        )
        if fill_price <= 0:
            return False
        fill_times = [
            str(row.get("fill_recorded_at") or "")
            for row in rows
            if str(row.get("fill_recorded_at") or "")
        ]
        logged_at = max(fill_times) if fill_times else datetime.now(timezone.utc).isoformat()
        confirmed_at = ensure_timezone(reconciled_at or datetime.now(timezone.utc))
        parsed_execution_at = parse_datetime(logged_at)
        execution_at = ensure_timezone(parsed_execution_at or confirmed_at)
        confirmation_delay_sec = max(
            0.0,
            (confirmed_at - execution_at).total_seconds(),
        )
        strategy_flag = str(first.get("strategy_flag") or "")
        entry_by = str(first.get("entry_by") or "")
        exit_by = str(first.get("exit_by") or "")
        reason = str(first.get("reason") or "")
        entry_price = self._parse_optional_float(first.get("entry_price"))
        entry_time = str(first.get("entry_time") or "") or None
        hold_duration_min = self._parse_optional_float(first.get("hold_duration_min"))
        activity_score = self._parse_optional_float(context.get("activity_score"))
        orderable_qty = int(context.get("orderable_qty") or filled_qty)
        stock_name = str(context.get("stock_name") or symbol)
        product_type = str(context.get("product_type") or "").strip()
        if self._is_virtual_sell_settlement_context(context):
            return await self._apply_confirmed_virtual_sell_settlement(
                first=first,
                context=context,
                execution_group_id=execution_group_id,
                fill_price=fill_price,
                filled_qty=filled_qty,
                target_qty=target_qty,
                confirmed_at=confirmed_at,
            )
        is_session_trade = int(first.get("is_session_trade") or 0)
        if side == "SELL" and not is_session_trade and self._is_session_owned(symbol):
            is_session_trade = 1
        is_full_group_fill = filled_qty >= max(1, target_qty)

        common = {
            "logged_at": logged_at,
            "market": market,
            "symbol": symbol,
            "exchange_code": first.get("exchange_code"),
            "action_reason": reason,
            "price": fill_price,
            "holding_qty": filled_qty,
            "rsi14": self._execution_signal_value(context, "rsi14"),
            "volume_ratio": self._execution_signal_value(context, "volume_ratio"),
            "intraday_momentum": self._execution_signal_value(
                context,
                "intraday_momentum",
            ),
            "intraday_bar_return": self._execution_signal_value(
                context,
                "intraday_bar_return",
            ),
            "minute_ma_fast": self._execution_signal_value(
                context,
                "minute_ma_fast",
            ),
            "minute_ma_slow": self._execution_signal_value(
                context,
                "minute_ma_slow",
            ),
            "activity_score": activity_score,
            "cycle_no": int(first.get("cycle_no") or 0),
            "session_id": str(first.get("session_id") or ""),
            "strategy_flag": strategy_flag,
            "entry_by": entry_by,
            "exit_by": exit_by,
            "is_session_trade": is_session_trade,
            "vwap": self._execution_signal_value(context, "vwap"),
            "macd_line": self._execution_signal_value(context, "macd_line"),
            "macd_signal": self._execution_signal_value(context, "macd_signal"),
            "macd_golden": (
                int(self._execution_signal_value(context, "macd_golden") or 0)
                if self._execution_signal_value(context, "macd_golden") is not None
                else None
            ),
            "breakout_distance_pct": self._execution_signal_value(
                context,
                "breakout_distance_pct",
            ),
            "atr": self._execution_signal_value(context, "atr"),
            "spread_pct": self._execution_signal_value(context, "spread_pct"),
            "consecutive_losses": self._consecutive_losses_for_market(market),
            "hold_cycles": int(context.get("hold_cycles") or 0),
            "entry_price": fill_price if side == "BUY" else entry_price,
            "qty_executed": filled_qty,
            "is_virtual": 0,
            "orderable_qty": orderable_qty,
            "stock_name": stock_name,
            "product_type": product_type,
            "cost_calculation_version": (
                DOMESTIC_COST_CALCULATION_VERSION
                if market == "domestic"
                else OVERSEAS_COST_CALCULATION_VERSION
            ),
            "hold_duration_min": 0.0 if side == "BUY" else hold_duration_min,
            "entry_time": logged_at if side == "BUY" else entry_time,
            "exit_cooldown_remaining": 0.0,
            "cb_active": self._cb_active_flag(market),
            "pool_size": int(context.get("pool_size") or 0),
            "execution_group_id": execution_group_id,
        }

        if side == "BUY":
            if market == "domestic":
                commission_krw = round(
                    fill_price * filled_qty * self._domestic_commission_rate(),
                    2,
                )
                inserted = self.repository.save_cycle_log(
                    **common,
                    action_bias="BUY_REAL",
                    pnl_pct=0.0,
                    realized_pnl_usd=None,
                    realized_pnl_krw=0.0,
                    net_pnl_usd=None,
                    net_pnl_krw=0.0,
                    commission_usd=None,
                    commission_krw=commission_krw,
                )
                price_label = f"{fill_price:,.0f}원"
            else:
                fx_rate = float(
                    context.get("fx_rate")
                    or getattr(
                        self._get_market_policy("overseas").auto_trade,
                        "usd_krw_fallback_rate",
                        1380.0,
                    )
                    or 1380.0
                )
                commission_usd = round(
                    fill_price * filled_qty * self._overseas_commission_rate(),
                    6,
                )
                inserted = self.repository.save_cycle_log(
                    **common,
                    action_bias="BUY_REAL",
                    pnl_pct=0.0,
                    realized_pnl_usd=0.0,
                    realized_pnl_krw=0.0,
                    net_pnl_usd=0.0,
                    net_pnl_krw=0.0,
                    commission_usd=commission_usd,
                    commission_krw=round(commission_usd * fx_rate, 2),
                )
                price_label = format_usd(fill_price)
            if inserted:
                triggered = self._decode_strategy_ids(strategy_flag, entry_by)
                confirmed_entry_time = parse_datetime(logged_at)
                if triggered and confirmed_entry_time is not None:
                    manager = self._get_strategy_manager(symbol, market)
                    prior_peak = (
                        float(manager.position.peak_price)
                        if manager.position is not None
                        else fill_price
                    )
                    manager.open_position(
                        symbol=symbol,
                        entry_price=fill_price,
                        triggered_by=triggered,
                        entry_time=ensure_timezone(confirmed_entry_time),
                    )
                    if manager.position is not None:
                        manager.position.peak_price = max(prior_peak, fill_price)
                self._mark_session_owned(symbol)
                self._queue_trade_notification(
                    " ".join(
                        [
                            format_market_korean(market),
                            self._format_trade_symbol_label(market, symbol),
                            "매수체결",
                            price_label,
                            f"x{filled_qty}",
                            f"전략={strategy_flag or '-'}",
                            f"주도={entry_by or '-'}",
                        ]
                    )
                )
            return inserted

        if entry_price is None or entry_price <= 0:
            self._save_event(
                event_type="execution_reconcile_failed",
                market=market,
                symbol=symbol,
                detail={
                    "reason": "confirmed_sell_missing_entry_price",
                    "execution_group_id": execution_group_id,
                    "fill_price": fill_price,
                    "filled_qty": filled_qty,
                },
            )
            return False

        pnl_pct = (fill_price - entry_price) / entry_price
        if market == "domestic":
            gross_pnl = (fill_price - entry_price) * filled_qty
            net_pnl_krw, sell_commission_krw = self._estimate_domestic_net_pnl_krw(
                entry_price=entry_price,
                exit_price=fill_price,
                qty=filled_qty,
                product_type=product_type,
            )
            inserted = self.repository.save_cycle_log(
                **common,
                action_bias="SELL_REAL",
                pnl_pct=pnl_pct,
                realized_pnl_usd=None,
                realized_pnl_krw=gross_pnl,
                net_pnl_usd=None,
                net_pnl_krw=net_pnl_krw,
                commission_usd=None,
                commission_krw=sell_commission_krw,
            )
            gross_pnl_krw = gross_pnl
            pnl_label = f"{net_pnl_krw:+,.0f}원"
            price_label = f"{fill_price:,.0f}원"
        else:
            fx_rate = float(
                context.get("fx_rate")
                or getattr(
                    self._get_market_policy("overseas").auto_trade,
                    "usd_krw_fallback_rate",
                    1380.0,
                )
                or 1380.0
            )
            gross_pnl_usd = (fill_price - entry_price) * filled_qty
            gross_pnl_krw = gross_pnl_usd * fx_rate
            net_pnl_usd, net_pnl_krw, sell_fee_usd, sell_fee_krw = (
                self._estimate_overseas_net_pnl(
                    entry_price=entry_price,
                    exit_price=fill_price,
                    qty=filled_qty,
                    fx_rate=fx_rate,
                )
            )
            inserted = self.repository.save_cycle_log(
                **common,
                action_bias="SELL_REAL",
                pnl_pct=pnl_pct,
                realized_pnl_usd=gross_pnl_usd,
                realized_pnl_krw=gross_pnl_krw,
                net_pnl_usd=net_pnl_usd,
                net_pnl_krw=net_pnl_krw,
                commission_usd=sell_fee_usd,
                commission_krw=sell_fee_krw,
            )
            pnl_label = format_usd(net_pnl_usd)
            price_label = format_usd(fill_price)

        if not inserted:
            return False
        was_halted = bool(common["cb_active"])
        cb = self._get_circuit_breaker()
        daily_was_halted = cb.daily_halted_at is not None
        execution_risk_day = cb.current_risk_day(execution_at)
        current_risk_day = cb.current_risk_day(confirmed_at)
        same_risk_day = execution_risk_day == current_risk_day
        risk_controls_replayed = (
            same_risk_day
            or confirmation_delay_sec
            <= _EXECUTION_RECONCILE_POST_CLOSE_GRACE_MIN * 60
        )
        entry_notional_krw = entry_price * filled_qty
        if market == "overseas":
            entry_notional_krw *= fx_rate
        net_pnl_pct = (
            float(net_pnl_krw) / entry_notional_krw
            if entry_notional_krw > 0
            else float(pnl_pct)
        )
        if risk_controls_replayed:
            self._on_realised(
                market=market,
                net_pnl_krw=float(net_pnl_krw),
                net_pnl_pct=net_pnl_pct,
                include_session_pnl=same_risk_day,
            )
        risk_summary = self._reconcile_confirmed_risk_day_pnl(confirmed_at)
        is_halted = self._is_trading_halted(market)
        daily_is_halted = cb.daily_halted_at is not None
        consecutive_losses = self._consecutive_losses_for_market(market)
        self.repository.update_cycle_log_execution_risk(
            execution_group_id,
            consecutive_losses=consecutive_losses,
            cb_active=int(is_halted),
        )
        if daily_is_halted and not daily_was_halted:
            daily_limit_pct = (
                float(
                    getattr(self.config.risk, "daily_loss_limit_pct", 0.0)
                    or 0.0
                )
                * 100.0
            )
            risk_day = current_risk_day.isoformat()
            _logger.warning(
                "[CB] confirmed-fill daily limit fired "
                "risk_day=%s session_pnl=%.0f",
                risk_day,
                self._session_realised_krw,
            )
            await self._send_circuit_breaker_notification(
                "\n".join(
                    [
                        "일일손실한도 발동",
                        (
                            f"리스크일={risk_day} | "
                            f"확정순손익 {self._session_realised_krw:+,.0f}원 | "
                            f"한도 {daily_limit_pct:.2f}%"
                        ),
                        "국장·미장 신규 매수를 07:00 KST 전환까지 중단합니다.",
                    ]
                )
            )
        elif is_halted and not was_halted:
            _logger.warning(
                "[CB] %s confirmed-fill breaker fired consecutive=%d session_pnl=%.0f",
                market,
                consecutive_losses,
                self._session_realised_krw,
            )
            await self._send_circuit_breaker_notification(
                "\n".join(
                    [
                        "서킷브레이커 발동",
                        (
                            f"시장={format_market_korean(market)} | "
                            f"연속손절 {consecutive_losses}회 | "
                            f"세션손익 {self._session_realised_krw:+,.0f}원"
                        ),
                        (
                            f"{format_market_korean(market)} 신규 매수만 "
                            "중단합니다."
                        ),
                    ]
                )
            )
        if risk_controls_replayed:
            self._register_exit_cooldown(
                market,
                symbol,
                reason,
                pnl_pct=net_pnl_pct,
                occurred_at=execution_at,
                observed_at=confirmed_at,
            )
        else:
            self._save_event(
                event_type="historical_execution_risk_not_replayed",
                market=market,
                symbol=symbol,
                detail={
                    "execution_group_id": execution_group_id,
                    "execution_at": execution_at.isoformat(),
                    "execution_time_source": "kis_order_timestamp",
                    "reconciled_at": confirmed_at.isoformat(),
                    "confirmation_delay_sec": round(confirmation_delay_sec, 3),
                    "execution_risk_day": execution_risk_day.isoformat(),
                    "current_risk_day": current_risk_day.isoformat(),
                    "current_risk_day_pnl_krw": risk_summary.get(
                        "total_pnl_krw"
                    ),
                    "reason": "outside_30_minute_risk_replay_window",
                },
            )
        if is_full_group_fill:
            self._reset_strategy_position(symbol, market)
        delayed_labels = []
        if confirmation_delay_sec >= 60:
            delayed_labels.append(
                f"확인지연={confirmation_delay_sec / 60:.0f}분"
            )
        if not risk_controls_replayed:
            delayed_labels.append("위험제어=과거귀속")
        exit_reason = reason or exit_by
        exit_signal_labels = []
        if exit_by and exit_by != exit_reason:
            exit_signal_labels.append(f"신호={format_reason_korean(exit_by)}")
        self._queue_trade_notification(
            " ".join(
                [
                    format_market_korean(market),
                    self._format_trade_symbol_label(market, symbol),
                    "매도체결",
                    price_label,
                    f"x{filled_qty}",
                    f"수익률={format_pct(pnl_pct)}",
                    f"순손익={pnl_label}",
                    f"청산={format_reason_korean(exit_reason)}",
                    *exit_signal_labels,
                    *delayed_labels,
                ]
            )
        )
        self._save_event(
            event_type="execution_confirmed",
            market=market,
            symbol=symbol,
            detail={
                "execution_group_id": execution_group_id,
                "side": side,
                "filled_qty": filled_qty,
                "target_qty": target_qty,
                "avg_fill_price": round(fill_price, 8),
                "pnl_pct": round(pnl_pct, 8),
                "execution_at": execution_at.isoformat(),
                "execution_time_source": "kis_order_timestamp",
                "reconciled_at": confirmed_at.isoformat(),
                "confirmation_delay_sec": round(confirmation_delay_sec, 3),
                "execution_risk_day": execution_risk_day.isoformat(),
                "current_risk_day": current_risk_day.isoformat(),
                "risk_controls_replayed": risk_controls_replayed,
                "exit_reason": exit_reason,
                "exit_signal_by": exit_by,
            },
        )
        return True

    async def _handle_no_fill_execution_group(
        self,
        executions: list[dict],
    ) -> None:
        if not executions:
            return
        first = executions[0]
        market = str(first.get("market") or "")
        symbol = str(first.get("symbol") or "")
        side = str(first.get("side") or "").upper()
        context = (
            first.get("context_json")
            if isinstance(first.get("context_json"), dict)
            else {}
        )
        if self._is_virtual_sell_settlement_context(context):
            self._save_event(
                event_type="virtual_pending_settlement_no_fill",
                market=market,
                symbol=symbol,
                detail={
                    "execution_group_id": first.get("execution_group_id"),
                    "side": side,
                    "statuses": sorted(
                        {str(row.get("status") or "") for row in executions}
                    ),
                    "pending_preserved": True,
                    "retry_allowed": True,
                },
            )
            return
        if side == "BUY":
            self._reset_strategy_position(symbol, market)
        self._save_event(
            event_type="execution_no_fill",
            market=market,
            symbol=symbol,
            detail={
                "execution_group_id": first.get("execution_group_id"),
                "side": side,
                "statuses": sorted(
                    {str(row.get("status") or "") for row in executions}
                ),
            },
        )

    async def _run_cycle(self) -> LiquidityLabReport:
        now = datetime.now(timezone.utc)
        self._cycle_count = getattr(self, "_cycle_count", 0) + 1
        self._cycle_active_inverse_symbols = {}
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
        if not getattr(self, "_confirmed_risk_state_restored", False):
            restored = self._reconcile_confirmed_risk_day_pnl(
                now,
                restore_consecutive=True,
            )
            self._confirmed_risk_state_restored = bool(
                restored.get("reconciled")
            )
        if not getattr(
            self,
            "_confirmed_symbol_loss_state_restored",
            False,
        ):
            restored = self._reconcile_confirmed_symbol_loss_state(now)
            self._confirmed_symbol_loss_state_restored = bool(
                restored.get("reconciled")
            )
        await self._refresh_market_regimes(now)
        self._strategy_guard_blocked_keys()
        now = datetime.now(timezone.utc)
        await self._reconcile_broker_executions(now)
        await self._ensure_tv_diagnostics()
        now = datetime.now(timezone.utc)
        krx_holiday, nyse_holiday = await self._apply_holiday_overrides(now)
        await self._maybe_send_overseas_relist_alert(now, nyse_holiday=nyse_holiday)
        krx_open = is_krx_regular_session(now) and not krx_holiday
        us_open = is_us_regular_session(now) and not nyse_holiday
        us_session = get_us_trading_session(now)
        us_orderable_in_profile = is_us_orderable_session_for_env(
            now,
            self.config.credentials.env,
        ) and not nyse_holiday
        self._observe_inverse_regime(
            "domestic",
            market_open=krx_open,
            now=now,
        )
        self._observe_inverse_regime(
            "overseas",
            market_open=us_open,
            now=now,
        )

        cycle_start_krx_open = krx_open
        cycle_start_us_open = us_open
        cycle_start_us_session = us_session
        cycle_start_us_orderable = us_orderable_in_profile
        krx_cycle_open = krx_open
        us_cycle_open = us_open
        us_transition_guard_active = False
        us_transition_remaining_sec = seconds_until_us_session_transition(now)
        if (
            self.config.credentials.env != "prod"
            and us_cycle_open
            and us_transition_remaining_sec is not None
            and us_transition_remaining_sec <= _MIN_VPS_US_FULL_SCAN_WINDOW_SEC
        ):
            us_cycle_open = False
            us_transition_guard_active = True
            guard_key = (now.astimezone(KST).date().isoformat(), us_session)
            if guard_key != getattr(self, "_last_us_transition_guard_key", None):
                self._last_us_transition_guard_key = guard_key
                self._save_event(
                    event_type="market_session_transition_guard",
                    market="overseas",
                    detail={
                        "session": us_session,
                        "remaining_seconds": us_transition_remaining_sec,
                        "minimum_scan_window_seconds": _MIN_VPS_US_FULL_SCAN_WINDOW_SEC,
                        "profile": self.config.credentials.env,
                    },
                )

        if not krx_open and not us_open:
            corporate_action_symbols = (
                self._effective_virtual_corporate_action_symbols(now=now)
            )
            corporate_action_settled: list[str] = []
            corporate_action_api_calls = 0
            if corporate_action_symbols:
                await self._get_held_symbol_map()
                corporate_action_settled = (
                    await self._reconcile_effective_overseas_corporate_actions(
                        now=now,
                    )
                )
                balance_cache = getattr(
                    self,
                    "_overseas_balance_cache",
                    {},
                )
                balance_data = balance_cache.get("data")
                if (
                    balance_cache.get("cycle") == self._cycle_count
                    and isinstance(balance_data, dict)
                ):
                    corporate_action_api_calls = len(balance_data)
                else:
                    corporate_action_api_calls = 1
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
                estimated_api_calls_per_cycle=corporate_action_api_calls,
                domestic_order={"skipped": True, "reason": "market_closed"},
                overseas_order={
                    "skipped": True,
                    "reason": "market_closed",
                    **(
                        {
                            "corporate_action_symbols": corporate_action_symbols,
                            "corporate_action_settled": corporate_action_settled,
                        }
                        if corporate_action_symbols
                        else {}
                    ),
                },
            )

        if not krx_cycle_open and not us_cycle_open:
            return LiquidityLabReport(
                scanned_at=format_kst(now) or "",
                krx_market_open=krx_open,
                us_market_open=us_open,
                us_market_session=us_session,
                us_orderable_in_profile=us_orderable_in_profile,
                primary_market="none",
                primary_target=None,
                primary_selection_reason="us_session_transition_guard",
                domestic_ranked=[],
                overseas_ranked=[],
                domestic_excluded=[],
                overseas_excluded=[],
                domestic_positions=[],
                overseas_positions=[],
                watch_targets=[],
                estimated_api_calls_per_cycle=0,
                domestic_order={"skipped": True, "reason": "market_closed"},
                overseas_order={
                    "skipped": True,
                    "reason": "us_session_transition_guard",
                    "remaining_seconds": us_transition_remaining_sec,
                },
            )

        refreshed_position_markets: set[str] = set()
        domestic_scan_started = krx_cycle_open
        overseas_scan_started = us_cycle_open
        overseas_scan_scope = self._overseas_scan_scope_for_cycle(
            now=now,
            krx_open=krx_cycle_open,
            us_open=us_cycle_open,
            us_orderable_in_profile=us_orderable_in_profile,
        )
        self._overseas_scan_scope = overseas_scan_scope
        if overseas_scan_scope != getattr(
            self,
            "_last_logged_overseas_scan_scope",
            "",
        ):
            self._last_logged_overseas_scan_scope = overseas_scan_scope
            overseas_policy = self._get_market_policy("overseas").auto_trade
            self._save_event(
                event_type="market_scan_scope",
                market="overseas",
                detail={
                    "scope": overseas_scan_scope,
                    "krx_open": krx_cycle_open,
                    "us_open": us_cycle_open,
                    "profile_orderable": us_orderable_in_profile,
                    "full_scan_interval_seconds": max(
                        60,
                        int(
                            getattr(
                                overseas_policy,
                                "intraday_bar_minutes",
                                5,
                            )
                            or 5
                        )
                        * 60,
                    ),
                    "reason": (
                        "non_orderable_policy_bar_refresh"
                        if (
                            overseas_scan_scope == "full"
                            and not us_orderable_in_profile
                        )
                        else "orderable_profile"
                        if overseas_scan_scope == "full"
                        else "protect_krx_live_cadence"
                        if overseas_scan_scope == "monitored"
                        and krx_cycle_open
                        else "non_orderable_between_policy_bars"
                        if overseas_scan_scope == "monitored"
                        else "us_market_closed"
                    ),
                },
            )
        domestic_ranked = await self.scan_domestic() if domestic_scan_started else []
        domestic_positions = (
            await self._load_domestic_positions(domestic_ranked)
            if domestic_scan_started
            else []
        )
        domestic_balance_cache = getattr(self, "_domestic_balance_cache", {})
        if (
            domestic_scan_started
            and domestic_balance_cache.get("cycle") == getattr(self, "_cycle_count", 0)
            and domestic_balance_cache.get("data")
        ):
            refreshed_position_markets.add("domestic")
        if overseas_scan_started:
            overseas_scan_started_at = datetime.now(timezone.utc)
            overseas_ranked, held_symbols_cache = await self.scan_overseas()
            if (
                overseas_scan_scope == "full"
                and not us_orderable_in_profile
            ):
                self._last_non_orderable_full_scan_at = (
                    overseas_scan_started_at
                )
            overseas_positions = await self._load_overseas_positions(
                overseas_ranked,
                held_symbols_cache=held_symbols_cache,
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

        decision_now = datetime.now(timezone.utc)
        fresh_krx_open = is_krx_regular_session(decision_now) and not krx_holiday
        fresh_us_open = is_us_regular_session(decision_now) and not nyse_holiday
        fresh_us_session = get_us_trading_session(decision_now)
        fresh_us_orderable = is_us_orderable_session_for_env(
            decision_now,
            self.config.credentials.env,
        ) and not nyse_holiday
        session_changed_markets: set[str] = set()
        if fresh_krx_open != cycle_start_krx_open:
            krx_cycle_open = False
            session_changed_markets.add("domestic")
            self._save_event(
                event_type="market_session_changed_during_cycle",
                market="domestic",
                detail={
                    "cycle_started_at": format_kst(now),
                    "rechecked_at": format_kst(decision_now),
                    "from_open": cycle_start_krx_open,
                    "to_open": fresh_krx_open,
                },
            )
        if (
            fresh_us_open != cycle_start_us_open
            or fresh_us_session != cycle_start_us_session
            or fresh_us_orderable != cycle_start_us_orderable
        ):
            us_cycle_open = False
            session_changed_markets.add("overseas")
            self._save_event(
                event_type="market_session_changed_during_cycle",
                market="overseas",
                detail={
                    "cycle_started_at": format_kst(now),
                    "rechecked_at": format_kst(decision_now),
                    "from_open": cycle_start_us_open,
                    "to_open": fresh_us_open,
                    "from_session": cycle_start_us_session,
                    "to_session": fresh_us_session,
                    "from_orderable": cycle_start_us_orderable,
                    "to_orderable": fresh_us_orderable,
                },
            )
        krx_open = fresh_krx_open
        us_open = fresh_us_open
        us_session = fresh_us_session
        us_orderable_in_profile = fresh_us_orderable

        if us_cycle_open and us_orderable_in_profile:
            await self._reconcile_pending_virtual_sells(
                overseas_positions=overseas_positions,
                overseas_ranked=overseas_ranked,
            )

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
            krx_open=krx_cycle_open,
            us_open=us_cycle_open,
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
                profile_orderable=us_orderable_in_profile,
            )
            if us_cycle_open
            else []
        )
        overseas_exit_target = overseas_exit_targets[0] if overseas_exit_targets else None
        domestic_exit_target = (
            self._select_domestic_exit_target(
                domestic_ranked,
                domestic_watch_targets,
                domestic_positions,
            )
            if krx_cycle_open
            else None
        )
        overseas_entry_block_reason = ""
        overseas_entry_block_detail: dict[str, int] = {}
        config_ll = self.config.liquidity_lab
        open_domestic_symbols = {
            position.stock_code.strip()
            for position in domestic_positions
            if position.stock_code.strip() and position.quantity > 0
        }
        open_overseas_symbols = {
            position.symbol.strip().upper()
            for position in monitored_overseas_positions
            if position.symbol.strip() and position.quantity > 0
        }
        _max_total = int(getattr(config_ll, "max_concurrent_total_positions", 0) or 0)
        remaining_total_slots = WatchStateHelper.remaining_total_position_slots(
            open_domestic_count=len(open_domestic_symbols),
            open_overseas_count=len(open_overseas_symbols),
            max_total_positions=_max_total,
        )
        domestic_cb_halted = self._is_trading_halted("domestic")
        overseas_cb_halted = self._is_trading_halted("overseas")
        if domestic_cb_halted or overseas_cb_halted:
            _logger.info(
                "[CB] 시장별 서킷브레이커 상태 domestic=%s overseas=%s"
                " losses=%s session_pnl=%.0f",
                domestic_cb_halted,
                overseas_cb_halted,
                getattr(self, "_consecutive_losses_by_market", {}),
                getattr(self, "_session_realised_krw", 0.0),
            )
        domestic_budget = int(getattr(config_ll, "max_concurrent_domestic_orders", 2))
        if remaining_total_slots is not None:
            domestic_budget = min(domestic_budget, remaining_total_slots)
        domestic_reject_halted = self._is_order_reject_halted(
            market="domestic",
            side="buy",
        )
        if domestic_reject_halted or domestic_cb_halted or not krx_cycle_open:
            domestic_budget = 0
        domestic_buy_targets = self._select_domestic_buy_targets(
            domestic_ranked,
            domestic_watch_targets,
            max_concurrent=domestic_budget,
        )
        domestic_buy_target = domestic_buy_targets[0] if domestic_buy_targets else None
        domestic_entry_block_reason = ""
        domestic_entry_block_detail: dict[str, object] = {}
        if not domestic_buy_targets and krx_cycle_open:
            if domestic_cb_halted:
                domestic_entry_block_reason = "domestic_circuit_breaker_halted"
            elif domestic_reject_halted:
                domestic_entry_block_reason = "domestic_order_reject_halted"
            else:
                post_cb_reason, post_cb_detail = self._post_cb_reentry_regime_gate(
                    "domestic",
                )
                if post_cb_reason:
                    domestic_entry_block_reason = f"watch:{post_cb_reason}"
                    domestic_entry_block_detail = post_cb_detail
                else:
                    domestic_entry_block_reason = (
                        self._dominant_entry_wait_reason(
                            domestic_watch_targets,
                            market="domestic",
                        )
                        or (
                            "no_domestic_ready_signal"
                            if domestic_ranked
                            else "no_domestic_candidate"
                        )
                    )

        _max_os = getattr(config_ll, "max_concurrent_overseas_orders", 20)
        remaining_overseas_slots = self._remaining_overseas_entry_slots(
            monitored_overseas_positions,
            max_positions=_max_os,
        )
        overseas_reject_halted = self._is_order_reject_halted(
            market="overseas",
            side="buy",
        )
        if overseas_reject_halted or overseas_cb_halted:
            remaining_overseas_slots = 0
        if not us_cycle_open:
            remaining_overseas_slots = 0
        if overseas_scan_scope != "full":
            remaining_overseas_slots = 0
        total_cap_binds_overseas = False
        if remaining_total_slots is not None:
            overseas_total_budget = max(
                0,
                remaining_total_slots - len(domestic_buy_targets),
            )
            if overseas_total_budget < remaining_overseas_slots:
                remaining_overseas_slots = overseas_total_budget
                total_cap_binds_overseas = True
        if remaining_overseas_slots <= 0:
            overseas_entry_block_reason = (
                "market_session_changed_during_cycle"
                if "overseas" in session_changed_markets
                else "us_session_transition_guard"
                if us_transition_guard_active
                else "overseas_circuit_breaker_halted"
                if overseas_cb_halted
                else "overseas_order_reject_halted"
                if overseas_reject_halted
                else "overseas_monitor_only"
                if overseas_scan_scope == "monitored"
                else "total_position_cap_reached"
                if total_cap_binds_overseas
                else "overseas_position_cap_reached"
            )
            overseas_entry_block_detail = {
                "open_positions": len(open_overseas_symbols),
                "max_positions": int(_max_os),
            }
            if total_cap_binds_overseas:
                overseas_entry_block_detail["open_total_positions"] = (
                    len(open_domestic_symbols) + len(open_overseas_symbols)
                )
                overseas_entry_block_detail["max_total_positions"] = _max_total
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
            if not overseas_buy_targets:
                overseas_entry_block_reason = (
                    self._dominant_entry_wait_reason(
                        overseas_watch_targets,
                        market="overseas",
                    )
                    or (
                        "no_overseas_ready_signal"
                        if overseas_ranked
                        else "no_overseas_candidate"
                    )
                )
                overseas_entry_block_detail = {
                    "ranked_candidates": len(overseas_ranked),
                    "watch_targets": len(overseas_watch_targets),
                }
        domestic_order: dict = {
            "skipped": True,
            "reason": (
                "market_session_changed_during_cycle"
                if "domestic" in session_changed_markets
                else domestic_entry_block_reason or "no_action"
            ),
        }
        if domestic_entry_block_detail:
            domestic_order["entry_block_detail"] = domestic_entry_block_detail
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
        elif domestic_buy_targets and krx_cycle_open:
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
        elif overseas_buy_targets and us_cycle_open and us_orderable_in_profile:
            for buy_candidate in overseas_buy_targets:
                overseas_orders.append(
                    await self._manage_overseas_position(
                        candidate=buy_candidate,
                        held_positions=overseas_positions,
                        watch_target=overseas_watch_map.get(buy_candidate.symbol),
                    )
                )
            overseas_order = overseas_orders[0]
        elif overseas_buy_targets and us_cycle_open and not us_orderable_in_profile:
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
                "market_session_changed_during_cycle"
                if "overseas" in session_changed_markets
                else "us_session_transition_guard"
                if us_transition_guard_active
                else "overseas_monitor_only"
                if overseas_scan_scope == "monitored"
                else "us_open_but_mock_session_not_supported"
                if us_cycle_open and not us_orderable_in_profile
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
            eligible_markets={
                market
                for market, eligible in (
                    ("domestic", krx_cycle_open),
                    (
                        "overseas",
                        us_cycle_open and us_orderable_in_profile,
                    ),
                )
                if eligible
            },
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
        elif krx_cycle_open and us_cycle_open and watch_targets:
            primary_market = "both"
            primary_target = None
            primary_reason = "both_waiting"
        elif krx_cycle_open and domestic_watch_targets:
            primary_market = "domestic"
            primary_target = domestic_watch_targets[0].code
            primary_reason = "watchlist_wait"
        elif us_cycle_open and overseas_watch_targets:
            primary_market = "overseas"
            primary_target = overseas_watch_targets[0].code
            primary_reason = "watchlist_wait"
        elif session_changed_markets:
            primary_market = "none"
            primary_target = None
            primary_reason = "market_session_changed_during_cycle"
        elif us_transition_guard_active:
            primary_market = "none"
            primary_target = None
            primary_reason = "us_session_transition_guard"
        elif us_cycle_open and not us_orderable_in_profile:
            primary_market = "none"
            primary_target = None
            primary_reason = "us_open_but_mock_session_not_supported"
        elif krx_cycle_open:
            primary_market = "domestic"
            primary_target = None
            primary_reason = "krx_open_but_no_candidate"
        elif us_cycle_open:
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
                krx_open=domestic_scan_started,
                us_open=overseas_scan_started,
                domestic_watch_count=len(domestic_watch_targets),
                overseas_watch_count=len(overseas_watch_targets),
                include_domestic_order=bool(domestic_exit_target or domestic_buy_target),
                include_overseas_order=bool(overseas_exit_target or overseas_buy_target),
                overseas_scan_scope=overseas_scan_scope,
            ),
            domestic_order=domestic_order,
            overseas_order=overseas_order,
            overseas_scan_scope=overseas_scan_scope,
        )
        await self._send_summary(report)
        return report

    async def _get_domestic_balance_for_cycle(self) -> dict | None:
        cycle = int(getattr(self, "_cycle_count", 0) or 0)
        cache = getattr(self, "_domestic_balance_cache", {}) or {}
        cached_data = cache.get("data")
        if cache.get("cycle") == cycle and isinstance(cached_data, dict):
            return cached_data

        get_balance = getattr(getattr(self, "client", None), "get_balance", None)
        if not callable(get_balance):
            return cached_data if isinstance(cached_data, dict) else None
        try:
            balance = await get_balance()
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[POSITIONS] 국내 잔고 조회 실패 - %s (error=%s)",
                "이전 캐시로 대체"
                if isinstance(cached_data, dict)
                else "보유종목 없음으로 처리됨(캐시 없음)",
                exc,
            )
            self._save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol="",
                detail={
                    "reason": "domestic_balance_lookup_failed",
                    "error": str(exc)[:200],
                },
            )
            return cached_data if isinstance(cached_data, dict) else None

        self._domestic_balance_cache = {
            "cycle": cycle,
            "data": balance,
        }
        return balance

    def _held_domestic_codes(self) -> set[str]:
        # The current-cycle balance is primed before quote scanning and reused
        # by the position loader, so held symbols outside a dynamic rank stay
        # observable without adding a second balance API call.
        cache = getattr(self, "_domestic_balance_cache", {}) or {}
        data = cache.get("data") or {}
        rows = data.get("positions", []) or data.get("output1", [])
        codes: set[str] = set()
        for row in rows:
            if parse_kis_number(row.get("hldg_qty")) <= 0:
                continue
            code = str(row.get("pdno", "")).strip()
            if code:
                codes.add(code)
        return codes

    def _prepare_domestic_cycle_caches(self) -> None:
        cycle = int(getattr(self, "_cycle_count", 0) or 0)
        if getattr(self, "_domestic_quote_cache_cycle", -1) != cycle:
            self._domestic_quote_cache_cycle = cycle
            self._domestic_quote_cache = {}
        if getattr(self, "_domestic_minute_chart_cache_cycle", -1) != cycle:
            self._domestic_minute_chart_cache_cycle = cycle
            self._domestic_minute_chart_cache = {}

    async def _enrich_domestic_inverse_etf_metadata(
        self,
        candidate: DomesticScanResult,
    ) -> DomesticScanResult:
        if not self._is_approved_domestic_inverse_product(candidate):
            return candidate
        current = datetime.now(timezone.utc)
        cache = getattr(self, "_domestic_inverse_etf_cache", None)
        if cache is None:
            cache = {}
            self._domestic_inverse_etf_cache = cache
        cache_key = candidate.stock_code.strip()
        cached = cache.get(cache_key)
        ttl_seconds = max(
            1,
            int(
                getattr(
                    self._get_market_policy("domestic").auto_trade,
                    "intraday_chart_refresh_sec",
                    60,
                )
                or 60
            ),
        )
        metadata: dict
        if (
            isinstance(cached, tuple)
            and len(cached) == 2
            and isinstance(cached[0], datetime)
            and isinstance(cached[1], dict)
            and (current - cached[0]).total_seconds() <= ttl_seconds
        ):
            metadata = dict(cached[1])
        else:
            fetch = getattr(
                self.client,
                "get_etf_etn_current_price",
                None,
            )
            if not callable(fetch):
                metadata = {
                    "available": False,
                    "reason": "etf_metadata_api_unavailable",
                }
            else:
                try:
                    response = await fetch(
                        candidate.stock_code,
                        self.config.trading.market_code,
                    )
                    nav = self._parse_float(response.get("nav"))
                    tracking_multiplier = self._parse_float(
                        response.get("tracking_multiplier")
                    )
                    quote_price = self._parse_float(
                        response.get("current_price")
                    )
                    nav_deviation_pct = (
                        (float(candidate.current_price) - nav) / nav
                        if candidate.current_price > 0 and nav > 0
                        else None
                    )
                    metadata = {
                        "available": bool(
                            nav > 0
                            and quote_price > 0
                            and tracking_multiplier != 0
                        ),
                        "nav": nav if nav > 0 else None,
                        "nav_deviation_pct": nav_deviation_pct,
                        "tracking_multiplier": (
                            tracking_multiplier
                            if tracking_multiplier != 0
                            else None
                        ),
                        "etf_quote_price": (
                            quote_price if quote_price > 0 else None
                        ),
                        "reported_deviation_pct": response.get(
                            "reported_deviation_pct"
                        ),
                        "tracking_error_pct": response.get(
                            "tracking_error_pct"
                        ),
                        "captured_at": current.isoformat(),
                    }
                except Exception as exc:  # noqa: BLE001
                    metadata = {
                        "available": False,
                        "reason": "etf_metadata_lookup_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:200],
                    }
                    self._record_inverse_observation(
                        event_type="inverse_quote_failed",
                        market="domestic",
                        symbol=candidate.stock_code,
                        reason="inverse_etf_metadata_lookup_failed",
                        detail={
                            "stage": "etf_metadata",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:200],
                        },
                    )
            cache[cache_key] = (current, metadata)

        return replace(
            candidate,
            etf_nav=metadata.get("nav"),
            etf_nav_deviation_pct=metadata.get("nav_deviation_pct"),
            etf_tracking_multiplier=metadata.get("tracking_multiplier"),
            etf_metadata_available=bool(metadata.get("available")),
        )

    async def scan_domestic(self) -> list[DomesticScanResult]:
        self._prepare_domestic_cycle_caches()
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
        active_inverse_symbols = self._active_inverse_symbols("domestic")
        cycle_inverse_symbols = getattr(
            self,
            "_cycle_active_inverse_symbols",
            None,
        )
        if cycle_inverse_symbols is None:
            cycle_inverse_symbols = {}
            self._cycle_active_inverse_symbols = cycle_inverse_symbols
        cycle_inverse_symbols.setdefault("domestic", set()).update(
            active_inverse_symbols
        )
        open_inverse_shadow_symbols = self._open_inverse_shadow_symbols(
            "domestic"
        )
        for inverse_symbol in active_inverse_symbols:
            if inverse_symbol not in active_codes:
                active_codes.append(inverse_symbol)
        await self._get_domestic_balance_for_cycle()
        held_codes = self._held_domestic_codes()
        for held_code in sorted(held_codes):
            if held_code not in active_codes:
                active_codes.append(held_code)
        monitored_codes = held_codes | open_inverse_shadow_symbols
        quote_results: list[DomesticScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for stock_code in active_codes:
            try:
                candidate = await self._scan_single_domestic_quote(stock_code)
            except Exception as exc:  # noqa: BLE001
                if self._is_inverse_symbol("domestic", stock_code):
                    self._record_inverse_observation(
                        event_type="inverse_quote_failed",
                        market="domestic",
                        symbol=stock_code,
                        reason="inverse_quote_fetch_failed",
                        detail={
                            "stage": "quote",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:240],
                        },
                    )
                await asyncio.sleep(0.05)
                continue
            # Mirrors the overseas held-symbol exemption: a new-candidate
            # liquidity/spread filter must not also block a symbol we already
            # hold from getting a fresh quote/signal this cycle.
            reasons = (
                []
                if candidate.stock_code in monitored_codes
                else self._domestic_quote_speculative_reasons(candidate)
            )
            if (
                candidate.current_price
                < self.config.liquidity_lab.domestic_min_price_krw
                and self._is_approved_domestic_inverse_product(candidate)
            ):
                self._record_inverse_observation(
                    event_type="inverse_price_floor_exempted",
                    market="domestic",
                    symbol=stock_code,
                    reason="approved_inverse_liquidity_routing",
                    detail={
                        "stage": "quote_filter",
                        "generic_min_price_krw": (
                            self.config.liquidity_lab.domestic_min_price_krw
                        ),
                        "remaining_reasons": reasons,
                        "snapshot": asdict(candidate),
                    },
                )
            if not reasons and self._is_approved_domestic_inverse_product(
                candidate
            ):
                candidate = await self._enrich_domestic_inverse_etf_metadata(
                    candidate
                )
                self._domestic_quote_cache[candidate.stock_code] = candidate
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=stock_code,
                        reasons=reasons,
                        snapshot=asdict(candidate),
                    )
                )
                if self._is_inverse_symbol("domestic", stock_code):
                    self._record_inverse_observation(
                        event_type="inverse_quote_excluded",
                        market="domestic",
                        symbol=stock_code,
                        reason=str(reasons[0]),
                        detail={
                            "stage": "quote_filter",
                            "reasons": reasons,
                            "snapshot": asdict(candidate),
                        },
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
            except Exception as exc:  # noqa: BLE001
                if self._is_inverse_symbol("domestic", candidate.stock_code):
                    self._record_inverse_observation(
                        event_type="inverse_quote_failed",
                        market="domestic",
                        symbol=candidate.stock_code,
                        reason="inverse_signal_fetch_failed",
                        detail={
                            "stage": "signal",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:240],
                        },
                    )
                refined.append(candidate)
                await asyncio.sleep(0.05)
                continue
            reasons = (
                []
                if full_candidate.stock_code in monitored_codes
                else self._domestic_speculative_reasons(full_candidate)
            )
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=full_candidate.stock_code,
                        reasons=reasons,
                        snapshot=asdict(full_candidate),
                    )
                )
                if self._is_inverse_symbol("domestic", full_candidate.stock_code):
                    self._record_inverse_observation(
                        event_type="inverse_quote_excluded",
                        market="domestic",
                        symbol=full_candidate.stock_code,
                        reason=str(reasons[0]),
                        detail={
                            "stage": "signal_filter",
                            "reasons": reasons,
                            "snapshot": asdict(full_candidate),
                        },
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
        result = DomesticScanResult(
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
            product_type=str(current.get("product_type", "") or "").strip(),
            sector_name=str(current.get("sector_name", "") or "").strip(),
        )
        self._prepare_domestic_cycle_caches()
        self._domestic_quote_cache[stock_code] = result
        return result

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
        full_scan = getattr(self, "_overseas_scan_scope", "full") != "monitored"
        if full_scan:
            self._overseas_scan_cycle_count = (
                getattr(self, "_overseas_scan_cycle_count", 0) + 1
            )
            if (
                getattr(self, "_dynamic_overseas_pool", None) is None
                or self._overseas_scan_cycle_count
                >= max(1, int(getattr(config, "overseas_rescan_cycles", 20)))
            ):
                self._overseas_scan_cycle_count = 0
                self._tv_diagnostic_ran = False
                await self._refresh_overseas_dynamic_pool()

        held_symbol_map = await self._get_held_symbol_map()
        await self._reconcile_effective_overseas_corporate_actions()
        held_symbol_map = await self._get_held_symbol_map()
        virtual_symbols = self._get_virtual_held_symbols()
        fully_pending_signal_symbols = (
            self._fully_pending_overseas_signal_symbols(
                virtual_symbols=virtual_symbols,
            )
        )
        open_inverse_shadow_symbols = self._open_inverse_shadow_symbols(
            "overseas"
        )
        if full_scan:
            active_overseas_pool = self._active_overseas_pool(
                held_symbol_map=held_symbol_map,
                held_symbols=set(held_symbol_map.keys()) | virtual_symbols,
            )
        else:
            monitored_symbol_map = dict(held_symbol_map)
            for symbol in virtual_symbols:
                monitored_symbol_map.setdefault(symbol, "NASD")
            active_overseas_pool = self._monitored_overseas_pool(
                held_symbol_map=monitored_symbol_map,
                open_inverse_shadow_symbols=open_inverse_shadow_symbols,
            )
        active_overseas_symbols = {
            candidate.symbol.strip().upper()
            for candidate in active_overseas_pool
        }
        active_inverse_symbols = (
            self._active_inverse_symbols("overseas")
            if full_scan
            else set(open_inverse_shadow_symbols)
        )
        fully_pending_signal_symbols.difference_update(
            active_inverse_symbols
        )
        previous_pending_signal_symbols = set(
            getattr(
                self,
                "_last_fully_pending_signal_symbols",
                set(),
            )
        )
        if (
            fully_pending_signal_symbols
            != previous_pending_signal_symbols
            and (
                fully_pending_signal_symbols
                or previous_pending_signal_symbols
            )
        ):
            self._last_fully_pending_signal_symbols = set(
                fully_pending_signal_symbols
            )
            self._save_event(
                event_type="overseas_pending_signal_scan_scope",
                market="overseas",
                detail={
                    "symbols": sorted(fully_pending_signal_symbols),
                    "minute_chart_slots_skipped_per_refresh": len(
                        fully_pending_signal_symbols
                    ),
                    "quotes_preserved": True,
                    "settlement_monitoring_preserved": True,
                },
            )
        cycle_inverse_symbols = getattr(
            self,
            "_cycle_active_inverse_symbols",
            None,
        )
        if cycle_inverse_symbols is None:
            cycle_inverse_symbols = {}
            self._cycle_active_inverse_symbols = cycle_inverse_symbols
        cycle_inverse_symbols.setdefault("overseas", set()).update(
            active_inverse_symbols
        )
        for inverse_symbol in active_inverse_symbols:
            if inverse_symbol in active_overseas_symbols:
                continue
            active_overseas_pool.append(
                OverseasCandidateConfig(
                    symbol=inverse_symbol,
                    exchange_code=self._overseas_inverse_exchange_code(
                        inverse_symbol
                    ),
                )
            )
            active_overseas_symbols.add(inverse_symbol)
        self._last_overseas_scan_candidate_count = len(active_overseas_pool)
        held_symbols = set(held_symbol_map.keys()) | virtual_symbols
        monitored_symbols = held_symbols | open_inverse_shadow_symbols
        for candidate in active_overseas_pool:
            symbol = candidate.symbol.strip().upper()
            corporate_action = self._effective_corporate_action(
                "overseas",
                symbol,
            )
            if corporate_action is not None:
                excluded.append(
                    ExcludedCandidate(
                        market="overseas",
                        code=symbol,
                        reasons=["corporate_action_effective"],
                        snapshot={
                            "symbol": symbol,
                            "exchange_code": candidate.exchange_code,
                            "held": symbol in held_symbols,
                            "corporate_action": (
                                self._corporate_action_snapshot(
                                    corporate_action
                                )
                            ),
                        },
                    )
                )
                continue
            if symbol not in monitored_symbols:
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
                    if self._is_inverse_symbol("overseas", symbol):
                        self._record_inverse_observation(
                            event_type="inverse_quote_excluded",
                            market="overseas",
                            symbol=symbol,
                            reason=suppression_reason,
                            detail={
                                "stage": "signal_suppression",
                                "exchange_code": candidate.exchange_code,
                            },
                        )
                    continue
            try:
                scan_result = await self._scan_single_overseas(candidate)
            except Exception as exc:  # noqa: BLE001
                if self._is_inverse_symbol("overseas", symbol):
                    self._record_inverse_observation(
                        event_type="inverse_quote_failed",
                        market="overseas",
                        symbol=symbol,
                        reason="inverse_quote_or_signal_fetch_failed",
                        detail={
                            "stage": "quote_and_signal",
                            "exchange_code": candidate.exchange_code,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:240],
                        },
                    )
                await asyncio.sleep(0.05)
                continue
            # The speculative filter (thin volume/wide spread/low turnover) exists
            # to keep the bot from *buying into* an illiquid new candidate. It must
            # not also gate a symbol we already hold -- doing so drops the held
            # position out of quote_results, so it never gets a fresh chart signal
            # this cycle and the exit decision silently falls back to whatever
            # signal was last cached (surfaced to the user as "stale_signal_cache"),
            # even while its price keeps moving.
            reasons = (
                []
                if symbol in monitored_symbols
                else self._overseas_speculative_reasons(scan_result)
            )
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="overseas",
                        code=candidate.symbol,
                        reasons=reasons,
                        snapshot=asdict(scan_result),
                    )
                )
                if self._is_inverse_symbol("overseas", symbol):
                    self._record_inverse_observation(
                        event_type="inverse_quote_excluded",
                        market="overseas",
                        symbol=symbol,
                        reason=str(reasons[0]),
                        detail={
                            "stage": "speculative_filter",
                            "reasons": reasons,
                            "snapshot": asdict(scan_result),
                        },
                    )
            else:
                quote_results.append(scan_result)
            await asyncio.sleep(0.05)

        self._overseas_excluded = excluded
        if not quote_results:
            # Keep held symbols and existing cached signals alive when quote fetches
            # temporarily fail so exit/watch logic can continue using last-good
            # balance data and persisted signal context.
            if not monitored_symbols:
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
            if (
                symbol in held_symbols
                and symbol not in fully_pending_signal_symbols
            ):
                signal_symbols.add(symbol)

        passing_symbols = {
            result.symbol.upper()
            for result in quote_results
        }
        skipped_pending_signal_count = len(
            fully_pending_signal_symbols & passing_symbols
        )
        signal_symbols.update(
            symbol
            for symbol in active_inverse_symbols
            if symbol in passing_symbols
        )

        remaining_slots = max(
            0,
            top_n
            - len(signal_symbols)
            - skipped_pending_signal_count,
        )
        for result in quote_results:
            if remaining_slots <= 0:
                break
            symbol = result.symbol.upper()
            if (
                symbol in signal_symbols
                or symbol in fully_pending_signal_symbols
            ):
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
                is_held=symbol in monitored_symbols,
            )
            await asyncio.sleep(0.05)

        if full_scan:
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
        except Exception as exc:  # noqa: BLE001
            fallback = self._last_held_symbols or self._get_virtual_held_symbols()
            _logger.warning(
                "[POSITIONS] 해외 보유종목 사전조회 실패 - %s (error=%s)",
                "이전 캐시로 대체" if fallback else "캐시 없음",
                exc,
            )
            self._save_event(
                event_type="maintenance_skip",
                market="overseas",
                detail={
                    "reason": "overseas_held_symbol_lookup_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                    "fallback_symbol_count": len(fallback),
                },
            )
            return fallback

    async def _get_held_symbol_map(self) -> dict[str, str]:
        """
        Return current overseas holdings as symbol -> exchange_code mapping.

        Reuses the balance cache populated by `_get_held_symbols()` to avoid
        extra API calls and preserves the exchange recorded with virtual
        positions.
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
        manager = getattr(self, "virtual_trades", None)
        if manager is not None:
            for position in manager.list_positions("overseas"):
                if position.qty <= 0:
                    continue
                symbol = position.symbol.strip().upper()
                exchange_code = str(position.exchange_code or "NASD").strip().upper()
                if symbol:
                    result.setdefault(symbol, exchange_code or "NASD")
        for symbol in self._get_virtual_held_symbols():
            result.setdefault(symbol.strip().upper(), "NASD")
        return result

    def _fully_pending_overseas_signal_symbols(
        self,
        *,
        virtual_symbols: set[str] | None = None,
    ) -> set[str]:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(
            repository,
            "list_virtual_sell_pending",
        ):
            return set()
        try:
            pending_rows = repository.list_virtual_sell_pending(
                market="overseas",
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "fully_pending_overseas_signal_lookup_failed",
                exc_info=True,
            )
            return set()
        if not pending_rows:
            return set()

        quantities_by_key: dict[tuple[str, str], int] = {}
        cache = getattr(self, "_overseas_balance_cache", {})
        for requested_exchange, balance in (cache.get("data", {}) or {}).items():
            for row in (balance or {}).get("positions", []):
                quantity = int(parse_kis_number(row.get("ovrs_cblc_qty")))
                if quantity <= 0:
                    continue
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                if not symbol:
                    continue
                exchange_code = (
                    str(row.get("ovrs_excg_cd", "")).strip().upper()
                    or str(requested_exchange).strip().upper()
                )
                key = (symbol, exchange_code)
                quantities_by_key[key] = max(
                    quantities_by_key.get(key, 0),
                    quantity,
                )

        real_qty_by_symbol: dict[str, int] = {}
        for (symbol, _), quantity in quantities_by_key.items():
            real_qty_by_symbol[symbol] = (
                real_qty_by_symbol.get(symbol, 0) + quantity
            )

        virtual_held = {
            str(symbol).strip().upper()
            for symbol in (
                virtual_symbols
                if virtual_symbols is not None
                else self._get_virtual_held_symbols()
            )
            if str(symbol).strip()
        }
        fully_pending: set[str] = set()
        for row in pending_rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            pending_qty = int(row.get("qty", 0) or 0)
            real_qty = real_qty_by_symbol.get(symbol, 0)
            if (
                symbol
                and symbol not in virtual_held
                and real_qty > 0
                and pending_qty >= real_qty
            ):
                fully_pending.add(symbol)
        return fully_pending

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
        self._prepare_domestic_cycle_caches()
        quote_snapshot = self._domestic_quote_cache.get(stock_code)
        if quote_snapshot is None:
            quote_snapshot = await self._scan_single_domestic_quote(stock_code)
        target_date = datetime.now(timezone.utc).astimezone(KST).strftime("%Y%m%d")
        bars = await self.client.get_time_daily_chart(
            stock_code=stock_code,
            target_date=target_date,
            market_code=self.config.trading.market_code,
            include_previous="Y",
        )
        self._domestic_minute_chart_cache[stock_code] = list(bars)
        limited_bars = bars[: min(8, len(bars))]
        closes = [parse_kis_number(row.get("stck_prpr")) for row in limited_bars]
        volumes = [parse_kis_number(row.get("cntg_vol")) for row in limited_bars]
        earliest = closes[-1] if closes else 0
        latest = closes[0] if closes else quote_snapshot.current_price
        minute_change_pct = 0.0 if earliest <= 0 else (latest - earliest) / earliest
        intraday_turnover = int(quote_snapshot.intraday_turnover_krw or 0)
        volume_sum = sum(volumes)
        spread_pct = float(quote_snapshot.spread_pct or 0.0)
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
            current_price=int(quote_snapshot.current_price),
            best_ask=int(quote_snapshot.best_ask),
            best_bid=int(quote_snapshot.best_bid),
            spread_pct=spread_pct,
            minute_change_pct=minute_change_pct,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=volume_sum,
            activity_score=round(activity_score, 4),
            stock_name=quote_snapshot.stock_name,
            product_type=quote_snapshot.product_type,
            sector_name=quote_snapshot.sector_name,
            etf_nav=quote_snapshot.etf_nav,
            etf_nav_deviation_pct=quote_snapshot.etf_nav_deviation_pct,
            etf_tracking_multiplier=quote_snapshot.etf_tracking_multiplier,
            etf_metadata_available=quote_snapshot.etf_metadata_available,
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
        has_two_sided_quote = bid > 0 and ask > 0
        tight_spread_bonus = (
            1.0 if has_two_sided_quote and spread_pct < 0.001 else 0.0
        )
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

    def _oversized_overseas_position_qty_ceiling(self) -> int:
        app_config = getattr(self, "config", None)
        if app_config is None:
            return 0
        config = getattr(app_config, "liquidity_lab", object())
        auto_cfg = self._get_market_policy("overseas").auto_trade
        risk_cfg = getattr(app_config, "risk", object())
        capital_krw = float(getattr(risk_cfg, "operating_capital_krw", 0) or 0)
        fx_rate = float(getattr(auto_cfg, "usd_krw_fallback_rate", 1350.0) or 1350.0)
        slot_max_pct = float(getattr(config, "slot_max_pct", 0.2) or 0.2)
        min_price_usd = float(getattr(config, "overseas_min_price_usd", 5.0) or 5.0)
        if capital_krw <= 0 or fx_rate <= 0 or min_price_usd <= 0:
            return 0
        max_slot_usd = (capital_krw / fx_rate) * slot_max_pct
        return int(max_slot_usd / min_price_usd)

    async def _warn_if_overseas_position_oversized(
        self,
        position: OverseasHeldPosition,
    ) -> None:
        # A legitimate slot-sized buy can never exceed this ceiling by more
        # than a small margin; a position several times over it is a sign
        # something bypassed normal sizing entirely (e.g. the 2026-07-13
        # duplicate-order incident left a ~5,251-share CRAN position that
        # sat undetected for over a week before exit logic finally caught
        # it). Alert once per symbol so this gets noticed within hours next
        # time, not a week later.
        ceiling = self._oversized_overseas_position_qty_ceiling()
        if ceiling <= 0 or position.quantity <= ceiling * 3:
            return
        warned = getattr(self, "_oversized_position_warned", None)
        if warned is None:
            warned = set()
            self._oversized_position_warned = warned
        key = f"overseas:{position.symbol.upper()}"
        if key in warned:
            return
        warned.add(key)
        notional_usd = position.quantity * position.avg_price
        self._save_event(
            event_type="oversized_position_detected",
            market="overseas",
            symbol=position.symbol,
            detail={
                "quantity": position.quantity,
                "expected_ceiling": ceiling,
                "avg_price": position.avg_price,
                "notional_usd": round(notional_usd, 2),
            },
        )
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return
        try:
            await notifier.send(
                "⚠️ [KIS] 비정상 대형 포지션 감지\n"
                f"종목={position.symbol}(해외) 수량={position.quantity}주 "
                f"평단=${position.avg_price:.2f} 평가액=${notional_usd:,.0f}\n"
                f"정상 슬롯 기준 예상 최대치({ceiling}주)를 크게 초과 - "
                "중복주문/누적오류 가능성 점검 필요"
            )
        except Exception:  # noqa: BLE001
            _logger.debug("oversized_position_notify_failed", exc_info=True)

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
            previous_balance_map = cache.get("data") or {}
            balance_map: dict[str, dict] = {}
            for exchange_code in sorted(exchange_codes):
                try:
                    balance = await self.client.get_overseas_balance(
                        exchange_code=exchange_code,
                        currency_code="USD",
                    )
                    balance_map[exchange_code] = balance
                except Exception as exc:  # noqa: BLE001
                    # Dropping this exchange's holdings silently would hide a
                    # real held position from stop-loss/exit monitoring for
                    # this cycle. Fall back to its last known-good snapshot
                    # instead, matching the sibling domestic-balance handling.
                    fallback = previous_balance_map.get(exchange_code)
                    _logger.warning(
                        "[POSITIONS] 해외 잔고 조회 실패 - %s (exchange=%s, error=%s)",
                        "이전 캐시로 대체" if fallback else "해당 거래소 보유종목 누락(캐시 없음)",
                        exchange_code,
                        exc,
                    )
                    self._save_event(
                        event_type="maintenance_skip",
                        market="overseas",
                        symbol="",
                        detail={
                            "reason": "overseas_balance_lookup_failed",
                            "exchange_code": exchange_code,
                            "error": str(exc)[:200],
                        },
                    )
                    if fallback is not None:
                        balance_map[exchange_code] = fallback
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
                corporate_action = self._effective_corporate_action(
                    "overseas",
                    symbol,
                )
                if corporate_action is not None:
                    current_price = float(corporate_action.cash_consideration)
                else:
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
                position = OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=row_exchange_code,
                    quantity=quantity,
                    orderable_qty=orderable_qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    is_virtual=False,
                )
                positions_by_key[(symbol, row_exchange_code)] = position
                await self._warn_if_overseas_position_oversized(position)

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
        balance = await self._get_domestic_balance_for_cycle()
        if not balance:
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
            # The balance row already carries the stock's Korean name --
            # capture it into the same name map the Telegram formatters read
            # from. Without this, a held position that isn't in today's
            # volume/fluctuation-rank pool (the only other source of this
            # map) would display as a bare code forever, even though the
            # name was sitting right here on every balance refresh.
            stock_name = str(row.get("prdt_name", "") or "").strip()
            if stock_name:
                name_map = getattr(self, "_dynamic_domestic_names", None)
                if name_map is None:
                    name_map = {}
                    self._dynamic_domestic_names = name_map
                name_map.setdefault(stock_code, stock_name)
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
        profile_orderable: bool = True,
    ) -> list[tuple[OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot | None]]:
        return await self._get_watch_state_helper().select_overseas_exit_targets(
            overseas_ranked,
            held_positions,
            max_exits=max_exits,
            profile_orderable=profile_orderable,
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
        if (
            candidate.current_price < config.domestic_min_price_krw
            and not self._is_approved_domestic_inverse_product(candidate)
        ):
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
        if (
            candidate.current_price < config.domestic_min_price_krw
            and not self._is_approved_domestic_inverse_product(candidate)
        ):
            reasons.append("low_price_krw")
        if candidate.intraday_turnover_krw < config.domestic_min_intraday_turnover_krw:
            reasons.append("thin_intraday_turnover")
        if candidate.spread_pct > config.domestic_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    def _is_approved_domestic_inverse_product(
        self,
        candidate: DomesticScanResult,
    ) -> bool:
        symbol = str(getattr(candidate, "stock_code", "") or "").strip()
        if not symbol:
            return False
        return (
            self._is_inverse_symbol("domestic", symbol)
            and str(getattr(candidate, "product_type", "") or "").strip().upper()
            in {"ETF", "ETN"}
        )

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
            details = getattr(
                self,
                "_overseas_signal_unavailable_details",
                None,
            )
            if details:
                details.pop(key, None)
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
            details = getattr(
                self,
                "_overseas_signal_unavailable_details",
                None,
            )
            if details:
                details.pop(symbol, None)
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
        unavailable_details = getattr(
            self,
            "_overseas_signal_unavailable_details",
            None,
        )
        unavailable_detail = (
            dict(unavailable_details.get(symbol) or {})
            if isinstance(unavailable_details, dict)
            else {}
        )
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
                **unavailable_detail,
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
        signal_snapshot: MovingAverageSnapshot | None = None,
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
        guard = getattr(self, "_exit_price_shock_guard", None)
        guarded_state = guard.get(key) if isinstance(guard, dict) else None
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
        if guarded_state is not None:
            guarded_reference_price = float(
                guarded_state.get("reference_price", 0.0) or 0.0
            )
            if guarded_reference_price > 0:
                reference_price = guarded_reference_price

        if reference_price <= 0:
            return None

        shock_pct = (last_price - reference_price) / reference_price
        shock_threshold = float(
            getattr(self.config.liquidity_lab, "overseas_exit_price_shock_pct", 0.20)
        )
        if abs(shock_pct) <= shock_threshold:
            if guard is not None:
                guard.pop(key, None)
            return None

        if guard is None:
            guard = {}
            self._exit_price_shock_guard = guard
        previous = guard.get(key)
        confirm_pct = float(
            getattr(self.config.liquidity_lab, "overseas_exit_price_shock_confirm_pct", 0.02)
        )
        if previous is not None:
            previous_price = float(previous.get("price", 0.0) or 0.0)
            stable_repeat = (
                previous_price > 0
                and abs(last_price - previous_price) / reference_price <= confirm_pct
            )
            has_two_sided_quote = quote.bid > 0 and quote.ask > 0
            min_volume_ratio = float(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_exit_price_shock_min_volume_ratio",
                    0.5,
                )
            )
            min_bar_volume = int(
                getattr(
                    self.config.liquidity_lab,
                    "overseas_exit_price_shock_min_bar_volume",
                    10,
                )
            )
            bar_volume = float(
                getattr(signal_snapshot, "volume_last", 0.0) or 0.0
            )
            volume_ratio = float(
                getattr(signal_snapshot, "volume_ratio", 0.0) or 0.0
            )
            has_volume_confirmation = (
                bar_volume >= min_bar_volume
                and volume_ratio >= min_volume_ratio
            )
            if stable_repeat and (
                has_two_sided_quote or has_volume_confirmation
            ):
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
                        "two_sided_quote": has_two_sided_quote,
                        "bar_volume": bar_volume,
                        "volume_ratio": volume_ratio,
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

    def _clear_overseas_stop_loss_confirm(self, symbol: str) -> None:
        guard = getattr(self, "_stop_loss_confirm_guard", None)
        if guard:
            guard.pop(f"overseas:{symbol.strip().upper()}", None)

    def _overseas_stop_loss_confirm_reason(
        self,
        *,
        symbol: str,
        quote: OverseasScanResult,
        pnl_pct: float,
        signal_snapshot: MovingAverageSnapshot | None,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        holding_qty: int = 0,
    ) -> str | None:
        """
        Guard the fixed stop-loss from transient one-print dips.

        A stop triggered by a genuine breakdown (heavy sell volume on a down
        bar), a loss already past the hard floor, or a dip that persists into
        the next cycle exits immediately. Only a first-seen marginal dip with
        no volume confirmation waits one cycle so a single anomalous print
        does not sell a recoverable position.
        """
        config = self.config.liquidity_lab
        if not getattr(config, "overseas_stop_loss_confirm_enabled", True):
            return None
        stop_pct = float(config.overseas_stop_loss_pct)
        hard_multiplier = float(
            getattr(config, "overseas_stop_loss_hard_multiplier", 2.0) or 2.0
        )
        if pnl_pct <= -(stop_pct * hard_multiplier):
            self._clear_overseas_stop_loss_confirm(symbol)
            return None
        volume_confirm_ratio = float(
            getattr(config, "overseas_stop_loss_volume_confirm_ratio", 1.5) or 1.5
        )
        if (
            signal_snapshot is not None
            and float(getattr(signal_snapshot, "volume_ratio", 0.0) or 0.0)
            >= volume_confirm_ratio
            and float(getattr(signal_snapshot, "intraday_bar_return", 0.0) or 0.0) < 0
        ):
            self._clear_overseas_stop_loss_confirm(symbol)
            return None

        guard = getattr(self, "_stop_loss_confirm_guard", None)
        if guard is None:
            guard = {}
            self._stop_loss_confirm_guard = guard
        key = f"overseas:{symbol.strip().upper()}"
        now = datetime.now(timezone.utc)
        previous = guard.get(key)
        if previous is not None:
            max_age_sec = int(
                getattr(config, "overseas_stop_loss_confirm_max_age_sec", 600) or 600
            )
            seen_at = parse_datetime(str(previous.get("seen_at") or ""))
            age_sec = (
                (now - ensure_timezone(seen_at)).total_seconds()
                if seen_at is not None
                else max_age_sec + 1
            )
            if age_sec <= max_age_sec:
                guard.pop(key, None)
                self._save_event(
                    event_type="trade_guard",
                    market="overseas",
                    symbol=symbol,
                    detail={
                        "reason": "stop_loss_confirmed",
                        "pnl_pct": pnl_pct,
                        "last_price": float(quote.last_price or 0.0),
                        "waited_sec": round(age_sec, 1),
                    },
                )
                return None

        guard[key] = {
            "price": float(quote.last_price or 0.0),
            "pnl_pct": pnl_pct,
            "seen_at": now.isoformat(),
        }
        reason = f"stop_loss_confirm_wait:{pnl_pct:+.1%}"
        self._record_trade_skip(
            market="overseas",
            symbol=symbol,
            exchange_code=quote.exchange_code,
            reason=reason,
            side="sell",
            price=float(quote.last_price or 0.0),
            signal_snapshot=signal_snapshot,
            strategy_flag=strategy_flag,
            entry_by=entry_by,
            exit_by=exit_by,
            stock_name=symbol,
            activity_score=quote.activity_score,
            holding_qty=holding_qty,
        )
        return reason

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
        active_inverse_symbols = getattr(
            self,
            "_cycle_active_inverse_symbols",
            {},
        )
        for item in unified:
            market_inverse_symbols = active_inverse_symbols.get(
                item.market,
                set(),
            )
            if item.code.upper() not in market_inverse_symbols:
                continue
            pair = (item.market, item.code.upper())
            if pair in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(pair)
            remaining_slots = max(0, remaining_slots - 1)

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
                watch_state = self._get_watch_state_helper()
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
                if (
                    held is not None
                    and watch_state.is_fully_pending_overseas_position(
                        held,
                        effective_qty=holding_qty,
                    )
                ):
                    persisted = (
                        watch_state.get_persisted_symbol_state(
                            "overseas",
                            symbol,
                        )
                        or {}
                    )
                    watch_target = self._make_watch_target_status(
                        market="overseas",
                        code=candidate.symbol,
                        exchange_code=candidate.exchange_code,
                        price=candidate.last_price,
                        activity_score=candidate.activity_score,
                        signal_score=0.0,
                        action_bias="WAIT",
                        signal_state="SETTLEMENT_PENDING",
                        ma_summary=(
                            self._ma_relation_summary(
                                signal_snapshot,
                                "overseas",
                            )
                            if signal_snapshot is not None
                            else "-"
                        ),
                        note="virtual_sell_pending",
                        holding_qty=0,
                        signal_snapshot=signal_snapshot,
                        strategy_flag=str(
                            persisted.get("strategy_flag", "")
                            or ""
                        ),
                        entry_by=str(
                            persisted.get("entry_by", "")
                            or ""
                        ),
                        decision_reason="virtual_sell_pending",
                        is_virtual=False,
                    )
                    watch_targets.append(watch_target)
                    self._save_cycle_log_from_watch_target(
                        watch_target,
                        pnl_pct=None,
                    )
                    continue
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

    def _get_persisted_symbol_state(self, market: str, symbol: str) -> dict | None:
        return self._get_watch_state_helper().get_persisted_symbol_state(market, symbol)

    def _prime_cycle_exit_reference_prices(
        self,
        overseas_positions: list[OverseasHeldPosition],
    ) -> None:
        self._get_watch_state_helper().prime_cycle_exit_reference_prices(overseas_positions)

    async def _get_overseas_signal_for_candidate(
        self,
        candidate: OverseasScanResult,
    ) -> MovingAverageSnapshot | None:
        return await self._get_watch_state_helper().get_overseas_signal_for_candidate(candidate)

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
        auto = self._get_market_policy("overseas").auto_trade
        symbol = candidate.symbol.strip().upper()
        unavailable_details = getattr(
            self,
            "_overseas_signal_unavailable_details",
            None,
        )
        if unavailable_details is None:
            unavailable_details = {}
            self._overseas_signal_unavailable_details = unavailable_details
        unavailable_details.pop(symbol, None)
        captured_at = datetime.now(timezone.utc)
        daily_rows = self._fresh_daily_chart_rows(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            now=captured_at,
            refresh_sec=auto.daily_chart_refresh_sec,
        )
        failure_stage = "daily_chart"
        try:
            if daily_rows is None:
                daily_rows = await self.client.get_overseas_daily_prices(
                    candidate.symbol,
                    candidate.exchange_code,
                    adjusted_price=True,
                )
                if daily_rows:
                    self._store_daily_chart_rows(
                        market="overseas",
                        symbol=candidate.symbol,
                        exchange_code=candidate.exchange_code,
                        rows=daily_rows,
                        captured_at=captured_at,
                    )
            failure_stage = "minute_chart"
            minute_rows = await self.client.get_overseas_minute_chart(
                candidate.symbol,
                candidate.exchange_code,
                interval_minutes=auto.intraday_bar_minutes,
                include_previous_day=True,
                record_count=max(
                    auto.intraday_slow_window + 8,
                    auto.breakout_lookback_bars + 6,
                    auto.bollinger_window + 4,
                    auto.atr_window + 4,
                    40,
                ),
            )
        except KisApiError as exc:
            unavailable_details[symbol] = {
                "unavailable_reason": "chart_api_error",
                "failure_stage": failure_stage,
                "error_type": type(exc).__name__,
            }
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
        vwap_series = extract_price_series(
            filter_latest_session_rows(
                minute_rows,
                date_fields=("xymd", "kymd"),
            ),
            close_fields=("last", "clos", "close"),
            high_fields=("high",),
            low_fields=("low",),
            volume_fields=("evol", "volume"),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        history_detail = {
            "daily_rows": len(daily_closes),
            "daily_required": int(auto.daily_slow_window),
            "minute_rows": len(minute_closes),
            "minute_required": int(auto.intraday_slow_window),
        }
        if len(daily_closes) < auto.daily_slow_window:
            unavailable_details[symbol] = {
                "unavailable_reason": "daily_history_short",
                **history_detail,
            }
            return None
        if len(minute_closes) < auto.intraday_slow_window:
            unavailable_details[symbol] = {
                "unavailable_reason": "minute_history_short",
                **history_detail,
            }
            return None
        bar_duration_sec = max(1, int(auto.intraday_bar_minutes)) * 60
        chart_elapsed_sec = chart_bar_elapsed_seconds(
            minute_rows,
            now=datetime.now(timezone.utc),
            bar_duration_sec=bar_duration_sec,
            timestamp_fields=(
                ("kymd", "khms", KST),
                ("xymd", "xhms", NEW_YORK),
            ),
        )

        return build_moving_average_snapshot(
            price=candidate.last_price,
            bid=candidate.bid,
            ask=candidate.ask,
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=auto.daily_fast_window,
            daily_slow_window=auto.daily_slow_window,
            intraday_fast_window=auto.intraday_fast_window,
            intraday_slow_window=auto.intraday_slow_window,
            volatility_window=auto.volatility_window,
            momentum_window=auto.momentum_window,
            volume_window=auto.volume_window,
            rsi_period=auto.rsi_period,
            breakout_lookback_bars=auto.breakout_lookback_bars,
            bollinger_window=auto.bollinger_window,
            bollinger_stddev=auto.bollinger_stddev,
            atr_window=auto.atr_window,
            vwap_closes=vwap_series.closes,
            vwap_highs=vwap_series.highs,
            vwap_lows=vwap_series.lows,
            vwap_volumes=vwap_series.volumes,
            bar_duration_sec=bar_duration_sec,
            chart_elapsed_sec=chart_elapsed_sec,
        )

    async def _load_domestic_signal(
        self,
        candidate: DomesticScanResult,
    ) -> MovingAverageSnapshot | None:
        auto = self._get_market_policy("domestic").auto_trade
        captured_at = datetime.now(timezone.utc)
        now_kst = captured_at.astimezone(KST)
        target_date = now_kst.strftime("%Y%m%d")
        start_date = (now_kst - timedelta(days=200)).strftime("%Y%m%d")
        daily_rows = self._fresh_daily_chart_rows(
            market="domestic",
            symbol=candidate.stock_code,
            now=captured_at,
            refresh_sec=auto.daily_chart_refresh_sec,
        )
        self._prepare_domestic_cycle_caches()
        minute_rows = self._domestic_minute_chart_cache.get(
            candidate.stock_code
        )
        try:
            if daily_rows is None:
                daily_rows = await self.client.get_daily_chart(
                    stock_code=candidate.stock_code,
                    start_date=start_date,
                    end_date=target_date,
                    market_code=self.config.trading.market_code,
                )
                if daily_rows:
                    self._store_daily_chart_rows(
                        market="domestic",
                        symbol=candidate.stock_code,
                        rows=daily_rows,
                        captured_at=captured_at,
                    )
            if minute_rows is None:
                minute_rows = await self.client.get_time_daily_chart(
                    stock_code=candidate.stock_code,
                    target_date=target_date,
                    market_code=self.config.trading.market_code,
                    include_previous="Y",
                )
                self._domestic_minute_chart_cache[candidate.stock_code] = list(
                    minute_rows
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
        vwap_series = extract_price_series(
            filter_latest_session_rows(
                minute_rows,
                date_fields=("stck_bsop_date",),
            ),
            close_fields=("stck_prpr",),
            high_fields=("stck_hgpr",),
            low_fields=("stck_lwpr",),
            volume_fields=("cntg_vol",),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        if (
            len(daily_closes) < auto.daily_slow_window
            or len(minute_closes) < auto.intraday_slow_window
        ):
            return None
        chart_elapsed_sec = chart_bar_elapsed_seconds(
            minute_rows,
            now=datetime.now(timezone.utc),
            bar_duration_sec=60,
            timestamp_fields=(("stck_bsop_date", "stck_cntg_hour", KST),),
        )

        return build_moving_average_snapshot(
            price=float(candidate.current_price),
            bid=float(candidate.best_bid),
            ask=float(candidate.best_ask),
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=auto.daily_fast_window,
            daily_slow_window=auto.daily_slow_window,
            intraday_fast_window=auto.intraday_fast_window,
            intraday_slow_window=auto.intraday_slow_window,
            volatility_window=auto.volatility_window,
            momentum_window=auto.momentum_window,
            volume_window=auto.volume_window,
            rsi_period=auto.rsi_period,
            breakout_lookback_bars=auto.breakout_lookback_bars,
            bollinger_window=auto.bollinger_window,
            bollinger_stddev=auto.bollinger_stddev,
            atr_window=auto.atr_window,
            vwap_closes=vwap_series.closes,
            vwap_highs=vwap_series.highs,
            vwap_lows=vwap_series.lows,
            vwap_volumes=vwap_series.volumes,
            bar_duration_sec=60,
            chart_elapsed_sec=chart_elapsed_sec,
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
            market="overseas",
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
        market: str = "overseas",
        take_profit_override: float | None = None,
    ) -> tuple[bool, str]:
        exit_setup = self._build_exit_setup(
            snapshot,
            pnl_pct,
            1,
            symbol=symbol,
            market=market,
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
        overseas_ranked: list[OverseasScanResult] | None = None,
        now: datetime | None = None,
    ) -> None:
        current = ensure_timezone(now or datetime.now(timezone.utc))
        pending_rows = self.repository.list_virtual_sell_pending(market="overseas")
        if not pending_rows:
            return

        real_by_symbol = {
            position.symbol.upper(): position
            for position in overseas_positions
            if not position.is_virtual
        }
        quote_by_symbol = {
            quote.symbol.upper(): quote
            for quote in (overseas_ranked or [])
        }
        virtual_manager = getattr(self, "virtual_trades", None)

        for row in pending_rows:
            symbol = str(row["symbol"]).upper()
            pending_qty = int(row["qty"])
            pending_avg_price = float(row["avg_sell_price"])
            exchange_code = row.get("exchange_code")
            currency = str(row["currency"])
            if symbol in getattr(
                self,
                "_untracked_virtual_settlement_symbols",
                set(),
            ):
                continue
            active_settlement = self._find_unfinalized_virtual_sell_settlement(
                symbol
            )
            if active_settlement is not None:
                self._clear_no_orderable_retry("overseas", symbol)
                self._reset_no_orderable_stall("overseas", symbol)
                await self._cancel_stale_virtual_sell_settlement(
                    execution=active_settlement,
                    exchange_code=str(
                        exchange_code
                        or active_settlement.get("exchange_code")
                        or ""
                    ),
                    now=current,
                )
                continue

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

            orderable_qty = 0 if real is None else real.orderable_qty
            settle_qty = min(pending_qty, orderable_qty)
            if real is not None and pending_qty > 0 and orderable_qty <= 0:
                self._track_no_orderable_stall(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=real.quantity,
                )
                self._defer_no_orderable_position(
                    market="overseas",
                    symbol=symbol,
                    holding_qty=real.quantity,
                    orderable_qty=orderable_qty,
                    cause="pending_virtual_sell_reconcile_zero_qty",
                    note=(
                        "profile-orderable session but pending virtual sell "
                        "has zero broker orderable quantity"
                    ),
                )
                continue
            if settle_qty > 0 and real is not None:
                strategy_flag = str(row.get("strategy_flag") or "")
                entry_by = str(row.get("entry_by") or "")
                entry_reason = str(row.get("entry_reason") or "")
                entry_time = str(row.get("entry_time") or "") or None
                if not all((strategy_flag, entry_by, entry_reason, entry_time)):
                    buy_context = self.repository.get_latest_confirmed_buy_context(
                        market="overseas",
                        symbol=symbol,
                        before_logged_at=current.astimezone(timezone.utc).isoformat(),
                        entry_price=real.avg_price,
                    )
                    if buy_context is not None:
                        strategy_flag = strategy_flag or str(
                            buy_context.get("strategy_flag") or ""
                        )
                        entry_by = entry_by or str(
                            buy_context.get("entry_by") or ""
                        )
                        entry_reason = entry_reason or str(
                            buy_context.get("action_reason") or ""
                        )
                        entry_time = entry_time or str(
                            buy_context.get("entry_time")
                            or buy_context.get("logged_at")
                            or ""
                        ) or None
                retry_allowed, retry_detail = self._virtual_settlement_retry_gate(
                    symbol=symbol,
                    now=current,
                )
                if not retry_allowed:
                    self._record_virtual_settlement_deferred(
                        symbol=symbol,
                        detail=retry_detail,
                    )
                    continue
                pending_started_at = parse_datetime(
                    str(row.get("updated_at") or "")
                )
                history_after = ensure_timezone(
                    pending_started_at or current
                ).astimezone(timezone.utc).isoformat()
                history = (
                    self.repository.get_virtual_settlement_submission_history(
                        market="overseas",
                        symbol=symbol,
                        after_created_at=history_after,
                    )
                    if hasattr(
                        self.repository,
                        "get_virtual_settlement_submission_history",
                    )
                    else {
                        "submission_count": 0,
                        "submission_session_count": 0,
                    }
                )
                retry_policy = self._virtual_settlement_retry_policy()
                failed_session_count = int(
                    history.get("submission_session_count") or 0
                )
                aggressive = (
                    failed_session_count
                    >= retry_policy["aggressive_after_sessions"]
                )
                quote = quote_by_symbol.get(symbol)
                quote_volume = int(
                    (quote.volume if quote is not None else 0) or 0
                )
                if aggressive and quote is not None and quote_volume <= 0:
                    zero_volume_detail = {
                        "session_date": self._market_session_date(
                            "overseas",
                            current,
                        ),
                        "submission_count": int(
                            history.get("submission_count") or 0
                        ),
                        "failed_session_count": failed_session_count,
                        "aggressive_after_sessions": retry_policy[
                            "aggressive_after_sessions"
                        ],
                        "reason": "zero_volume_after_repeated_no_fill",
                        "quote_last": float(quote.last_price or 0.0),
                        "quote_bid": float(quote.bid or 0.0),
                        "quote_volume": quote_volume,
                    }
                    if self._record_virtual_settlement_deferred(
                        symbol=symbol,
                        detail=zero_volume_detail,
                    ):
                        await self.notifier.send(
                            "\n".join(
                                [
                                    "[KIS][VIRTUAL_SETTLEMENT_DEFERRED]",
                                    f"시각={format_kst_korean(current)}",
                                    f"시장={format_market_korean('overseas')}",
                                    f"종목={symbol}",
                                    "상태=반복 미체결 후 현재 세션 거래량 0",
                                    f"미체결세션={failed_session_count}일",
                                    f"정산대기={pending_qty}주",
                                    "조치=무효 주문 반복 중단·거래량 회복 시 자동 재시도",
                                ]
                            )
                        )
                    continue
                quote_last = float(
                    (quote.last_price if quote is not None else 0.0) or 0.0
                )
                quote_bid = float(
                    (quote.bid if quote is not None else 0.0) or 0.0
                )
                reference_price = quote_bid or quote_last or real.current_price
                settlement_price = reference_price
                if aggressive:
                    discount_base = quote_last or real.current_price
                    aggressive_floor = discount_base * (
                        1.0
                        - retry_policy["aggressive_limit_bps"] / 10_000.0
                    )
                    settlement_price = min(reference_price, aggressive_floor)
                settlement_price = round(max(0.0001, settlement_price), 4)
                order_kind = "aggressive_limit" if aggressive else "limit"
                try:
                    response = (
                        await self.client.place_overseas_order_for_current_session(
                            side="sell",
                            symbol=symbol,
                            exchange_code=(exchange_code or real.exchange_code),
                            qty=settle_qty,
                            price=f"{settlement_price:.4f}",
                            order_division="00",
                        )
                    )
                except KisApiError as exc:
                    self._track_no_orderable_stall(
                        market="overseas",
                        symbol=symbol,
                        holding_qty=real.quantity,
                    )
                    self._defer_no_orderable_position(
                        market="overseas",
                        symbol=symbol,
                        holding_qty=real.quantity,
                        orderable_qty=orderable_qty,
                        cause="pending_virtual_sell_reconcile_rejected",
                        note=(
                            "broker rejected pending virtual sell settlement "
                            "during profile-orderable session"
                        ),
                        error=str(exc),
                    )
                    continue

                self._clear_no_orderable_retry("overseas", symbol)
                self._reset_no_orderable_stall("overseas", symbol)
                execution = self._record_broker_order_event(
                    market="overseas",
                    symbol=symbol,
                    exchange_code=(exchange_code or real.exchange_code),
                    side="SELL",
                    order_kind=order_kind,
                    requested_qty=settle_qty,
                    requested_price=settlement_price,
                    strategy_flag=strategy_flag,
                    entry_by=entry_by,
                    exit_by=_VIRTUAL_SELL_SETTLEMENT_ROLE,
                    status="SUBMITTED",
                    reason=_VIRTUAL_SELL_SETTLEMENT_ROLE,
                    payload={
                        "response": response,
                        "pending_qty": pending_qty,
                        "virtual_sell_avg_price": pending_avg_price,
                        "settlement_pricing": {
                            "reference_price": reference_price,
                            "quote_last": quote_last,
                            "quote_bid": quote_bid,
                            "requested_price": settlement_price,
                            "aggressive": aggressive,
                            "failed_session_count": failed_session_count,
                            "aggressive_limit_bps": retry_policy[
                                "aggressive_limit_bps"
                            ],
                        },
                    },
                    execution_context={
                        "execution_role": _VIRTUAL_SELL_SETTLEMENT_ROLE,
                        "virtual_sell_avg_price": pending_avg_price,
                        "pending_qty_at_submission": pending_qty,
                        "entry_price": real.avg_price,
                        "strategy_flag": strategy_flag,
                        "entry_by": entry_by,
                        "entry_reason": entry_reason,
                        "entry_time": entry_time,
                        "fx_rate": float(
                            getattr(
                                self._get_market_policy(
                                    "overseas"
                                ).auto_trade,
                                "usd_krw_fallback_rate",
                                1380.0,
                            )
                            or 1380.0
                        ),
                        "orderable_qty": real.orderable_qty,
                        "real_qty_at_submission": real.quantity,
                        "stock_name": symbol,
                        "currency": currency,
                        "session_id": getattr(self, "_session_id", ""),
                        "cycle_no": getattr(self, "_cycle_count", 0),
                        "is_session_trade": 0,
                        "settlement_pricing": {
                            "reference_price": reference_price,
                            "requested_price": settlement_price,
                            "aggressive": aggressive,
                            "failed_session_count": failed_session_count,
                        },
                    },
                )
                broker_order_no = self._extract_broker_order_no(response)
                if execution is None:
                    untracked = getattr(
                        self,
                        "_untracked_virtual_settlement_symbols",
                        set(),
                    )
                    untracked.add(symbol)
                    self._untracked_virtual_settlement_symbols = untracked
                    self._save_event(
                        event_type="virtual_pending_settlement_tracking_failed",
                        market="overseas",
                        symbol=symbol,
                        detail={
                            "reason": "accepted_order_missing_execution_ledger",
                            "requested_qty": settle_qty,
                            "broker_order_no": broker_order_no,
                            "pending_preserved": True,
                        },
                    )
                    await self.notifier.send(
                        "\n".join(
                            [
                                "[KIS][VIRTUAL_SETTLEMENT_TRACKING_FAILED]",
                                f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                                f"시장={format_market_korean('overseas')}",
                                f"종목={symbol}",
                                f"접수수량={settle_qty}주",
                                "상태=주문 접수 후 체결원장 생성 실패",
                                "조치=정산대기 유지·자동 재제출 중단",
                            ]
                        )
                    )
                    continue

                self._save_event(
                    event_type="virtual_pending_settlement_submitted",
                    market="overseas",
                    symbol=symbol,
                    detail={
                        "execution_group_id": execution.get(
                            "execution_group_id"
                        ),
                        "broker_order_no": broker_order_no,
                        "requested_qty": settle_qty,
                        "requested_price": settlement_price,
                        "order_kind": order_kind,
                        "quote_bid": quote_bid,
                        "quote_last": quote_last,
                        "failed_session_count": failed_session_count,
                        "aggressive_limit_bps": (
                            retry_policy["aggressive_limit_bps"]
                            if aggressive
                            else 0
                        ),
                        "pending_qty": pending_qty,
                        "pending_preserved_until_fill": True,
                        "submission_number": int(
                            retry_detail.get("submission_count") or 0
                        )
                        + 1,
                        "retry_policy": retry_detail,
                    },
                )
                await self.notifier.send(
                    "\n".join(
                        [
                            "[KIS][VIRTUAL_SETTLEMENT_SUBMITTED]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            f"시장={format_market_korean('overseas')}",
                            f"종목={symbol}",
                            "구분=정산매도 접수",
                            f"접수수량={settle_qty}주",
                            f"주문번호={broker_order_no}",
                            f"주문가={format_usd(settlement_price)}",
                            f"주문방식={order_kind}",
                            f"가상매도가={format_usd(pending_avg_price)}",
                            f"매입가={format_usd(real.avg_price)}",
                            f"정산대기={pending_qty}주",
                            "참고=접수 상태이며 KIS 체결내역 확인 전에는 정산하지 않음",
                        ]
                    )
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
            skip_label = (
                "주문거부"
                if action["action_raw"] in {"BUY_REJECTED", "SELL_REJECTED"}
                else "미실행"
            )
            lines.append(f"{skip_label}={skip_count}건 ({skip_top_reasons})")
        if action.get("replacement_note"):
            lines.append(f"참고={action['replacement_note']}")
        if action["action_raw"] in {"BUY_REJECTED", "SELL_REJECTED"}:
            lines.append("참고=주문이 거부되어 실제로 체결되지 않았습니다")
        if action["action_raw"] == "WAIT" and skip_count > 0 and not self._should_send_repeated_skip_notice(
            market=display_market_key,
            symbol=str(action.get("symbol_raw") or display_target),
            signature=skip_top_reasons or str(action.get("reason_raw") or ""),
        ):
            return
        await self.notifier.send("\n".join(lines))

    def _should_send_repeated_skip_notice(self, *, market: str, symbol: str, signature: str) -> bool:
        # A held position can be perpetually ineligible to exit (e.g. net profit
        # after fees stays <= 0 while price is flat), which would otherwise emit
        # an identical no-execution notice every scan cycle forever. Collapse repeats
        # of the same (market, symbol, reason) into one notice per cooldown window.
        last_notified = getattr(self, "_repeated_skip_notify_last", None)
        if last_notified is None:
            last_notified = {}
            self._repeated_skip_notify_last = last_notified
        now = datetime.now(timezone.utc)
        key = (market, symbol, signature)
        cooldown_minutes = max(
            1,
            int(getattr(self.config.risk, "repeated_skip_notify_cooldown_minutes", 30) or 30),
        )
        last = last_notified.get(key)
        if last is not None and now - ensure_timezone(last) < timedelta(minutes=cooldown_minutes):
            return False
        last_notified[key] = now
        return True

    def _iter_leaf_orders(self, order_result: dict | None):
        if not order_result:
            return
        batched_orders = order_result.get("batched_orders")
        if isinstance(batched_orders, list) and batched_orders:
            for item in batched_orders:
                yield from self._iter_leaf_orders(item)
            return
        yield order_result

    _IGNORED_SKIP_REASONS = frozenset(
        {
            "",
            "no_action",
            "market_closed",
            "no_overseas_candidate",
            "krx_open_but_no_candidate",
            "us_open_but_no_candidate",
            "us_open_but_mock_session_not_supported",
            "overseas_monitor_only",
            "us_session_transition_guard",
            "market_session_changed_during_cycle",
            "market_closed_during_cycle",
            # Reaching a configured concurrency cap is the risk limit working as
            # designed, not a broker rejection -- don't badge it "주문거부".
            "overseas_position_cap_reached",
            "total_position_cap_reached",
            "recent_sell_awaiting_broker_confirmation",
            "recent_full_sell_balance_pending",
        }
    )

    @classmethod
    def _order_has_meaningful_skip(cls, order: dict | None) -> bool:
        if not order or order.get("submitted"):
            return False
        if not order.get("skipped") and not order.get("error"):
            return False
        reason = str(order.get("reason") or order.get("error") or "unknown")
        if reason.startswith("watch:"):
            return False
        return reason not in cls._IGNORED_SKIP_REASONS

    def _summarize_skipped_orders(self, report: LiquidityLabReport) -> tuple[int, str]:
        reason_counts: dict[str, int] = {}
        for root in (report.domestic_order or {}, report.overseas_order or {}):
            for order in self._iter_leaf_orders(root):
                if not self._order_has_meaningful_skip(order):
                    continue
                reason = str(order.get("reason") or order.get("error") or "unknown")
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
        primary_is_overseas = report.primary_market == "overseas"
        primary_is_domestic = report.primary_market == "domestic"
        primary_order = overseas_order if primary_is_overseas else domestic_order if primary_is_domestic else None
        primary_currency = "USD" if primary_is_overseas else "KRW"
        other_order = domestic_order if primary_is_overseas else overseas_order
        other_currency = "KRW" if primary_is_overseas else "USD"
        # Prefer the primary market's own order when it's actually skipping for
        # a real reason. A skip/error can also exist only in the *non-primary*
        # market's order tree (e.g. this cycle's top-ranked candidate is
        # overseas, but an unrelated domestic position is the one stuck
        # skipping every cycle) — falling through to the generic WAIT dict
        # below would then display an unrelated rotating symbol
        # (primary_target) and drop symbol_raw/market_raw entirely, which both
        # mislabels the notification and defeats the repeated-skip notify
        # cooldown (its dedup key relies on symbol_raw being correct).
        if primary_order and self._order_has_meaningful_skip(primary_order):
            return self._format_order_summary(primary_order, currency=primary_currency)
        if other_order and self._order_has_meaningful_skip(other_order):
            return self._format_order_summary(other_order, currency=other_currency)
        if primary_order and (primary_order.get("skipped") or primary_order.get("error")):
            return self._format_order_summary(primary_order, currency=primary_currency)
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
            elif side in {"BUY", "SELL"} and reason_raw in {
                "session_not_orderable_in_profile",
                "order_rejected",
                "no_orderable_qty",
            }:
                action = f"{side}_REJECTED"
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

    @staticmethod
    def _strategy_manager_key(market: str, symbol: str) -> str:
        return f"{normalize_market_name(market)}:{symbol.strip().upper()}"

    def _get_strategy_manager(
        self,
        symbol: str,
        market: str = "overseas",
    ) -> PriorityStrategyManager:
        key = self._strategy_manager_key(market, symbol)
        managers = getattr(self, "_strategy_managers", None)
        if managers is None:
            managers = {}
            self._strategy_managers = managers
        manager = managers.get(key)
        if manager is None:
            manager = self._get_market_policy(market).make_strategy_manager()
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
        session_id = str(getattr(self, "_session_id", "") or "").strip()
        loaded_for = str(
            getattr(self, "_session_owned_symbols_loaded_for_session", "") or ""
        )
        if session_id and loaded_for != session_id:
            loader = getattr(
                getattr(self, "repository", None),
                "list_confirmed_session_buy_symbols",
                None,
            )
            restored = loader(session_id=session_id) if callable(loader) else []
            owned = {
                str(symbol).strip().upper()
                for symbol in restored
                if str(symbol).strip()
            }
            self._session_owned_symbols = owned
            self._session_owned_symbols_loaded_for_session = session_id
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
        market: str = "overseas",
    ) -> None:
        if snapshot is None:
            return
        manager = self._get_strategy_manager(symbol, market)
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

    def _reset_strategy_position(
        self,
        symbol: str,
        market: str = "overseas",
    ) -> None:
        manager = getattr(self, "_strategy_managers", {}).get(
            self._strategy_manager_key(market, symbol)
        )
        if manager is not None:
            manager.reset()

    def _get_strategy_labels(
        self,
        symbol: str,
        snapshot: MovingAverageSnapshot | None,
        market: str = "overseas",
    ) -> tuple[str, str, str]:
        manager = getattr(self, "_strategy_managers", {}).get(
            self._strategy_manager_key(market, symbol)
        )
        if manager is None:
            if snapshot is None:
                return "", "", ""
            preview = self._get_strategy_manager(symbol, market).evaluate(
                symbol,
                snapshot,
                commit=False,
            )
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

    def _estimate_hold_cycles(
        self,
        symbol: str,
        market: str = "overseas",
    ) -> int:
        manager = getattr(self, "_strategy_managers", {}).get(
            self._strategy_manager_key(market, symbol)
        )
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

    def _domestic_commission_rate(self) -> float:
        auto_trade = self._get_market_policy("domestic").auto_trade
        legacy = float(getattr(auto_trade, "commission_rate", 0.0025) or 0.0025)
        return float(getattr(auto_trade, "domestic_commission_rate", 0.00015) or legacy)

    def _overseas_commission_rate(self) -> float:
        auto_trade = self._get_market_policy("overseas").auto_trade
        legacy = float(getattr(auto_trade, "commission_rate", 0.0025) or 0.0025)
        return float(getattr(auto_trade, "overseas_commission_rate", legacy) or legacy)

    def _domestic_sell_tax_rate(self) -> float:
        auto_trade = self._get_market_policy("domestic").auto_trade
        return float(getattr(auto_trade, "domestic_sell_tax_rate", 0.0) or 0.0)

    def _sec_fee_rate(self) -> float:
        auto_trade = self._get_market_policy("overseas").auto_trade
        return float(getattr(auto_trade, "sec_fee_rate", 0.0000206) or 0.0)

    def _fx_fee_rate(self) -> float:
        auto_trade = self._get_market_policy("overseas").auto_trade
        return float(getattr(auto_trade, "fx_fee_rate", 0.0) or 0.0)

    def _estimate_domestic_net_pnl_krw(
        self,
        *,
        entry_price: float,
        exit_price: float,
        qty: int,
        product_type: str = "",
    ) -> tuple[float, float]:
        estimate = estimate_domestic_trade_costs(
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            commission_rate=self._domestic_commission_rate(),
            stock_sell_tax_rate=self._domestic_sell_tax_rate(),
            product_type=product_type,
        )
        return estimate.net_pnl_krw, round(estimate.sell_cost_krw, 2)

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

    def _cb_active_flag(self, market: str | None = None) -> int:
        cb = self._get_circuit_breaker()
        halted = cb.is_halted(market)
        self._sync_circuit_breaker_legacy_state(cb)
        return int(halted)

    def _consecutive_losses_for_market(self, market: str) -> int:
        losses = getattr(self, "_consecutive_losses_by_market", None)
        if isinstance(losses, dict):
            normalized = normalize_market_name(market)
            if normalized in losses:
                return int(losses.get(normalized, 0) or 0)
        return int(getattr(self, "_consecutive_losses", 0) or 0)

    def _register_order_rejection(self, *, market: str, side: str, error: str = "") -> None:
        cb = self._get_circuit_breaker()
        tripped = cb.record_order_rejection(market=market, side=side, error=error)
        if not tripped:
            return
        risk = getattr(self.config, "risk", None)
        threshold = int(getattr(risk, "order_reject_threshold", 5) or 5)
        window_minutes = int(getattr(risk, "order_reject_window_minutes", 15) or 15)
        cooldown_minutes = int(getattr(risk, "order_reject_cooldown_minutes", 30) or 30)
        _logger.warning(
            "[CB] 주문거부 서킷브레이커 발동 대상=%s/%s 최근%d분 %d회 이상 error=%s",
            market,
            side,
            window_minutes,
            threshold,
            error,
        )
        notifier = getattr(self, "notifier", None)
        if notifier is not None and getattr(notifier, "enabled", True):
            asyncio.create_task(
                notifier.send(
                    "⛔ 주문거부 서킷브레이커 발동\n"
                    f"대상={format_market_korean(market)}/{side}\n"
                    f"최근 {window_minutes}분 내 {threshold}회 이상 주문거부\n"
                    f"오류={str(error)[:150]}\n"
                    f"조치={cooldown_minutes}분간 해당 시장/방향 신규 주문 중단"
                )
            )

    def _is_order_reject_halted(self, *, market: str, side: str) -> bool:
        cb = self._get_circuit_breaker()
        return cb.is_order_reject_halted(market=market, side=side)

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
        eligible_markets: set[str] | None = None,
    ) -> None:
        runtime = self._get_runtime_manager()
        runtime.record_cycle_trade_frequency(
            domestic_orders=domestic_orders,
            overseas_orders=overseas_orders,
            eligible_markets=eligible_markets,
        )
        self._sync_runtime_legacy_state(runtime)

    @staticmethod
    def _dominant_entry_wait_reason(
        watch_targets: list[WatchTargetStatus],
        *,
        market: str,
    ) -> str:
        policy_markers = (
            "recent_strategy_underperformance",
            "standalone_",
            "entry_market_",
            "entry_benchmark_",
            "post_cb_",
            "corporate_action_",
        )
        all_counts: dict[str, int] = {}
        policy_counts: dict[str, int] = {}
        market_key = normalize_market_name(market)
        for target in watch_targets:
            if (
                normalize_market_name(target.market) != market_key
                or target.action_bias != "WAIT"
                or int(target.holding_qty or 0) > 0
            ):
                continue
            reason = str(target.note or target.signal_state or "wait").strip()
            if not reason:
                reason = "wait"
            all_counts[reason] = all_counts.get(reason, 0) + 1
            if any(marker in reason for marker in policy_markers):
                policy_counts[reason] = policy_counts.get(reason, 0) + 1
        selected = policy_counts or all_counts
        if not selected:
            return ""
        reason = min(
            selected,
            key=lambda item: (-selected[item], item),
        )
        return f"watch:{reason}"

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

    def _suppress_recent_full_sell_stale_balance(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
        orderable_qty: int = 0,
        defer_no_orderable: bool = True,
        now: datetime | None = None,
        window_sec: int | None = None,
    ) -> bool:
        """Suppress a duplicate sell while KIS balance lags a confirmed full fill."""
        if holding_qty <= 0:
            return False
        if window_sec is None:
            default_minutes = 30 if market.strip().lower() == "overseas" else 10
            try:
                market_auto_trade = self._get_market_policy(market).auto_trade
                configured_minutes = getattr(
                    market_auto_trade,
                    "post_fill_stale_balance_minutes",
                    default_minutes,
                )
                window_sec = max(1, int(configured_minutes)) * 60
            except (AttributeError, RuntimeError, ValueError):
                window_sec = default_minutes * 60
        repository = getattr(self, "repository", None)
        getter = getattr(repository, "get_recent_completed_sell_execution", None)
        if not callable(getter):
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(
            seconds=max(1, int(window_sec))
        )
        try:
            execution = getter(
                market=market,
                symbol=symbol,
                after_updated_at=cutoff.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[EXECUTION] recent_sell_lookup_failed market=%s symbol=%s error=%s",
                market,
                symbol,
                exc,
            )
            return False
        if execution is None:
            return False
        filled_qty = int(execution.get("filled_qty") or 0)
        target_qty = int(execution.get("target_qty") or 0)
        if filled_qty < target_qty or filled_qty < int(holding_qty):
            return False

        execution_group_id = str(execution.get("execution_group_id") or "")
        notice_keys = getattr(self, "_post_fill_balance_notice_keys", None)
        if notice_keys is None:
            notice_keys = set()
            self._post_fill_balance_notice_keys = notice_keys
        if execution_group_id not in notice_keys:
            notice_keys.add(execution_group_id)
            self._save_event(
                event_type="post_fill_stale_balance_suppressed",
                market=market,
                symbol=symbol,
                detail={
                    "reason": "recent_full_sell_balance_pending",
                    "execution_group_id": execution_group_id,
                    "target_qty": target_qty,
                    "filled_qty": filled_qty,
                    "stale_holding_qty": int(holding_qty),
                    "stale_orderable_qty": int(orderable_qty),
                    "execution_updated_at": execution.get("latest_updated_at"),
                    "window_sec": max(1, int(window_sec)),
                },
            )
        if defer_no_orderable:
            self._defer_no_orderable_position(
                market=market,
                symbol=symbol,
                holding_qty=int(holding_qty),
                orderable_qty=int(orderable_qty),
            )
        return True

    def _suppress_recent_pending_sell_stale_balance(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
        orderable_qty: int = 0,
        defer_no_orderable: bool = True,
        now: datetime | None = None,
        window_sec: int = 480,
    ) -> bool:
        """Defer a duplicate sell while an accepted sell awaits final history."""
        if holding_qty <= 0:
            return False
        repository = getattr(self, "repository", None)
        getter = getattr(
            repository,
            "get_recent_unfinalized_sell_execution",
            None,
        )
        if not callable(getter):
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(
            seconds=max(1, int(window_sec))
        )
        try:
            execution = getter(
                market=market,
                symbol=symbol,
                after_created_at=cutoff.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[EXECUTION] recent_pending_sell_lookup_failed "
                "market=%s symbol=%s error=%s",
                market,
                symbol,
                exc,
            )
            return False
        if execution is None:
            return False
        pending_requested_qty = int(
            execution.get("pending_requested_qty") or 0
        )
        if pending_requested_qty < int(holding_qty):
            return False

        execution_group_id = str(execution.get("execution_group_id") or "")
        notice_keys = getattr(self, "_post_submit_balance_notice_keys", None)
        if notice_keys is None:
            notice_keys = set()
            self._post_submit_balance_notice_keys = notice_keys
        if execution_group_id not in notice_keys:
            notice_keys.add(execution_group_id)
            self._save_event(
                event_type="post_submit_stale_balance_suppressed",
                market=market,
                symbol=symbol,
                detail={
                    "reason": "recent_sell_awaiting_broker_confirmation",
                    "execution_group_id": execution_group_id,
                    "target_qty": int(execution.get("target_qty") or 0),
                    "pending_requested_qty": pending_requested_qty,
                    "filled_qty": int(execution.get("filled_qty") or 0),
                    "stale_holding_qty": int(holding_qty),
                    "stale_orderable_qty": int(orderable_qty),
                    "execution_created_at": execution.get(
                        "latest_created_at"
                    ),
                    "window_sec": max(1, int(window_sec)),
                },
            )
        if defer_no_orderable:
            self._defer_no_orderable_position(
                market=market,
                symbol=symbol,
                holding_qty=int(holding_qty),
                orderable_qty=int(orderable_qty),
            )
        return True

    def _get_entry_context(
        self,
        market: str,
        symbol: str,
        *,
        fallback_price: float | None = None,
    ) -> tuple[float | None, str | None, float | None]:
        manager = getattr(self, "_strategy_managers", {}).get(
            self._strategy_manager_key(market, symbol)
        )
        entry_price: float | None = None
        entry_time_iso: str | None = None
        hold_duration_min: float | None = None
        if fallback_price is not None and fallback_price > 0:
            # The broker balance average is execution-derived. Strategy state
            # may still carry the submitted quote, which is not a fill price.
            entry_price = float(fallback_price)
        if manager is not None and manager.position is not None:
            if entry_price is None:
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
                if entry_price is None and raw_entry_price is not None:
                    try:
                        entry_price = float(raw_entry_price)
                    except (TypeError, ValueError):
                        entry_price = None
                entry_dt = self._get_watch_state_helper().resolve_position_entry_time(
                    market,
                    symbol,
                    persisted,
                )
                if entry_dt is not None:
                    entry_time_iso = entry_dt.isoformat()
                    hold_duration_min = round(
                        max(
                            0.0,
                            (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60,
                        ),
                        2,
                    )
        return entry_price, entry_time_iso, hold_duration_min

    def _defer_no_orderable_position(
        self,
        *,
        market: str,
        symbol: str,
        holding_qty: int,
        orderable_qty: int,
        cause: str = "broker_orderable_qty_zero",
        note: str = (
            "unrepresented holding has zero sellable quantity; "
            "check open orders or broker balance state"
        ),
        error: str = "",
    ) -> bool:
        runtime = self._get_runtime_manager()
        deferred = runtime.defer_no_orderable_position(
            market=market,
            symbol=symbol,
            holding_qty=holding_qty,
            orderable_qty=orderable_qty,
            cause=cause,
            note=note,
            error=error,
        )
        self._sync_runtime_legacy_state(runtime)
        return deferred

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
            consecutive_losses=self._consecutive_losses_for_market(market),
            orderable_qty=orderable_qty,
            stock_name=stock_name,
            exit_cooldown_remaining=self._cooldown_remaining_minutes(market, symbol),
            cb_active=self._cb_active_flag(market),
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
            "cb_active": self._cb_active_flag(market),
        }
        records_policy_regime = (
            reason.startswith("entry_market_")
            or reason.startswith("entry_benchmark_")
            or reason.startswith("post_cb_")
            or reason == "recent_strategy_underperformance"
        )
        if side == "buy" and records_policy_regime:
            detail["entry_market_regime"] = self._market_regime_context(
                market
            )
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
        *,
        pnl_pct: float | None = None,
        occurred_at: datetime | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        runtime = self._get_runtime_manager()
        runtime.register_exit_cooldown(
            market,
            symbol,
            exit_reason,
            pnl_pct=pnl_pct,
            occurred_at=occurred_at,
            observed_at=observed_at,
        )
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

    def _is_trading_halted(self, market: str | None = None) -> bool:
        cb = self._get_circuit_breaker()
        halted = cb.is_halted(market)
        self._sync_circuit_breaker_legacy_state(cb)
        return halted

    def _ma_relation_summary(
        self,
        snapshot: MovingAverageSnapshot,
        market: str = "overseas",
    ) -> str:
        auto = self._get_market_policy(market).auto_trade
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
        overseas_scan_scope: str = "full",
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
            estimated_calls += max(0, int(domestic_watch_count or 0))
            if include_domestic_order:
                estimated_calls += 1
        if us_open:
            active_overseas_candidates = self._active_overseas_pool()
            n_candidates = int(
                getattr(self, "_last_overseas_scan_candidate_count", 0) or 0
            ) or len(
                active_overseas_candidates
            )
            top_n = max(config.overseas_scan_top_n, 1)
            estimated_calls += n_candidates
            estimated_calls += min(top_n, n_candidates) * 2
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in active_overseas_candidates
            }
            estimated_calls += len(exchange_codes)
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

    @staticmethod
    def _parse_optional_float(value: object) -> float | None:
        if value is None:
            return None
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    def _build_exit_setup(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
        position_qty: int,
        *,
        symbol: str = "",
        market: str = "overseas",
        take_profit_override: float | None = None,
    ):
        policy = self._get_market_policy(market)
        policy_config = policy.auto_trade
        if self._is_inverse_symbol(market, symbol):
            policy_config = replace(
                policy_config,
                take_profit_pct=float(
                    getattr(policy_config, "inverse_take_profit_pct", 0.02)
                    or 0.02
                ),
                stop_loss_pct=float(
                    getattr(policy_config, "inverse_stop_loss_pct", 0.0075)
                    or 0.0075
                ),
                hard_stop_loss_pct=float(
                    getattr(
                        policy_config,
                        "inverse_hard_stop_loss_pct",
                        0.012,
                    )
                    or 0.012
                ),
                max_hold_cycles=max(
                    1,
                    int(
                        getattr(
                            policy_config,
                            "inverse_max_hold_cycles",
                            24,
                        )
                        or 24
                    ),
                ),
            )
        elif take_profit_override is not None:
            policy_config = replace(
                policy_config,
                take_profit_pct=take_profit_override,
            )
        return evaluate_exit_setup(
            policy_config,
            snapshot,
            pnl_pct,
            market=market,
            drawdown_from_peak=0.0,
            hold_cycles=self._estimate_hold_cycles(symbol, market) if symbol else 0,
            position_qty=position_qty,
            partial_exit_done=False,
        )
