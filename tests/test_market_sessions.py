from datetime import datetime, timezone

import pytest

from kinvest_trade.market_sessions import (
    determine_loop_interval_sec,
    get_domestic_trading_session,
    get_domestic_session_for_env,
    get_us_trading_session,
    is_krx_execution_reconcile_window,
    is_krx_regular_session,
    is_us_execution_reconcile_window,
    is_us_orderable_session_for_env,
    is_us_regular_session,
    is_us_market_session,
    minutes_until_regular_session_close,
    minutes_until_next_tradeable_session,
    seconds_until_us_session_transition,
    us_holiday_date_for_kis_session,
)


def test_krx_regular_session_true() -> None:
    assert is_krx_regular_session(datetime(2026, 6, 25, 4, 0, tzinfo=timezone.utc))


def test_krx_regular_session_false_on_weekend() -> None:
    assert not is_krx_regular_session(datetime(2026, 6, 27, 4, 0, tzinfo=timezone.utc))


def test_us_regular_session_true() -> None:
    assert is_us_regular_session(datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc))


def test_us_premarket_is_watchable_but_not_regular() -> None:
    now = datetime(2026, 6, 25, 8, 13, tzinfo=timezone.utc)
    assert not is_us_regular_session(now)
    assert is_us_market_session(now)


def test_us_session_classified_as_daytime_during_kis_daytime() -> None:
    assert get_us_trading_session(datetime(2026, 6, 25, 2, 0, tzinfo=timezone.utc)) == "daytime"


def test_us_session_closed_before_kis_daytime_10am() -> None:
    assert get_us_trading_session(datetime(2026, 6, 25, 0, 30, tzinfo=timezone.utc)) == "closed"


def test_us_session_classified_as_premarket_during_kis_premarket() -> None:
    assert get_us_trading_session(datetime(2026, 6, 25, 8, 13, tzinfo=timezone.utc)) == "premarket"


def test_us_premarket_not_orderable_in_mock_profile() -> None:
    now = datetime(2026, 6, 25, 8, 13, tzinfo=timezone.utc)
    assert not is_us_orderable_session_for_env(now, "vps")
    assert is_us_orderable_session_for_env(now, "prod")


def test_us_regular_session_is_orderable_in_mock_profile() -> None:
    now = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)
    assert is_us_orderable_session_for_env(now, "vps")


def test_minutes_until_krx_regular_close_preserves_partial_minute() -> None:
    now = datetime(2026, 7, 29, 6, 2, 8, tzinfo=timezone.utc)

    assert minutes_until_regular_session_close("domestic", now) == 27.87


def test_minutes_until_us_regular_close_uses_new_york_clock() -> None:
    now = datetime(2026, 7, 29, 19, 0, 0, tzinfo=timezone.utc)

    assert minutes_until_regular_session_close("overseas", now) == 60.0


def test_minutes_until_regular_close_is_none_outside_regular_clock() -> None:
    before_krx = datetime(2026, 7, 29, 23, 0, 0, tzinfo=timezone.utc)
    after_us = datetime(2026, 7, 29, 21, 0, 0, tzinfo=timezone.utc)

    assert minutes_until_regular_session_close("domestic", before_krx) is None
    assert minutes_until_regular_session_close("overseas", after_us) is None


def test_minutes_until_regular_close_is_none_on_weekend() -> None:
    krx_saturday = datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc)
    us_saturday = datetime(2026, 7, 25, 17, 0, 0, tzinfo=timezone.utc)

    assert minutes_until_regular_session_close("domestic", krx_saturday) is None
    assert minutes_until_regular_session_close("overseas", us_saturday) is None


def test_us_regular_session_false_before_kis_day_session() -> None:
    assert not is_us_regular_session(datetime(2026, 6, 24, 23, 30, tzinfo=timezone.utc))


def test_us_regular_session_false_on_sunday_kst_morning() -> None:
    assert not is_us_regular_session(datetime(2026, 6, 28, 21, 0, tzinfo=timezone.utc))


def test_seconds_until_us_session_transition_near_aftermarket_close() -> None:
    now = datetime(2026, 7, 28, 21, 59, 48, tzinfo=timezone.utc)

    assert seconds_until_us_session_transition(now) == 12


def test_seconds_until_us_session_transition_near_premarket_end() -> None:
    now = datetime(2026, 7, 28, 13, 29, 0, tzinfo=timezone.utc)

    assert seconds_until_us_session_transition(now) == 60


def test_seconds_until_us_session_transition_regular_crosses_kst_midnight() -> None:
    now = datetime(2026, 7, 28, 13, 30, 0, tzinfo=timezone.utc)

    assert seconds_until_us_session_transition(now) == 23_400


def test_seconds_until_us_session_transition_returns_none_when_closed() -> None:
    now = datetime(2026, 7, 28, 22, 0, 0, tzinfo=timezone.utc)

    assert seconds_until_us_session_transition(now) is None


