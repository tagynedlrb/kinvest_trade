from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

import httpx

from .config import KisCredentials
from .market_sessions import get_us_trading_session, is_us_daytime_session


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


class KisRestClient:
    """Thin async wrapper around a small subset of KIS REST endpoints.

    The goal here is not to recreate the entire official sample repository.
    Instead, this class exposes only the pieces this project needs right now:
    token issuance, quote polling, daily/minute chart reads, and a future-ready
    domestic cash order method.
    """

    TOKEN_PATH = "/oauth2/tokenP"
    DOMESTIC_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
    DOMESTIC_ASKING_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    DOMESTIC_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    DOMESTIC_TIME_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    DOMESTIC_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
    DOMESTIC_POSSIBLE_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    DOMESTIC_ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
    OVERSEAS_PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
    OVERSEAS_DAILY_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
    OVERSEAS_MINUTE_CHART_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
    OVERSEAS_SEARCH_INFO_PATH = "/uapi/overseas-price/v1/quotations/search-info"
    OVERSEAS_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
    OVERSEAS_POSSIBLE_ORDER_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
    OVERSEAS_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
    OVERSEAS_DAYTIME_ORDER_PATH = "/uapi/overseas-stock/v1/trading/daytime-order"

    def __init__(self, credentials: KisCredentials) -> None:
        self.credentials = credentials
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=8.0)
        )

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
    ) -> dict[str, Any]:
        # KIS는 초당 호출 제한 응답(EGW00201)을 줄 수 있다.
        # 토큰 만료(EGW00123)도 간헐적으로 발생할 수 있어 자동 갱신 후 재시도한다.
        for attempt in range(3):
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
            try:
                response = await self._client.request(
                    method=method,
                    url=f"{self.credentials.base_url}{path}",
                    headers=headers,
                    params=params if method == "GET" else None,
                    json=body if method == "POST" else None,
                )
            except httpx.HTTPError as exc:
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                raise KisApiError(f"{tr_id} transport_error: {exc}") from exc
            try:
                payload = response.json()
            except json.JSONDecodeError:
                response.raise_for_status()
                raise

            token_expired = payload.get("msg_cd") == "EGW00123"
            if response.status_code >= 400:
                if token_expired and attempt < 2:
                    self._invalidate_token()
                    await asyncio.sleep(0.2)
                    continue
                if payload.get("msg_cd") == "EGW00201" and attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                raise KisApiError(
                    f"{tr_id} http_error={response.status_code} "
                    f"{payload.get('msg_cd')} {payload.get('msg1')}"
                )

            if str(payload.get("rt_cd", "")) == "0":
                return payload

            if token_expired and attempt < 2:
                self._invalidate_token()
                await asyncio.sleep(0.2)
                continue
            if payload.get("msg_cd") == "EGW00201" and attempt < 2:
                await asyncio.sleep(1.0)
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

    def environment_division(self) -> str:
        if self.credentials.env == "prod":
            return "real"
        return "demo"

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
    ) -> dict[str, Any]:
        cano, product_code = self.account_parts()
        tr_id = "TTTS3012R" if self.credentials.env == "prod" else "VTTS3012R"
        payload = await self._request(
            "GET",
            self.OVERSEAS_BALANCE_PATH,
            tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "OVRS_EXCG_CD": exchange_code,
                "TR_CRCY_CD": currency_code,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        positions_raw = payload.get("output1", []) or []
        summary_raw = payload.get("output2", []) or []
        positions = positions_raw if isinstance(positions_raw, list) else [positions_raw]
        if isinstance(summary_raw, list):
            summary = summary_raw[0] if summary_raw else {}
        else:
            summary = summary_raw
        return {
            "account_masked": self._mask_account(cano),
            "exchange_code": exchange_code,
            "currency_code": currency_code,
            "positions": positions,
            "position_count": len(positions),
            "summary": summary,
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
