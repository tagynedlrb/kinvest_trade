from datetime import date

from kinvest_trade.market_calendar import is_krx_holiday, is_nyse_holiday


def test_is_nyse_holiday_true_for_2026_independence_day_observed() -> None:
    assert is_nyse_holiday(date(2026, 7, 3)) is True


def test_is_krx_holiday_false_for_2026_07_03() -> None:
    assert is_krx_holiday(date(2026, 7, 3)) is False
