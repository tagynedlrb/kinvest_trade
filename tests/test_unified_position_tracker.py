import asyncio
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

from kinvest_trade.liquidity_lab import (
    LiquidityLabService,
    OverseasHeldPosition,
    OverseasScanResult,
    UnifiedPositionTracker,
    VirtualTradeManager,
)
from kinvest_trade.repository import SqliteRepository


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class DummyClient:
    def __init__(self) -> None:
        self.order_calls: list[dict] = []
        self.raise_error = False

    async def place_overseas_order_for_current_session(
        self,
        *,
        side: str,
        symbol: str,
        exchange_code: str,
        qty: int,
        price: str,
        order_division: str,
    ) -> dict:
        self.order_calls.append(
            {
                "side": side,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "qty": qty,
                "price": price,
                "order_division": order_division,
            }
        )
        if self.raise_error:
            raise RuntimeError("unexpected order failure")
        return self.order_calls[-1]


def _build_tracker() -> tuple[SqliteRepository, VirtualTradeManager, UnifiedPositionTracker]:
    repository = SqliteRepository(Path(mkdtemp()) / "unified_tracker.db")
    virtual_trades = VirtualTradeManager(repository)
    tracker = UnifiedPositionTracker(repository, virtual_trades)
    return repository, virtual_trades, tracker


def _build_service() -> LiquidityLabService:
    repository, virtual_trades, tracker = _build_tracker()
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        liquidity_lab=SimpleNamespace(
            overseas_stop_loss_pct=0.008,
            overseas_take_profit_pct=0.012,
        )
    )
    service.repository = repository
    service.virtual_trades = virtual_trades
    service.position_tracker = tracker
    service.client = DummyClient()
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    return service


def test_apply_sell_deducts_virtual_buy_first() -> None:
    _, virtual_trades, tracker = _build_tracker()
    virtual_trades.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=4,
        fill_price=100.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    result = tracker.apply_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        sell_qty=2,
        price=110.0,
        currency="USD",
        session="daytime",
        reason="take_profit",
        real_qty=0,
        can_execute_real=False,
        created_at="2026-06-30 20:00:00 KST",
    )

    position = virtual_trades.get_position("overseas", "SOXL")
    assert position is not None
    assert position.qty == 2
    assert tracker.get_pending_settlement("overseas", "SOXL") is None
    assert result["qty_from_virtual_buy"] == 2


def test_apply_sell_overflow_to_virtual_sell_pending() -> None:
    repository, virtual_trades, tracker = _build_tracker()
    virtual_trades.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=2,
        fill_price=100.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    result = tracker.apply_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        sell_qty=5,
        price=110.0,
        currency="USD",
        session="daytime",
        reason="take_profit",
        real_qty=10,
        can_execute_real=False,
        created_at="2026-06-30 20:00:00 KST",
    )

    assert virtual_trades.get_position("overseas", "SOXL") is None
    pending = repository.get_virtual_sell_pending("overseas", "SOXL")
    assert pending is not None
    assert int(pending["qty"]) == 3
    assert result["qty_pending_real"] == 3


def test_apply_sell_real_execution_does_not_touch_pending() -> None:
    repository, _, tracker = _build_tracker()

    result = tracker.apply_sell(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        sell_qty=3,
        price=110.0,
        currency="USD",
        session="regular",
        reason="take_profit",
        real_qty=3,
        can_execute_real=True,
        created_at="2026-06-30 20:00:00 KST",
    )

    assert result["qty_from_real"] == 3
    assert repository.get_virtual_sell_pending("overseas", "NVDA") is None


def test_settle_clears_pending_without_log() -> None:
    repository, _, tracker = _build_tracker()
    repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=5,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-06-30 20:00:00 KST",
    )

    tracker.settle(
        market="overseas",
        symbol="NVDA",
        real_qty_after_settlement=0,
    )

    assert tracker.get_pending_settlement("overseas", "NVDA") is None


def test_exit_target_does_not_repick_already_pending_quantity() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=5,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-06-30 20:00:00 KST",
    )
    quote = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=120.0,
        bid=119.9,
        ask=120.1,
        spread_pct=0.001,
        change_rate_pct=2.0,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=1350.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="NVDA",
        exchange_code="NASD",
        quantity=7,
        orderable_qty=7,
        avg_price=100.0,
        current_price=120.0,
        pnl_pct=0.2,
    )

    result = asyncio.run(service._select_overseas_exit_target([quote], [held]))

    assert result is not None
    _, selected_held, reason, _ = result
    assert reason == "take_profit"
    assert selected_held.orderable_qty == 2


def test_reconcile_settles_min_of_pending_and_orderable() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=5,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-06-30 20:00:00 KST",
    )
    positions = [
        OverseasHeldPosition(
            symbol="NVDA",
            exchange_code="NASD",
            quantity=7,
            orderable_qty=3,
            avg_price=100.0,
            current_price=111.0,
            pnl_pct=0.11,
        )
    ]

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))

    assert service.client.order_calls[0]["qty"] == 3
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 2


def test_reconcile_clears_orphan_virtual_sell_pending() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="MSEX",
        exchange_code="NASD",
        qty=522,
        avg_sell_price=54.53,
        currency="USD",
        updated_at="2026-07-10 05:40:25 KST",
    )
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="MSEX",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="stale_signal_cache",
        strategy_flag="VWAP",
        entry_by="VWAP",
        holding_qty=522,
        last_price=54.53,
        pnl_pct=0.007,
        has_position=1,
        updated_at="2026-07-10T09:00:37+00:00",
    )

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=[]))

    assert service.repository.get_virtual_sell_pending("overseas", "MSEX") is None
    state = service.repository.get_lab_symbol_state("overseas", "MSEX")
    assert state is not None
    assert state["has_position"] == 1
    assert state["holding_qty"] == 522
    assert state["note"] == "stale_signal_cache"
    events = service.repository.list_event_log(event_type="virtual_pending_cleanup", limit=5)
    assert events[0]["symbol"] == "MSEX"


def test_reconcile_uses_virtual_sell_price_for_pnl_log() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=2,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-06-30 20:00:00 KST",
    )
    positions = [
        OverseasHeldPosition(
            symbol="NVDA",
            exchange_code="NASD",
            quantity=2,
            orderable_qty=2,
            avg_price=100.0,
            current_price=111.0,
            pnl_pct=0.11,
        )
    ]

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))

    message = service.notifier.messages[-1]
    assert "[KIS][VIRTUAL_SETTLED]" in message
    assert "가상매도가=+$115.00" in message
    assert "손익=+$30.00" in message
