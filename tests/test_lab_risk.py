import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from kinvest_trade.lab_risk import CircuitBreakerManager
from kinvest_trade.market_sessions import KST


def _build_config():
    return SimpleNamespace(
        risk=SimpleNamespace(
            daily_loss_limit_pct=0.01,
            max_consecutive_losses=3,
            circuit_breaker_cooldown_minutes=30,
            operating_capital_krw=50_000_000,
        )
    )


def test_circuit_breaker_blocks_after_consecutive_losses() -> None:
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(_build_config(), event_hook=lambda event_type, detail: events.append((event_type, detail)))
    manager.load_state(consecutive_losses=3)

    assert manager.is_halted() is True
    assert events == [
        (
            "cb_fired",
            {
                "consecutive_losses": 3,
                "type": "consecutive",
            },
        )
    ]
    assert manager.halted_at is not None


def test_circuit_breaker_auto_releases_after_cooldown() -> None:
    async def run_case() -> None:
        messages: list[str] = []
        manager = CircuitBreakerManager(_build_config(), notify_hook=lambda message: messages.append(message))
        manager.load_state(
            consecutive_losses=3,
            halted_at=datetime.now(timezone.utc) - timedelta(minutes=31),
        )

        assert manager.is_halted() is False
        assert manager.consecutive_losses == 0
        assert manager.halted_at is None
        assert manager.last_cb_released_at is not None
        assert messages == [
            "✅ 서킷브레이커 자동 해제\n쿨다운 30분 완료 → 매수 재개"
        ]

    asyncio.run(run_case())


def test_circuit_breaker_daily_limit_keeps_block_after_consecutive_release() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        consecutive_losses=3,
        session_realised_krw=-600_000.0,
        daily_loss_date=datetime.now(timezone.utc).astimezone(KST).date(),
        halted_at=datetime.now(timezone.utc) - timedelta(minutes=31),
    )

    assert manager.is_halted() is True
    assert manager.consecutive_losses == 0
    assert manager.daily_halted_at is not None


def test_circuit_breaker_blocks_on_daily_loss_limit() -> None:
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(_build_config(), event_hook=lambda event_type, detail: events.append((event_type, detail)))
    manager.load_state(
        session_realised_krw=-600_000.0,
        daily_loss_date=datetime.now(timezone.utc).astimezone(KST).date(),
    )

    assert manager.is_halted() is True
    assert manager.daily_halted_at is not None
    assert events == [
        (
            "cb_fired",
            {
                "daily_loss_limit_pct": 0.01,
                "session_realised_krw": -600000.0,
                "type": "daily_limit",
            },
        )
    ]


def test_circuit_breaker_daily_limit_auto_releases_after_cooldown() -> None:
    async def run_case() -> None:
        messages: list[str] = []
        manager = CircuitBreakerManager(_build_config(), notify_hook=lambda message: messages.append(message))
        manager.load_state(
            session_realised_krw=-600_000.0,
            daily_loss_date=datetime.now(timezone.utc).astimezone(KST).date(),
            daily_halted_at=datetime.now(timezone.utc) - timedelta(minutes=31),
        )

        assert manager.is_halted() is False
        assert manager.session_realised_krw == 0.0
        assert manager.daily_halted_at is None
        assert messages == [
            "✅ 일일손실한도 CB 자동 해제\n쿨다운 30분 완료 → 매수 재개"
        ]

    asyncio.run(run_case())


def test_circuit_breaker_resets_daily_state_on_new_kst_day() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        session_realised_krw=-100_000.0,
        session_realised_krw_overseas=-50_000.0,
        daily_loss_date=date(2026, 7, 9),
        daily_halted_at=datetime.now(timezone.utc),
        overseas_cb_active=True,
    )

    assert manager.is_halted() is False
    assert manager.daily_loss_date != date(2026, 7, 9)
    assert manager.session_realised_krw == 0.0
    assert manager.session_realised_krw_overseas == 0.0
    assert manager.daily_halted_at is None
    assert manager.overseas_cb_active is False
