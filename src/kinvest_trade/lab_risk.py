from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable

from .config import AppConfig
from .market_sessions import KST
from .time_utils import ensure_timezone

_logger = logging.getLogger(__name__)

EventHook = Callable[[str, dict], None]
NotifyHook = Callable[[str], Awaitable[object] | object]

_UNSET = object()


class CircuitBreakerManager:
    """Manage consecutive-loss and daily-loss circuit-breaker state."""

    def __init__(
        self,
        config: AppConfig,
        *,
        event_hook: EventHook | None = None,
        notify_hook: NotifyHook | None = None,
    ) -> None:
        self._config = config
        self._event_hook = event_hook
        self._notify_hook = notify_hook
        self.consecutive_losses: int = 0
        self._consecutive_losses_by_market: dict[str, int] = {}
        self.session_realised_krw: float = 0.0
        self.session_realised_krw_overseas: float = 0.0
        self.daily_loss_date: date | None = None
        self._halted_at: datetime | None = None
        self._halted_at_by_market: dict[str, datetime] = {}
        self._daily_halted_at: datetime | None = None
        self._last_cb_released_at: datetime | None = None
        self._last_cb_released_at_by_market: dict[str, datetime] = {}
        self._overseas_cb_active: bool = False
        self._order_reject_history: dict[str, list[datetime]] = {}
        self._order_reject_halted_at: dict[str, datetime] = {}

    def load_state(
        self,
        *,
        consecutive_losses: int | None = None,
        consecutive_losses_by_market: dict[str, int] | None = None,
        session_realised_krw: float | None = None,
        session_realised_krw_overseas: float | None = None,
        daily_loss_date: date | None = None,
        halted_at: datetime | None | object = _UNSET,
        halted_at_by_market: dict[str, datetime] | None = None,
        daily_halted_at: datetime | None | object = _UNSET,
        last_cb_released_at: datetime | None | object = _UNSET,
        last_cb_released_at_by_market: dict[str, datetime] | None = None,
        overseas_cb_active: bool | None = None,
        order_reject_history: dict[str, list[datetime]] | None = None,
        order_reject_halted_at: dict[str, datetime] | None = None,
    ) -> None:
        if consecutive_losses is not None:
            self.consecutive_losses = int(consecutive_losses)
        if consecutive_losses_by_market is not None:
            self._consecutive_losses_by_market = {
                self._normalize_market(market): max(0, int(count))
                for market, count in consecutive_losses_by_market.items()
                if str(market).strip()
            }
            self._sync_aggregate_consecutive_losses()
        if session_realised_krw is not None:
            self.session_realised_krw = float(session_realised_krw)
        if session_realised_krw_overseas is not None:
            self.session_realised_krw_overseas = float(session_realised_krw_overseas)
        self.daily_loss_date = daily_loss_date
        if halted_at is not _UNSET:
            self._halted_at = halted_at
        if halted_at_by_market is not None:
            self._halted_at_by_market = {
                self._normalize_market(market): ensure_timezone(value)
                for market, value in halted_at_by_market.items()
                if str(market).strip() and value is not None
            }
            self._sync_aggregate_halted_at()
        if daily_halted_at is not _UNSET:
            self._daily_halted_at = daily_halted_at
        if last_cb_released_at is not _UNSET:
            self._last_cb_released_at = last_cb_released_at
        if last_cb_released_at_by_market is not None:
            self._last_cb_released_at_by_market = {
                self._normalize_market(market): ensure_timezone(value)
                for market, value in last_cb_released_at_by_market.items()
                if str(market).strip() and value is not None
            }
        if overseas_cb_active is not None:
            self._overseas_cb_active = bool(overseas_cb_active)
        if order_reject_history is not None:
            self._order_reject_history = {
                str(key): [ensure_timezone(value) for value in values]
                for key, values in order_reject_history.items()
                if str(key).strip()
            }
        if order_reject_halted_at is not None:
            self._order_reject_halted_at = {
                str(key): ensure_timezone(value)
                for key, value in order_reject_halted_at.items()
                if str(key).strip() and value is not None
            }

    def snapshot(self) -> dict[str, object]:
        return {
            "consecutive_losses": self.consecutive_losses,
            "consecutive_losses_by_market": dict(self._consecutive_losses_by_market),
            "session_realised_krw": self.session_realised_krw,
            "session_realised_krw_overseas": self.session_realised_krw_overseas,
            "daily_loss_date": self.daily_loss_date,
            "halted_at": self._halted_at,
            "halted_at_by_market": dict(self._halted_at_by_market),
            "daily_halted_at": self._daily_halted_at,
            "last_cb_released_at": self._last_cb_released_at,
            "last_cb_released_at_by_market": dict(
                self._last_cb_released_at_by_market
            ),
            "overseas_cb_active": self._overseas_cb_active,
            "order_reject_history": {
                key: list(values)
                for key, values in self._order_reject_history.items()
            },
            "order_reject_halted_at": dict(self._order_reject_halted_at),
        }

    @property
    def halted_at(self) -> datetime | None:
        return self._halted_at

    @property
    def daily_halted_at(self) -> datetime | None:
        return self._daily_halted_at

    @property
    def last_cb_released_at(self) -> datetime | None:
        return self._last_cb_released_at

    @property
    def overseas_cb_active(self) -> bool:
        return self._overseas_cb_active

    @property
    def is_active(self) -> bool:
        return bool(self._halted_at_by_market) or self._halted_at is not None or self._daily_halted_at is not None

    def is_halted(self, market: str | None = None) -> bool:
        self._maybe_reset_daily()
        risk = getattr(self._config, "risk", None)
        if risk is None:
            return False
        if market is not None:
            normalized_market = self._normalize_market(market)
            if self._check_consecutive(risk, market=normalized_market):
                return True
        elif self._consecutive_losses_by_market:
            for known_market in sorted(self._consecutive_losses_by_market):
                if self._check_consecutive(risk, market=known_market):
                    return True
        elif self._check_consecutive(risk, market=None):
            return True
        return self._check_daily(risk)

    def is_daily_halted(self, now: datetime | None = None) -> bool:
        self._maybe_reset_daily(now)
        risk = getattr(self._config, "risk", None)
        return False if risk is None else self._check_daily(risk, now=now)

    def overseas_allowed(self) -> bool:
        released_at = self._last_cb_released_at
        if released_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - ensure_timezone(released_at)).total_seconds() / 60
        return elapsed >= 12.5

    def on_realised(
        self,
        *,
        market: str,
        realized_pnl_krw: float,
        pnl_pct: float | None = None,
        include_session_pnl: bool = True,
    ) -> None:
        if include_session_pnl:
            self._maybe_reset_daily()
        normalized_market = self._normalize_market(market)
        is_loss = pnl_pct < 0 if pnl_pct is not None else realized_pnl_krw < 0
        if is_loss:
            self._consecutive_losses_by_market[normalized_market] = (
                self._consecutive_losses_by_market.get(normalized_market, 0) + 1
            )
        else:
            self._consecutive_losses_by_market[normalized_market] = 0
        self._sync_aggregate_consecutive_losses()
        if include_session_pnl:
            self.session_realised_krw += float(realized_pnl_krw)
            if normalized_market == "overseas":
                self.session_realised_krw_overseas += float(realized_pnl_krw)

    def reset(self, market: str | None = None) -> None:
        if market is not None:
            normalized_market = self._normalize_market(market)
            self._consecutive_losses_by_market[normalized_market] = 0
            self._halted_at_by_market.pop(normalized_market, None)
            self._sync_aggregate_consecutive_losses()
            self._sync_aggregate_halted_at()
            return
        self.consecutive_losses = 0
        self._consecutive_losses_by_market = {}
        self._halted_at = None
        self._halted_at_by_market = {}
        self._daily_halted_at = None
        self._overseas_cb_active = False

    @staticmethod
    def _reject_key(market: str, side: str) -> str:
        return f"{str(market).strip().lower()}:{str(side).strip().lower()}"

    def record_order_rejection(self, *, market: str, side: str, error: str = "") -> bool:
        """Record a rejected order attempt; returns True if this call trips the breaker."""
        risk = getattr(self._config, "risk", None)
        threshold = int(getattr(risk, "order_reject_threshold", 0) or 0) if risk else 0
        if threshold <= 0:
            return False
        key = self._reject_key(market, side)
        if key in self._order_reject_halted_at:
            return False
        window_minutes = int(getattr(risk, "order_reject_window_minutes", 15) or 15)
        now = datetime.now(timezone.utc)
        history = self._order_reject_history.setdefault(key, [])
        history.append(now)
        cutoff = now - timedelta(minutes=window_minutes)
        history[:] = [ts for ts in history if ensure_timezone(ts) >= cutoff]
        if len(history) < threshold:
            return False
        self._order_reject_halted_at[key] = now
        self._emit_event(
            "order_reject_cb_fired",
            {
                "market": market,
                "side": side,
                "count": len(history),
                "window_min": window_minutes,
                "error": str(error)[:200],
            },
        )
        return True

    def is_order_reject_halted(self, *, market: str, side: str) -> bool:
        key = self._reject_key(market, side)
        halted_at = self._order_reject_halted_at.get(key)
        if halted_at is None:
            return False
        risk = getattr(self._config, "risk", None)
        cooldown_minutes = int(
            getattr(risk, "order_reject_cooldown_minutes", 30) or 30
        ) if risk else 30
        elapsed_minutes = (
            datetime.now(timezone.utc) - ensure_timezone(halted_at)
        ).total_seconds() / 60
        if elapsed_minutes < cooldown_minutes:
            return True
        self._order_reject_halted_at.pop(key, None)
        self._order_reject_history.pop(key, None)
        _logger.info(
            "[CB] 주문거부 서킷브레이커 자동 해제 대상=%s (%.0f분 경과)",
            key,
            elapsed_minutes,
        )
        self._emit_event(
            "order_reject_cb_released",
            {"market": market, "side": side, "elapsed_min": round(elapsed_minutes, 1)},
        )
        self._schedule_notification(
            f"✅ 주문거부 서킷브레이커 자동 해제\n"
            f"대상={market}/{side} 쿨다운 {cooldown_minutes}분 완료 → 신규 주문 재개"
        )
        return False

    def order_reject_status(self) -> dict[str, dict[str, object]]:
        return {
            key: {"count": len(history), "halted": key in self._order_reject_halted_at}
            for key, history in self._order_reject_history.items()
            if history
        }

    def reset_order_rejections(self) -> None:
        self._order_reject_history = {}
        self._order_reject_halted_at = {}

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = str(market).strip().lower()
        aliases = {
            "krx": "domestic",
            "korea": "domestic",
            "us": "overseas",
            "usa": "overseas",
        }
        return aliases.get(normalized, normalized)

    def _market_risk_value(
        self,
        market: str | None,
        field_name: str,
        fallback: int,
    ) -> int:
        if market is None:
            return fallback
        policies = getattr(self._config, "market_policies", None)
        definition = getattr(policies, market, None) if policies is not None else None
        configured = getattr(definition, field_name, None)
        return fallback if configured is None else int(configured)

    def _sync_aggregate_consecutive_losses(self) -> None:
        if self._consecutive_losses_by_market:
            self.consecutive_losses = max(self._consecutive_losses_by_market.values())

    def _sync_aggregate_halted_at(self) -> None:
        self._halted_at = (
            min(self._halted_at_by_market.values())
            if self._halted_at_by_market
            else None
        )

    def _risk_day_rollover_hour_kst(self) -> int:
        risk = getattr(self._config, "risk", None)
        configured = int(
            getattr(risk, "account_risk_day_rollover_hour_kst", 7) or 0
        )
        return min(23, max(0, configured))

    def current_risk_day(self, now: datetime | None = None) -> date:
        current = ensure_timezone(now or datetime.now(timezone.utc)).astimezone(KST)
        rollover_hour = self._risk_day_rollover_hour_kst()
        if current.time() < time(hour=rollover_hour):
            return current.date() - timedelta(days=1)
        return current.date()

    def current_risk_day_start(self, now: datetime | None = None) -> datetime:
        risk_day = self.current_risk_day(now)
        rollover_hour = self._risk_day_rollover_hour_kst()
        return datetime.combine(
            risk_day,
            time(hour=rollover_hour),
            tzinfo=KST,
        ).astimezone(timezone.utc)

    def _maybe_reset_daily(self, now: datetime | None = None) -> None:
        risk_day = self.current_risk_day(now)
        if self.daily_loss_date == risk_day:
            return
        self.daily_loss_date = risk_day
        self.session_realised_krw = 0.0
        self.session_realised_krw_overseas = 0.0
        self._daily_halted_at = None
        self._overseas_cb_active = False
        _logger.info(
            "[CB] 통합 리스크 날짜 전환 → daily_loss 초기화 "
            "(risk_day=%s rollover=%02d:00 KST)",
            risk_day,
            self._risk_day_rollover_hour_kst(),
        )

    def _check_consecutive(
        self,
        risk: object,
        *,
        market: str | None,
    ) -> bool:
        fallback_max = int(getattr(risk, "max_consecutive_losses", 0) or 0)
        max_consecutive = self._market_risk_value(
            market,
            "max_consecutive_losses",
            fallback_max,
        )
        consecutive_losses = (
            self.consecutive_losses
            if market is None
            else self._consecutive_losses_by_market.get(market, 0)
        )
        if max_consecutive <= 0:
            return False

        fallback_cooldown = int(
            getattr(risk, "circuit_breaker_cooldown_minutes", 0) or 0
        )
        cooldown_minutes = self._market_risk_value(
            market,
            "circuit_breaker_cooldown_minutes",
            fallback_cooldown,
        )
        now = datetime.now(timezone.utc)
        halted_at = (
            self._halted_at
            if market is None
            else self._halted_at_by_market.get(market)
        )
        if halted_at is not None:
            if cooldown_minutes <= 0:
                return True

            elapsed_minutes = (
                now - ensure_timezone(halted_at)
            ).total_seconds() / 60
            if elapsed_minutes < cooldown_minutes:
                return True

            _logger.info(
                "[CB] 서킷브레이커 자동 해제 market=%s (%.0f분 경과)",
                market or "all",
                elapsed_minutes,
            )
            if market is None:
                self.consecutive_losses = 0
                self._halted_at = None
            else:
                self._consecutive_losses_by_market[market] = 0
                self._halted_at_by_market.pop(market, None)
                self._last_cb_released_at_by_market[market] = now
                self._sync_aggregate_consecutive_losses()
                self._sync_aggregate_halted_at()
            self._last_cb_released_at = now
            max_fires = self._market_risk_value(
                market,
                "post_cb_max_fires_per_session",
                0,
            )
            session_entry_stop_active = market is not None and max_fires == 1
            detail = {
                "elapsed_min": round(elapsed_minutes, 1),
                "trigger": "auto_cooldown",
                "type": "consecutive",
                "post_cb_max_fires_per_session": max_fires or None,
                "session_entry_stop_active": session_entry_stop_active,
            }
            if market is not None:
                detail["market"] = market
            self._emit_event("cb_released", detail)
            market_label = {
                "domestic": "국장",
                "overseas": "미장",
            }.get(str(market or ""), str(market or "전체"))
            if session_entry_stop_active:
                self._schedule_notification(
                    "✅ 서킷브레이커 시간쿨다운 해제\n"
                    f"시장={market_label} 쿨다운={cooldown_minutes}분 완료\n"
                    "세션손실한도=1회 도달\n"
                    "일반 신규매수=다음 시장 세션까지 중단\n"
                    "기존포지션 청산·인버스 감시는 계속"
                )
            else:
                market_line = f"시장={market_label} " if market is not None else ""
                self._schedule_notification(
                    "✅ 서킷브레이커 자동 해제\n"
                    f"{market_line}시간쿨다운 {cooldown_minutes}분 완료 "
                    "→ 시장별 진입조건 재검사"
                )
            return False

        if consecutive_losses < max_consecutive:
            return False
        if cooldown_minutes <= 0:
            return True

        if market is None:
            self._halted_at = now
        else:
            self._halted_at_by_market[market] = now
            self._sync_aggregate_halted_at()
        detail = {
            "consecutive_losses": consecutive_losses,
            "type": "consecutive",
        }
        if market is not None:
            detail["market"] = market
        self._emit_event("cb_fired", detail)
        return True

    def _check_daily(
        self,
        risk: object,
        *,
        now: datetime | None = None,
    ) -> bool:
        if self._daily_halted_at is not None:
            return True
        daily_limit = float(getattr(risk, "daily_loss_limit_pct", 0.0) or 0.0)
        session_realised_krw = float(self.session_realised_krw or 0.0)
        if daily_limit <= 0 or session_realised_krw >= 0:
            return False

        operating_capital = float(
            getattr(self._config.risk, "operating_capital_krw", 0) or 50_000_000
        )
        if (
            operating_capital <= 0
            or abs(session_realised_krw) / operating_capital <= daily_limit
        ):
            return False

        current = ensure_timezone(now or datetime.now(timezone.utc))
        if self._daily_halted_at is None:
            self._daily_halted_at = current
            self._emit_event(
                "cb_fired",
                {
                    "daily_loss_limit_pct": daily_limit,
                    "session_realised_krw": round(session_realised_krw, 2),
                    "type": "daily_limit",
                    "risk_day": self.current_risk_day(current).isoformat(),
                    "rollover_hour_kst": self._risk_day_rollover_hour_kst(),
                },
            )
        return True

    def _emit_event(self, event_type: str, detail: dict) -> None:
        if self._event_hook is None:
            return
        try:
            self._event_hook(event_type=event_type, detail=detail)
        except Exception:  # noqa: BLE001
            _logger.exception("circuit_breaker_event_hook_failed type=%s", event_type)

    def _schedule_notification(self, message: str) -> None:
        if not message or self._notify_hook is None:
            return
        try:
            loop = None
            if inspect.iscoroutinefunction(self._notify_hook):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
            result = self._notify_hook(message)
            if inspect.isawaitable(result):
                if loop is None:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        return
                loop.create_task(result)
        except Exception:  # noqa: BLE001
            _logger.exception("circuit_breaker_notify_hook_failed")
