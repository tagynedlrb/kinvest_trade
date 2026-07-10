import asyncio
import json
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
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
    VirtualPosition,
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


def test_overseas_speculative_reasons_exclude_structured_symbols() -> None:
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

    unit = OverseasScanResult("CXIIU", "NASD", 10.0, 9.99, 10.01, 0.001, 1.0, 800_000, 0, 0.0, 1.0)
    warrant = OverseasScanResult("ABCDW", "NASD", 10.0, 9.99, 10.01, 0.001, 1.0, 800_000, 0, 0.0, 1.0)
    regular = OverseasScanResult("BIDU", "NASD", 100.0, 99.9, 100.1, 0.001, 1.0, 800_000, 0, 0.0, 1.0)

    assert "structured_unit_symbol" in service._overseas_speculative_reasons(unit)
    assert "structured_warrant_or_right_symbol" in service._overseas_speculative_reasons(warrant)
    assert service._overseas_speculative_reasons(regular) == []


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


def test_select_overseas_exit_target_works_with_empty_ranked_list() -> None:
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
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service._signal_cache = {}
    held_positions = [
        OverseasHeldPosition(
            symbol="PFE",
            exchange_code="NYSE",
            quantity=3,
            orderable_qty=3,
            avg_price=26.0,
            current_price=25.3,
            pnl_pct=-0.0269,
        )
    ]

    candidate, held, reason, signal_snapshot = asyncio.run(
        service._select_overseas_exit_target([], held_positions)
    )

    assert candidate.symbol == "PFE"
    assert held.symbol == "PFE"
    assert reason == "stop_loss"
    assert signal_snapshot is None


def test_overseas_exit_price_shock_requires_confirmation(tmp_path) -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.025,
                    "overseas_stop_loss_pct": 0.015,
                    "overseas_exit_price_shock_pct": 0.20,
                    "overseas_exit_price_shock_confirm_pct": 0.02,
                    "overseas_exit_mid_mismatch_pct": 0.03,
                },
            )()
        },
    )()
    service.repository = SqliteRepository(tmp_path / "price_shock.db")
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service._signal_cache = {}
    service._exit_price_shock_guard = {}
    service._cycle_count = 1
    service._session_id = "test"
    service._exit_cooldown = {}
    service._dynamic_overseas_pool = []
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="PLBL",
        exchange_code="NASD",
        action_bias="SELL",
        signal_state="SELL_READY",
        note="time_exit_profit",
        holding_qty=2027,
        last_price=10.01,
        pnl_pct=0.0,
        strategy_flag="VWAP+RSI",
        entry_by="VWAP",
        has_position=1,
    )
    ranked = [
        OverseasScanResult(
            symbol="PLBL",
            exchange_code="NASD",
            last_price=6.42,
            bid=6.41,
            ask=6.43,
            spread_pct=0.0031,
            change_rate_pct=-35.0,
            volume=100_000,
            orderable_qty=2027,
            fx_rate_krw=1350.0,
            activity_score=5.0,
        )
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="PLBL",
            exchange_code="NASD",
            quantity=2027,
            orderable_qty=2027,
            avg_price=10.01,
            current_price=6.42,
            pnl_pct=(6.42 - 10.01) / 10.01,
        )
    ]

    first = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert first == []
    with service.repository._connect() as conn:
        skip_row = conn.execute(
            "SELECT action_bias, action_reason, is_session_trade FROM cycle_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert skip_row["action_bias"] == "SKIP"
    assert "sell:price_shock_confirm" in skip_row["action_reason"]
    assert skip_row["is_session_trade"] == 0

    second = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert len(second) == 1
    assert second[0][0].symbol == "PLBL"
    assert second[0][2] == "stop_loss"


def test_select_overseas_exit_targets_returns_multiple_candidates() -> None:
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
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service._signal_cache = {}
    ranked = [
        OverseasScanResult(
            symbol="PFE",
            exchange_code="NYSE",
            last_price=25.3,
            bid=25.29,
            ask=25.31,
            spread_pct=0.0008,
            change_rate_pct=-1.0,
            volume=500_000,
            orderable_qty=3,
            fx_rate_krw=1350.0,
            activity_score=10.0,
        ),
        OverseasScanResult(
            symbol="AAL",
            exchange_code="NASD",
            last_price=14.5,
            bid=14.49,
            ask=14.51,
            spread_pct=0.0010,
            change_rate_pct=2.0,
            volume=600_000,
            orderable_qty=2,
            fx_rate_krw=1350.0,
            activity_score=9.0,
        ),
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="PFE",
            exchange_code="NYSE",
            quantity=3,
            orderable_qty=3,
            avg_price=26.0,
            current_price=25.3,
            pnl_pct=-0.0269,
        ),
        OverseasHeldPosition(
            symbol="AAL",
            exchange_code="NASD",
            quantity=2,
            orderable_qty=2,
            avg_price=14.0,
            current_price=14.5,
            pnl_pct=0.0357,
        ),
    ]

    results = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert [item[0].symbol for item in results] == ["PFE", "AAL"]
    assert [item[2] for item in results] == ["stop_loss", "take_profit"]


def test_select_overseas_exit_targets_includes_virtual_only_stop_loss() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.025,
                    "overseas_stop_loss_pct": 0.015,
                },
            )()
        },
    )()
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service._signal_cache = {}
    service.virtual_trades = type(
        "VirtualTrades",
        (),
        {
            "list_positions": lambda self, market=None: [
                VirtualPosition(
                    market="overseas",
                    symbol="SOLS",
                    exchange_code="NASD",
                    qty=441,
                    avg_price=68.70,
                    currency="USD",
                )
            ],
            "get_position": lambda self, market, symbol: VirtualPosition(
                market="overseas",
                symbol="SOLS",
                exchange_code="NASD",
                qty=441,
                avg_price=68.70,
                currency="USD",
            ) if symbol.upper() == "SOLS" else None,
        },
    )()
    ranked = [
        OverseasScanResult(
            symbol="SOLS",
            exchange_code="NASD",
            last_price=61.75,
            bid=61.70,
            ask=61.80,
            spread_pct=0.0016,
            change_rate_pct=-5.0,
            volume=900_000,
            orderable_qty=0,
            fx_rate_krw=1350.0,
            activity_score=8.0,
        )
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="SOLS",
            exchange_code="NASD",
            quantity=441,
            orderable_qty=441,
            avg_price=68.70,
            current_price=61.75,
            pnl_pct=(61.75 - 68.70) / 68.70,
            is_virtual=True,
        )
    ]

    results = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert len(results) == 1
    candidate, held, reason, signal_snapshot = results[0]
    assert candidate.symbol == "SOLS"
    assert held.is_virtual is True
    assert held.orderable_qty == 441
    assert reason == "stop_loss"
    assert signal_snapshot is None


def test_select_overseas_exit_targets_uses_held_qty_when_orderable_is_zero() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.025,
                    "overseas_stop_loss_pct": 0.015,
                },
            )()
        },
    )()
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service._signal_cache = {}
    service._defer_no_orderable_position = lambda **kwargs: None  # type: ignore[method-assign]
    service._clear_no_orderable_retry = lambda *args, **kwargs: None  # type: ignore[method-assign]
    ranked = [
        OverseasScanResult(
            symbol="PCAP",
            exchange_code="NASD",
            last_price=9.5,
            bid=9.49,
            ask=9.51,
            spread_pct=0.0010,
            change_rate_pct=-3.0,
            volume=600_000,
            orderable_qty=0,
            fx_rate_krw=1350.0,
            activity_score=8.0,
        )
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="PCAP",
            exchange_code="NASD",
            quantity=2763,
            orderable_qty=0,
            avg_price=10.0,
            current_price=9.5,
            pnl_pct=-0.05,
        )
    ]

    results = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert len(results) == 1
    candidate, held, reason, signal_snapshot = results[0]
    assert candidate.symbol == "PCAP"
    assert held.orderable_qty == 2763
    assert reason == "stop_loss"
    assert signal_snapshot is None


def test_select_overseas_exit_targets_skips_during_no_orderable_retry_window() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.025,
                    "overseas_stop_loss_pct": 0.015,
                },
            )()
        },
    )()
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service._signal_cache = {}
    service._no_orderable_retry = {
        "overseas:ALNY": datetime.now(timezone.utc) + timedelta(minutes=5)
    }
    ranked = [
        OverseasScanResult(
            symbol="ALNY",
            exchange_code="NASD",
            last_price=317.0,
            bid=316.9,
            ask=317.1,
            spread_pct=0.0006,
            change_rate_pct=-2.0,
            volume=1_500_000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=9.0,
        )
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="ALNY",
            exchange_code="NASD",
            quantity=61,
            orderable_qty=0,
            avg_price=338.41,
            current_price=317.0,
            pnl_pct=(317.0 - 338.41) / 338.41,
        )
    ]

    results = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert results == []


def test_select_overseas_exit_targets_skips_during_exit_cooldown_when_orderable_is_zero() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "liquidity_lab": type(
                "LiquidityCfg",
                (),
                {
                    "overseas_take_profit_pct": 0.025,
                    "overseas_stop_loss_pct": 0.015,
                    "loop_interval_sec": 25,
                },
            )()
        },
    )()
    service._get_position_tracker = lambda: None  # type: ignore[method-assign]
    service.virtual_trades = None
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    service._no_orderable_retry = {}
    service._exit_cooldown = {
        "overseas:ALNY": datetime.now(timezone.utc) + timedelta(minutes=20)
    }
    ranked = [
        OverseasScanResult(
            symbol="ALNY",
            exchange_code="NASD",
            last_price=317.0,
            bid=316.9,
            ask=317.1,
            spread_pct=0.0006,
            change_rate_pct=-2.0,
            volume=1_500_000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=9.0,
        )
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="ALNY",
            exchange_code="NASD",
            quantity=61,
            orderable_qty=0,
            avg_price=338.41,
            current_price=317.0,
            pnl_pct=(317.0 - 338.41) / 338.41,
        )
    ]

    results = asyncio.run(service._select_overseas_exit_targets(ranked, held_positions, max_exits=5))

    assert results == []


