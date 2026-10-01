import asyncio

import httpx
import pytest

from kinvest_trade.tv_scanner import check_connectivity, scan_top_volume_surge


def test_check_connectivity_returns_true_on_http_200() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    async def run_case() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_connectivity(client)

    assert asyncio.run(run_case()) is True


def _row(
    ticker: str,
    typespecs: list[str] | None = None,
    *,
    sector: str = "Technology Services",
    industry: str = "Packaged Software",
) -> dict:
    # Mirrors the real TradingView scanner.tradingview.com/america/scan shape:
    # the exchange lives only in the top-level "s" ticker (e.g. "NASDAQ:AAPL"),
    # never in the "d"/"name" column, which is a bare symbol.
    name = ticker.split(":", 1)[1]
    return {
        "s": ticker,
        "d": [
            name,
            10.0,
            1_000_000,
            3.0,
            1.0,
            5e8,
            sector,
            industry,
            typespecs or ["common"],
        ],
    }


def _expected(symbol: str, exchange_code: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange_code": exchange_code,
        "sector_name": "Technology Services",
        "industry_name": "Packaged Software",
        "scanner_price": 10.0,
        "scanner_volume": 1_000_000,
        "scanner_relative_volume": 3.0,
        "scanner_change_pct": 1.0,
        "scanner_market_cap": 5e8,
    }


def test_scan_top_volume_surge_parses_supported_exchange_symbols() -> None:
    payload = {
        "data": [
            _row("NASDAQ:NVDA"),
            _row("NYSE:PLTR"),
            _row("AMEX:SOXL"),
            _row("OTC:ABCD"),
            _row("NASDAQ:NVDA"),
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run_case() -> list[dict[str, object]]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await scan_top_volume_surge(client, top_n=5)

    assert asyncio.run(run_case()) == [
        _expected("NVDA", "NASD"),
        _expected("PLTR", "NYSE"),
        _expected("SOXL", "AMEX"),
    ]


def test_scan_top_volume_surge_excludes_preferred_and_non_symbol_tickers() -> None:
    payload = {
        "data": [
            _row("NYSE:APO/PA", typespecs=["preferred"]),
            _row("NYSE:HPE/PC", typespecs=["preferred"]),
            _row("NASDAQ:XOMA", typespecs=["common"]),
            _row("NASDAQ:ERIC", typespecs=[""]),
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run_case() -> list[dict[str, object]]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await scan_top_volume_surge(client, top_n=10)

    assert asyncio.run(run_case()) == [
        _expected("XOMA", "NASD"),
        _expected("ERIC", "NASD"),
    ]


def test_scan_top_volume_surge_ignores_bare_name_column_without_exchange() -> None:
    # Regression: the "name" column alone (no "EXCHANGE:" prefix) must never be
    # trusted for exchange detection -- that previously defaulted every OTC/
    # pink-sheet row to NASD and flooded the pool with untradeable tickers.
    payload = {"data": [{"d": ["SNEJF", 20.0, 600_000, 5.0, 1.0, 1e11, ["common"]]}]}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run_case() -> list[dict[str, object]]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await scan_top_volume_surge(client, top_n=5)

    assert asyncio.run(run_case()) == []


@pytest.mark.parametrize("fraction, expected", [(0.0, ["D1", "D2", "D3", "U1"]), (0.5, ["U1", "U2", "D1", "D2"])])
def test_scan_reserves_bounded_positive_discovery_slots(fraction, expected):
    rows = [_row("NASDAQ:" + name) for name in ["D1", "D2", "D3", "U1", "U2", "U3"]]
    for row in rows[:3]:
        row["d"][4] = -10.0

    async def run_case():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows})
        )) as client:
            return await scan_top_volume_surge(client, top_n=4, positive_change_fraction=fraction)

    result = asyncio.run(run_case())
    assert [r["symbol"] for r in result] == expected
    assert len(result) == 4


def test_scan_positive_slots_fall_back_without_dropping_universe():
    rows = [_row("NASDAQ:" + name) for name in ["D1", "D2", "U1", "BAD"]]
    rows[0]["d"][4] = -1
    rows[1]["d"][4] = None
    rows[3]["d"][4] = "nan"

    async def run_case():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows})
        )) as client:
            return await scan_top_volume_surge(client, top_n=4, positive_change_fraction=0.5)

    result = asyncio.run(run_case())
    assert [r["symbol"] for r in result] == ["U1", "D1", "D2", "BAD"]


@pytest.mark.parametrize("fraction", [-0.1, 1.1, float("nan")])
def test_scan_rejects_invalid_discovery_fraction_before_network(fraction):
    async def run_case():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("invalid fraction must not reach network")
        )) as client:
            with pytest.raises(ValueError, match="positive_change_fraction"):
                await scan_top_volume_surge(client, positive_change_fraction=fraction)
    asyncio.run(run_case())
