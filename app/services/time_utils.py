from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_now_naive() -> datetime:
    return ist_now().replace(tzinfo=None)


def ist_today() -> date:
    return ist_now().date()


def to_ist_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)


def format_ist(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{to_ist_naive(value).isoformat()} IST"


def format_ist_space(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{to_ist_naive(value).isoformat(sep=' ')} IST"