def test_track_no_orderable_stall_sends_alert_on_30th_cycle() -> None:
    async def run() -> None:
        service = LiquidityLabService.__new__(LiquidityLabService)
        service.config = SimpleNamespace(
            liquidity_lab=SimpleNamespace(loop_interval_sec=25)
        )
        service.notifier = DummyNotifier()
        for _ in range(29):
            service._track_no_orderable_stall(
                market="overseas",
                symbol="BBIO",
                holding_qty=522,
            )
        assert service.notifier.messages == []
        service._track_no_orderable_stall(
            market="overseas",
            symbol="BBIO",
            holding_qty=522,
        )
        await asyncio.sleep(0)
        assert len(service.notifier.messages) == 1
        assert "종목=BBIO" in service.notifier.messages[0]
        assert "보유=522주" in service.notifier.messages[0]

    asyncio.run(run())


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
    def __init__(
        self,
        *,
        error: Exception | None = None,
        pending_orders: list[dict] | None = None,
    ) -> None:
        self.error = error
        self.pending_orders = pending_orders or []
        self.order_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

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
        payload = {
            "side": side,
            "symbol": symbol,
            "exchange_code": exchange_code,
            "qty": qty,
            "price": price,
            "order_division": order_division,
        }
        self.order_calls.append(payload)
        return payload

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
        payload = {
            "side": side,
            "stock_code": stock_code,
            "qty": qty,
            "price": price,
            "order_division": order_division,
        }
        self.order_calls.append(payload)
        return payload

    async def get_overseas_order_history(self, **kwargs):
        del kwargs
        return {"orders": list(self.pending_orders)}

    async def revise_or_cancel_overseas_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return {
            "rt_cd": "0",
            "msg_cd": "00000000",
            "msg1": "취소 완료",
            "output": {"ODNO": kwargs.get("original_order_no", "")},
        }


def _build_sell_service(
    *,
    dry_run: bool = False,
    error: Exception | None = None,
    pending_orders: list[dict] | None = None,
) -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "credentials": type("Creds", (), {"dry_run": dry_run, "env": "vps"})(),
        },
    )()
    service.client = DummySellClient(error=error, pending_orders=pending_orders)
    service.repository = _build_repository()
    service.virtual_trades = VirtualTradeManager(service.repository)
    service.position_tracker = UnifiedPositionTracker(service.repository, service.virtual_trades)
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    service._session_id = "sess-test"
    service._pending_trade_notifications = []
    service._pending_trade_notification_started_at = None
    service._trade_notification_window_sec = 0
    service._trade_notification_max_batch_size = 8
    return service


@contextmanager
def _force_overseas_orderable_session():
    original = liquidity_lab_module.is_us_orderable_session_for_env
    liquidity_lab_module.is_us_orderable_session_for_env = lambda *_args: True
    try:
        yield
    finally:
        liquidity_lab_module.is_us_orderable_session_for_env = original


def _run_orderable_overseas_sell(
    service: LiquidityLabService,
    candidate: OverseasScanResult,
    held: OverseasHeldPosition,
    exit_reason: str,
):
    with _force_overseas_orderable_session():
        return asyncio.run(service._place_overseas_sell_order(candidate, held, exit_reason))


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

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is True
    assert len(service.notifier.messages) == 1
    message = service.notifier.messages[0]
    assert message.startswith("[KIS][거래알림]")
    assert "해외 TSLA 매도접수 +$281.90 x2" in message
    assert "매수=-" in message
    assert "청산=긴급 손절" in message
    assert "수익률=+0.68%" in message
    assert service.client.order_calls[0]["price"] == "281.9000"


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

    _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")
    rows = service.repository.query_cycle_log(action_bias="SELL_REAL", limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "TSLA"
    assert rows[0]["session_id"] == "sess-test"
    assert rows[0]["exit_by"] == "atr_hard_stop"
    assert abs(rows[0]["realized_pnl_usd"] - 3.8) < 1e-9
    assert abs(rows[0]["realized_pnl_krw"] - 5244.0) < 1e-6


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

    _run_orderable_overseas_sell(service, candidate, held, "momentum_loss_cut")

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

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is False
    assert service.notifier.messages == []


def test_place_overseas_sell_order_mock_balance_missing_treated_as_no_orderable() -> None:
    service = _build_sell_service(
        error=KisApiError("VTTT1001U error: 40240000 모의투자 잔고내역이 없습니다.")
    )
    candidate = OverseasScanResult(
        symbol="ALNY",
        exchange_code="NASD",
        last_price=317.0,
        bid=316.9,
        ask=317.1,
        spread_pct=0.0006,
        change_rate_pct=-2.0,
        volume=1_500_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=9.0,
    )
    held = OverseasHeldPosition(
        symbol="ALNY",
        exchange_code="NASD",
        quantity=61,
        orderable_qty=61,
        avg_price=338.41,
        current_price=317.0,
        pnl_pct=(317.0 - 338.41) / 338.41,
    )

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is False
    assert result["reason"] == "no_orderable_qty"
    rows = service.repository.query_cycle_log(action_bias="SKIP", limit=5)
    assert rows[0]["action_reason"] == "sell:no_orderable_qty"
    assert rows[0]["is_session_trade"] == 0


def test_place_overseas_sell_order_rejected_adds_20min_cooldown() -> None:
    service = _build_sell_service(
        error=KisApiError("40210000 Both-sided waiting order exists")
    )
    candidate = OverseasScanResult(
        symbol="ALNY",
        exchange_code="NASD",
        last_price=317.0,
        bid=316.9,
        ask=317.1,
        spread_pct=0.0006,
        change_rate_pct=-2.0,
        volume=1_500_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=9.0,
    )
    held = OverseasHeldPosition(
        symbol="ALNY",
        exchange_code="NASD",
        quantity=61,
        orderable_qty=61,
        avg_price=338.41,
        current_price=317.0,
        pnl_pct=(317.0 - 338.41) / 338.41,
    )

    result = _run_orderable_overseas_sell(service, candidate, held, "stop_loss")

    assert result["submitted"] is False
    assert result["reason"] == "order_rejected"
    assert service._cooldown_remaining_minutes("overseas", "ALNY") > 19.0
    events = service.repository.list_event_log(event_type="trade_skip", limit=5)
    assert any("Both-sided waiting order exists" in row["detail"] for row in events)
    broker_rows = service.repository.list_broker_order_events(limit=1)
    assert broker_rows[0]["status"] == "REJECTED"
    assert broker_rows[0]["reason"] == "order_rejected"
    assert "Both-sided waiting order exists" in broker_rows[0]["payload_json"]["error"]


def test_place_domestic_sell_order_rejected_adds_10min_cooldown_and_logs_it() -> None:
    service = _build_sell_service(
        error=KisApiError("40580000 already waiting order exists")
    )
    candidate = DomesticScanResult(
        stock_code="069500",
        current_price=122_400,
        best_ask=122_405,
        best_bid=122_395,
        spread_pct=0.00008,
        minute_change_pct=-0.1,
        intraday_turnover_krw=50_000_000_000,
        volume_sum=300_000,
        activity_score=12.0,
        stock_name="KODEX 200",
    )
    held = DomesticHeldPosition(
        stock_code="069500",
        quantity=7,
        orderable_qty=7,
        avg_price=122_900,
        current_price=122_400,
        pnl_pct=(122_400 - 122_900) / 122_900,
    )

    result = asyncio.run(
        service._place_domestic_sell_order(candidate, held, "trend_filter_lost")
    )

    assert result["submitted"] is False
    assert result["reason"] == "order_rejected"
    assert service._cooldown_remaining_minutes("domestic", "069500") > 9.0
    rows = service.repository.query_cycle_log(action_bias="SKIP", limit=5)
    assert rows[0]["action_reason"] == "sell:order_rejected"
    assert rows[0]["exit_cooldown_remaining"] > 9.0
    events = service.repository.list_event_log(event_type="trade_skip", limit=5)
    assert any("already waiting order exists" in row["detail"] for row in events)
    broker_rows = service.repository.list_broker_order_events(limit=1)
    assert broker_rows[0]["status"] == "REJECTED"
    assert broker_rows[0]["reason"] == "order_rejected"
    assert "already waiting order exists" in broker_rows[0]["payload_json"]["error"]


def test_overseas_sell_session_blocked_does_not_convert_real_to_virtual_trade() -> None:
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
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is None
    assert service.virtual_trades.performance_summary() == {}
    assert service.client.order_calls == []


def test_overseas_sell_session_blocked_sends_no_virtual_trade_notification() -> None:
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
    assert result["reason"] == "session_not_orderable_in_profile"
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

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is True
    assert "매수=-" in service.notifier.messages[0]
    assert "수익률=-" in service.notifier.messages[0]


def test_place_overseas_sell_order_cancels_stale_pending_exit_then_reorders() -> None:
    service = _build_sell_service(
        pending_orders=[
            {
                "pdno": "ALNY",
                "sll_buy_dvsn_cd": "01",
                "nccs_qty": "61",
                "odno": "52821",
                "ft_ord_unpr3": "337.57000000",
                "dmst_ord_dt": "20260710",
                "thco_ord_tmd": "000000",
            }
        ]
    )
    candidate = OverseasScanResult(
        symbol="ALNY",
        exchange_code="NASD",
        last_price=317.0,
        bid=316.9,
        ask=317.1,
        spread_pct=0.0006,
        change_rate_pct=-2.0,
        volume=1_500_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=9.0,
    )
    held = OverseasHeldPosition(
        symbol="ALNY",
        exchange_code="NASD",
        quantity=61,
        orderable_qty=61,
        avg_price=338.41,
        current_price=317.0,
        pnl_pct=(317.0 - 338.41) / 338.41,
    )

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is True
    assert len(service.client.cancel_calls) == 1
    assert service.client.cancel_calls[0]["original_order_no"] == "52821"
    assert service.client.order_calls[0]["price"] == "316.9000"


def test_place_overseas_buy_order_cancels_stale_conflicting_sell_order() -> None:
    service = _build_run_service()
    service.client = DummySellClient(
        pending_orders=[
            {
                "pdno": "PLTR",
                "sll_buy_dvsn_cd": "01",
                "nccs_qty": "2",
                "odno": "61001",
                "ft_ord_unpr3": "23.50000000",
                "dmst_ord_dt": "20260710",
                "thco_ord_tmd": "000000",
            }
        ]
    )
    candidate = OverseasScanResult(
        symbol="PLTR",
        exchange_code="NASD",
        last_price=23.10,
        bid=23.09,
        ask=23.11,
        spread_pct=0.0008,
        change_rate_pct=1.1,
        volume=1_200_000,
        orderable_qty=5,
        fx_rate_krw=0.0,
        activity_score=11.0,
    )
    service._signal_cache["PLTR"] = _snapshot(price=23.10)

    result = asyncio.run(service._place_overseas_test_order(candidate))

    assert result["submitted"] is True
    assert len(service.client.cancel_calls) == 1
    assert service.client.cancel_calls[0]["original_order_no"] == "61001"
    assert service.client.order_calls[0]["price"] == "23.1100"


def test_place_overseas_buy_order_skips_when_recent_pending_buy_exists() -> None:
    service = _build_run_service()
    service.client = DummySellClient(
        pending_orders=[
            {
                "pdno": "PLTR",
                "sll_buy_dvsn_cd": "02",
                "nccs_qty": "3",
                "odno": "60001",
                "ft_ord_unpr3": "23.15000000",
                "dmst_ord_dt": datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).strftime("%Y%m%d"),
                "thco_ord_tmd": datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).strftime("%H%M%S"),
            }
        ]
    )
    candidate = OverseasScanResult(
        symbol="PLTR",
        exchange_code="NASD",
        last_price=23.10,
        bid=23.09,
        ask=23.11,
        spread_pct=0.0008,
        change_rate_pct=1.1,
        volume=1_200_000,
        orderable_qty=5,
        fx_rate_krw=0.0,
        activity_score=11.0,
    )
    service._signal_cache["PLTR"] = _snapshot(price=23.10)

    result = asyncio.run(service._place_overseas_test_order(candidate))

    assert result["skipped"] is True
    assert result["reason"] == "pending_buy_order"


