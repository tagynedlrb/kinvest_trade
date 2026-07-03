import asyncio

import httpx

from kinvest_trade.tv_scanner import check_connectivity, scan_top_volume_surge


def test_check_connectivity_returns_true_on_http_200() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    async def run_case() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_connectivity(client)

    assert asyncio.run(run_case()) is True


def test_scan_top_volume_surge_parses_supported_exchange_symbols() -> None:
    payload = {
        "data": [
            {"d": ["NASDAQ:NVDA"]},
            {"d": ["NYSE:PLTR"]},
            {"d": ["AMEX:SOXL"]},
            {"d": ["OTC:ABCD"]},
            {"d": ["NASDAQ:NVDA"]},
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run_case() -> list[dict[str, str]]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await scan_top_volume_surge(client, top_n=5)

    assert asyncio.run(run_case()) == [
        {"symbol": "NVDA", "exchange_code": "NASD"},
        {"symbol": "PLTR", "exchange_code": "NYSE"},
        {"symbol": "SOXL", "exchange_code": "AMEX"},
    ]
