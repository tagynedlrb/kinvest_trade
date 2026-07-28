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

REGIME_CALCULATION_VERSION = "true_range_v2"

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


def _true_range_pct(
    previous_close: float | None,
    high_price: float | None,
    low_price: float | None,
) -> float | None:
    if (
        previous_close is None
        or previous_close <= 0
        or high_price is None
        or low_price is None
        or high_price < low_price
    ):
        return None
    true_range = max(
        high_price - low_price,
        abs(high_price - previous_close),
        abs(low_price - previous_close),
    )
    return true_range / previous_close * 100.0


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
        range_pct = _true_range_pct(
            previous_close,
            None if high_price is None else float(high_price),
            None if low_price is None else float(low_price),
        )
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
                "calculation_version": REGIME_CALCULATION_VERSION,
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
        incomplete_final_retry_minutes: int = 10,
    ) -> None:
        self.client = client
        self.repository = repository
        self.backfill_days = max(45, int(backfill_days))
        self.refresh_interval = timedelta(minutes=max(5, int(refresh_minutes)))
        self.incomplete_final_retry_interval = timedelta(
            minutes=max(5, int(incomplete_final_retry_minutes))
        )
        self._last_attempt: dict[str, datetime] = {}

    @staticmethod
    def _needs_final_activity_refresh(
        market: str,
        record: dict[str, object] | None,
    ) -> bool:
        if market != "overseas" or record is None:
            return False
        return (
            int(record.get("is_final") or 0) == 1
            and int(record.get("volume") or 0) <= 0
        )

    @staticmethod
    def _needs_calculation_refresh(
        record: dict[str, object] | None,
    ) -> bool:
        return (
            record is not None
            and str(record.get("calculation_version") or "")
            != REGIME_CALCULATION_VERSION
        )

    def _has_outdated_calculation(
        self,
        market: str,
        *,
        start_date: str,
    ) -> bool:
        checker = getattr(
            self.repository,
            "has_outdated_market_regime_calculation",
            None,
        )
        if not callable(checker):
            return False
        try:
            return bool(
                checker(
                    market=market,
                    calculation_version=REGIME_CALCULATION_VERSION,
                    start_date=start_date,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[REGIME][%s] version_lookup_failed error=%s",
                market,
                exc,
            )
            return False

    def _merge_stored_raw_rows(
        self,
        market: str,
        fetched_rows: list[dict[str, Any]],
        *,
        start_date: str,
    ) -> list[dict[str, Any]]:
        config = _MARKET_CONFIG[market]
        merged: dict[str, dict[str, Any]] = {}

        def add_row(raw: object) -> None:
            if not isinstance(raw, dict):
                return
            raw_date = str(raw.get("stck_bsop_date") or "").strip()
            if len(raw_date) != 8:
                return
            close_price = _as_float(raw.get(config["close_field"]))
            if close_price is None or close_price <= 0:
                return
            merged[raw_date] = raw

        lister = getattr(self.repository, "list_market_regimes", None)
        if callable(lister):
            try:
                stored_rows = lister(
                    market=market,
                    start_date=start_date,
                    limit=max(250, self.backfill_days * 2),
                )
                for stored in reversed(list(stored_rows or [])):
                    raw = stored.get("raw_json")
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                    add_row(raw)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "[REGIME][%s] stored_raw_merge_failed error=%s",
                    market,
                    exc,
                )
        for raw in fetched_rows:
            add_row(raw)
        return list(merged.values())

    def _is_trading_day(self, market: str, session_date: date) -> bool:
        if session_date.weekday() >= 5:
            return False
        if market == "domestic":
            return not is_krx_holiday(session_date)
        return not is_nyse_holiday(session_date)

    def _is_due(self, market: str, now_utc: datetime, *, force: bool) -> bool:
        if force:
            return True
        config = _MARKET_CONFIG[market]
        local_now = now_utc.astimezone(config["timezone"])
        today = local_now.date().isoformat()
        today_record = self.repository.get_market_regime(market, today)
        needs_final_refresh = self._needs_final_activity_refresh(
            market,
            today_record,
        )
        retry_interval = (
            min(
                self.refresh_interval,
                self.incomplete_final_retry_interval,
            )
            if needs_final_refresh
            else self.refresh_interval
        )
        last_attempt = self._last_attempt.get(market)
        if last_attempt is not None and now_utc - last_attempt < retry_interval:
            return False
        latest = self.repository.get_market_regime(market)
        if latest is None:
            return True
        backfill_start = (
            local_now.date() - timedelta(days=self.backfill_days)
        ).isoformat()
        if self._needs_calculation_refresh(
            today_record or latest
        ) or self._has_outdated_calculation(
            market,
            start_date=backfill_start,
        ):
            return True
        if not self._is_trading_day(market, local_now.date()):
            return False
        if today_record is not None and int(today_record.get("is_final") or 0) == 1:
            return needs_final_refresh
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
        start_session_date = local_date - timedelta(days=self.backfill_days)
        start_date = start_session_date.strftime("%Y%m%d")
        end_date = local_date.strftime("%Y%m%d")
        try:
            fetched_rows = list(
                await method(
                    index_code=config["benchmark_code"],
                    start_date=start_date,
                    end_date=end_date,
                    period="D",
                )
                or []
            )
            rows = self._merge_stored_raw_rows(
                market,
                fetched_rows,
                start_date=start_session_date.isoformat(),
            )
            records = build_regime_records(
                market,
                rows,
                captured_at=now_utc,
            )
            for record in records:
                self.repository.upsert_market_regime(record)
            final_count = sum(int(record["is_final"]) for record in records)
            _logger.info(
                "[REGIME][%s] benchmark=%s fetched=%d merged=%d final=%d",
                market,
                config["benchmark_name"],
                len(fetched_rows),
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
