import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import kinvest_trade.liquidity_lab as liquidity_lab_module
from kinvest_trade.config import load_app_config
from kinvest_trade.liquidity_lab import (
    DomesticHeldPosition,
    DomesticScanResult,
    LiquidityLabService,
    LiquidityLabReport,
    OverseasHeldPosition,
    OverseasScanResult,
    UnifiedPositionTracker,
    VirtualTradeManager,
    WatchTargetStatus,
)
from kinvest_trade.client import KisApiError
from kinvest_trade.repository import SqliteRepository
from kinvest_trade.technical_signals import MovingAverageSnapshot


def test_select_primary_target_reports_mock_daytime_limit() -> None:
    market, target, reason = LiquidityLabService._select_primary_target(
        krx_open=False,
        us_open=True,
        us_orderable_in_profile=False,
        domestic_ranked=[],
        overseas_ranked=[
            OverseasScanResult(
                symbol="SOXL",
                exchange_code="AMEX",
                last_price=10.0,
                bid=9.99,
                ask=10.01,
                spread_pct=0.002,
                change_rate_pct=1.0,
                volume=1000,
                orderable_qty=10,
                fx_rate_krw=1300.0,
                activity_score=10.0,
            )
        ],
    )

    assert market == "none"
    assert target is None
    assert reason == "us_open_but_mock_session_not_supported"


def test_domestic_speculative_reasons_flag_low_price_and_turnover() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "domestic_min_price_krw": 5000,
                    "domestic_min_intraday_turnover_krw": 50_000_000_000,
                    "domestic_min_volume_sum": 30_000,
                    "domestic_max_spread_pct": 0.003,
                },
            )()
        },
    )()
    candidate = type(
        "DomesticCandidate",
        (),
        {
            "current_price": 1800,
            "intraday_turnover_krw": 10_000_000_000,
            "volume_sum": 10_000,
            "spread_pct": 0.005,
        },
    )()

    reasons = service._domestic_speculative_reasons(candidate)

    assert reasons == [
        "low_price_krw",
        "thin_intraday_turnover",
        "thin_recent_volume",
        "wide_spread",
    ]


def test_overseas_speculative_reasons_flag_low_volume_and_spread() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_min_price_usd": 10.0,
                    "overseas_min_volume": 50_000,
                    "overseas_max_spread_pct": 0.004,
                },
            )()
        },
    )()
    candidate = OverseasScanResult(
        symbol="AAL",
        exchange_code="NASD",
        last_price=8.5,
        bid=8.4,
        ask=8.6,
        spread_pct=0.0235,
        change_rate_pct=1.0,
        volume=12_000,
        orderable_qty=100,
        fx_rate_krw=1300.0,
        activity_score=10.0,
    )

    reasons = service._overseas_speculative_reasons(candidate)

    assert reasons == [
        "low_price_usd",
        "thin_volume",
        "wide_spread",
        "thin_turnover",
    ]


def test_overseas_speculative_reasons_flag_thin_turnover() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_min_price_usd": 5.0,
                    "overseas_min_volume": 500_000,
                    "overseas_max_spread_pct": 0.003,
                },
            )()
        },
    )()
    candidate = OverseasScanResult(
        symbol="AAL",
        exchange_code="NASD",
        last_price=6.0,
        bid=5.99,
        ask=6.01,
        spread_pct=0.002,
        change_rate_pct=1.0,
        volume=100_000,
        orderable_qty=100,
        fx_rate_krw=1300.0,
        activity_score=10.0,
    )

    reasons = service._overseas_speculative_reasons(candidate)

    assert "thin_turnover" in reasons


def test_select_overseas_exit_target_prioritizes_stop_loss() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.012,
                    "overseas_stop_loss_pct": 0.008,
                },
            )()
        },
    )()
    overseas_ranked = [
        OverseasScanResult(
            symbol="SOXL",
            exchange_code="AMEX",
            last_price=255.73,
            bid=255.70,
            ask=255.76,
            spread_pct=0.0002,
            change_rate_pct=3.0,
            volume=1000000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=10.0,
        ),
        OverseasScanResult(
            symbol="AAL",
            exchange_code="NASD",
            last_price=17.50,
            bid=17.49,
            ask=17.51,
            spread_pct=0.0011,
            change_rate_pct=-0.8,
            volume=500000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=9.0,
        ),
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="SOXL",
            exchange_code="AMEX",
            quantity=10,
            orderable_qty=10,
            avg_price=247.72,
            current_price=255.73,
            pnl_pct=0.0323,
        ),
        OverseasHeldPosition(
            symbol="AAL",
            exchange_code="NASD",
            quantity=5,
            orderable_qty=5,
            avg_price=17.655,
            current_price=17.50,
            pnl_pct=-0.0088,
        ),
    ]

    import asyncio

    candidate, held, reason, signal_snapshot = asyncio.run(
        service._select_overseas_exit_target(overseas_ranked, held_positions)
    )

    assert candidate.symbol == "AAL"
    assert held.symbol == "AAL"
    assert reason == "stop_loss"
    assert signal_snapshot is None


def test_manage_overseas_position_waits_when_already_holding_max_qty() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_max_position_qty": 1,
                },
            )()
        },
    )()
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=255.73,
        bid=255.70,
        ask=255.76,
        spread_pct=0.0002,
        change_rate_pct=3.0,
        volume=1000000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held_positions = [
        OverseasHeldPosition(
            symbol="SOXL",
            exchange_code="AMEX",
            quantity=3,
            orderable_qty=3,
            avg_price=250.0,
            current_price=255.73,
            pnl_pct=0.02,
        )
    ]

    import asyncio

    result = asyncio.run(
        service._manage_overseas_position(candidate=candidate, held_positions=held_positions)
    )

    assert result["skipped"] is True
    assert result["reason"] == "already_holding_max_qty_waiting_for_exit"


def test_manage_overseas_position_waits_when_exit_order_already_pending() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_max_position_qty": 10,
                },
            )()
        },
    )()
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=255.73,
        bid=255.70,
        ask=255.76,
        spread_pct=0.0002,
        change_rate_pct=3.0,
        volume=1000000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held_positions = [
        OverseasHeldPosition(
            symbol="SOXL",
            exchange_code="AMEX",
            quantity=3,
            orderable_qty=0,
            avg_price=250.0,
            current_price=255.73,
            pnl_pct=0.02,
        )
    ]

    import asyncio

    result = asyncio.run(
        service._manage_overseas_position(candidate=candidate, held_positions=held_positions)
    )

    assert result["skipped"] is True
    assert result["reason"] == "pending_exit_order"


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _build_repository() -> SqliteRepository:
    return SqliteRepository(Path(tempfile.mkdtemp()) / "liquidity_lab_test.db")


