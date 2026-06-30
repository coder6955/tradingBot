import os
import tempfile
import unittest
from datetime import date

from app.models import Signal
from app.services.database import init_db
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.opportunity_repository import OpportunityRepository


class FakeKiteProvider:
    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": 1.87,
            }
        }


class OpportunityOutcomeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.database_url = f"sqlite:///{self.temp_db.name}"
        init_db(self.database_url)

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_evaluate_once_marks_stop_loss_and_failure_tags(self) -> None:
        repo = OpportunityRepository()
        signal = Signal(
            symbol="HDFCBANK",
            action="BUY_PE",
            side="BUY",
            tradingsymbol="HDFCBANK26JUN800PE",
            exchange="NFO",
            strike=800,
            expiry=date.today().isoformat(),
            entry_price=2.4,
            stop_loss=1.87,
            target_1=3.24,
            quantity=550,
            lot_size=550,
            probability=0.78,
            risk_reward=1.59,
            score=86,
            factor_scores={
                "price_action": {"details": {"room_to_level_pct": 0.33}},
                "option_chain": {"details": {"pcr_volume": 0.53}, "reasons": ["selected option spread is acceptable but not ideal"]},
                "contract": {"bid": 2.3, "ask": 2.4, "last_price": 2.4},
            },
        )
        record = repo.save_opportunity(signal)
        service = OpportunityOutcomeService(repo, kite_provider_factory=lambda: FakeKiteProvider())  # type: ignore[arg-type]

        result = service.evaluate_once()
        updated = repo.get_opportunity(record.id)
        analysis = repo.failure_analysis()

        self.assertEqual(result["closed"], 1)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.outcome, "stop_loss")
        self.assertIn("low_premium_option_noise", analysis["top_failure_tags"])
        self.assertIn("insufficient_room_to_nearest_level", analysis["top_failure_tags"])


if __name__ == "__main__":
    unittest.main()
