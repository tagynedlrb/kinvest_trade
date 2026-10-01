import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kinvest_trade.paper_execution_check as module
from kinvest_trade.paper_execution_check import PaperExecutionCheck
from kinvest_trade.repository import SqliteRepository


class Broker:
    def __init__(self, market, mode="fill"):
        self.market, self.mode = market, mode
        self.orders = []
        self.qty = 0

    def account_parts(self):
        return "test", "01"

    async def get_overseas_price(self, *args):
        return {"last_price": "100"}

    async def get_orderbook(self, *args):
        return {"best_ask": 10000, "best_bid": 9990}

    async def get_overseas_possible_order(self, *args):
        return {"max_order_quantity": "1"}

    async def get_possible_order(self, *args, **kwargs):
        return {"nrcvb_buy_qty": 1}

    async def get_overseas_balance(self, *args):
        return {"positions": [{"ovrs_pdno": "TEST", "ovrs_cblc_qty": str(self.qty)}]}

    async def get_balance(self):
        return {"positions": [{"pdno": "TEST", "hldg_qty": str(self.qty)}]}

    async def place_overseas_order_for_current_session(self, **kwargs):
        return self.submit(kwargs)

    async def place_cash_order(self, **kwargs):
        return self.submit(kwargs)

    def submit(self, args):
        assert args["qty"] == 1
        if self.mode == "timeout":
            raise TimeoutError("unknown POST outcome")
        side = args["side"]
        no = str(len(self.orders) + 1)
        filled = self.mode != "pending" and not (
            self.mode == "sell_rejected" and side == "sell"
        )
        self.orders.append(
            {"no": no, "side": side, "filled": filled, "canceled": False}
        )
        if filled:
            self.qty += 1 if side == "buy" else -1
        return {"rt_cd": "0", "output": {"ODNO": no, "KRX_FWDG_ORD_ORGNO": "001"}}

    async def get_overseas_order_history(self, **kwargs):
        return {"orders": self.history(kwargs["start_date"])}

    async def get_domestic_order_history(self, **kwargs):
        return {"orders": self.history(kwargs["start_date"])}

    def history(self, day):
        rows = []
        for order in self.orders:
            base = {
                "ord_dt": day,
                "odno": order["no"],
                "ord_tmd": "100000",
                "rvse_cncl_dvsn": "00",
            }
            n = int(order["filled"])
            if self.market == "overseas":
                base.update(
                    ft_ord_qty="1",
                    ft_ccld_qty=str(n),
                    ft_ccld_unpr3="100",
                    ft_ccld_amt3=str(100 * n),
                    nccs_qty=str(1 - n),
                )
            else:
                base.update(
                    ord_qty="1",
                    tot_ccld_qty=str(n),
                    avg_prvs="10000",
                    tot_ccld_amt=str(10000 * n),
                    rmn_qty=str(1 - n),
                )
            rows.append(base)
            if order["canceled"]:
                rows.append(
                    {
                        "ord_dt": day,
                        "odno": "999",
                        "orgn_odno": order["no"],
                        "rvse_cncl_dvsn": "02",
                    }
                )
        return rows

    async def revise_or_cancel_overseas_order(self, **kwargs):
        self.cancel(kwargs["original_order_no"])

    async def revise_or_cancel_domestic_order(self, **kwargs):
        self.cancel(kwargs["original_order_no"])

    def cancel(self, no):
        for row in self.orders:
            if row["no"] == no:
                row["canceled"] = True


def make_check(tmp_path, monkeypatch, market, mode="fill", env="vps"):
    monkeypatch.setattr(module, "is_us_regular_session", lambda _now: True)
    monkeypatch.setattr(module, "is_krx_regular_session", lambda _now: True)
    monkeypatch.setattr(module, "is_krx_holiday", lambda _date: False)
    monkeypatch.setattr(module, "is_nyse_holiday", lambda _date: False)
    tz = module.NEW_YORK if market == "overseas" else module.KST
    date = datetime.now(timezone.utc).astimezone(tz).date().isoformat()
    repo = SqliteRepository(tmp_path / "trading.db")
    config = SimpleNamespace(
        credentials=SimpleNamespace(env=env, dry_run=False, live_trading_enabled=False)
    )
    broker = Broker(market, mode)
    check = PaperExecutionCheck(
        config, repo, broker, market=market, symbol="TEST", session_date=date, timeout=0
    )
    return check, broker, repo


@pytest.mark.parametrize("market", ["domestic", "overseas"])
def test_one_share_roundtrip_requires_two_confirmed_fills_and_flat_balance(
    tmp_path, monkeypatch, market
):
    check, broker, repo = make_check(tmp_path, monkeypatch, market)
    result = asyncio.run(check.run())
    assert result["status"] == "ROUNDTRIP_CONFIRMED"
    assert result["safe_to_resume"] and result["remaining_qty"] == 0
    assert [order["status"] for order in result["orders"]] == ["FILLED", "FILLED"]
    assert result["strategy_profit_evidence"] is False
    assert len(repo.query_cycle_log()) == 0
    assert broker.qty == 0
    assert repo.list_event_log(event_type="paper_execution_check_result")
    assert asyncio.run(check.run())["reason"] == "diagnostic_already_attempted"
    assert len(broker.orders) == 2


