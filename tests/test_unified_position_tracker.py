import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

from kinvest_trade.client import KisApiError
from kinvest_trade.lab_positions import UnifiedPositionTracker, VirtualTradeManager
from kinvest_trade.liquidity_lab import (
    LiquidityLabService,
    OverseasHeldPosition,
    OverseasScanResult,
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
        self.cancel_calls: list[dict] = []
        self.pending_orders: list[dict] = []
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
        return {
            "request": self.order_calls[-1],
            "output": {"ODNO": f"{len(self.order_calls):010d}"},
        }

    async def get_overseas_order_history(self, **kwargs) -> dict:
        del kwargs
        return {"orders": list(self.pending_orders)}

    async def revise_or_cancel_overseas_order(self, **kwargs) -> dict:
        self.cancel_calls.append(kwargs)
        return {
            "output": {
                "ODNO": str(kwargs.get("original_order_no") or ""),
            }
        }


def _build_tracker() -> tuple[SqliteRepository, VirtualTradeManager, UnifiedPositionTracker]:
    repository = SqliteRepository(Path(mkdtemp()) / "unified_tracker.db")
    virtual_trades = VirtualTradeManager(repository)
    tracker = UnifiedPositionTracker(repository, virtual_trades)
    return repository, virtual_trades, tracker


def _build_service() -> LiquidityLabService:
    repository, virtual_trades, tracker = _build_tracker()
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(env="vps"),
        risk=SimpleNamespace(
            max_consecutive_losses=3,
            circuit_breaker_cooldown_minutes=30,
            daily_loss_limit_pct=0.99,
            operating_capital_krw=50_000_000,
            risk_day_rollover_hour_kst=7,
        ),
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
        strategy_flag="VWAP+VOL+RSI",
        entry_by="VWAP",
        entry_reason="strategy_guard_probe:VWAP+VOL+RSI|pullback_buy",
        entry_time="2026-06-30T10:00:00+00:00",
    )

    assert virtual_trades.get_position("overseas", "SOXL") is None
    pending = repository.get_virtual_sell_pending("overseas", "SOXL")
    assert pending is not None
    assert int(pending["qty"]) == 3
    assert pending["strategy_flag"] == "VWAP+VOL+RSI"
    assert pending["entry_by"] == "VWAP"
    assert pending["entry_reason"].startswith("strategy_guard_probe:")
    assert pending["entry_time"] == "2026-06-30T10:00:00+00:00"
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


def test_exit_target_fully_pending_is_not_no_orderable_stall_outside_profile() -> None:
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
    service._no_orderable_retry = {
        "overseas:NVDA": datetime.now(timezone.utc) + timedelta(minutes=20),
    }
    service._no_orderable_counts = {"overseas:NVDA": 42}
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
        quantity=5,
        orderable_qty=0,
        avg_price=100.0,
        current_price=120.0,
        pnl_pct=0.2,
    )

    result = asyncio.run(
        service._select_overseas_exit_targets(
            [quote],
            [held],
            profile_orderable=False,
        )
    )

    assert result == []
    assert service._no_orderable_retry == {}
    assert service._no_orderable_counts == {}
    events = service.repository.list_event_log(event_type="trade_skip", limit=10)
    assert events == []


def test_reconcile_pending_zero_orderable_tracks_real_stall() -> None:
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
            quantity=5,
            orderable_qty=0,
            avg_price=100.0,
            current_price=111.0,
            pnl_pct=0.11,
        )
    ]

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))

    assert service.client.order_calls == []
    assert service._no_orderable_counts == {"overseas:NVDA": 1}
    assert "overseas:NVDA" in service._no_orderable_retry
    events = service.repository.list_event_log(event_type="trade_skip", limit=5)
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["reason"] == "no_orderable_qty"
    assert detail["cause"] == "pending_virtual_sell_reconcile_zero_qty"
    assert "T+2" not in detail["note"]

    quote = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=111.0,
        bid=110.9,
        ask=111.1,
        spread_pct=0.001,
        change_rate_pct=1.0,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=1350.0,
        activity_score=10.0,
    )
    asyncio.run(
        service._select_overseas_exit_targets(
            [quote],
            positions,
            profile_orderable=True,
        )
    )
    assert service._no_orderable_counts == {"overseas:NVDA": 1}


