from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .market_calendar import is_krx_holiday, is_nyse_holiday
from .market_sessions import KST, NEW_YORK

if TYPE_CHECKING:
    from .client import KisRestClient
    from .repository import SqliteRepository

_logger = logging.getLogger(__name__)

_MARKET_CONFIG = {
    "domestic": {
        "benchmark_code": "0001",
        "benchmark_name": "KOSPI",
        "source": "KIS:FHKUP03500100",
        "timezone": KST,
        "open_time": time(9, 0),
        "final_time": time(15, 35),
        "close_field": "bstp_nmix_prpr",
        "open_field": "bstp_nmix_oprc",
        "high_field": "bstp_nmix_hgpr",
        "low_field": "bstp_nmix_lwpr",
        "turnover_field": "acml_tr_pbmn",
    },
    "overseas": {
        "benchmark_code": "COMP",
        "benchmark_name": "NASDAQ Composite",
        "source": "KIS:FHKST03030100",
        "timezone": NEW_YORK,
        "open_time": time(9, 30),
        "final_time": time(16, 5),
        "close_field": "ovrs_nmix_prpr",
        "open_field": "ovrs_nmix_oprc",
        "high_field": "ovrs_nmix_hgpr",
        "low_field": "ovrs_nmix_lwpr",
        "turnover_field": "",
    },
}


def _as_float(value: object) -> float | None:
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def classify_trend(return_pct: float | None) -> str:
    if return_pct is None:
        return "unknown"
    if return_pct <= -1.5:
        return "strong_down"
    if return_pct <= -0.3:
        return "down"
    if return_pct < 0.3:
        return "sideways"
    if return_pct < 1.5:
        return "up"
    return "strong_up"


def classify_activity(volume_ratio: float | None) -> str:
    if volume_ratio is None:
        return "unknown"
    if volume_ratio < 0.7:
        return "quiet"
    if volume_ratio <= 1.3:
        return "normal"
    if volume_ratio <= 1.8:
        return "active"
    return "very_active"


def classify_volatility(range_ratio: float | None) -> str:
    if range_ratio is None:
        return "unknown"
    if range_ratio < 0.7:
        return "calm"
    if range_ratio <= 1.3:
        return "normal"
    if range_ratio <= 2.0:
        return "high"
    return "extreme"


def is_session_final(
    market: str,
    session_date: date,
    now_utc: datetime,
) -> bool:
    config = _MARKET_CONFIG[market]
    local_now = now_utc.astimezone(config["timezone"])
    if session_date < local_now.date():
        return True
    if session_date > local_now.date():
        return False
    return local_now.time() >= config["final_time"]


