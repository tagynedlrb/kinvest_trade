from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta

import httpx

from .client import KisApiError, KisRestClient, MissingCredentialsError, parse_kis_number
from .auto_trader import FixedSymbolAutoTrader
from .config import AppConfig, load_app_config
from .indicators import summarize_indicators
from .liquidity_lab import LiquidityLabService
from .notifier import TelegramNotifier
from .paper import PaperTradingService
from .repository import SqliteRepository
from .telegram_control import TelegramLiquidityLabController
from .time_utils import format_display_times
from .watcher import ConsoleWatchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KIS short-term trading scaffold")
    parser.add_argument(
        "--settings",
        default="config/fixed_config.json",
        help="Path to JSON settings file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "auto-run",
        help=(
            "Run a fixed single-symbol strategy on the symbol configured in "
            "auto_trade.symbol (no candidate scanning; same entry/exit logic "
            "as liquidity-lab but applied to one pinned symbol only)"
        ),
    )
    subparsers.add_parser(
        "liquidity-lab",
        help=(
            "Scan all candidates in liquidity_lab.domestic_candidates / "
            "overseas_candidates, automatically pick the most active symbol "
            "each cycle, and trade it (no fixed symbol)"
        ),
    )
    subparsers.add_parser(
        "telegram-control",
        help="Run a Telegram command daemon that controls the liquidity-lab loop",
    )
    subparsers.add_parser("doctor", help="Print configuration and safety status")
    subparsers.add_parser("auth-check", help="Issue token and verify pre-test readiness")
    subparsers.add_parser("balance-check", help="Inspect current account balance for the active profile")
    overseas_balance = subparsers.add_parser(
        "overseas-balance-check",
        help="Inspect overseas balance for the active profile and selected exchange/currency",
    )
    overseas_balance.add_argument("--exchange", default="NASD")
    overseas_balance.add_argument("--currency", default="USD")

    paper_run = subparsers.add_parser(
        "paper-run", help="Poll live KIS quotes and record virtual trades"
    )
    paper_run.add_argument("--iterations", type=int)
    paper_run.add_argument("--interval-sec", type=int)

    paper_report = subparsers.add_parser(
        "paper-report", help="Show summary for the latest or selected paper run"
    )
    paper_report.add_argument("--run-id", type=int)

    subparsers.add_parser("telegram-test", help="Send a test message to Telegram if configured")

    indicator = subparsers.add_parser(
        "indicator-check",
        help="Fetch KIS chart data and compute RSI/SMA without placing orders",
    )
    indicator.add_argument("stock_code")
    indicator.add_argument("--timeframe", choices=["minute", "daily"], default="minute")
    indicator.add_argument(
        "--base-date",
        default=datetime.now().strftime("%Y%m%d"),
        help="Target date for minute mode or end date for daily mode in YYYYMMDD",
    )
    indicator.add_argument(
        "--limit",
        type=int,
        default=30,
        help="How many bars to use from the API response",
    )

    orderable = subparsers.add_parser(
        "orderable-check",
        help="Check possible buy quantity/amount for a stock at the chosen price",
    )
    orderable.add_argument("stock_code")
    orderable.add_argument("--price", type=int, required=True)
    orderable.add_argument(
        "--order-division",
        default="01",
        help="KIS order division code. 01 is market order, 00 is limit order.",
    )

    order_test = subparsers.add_parser(
        "order-test",
        help="Preview or submit a domestic cash order for the active profile",
    )
    order_test.add_argument("side", choices=["buy", "sell"])
    order_test.add_argument("stock_code")
    order_test.add_argument("--qty", type=int, required=True)
    order_test.add_argument(
        "--price",
        type=int,
        default=0,
        help="Use 0 for market orders when order-division=01.",
    )
    order_test.add_argument(
        "--order-division",
        default="00",
        help="KIS order division code. 00 is limit order, 01 is market order.",
    )
    order_test.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the order instead of printing a preview.",
    )
    order_test.add_argument(
        "--confirm-live",
        default="",
        help="Required only for real-account submission. Must be EXECUTE_LIVE.",
    )

    overseas_price = subparsers.add_parser(
        "overseas-price-check",
        help="Fetch overseas quote and symbol metadata",
    )
    overseas_price.add_argument("symbol")
    overseas_price.add_argument("--exchange", default="AMEX")

    overseas_orderable = subparsers.add_parser(
        "overseas-orderable-check",
        help="Check possible overseas buy amount/quantity for a symbol",
    )
    overseas_orderable.add_argument("symbol")
    overseas_orderable.add_argument("--exchange", default="AMEX")
    overseas_orderable.add_argument("--price", required=True)

    overseas_order = subparsers.add_parser(
        "overseas-order-test",
        help="Preview or submit an overseas stock order for the active profile",
    )
    overseas_order.add_argument("side", choices=["buy", "sell"])
    overseas_order.add_argument("symbol")
    overseas_order.add_argument("--exchange", default="AMEX")
    overseas_order.add_argument("--qty", type=int, required=True)
    overseas_order.add_argument("--price", required=True)
    overseas_order.add_argument("--order-division", default="00")
    overseas_order.add_argument("--execute", action="store_true")
    overseas_order.add_argument("--confirm-live", default="")

    return parser