def test_place_overseas_sell_order_cancels_conflicting_buy_order_before_stop_loss() -> None:
    service = _build_sell_service(
        pending_orders=[
            {
                "pdno": "ALNY",
                "sll_buy_dvsn_cd": "02",
                "nccs_qty": "1",
                "odno": "62001",
                "ft_ord_unpr3": "339.00000000",
                "dmst_ord_dt": datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).strftime("%Y%m%d"),
                "thco_ord_tmd": datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).strftime("%H%M%S"),
            }
        ]
    )
    candidate = OverseasScanResult(
        symbol="ALNY",
        exchange_code="NASD",
        last_price=317.0,
        bid=316.9,
        ask=317.1,
        spread_pct=0.0006,
        change_rate_pct=-2.0,
        volume=1_500_000,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=9.0,
    )
    held = OverseasHeldPosition(
        symbol="ALNY",
        exchange_code="NASD",
        quantity=61,
        orderable_qty=61,
        avg_price=338.41,
        current_price=317.0,
        pnl_pct=(317.0 - 338.41) / 338.41,
    )

    result = _run_orderable_overseas_sell(service, candidate, held, "atr_hard_stop")

    assert result["submitted"] is True
    assert len(service.client.cancel_calls) == 1
    assert service.client.cancel_calls[0]["original_order_no"] == "62001"
    assert service.client.order_calls[0]["price"] == "316.9000"


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


class CountingDomesticBalanceClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_balance(self):
        self.calls += 1
        return {"summary": {"ord_psbl_cash": "2500000"}}


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


def test_load_domestic_positions_reads_balance_without_ranked_candidates() -> None:
    class DomesticBalanceOnlyClient:
        async def get_balance(self):
            return {
                "positions": [
                    {
                        "pdno": "058730",
                        "hldg_qty": "1,184",
                        "ord_psbl_qty": "184",
                        "pchs_avg_pric": "5,310",
                        "prpr": "5,030",
                    }
                ]
            }

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.client = DomesticBalanceOnlyClient()
    service._cycle_count = 7

    positions = asyncio.run(service._load_domestic_positions([]))

    assert positions == [
        DomesticHeldPosition(
            stock_code="058730",
            quantity=1184,
            orderable_qty=184,
            avg_price=5310.0,
            current_price=5030.0,
            pnl_pct=(5030.0 - 5310.0) / 5310.0,
        )
    ]
    assert service._domestic_balance_cache["cycle"] == 7


def test_get_domestic_available_krw_uses_cycle_cache() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service._cycle_count = 11
    service._domestic_balance_cache = {}
    service.client = CountingDomesticBalanceClient()

    first = asyncio.run(service._get_domestic_available_krw())
    second = asyncio.run(service._get_domestic_available_krw())

    assert first == 2_500_000
    assert second == 2_500_000
    assert service.client.calls == 1


class DummyDomesticSellClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        pending_orders: list[dict] | None = None,
    ) -> None:
        self.error = error
        self.order_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.pending_orders = list(pending_orders or [])

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
        payload = {
            "side": side,
            "stock_code": stock_code,
            "qty": qty,
            "price": price,
            "order_division": order_division,
        }
        self.order_calls.append(payload)
        return payload

    async def get_domestic_order_history(self, **kwargs):
        del kwargs
        return {"orders": list(self.pending_orders)}

    async def revise_or_cancel_domestic_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return {
            "rt_cd": "0",
            "msg_cd": "00000000",
            "msg1": "취소 완료",
            "output": {"ODNO": kwargs.get("original_order_no", "")},
        }


class DummyOverseasBalanceClient:
    def __init__(self, positions_by_exchange: dict[str, list[dict[str, str]]]) -> None:
        self.positions_by_exchange = positions_by_exchange

    async def get_overseas_balance(self, exchange_code: str, currency_code: str):
        del currency_code
        return {"positions": list(self.positions_by_exchange.get(exchange_code, []))}


class CountingOverseasBalanceClient(DummyOverseasBalanceClient):
    def __init__(self, positions_by_exchange: dict[str, list[dict[str, str]]]) -> None:
        super().__init__(positions_by_exchange)
        self.calls = 0

    async def get_overseas_balance(self, exchange_code: str, currency_code: str):
        self.calls += 1
        return await super().get_overseas_balance(exchange_code, currency_code)


def test_load_overseas_positions_includes_real_holdings_outside_ranked_candidates() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        liquidity_lab=SimpleNamespace(
            overseas_candidates=[
                SimpleNamespace(symbol="SOFI", exchange_code="NASD"),
                SimpleNamespace(symbol="AAL", exchange_code="NASD"),
                SimpleNamespace(symbol="HOOD", exchange_code="NASD"),
            ]
        )
    )
    service.client = DummyOverseasBalanceClient(
        {
            "NASD": [
                {
                    "ovrs_pdno": "SOFI",
                    "ovrs_cblc_qty": "1",
                    "ord_psbl_qty": "1",
                    "pchs_avg_pric": "20.00",
                },
                {
                    "ovrs_pdno": "AAL",
                    "ovrs_cblc_qty": "2",
                    "ord_psbl_qty": "2",
                    "pchs_avg_pric": "11.00",
                },
                {
                    "ovrs_pdno": "HOOD",
                    "ovrs_cblc_qty": "3",
                    "ord_psbl_qty": "3",
                    "pchs_avg_pric": "22.00",
                },
            ]
        }
    )
    overseas_ranked = [
        OverseasScanResult(
            symbol="SOFI",
            exchange_code="NASD",
            last_price=21.0,
            bid=20.9,
            ask=21.1,
            spread_pct=0.001,
            change_rate_pct=1.0,
            volume=500000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=10.0,
        )
    ]

    positions = asyncio.run(service._load_overseas_positions(overseas_ranked))

    assert {position.symbol for position in positions} == {"SOFI", "AAL", "HOOD"}
    fallback = {position.symbol: position.current_price for position in positions}
    assert fallback["SOFI"] == 21.0
    assert fallback["AAL"] == 11.0
    assert fallback["HOOD"] == 22.0


def test_overseas_balance_cache_reused_within_cycle() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service._cycle_count = 7
    service._overseas_balance_cache = {}
    service._last_held_symbols = set()
    service.virtual_trades = VirtualTradeManager(_build_repository())
    service._manual_overseas_pool = None
    service._dynamic_overseas_pool = [{"symbol": "SOFI", "exchange_code": "NASD"}]
    service.client = CountingOverseasBalanceClient(
        {
            "NASD": [
                {
                    "ovrs_pdno": "SOFI",
                    "ovrs_cblc_qty": "1",
                    "ord_psbl_qty": "1",
                    "pchs_avg_pric": "20.00",
                }
            ]
        }
    )
    overseas_ranked = [
        OverseasScanResult(
            symbol="SOFI",
            exchange_code="NASD",
            last_price=21.0,
            bid=20.9,
            ask=21.1,
            spread_pct=0.001,
            change_rate_pct=1.0,
            volume=500000,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=10.0,
        )
    ]

    held_symbols = asyncio.run(service._get_held_symbols())
    positions = asyncio.run(service._load_overseas_positions(overseas_ranked))

    assert held_symbols == {"SOFI"}
    assert {position.symbol for position in positions} == {"SOFI"}
    assert service.client.calls == 1


def _build_domestic_sell_service(
    *,
    dry_run: bool = False,
    error: Exception | None = None,
    pending_orders: list[dict] | None = None,
) -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = type(
        "Config",
        (),
        {
            "credentials": type("Creds", (), {"dry_run": dry_run})(),
        },
    )()
    service.client = DummyDomesticSellClient(error=error, pending_orders=pending_orders)
    service.repository = _build_repository()
    service.virtual_trades = VirtualTradeManager(service.repository)
    service.position_tracker = UnifiedPositionTracker(service.repository, service.virtual_trades)
    service.notifier = DummyNotifier()
    service._signal_cache = {}
    service._session_id = "sess-domestic"
    service._pending_trade_notifications = []
    service._pending_trade_notification_started_at = None
    service._trade_notification_window_sec = 0
    service._trade_notification_max_batch_size = 8
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
    assert message.startswith("[KIS][거래알림]")
    assert "국내 005930 매도접수 +81,950원 x2" in message
    assert "매수=-" in message
    assert "청산=손절" in message
    assert "수익률=+2.44%" in message
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
    assert rows[0]["exit_by"] == "stop_loss"
    assert rows[0]["realized_pnl_krw"] == 3900.0


