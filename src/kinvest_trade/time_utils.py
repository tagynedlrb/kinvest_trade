from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

DISPLAY_TIME_FIELDS = {
    "active_cycle_started_at",
    "captured_at",
    "created_at",
    "ended_at",
    "last_command_at",
    "last_completed_at",
    "next_run_at",
    "opened_at",
    "scanned_at",
    "started_at",
    "updated_at",
}


def ensure_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def to_kst(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return ensure_timezone(value).astimezone(KST)


def format_kst(value: datetime | None) -> str | None:
    local_dt = to_kst(value)
    if local_dt is None:
        return None
    return local_dt.strftime("%Y-%m-%d %H:%M:%S KST")


def format_kst_korean(value: datetime | None) -> str:
    local_dt = to_kst(value)
    if local_dt is None:
        return "-"
    return f"{local_dt.month}월 {local_dt.day}일 {local_dt.hour:02d}:{local_dt.minute:02d}"


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_timezone(value)

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    for candidate in (
        normalized,
        normalized.replace(" ", "T", 1),
    ):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass

    for pattern in (
        "%Y-%m-%d %H:%M:%S KST",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
            if pattern.endswith("KST"):
                return parsed.replace(tzinfo=KST)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def format_any_datetime_to_kst(value: str | datetime | None) -> str | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None if value is None else str(value)
    return format_kst(parsed)


def format_display_times(payload: object) -> object:
    if isinstance(payload, dict):
        formatted: dict[object, object] = {}
        for key, value in payload.items():
            if isinstance(key, str) and key in DISPLAY_TIME_FIELDS:
                formatted[key] = format_any_datetime_to_kst(value)
            else:
                formatted[key] = format_display_times(value)
        return formatted
    if isinstance(payload, list):
        return [format_display_times(item) for item in payload]
    return payload
