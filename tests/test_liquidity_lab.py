import asyncio
from types import SimpleNamespace

import kinvest_trade.liquidity_lab as liquidity_lab_module
from kinvest_trade.liquidity_lab import (
    DomesticHeldPosition,
    DomesticScanResult,
    LiquidityLabService,
    LiquidityLabReport,
    OverseasHeldPosition,
    OverseasScanResult,
    WatchTargetStatus,
)
from kinvest_trade.client import KisApiError


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
    ]


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
    service.notifier = DummyNotifier()
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


def test_overseas_sell_rejected_marks_skipped_true() -> None:
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

    assert result["submitted"] is False
    assert result["skipped"] is True
    assert result["reason"] == "session_not_orderable_in_profile"


def test_overseas_sell_rejected_does_not_send_lab_sell_notification() -> None:
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

    assert result["submitted"] is False
    assert service.notifier.messages == []


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
    service.notifier = DummyNotifier()
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
    assert "손익=+4,000원" in message
    assert "수익률=+2.50%" in message


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
        liquidity_lab=SimpleNamespace(domestic_test_order_qty=1),
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


def _build_run_service() -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        credentials=SimpleNamespace(env="vps", dry_run=False),
        liquidity_lab=SimpleNamespace(
            domestic_top_n=3,
            domestic_candidates=[],
            overseas_candidates=[],
            overseas_scan_top_n=3,
        ),
        auto_trade=SimpleNamespace(
            daily_fast_window=20,
            daily_slow_window=60,
            intraday_fast_window=5,
            intraday_slow_window=20,
        ),
    )
    service.client = object()
    service.repository = object()
    service.notifier = DummyNotifier()
    service._domestic_excluded = []
    service._overseas_excluded = []
    service._last_held_symbols = set()
    service._signal_cache = {}
    return service


def test_overseas_buy_not_attempted_when_session_not_orderable() -> None:
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

    async def fake_build_overseas_watch_targets(overseas_ranked, overseas_positions):
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
    service._build_domestic_watch_targets = lambda domestic_ranked, held_positions: []  # type: ignore[method-assign]
    service._build_overseas_watch_targets = fake_build_overseas_watch_targets  # type: ignore[method-assign]
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
    assert report.primary_selection_reason == "watchlist_wait"
    assert report.overseas_order["skipped"] is True
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

    async def fake_build_overseas_watch_targets(overseas_ranked, overseas_positions):
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
    service._build_domestic_watch_targets = lambda domestic_ranked, held_positions: []  # type: ignore[method-assign]
    service._build_overseas_watch_targets = fake_build_overseas_watch_targets  # type: ignore[method-assign]
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


def test_send_summary_sends_when_action_raw_is_buy() -> None:
    service = _build_run_service()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "BUY",
        "action": "매수",
        "price": "$41.0000",
        "qty": "1",
        "indicator": "RSI 61.0, 거래량 2.5x",
        "reason": "거래량 돌파 진입",
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

    assert len(service.notifier.messages) == 1
    assert "시장=해외 (거래불가 세션)" in service.notifier.messages[0]


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