def _snapshot(**overrides) -> MovingAverageSnapshot:
    payload = dict(
        price=20.0,
        spread_pct=0.001,
        daily_ma_fast=19.8,
        daily_ma_slow=19.4,
        minute_ma_fast=20.1,
        minute_ma_slow=19.9,
        prev_minute_ma_fast=19.9,
        prev_minute_ma_slow=19.8,
        rsi14=58.0,
        intraday_volatility=0.001,
        intraday_momentum=0.003,
        intraday_bar_return=0.0012,
        volume_last=200_000.0,
        volume_avg=100_000.0,
        volume_ratio=2.0,
        breakout_level=19.9,
        breakdown_level=19.2,
        breakout_distance_pct=0.002,
        atr=0.2,
        atr_pct=0.01,
        bollinger_basis=19.7,
        bollinger_upper=20.2,
        bollinger_lower=19.2,
        daily_gap_fast_pct=0.01,
        daily_gap_slow_pct=0.02,
        minute_gap_slow_pct=0.004,
        fast_above_slow=True,
        crossed_up=False,
        crossed_down=False,
        regime="momentum_breakout",
    )
    payload.update(overrides)
    return MovingAverageSnapshot(**payload)


class DummySellClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def place_overseas_order_for_current_session(
        self,
        *,
        side: str,
        symbol: str,
        exchange_code: str,
        qty: int,
        price: str,
        order_division: str,
    ):
        if self.error is not None:
            raise self.error
        return {
            "side": side,
            "symbol": symbol,
            "exchange_code": exchange_code,
            "qty": qty,
            "price": price,
            "order_division": order_division,
        }


def _build_sell_service(*, dry_run: bool = False, error: Exception | None = None) -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "credentials": type("Creds", (), {"dry_run": dry_run})(),
        },
    )()
    service.client = DummySellClient(error=error)
    service.repository = _build_repository()
    service.virtual_trades = VirtualTradeManager(service.repository)
    service.position_tracker = UnifiedPositionTracker(service.repository, service.virtual_trades)
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    service._session_id = "sess-test"
    return service


def test_place_overseas_sell_order_sends_telegram_on_success() -> None:
    service = _build_sell_service()
    candidate = OverseasScanResult(
        symbol="TSLA",
        exchange_code="NASD",
        last_price=282.0,
        bid=281.9,
        ask=282.1,
        spread_pct=0.0007,
        change_rate_pct=1.2,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="TSLA",
        exchange_code="NASD",
        quantity=2,
        orderable_qty=2,
        avg_price=280.0,
        current_price=282.0,
        pnl_pct=0.0071,
    )

    import asyncio

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "atr_hard_stop"))

    assert result["submitted"] is True
    assert len(service.notifier.messages) == 1
    message = service.notifier.messages[0]
    assert "[KIS][LAB_SELL]" in message
    assert "손익=+$4.00" in message
    assert "수익률=+0.71%" in message


def test_place_overseas_sell_order_saves_realized_pnl_cycle_log() -> None:
    service = _build_sell_service()
    candidate = OverseasScanResult(
        symbol="TSLA",
        exchange_code="NASD",
        last_price=282.0,
        bid=281.9,
        ask=282.1,
        spread_pct=0.0007,
        change_rate_pct=1.2,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="TSLA",
        exchange_code="NASD",
        quantity=2,
        orderable_qty=2,
        avg_price=280.0,
        current_price=282.0,
        pnl_pct=0.0071,
    )

    asyncio.run(service._place_overseas_sell_order(candidate, held, "atr_hard_stop"))
    rows = service.repository.query_cycle_log(action_bias="SELL_REAL", limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "TSLA"
    assert rows[0]["session_id"] == "sess-test"
    assert rows[0]["realized_pnl_usd"] == 4.0
    assert rows[0]["realized_pnl_krw"] == 5520.0


def test_real_sell_clears_virtual_sell_pending() -> None:
    service = _build_sell_service()
    service.repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="AAL",
        exchange_code="NASD",
        qty=1,
        avg_sell_price=18.0,
        currency="USD",
        updated_at="2026-07-02T04:40:00+00:00",
    )
    candidate = OverseasScanResult(
        symbol="AAL",
        exchange_code="NASD",
        last_price=17.50,
        bid=17.49,
        ask=17.51,
        spread_pct=0.0011,
        change_rate_pct=-0.8,
        volume=500_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=9.0,
    )
    held = OverseasHeldPosition(
        symbol="AAL",
        exchange_code="NASD",
        quantity=973,
        orderable_qty=973,
        avg_price=17.655,
        current_price=17.50,
        pnl_pct=-0.0088,
    )

    asyncio.run(service._place_overseas_sell_order(candidate, held, "momentum_loss_cut"))

    assert service.repository.get_virtual_sell_pending("overseas", "AAL") is None


def test_place_overseas_sell_order_no_telegram_on_failure() -> None:
    service = _build_sell_service(error=KisApiError("failed"))
    candidate = OverseasScanResult(
        symbol="TSLA",
        exchange_code="NASD",
        last_price=282.0,
        bid=281.9,
        ask=282.1,
        spread_pct=0.0007,
        change_rate_pct=1.2,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="TSLA",
        exchange_code="NASD",
        quantity=2,
        orderable_qty=2,
        avg_price=280.0,
        current_price=282.0,
        pnl_pct=0.0071,
    )

    import asyncio

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "atr_hard_stop"))

    assert result["submitted"] is False
    assert service.notifier.messages == []


def test_overseas_sell_rejected_converts_to_virtual_trade() -> None:
    service = _build_sell_service(
        error=KisApiError("KIS mock does not support US daytime trading for this session")
    )
    candidate = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=196.96,
        bid=196.95,
        ask=196.97,
        spread_pct=0.0001,
        change_rate_pct=0.9,
        volume=2_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=12.0,
    )
    held = OverseasHeldPosition(
        symbol="NVDA",
        exchange_code="NASD",
        quantity=1,
        orderable_qty=1,
        avg_price=193.46,
        current_price=196.96,
        pnl_pct=0.0181,
    )

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "take_profit"))

    assert result["submitted"] is True
    assert result["virtual"] is True
    assert result["reason"] == "session_not_orderable_in_profile"
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert int(pending["qty"]) == 1
    assert service.virtual_trades.performance_summary()["overseas_USD"]["trade_count"] == 1


def test_overseas_sell_rejected_sends_virtual_trade_notification() -> None:
    service = _build_sell_service(
        error=KisApiError("KIS mock does not support US daytime trading for this session")
    )
    candidate = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=196.96,
        bid=196.95,
        ask=196.97,
        spread_pct=0.0001,
        change_rate_pct=0.9,
        volume=2_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=12.0,
    )
    held = OverseasHeldPosition(
        symbol="NVDA",
        exchange_code="NASD",
        quantity=1,
        orderable_qty=1,
        avg_price=193.46,
        current_price=196.96,
        pnl_pct=0.0181,
    )

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "take_profit"))

    assert result["submitted"] is True
    assert any("[KIS][VIRTUAL_TRADE]" in message for message in service.notifier.messages)
    assert "실보유정산대기=1주" in service.notifier.messages[-1]


def test_place_overseas_sell_order_unknown_pnl_when_avg_zero() -> None:
    service = _build_sell_service()
    candidate = OverseasScanResult(
        symbol="TSLA",
        exchange_code="NASD",
        last_price=282.0,
        bid=281.9,
        ask=282.1,
        spread_pct=0.0007,
        change_rate_pct=1.2,
        volume=1_000_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="TSLA",
        exchange_code="NASD",
        quantity=2,
        orderable_qty=2,
        avg_price=0.0,
        current_price=282.0,
        pnl_pct=0.0,
    )

    import asyncio

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "atr_hard_stop"))

    assert result["submitted"] is True
    assert "매입가=알수없음" in service.notifier.messages[0]


