import os
import tempfile
import unittest

from app.models import Signal
from app.services.database import init_db
from app.services.opportunity_repository import OpportunityRepository


class OpportunityRepositoryTests(unittest.TestCase):
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

    def test_save_and_update_opportunity_outcome(self) -> None:
        repo = OpportunityRepository()
        signal = Signal(
            symbol="HDFCBANK",
            action="BUY_PE",
            side="BUY",
            tradingsymbol="HDFCBANK26JUN800PE",
            exchange="NFO",
            strike=800,
            expiry="2026-06-30",
            entry_price=2.4,
            stop_loss=1.87,
            target_1=3.24,
            quantity=550,
            lot_size=550,
            probability=0.78,
            risk_reward=1.59,
            score=86,
            factor_scores={"price_action": {"score": 93}},
        )

        record = repo.save_opportunity(signal)
        updated = repo.update_outcome(record.id, outcome="stop_loss", exit_price=1.87, review_notes="expiry-day false signal")
        summary = repo.summarize_performance()

        self.assertEqual(updated.outcome, "stop_loss")
        self.assertEqual(updated.status, "closed")
        self.assertLess(updated.pnl, 0)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["losses"], 1)


if __name__ == "__main__":
    unittest.main()