def test_reconcile_pending_rejection_keeps_pending_and_records_cause() -> None:
    class RejectingClient(DummyClient):
        async def place_overseas_order_for_current_session(self, **kwargs) -> dict:
            await super().place_overseas_order_for_current_session(**kwargs)
            raise KisApiError("mock settlement rejected")

    service = _build_service()
    service.client = RejectingClient()
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

    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 2
    assert service._no_orderable_counts == {"overseas:NVDA": 1}
    events = service.repository.list_event_log(event_type="trade_skip", limit=5)
    detail = json.loads(events[0]["detail"])
    assert detail["cause"] == "pending_virtual_sell_reconcile_rejected"
    assert detail["error"] == "mock settlement rejected"


def test_reconcile_missing_order_number_preserves_pending_without_resubmit() -> None:
    class MissingOrderNumberClient(DummyClient):
        async def place_overseas_order_for_current_session(self, **kwargs) -> dict:
            await super().place_overseas_order_for_current_session(**kwargs)
            return {"rt_cd": "0", "msg1": "accepted without order number"}

    service = _build_service()
    service.client = MissingOrderNumberClient()
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
    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))

    assert len(service.client.order_calls) == 1
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 2
    assert service.repository.list_unfinalized_broker_executions() == []
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_tracking_failed",
        limit=1,
    )[0]
    assert json.loads(event["detail"])["pending_preserved"] is True
    assert "[KIS][VIRTUAL_SETTLEMENT_TRACKING_FAILED]" in service.notifier.messages[-1]


def test_reconcile_submits_min_of_pending_and_preserves_pending_until_fill() -> None:
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
    assert pending["qty"] == 5
    executions = service.repository.list_unfinalized_broker_executions()
    assert len(executions) == 1
    assert executions[0]["requested_qty"] == 3
    assert (
        executions[0]["context_json"]["execution_role"]
        == "virtual_sell_settlement"
    )
    assert "[KIS][VIRTUAL_SETTLEMENT_SUBMITTED]" in service.notifier.messages[-1]

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))

    assert len(service.client.order_calls) == 1
    assert service.repository.get_virtual_sell_pending("overseas", "NVDA")["qty"] == 5

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=[]))

    assert service.repository.get_virtual_sell_pending("overseas", "NVDA")["qty"] == 5


