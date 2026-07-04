import os
import tempfile
import unittest
from datetime import date

from app.models import Signal
from app.services.database import init_db
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.opportunity_repository import OpportunityRepository
from app.services.rejected_opportunity_outcome_service import RejectedOpportunityOutcomeService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_setup_service import OptionContract


class FakeKiteProvider:
    def __init__(self, price: float = 1.87) -> None:
        self.price = price

    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": self.price,
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

    def test_evaluate_once_also_marks_rejected_later_outcomes(self) -> None:
        repo = OpportunityRepository()
        rejected_repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=580001,
            name="BANKNIFTY",
            expiry=date.today().isoformat(),
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            open_interest=50000,
            volume=10000,
            bid=99,
            ask=101,
        )
        rejected = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=82,
            reasons=["final weighted score is below threshold"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120, "target_2": 130, "target_3": 140}},
            market_session="REGULAR_MARKET",
        )
        rejected_service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=121))  # type: ignore[arg-type]
        service = OpportunityOutcomeService(repo, kite_provider_factory=lambda: FakeKiteProvider(), rejected_outcome_service=rejected_service)  # type: ignore[arg-type]

        result = service.evaluate_once()
        analysis = rejected_repo.analyze(symbol="BANKNIFTY")

        self.assertEqual(result["rejected_opportunities"]["updated"], 1)
        self.assertEqual(analysis["sample"]["with_later_outcome"], 1)
        self.assertEqual(analysis["examples"][0]["id"], rejected.id)
        self.assertEqual(analysis["examples"][0]["later_outcome"], "would_have_hit_target_1")

    def test_rejected_later_outcomes_skip_non_learning_rows_by_default(self) -> None:
        rejected_repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=580001,
            name="BANKNIFTY",
            expiry=date.today().isoformat(),
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            open_interest=50000,
            volume=10000,
            bid=99,
            ask=101,
        )
        rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=82,
            reasons=["selected_option_quote_invalid"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            rejection_source="manual_diagnostic",
            market_session="REGULAR_MARKET",
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=121))  # type: ignore[arg-type]

        default_result = service.evaluate_once()
        explicit_result = service.evaluate_once(learning_only=False)
        analysis = rejected_repo.analyze(symbol="BANKNIFTY")

        self.assertEqual(default_result["evaluated"], 0)
        self.assertEqual(explicit_result["updated"], 1)
        self.assertEqual(analysis["sample"]["learning_eligible"], 0)
        self.assertEqual(analysis["sample"]["learning_excluded"], 1)
        self.assertEqual(analysis["learning_exclusion_reasons"]["manual_diagnostic"], 1)


if __name__ == "__main__":
    unittest.main()
