import asyncio
from types import SimpleNamespace

from kinvest_trade.liquidity_lab import DomesticScanResult, LiquidityLabService


def _build_service() -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = SimpleNamespace(
        liquidity_lab=SimpleNamespace(
            domestic_dynamic_scan=False,
            domestic_dynamic_rescan_cycles=20,
            domestic_candidates=["005930", "058730", "042660"],
            unified_scan_top_n=2,
            max_wait_cycles_before_penalty=15,
            wait_penalty_decay=0.07,
            domestic_min_price_krw=3000,
            domestic_min_intraday_turnover_krw=20_000_000_000,
            domestic_min_volume_sum=100_000,
            domestic_max_spread_pct=0.003,
        )
    )
    service._domestic_excluded = []
    service._wait_cycles = {}
    service._domestic_balance_cache = {}
    return service


def _quote(stock_code: str, *, thin: bool) -> DomesticScanResult:
    return DomesticScanResult(
        stock_code=stock_code,
        current_price=80_000,
        best_ask=80_050,
        best_bid=79_950,
        spread_pct=0.001,
        minute_change_pct=0.005,
        intraday_turnover_krw=1_000_000 if thin else 100_000_000_000,
        volume_sum=1_000 if thin else 500_000,
        activity_score=10.0,
    )


def test_scan_domestic_excludes_thin_non_held_candidate() -> None:
    service = _build_service()

    async def fake_quote(stock_code):
        return _quote(stock_code, thin=(stock_code == "042660"))

    async def fake_full(stock_code):
        return _quote(stock_code, thin=(stock_code == "042660"))

    service._scan_single_domestic_quote = fake_quote
    service._scan_single_domestic = fake_full

    ranked = asyncio.run(service.scan_domestic())

    assert "042660" not in [item.stock_code for item in ranked]
    assert "042660" in [item.code for item in service._domestic_excluded]


def test_held_domestic_symbol_exempt_from_speculative_liquidity_filter() -> None:
    # Mirrors the overseas held-symbol exemption regression: a held position
    # must not be dropped from domestic_ranked (and thus lose its fresh
    # quote/signal) just because its live quote momentarily trips the
    # thin-volume/wide-spread "new candidate" quality filter.
    service = _build_service()
    service._domestic_balance_cache = {
        "cycle": 1,
        "data": {"positions": [{"pdno": "042660", "hldg_qty": "10"}]},
    }

    async def fake_quote(stock_code):
        return _quote(stock_code, thin=(stock_code == "042660"))

    async def fake_full(stock_code):
        return _quote(stock_code, thin=(stock_code == "042660"))

    service._scan_single_domestic_quote = fake_quote
    service._scan_single_domestic = fake_full

    ranked = asyncio.run(service.scan_domestic())

    assert "042660" in [item.stock_code for item in ranked]
    assert "042660" not in [item.code for item in service._domestic_excluded]
