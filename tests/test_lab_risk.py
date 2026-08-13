import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import kinvest_trade.lab_risk as lab_risk_module
from kinvest_trade.lab_risk import CircuitBreakerManager


def _build_config():
    return SimpleNamespace(
        risk=SimpleNamespace(
            daily_loss_limit_pct=0.01,
            max_consecutive_losses=3,
            circuit_breaker_cooldown_minutes=30,
            operating_capital_krw=50_000_000,
            account_risk_day_rollover_hour_kst=7,
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


def test_consecutive_loss_breakers_are_independent_by_market() -> None:
    config = _build_config()
    config.market_policies = SimpleNamespace(
        domestic=SimpleNamespace(
            max_consecutive_losses=2,
            circuit_breaker_cooldown_minutes=30,
        ),
        overseas=SimpleNamespace(
            max_consecutive_losses=4,
            circuit_breaker_cooldown_minutes=30,
        ),
    )
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(
        config,
        event_hook=lambda event_type, detail: events.append((event_type, detail)),
    )
    manager.load_state(
        daily_loss_date=manager.current_risk_day(),
    )

    for _ in range(2):
        manager.on_realised(market="domestic", realized_pnl_krw=-1_000, pnl_pct=-0.01)
        manager.on_realised(market="overseas", realized_pnl_krw=-1_000, pnl_pct=-0.01)

    assert manager.is_halted("domestic") is True
    assert manager.is_halted("overseas") is False
    assert manager.snapshot()["consecutive_losses_by_market"] == {
        "domestic": 2,
        "overseas": 2,
    }
    assert events[-1][1]["market"] == "domestic"

    for _ in range(2):
        manager.on_realised(market="overseas", realized_pnl_krw=-1_000, pnl_pct=-0.01)

    assert manager.is_halted("overseas") is True
    assert events[-1][1]["market"] == "overseas"


def test_daily_loss_limit_remains_shared_across_markets() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        session_realised_krw=-600_000.0,
        daily_loss_date=manager.current_risk_day(),
    )

    assert manager.is_halted("domestic") is True
    assert manager.is_halted("overseas") is True


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
        daily_loss_date=manager.current_risk_day(),
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
            "✅ 서킷브레이커 자동 해제\n"
            "시간쿨다운 30분 완료 → 시장별 진입조건 재검사"
        ]

    asyncio.run(run_case())


def test_triggered_consecutive_breaker_stays_latched_after_profit() -> None:
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(
        _build_config(),
        event_hook=lambda event_type, detail: events.append(
            (event_type, detail)
        ),
    )

    for _ in range(3):
        manager.on_realised(
            market="overseas",
            realized_pnl_krw=-1_000,
            pnl_pct=-0.01,
        )
    assert manager.is_halted("overseas") is True
    halted_at = manager.snapshot()["halted_at_by_market"]["overseas"]

    manager.on_realised(
        market="overseas",
        realized_pnl_krw=10_000,
        pnl_pct=0.01,
    )

    assert manager.snapshot()["consecutive_losses_by_market"]["overseas"] == 0
    assert manager.is_halted("overseas") is True
    assert manager.snapshot()["halted_at_by_market"]["overseas"] == halted_at
    assert [event_type for event_type, _ in events] == ["cb_fired"]


def test_latched_consecutive_breaker_releases_with_reset_streak() -> None:
    events: list[tuple[str, dict]] = []
    manager = CircuitBreakerManager(
        _build_config(),
        event_hook=lambda event_type, detail: events.append(
            (event_type, detail)
        ),
    )
    manager.load_state(
        consecutive_losses_by_market={"overseas": 0},
        halted_at_by_market={
            "overseas": datetime.now(timezone.utc) - timedelta(minutes=31)
        },
    )

    assert manager.is_halted("overseas") is False
    assert manager.snapshot()["halted_at_by_market"] == {}
    assert manager.last_cb_released_at is not None
    assert events[0][0] == "cb_released"
    assert events[0][1]["market"] == "overseas"


def test_market_cb_release_notice_explains_single_fire_session_stop() -> None:
    messages: list[str] = []
    events: list[tuple[str, dict]] = []
    config = _build_config()
    config.market_policies = SimpleNamespace(
        domestic=SimpleNamespace(
            max_consecutive_losses=3,
            circuit_breaker_cooldown_minutes=30,
            post_cb_max_fires_per_session=1,
        )
    )
    manager = CircuitBreakerManager(
        config,
        event_hook=lambda event_type, detail: events.append(
            (event_type, detail)
        ),
        notify_hook=lambda message: messages.append(message),
    )
    manager.load_state(
        consecutive_losses_by_market={"domestic": 0},
        halted_at_by_market={
            "domestic": datetime.now(timezone.utc) - timedelta(minutes=31)
        },
    )

    assert manager.is_halted("domestic") is False
    assert messages == [
        "✅ 서킷브레이커 시간쿨다운 해제\n"
        "시장=국장 쿨다운=30분 완료\n"
        "세션손실한도=1회 도달\n"
        "일반 신규매수=다음 시장 세션까지 중단\n"
        "기존포지션 청산·인버스 감시는 계속"
    ]
    assert events[0][0] == "cb_released"
    assert events[0][1]["post_cb_max_fires_per_session"] == 1
    assert events[0][1]["session_entry_stop_active"] is True


