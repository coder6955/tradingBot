import unittest

from app.models import Signal
from app.services.auto_trader_service import AutoTraderService


class FakeScanner:
    def scan_symbols(self, symbols=None, side="BUY", order_mode="paper"):  # type: ignore[no-untyped-def]
        return [
            Signal(
                symbol="NIFTY",
                action="BUY_CE",
                side=side,
                tradingsymbol="NIFTY24JUN22000CE",
                entry_price=100,
                stop_loss=80,
                quantity=50,
                score=85,
            )
        ]


class FakeOrderService:
    def __init__(self) -> None:
        self.calls = 0

    def place_signal_order(self, signal, confirm_live=False, opportunity_id=None, order_mode="paper"):  # type: ignore[no-untyped-def]
        self.calls += 1
        return {"status": order_mode, "symbol": signal.tradingsymbol, "confirm_live": confirm_live}


class AutoTraderServiceTests(unittest.TestCase):
    def test_paper_scan_once_records_repeated_qualified_signals_for_learning(self) -> None:
        order_service = FakeOrderService()
        service = AutoTraderService(
            scanner_factory=lambda: FakeScanner(),  # type: ignore[arg-type]
            order_service_factory=lambda: order_service,  # type: ignore[arg-type]
        )
        service.config = {
            "side": "BUY",
            "symbols": ["NIFTY"],
            "interval_seconds": 1,
            "limit": 5,
            "place_orders": True,
            "confirm_live": False,
            "order_mode": "paper",
        }

        first = service.scan_once()
        second = service.scan_once()

        self.assertEqual(first["count"], 1)
        self.assertEqual(len(first["placed"]), 1)
        self.assertEqual(len(second["placed"]), 1)
        self.assertEqual(order_service.calls, 2)
        self.assertEqual(len(service.latest_opportunities), 1)

    def test_paper_mode_can_react_to_enter_now_signal(self) -> None:
        order_service = FakeOrderService()
        service = AutoTraderService(
            scanner_factory=lambda: FakeScanner(),  # type: ignore[arg-type]
            order_service_factory=lambda: order_service,  # type: ignore[arg-type]
        )
        service.config = {
            "side": "BUY",
            "symbols": ["BANKNIFTY"],
            "interval_seconds": 1,
            "limit": 1,
            "place_orders": True,
            "confirm_live": False,
            "order_mode": "paper",
        }

        result = service.scan_once()

        self.assertEqual(len(result["placed"]), 1)
        self.assertEqual(result["placed"][0]["result"]["status"], "paper")
        self.assertFalse(result["placed"][0]["result"]["confirm_live"])

    def test_live_mode_keeps_existing_confirm_live_guard(self) -> None:
        order_service = FakeOrderService()
        service = AutoTraderService(
            scanner_factory=lambda: FakeScanner(),  # type: ignore[arg-type]
            order_service_factory=lambda: order_service,  # type: ignore[arg-type]
        )
        service.config = {
            "side": "BUY",
            "symbols": ["BANKNIFTY"],
            "interval_seconds": 1,
            "limit": 1,
            "place_orders": True,
            "confirm_live": False,
            "order_mode": "live",
        }

        result = service.scan_once()

        self.assertEqual(len(result["placed"]), 1)
        self.assertEqual(result["placed"][0]["result"]["status"], "live")
        self.assertFalse(result["placed"][0]["result"]["confirm_live"])


if __name__ == "__main__":
    unittest.main()