def test_place_domestic_protective_sell_replaces_stale_pending_exit_when_orderable_zero() -> None:
    pending_orders = [
        {
            "pdno": "058730",
            "prdt_name": "다스코",
            "sll_buy_dvsn_cd": "01",
            "sll_buy_dvsn_cd_name": "매도",
            "rmn_qty": "184",
            "ord_qty": "184",
            "tot_ccld_qty": "0",
            "cncl_cfrm_qty": "0",
            "rjct_qty": "0",
            "odno": "0000015789",
            "ord_gno_brno": "00950",
            "ord_dvsn_cd": "00",
            "excg_id_dvsn_cd": "KRX",
            "ord_unpr": "5710",
            "ord_dt": "20000101",
            "ord_tmd": "100816",
        }
    ]
    service = _build_domestic_sell_service(pending_orders=pending_orders)
    candidate = DomesticScanResult(
        stock_code="058730",
        current_price=4925,
        best_ask=4930,
        best_bid=4925,
        spread_pct=0.001,
        minute_change_pct=-0.015,
        intraday_turnover_krw=5_000_000_000,
        volume_sum=500_000,
        activity_score=18.0,
        stock_name="다스코",
    )
    held = DomesticHeldPosition(
        stock_code="058730",
        quantity=184,
        orderable_qty=0,
        avg_price=5310.0,
        current_price=4925.0,
        pnl_pct=(4925.0 - 5310.0) / 5310.0,
    )

    result = asyncio.run(service._place_domestic_sell_order(candidate, held, "atr_hard_stop"))

    assert result["submitted"] is True
    assert result["replacement_note"] == "미체결 매도 정정 후 재주문"
    assert service.client.cancel_calls == [
        {
            "krx_order_orgno": "00950",
            "original_order_no": "0000015789",
            "order_division": "00",
            "rvse_cncl_dvsn_cd": "02",
            "qty": 0,
            "price": 0,
            "qty_all_order_yn": "Y",
            "exchange_code": "KRX",
        }
    ]
    assert service.client.order_calls == [
        {
            "side": "sell",
            "stock_code": "058730",
            "qty": 184,
            "price": 4925,
            "order_division": "00",
        }
    ]
    broker_rows = service.repository.list_broker_order_events(limit=2)
    assert [row["status"] for row in broker_rows] == ["SUBMITTED", "CANCELED"]
    assert broker_rows[1]["reason"] == "stale_exit_replace"
    assert "참고=미체결 매도 정정 후 재주문" in service.notifier.messages[0]


def test_place_domestic_sell_order_defers_unprofitable_time_exit_profit() -> None:
    service = _build_domestic_sell_service()
    service.config.auto_trade = SimpleNamespace(
        commission_rate=0.0025,
        domestic_commission_rate=0.0025,
        domestic_sell_tax_rate=0.0,
    )
    candidate = DomesticScanResult(
        stock_code="005930",
        current_price=80120,
        best_ask=80130,
        best_bid=80110,
        spread_pct=0.0012,
        minute_change_pct=-0.001,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=11.0,
    )
    held = DomesticHeldPosition(
        stock_code="005930",
        quantity=2,
        orderable_qty=2,
        avg_price=80000.0,
        current_price=80120.0,
        pnl_pct=0.0015,
    )

    result = asyncio.run(service._place_domestic_sell_order(candidate, held, "time_exit_profit"))

    assert result["skipped"] is True
    assert result["reason"] == "net_profit_below_cost"
    assert service.client.order_calls == []
    assert service.notifier.messages == []
    rows = service.repository.query_cycle_log(action_bias="SKIP", limit=5)
    assert rows[0]["action_reason"] == "sell:net_profit_below_cost"


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


def test_domestic_sell_rejected_adds_10min_cooldown() -> None:
    service = _build_domestic_sell_service(error=KisApiError("domestic rejected"))
    candidate = DomesticScanResult(
        stock_code="379800",
        current_price=25600,
        best_ask=25605,
        best_bid=25595,
        spread_pct=0.0004,
        minute_change_pct=-0.003,
        intraday_turnover_krw=100_000_000_000,
        volume_sum=500_000,
        activity_score=124.0,
    )
    held = DomesticHeldPosition(
        stock_code="379800",
        quantity=34,
        orderable_qty=34,
        avg_price=25770.0,
        current_price=25600.0,
        pnl_pct=-0.0066,
    )

    result = asyncio.run(service._place_domestic_sell_order(candidate, held, "time_exit_loss"))

    assert result["reason"] == "order_rejected"
    assert service._cooldown_remaining_minutes("domestic", "379800") > 9.0


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


def test_select_domestic_exit_target_keeps_zero_orderable_positions_for_pending_repair() -> None:
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
            orderable_qty=0,
            avg_price=80000.0,
            current_price=82000.0,
            pnl_pct=-0.01,
        )
    ]

    result = service._select_domestic_exit_target(ranked, watch_targets, held_positions)

    assert result is not None
    candidate, held, exit_reason, _snapshot = result
    assert candidate.stock_code == "005930"
    assert held.orderable_qty == 0
    assert exit_reason == "stop_loss"


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
        skip_holiday_overseas=False,
        skip_holiday_domestic=False,
        risk=SimpleNamespace(
            daily_loss_limit_pct=0.01,
            max_consecutive_losses=3,
            circuit_breaker_cooldown_minutes=30,
            operating_capital_krw=50_000_000,
        ),
        liquidity_lab=SimpleNamespace(
            unified_watch_top_n=3,
            unified_scan_top_n=3,
            domestic_candidates=[],
            overseas_candidates=[],
            overseas_scan_top_n=3,
            max_wait_cycles_before_penalty=15,
            wait_penalty_decay=0.07,
            domestic_dynamic_scan=False,
            domestic_dynamic_top_n=20,
            domestic_dynamic_rescan_cycles=20,
            domestic_dynamic_min_price_krw=5000,
            domestic_dynamic_min_volume=200000,
            vol_surge_threshold_strong=5.0,
            vol_surge_threshold_mild=3.0,
            overseas_relist_schedule_kst="22:35,01:00,03:30",
            overseas_test_order_qty=1,
            overseas_max_position_qty=3,
            overseas_take_profit_pct=0.012,
            overseas_stop_loss_pct=0.008,
            max_concurrent_overseas_orders=20,
            max_concurrent_domestic_orders=8,
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
    service._wait_cycles = {}
    service._exit_cooldown = {}
    service._vol_history = {}
    service._vol_history_maxlen = 12
    service._dynamic_domestic_codes = None
    service._domestic_scan_cycle_count = 0
    service._dynamic_overseas_pool = None
    service._manual_overseas_pool = None
    service._overseas_scan_cycle_count = 0
    service._overseas_relist_schedule = []
    service._last_relist_kst = None
    service._awaiting_relist = False
    service._tv_available = False
    service._consecutive_losses = 0
    service._session_realised_krw = 0.0
    service._daily_halted_at = None
    service._tv_diagnostic_ran = True
    service._last_holiday_notice_key = None
    service._pending_trade_notifications = []
    service._pending_trade_notification_started_at = None
    service._trade_notification_window_sec = 0
    service._trade_notification_max_batch_size = 8
    service._recent_trade_count = 0
    service._recent_cycle_count = 0
    service._rsi_blocked_count = 0
    service._last_trend_filter_alert_cycle = 0
    return service


def test_record_cycle_trade_frequency_saves_low_frequency_event() -> None:
    service = _build_run_service()

    for _ in range(50):
        service._record_cycle_trade_frequency(
            domestic_orders=[{"skipped": True, "reason": "no_action"}],
            overseas_orders=[{"skipped": True, "reason": "no_action"}],
        )

    events = service.repository.list_event_log(event_type="low_trade_frequency", limit=1)
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["cycle_count"] == 50
    assert detail["trade_count"] == 0
    assert detail["ratio"] == 0.0
    assert service._recent_cycle_count == 0


def test_track_rsi_threshold_blocks_counts_rsi_watch_targets() -> None:
    service = _build_run_service()
    service._rsi_blocked_count = 19
    watch_target = WatchTargetStatus(
        market="overseas",
        code="PLTR",
        exchange_code="NYSE",
        price=20.0,
        activity_score=10.0,
        signal_score=5.0,
        action_bias="HOLD",
        signal_state="HOLD",
        ma_summary="watch",
        note="RSI watching",
        signal_snapshot=_snapshot(rsi14=42.0),
        strategy_flag="RSI",
    )

    service._track_rsi_threshold_blocks([watch_target])

    assert service._rsi_blocked_count == 20


def test_check_trend_filter_lost_ratio_saves_warning_event() -> None:
    service = _build_run_service()
    now = datetime.now(timezone.utc).isoformat()
    for idx in range(4):
        service.repository.save_cycle_log(
            logged_at=now,
            market="overseas",
            symbol=f"TFL{idx}",
            exchange_code="NASD",
            action_bias="SELL_REAL",
            action_reason="trend_filter_lost",
            pnl_pct=-0.01,
            qty_executed=1,
        )
    for idx in range(2):
        service.repository.save_cycle_log(
            logged_at=now,
            market="overseas",
            symbol=f"STP{idx}",
            exchange_code="NASD",
            action_bias="SELL_REAL",
            action_reason="stop_loss",
            pnl_pct=-0.01,
            qty_executed=1,
        )
    service._cycle_count = 200

    service._check_trend_filter_lost_ratio()

    events = service.repository.list_event_log(event_type="trend_filter_lost_ratio_high", limit=1)
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["trend_filter_lost"] == 4
    assert detail["total_sell_real"] == 6
    assert detail["ratio"] == 0.6667


def test_active_overseas_pool_includes_held_symbols_without_positions() -> None:
    service = _build_run_service()
    service._dynamic_overseas_pool = [{"symbol": "NVDA", "exchange_code": "NASD"}]

    pool = service._active_overseas_pool(held_symbols={"AAL", "hood"})

    assert sorted((candidate.symbol, candidate.exchange_code) for candidate in pool) == [
        ("AAL", "NASD"),
        ("HOOD", "NASD"),
        ("NVDA", "NASD"),
    ]


def test_active_overseas_pool_uses_held_symbol_exchange_map() -> None:
    service = _build_run_service()
    service._dynamic_overseas_pool = [{"symbol": "NVDA", "exchange_code": "NASD"}]

    pool = service._active_overseas_pool(
        held_symbols={"GM", "hood"},
        held_symbol_map={"GM": "NYSE", "HOOD": "NASD"},
    )

    assert sorted((candidate.symbol, candidate.exchange_code) for candidate in pool) == [
        ("GM", "NYSE"),
        ("HOOD", "NASD"),
        ("NVDA", "NASD"),
    ]


def test_is_trading_halted_when_consecutive_losses_reach_limit() -> None:
    service = _build_run_service()
    service._consecutive_losses = 3

    assert service._is_trading_halted() is True


def test_is_trading_halted_auto_releases_after_cooldown() -> None:
    service = _build_run_service()
    service.notifier = DummyNotifier()
    service._consecutive_losses = 3
    service._halted_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    service.config.risk.circuit_breaker_cooldown_minutes = 30

    assert service._is_trading_halted() is False
    assert service._consecutive_losses == 0
    assert service._halted_at is None


def test_is_trading_halted_still_blocks_when_daily_loss_remains_after_consecutive_release() -> None:
    service = _build_run_service()
    service.notifier = DummyNotifier()
    service._consecutive_losses = 3
    service._halted_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    service._daily_loss_date = datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).date()
    service._session_realised_krw = -600_000.0
    service.config.risk.circuit_breaker_cooldown_minutes = 30

    assert service._is_trading_halted() is True
    assert service._consecutive_losses == 0
    assert service._halted_at is None
    assert service._daily_halted_at is not None


