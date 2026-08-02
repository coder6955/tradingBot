import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.models import Signal
from app.services.backtest_service import BacktestService
from app.services.database import OpportunityRecord, get_session, init_db
from app.services.opportunity_repository import OpportunityRepository
from app.services.signal_service import SignalService
from app.services.time_bucket_edge_service import TimeBucketEdgeService
from app.services.auto_trader_service import AutoTraderService
from app.services.professional_readiness_service import ProfessionalReadinessService


class ValidationAndHotPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_time_bucket_cache_miss_never_runs_backtest_in_scanner_path(self) -> None:
        class ExplodingBacktest:
            def run_option_premium(self, **kwargs):
                raise AssertionError("research replay must not run from evaluate")

        service = TimeBucketEdgeService(backtest_service=ExplodingBacktest())  # type: ignore[arg-type]
        result = service.evaluate(symbol="BANKNIFTY", trend="bullish")
        self.assertEqual(result["details"]["all_buckets"], {})

    def test_fast_candidate_uses_cached_validation_and_rejects_stale_context(
        self,
    ) -> None:
        scanner_calls = []

        class Scanner:
            def scan_symbols(self, **kwargs):
                scanner_calls.append(kwargs)
                return []

        class Context:
            passed = True

            def validate_candidate(self, event):
                return {
                    "passed": self.passed,
                    "reason": None if self.passed else "fast_scan_context_stale",
                }

        context = Context()
        service = AutoTraderService(
            scanner_factory=lambda: Scanner(),
            order_service_factory=lambda: object(),
            fast_scan_context_service=context,
        )
        service.config = {
            "side": "BUY",
            "order_mode": "paper",
            "limit": 5,
            "place_orders": False,
        }
        event = {
            "direction": "bullish",
            "timestamp": datetime.now().isoformat(),
            "receive_timestamp": datetime.now().isoformat(),
        }
        service._run_fast_candidate_validation(event)
        self.assertEqual(scanner_calls, [])
        self.assertTrue(service.last_fast_candidate_decision["passed"])
        self.assertEqual(
            service.last_fast_candidate_decision["io_calls"],
            {"rest_calls": 0, "database_queries": 0},
        )
        context.passed = False
        service._run_fast_candidate_validation(event)
        self.assertEqual(scanner_calls, [])
        self.assertEqual(
            service.last_fast_candidate_decision["reason"], "fast_scan_context_stale"
        )
        self.assertEqual(service.errors, [])
        self.assertEqual(
            service.recent_decision_events()[-1]["event_type"],
            "fast_rally_candidate_validation",
        )
        self.assertFalse(service.recent_decision_events()[-1]["passed"])
        self.assertEqual(
            service.recent_decision_events()[-1]["reason"], "fast_scan_context_stale"
        )

    def test_uncalibrated_signal_keeps_probability_null_and_labels_heuristic(
        self,
    ) -> None:
        signal = SignalService().generate_signal(
            symbol="BANKNIFTY",
            score=max(settings.min_signal_score, 80),
            confidence=0.84,
            trend="bullish",
            market_context="strong",
            probability=None,
            factor_scores={
                "probability_estimate": {
                    "probability": None,
                    "source": "unavailable_insufficient_calibration",
                    "heuristic_score_confidence": 0.81,
                }
            },
        )
        record = OpportunityRepository().save_opportunity(signal)
        session = get_session()
        try:
            persisted = session.get(OpportunityRecord, record.id)
            self.assertIsNone(persisted.probability)
            self.assertEqual(persisted.heuristic_score_confidence, 0.81)
            self.assertEqual(
                persisted.probability_source, "unavailable_insufficient_calibration"
            )
            self.assertIsNotNone(persisted.strategy_version)
            self.assertIsNotNone(persisted.config_hash)
        finally:
            session.close()

    def test_walk_forward_uses_multiple_chronological_embargoed_folds(self) -> None:
        base = datetime(2026, 1, 1, 9, 15)
        candles = [
            SimpleNamespace(timestamp=base + timedelta(minutes=5 * index))
            for index in range(540)
        ]

        class DeterministicBacktest(BacktestService):
            def _load_candles(self, *, symbol, timeframe, limit):
                return candles

            def _run_option_premium_on_candles(self, **kwargs):
                count = max(1, len(kwargs["underlying"]) // 4)
                return {
                    "summary": {
                        "trades": count,
                        "wins": count,
                        "losses": 0,
                        "win_rate": 100.0,
                        "average_win_pct": 1.0,
                        "average_loss_pct": 0.0,
                        "expectancy_pct": 1.0,
                        "profit_factor": None,
                        "max_drawdown_pct": 0.0,
                        "net_pnl_pct": float(count),
                    }
                }

        originals = {
            "walk_forward_folds": settings.walk_forward_folds,
            "walk_forward_embargo_candles": settings.walk_forward_embargo_candles,
            "walk_forward_min_train_candles": settings.walk_forward_min_train_candles,
            "walk_forward_min_validation_candles": settings.walk_forward_min_validation_candles,
            "readiness_min_validation_folds": settings.readiness_min_validation_folds,
        }
        try:
            object.__setattr__(settings, "walk_forward_folds", 4)
            object.__setattr__(settings, "walk_forward_embargo_candles", 12)
            object.__setattr__(settings, "walk_forward_min_train_candles", 120)
            object.__setattr__(settings, "walk_forward_min_validation_candles", 60)
            object.__setattr__(settings, "readiness_min_validation_folds", 3)
            result = DeterministicBacktest().run_walk_forward(
                symbol="BANKNIFTY", horizon_candles=6
            )
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertEqual(result["fold_count"], 4)
        self.assertEqual(result["embargo_candles"], 12)
        previous_validation_end = None
        for fold in result["folds"]:
            train_end = datetime.fromisoformat(fold["train_end"])
            validation_start = datetime.fromisoformat(fold["validation_start"])
            self.assertGreaterEqual(
                (validation_start - train_end).total_seconds(), 12 * 5 * 60
            )
            if previous_validation_end is not None:
                self.assertGreater(validation_start, previous_validation_end)
            previous_validation_end = datetime.fromisoformat(fold["validation_end"])

    def test_zero_trade_validation_fails_with_evidence_reasons(self) -> None:
        passed, reasons = BacktestService()._validation_passed(
            {
                "trades": 0,
                "expectancy_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
            }
        )

        self.assertFalse(passed)
        self.assertIn("not enough out-of-sample trades", reasons)
        self.assertIn("out-of-sample profit factor is below threshold", reasons)

    def test_professional_readiness_rejects_zero_trades_and_missing_regimes(
        self,
    ) -> None:
        service = ProfessionalReadinessService.__new__(ProfessionalReadinessService)
        checks = service._checks(
            option_coverage={"snapshots": 0},
            opportunities={
                "overall": {"trades": 0, "expectancy": 0.0},
                "mixed_lineage": False,
            },
            execution={
                "overall": {"trades": 0},
                "execution_quality": {},
                "mixed_lineage": False,
            },
            option_backtest={
                "status": "ok",
                "summary": {"trades": 0, "expectancy_pct": 0.0},
            },
            walk_forward={
                "status": "ok",
                "passed": False,
                "fold_count": 0,
                "out_of_sample_sessions": 0,
                "summary": {
                    "trades": 0,
                    "expectancy_pct": 0.0,
                    "profit_factor": None,
                    "max_drawdown_pct": 0.0,
                },
                "regime_stability": {
                    "passed": False,
                    "reasons": ["no regime evidence"],
                },
            },
        )

        self.assertFalse(checks["walk_forward_after_cost_expectancy"]["passed"])
        self.assertFalse(checks["walk_forward_drawdown"]["passed"])
        self.assertFalse(checks["walk_forward_regime_stability"]["passed"])


if __name__ == "__main__":
    unittest.main()
