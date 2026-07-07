from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from kinvest_trade.client import KisRestClient
from kinvest_trade.config import KisCredentials


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