def test_is_trading_halted_when_daily_loss_limit_exceeded() -> None:
    service = _build_run_service()
    service._daily_loss_date = datetime.now(timezone.utc).astimezone(liquidity_lab_module.KST).date()
    service._session_realised_krw = -600_000.0

    assert service._is_trading_halted() is True
    assert service._daily_halted_at is not None


def test_is_trading_halted_daily_limit_auto_releases_after_cooldown() -> None:
    service = _build_run_service()
    service.notifier = DummyNotifier()
    service._session_realised_krw = -600_000.0
    service._daily_halted_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    service.config.risk.circuit_breaker_cooldown_minutes = 30

    assert service._is_trading_halted() is False
    assert service._session_realised_krw == 0.0
    assert service._daily_halted_at is None


def test_is_trading_halted_resets_daily_loss_on_new_kst_day() -> None:
    service = _build_run_service()
    service._daily_loss_date = date(2026, 7, 9)
    service._session_realised_krw = -600_000.0
    service._daily_halted_at = datetime.now(timezone.utc)

    original_datetime = liquidity_lab_module.datetime

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)
            return base if tz is None else base.astimezone(tz)

    liquidity_lab_module.datetime = _FakeDateTime
    try:
        assert service._is_trading_halted() is False
    finally:
        liquidity_lab_module.datetime = original_datetime

    assert service._daily_loss_date == date(2026, 7, 10)
    assert service._session_realised_krw == 0.0
    assert service._daily_halted_at is None


def test_build_watch_target_status_preserves_ready_signal_state() -> None:
    service = _build_run_service()
    snapshot = _snapshot(
        price=20.0,
        vwap=25.0,
        volume_ratio=1.7,
        breakout_distance_pct=-0.01,
        rsi14=52.0,
        macd_golden=False,
        macd_line=0.2,
        macd_signal=0.1,
    )
    original_entry_setup = liquidity_lab_module.evaluate_entry_setup
    original_derive_watch_state = liquidity_lab_module.derive_watch_state

    liquidity_lab_module.evaluate_entry_setup = lambda *args, **kwargs: SimpleNamespace(
        ready=False,
        score=12.5,
        reason="near_breakout",
    )
    liquidity_lab_module.derive_watch_state = lambda *args, **kwargs: (
        "READY",
        "near_breakout",
    )

    try:
        watch_target = service._build_watch_target_status(
            market="overseas",
            code="SOXL",
            exchange_code="AMEX",
            price=20.0,
            activity_score=12.0,
            signal_snapshot=snapshot,
            held_position=None,
            holding_qty=0,
        )
    finally:
        liquidity_lab_module.evaluate_entry_setup = original_entry_setup
        liquidity_lab_module.derive_watch_state = original_derive_watch_state

    assert watch_target.action_bias == "READY"
    assert watch_target.signal_state == "READY"
    assert watch_target.strategy_flag == "VOL+RSI"
    assert watch_target.entry_by == ""


def test_build_watch_target_status_blocks_overseas_standalone_vwap() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    snapshot = _snapshot(price=20.0, vwap=19.9, rsi14=45.0)

    class FakeStrategyManager:
        def evaluate(self, *args, **kwargs):
            return SimpleNamespace(signal="BUY", flag="VWAP", entry_by="VWAP", exit_by="")

    service._get_strategy_manager = lambda code: FakeStrategyManager()  # type: ignore[method-assign]

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="COIN",
        exchange_code="NASD",
        price=20.0,
        activity_score=12.0,
        signal_snapshot=snapshot,
        held_position=None,
        holding_qty=0,
    )

    assert watch_target.action_bias == "WAIT"
    assert watch_target.signal_state == "WAIT"
    assert watch_target.note == "[VWAP] standalone_vwap_blocked"
    assert watch_target.strategy_flag == "VWAP"
    assert watch_target.entry_by == "VWAP"


def test_build_watch_target_status_blocks_cached_overseas_standalone_vwap() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    persisted_snapshot = _snapshot(price=20.0, vwap=19.9, rsi14=45.0)
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="cached_signal",
        strategy_flag="VWAP",
        entry_by="VWAP",
        holding_qty=0,
        last_price=20.0,
        pnl_pct=0.0,
        has_position=0,
        snapshot_json=asdict(persisted_snapshot),
        updated_at="2026-07-10T00:00:00+00:00",
    )

    class FakeStrategyManager:
        def evaluate(self, *args, **kwargs):
            return SimpleNamespace(signal="BUY", flag="VWAP", entry_by="VWAP", exit_by="")

    original_derive_watch_state = liquidity_lab_module.derive_watch_state
    service._get_strategy_manager = lambda code: FakeStrategyManager()  # type: ignore[method-assign]
    liquidity_lab_module.derive_watch_state = lambda *args, **kwargs: (
        "BUY",
        "cached_buy",
    )
    try:
        watch_target = service._build_watch_target_status(
            market="overseas",
            code="COIN",
            exchange_code="NASD",
            price=20.0,
            activity_score=12.0,
            signal_snapshot=None,
            held_position=None,
            holding_qty=0,
        )
    finally:
        liquidity_lab_module.derive_watch_state = original_derive_watch_state

    assert watch_target.action_bias == "WAIT"
    assert watch_target.signal_state == "WAIT"
    assert watch_target.note == "[VWAP] standalone_vwap_blocked|stale_signal_cache"
    assert watch_target.strategy_flag == "VWAP"
    assert watch_target.entry_by == "VWAP"


def test_build_watch_target_status_blocks_cached_buy_for_flat_symbol() -> None:
    service = _build_run_service()
    persisted_snapshot = _snapshot(price=20.0, vwap=19.9, rsi14=45.0)
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="cached_signal",
        strategy_flag="VWAP+RSI",
        entry_by="VWAP",
        holding_qty=0,
        last_price=20.0,
        pnl_pct=0.0,
        has_position=0,
        snapshot_json=asdict(persisted_snapshot),
        updated_at="2026-07-10T00:00:00+00:00",
    )

    class FakeStrategyManager:
        def evaluate(self, *args, **kwargs):
            return SimpleNamespace(signal="BUY", flag="VWAP+RSI", entry_by="VWAP", exit_by="")

    original_derive_watch_state = liquidity_lab_module.derive_watch_state
    service._get_strategy_manager = lambda code: FakeStrategyManager()  # type: ignore[method-assign]
    liquidity_lab_module.derive_watch_state = lambda *args, **kwargs: (
        "BUY",
        "cached_combo_buy",
    )
    try:
        watch_target = service._build_watch_target_status(
            market="overseas",
            code="COIN",
            exchange_code="NASD",
            price=20.0,
            activity_score=12.0,
            signal_snapshot=None,
            held_position=None,
            holding_qty=0,
        )
    finally:
        liquidity_lab_module.derive_watch_state = original_derive_watch_state

    assert watch_target.action_bias == "WAIT"
    assert watch_target.signal_state == "WAIT"
    assert watch_target.note == "[VWAP+RSI] stale_signal_cache_buy_blocked"
    assert watch_target.strategy_flag == "VWAP+RSI"
    assert watch_target.entry_by == "VWAP"


def test_build_watch_target_status_allows_overseas_vwap_combo() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    snapshot = _snapshot(price=20.0, vwap=19.9, rsi14=45.0)

    class FakeStrategyManager:
        def evaluate(self, *args, **kwargs):
            return SimpleNamespace(signal="BUY", flag="VWAP+RSI", entry_by="VWAP", exit_by="")

        def buy_score(self, snapshot):
            return 10.0

    service._get_strategy_manager = lambda code: FakeStrategyManager()  # type: ignore[method-assign]

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="COIN",
        exchange_code="NASD",
        price=20.0,
        activity_score=12.0,
        signal_snapshot=snapshot,
        held_position=None,
        holding_qty=0,
    )

    assert watch_target.action_bias == "BUY"
    assert watch_target.strategy_flag == "VWAP+RSI"
    assert watch_target.entry_by == "VWAP"


def test_restore_strategy_contexts_recovers_held_position_after_restart() -> None:
    service = _build_run_service()
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="vr=3.9x mom=+0.42%",
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        holding_qty=57,
        last_price=170.29,
        pnl_pct=0.028,
        entry_price=165.03,
        peak_price=171.0,
        has_position=1,
        updated_at="2026-07-06T07:00:36+00:00",
    )
    held = OverseasHeldPosition(
        symbol="COIN",
        exchange_code="NASD",
        quantity=57,
        orderable_qty=57,
        avg_price=165.03,
        current_price=170.29,
        pnl_pct=0.028,
    )

    service._restore_strategy_contexts(domestic_positions=[], overseas_positions=[held])

    manager = service._get_strategy_manager("COIN")
    assert manager.position is not None
    assert manager.position.flag == "VWAP+VOL"
    assert manager.position.entry_by == "VWAP"
    assert manager.position.entry_price == 165.03


def test_clear_stale_lab_position_states_uses_refreshed_positions() -> None:
    service = _build_run_service()
    for symbol in ("COIN", "ADBE"):
        service.repository.upsert_lab_symbol_state(
            market="overseas",
            symbol=symbol,
            exchange_code="NASD",
            action_bias="SELL",
            signal_state="SELL_READY",
            note="atr_hard_stop",
            holding_qty=10,
            last_price=100.0,
            pnl_pct=-0.02,
            strategy_flag="VWAP",
            entry_by="VWAP",
            has_position=1,
            updated_at="2026-07-06T07:00:36+00:00",
        )
    held = OverseasHeldPosition(
        symbol="COIN",
        exchange_code="NASD",
        quantity=10,
        orderable_qty=10,
        avg_price=100.0,
        current_price=101.0,
        pnl_pct=0.01,
    )

    service._clear_stale_lab_position_states(
        domestic_positions=[],
        overseas_positions=[held],
        refreshed_markets={"overseas"},
    )

    assert service.repository.get_lab_symbol_state("overseas", "COIN")["has_position"] == 1
    adbe = service.repository.get_lab_symbol_state("overseas", "ADBE")
    assert adbe["has_position"] == 0
    assert adbe["note"] == "stale_position_cleared"
    events = service.repository.list_event_log(event_type="lab_position_state_cleanup", limit=1)
    assert events[0]["event_type"] == "lab_position_state_cleanup"


