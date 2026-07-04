import unittest
from types import SimpleNamespace

from app.models import Signal
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService


class FailingKiteProvider:
    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": 100,
                "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100}]},
            }
        }

    def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("live order should not be called")


class LiveKiteProvider:
    def __init__(self) -> None:
        self.order = None

    def margins(self):  # type: ignore[no-untyped-def]
        return {"equity": {"available": {"cash": 10000}}}

    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": 100,
                "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100}]},
            }
        }

    def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.order = kwargs
        return {"order_id": "test-order"}


class CountingQuoteProvider(FailingKiteProvider):
    def __init__(self) -> None:
        self.quote_count = 0

    def quote(self, instruments):  # type: ignore[no-untyped-def]
        self.quote_count += 1
        return super().quote(instruments)


class PassingRiskService:
    def evaluate_signal(self, symbol):  # type: ignore[no-untyped-def]
        return {"passed": True, "reasons": []}


class CapturingTradeRepository:
    def __init__(self) -> None:
        self.created = None

    def create_trade(self, signal, **kwargs):  # type: ignore[no-untyped-def]
        self.created = {"signal": signal, **kwargs}
        return SimpleNamespace(id=501)


class OrderServiceTests(unittest.TestCase):
    def test_place_signal_order_defaults_to_paper(self) -> None:
        service = OrderService(
            kite_provider=FailingKiteProvider(),  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
        )
        signal = Signal(
            symbol="NIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="NIFTY24JUN22000CE",
            entry_price=100,
            stop_loss=80,
            quantity=50,
            score=85,
        )

        result = service.place_signal_order(signal, confirm_live=False)

        self.assertEqual(result["status"], "paper")
        self.assertEqual(result["trade"]["symbol"], "NIFTY24JUN22000CE")

    def test_live_order_downsizes_to_available_cash(self) -> None:
        provider = LiveKiteProvider()
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            risk_management_service=PassingRiskService(),  # type: ignore[arg-type]
        )
        signal = Signal(
            symbol="NIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="NIFTY24JUN22000CE",
            entry_price=100,
            stop_loss=80,
            quantity=500,
            lot_size=50,
            score=85,
        )

        from app.services import order_service

        original_live = order_service.settings.live_trading_mode
        original_paper = order_service.settings.paper_trading_mode
        try:
            object.__setattr__(order_service.settings, "live_trading_mode", True)
            object.__setattr__(order_service.settings, "paper_trading_mode", False)
            result = service.place_signal_order(signal, confirm_live=True, order_mode="live")
        finally:
            object.__setattr__(order_service.settings, "live_trading_mode", original_live)
            object.__setattr__(order_service.settings, "paper_trading_mode", original_paper)

        self.assertEqual(result["status"], "live")
        self.assertEqual(result["requested_quantity"], 500)
        self.assertEqual(result["placed_quantity"], 100)
        self.assertEqual(provider.order["quantity"], 100)

    def test_execution_quality_uses_market_data_coordinator_cache(self) -> None:
        provider = CountingQuoteProvider()
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            market_data_coordinator=coordinator,
        )
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            exchange="NFO",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            entry_price=100,
            stop_loss=80,
            quantity=15,
            score=85,
        )

        service.place_signal_order(signal, confirm_live=False)
        service.place_signal_order(signal, confirm_live=False)

        self.assertEqual(provider.quote_count, 1)
        self.assertGreaterEqual(coordinator.status()["quote_cache_hits"], 1)

    def test_event_driven_metadata_is_saved_on_paper_trade(self) -> None:
        repo = CapturingTradeRepository()
        service = OrderService(
            kite_provider=FailingKiteProvider(),  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            trade_repository=repo,  # type: ignore[arg-type]
        )
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            exchange="NFO",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            entry_price=105,
            stop_loss=90,
            quantity=15,
            score=86,
        )
        metadata = {
            "entry_source": "event_driven_websocket",
            "armed_setup_id": "armed-test",
            "trigger_price": 105,
            "executable_entry_price": 105,
        }

        result = service.place_signal_order(
            signal,
            confirm_live=False,
            order_mode="paper",
            metadata=metadata,
            execution_quality_override={"passed": True, "reasons": [], "details": {"source": "event_driven_websocket"}},
        )

        self.assertEqual(result["trade"]["metadata"]["entry_source"], "event_driven_websocket")
        self.assertEqual(repo.created["order_response"]["metadata"]["armed_setup_id"], "armed-test")
        self.assertIn("entry_source=event_driven_websocket", repo.created["notes"])


if __name__ == "__main__":
    unittest.main()
