from __future__ import annotations

from datetime import date, datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.time_utils import to_ist_naive


class MarketSessionService:
    """Central runtime session policy for NSE market-hour orchestration."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))

    def is_market_day(self, now: datetime | None = None) -> bool:
        current = self._coerce_now(now)
        return current.weekday() < 5 and current.date() not in self._holiday_dates()

    def is_pre_market_window(self, now: datetime | None = None) -> bool:
        current = self._coerce_now(now)
        return self.is_market_day(current) and self._time(settings.runtime_pre_market_start_time) <= current.time() < self._time(settings.runtime_market_open_time)

    def is_market_open(self, now: datetime | None = None) -> bool:
        current = self._coerce_now(now)
        return self.is_market_day(current) and self._time(settings.runtime_market_open_time) <= current.time() <= self._time(settings.runtime_market_close_time)

    def is_market_closing_window(self, now: datetime | None = None) -> bool:
        current = self._coerce_now(now)
        return self.is_market_day(current) and self._time(settings.runtime_market_closing_start_time) <= current.time() <= self._time(settings.runtime_market_close_time)

    def is_after_market_window(self, now: datetime | None = None) -> bool:
        current = self._coerce_now(now)
        return self.is_market_day(current) and current.time() >= self._time(settings.runtime_after_market_review_start_time)

    def should_run_live_modules(self, now: datetime | None = None) -> bool:
        return bool(settings.runtime_manual_override) or self.is_market_open(now)

    def should_run_after_market_review(self, now: datetime | None = None) -> bool:
        return self.is_after_market_window(now)

    def current_runtime_mode(self, now: datetime | None = None) -> str:
        current = self._coerce_now(now)
        if settings.runtime_manual_override:
            return "MANUAL_OVERRIDE"
        if not self.is_market_day(current):
            return "HOLIDAY" if current.date() in self._holiday_dates() else "MARKET_CLOSED"
        if self.is_pre_market_window(current):
            return "PRE_MARKET"
        if self.is_market_closing_window(current):
            return "MARKET_CLOSING"
        if self.is_market_open(current):
            return "MARKET_OPEN"
        if self.is_after_market_window(current):
            return "AFTER_MARKET_REVIEW"
        return "MARKET_CLOSED"

    def trading_date(self, now: datetime | None = None) -> date:
        return self._coerce_now(now).date()

    def status(self, now: datetime | None = None) -> dict[str, object]:
        current = self._coerce_now(now)
        return {
            "runtime_mode": self.current_runtime_mode(current),
            "manual_override": bool(settings.runtime_manual_override),
            "market_open": self.is_market_open(current),
            "should_run_live_modules": self.should_run_live_modules(current),
            "should_run_after_market_review": self.should_run_after_market_review(current),
            "trading_date": current.date().isoformat(),
            "timestamp": current.isoformat(sep=" "),
            "windows": {
                "pre_market_start": settings.runtime_pre_market_start_time,
                "market_open": settings.runtime_market_open_time,
                "market_closing_start": settings.runtime_market_closing_start_time,
                "market_close": settings.runtime_market_close_time,
                "after_market_review_start": settings.runtime_after_market_review_start_time,
            },
            "holidays": sorted(day.isoformat() for day in self._holiday_dates()),
        }

    def _coerce_now(self, now: datetime | None) -> datetime:
        return to_ist_naive(now or self.clock())

    def _holiday_dates(self) -> set[date]:
        days: set[date] = set()
        for item in str(settings.runtime_holidays or "").split(","):
            value = item.strip()
            if not value:
                continue
            try:
                days.add(date.fromisoformat(value))
            except ValueError:
                continue
        return days

    def _time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
