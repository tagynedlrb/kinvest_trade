from __future__ import annotations

import asyncio
import multiprocessing
import time
from pathlib import Path
from queue import Empty

import httpx
import pytest

from kinvest_trade.client import KisApiError, KisRestClient
from kinvest_trade.config import KisCredentials


@pytest.fixture(autouse=True)
def _reset_client_rate_limit_state():
    # The asyncio lock is shared across clients in one process. Reset it so an
    # event loop created by one test cannot leak into another test.
    KisRestClient._rate_limit_lock = None
    KisRestClient._last_response_completed_at_by_profile = {}
    KisRestClient._last_response_path_by_profile = {}
    KisRestClient._adaptive_rate_limit_until_by_profile = {}
    yield
    KisRestClient._rate_limit_lock = None
    KisRestClient._last_response_completed_at_by_profile = {}
    KisRestClient._last_response_path_by_profile = {}
    KisRestClient._adaptive_rate_limit_until_by_profile = {}


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

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


@pytest.mark.parametrize("rate_code", ["EGW00201", "EGW00215"])
def test_request_reports_api_calls_via_on_api_call_hook(
    tmp_path: Path,
    rate_code: str,
) -> None:
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
            FakeResponse(500, {"msg_cd": rate_code, "msg1": "초당 거래건수를 초과하였습니다."}),
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
    assert calls[0]["msg_cd"] == rate_code
    assert calls[0]["tr_id"] == "TRTEST"
    assert calls[0]["path"] == "/test"
    assert calls[0]["method"] == "GET"
    assert isinstance(calls[0]["elapsed_ms"], int)
    assert calls[0]["logical_request_id"]
    assert calls[0]["attempt_no"] == 1
    assert calls[0]["max_attempts"] == 3
    assert calls[0]["retry_scheduled"] is True
    assert calls[0]["retry_reason"] == "rate_limit"
    assert calls[0]["logical_terminal"] is False
    assert calls[0]["dispatched_at"].endswith("+00:00")
    assert calls[0]["throttle_wait_ms"] >= 0
    assert calls[0]["network_elapsed_ms"] >= 0
    assert calls[0]["adaptive_pacing_active"] is False
    assert calls[0]["adaptive_wait_ms"] == 0
    assert calls[0]["balance_pair_pacing_active"] is False
    assert calls[0]["balance_pair_wait_ms"] == 0
    assert calls[1]["success"] is True
    assert calls[1]["http_status"] == 200
    assert calls[1]["logical_request_id"] == calls[0]["logical_request_id"]
    assert calls[1]["attempt_no"] == 2
    assert calls[1]["max_attempts"] == 3
    assert calls[1]["retry_scheduled"] is False
    assert calls[1]["retry_reason"] == ""
    assert calls[1]["logical_terminal"] is True
    assert calls[1]["dispatched_at"].endswith("+00:00")
    assert calls[1]["throttle_wait_ms"] >= 0
    assert calls[1]["network_elapsed_ms"] >= 0
    assert calls[1]["adaptive_pacing_active"] is True
    assert calls[1]["adaptive_wait_ms"] >= 0
    assert calls[1]["balance_pair_pacing_active"] is False
    assert calls[1]["balance_pair_wait_ms"] == 0
    # None of the logged fields ever carry account number or credentials.
    for call in calls:
        serialized = str(call)
        assert credentials.account_no not in serialized
        assert credentials.appkey not in serialized
        assert credentials.appsecret not in serialized


def test_request_marks_exhausted_rate_limit_as_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client._client = FakeAsyncClient(
        [
            FakeResponse(
                500,
                {
                    "msg_cd": "EGW00201",
                    "msg1": "초당 거래건수를 초과하였습니다.",
                },
            )
            for _ in range(3)
        ]
    )

    async def token() -> str:
        return "tok"

    async def no_wait(*_args, **_kwargs) -> tuple[bool, int, bool, int]:
        return False, 0, False, 0

    client.ensure_token = token  # type: ignore[method-assign]
    client._throttle = no_wait  # type: ignore[method-assign]
    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", no_wait)

    with pytest.raises(KisApiError, match="EGW00201"):
        asyncio.run(client._request("GET", "/test", "TRTEST"))

    assert [call["attempt_no"] for call in calls] == [1, 2, 3]
    assert len({call["logical_request_id"] for call in calls}) == 1
    assert [call["retry_scheduled"] for call in calls] == [True, True, False]
    assert [call["retry_reason"] for call in calls] == [
        "rate_limit",
        "rate_limit",
        "",
    ]
    assert [call["logical_terminal"] for call in calls] == [
        False,
        False,
        True,
    ]


