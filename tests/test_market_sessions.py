from datetime import datetime, timezone

from kinvest_trade.market_sessions import (
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    is_us_regular_session,
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
    assert get_us_trading_session(datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)) == "daytime"


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
    assert not is_us_regular_session(datetime(2026, 6, 25, 0, 30, tzinfo=timezone.utc))


def test_us_regular_session_false_on_sunday_kst_morning() -> None:
    assert not is_us_regular_session(datetime(2026, 6, 28, 21, 0, tzinfo=timezone.utc))
