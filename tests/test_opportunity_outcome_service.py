import os
import tempfile
import unittest
from datetime import date, timedelta

from app.models import Signal
from app.services.database import Candle, get_session, init_db
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

    def _save_candle(
        self,
        *,
        symbol: str,
        timeframe: str,
        timestamp,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
    ) -> None:
        session = get_session()
        try:
            session.add(
                Candle(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    timestamp=timestamp.replace(second=0, microsecond=0),
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=1000,
                )
            )
            session.commit()
        finally:
            session.close()

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
        service = OpportunityOutcomeService(repo, kite_provider_factory=lambda: FakeKiteProvider(), market_session_provider=lambda: "AFTER_MARKET")  # type: ignore[arg-type]

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
        service = OpportunityOutcomeService(
            repo,
            kite_provider_factory=lambda: FakeKiteProvider(),
            rejected_outcome_service=rejected_service,
            market_session_provider=lambda: "AFTER_MARKET",
        )  # type: ignore[arg-type]

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

    def test_rejected_later_outcome_replays_candles_before_quote_fallback(self) -> None:
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
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120, "target_2": 130}},
            market_session="REGULAR_MARKET",
        )
        self._save_candle(
            symbol="BANKNIFTY26JUL58000CE",
            timeframe="1minute",
            timestamp=rejected.created_at + timedelta(minutes=1),
            open_price=100,
            high_price=121,
            low_price=98,
            close_price=105,
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=105))  # type: ignore[arg-type]

        result = service.evaluate_once()
        updated = rejected_repo.list_rejections(symbol="BANKNIFTY", limit=1)[0]

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["results"][0]["source"], "candle_replay")
        self.assertEqual(updated.later_outcome, "would_have_hit_target_1")
        self.assertEqual(updated.later_exit_price, 120)
        self.assertEqual(updated.later_outcome_source, "candle_replay")
        self.assertEqual(updated.later_outcome_timeframe, "1minute")
        self.assertEqual(updated.later_outcome_minutes, 1)
        self.assertIsNotNone(updated.later_outcome_at)
        self.assertIn("source=candle_replay", updated.later_notes or "")

    def test_rejected_later_outcome_replay_uses_first_stop_before_later_target(self) -> None:
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
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120, "target_2": 130}},
            market_session="REGULAR_MARKET",
        )
        self._save_candle(
            symbol="BANKNIFTY26JUL58000CE",
            timeframe="1minute",
            timestamp=rejected.created_at + timedelta(minutes=1),
            open_price=100,
            high_price=104,
            low_price=89,
            close_price=92,
        )
        self._save_candle(
            symbol="BANKNIFTY26JUL58000CE",
            timeframe="1minute",
            timestamp=rejected.created_at + timedelta(minutes=2),
            open_price=92,
            high_price=132,
            low_price=91,
            close_price=125,
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=125))  # type: ignore[arg-type]

        result = service.evaluate_once()
        updated = rejected_repo.list_rejections(symbol="BANKNIFTY", limit=1)[0]

        self.assertEqual(result["updated"], 1)
        self.assertEqual(updated.later_outcome, "would_have_hit_stop_loss")
        self.assertEqual(updated.later_exit_price, 90)

    def test_rejected_later_outcome_replays_ws_token_candles(self) -> None:
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
            factor_scores={
                "contract": {"instrument_token": 580001},
                "prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120},
            },
            market_session="REGULAR_MARKET",
        )
        self._save_candle(
            symbol="WS_TOKEN:580001",
            timeframe="1minute",
            timestamp=rejected.created_at + timedelta(minutes=1),
            open_price=100,
            high_price=123,
            low_price=99,
            close_price=121,
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=105))  # type: ignore[arg-type]

        result = service.evaluate_once()
        updated = rejected_repo.list_rejections(symbol="BANKNIFTY", limit=1)[0]

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["results"][0]["source_symbol"], "WS_TOKEN:580001")
        self.assertEqual(updated.later_outcome, "would_have_hit_target_1")

    def test_rejected_later_outcome_labels_ambiguous_same_candle(self) -> None:
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
            reasons=["option premium has not broken recent high"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
        )
        self._save_candle(
            symbol="BANKNIFTY26JUL58000CE",
            timeframe="1minute",
            timestamp=rejected.created_at + timedelta(minutes=1),
            open_price=100,
            high_price=123,
            low_price=89,
            close_price=101,
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=101))  # type: ignore[arg-type]

        result = service.evaluate_once()
        updated = rejected_repo.list_rejections(symbol="BANKNIFTY", limit=1)[0]

        self.assertEqual(result["updated"], 1)
        self.assertTrue(result["results"][0]["ambiguous"])
        self.assertEqual(updated.later_outcome, "ambiguous_stop_and_target_same_candle")
        self.assertTrue(updated.later_outcome_ambiguous)

    def test_rejected_later_outcome_batches_progress_past_unresolved_rows(self) -> None:
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
        first = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=82,
            reasons=["first unresolved"],
            contract=OptionContract(
                tradingsymbol="BANKNIFTY26JUL57900CE",
                exchange="NFO",
                instrument_token=579001,
                name="BANKNIFTY",
                expiry=date.today().isoformat(),
                strike=57900,
                option_type="CE",
                lot_size=15,
                last_price=100,
                open_interest=50000,
                volume=10000,
                bid=99,
                ask=101,
            ),
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
        )
        second = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=82,
            reasons=["second hits target"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
        )
        self._save_candle(
            symbol="BANKNIFTY26JUL58000CE",
            timeframe="1minute",
            timestamp=second.created_at + timedelta(minutes=1),
            open_price=100,
            high_price=121,
            low_price=99,
            close_price=120,
        )
        service = RejectedOpportunityOutcomeService(rejected_repo, kite_provider_factory=lambda: FakeKiteProvider(price=100))  # type: ignore[arg-type]

        result = service.evaluate_batches(batch_limit=1, max_batches=3, delay_seconds=0)
        rows = {row.id: row for row in rejected_repo.list_rejections(symbol="BANKNIFTY", limit=10)}

        self.assertEqual(result["evaluated"], 2)
        self.assertEqual(result["updated"], 1)
        self.assertIsNone(rows[first.id].later_outcome)
        self.assertEqual(rows[second.id].later_outcome, "would_have_hit_target_1")

    def test_regular_market_defers_review_labeling_but_keeps_trade_exits(self) -> None:
        repo = OpportunityRepository()
        provider_calls: list[bool] = []

        class FakeTradeExitService:
            def evaluate_once(self, limit=100):  # type: ignore[no-untyped-def]
                return {"evaluated": 1, "limit": limit}

        def provider_factory():
            provider_calls.append(True)
            return FakeKiteProvider()

        service = OpportunityOutcomeService(
            repo,
            kite_provider_factory=provider_factory,  # type: ignore[arg-type]
            trade_exit_service=FakeTradeExitService(),  # type: ignore[arg-type]
            market_session_provider=lambda: "REGULAR_MARKET",
        )

        result = service.evaluate_once(limit=10)

        self.assertTrue(result["review_analysis_deferred"])
        self.assertEqual(result["deferred_reason"], "market_open")
        self.assertEqual(result["trade_exits"], {"evaluated": 1, "limit": 10})
        self.assertEqual(provider_calls, [])


if __name__ == "__main__":
    unittest.main()