def test_request_retries_vps_get_after_service_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            FakeResponse(
                200,
                {
                    "rt_cd": "1",
                    "msg_cd": "90020000",
                    "msg1": "모의투자 서비스가 지연되고 있습니다. 잠시후 재시도 바랍니다.",
                },
            ),
            FakeResponse(
                200,
                {
                    "rt_cd": "0",
                    "msg_cd": "20310000",
                    "msg1": "모의투자 조회가 완료되었습니다.",
                    "output": {"value": "ok"},
                },
            ),
        ]
    )
    client._client = fake_http
    delays: list[float] = []

    async def token() -> str:
        return "tok"

    async def no_throttle(*_args, **_kwargs) -> tuple[bool, int, bool, int]:
        return False, 0, False, 0

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    client.ensure_token = token  # type: ignore[method-assign]
    client._throttle = no_throttle  # type: ignore[method-assign]
    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", capture_sleep)

    payload = asyncio.run(client._request("GET", "/balance", "BALANCE"))

    assert payload["output"]["value"] == "ok"
    assert len(fake_http.calls) == 2
    assert delays == [2.0]
    assert [call["retry_scheduled"] for call in calls] == [True, False]
    assert [call["retry_reason"] for call in calls] == ["service_delay", ""]
    assert [call["logical_terminal"] for call in calls] == [False, True]
    assert len({call["logical_request_id"] for call in calls}) == 1


def test_request_marks_exhausted_service_delay_as_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client._client = FakeAsyncClient(
        [
            FakeResponse(
                200,
                {
                    "rt_cd": "1",
                    "msg_cd": "90020000",
                    "msg1": "모의투자 서비스가 지연되고 있습니다. 잠시후 재시도 바랍니다.",
                },
            )
            for _ in range(3)
        ]
    )
    delays: list[float] = []

    async def token() -> str:
        return "tok"

    async def no_throttle(*_args, **_kwargs) -> tuple[bool, int, bool, int]:
        return False, 0, False, 0

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    client.ensure_token = token  # type: ignore[method-assign]
    client._throttle = no_throttle  # type: ignore[method-assign]
    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", capture_sleep)

    with pytest.raises(KisApiError, match="90020000"):
        asyncio.run(client._request("GET", "/balance", "BALANCE"))

    assert delays == [2.0, 4.0]
    assert [call["retry_scheduled"] for call in calls] == [True, True, False]
    assert [call["retry_reason"] for call in calls] == [
        "service_delay",
        "service_delay",
        "",
    ]
    assert [call["logical_terminal"] for call in calls] == [
        False,
        False,
        True,
    ]


@pytest.mark.parametrize(
    ("env", "method"),
    [
        ("vps", "POST"),
        ("prod", "GET"),
    ],
)
def test_request_retries_service_delay_only_for_vps_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    method: str,
) -> None:
    credentials = KisCredentials(
        env=env,
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
            FakeResponse(
                200,
                {
                    "rt_cd": "1",
                    "msg_cd": "90020000",
                    "msg1": "모의투자 서비스가 지연되고 있습니다. 잠시후 재시도 바랍니다.",
                },
            )
        ]
    )
    client._client = fake_http
    delays: list[float] = []

    async def token() -> str:
        return "tok"

    async def no_throttle(*_args, **_kwargs) -> tuple[bool, int, bool, int]:
        return False, 0, False, 0

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    client.ensure_token = token  # type: ignore[method-assign]
    client._throttle = no_throttle  # type: ignore[method-assign]
    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", capture_sleep)

    with pytest.raises(KisApiError, match="90020000"):
        asyncio.run(
            client._request(
                method,
                "/order",
                "ORDER",
                body={"symbol": "TEST", "qty": 1},
            )
        )

    assert len(fake_http.calls) == 1
    assert delays == []
    assert calls[0]["retry_scheduled"] is False
    assert calls[0]["retry_reason"] == ""
    assert calls[0]["logical_terminal"] is True


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


