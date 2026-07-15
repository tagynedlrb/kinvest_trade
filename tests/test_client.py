from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from kinvest_trade.client import KisRestClient
from kinvest_trade.config import KisCredentials


@pytest.fixture(autouse=True)
def _reset_client_rate_limit_state():
    # _rate_limit_lock/_last_request_at are class attributes shared across
    # every KisRestClient instance in the process (2026-07-15 fix, so a
    # temporary admin-command client and the main loop's long-lived client
    # pace against the same clock). Reset them per test so test order and
    # wall-clock timing don't leak between tests in this file.
    KisRestClient._rate_limit_lock = None
    KisRestClient._last_request_at = 0.0
    yield
    KisRestClient._rate_limit_lock = None
    KisRestClient._last_request_at = 0.0


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeAsyncClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def request(self, method: str, url: str, headers: dict, params: dict | None, json: dict | None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        return self.responses.pop(0)

    async def post(self, url: str, headers: dict, json: dict | None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "params": None,
                "json": json,
            }
        )
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self) -> None:
        return None


def test_request_reissues_token_after_expired_token_response(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)
    fake_http = FakeAsyncClient(
        [
            FakeResponse(500, {"msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}),
            FakeResponse(200, {"rt_cd": "0", "output": {"value": "ok"}}),
        ]
    )
    client._client = fake_http

    tokens = iter(["expired-token", "fresh-token"])

    async def fake_ensure_token() -> str:
        return next(tokens)

    client.ensure_token = fake_ensure_token  # type: ignore[method-assign]
    client._token = "expired-token"
    client._expires_at = 9999999999.0
    credentials.token_cache_path.write_text('{"access_token":"expired-token","expires_at":9999999999}', encoding="utf-8")

    payload = asyncio.run(client._request("GET", "/test", "TRTEST", params={"a": "1"}))

    assert payload["output"]["value"] == "ok"
    assert [call["headers"]["authorization"] for call in fake_http.calls] == [
        "Bearer expired-token",
        "Bearer fresh-token",
    ]
    assert credentials.token_cache_path.exists() is False


def test_request_reports_api_calls_via_on_api_call_hook(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    calls: list[dict] = []
    client = KisRestClient(credentials, on_api_call=calls.append)
    fake_http = FakeAsyncClient(
        [
            FakeResponse(500, {"msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0", "msg1": "정상", "output": {"value": "ok"}}),
        ]
    )
    client._client = fake_http
    client.ensure_token = lambda: asyncio.sleep(0, result="tok")  # type: ignore[method-assign]

    payload = asyncio.run(client._request("GET", "/test", "TRTEST", params={"a": "1"}))

    assert payload["output"]["value"] == "ok"
    assert len(calls) == 2
    assert calls[0]["success"] is False
    assert calls[0]["http_status"] == 500
    assert calls[0]["msg_cd"] == "EGW00201"
    assert calls[0]["tr_id"] == "TRTEST"
    assert calls[0]["path"] == "/test"
    assert calls[0]["method"] == "GET"
    assert isinstance(calls[0]["elapsed_ms"], int)
    assert calls[1]["success"] is True
    assert calls[1]["http_status"] == 200
    # None of the logged fields ever carry account number or credentials.
    for call in calls:
        serialized = str(call)
        assert credentials.account_no not in serialized
        assert credentials.appkey not in serialized
        assert credentials.appsecret not in serialized


def _make_paced_test_client(credentials: KisCredentials) -> KisRestClient:
    client = KisRestClient(credentials)
    client.ensure_token = lambda: asyncio.sleep(0, result="tok")  # type: ignore[method-assign]
    client._client = FakeAsyncClient(
        [
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0", "msg1": "정상", "output": {}}),
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0", "msg1": "정상", "output": {}}),
        ]
    )
    return client


def test_consecutive_requests_are_paced_to_avoid_rate_limit(tmp_path: Path) -> None:
    # Regression test: back-to-back calls on the same client (e.g. several
    # domestic buy candidates submitted in one cycle, each now also doing a
    # pending-order lookup first) used to fire with no pacing at all, which in
    # production repeatedly tripped KIS's per-second call limit (EGW00201) and
    # a correlated "malformed body" error (IGW00007) on the same tr_id even
    # though the request body itself was fine both before and after. Every
    # call through this client must now be spaced by at least
    # _min_request_interval_sec.
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = _make_paced_test_client(credentials)

    async def run_two_calls() -> float:
        start = time.monotonic()
        await client._request("GET", "/a", "TR1")
        await client._request("GET", "/b", "TR2")
        return time.monotonic() - start

    elapsed = asyncio.run(run_two_calls())

    assert elapsed >= client._min_request_interval_sec


def test_pacing_is_shared_across_separate_client_instances(tmp_path: Path) -> None:
    # Regression (2026-07-15): the throttle used to live on `self`, so a
    # temporary KisRestClient opened by an admin command (/lab_portfolio,
    # /lab_status, gitlog upload, ...) paced itself independently of the main
    # loop's long-lived client. Two separate instances each individually
    # honoring _min_request_interval_sec could still combine to exceed KIS's
    # real per-account limit -- in production this showed up as a ~30-40%
    # failure rate across almost every endpoint (EGW00201), including 100% of
    # domestic buy orders, even though each client looked correctly paced on
    # its own. The pacing clock must be shared across instances, not per-client.
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client_a = _make_paced_test_client(credentials)
    client_b = _make_paced_test_client(credentials)

    async def run_two_calls_on_separate_clients() -> float:
        start = time.monotonic()
        await client_a._request("GET", "/a", "TR1")
        await client_b._request("GET", "/b", "TR2")
        return time.monotonic() - start

    elapsed = asyncio.run(run_two_calls_on_separate_clients())

    assert elapsed >= KisRestClient._min_request_interval_sec


def test_request_without_on_api_call_hook_does_not_error(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)
    fake_http = FakeAsyncClient(
        [FakeResponse(200, {"rt_cd": "0", "msg_cd": "0", "msg1": "정상", "output": {"value": "ok"}})]
    )
    client._client = fake_http
    client.ensure_token = lambda: asyncio.sleep(0, result="tok")  # type: ignore[method-assign]

    payload = asyncio.run(client._request("GET", "/test", "TRTEST", params={"a": "1"}))

    assert payload["output"]["value"] == "ok"


def test_get_overseas_daily_prices_uses_official_endpoint_fields(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        assert method == "GET"
        assert path == client.OVERSEAS_DAILY_PRICE_PATH
        assert tr_id == "HHDFS76240000"
        assert kwargs["params"]["EXCD"] == "NAS"
        assert kwargs["params"]["SYMB"] == "TSLA"
        return {"output2": [{"xymd": "20260626", "clos": "219.53"}]}

    client._request = fake_request  # type: ignore[method-assign]

    rows = asyncio.run(client.get_overseas_daily_prices("TSLA", "NASD"))

    assert rows == [{"xymd": "20260626", "clos": "219.53"}]


def test_get_overseas_minute_chart_reads_output2_head_when_present(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        assert method == "GET"
        assert path == client.OVERSEAS_MINUTE_CHART_PATH
        assert tr_id == "HHDFS76950200"
        assert kwargs["params"]["EXCD"] == "AMS"
        assert kwargs["params"]["SYMB"] == "SOXL"
        assert kwargs["params"]["NMIN"] == "5"
        assert kwargs["params"]["NREC"] == "60"
        return {"output2_head": [{"xymd": "20260626", "xhms": "110000", "last": "219.53"}]}

    client._request = fake_request  # type: ignore[method-assign]

    rows = asyncio.run(client.get_overseas_minute_chart("SOXL", "AMEX"))

    assert rows == [{"xymd": "20260626", "xhms": "110000", "last": "219.53"}]


def test_get_domestic_order_history_uses_modern_daily_ccld_endpoint(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        assert method == "GET"
        assert path == client.DOMESTIC_ORDER_HISTORY_PATH
        assert tr_id == "VTTC0081R"
        assert kwargs["params"]["CANO"] == "12345678"
        assert kwargs["params"]["ACNT_PRDT_CD"] == "01"
        assert kwargs["params"]["INQR_STRT_DT"] == "20260710"
        assert kwargs["params"]["INQR_END_DT"] == "20260710"
        assert kwargs["params"]["SLL_BUY_DVSN_CD"] == "00"
        assert kwargs["params"]["CCLD_DVSN"] == "02"
        assert kwargs["params"]["EXCG_ID_DVSN_CD"] == "KRX"
        return {
            "output1": [{"pdno": "073240", "rmn_qty": "126"}],
            "output2": {"tot_ord_qty": "126"},
            "ctx_area_fk100": "",
            "ctx_area_nk100": "",
        }

    client._request = fake_request  # type: ignore[method-assign]

    history = asyncio.run(
        client.get_domestic_order_history(
            start_date="20260710",
            end_date="20260710",
            fill_filter="02",
        )
    )

    assert history["tr_id"] == "VTTC0081R"
    assert history["orders"] == [{"pdno": "073240", "rmn_qty": "126"}]
    assert history["summary"] == {"tot_ord_qty": "126"}


def test_revise_or_cancel_domestic_order_uses_full_cancel_body(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        assert method == "POST"
        assert path == client.DOMESTIC_REVISE_CANCEL_PATH
        assert tr_id == "VTTC0013U"
        assert kwargs["body"] == {
            "CANO": "12345678",
            "ACNT_PRDT_CD": "01",
            "KRX_FWDG_ORD_ORGNO": "00950",
            "ORGN_ODNO": "0000013669",
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        return {"output": {"ODNO": "0000014000"}}

    client._request = fake_request  # type: ignore[method-assign]

    result = asyncio.run(
        client.revise_or_cancel_domestic_order(
            krx_order_orgno="00950",
            original_order_no="0000013669",
            order_division="00",
            rvse_cncl_dvsn_cd="02",
            qty=126,
            price=6990,
            qty_all_order_yn="Y",
        )
    )

    assert result["output"]["ODNO"] == "0000014000"


def test_ensure_token_retries_after_connect_timeout(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)
    client._client = FakeAsyncClient(
        [
            httpx.ConnectTimeout("timeout-1"),
            httpx.ConnectTimeout("timeout-2"),
            FakeResponse(
                200,
                {
                    "access_token": "fresh-token",
                    "access_token_token_expired": "",
                },
            ),
        ]
    )

    token = asyncio.run(client.ensure_token())

    assert token == "fresh-token"
    assert len(client._client.calls) == 3


def test_ensure_token_raises_kis_api_error_after_retries(tmp_path: Path) -> None:
    credentials = KisCredentials(
        env="vps",
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / "token.json",
    )
    client = KisRestClient(credentials)
    client._client = FakeAsyncClient(
        [
            httpx.ConnectTimeout("timeout-1"),
            httpx.ConnectTimeout("timeout-2"),
            httpx.ConnectTimeout("timeout-3"),
        ]
    )

    try:
        asyncio.run(client.ensure_token())
    except Exception as exc:  # noqa: BLE001
        error = exc
    else:
        error = None

    assert error is not None
    assert "token_request_failed" in str(error)
