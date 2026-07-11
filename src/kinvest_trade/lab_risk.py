from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import date, datetime, timezone
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
        self.session_realised_krw: float = 0.0
        self.session_realised_krw_overseas: float = 0.0
        self.daily_loss_date: date | None = None
        self._halted_at: datetime | None = None
        self._daily_halted_at: datetime | None = None
        self._last_cb_released_at: datetime | None = None
        self._overseas_cb_active: bool = False

    def load_state(
        self,
        *,
        consecutive_losses: int | None = None,
        session_realised_krw: float | None = None,
        session_realised_krw_overseas: float | None = None,
        daily_loss_date: date | None = None,
        halted_at: datetime | None | object = _UNSET,
        daily_halted_at: datetime | None | object = _UNSET,
        last_cb_released_at: datetime | None | object = _UNSET,
        overseas_cb_active: bool | None = None,
    ) -> None:
        if consecutive_losses is not None:
            self.consecutive_losses = int(consecutive_losses)
        if session_realised_krw is not None:
            self.session_realised_krw = float(session_realised_krw)
        if session_realised_krw_overseas is not None:
            self.session_realised_krw_overseas = float(session_realised_krw_overseas)
        self.daily_loss_date = daily_loss_date
        if halted_at is not _UNSET:
            self._halted_at = halted_at
        if daily_halted_at is not _UNSET:
            self._daily_halted_at = daily_halted_at
        if last_cb_released_at is not _UNSET:
            self._last_cb_released_at = last_cb_released_at
        if overseas_cb_active is not None:
            self._overseas_cb_active = bool(overseas_cb_active)

    def snapshot(self) -> dict[str, object]:
        return {
            "consecutive_losses": self.consecutive_losses,
            "session_realised_krw": self.session_realised_krw,
            "session_realised_krw_overseas": self.session_realised_krw_overseas,
            "daily_loss_date": self.daily_loss_date,
            "halted_at": self._halted_at,
            "daily_halted_at": self._daily_halted_at,
            "last_cb_released_at": self._last_cb_released_at,
            "overseas_cb_active": self._overseas_cb_active,
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
        return self._halted_at is not None or self._daily_halted_at is not None

    def is_halted(self) -> bool:
        self._maybe_reset_daily()
        risk = getattr(self._config, "risk", None)
        if risk is None:
            return False
        if self._check_consecutive(risk):
            return True
        return self._check_daily(risk)

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
        gross_pnl_krw: float,
        pnl_pct: float | None = None,
    ) -> None:
        is_loss = pnl_pct < 0 if pnl_pct is not None else gross_pnl_krw < 0
        if is_loss:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.session_realised_krw += float(gross_pnl_krw)
        if str(market).strip().lower() == "overseas":
            self.session_realised_krw_overseas += float(gross_pnl_krw)

    def reset(self) -> None:
        self.consecutive_losses = 0
        self._halted_at = None
        self._daily_halted_at = None
        self._overseas_cb_active = False

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).astimezone(KST).date()
        if self.daily_loss_date == today:
            return
        self.daily_loss_date = today
        self.session_realised_krw = 0.0
        self.session_realised_krw_overseas = 0.0
        self._daily_halted_at = None
        self._overseas_cb_active = False
        _logger.info("[CB] KST 날짜 전환 → daily_loss 초기화 (date=%s)", today)

    def _check_consecutive(self, risk: object) -> bool:
        max_consecutive = int(getattr(risk, "max_consecutive_losses", 0) or 0)
        if max_consecutive <= 0 or self.consecutive_losses < max_consecutive:
            return False

        cooldown_minutes = int(getattr(risk, "circuit_breaker_cooldown_minutes", 0) or 0)
        if cooldown_minutes <= 0:
            return True

        now = datetime.now(timezone.utc)
        if self._halted_at is None:
            self._halted_at = now
            self._emit_event(
                "cb_fired",
                {
                    "consecutive_losses": self.consecutive_losses,
                    "type": "consecutive",
                },
            )
            return True

        elapsed_minutes = (now - ensure_timezone(self._halted_at)).total_seconds() / 60
        if elapsed_minutes < cooldown_minutes:
            return True

        _logger.info("[CB] 서킷브레이커 자동 해제 (%.0f분 경과)", elapsed_minutes)
        self.consecutive_losses = 0
        self._halted_at = None
        self._last_cb_released_at = now
        self._emit_event(
            "cb_released",
            {
                "elapsed_min": round(elapsed_minutes, 1),
                "trigger": "auto_cooldown",
                "type": "consecutive",
            },
        )
        self._schedule_notification(
            f"✅ 서킷브레이커 자동 해제\n"
            f"쿨다운 {cooldown_minutes}분 완료 → 매수 재개"
        )
        return False

    def _check_daily(self, risk: object) -> bool:
        daily_limit = float(getattr(risk, "daily_loss_limit_pct", 0.0) or 0.0)
        session_realised_krw = float(self.session_realised_krw or 0.0)
        if daily_limit <= 0 or session_realised_krw >= 0:
            return False

        operating_capital = float(
            getattr(self._config.risk, "operating_capital_krw", 0) or 5_000_000
        )
        if operating_capital <= 0 or abs(session_realised_krw) / operating_capital <= daily_limit:
            return False

        cooldown_minutes = int(getattr(risk, "circuit_breaker_cooldown_minutes", 0) or 0)
        if cooldown_minutes <= 0:
            return True

        now = datetime.now(timezone.utc)
        if self._daily_halted_at is None:
            self._daily_halted_at = now
            self._emit_event(
                "cb_fired",
                {
                    "daily_loss_limit_pct": daily_limit,
                    "session_realised_krw": round(session_realised_krw, 2),
                    "type": "daily_limit",
                },
            )
            return True

        elapsed_minutes = (now - ensure_timezone(self._daily_halted_at)).total_seconds() / 60
        if elapsed_minutes < cooldown_minutes:
            return True

        _logger.info("[CB] daily_limit 자동 해제 (%.0f분 경과)", elapsed_minutes)
        self.session_realised_krw = 0.0
        self.session_realised_krw_overseas = 0.0
        self._daily_halted_at = None
        self._last_cb_released_at = now
        self._emit_event(
            "cb_released",
            {
                "elapsed_min": round(elapsed_minutes, 1),
                "trigger": "auto_cooldown",
                "type": "daily_limit",
            },
        )
        self._schedule_notification(
            f"✅ 일일손실한도 CB 자동 해제\n"
            f"쿨다운 {cooldown_minutes}분 완료 → 매수 재개"
        )
        return False

    def _emit_event(self, event_type: str, detail: dict) -> None:
        if self._event_hook is None:
            return
        try:
            self._event_hook(event_type, detail)
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
