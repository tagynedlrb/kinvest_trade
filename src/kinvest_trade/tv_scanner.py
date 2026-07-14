"""
TradingView Scanner API wrapper for overseas dynamic pool refresh.

This module is used only for overseas pool discovery. Domestic dynamic
scanning stays on the KIS ranking endpoints added in #39.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TV_URL = "https://scanner.tradingview.com/america/scan"
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; tradingview-screener/3.0)",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
_TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=12.0, pool=12.0)
_SUPPORTED_EXCHANGES = {
    "NASDAQ": "NASD",
    "NYSE": "NYSE",
    "AMEX": "AMEX",
    "NYSEARCA": "AMEX",
    "ARCA": "AMEX",
}


def _parse_tv_symbol(raw_ticker: object) -> dict[str, str] | None:
    # The scanner's `s` field is always "EXCHANGE:SYMBOL" (e.g. "NASDAQ:AAPL",
    # "OTC:PCRHY"). Do not fall back to defaulting an unprefixed name to NASD:
    # that previously let every OTC/pink-sheet ticker through mislabeled as a
    # NASDAQ symbol, since the bare `name` column has no exchange prefix at all.
    text = str(raw_ticker or "").strip().upper()
    if ":" not in text:
        return None
    exchange_text, symbol = text.split(":", 1)
    exchange_key = exchange_text.replace(" ", "")
    exchange_code = _SUPPORTED_EXCHANGES.get(exchange_key)
    if not exchange_code or not symbol:
        return None
    return {"symbol": symbol, "exchange_code": exchange_code}


async def check_connectivity(client: httpx.AsyncClient) -> bool:
    payload = {
        "filter": [
            {
                "left": "relative_volume_10d_calc",
                "operation": "greater",
                "right": 2.0,
            }
        ],
        "columns": ["name"],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 1],
        "markets": ["america"],
    }
    try:
        response = await client.post(
            _TV_URL,
            json=payload,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if response.status_code == 200:
            logger.info("[TV] connectivity_ok http=200")
            return True
        logger.warning("[TV] connectivity_failed http=%s", response.status_code)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] connectivity_failed error=%s", exc)
        return False


async def scan_top_volume_surge(
    client: httpx.AsyncClient,
    top_n: int = 30,
    min_rel_volume: float = 2.0,
    min_price_usd: float = 1.0,
    min_volume: int = 500_000,
    min_market_cap: float = 3e8,
    max_market_cap: float = 2e12,
    max_change_pct: float = 20.0,
) -> list[dict[str, str]]:
    payload = {
        "filter": [
            {
                "left": "relative_volume_10d_calc",
                "operation": "greater",
                "right": min_rel_volume,
            },
            {
                "left": "market_cap_basic",
                "operation": "in_range",
                "right": [min_market_cap, max_market_cap],
            },
            {
                "left": "close",
                "operation": "greater",
                "right": min_price_usd,
            },
            {
                "left": "volume",
                "operation": "greater",
                "right": min_volume,
            },
            {
                "left": "change",
                "operation": "in_range",
                "right": [-max_change_pct, max_change_pct],
            },
        ],
        "columns": [
            "name",
            "close",
            "volume",
            "relative_volume_10d_calc",
            "change",
            "market_cap_basic",
            "typespecs",
        ],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        # Over-fetch: OTC/preferred/other non-tradeable rows get dropped below,
        # so requesting exactly top_n would starve the pool after filtering.
        "range": [0, max(int(top_n) * 4, 40)],
        "markets": ["america"],
    }
    try:
        response = await client.post(
            _TV_URL,
            json=payload,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if response.status_code != 200:
            logger.warning("[TV] scan_failed http=%s", response.status_code)
            return []
        body: dict[str, Any] = response.json()
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in body.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            # `s` (e.g. "NASDAQ:AAPL") carries the real exchange; the `d`/"name"
            # column is a bare ticker with no exchange info at all.
            parsed = _parse_tv_symbol(item.get("s"))
            if parsed is None:
                continue
            data = item.get("d", []) or []
            typespecs = data[6] if len(data) > 6 else None
            if isinstance(typespecs, list) and any(
                spec in {"preferred", "warrant", "right", "unit"} for spec in typespecs
            ):
                continue
            symbol = parsed["symbol"].upper()
            if symbol in seen or "/" in symbol or "." in symbol:
                continue
            seen.add(symbol)
            results.append(parsed)
            if len(results) >= top_n:
                break
        logger.info("[TV] scan_complete count=%s", len(results))
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] scan_failed error=%s", exc)
        return []
