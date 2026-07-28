from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kinvest_trade.market_regime import (
    MarketRegimeCollector,
    build_regime_records,
    classify_activity,
    classify_trend,
    classify_volatility,
)
from kinvest_trade.repository import SqliteRepository


def _domestic_row(
    session_date: str,
    close: float,
    *,
    volume: int = 100,
    high: float | None = None,
    low: float | None = None,
) -> dict:
    return {
        "stck_bsop_date": session_date,
        "bstp_nmix_prpr": str(close),
        "bstp_nmix_oprc": str(close),
        "bstp_nmix_hgpr": str(high if high is not None else close + 1),
        "bstp_nmix_lwpr": str(low if low is not None else close - 1),
        "acml_vol": str(volume),
        "acml_tr_pbmn": str(volume * 1000),
    }


def _overseas_row(session_date: str, close: float, *, volume: int = 100) -> dict:
    return {
        "stck_bsop_date": session_date,
        "ovrs_nmix_prpr": str(close),
        "ovrs_nmix_oprc": str(close),
        "ovrs_nmix_hgpr": str(close + 1),
        "ovrs_nmix_lwpr": str(close - 1),
        "acml_vol": str(volume),
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-2.0, "strong_down"),
        (-0.5, "down"),
        (0.0, "sideways"),
        (0.5, "up"),
        (2.0, "strong_up"),
    ],
)
def test_classify_trend(value: float, expected: str) -> None:
    assert classify_trend(value) == expected


def test_activity_and_volatility_thresholds() -> None:
    assert classify_activity(None) == "unknown"
    assert classify_activity(0.5) == "quiet"
    assert classify_activity(1.0) == "normal"
    assert classify_activity(1.5) == "active"
    assert classify_activity(2.0) == "very_active"
    assert classify_volatility(0.5) == "calm"
    assert classify_volatility(1.0) == "normal"
    assert classify_volatility(1.5) == "high"
    assert classify_volatility(2.5) == "extreme"


def test_build_regime_records_uses_prior_history_and_marks_final() -> None:
    dates = [
        "20260720",
        "20260721",
        "20260722",
        "20260723",
        "20260724",
        "20260727",
        "20260728",
    ]
    closes = [100, 101, 102, 103, 104, 105, 103]
    rows = [
        _domestic_row(
            session_date,
            close,
            volume=200 if index == 6 else 100,
            high=106 if index == 6 else close + 1,
            low=100 if index == 6 else close - 1,
        )
        for index, (session_date, close) in enumerate(zip(dates, closes))
    ]

    records = build_regime_records(
        "domestic",
        rows,
        captured_at=datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc),
    )
    latest = records[-1]

    assert latest["session_date"] == "2026-07-28"
    assert latest["previous_close"] == 105.0
    assert latest["return_pct"] == pytest.approx(-1.9047619)
    assert latest["volume_ratio_20"] == pytest.approx(2.0)
    assert latest["trend_regime"] == "strong_down"
    assert latest["activity_regime"] == "very_active"
    assert latest["volatility_regime"] == "extreme"
    assert latest["regime_key"] == "strong_down|very_active|extreme"
    assert latest["is_final"] == 1


def test_build_regime_records_excludes_open_us_day_from_final_status() -> None:
    records = build_regime_records(
        "overseas",
        [
            _overseas_row("20260727", 100.0),
            _overseas_row("20260728", 99.0, volume=20),
        ],
        captured_at=datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc),
    )

    assert records[0]["is_final"] == 1
    assert records[1]["is_final"] == 0


def test_collector_persists_both_market_benchmarks(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "regime.db")

    class FakeClient:
        async def get_domestic_index_daily_prices(self, **_kwargs):
            return [
                _domestic_row("20260727", 100.0),
                _domestic_row("20260728", 101.0),
            ]

        async def get_overseas_index_daily_prices(self, **_kwargs):
            return [
                _overseas_row("20260727", 200.0),
                _overseas_row("20260728", 201.0),
            ]

    collector = MarketRegimeCollector(FakeClient(), repository)
    result = asyncio.run(
        collector.refresh_if_due(
            datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc),
            force=True,
        )
    )

    assert result["domestic"]["status"] == "updated"
    assert result["overseas"]["status"] == "updated"
    domestic = repository.get_market_regime("domestic", "2026-07-28")
    overseas = repository.get_market_regime("overseas", "2026-07-28")
    assert domestic is not None
    assert domestic["benchmark_name"] == "KOSPI"
    assert domestic["is_final"] == 1
    assert overseas is not None
    assert overseas["benchmark_name"] == "NASDAQ Composite"
    assert overseas["is_final"] == 0
    assert len(repository.list_market_regimes(final_only=True)) == 3


def test_collector_retries_incomplete_final_overseas_activity(
    tmp_path,
) -> None:
    repository = SqliteRepository(tmp_path / "regime.db")
    dates = [
        "20260720",
        "20260721",
        "20260722",
        "20260723",
        "20260724",
        "20260727",
        "20260728",
    ]
    initial_rows = [
        _overseas_row(
            session_date,
            100.0 + index,
            volume=0 if session_date == "20260728" else 100,
        )
        for index, session_date in enumerate(dates)
    ]
    captured_at = datetime(2026, 7, 28, 20, 5, tzinfo=timezone.utc)
    for record in build_regime_records(
        "overseas",
        initial_rows,
        captured_at=captured_at,
    ):
        repository.upsert_market_regime(record)

    class FakeClient:
        async def get_overseas_index_daily_prices(self, **_kwargs):
            return [
                _overseas_row(
                    session_date,
                    100.0 + index,
                    volume=200 if session_date == "20260728" else 100,
                )
                for index, session_date in enumerate(dates)
            ]

    collector = MarketRegimeCollector(FakeClient(), repository)
    assert collector._is_due(
        "overseas",
        captured_at + timedelta(minutes=1),
        force=False,
    )
    collector._last_attempt["overseas"] = captured_at

    assert not collector._is_due(
        "overseas",
        captured_at + timedelta(minutes=9),
        force=False,
    )
    assert collector._is_due(
        "overseas",
        captured_at + timedelta(minutes=10),
        force=False,
    )

    result = asyncio.run(
        collector.refresh_if_due(
            captured_at + timedelta(minutes=10),
        )
    )
    latest = repository.get_market_regime("overseas", "2026-07-28")

    assert result["overseas"]["status"] == "updated"
    assert latest is not None
    assert latest["volume"] == 200
    assert latest["volume_ratio_20"] == pytest.approx(2.0)
    assert latest["activity_regime"] == "very_active"
    assert not collector._is_due(
        "overseas",
        captured_at + timedelta(minutes=40),
        force=False,
    )


def test_collector_does_not_retry_zero_volume_final_domestic_regime(
    tmp_path,
) -> None:
    repository = SqliteRepository(tmp_path / "regime.db")
    captured_at = datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc)
    records = build_regime_records(
        "domestic",
        [
            _domestic_row("20260727", 100.0),
            _domestic_row("20260728", 101.0, volume=0),
        ],
        captured_at=captured_at,
    )
    for record in records:
        repository.upsert_market_regime(record)
    collector = MarketRegimeCollector(object(), repository)

    assert not collector._is_due(
        "domestic",
        captured_at + timedelta(minutes=30),
        force=False,
    )