class DummyDomesticBalanceClient:
    async def get_balance(self):
        return {
            "positions": [
                {
                    "pdno": "005930",
                    "hldg_qty": "2",
                    "ord_psbl_qty": "1",
                    "pchs_avg_pric": "80000",
                }
            ]
        }


def test_load_domestic_positions_reads_balance() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.client = DummyDomesticBalanceClient()
    ranked = [
        DomesticScanResult(
            stock_code="005930",
            current_price=82000,
            best_ask=82050,
            best_bid=81950,
            spread_pct=0.0012,
            minute_change_pct=0.01,
            intraday_turnover_krw=100_000_000_000,
            volume_sum=500_000,
            activity_score=12.0,
        )
    ]

    import asyncio

    positions = asyncio.run(service._load_domestic_positions(ranked))

    assert positions == [
        DomesticHeldPosition(
            stock_code="005930",
            quantity=2,
            orderable_qty=1,
            avg_price=80000.0,
            current_price=82000,
            pnl_pct=0.025,
        )
    ]


class DummyDomesticSellClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def place_cash_order(
        self,
        *,
        side: str,
        stock_code: str,
        qty: int,
        price: int,
        order_division: str,
    ):
        if self.error is not None:
            raise self.error
        return {
            "side": side,
            "stock_code": stock_code,
            "qty": qty,
            "price": price,
            "order_division": order_division,
        }


def _build_domestic_sell_service(*, dry_run: bool = False, error: Exception | None = None) -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "credentials": type("Creds", (), {"dry_run": dry_run})(),
        },
    )()
    service.client = DummyDomesticSellClient(error=error)
    service.repository = _build_repository()
    service.virtual_trades = VirtualTradeManager(service.repository)
    service.position_tracker = UnifiedPositionTracker(service.repository, service.virtual_trades)
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    service._session_id = "sess-domestic"
    return service


def test_place_domestic_sell_order_sends_telegram_on_success() -> None:
    service = _build_domestic_sell_service()
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=-0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=11.0,
    )
    held = DomesticHeldPosition(
        stock_code="005930",
        quantity=2,
        orderable_qty=2,
        avg_price=80000.0,
        current_price=82000.0,
        pnl_pct=0.025,
    )

    import asyncio

    result = asyncio.run(service._place_domestic_sell_order(candidate, held, "stop_loss"))

    assert result["submitted"] is True
    message = service.notifier.messages[0]
    assert "[KIS][LAB_SELL]" in message
    assert "시장=국내" in message
    assert "손익=+3,900원" in message
    assert "수익률=+2.44%" in message


def test_place_domestic_sell_order_saves_realized_pnl_cycle_log() -> None:
    service = _build_domestic_sell_service()
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=-0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=11.0,
    )
    held = DomesticHeldPosition(
        stock_code="005930",
        quantity=2,
        orderable_qty=2,
        avg_price=80000.0,
        current_price=82000.0,
        pnl_pct=0.025,
    )

    asyncio.run(service._place_domestic_sell_order(candidate, held, "stop_loss"))
    rows = service.repository.query_cycle_log(action_bias="SELL_REAL", limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["session_id"] == "sess-domestic"
    assert rows[0]["realized_pnl_krw"] == 3900.0


def test_domestic_sell_rejected_marks_skipped_true() -> None:
    service = _build_domestic_sell_service(error=KisApiError("domestic rejected"))
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=-0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=11.0,
    )
    held = DomesticHeldPosition(
        stock_code="005930",
        quantity=2,
        orderable_qty=2,
        avg_price=80000.0,
        current_price=82000.0,
        pnl_pct=0.025,
    )

    result = asyncio.run(service._place_domestic_sell_order(candidate, held, "stop_loss"))

    assert result["submitted"] is False
    assert result["skipped"] is True
    assert result["reason"] == "order_rejected"


def test_domestic_buy_rejected_marks_skipped_true() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(dry_run=False),
        liquidity_lab=SimpleNamespace(
            domestic_test_order_qty=1,
            use_slot_sizing=False,
        ),
    )
    service.client = DummyDomesticSellClient(error=KisApiError("domestic rejected"))
    service.notifier = DummyNotifier()
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=11.0,
    )

    result = asyncio.run(service._place_domestic_test_order(candidate))

    assert result["submitted"] is False
    assert result["skipped"] is True
    assert result["reason"] == "order_rejected"
    assert service.notifier.messages == []


def test_domestic_buy_uses_slot_sizing_when_balance_is_available() -> None:
    class DummyDomesticSlotClient(DummyDomesticSellClient):
        async def get_balance(self):
            return {"summary": {"ord_psbl_cash": "2500000"}}

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(dry_run=False),
        liquidity_lab=SimpleNamespace(
            domestic_test_order_qty=1,
            use_slot_sizing=True,
            slot_entry_pct=0.10,
            slot_max_pct=0.20,
        ),
    )
    service.client = DummyDomesticSlotClient()
    service.notifier = DummyNotifier()
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=80000,
        best_ask=80000,
        best_bid=79950,
        spread_pct=0.0006,
        minute_change_pct=0.002,
        intraday_turnover_krw=120_000_000_000,
        volume_sum=600_000,
        activity_score=12.0,
    )

    result = asyncio.run(service._place_domestic_test_order(candidate))

    assert result["submitted"] is True
    assert result["qty"] == 3


def test_domestic_buy_saves_buy_real_cycle_log() -> None:
    class DummyDomesticSlotClient(DummyDomesticSellClient):
        async def get_balance(self):
            return {"summary": {"ord_psbl_cash": "2500000"}}

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(dry_run=False),
        liquidity_lab=SimpleNamespace(
            domestic_test_order_qty=1,
            use_slot_sizing=True,
            slot_entry_pct=0.10,
            slot_max_pct=0.20,
        ),
    )
    service.client = DummyDomesticSlotClient()
    service.repository = _build_repository()
    service.notifier = DummyNotifier()
    service._session_id = "sess-domestic-buy"
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=80000,
        best_ask=80000,
        best_bid=79950,
        spread_pct=0.0006,
        minute_change_pct=0.002,
        intraday_turnover_krw=120_000_000_000,
        volume_sum=600_000,
        activity_score=12.0,
    )

    result = asyncio.run(service._place_domestic_test_order(candidate))
    rows = service.repository.query_cycle_log(action_bias="BUY_REAL", limit=5)

    assert result["submitted"] is True
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["session_id"] == "sess-domestic-buy"
    assert rows[0]["action_reason"] == "domestic_buy"


def test_select_domestic_exit_target_uses_held_position_watch_targets() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    ranked = [
        DomesticScanResult(
            stock_code="005930",
            current_price=82000,
            best_ask=82050,
            best_bid=81950,
            spread_pct=0.0012,
            minute_change_pct=-0.003,
            intraday_turnover_krw=100_000_000_000,
            volume_sum=500_000,
            activity_score=11.0,
        )
    ]
    watch_targets = [
        type(
            "WatchTarget",
            (),
            {
                "market": "domestic",
                "code": "005930",
                "action_bias": "SELL",
                "note": "stop_loss",
            },
        )()
    ]
    held_positions = [
        DomesticHeldPosition(
            stock_code="005930",
            quantity=2,
            orderable_qty=2,
            avg_price=80000.0,
            current_price=82000.0,
            pnl_pct=-0.01,
        )
    ]

    result = service._select_domestic_exit_target(ranked, watch_targets, held_positions)

    assert result is not None
    candidate, held, reason, signal_snapshot = result
    assert candidate.stock_code == "005930"
    assert held.stock_code == "005930"
    assert reason == "stop_loss"
    assert signal_snapshot is None