def _run_file_throttle_in_process(
    token_cache_path: str,
    interval_sec: float,
    ready_queue,
    start_event,
    result_queue,
) -> None:
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
        token_cache_path=Path(token_cache_path),
    )
    client = KisRestClient(credentials)
    client._vps_min_request_interval_sec = interval_sec
    ready_queue.put(True)
    if not start_event.wait(timeout=3):
        raise RuntimeError("parent did not release throttle workers")
    started_at = time.monotonic()
    client._throttle_across_processes()
    result_queue.put((started_at, time.monotonic()))


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

    assert client._request_interval_sec() == client._vps_min_request_interval_sec
    assert elapsed >= client._vps_min_request_interval_sec


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

    assert client_a._request_interval_sec() == KisRestClient._vps_min_request_interval_sec
    assert elapsed >= KisRestClient._vps_min_request_interval_sec


def test_pacing_file_coordinates_separate_clients(tmp_path: Path) -> None:
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
    client_a = KisRestClient(credentials)
    client_b = KisRestClient(credentials)
    client_a._vps_min_request_interval_sec = 0.05
    client_b._vps_min_request_interval_sec = 0.05

    client_a._throttle_across_processes()
    started_at = time.monotonic()
    client_b._throttle_across_processes()
    elapsed = time.monotonic() - started_at

    state_path = tmp_path / "token.json.rate_limit"
    assert elapsed >= 0.045
    assert state_path.exists()
    assert state_path.stat().st_mode & 0o777 == 0o600
    float(state_path.read_text(encoding="ascii"))


def test_vps_rate_limit_temporarily_adds_response_completion_floor(
    tmp_path: Path,
) -> None:
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
    client._vps_min_request_interval_sec = 0.05
    client._vps_adaptive_response_interval_sec = 0.08
    client._vps_adaptive_window_sec = 1.0

    active, adaptive_wait_ms, balance_active, balance_wait_ms = (
        client._throttle_across_processes()
    )
    assert active is False
    assert adaptive_wait_ms == 0
    assert balance_active is False
    assert balance_wait_ms == 0
    client._record_response_completion()
    client._activate_adaptive_rate_limit_pacing()
    started_at = time.monotonic()
    active, adaptive_wait_ms, balance_active, balance_wait_ms = (
        client._throttle_across_processes()
    )
    elapsed = time.monotonic() - started_at

    assert active is True
    assert elapsed >= 0.07
    assert adaptive_wait_ms >= 20
    assert balance_active is False
    assert balance_wait_ms == 0


def test_vps_default_adaptive_pacing_covers_full_market_session() -> None:
    assert KisRestClient._vps_adaptive_window_sec >= 8 * 60 * 60


def test_vps_consecutive_overseas_balance_adds_preventive_response_floor(
    tmp_path: Path,
) -> None:
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
    client._vps_min_request_interval_sec = 0.05
    client._vps_balance_pair_response_interval_sec = 0.08

    first = client._throttle_across_processes(
        path=client.OVERSEAS_BALANCE_PATH,
        attempt_no=1,
    )
    assert first == (False, 0, False, 0)
    client._record_response_completion(client.OVERSEAS_BALANCE_PATH)
    started_at = time.monotonic()
    (
        adaptive_active,
        adaptive_wait_ms,
        balance_active,
        balance_wait_ms,
    ) = client._throttle_across_processes(
        path=client.OVERSEAS_BALANCE_PATH,
        attempt_no=1,
    )
    elapsed = time.monotonic() - started_at

    assert adaptive_active is False
    assert adaptive_wait_ms == 0
    assert balance_active is True
    assert elapsed >= 0.07
    assert balance_wait_ms >= 20


