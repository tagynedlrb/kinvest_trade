from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .config import KisCredentials
from .market_sessions import get_us_trading_session, is_us_daytime_session

_logger = logging.getLogger(__name__)


class KisApiError(RuntimeError):
    """Raised when the broker API returns an error payload."""


class MissingCredentialsError(KisApiError):
    """Raised when required KIS credentials are missing."""


def parse_kis_number(value: str | int | float | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    text = str(value).strip()
    if not text:
        return 0

    sign = -1 if text.startswith("-") else 1
    cleaned = text.lstrip("+-").replace(",", "")
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return 0
    return sign * int(digits)


def parse_kis_float(value: str | int | float | None) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class KisRestClient:
    """Thin async wrapper around a small subset of KIS REST endpoints.

    The goal here is not to recreate the entire official sample repository.
    Instead, this class exposes only the pieces this project needs right now:
    token issuance, quote polling, daily/minute chart reads, and a future-ready
    domestic cash order method.
    """

    TOKEN_PATH = "/oauth2/tokenP"
    DOMESTIC_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
    ETF_ETN_PRICE_PATH = "/uapi/etfetn/v1/quotations/inquire-price"
    DOMESTIC_ASKING_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    DOMESTIC_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    DOMESTIC_INDEX_DAILY_PATH = (
        "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    )
    DOMESTIC_FUTURE_DAILY_PATH = (
        "/uapi/domestic-futureoption/v1/quotations/"
        "inquire-daily-fuopchartprice"
    )
    DOMESTIC_TIME_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    DOMESTIC_RANKING_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
    DOMESTIC_FLUCTUATION_PATH = "/uapi/domestic-stock/v1/quotations/fluctuation-rank"
    DOMESTIC_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
    DOMESTIC_POSSIBLE_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    DOMESTIC_ORDER_HISTORY_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    DOMESTIC_ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
    DOMESTIC_REVISE_CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
    OVERSEAS_PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
    OVERSEAS_DAILY_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
    OVERSEAS_INDEX_DAILY_PATH = (
        "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
    )
    OVERSEAS_MINUTE_CHART_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
    OVERSEAS_SEARCH_INFO_PATH = "/uapi/overseas-price/v1/quotations/search-info"
    OVERSEAS_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
    OVERSEAS_POSSIBLE_ORDER_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
    OVERSEAS_ORDER_HISTORY_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
    OVERSEAS_OPEN_ORDERS_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"
    OVERSEAS_REVISE_CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
    OVERSEAS_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
    OVERSEAS_DAYTIME_ORDER_PATH = "/uapi/overseas-stock/v1/trading/daytime-order"

    def __init__(
        self,
        credentials: KisCredentials,
        *,
        on_api_call: "Callable[[dict[str, Any]], None] | None" = None,
    ) -> None:
        self.credentials = credentials
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._on_api_call = on_api_call
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)
        )

    # KIS's per-second call limit (EGW00201) is enforced per account/appkey, not
    # per Python object or process. A class lock coordinates clients inside the
    # telegram daemon, while a small file-locked timestamp also coordinates
    # independently launched CLI/audit processes using the same token profile.
    # VPS is paced at 1.05s because live verification showed that this paper
    # account effectively accepts one request per second; production keeps the
    # existing conservative 0.7s floor.
    _rate_limit_lock: "asyncio.Lock | None" = None
    _last_response_completed_at_by_profile: dict[str, float] = {}
    _last_response_path_by_profile: dict[str, str] = {}
    _adaptive_rate_limit_until_by_profile: dict[str, float] = {}
    _min_request_interval_sec: float = 0.7
    _vps_min_request_interval_sec: float = 1.05
    # Timed VPS lineage found every consecutive overseas-balance rate-limit
    # response below 826ms of post-response quiet time. Keep a narrow 850ms
    # preventive floor for that pair only; all other error-free paths retain
    # request-start pacing.
    _vps_balance_pair_response_interval_sec: float = 0.85
    # Timed VPS evidence showed no rate-limit response when the next dispatch
    # followed the previous response completion by at least 950ms. Once VPS
    # proves the stricter boundary is active, retain it for a full market
    # session; a two-minute window repeatedly expired and caused the same
    # recovered EGW00201 response throughout the session.
    _vps_adaptive_response_interval_sec: float = 0.95
    _vps_adaptive_window_sec: float = 8 * 60 * 60
    # KIS's official inquire_ccnl example pauses before each continuation page.
    # Live VPS logs showed that the existing request-start throttle alone was
    # insufficient, while every one-second rate-limit retry succeeded.
    _vps_overseas_history_continuation_delay_sec: float = 1.0
    _RATE_LIMIT_MSG_CODES = frozenset({"EGW00201", "EGW00215"})
    _VPS_TRANSIENT_READ_MSG_CODES = frozenset({"90020000"})
    _VPS_GATEWAY_ROUTING_MSG_CODES = frozenset({"EGW00300"})

    def _request_interval_sec(self) -> float:
        if str(self.credentials.env).strip().lower() == "vps":
            return self._vps_min_request_interval_sec
        return self._min_request_interval_sec

    def _rate_limit_profile_key(self) -> str:
        return str(self.credentials.token_cache_path)

    def _record_response_completion(self, path: str = "") -> None:
        key = self._rate_limit_profile_key()
        KisRestClient._last_response_completed_at_by_profile[key] = time.monotonic()
        KisRestClient._last_response_path_by_profile[key] = str(path)

    def _balance_pair_pacing_state(
        self,
        *,
        path: str,
        attempt_no: int,
        now: float,
    ) -> tuple[bool, float]:
        if (
            str(self.credentials.env).strip().lower() != "vps"
            or path != self.OVERSEAS_BALANCE_PATH
            or attempt_no != 1
        ):
            return False, 0.0
        key = self._rate_limit_profile_key()
        if (
            KisRestClient._last_response_path_by_profile.get(key, "")
            != self.OVERSEAS_BALANCE_PATH
        ):
            return False, 0.0
        completed_at = KisRestClient._last_response_completed_at_by_profile.get(
            key,
            0.0,
        )
        if completed_at <= 0.0 or completed_at > now:
            return False, 0.0
        return True, completed_at + self._vps_balance_pair_response_interval_sec

    def _activate_adaptive_rate_limit_pacing(self) -> None:
        if str(self.credentials.env).strip().lower() != "vps":
            return
        now = time.monotonic()
        key = self._rate_limit_profile_key()
        KisRestClient._adaptive_rate_limit_until_by_profile[key] = max(
            KisRestClient._adaptive_rate_limit_until_by_profile.get(key, 0.0),
            now + self._vps_adaptive_window_sec,
        )

    def _adaptive_rate_limit_state(
        self,
        *,
        now: float,
    ) -> tuple[bool, float]:
        if str(self.credentials.env).strip().lower() != "vps":
            return False, 0.0
        key = self._rate_limit_profile_key()
        active_until = KisRestClient._adaptive_rate_limit_until_by_profile.get(
            key,
            0.0,
        )
        if now >= active_until:
            return False, 0.0
        completed_at = KisRestClient._last_response_completed_at_by_profile.get(
            key,
            0.0,
        )
        if completed_at <= 0.0 or completed_at > now:
            return True, 0.0
        return True, completed_at + self._vps_adaptive_response_interval_sec

    def _response_retry_reason(
        self,
        *,
        method: str,
        msg_cd: str,
        attempt_no: int,
        max_attempts: int,
    ) -> str:
        if attempt_no >= max_attempts:
            return ""
        if msg_cd == "EGW00123":
            return "token_expired"
        if msg_cd in self._RATE_LIMIT_MSG_CODES:
            return "rate_limit"
        if (
            str(self.credentials.env).strip().lower() == "vps"
            and method.upper() == "GET"
            and msg_cd in self._VPS_TRANSIENT_READ_MSG_CODES
        ):
            return "service_delay"
        if (
            str(self.credentials.env).strip().lower() == "vps"
            and method.upper() == "GET"
            and msg_cd in self._VPS_GATEWAY_ROUTING_MSG_CODES
        ):
            return "gateway_routing"
        return ""

    @staticmethod
    def _response_retry_delay_sec(*, retry_reason: str, attempt_no: int) -> float:
        if retry_reason == "token_expired":
            return 0.2
        if retry_reason == "rate_limit":
            return 1.0
        if retry_reason == "service_delay":
            return min(2.0**attempt_no, 4.0)
        if retry_reason == "gateway_routing":
            return min(2.0**attempt_no, 4.0)
        return 0.0

    async def _pace_overseas_history_continuation(self) -> None:
        if str(self.credentials.env).strip().lower() != "vps":
            return
        await asyncio.sleep(self._vps_overseas_history_continuation_delay_sec)

    async def _throttle(
        self,
        *,
        path: str = "",
        attempt_no: int = 1,
    ) -> tuple[bool, int, bool, int]:
        if KisRestClient._rate_limit_lock is None:
            KisRestClient._rate_limit_lock = asyncio.Lock()
        async with KisRestClient._rate_limit_lock:
            return await asyncio.to_thread(
                self._throttle_across_processes,
                path=path,
                attempt_no=attempt_no,
            )

    def _throttle_across_processes(
        self,
        *,
        path: str = "",
        attempt_no: int = 1,
    ) -> tuple[bool, int, bool, int]:
        interval_sec = self._request_interval_sec()
        state_path = self.credentials.token_cache_path.with_suffix(
            f"{self.credentials.token_cache_path.suffix}.rate_limit"
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            state_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r+", encoding="ascii") as state_file:
                descriptor = -1
                fcntl.flock(state_file.fileno(), fcntl.LOCK_EX)
                try:
                    raw_last_request = state_file.read().strip()
                    try:
                        last_request_at = float(raw_last_request or 0.0)
                    except ValueError:
                        last_request_at = 0.0
                    started_at = time.monotonic()
                    if last_request_at > started_at:
                        last_request_at = 0.0
                    request_start_ready_at = (
                        last_request_at + interval_sec
                        if last_request_at > 0.0
                        else started_at
                    )
                    adaptive_active = False
                    adaptive_ready_at = 0.0
                    balance_pair_active = False
                    balance_pair_ready_at = 0.0
                    while True:
                        now = time.monotonic()
                        adaptive_active, adaptive_ready_at = (
                            self._adaptive_rate_limit_state(now=now)
                        )
                        balance_pair_active, balance_pair_ready_at = (
                            self._balance_pair_pacing_state(
                                path=path,
                                attempt_no=attempt_no,
                                now=now,
                            )
                        )
                        ready_at = max(
                            request_start_ready_at,
                            adaptive_ready_at,
                            balance_pair_ready_at,
                        )
                        if ready_at <= now:
                            break
                        time.sleep(ready_at - now)
                    dispatched_at = time.monotonic()
                    baseline_ready_at = max(started_at, request_start_ready_at)
                    balance_pair_wait_ms = int(
                        max(
                            0.0,
                            balance_pair_ready_at - baseline_ready_at,
                        )
                        * 1000
                        if balance_pair_active
                        else 0
                    )
                    adaptive_wait_ms = int(
                        max(
                            0.0,
                            adaptive_ready_at
                            - max(baseline_ready_at, balance_pair_ready_at),
                        )
                        * 1000
                        if adaptive_active
                        else 0
                    )
                    state_file.seek(0)
                    state_file.truncate()
                    state_file.write(f"{dispatched_at:.9f}")
                    state_file.flush()
                    return (
                        adaptive_active,
                        adaptive_wait_ms,
                        balance_pair_active,
                        balance_pair_wait_ms,
                    )
                finally:
                    fcntl.flock(state_file.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _log_api_call(
        self,
        *,
        method: str,
        path: str,
        tr_id: str,
        started_at: float,
        success: bool,
        http_status: int | None = None,
        msg_cd: str = "",
        msg1: str = "",
        logical_request_id: str = "",
        attempt_no: int = 1,
        max_attempts: int = 1,
        retry_scheduled: bool = False,
        retry_reason: str = "",
        logical_terminal: bool = True,
        dispatched_at: str = "",
        throttle_wait_ms: int | None = None,
        network_elapsed_ms: int | None = None,
        adaptive_pacing_active: bool = False,
        adaptive_wait_ms: int | None = None,
        balance_pair_pacing_active: bool = False,
        balance_pair_wait_ms: int | None = None,
    ) -> None:
        if self._on_api_call is None:
            return
        try:
            self._on_api_call(
                {
                    "method": method,
                    "path": path,
                    "tr_id": tr_id,
                    "success": success,
                    "http_status": http_status,
                    "msg_cd": msg_cd,
                    "msg1": msg1,
                    "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                    "logical_request_id": logical_request_id,
                    "attempt_no": attempt_no,
                    "max_attempts": max_attempts,
                    "retry_scheduled": retry_scheduled,
                    "retry_reason": retry_reason,
                    "logical_terminal": logical_terminal,
                    "dispatched_at": dispatched_at,
                    "throttle_wait_ms": throttle_wait_ms,
                    "network_elapsed_ms": network_elapsed_ms,
                    "adaptive_pacing_active": adaptive_pacing_active,
                    "adaptive_wait_ms": adaptive_wait_ms,
                    "balance_pair_pacing_active": balance_pair_pacing_active,
                    "balance_pair_wait_ms": balance_pair_wait_ms,
                }
            )
        except Exception:  # noqa: BLE001
            _logger.exception("api_call_telemetry_callback_failed")

    async def __aenter__(self) -> "KisRestClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _load_cached_token(self) -> bool:
        cache_path = self.credentials.token_cache_path
        if not cache_path.exists():
            return False

        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        token = str(raw.get("access_token", "")).strip()
        expires_at = float(raw.get("expires_at", 0.0) or 0.0)
        if not token or expires_at <= time.time() + 120:
            return False

        self._token = token
        self._expires_at = expires_at
        return True

    def _save_cached_token(self) -> None:
        cache_path = self.credentials.token_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": self._token,
            "expires_at": self._expires_at,
        }
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        cache_path.chmod(0o600)

    def _invalidate_token(self) -> None:
        self._token = None
        self._expires_at = 0.0
        cache_path = self.credentials.token_cache_path
        try:
            if cache_path.exists():
                cache_path.unlink()
        except OSError:
            pass

    async def ensure_token(self) -> str:
        """Issue or reuse an access token.

        Official KIS samples use `/oauth2/tokenP` with `grant_type=client_credentials`.
        We keep the token in memory for this process only.
        """

        if not self.credentials.appkey or not self.credentials.appsecret:
            raise MissingCredentialsError(
                "KIS appkey/appsecret are missing. Populate the active profile keys file or matching env vars first."
            )

        now = time.time()
        if self._token and now < self._expires_at - 120:
            return self._token
        if self._load_cached_token():
            return self._token or ""

        last_exc: Exception | None = None
        body: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(
                    f"{self.credentials.base_url}{self.TOKEN_PATH}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "grant_type": "client_credentials",
                        "appkey": self.credentials.appkey,
                        "appsecret": self.credentials.appsecret,
                    },
                )
                response.raise_for_status()
                body = response.json()
                last_exc = None
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
        if last_exc is not None:
            raise KisApiError(f"token_request_failed: {last_exc}") from last_exc
        if body is None:
            raise KisApiError("token_request_failed: empty_response")

        token = body.get("access_token", "")
        if not token:
            raise KisApiError(f"token error: {body}")

        expires_dt = str(body.get("access_token_token_expired", "")).strip()
        self._token = token
        if expires_dt:
            self._expires_at = datetime.strptime(expires_dt, "%Y-%m-%d %H:%M:%S").timestamp()
        else:
            self._expires_at = now + (60 * 60 * 23)
        self._save_cached_token()

        return token

    async def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        include_response_headers: bool = False,
    ) -> dict[str, Any]:
        # KIS는 초당 호출 제한 응답(EGW00201)을 줄 수 있다.
        # 토큰 만료(EGW00123)도 간헐적으로 발생할 수 있어 자동 갱신 후 재시도한다.
        max_attempts = 3
        logical_request_id = uuid.uuid4().hex
        for attempt in range(max_attempts):
            attempt_no = attempt + 1
            started_at = time.monotonic()
            token = await self.ensure_token()
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.credentials.appkey,
                "appsecret": self.credentials.appsecret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            if extra_headers:
                headers.update(extra_headers)
            throttle_started_at = time.monotonic()
            (
                adaptive_pacing_active,
                adaptive_wait_ms,
                balance_pair_pacing_active,
                balance_pair_wait_ms,
            ) = await self._throttle(
                path=path,
                attempt_no=attempt_no,
            )
            throttle_wait_ms = int(
                (time.monotonic() - throttle_started_at) * 1000
            )
            dispatched_at = datetime.now(timezone.utc).isoformat()
            network_started_at = time.monotonic()
            try:
                response = await self._client.request(
                    method=method,
                    url=f"{self.credentials.base_url}{path}",
                    headers=headers,
                    params=params if method == "GET" else None,
                    json=body if method == "POST" else None,
                )
            except httpx.HTTPError as exc:
                error_detail = str(exc).strip() or type(exc).__name__
                network_elapsed_ms = int(
                    (time.monotonic() - network_started_at) * 1000
                )
                self._record_response_completion(path)
                self._log_api_call(
                    method=method,
                    path=path,
                    tr_id=tr_id,
                    started_at=started_at,
                    success=False,
                    msg1=f"transport_error: {error_detail}"[:200],
                    logical_request_id=logical_request_id,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                    retry_scheduled=attempt < max_attempts - 1,
                    retry_reason="transport_error" if attempt < max_attempts - 1 else "",
                    logical_terminal=attempt >= max_attempts - 1,
                    dispatched_at=dispatched_at,
                    throttle_wait_ms=throttle_wait_ms,
                    network_elapsed_ms=network_elapsed_ms,
                    adaptive_pacing_active=adaptive_pacing_active,
                    adaptive_wait_ms=adaptive_wait_ms,
                    balance_pair_pacing_active=balance_pair_pacing_active,
                    balance_pair_wait_ms=balance_pair_wait_ms,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.0)
                    continue
                raise KisApiError(
                    f"{tr_id} transport_error: {error_detail}"
                ) from exc
            network_elapsed_ms = int(
                (time.monotonic() - network_started_at) * 1000
            )
            self._record_response_completion(path)
            try:
                payload = response.json()
            except json.JSONDecodeError:
                self._log_api_call(
                    method=method,
                    path=path,
                    tr_id=tr_id,
                    started_at=started_at,
                    success=False,
                    http_status=response.status_code,
                    msg1="non_json_response",
                    logical_request_id=logical_request_id,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                    logical_terminal=True,
                    dispatched_at=dispatched_at,
                    throttle_wait_ms=throttle_wait_ms,
                    network_elapsed_ms=network_elapsed_ms,
                    adaptive_pacing_active=adaptive_pacing_active,
                    adaptive_wait_ms=adaptive_wait_ms,
                    balance_pair_pacing_active=balance_pair_pacing_active,
                    balance_pair_wait_ms=balance_pair_wait_ms,
                )
                response.raise_for_status()
                raise

            msg_cd = str(payload.get("msg_cd") or "")
            if msg_cd in self._RATE_LIMIT_MSG_CODES:
                self._activate_adaptive_rate_limit_pacing()
            if response.status_code >= 400:
                retry_reason = self._response_retry_reason(
                    method=method,
                    msg_cd=msg_cd,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                )
                self._log_api_call(
                    method=method,
                    path=path,
                    tr_id=tr_id,
                    started_at=started_at,
                    success=False,
                    http_status=response.status_code,
                    msg_cd=msg_cd,
                    msg1=str(payload.get("msg1") or ""),
                    logical_request_id=logical_request_id,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                    retry_scheduled=bool(retry_reason),
                    retry_reason=retry_reason,
                    logical_terminal=not bool(retry_reason),
                    dispatched_at=dispatched_at,
                    throttle_wait_ms=throttle_wait_ms,
                    network_elapsed_ms=network_elapsed_ms,
                    adaptive_pacing_active=adaptive_pacing_active,
                    adaptive_wait_ms=adaptive_wait_ms,
                    balance_pair_pacing_active=balance_pair_pacing_active,
                    balance_pair_wait_ms=balance_pair_wait_ms,
                )
                if retry_reason:
                    if retry_reason == "token_expired":
                        self._invalidate_token()
                    await asyncio.sleep(
                        self._response_retry_delay_sec(
                            retry_reason=retry_reason,
                            attempt_no=attempt_no,
                        )
                    )
                    continue
                raise KisApiError(
                    f"{tr_id} http_error={response.status_code} "
                    f"{payload.get('msg_cd')} {payload.get('msg1')}"
                )

            if str(payload.get("rt_cd", "")) == "0":
                self._log_api_call(
                    method=method,
                    path=path,
                    tr_id=tr_id,
                    started_at=started_at,
                    success=True,
                    http_status=response.status_code,
                    msg_cd=str(payload.get("msg_cd") or ""),
                    msg1=str(payload.get("msg1") or ""),
                    logical_request_id=logical_request_id,
                    attempt_no=attempt_no,
                    max_attempts=max_attempts,
                    logical_terminal=True,
                    dispatched_at=dispatched_at,
                    throttle_wait_ms=throttle_wait_ms,
                    network_elapsed_ms=network_elapsed_ms,
                    adaptive_pacing_active=adaptive_pacing_active,
                    adaptive_wait_ms=adaptive_wait_ms,
                    balance_pair_pacing_active=balance_pair_pacing_active,
                    balance_pair_wait_ms=balance_pair_wait_ms,
                )
                if include_response_headers:
                    response_headers = getattr(response, "headers", {}) or {}
                    payload = dict(payload)
                    payload["_response_headers"] = {
                        str(key).lower(): str(value)
                        for key, value in response_headers.items()
                    }
                return payload

            retry_reason = self._response_retry_reason(
                method=method,
                msg_cd=msg_cd,
                attempt_no=attempt_no,
                max_attempts=max_attempts,
            )
            self._log_api_call(
                method=method,
                path=path,
                tr_id=tr_id,
                started_at=started_at,
                success=False,
                http_status=response.status_code,
                msg_cd=msg_cd,
                msg1=str(payload.get("msg1") or ""),
                logical_request_id=logical_request_id,
                attempt_no=attempt_no,
                max_attempts=max_attempts,
                retry_scheduled=bool(retry_reason),
                retry_reason=retry_reason,
                logical_terminal=not bool(retry_reason),
                dispatched_at=dispatched_at,
                throttle_wait_ms=throttle_wait_ms,
                network_elapsed_ms=network_elapsed_ms,
                adaptive_pacing_active=adaptive_pacing_active,
                adaptive_wait_ms=adaptive_wait_ms,
                balance_pair_pacing_active=balance_pair_pacing_active,
                balance_pair_wait_ms=balance_pair_wait_ms,
            )
            if retry_reason:
                if retry_reason == "token_expired":
                    self._invalidate_token()
                await asyncio.sleep(
                    self._response_retry_delay_sec(
                        retry_reason=retry_reason,
                        attempt_no=attempt_no,
                    )
                )
                continue

            raise KisApiError(
                f"{tr_id} error: {payload.get('msg_cd')} {payload.get('msg1')}"
            )

        raise KisApiError(f"{tr_id} rate-limit retries exhausted")

    @staticmethod
    def _mask_account(account_no: str) -> str | None:
        if not account_no:
            return None
        if len(account_no) < 4:
            return account_no
        return f"{account_no[:4]}...{account_no[-2:]}"

    def account_parts(self) -> tuple[str, str]:
        if not self.credentials.account_no or not self.credentials.account_product_code:
            raise MissingCredentialsError(
                "KIS_ACCOUNT_NO / KIS_ACCOUNT_PRODUCT_CODE are missing. "
                "Provide 8-digit account number and 2-digit product code."
            )
        return self.credentials.account_no, self.credentials.account_product_code

    def product_type_code_for_exchange(self, exchange_code: str) -> str:
        exchange_upper = exchange_code.upper()
        mapping = {
            "NAS": "512",
            "NASD": "512",
            "NYSE": "513",
            "NYS": "513",
            "AMEX": "529",
            "AMS": "529",
            "SEHK": "501",
            "SHAA": "551",
            "SZAA": "552",
            "TKSE": "515",
            "HASE": "507",
            "VNSE": "508",
        }
        if exchange_upper not in mapping:
            raise KisApiError(f"unsupported overseas exchange code: {exchange_code}")
        return mapping[exchange_upper]

    def overseas_quote_exchange_code(self, exchange_code: str) -> str:
        exchange_upper = exchange_code.upper()
        mapping = {
            "NAS": "NAS",
            "NASD": "NAS",
            "NYSE": "NYS",
            "NYS": "NYS",
            "AMEX": "AMS",
            "AMS": "AMS",
        }
        return mapping.get(exchange_upper, exchange_upper)

    def overseas_order_exchange_code(self, exchange_code: str) -> str:
        exchange_upper = exchange_code.upper()
        mapping = {
            "NAS": "NASD",
            "NASD": "NASD",
            "NYSE": "NYSE",
            "NYS": "NYSE",
            "AMEX": "AMEX",
            "AMS": "AMEX",
        }
        return mapping.get(exchange_upper, exchange_upper)

    async def get_current_price(
        self,
        stock_code: str,
        market_code: str = "J",
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            self.DOMESTIC_PRICE_PATH,
            "FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": stock_code,
            },
        )
        output = payload.get("output", {}) or {}
        current_price = parse_kis_number(output.get("stck_prpr"))
        reference_price = parse_kis_number(output.get("stck_sdpr"))
        volume = parse_kis_number(output.get("acml_vol"))
        turnover = parse_kis_number(output.get("acml_tr_pbmn"))

        return {
            "stock_code": stock_code,
            "current_price": current_price,
            "reference_price": reference_price,
            "volume": volume,
            "turnover_krw": turnover,
            "open_price": parse_kis_number(output.get("stck_oprc")),
            "high_price": parse_kis_number(output.get("stck_hgpr")),
            "low_price": parse_kis_number(output.get("stck_lwpr")),
            "product_type": str(output.get("rprs_mrkt_kor_name", "") or "").strip(),
            "sector_name": str(output.get("bstp_kor_isnm", "") or "").strip(),
            "raw": output,
        }

    async def get_orderbook(
        self,
        stock_code: str,
        market_code: str = "J",
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            self.DOMESTIC_ASKING_PATH,
            "FHKST01010200",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": stock_code,
            },
        )
        output1 = payload.get("output1", {}) or {}
        output2 = payload.get("output2", {}) or {}

        best_ask = parse_kis_number(output1.get("askp1"))
        best_bid = parse_kis_number(output1.get("bidp1"))
        ask_size = parse_kis_number(output1.get("askp_rsqn1"))
        bid_size = parse_kis_number(output1.get("bidp_rsqn1"))
        expected_price = parse_kis_number(output2.get("antc_cnpr"))

        mid_price = (best_ask + best_bid) / 2 if best_ask and best_bid else float(best_ask or best_bid)
        spread_pct = 0.0
        if best_ask > 0 and best_bid > 0 and mid_price > 0:
            spread_pct = (best_ask - best_bid) / mid_price

        return {
            "stock_code": stock_code,
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_size": ask_size,
            "bid_size": bid_size,
            "expected_price": expected_price,
            "mid_price": mid_price,
            "spread_pct": spread_pct,
            "raw_orderbook": output1,
            "raw_expected": output2,
        }

    async def get_etf_etn_current_price(
        self,
        stock_code: str,
        market_code: str = "J",
    ) -> dict[str, Any]:
        """Return ETF/ETN NAV and tracking metadata from the product endpoint."""
        payload = await self._request(
            "GET",
            self.ETF_ETN_PRICE_PATH,
            "FHPST02400000",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": stock_code,
            },
        )
        output = payload.get("output", {}) or {}
        current_price = parse_kis_float(output.get("stck_prpr"))
        nav = parse_kis_float(output.get("nav"))
        return {
            "stock_code": stock_code,
            "current_price": current_price,
            "nav": nav,
            "nav_deviation_pct": (
                (current_price - nav) / nav
                if current_price > 0 and nav > 0
                else None
            ),
            "reported_deviation_pct": parse_kis_float(output.get("dprt")),
            "tracking_error_pct": parse_kis_float(output.get("trc_errt")),
            "tracking_multiplier": parse_kis_float(
                output.get("etf_trc_ert_mltp")
            ),
            "lp_orderable_code": str(
                output.get("lp_oder_able_cls_code", "") or ""
            ).strip(),
            "vi_code": str(output.get("vi_cls_code", "") or "").strip(),
            "raw": output,
        }

    async def get_daily_chart(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        market_code: str = "J",
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            self.DOMESTIC_DAILY_PATH,
            "FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        return payload.get("output2", []) or []

    async def get_domestic_index_daily_prices(
        self,
        *,
        index_code: str = "0001",
        start_date: str,
        end_date: str,
        period: str = "D",
    ) -> list[dict[str, Any]]:
        """Return KRX index OHLCV rows; ``0001`` is KOSPI."""
        payload = await self._request(
            "GET",
            self.DOMESTIC_INDEX_DAILY_PATH,
            "FHKUP03500100",
            params={
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": index_code,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": period,
            },
        )
        return self._coerce_kis_list(payload.get("output2"))

    async def get_domestic_futures_continuous_daily_snapshot(
        self,
        *,
        index_code: str = "101000",
        start_date: str,
        end_date: str,
        period: str = "D",
    ) -> dict[str, Any]:
        """Return the current KOSPI200 front-futures summary and daily row."""
        payload = await self._request(
            "GET",
            self.DOMESTIC_FUTURE_DAILY_PATH,
            "FHKIF03020100",
            params={
                "FID_COND_MRKT_DIV_CODE": "F",
                "FID_INPUT_ISCD": index_code,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": period,
            },
        )
        summary = payload.get("output1")
        return {
            "summary": dict(summary) if isinstance(summary, dict) else {},
            "rows": self._coerce_kis_list(payload.get("output2")),
        }

    async def get_time_daily_chart(
        self,
        stock_code: str,
        target_date: str,
        end_time: str = "153000",
        market_code: str = "J",
        include_previous: str = "Y",
        include_fake_tick: str = "",
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            self.DOMESTIC_TIME_DAILY_PATH,
            "FHKST03010230",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_HOUR_1": end_time,
                "FID_INPUT_DATE_1": target_date,
                "FID_PW_DATA_INCU_YN": include_previous,
                "FID_FAKE_TICK_INCU_YN": include_fake_tick,
            },
        )
        return payload.get("output2", []) or []

    async def get_domestic_volume_rank(
        self,
        market_code: str = "J",
        top_n: int = 30,
        min_price_krw: int = 5000,
        min_volume: int = 100_000,
    ) -> list[dict]:
        """국내 거래량 순위 (FHPST01710000). 1~5분 집계 지연이 있을 수 있다."""
        payload = await self._request(
            "GET",
            self.DOMESTIC_RANKING_PATH,
            "FHPST01710000",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": str(min_price_krw),
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": str(min_volume),
                "FID_INPUT_DATE_1": "",
            },
        )
        output = payload.get("output", []) or []
        results: list[dict] = []
        for row in output[:top_n]:
            code = str(
                row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or ""
            ).strip()
            if not code:
                continue
            results.append(
                {
                    "stock_code": code,
                    "name": str(row.get("hts_kor_isnm", "")),
                    "price": parse_kis_number(row.get("stck_prpr")),
                    "change_rate": parse_kis_number(row.get("prdy_ctrt")),
                    "volume": parse_kis_number(row.get("acml_vol")),
                    "turnover_krw": parse_kis_number(row.get("acml_tr_pbmn")),
                }
            )
        return results

    async def get_domestic_fluctuation_rank(
        self,
        market_code: str = "J",
        top_n: int = 15,
        min_price_krw: int = 5000,
        min_volume: int = 100_000,
        ascending: bool = False,
    ) -> list[dict]:
        """국내 등락률 순위 (FHPST01720000). ascending=False 이면 상승률 순."""
        payload = await self._request(
            "GET",
            self.DOMESTIC_FLUCTUATION_PATH,
            "FHPST01720000",
            params={
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_COND_SCR_DIV_CODE": "20172",
                "FID_INPUT_ISCD": "0000",
                "FID_RANK_SORT_CLS_CODE": "1" if ascending else "0",
                "FID_INPUT_PRICE_1": str(min_price_krw),
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": str(min_volume),
                "FID_INPUT_DATE_1": "",
            },
        )
        output = payload.get("output", []) or []
        results: list[dict] = []
        for row in output[:top_n]:
            code = str(
                row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or ""
            ).strip()
            if not code:
                continue
            results.append(
                {
                    "stock_code": code,
                    "name": str(row.get("hts_kor_isnm", "")),
                    "price": parse_kis_number(row.get("stck_prpr")),
                    "change_rate": parse_kis_number(row.get("prdy_ctrt")),
                    "volume": parse_kis_number(row.get("acml_vol")),
                    "turnover_krw": parse_kis_number(row.get("acml_tr_pbmn")),
                }
            )
        return results

    async def get_balance(self) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTC8434R" if self.credentials.env == "prod" else "VTTC8434R"
        payload = await self._request(
            "GET",
            self.DOMESTIC_BALANCE_PATH,
            tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        positions = payload.get("output1", []) or []
        summary_rows = payload.get("output2", []) or []
        return {
            "account_masked": self._mask_account(cano),
            "positions": positions,
            "position_count": len(positions),
            "summary": summary_rows[0] if summary_rows else {},
        }

    async def get_possible_order(
        self,
        stock_code: str,
        price: int,
        *,
        order_division: str = "01",
        include_cma_value: str = "N",
        include_overseas: str = "N",
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTC8908R" if self.credentials.env == "prod" else "VTTC8908R"
        payload = await self._request(
            "GET",
            self.DOMESTIC_POSSIBLE_ORDER_PATH,
            tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "PDNO": stock_code,
                "ORD_UNPR": str(price),
                "ORD_DVSN": order_division,
                "CMA_EVLU_AMT_ICLD_YN": include_cma_value,
                "OVRS_ICLD_YN": include_overseas,
            },
        )
        output = payload.get("output", {}) or {}
        return {
            "stock_code": stock_code,
            "order_price": price,
            "order_division": order_division,
            "max_buy_qty": parse_kis_number(output.get("max_buy_qty")),
            "nrcvb_buy_qty": parse_kis_number(output.get("nrcvb_buy_qty")),
            "max_buy_amt": parse_kis_number(output.get("max_buy_amt")),
            "nrcvb_buy_amt": parse_kis_number(output.get("nrcvb_buy_amt")),
            "ord_psbl_cash": parse_kis_number(output.get("ord_psbl_cash")),
            "raw": output,
        }

    async def get_overseas_price(
        self,
        symbol: str,
        exchange_code: str,
        *,
        auth: str = "",
    ) -> dict[str, Any]:
        quote_exchange_code = self.overseas_quote_exchange_code(exchange_code)
        payload = await self._request(
            "GET",
            self.OVERSEAS_PRICE_PATH,
            "HHDFS00000300",
            params={
                "AUTH": auth,
                "EXCD": quote_exchange_code,
                "SYMB": symbol,
            },
        )
        output = payload.get("output", {}) or {}
        return {
            "symbol": symbol,
            "exchange_code": quote_exchange_code,
            "last_price": output.get("last"),
            "change": output.get("diff"),
            "change_rate": output.get("rate"),
            "bid": output.get("bid"),
            "ask": output.get("ask"),
            "volume": output.get("tvol"),
            "raw": output,
        }

    async def get_overseas_search_info(
        self,
        symbol: str,
        exchange_code: str,
    ) -> dict[str, Any]:
        if self.credentials.env != "prod":
            raise KisApiError(
                "overseas search-info is not available in KIS mock mode. Use price quote checks instead."
            )
        payload = await self._request(
            "GET",
            self.OVERSEAS_SEARCH_INFO_PATH,
            "CTPF1702R",
            params={
                "PRDT_TYPE_CD": self.product_type_code_for_exchange(exchange_code),
                "PDNO": symbol,
            },
        )
        output = payload.get("output", {}) or {}
        return {
            "symbol": symbol,
            "exchange_code": exchange_code,
            "name": output.get("prdt_name"),
            "currency": output.get("tr_crcy_cd"),
            "raw": output,
        }

    async def get_overseas_daily_prices(
        self,
        symbol: str,
        exchange_code: str,
        *,
        auth: str = "",
        period_type: str = "0",
        base_date: str = "",
        adjusted_price: bool = True,
    ) -> list[dict[str, Any]]:
        quote_exchange_code = self.overseas_quote_exchange_code(exchange_code)
        payload = await self._request(
            "GET",
            self.OVERSEAS_DAILY_PRICE_PATH,
            "HHDFS76240000",
            params={
                "AUTH": auth,
                "EXCD": quote_exchange_code,
                "SYMB": symbol,
                "GUBN": period_type,
                "BYMD": base_date,
                "MODP": "1" if adjusted_price else "0",
            },
        )
        return self._coerce_kis_list(
            payload.get("output2")
            or payload.get("output2_head")
            or payload.get("output")
        )

    async def get_overseas_index_daily_prices(
        self,
        *,
        index_code: str = "COMP",
        start_date: str,
        end_date: str,
        period: str = "D",
    ) -> list[dict[str, Any]]:
        """Return overseas index OHLCV rows; ``COMP`` is NASDAQ Composite."""
        payload = await self._request(
            "GET",
            self.OVERSEAS_INDEX_DAILY_PATH,
            "FHKST03030100",
            params={
                "FID_COND_MRKT_DIV_CODE": "N",
                "FID_INPUT_ISCD": index_code,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": period,
            },
        )
        return self._coerce_kis_list(payload.get("output2"))

    async def get_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str,
        *,
        auth: str = "",
        interval_minutes: int = 5,
        include_previous_day: bool = True,
        next_flag: str = "",
        record_count: int = 60,
        fill: str = "",
        next_key: str = "",
    ) -> list[dict[str, Any]]:
        quote_exchange_code = self.overseas_quote_exchange_code(exchange_code)
        payload = await self._request(
            "GET",
            self.OVERSEAS_MINUTE_CHART_PATH,
            "HHDFS76950200",
            params={
                "AUTH": auth,
                "EXCD": quote_exchange_code,
                "SYMB": symbol,
                "NMIN": str(max(int(interval_minutes), 1)),
                "PINC": "1" if include_previous_day else "0",
                "NEXT": next_flag,
                "NREC": str(min(max(int(record_count), 1), 120)),
                "FILL": fill,
                "KEYB": next_key,
            },
        )
        return self._coerce_kis_list(
            payload.get("output2")
            or payload.get("output2_head")
            or payload.get("output")
        )

    async def get_overseas_balance(
        self,
        exchange_code: str,
        currency_code: str,
        *,
        paginate: bool = True,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTS3012R" if self.credentials.env == "prod" else "VTTS3012R"
        positions: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        payload: dict[str, Any] = {}
        current_fk = ""
        current_nk = ""
        seen_contexts: set[tuple[str, str]] = set()
        page_count = 0
        page_limit = max(1, int(max_pages))
        for page_index in range(page_limit):
            payload = await self._request(
                "GET",
                self.OVERSEAS_BALANCE_PATH,
                tr_id,
                params={
                    "CANO": cano,
                    "ACNT_PRDT_CD": product_code,
                    "OVRS_EXCG_CD": exchange_code,
                    "TR_CRCY_CD": currency_code,
                    "CTX_AREA_FK200": current_fk,
                    "CTX_AREA_NK200": current_nk,
                },
                extra_headers={"tr_cont": "N"} if page_index > 0 else None,
                include_response_headers=True,
            )
            page_count += 1
            positions_raw = payload.get("output1", []) or []
            page_positions = (
                positions_raw if isinstance(positions_raw, list) else [positions_raw]
            )
            positions.extend(row for row in page_positions if isinstance(row, dict))
            summary_raw = payload.get("output2", []) or []
            if not summary:
                if isinstance(summary_raw, list):
                    summary = next(
                        (row for row in summary_raw if isinstance(row, dict)),
                        {},
                    )
                elif isinstance(summary_raw, dict):
                    summary = summary_raw

            headers = payload.get("_response_headers", {}) or {}
            tr_cont = str(headers.get("tr_cont") or "").strip().upper()
            next_fk = str(payload.get("ctx_area_fk200") or "")
            next_nk = str(payload.get("ctx_area_nk200") or "")
            context = (next_fk, next_nk)
            if (
                not paginate
                or tr_cont not in {"M", "F"}
                or (not next_fk.strip() and not next_nk.strip())
                or context in seen_contexts
            ):
                break
            seen_contexts.add(context)
            current_fk, current_nk = next_fk, next_nk
            if page_index + 1 < page_limit:
                await self._pace_overseas_history_continuation()
        return {
            "account_masked": self._mask_account(cano),
            "exchange_code": exchange_code,
            "currency_code": currency_code,
            "positions": positions,
            "position_count": len(positions),
            "summary": summary,
            "ctx_area_fk200": payload.get("ctx_area_fk200", ""),
            "ctx_area_nk200": payload.get("ctx_area_nk200", ""),
            "tr_cont": str(
                (payload.get("_response_headers", {}) or {}).get("tr_cont") or ""
            ),
            "page_count": page_count,
        }

    async def get_overseas_possible_order(
        self,
        symbol: str,
        exchange_code: str,
        price: str,
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTS3007R" if self.credentials.env == "prod" else "VTTS3007R"
        payload = await self._request(
            "GET",
            self.OVERSEAS_POSSIBLE_ORDER_PATH,
            tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "OVRS_EXCG_CD": exchange_code,
                "OVRS_ORD_UNPR": price,
                "ITEM_CD": symbol,
            },
        )
        output = payload.get("output", {}) or {}
        return {
            "symbol": symbol,
            "exchange_code": exchange_code,
            "order_price": price,
            "foreign_buy_amount_before_exchange": output.get("frcr_buy_amt1"),
            "max_order_quantity": output.get("max_ord_psbl_qty"),
            "overseas_max_order_amount": output.get("ovrs_max_ord_psbl_amt"),
            "cash_available": output.get("frcr_dncl_amt_2"),
            "raw": output,
        }

    async def get_overseas_order_history(
        self,
        *,
        symbol: str = "",
        start_date: str,
        end_date: str,
        side_filter: str = "00",
        fill_filter: str = "00",
        exchange_code: str = "",
        sort_sqn: str = "DS",
        order_date: str = "",
        order_branch_no: str = "",
        order_no: str = "",
        fk200: str = "",
        nk200: str = "",
        paginate: bool = True,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTS3035R" if self.credentials.env == "prod" else "VTTS3035R"
        rows: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        current_fk = fk200
        current_nk = nk200
        seen_contexts: set[tuple[str, str]] = set()
        page_count = 0
        page_limit = max(1, int(max_pages))
        for page_index in range(page_limit):
            payload = await self._request(
                "GET",
                self.OVERSEAS_ORDER_HISTORY_PATH,
                tr_id,
                params={
                    "CANO": cano,
                    "ACNT_PRDT_CD": product_code,
                    "PDNO": symbol,
                    "ORD_STRT_DT": start_date,
                    "ORD_END_DT": end_date,
                    "SLL_BUY_DVSN": side_filter,
                    "CCLD_NCCS_DVSN": fill_filter,
                    "OVRS_EXCG_CD": exchange_code,
                    "SORT_SQN": sort_sqn,
                    "ORD_DT": order_date,
                    "ORD_GNO_BRNO": order_branch_no,
                    "ODNO": order_no,
                    "CTX_AREA_NK200": current_nk,
                    "CTX_AREA_FK200": current_fk,
                },
                extra_headers={"tr_cont": "N"} if page_index > 0 else None,
                include_response_headers=True,
            )
            page_count += 1
            output = payload.get("output", []) or []
            page_rows = output if isinstance(output, list) else [output]
            rows.extend(row for row in page_rows if isinstance(row, dict))
            headers = payload.get("_response_headers", {}) or {}
            tr_cont = str(headers.get("tr_cont") or "").strip().upper()
            next_fk = str(payload.get("ctx_area_fk200") or "")
            next_nk = str(payload.get("ctx_area_nk200") or "")
            context = (next_fk, next_nk)
            if (
                not paginate
                or tr_cont not in {"M", "F"}
                or context in seen_contexts
            ):
                break
            seen_contexts.add(context)
            current_fk, current_nk = next_fk, next_nk
            if page_index + 1 < page_limit:
                await self._pace_overseas_history_continuation()
        return {
            "orders": rows,
            "ctx_area_fk200": payload.get("ctx_area_fk200", ""),
            "ctx_area_nk200": payload.get("ctx_area_nk200", ""),
            "tr_cont": str(
                (payload.get("_response_headers", {}) or {}).get("tr_cont") or ""
            ),
            "page_count": page_count,
            "pagination_truncated": bool(
                paginate
                and page_count >= page_limit
                and str(
                    (payload.get("_response_headers", {}) or {}).get("tr_cont")
                    or ""
                ).strip().upper()
                in {"M", "F"}
            ),
            "raw": payload,
        }

    async def get_overseas_open_orders(
        self,
        *,
        exchange_code: str,
        sort_sqn: str = "DS",
        fk200: str = "",
        nk200: str = "",
        paginate: bool = True,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """Return the production broker's current overseas unfilled orders."""
        if self.credentials.env != "prod":
            raise KisApiError(
                "TTTS3018R overseas open-order inquiry is production-only"
            )
        cano, product_code = self.account_parts()
        rows: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        current_fk = fk200
        current_nk = nk200
        seen_contexts: set[tuple[str, str]] = set()
        page_count = 0
        page_limit = max(1, int(max_pages))
        for page_index in range(page_limit):
            payload = await self._request(
                "GET",
                self.OVERSEAS_OPEN_ORDERS_PATH,
                "TTTS3018R",
                params={
                    "CANO": cano,
                    "ACNT_PRDT_CD": product_code,
                    "OVRS_EXCG_CD": exchange_code,
                    "SORT_SQN": sort_sqn,
                    "CTX_AREA_FK200": current_fk,
                    "CTX_AREA_NK200": current_nk,
                },
                extra_headers={"tr_cont": "N"} if page_index > 0 else None,
                include_response_headers=True,
            )
            page_count += 1
            output = payload.get("output", []) or []
            page_rows = output if isinstance(output, list) else [output]
            rows.extend(row for row in page_rows if isinstance(row, dict))
            headers = payload.get("_response_headers", {}) or {}
            tr_cont = str(headers.get("tr_cont") or "").strip().upper()
            next_fk = str(payload.get("ctx_area_fk200") or "")
            next_nk = str(payload.get("ctx_area_nk200") or "")
            context = (next_fk, next_nk)
            if (
                not paginate
                or tr_cont not in {"M", "F"}
                or context in seen_contexts
            ):
                break
            seen_contexts.add(context)
            current_fk, current_nk = next_fk, next_nk
        return {
            "orders": rows,
            "ctx_area_fk200": payload.get("ctx_area_fk200", ""),
            "ctx_area_nk200": payload.get("ctx_area_nk200", ""),
            "tr_cont": str(
                (payload.get("_response_headers", {}) or {}).get("tr_cont") or ""
            ),
            "page_count": page_count,
            "raw": payload,
        }

    async def get_domestic_order_history(
        self,
        *,
        symbol: str = "",
        start_date: str,
        end_date: str,
        side_filter: str = "00",
        fill_filter: str = "00",
        query_order: str = "00",
        query_type: str = "00",
        exchange_code: str = "KRX",
        order_branch_no: str = "",
        order_no: str = "",
        query_type_1: str = "",
        fk100: str = "",
        nk100: str = "",
        paginate: bool = True,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        modern_tr_id = "TTTC0081R" if self.credentials.env == "prod" else "VTTC0081R"
        legacy_tr_id = "TTTC8001R" if self.credentials.env == "prod" else "VTTC8001R"
        last_error: KisApiError | None = None
        for tr_id in (modern_tr_id, legacy_tr_id):
            rows: list[dict[str, Any]] = []
            summaries: list[dict[str, Any]] = []
            current_fk = fk100
            current_nk = nk100
            seen_contexts: set[tuple[str, str]] = set()
            page_count = 0
            payload: dict[str, Any] = {}
            try:
                for page_index in range(max(1, int(max_pages))):
                    params = {
                        "CANO": cano,
                        "ACNT_PRDT_CD": product_code,
                        "INQR_STRT_DT": start_date,
                        "INQR_END_DT": end_date,
                        "SLL_BUY_DVSN_CD": side_filter,
                        "PDNO": symbol,
                        "CCLD_DVSN": fill_filter,
                        "INQR_DVSN": query_order,
                        "INQR_DVSN_3": query_type,
                        "ORD_GNO_BRNO": order_branch_no,
                        "ODNO": order_no,
                        "INQR_DVSN_1": query_type_1,
                        "CTX_AREA_FK100": current_fk,
                        "CTX_AREA_NK100": current_nk,
                    }
                    if exchange_code:
                        params["EXCG_ID_DVSN_CD"] = exchange_code
                    payload = await self._request(
                        "GET",
                        self.DOMESTIC_ORDER_HISTORY_PATH,
                        tr_id,
                        params=params,
                        extra_headers={"tr_cont": "N"} if page_index > 0 else None,
                        include_response_headers=True,
                    )
                    page_count += 1
                    output1 = payload.get("output1")
                    if output1 is None:
                        output1 = payload.get("output")
                    rows.extend(self._coerce_kis_list(output1))
                    summary = payload.get("output2") or {}
                    if isinstance(summary, list):
                        summaries.extend(
                            item for item in summary if isinstance(item, dict)
                        )
                    elif isinstance(summary, dict) and summary:
                        summaries.append(summary)
                    headers = payload.get("_response_headers", {}) or {}
                    tr_cont = str(headers.get("tr_cont") or "").strip().upper()
                    next_fk = str(payload.get("ctx_area_fk100") or "")
                    next_nk = str(payload.get("ctx_area_nk100") or "")
                    context = (next_fk, next_nk)
                    if (
                        not paginate
                        or tr_cont not in {"M", "F"}
                        or context in seen_contexts
                    ):
                        break
                    seen_contexts.add(context)
                    current_fk, current_nk = next_fk, next_nk
            except KisApiError as exc:
                last_error = exc
                continue
            return {
                "orders": rows,
                "summary": summaries[0] if summaries else {},
                "ctx_area_fk100": payload.get("ctx_area_fk100", ""),
                "ctx_area_nk100": payload.get("ctx_area_nk100", ""),
                "tr_cont": str(
                    (payload.get("_response_headers", {}) or {}).get("tr_cont") or ""
                ),
                "page_count": page_count,
                "tr_id": tr_id,
                "raw": payload,
            }

        if last_error is not None:
            raise last_error
        raise KisApiError("domestic order history request failed")

    async def revise_or_cancel_overseas_order(
        self,
        *,
        symbol: str,
        exchange_code: str,
        original_order_no: str,
        rvse_cncl_dvsn_cd: str,
        qty: int,
        price: str,
        mgco_aptm_odno: str = "",
        ord_svr_dvsn_cd: str = "0",
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        exchange_upper = self.overseas_order_exchange_code(exchange_code)
        tr_ids = {
            "NASD": "TTTT1004U",
            "NYSE": "TTTT1004U",
            "AMEX": "TTTT1004U",
            "SEHK": "TTTS1003U",
            "SHAA": "TTTS0302U",
            "SZAA": "TTTS0306U",
            "TKSE": "TTTS0309U",
            "HASE": "TTTS0312U",
            "VNSE": "TTTS0312U",
        }
        if exchange_upper not in tr_ids:
            raise KisApiError(f"unsupported overseas exchange code: {exchange_code}")
        tr_id = tr_ids[exchange_upper]
        if self.credentials.env != "prod":
            tr_id = f"V{tr_id[1:]}"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "OVRS_EXCG_CD": exchange_upper,
            "PDNO": symbol,
            "ORGN_ODNO": original_order_no,
            "RVSE_CNCL_DVSN_CD": rvse_cncl_dvsn_cd,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": price,
            "MGCO_APTM_ODNO": mgco_aptm_odno,
            "ORD_SVR_DVSN_CD": ord_svr_dvsn_cd,
        }
        return await self._request("POST", self.OVERSEAS_REVISE_CANCEL_PATH, tr_id, body=body)

    async def revise_or_cancel_domestic_order(
        self,
        *,
        krx_order_orgno: str,
        original_order_no: str,
        order_division: str,
        rvse_cncl_dvsn_cd: str,
        qty: int,
        price: int,
        qty_all_order_yn: str,
        exchange_code: str = "KRX",
        condition_price: str = "",
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        if qty_all_order_yn.upper() == "Y":
            qty = 0
            if rvse_cncl_dvsn_cd == "02":
                price = 0
        modern_tr_id = "TTTC0013U" if self.credentials.env == "prod" else "VTTC0013U"
        legacy_tr_id = "TTTC0803U" if self.credentials.env == "prod" else "VTTC0803U"
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "KRX_FWDG_ORD_ORGNO": krx_order_orgno,
            "ORGN_ODNO": original_order_no,
            "ORD_DVSN": order_division,
            "RVSE_CNCL_DVSN_CD": rvse_cncl_dvsn_cd,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "QTY_ALL_ORD_YN": qty_all_order_yn.upper(),
        }
        if exchange_code:
            body["EXCG_ID_DVSN_CD"] = exchange_code
        if condition_price:
            body["CNDT_PRIC"] = condition_price

        last_error: KisApiError | None = None
        for tr_id in (modern_tr_id, legacy_tr_id):
            try:
                return await self._request(
                    "POST",
                    self.DOMESTIC_REVISE_CANCEL_PATH,
                    tr_id,
                    body=body,
                )
            except KisApiError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise KisApiError("domestic revise/cancel request failed")

    async def place_overseas_order(
        self,
        side: str,
        symbol: str,
        exchange_code: str,
        qty: int,
        price: str,
        *,
        order_division: str = "00",
        contact_phone: str = "",
        agency_order_no: str = "",
        order_server_division_code: str = "0",
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        exchange_upper = self.overseas_order_exchange_code(exchange_code)
        side_lower = side.lower()

        if side_lower == "buy":
            tr_ids = {
                "NASD": "TTTT1002U",
                "NYSE": "TTTT1002U",
                "AMEX": "TTTT1002U",
                "SEHK": "TTTS1002U",
                "SHAA": "TTTS0202U",
                "SZAA": "TTTS0305U",
                "TKSE": "TTTS0308U",
                "HASE": "TTTS0311U",
                "VNSE": "TTTS0311U",
            }
            sell_type = ""
        elif side_lower == "sell":
            tr_ids = {
                "NASD": "TTTT1006U",
                "NYSE": "TTTT1006U",
                "AMEX": "TTTT1006U",
                "SEHK": "TTTS1001U",
                "SHAA": "TTTS1005U",
                "SZAA": "TTTS0304U",
                "TKSE": "TTTS0307U",
                "HASE": "TTTS0310U",
                "VNSE": "TTTS0310U",
            }
            sell_type = "00"
        else:
            raise KisApiError("overseas order side must be buy or sell")

        if exchange_upper not in tr_ids:
            raise KisApiError(f"unsupported overseas exchange code: {exchange_code}")

        tr_id = tr_ids[exchange_upper]
        if self.credentials.env != "prod":
            if side_lower == "sell" and exchange_upper in {"NASD", "NYSE", "AMEX"}:
                tr_id = "VTTT1001U"
            else:
                tr_id = f"V{tr_id[1:]}"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "OVRS_EXCG_CD": exchange_upper,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": price,
            "CTAC_TLNO": contact_phone,
            "MGCO_APTM_ODNO": agency_order_no,
            "SLL_TYPE": sell_type,
            "ORD_SVR_DVSN_CD": order_server_division_code,
            "ORD_DVSN": order_division,
        }
        return await self._request("POST", self.OVERSEAS_ORDER_PATH, tr_id, body=body)

    async def place_overseas_daytime_order(
        self,
        side: str,
        symbol: str,
        exchange_code: str,
        qty: int,
        price: str,
        *,
        contact_phone: str = "",
        agency_order_no: str = "",
        order_server_division_code: str = "0",
        order_division: str = "00",
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        exchange_upper = self.overseas_order_exchange_code(exchange_code)
        if exchange_upper not in {"NASD", "NYSE", "AMEX"}:
            raise KisApiError(
                f"US daytime trading supports only NASD/NYSE/AMEX: {exchange_code}"
            )

        side_lower = side.lower()
        if side_lower == "buy":
            tr_id = "TTTS6036U"
        elif side_lower == "sell":
            tr_id = "TTTS6037U"
        else:
            raise KisApiError("overseas order side must be buy or sell")

        if self.credentials.env != "prod":
            tr_id = f"V{tr_id[1:]}"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "OVRS_EXCG_CD": exchange_upper,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": price,
            "CTAC_TLNO": contact_phone,
            "MGCO_APTM_ODNO": agency_order_no,
            "ORD_SVR_DVSN_CD": order_server_division_code,
            "ORD_DVSN": order_division,
        }
        return await self._request("POST", self.OVERSEAS_DAYTIME_ORDER_PATH, tr_id, body=body)

    async def place_overseas_order_for_current_session(
        self,
        side: str,
        symbol: str,
        exchange_code: str,
        qty: int,
        price: str,
        *,
        now_utc: datetime | None = None,
        order_division: str = "00",
        contact_phone: str = "",
        agency_order_no: str = "",
        order_server_division_code: str = "0",
    ) -> dict[str, Any]:
        exchange_upper = self.overseas_order_exchange_code(exchange_code)
        us_session = get_us_trading_session(now_utc)
        if exchange_upper in {"NASD", "NYSE", "AMEX"} and is_us_daytime_session(now_utc):
            if self.credentials.env != "prod":
                raise KisApiError(
                    "KIS mock does not support US daytime trading "
                    "(`모의투자에서는 미국주식 주간거래는 제공하지 않습니다.`)."
                )
            return await self.place_overseas_daytime_order(
                side=side,
                symbol=symbol,
                exchange_code=exchange_upper,
                qty=qty,
                price=price,
                contact_phone=contact_phone,
                agency_order_no=agency_order_no,
                order_server_division_code=order_server_division_code,
                order_division=order_division,
            )
        if exchange_upper in {"NASD", "NYSE", "AMEX"} and self.credentials.env != "prod" and us_session != "regular":
            raise KisApiError(
                "KIS mock currently supports US order tests only during the US regular session "
                f"(current_session={us_session})."
            )

        return await self.place_overseas_order(
            side=side,
            symbol=symbol,
            exchange_code=exchange_upper,
            qty=qty,
            price=price,
            order_division=order_division,
            contact_phone=contact_phone,
            agency_order_no=agency_order_no,
            order_server_division_code=order_server_division_code,
        )

    async def place_cash_order(
        self,
        side: str,
        stock_code: str,
        qty: int,
        price: int,
        *,
        order_division: str = "00",
        exchange_code: str = "KRX",
    ) -> dict[str, Any]:
        """Prepare a domestic cash order call.

        This method is included so the project can be extended to real ordering
        without reshaping the rest of the architecture. For now, operator safety
        still depends on `DRY_RUN` and `LIVE_TRADING_ENABLED`.
        """

        cano, product_code = self.account_parts()
        side_upper = side.upper()
        if self.credentials.env == "prod":
            tr_id = "TTTC0012U" if side_upper == "BUY" else "TTTC0011U"
        else:
            tr_id = "VTTC0012U" if side_upper == "BUY" else "VTTC0011U"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "PDNO": stock_code,
            "ORD_DVSN": order_division,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": exchange_code,
            "SLL_TYPE": "01" if side_upper == "SELL" else "",
            "CNDT_PRIC": "",
        }
        return await self._request(
            "POST",
            self.DOMESTIC_ORDER_CASH_PATH,
            tr_id,
            body=body,
        )

    @staticmethod
    def _coerce_kis_list(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [value]
        return []
