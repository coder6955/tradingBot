import os
import tempfile
import unittest
from types import SimpleNamespace

from app.models import Signal
from app.services.database import init_db
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
        self.orders = []

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
        self.orders.append(kwargs)
        return {"order_id": "test-order"}

    def order_history(self, order_id):  # type: ignore[no-untyped-def]
        return [{"order_id": order_id, "status": "COMPLETE", "filled_quantity": 100, "quantity": 100, "average_price": 100}]


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
        self.broker_status = None
        self.protective = None

    def create_trade(self, signal, **kwargs):  # type: ignore[no-untyped-def]
        self.created = {"signal": signal, **kwargs}
        return SimpleNamespace(id=501)

    def update_broker_status(self, trade_id, **kwargs):  # type: ignore[no-untyped-def]
        self.broker_status = {"trade_id": trade_id, **kwargs}
        return SimpleNamespace(id=trade_id)

    def update_protective_order(self, trade_id, **kwargs):  # type: ignore[no-untyped-def]
        self.protective = {"trade_id": trade_id, **kwargs}
        return SimpleNamespace(id=trade_id)


class OrderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _signal(self, **overrides):  # type: ignore[no-untyped-def]
        payload = {
            "symbol": "BANKNIFTY",
            "action": "BUY_CE",
            "side": "BUY",
            "exchange": "NFO",
            "tradingsymbol": "BANKNIFTY26JUL58000CE",
            "expiry": "2026-07-26",
            "strike": 58000,
            "entry_price": 100,
            "stop_loss": 80,
            "target_1": 125,
            "quantity": 15,
            "lot_size": 15,
            "score": 85,
            "factor_scores": {
                "strategy_metadata": {"strategy_name": "banknifty_option_buying", "strategy_version": "test"},
                "contract": {"expiry": "2026-07-26"},
            },
        }
        payload.update(overrides)
        return Signal(**payload)

    def test_place_signal_order_defaults_to_paper(self) -> None:
        service = OrderService(
            kite_provider=FailingKiteProvider(),  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
        )
        signal = self._signal()

        result = service.place_signal_order(signal, confirm_live=False)

        self.assertEqual(result["status"], "paper")
        self.assertEqual(result["trade"]["symbol"], "BANKNIFTY26JUL58000CE")

    def test_rejects_non_banknifty_signal(self) -> None:
        service = OrderService(kite_provider=FailingKiteProvider())  # type: ignore[arg-type]
        signal = self._signal(symbol="NIFTY", tradingsymbol="NIFTY24JUN22000CE")

        with self.assertRaisesRegex(ValueError, "only BANKNIFTY"):
            service.place_signal_order(signal, confirm_live=False)

    def test_rejects_expired_contract(self) -> None:
        service = OrderService(kite_provider=FailingKiteProvider())  # type: ignore[arg-type]
        signal = self._signal(expiry="2024-06-30", factor_scores={"strategy_metadata": {"strategy_version": "test"}, "contract": {"expiry": "2024-06-30"}})

        with self.assertRaisesRegex(ValueError, "expired"):
            service.place_signal_order(signal, confirm_live=False)

    def test_rejects_minimal_non_scanner_signal(self) -> None:
        service = OrderService(kite_provider=FailingKiteProvider())  # type: ignore[arg-type]
        signal = self._signal(factor_scores={})

        with self.assertRaisesRegex(ValueError, "strategy metadata"):
            service.place_signal_order(signal, confirm_live=False)

    def test_live_order_downsizes_to_available_cash(self) -> None:
        provider = LiveKiteProvider()
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            risk_management_service=PassingRiskService(),  # type: ignore[arg-type]
        )
        signal = self._signal(quantity=150, lot_size=15)

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
        self.assertEqual(result["requested_quantity"], 150)
        self.assertEqual(result["placed_quantity"], 90)
        self.assertEqual(provider.order["quantity"], 90)

    def test_live_order_places_broker_emergency_sl_when_enabled_and_entry_filled(self) -> None:
        provider = LiveKiteProvider()
        repo = CapturingTradeRepository()
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            risk_management_service=PassingRiskService(),  # type: ignore[arg-type]
            trade_repository=repo,  # type: ignore[arg-type]
        )
        signal = self._signal(quantity=90, lot_size=15)

        from app.services import order_service

        originals = {
            "live_trading_mode": order_service.settings.live_trading_mode,
            "paper_trading_mode": order_service.settings.paper_trading_mode,
            "enable_broker_emergency_sl": order_service.settings.enable_broker_emergency_sl,
        }
        try:
            object.__setattr__(order_service.settings, "live_trading_mode", True)
            object.__setattr__(order_service.settings, "paper_trading_mode", False)
            object.__setattr__(order_service.settings, "enable_broker_emergency_sl", True)
            result = service.place_signal_order(signal, confirm_live=True, order_mode="live")
        finally:
            for key, value in originals.items():
                object.__setattr__(order_service.settings, key, value)

        self.assertEqual(result["status"], "live")
        self.assertTrue(result["broker_emergency_sl"]["submitted"])
        self.assertEqual(len(provider.orders), 2)
        self.assertEqual(provider.orders[1]["transaction_type"], "SELL")
        self.assertEqual(provider.orders[1]["order_type"], "SL-M")
        self.assertEqual(provider.orders[1]["trigger_price"], 80)
        self.assertEqual(repo.protective["status"], "submitted")

    def test_execution_quality_uses_market_data_coordinator_cache(self) -> None:
        provider = CountingQuoteProvider()
        coordinator = MarketDataCoordinator(lambda: provider, quote_ttl_seconds=5)
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            market_data_coordinator=coordinator,
        )
        signal = self._signal()

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
        signal = self._signal(entry_price=105, stop_loss=90, target_1=130, score=86)
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
