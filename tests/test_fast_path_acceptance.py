import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.services.auto_trader_service import AutoTraderService
from app.services.backtest_service import BacktestService
from app.services.database import (
    Candle,
    RejectedOpportunityRecord,
    get_session,
    init_db,
)
from app.services.fast_scan_context_service import FastScanContextService
from app.services.io_call_metrics_service import io_call_metrics
from app.services.latency_metrics_service import LatencyMetricsService
from app.services.multi_timeframe_context_service import MultiTimeframeContextService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository


class _MemoryWebSocket:
    def __init__(self, tick=None, *, connected=True, gap=False):
        self.tick = tick
        self.connected = connected
        self.gap = gap

    def entry_health(self):
        return {"connected": self.connected, "entry_blocking_gap": self.gap}

    def get_latest_tick(self, instrument_token):
        return self.tick if instrument_token == 101 else None


class FastPathAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self):
        try:
            os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _context(self, *, tick=None, created_at=None, candidate_overrides=None):
        now = datetime.now().replace(tzinfo=None)
        tick = tick or SimpleNamespace(
            price=101.0,
            bid=100.5,
            ask=101.0,
            volume=5000,
            timestamp=now,
            receive_timestamp=now,
            buy_depth=({"price": 100.5, "quantity": 100},),
        )
        service = FastScanContextService(_MemoryWebSocket(tick))
        service.refresh(
            symbols=["BANKNIFTY"],
            market_snapshots={"BANKNIFTY": {"price": 58100}},
            completed_candles={
                "1minute": [{"last_completed_at": now.isoformat()}],
                "5minute": [{"last_completed_at": now.isoformat()}],
            },
            candidates={
                "bullish": {
                    "instrument_token": 101,
                    "tradingsymbol": "BANKNIFTY_TEST_CE",
                    "entry_price": 100.0,
                    "entry_trigger_price": 100.0,
                    "stop_loss": 90.0,
                    "target_1": 130.0,
                    "directional_agreement": True,
                    "constituent_participation": True,
                    "data_fresh": True,
                    "gap_safe": True,
                    "risk_preflight": True,
                    **(candidate_overrides or {}),
                }
            },
            created_at=created_at,
        )
        return service

    def test_cached_candidate_validation_has_zero_rest_and_database_calls(self):
        service = self._context()
        with io_call_metrics.measure("fast_acceptance") as calls:
            decision = service.validate_candidate({"direction": "bullish"})
        self.assertTrue(decision["passed"], decision)
        self.assertEqual(calls.rest_calls, 0)
        self.assertEqual(calls.database_queries, 0)

    def test_latency_telemetry_exposes_targets_without_inventing_live_samples(self):
        report = LatencyMetricsService().report()
        self.assertEqual(report["acceptance_targets_ms"]["websocket_callback_p95"], 5.0)
        self.assertEqual(
            report["acceptance_targets_ms"]["cached_candidate_decision_p95"], 100.0
        )
        self.assertIsNone(report["metrics"]["websocket_callback_duration"]["p95_ms"])
        self.assertIsNone(
            report["metrics"]["cached_candidate_decision_duration"]["p95_ms"]
        )

    def test_stale_context_rejects_without_synchronous_fallback(self):
        created_at = datetime.now() - timedelta(
            seconds=settings.fast_scan_context_max_age_seconds + 1
        )
        service = self._context(created_at=created_at)
        with io_call_metrics.measure("fast_stale") as calls:
            decision = service.validate_candidate({"direction": "bullish"})
        self.assertEqual(decision["reason"], "fast_scan_context_stale")
        self.assertEqual((calls.rest_calls, calls.database_queries), (0, 0))

    def test_soft_confirmation_pauses_fast_candidate_without_hard_rejection(self):
        service = self._context(
            candidate_overrides={
                "directional_agreement": False,
                "constituent_participation": False,
                "one_minute_opposed": True,
                "constituent_strongly_opposed": False,
            }
        )

        decision = service.validate_candidate({"direction": "bullish"})

        self.assertFalse(decision["passed"])
        self.assertEqual(decision["state"], "WATCHING_SETUP")
        self.assertFalse(decision["hard_rejection"])
        self.assertEqual(decision["reason"], "one_minute_opposes_five_minute_wait")

    def test_mixed_constituents_do_not_block_fast_candidate(self):
        service = self._context(
            candidate_overrides={
                "directional_agreement": False,
                "constituent_participation": False,
                "one_minute_opposed": False,
                "constituent_strongly_opposed": False,
            }
        )

        decision = service.validate_candidate({"direction": "bullish"})

        self.assertTrue(decision["passed"], decision)

    def test_scheduled_scan_enforces_explicit_rest_call_budget(self):
        class Scanner:
            def scan_symbols(self, **kwargs):
                for index in range(settings.scheduled_scan_max_rest_calls + 1):
                    io_call_metrics.record_rest(f"test-{index}")
                return []

        service = AutoTraderService(lambda: Scanner(), lambda: object())
        service.config = {
            "side": "BUY",
            "order_mode": "paper",
            "limit": 1,
            "place_orders": False,
        }
        result = service.scan_once()
        self.assertTrue(result["io_calls"]["budget_exceeded"])
        self.assertEqual(result["count"], 0)

    def test_overlapping_fast_events_do_not_scan_or_place_orders(self):
        calls = {"validate": 0, "scanner": 0, "orders": 0}

        class Context:
            def validate_candidate(self, event):
                calls["validate"] += 1
                time.sleep(0.05)
                return {"passed": True}

        service = AutoTraderService(
            scanner_factory=lambda: calls.__setitem__("scanner", calls["scanner"] + 1),
            order_service_factory=lambda: calls.__setitem__(
                "orders", calls["orders"] + 1
            ),
            fast_scan_context_service=Context(),
        )
        service.running = True
        service.config = {
            "side": "BUY",
            "order_mode": "paper",
            "limit": 1,
            "place_orders": True,
        }
        event = {"direction": "bullish", "timestamp": datetime.now().isoformat()}
        first = service.request_fast_rescan(event)
        second = service.request_fast_rescan(event)
        time.sleep(0.1)
        self.assertTrue(first["scheduled"])
        self.assertFalse(second["scheduled"])
        self.assertEqual(calls, {"validate": 1, "scanner": 0, "orders": 0})
        status = service.status()["fast_rally"]
        self.assertEqual(status["request_count"], 2)
        self.assertEqual(status["validation_scheduled_count"], 1)
        self.assertEqual(status["validation_suppressed_count"], 1)
        self.assertEqual(
            status["suppressed_reasons"], {"fast_rescan_already_running": 1}
        )
        self.assertEqual(
            service.recent_decision_events()[0]["event_type"], "fast_rally_dispatch"
        )

    def test_fast_event_suppression_is_observable_when_auto_trader_is_stopped(self):
        service = AutoTraderService(lambda: object(), lambda: object())

        result = service.request_fast_rescan(
            {
                "direction": "bullish",
                "move_pct": 0.09,
                "timestamp": datetime.now().isoformat(),
            }
        )

        self.assertFalse(result["scheduled"])
        self.assertEqual(result["reason"], "auto_trader_not_running")
        status = service.status()["fast_rally"]
        self.assertEqual(status["validation_suppressed_count"], 1)
        self.assertEqual(status["last_dispatch"]["reason"], "auto_trader_not_running")
        self.assertEqual(
            service.recent_decision_events()[-1]["event_type"], "fast_rally_dispatch"
        )

    def test_fast_validation_rejects_any_database_io_and_records_decision(self):
        class Context:
            def validate_candidate(self, event):
                session = get_session()
                try:
                    session.query(Candle).count()
                finally:
                    session.close()
                return {"passed": True, "reason": "candidate_confirmed"}

        service = AutoTraderService(
            scanner_factory=lambda: object(),
            order_service_factory=lambda: object(),
            fast_scan_context_service=Context(),
        )
        service.running = True
        service.config = {
            "side": "BUY",
            "order_mode": "paper",
            "limit": 1,
            "place_orders": False,
        }

        result = service.request_fast_rescan(
            {
                "direction": "bullish",
                "move_pct": 0.09,
                "timestamp": datetime.now().isoformat(),
            }
        )
        deadline = time.time() + 1.0
        while (
            service.status()["fast_rally"]["validation_running"]
            and time.time() < deadline
        ):
            time.sleep(0.01)

        self.assertTrue(result["scheduled"])
        decision = service.last_fast_candidate_decision
        self.assertFalse(decision["passed"])
        self.assertEqual(decision["reason"], "fast_candidate_io_budget_exceeded")
        self.assertGreater(decision["io_calls"]["database_queries"], 0)
        self.assertEqual(service.status()["fast_rally"]["validation_rejected_count"], 1)

    def test_identical_rejections_are_one_row_until_gate_or_five_minute_candle_changes(
        self,
    ):
        repo = RejectedOpportunityRepository()
        contract = SimpleNamespace(
            tradingsymbol="BANKNIFTY_TEST_CE",
            exchange="NFO",
            expiry="2099-01-01",
            strike=58000,
            option_type="CE",
        )

        def save(marker, reason="premium_trigger_not_confirmed"):
            return repo.save_rejection(
                symbol="BANKNIFTY",
                side="BUY",
                action="BUY_CE",
                score=70,
                reasons=[reason],
                contract=contract,
                factor_scores={
                    "multi_timeframe": {
                        "frames": [
                            {"timeframe": "5minute", "last_completed_at": marker}
                        ]
                    }
                },
            )

        save("2026-07-21 10:00:00")
        save("2026-07-21 10:00:00")
        session = get_session()
        try:
            self.assertEqual(session.query(RejectedOpportunityRecord).count(), 1)
        finally:
            session.close()
        save("2026-07-21 10:05:00")
        save("2026-07-21 10:05:00", reason="spread_too_wide")
        session = get_session()
        try:
            self.assertEqual(session.query(RejectedOpportunityRecord).count(), 3)
        finally:
            session.close()

    def test_only_one_and_five_minute_frames_are_used_and_future_rows_do_not_change_decision(
        self,
    ):
        replay_at = datetime(2026, 7, 21, 11, 0)
        session = get_session()
        try:
            for timeframe, minutes in (
                ("1minute", 1),
                ("5minute", 5),
                ("15minute", 15),
                ("30minute", 30),
                ("day", 1440),
            ):
                for index in range(24):
                    close = 58000 + index * 10
                    session.add(
                        Candle(
                            symbol="BANKNIFTY",
                            timeframe=timeframe,
                            timestamp=replay_at
                            - timedelta(minutes=(24 - index) * minutes),
                            open_price=close - 2,
                            high_price=close + 4,
                            low_price=close - 4,
                            close_price=close,
                            volume=1000,
                        )
                    )
            session.commit()
        finally:
            session.close()

        service = MultiTimeframeContextService()
        before = service.evaluate(symbol="BANKNIFTY", trend="bullish", as_of=replay_at)
        session = get_session()
        try:
            for timeframe, minutes in (
                ("1minute", 1),
                ("5minute", 5),
                ("15minute", 15),
            ):
                session.add(
                    Candle(
                        symbol="BANKNIFTY",
                        timeframe=timeframe,
                        timestamp=replay_at + timedelta(minutes=minutes),
                        open_price=100,
                        high_price=100,
                        low_price=1,
                        close_price=1,
                        volume=999999,
                    )
                )
            session.commit()
        finally:
            session.close()
        after = service.evaluate(symbol="BANKNIFTY", trend="bullish", as_of=replay_at)
        self.assertEqual(before, after)
        self.assertEqual(
            set(before["responsibilities"]), {"1minute", "5minute", "tick"}
        )
        self.assertEqual(
            {frame["timeframe"] for frame in before["frames"]}, {"1minute", "5minute"}
        )
        with self.assertRaisesRegex(ValueError, "only 1minute and 5minute"):
            BacktestService().run(symbol="BANKNIFTY", timeframe="15minute")

    def test_multi_timeframe_direction_uses_latest_session_only(self):
        replay_at = datetime(2026, 7, 21, 10, 0)
        session = get_session()
        try:
            for timeframe, minutes in (("1minute", 1), ("5minute", 5)):
                for index in range(20):
                    close = 58400 - index * 10
                    session.add(
                        Candle(
                            symbol="BANKNIFTY",
                            timeframe=timeframe,
                            timestamp=datetime(2026, 7, 20, 14, 0)
                            + timedelta(minutes=index * minutes),
                            open_price=close + 2,
                            high_price=close + 4,
                            low_price=close - 4,
                            close_price=close,
                            volume=1000,
                        )
                    )
                for index in range(6):
                    close = 58000 + index * 25
                    session.add(
                        Candle(
                            symbol="BANKNIFTY",
                            timeframe=timeframe,
                            timestamp=datetime(2026, 7, 21, 9, 15)
                            + timedelta(minutes=index * minutes),
                            open_price=close - 2,
                            high_price=close + 4,
                            low_price=close - 4,
                            close_price=close,
                            volume=1000,
                        )
                    )
            session.commit()
        finally:
            session.close()

        result = MultiTimeframeContextService().evaluate(
            symbol="BANKNIFTY", trend="bullish", as_of=replay_at
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual({frame["samples"] for frame in result["frames"]}, {6})
        self.assertEqual(
            {frame["direction"] for frame in result["frames"]}, {"bullish"}
        )


if __name__ == "__main__":
    unittest.main()
