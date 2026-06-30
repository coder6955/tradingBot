import unittest

from app.models import Signal
from app.services.auto_trader_service import AutoTraderService


class FakeScanner:
    def scan_symbols(self, symbols=None, side="BUY"):  # type: ignore[no-untyped-def]
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

    def place_signal_order(self, signal, confirm_live=False):  # type: ignore[no-untyped-def]
        self.calls += 1
        return {"status": "paper", "symbol": signal.tradingsymbol, "confirm_live": confirm_live}


class AutoTraderServiceTests(unittest.TestCase):
    def test_scan_once_caches_latest_and_places_only_once_per_signal(self) -> None:
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
        }

        first = service.scan_once()
        second = service.scan_once()

        self.assertEqual(first["count"], 1)
        self.assertEqual(len(first["placed"]), 1)
        self.assertEqual(len(second["placed"]), 0)
        self.assertEqual(order_service.calls, 1)
        self.assertEqual(len(service.latest_opportunities), 1)


if __name__ == "__main__":
    unittest.main()