def print_doctor(config: AppConfig) -> None:
    print("== KIS Trade Doctor ==")
    print(f"project_root: {config.project_root}")
    print(f"active_env: {config.credentials.env}")
    print(f"active_profile: {config.credentials.profile_name}")
    print(f"settings_watchlist: {config.trading.watchlist}")
    print(f"market: {config.trading.market} ({config.trading.market_code})")
    print(f"base_url: {config.credentials.base_url}")
    print(f"websocket_url: {config.credentials.websocket_url}")
    print(f"dry_run: {config.credentials.dry_run}")
    print(f"live_trading_enabled: {config.credentials.live_trading_enabled}")
    print(f"db_path: {config.storage.db_path}")
    print(f"log_dir: {config.storage.log_dir}")
    print(f"runtime_state_path: {config.storage.runtime_state_path}")
    print(f"appkey_file: {config.credentials.appkey_path}")
    print(f"appsecret_file: {config.credentials.appsecret_path}")
    print(f"telegram_enabled: {config.notifications.telegram_enabled}")
    print(f"telegram_bot_token_file: {config.notifications.telegram_bot_token_path}")
    print(f"telegram_chat_id_file: {config.notifications.telegram_chat_id_path}")
    print(f"account_no_configured: {bool(config.credentials.account_no)}")
    print(f"account_product_code_configured: {bool(config.credentials.account_product_code)}")
    if config.credentials.account_no:
        print(
            "account_masked: "
            f"{config.credentials.account_no[:4]}...{config.credentials.account_no[-2:]}"
        )

    warnings: list[str] = []
    if not config.credentials.appkey:
        warnings.append("Active profile appkey is empty")
    if not config.credentials.appsecret:
        warnings.append("Active profile appsecret is empty")
    if not config.credentials.account_no:
        warnings.append("Active profile account number is empty")
    if not config.credentials.account_product_code:
        warnings.append("Active profile account product code is empty")
    if config.credentials.live_trading_enabled and config.credentials.dry_run:
        warnings.append("LIVE_TRADING_ENABLED=true but DRY_RUN is still true")
    if config.notifications.telegram_enabled and (
        not config.notifications.telegram_bot_token or not config.notifications.telegram_chat_id
    ):
        warnings.append("Telegram is enabled but bot token/chat id are missing")
    if config.credentials.env != "prod" and config.credentials.live_trading_enabled:
        warnings.append("LIVE_TRADING_ENABLED is ignored in paper mode")

    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"- {warning}")
    else:
        print("warnings: none")


