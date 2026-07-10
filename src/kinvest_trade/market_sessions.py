from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
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

    daytime_start = time(10, 0)
    daytime_end = time(17, 0) if is_dst else time(18, 0)
    premarket_end = time(22, 30) if is_dst else time(23, 30)
    regular_end = time(5, 0) if is_dst else time(6, 0)
    aftermarket_end = time(7, 0)
    extended_aftermarket_end = time(9, 0)

    if weekday <= 4:
        if daytime_start <= current_time < daytime_end:
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


def us_holiday_date_for_kis_session(now_utc: datetime) -> datetime.date:
    """Return the US holiday date that matches KIS's KST-based US session."""

    current = now_utc.astimezone(KST)
    if current.time() < time(7, 0):
        return now_utc.astimezone(NEW_YORK).date()
    return current.date()


def _is_us_orderable_trading_day(now_utc: datetime, env: str) -> bool:
    if not is_us_orderable_session_for_env(now_utc, env):
        return False
    from .market_calendar import is_nyse_holiday

    return not is_nyse_holiday(us_holiday_date_for_kis_session(now_utc))


def _is_krx_regular_trading_day(now_utc: datetime) -> bool:
    if not is_krx_regular_session(now_utc):
        return False
    from .market_calendar import is_krx_holiday

    return not is_krx_holiday(now_utc.astimezone(KST).date())


def minutes_until_next_tradeable_session(
    now_utc: datetime,
    env: str = "prod",
) -> int:
    """
    Return minutes until the next KRX or env-tradeable US session.

    Returns 0 when trading is already available in either market.
    """
    if _is_krx_regular_trading_day(now_utc):
        return 0
    if _is_us_orderable_trading_day(now_utc, env):
        return 0

    kst_now = now_utc.astimezone(KST)
    today_kst = kst_now.date()
    from .market_calendar import is_krx_holiday, is_nyse_holiday

    krx_candidates: list[datetime] = []
    for delta in range(0, 8):
        candidate_date = today_kst + timedelta(days=delta)
        candidate_dt = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            9,
            0,
            0,
            tzinfo=KST,
        )
        candidate_utc = candidate_dt.astimezone(timezone.utc)
        if (
            candidate_utc > now_utc
            and candidate_date.weekday() < 5
            and not is_krx_holiday(candidate_date)
        ):
            krx_candidates.append(candidate_utc)
            break

    is_dst = _is_new_york_dst(now_utc)
    if env == "prod":
        us_start_hour = 10
        us_start_minute = 0
    else:
        us_start_hour = 22 if is_dst else 23
        us_start_minute = 30
    us_candidates: list[datetime] = []
    for delta in range(0, 4):
        candidate_date = today_kst + timedelta(days=delta)
        candidate_dt = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            us_start_hour,
            us_start_minute,
            0,
            tzinfo=KST,
        )
        candidate_utc = candidate_dt.astimezone(timezone.utc)
        if (
            candidate_utc > now_utc
            and candidate_date.weekday() < 5
            and not is_nyse_holiday(candidate_date)
        ):
            us_candidates.append(candidate_utc)
            break

    all_candidates = krx_candidates + us_candidates
    if not all_candidates:
        return 120

    nearest = min(all_candidates)
    delta_seconds = (nearest - now_utc).total_seconds()
    return max(0, int(delta_seconds / 60))


def determine_loop_interval_sec(
    now_utc: datetime,
    env: str = "prod",
    consecutive_errors: int = 0,
) -> int:
    """
    Determine the loop interval from market state and recent error streak.
    """
    if consecutive_errors >= 5:
        return 120

    if _is_krx_regular_trading_day(now_utc):
        return 20
    if _is_us_orderable_trading_day(now_utc, env):
        return 20

    session = get_us_trading_session(now_utc)
    if session in {"premarket", "aftermarket"}:
        from .market_calendar import is_nyse_holiday

        if not is_nyse_holiday(us_holiday_date_for_kis_session(now_utc)):
            return 30

    mins = minutes_until_next_tradeable_session(now_utc, env)
    if mins <= 30:
        return 30
    return 120
