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

    def run_ablation(self, **kwargs):
        self.calls.append(("ablation", kwargs))
        return {"status": "ok", "summary": {"baseline": 0.2}}


class FakeInsightsService:
    def daily_banknifty_summary(self, *, summary_date=None):
        return {
            "status": "ok",
            "date": summary_date.isoformat(),
            "total_closed_paper_trades": 10,
            "recommendation": {"safe_to_enable_live": False},
        }

    def data_completeness(self, *, symbol="BANKNIFTY"):
        return {
            "status": "ok",
            "symbol": symbol,
            "candles": {"rows": 100},
            "option_snapshots": {"rows": 100},
        }

    def analyze(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def research_engine_report(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def gate_effectiveness_report(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def rejected_opportunity_quality_report(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def threshold_validation_report(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def execution_realism_report(self, *, symbol="BANKNIFTY", limit=3000):
        return {"status": "ok", "symbol": symbol, "limit": limit}

    def daily_review(self, *, symbol="BANKNIFTY", review_date=None, limit=3000):
        return {
            "status": "ok",
            "symbol": symbol,
            "date": review_date.isoformat(),
            "limit": limit,
        }


class ReadyInsightsService(FakeInsightsService):
    def daily_banknifty_summary(self, *, summary_date=None):
        return {
            "status": "ok",
            "date": summary_date.isoformat(),
            "total_closed_paper_trades": 35,
            "recommendation": {"safe_to_enable_live": False},
        }


class PartiallyFailingInsightsService(FakeInsightsService):
    def research_engine_report(self, *, symbol="BANKNIFTY", limit=3000):
        raise RuntimeError("research engine unavailable")


class FakeDataIngestionService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def backfill_relevant_option_candles(self, **kwargs):
        self.calls.append("targeted_backfill")
        return {
            "status": "ok",
            "kwargs": kwargs,
            "coverage_after": {"summary": {"data_quality": "high_confidence"}},
        }

    def option_candle_coverage_report(self, **kwargs):
        self.calls.append("coverage")
        return {
            "status": "ok",
            "summary": {"data_quality": "high_confidence"},
            "kwargs": kwargs,
        }


class FakeRejectedOutcomeService:
    def __init__(self, data_ingestion: FakeDataIngestionService) -> None:
        self.calls: list[str] = []
        self.data_ingestion = data_ingestion

    def evaluate_batches(self, **kwargs):
        self.calls.append("replay")
        return {
            "status": "ok",
            "data_ingestion_calls_before_replay": list(self.data_ingestion.calls),
            "kwargs": kwargs,
        }


class FakeJobRepository:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.next_id = 1

    def latest(self, *, job_name: str, trading_date: str):
        matches = [
            row
            for row in self.rows
            if row["job_name"] == job_name and row["trading_date"] == trading_date
        ]
        return dict(matches[-1]) if matches else None

    def count(self, *, job_name: str, trading_date: str, status: str | None = None):
        return len(
            [
                row
                for row in self.rows
                if row["job_name"] == job_name
                and row["trading_date"] == trading_date
                and (status is None or row["status"] == status)
            ]
        )

    def start(self, *, job_name: str, trading_date: str, metadata=None):
        row = {
            "id": self.next_id,
            "job_name": job_name,
            "trading_date": trading_date,
            "status": "running",
            "metadata": metadata or {},
        }
        self.next_id += 1
        self.rows.append(row)
        return dict(row)

    def finish(self, *, run_id: int, status: str, metadata=None, error_message=None):
        for row in self.rows:
            if row["id"] == run_id:
                row["status"] = status
                row["metadata"] = metadata or {}
                row["error_message"] = error_message
                return dict(row)
        return None


class AfterMarketResearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_after_market_research_job": settings.enable_after_market_research_job,
            "after_market_research_time": settings.after_market_research_time,
            "after_market_research_step_delay_seconds": settings.after_market_research_step_delay_seconds,
            "enable_targeted_option_candle_backfill": settings.enable_targeted_option_candle_backfill,
        }
        object.__setattr__(settings, "enable_after_market_research_job", True)
        object.__setattr__(settings, "after_market_research_time", "15:45")
        object.__setattr__(settings, "after_market_research_step_delay_seconds", 0.0)
        object.__setattr__(settings, "enable_targeted_option_candle_backfill", True)

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
        self.assertEqual(
            [name for name, _ in backtest.calls],
            ["option_backtest", "ablation", "walk_forward"],
        )
        self.assertTrue(
            all(
                call[1].get("decision_mode", "scanner_parity") == "scanner_parity"
                for call in backtest.calls
            )
        )

    def test_scheduled_research_runs_only_once_per_day(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 16, 0))

        first = service.maybe_run_after_market()
        second = service.maybe_run_after_market()

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["reason"], "already_ran_today")
        self.assertEqual(len(backtest.calls), 3)

    def test_manual_force_can_run_outside_after_market_window(self) -> None:
        backtest = FakeBacktestService()
        service = self._service(backtest, datetime(2026, 7, 3, 10, 30))

        result = service.run_once(trigger="manual", force=True)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["force"], True)
        self.assertEqual(len(backtest.calls), 3)

    def test_targeted_option_backfill_runs_before_rejected_replay(self) -> None:
        backtest = FakeBacktestService()
        data_ingestion = FakeDataIngestionService()
        rejected = FakeRejectedOutcomeService(data_ingestion)
        service = AfterMarketResearchService(
            backtest_service=backtest,
            professional_insights_service=FakeInsightsService(),
            data_ingestion_service=data_ingestion,
            rejected_outcome_service=rejected,
            clock=lambda: datetime(2026, 7, 3, 16, 0),
            job_repository=FakeJobRepository(),
        )

        result = service.maybe_run_after_market()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(data_ingestion.calls, ["targeted_backfill", "coverage"])
        self.assertEqual(rejected.calls, ["replay"])
        replay = result["reports"]["rejected_outcome_replay"]["result"]
        self.assertEqual(
            replay["data_ingestion_calls_before_replay"],
            ["targeted_backfill", "coverage"],
        )

    def test_recommendation_uses_stage_results_not_report_wrappers(self) -> None:
        service = AfterMarketResearchService(
            backtest_service=FakeBacktestService(),
            professional_insights_service=ReadyInsightsService(),
            clock=lambda: datetime(2026, 7, 3, 16, 0),
            job_repository=FakeJobRepository(),
        )

        result = service.maybe_run_after_market()

        self.assertEqual(result["status"], "ok")
        self.assertIn("Evidence is improving", result["recommendation"]["reason"])

    def test_restart_after_partial_after_market_pipeline_does_not_repeat_heavy_stages(
        self,
    ) -> None:
        job_repository = FakeJobRepository()
        data_ingestion = FakeDataIngestionService()
        first = AfterMarketResearchService(
            backtest_service=FakeBacktestService(),
            professional_insights_service=PartiallyFailingInsightsService(),
            data_ingestion_service=data_ingestion,
            clock=lambda: datetime(2026, 7, 3, 16, 0),
            job_repository=job_repository,
        )

        first_result = first.maybe_run_after_market()
        restarted = AfterMarketResearchService(
            backtest_service=FakeBacktestService(),
            professional_insights_service=FakeInsightsService(),
            data_ingestion_service=data_ingestion,
            clock=lambda: datetime(2026, 7, 3, 17, 0),
            job_repository=job_repository,
        )
        second_result = restarted.maybe_run_after_market()

        self.assertEqual(first_result["status"], "partial")
        self.assertEqual(
            job_repository.latest(
                job_name=AfterMarketResearchService.JOB_NAME, trading_date="2026-07-03"
            )["status"],
            "partial",
        )
        self.assertEqual(second_result["status"], "idle")
        self.assertEqual(second_result["reason"], "already_ran_today")
        self.assertEqual(data_ingestion.calls, ["targeted_backfill", "coverage"])

    def test_manual_force_can_rerun_after_completed_pipeline(self) -> None:
        job_repository = FakeJobRepository()
        data_ingestion = FakeDataIngestionService()
        first = AfterMarketResearchService(
            backtest_service=FakeBacktestService(),
            professional_insights_service=FakeInsightsService(),
            data_ingestion_service=data_ingestion,
            clock=lambda: datetime(2026, 7, 3, 16, 0),
            job_repository=job_repository,
        )
        first.maybe_run_after_market()
        forced = AfterMarketResearchService(
            backtest_service=FakeBacktestService(),
            professional_insights_service=FakeInsightsService(),
            data_ingestion_service=data_ingestion,
            clock=lambda: datetime(2026, 7, 3, 17, 0),
            job_repository=job_repository,
        )

        result = forced.run_once(trigger="manual", force=True)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            data_ingestion.calls,
            ["targeted_backfill", "coverage", "targeted_backfill", "coverage"],
        )

    def _service(
        self, backtest: FakeBacktestService, now: datetime
    ) -> AfterMarketResearchService:
        return AfterMarketResearchService(
            backtest_service=backtest,
            professional_insights_service=FakeInsightsService(),
            clock=lambda: now,
            job_repository=FakeJobRepository(),
        )


if __name__ == "__main__":
    unittest.main()
