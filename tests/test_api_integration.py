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
from app.services.database import TradeRecord, init_db
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

    def get(self, path: str, headers: dict[str, str] | None = None) -> AsgiResponse:
        return self.request("GET", path, headers=headers)

    def post(self, path: str, json_body=None, json=None, headers: dict[str, str] | None = None) -> AsgiResponse:
        body_value = json if json is not None else json_body
        return self.request("POST", path, json_body=body_value, headers=headers)

    def request(self, method: str, path: str, json_body=None, headers: dict[str, str] | None = None) -> AsgiResponse:
        return asyncio.run(self._request(method, path, json_body=json_body, headers=headers))

    async def _request(self, method: str, path: str, json_body=None, headers: dict[str, str] | None = None) -> AsgiResponse:
        parsed = urlsplit(path)
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        request_headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        for key, value in (headers or {}).items():
            request_headers.append((key.lower().encode("ascii"), value.encode("utf-8")))
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parsed.path,
            "raw_path": parsed.path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "headers": request_headers,
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
        self.market_session_patcher = patch.object(api, "_current_market_session", return_value="AFTER_MARKET")
        self.market_session_patcher.start()
        self._api_auth_token = api.settings.api_auth_token
        self._api_auth_required = api.settings.api_auth_required

    def tearDown(self) -> None:
        self.market_session_patcher.stop()
        api._research_report_cache.clear()
        object.__setattr__(api.settings, "api_auth_token", self._api_auth_token)
        object.__setattr__(api.settings, "api_auth_required", self._api_auth_required)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _enable_api_auth(self) -> dict[str, str]:
        object.__setattr__(api.settings, "api_auth_token", "test-token")
        object.__setattr__(api.settings, "api_auth_required", True)
        return {"Authorization": "Bearer test-token"}

    def test_health_endpoints(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        db = self.client.get("/db/health")
        self.assertEqual(db.status_code, 200)
        self.assertEqual(db.json()["status"], "ok")

    def test_protected_endpoint_requires_api_auth_when_configured(self) -> None:
        self._enable_api_auth()

        unauthorized = self.client.post("/automation/start", json={"symbols": "BANKNIFTY"})
        invalid = self.client.post("/automation/start", json={"symbols": "BANKNIFTY"}, headers={"X-API-Key": "wrong"})

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(invalid.status_code, 401)

    def test_protected_endpoint_accepts_bearer_token(self) -> None:
        headers = self._enable_api_auth()
        with patch.object(api.automation_supervisor_service, "start", return_value={"running": True, "duplicate_start_prevented": False}):
            response = self.client.post("/automation/start", json={"symbols": "BANKNIFTY"}, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["running"])

    def test_auto_trader_status_does_not_evaluate_risk(self) -> None:
        with patch.object(api.auto_trader_service.risk_management_service, "evaluate_entry", side_effect=AssertionError("risk evaluated")):
            response = self.client.get("/auto-trader/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk"]["status"], "deferred")

    def test_heavy_ingestion_blocked_during_market_without_manual_override(self) -> None:
        headers = self._enable_api_auth()
        self.market_session_patcher.stop()
        self.market_session_patcher = patch.object(api, "_current_market_session", return_value="REGULAR_MARKET")
        self.market_session_patcher.start()

        blocked = self.client.post("/data/ingest/candles", json={"symbols": "BANKNIFTY"}, headers=headers)
        self.assertEqual(blocked.status_code, 409)

        with patch.object(api.data_ingestion_service, "ingest_candles", return_value={"status": "ok", "days": 365}) as ingest:
            allowed = self.client.post(
                "/data/ingest/candles",
                json={"symbols": "BANKNIFTY", "days": 999, "manual_override": True},
                headers=headers,
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(ingest.call_args.kwargs["days"], 365)

    def test_dashboard_renders_trader_cockpit_sections(self) -> None:
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        html = response.body.decode("utf-8")
        self.assertIn("Bank Nifty Option Command Center", html)
        self.assertIn("System Health", html)
        self.assertIn("Active Trade / Exit Watch", html)
        self.assertIn("Latest Watch", html)
        self.assertIn("System Thought Feed", html)
        self.assertIn("V4 Strategy &amp; Market Session", html)
        self.assertIn("Market Data Pipeline", html)
        self.assertIn("WebSocket &amp; Subscription Ownership", html)
        self.assertIn("Armed Entry Lifecycle", html)
        self.assertIn("Trading-Path Latency", html)
        self.assertIn("Bank Nifty Constituent Intelligence", html)
        self.assertIn("Broker Protection &amp; Reconciliation", html)
        self.assertIn("Professional Readiness", html)
        self.assertIn("After-Market Research", html)
        self.assertIn("Research Engine", html)
        self.assertIn("Thresholds", html)
        self.assertIn("/research/after-market/status", html)
        self.assertIn("/research/research-engine", html)
        self.assertIn("/research/threshold-validation", html)
        self.assertIn("Confirm Automation Mode", html)
        self.assertIn("/runtime/trading-config", html)
        self.assertIn("const DASHBOARD_REFRESH_MS = 15000;", html)
        self.assertIn("const DASHBOARD_BROKER_REFRESH_MS = 60000;", html)
        self.assertIn("AbortController", html)
        self.assertIn("timeoutMs=4500", html)
        self.assertIn("getJsonCached(\"margins\", \"/kite/margins\", DASHBOARD_BROKER_REFRESH_MS)", html)
        self.assertIn("getReviewJsonCached(\"learning\", \"/research/outcome-learning\"", html)
        self.assertIn("/research/gate-effectiveness?summary_only=true&limit=500&top_n=12&cache_seconds=300", html)
        self.assertIn("dashboard_skip", html)
        self.assertIn("/market-data/pipeline-status", html)
        self.assertIn("/runtime/status", html)
        self.assertIn("/strategy/versions/current", html)
        self.assertIn("/scanner/armed-entries", html)
        self.assertIn("/market-data/banknifty-constituents/status", html)
        self.assertIn("/broker/reconciliation/status", html)
        self.assertIn("/broker/emergency-protection/status", html)
        self.assertIn("should_run_live_modules", html)
        self.assertIn("System Safe — Live Modules Idle", html)
        self.assertIn("exit_executable_price", html)

    def test_banknifty_constituent_status_endpoint(self) -> None:
        response = self.client.get("/market-data/banknifty-constituents/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_date"], "2026-06-30")
        self.assertEqual(payload["constituent_count"], 14)
        self.assertTrue(payload["valid"])
        self.assertEqual(len(payload["constituents"]), 14)

    def test_runtime_trading_config_switches_live_and_paper_modes(self) -> None:
        preview = self.client.get("/runtime/trading-config/preview?mode=live")
        self.assertEqual(preview.status_code, 200)
        live_preview = preview.json()
        self.assertEqual(live_preview["mode"], "live")
        self.assertTrue(live_preview["required"]["settings_overrides"]["LIVE_TRADING_MODE"])
        self.assertIn("Broker emergency SL", [choice["label"] for choice in live_preview["optional_choices"]])

        live = self.client.post(
            "/runtime/trading-config/apply",
            json={
                "mode": "live",
                "warning_acknowledged": True,
                "options": {"broker_emergency_sl": True, "event_driven_live_entry": False},
            },
        )
        self.assertEqual(live.status_code, 200)
        live_payload = live.json()
        self.assertEqual(live_payload["mode"], "live")
        self.assertTrue(live_payload["effective"]["automation"]["confirm_live"])
        self.assertTrue(live_payload["effective"]["runtime_options"]["broker_emergency_sl"])
        self.assertEqual(live_payload["required"]["settings_overrides"]["MAX_OPEN_TRADES"], 1)

        paper = self.client.post(
            "/runtime/trading-config/apply",
            json={"mode": "paper", "warning_acknowledged": True, "options": {"broker_emergency_sl": True}},
        )
        self.assertEqual(paper.status_code, 200)
        paper_payload = paper.json()
        self.assertEqual(paper_payload["mode"], "paper")
        self.assertFalse(paper_payload["effective"]["automation"]["confirm_live"])
        self.assertFalse(paper_payload["effective"]["runtime_options"]["broker_emergency_sl"])
        self.assertFalse(paper_payload["required"]["settings_overrides"]["LIVE_TRADING_MODE"])

    def test_runtime_trading_config_preview_shows_all_optional_choices(self) -> None:
        preview = self.client.get("/runtime/trading-config/preview?mode=live")

        self.assertEqual(preview.status_code, 200)
        keys = {choice["key"] for choice in preview.json()["optional_choices"]}
        self.assertEqual(keys, set(api.runtime_trading_config_service.OPTIONAL_DEFAULTS))

    def test_runtime_status_is_lightweight_control_snapshot(self) -> None:
        response = self.client.get("/runtime/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("session", payload)
        self.assertIn("market_data_cache", payload)
        self.assertIn("runtime_jobs", payload)

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

    def test_scanner_opportunities_returns_fast_when_market_closed(self) -> None:
        with patch.object(api, "_scanner_market_is_open", return_value=False), patch.object(api, "get_scanner_service", side_effect=AssertionError("scanner should not be built")):
            response = self.client.get("/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["market_open"])
        self.assertEqual(payload["reason"], "market_closed")
        self.assertEqual(payload["market_data"], "not_requested")

    def test_review_analysis_endpoints_defer_during_market(self) -> None:
        with (
            patch.object(api, "_current_market_session", return_value="REGULAR_MARKET"),
            patch.object(api.outcome_learning_service, "analyze", side_effect=AssertionError("learning should be deferred")),
            patch.object(api.opportunity_repository, "failure_analysis", side_effect=AssertionError("failure analysis should be deferred")),
        ):
            learning = self.client.get("/research/outcome-learning")
            failures = self.client.get("/opportunities/failure-analysis")

        self.assertEqual(learning.status_code, 200)
        self.assertEqual(failures.status_code, 200)
        self.assertEqual(learning.json()["status"], "deferred")
        self.assertEqual(learning.json()["reason"], "market_open")
        self.assertEqual(failures.json()["status"], "deferred")
        self.assertEqual(failures.json()["report"], "opportunity_failure_analysis")

    def test_scanner_opportunities_starts_background_refresh_when_cache_empty(self) -> None:
        api.scanner_response_cache.clear()
        started: list[str] = []

        def fake_start(**kwargs):
            started.append(kwargs["cache_key"])

        with (
            patch.object(api, "_scanner_market_is_open", return_value=True),
            patch.object(api, "_start_scanner_refresh", side_effect=fake_start),
            patch.object(api, "get_scanner_service", side_effect=AssertionError("scanner should not run in request thread")),
        ):
            response = self.client.get("/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "warming")
        self.assertEqual(payload["reason"], "scanner_refresh_started")
        self.assertEqual(started, ["BUY|BANKNIFTY|3|paper"])

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

    def test_trade_test_artifacts_can_be_quarantined(self) -> None:
        trade_repo = TradeRepository()
        trade_repo.create_trade(
            Signal(
                symbol="NIFTY",
                action="BUY_CE",
                side="BUY",
                tradingsymbol="NIFTY24JUN22000CE",
                exchange="NFO",
                entry_price=100,
                stop_loss=80,
                quantity=50,
                score=85,
            ),
            mode="paper",
            status="filled",
            requested_quantity=50,
            placed_quantity=50,
        )

        report = self.client.get("/trades/test-artifacts")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["count"], 1)
        self.assertEqual(report.json()["artifacts"][0]["tradingsymbol"], "NIFTY24JUN22000CE")

        dry_run = self.client.post("/trades/test-artifacts/quarantine", json={"dry_run": True})
        self.assertEqual(dry_run.status_code, 200)
        self.assertTrue(dry_run.json()["dry_run"])
        self.assertEqual(self.client.get("/trades/test-artifacts").json()["count"], 1)

        quarantine = self.client.post("/trades/test-artifacts/quarantine", json={"dry_run": False})
        self.assertEqual(quarantine.status_code, 200)
        self.assertFalse(quarantine.json()["dry_run"])
        self.assertEqual(self.client.get("/trades/test-artifacts").json()["count"], 0)
        self.assertEqual(self.client.get("/trades").json()["count"], 0)
        self.assertEqual(self.client.get("/trades?include_artifacts=true").json()["count"], 1)

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

    def test_evidence_matrix_endpoint_is_read_only_and_bounded(self) -> None:
        class FakeEvidence:
            def report(self, *, group_by=None, limit=0):
                return {"dimensions": group_by, "limit": limit, "groups": [], "accepted_outcomes": 0, "rejected_observations": 0}

        with patch.object(api, "evidence_matrix_service", FakeEvidence()):
            response = self.client.get("/research/evidence-matrix?group_by=setup_family,market_regime&limit=99999")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dimensions"], ["setup_family", "market_regime"])
        self.assertEqual(response.json()["limit"], 10000)

    def test_professional_readiness_endpoint_serves_after_market_cache_only(self) -> None:
        class FakeCachedResearch:
            def status(self):
                return {
                    "last_run_date": "2026-07-20",
                    "last_result": {
                        "reports": {
                            "professional_readiness": {
                                "result": {"status": "ok", "ready_for_live": False, "checks": []}
                            }
                        }
                    },
                }

        with patch.object(api, "after_market_research_service", FakeCachedResearch()), patch.object(
            api.professional_readiness_service, "report", side_effect=AssertionError("hot-path report must not run")
        ):
            response = self.client.get("/research/professional-readiness")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["non_blocking"])
        self.assertEqual(response.json()["source"], "after_market_research_cache")

    def test_research_engine_endpoint(self) -> None:
        response = self.client.get("/research/research-engine?symbol=BANKNIFTY&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("filter_rejection_quality", payload)
        self.assertIn("accepted_trade_loss_impact", payload)
        self.assertIn("segment_expectancy", payload)

    def test_gate_effectiveness_endpoint_uses_summary_cache(self) -> None:
        api._research_report_cache.clear()
        report = {
            "status": "ok",
            "summary_only": True,
            "rejected_summary": {"reviewed": 1},
            "gates": [{"gate_or_reason": "premium_confirmation_failed"}],
        }
        with patch.object(api.professional_insights_service, "gate_effectiveness_report", return_value=report) as gate_report:
            first = self.client.get("/research/gate-effectiveness?summary_only=true&limit=500&top_n=12&cache_seconds=300")
            second = self.client.get("/research/gate-effectiveness?summary_only=true&limit=500&top_n=12&cache_seconds=300")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["cache_hit"])
        self.assertTrue(second.json()["cache_hit"])
        self.assertEqual(gate_report.call_count, 1)
        self.assertTrue(gate_report.call_args.kwargs["summary_only"])
        self.assertEqual(gate_report.call_args.kwargs["limit"], 500)
        self.assertEqual(gate_report.call_args.kwargs["top_n"], 12)

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

    def test_trade_serialization_exposes_executable_exit_and_lineage(self) -> None:
        trade = TradeRecord(
            symbol="BANKNIFTY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            action="BUY_CE",
            side="BUY",
            mode="paper",
            status="open",
            requested_quantity=15,
            placed_quantity=15,
            filled_quantity=15,
            exit_ltp=105.0,
            exit_best_bid=104.5,
            exit_best_ask=105.5,
            exit_executable_price=104.25,
            exit_depth_coverage=1.0,
            exit_spread_pct=0.95,
            exit_execution_source="bid_depth_vwap",
            exit_triggered_rules_json='["stop_loss"]',
            strategy_version="banknifty_option_buying_v3",
            config_hash="abc123",
        )

        payload = api.trade_record_to_dict(trade)

        self.assertEqual(payload["exit_executable_price"], 104.25)
        self.assertEqual(payload["exit_best_bid"], 104.5)
        self.assertEqual(payload["exit_triggered_rules"], ["stop_loss"])
        self.assertEqual(payload["exit_execution_source"], "bid_depth_vwap")
        self.assertEqual(payload["strategy_version"], "banknifty_option_buying_v3")
        self.assertEqual(payload["config_hash"], "abc123")

    def test_evaluate_exits_endpoint(self) -> None:
        with patch.object(api, "trade_exit_service", FakeExitService()):
            response = self.client.post("/trades/evaluate-exits", json={"limit": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 3)

    def test_websocket_status_endpoint(self) -> None:
        response = self.client.get("/kite/websocket/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("websocket_enabled", response.json())

    def test_runtime_status_uses_websocket_status_keys(self) -> None:
        with patch.object(
            api.application_context.market_data_runtime_service,
            "control_status",
            return_value={
                "state": "CONNECTED",
                "reason": None,
                "websocket_status": "CONNECTED",
                "running": True,
                "connected": True,
                "market_session": "REGULAR_MARKET",
                "duplicate_start_prevented_count": 0,
                "reconnect_count": 1,
                "disconnect_count": 0,
                "last_error": None,
            },
        ):
            response = self.client.get("/runtime/status")

        self.assertEqual(response.status_code, 200)
        websocket = response.json()["websocket"]
        self.assertEqual(websocket["state"], "CONNECTED")
        self.assertEqual(websocket["status"], "CONNECTED")
        self.assertTrue(websocket["connected"])

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
