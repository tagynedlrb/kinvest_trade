#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from kinvest_trade.client import KisRestClient
from kinvest_trade.config import load_app_config
from kinvest_trade.market_policy import get_market_auto_trade_config
from kinvest_trade.repository import (
    CONFIRMED_SELL_CYCLE_PREDICATE,
    SqliteRepository,
)


def _confirmed_domestic_symbols(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT symbol
            FROM cycle_log
            WHERE market = 'domestic'
              AND action_bias = 'SELL_REAL'
              AND COALESCE(entry_price, 0) > 0
              AND COALESCE(price, 0) > 0
              AND COALESCE(qty_executed, 0) > 0
              AND {CONFIRMED_SELL_CYCLE_PREDICATE}
            ORDER BY symbol
            """
        ).fetchall()
        return [str(row[0]).strip().upper() for row in rows if str(row[0]).strip()]
    finally:
        conn.close()


async def _load_product_types(
    settings_path: Path,
    symbols: list[str],
) -> dict[str, str]:
    config = load_app_config(settings_path)
    product_types: dict[str, str] = {}
    async with KisRestClient(config.credentials) as client:
        for symbol in symbols:
            quote = await client.get_current_price(
                symbol,
                config.trading.market_code,
            )
            product_type = str(quote.get("product_type") or "").strip()
            if product_type:
                product_types[symbol] = product_type
    return product_types


def _backup_evidence(backup_path: Path) -> dict[str, object]:
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    with sqlite3.connect(backup_path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(
            conn.execute("PRAGMA foreign_key_check").fetchall()
        )
    return {
        "path": str(backup_path),
        "sha256": digest,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "KIS 상품구분을 조회해 국내 확정 매도의 수수료·매도세를 재산정"
        )
    )
    parser.add_argument("db_path", type=Path)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("config/fixed_config.json"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="온라인 백업 후 cycle_log 확정 체결을 실제 보정",
    )
    args = parser.parse_args()

    symbols = _confirmed_domestic_symbols(args.db_path)
    product_types = await _load_product_types(args.settings, symbols)
    missing = sorted(set(symbols) - set(product_types))
    output: dict[str, object] = {
        "symbols": symbols,
        "product_types": product_types,
        "missing_product_types": missing,
        "applied": False,
    }
    if missing:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    if not args.apply:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    repository = SqliteRepository(args.db_path)
    backup_path = repository.backup_db("pre_domestic_product_tax_v2")
    config = load_app_config(args.settings)
    auto_trade = get_market_auto_trade_config(config, "domestic")
    result = repository.reconcile_domestic_sell_costs(
        product_types=product_types,
        commission_rate=float(auto_trade.domestic_commission_rate),
        stock_sell_tax_rate=float(auto_trade.domestic_sell_tax_rate),
    )
    idempotency_check = repository.reconcile_domestic_sell_costs(
        product_types=product_types,
        commission_rate=float(auto_trade.domestic_commission_rate),
        stock_sell_tax_rate=float(auto_trade.domestic_sell_tax_rate),
    )
    output.update(
        {
            "applied": True,
            "backup": _backup_evidence(backup_path),
            "result": result,
            "idempotency_check": idempotency_check,
        }
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not result["missing_product_type"] else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
