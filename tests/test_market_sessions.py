from datetime import datetime, timezone

from kinvest_trade.market_sessions import (
    determine_loop_interval_sec,
    get_us_trading_session,
    is_krx_execution_reconcile_window,
    is_krx_regular_session,
    is_us_execution_reconcile_window,
    is_us_orderable_session_for_env,
    is_us_regular_session,
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


def test_us_regular_session_true_during_kis_premarket() -> None:
    assert is_us_regular_session(datetime(2026, 6, 25, 8, 13, tzinfo=timezone.utc))


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