def test_select_domestic_buy_targets_returns_multiple() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    ranked = [
        DomesticScanResult("005930", 82000, 82050, 81950, 0.0012, 0.003, 100_000_000_000, 500_000, 18.0),
        DomesticScanResult("000660", 210000, 210500, 209500, 0.0015, 0.004, 90_000_000_000, 420_000, 17.0),
        DomesticScanResult("035420", 180000, 180500, 179500, 0.0014, 0.002, 85_000_000_000, 390_000, 16.0),
    ]
    watch_targets = [
        WatchTargetStatus("domestic", "005930", None, 82000.0, 18.0, 12.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("domestic", "000660", None, 210000.0, 17.0, 11.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("domestic", "005930", None, 82000.0, 18.0, 10.0, "BUY", "BUY_READY", "20d>60d 5>20", "duplicate", 0),
        WatchTargetStatus("domestic", "035420", None, 180000.0, 16.0, 9.0, "BUY", "BUY_READY", "20d>60d 5>20", "volume_breakout_entry", 0),
    ]

    selected = service._select_domestic_buy_targets(ranked, watch_targets, max_concurrent=2)

    assert [item.stock_code for item in selected] == ["005930", "000660"]


def test_select_domestic_buy_targets_returns_empty_when_no_buy() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    ranked = [
        DomesticScanResult("005930", 82000, 82050, 81950, 0.0012, 0.003, 100_000_000_000, 500_000, 18.0),
    ]
    watch_targets = [
        WatchTargetStatus("domestic", "005930", None, 82000.0, 18.0, 0.0, "WAIT", "WAIT", "20d>60d 5>20", "watch", 0),
    ]

    selected = service._select_domestic_buy_targets(ranked, watch_targets)

    assert selected == []


def _build_run_service() -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    project_root = Path(__file__).resolve().parents[1]
    base_auto = load_app_config(project_root / "config" / "fixed_config.json").auto_trade
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(env="vps", dry_run=False),
        liquidity_lab=SimpleNamespace(
            unified_watch_top_n=3,
            unified_scan_top_n=3,
            domestic_candidates=[],
            overseas_candidates=[],
            overseas_scan_top_n=3,
            overseas_test_order_qty=1,
            overseas_max_position_qty=3,
            overseas_take_profit_pct=0.012,
            overseas_stop_loss_pct=0.008,
            max_concurrent_overseas_orders=3,
            max_concurrent_domestic_orders=2,
            use_slot_sizing=False,
            slot_entry_pct=0.10,
            slot_max_pct=0.20,
        ),
        auto_trade=base_auto,
    )
    service.client = object()
    service.repository = _build_repository()
    service.virtual_trades = VirtualTradeManager(service.repository)
    service.position_tracker = UnifiedPositionTracker(service.repository, service.virtual_trades)
    service.notifier = DummyNotifier()
    service._domestic_excluded = []
    service._overseas_excluded = []
    service._last_held_symbols = set()
    service._signal_cache = {}
    return service


def test_build_unified_watch_targets_merges_domestic_and_overseas() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 4
    service.config.liquidity_lab.domestic_candidates = ["D1", "D2", "D3"]
    domestic_ranked = [
        DomesticScanResult("D1", 10100, 10110, 10090, 0.001, 0.01, 9_000_000_000, 100_000, 50.0),
        DomesticScanResult("D2", 9900, 9910, 9890, 0.001, 0.008, 8_000_000_000, 90_000, 40.0),
        DomesticScanResult("D3", 9800, 9810, 9790, 0.001, 0.007, 7_000_000_000, 80_000, 30.0),
    ]
    overseas_ranked = [
        OverseasScanResult("O1", "NASD", 51.0, 50.9, 51.1, 0.001, 1.0, 100_000, 0, 1350.0, 60.0),
        OverseasScanResult("O2", "NASD", 41.0, 40.9, 41.1, 0.001, 1.0, 100_000, 0, 1350.0, 55.0),
        OverseasScanResult("O3", "NYSE", 31.0, 30.9, 31.1, 0.001, 1.0, 100_000, 0, 1350.0, 25.0),
    ]
    service._signal_cache = {
        "O1": _snapshot(price=51.0),
        "O2": _snapshot(price=41.0),
        "O3": _snapshot(price=31.0),
    }

    async def fake_load_domestic_signal(candidate):
        return _snapshot(price=float(candidate.current_price))

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_positions=[],
            overseas_positions=[],
            krx_open=True,
            us_open=True,
        )
    )

    assert [(item.market, item.code) for item in watch_targets] == [
        ("overseas", "O1"),
        ("overseas", "O2"),
        ("domestic", "D1"),
        ("domestic", "D2"),
    ]


def test_cycle_log_saved_per_watch_target() -> None:
    service = _build_run_service()
    service._cycle_count = 9
    service.config.liquidity_lab.unified_watch_top_n = 3
    domestic_ranked = [
        DomesticScanResult("D1", 10100, 10110, 10090, 0.001, 0.01, 9_000_000_000, 100_000, 50.0),
    ]
    overseas_ranked = [
        OverseasScanResult("O1", "NASD", 51.0, 50.9, 51.1, 0.001, 1.0, 100_000, 0, 1350.0, 60.0),
        OverseasScanResult("O2", "NASD", 41.0, 40.9, 41.1, 0.001, 1.0, 100_000, 0, 1350.0, 55.0),
    ]
    service._signal_cache = {
        "O1": _snapshot(price=51.0),
        "O2": _snapshot(price=41.0),
    }

    async def fake_load_domestic_signal(candidate):
        return _snapshot(price=float(candidate.current_price))

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_positions=[],
            overseas_positions=[],
            krx_open=True,
            us_open=True,
        )
    )
    rows = service.repository.query_cycle_log(limit=10)

    assert len(rows) == len(watch_targets)
    assert rows[0]["cycle_no"] == 9
    assert {row["symbol"] for row in rows} == {"D1", "O1", "O2"}


def test_unified_watch_includes_held_domestic_regardless_of_rank() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 2
    domestic_ranked = [
        DomesticScanResult("D1", 10100, 10110, 10090, 0.001, 0.01, 9_000_000_000, 100_000, 50.0),
        DomesticScanResult("D2", 9900, 9910, 9890, 0.001, 0.008, 8_000_000_000, 90_000, 40.0),
        DomesticScanResult("D3", 9800, 9810, 9790, 0.001, 0.007, 7_000_000_000, 80_000, 10.0),
    ]
    held_positions = [
        DomesticHeldPosition(
            stock_code="D3",
            quantity=2,
            orderable_qty=2,
            avg_price=9700.0,
            current_price=9800.0,
            pnl_pct=0.0103,
        )
    ]

    async def fake_load_domestic_signal(candidate):
        return _snapshot(price=float(candidate.current_price))

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=[],
            domestic_positions=held_positions,
            overseas_positions=[],
            krx_open=True,
            us_open=False,
        )
    )

    assert [item.code for item in watch_targets] == ["D3", "D1", "D2"]