def test_reconcile_defers_repeated_no_fill_settlement_when_volume_is_zero() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NPAC",
        exchange_code="NASD",
        qty=396,
        avg_sell_price=10.49,
        currency="USD",
        updated_at="2026-08-17T13:00:00+00:00",
    )
    for created_at, order_no in (
        ("2026-08-18T13:31:00+00:00", "1001"),
        ("2026-08-19T13:31:00+00:00", "1002"),
    ):
        service.repository.save_broker_order_event(
            created_at=created_at,
            market="overseas",
            symbol="NPAC",
            exchange_code="NASD",
            side="SELL",
            order_kind="limit",
            requested_qty=396,
            requested_price=10.45,
            status="SUBMITTED",
            reason="virtual_sell_settlement",
            broker_order_no=order_no,
        )
    positions = [
        OverseasHeldPosition(
            symbol="NPAC",
            exchange_code="NASD",
            quantity=396,
            orderable_qty=396,
            avg_price=10.38,
            current_price=10.4659,
            pnl_pct=0.008,
        )
    ]
    quotes = [
        OverseasScanResult(
            symbol="NPAC",
            exchange_code="NASD",
            last_price=10.4659,
            bid=0.0,
            ask=0.0,
            spread_pct=0.0,
            change_rate_pct=0.0,
            volume=0,
            orderable_qty=0,
            fx_rate_krw=1380.0,
            activity_score=0.0,
        )
    ]
    now = datetime(2026, 8, 20, 13, 35, tzinfo=timezone.utc)

    asyncio.run(
        service._reconcile_pending_virtual_sells(
            overseas_positions=positions,
            overseas_ranked=quotes,
            now=now,
        )
    )
    asyncio.run(
        service._reconcile_pending_virtual_sells(
            overseas_positions=positions,
            overseas_ranked=quotes,
            now=now + timedelta(minutes=1),
        )
    )

    assert service.client.order_calls == []
    assert service.repository.get_virtual_sell_pending("overseas", "NPAC") is not None
    events = service.repository.list_event_log(
        event_type="virtual_pending_settlement_deferred",
        limit=5,
    )
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["reason"] == (
        "zero_volume_after_repeated_no_fill"
    )
    assert len(service.notifier.messages) == 1
    assert "거래량 0" in service.notifier.messages[0]
    assert "거래량 회복 시 자동 재시도" in service.notifier.messages[0]


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
    entry_time = "2026-06-30T13:00:00+00:00"
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=2,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-06-30 20:00:00 KST",
        strategy_flag="VWAP+VOL+RSI",
        entry_by="VWAP",
        entry_reason="strategy_guard_probe:VWAP+VOL+RSI|pullback_buy",
        entry_time=entry_time,
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
    submitted = service.repository.list_unfinalized_broker_executions()[0]
    assert submitted["strategy_flag"] == "VWAP+VOL+RSI"
    assert submitted["entry_by"] == "VWAP"
    assert submitted["entry_time"] == entry_time
    assert submitted["context_json"]["entry_reason"].startswith(
        "strategy_guard_probe:"
    )
    service.client.pending_orders = [
        {
            "odno": "0000000001",
            "pdno": "NVDA",
            "sll_buy_dvsn_cd": "01",
            "ft_ord_qty": "2",
            "ft_ccld_qty": "2",
            "ft_ccld_amt3": "222.0",
            "ft_ccld_unpr3": "111.0",
            "nccs_qty": "0",
            "rvse_cncl_dvsn": "00",
            "dmst_ord_dt": "20260701",
            "thco_ord_tmd": "223100",
        }
    ]
    stats = asyncio.run(
        service._reconcile_broker_executions(
            datetime(2026, 7, 1, 13, 32, tzinfo=timezone.utc),
            force=True,
        )
    )

    assert stats["finalized"] == 1
    assert service.repository.get_virtual_sell_pending("overseas", "NVDA") is None
    account_rows = service.repository.query_cycle_log(
        action_bias="SELL_REAL",
        limit=5,
    )
    assert len(account_rows) == 1
    assert account_rows[0]["action_reason"] == "virtual_sell_settlement"
    assert account_rows[0]["strategy_flag"] == "VWAP+VOL+RSI"
    assert account_rows[0]["entry_by"] == "VWAP"
    assert account_rows[0]["entry_time"] == entry_time
    assert account_rows[0]["session_id"] == ""
    assert account_rows[0]["is_session_trade"] == 0
    assert service.repository.get_realized_strategy_performance() == []
    strategy_summary = service.repository.get_session_pnl_summary(
        include_virtual=False,
    )
    assert strategy_summary["real"] == {}
    account_summary = service.repository.get_session_pnl_summary(
        include_virtual=False,
        include_non_session_real=True,
    )
    assert account_summary["real"]["overseas"]["trade_count"] == 1
    message = service.notifier.messages[-1]
    assert "[KIS][VIRTUAL_SETTLED]" in message
    assert "가상매도가=+$115.00" in message
    assert "전략손익=+$30.00" in message
    assert "실제체결가=+$111.00" in message
    assert "정산슬리피지=-$8.00" in message


def test_reconcile_partial_terminal_fill_reduces_only_confirmed_quantity() -> None:
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
            quantity=5,
            orderable_qty=3,
            avg_price=100.0,
            current_price=111.0,
            pnl_pct=0.11,
        )
    ]

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))
    execution = service.repository.list_unfinalized_broker_executions()[0]
    execution.update(
        {
            "filled_qty": 2,
            "filled_amount": 222.0,
            "avg_fill_price": 111.0,
            "remaining_qty": 0,
            "canceled_qty": 1,
            "status": "PARTIAL_CANCELED",
            "fill_recorded_at": "2026-07-01T13:31:00+00:00",
        }
    )

    applied = asyncio.run(
        service._apply_confirmed_execution_group(
            [execution],
            filled_qty=2,
            filled_amount=222.0,
            target_qty=3,
            reconciled_at=datetime(2026, 7, 1, 13, 32, tzinfo=timezone.utc),
        )
    )

    assert applied is True
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 3
    assert service.repository.list_unfinalized_broker_executions() == []
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_confirmed",
        limit=1,
    )[0]
    detail = json.loads(event["detail"])
    assert detail["settled_qty"] == 2
    assert detail["remaining_pending_qty"] == 3