def test_krx_execution_reconcile_window_includes_post_close_grace() -> None:
    during_grace = datetime(2026, 7, 28, 6, 40, 0, tzinfo=timezone.utc)
    after_grace = datetime(2026, 7, 28, 7, 1, 0, tzinfo=timezone.utc)

    assert is_krx_execution_reconcile_window(during_grace)
    assert not is_krx_execution_reconcile_window(after_grace)


def test_us_vps_execution_reconcile_window_uses_regular_close_grace() -> None:
    during_regular = datetime(2026, 7, 28, 19, 30, 0, tzinfo=timezone.utc)
    during_grace = datetime(2026, 7, 28, 20, 20, 0, tzinfo=timezone.utc)
    after_grace = datetime(2026, 7, 28, 20, 31, 0, tzinfo=timezone.utc)

    assert is_us_execution_reconcile_window(during_regular, "vps")
    assert is_us_execution_reconcile_window(during_grace, "vps")
    assert not is_us_execution_reconcile_window(after_grace, "vps")


def test_us_prod_execution_reconcile_window_uses_aftermarket_close_grace() -> None:
    during_aftermarket = datetime(2026, 7, 28, 21, 30, 0, tzinfo=timezone.utc)
    during_grace = datetime(2026, 7, 28, 22, 20, 0, tzinfo=timezone.utc)
    after_grace = datetime(2026, 7, 28, 22, 31, 0, tzinfo=timezone.utc)

    assert is_us_execution_reconcile_window(during_aftermarket, "prod")
    assert is_us_execution_reconcile_window(during_grace, "prod")
    assert not is_us_execution_reconcile_window(after_grace, "prod")


def test_us_vps_execution_reconcile_window_tracks_standard_time_close() -> None:
    during_grace = datetime(2026, 12, 1, 21, 20, 0, tzinfo=timezone.utc)
    after_grace = datetime(2026, 12, 1, 21, 31, 0, tzinfo=timezone.utc)

    assert is_us_execution_reconcile_window(during_grace, "vps")
    assert not is_us_execution_reconcile_window(after_grace, "vps")


def test_us_vps_execution_reconcile_window_excludes_extended_session() -> None:
    daytime = datetime(2026, 7, 29, 2, 0, 0, tzinfo=timezone.utc)

    assert not is_us_execution_reconcile_window(daytime, "vps")
    assert is_us_execution_reconcile_window(daytime, "prod")


def test_minutes_until_next_session_returns_zero_during_krx() -> None:
    now = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    assert minutes_until_next_tradeable_session(now, "prod") == 0


def test_minutes_until_next_session_returns_zero_during_us_regular() -> None:
    now = datetime(2026, 6, 25, 16, 0, tzinfo=timezone.utc)
    assert minutes_until_next_tradeable_session(now, "vps") == 0


def test_minutes_until_next_session_during_both_closed() -> None:
    now = datetime(2026, 6, 25, 23, 0, tzinfo=timezone.utc)
    mins = minutes_until_next_tradeable_session(now, "prod")
    assert 55 <= mins <= 65


def test_minutes_until_next_session_zero_during_daytime_for_prod() -> None:
    now = datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)
    assert minutes_until_next_tradeable_session(now, "prod") == 0


def test_minutes_until_next_session_waits_for_regular_during_daytime_for_mock() -> None:
    now = datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)
    mins = minutes_until_next_tradeable_session(now, "vps")
    assert 385 <= mins <= 395


def test_minutes_until_next_session_skips_krx_holiday() -> None:
    now = datetime(2026, 12, 30, 23, 45, tzinfo=timezone.utc)
    mins = minutes_until_next_tradeable_session(now, "vps")
    assert mins > 60


def test_minutes_until_next_session_skips_nyse_holiday_regular_open() -> None:
    now = datetime(2026, 7, 3, 13, 15, tzinfo=timezone.utc)
    mins = minutes_until_next_tradeable_session(now, "vps")
    assert mins > 60


def test_us_holiday_date_for_kis_session_uses_ny_date_for_early_regular() -> None:
    now = datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc)
    assert us_holiday_date_for_kis_session(now).isoformat() == "2026-07-03"


def test_us_holiday_date_for_kis_session_uses_kst_date_for_daytime() -> None:
    now = datetime(2026, 7, 3, 1, 30, tzinfo=timezone.utc)
    assert us_holiday_date_for_kis_session(now).isoformat() == "2026-07-03"


def test_determine_loop_interval_returns_20_during_krx() -> None:
    now = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    assert determine_loop_interval_sec(now, "prod", 0) == 20


def test_determine_loop_interval_returns_120_both_closed_far() -> None:
    now = datetime(2026, 6, 27, 3, 0, tzinfo=timezone.utc)
    assert determine_loop_interval_sec(now, "prod", 0) == 120


def test_determine_loop_interval_returns_30_near_open() -> None:
    now = datetime(2026, 6, 25, 23, 45, tzinfo=timezone.utc)
    assert determine_loop_interval_sec(now, "prod", 0) == 30


def test_determine_loop_interval_stays_slow_near_nyse_holiday_open() -> None:
    now = datetime(2026, 7, 3, 13, 15, tzinfo=timezone.utc)
    assert determine_loop_interval_sec(now, "vps", 0) == 120


