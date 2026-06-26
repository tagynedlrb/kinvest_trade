from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


def _is_weekday(local_dt: datetime) -> bool:
    return local_dt.weekday() < 5


def is_krx_regular_session(now_utc: datetime | None = None) -> bool:
    current = (now_utc or datetime.now(timezone.utc)).astimezone(KST)
    if not _is_weekday(current):
        return False
    session_start = time(9, 0)
    session_end = time(15, 30)
    return session_start <= current.time() <= session_end


def _is_new_york_dst(now_utc: datetime | None = None) -> bool:
    ny_time = (now_utc or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    return bool(ny_time.dst())


def get_us_trading_session(
    now_utc: datetime | None = None,
    *,
    include_extended_aftermarket: bool = False,
) -> str:
    """Return the current KIS US-stock session in KST."""

    current = (now_utc or datetime.now(timezone.utc)).astimezone(KST)
    weekday = current.weekday()
    current_time = current.time()
    is_dst = _is_new_york_dst(now_utc)

    daytime_end = time(17, 0) if is_dst else time(18, 0)
    premarket_end = time(22, 30) if is_dst else time(23, 30)
    regular_end = time(5, 0) if is_dst else time(6, 0)
    aftermarket_end = time(7, 0)
    extended_aftermarket_end = time(9, 0)

    if weekday <= 4:
        if time(10, 0) <= current_time < daytime_end:
            return "daytime"
        if daytime_end <= current_time < premarket_end:
            return "premarket"
        if current_time >= premarket_end:
            return "regular"

    if 1 <= weekday <= 5:
        if current_time < regular_end:
            return "regular"
        if regular_end <= current_time < aftermarket_end:
            return "aftermarket"
        if include_extended_aftermarket and aftermarket_end <= current_time < extended_aftermarket_end:
            return "aftermarket_extended"

    return "closed"


def is_us_regular_session(now_utc: datetime | None = None) -> bool:
    return get_us_trading_session(now_utc) != "closed"


def is_us_daytime_session(now_utc: datetime | None = None) -> bool:
    return get_us_trading_session(now_utc) == "daytime"


def is_us_orderable_session_for_env(now_utc: datetime | None, env: str) -> bool:
    session = get_us_trading_session(now_utc)
    if env == "prod":
        return session in {"daytime", "premarket", "regular", "aftermarket"}
    return session == "regular"