@pytest.mark.parametrize(
    "case",
    ["prod", "dry_run", "live_enabled", "closed", "holiday", "wrong_day", "existing"],
)
def test_guard_never_submits_when_environment_or_state_is_unsafe(
    tmp_path, monkeypatch, case
):
    check, broker, _ = make_check(tmp_path, monkeypatch, "domestic")
    if case == "prod":
        check.config.credentials.env = "prod"
    elif case == "dry_run":
        check.config.credentials.dry_run = True
    elif case == "live_enabled":
        check.config.credentials.live_trading_enabled = True
    elif case == "closed":
        monkeypatch.setattr(module, "is_krx_regular_session", lambda _now: False)
    elif case == "holiday":
        monkeypatch.setattr(module, "is_krx_holiday", lambda _date: True)
    elif case == "wrong_day":
        check.session_date = "2000-01-01"
    else:
        broker.qty = 1
    result = asyncio.run(check.run())
    assert result["status"] == "FAILED"
    assert broker.orders == []


@pytest.mark.parametrize("market", ["domestic", "overseas"])
def test_no_fill_cancels_only_own_order_and_never_sells(tmp_path, monkeypatch, market):
    check, broker, _ = make_check(tmp_path, monkeypatch, market, "pending")
    result = asyncio.run(check.run())
    assert result["status"] == "NO_BUY_FILL"
    assert result["safe_to_resume"]
    assert len(broker.orders) == 1 and broker.orders[0]["canceled"]


def test_unknown_submission_is_never_retried_and_blocks_service_restart(
    tmp_path, monkeypatch
):
    check, broker, _ = make_check(tmp_path, monkeypatch, "overseas", "timeout")
    result = asyncio.run(check.run())
    assert result["status"] == "FAILED"
    assert not result["safe_to_resume"]
    assert result["error_type"] == "TimeoutError"
    assert broker.orders == []


def test_unsold_diagnostic_position_blocks_service_restart(tmp_path, monkeypatch):
    check, broker, _ = make_check(tmp_path, monkeypatch, "domestic", "sell_rejected")
    result = asyncio.run(check.run())
    assert result["status"] == "FAILED"
    assert not result["safe_to_resume"]
    assert result["reason"] == "diagnostic_sell_fill_unconfirmed"
    assert broker.qty == 1


@pytest.mark.parametrize("market,quote", [
    ("domestic", {"best_ask": 109005, "best_bid": 109000}),
    ("overseas", {"last_price": 500}),
])
def test_notional_rejection_is_not_a_bad_quote_and_does_not_consume_attempt(
    tmp_path, monkeypatch, market, quote
):
    check, broker, repo = make_check(tmp_path, monkeypatch, market)
    method = "get_orderbook" if market == "domestic" else "get_overseas_price"
    monkeypatch.setattr(broker, method, AsyncMock(return_value=quote))
    result = asyncio.run(check.run())
    assert result["reason"] == "diagnostic_notional_out_of_bounds"
    assert result["safe_to_resume"] and not broker.orders
    assert not repo.list_event_log(event_type="paper_execution_check_started")
    assert repo.list_event_log(event_type="paper_execution_check_quote_checked")
    # A price increase beyond the entry cap must not prevent liquidation.
    assert float(asyncio.run(check.price("sell"))) > 0


@pytest.mark.parametrize("ask,bid", [(float("nan"), 1), (100, float("inf")), (100, 101), (0, 0), (1000.5, 1000)])
def test_invalid_domestic_quotes_never_submit(tmp_path, monkeypatch, ask, bid):
    check, broker, _ = make_check(tmp_path, monkeypatch, "domestic")
    monkeypatch.setattr(broker, "get_orderbook", AsyncMock(return_value={"best_ask": ask, "best_bid": bid}))
    result = asyncio.run(check.run())
    assert result["reason"] == "diagnostic_quote_out_of_bounds"
    assert not broker.orders


@pytest.mark.parametrize("clock", ["10:00:00", "18:00:00", "06:00:00"])
def test_paper_us_guard_requires_actual_regular_session(tmp_path, monkeypatch, clock):
    from kinvest_trade.market_sessions import is_us_regular_session
    check, broker, _ = make_check(tmp_path, monkeypatch, "overseas")
    now = datetime.fromisoformat(f"2026-10-01T{clock}+09:00")
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(module, "is_us_regular_session", is_us_regular_session)
    check.session_date = now.astimezone(module.NEW_YORK).date().isoformat()
    result = asyncio.run(check.run())
    assert result["reason"] == "regular_trading_session_required"
    assert not broker.orders
