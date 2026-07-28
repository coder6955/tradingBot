import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.config import settings
from app.services.armed_entry_tracker_service import ArmedEntryTrackerService
from app.services.database import init_db
from app.services.entry_timing_service import EntryTimingService
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_setup_service import OptionContract
from app.services.tick_replay_service import TickReplayService


class FakeWebSocketFeed:
    def __init__(self) -> None:
        self.subscriptions: list[set[int]] = []

    def subscribe(self, tokens):
        clean = {int(token) for token in tokens}
        self.subscriptions.append(clean)
        return {"subscribed": sorted(clean)}


class FailingWebSocketFeed(FakeWebSocketFeed):
    def subscribe(self, tokens):
        return {"subscribed": [], "reason": "subscription_failed", "message": "test failure"}


class FakeOrderService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def place_signal_order(self, signal, confirm_live=False, order_mode="paper", metadata=None, execution_quality_override=None, **kwargs):
        self.calls.append(
            {
                "signal": signal,
                "confirm_live": confirm_live,
                "order_mode": order_mode,
                "metadata": metadata,
                "execution_quality": execution_quality_override,
            }
        )
        return {"status": "paper", "trade_id": 101, "trade": {"metadata": metadata, "entry_price": signal.entry_price}}


class PassingRiskService:
    def evaluate_signal(self, symbol):
        return {"passed": True, "reasons": []}


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 3, 10, 30, 0)

    def __call__(self) -> datetime:
        return self.now


class InMemoryArmedEntryRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def upsert(self, payload):
        self.rows[str(payload["setup_id"])] = dict(payload)

    def active(self, *, now=None):
        return [dict(payload) for payload in self.rows.values() if payload["valid_until"] >= now]


class ArmedEntryTrackerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_event_driven_paper_entry": settings.enable_event_driven_paper_entry,
            "enable_event_driven_live_entry": settings.enable_event_driven_live_entry,
            "armed_entry_valid_seconds": settings.armed_entry_valid_seconds,
            "max_entry_chase_pct": settings.max_entry_chase_pct,
            "max_premium_move_from_base_pct": settings.max_premium_move_from_base_pct,
            "min_remaining_risk_reward": settings.min_remaining_risk_reward,
            "min_target1_room_pct": settings.min_target1_room_pct,
            "cancel_armed_entries_on_data_gap": settings.cancel_armed_entries_on_data_gap,
            "enable_tick_quality_confirmation": settings.enable_tick_quality_confirmation,
            "tick_quality_min_ticks_above_trigger": settings.tick_quality_min_ticks_above_trigger,
            "tick_quality_hold_seconds": settings.tick_quality_hold_seconds,
            "tick_quality_require_bid_progress": settings.tick_quality_require_bid_progress,
            "tick_quality_max_spread_multiplier": settings.tick_quality_max_spread_multiplier,
        }
        object.__setattr__(settings, "enable_event_driven_paper_entry", True)
        object.__setattr__(settings, "enable_event_driven_live_entry", False)
        object.__setattr__(settings, "armed_entry_valid_seconds", 60)
        object.__setattr__(settings, "max_entry_chase_pct", 1.0)
        object.__setattr__(settings, "max_premium_move_from_base_pct", 4.0)
        object.__setattr__(settings, "min_remaining_risk_reward", 1.3)
        object.__setattr__(settings, "min_target1_room_pct", 8.0)
        object.__setattr__(settings, "cancel_armed_entries_on_data_gap", True)
        object.__setattr__(settings, "enable_tick_quality_confirmation", True)
        object.__setattr__(settings, "tick_quality_min_ticks_above_trigger", 2)
        object.__setattr__(settings, "tick_quality_hold_seconds", 1.0)
        object.__setattr__(settings, "tick_quality_require_bid_progress", True)
        object.__setattr__(settings, "tick_quality_max_spread_multiplier", 1.5)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.clock = MutableClock()
        self.websocket = FakeWebSocketFeed()
        self.order_service = FakeOrderService()
        self.repo = RejectedOpportunityRepository()
        self.tracker = ArmedEntryTrackerService(
            order_service_factory=lambda: self.order_service,
            websocket_price_feed=self.websocket,
            rejected_opportunity_repository=self.repo,
            risk_management_service=PassingRiskService(),
            market_session_provider=lambda: "REGULAR_MARKET",
            clock=self.clock,
        )

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _contract(self) -> OptionContract:
        return OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            103,
            100000,
            50000,
            102,
            103,
        )

    def _register(self, *, order_mode: str = "paper", prices: dict[str, float] | None = None) -> str:
        result = self.tracker.register_from_scan(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            contract=self._contract(),
            prices=prices or {"entry_price": 103, "stop_loss": 90, "target_1": 130, "target_2": 145, "target_3": 160, "risk_reward": 2.0},
            entry_timing={
                "entry_timing_state": EntryTimingService.ARMED_FOR_ENTRY,
                "entry_trigger_price": 105,
                "current_premium": 103,
                "spread_pct": 1.0,
                "reasons": ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"],
            },
            score=86,
            probability=0.78,
            confidence=0.86,
            quantity=15,
            factor_scores={
                "score_breakdown": {"score": 86},
                "strategy_metadata": {
                    "strategy_name": settings.strategy_name,
                    "strategy_version": settings.strategy_version,
                },
            },
            order_mode=order_mode,
        )
        self.assertTrue(result["registered"])
        return str(result["setup_id"])

    def _tick(self, price: float, *, bid: float | None = 104, ask: float | None = None) -> WebSocketTick:
        return WebSocketTick(
            instrument_token=580001,
            price=price,
            bid=bid,
            ask=ask if ask is not None else price,
            timestamp=self.clock(),
            timestamp_source="exchange_timestamp",
        )

    def test_scanner_arming_subscribes_selected_option_token(self) -> None:
        setup_id = self._register()

        self.assertTrue(setup_id.startswith("armed-"))
        self.assertIn({580001}, self.websocket.subscriptions)
        self.assertEqual(self.tracker.active_tokens(), {580001})

    def test_subscription_failure_is_not_reported_as_armed(self) -> None:
        tracker = ArmedEntryTrackerService(
            websocket_price_feed=FailingWebSocketFeed(),
            risk_management_service=PassingRiskService(),
            market_session_provider=lambda: "REGULAR_MARKET",
            clock=self.clock,
        )
        result = tracker.register_from_scan(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            contract=self._contract(),
            prices={"entry_price": 103, "stop_loss": 90, "target_1": 130, "target_2": 145, "target_3": 160, "risk_reward": 2.0},
            entry_timing={"entry_trigger_price": 105, "current_premium": 103, "spread_pct": 1.0},
            score=86,
            probability=None,
            confidence=0.86,
            quantity=15,
            factor_scores={},
            order_mode="paper",
        )

        self.assertFalse(result["registered"])
        self.assertFalse(result["websocket_tracking_enabled"])
        self.assertEqual(result["subscription_health"]["state"], "failed")
        self.assertEqual(tracker.active_tokens(), set())

    def test_armed_entry_is_persisted_and_recovered_with_subscription_owner(self) -> None:
        durable = InMemoryArmedEntryRepository()
        first = ArmedEntryTrackerService(
            websocket_price_feed=self.websocket,
            armed_entry_repository=durable,
            risk_management_service=PassingRiskService(),
            market_session_provider=lambda: "REGULAR_MARKET",
            clock=self.clock,
        )
        result = first.register_from_scan(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            contract=self._contract(),
            prices={"entry_price": 103, "stop_loss": 90, "target_1": 130, "target_2": 145, "target_3": 160, "risk_reward": 2.0},
            entry_timing={"entry_trigger_price": 105, "current_premium": 103, "spread_pct": 1.0, "reasons": ["waiting_for_entry_trigger"]},
            score=86,
            probability=None,
            confidence=0.86,
            quantity=15,
            factor_scores={"market_regime": {"regime": "trend_expansion"}},
            order_mode="paper",
        )
        self.assertTrue(result["registered"])
        second_websocket = FakeWebSocketFeed()
        second = ArmedEntryTrackerService(
            websocket_price_feed=second_websocket,
            armed_entry_repository=durable,
            risk_management_service=PassingRiskService(),
            market_session_provider=lambda: "REGULAR_MARKET",
            clock=self.clock,
        )

        recovery = second.recover_active()

        self.assertEqual(recovery["recovered"], 1)
        self.assertEqual(second.active_tokens(), {580001})
        self.assertIn({580001}, second_websocket.subscriptions)

    def test_websocket_tick_below_trigger_does_not_enter(self) -> None:
        setup_id = self._register()

        result = self.tracker.evaluate_tick(setup_id, self._tick(104, ask=104))

        self.assertEqual(result["latest_state"], EntryTimingService.ARMED_FOR_ENTRY)
        self.assertEqual(len(self.order_service.calls), 0)

    def test_websocket_tick_crossing_trigger_waits_for_tick_quality_then_creates_paper_order(self) -> None:
        setup_id = self._register()

        first = self.tracker.evaluate_tick(setup_id, self._tick(105, bid=105.0, ask=105.1))
        self.clock.now = self.clock.now + timedelta(seconds=1.1)
        result = self.tracker.evaluate_tick(setup_id, self._tick(105.2, bid=105.1, ask=105.2))

        self.assertEqual(first["latest_state"], EntryTimingService.ARMED_FOR_ENTRY)
        self.assertIn("tick_quality", first)
        self.assertFalse(first["tick_quality"]["passed"])
        self.assertEqual(result["latest_state"], "ENTERED_PAPER")
        self.assertEqual(len(self.order_service.calls), 1)
        call = self.order_service.calls[0]
        self.assertFalse(call["confirm_live"])
        self.assertEqual(call["order_mode"], "paper")
        self.assertEqual(call["metadata"]["entry_source"], "event_driven_websocket")
        self.assertEqual(call["metadata"]["armed_setup_id"], setup_id)
        self.assertTrue(call["metadata"]["tick_quality"]["confirmed"])
        self.assertEqual(call["signal"].entry_price, 105.2)
        self.assertTrue(call["signal"].factor_scores["decision_policy"]["primary_gates_passed"])
        self.assertTrue(call["signal"].factor_scores["decision_policy"]["event_confirmation_passed"])
        self.assertEqual(call["signal"].factor_scores["decision_policy"]["score_role"], "ranking_only")

    def test_deterministic_fast_rally_replay_enters_after_dense_tick_confirmation(self) -> None:
        setup_id = self._register()
        start = self.clock.now

        def handle(tick: WebSocketTick):
            self.clock.now = tick.timestamp
            return self.tracker.evaluate_tick(setup_id, tick)

        replay = TickReplayService(handle).replay(
            [
                {"instrument_token": 580001, "price": 105.00, "bid": 105.00, "ask": 105.10, "timestamp": start},
                {"instrument_token": 580001, "price": 105.10, "bid": 105.05, "ask": 105.15, "timestamp": start + timedelta(seconds=0.10)},
                {"instrument_token": 580001, "price": 105.20, "bid": 105.10, "ask": 105.20, "timestamp": start + timedelta(seconds=0.20)},
                {"instrument_token": 580001, "price": 105.30, "bid": 105.20, "ask": 105.30, "timestamp": start + timedelta(seconds=0.30)},
            ]
        )

        self.assertEqual(replay["ticks_replayed"], 4)
        self.assertEqual(self.tracker.list_entries()["entered_paper"][0]["setup_id"], setup_id)
        self.assertEqual(len(self.order_service.calls), 1)

    def test_tick_quality_requires_bid_progress_before_entry(self) -> None:
        setup_id = self._register()

        self.tracker.evaluate_tick(setup_id, self._tick(105.2, bid=105.2, ask=105.3))
        self.clock.now = self.clock.now + timedelta(seconds=1.1)
        waiting = self.tracker.evaluate_tick(setup_id, self._tick(105.1, bid=105.1, ask=105.2))
        self.clock.now = self.clock.now + timedelta(seconds=0.2)
        entered = self.tracker.evaluate_tick(setup_id, self._tick(105.3, bid=105.3, ask=105.4))

        self.assertEqual(waiting["latest_state"], EntryTimingService.ARMED_FOR_ENTRY)
        self.assertIn("tick_quality_bid_not_rising", waiting["latest_reason"])
        self.assertEqual(len(self.order_service.calls), 1)
        self.assertEqual(entered["latest_state"], "ENTERED_PAPER")

    def test_live_mode_does_not_place_event_driven_order(self) -> None:
        setup_id = self._register(order_mode="live")

        self.tracker.evaluate_tick(setup_id, self._tick(105, bid=105.0, ask=105.1))
        self.clock.now = self.clock.now + timedelta(seconds=1.1)
        result = self.tracker.evaluate_tick(setup_id, self._tick(105.2, bid=105.1, ask=105.2))

        self.assertTrue(result["live_event_entry_blocked"])
        self.assertEqual(result["reason"], "live_trading_not_enabled_for_event_entry")
        self.assertEqual(len(self.order_service.calls), 0)

    def test_premium_far_above_trigger_marks_too_late_and_saves_rejection(self) -> None:
        setup_id = self._register()

        result = self.tracker.evaluate_tick(setup_id, self._tick(108, bid=107.5, ask=108))

        self.assertEqual(result["latest_state"], "TOO_LATE")
        self.assertIn("chase_risk_high", result["latest_reason"])
        self.assertEqual(self.repo.analyze(symbol="BANKNIFTY")["sample"]["total_rejected"], 1)

    def test_data_gap_cancels_armed_setup_before_entry(self) -> None:
        setup_id = self._register()

        result = self.tracker.cancel_for_data_gap({"reason": "tick_gap_detected"})
        after_tick = self.tracker.evaluate_tick(setup_id, self._tick(105, ask=105))
        analysis = self.repo.analyze(symbol="BANKNIFTY")

        self.assertEqual(result["cancelled"], 1)
        self.assertIsNone(after_tick)
        self.assertEqual(analysis["sample"]["total_rejected"], 1)
        self.assertIn("data_gap_detected", analysis["top_reasons"])

    def test_non_blocking_or_recovered_gap_does_not_cancel_setup(self) -> None:
        setup_id = self._register()

        quiet = self.tracker.cancel_for_data_gap(
            {"reason": "token_tick_inactivity_detected", "instrument_token": 580001, "entry_blocking": False}
        )
        recovered = self.tracker.cancel_for_data_gap({"reason": "websocket_reconnect", "recovered": True})

        self.assertEqual(quiet["cancelled"], 0)
        self.assertEqual(recovered["cancelled"], 0)
        self.assertIn(setup_id, {row["setup_id"] for row in self.tracker.list_entries()["active"]})

    def test_token_scoped_gap_only_cancels_matching_setup(self) -> None:
        setup_id = self._register()

        result = self.tracker.cancel_for_data_gap(
            {"reason": "selected_option_feed_unavailable", "instrument_token": 999999, "entry_blocking": True}
        )

        self.assertEqual(result["cancelled"], 0)
        self.assertIn(setup_id, {row["setup_id"] for row in self.tracker.list_entries()["active"]})

    def test_setup_expiry_marks_expired(self) -> None:
        setup_id = self._register()
        self.clock.now = self.clock.now + timedelta(seconds=90)

        result = self.tracker.evaluate_tick(setup_id, self._tick(105, ask=105))

        self.assertEqual(result["latest_state"], "EXPIRED")
        self.assertIn("armed_setup_expired", result["latest_reason"])

    def test_spread_widening_blocks_entry(self) -> None:
        setup_id = self._register()

        result = self.tracker.evaluate_tick(setup_id, self._tick(105, bid=90, ask=105))

        self.assertEqual(result["latest_state"], "TOO_LATE")
        self.assertIn("spread_widened_after_trigger", result["latest_reason"])

    def test_remaining_rr_compression_blocks_entry(self) -> None:
        setup_id = self._register(prices={"entry_price": 103, "stop_loss": 50, "target_1": 120, "target_2": 135, "target_3": 150, "risk_reward": 0.5})

        result = self.tracker.evaluate_tick(setup_id, self._tick(105, ask=105))

        self.assertEqual(result["latest_state"], "TOO_LATE")
        self.assertIn("remaining_rr_compressed", result["latest_reason"])

    def test_target1_room_too_small_blocks_entry(self) -> None:
        setup_id = self._register(prices={"entry_price": 103, "stop_loss": 100, "target_1": 110, "target_2": 120, "target_3": 130, "risk_reward": 1.5})

        result = self.tracker.evaluate_tick(setup_id, self._tick(105, ask=105))

        self.assertEqual(result["latest_state"], "TOO_LATE")
        self.assertIn("insufficient_target_room_after_entry", result["latest_reason"])


if __name__ == "__main__":
    unittest.main()
