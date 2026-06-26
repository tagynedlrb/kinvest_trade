from kinvest_trade.liquidity_lab import (
    LiquidityLabService,
    OverseasHeldPosition,
    OverseasScanResult,
)


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
