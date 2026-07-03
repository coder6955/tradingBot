import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.models import Signal
from app.services.active_price_feed import PriceTick
from app.services.broker_sync_service import BrokerSyncService
from app.services.database import init_db
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository
from app.services.time_utils import ist_now_naive


class LiveExitProvider:
    def __init__(self, *, order_status: str = "COMPLETE", position_quantity: int = 0, average_price: float = 111.0, filled_quantity: int = 15) -> None:
        self.order_status = order_status
        self.position_quantity = position_quantity
        self.average_price = average_price
        self.filled_quantity = filled_quantity
        self.place_order_count = 0
        self._lock = threading.Lock()

    def instruments(self, exchange=None):
        if exchange == "NFO":
            return [{"tradingsymbol": "BANKNIFTY26JUL58000CE", "exchange": "NFO", "instrument_token": 123}]
        return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]

    def quote(self, instruments):
        return {instruments[0]: {"last_price": 111.0, "depth": {"buy": [{"price": 110.5}], "sell": [{"price": 111.0}]}}}

    def place_order(self, **kwargs):
        with self._lock:
            self.place_order_count += 1
        return {"status": "submitted", "order_id": "exit-1", **kwargs}

    def order_history(self, order_id):
        return [
            {
                "order_id": order_id,
                "status": self.order_status,
                "filled_quantity": self.filled_quantity,
                "quantity": 15,
                "average_price": self.average_price,
            }
        ]

    def positions(self):
        return {"net": [{"exchange": "NFO", "tradingsymbol": "BANKNIFTY26JUL58000CE", "quantity": self.position_quantity}]}


class BrokerPositionOnlyProvider(LiveExitProvider):
    def __init__(self, positions):
        super().__init__()
        self._positions = positions

    def positions(self):
        return self._positions


class FixedActiveFeed:
    def __init__(self, price: float = 111.0) -> None:
        self.price = price
        self.last_reason = None
        self.active_trade_tokens: set[int] = set()
        self.fallback_active = False

    def latest_price(self, *, provider, exchange, tradingsymbol, instrument_token=None, mode="paper"):
        return PriceTick(
            instrument=f"{exchange}:{tradingsymbol}",
            price=self.price,
            timestamp=ist_now_naive(),
            source="kite_websocket",
            instrument_token=instrument_token,
        )

    def subscribe(self, tokens):
        self.active_trade_tokens.update({int(token) for token in tokens})
        return {"subscribed": sorted(self.active_trade_tokens)}

    def unsubscribe(self, tokens):
        self.active_trade_tokens.difference_update({int(token) for token in tokens})
        return {"unsubscribed": sorted(tokens)}

    def status(self):
        return {"websocket_enabled": True, "websocket_connected": True, "active_trade_tokens": sorted(self.active_trade_tokens)}


class LiveExitSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "live_auto_squareoff": settings.live_auto_squareoff,
            "enable_underlying_invalidation_exit": settings.enable_underlying_invalidation_exit,
            "enable_premium_invalidation_exit": settings.enable_premium_invalidation_exit,
            "live_reconciliation_blocks_automation": settings.live_reconciliation_blocks_automation,
        }
        object.__setattr__(settings, "live_auto_squareoff", True)
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        object.__setattr__(settings, "live_reconciliation_blocks_automation", True)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.repo = TradeRepository()

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _create_live_trade(self, status: str = "filled"):
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            quantity=15,
            score=90,
        )
        return self.repo.create_trade(signal, mode="live", status=status, requested_quantity=15, placed_quantity=15, broker_order_id="entry-1")

    def _service(self, provider: LiveExitProvider) -> TradeExitService:
        return TradeExitService(
            trade_repository=self.repo,
            kite_provider_factory=lambda: provider,
            paper_trading_service=PaperTradingService(),
            active_price_feed=FixedActiveFeed(price=111.0),
        )

    def test_concurrent_exit_evaluations_submit_only_one_squareoff(self) -> None:
        self._create_live_trade()
        provider = LiveExitProvider(order_status="OPEN", position_quantity=15)
        service = self._service(provider)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.evaluate_once(limit=10), range(2)))

        self.assertEqual(provider.place_order_count, 1)
        self.assertTrue(any(item["results"][0].get("reason") == "exit_already_pending_or_closed" for item in results))
        self.assertEqual(self.repo.list_trades(limit=1)[0].status, "closing")

    def test_atomic_transition_allows_only_first_closer(self) -> None:
        record = self._create_live_trade()
        first = self.repo.try_mark_closing(int(record.id), outcome="target_1", exit_price=111)
        second = self.repo.try_mark_closing(int(record.id), outcome="stop_loss", exit_price=89)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.repo.list_trades(limit=1)[0].status, "closing")

    def test_rejected_exit_order_does_not_mark_closed(self) -> None:
        self._create_live_trade()
        provider = LiveExitProvider(order_status="REJECTED", position_quantity=15)

        result = self._service(provider).evaluate_once(limit=10)
        trade = self.repo.list_trades(limit=1)[0]

        self.assertFalse(result["results"][0]["closed"])
        self.assertEqual(trade.status, "exit_failed")
        self.assertEqual(trade.exit_order_status, "failed")

    def test_broker_position_not_flat_prevents_closed_status(self) -> None:
        self._create_live_trade()
        provider = LiveExitProvider(order_status="COMPLETE", position_quantity=15)

        result = self._service(provider).evaluate_once(limit=10)
        trade = self.repo.list_trades(limit=1)[0]

        self.assertEqual(result["results"][0]["confirmation"]["reason"], "broker_position_not_zero")
        self.assertEqual(trade.status, "closing")

    def test_successful_broker_complete_and_zero_position_marks_closed(self) -> None:
        self._create_live_trade()
        provider = LiveExitProvider(order_status="COMPLETE", position_quantity=0)

        result = self._service(provider).evaluate_once(limit=10)
        trade = self.repo.list_trades(limit=1)[0]

        self.assertTrue(result["results"][0]["closed"])
        self.assertEqual(trade.status, "closed")
        self.assertEqual(trade.exit_confirmed_at is not None, True)

    def test_stuck_exit_report_includes_closing_and_exit_failed(self) -> None:
        closing = self._create_live_trade()
        self.repo.try_mark_closing(int(closing.id), outcome="target_1", exit_price=111)
        failed = self._create_live_trade()
        self.repo.mark_exit_failed(int(failed.id), reason="test failure")

        alerts = self.repo.exit_alerts(limit=10)

        self.assertEqual({item.status for item in alerts}, {"closing", "exit_failed"})

    def test_startup_broker_without_local_trade_blocks_live_automation(self) -> None:
        provider = BrokerPositionOnlyProvider({"net": [{"exchange": "NFO", "tradingsymbol": "BANKNIFTY26JUL58000CE", "quantity": 15}]})
        service = BrokerSyncService(self.repo, kite_provider_factory=lambda: provider)

        result = service.reconcile_startup_positions()

        self.assertTrue(result["live_trading_blocked"])
        self.assertEqual(result["mismatches"][0]["type"], "broker_position_without_local_trade")

    def test_local_trade_without_broker_position_marks_mismatch_and_blocks(self) -> None:
        self._create_live_trade()
        provider = BrokerPositionOnlyProvider({"net": []})
        service = BrokerSyncService(self.repo, kite_provider_factory=lambda: provider)

        result = service.reconcile_startup_positions()
        trade = self.repo.list_trades(limit=1)[0]

        self.assertTrue(result["live_trading_blocked"])
        self.assertEqual(trade.status, "reconciliation_mismatch")

    def test_order_postback_triggers_rest_sync_idempotently(self) -> None:
        record = self._create_live_trade()
        provider = LiveExitProvider(order_status="COMPLETE", position_quantity=0)
        calls = []
        service = BrokerSyncService(self.repo, kite_provider_factory=lambda: provider, exit_confirmation_callback=lambda trade_id: calls.append(trade_id) or {"trade_id": trade_id})
        self.repo.try_mark_closing(int(record.id), outcome="target_1", exit_price=111)
        self.repo.update_exit_order_status(int(record.id), status="submitted", exit_order_id="exit-1", broker_payload={})

        first = service.handle_order_postback({"order_id": "exit-1", "status": "COMPLETE", "filled_quantity": 15, "average_price": 111})
        second = service.handle_order_postback({"order_id": "exit-1", "status": "COMPLETE", "filled_quantity": 15, "average_price": 111})

        self.assertTrue(first["handled"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(calls, [record.id])


if __name__ == "__main__":
    unittest.main()