async def run_auth_check(config: AppConfig) -> None:
    try:
        async with KisRestClient(config.credentials) as client:
            token = await client.ensure_token()
    except MissingCredentialsError as exc:
        print(
            json.dumps(
                {
                    "token_ok": False,
                    "ready_for_live_test": False,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(
        json.dumps(
            {
                "token_ok": bool(token),
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                "base_url": config.credentials.base_url,
                "account_configured": bool(config.credentials.account_no),
                "account_product_code_configured": bool(config.credentials.account_product_code),
                "ready_for_live_test": bool(
                    token
                    and config.credentials.account_no
                    and config.credentials.account_product_code
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_balance_check(config: AppConfig) -> None:
    async with KisRestClient(config.credentials) as client:
        balance = await client.get_balance()

    print(
        json.dumps(
            {
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                "account_masked": balance["account_masked"],
                "position_count": balance["position_count"],
                "summary": balance["summary"],
                "positions": balance["positions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_orderable_check(
    config: AppConfig,
    stock_code: str,
    price: int,
    order_division: str,
) -> None:
    async with KisRestClient(config.credentials) as client:
        result = await client.get_possible_order(
            stock_code=stock_code,
            price=price,
            order_division=order_division,
        )

    print(
        json.dumps(
            {
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_order_test(
    config: AppConfig,
    side: str,
    stock_code: str,
    qty: int,
    price: int,
    order_division: str,
    execute: bool,
    confirm_live: str,
) -> None:
    preview = {
        "env": config.credentials.env,
        "profile": config.credentials.profile_name,
        "side": side.upper(),
        "stock_code": stock_code,
        "qty": qty,
        "price": price,
        "order_division": order_division,
        "dry_run": config.credentials.dry_run,
        "live_trading_enabled": config.credentials.live_trading_enabled,
        "execute_requested": execute,
    }

    if qty <= 0:
        raise KisApiError("qty must be greater than zero")

    if not execute:
        print(
            json.dumps(
                {
                    **preview,
                    "submitted": False,
                    "reason": "preview_only",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if config.credentials.dry_run:
        raise KisApiError("DRY_RUN=true blocks order submission. Set DRY_RUN=false to submit.")

    if config.credentials.env == "prod":
        if not config.credentials.live_trading_enabled:
            raise KisApiError(
                "LIVE_TRADING_ENABLED=false blocks real-account submission."
            )
        if confirm_live != "EXECUTE_LIVE":
            raise KisApiError(
                "Real-account submission requires --confirm-live EXECUTE_LIVE."
            )

    async with KisRestClient(config.credentials) as client:
        response = await client.place_cash_order(
            side=side,
            stock_code=stock_code,
            qty=qty,
            price=price,
            order_division=order_division,
        )

    print(
        json.dumps(
            {
                **preview,
                "submitted": True,
                "response": response,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_overseas_balance_check(
    config: AppConfig,
    exchange_code: str,
    currency_code: str,
) -> None:
    async with KisRestClient(config.credentials) as client:
        balance = await client.get_overseas_balance(exchange_code, currency_code)

    print(
        json.dumps(
            {
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                **balance,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_overseas_price_check(
    config: AppConfig,
    symbol: str,
    exchange_code: str,
) -> None:
    async with KisRestClient(config.credentials) as client:
        quote = await client.get_overseas_price(symbol, exchange_code)
        try:
            info = await client.get_overseas_search_info(symbol, exchange_code)
        except KisApiError as exc:
            info = {"note": str(exc)}

    print(
        json.dumps(
            {
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "search_info": info,
                "quote": quote,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_overseas_orderable_check(
    config: AppConfig,
    symbol: str,
    exchange_code: str,
    price: str,
) -> None:
    async with KisRestClient(config.credentials) as client:
        result = await client.get_overseas_possible_order(symbol, exchange_code, price)

    print(
        json.dumps(
            {
                "env": config.credentials.env,
                "profile": config.credentials.profile_name,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_overseas_order_test(
    config: AppConfig,
    side: str,
    symbol: str,
    exchange_code: str,
    qty: int,
    price: str,
    order_division: str,
    execute: bool,
    confirm_live: str,
) -> None:
    preview = {
        "env": config.credentials.env,
        "profile": config.credentials.profile_name,
        "side": side.upper(),
        "symbol": symbol,
        "exchange_code": exchange_code,
        "qty": qty,
        "price": price,
        "order_division": order_division,
        "dry_run": config.credentials.dry_run,
        "live_trading_enabled": config.credentials.live_trading_enabled,
        "execute_requested": execute,
    }

    if qty <= 0:
        raise KisApiError("qty must be greater than zero")

    if not execute:
        print(
            json.dumps(
                {
                    **preview,
                    "submitted": False,
                    "reason": "preview_only",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if config.credentials.dry_run:
        raise KisApiError("DRY_RUN=true blocks order submission. Set DRY_RUN=false to submit.")

    if config.credentials.env == "prod":
        if not config.credentials.live_trading_enabled:
            raise KisApiError(
                "LIVE_TRADING_ENABLED=false blocks real-account submission."
            )
        if confirm_live != "EXECUTE_LIVE":
            raise KisApiError(
                "Real-account submission requires --confirm-live EXECUTE_LIVE."
            )

    async with KisRestClient(config.credentials) as client:
        response = await client.place_overseas_order_for_current_session(
            side=side,
            symbol=symbol,
            exchange_code=exchange_code,
            qty=qty,
            price=price,
            order_division=order_division,
        )

    print(
        json.dumps(
            {
                **preview,
                "submitted": True,
                "response": response,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_paper(config: AppConfig, iterations: int | None, interval_sec: int | None) -> None:
    repository = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications)

    async with KisRestClient(config.credentials) as client:
        service = PaperTradingService(config, client, repository, notifier)
        state = await service.run(iterations=iterations, interval_sec=interval_sec)

    print(
        json.dumps(
            {
                "run_id": state.run_id,
                "ending_cash_krw": state.cash_krw,
                "realized_pnl_krw": state.realized_pnl_krw,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_liquidity_lab(config: AppConfig) -> None:
    repository = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications)

    async with KisRestClient(config.credentials) as client:
        service = LiquidityLabService(config, client, repository, notifier)
        report = await service.run()

    print(json.dumps(format_display_times(report.to_dict()), ensure_ascii=False, indent=2))


async def run_telegram_control(config: AppConfig) -> None:
    repository = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications)
    controller = TelegramLiquidityLabController(config, repository, notifier)
    await controller.run()


async def run_auto_trade(config: AppConfig) -> None:
    if not config.auto_trade.enabled:
        raise KisApiError("auto_trade.enabled is false in config/fixed_config.json")
    if config.credentials.dry_run:
        raise KisApiError(
            "Auto trade requires DRY_RUN=false. Update .env before running python3 main.py."
        )
    if config.credentials.env == "prod" and not config.credentials.live_trading_enabled:
        raise KisApiError(
            "Auto trade on real account requires LIVE_TRADING_ENABLED=true."
        )

    repository = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications)
    async with KisRestClient(config.credentials) as client:
        service = FixedSymbolAutoTrader(config, client, repository, notifier)
        summary = await service.run()

    print(
        json.dumps(
            {
                "run_id": summary.run_id,
                "decision_count": summary.decision_count,
                "skip_count": summary.skip_count,
                "action_count": summary.action_count,
                "buy_count": summary.buy_count,
                "sell_count": summary.sell_count,
                "realized_pnl_gross_usd": summary.realized_pnl_usd,
                "realized_pnl_net_usd": summary.realized_pnl_net_usd,
                "realized_pnl_net_krw": summary.realized_pnl_net_krw,
                "estimated_tax_krw": summary.estimated_tax_krw,
                "fees_total_usd": summary.fees_total_usd,
                "fx_pnl_krw": summary.fx_pnl_krw,
                "last_price": summary.last_price,
                "final_position_qty": summary.final_position_qty,
                "completion_reason": summary.completion_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_indicator_check(
    config: AppConfig,
    stock_code: str,
    timeframe: str,
    base_date: str,
    limit: int,
) -> None:
    repository = SqliteRepository(config.storage.db_path)
    async with KisRestClient(config.credentials) as client:
        if timeframe == "minute":
            rows = await client.get_time_daily_chart(
                stock_code=stock_code,
                target_date=base_date,
                market_code=config.trading.market_code,
            )
            label = "minute"
            closes = [parse_kis_number(row.get("stck_prpr")) for row in rows[:limit]]
            volumes = [parse_kis_number(row.get("cntg_vol")) for row in rows[:limit]]
        else:
            start_date = (datetime.strptime(base_date, "%Y%m%d") - timedelta(days=120)).strftime("%Y%m%d")
            rows = await client.get_daily_chart(
                stock_code=stock_code,
                start_date=start_date,
                end_date=base_date,
                market_code=config.trading.market_code,
            )
            label = "daily"
            closes = [parse_kis_number(row.get("stck_clpr")) for row in rows[:limit]]
            volumes = [parse_kis_number(row.get("acml_vol")) for row in rows[:limit]]

    summary = summarize_indicators(closes, volumes)
    repository.save_indicator_check(
        stock_code=stock_code,
        timeframe=label,
        bar_count=summary.bar_count,
        last_close=summary.last_close,
        rsi14=summary.rsi14,
        sma5=summary.sma5,
        sma20=summary.sma20,
        volume_sum=summary.volume_sum,
        change_pct_from_oldest=summary.change_pct_from_oldest,
        raw_payload=rows[:limit],
    )

    print(
        json.dumps(
            format_display_times(
                {
                "stock_code": stock_code,
                "timeframe": label,
                "bar_count": summary.bar_count,
                "last_close": summary.last_close,
                "rsi14": summary.rsi14,
                "sma5": summary.sma5,
                "sma20": summary.sma20,
                "change_pct_from_oldest": summary.change_pct_from_oldest,
                "volume_sum": summary.volume_sum,
                "latest_bar": rows[0] if rows else None,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


async def run_telegram_test(config: AppConfig) -> None:
    notifier = TelegramNotifier(config.notifications)
    sent = await notifier.send(
        "[KIS][TELEGRAM_TEST]\nTelegram connection test from kinvest_trade."
    )
    print(json.dumps({"telegram_sent": sent}, ensure_ascii=False, indent=2))


async def run_watch_console(config: AppConfig) -> None:
    repository = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications)
    async with KisRestClient(config.credentials) as client:
        service = ConsoleWatchService(config, client, repository, notifier)
        await service.run()


def run_paper_report(config: AppConfig, run_id: int | None) -> None:
    repository = SqliteRepository(config.storage.db_path)
    target_run_id = run_id or repository.get_latest_paper_run_id()
    if target_run_id is None:
        print(json.dumps({"error": "no paper run history"}, ensure_ascii=False, indent=2))
        return

    run = repository.get_paper_run(target_run_id)
    if run is None:
        print(json.dumps({"error": "paper run not found"}, ensure_ascii=False, indent=2))
        return

    orders = repository.get_paper_orders(target_run_id)
    positions = repository.get_paper_positions(target_run_id)
    latest_quotes = repository.get_latest_quotes_for_run(target_run_id)

    open_position_value = 0
    unrealized_pnl = 0
    for position in positions:
        latest = latest_quotes.get(position["stock_code"])
        if latest is None:
            continue
        mark_price = int(latest["best_bid"])
        qty = int(position["qty"])
        avg_price = int(position["avg_price"])
        open_position_value += mark_price * qty
        unrealized_pnl += (mark_price - avg_price) * qty

    print(
        json.dumps(
            format_display_times(
                {
                "run_id": target_run_id,
                "status": run["status"],
                "mode": run["mode"],
                "started_at": run["started_at"],
                "ended_at": run["ended_at"],
                "starting_cash_krw": run["starting_cash_krw"],
                "ending_cash_krw": run["ending_cash_krw"],
                "realized_pnl_krw": run["realized_pnl_krw"],
                "unrealized_pnl_krw": unrealized_pnl,
                "open_position_value_krw": open_position_value,
                "order_count": len(orders),
                "buy_count": sum(1 for order in orders if order["side"] == "BUY"),
                "sell_count": sum(1 for order in orders if order["side"] == "SELL"),
                "open_positions": positions,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_app_config(args.settings)

    config.storage.log_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.command == "telegram-control":
            asyncio.run(run_telegram_control(config))
            return
        if args.command == "liquidity-lab":
            asyncio.run(run_liquidity_lab(config))
            return
        if args.command == "auto-run":
            asyncio.run(run_auto_trade(config))
            return

        if args.command == "doctor":
            print_doctor(config)
            return

        if args.command == "auth-check":
            asyncio.run(run_auth_check(config))
            return

        if args.command == "balance-check":
            asyncio.run(run_balance_check(config))
            return

        if args.command == "overseas-balance-check":
            asyncio.run(run_overseas_balance_check(config, args.exchange, args.currency))
            return

        if args.command == "paper-run":
            asyncio.run(run_paper(config, args.iterations, args.interval_sec))
            return

        if args.command == "paper-report":
            run_paper_report(config, args.run_id)
            return

        if args.command == "indicator-check":
            asyncio.run(
                run_indicator_check(
                    config,
                    args.stock_code,
                    args.timeframe,
                    args.base_date,
                    args.limit,
                )
            )
            return

        if args.command == "orderable-check":
            asyncio.run(
                run_orderable_check(
                    config,
                    args.stock_code,
                    args.price,
                    args.order_division,
                )
            )
            return

        if args.command == "order-test":
            asyncio.run(
                run_order_test(
                    config,
                    args.side,
                    args.stock_code,
                    args.qty,
                    args.price,
                    args.order_division,
                    args.execute,
                    args.confirm_live,
                )
            )
            return

        if args.command == "overseas-price-check":
            asyncio.run(
                run_overseas_price_check(
                    config,
                    args.symbol,
                    args.exchange,
                )
            )
            return

        if args.command == "overseas-orderable-check":
            asyncio.run(
                run_overseas_orderable_check(
                    config,
                    args.symbol,
                    args.exchange,
                    args.price,
                )
            )
            return

        if args.command == "overseas-order-test":
            asyncio.run(
                run_overseas_order_test(
                    config,
                    args.side,
                    args.symbol,
                    args.exchange,
                    args.qty,
                    args.price,
                    args.order_division,
                    args.execute,
                    args.confirm_live,
                )
            )
            return

        if args.command == "telegram-test":
            asyncio.run(run_telegram_test(config))
            return
    except (KisApiError, MissingCredentialsError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:1000]
        print(
            json.dumps(
                {
                    "error": f"http_status={exc.response.status_code}",
                    "body": body,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
