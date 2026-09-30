from datetime import date

import sys

import pytest

from kinvest_trade.market_calendar import is_krx_holiday, is_nyse_holiday


def test_is_nyse_holiday_true_for_2026_independence_day_observed() -> None:
    assert is_nyse_holiday(date(2026, 7, 3)) is True


def test_is_krx_holiday_false_for_2026_07_03() -> None:
    assert is_krx_holiday(date(2026, 7, 3)) is False


@pytest.mark.parametrize("day,closed", [
    ("2026-01-28", False), ("2026-02-16", True), ("2026-02-18", True),
    ("2026-03-02", True), ("2026-05-01", True), ("2026-06-03", True),
    ("2026-09-28", False), ("2026-10-01", False), ("2026-10-05", True),
])
def test_krx_2026_fallback_calendar(monkeypatch, day, closed):
    monkeypatch.setitem(sys.modules, "exchange_calendars", None)
    assert is_krx_holiday(date.fromisoformat(day)) is closed
