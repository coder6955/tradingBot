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

    def test_fast_candidate_uses_banknifty_only_and_rejects_stale_context(self) -> None:
        calls = []

        class Scanner:
            def scan_symbols(self, **kwargs):
                calls.append(kwargs)
                return []

        class Context:
            passed = True

            def validate(self, **kwargs):
                return {"passed": self.passed, "age_seconds": 1.0}

        context = Context()
        service = AutoTraderService(
            scanner_factory=lambda: Scanner(),
            order_service_factory=lambda: object(),
            fast_scan_context_service=context,
        )
        service.config = {"side": "BUY", "order_mode": "paper", "limit": 5, "place_orders": False}
        event = {"direction": "bullish", "timestamp": datetime.now().isoformat(), "receive_timestamp": datetime.now().isoformat()}
        service._run_fast_candidate_validation(event)
        self.assertEqual(calls[0]["symbols"], ["BANKNIFTY"])
        self.assertEqual(calls[0]["rejection_source"], "fast_rally_candidate_validation")
        context.passed = False
        service._run_fast_candidate_validation(event)
        self.assertEqual(len(calls), 1)

    def test_uncalibrated_signal_keeps_probability_null_and_labels_heuristic(self) -> None:
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
            self.assertEqual(persisted.probability_source, "unavailable_insufficient_calibration")
            self.assertIsNotNone(persisted.strategy_version)
            self.assertIsNotNone(persisted.config_hash)
        finally:
            session.close()

    def test_walk_forward_uses_multiple_chronological_embargoed_folds(self) -> None:
        base = datetime(2026, 1, 1, 9, 15)
        candles = [SimpleNamespace(timestamp=base + timedelta(minutes=5 * index)) for index in range(540)]

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
            result = DeterministicBacktest().run_walk_forward(symbol="BANKNIFTY", horizon_candles=6)
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertEqual(result["fold_count"], 4)
        self.assertEqual(result["embargo_candles"], 12)
        previous_validation_end = None
        for fold in result["folds"]:
            train_end = datetime.fromisoformat(fold["train_end"])
            validation_start = datetime.fromisoformat(fold["validation_start"])
            self.assertGreaterEqual((validation_start - train_end).total_seconds(), 12 * 5 * 60)
            if previous_validation_end is not None:
                self.assertGreater(validation_start, previous_validation_end)
            previous_validation_end = datetime.fromisoformat(fold["validation_end"])


if __name__ == "__main__":
    unittest.main()