def test_domestic_signal_none_skips_watch_target() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 2
    domestic_ranked = [
        DomesticScanResult("005930", 82000, 82050, 81950, 0.0012, 0.003, 100_000_000_000, 500_000, 18.0),
    ]

    async def fake_load_domestic_signal(candidate):
        return None

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=[],
            domestic_positions=[],
            overseas_positions=[],
            krx_open=True,
            us_open=False,
        )
    )

    assert watch_targets == []


def test_held_position_shows_hold_not_wait() -> None:
    service = _build_run_service()
    held = OverseasHeldPosition(
        symbol="NVDA",
        exchange_code="NASD",
        quantity=2,
        orderable_qty=2,
        avg_price=150.0,
        current_price=151.0,
        pnl_pct=0.0067,
    )

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="NVDA",
        exchange_code="NASD",
        price=151.0,
        activity_score=20.0,
        signal_snapshot=_snapshot(price=151.0, volume_ratio=1.2),
        held_position=held,
        holding_qty=2,
    )

    assert watch_target.action_bias == "HOLD"
    assert watch_target.signal_state == "HOLD"


def test_buy_target_excludes_hold_status() -> None:
    service = _build_run_service()
    overseas_ranked = [
        OverseasScanResult("NVDA", "NASD", 150.0, 149.9, 150.1, 0.0013, 2.0, 900_000, 10, 1350.0, 18.0),
        OverseasScanResult("AMD", "NASD", 155.0, 154.9, 155.1, 0.0012, 1.5, 800_000, 10, 1350.0, 17.0),
        OverseasScanResult("AAPL", "NASD", 210.0, 209.9, 210.1, 0.0010, 1.2, 700_000, 10, 1350.0, 16.0),
    ]
    watch_targets = [
        WatchTargetStatus("overseas", "NVDA", "NASD", 150.0, 18.0, 12.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "AMD", "NASD", 155.0, 17.0, 11.0, "HOLD", "HOLD", "20d>60d 5>20", "trend_holding", 3),
        WatchTargetStatus("overseas", "AAPL", "NASD", 210.0, 16.0, 10.0, "HOLD", "HOLD", "20d>60d 5>20", "hold", 2),
    ]

    selected = service._select_overseas_buy_targets(overseas_ranked, watch_targets, max_concurrent=3)

    assert [item.symbol for item in selected] == ["NVDA"]


def test_select_overseas_buy_targets_returns_multiple() -> None:
    service = _build_run_service()
    overseas_ranked = [
        OverseasScanResult("NVDA", "NASD", 150.0, 149.9, 150.1, 0.0013, 2.0, 900_000, 10, 1350.0, 18.0),
        OverseasScanResult("AMD", "NASD", 155.0, 154.9, 155.1, 0.0012, 1.5, 800_000, 10, 1350.0, 17.0),
        OverseasScanResult("AAPL", "NASD", 210.0, 209.9, 210.1, 0.0010, 1.2, 700_000, 10, 1350.0, 16.0),
        OverseasScanResult("MSFT", "NASD", 430.0, 429.9, 430.1, 0.0009, 1.1, 600_000, 10, 1350.0, 15.0),
    ]
    watch_targets = [
        WatchTargetStatus("overseas", "NVDA", "NASD", 150.0, 18.0, 12.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "AMD", "NASD", 155.0, 17.0, 11.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "AAPL", "NASD", 210.0, 16.0, 10.0, "BUY", "BUY_READY", "20d>60d 5>20", "volume_breakout_entry", 0),
        WatchTargetStatus("overseas", "NVDA", "NASD", 150.0, 18.0, 9.0, "BUY", "BUY_READY", "20d>60d 5>20", "duplicate", 0),
        WatchTargetStatus("overseas", "MSFT", "NASD", 430.0, 15.0, 8.0, "WAIT", "WAIT", "20d>60d 5>20", "watch", 0),
    ]

    selected = service._select_overseas_buy_targets(overseas_ranked, watch_targets, max_concurrent=3)

    assert [item.symbol for item in selected] == ["NVDA", "AMD", "AAPL"]


def test_unified_watch_excludes_closed_market() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 3
    domestic_ranked = [
        DomesticScanResult("D1", 10100, 10110, 10090, 0.001, 0.01, 9_000_000_000, 100_000, 50.0),
    ]
    overseas_ranked = [
        OverseasScanResult("O1", "NASD", 51.0, 50.9, 51.1, 0.001, 1.0, 100_000, 0, 1350.0, 60.0),
    ]
    service._signal_cache = {"O1": _snapshot(price=51.0)}

    async def fake_load_domestic_signal(candidate):
        return _snapshot(price=float(candidate.current_price))

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_positions=[],
            overseas_positions=[],
            krx_open=False,
            us_open=True,
        )
    )

    assert [item.market for item in watch_targets] == ["overseas"]


def test_overseas_buy_records_virtual_trade_when_session_not_orderable() -> None:
    service = _build_run_service()
    candidate = OverseasScanResult(
        symbol="SMCI",
        exchange_code="NASD",
        last_price=41.0,
        bid=40.9,
        ask=41.1,
        spread_pct=0.0048,
        change_rate_pct=2.0,
        volume=500_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=15.0,
    )
    watch_target = WatchTargetStatus(
        market="overseas",
        code="SMCI",
        exchange_code="NASD",
        price=41.0,
        activity_score=15.0,
        signal_score=9.0,
        action_bias="BUY",
        signal_state="BUY_READY",
        ma_summary="20d>60d 5>20",
        note="volume_breakout_entry",
        holding_qty=0,
    )
    manage_calls: list[str] = []

    async def fake_scan_overseas():
        return [candidate], set()

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return []

    async def fake_build_unified_watch_targets(**kwargs):
        return [watch_target]

    async def fake_manage_overseas_position(*, candidate, held_positions):
        manage_calls.append(candidate.symbol)
        return {"submitted": True}

    async def fake_send_summary(report):
        return None

    async def fake_select_overseas_exit_target(overseas_ranked, overseas_positions):
        return None

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._select_overseas_exit_target = fake_select_overseas_exit_target  # type: ignore[method-assign]
    service._manage_overseas_position = fake_manage_overseas_position  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: False
    liquidity_lab_module.get_us_trading_session = lambda now: "daytime"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.primary_market == "overseas"
    assert report.primary_selection_reason == "watchlist_buy_signal"
    assert report.overseas_order["submitted"] is True
    assert report.overseas_order["virtual"] is True
    assert report.overseas_order["reason"] == "session_not_orderable_in_profile"
    assert manage_calls == []


