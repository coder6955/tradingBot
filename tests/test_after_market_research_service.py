import unittest
from datetime import datetime

from app.config import settings
from app.services.after_market_research_service import AfterMarketResearchService


class FakeBacktestService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run_option_premium(self, **kwargs):
        self.calls.append(("option_backtest", kwargs))
        return {
            "status": "ok",
            "mode": "scanner_parity_option_premium_replay",
            "decision_mode": kwargs.get("decision_mode"),
            "summary": {"trades": 12, "expectancy_pct": 0.4},
        }

    def run_walk_forward(self, **kwargs):
        self.calls.append(("walk_forward", kwargs))
        return {
            "status": "ok",
            "decision_mode": kwargs.get("decision_mode"),
            "passed": True,
            "summary": {"trades": 8, "expectancy_pct": 0.2},
        }


class FakeInsightsService:
    def daily_banknifty_summary(self, *, summary_date=None):
        return {
            "status": "ok",
            "date": summary_date.isoformat(),
            "total_closed_paper_trades": 10,
            "recommendation": {"safe_to_enable_live": False},
        }

    def data_completeness(self, *, symbol="BANKNIFTY"):
        return {"status": "ok", "symbol": symbol, "candles": {"rows": 100}, "option_snapshots": {"rows": 100}}


class AfterMarketResearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_after_market_research_job": settings.enable_after_market_research_job,
            "after_market_research_time": settings.after_market_research_time,
        }
        object.__setattr__(settings, "enable_after_market_research_job", True)
        object.__setattr__(settings, "after_market_research_time", "15:45")

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def test_regular_market_does_not_run_scheduled_research(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 10, 30))

        result = service.run_once(trigger="manual", force=False)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_after_market")
        self.assertEqual(backtest.calls, [])

    def test_after_market_runs_scanner_parity_research_once(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 16, 0))

        result = service.maybe_run_after_market()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "after_market_research")
        self.assertEqual(service.status()["last_run_date"], "2026-07-03")
        self.assertEqual([name for name, _ in backtest.calls], ["option_backtest", "walk_forward"])
        self.assertTrue(all(call[1]["decision_mode"] == "scanner_parity" for call in backtest.calls))

    def test_scheduled_research_runs_only_once_per_day(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 16, 0))

        first = service.maybe_run_after_market()
        second = service.maybe_run_after_market()

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["reason"], "already_ran_today")
        self.assertEqual(len(backtest.calls), 2)

    def test_manual_force_can_run_outside_after_market_window(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 10, 30))

        result = service.run_once(trigger="manual", force=True)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["force"], True)
        self.assertEqual(len(backtest.calls), 2)

    def _service(self, backtest: FakeBacktestService, now: datetime) -> AfterMarketResearchService:
        return AfterMarketResearchService(
            backtest_service=backtest,
            professional_insights_service=FakeInsightsService(),
            clock=lambda: now,
        )


if __name__ == "__main__":
    unittest.main()
