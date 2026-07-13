import unittest
from datetime import datetime

from app.config import settings
from app.services.market_session_service import MarketSessionService


class MarketSessionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "runtime_holidays": settings.runtime_holidays,
            "runtime_manual_override": settings.runtime_manual_override,
        }
        object.__setattr__(settings, "runtime_holidays", "")
        object.__setattr__(settings, "runtime_manual_override", False)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def test_runtime_modes_follow_configured_nse_windows(self) -> None:
        self.assertEqual(self._mode(datetime(2026, 7, 3, 9, 10)), "PRE_MARKET")
        self.assertEqual(self._mode(datetime(2026, 7, 3, 9, 20)), "MARKET_OPEN")
        self.assertEqual(self._mode(datetime(2026, 7, 3, 15, 25)), "MARKET_CLOSING")
        self.assertEqual(self._mode(datetime(2026, 7, 3, 15, 40)), "AFTER_MARKET_REVIEW")

    def test_live_modules_do_not_run_pre_market_or_after_market(self) -> None:
        pre_market = MarketSessionService(clock=lambda: datetime(2026, 7, 3, 9, 10))
        after_market = MarketSessionService(clock=lambda: datetime(2026, 7, 3, 15, 40))
        market_open = MarketSessionService(clock=lambda: datetime(2026, 7, 3, 10, 0))

        self.assertFalse(pre_market.should_run_live_modules())
        self.assertFalse(after_market.should_run_live_modules())
        self.assertTrue(after_market.should_run_after_market_review())
        self.assertTrue(market_open.should_run_live_modules())

    def test_weekends_and_holidays_are_not_market_days(self) -> None:
        self.assertEqual(self._mode(datetime(2026, 7, 4, 10, 0)), "MARKET_CLOSED")

        object.__setattr__(settings, "runtime_holidays", "2026-07-03")
        holiday = MarketSessionService(clock=lambda: datetime(2026, 7, 3, 10, 0))
        self.assertEqual(holiday.current_runtime_mode(), "HOLIDAY")
        self.assertFalse(holiday.should_run_live_modules())

    def test_manual_override_exposes_explicit_mode(self) -> None:
        object.__setattr__(settings, "runtime_manual_override", True)
        service = MarketSessionService(clock=lambda: datetime(2026, 7, 4, 10, 0))

        self.assertEqual(service.current_runtime_mode(), "MANUAL_OVERRIDE")
        self.assertTrue(service.should_run_live_modules())

    def _mode(self, now: datetime) -> str:
        return MarketSessionService(clock=lambda: now).current_runtime_mode()


if __name__ == "__main__":
    unittest.main()
