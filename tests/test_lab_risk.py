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
            order_reject_threshold=3,
            order_reject_window_minutes=15,
            order_reject_cooldown_minutes=30,
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


def test_daily_circuit_breaker_fallback_matches_configured_default_capital() -> None:
    # operating_capital_krw=0 simulates a misconfigured/missing value falling
    # through to the `or <fallback>` branch. That fallback must match the
    # dataclass default (50,000,000 KRW, see config.py RiskConfig), not some
    # unrelated stray literal -- otherwise the daily-loss threshold silently
    # becomes far stricter than configured.
    config = _build_config()
    config.risk.operating_capital_krw = 0
    config.risk.daily_loss_limit_pct = 0.01
    manager = CircuitBreakerManager(config)
    manager.load_state(
        session_realised_krw=-400_000.0,
        daily_loss_date=datetime.now(timezone.utc).astimezone(KST).date(),
    )

    assert manager.is_halted() is False


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


def test_order_reject_breaker_trips_after_threshold_within_window() -> None:
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(
        _build_config(),
        event_hook=lambda event_type, detail: events.append((event_type, detail)),
    )

    assert manager.record_order_rejection(market="domestic", side="buy", error="e1") is False
    assert manager.record_order_rejection(market="domestic", side="buy", error="e2") is False
    tripped = manager.record_order_rejection(market="domestic", side="buy", error="e3")

    assert tripped is True
    assert manager.is_order_reject_halted(market="domestic", side="buy") is True
    assert events[-1][0] == "order_reject_cb_fired"
    assert events[-1][1]["market"] == "domestic"
    assert events[-1][1]["side"] == "buy"
    assert events[-1][1]["count"] == 3


def test_order_reject_breaker_is_per_market_and_side() -> None:
    manager = CircuitBreakerManager(_build_config())
    for _ in range(3):
        manager.record_order_rejection(market="domestic", side="buy", error="e")

    assert manager.is_order_reject_halted(market="domestic", side="buy") is True
    assert manager.is_order_reject_halted(market="domestic", side="sell") is False
    assert manager.is_order_reject_halted(market="overseas", side="buy") is False


def test_order_reject_breaker_ignores_old_rejections_outside_window() -> None:
    manager = CircuitBreakerManager(_build_config())
    now = datetime.now(timezone.utc)
    old_key = manager._reject_key("domestic", "buy")
    manager._order_reject_history[old_key] = [
        now - timedelta(minutes=20),
        now - timedelta(minutes=18),
    ]

    tripped = manager.record_order_rejection(market="domestic", side="buy", error="e")

    assert tripped is False
    assert manager.is_order_reject_halted(market="domestic", side="buy") is False


def test_order_reject_breaker_auto_releases_after_cooldown() -> None:
    async def run_case() -> None:
        messages: list[str] = []
        manager = CircuitBreakerManager(
            _build_config(), notify_hook=lambda message: messages.append(message)
        )
        for _ in range(3):
            manager.record_order_rejection(market="overseas", side="buy", error="e")
        key = manager._reject_key("overseas", "buy")
        manager._order_reject_halted_at[key] = datetime.now(timezone.utc) - timedelta(minutes=31)

        assert manager.is_order_reject_halted(market="overseas", side="buy") is False
        assert manager.order_reject_status() == {}
        assert messages == [
            "✅ 주문거부 서킷브레이커 자동 해제\n"
            "대상=overseas/buy 쿨다운 30분 완료 → 신규 주문 재개"
        ]

    asyncio.run(run_case())


def test_order_reject_breaker_disabled_when_threshold_zero() -> None:
    config = _build_config()
    config.risk.order_reject_threshold = 0
    manager = CircuitBreakerManager(config)

    for _ in range(10):
        tripped = manager.record_order_rejection(market="domestic", side="buy", error="e")
        assert tripped is False
    assert manager.is_order_reject_halted(market="domestic", side="buy") is False


def test_reset_order_rejections_clears_all_state() -> None:
    manager = CircuitBreakerManager(_build_config())
    for _ in range(3):
        manager.record_order_rejection(market="domestic", side="buy", error="e")
    assert manager.is_order_reject_halted(market="domestic", side="buy") is True

    manager.reset_order_rejections()

    assert manager.is_order_reject_halted(market="domestic", side="buy") is False
    assert manager.order_reject_status() == {}
