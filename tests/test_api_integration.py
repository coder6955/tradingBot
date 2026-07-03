import os
import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from urllib.parse import urlsplit
from unittest.mock import patch

import app.api as api
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.database import init_db
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository


class FakeScanner:
    feed = None

    def scan_with_diagnostics(self, symbols=None, side="BUY", order_mode="paper"):
        return [
            {
                "symbol": "BANKNIFTY",
                "side": side,
                "order_mode": order_mode,
                "passed": False,
                "reasons": ["test rejection"],
                "signal": None,
            }
        ]


class FakeOrderService:
    def place_signal_order(self, signal, confirm_live=False, opportunity_id=None, order_mode=None):
        return {
            "status": "paper" if not confirm_live else "live",
            "confirm_live": confirm_live,
            "order_mode": order_mode,
            "tradingsymbol": signal.tradingsymbol,
            "opportunity_id": opportunity_id,
        }


class FakeExitService:
    def evaluate_once(self, limit=100):
        return {"enabled": True, "evaluated": 0, "closed": 0, "results": [], "limit": limit}


class FakePollingProvider:
    def quote(self, instruments):
        return {instruments[0]: {"last_price": 100.0}}


@dataclass
class AsgiResponse:
    status_code: int
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8") or "{}")


class SimpleAsgiClient:
    def __init__(self, app):
        self.app = app

    def get(self, path: str) -> AsgiResponse:
        return self.request("GET", path)

    def post(self, path: str, json_body=None, json=None) -> AsgiResponse:
        body_value = json if json is not None else json_body
        return self.request("POST", path, json_body=body_value)

    def request(self, method: str, path: str, json_body=None) -> AsgiResponse:
        return asyncio.run(self._request(method, path, json_body=json_body))

    async def _request(self, method: str, path: str, json_body=None) -> AsgiResponse:
        parsed = urlsplit(path)
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parsed.path,
            "raw_path": parsed.path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        sent = False
        messages = []

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        chunks = [message.get("body", b"") for message in messages if message["type"] == "http.response.body"]
        return AsgiResponse(status_code=status, body=b"".join(chunks))


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.client = SimpleAsgiClient(api.app)

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_health_endpoints(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        db = self.client.get("/db/health")
        self.assertEqual(db.status_code, 200)
        self.assertEqual(db.json()["status"], "ok")

    def test_scanner_diagnostics_endpoint(self) -> None:
        with patch.object(api, "get_scanner_service", return_value=FakeScanner()):
            response = self.client.get("/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&order_mode=paper&limit=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertIn("websocket", payload)

    def test_paper_order_placement_endpoint(self) -> None:
        payload = {
            "confirm_live": False,
            "order_mode": "paper",
            "signal": {
                "symbol": "BANKNIFTY",
                "action": "BUY_CE",
                "side": "BUY",
                "tradingsymbol": "BANKNIFTY26JUL58000CE",
                "exchange": "NFO",
                "entry_price": 100,
                "stop_loss": 90,
                "quantity": 15,
                "lot_size": 15,
                "score": 90,
            },
        }
        with patch.object(api, "get_order_service", return_value=FakeOrderService()):
            response = self.client.post("/orders/place", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paper")

    def test_live_order_mode_without_confirm_live_does_not_place_live(self) -> None:
        payload = {
            "confirm_live": False,
            "order_mode": "live",
            "signal": {
                "symbol": "BANKNIFTY",
                "action": "BUY_CE",
                "side": "BUY",
                "tradingsymbol": "BANKNIFTY26JUL58000CE",
                "exchange": "NFO",
                "entry_price": 100,
                "stop_loss": 90,
                "quantity": 15,
                "lot_size": 15,
                "score": 90,
            },
        }
        with patch.object(api, "get_order_service", return_value=FakeOrderService()):
            response = self.client.post("/orders/place", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["confirm_live"])
        self.assertEqual(response.json()["order_mode"], "live")
        self.assertEqual(response.json()["status"], "paper")

    def test_rejected_opportunity_persistence_endpoint(self) -> None:
        RejectedOpportunityRepository().save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=70,
            reasons=["liquidity/spread"],
        )

        response = self.client.get("/opportunities/rejections?symbol=BANKNIFTY&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["sample"]["total_rejected"], 1)

    def test_trades_endpoint(self) -> None:
        response = self.client.get("/trades?limit=5")

        self.assertEqual(response.status_code, 200)
        self.assertIn("trades", response.json())

    def test_evaluate_exits_endpoint(self) -> None:
        with patch.object(api, "trade_exit_service", FakeExitService()):
            response = self.client.post("/trades/evaluate-exits", json={"limit": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 3)

    def test_websocket_status_endpoint(self) -> None:
        response = self.client.get("/kite/websocket/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("websocket_enabled", response.json())

    def test_stale_live_websocket_blocks_active_price_use(self) -> None:
        original_enabled = api.settings.enable_kite_websocket
        original_blocks = api.settings.websocket_live_stale_blocks
        try:
            object.__setattr__(api.settings, "enable_kite_websocket", True)
            object.__setattr__(api.settings, "websocket_live_stale_blocks", True)
            ws = KiteWebSocketPriceFeed(api_key="k", access_token="t")
            active = ActiveTradePriceFeed(ws)

            tick = active.latest_price(
                provider=FakePollingProvider(),
                exchange="NFO",
                tradingsymbol="BANKNIFTY26JUL58000CE",
                instrument_token=123,
                mode="live",
            )
        finally:
            object.__setattr__(api.settings, "enable_kite_websocket", original_enabled)
            object.__setattr__(api.settings, "websocket_live_stale_blocks", original_blocks)

        self.assertIsNone(tick)
        self.assertIn(active.last_reason, {"websocket_disabled", "websocket_disconnected"})


if __name__ == "__main__":
    unittest.main()
