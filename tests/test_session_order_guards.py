import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kinvest_trade.client import KisApiError, KisRestClient


def broker(env):
    client = object.__new__(KisRestClient)
    client.credentials = SimpleNamespace(env=env)
    client.account_parts = Mock(return_value=("test", "01"))
    client._request = AsyncMock(return_value={"rt_cd": "0"})
    client.place_overseas_order = AsyncMock(return_value={"route": "standard"})
    client.place_overseas_daytime_order = AsyncMock(return_value={"route": "daytime"})
    return client


@pytest.mark.parametrize("venue,division", [
    ("NXT", "00"), ("SOR", "00"), ("KRX", "05"), ("KRX", "06"),
    ("KRX", "07"), ("KRX", "27"), ("KRX", "28"), ("KRX", "29"),
    *[("KRX", str(code)) for code in range(41, 48)],
])
def test_mock_rejects_unsupported_domestic_orders_before_post(venue, division):
    client = broker("vps")
    with pytest.raises(KisApiError, match="KRX regular"):
        asyncio.run(client.place_cash_order("buy", "229200", 1, 15000, exchange_code=venue, order_division=division))
    client._request.assert_not_called()


@pytest.mark.parametrize("env,prefix", [("vps", "V"), ("prod", "T")])
@pytest.mark.parametrize("side,suffix", [("buy", "0012U"), ("sell", "0011U")])
def test_domestic_regular_order_preserves_account_tr_and_quantity(env, prefix, side, suffix):
    client = broker(env)
    asyncio.run(client.place_cash_order(side, "229200", 1, 15000))
    args, kwargs = client._request.call_args
    assert args[2] == prefix + "TTC" + suffix
    assert kwargs["body"]["EXCG_ID_DVSN_CD"] == "KRX"
    assert kwargs["body"]["ORD_QTY"] == "1"
    assert kwargs["body"]["ORD_DVSN"] == "00"


def test_invalid_side_does_not_default_to_sell():
    client = broker("vps")
    with pytest.raises(KisApiError, match="side"):
        asyncio.run(client.place_cash_order("typo", "229200", 1, 15000))
    client._request.assert_not_called()


@pytest.mark.parametrize("env,clock,route", [
    ("vps", "10:00:00", None), ("vps", "18:00:00", None),
    ("vps", "23:00:00", "standard"), ("vps", "06:00:00", None),
    ("prod", "10:00:00", "daytime"), ("prod", "18:00:00", "standard"),
    ("prod", "23:00:00", "standard"), ("prod", "06:00:00", "standard"),
    ("prod", "08:00:00", None), ("vps", "08:00:00", None),
])
def test_us_orders_route_only_to_supported_account_session(env, clock, route):
    client = broker(env)
    now = datetime.fromisoformat(f"2026-10-01T{clock}+09:00")
    order = client.place_overseas_order_for_current_session("buy", "AAPL", "NASD", 1, "100", now_utc=now)
    if route is None:
        with pytest.raises(KisApiError):
            asyncio.run(order)
        client.place_overseas_order.assert_not_called()
        client.place_overseas_daytime_order.assert_not_called()
    else:
        assert asyncio.run(order) == {"route": route}
        assert client.place_overseas_order.await_count + client.place_overseas_daytime_order.await_count == 1
    client._request.assert_not_called()