def test_build_watch_target_status_uses_persisted_snapshot_when_signal_unavailable() -> None:
    service = _build_run_service()
    persisted_snapshot = _snapshot(
        price=170.29,
        volume_ratio=0.5,
        intraday_momentum=-0.001,
        intraday_bar_return=-0.0005,
        rsi14=69.0,
    )
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="cached_signal",
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        holding_qty=57,
        last_price=170.29,
        pnl_pct=0.03,
        entry_price=165.03,
        peak_price=171.0,
        has_position=1,
        snapshot_json=asdict(persisted_snapshot),
        updated_at="2026-07-06T07:00:36+00:00",
    )
    held = OverseasHeldPosition(
        symbol="COIN",
        exchange_code="NASD",
        quantity=57,
        orderable_qty=57,
        avg_price=165.03,
        current_price=170.29,
        pnl_pct=(170.29 - 165.03) / 165.03,
    )

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="COIN",
        exchange_code="NASD",
        price=170.29,
        activity_score=0.0,
        signal_snapshot=None,
        held_position=held,
        holding_qty=57,
    )

    assert watch_target.action_bias == "SELL"
    assert watch_target.signal_state == "SELL_READY"
    assert watch_target.strategy_flag == "VWAP+VOL"
    assert watch_target.entry_by == "VWAP"


def test_load_virtual_overseas_positions_uses_persisted_state_when_rank_missing() -> None:
    service = _build_run_service()
    service.virtual_trades.record_buy(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        qty=3,
        fill_price=68.7,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-07-09 20:10:00 KST",
    )
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="persisted",
        strategy_flag="VWAP",
        entry_by="VWAP",
        holding_qty=3,
        last_price=61.25,
        pnl_pct=(61.25 - 68.7) / 68.7,
        has_position=1,
        updated_at="2026-07-09T11:00:00+00:00",
    )

    positions = service._load_virtual_overseas_positions([])

    assert len(positions) == 1
    assert positions[0].symbol == "SOLS"
    assert positions[0].exchange_code == "NASD"
    assert positions[0].current_price == 61.25
    assert positions[0].is_virtual is True


def test_get_overseas_signal_for_candidate_reuses_recent_cache_without_reload() -> None:
    service = _build_run_service()
    service._signal_cache["COIN"] = _snapshot(price=165.0)
    service._signal_cache_updated_at = {}
    service._signal_cache_updated_at["COIN"] = datetime.now(timezone.utc)

    async def fail_if_called(candidate):  # noqa: ANN001
        raise AssertionError("reload should not be called for fresh cache")

    service._load_overseas_signal = fail_if_called  # type: ignore[method-assign]
    candidate = OverseasScanResult(
        symbol="COIN",
        exchange_code="NASD",
        last_price=170.29,
        bid=170.28,
        ask=170.30,
        spread_pct=0.0001,
        change_rate_pct=0.0,
        volume=0,
        orderable_qty=0,
        fx_rate_krw=1350.0,
        activity_score=0.0,
    )

    snapshot = asyncio.run(service._get_overseas_signal_for_candidate(candidate))

    assert snapshot is not None
    assert snapshot.price == 170.29


def test_maybe_send_overseas_relist_alert_skips_on_nyse_holiday() -> None:
    service = _build_run_service()
    service.notifier = DummyNotifier()
    service._overseas_relist_schedule = [(1, 0)]
    service._last_relist_kst = None
    service._dynamic_overseas_pool = []

    now = datetime(2026, 7, 4, 16, 0, tzinfo=timezone.utc)
    asyncio.run(service._maybe_send_overseas_relist_alert(now, nyse_holiday=True))

    assert service.notifier.messages == []


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
    assert {row["is_session_trade"] for row in rows} == {0}


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


def test_build_unified_watch_targets_updates_wait_cycles() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 2
    domestic_ranked = [
        DomesticScanResult("D1", 10100, 10110, 10090, 0.001, 0.01, 9_000_000_000, 100_000, 50.0),
    ]

    async def fake_load_domestic_signal(candidate):
        return _snapshot(price=float(candidate.current_price))

    def fake_build_watch_target_status(**kwargs):
        return WatchTargetStatus(
            market=kwargs["market"],
            code=kwargs["code"],
            exchange_code=kwargs["exchange_code"],
            price=kwargs["price"],
            activity_score=kwargs["activity_score"],
            signal_score=0.0,
            action_bias="WAIT",
            signal_state="WAIT",
            ma_summary="20d>60d 5>20",
            note="watch",
            holding_qty=kwargs["holding_qty"],
        )

    service._load_domestic_signal = fake_load_domestic_signal  # type: ignore[method-assign]
    service._build_watch_target_status = fake_build_watch_target_status  # type: ignore[method-assign]

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

    assert watch_targets[0].action_bias == "WAIT"
    assert service._wait_cycles["domestic:D1"] == 1


def test_build_unified_watch_targets_includes_virtual_held_outside_rank() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.unified_watch_top_n = 0
    service.virtual_trades.record_buy(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        qty=3,
        fill_price=68.7,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-07-09 20:10:00 KST",
    )
    persisted_snapshot = _snapshot(price=61.25, rsi14=48.0)
    service.repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="cached_signal",
        strategy_flag="VWAP",
        entry_by="VWAP",
        holding_qty=3,
        last_price=61.25,
        pnl_pct=(61.25 - 68.7) / 68.7,
        has_position=1,
        snapshot_json=asdict(persisted_snapshot),
        updated_at="2026-07-09T11:00:00+00:00",
    )
    virtual_held = OverseasHeldPosition(
        symbol="SOLS",
        exchange_code="NASD",
        quantity=3,
        orderable_qty=3,
        avg_price=68.7,
        current_price=61.25,
        pnl_pct=(61.25 - 68.7) / 68.7,
        is_virtual=True,
    )

    watch_targets = asyncio.run(
        service._build_unified_watch_targets(
            domestic_ranked=[],
            overseas_ranked=[],
            domestic_positions=[],
            overseas_positions=[virtual_held],
            krx_open=False,
            us_open=True,
        )
    )

    assert [item.code for item in watch_targets] == ["SOLS"]
    assert watch_targets[0].holding_qty == 3
    assert watch_targets[0].strategy_flag == "VWAP"


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


def test_strategy_buy_can_override_entry_setup_wait() -> None:
    service = _build_run_service()

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="SOXL",
        exchange_code="AMEX",
        price=20.0,
        activity_score=20.0,
        signal_snapshot=_snapshot(
            price=20.0,
            vwap=20.0,
            rsi14=45.0,
            volume_ratio=1.0,
            macd_golden=True,
            macd_dead=False,
            breakout_distance_pct=-0.001,
            intraday_momentum=0.0002,
            intraday_bar_return=0.0001,
        ),
    )

    assert watch_target.action_bias == "BUY"
    assert watch_target.signal_state == "BUY"
    assert watch_target.strategy_flag == "VWAP+RSI"


def test_overseas_single_vwap_buy_waits_for_entry_confirmation() -> None:
    service = _build_run_service()

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="AAPL",
        exchange_code="NASD",
        price=100.5,
        activity_score=20.0,
        signal_snapshot=_snapshot(
            price=100.5,
            vwap=100.0,
            rsi14=45.0,
            volume_ratio=1.0,
            macd_golden=False,
            macd_dead=False,
            macd_line=0.1,
            macd_signal=0.2,
            breakout_distance_pct=-0.01,
            intraday_momentum=0.0002,
            intraday_bar_return=0.0001,
        ),
    )

    assert watch_target.action_bias == "WAIT"
    assert watch_target.strategy_flag == "VWAP"
    assert watch_target.note.startswith("[VWAP] confirm_wait:")


def test_overseas_single_rsi_buy_waits_for_entry_confirmation() -> None:
    service = _build_run_service()

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="ALNY",
        exchange_code="NASD",
        price=100.0,
        activity_score=20.0,
        signal_snapshot=_snapshot(
            price=100.0,
            vwap=105.0,
            rsi14=30.0,
            volume_ratio=1.0,
            macd_golden=True,
            macd_dead=False,
            macd_line=0.4,
            macd_signal=0.2,
            breakout_distance_pct=-0.01,
            intraday_momentum=0.0002,
            intraday_bar_return=0.0001,
        ),
    )

    assert watch_target.action_bias == "WAIT"
    assert watch_target.strategy_flag == "RSI"
    assert watch_target.note.startswith("[RSI] confirm_wait:")
    assert watch_target.entry_by == "RSI"


