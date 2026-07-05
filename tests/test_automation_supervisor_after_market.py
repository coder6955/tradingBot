import unittest
from datetime import datetime

from app.services.automation_supervisor_service import AutomationSupervisorService


class FakeDataIngestionService:
    def ingest_candles(self, **kwargs):
        return {"status": "ok", "kwargs": kwargs}


class FakeSnapshotCollectorService:
    running = False

    def status(self):
        return {"running": self.running}

    def start(self, **kwargs):
        self.running = True
        return {"running": True, "kwargs": kwargs}


class FakeAutoTraderService:
    running = False

    def status(self):
        return {"running": self.running}

    def start(self, **kwargs):
        self.running = True
        return {"running": True, "kwargs": kwargs}


class FakeOutcomeService:
    running = False

    def status(self):
        return {"running": self.running}

    def start(self, interval_seconds=30):
        self.running = True
        return {"running": True, "interval_seconds": interval_seconds}

    def evaluate_once(self, limit=100):
        return {"evaluated": 0, "limit": limit}


class FakeRiskManagementService:
    def evaluate_entry(self):
        return {"passed": True}


class FakeNotificationService:
    def send(self, message):
        return None


class FakeAfterMarketResearchService:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def maybe_run_after_market(self, now):
        self.calls.append(now)
        return {"action": "after_market_research", "status": "ok"}

    def status(self):
        return {"status": "ok", "running": False}


class FixedClockAutomationSupervisor(AutomationSupervisorService):
    def __init__(self, *, now: datetime, after_market_research_service: FakeAfterMarketResearchService) -> None:
        self.fixed_now = now
        super().__init__(
            data_ingestion_service=FakeDataIngestionService(),
            snapshot_collector_service=FakeSnapshotCollectorService(),
            auto_trader_service=FakeAutoTraderService(),
            outcome_service=FakeOutcomeService(),
            risk_management_service=FakeRiskManagementService(),
            notification_service=FakeNotificationService(),
            after_market_research_service=after_market_research_service,
        )

    def _now(self) -> datetime:
        return self.fixed_now


class AutomationSupervisorAfterMarketTests(unittest.TestCase):
    def test_supervisor_runs_research_in_market_closed_branch(self) -> None:
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(now=datetime(2026, 7, 3, 16, 0), after_market_research_service=research)

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(research.calls), 1)
        self.assertTrue(any(action.get("action") == "after_market_research" for action in result["actions"]))

    def test_supervisor_does_not_run_research_during_market_hours(self) -> None:
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(now=datetime(2026, 7, 3, 10, 30), after_market_research_service=research)

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(research.calls, [])
        self.assertFalse(any(action.get("action") == "after_market_research" for action in result["actions"]))


if __name__ == "__main__":
    unittest.main()