def test_reconcile_no_fill_preserves_pending_and_defers_immediate_retry() -> None:
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
    execution = service.repository.list_unfinalized_broker_executions()[0]
    service.repository.finalize_broker_execution_group_without_fill(
        execution["execution_group_id"],
        finalized_at="2026-07-01T13:32:00+00:00",
    )
    asyncio.run(service._handle_no_fill_execution_group([execution]))

    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 2
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_no_fill",
        limit=1,
    )[0]
    assert json.loads(event["detail"])["retry_allowed"] is True

    asyncio.run(service._reconcile_pending_virtual_sells(overseas_positions=positions))
    assert len(service.client.order_calls) == 1
    deferred = service.repository.list_event_log(
        event_type="virtual_pending_settlement_deferred",
        limit=1,
    )[0]
    assert json.loads(deferred["detail"])["reason"] == "retry_cooldown"

    asyncio.run(
        service._reconcile_pending_virtual_sells(
            overseas_positions=positions,
            now=datetime.now(timezone.utc) + timedelta(minutes=16),
        )
    )
    assert len(service.client.order_calls) == 2


def test_virtual_settlement_submission_limit_is_durable_per_session() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NVDA",
        exchange_code="NASD",
        qty=2,
        avg_sell_price=115.0,
        currency="USD",
        updated_at="2026-08-17 20:00:00 KST",
    )
    for minute in (31, 46, 59):
        service.repository.save_broker_order_event(
            created_at=f"2026-08-17T14:{minute:02d}:00+00:00",
            market="overseas",
            symbol="NVDA",
            exchange_code="NASD",
            side="SELL",
            order_kind="limit",
            requested_qty=2,
            requested_price=111.0,
            status="SUBMITTED",
            reason="virtual_sell_settlement",
            broker_order_no=f"10{minute}",
            is_virtual=0,
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

    asyncio.run(
        service._reconcile_pending_virtual_sells(
            overseas_positions=positions,
            now=datetime(2026, 8, 17, 15, 30, tzinfo=timezone.utc),
        )
    )

    assert service.client.order_calls == []
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_deferred",
        limit=1,
    )[0]
    detail = json.loads(event["detail"])
    assert detail["reason"] == "session_submission_limit"
    assert detail["submission_count"] == 3
    assert detail["pending_preserved"] is True


def test_virtual_settlement_uses_marketable_limit_after_repeated_sessions() -> None:
    service = _build_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="NPAC",
        exchange_code="NASD",
        qty=396,
        avg_sell_price=10.49,
        currency="USD",
        updated_at="2026-08-14T13:30:00+00:00",
    )
    for created_at in (
        "2026-08-18T13:31:00+00:00",
        "2026-08-19T13:31:00+00:00",
    ):
        service.repository.save_broker_order_event(
            created_at=created_at,
            market="overseas",
            symbol="NPAC",
            exchange_code="NASD",
            side="SELL",
            order_kind="limit",
            requested_qty=396,
            requested_price=100.0,
            status="SUBMITTED",
            reason="virtual_sell_settlement",
            broker_order_no=created_at[8:10],
            is_virtual=0,
        )
    positions = [
        OverseasHeldPosition(
            symbol="NPAC",
            exchange_code="NASD",
            quantity=396,
            orderable_qty=396,
            avg_price=98.0,
            current_price=100.0,
            pnl_pct=0.02,
        )
    ]
    quotes = [
        OverseasScanResult(
            symbol="NPAC",
            exchange_code="NASD",
            last_price=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.002,
            change_rate_pct=0.5,
            volume=1_000_000,
            orderable_qty=396,
            fx_rate_krw=1350.0,
            activity_score=10.0,
        )
    ]

    asyncio.run(
        service._reconcile_pending_virtual_sells(
            overseas_positions=positions,
            overseas_ranked=quotes,
            now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
        )
    )

    assert service.client.order_calls[0]["price"] == "99.5000"
    events = service.repository.list_broker_order_events(limit=1)
    assert events[0]["order_kind"] == "aggressive_limit"
    payload = events[0]["payload_json"]
    pricing = payload["settlement_pricing"]
    assert pricing["aggressive"] is True
    assert pricing["failed_session_count"] == 2
    assert pricing["quote_bid"] == 99.9
    assert pricing["requested_price"] == 99.5
    assert "주문방식=aggressive_limit" in service.notifier.messages[-1]


