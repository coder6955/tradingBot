import asyncio
import unittest
from datetime import datetime

from app.config import settings
from app.services.automation_supervisor_service import AutomationSupervisorService


class FakeDataIngestionService:
    def __init__(self) -> None:
        self.live_gap_calls: list[dict[str, object]] = []

    def ingest_candles(self, **kwargs):
        return {"status": "ok", "kwargs": kwargs}

    def backfill_live_relevant_option_candle_gaps(self, **kwargs):
        self.live_gap_calls.append(kwargs)
        return {"status": "ok", "inserted": 3, "kwargs": kwargs}


class FakeSnapshotCollectorService:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.task = None

    def status(self):
        return {"running": self.running}

    def start(self, **kwargs):
        self.running = True
        return {"running": True, "kwargs": kwargs}


class FakeAutoTraderService:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.task = None

    def status(self):
        return {"running": self.running}

    def start(self, **kwargs):
        self.running = True
        return {"running": True, "kwargs": kwargs}


class FakeOutcomeService:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.task = None
        self.evaluate_calls = 0

    def status(self):
        return {"running": self.running}

    def start(self, interval_seconds=30):
        self.running = True
        return {"running": True, "interval_seconds": interval_seconds}

    def evaluate_once(self, limit=100, **kwargs):
        self.evaluate_calls += 1
        return {"evaluated": 0, "limit": limit, **kwargs}


class FakeRiskManagementService:
    def evaluate_entry(self):
        return {"passed": True}


class FakeNotificationService:
    def send(self, message):
        return None


class FakeLifecycleRepository:
    def __init__(self, previous: dict[str, object] | None = None) -> None:
        self.previous = previous
        self.started: list[dict[str, object]] = []
        self.finished: list[dict[str, object]] = []

    def latest(self, **kwargs):
        return dict(self.previous) if self.previous else None

    def latest_run(self, **kwargs):
        return dict(self.previous) if self.previous else None

    def start(self, **kwargs):
        self.started.append(dict(kwargs))
        return {
            "id": 2,
            "status": "running",
            "metadata": dict(kwargs.get("metadata") or {}),
            "job_name": kwargs.get("job_name"),
            "trading_date": kwargs.get("trading_date"),
        }

    def finish(self, **kwargs):
        self.finished.append(dict(kwargs))
        return {
            "id": kwargs["run_id"],
            "status": kwargs["status"],
            "metadata": dict(kwargs.get("metadata") or {}),
        }


class FakeAfterMarketResearchService:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def maybe_run_after_market(self, now):
        self.calls.append(now)
        return {"action": "after_market_research", "status": "ok"}

    def status(self):
        return {"status": "ok", "running": False}


class FixedClockAutomationSupervisor(AutomationSupervisorService):
    def __init__(
        self,
        *,
        now: datetime,
        after_market_research_service: FakeAfterMarketResearchService,
        auto_trader_service: FakeAutoTraderService | None = None,
        snapshot_collector_service: FakeSnapshotCollectorService | None = None,
        outcome_service: FakeOutcomeService | None = None,
        lifecycle_repository: FakeLifecycleRepository | None = None,
    ) -> None:
        self.fixed_now = now
        self.fake_outcome_service = outcome_service or FakeOutcomeService()
        super().__init__(
            data_ingestion_service=FakeDataIngestionService(),
            snapshot_collector_service=snapshot_collector_service
            or FakeSnapshotCollectorService(),
            auto_trader_service=auto_trader_service or FakeAutoTraderService(),
            outcome_service=self.fake_outcome_service,
            risk_management_service=FakeRiskManagementService(),
            notification_service=FakeNotificationService(),
            after_market_research_service=after_market_research_service,
            lifecycle_repository=lifecycle_repository,
        )

    def _now(self) -> datetime:
        return self.fixed_now


class AutomationSupervisorAfterMarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stop_after_complete = (
            settings.automation_stop_after_after_market_complete
        )
        self.original_enable_live_gap_backfill = (
            settings.enable_live_option_candle_gap_backfill
        )
        self.original_live_gap_interval = (
            settings.live_option_candle_backfill_interval_seconds
        )
        self.original_live_gap_timeframes = (
            settings.live_option_candle_backfill_timeframes
        )
        self.original_automation_enabled = settings.automation_enabled
        object.__setattr__(
            settings, "automation_stop_after_after_market_complete", True
        )
        object.__setattr__(settings, "automation_enabled", False)
        object.__setattr__(settings, "enable_live_option_candle_gap_backfill", True)
        object.__setattr__(
            settings, "live_option_candle_backfill_interval_seconds", 120
        )
        object.__setattr__(
            settings, "live_option_candle_backfill_timeframes", "1minute"
        )

    def tearDown(self) -> None:
        object.__setattr__(
            settings,
            "automation_stop_after_after_market_complete",
            self.original_stop_after_complete,
        )
        object.__setattr__(
            settings,
            "enable_live_option_candle_gap_backfill",
            self.original_enable_live_gap_backfill,
        )
        object.__setattr__(
            settings,
            "live_option_candle_backfill_interval_seconds",
            self.original_live_gap_interval,
        )
        object.__setattr__(
            settings,
            "live_option_candle_backfill_timeframes",
            self.original_live_gap_timeframes,
        )
        object.__setattr__(
            settings, "automation_enabled", self.original_automation_enabled
        )

    def test_supervisor_runs_research_in_market_closed_branch(self) -> None:
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 16, 0), after_market_research_service=research
        )

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(research.calls), 1)
        self.assertTrue(
            any(
                action.get("action") == "after_market_research"
                for action in result["actions"]
            )
        )

    def test_supervisor_stops_intraday_services_after_market_close(self) -> None:
        research = FakeAfterMarketResearchService()
        auto_trader = FakeAutoTraderService(running=True)
        snapshot_collector = FakeSnapshotCollectorService(running=True)
        outcome = FakeOutcomeService(running=True)
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 16, 0),
            after_market_research_service=research,
            auto_trader_service=auto_trader,
            snapshot_collector_service=snapshot_collector,
            outcome_service=outcome,
        )

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertFalse(auto_trader.running)
        self.assertFalse(snapshot_collector.running)
        self.assertFalse(outcome.running)
        self.assertTrue(
            any(
                action.get("action") == "stop_auto_trader"
                for action in result["actions"]
            )
        )
        self.assertTrue(
            any(
                action.get("action") == "stop_snapshot_collector"
                for action in result["actions"]
            )
        )
        self.assertTrue(
            any(
                action.get("action") == "stop_outcome_monitor"
                for action in result["actions"]
            )
        )

    def test_supervisor_does_not_repeat_after_market_outcome_evaluation(self) -> None:
        research = FakeAfterMarketResearchService()
        outcome = FakeOutcomeService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 16, 0),
            after_market_research_service=research,
            outcome_service=outcome,
        )

        first = supervisor.run_once({"symbols": "BANKNIFTY"})
        second = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(outcome.evaluate_calls, 1)
        self.assertTrue(
            any(
                action.get("action") == "evaluate_open_opportunities"
                and action.get("status") == "skipped"
                for action in second["actions"]
            )
        )

    def test_supervisor_does_not_call_outcome_evaluation_before_market_open(
        self,
    ) -> None:
        research = FakeAfterMarketResearchService()
        outcome = FakeOutcomeService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 8, 30),
            after_market_research_service=research,
            outcome_service=outcome,
        )

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(outcome.evaluate_calls, 0)
        self.assertTrue(
            any(
                action.get("action") == "evaluate_open_opportunities"
                and action.get("reason") == "market_not_closed_for_day"
                for action in result["actions"]
            )
        )

    def test_supervisor_does_not_run_research_during_market_hours(self) -> None:
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 10, 30), after_market_research_service=research
        )

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(research.calls, [])
        self.assertFalse(
            any(
                action.get("action") == "after_market_research"
                for action in result["actions"]
            )
        )

    def test_supervisor_runs_live_option_candle_gap_catchup_during_market_hours(
        self,
    ) -> None:
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 10, 30), after_market_research_service=research
        )

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(supervisor.data_ingestion_service.live_gap_calls), 1)
        self.assertEqual(
            supervisor.data_ingestion_service.live_gap_calls[0]["symbols"],
            ["BANKNIFTY"],
        )
        self.assertEqual(
            supervisor.data_ingestion_service.live_gap_calls[0]["timeframes"],
            ["1minute"],
        )
        self.assertTrue(
            any(
                action.get("action") == "live_option_candle_gap_catchup"
                for action in result["actions"]
            )
        )

    def test_running_supervisor_stops_after_after_market_pipeline_completes(
        self,
    ) -> None:
        research = FakeAfterMarketResearchService()
        outcome = FakeOutcomeService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 16, 0),
            after_market_research_service=research,
            outcome_service=outcome,
        )
        supervisor.running = True

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertFalse(supervisor.running)
        self.assertTrue(
            any(
                action.get("action") == "automation_stop_after_after_market_complete"
                for action in result["actions"]
            )
        )

    def test_boot_managed_supervisor_survives_after_market_for_next_session(
        self,
    ) -> None:
        object.__setattr__(settings, "automation_enabled", True)
        research = FakeAfterMarketResearchService()
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 16, 0),
            after_market_research_service=research,
        )
        supervisor.running = True

        result = supervisor.run_once({"symbols": "BANKNIFTY"})

        self.assertEqual(result["status"], "ok")
        self.assertTrue(supervisor.running)
        self.assertFalse(
            any(
                action.get("action") == "automation_stop_after_after_market_complete"
                for action in result["actions"]
            )
        )

    def test_start_records_and_recovers_unclean_previous_process_run(self) -> None:
        repository = FakeLifecycleRepository(
            previous={
                "id": 1,
                "status": "running",
                "metadata": {"boot_id": "old-boot"},
            }
        )
        supervisor = FixedClockAutomationSupervisor(
            now=datetime(2026, 7, 3, 10, 30),
            after_market_research_service=FakeAfterMarketResearchService(),
            lifecycle_repository=repository,
        )

        async def exercise() -> None:
            supervisor.start({"symbols": "BANKNIFTY"}, trigger="application_startup")
            await supervisor.stop(reason="test_shutdown")

        asyncio.run(exercise())

        self.assertEqual(supervisor.recovered_unclean_run_count, 1)
        self.assertEqual(repository.finished[0]["run_id"], 1)
        self.assertEqual(repository.finished[0]["status"], "interrupted")
        self.assertEqual(repository.started[0]["job_name"], "automation_supervisor")
        self.assertEqual(
            repository.started[0]["metadata"]["trigger"], "application_startup"
        )
        self.assertEqual(repository.finished[-1]["status"], "stopped")
        self.assertEqual(
            repository.finished[-1]["metadata"]["stop_reason"], "test_shutdown"
        )


if __name__ == "__main__":
    unittest.main()