def test_circuit_breaker_daily_limit_keeps_block_after_consecutive_release() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        consecutive_losses=3,
        session_realised_krw=-600_000.0,
        daily_loss_date=manager.current_risk_day(),
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
        daily_loss_date=manager.current_risk_day(),
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
                "risk_day": manager.current_risk_day().isoformat(),
                "rollover_hour_kst": 7,
            },
        )
    ]


def test_circuit_breaker_daily_limit_remains_until_risk_day_rollover() -> None:
    async def run_case() -> None:
        messages: list[str] = []
        manager = CircuitBreakerManager(_build_config(), notify_hook=lambda message: messages.append(message))
        halted_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        manager.load_state(
            session_realised_krw=-600_000.0,
            daily_loss_date=manager.current_risk_day(),
            daily_halted_at=halted_at,
        )

        assert manager.is_halted() is True
        assert manager.session_realised_krw == -600_000.0
        assert manager.daily_halted_at == halted_at
        assert messages == []

    asyncio.run(run_case())


def test_circuit_breaker_daily_limit_remains_after_pnl_recovers() -> None:
    manager = CircuitBreakerManager(_build_config())
    halted_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    manager.load_state(
        session_realised_krw=100_000.0,
        daily_loss_date=manager.current_risk_day(),
        daily_halted_at=halted_at,
    )

    assert manager.is_daily_halted() is True
    assert manager.daily_halted_at == halted_at


def test_risk_day_rolls_at_0700_kst_instead_of_midnight() -> None:
    manager = CircuitBreakerManager(_build_config())

    assert manager.current_risk_day(
        datetime(2026, 7, 28, 14, 59, tzinfo=timezone.utc)
    ) == date(2026, 7, 28)
    assert manager.current_risk_day(
        datetime(2026, 7, 28, 15, 1, tzinfo=timezone.utc)
    ) == date(2026, 7, 28)
    assert manager.current_risk_day(
        datetime(2026, 7, 28, 21, 59, tzinfo=timezone.utc)
    ) == date(2026, 7, 28)
    assert manager.current_risk_day(
        datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 29)
    assert manager.current_risk_day_start(
        datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)


def test_circuit_breaker_preserves_daily_state_at_kst_midnight() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        session_realised_krw=-100_000.0,
        session_realised_krw_overseas=-50_000.0,
        daily_loss_date=date(2026, 7, 28),
        daily_halted_at=None,
    )
    original_datetime = lab_risk_module.datetime

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 28, 15, 5, tzinfo=timezone.utc)
            return base if tz is None else base.astimezone(tz)

    lab_risk_module.datetime = _FakeDateTime
    try:
        assert manager.is_halted() is False
    finally:
        lab_risk_module.datetime = original_datetime

    assert manager.daily_loss_date == date(2026, 7, 28)
    assert manager.session_realised_krw == -100_000.0
    assert manager.session_realised_krw_overseas == -50_000.0


def test_circuit_breaker_resets_daily_state_on_new_risk_day() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        session_realised_krw=-100_000.0,
        session_realised_krw_overseas=-50_000.0,
        daily_loss_date=date(2026, 7, 9),
        daily_halted_at=datetime.now(timezone.utc),
        overseas_cb_active=True,
    )
    original_datetime = lab_risk_module.datetime

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 10, 22, 5, tzinfo=timezone.utc)
            return base if tz is None else base.astimezone(tz)

    lab_risk_module.datetime = _FakeDateTime
    try:
        assert manager.is_halted() is False
    finally:
        lab_risk_module.datetime = original_datetime

    assert manager.daily_loss_date == date(2026, 7, 11)
    assert manager.session_realised_krw == 0.0
    assert manager.session_realised_krw_overseas == 0.0
    assert manager.daily_halted_at is None
    assert manager.overseas_cb_active is False


def test_first_realised_trade_after_rollover_is_not_discarded() -> None:
    manager = CircuitBreakerManager(_build_config())
    manager.load_state(
        session_realised_krw=-100_000.0,
        session_realised_krw_overseas=-100_000.0,
        daily_loss_date=date(2026, 7, 10),
        daily_halted_at=datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc),
    )
    original_datetime = lab_risk_module.datetime

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 10, 22, 0, 1, tzinfo=timezone.utc)
            return base if tz is None else base.astimezone(tz)

    lab_risk_module.datetime = _FakeDateTime
    try:
        manager.on_realised(
            market="overseas",
            realized_pnl_krw=-12_345.0,
            pnl_pct=-0.01,
        )
    finally:
        lab_risk_module.datetime = original_datetime

    assert manager.daily_loss_date == date(2026, 7, 11)
    assert manager.session_realised_krw == -12_345.0
    assert manager.session_realised_krw_overseas == -12_345.0
    assert manager.daily_halted_at is None
    assert manager.snapshot()["consecutive_losses_by_market"] == {
        "overseas": 1,
    }


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