def test_overseas_sell_still_attempted_when_session_not_orderable() -> None:
    service = _build_run_service()
    candidate = OverseasScanResult(
        symbol="SMCI",
        exchange_code="NASD",
        last_price=41.0,
        bid=40.9,
        ask=41.1,
        spread_pct=0.0048,
        change_rate_pct=2.0,
        volume=500_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=15.0,
    )
    held = OverseasHeldPosition(
        symbol="SMCI",
        exchange_code="NASD",
        quantity=1,
        orderable_qty=1,
        avg_price=45.0,
        current_price=41.0,
        pnl_pct=-0.0889,
    )
    sell_calls: list[str] = []

    async def fake_scan_overseas():
        return [candidate], {"SMCI"}

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return [held]

    async def fake_build_unified_watch_targets(**kwargs):
        return []

    async def fake_select_overseas_exit_target(overseas_ranked, overseas_positions):
        return candidate, held, "stop_loss", None

    async def fake_place_overseas_sell_order(candidate, held, exit_reason, signal_snapshot=None):
        sell_calls.append(candidate.symbol)
        return {"submitted": True, "side": "sell", "candidate": {"symbol": candidate.symbol}}

    async def fake_send_summary(report):
        return None

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._select_overseas_exit_target = fake_select_overseas_exit_target  # type: ignore[method-assign]
    service._place_overseas_sell_order = fake_place_overseas_sell_order  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: False
    liquidity_lab_module.get_us_trading_session = lambda now: "daytime"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.primary_market == "overseas"
    assert report.primary_selection_reason == "existing_position_stop_loss"
    assert report.overseas_order["submitted"] is True
    assert sell_calls == ["SMCI"]


def test_run_executes_both_markets_when_both_open() -> None:
    service = _build_run_service()
    domestic_candidate = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=18.0,
    )
    overseas_candidate = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=131.0,
        bid=130.9,
        ask=131.1,
        spread_pct=0.0015,
        change_rate_pct=2.0,
        volume=900_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=19.0,
    )
    watch_targets = [
        WatchTargetStatus("domestic", "005930", None, 82000.0, 18.0, 9.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "NVDA", "NASD", 131.0, 19.0, 10.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
    ]

    async def fake_scan_domestic():
        return [domestic_candidate]

    async def fake_load_domestic_positions(domestic_ranked):
        return []

    async def fake_scan_overseas():
        return [overseas_candidate], set()

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return []

    async def fake_build_unified_watch_targets(**kwargs):
        return watch_targets

    async def fake_place_domestic_test_order(candidate):
        return {"submitted": True, "side": "buy", "candidate": {"stock_code": candidate.stock_code}, "qty": 1}

    async def fake_manage_overseas_position(*, candidate, held_positions):
        return {"submitted": True, "side": "buy", "candidate": {"symbol": candidate.symbol}, "qty": 1}

    async def fake_send_summary(report):
        return None

    service.scan_domestic = fake_scan_domestic  # type: ignore[method-assign]
    service._load_domestic_positions = fake_load_domestic_positions  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._place_domestic_test_order = fake_place_domestic_test_order  # type: ignore[method-assign]
    service._manage_overseas_position = fake_manage_overseas_position  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: True
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: True
    liquidity_lab_module.get_us_trading_session = lambda now: "regular"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.primary_market == "both"
    assert report.primary_selection_reason == "dual_market_active"
    assert report.domestic_order["submitted"] is True
    assert report.overseas_order["submitted"] is True


def test_run_executes_domestic_buy_for_multiple_targets() -> None:
    service = _build_run_service()
    first = DomesticScanResult(
        stock_code="005930",
        current_price=82000,
        best_ask=82050,
        best_bid=81950,
        spread_pct=0.0012,
        minute_change_pct=0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=18.0,
    )
    second = DomesticScanResult(
        stock_code="000660",
        current_price=210000,
        best_ask=210500,
        best_bid=209500,
        spread_pct=0.0015,
        minute_change_pct=0.004,
        intraday_turnover_krw=90_000_000_000,
        volume_sum=420_000,
        activity_score=17.0,
    )
    watch_targets = [
        WatchTargetStatus("domestic", "005930", None, 82000.0, 18.0, 9.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("domestic", "000660", None, 210000.0, 17.0, 8.0, "BUY", "BUY_READY", "20d>60d 5>20", "volume_breakout_entry", 0),
    ]
    order_calls: list[str] = []

    async def fake_scan_domestic():
        return [first, second]

    async def fake_load_domestic_positions(domestic_ranked):
        return []

    async def fake_build_unified_watch_targets(**kwargs):
        return watch_targets

    async def fake_place_domestic_test_order(candidate):
        order_calls.append(candidate.stock_code)
        return {
            "submitted": True,
            "side": "buy",
            "candidate": {"stock_code": candidate.stock_code},
            "qty": 1,
        }

    async def fake_send_summary(report):
        return None

    service.scan_domestic = fake_scan_domestic  # type: ignore[method-assign]
    service._load_domestic_positions = fake_load_domestic_positions  # type: ignore[method-assign]
    async def fake_scan_overseas():
        return [], set()

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return []

    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._place_domestic_test_order = fake_place_domestic_test_order  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: True
    liquidity_lab_module.is_us_regular_session = lambda now: False
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: False
    liquidity_lab_module.get_us_trading_session = lambda now: "closed"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert order_calls == ["005930", "000660"]
    assert report.domestic_order["candidate"]["stock_code"] == "005930"
    assert len(report.domestic_order["batched_orders"]) == 2


def test_run_executes_overseas_when_only_us_open() -> None:
    service = _build_run_service()
    overseas_candidate = OverseasScanResult(
        symbol="AMD",
        exchange_code="NASD",
        last_price=155.0,
        bid=154.9,
        ask=155.1,
        spread_pct=0.0012,
        change_rate_pct=1.5,
        volume=800_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=17.0,
    )
    watch_target = WatchTargetStatus(
        market="overseas",
        code="AMD",
        exchange_code="NASD",
        price=155.0,
        activity_score=17.0,
        signal_score=8.0,
        action_bias="BUY",
        signal_state="BUY_READY",
        ma_summary="20d>60d 5>20",
        note="pullback_entry",
        holding_qty=0,
    )

    async def fake_scan_overseas():
        return [overseas_candidate], set()

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return []

    async def fake_build_unified_watch_targets(**kwargs):
        return [watch_target]

    async def fake_manage_overseas_position(*, candidate, held_positions):
        return {"submitted": True, "side": "buy", "candidate": {"symbol": candidate.symbol}, "qty": 1}

    async def fake_send_summary(report):
        return None

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._manage_overseas_position = fake_manage_overseas_position  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: True
    liquidity_lab_module.get_us_trading_session = lambda now: "regular"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.primary_market == "overseas"
    assert report.primary_selection_reason == "watchlist_buy_signal"
    assert report.domestic_order["skipped"] is True
    assert report.overseas_order["submitted"] is True


def test_send_summary_skips_when_action_raw_is_wait() -> None:
    service = _build_run_service()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "WAIT",
        "action": "대기",
        "price": "-",
        "qty": "-",
        "indicator": "-",
        "reason": "watchlist_wait",
    }
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:00:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="daytime",
        us_orderable_in_profile=False,
        primary_market="overseas",
        primary_target="SMCI",
        primary_selection_reason="watchlist_wait",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_send_summary_skips_when_overseas_sell_already_notified() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:00:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="regular",
        us_orderable_in_profile=True,
        primary_market="overseas",
        primary_target="NVDA",
        primary_selection_reason="existing_position_stop_loss",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order={
            "submitted": True,
            "already_notified": True,
            "side": "sell",
            "candidate": {"symbol": "NVDA", "last_price": 196.96},
            "qty": 1,
            "exit_reason": "stop_loss",
        },
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_send_summary_still_sends_when_overseas_buy_not_pre_notified() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:00:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="regular",
        us_orderable_in_profile=True,
        primary_market="overseas",
        primary_target="SMCI",
        primary_selection_reason="watchlist_buy_signal",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order={
            "submitted": True,
            "side": "buy",
            "candidate": {"symbol": "SMCI", "last_price": 41.0},
            "qty": 1,
            "reason": "volume_breakout_entry",
        },
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "종목=SMCI" in service.notifier.messages[0]
    assert "동작=매수" in service.notifier.messages[0]


def test_send_summary_skips_when_domestic_buy_already_notified() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 10:00:00 KST",
        krx_market_open=True,
        us_market_open=False,
        us_market_session="closed",
        us_orderable_in_profile=False,
        primary_market="domestic",
        primary_target="005930",
        primary_selection_reason="watchlist_buy_signal",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order={
            "submitted": True,
            "already_notified": True,
            "side": "buy",
            "candidate": {"stock_code": "005930", "current_price": 82000},
            "qty": 1,
            "reason": "volume_breakout_entry",
        },
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_send_summary_skips_when_domestic_sell_already_notified() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 10:05:00 KST",
        krx_market_open=True,
        us_market_open=False,
        us_market_session="closed",
        us_orderable_in_profile=False,
        primary_market="domestic",
        primary_target="005930",
        primary_selection_reason="existing_position_stop_loss",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order={
            "submitted": True,
            "already_notified": True,
            "side": "sell",
            "candidate": {"stock_code": "005930", "current_price": 82000},
            "held_position": {"quantity": 1, "avg_price": 80000, "pnl_pct": 0.025},
            "qty": 1,
            "exit_reason": "stop_loss",
        },
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_send_summary_real_overseas_buy_without_pre_notification_still_sends_once() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 22:05:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="regular",
        us_orderable_in_profile=True,
        primary_market="overseas",
        primary_target="INTC",
        primary_selection_reason="watchlist_buy_signal",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order={
            "submitted": True,
            "side": "buy",
            "candidate": {"symbol": "INTC", "last_price": 35.25},
            "qty": 1,
            "reason": "pullback_entry",
        },
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "종목=INTC" in service.notifier.messages[0]
    assert "동작=매수" in service.notifier.messages[0]


def test_format_order_summary_sell_rejected_returns_sell_rejected_action() -> None:
    service = _build_sell_service()

    summary = service._format_order_summary(
        {
            "submitted": False,
            "skipped": True,
            "side": "sell",
            "candidate": {"last_price": 196.96},
            "qty": 1,
            "reason": "session_not_orderable_in_profile",
            "error": "KIS mock does not support US daytime trading for this session",
            "exit_reason": "partial_profit_lock",
        },
        currency="USD",
    )

    assert summary["action_raw"] == "SELL_REJECTED"
    assert summary["action"] == "매도거부"


def test_send_summary_sends_message_for_sell_rejected() -> None:
    service = _build_run_service()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "SELL_REJECTED",
        "action": "매도거부",
        "price": "$196.9600",
        "qty": "1",
        "indicator": "손익 +1.81%",
        "reason": "현재 계정에서 거래 불가한 세션",
    }
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:37:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="daytime",
        us_orderable_in_profile=False,
        primary_market="overseas",
        primary_target="NVDA",
        primary_selection_reason="existing_position_take_profit",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "동작=매도거부" in service.notifier.messages[0]
    assert "참고=주문이 거부되어 실제로 체결되지 않았습니다" in service.notifier.messages[0]


