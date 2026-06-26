from datetime import datetime, timezone

from kinvest_trade.time_utils import format_any_datetime_to_kst, format_display_times, format_kst


def test_format_kst_from_aware_datetime() -> None:
    value = datetime(2026, 6, 25, 8, 30, 45, tzinfo=timezone.utc)
    assert format_kst(value) == "2026-06-25 17:30:45 KST"


def test_format_any_datetime_to_kst_from_iso_string() -> None:
    assert (
        format_any_datetime_to_kst("2026-06-25T08:30:45+00:00")
        == "2026-06-25 17:30:45 KST"
    )


def test_format_any_datetime_to_kst_from_sqlite_timestamp() -> None:
    assert (
        format_any_datetime_to_kst("2026-06-25 08:30:45")
        == "2026-06-25 17:30:45 KST"
    )


def test_format_display_times_recursively_formats_known_fields() -> None:
    payload = {
        "updated_at": "2026-06-25T08:30:45+00:00",
        "nested": {
            "started_at": "2026-06-25 08:30:45",
            "other": "unchanged",
        },
    }
    assert format_display_times(payload) == {
        "updated_at": "2026-06-25 17:30:45 KST",
        "nested": {
            "started_at": "2026-06-25 17:30:45 KST",
            "other": "unchanged",
        },
    }