def test_strategy_buy_blocked_by_exit_cooldown() -> None:
    service = _build_run_service()
    service._exit_cooldown = {
        "overseas:SOXL": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    watch_target = service._build_watch_target_status(
        market="overseas",
        code="SOXL",
        exchange_code="AMEX",
        price=20.0,
        activity_score=20.0,
        signal_snapshot=_snapshot(
            price=20.0,
            vwap=20.0,
            rsi14=45.0,
            volume_ratio=1.0,
            macd_golden=True,
            macd_dead=False,
            breakout_distance_pct=-0.001,
            intraday_momentum=0.0002,
            intraday_bar_return=0.0001,
        ),
    )

    assert watch_target.action_bias == "WAIT"
    assert watch_target.note.startswith("재진입대기")


def test_record_volume_and_get_surge_ratio_detects_spike() -> None:
    service = _build_run_service()

    ratios = [
        service._record_volume_and_get_surge_ratio("SOXL", volume)
        for volume in (1000, 1100, 1200, 1300, 1400, 2400)
    ]

    assert ratios[0] == 1.0
    assert ratios[-1] >= 5.0


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


def test_select_overseas_buy_targets_excludes_standalone_vwap_when_blocked() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    overseas_ranked = [
        OverseasScanResult("NVDA", "NASD", 150.0, 149.9, 150.1, 0.0013, 2.0, 900_000, 10, 1350.0, 18.0),
        OverseasScanResult("AMD", "NASD", 155.0, 154.9, 155.1, 0.0012, 1.5, 800_000, 10, 1350.0, 17.0),
    ]
    watch_targets = [
        WatchTargetStatus(
            "overseas",
            "NVDA",
            "NASD",
            150.0,
            18.0,
            12.0,
            "BUY",
            "BUY_READY",
            "20d>60d 5>20",
            "[VWAP] strategy_buy_signal",
            0,
            strategy_flag="VWAP",
            entry_by="VWAP",
        ),
        WatchTargetStatus(
            "overseas",
            "AMD",
            "NASD",
            155.0,
            17.0,
            11.0,
            "BUY",
            "BUY_READY",
            "20d>60d 5>20",
            "[VWAP+RSI] strategy_buy_signal",
            0,
            strategy_flag="VWAP+RSI",
            entry_by="VWAP",
        ),
    ]

    selected = service._select_overseas_buy_targets(overseas_ranked, watch_targets, max_concurrent=3)

    assert [item.symbol for item in selected] == ["AMD"]


def test_select_overseas_buy_targets_excludes_already_held_symbols() -> None:
    service = _build_run_service()
    overseas_ranked = [
        OverseasScanResult("CHW", "NASD", 10.0, 9.9, 10.1, 0.001, 1.0, 700_000, 10, 1350.0, 18.0),
        OverseasScanResult("AMD", "NASD", 155.0, 154.9, 155.1, 0.0012, 1.5, 800_000, 10, 1350.0, 17.0),
        OverseasScanResult("AAPL", "NASD", 210.0, 209.9, 210.1, 0.0010, 1.2, 700_000, 10, 1350.0, 16.0),
    ]
    watch_targets = [
        WatchTargetStatus("overseas", "CHW", "NASD", 10.0, 18.0, 12.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "AMD", "NASD", 155.0, 17.0, 11.0, "BUY", "BUY_READY", "20d>60d 5>20", "pullback_entry", 0),
        WatchTargetStatus("overseas", "AAPL", "NASD", 210.0, 16.0, 10.0, "BUY", "BUY_READY", "20d>60d 5>20", "volume_breakout_entry", 0),
    ]
    held_positions = [
        OverseasHeldPosition(
            symbol="CHW",
            exchange_code="NASD",
            quantity=100,
            orderable_qty=100,
            avg_price=9.8,
            current_price=10.0,
            pnl_pct=0.02,
        )
    ]

    selected = service._select_overseas_buy_targets(
        overseas_ranked,
        watch_targets,
        max_concurrent=3,
        held_positions=held_positions,
    )

    assert [item.symbol for item in selected] == ["AMD", "AAPL"]


def test_remaining_overseas_entry_slots_counts_virtual_positions() -> None:
    positions = [
        OverseasHeldPosition(
            symbol="REAL",
            exchange_code="NASD",
            quantity=1,
            orderable_qty=1,
            avg_price=10.0,
            current_price=10.0,
            pnl_pct=0.0,
            is_virtual=False,
        ),
        OverseasHeldPosition(
            symbol="VIRT",
            exchange_code="NASD",
            quantity=1,
            orderable_qty=1,
            avg_price=10.0,
            current_price=10.0,
            pnl_pct=0.0,
            is_virtual=True,
        ),
        OverseasHeldPosition(
            symbol="REAL",
            exchange_code="NASD",
            quantity=1,
            orderable_qty=1,
            avg_price=10.0,
            current_price=10.0,
            pnl_pct=0.0,
            is_virtual=True,
        ),
    ]

    remaining = LiquidityLabService._remaining_overseas_entry_slots(
        positions,
        max_positions=2,
    )

    assert remaining == 0


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

    async def fake_manage_overseas_position(*, candidate, held_positions, watch_target=None):
        manage_calls.append(candidate.symbol)
        return {"submitted": True}

    async def fake_send_summary(report):
        return None

    async def fake_select_overseas_exit_targets(overseas_ranked, overseas_positions, max_exits=5):
        return None if max_exits < 0 else []

    service.scan_domestic = lambda: []  # type: ignore[method-assign]
    service._load_domestic_positions = lambda domestic_ranked: []  # type: ignore[method-assign]
    service.scan_overseas = fake_scan_overseas  # type: ignore[method-assign]
    service._load_overseas_positions = fake_load_overseas_positions  # type: ignore[method-assign]
    service._build_unified_watch_targets = fake_build_unified_watch_targets  # type: ignore[method-assign]
    service._select_overseas_exit_targets = fake_select_overseas_exit_targets  # type: ignore[method-assign]
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

    async def fake_select_overseas_exit_targets(overseas_ranked, overseas_positions, max_exits=5):
        return [(candidate, held, "stop_loss", None)]

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
    service._select_overseas_exit_targets = fake_select_overseas_exit_targets  # type: ignore[method-assign]
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

    async def fake_place_domestic_test_order(candidate, watch_target=None):
        return {"submitted": True, "side": "buy", "candidate": {"stock_code": candidate.stock_code}, "qty": 1}

    async def fake_manage_overseas_position(*, candidate, held_positions, watch_target=None):
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

    async def fake_place_domestic_test_order(candidate, watch_target=None):
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

    async def fake_manage_overseas_position(*, candidate, held_positions, watch_target=None):
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
    assert "동작=매수접수" in service.notifier.messages[0]


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
    assert "동작=매수접수" in service.notifier.messages[0]


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
        "pnl_text": "-",
        "strategy_flag": "-",
        "entry_by": "-",
        "exit_by": "-",
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
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "동작=매도거부" in service.notifier.messages[0]
    assert "참고=주문이 거부되어 실제로 체결되지 않았습니다" in service.notifier.messages[0]


def test_send_summary_uses_rejected_order_symbol_instead_of_primary_target() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-07-10 02:54:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="regular",
        us_orderable_in_profile=True,
        primary_market="overseas",
        primary_target="BBIO",
        primary_selection_reason="existing_position_stop_loss",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        domestic_order=None,
        overseas_order={
            "submitted": False,
            "skipped": True,
            "market": "overseas",
            "side": "sell",
            "candidate": {"symbol": "ALNY", "last_price": 318.12},
            "held_position": {
                "symbol": "ALNY",
                "quantity": 61,
                "current_price": 318.12,
                "pnl_pct": -0.06,
            },
            "qty": 61,
            "reason": "no_orderable_qty",
            "error": "mock balance missing",
            "exit_reason": "stop_loss",
        },
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "종목=ALNY" in service.notifier.messages[0]
    assert "종목=BBIO" not in service.notifier.messages[0]
    assert "수량=61" in service.notifier.messages[0]
    assert "동작=매도거부" in service.notifier.messages[0]


def test_send_summary_reports_skip_counts_when_trade_already_notified() -> None:
    service = _build_run_service()
    report = LiquidityLabReport(
        scanned_at="2026-06-30 22:05:00 KST",
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
        domestic_order={
            "submitted": True,
            "already_notified": True,
            "side": "buy",
            "candidate": {"stock_code": "005930", "current_price": 82000},
            "qty": 1,
            "batched_orders": [
                {
                    "submitted": True,
                    "already_notified": True,
                    "side": "buy",
                    "candidate": {"stock_code": "005930", "current_price": 82000},
                    "qty": 1,
                },
                {
                    "skipped": True,
                    "side": "buy",
                    "candidate": {"stock_code": "000660", "current_price": 210000},
                    "reason": "entry_rsi_too_high",
                },
            ],
        },
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert service.notifier.messages[0].startswith("[KIS][거래알림]")
    assert "동작=추가미실행" in service.notifier.messages[0]
    assert "미실행=1건" in service.notifier.messages[0]


def test_place_overseas_sell_order_defers_unprofitable_time_exit_profit_before_submit() -> None:
    service = _build_sell_service()
    service.config.auto_trade = SimpleNamespace(
        usd_krw_fallback_rate=1380.0,
        overseas_buy_fee_rate=0.0025,
        overseas_sell_fee_rate=0.0025,
        overseas_tax_rate=0.0,
        overseas_min_fee_usd=0.0,
        overseas_min_sell_fee_usd=0.0,
    )
    candidate = OverseasScanResult(
        symbol="PCAP",
        exchange_code="NASD",
        last_price=10.295,
        bid=10.295,
        ask=10.305,
        spread_pct=0.001,
        change_rate_pct=0.1,
        volume=500_000,
        orderable_qty=0,
        fx_rate_krw=1380.0,
        activity_score=10.0,
    )
    held = OverseasHeldPosition(
        symbol="PCAP",
        exchange_code="NASD",
        quantity=610,
        orderable_qty=610,
        avg_price=10.29,
        current_price=10.295,
        pnl_pct=0.00048,
    )

    result = _run_orderable_overseas_sell(service, candidate, held, "time_exit_profit")

    assert result["skipped"] is True
    assert result["reason"] == "net_profit_below_cost"
    assert service.client.order_calls == []
    assert service.notifier.messages == []


def test_register_exit_cooldown_uses_reason_specific_minutes() -> None:
    service = _build_run_service()
    service._exit_cooldown = {}
    before = datetime.now(timezone.utc)

    service._register_exit_cooldown("overseas", "RIVN", "stop_loss")
    service._register_exit_cooldown("overseas", "SOXL", "momentum_loss_cut")
    service._register_exit_cooldown("overseas", "PLBL", "marginal_profit_exit")
    service._register_exit_cooldown("overseas", "AAPL", "other_exit")

    stop_loss_delta = (service._exit_cooldown["overseas:RIVN"] - before).total_seconds() / 60.0
    momentum_delta = (service._exit_cooldown["overseas:SOXL"] - before).total_seconds() / 60.0
    marginal_delta = (service._exit_cooldown["overseas:PLBL"] - before).total_seconds() / 60.0
    default_delta = (service._exit_cooldown["overseas:AAPL"] - before).total_seconds() / 60.0

    assert 24.5 <= stop_loss_delta <= 25.5
    assert 11.5 <= momentum_delta <= 12.5
    assert 14.5 <= marginal_delta <= 15.5
    assert 7.5 <= default_delta <= 8.5


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


def test_run_marks_us_holiday_as_closed_when_skip_enabled() -> None:
    service = _build_run_service()
    service.config.skip_holiday_overseas = True
    service.config.skip_holiday_domestic = True
    seen_krx_dates = []
    seen_nyse_dates = []
    original_is_krx_regular_session = liquidity_lab_module.is_krx_regular_session
    original_is_us_regular_session = liquidity_lab_module.is_us_regular_session
    original_is_us_orderable_session_for_env = liquidity_lab_module.is_us_orderable_session_for_env
    original_get_us_trading_session = liquidity_lab_module.get_us_trading_session
    original_is_krx_holiday = liquidity_lab_module.is_krx_holiday
    original_is_nyse_holiday = liquidity_lab_module.is_nyse_holiday
    liquidity_lab_module.is_krx_regular_session = lambda now: True
    liquidity_lab_module.is_us_regular_session = lambda now: True
    liquidity_lab_module.is_us_orderable_session_for_env = lambda now, env: True
    liquidity_lab_module.get_us_trading_session = lambda now: "regular"

    def fake_krx_holiday(target_date=None):
        seen_krx_dates.append(target_date)
        return False

    def fake_nyse_holiday(target_date=None):
        seen_nyse_dates.append(target_date)
        return True

    liquidity_lab_module.is_krx_holiday = fake_krx_holiday
    liquidity_lab_module.is_nyse_holiday = fake_nyse_holiday
    try:
        report = asyncio.run(service.run())
    finally:
        liquidity_lab_module.is_krx_regular_session = original_is_krx_regular_session
        liquidity_lab_module.is_us_regular_session = original_is_us_regular_session
        liquidity_lab_module.is_us_orderable_session_for_env = original_is_us_orderable_session_for_env
        liquidity_lab_module.get_us_trading_session = original_get_us_trading_session
        liquidity_lab_module.is_krx_holiday = original_is_krx_holiday
        liquidity_lab_module.is_nyse_holiday = original_is_nyse_holiday

    assert report.krx_market_open is True
    assert seen_krx_dates
    assert seen_nyse_dates
    assert all(target_date is not None for target_date in seen_krx_dates)
    assert all(target_date is not None for target_date in seen_nyse_dates)


def test_run_returns_network_error_report_on_connect_timeout() -> None:
    service = _build_run_service()

    async def fake_run_cycle():
        raise httpx.ConnectTimeout("connect timeout")

    service._run_cycle = fake_run_cycle  # type: ignore[method-assign]

    report = asyncio.run(service.run())

    assert report.primary_selection_reason == "network_error"
    assert report.primary_market == "none"
    assert report.domestic_order is None
    assert report.overseas_order is None
    assert report.estimated_api_calls_per_cycle == 0


def test_run_returns_network_error_report_on_kis_api_error() -> None:
    service = _build_run_service()

    async def fake_run_cycle():
        raise KisApiError("token_request_failed: timeout")

    service._run_cycle = fake_run_cycle  # type: ignore[method-assign]

    report = asyncio.run(service.run())

    assert report.primary_selection_reason == "network_error"
    assert report.primary_market == "none"
    assert report.watch_targets == []
    assert report.us_market_open is False


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
    assert any("SOXL(가상) 가상매도" in message for message in service.notifier.messages)


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
        def __init__(self) -> None:
            self.possible_order_prices: list[str] = []

        async def get_overseas_possible_order(self, *, symbol: str, exchange_code: str, price: str):
            self.possible_order_prices.append(price)
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
    client = DummyOverseasSlotClient()
    service.client = client
    service.notifier = DummyNotifier()
    service._signal_cache = {
        "SOXL": _snapshot(
            price=25.0,
            vwap=24.9,
            macd_line=0.4,
            macd_signal=0.2,
        )
    }
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
    assert result["qty"] == 3
    assert client.possible_order_prices == ["25.0100"]


def test_get_overseas_available_usd_caps_large_theoretical_amount_by_orderable_qty() -> None:
    class DummyPossibleOrderClient:
        async def get_overseas_possible_order(self, *, symbol: str, exchange_code: str, price: str):
            return {
                "cash_available": None,
                "max_order_quantity": "1217",
                "overseas_max_order_amount": None,
                "raw": {
                    "tr_crcy_cd": "USD",
                    "ord_psbl_frcr_amt": "67071.63",
                    "ovrs_ord_psbl_amt": "67071.63",
                    "echm_af_ord_psbl_amt": "67071.63",
                    "max_ord_psbl_qty": "1217",
                    "ord_psbl_qty": "1217",
                    "echm_af_ord_psbl_qty": "1217",
                    # Larger theoretical/pre-exchange amount must not drive sizing.
                    "frcr_ord_psbl_amt1": "171292.388229",
                },
            }

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.client = DummyPossibleOrderClient()

    available_usd = asyncio.run(
        service._get_overseas_available_usd(
            symbol="MSEX",
            exchange_code="NASD",
            price=54.53,
        )
    )

    expected = 1217 * 54.53
    assert abs(available_usd - expected) < 0.000001
    assert abs(service._last_overseas_available_usd - expected) < 0.000001


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
    service._signal_cache = {
        "SOXL": _snapshot(
            price=25.0,
            vwap=24.9,
            macd_line=0.4,
            macd_signal=0.2,
        )
    }
    service._pending_trade_notifications = []
    service._pending_trade_notification_started_at = None
    service._trade_notification_window_sec = 0
    service._trade_notification_max_batch_size = 8
    service._session_id = "sess-overseas-buy"
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
    broker_rows = service.repository.list_broker_order_events(limit=5)

    assert result["submitted"] is True
    assert result["qty"] == 3
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SOXL"
    assert rows[0]["session_id"] == "sess-overseas-buy"
    assert len(broker_rows) == 1
    assert broker_rows[0]["symbol"] == "SOXL"
    assert broker_rows[0]["requested_price"] == 25.01
    assert service.notifier.messages[0].startswith("[KIS][거래알림]")
    assert rows[0]["action_reason"] == "strategy_buy_signal"
    assert rows[0]["vwap"] is not None
    assert rows[0]["macd_line"] is not None
    assert rows[0]["macd_signal"] is not None
    assert rows[0]["spread_pct"] is not None
    assert rows[0]["consecutive_losses"] == 0


def test_place_overseas_test_order_blocks_standalone_vwap_before_submission() -> None:
    class FailingOverseasClient:
        async def get_overseas_possible_order(self, **kwargs):
            raise AssertionError("possible-order API should not be called")

        async def place_overseas_order_for_current_session(self, **kwargs):
            raise AssertionError("order API should not be called")

    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    service.client = FailingOverseasClient()
    snapshot = _snapshot(price=25.0, vwap=24.9, rsi14=45.0)
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
    watch_target = WatchTargetStatus(
        market="overseas",
        code="SOXL",
        exchange_code="AMEX",
        price=25.0,
        activity_score=16.0,
        signal_score=40.0,
        action_bias="BUY",
        signal_state="BUY",
        ma_summary="20d>60d 9>21",
        note="[VWAP] strategy_buy_signal",
        signal_snapshot=snapshot,
        strategy_flag="VWAP",
        entry_by="VWAP",
    )

    result = asyncio.run(service._place_overseas_test_order(candidate, watch_target=watch_target))

    assert result["skipped"] is True
    assert result["reason"] == "standalone_vwap_blocked"
    rows = service.repository.query_cycle_log(action_bias="SKIP", limit=5)
    assert rows[0]["symbol"] == "SOXL"
    assert rows[0]["action_reason"] == "buy:standalone_vwap_blocked"


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


def test_record_virtual_overseas_buy_blocks_standalone_vwap() -> None:
    service = _build_run_service()
    service.config.liquidity_lab.overseas_block_standalone_vwap = True
    snapshot = _snapshot(price=25.0, vwap=24.9, rsi14=45.0)
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
    watch_target = WatchTargetStatus(
        market="overseas",
        code="SOXL",
        exchange_code="AMEX",
        price=25.0,
        activity_score=16.0,
        signal_score=40.0,
        action_bias="BUY",
        signal_state="BUY",
        ma_summary="20d>60d 9>21",
        note="[VWAP] strategy_buy_signal",
        signal_snapshot=snapshot,
        strategy_flag="VWAP",
        entry_by="VWAP",
    )

    result = asyncio.run(
        service._record_virtual_overseas_buy(
            candidate,
            signal_snapshot=snapshot,
            watch_target=watch_target,
        )
    )

    assert result["skipped"] is True
    assert result["reason"] == "standalone_vwap_blocked"
    assert service.virtual_trades.get_position("overseas", "SOXL") is None


def test_virtual_overseas_buy_respects_total_virtual_exposure_limit() -> None:
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
    service.config.liquidity_lab.max_virtual_exposure_pct = 1.0
    service.client = DummyVirtualSlotClient()
    service.virtual_trades.record_buy(
        market="overseas",
        symbol="FULL",
        exchange_code="NASD",
        qty=10,
        fill_price=100.0,
        currency="USD",
        session="daytime",
        reason="seed",
        created_at="2026-07-10 10:00:00 KST",
    )
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

    assert result["skipped"] is True
    assert result["reason"] == "virtual_exposure_limit"
    assert service.virtual_trades.get_position("overseas", "SOXL") is None
    rows = service.repository.query_cycle_log(action_bias="SKIP", limit=5)
    events = service.repository.list_event_log(event_type="trade_skip", limit=5)
    assert rows[0]["symbol"] == "SOXL"
    assert rows[0]["action_reason"] == "buy:virtual_exposure_limit"
    assert events[0]["symbol"] == "SOXL"
    detail = json.loads(events[0]["detail"])
    assert detail["reason"] == "virtual_exposure_limit"
    assert detail["available_usd"] == 1000.0
    assert detail["virtual_notional_usd"] == 1000.0


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
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_session_blocked_real_sell_does_not_record_virtual_sell() -> None:
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

    assert first.overseas_order["skipped"] is True
    assert first.overseas_order["reason"] == "session_not_orderable_in_profile"
    assert second.overseas_order["skipped"] is True
    assert second.overseas_order["reason"] == "session_not_orderable_in_profile"
    assert service.client.order_calls == []
    pending = service.repository.get_virtual_sell_pending("overseas", "NVDA")
    assert pending is None
    sell_orders = [
        row
        for row in service.repository.list_virtual_orders(limit=20)
        if row["symbol"] == "NVDA" and row["side"] == "sell"
    ]
    assert sell_orders == []


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

    async def fake_select_overseas_exit_targets(overseas_ranked, overseas_positions, max_exits=5):
        return [(candidate, held, "stop_loss", None)]

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
    service._select_overseas_exit_targets = fake_select_overseas_exit_targets  # type: ignore[method-assign]
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
