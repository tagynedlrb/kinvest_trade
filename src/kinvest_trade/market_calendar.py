from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

_NYSE_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)

_KRX_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 28),
        date(2026, 1, 29),
        date(2026, 1, 30),
        date(2026, 3, 1),
        date(2026, 5, 5),
        date(2026, 5, 25),
        date(2026, 6, 6),
        date(2026, 8, 17),
        date(2026, 9, 24),
        date(2026, 9, 25),
        date(2026, 9, 28),
        date(2026, 10, 9),
        date(2026, 12, 25),
        date(2026, 12, 31),
    }
)


def _today_nyse() -> date:
    return datetime.now(NEW_YORK).date()


def _today_krx() -> date:
    return datetime.now(KST).date()


def is_nyse_holiday(target_date: date | None = None) -> bool:
    current = target_date or _today_nyse()
    if current.weekday() >= 5:
        return True
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = xcals.get_calendar("XNYS")
        return not calendar.is_session(pd.Timestamp(current))
    except Exception as exc:  # noqa: BLE001
        logger.debug("nyse_calendar_fallback error=%s", exc)
    return current in _NYSE_HOLIDAYS_2026


def is_krx_holiday(target_date: date | None = None) -> bool:
    current = target_date or _today_krx()
    if current.weekday() >= 5:
        return True
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = xcals.get_calendar("XKRX")
        return not calendar.is_session(pd.Timestamp(current))
    except Exception as exc:  # noqa: BLE001
        logger.debug("krx_calendar_fallback error=%s", exc)
    return current in _KRX_HOLIDAYS_2026


def market_status_summary(
    *,
    nyse_date: date | None = None,
    krx_date: date | None = None,
) -> str:
    nyse_closed = is_nyse_holiday(nyse_date)
    krx_closed = is_krx_holiday(krx_date)
    lines: list[str] = []
    if nyse_closed:
        lines.append("🇺🇸 NYSE/NASDAQ 오늘 휴장")
    else:
        lines.append("🇺🇸 NYSE/NASDAQ 개장일")
    if krx_closed:
        lines.append("🇰🇷 KRX(코스피/코스닥) 오늘 휴장")
    else:
        lines.append("🇰🇷 KRX(코스피/코스닥) 개장일")
    return "\n".join(lines)
