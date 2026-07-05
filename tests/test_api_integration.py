import os
import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from urllib.parse import urlsplit
from unittest.mock import patch

import app.api as api
from app.models import Signal
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.database import init_db
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_repository import TradeRepository


class FakeScanner:
    feed = None

    def scan_with_diagnostics(self, symbols=None, side="BUY", order_mode="paper", rejection_source="scanner"):
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


class FakeAfterMarketResearchService:
    def status(self):
        return {
            "status": "ok",
            "enabled": True,
            "running": False,
            "last_run_date": "2026-07-03",
            "next_action": "research_completed_for_today",
        }

    def run_once(self, *, trigger="manual", force=False):
        return {"action": "after_market_research", "status": "ok", "trigger": trigger, "force": force}


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

    def test_dashboard_renders_trader_cockpit_sections(self) -> None:
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        html = response.body.decode("utf-8")
        self.assertIn("Bank Nifty Option Command Center", html)
        self.assertIn("System Health", html)
        self.assertIn("Active Trade / Exit Watch", html)
        self.assertIn("Latest Watch", html)
        self.assertIn("System Thought Feed", html)
        self.assertIn("After-Market Research", html)
        self.assertIn("Research Engine", html)
        self.assertIn("Thresholds", html)
        self.assertIn("/research/after-market/status", html)
        self.assertIn("/research/research-engine", html)
        self.assertIn("/research/threshold-validation", html)

    def test_dashboard_decision_feed_shows_rejected_setup(self) -> None:
        RejectedOpportunityRepository().save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"],
        )

        response = self.client.get("/dashboard/decision-feed?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertGreaterEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["type"], "rejected_opportunity")
        self.assertIn("waiting_for_entry_trigger", payload["events"][0]["message"])

    def test_scanner_diagnostics_endpoint(self) -> None:
        with patch.object(api, "get_scanner_service", return_value=FakeScanner()):
            response = self.client.get("/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&order_mode=paper&limit=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertIn("websocket", payload)

    def test_scanner_armed_entries_endpoint(self) -> None:
        response = self.client.get("/scanner/armed-entries")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("active", payload)
        self.assertIn("entered_paper", payload)
        self.assertIn("too_late", payload)

    def test_websocket_status_exposes_armed_entry_metrics(self) -> None:
        response = self.client.get("/kite/websocket/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("armed_entry_count", payload)
        self.assertIn("armed_entry_tokens", payload)
        self.assertIn("event_entry_enabled", payload)

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

    def test_daily_banknifty_summary_endpoint(self) -> None:
        trade_repo = TradeRepository()
        trade = trade_repo.create_trade(
            self._signal("BUY_CE", "BANKNIFTY26JUL58000CE"),
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )
        trade_repo.close_trade(trade.id, outcome="target_1", exit_price=140)
        RejectedOpportunityRepository().save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_PE",
            score=76,
            reasons=["insufficient_current_session_premium_candles", "expected move is smaller than option premium target requirement"],
        )

        response = self.client.get("/research/daily-banknifty-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["symbol"], "BANKNIFTY")
        self.assertEqual(payload["total_paper_trades"], 1)
        self.assertEqual(payload["total_closed_paper_trades"], 1)
        self.assertTrue(payload["low_sample_warning"])
        self.assertFalse(payload["recommendation"]["safe_to_enable_live"])
        self.assertEqual(payload["rejection_reasons_count"]["insufficient_current_session_premium_candles"], 1)
        self.assertEqual(payload["rejection_reasons_count"]["expected_move_too_small"], 1)

    def test_daily_summary_does_not_change_scanner_endpoint(self) -> None:
        self.client.get("/research/daily-banknifty-summary")
        with patch.object(api, "get_scanner_service", return_value=FakeScanner()):
            response = self.client.get("/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&order_mode=paper&limit=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["diagnostics"][0]["reasons"], ["test rejection"])

    def test_after_market_research_endpoints(self) -> None:
        fake_service = FakeAfterMarketResearchService()
        with patch.object(api, "after_market_research_service", fake_service):
            status = self.client.get("/research/after-market/status")
            run = self.client.post("/research/after-market/run", json={"force": True})

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["next_action"], "research_completed_for_today")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["status"], "ok")
        self.assertTrue(run.json()["force"])

    def test_research_engine_endpoint(self) -> None:
        response = self.client.get("/research/research-engine?symbol=BANKNIFTY&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("filter_rejection_quality", payload)
        self.assertIn("accepted_trade_loss_impact", payload)
        self.assertIn("segment_expectancy", payload)

    def test_threshold_validation_endpoint(self) -> None:
        response = self.client.get("/research/threshold-validation?symbol=BANKNIFTY&limit=10&mode=all")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("threshold_inventory", payload)
        self.assertIn("score_threshold_validation", payload)
        self.assertIn("threshold_sensitivity", payload)
        self.assertIn("verdicts", payload)

    def test_execution_realism_endpoint(self) -> None:
        response = self.client.get("/research/execution-realism?symbol=BANKNIFTY&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("assumptions", payload)
        self.assertTrue(payload["assumptions"]["enabled"])
        self.assertIn("execution_drag", payload)

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

    def test_strategy_version_registry_endpoints(self) -> None:
        current = self.client.get("/strategy/versions/current")

        self.assertEqual(current.status_code, 200)
        current_payload = current.json()
        self.assertEqual(current_payload["status"], "ok")
        self.assertIn("config_snapshot", current_payload["version"])
        self.assertIn("settings_purpose", current_payload["version"])

        registered = self.client.post(
            "/strategy/versions/register",
            json={
                "human_note": "API test note",
                "reason_for_change": "API test reason",
            },
        )

        self.assertEqual(registered.status_code, 200)
        self.assertEqual(registered.json()["status"], "ok")
        self.assertEqual(registered.json()["version"]["human_note"], "API test note")

        listed = self.client.get("/strategy/versions?limit=5")
        self.assertEqual(listed.status_code, 200)
        self.assertGreaterEqual(listed.json()["count"], 1)

    def test_stale_live_websocket_blocks_active_price_use(self) -> None:
        original_enabled = api.settings.enable_kite_websocket
        original_blocks = api.settings.websocket_live_stale_blocks
        original_gap_fallback = api.settings.websocket_live_gap_polling_fallback
        try:
            object.__setattr__(api.settings, "enable_kite_websocket", True)
            object.__setattr__(api.settings, "websocket_live_stale_blocks", True)
            object.__setattr__(api.settings, "websocket_live_gap_polling_fallback", False)
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
            object.__setattr__(api.settings, "websocket_live_gap_polling_fallback", original_gap_fallback)

        self.assertIsNone(tick)
        self.assertIn(active.last_reason, {"websocket_disabled", "websocket_disconnected"})

    def _signal(self, action: str, tradingsymbol: str) -> Signal:
        return Signal(
            symbol="BANKNIFTY",
            action=action,
            side="BUY",
            tradingsymbol=tradingsymbol,
            exchange="NFO",
            strike=58000,
            expiry="2026-07-26",
            entry_price=100,
            stop_loss=80,
            target_1=140,
            quantity=15,
            lot_size=15,
            probability=0.78,
            risk_reward=1.5,
            score=86,
            setup_type="directional_option_buy",
            factor_scores={"score_breakdown": {"score": 86}},
        )


if __name__ == "__main__":
    unittest.main()
