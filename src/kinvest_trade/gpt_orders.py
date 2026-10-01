"""Explicit, expiring owner limit orders for the paper account only."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import account_fingerprint, load_app_config
from .gpt_operations import operation_guard, service_command
from .telegram_gpt import bridge_root


def paper_guard(config):
    c = config.credentials
    identity = account_fingerprint(c)
    if c.env != "vps" or c.live_trading_enabled or c.dry_run or not identity:
        raise ValueError("enabled_paper_account_required")
    return identity


def parse_order(text: str) -> dict:
    parts = text.upper().split()
    if len(parts) not in {5, 6}:
        raise ValueError("/gpt_order KR|US BUY|SELL 종목 수량 지정가 [NASD|NYSE|AMEX]")
    market, side, symbol, qty, price = parts[:5]
    if market not in {"KR", "US"} or side not in {"BUY", "SELL"}:
        raise ValueError("invalid_market_or_side")
    if not re.fullmatch(r"[0-9]{6}" if market == "KR" else r"[A-Z][A-Z0-9.]{0,9}", symbol):
        raise ValueError("invalid_symbol")
    if not re.fullmatch(r"[0-9]{1,2}", qty) or not 1 <= int(qty) <= 10:
        raise ValueError("quantity_one_to_ten_required")
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", price):
        raise ValueError("finite_positive_limit_price_required")
    try:
        value = Decimal(price)
    except InvalidOperation:
        raise ValueError("invalid_limit_price") from None
    if value <= 0 or market == "KR" and value != value.to_integral_value():
        raise ValueError("invalid_limit_price")
    notional = value * int(qty)
    if notional > (100000 if market == "KR" else 500):
        raise ValueError("manual_order_cap_KRW100000_USD500")
    exchange = parts[5] if len(parts) == 6 else ("KRX" if market == "KR" else "NASD")
    if exchange not in ({"KRX"} if market == "KR" else {"NASD", "NYSE", "AMEX"}):
        raise ValueError("invalid_exchange")
    return {"market": "domestic" if market == "KR" else "overseas", "side": side.lower(),
            "symbol": symbol, "qty": int(qty), "price": str(value), "notional": str(notional),
            "exchange": exchange}


def guard_order_job(config, job, order):
    identity = paper_guard(config)
    if order["account_fingerprint"] != identity:
        raise ValueError("account_changed_reapprove")
    if not 0 <= time.time() - job["created_at"] <= 60:
        raise ValueError("order_approval_expired_60_seconds")
    checked = parse_order(" ".join(["KR" if order["market"] == "domestic" else "US",
        order["side"], order["symbol"], str(order["qty"]), order["price"], order["exchange"]]))
    if any(order[key] != value for key, value in checked.items()):
        raise ValueError("order_payload_changed")


async def preflight(config, repository, client, notifier, order):
    from .liquidity_lab import LiquidityLabService
    from .market_sessions import KST, NEW_YORK
    from .paper_execution_check import PaperExecutionCheck
    from .telegram_control import TelegramLiquidityLabController
    market, symbol, exchange = order["market"], order["symbol"], order["exchange"]
    date = datetime.now(timezone.utc).astimezone(KST if market == "domestic" else NEW_YORK).date().isoformat()
    check = PaperExecutionCheck(config, repository, client, market=market, symbol=symbol, session_date=date)
    check.exchange = exchange
    check.guard()
    await check.history()
    service = LiquidityLabService(config, client, repository, notifier)
    # Restore account risk state without creating a second polling controller.
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.config = config
    runtime = json.loads(config.storage.runtime_state_path.read_text())
    if runtime.get("linked_account_fingerprint") != paper_guard(config):
        raise ValueError("controller_account_identity_mismatch")
    controller._restored_lab_runtime_state = runtime.get("lab_runtime_state", {})
    controller._apply_restored_lab_runtime_state(service)
    service._reconcile_confirmed_risk_day_pnl()
    if order["side"] == "buy" and (service._is_trading_halted(market) or service._is_order_reject_halted(market=market, side="BUY")):
        raise ValueError("existing_account_risk_halt")
    if market == "domestic":
        open_orders = await service._list_open_domestic_orders(symbol=symbol)
        balance = await client.get_balance()
        key, quantity_key, average_key = "pdno", "hldg_qty", "pchs_avg_pric"
        quote = await client.get_orderbook(symbol)
        reference = Decimal(str(quote.get("best_ask" if order["side"] == "buy" else "best_bid") or 0))
    else:
        open_orders = await service._list_open_overseas_orders(symbol=symbol, exchange_code=exchange)
        balance = await client.get_overseas_balance(exchange, "USD")
        key, quantity_key, average_key = "ovrs_pdno", "ovrs_cblc_qty", "pchs_avg_pric"
        quote = await client.get_overseas_price(symbol, exchange)
        reference = Decimal(str(quote.get("last_price") or 0))
    if open_orders or str(balance.get("tr_cont", "")).strip() in {"M", "F"}:
        raise ValueError("open_orders_or_incomplete_balance")
    price = Decimal(order["price"])
    if not reference.is_finite() or reference <= 0 or abs(price / reference - 1) > Decimal("0.005"):
        raise ValueError("limit_outside_0_5_percent_of_quote")
    held = [row for row in balance.get("positions", []) if str(row.get(key, "")).upper() == symbol]
    quantity = sum(int(Decimal(str(row.get(quantity_key) or 0))) for row in held)
    sellable = sum(int(Decimal(str(row.get("ord_psbl_qty") or 0))) for row in held)
    if order["side"] == "sell" and (quantity < order["qty"] or sellable < order["qty"]):
        raise ValueError("insufficient_sellable_quantity_no_short_selling")
    if order["side"] == "buy":
        if quantity:
            raise ValueError("manual_scale_in_not_supported")
        possible = (await client.get_possible_order(symbol, int(price), order_division="00") if market == "domestic" else
                    await client.get_overseas_possible_order(symbol, exchange, str(price)))
        available = possible.get("nrcvb_buy_qty" if market == "domestic" else "max_order_quantity")
        if int(Decimal(str(available or 0))) < order["qty"]:
            raise ValueError("insufficient_buying_power")
    entry_price = float(held[0].get(average_key) or 0) if held else None
    return service, check, entry_price


def pause_uncertain(project):
    path = project / "state/runtime_state.json"
    state = json.loads(path.read_text())
    state.setdefault("telegram_control", {})["mode"] = "paused"
    temporary = path.with_suffix(".gpt.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.replace(path)


def write_order_marker(marker, payload):
    temporary = marker.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(payload, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(marker)
    directory = os.open(marker.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def execute_order(project, settings, store, job, notifier):
    from .client import KisRestClient
    from .repository import SqliteRepository
    operation_guard(settings, "order")
    config = load_app_config()
    order = json.loads(job["payload_json"])
    guard_order_job(config, job, order)
    marker = bridge_root(project) / "uncertain_order.json"
    if marker.exists():
        raise ValueError("previous_order_uncertain_manual_reconciliation_required")
    repository = SqliteRepository.__new__(SqliteRepository)
    repository.db_path = project / "data/trading.db"
    def log_api(data):
        repository.save_api_call(created_at=datetime.now(timezone.utc).isoformat(),
                                 **{**data, "client_source": "telegram_gpt_order"})
    # Fail closed before stopping the controller when the market is closed.
    async with KisRestClient(config.credentials, on_api_call=log_api) as client:
        await preflight(config, repository, client, notifier, order)
        store.begin_side_effect(job["id"])
        try:
            await asyncio.to_thread(service_command, "stop")
            guard_order_job(config, job, order)
            service, check, entry_price = await preflight(config, repository, client, notifier, order)
            guard_order_job(config, job, order)
            check.guard()
            write_order_marker(marker, {"job_id": job["id"], "order": order, "status": "POST_MAY_HAVE_REACHED_BROKER"})
            repository.save_event(event_type="gpt_manual_order_attempt", market=order["market"],
                                  symbol=order["symbol"], detail={"job_id": job["id"], **order})
            if order["market"] == "domestic":
                response = await client.place_cash_order(side=order["side"], stock_code=order["symbol"],
                    qty=order["qty"], price=int(Decimal(order["price"])), order_division="00")
            else:
                response = await client.place_overseas_order_for_current_session(side=order["side"],
                    symbol=order["symbol"], exchange_code=order["exchange"], qty=order["qty"],
                    price=order["price"], order_division="00")
            if str(response.get("rt_cd")) != "0":
                raise ValueError("broker_submission_not_confirmed")
            execution = service._record_broker_order_event(
                market=order["market"], symbol=order["symbol"], exchange_code=order["exchange"],
                side=order["side"].upper(), order_kind="limit", requested_qty=order["qty"],
                requested_price=float(order["price"]), strategy_flag="MANUAL_GPT", entry_by="MANUAL_GPT",
                exit_by="MANUAL_GPT" if order["side"] == "sell" else "", status="SUBMITTED",
                reason="owner_confirmed_manual_order", payload={"response": response},
                execution_context={"gpt_job_id": job["id"], "is_session_trade": 0, "manual_order": True,
                                   "entry_price": entry_price, "orderable_qty": order["qty"],
                                   "stock_name": order["symbol"], "reference_price": float(order["price"])})
            if not execution:
                raise ValueError("order_tracking_not_persisted")
            marker.unlink()
            try:
                await service._reconcile_broker_executions(datetime.now(timezone.utc), force=True)
            except Exception:
                repository.save_event(event_type="gpt_manual_order_reconcile_deferred", market=order["market"],
                                      symbol=order["symbol"], detail={"job_id": job["id"], "execution_id": execution["id"]})
            return (f"모의 주문 접수: {order['market']} {order['side'].upper()} {order['symbol']} "
                    f"{order['qty']}주 @ {order['price']}\n추적번호={execution['id']} / MANUAL_GPT\n"
                    "접수는 체결이 아닙니다. /lab_orders에서 확정 체결 상태 확인. 자동 감시를 재개합니다.")
        finally:
            try:
                if marker.exists():
                    pause_uncertain(project)
                    await notifier.send(f"[GPT #{job['id']}] 주문 결과 불확실. 자동 재전송하지 않습니다. "
                                        "자동매매를 일시정지했습니다. 브로커 주문내역 확인이 필요합니다.")
            finally:
                await asyncio.to_thread(service_command, "start")