def test_stale_virtual_settlement_is_canceled_without_clearing_pending() -> None:
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
    execution = service.repository.list_unfinalized_broker_executions()[0]
    execution["created_at"] = "2026-06-30T13:30:00+00:00"
    service.client.pending_orders = [
        {
            "pdno": "NVDA",
            "sll_buy_dvsn_cd": "01",
            "nccs_qty": "2",
            "odno": "0000000001",
            "ft_ord_unpr3": "111.0",
        }
    ]

    canceled = asyncio.run(
        service._cancel_stale_virtual_sell_settlement(
            execution=execution,
            exchange_code="NASD",
            now=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        )
    )

    assert canceled is True
    assert len(service.client.cancel_calls) == 1
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert pending["qty"] == 2
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_cancel_submitted",
        limit=1,
    )[0]
    assert json.loads(event["detail"])["pending_preserved_until_history_confirmation"]


def test_virtual_settlement_is_not_canceled_before_policy_age() -> None:
    service = _build_service()
    now = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
    execution = {
        "created_at": (now - timedelta(minutes=4)).isoformat(),
        "execution_group_id": "settlement-group",
        "market": "overseas",
        "symbol": "NVDA",
        "exchange_code": "NASD",
        "side": "SELL",
        "broker_order_no": "0000000001",
    }
    service.client.pending_orders = [
        {
            "pdno": "NVDA",
            "sll_buy_dvsn_cd": "01",
            "nccs_qty": "2",
            "odno": "0000000001",
            "ft_ord_unpr3": "111.0",
        }
    ]

    canceled = asyncio.run(
        service._cancel_stale_virtual_sell_settlement(
            execution=execution,
            exchange_code="NASD",
            now=now,
        )
    )

    assert canceled is False
    assert service.client.cancel_calls == []


def test_stale_virtual_settlement_preserves_order_when_lookup_fails() -> None:
    service = _build_service()
    execution = {
        "created_at": "2026-06-30T13:30:00+00:00",
        "execution_group_id": "settlement-group",
        "market": "overseas",
        "symbol": "NVDA",
        "exchange_code": "NASD",
        "side": "SELL",
        "broker_order_no": "0000000001",
    }

    async def failing_history(**_kwargs):
        raise KisApiError("EGW00300 gateway routing error")

    service.client.get_overseas_order_history = failing_history

    canceled = asyncio.run(
        service._cancel_stale_virtual_sell_settlement(
            execution=execution,
            exchange_code="NASD",
            now=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        )
    )

    assert canceled is False
    assert service.client.cancel_calls == []
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_cancel_skipped",
        limit=1,
    )[0]
    detail = json.loads(event["detail"])
    assert detail["reason"] == "open_order_lookup_failed"
    assert detail["broker_order_no"] == "0000000001"


def test_stale_virtual_settlement_does_not_cancel_mismatched_open_sell() -> None:
    service = _build_service()
    execution = {
        "created_at": "2026-06-30T13:30:00+00:00",
        "execution_group_id": "settlement-group",
        "market": "overseas",
        "symbol": "NVDA",
        "exchange_code": "NASD",
        "side": "SELL",
        "broker_order_no": "0000000001",
    }
    service.client.pending_orders = [
        {
            "pdno": "NVDA",
            "sll_buy_dvsn_cd": "01",
            "nccs_qty": "2",
            "odno": "0000009999",
            "ft_ord_unpr3": "111.0",
        }
    ]

    canceled = asyncio.run(
        service._cancel_stale_virtual_sell_settlement(
            execution=execution,
            exchange_code="NASD",
            now=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        )
    )

    assert canceled is False
    assert service.client.cancel_calls == []
    event = service.repository.list_event_log(
        event_type="virtual_pending_settlement_cancel_skipped",
        limit=1,
    )[0]
    assert json.loads(event["detail"])["reason"] == "open_sell_order_number_mismatch"