def build_regime_records(
    market: str,
    rows: list[dict[str, Any]],
    *,
    captured_at: datetime | None = None,
) -> list[dict[str, Any]]:
    market_key = str(market).strip().lower()
    if market_key not in _MARKET_CONFIG:
        raise ValueError(f"unsupported market: {market}")
    config = _MARKET_CONFIG[market_key]
    now_utc = captured_at or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    normalized_by_date: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        raw_date = str(raw.get("stck_bsop_date") or "").strip()
        try:
            parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        close_price = _as_float(raw.get(config["close_field"]))
        if close_price is None or close_price <= 0:
            continue
        normalized_by_date[parsed_date.isoformat()] = {
            "date": parsed_date,
            "open_price": _as_float(raw.get(config["open_field"])),
            "high_price": _as_float(raw.get(config["high_field"])),
            "low_price": _as_float(raw.get(config["low_field"])),
            "close_price": close_price,
            "volume": _as_int(raw.get("acml_vol")),
            "turnover": (
                _as_float(raw.get(config["turnover_field"]))
                if config["turnover_field"]
                else None
            ),
            "raw": raw,
        }

    ordered = [
        normalized_by_date[key]
        for key in sorted(normalized_by_date)
    ]
    ranges: list[float | None] = []
    for index, item in enumerate(ordered):
        previous_close = (
            float(ordered[index - 1]["close_price"])
            if index > 0
            else None
        )
        high_price = item["high_price"]
        low_price = item["low_price"]
        range_pct = None
        if (
            previous_close
            and previous_close > 0
            and high_price is not None
            and low_price is not None
            and high_price >= low_price
        ):
            range_pct = (float(high_price) - float(low_price)) / previous_close * 100.0
        ranges.append(range_pct)

    records: list[dict[str, Any]] = []
    for index, item in enumerate(ordered):
        previous_close = (
            float(ordered[index - 1]["close_price"])
            if index > 0
            else None
        )
        return_pct = None
        if previous_close and previous_close > 0:
            return_pct = (
                (float(item["close_price"]) - previous_close)
                / previous_close
                * 100.0
            )

        history_start = max(0, index - 20)
        previous_items = ordered[history_start:index]
        previous_volumes = [
            int(previous["volume"])
            for previous in previous_items
            if previous["volume"] is not None and int(previous["volume"]) > 0
        ]
        previous_ranges = [
            float(value)
            for value in ranges[history_start:index]
            if value is not None and float(value) > 0
        ]
        volume_avg = fmean(previous_volumes) if previous_volumes else None
        range_avg = fmean(previous_ranges) if previous_ranges else None
        volume = item["volume"]
        volume_ratio = (
            float(volume) / volume_avg
            if volume is not None
            and int(volume) > 0
            and volume_avg is not None
            and volume_avg > 0
            and len(previous_volumes) >= 5
            else None
        )
        range_pct = ranges[index]
        range_ratio = (
            float(range_pct) / range_avg
            if range_pct is not None
            and range_avg is not None
            and range_avg > 0
            and len(previous_ranges) >= 5
            else None
        )
        trend = classify_trend(return_pct)
        activity = classify_activity(volume_ratio)
        volatility = classify_volatility(range_ratio)
        session_date = item["date"]
        records.append(
            {
                "market": market_key,
                "session_date": session_date.isoformat(),
                "benchmark_code": config["benchmark_code"],
                "benchmark_name": config["benchmark_name"],
                "source": config["source"],
                "captured_at": now_utc.astimezone(timezone.utc).isoformat(),
                "is_final": int(is_session_final(market_key, session_date, now_utc)),
                "open_price": item["open_price"],
                "high_price": item["high_price"],
                "low_price": item["low_price"],
                "close_price": item["close_price"],
                "previous_close": previous_close,
                "return_pct": return_pct,
                "volume": volume,
                "turnover": item["turnover"],
                "volume_avg_20": volume_avg,
                "volume_ratio_20": volume_ratio,
                "range_pct": range_pct,
                "range_avg_20": range_avg,
                "range_ratio_20": range_ratio,
                "trend_regime": trend,
                "activity_regime": activity,
                "volatility_regime": volatility,
                "regime_key": f"{trend}|{activity}|{volatility}",
                "sample_days": min(
                    len(previous_volumes),
                    len(previous_ranges),
                ),
                "raw_json": json.dumps(
                    item["raw"],
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
    return records


class MarketRegimeCollector:
    """Collect and persist daily KOSPI/NASDAQ market context without blocking trades."""

    def __init__(
        self,
        client: "KisRestClient",
        repository: "SqliteRepository",
        *,
        backfill_days: int = 150,
        refresh_minutes: int = 30,
    ) -> None:
        self.client = client
        self.repository = repository
        self.backfill_days = max(45, int(backfill_days))
        self.refresh_interval = timedelta(minutes=max(5, int(refresh_minutes)))
        self._last_attempt: dict[str, datetime] = {}

    def _is_trading_day(self, market: str, session_date: date) -> bool:
        if session_date.weekday() >= 5:
            return False
        if market == "domestic":
            return not is_krx_holiday(session_date)
        return not is_nyse_holiday(session_date)

    def _is_due(self, market: str, now_utc: datetime, *, force: bool) -> bool:
        if force:
            return True
        last_attempt = self._last_attempt.get(market)
        if last_attempt is not None and now_utc - last_attempt < self.refresh_interval:
            return False
        latest = self.repository.get_market_regime(market)
        if latest is None:
            return True
        config = _MARKET_CONFIG[market]
        local_now = now_utc.astimezone(config["timezone"])
        if not self._is_trading_day(market, local_now.date()):
            return False
        today = local_now.date().isoformat()
        today_record = self.repository.get_market_regime(market, today)
        if today_record is not None and int(today_record.get("is_final") or 0) == 1:
            return False
        return local_now.time() >= config["open_time"]

    async def refresh_if_due(
        self,
        now_utc: datetime | None = None,
        *,
        force: bool = False,
    ) -> dict[str, dict[str, object]]:
        current = now_utc or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        results: dict[str, dict[str, object]] = {}
        for market in ("domestic", "overseas"):
            if not self._is_due(market, current, force=force):
                results[market] = {"status": "not_due", "records": 0}
                continue
            self._last_attempt[market] = current
            results[market] = await self._refresh_market(market, current)
        return results

    async def _refresh_market(
        self,
        market: str,
        now_utc: datetime,
    ) -> dict[str, object]:
        config = _MARKET_CONFIG[market]
        method_name = (
            "get_domestic_index_daily_prices"
            if market == "domestic"
            else "get_overseas_index_daily_prices"
        )
        method = getattr(self.client, method_name, None)
        if method is None:
            return {"status": "unsupported_client", "records": 0}
        local_date = now_utc.astimezone(config["timezone"]).date()
        start_date = (local_date - timedelta(days=self.backfill_days)).strftime("%Y%m%d")
        end_date = local_date.strftime("%Y%m%d")
        try:
            rows = await method(
                index_code=config["benchmark_code"],
                start_date=start_date,
                end_date=end_date,
                period="D",
            )
            records = build_regime_records(
                market,
                list(rows or []),
                captured_at=now_utc,
            )
            for record in records:
                self.repository.upsert_market_regime(record)
            final_count = sum(int(record["is_final"]) for record in records)
            _logger.info(
                "[REGIME][%s] benchmark=%s records=%d final=%d",
                market,
                config["benchmark_name"],
                len(records),
                final_count,
            )
            return {
                "status": "updated",
                "records": len(records),
                "final_records": final_count,
            }
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[REGIME][%s] refresh_failed error=%s", market, exc)
            return {
                "status": "failed",
                "records": 0,
                "error": str(exc)[:200],
            }