def test_request_logs_balance_pair_pacing_and_resets_after_other_path(
    tmp_path: Path,
) -> None:
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
    client._vps_min_request_interval_sec = 0.005
    client._vps_balance_pair_response_interval_sec = 0.015
    client._client = FakeAsyncClient(
        [
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0"}),
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0"}),
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0"}),
            FakeResponse(200, {"rt_cd": "0", "msg_cd": "0"}),
        ]
    )
    client.ensure_token = lambda: asyncio.sleep(0, result="tok")  # type: ignore[method-assign]

    async def run_requests() -> None:
        await client._request(
            "GET",
            client.OVERSEAS_BALANCE_PATH,
            "BALANCE",
        )
        await client._request(
            "GET",
            client.OVERSEAS_BALANCE_PATH,
            "BALANCE",
        )
        await client._request(
            "GET",
            client.OVERSEAS_PRICE_PATH,
            "PRICE",
        )
        await client._request(
            "GET",
            client.OVERSEAS_BALANCE_PATH,
            "BALANCE",
        )

    asyncio.run(run_requests())

    assert [
        call["balance_pair_pacing_active"]
        for call in calls
    ] == [False, True, False, False]
    assert calls[1]["balance_pair_wait_ms"] > 0
    assert calls[0]["balance_pair_wait_ms"] == 0
    assert calls[2]["balance_pair_wait_ms"] == 0
    assert calls[3]["balance_pair_wait_ms"] == 0


@pytest.mark.parametrize(
    ("env", "path", "attempt_no"),
    [
        ("prod", KisRestClient.OVERSEAS_BALANCE_PATH, 1),
        ("vps", KisRestClient.OVERSEAS_PRICE_PATH, 1),
        ("vps", KisRestClient.OVERSEAS_BALANCE_PATH, 2),
    ],
)
def test_balance_pair_pacing_excludes_other_profiles_paths_and_retries(
    tmp_path: Path,
    env: str,
    path: str,
    attempt_no: int,
) -> None:
    credentials = KisCredentials(
        env=env,
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
        token_cache_path=tmp_path / f"{env}.json",
    )
    client = KisRestClient(credentials)
    client._record_response_completion(client.OVERSEAS_BALANCE_PATH)

    active, ready_at = client._balance_pair_pacing_state(
        path=path,
        attempt_no=attempt_no,
        now=time.monotonic(),
    )

    assert active is False
    assert ready_at == 0.0


def test_adaptive_rate_limit_pacing_is_vps_profile_local(tmp_path: Path) -> None:
    base = dict(
        appkey="appkey",
        appsecret="appsecret",
        account_no="12345678",
        account_product_code="01",
        hts_id="",
        dry_run=False,
        live_trading_enabled=False,
        appkey_path=None,
        appsecret_path=None,
    )
    vps_a = KisRestClient(
        KisCredentials(
            env="vps",
            token_cache_path=tmp_path / "a.json",
            **base,
        )
    )
    vps_b = KisRestClient(
        KisCredentials(
            env="vps",
            token_cache_path=tmp_path / "b.json",
            **base,
        )
    )
    prod = KisRestClient(
        KisCredentials(
            env="prod",
            token_cache_path=tmp_path / "prod.json",
            **base,
        )
    )

    vps_a._record_response_completion()
    vps_a._activate_adaptive_rate_limit_pacing()
    now = time.monotonic()

    assert vps_a._adaptive_rate_limit_state(now=now)[0] is True
    assert vps_b._adaptive_rate_limit_state(now=now)[0] is False
    prod._record_response_completion()
    prod._activate_adaptive_rate_limit_pacing()
    assert prod._adaptive_rate_limit_state(now=time.monotonic())[0] is False