def test_determine_loop_interval_returns_120_on_many_errors() -> None:
    now = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    assert determine_loop_interval_sec(now, "prod", 6) == 120


@pytest.mark.parametrize("clock,venue,expected", [
    ("08:00:00", "NXT", "premarket"),
    ("08:49:59", "NXT", "premarket"),
    ("08:50:00", "NXT", "closed"),
    ("09:00:29", "NXT", "closed"),
    ("09:00:30", "NXT", "regular"),
    ("15:20:00", "NXT", "closed"),
    ("15:39:59", "NXT", "closed"),
    ("15:40:00", "NXT", "aftermarket"),
    ("20:00:00", "NXT", "closed"),
    ("08:30:00", "KRX", "pre_close_price"),
    ("08:40:00", "KRX", "closed"),
    ("09:00:00", "KRX", "regular"),
    ("15:29:59", "KRX", "regular"),
    ("15:30:00", "KRX", "closed"),
    ("15:40:00", "KRX", "after_close_price"),
    ("16:00:00", "KRX", "aftermarket"),
    ("19:59:59", "KRX", "aftermarket"),
    ("20:00:00", "KRX", "closed"),
])
def test_domestic_broker_clock_and_mock_support(clock, venue, expected):
    now = datetime.fromisoformat(f"2026-10-01T{clock}+09:00")
    assert get_domestic_trading_session(now, exchange_code=venue) == expected
    assert get_domestic_session_for_env(now, "prod", exchange_code=venue) == expected
    mock_session = "regular" if venue == "KRX" and expected == "regular" else "closed"
    assert get_domestic_session_for_env(now, "vps", exchange_code=venue) == mock_session


def test_krx_aftermarket_effective_date_and_weekends():
    before = datetime.fromisoformat("2026-09-11T17:00:00+09:00")
    after = datetime.fromisoformat("2026-09-14T17:00:00+09:00")
    weekend = datetime.fromisoformat("2026-10-03T10:00:00+09:00")
    assert get_domestic_trading_session(before) == "single_price"
    assert get_domestic_trading_session(after) == "aftermarket"
    for venue in ("KRX", "NXT", "UNKNOWN"):
        assert get_domestic_session_for_env(weekend, "prod", exchange_code=venue) == "closed"


def test_exact_krx_close_stops_new_orders_but_keeps_reconciliation():
    now = datetime.fromisoformat("2026-10-01T15:30:00+09:00")
    assert not is_krx_regular_session(now)
    assert is_krx_execution_reconcile_window(now)


@pytest.mark.parametrize("date,clock,expected", [
    ("2026-10-01", "10:00:00", "daytime"),
    ("2026-10-01", "17:00:00", "premarket"),
    ("2026-10-01", "22:30:00", "regular"),
    ("2026-10-02", "04:59:59", "regular"),
    ("2026-10-02", "05:00:00", "aftermarket"),
    ("2026-10-02", "07:00:00", "closed"),
    ("2026-11-02", "17:00:00", "daytime"),
    ("2026-11-02", "18:00:00", "premarket"),
    ("2026-11-02", "22:30:00", "premarket"),
    ("2026-11-02", "23:30:00", "regular"),
    ("2026-11-03", "05:00:00", "regular"),
    ("2026-11-03", "06:00:00", "aftermarket"),
    ("2026-11-03", "07:00:00", "closed"),
])
def test_us_live_paper_session_matrix(date, clock, expected):
    now = datetime.fromisoformat(f"{date}T{clock}+09:00")
    assert get_us_trading_session(now) == expected
    assert is_us_orderable_session_for_env(now, "prod") == (expected != "closed")
    assert is_us_orderable_session_for_env(now, "vps") == (expected == "regular")
    assert is_us_regular_session(now) == (expected == "regular")
    assert not is_us_orderable_session_for_env(now, "unknown")


@pytest.mark.parametrize("start,end", [
    ("2026-03-07T12:00:00+00:00", "2026-03-09T13:30:00+00:00"),
    ("2026-10-31T12:00:00+00:00", "2026-11-02T14:30:00+00:00"),
])
def test_next_paper_open_uses_dst_at_future_session(monkeypatch, start, end):
    from kinvest_trade import market_calendar
    monkeypatch.setattr(market_calendar, "is_krx_holiday", lambda _: True)
    monkeypatch.setattr(market_calendar, "is_nyse_holiday", lambda _: False)
    now, opening = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert minutes_until_next_tradeable_session(now, "vps") == int((opening - now).total_seconds() / 60)


def test_extended_aftermarket_requires_opt_in_and_uses_prior_session_date():
    now = datetime.fromisoformat("2026-10-02T08:00:00+09:00")
    assert get_us_trading_session(now) == "closed"
    assert get_us_trading_session(now, include_extended_aftermarket=True) == "aftermarket_extended"
    assert not is_us_orderable_session_for_env(now, "prod")
    assert us_holiday_date_for_kis_session(now).isoformat() == "2026-10-01"
