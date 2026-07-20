import os
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from app.models import Signal
from app.services.database import init_db
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.opportunity_repository import OpportunityRepository
from app.services.rejected_opportunity_outcome_service import RejectedOpportunityOutcomeService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_setup_service import OptionContract


class CountingProvider:
    def __init__(self, price: float = 121.0, delay_seconds: float = 0.0) -> None:
        self.price = price
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.quote_count = 0
        self.instrument_count = 0

    def quote(self, instruments):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        with self.lock:
            self.quote_count += 1
        return {instrument: {"last_price": self.price} for instrument in instruments}

    def instruments(self, exchange=None):
        with self.lock:
            self.instrument_count += 1
        if exchange == "NSE":
            return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]
        return [{"tradingsymbol": "BANKNIFTY26JUL58000CE", "exchange": exchange or "NFO", "instrument_token": 123}]


class MarketDataCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_quote_cache_reuses_provider_call_inside_ttl(self) -> None:
        provider = CountingProvider()
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)

        first = coordinator.quote(["NFO:BANKNIFTY26JUL58000CE"])
        second = coordinator.quote(["NFO:BANKNIFTY26JUL58000CE"])

        self.assertEqual(provider.quote_count, 1)
        self.assertEqual(first["NFO:BANKNIFTY26JUL58000CE"]["last_price"], 121.0)
        self.assertEqual(second["NFO:BANKNIFTY26JUL58000CE"]["last_price"], 121.0)
        self.assertEqual(coordinator.status()["quote_cache_hits"], 1)

    def test_instrument_cache_reuses_provider_call_inside_ttl(self) -> None:
        provider = CountingProvider()
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)

        first = coordinator.instruments("NSE", provider=provider)
        second = coordinator.instruments("NSE", provider=provider)

        self.assertEqual(provider.instrument_count, 1)
        self.assertEqual(first[0]["tradingsymbol"], "NIFTY BANK")
        self.assertEqual(second[0]["instrument_token"], 260105)
        self.assertEqual(coordinator.status()["instrument_cache_hits"], 1)

    def test_concurrent_quote_requests_reuse_inflight_provider_call(self) -> None:
        provider = CountingProvider(delay_seconds=0.05)
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)
        instrument = "NFO:BANKNIFTY26JUL58000CE"
        barrier = threading.Barrier(5)

        def call_quote(_):
            barrier.wait(timeout=2)
            return coordinator.quote([instrument])

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(call_quote, range(5)))

        self.assertTrue(all(result[instrument]["last_price"] == 121.0 for result in results))
        self.assertEqual(provider.quote_count, 1)
        self.assertGreaterEqual(coordinator.status()["quote_inflight_reused"], 1)

    def test_rejected_outcome_does_not_infer_first_touch_from_shared_current_quote(self) -> None:
        provider = CountingProvider(price=121.0)
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)
        opportunity_repo = OpportunityRepository()
        rejected_repo = RejectedOpportunityRepository()
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            expiry=date.today().isoformat(),
            entry_price=100,
            stop_loss=90,
            target_1=120,
            quantity=15,
            lot_size=15,
            score=85,
        )
        opportunity_repo.save_opportunity(signal)
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", date.today().isoformat(), 58000, "CE", 15)
        rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=82,
            reasons=["final weighted score is below threshold"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
        )
        rejected_service = RejectedOpportunityOutcomeService(
            rejected_repo,
            kite_provider_factory=lambda: provider,
            market_data_coordinator=coordinator,
        )
        service = OpportunityOutcomeService(
            opportunity_repo,
            kite_provider_factory=lambda: provider,
            rejected_outcome_service=rejected_service,
            market_data_coordinator=coordinator,
            market_session_provider=lambda: "AFTER_MARKET",
        )

        result = service.evaluate_once()

        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["rejected_opportunities"]["updated"], 0)
        self.assertEqual(result["rejected_opportunities"]["results"][0]["reason"], "chronological_outcome_pending")
        self.assertEqual(provider.quote_count, 1)


if __name__ == "__main__":
    unittest.main()