def test_pacing_file_serializes_concurrent_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    token_cache_path = str(tmp_path / "token.json")
    interval_sec = 0.08
    processes = [
        context.Process(
            target=_run_file_throttle_in_process,
            args=(
                token_cache_path,
                interval_sec,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    try:
        for _ in processes:
            assert ready_queue.get(timeout=3) is True
        start_event.set()
        results = [result_queue.get(timeout=3) for _ in processes]
    except Empty as exc:
        raise AssertionError("throttle worker did not report in time") from exc
    finally:
        start_event.set()
        for process in processes:
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)

    assert all(process.exitcode == 0 for process in processes)
    completed_at = sorted(result[1] for result in results)
    assert completed_at[1] - completed_at[0] >= interval_sec * 0.8


@pytest.mark.parametrize("stored_value", ["not-a-timestamp", "future"])
def test_pacing_file_recovers_invalid_or_pre_reboot_clock(
    tmp_path: Path,
    stored_value: str,
) -> None:
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
    state_path = tmp_path / "token.json.rate_limit"
    value = (
        f"{time.monotonic() + 3600:.9f}"
        if stored_value == "future"
        else stored_value
    )
    state_path.write_text(value, encoding="ascii")
    client = KisRestClient(credentials)
    client._vps_min_request_interval_sec = 0.05

    started_at = time.monotonic()
    client._throttle_across_processes()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert float(state_path.read_text(encoding="ascii")) <= time.monotonic()
    assert state_path.stat().st_mode & 0o777 == 0o600


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


def test_get_domestic_index_daily_prices_uses_kospi_endpoint(tmp_path: Path) -> None:
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
        assert path == client.DOMESTIC_INDEX_DAILY_PATH
        assert tr_id == "FHKUP03500100"
        assert kwargs["params"] == {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": "0001",
            "FID_INPUT_DATE_1": "20260701",
            "FID_INPUT_DATE_2": "20260728",
            "FID_PERIOD_DIV_CODE": "D",
        }
        return {"output2": [{"stck_bsop_date": "20260728", "bstp_nmix_prpr": "6023.66"}]}

    client._request = fake_request  # type: ignore[method-assign]

    rows = asyncio.run(
        client.get_domestic_index_daily_prices(
            start_date="20260701",
            end_date="20260728",
        )
    )

    assert rows[0]["bstp_nmix_prpr"] == "6023.66"


def test_get_domestic_futures_continuous_daily_snapshot_uses_front_alias(
    tmp_path: Path,
) -> None:
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
        assert path == client.DOMESTIC_FUTURE_DAILY_PATH
        assert tr_id == "FHKIF03020100"
        assert kwargs["params"] == {
            "FID_COND_MRKT_DIV_CODE": "F",
            "FID_INPUT_ISCD": "101000",
            "FID_INPUT_DATE_1": "20260701",
            "FID_INPUT_DATE_2": "20260729",
            "FID_PERIOD_DIV_CODE": "D",
        }
        return {
            "output1": {
                "futs_shrn_iscd": "A01609",
                "futs_prdy_clpr": "956.75",
            },
            "output2": [
                {
                    "stck_bsop_date": "20260729",
                    "futs_prpr": "898.65",
                }
            ],
        }

    client._request = fake_request  # type: ignore[method-assign]

    snapshot = asyncio.run(
        client.get_domestic_futures_continuous_daily_snapshot(
            start_date="20260701",
            end_date="20260729",
        )
    )

    assert snapshot["summary"]["futs_shrn_iscd"] == "A01609"
    assert snapshot["rows"][0]["futs_prpr"] == "898.65"


def test_get_etf_etn_current_price_preserves_nav_and_tracking_fields(
    tmp_path: Path,
) -> None:
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
        assert path == client.ETF_ETN_PRICE_PATH
        assert tr_id == "FHPST02400000"
        assert kwargs["params"] == {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": "114800",
        }
        return {
            "output": {
                "stck_prpr": "1,404",
                "nav": "1404.68",
                "dprt": "-0.05",
                "trc_errt": "0.52",
                "etf_trc_ert_mltp": "-1.00",
                "lp_oder_able_cls_code": "N",
                "vi_cls_code": "N",
            }
        }

    client._request = fake_request  # type: ignore[method-assign]

    quote = asyncio.run(client.get_etf_etn_current_price("114800"))

    assert quote["current_price"] == 1404.0
    assert quote["nav"] == 1404.68
    assert quote["tracking_multiplier"] == -1.0
    assert quote["reported_deviation_pct"] == -0.05
    assert abs(quote["nav_deviation_pct"] + 0.000484095) < 1e-8


def test_get_overseas_index_daily_prices_uses_nasdaq_composite_endpoint(
    tmp_path: Path,
) -> None:
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
        assert path == client.OVERSEAS_INDEX_DAILY_PATH
        assert tr_id == "FHKST03030100"
        assert kwargs["params"] == {
            "FID_COND_MRKT_DIV_CODE": "N",
            "FID_INPUT_ISCD": "COMP",
            "FID_INPUT_DATE_1": "20260701",
            "FID_INPUT_DATE_2": "20260728",
            "FID_PERIOD_DIV_CODE": "D",
        }
        return {"output2": [{"stck_bsop_date": "20260728", "ovrs_nmix_prpr": "24646.14"}]}

    client._request = fake_request  # type: ignore[method-assign]

    rows = asyncio.run(
        client.get_overseas_index_daily_prices(
            start_date="20260701",
            end_date="20260728",
        )
    )

    assert rows[0]["ovrs_nmix_prpr"] == "24646.14"


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


def test_get_overseas_order_history_follows_kis_continuation_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    calls: list[dict] = []
    continuation_delays: list[float] = []
    pages = iter(
        [
            {
                "output": [{"odno": "100", "pdno": "PDI"}],
                "ctx_area_fk200": " ",
                "ctx_area_nk200": "NEXT",
                "_response_headers": {"tr_cont": "F"},
            },
            {
                "output": [{"odno": "101", "pdno": "PDI"}],
                "ctx_area_fk200": "",
                "ctx_area_nk200": "",
                "_response_headers": {"tr_cont": "D"},
            },
        ]
    )

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        calls.append(kwargs)
        return next(pages)

    async def fake_sleep(delay: float) -> None:
        continuation_delays.append(delay)

    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", fake_sleep)
    client._request = fake_request  # type: ignore[method-assign]
    history = asyncio.run(
        client.get_overseas_order_history(
            start_date="20260727",
            end_date="20260728",
        )
    )

    assert [row["odno"] for row in history["orders"]] == ["100", "101"]
    assert history["page_count"] == 2
    assert history["pagination_truncated"] is False
    assert calls[0]["params"]["CTX_AREA_NK200"] == ""
    assert calls[1]["params"]["CTX_AREA_NK200"] == "NEXT"
    assert calls[1]["extra_headers"] == {"tr_cont": "N"}
    assert continuation_delays == [1.0]


def test_get_overseas_order_history_reports_pagination_truncation(
    tmp_path: Path,
) -> None:
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

    async def fake_request(*_args, **_kwargs):
        return {
            "output": [{"odno": "100", "pdno": "PDI"}],
            "ctx_area_fk200": "NEXT_FK",
            "ctx_area_nk200": "NEXT_NK",
            "_response_headers": {"tr_cont": "F"},
        }

    client._request = fake_request  # type: ignore[method-assign]
    history = asyncio.run(
        client.get_overseas_order_history(
            start_date="20260814",
            end_date="20260817",
            max_pages=1,
        )
    )

    assert history["page_count"] == 1
    assert history["pagination_truncated"] is True


def test_get_overseas_balance_follows_kis_continuation_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    calls: list[dict] = []
    continuation_delays: list[float] = []
    pages = iter(
        [
            {
                "output1": [{"ovrs_pdno": "AAA", "ovrs_cblc_qty": "1"}],
                "output2": [{"frcr_buy_amt_smtl1": "100"}],
                "ctx_area_fk200": "NEXT_FK",
                "ctx_area_nk200": "NEXT_NK",
                "_response_headers": {"tr_cont": "F"},
            },
            {
                "output1": [{"ovrs_pdno": "BBB", "ovrs_cblc_qty": "2"}],
                "output2": [],
                "ctx_area_fk200": "",
                "ctx_area_nk200": "",
                "_response_headers": {"tr_cont": "D"},
            },
        ]
    )

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        calls.append(
            {
                "method": method,
                "path": path,
                "tr_id": tr_id,
                **kwargs,
            }
        )
        return next(pages)

    async def fake_sleep(delay: float) -> None:
        continuation_delays.append(delay)

    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", fake_sleep)
    client._request = fake_request  # type: ignore[method-assign]
    balance = asyncio.run(client.get_overseas_balance("NASD", "USD"))

    assert [row["ovrs_pdno"] for row in balance["positions"]] == ["AAA", "BBB"]
    assert balance["position_count"] == 2
    assert balance["summary"] == {"frcr_buy_amt_smtl1": "100"}
    assert balance["page_count"] == 2
    assert balance["tr_cont"] == "D"
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == client.OVERSEAS_BALANCE_PATH
    assert calls[0]["tr_id"] == "VTTS3012R"
    assert calls[0]["include_response_headers"] is True
    assert calls[1]["params"]["CTX_AREA_FK200"] == "NEXT_FK"
    assert calls[1]["params"]["CTX_AREA_NK200"] == "NEXT_NK"
    assert calls[1]["extra_headers"] == {"tr_cont": "N"}
    assert continuation_delays == [1.0]


def test_get_overseas_balance_stops_on_empty_continuation_context(
    tmp_path: Path,
) -> None:
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
    calls: list[dict] = []

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        calls.append(kwargs)
        return {
            "output1": [{"ovrs_pdno": "AAA"}],
            "output2": {},
            "ctx_area_fk200": " ",
            "ctx_area_nk200": " ",
            "_response_headers": {"tr_cont": "F"},
        }

    client._request = fake_request  # type: ignore[method-assign]
    balance = asyncio.run(client.get_overseas_balance("NASD", "USD"))

    assert balance["position_count"] == 1
    assert balance["page_count"] == 1
    assert len(calls) == 1


def test_get_overseas_open_orders_uses_production_nccs_and_continuation(
    tmp_path: Path,
) -> None:
    credentials = KisCredentials(
        env="prod",
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
    calls: list[dict] = []
    pages = iter(
        [
            {
                "output": [{"odno": "100", "pdno": "FSUN", "nccs_qty": "2"}],
                "ctx_area_fk200": "NEXT_FK",
                "ctx_area_nk200": "NEXT_NK",
                "_response_headers": {"tr_cont": "F"},
            },
            {
                "output": [{"odno": "101", "pdno": "HUBB", "nccs_qty": "1"}],
                "ctx_area_fk200": "",
                "ctx_area_nk200": "",
                "_response_headers": {"tr_cont": "D"},
            },
        ]
    )

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        calls.append(
            {
                "method": method,
                "path": path,
                "tr_id": tr_id,
                **kwargs,
            }
        )
        return next(pages)

    client._request = fake_request  # type: ignore[method-assign]
    result = asyncio.run(
        client.get_overseas_open_orders(exchange_code="NASD")
    )

    assert [row["odno"] for row in result["orders"]] == ["100", "101"]
    assert result["page_count"] == 2
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == client.OVERSEAS_OPEN_ORDERS_PATH
    assert calls[0]["tr_id"] == "TTTS3018R"
    assert calls[0]["params"]["OVRS_EXCG_CD"] == "NASD"
    assert calls[1]["params"]["CTX_AREA_FK200"] == "NEXT_FK"
    assert calls[1]["params"]["CTX_AREA_NK200"] == "NEXT_NK"
    assert calls[1]["extra_headers"] == {"tr_cont": "N"}


def test_get_overseas_open_orders_rejects_vps_profile(tmp_path: Path) -> None:
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

    with pytest.raises(KisApiError, match="production-only"):
        asyncio.run(client.get_overseas_open_orders(exchange_code="NASD"))


def test_overseas_history_continuation_delay_is_not_applied_to_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = KisCredentials(
        env="prod",
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
    continuation_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        continuation_delays.append(delay)

    monkeypatch.setattr("kinvest_trade.client.asyncio.sleep", fake_sleep)

    asyncio.run(client._pace_overseas_history_continuation())

    assert continuation_delays == []


def test_get_domestic_order_history_follows_kis_continuation_pages(
    tmp_path: Path,
) -> None:
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
    calls: list[dict] = []
    pages = iter(
        [
            {
                "output1": [{"odno": "200", "pdno": "005930"}],
                "output2": {"tot_ord_qty": "2"},
                "ctx_area_fk100": " ",
                "ctx_area_nk100": "NEXT",
                "_response_headers": {"tr_cont": "M"},
            },
            {
                "output1": [{"odno": "201", "pdno": "005930"}],
                "output2": {"tot_ord_qty": "2"},
                "ctx_area_fk100": "",
                "ctx_area_nk100": "",
                "_response_headers": {"tr_cont": "D"},
            },
        ]
    )

    async def fake_request(method: str, path: str, tr_id: str, **kwargs):
        calls.append(kwargs)
        return next(pages)

    client._request = fake_request  # type: ignore[method-assign]
    history = asyncio.run(
        client.get_domestic_order_history(
            start_date="20260728",
            end_date="20260728",
        )
    )

    assert [row["odno"] for row in history["orders"]] == ["200", "201"]
    assert history["page_count"] == 2
    assert calls[1]["params"]["CTX_AREA_NK100"] == "NEXT"
    assert calls[1]["extra_headers"] == {"tr_cont": "N"}


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