def test_overseas_buy_stays_skipped_when_market_closed() -> None:
    service = _build_run_service()
    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: False
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: False
    liquidity_lab_module.get_us_trading_session = lambda now: "closed"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.primary_selection_reason == "no_supported_market_open"
    assert report.overseas_order["skipped"] is True
    assert service.virtual_trades.list_positions("overseas") == []


def test_virtual_overseas_sell_uses_existing_virtual_position() -> None:
    service = _build_sell_service()
    service.virtual_trades.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=21.0,
        bid=20.95,
        ask=21.05,
        spread_pct=0.0001,
        change_rate_pct=0.9,
        volume=2_000_000,
        orderable_qty=0,
        fx_rate_krw=1350.0,
        activity_score=12.0,
    )
    held = OverseasHeldPosition(
        symbol="SOXL",
        exchange_code="AMEX",
        quantity=1,
        orderable_qty=1,
        avg_price=20.0,
        current_price=21.0,
        pnl_pct=0.05,
        is_virtual=True,
    )

    result = asyncio.run(service._place_overseas_sell_order(candidate, held, "take_profit"))

    assert result["submitted"] is True
    assert result["virtual"] is True
    assert service.virtual_trades.get_position("overseas", "SOXL") is None
    assert any("구분=매도 (virtual)" in message for message in service.notifier.messages)


def test_virtual_buy_does_not_touch_real_broker_balance() -> None:
    class BalanceForbiddenClient:
        async def get_overseas_balance(self, *args, **kwargs):
            raise AssertionError("real broker balance should not be called")

    service = _build_run_service()
    service.client = BalanceForbiddenClient()
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=20.0,
        bid=19.99,
        ask=20.01,
        spread_pct=0.0001,
        change_rate_pct=1.2,
        volume=2_000_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=15.0,
    )

    result = asyncio.run(service._record_virtual_overseas_buy(candidate))

    assert result["submitted"] is True
    assert result["virtual"] is True
    assert service.virtual_trades.get_position("overseas", "SOXL") is not None


def test_overseas_buy_uses_slot_sizing_when_balance_is_available() -> None:
    class DummyOverseasSlotClient:
        async def get_overseas_possible_order(self, *, symbol: str, exchange_code: str, price: str):
            return {
                "cash_available": "1000",
                "raw": {
                    "ord_psbl_frcr_amt_wcrc": "1000",
                },
            }

        async def place_overseas_order_for_current_session(
            self,
            *,
            side: str,
            symbol: str,
            exchange_code: str,
            qty: int,
            price: str,
            order_division: str,
        ):
            return {
                "side": side,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "qty": qty,
                "price": price,
                "order_division": order_division,
            }

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(dry_run=False),
        liquidity_lab=SimpleNamespace(
            overseas_test_order_qty=1,
            use_slot_sizing=True,
            slot_entry_pct=0.10,
            slot_max_pct=0.20,
        ),
    )
    service.client = DummyOverseasSlotClient()
    service.notifier = DummyNotifier()
    service._signal_cache = {"SOXL": _snapshot(price=25.0)}
    service._should_buy_overseas_candidate = (
        lambda snapshot, symbol="": (True, "volume_breakout_entry")
    )  # type: ignore[method-assign]
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=25.0,
        bid=24.99,
        ask=25.01,
        spread_pct=0.0008,
        change_rate_pct=1.0,
        volume=1_500_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=16.0,
    )

    result = asyncio.run(service._place_overseas_test_order(candidate))

    assert result["submitted"] is True
    assert result["qty"] == 4


