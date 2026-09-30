from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path

from .client import KisRestClient
from .config import load_app_config
from .execution_reconciler import BrokerExecutionReconciler
from .market_calendar import is_krx_holiday, is_nyse_holiday
from .market_sessions import (
    KST,
    NEW_YORK,
    is_krx_regular_session,
    is_us_regular_session,
)
from .notifier import TelegramNotifier
from .repository import SqliteRepository


SERVICE = "kinvest-telegram-control.service"
TERMINAL = {"FILLED", "CANCELED", "PARTIAL_CANCELED", "REJECTED"}


class PaperExecutionCheck:
    """One-share broker plumbing check, deliberately outside strategy P&L."""

    def __init__(
        self, config, repository, client, *, market, symbol, session_date, timeout=90
    ):
        self.config, self.repository, self.client = config, repository, client
        self.market, self.symbol, self.session_date = market, symbol, session_date
        self.exchange = "NASD" if market == "overseas" else "KRX"
        self.timeout = timeout
        self.reconciler = BrokerExecutionReconciler(self)
        self.orders: list[dict] = []
        self.result = {
            "market": market,
            "symbol": symbol,
            "session_date": session_date,
            "environment": "vps",
            "qty": 1,
            "strategy_profit_evidence": False,
            "status": "NOT_STARTED",
            "safe_to_resume": True,
            "orders": self.orders,
        }

    def guard(self):
        c = self.config.credentials
        if c.env != "vps" or c.dry_run or c.live_trading_enabled:
            raise ValueError("enabled_paper_account_required")
        now = datetime.now(timezone.utc)
        tz = NEW_YORK if self.market == "overseas" else KST
        if now.astimezone(tz).date().isoformat() != self.session_date:
            raise ValueError("requested_session_date_mismatch")
        regular = (
            is_us_regular_session(now)
            if self.market == "overseas"
            else is_krx_regular_session(now)
        )
        holiday = (
            is_nyse_holiday(now.astimezone(tz).date())
            if self.market == "overseas"
            else is_krx_holiday(now.astimezone(tz).date())
        )
        if not regular or holiday:
            raise ValueError("regular_trading_session_required")

    def record(self, kind, detail):
        self.repository.save_event(
            event_type=f"paper_execution_check_{kind}",
            market=self.market,
            symbol=self.symbol,
            detail=detail,
        )

    async def history(self):
        date = self.session_date.replace("-", "")
        kwargs = dict(symbol=self.symbol, start_date=date, end_date=date)
        if self.market == "overseas":
            data = await self.client.get_overseas_order_history(
                **kwargs, exchange_code=self.exchange
            )
        else:
            data = await self.client.get_domestic_order_history(**kwargs)
        if str(data.get("tr_cont", "")).strip() in {"M", "F"}:
            raise ValueError("incomplete_order_history")
        return data.get("orders", [])

    async def quantity(self):
        if self.market == "overseas":
            data = await self.client.get_overseas_balance(self.exchange, "USD")
            symbol_key, qty_key = "ovrs_pdno", "ovrs_cblc_qty"
        else:
            data = await self.client.get_balance()
            symbol_key, qty_key = "pdno", "hldg_qty"
        return sum(
            int(float(row.get(qty_key) or 0))
            for row in data.get("positions", [])
            if str(row.get(symbol_key, "")).upper() == self.symbol
        )

    async def price(self, side):
        if self.market == "overseas":
            quote = await self.client.get_overseas_price(self.symbol, self.exchange)
            last = float(quote.get("last_price") or 0)
            if not math.isfinite(last) or not 5 <= last <= 500:
                raise ValueError("diagnostic_notional_out_of_bounds")
            factor = Decimal("1.002") if side == "buy" else Decimal("0.998")
            return str(
                (Decimal(str(last)) * factor).quantize(
                    Decimal("0.01"), rounding=ROUND_UP
                )
            )
        quote = await self.client.get_orderbook(self.symbol)
        ask, bid = float(quote.get("best_ask") or 0), float(quote.get("best_bid") or 0)
        if (
            not all(math.isfinite(v) for v in (ask, bid))
            or not 0 < bid <= ask <= 100_000
        ):
            raise ValueError("diagnostic_quote_out_of_bounds")
        if (ask - bid) / ask > 0.003:
            raise ValueError("diagnostic_spread_too_wide")
        return int(ask if side == "buy" else bid)

    async def submit(self, side):
        self.guard()
        price = await self.price(side)
        if side == "buy":
            if self.market == "overseas":
                possible = await self.client.get_overseas_possible_order(
                    self.symbol, self.exchange, str(price)
                )
                available = possible.get("max_order_quantity")
            else:
                possible = await self.client.get_possible_order(
                    self.symbol, int(price), order_division="00"
                )
                available = possible.get("nrcvb_buy_qty")
            if int(float(available or 0)) < 1:
                raise ValueError("diagnostic_buying_power_unavailable")
        self.result["safe_to_resume"] = False
        self.record(
            "submission_attempt",
            {"side": side, "qty": 1, "price": price, "run_key": self.result["run_key"]},
        )
        if self.market == "overseas":
            response = await self.client.place_overseas_order_for_current_session(
                side=side,
                symbol=self.symbol,
                exchange_code=self.exchange,
                qty=1,
                price=str(price),
                order_division="00",
            )
        else:
            response = await self.client.place_cash_order(
                side=side,
                stock_code=self.symbol,
                qty=1,
                price=int(price),
                order_division="00",
            )
        output = response.get("output", {})
        no = str(output.get("ODNO") or output.get("odno") or "")
        if not no or str(response.get("rt_cd")) != "0":
            raise ValueError("diagnostic_submission_not_confirmed")
        order = {
            "side": side,
            "order_no": no,
            "limit_price": price,
            "branch": output.get("KRX_FWDG_ORD_ORGNO")
            or output.get("krx_fwdg_ord_orgno")
            or "",
        }
        self.orders.append(order)
        self.record("submitted", {**order, "run_key": self.result["run_key"]})
        return order

    async def snapshot(self, order):
        rows, canceled = self.reconciler._index_history(
            self.market, await self.history()
        )
        no = self.repository.normalize_broker_order_no(order["order_no"])
        key = (self.session_date, no)
        row = rows.get(key) or rows.get(("", no))
        if row is None:
            return None
        return self.reconciler._execution_snapshot(
            self.market,
            {"requested_qty": 1},
            row,
            canceled=key in canceled or ("", no) in canceled,
        )

    async def cancel(self, order):
        if self.market == "overseas":
            await self.client.revise_or_cancel_overseas_order(
                symbol=self.symbol,
                exchange_code=self.exchange,
                original_order_no=order["order_no"],
                rvse_cncl_dvsn_cd="02",
                qty=1,
                price="0",
            )
        else:
            await self.client.revise_or_cancel_domestic_order(
                krx_order_orgno=order["branch"],
                original_order_no=order["order_no"],
                order_division="00",
                rvse_cncl_dvsn_cd="02",
                qty=0,
                price=0,
                qty_all_order_yn="Y",
            )
        self.record(
            "cancel_requested",
            {"order_no": order["order_no"], "run_key": self.result["run_key"]},
        )

    async def settle(self, order):
        # A cancellation acknowledgement alone is not evidence of a flat account.
        for cancel_phase, duration in ((False, self.timeout), (True, 30)):
            if cancel_phase:
                await self.cancel(order)
            deadline = time.monotonic() + duration
            while True:
                snapshot = await self.snapshot(order)
                if snapshot is not None and snapshot["status"] in TERMINAL:
                    order.update(snapshot)
                    self.record("settled", {**order, "run_key": self.result["run_key"]})
                    return snapshot
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(5)
        raise ValueError("diagnostic_order_state_uncertain")

    async def run(self):
        try:
            self.guard()
            fingerprint = hashlib.sha256(
                ":".join(self.client.account_parts()).encode()
            ).hexdigest()[:16]
            key = f"{fingerprint}:{self.market}:{self.session_date}:{self.symbol}"
            self.result["run_key"] = key
            for event in self.repository.list_event_log(
                event_type="paper_execution_check_started", limit=200
            ):
                detail = event.get("detail") or {}
                if isinstance(detail, str):
                    detail = json.loads(detail)
                if detail.get("run_key") == key:
                    self.result["safe_to_resume"] = False
                    raise ValueError("diagnostic_already_attempted")
            if await self.quantity() or await self.history():
                raise ValueError("diagnostic_symbol_not_clean")
            self.record("started", self.result)
            buy = await self.settle(await self.submit("buy"))
            if buy["filled_qty"] == 0:
                self.result["status"] = "NO_BUY_FILL"
                self.result["safe_to_resume"] = await self.quantity() == 0
                return self.result
            if buy["filled_qty"] != 1:
                raise ValueError("diagnostic_unexpected_fill_quantity")
            for _ in range(6):
                if await self.quantity() == 1:
                    break
                await asyncio.sleep(3)
            else:
                raise ValueError("diagnostic_buy_balance_unconfirmed")
            sell = await self.settle(await self.submit("sell"))
            if sell["filled_qty"] != 1:
                raise ValueError("diagnostic_sell_fill_unconfirmed")
            for _ in range(6):
                if await self.quantity() == 0:
                    break
                await asyncio.sleep(3)
            else:
                raise ValueError("diagnostic_flat_balance_unconfirmed")
            self.result.update(
                status="ROUNDTRIP_CONFIRMED", safe_to_resume=True, remaining_qty=0
            )
            return self.result
        except Exception as exc:
            self.result.update(status="FAILED", error_type=type(exc).__name__)
            # Avoid persisting API exception text, which can contain credentials.
            if isinstance(exc, ValueError):
                self.result["reason"] = str(exc)
            return self.result
        finally:
            self.result["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.record("result", self.result)


async def execute(args):
    config = load_app_config()
    repo = SqliteRepository(config.storage.db_path)
    notifier = TelegramNotifier(config.notifications, repository=repo)

    def log_api(data):
        repo.save_api_call(
            created_at=datetime.now(timezone.utc).isoformat(),
            **{**data, "client_source": "paper_execution_check"},
        )

    async with KisRestClient(config.credentials, on_api_call=log_api) as client:
        check = PaperExecutionCheck(
            config,
            repo,
            client,
            market=args.market,
            symbol=args.symbol,
            session_date=args.session_date,
        )
        if not args.execute:
            check.guard()
            return {"status": "PREVIEW", "market": args.market, "symbol": args.symbol}
        stopped_service = False
        try:
            check.guard()
        except ValueError:
            result = await check.run()
        else:
            if args.manage_service:
                subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)
                stopped_service = True
            elif (
                subprocess.run(
                    ["systemctl", "--user", "is-active", "--quiet", SERVICE]
                ).returncode
                == 0
            ):
                raise ValueError("stop_automatic_service_before_diagnostic")
            result = await check.run()
        if stopped_service and result["safe_to_resume"]:
            subprocess.run(["systemctl", "--user", "start", SERVICE], check=True)
            result["service_restarted"] = True
        elif not result["safe_to_resume"]:
            result["service_restarted"] = False
        if args.notify:
            result["git_commit"] = subprocess.check_output(
                ["git", "rev-parse", "--short=12", "HEAD"], text=True
            ).strip()
            message = (
                f"[모의계좌 주문 경로 검증] {args.market} {args.symbol}\n"
                f"Git: {result['git_commit']}\n"
                f"결과: {result['status']} / 사유: {result.get('reason', '')}\n"
                f"수량: 1주 / 잔고 0 확인: {result.get('remaining_qty') == 0}\n"
                f"자동 서비스 재개: {result.get('service_restarted', False)}\n"
                "진단 거래는 전략 성적 및 수익성 증거와 분리 기록했습니다."
            )
            result["telegram_sent"] = await notifier.send(message)
        repo.save_event(
            event_type="paper_execution_check_report",
            market=args.market,
            symbol=args.symbol,
            detail=result,
        )
        return result


def main():
    parser = argparse.ArgumentParser(
        description="VPS-only, one-share broker roundtrip diagnostic."
    )
    parser.add_argument("--market", choices=["domestic", "overseas"], required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--manage-service", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    lock_path = Path("state/paper_execution_check.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = asyncio.run(execute(args))
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] not in {"ROUNDTRIP_CONFIRMED", "PREVIEW"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