def test_overseas_buy_saves_buy_real_cycle_log() -> None:
    class DummyOverseasSlotClient:
        async def get_overseas_possible_order(self, *, symbol: str, exchange_code: str, price: str):
            return {
                "cash_available": "1000",
                "raw": {
                    "ord_psbl_frcr_amt_wcrc": "1000",
                },
            }

        async def place_overseas_order_for_current_session(
            self,
            *,
            side: str,
            symbol: str,
            exchange_code: str,
            qty: int,
            price: str,
            order_division: str,
        ):
            return {
                "side": side,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "qty": qty,
                "price": price,
                "order_division": order_division,
            }

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(dry_run=False),
        liquidity_lab=SimpleNamespace(
            overseas_test_order_qty=1,
            use_slot_sizing=True,
            slot_entry_pct=0.10,
            slot_max_pct=0.20,
        ),
    )
    service.client = DummyOverseasSlotClient()
    service.repository = _build_repository()
    service.notifier = DummyNotifier()
    service._signal_cache = {"SOXL": _snapshot(price=25.0)}
    service._session_id = "sess-overseas-buy"
    service._should_buy_overseas_candidate = (
        lambda snapshot, symbol="": (True, "volume_breakout_entry")
    )  # type: ignore[method-assign]
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=25.0,
        bid=24.99,
        ask=25.01,
        spread_pct=0.0008,
        change_rate_pct=1.0,
        volume=1_500_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=16.0,
    )

    result = asyncio.run(service._place_overseas_test_order(candidate))
    rows = service.repository.query_cycle_log(action_bias="BUY_REAL", limit=5)

    assert result["submitted"] is True
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SOXL"
    assert rows[0]["session_id"] == "sess-overseas-buy"
    assert rows[0]["action_reason"] == "volume_breakout_entry"


def test_virtual_overseas_buy_uses_slot_sizing_when_balance_is_available() -> None:
    class DummyVirtualSlotClient:
        async def get_overseas_possible_order(self, *, symbol: str, exchange_code: str, price: str):
            return {
                "cash_available": "1000",
                "raw": {
                    "ord_psbl_frcr_amt_wcrc": "1000",
                },
            }

    service = _build_run_service()
    service.config.liquidity_lab.use_slot_sizing = True
    service.client = DummyVirtualSlotClient()
    candidate = OverseasScanResult(
        symbol="SOXL",
        exchange_code="AMEX",
        last_price=25.0,
        bid=24.99,
        ask=25.01,
        spread_pct=0.0008,
        change_rate_pct=1.0,
        volume=1_500_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=16.0,
    )

    result = asyncio.run(service._record_virtual_overseas_buy(candidate))

    assert result["submitted"] is True
    assert result["qty"] == 4
    assert service.virtual_trades.get_position("overseas", "SOXL") is not None


def test_send_summary_skips_virtual_trade_messages() -> None:
    service = _build_run_service()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "VIRTUAL_BUY",
        "action": "매수",
        "price": "$41.0000",
        "qty": "1",
        "indicator": "RSI 61.0",
        "reason": "거래불가 세션 가상체결",
    }
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:37:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="daytime",
        us_orderable_in_profile=False,
        primary_market="overseas",
        primary_target="SMCI",
        primary_selection_reason="watchlist_wait",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        paper_run=None,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_repeated_cycles_do_not_duplicate_virtual_sell() -> None:
    service = _build_run_service()
    service.client = DummySellClient(
        error=KisApiError("KIS mock does not support US daytime trading for this session")
    )
    candidate = OverseasScanResult(
        symbol="NVDA",
        exchange_code="NASD",
        last_price=196.96,
        bid=196.95,
        ask=196.97,
        spread_pct=0.0001,
        change_rate_pct=0.9,
        volume=2_000_000,
        orderable_qty=0,
        fx_rate_krw=1350.0,
        activity_score=12.0,
    )
    held = OverseasHeldPosition(
        symbol="NVDA",
        exchange_code="NASD",
        quantity=1,
        orderable_qty=1,
        avg_price=193.46,
        current_price=196.96,
        pnl_pct=0.0181,
    )

    async def fake_scan_overseas():
        return [candidate], {"NVDA"}

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return [
            OverseasHeldPosition(
                symbol=held.symbol,
                exchange_code=held.exchange_code,
                quantity=held.quantity,
                orderable_qty=held.orderable_qty,
                avg_price=held.avg_price,
                current_price=held.current_price,
                pnl_pct=held.pnl_pct,
            )
        ]

    async def fake_build_unified_watch_targets(**kwargs):
        return []

    async def fake_send_summary(report):
        return None

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._send_summary = fake_send_summary  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: False
    liquidity_lab_module.get_us_trading_session = lambda now: "daytime"
    try:
        first = asyncio.run(service.run())
        second = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert first.overseas_order["submitted"] is True
    assert first.overseas_order["virtual"] is True
    assert second.overseas_order["skipped"] is True
    assert second.overseas_order["reason"] == "us_open_but_mock_session_not_supported"
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is not None
    assert int(pending["qty"]) == 1
    sell_orders = [
        row
        for row in service.repository.list_virtual_orders(limit=20)
        if row["symbol"] == "NVDA" and row["side"] == "sell"
    ]
    assert len(sell_orders) == 1


def test_full_cycle_sends_exactly_one_notification_per_real_sell_trade() -> None:
    service = _build_run_service()
    candidate = OverseasScanResult(
        symbol="AMD",
        exchange_code="NASD",
        last_price=155.5,
        bid=155.4,
        ask=155.6,
        spread_pct=0.0013,
        change_rate_pct=-1.2,
        volume=1_200_000,
        orderable_qty=10,
        fx_rate_krw=1350.0,
        activity_score=15.0,
    )
    held = OverseasHeldPosition(
        symbol="AMD",
        exchange_code="NASD",
        quantity=1,
        orderable_qty=1,
        avg_price=160.0,
        current_price=155.5,
        pnl_pct=-0.0281,
    )

    async def fake_scan_overseas():
        return [candidate], {"AMD"}

    async def fake_load_overseas_positions(overseas_ranked, held_symbols_cache=None):
        return [held]

    async def fake_build_unified_watch_targets(**kwargs):
        return []

    async def fake_select_overseas_exit_target(overseas_ranked, overseas_positions):
        return candidate, held, "stop_loss", None

    async def fake_place_overseas_sell_order(candidate, held, exit_reason, signal_snapshot=None):
        await service.notifier.send(
            "\n".join(
                [
                    "[KIS][LAB_SELL]",
                    "시각=6월 30일 22:30",
                    "시장=해외",
                    f"종목={candidate.symbol}",
                    "구분=매도",
                    f"가격=${candidate.last_price:.4f}",
                    "수량=1주",
                    "사유=손절",
                ]
            )
        )
        return {
            "submitted": True,
            "already_notified": True,
            "market": "overseas",
            "side": "sell",
            "candidate": {"symbol": candidate.symbol, "last_price": candidate.last_price},
            "held_position": {"quantity": held.quantity, "avg_price": held.avg_price, "pnl_pct": held.pnl_pct},
            "qty": 1,
            "exit_reason": exit_reason,
        }

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._select_overseas_exit_target = fake_select_overseas_exit_target  # type: ignore[method-assign]
    service._place_overseas_sell_order = fake_place_overseas_sell_order  # type: ignore[method-assign]

    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    liquidity_lab_module.is_krx_regular_session = lambda now: False
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: True
    liquidity_lab_module.get_us_trading_session = lambda now: "regular"
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session

    assert report.overseas_order["submitted"] is True
    assert len(service.notifier.messages) == 1
    assert service.notifier.messages[0].startswith("[KIS][LAB_SELL]")
