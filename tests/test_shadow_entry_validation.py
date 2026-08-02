import os
import tempfile
import time
import unittest
from dataclasses import fields
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.models import Signal
from app.services.account_equity_state_service import AccountEquityStateService
from app.services.database import (
    DecisionRiskEvidenceRecord,
    SetupEpisodeRecord,
    get_session,
    init_db,
)
from app.services.entry_policy_shadow_service import (
    ContinuousTransmissionShadowPolicy,
    CurrentBaselineEntryPolicy,
    EntryPolicyContext,
    ShadowEntryPolicyComparisonService,
    TransitionPreparationShadowPolicy,
)
from app.services.episode_outcome_collector import (
    EpisodeOutcomeCollector,
    MarketPathEvent,
)
from app.services.episode_reservation_service import EpisodeReservationService
from app.services.event_time_replay_service import EventTimeReplayService, ReplayEvent
from app.services.evidence_persistence_queue import EvidencePersistenceQueue
from app.services.order_service import OrderService
from app.services.policy_evaluation_service import (
    MissedOpportunityClassifier,
    PolicyEvaluationService,
    PolicyTimingWaterfallService,
    RiskTierCounterfactualSimulationService,
)
from app.services.pre_order_risk_service import PreOrderRiskService
from app.services.stop_exit_liquidity_service import (
    CURRENT_BOOK_PROXY,
    HISTORICAL_ADVERSE_MOVE_ESTIMATE,
    STRESS_EXIT_ESTIMATE,
    StopExitLiquidityResearchService,
)


class PassingRisk:
    def evaluate_signal(self, symbol):  # type: ignore[no-untyped-def]
        return {
            "passed": True,
            "reasons": [],
            "summary": {"trades": 0, "pnl": 0.0, "stop_losses": 0},
            "open_exposure": {
                "open_trades": 0,
                "by_symbol": {},
                "premium_exposure": 0.0,
            },
            "limits": {"available_cash": 100000.0, "current_audited_equity": 100000.0},
            "risk_state": {
                "current_audited_equity": 100000.0,
                "realized_daily_pnl": 0.0,
                "unrealized_daily_pnl": 0.0,
                "current_drawdown_pct": 0.0,
                "consecutive_losses": 0,
                "planned_risk_today": 0.0,
                "total_open_risk": 0.0,
                "banknifty_open_risk": 0.0,
            },
        }


class ShadowEntryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings_snapshot = {
            item.name: getattr(settings, item.name) for item in fields(type(settings))
        }
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        object.__setattr__(settings, "max_daily_planned_risk_percent", 10.0)
        object.__setattr__(settings, "max_realized_daily_loss_percent", 10.0)
        object.__setattr__(settings, "max_total_open_risk_percent", 10.0)
        object.__setattr__(settings, "max_banknifty_open_risk_percent", 10.0)

    def tearDown(self) -> None:
        for key, value in self.settings_snapshot.items():
            object.__setattr__(settings, key, value)
        try:
            os.remove(self.temp_db.name)
        except (PermissionError, FileNotFoundError):
            pass

    def _signal(self, *, token: int = 580001) -> Signal:
        return Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol=f"BANKNIFTY99DEC{token}CE",
            exchange="NFO",
            instrument_token=token,
            strike=58000,
            expiry="2099-12-31",
            entry_price=100,
            stop_loss=95,
            target_1=110,
            target_2=120,
            target_3=130,
            quantity=15,
            lot_size=15,
            score=99,
            factor_scores={
                "strategy_metadata": {"strategy_version": settings.strategy_version},
                "risk_request": {"requested_tier": "TIER_1_BASE"},
                "contract": {"instrument_token": token, "expiry": "2099-12-31"},
            },
        )

    def _context(self, **updates):  # type: ignore[no-untyped-def]
        values = {
            "episode_key": "episode-a",
            "direction": "bullish",
            "setup_family": "breakout",
            "observed_at": datetime(2026, 7, 20, 10, 0, 0),
            "underlying_trigger": 58010.0,
            "stop_loss": 95.0,
            "target_1": 110.0,
            "lot_size": 15,
            "minimum_depth": 15,
            "maximum_spread_pct": 2.0,
            "five_minute_structure": "bullish",
            "one_minute_structure": "bullish",
            "constituent_evidence_available": True,
            "contract_tradeable": True,
            "price_plan_valid": True,
            "base_risk_feasible": True,
            "provenance_valid": True,
            "data_fresh": True,
            "premium_confirmation_passed": True,
            "preparation_passed": True,
            "fast_candidate_requirements_passed": True,
            "promotion_registered": True,
            "armed_confirmation_passed": True,
            "chase_valid": True,
            "target_room_valid": True,
            "remaining_rr_valid": True,
            "session_eligible": True,
            "account_eligible": True,
            "metadata": {"underlying_token": 260105, "option_token": 580001},
        }
        values.update(updates)
        return EntryPolicyContext(**values)

    def _event(self, seconds: float, token: int, **values):  # type: ignore[no-untyped-def]
        timestamp = datetime(2026, 7, 20, 10, 0, 0) + timedelta(seconds=seconds)
        return MarketPathEvent(
            instrument_token=token,
            exchange_timestamp=timestamp,
            receive_timestamp=timestamp + timedelta(milliseconds=5),
            **values,
        )

    def test_continuous_outcome_uses_executable_ask_bid_costs_and_deduplicates(
        self,
    ) -> None:
        collector = EpisodeOutcomeCollector(
            start_worker=False, persist_observations=False
        )
        collector.register_episode(
            "episode-a",
            state="PREPARED",
            observed_at=datetime(2026, 7, 20, 10, 0, 0),
            context={
                "option_token": 580001,
                "underlying_token": 260105,
                "target_1": 110,
                "stop_loss": 95,
                "quantity": 15,
            },
        )
        entry = self._event(
            0, 580001, bid=99, ask=100, ltp=100, bid_depth=100, ask_depth=100
        )
        target = self._event(
            30, 580001, bid=111, ask=112, ltp=113, bid_depth=80, ask_depth=80
        )
        self.assertEqual(collector.process_event(entry), 1)
        self.assertEqual(collector.process_event(entry), 0)
        collector.process_event(target)
        outcome = collector.snapshot("episode-a")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertGreater(outcome["hypothetical_entry_after_cost"], 100)
        self.assertLess(outcome["latest_exit_after_cost"], 111)
        self.assertTrue(outcome["target_before_stop"])
        self.assertEqual(outcome["option_event_count"], 2)

    def test_insufficient_quote_coverage_and_ltp_only_markers_are_not_profit(
        self,
    ) -> None:
        object.__setattr__(settings, "outcome_min_executable_coverage_pct", 80.0)
        collector = EpisodeOutcomeCollector(
            start_worker=False, persist_observations=False
        )
        collector.register_episode(
            "episode-gap",
            state="REJECTED",
            observed_at=datetime(2026, 7, 20, 10, 0, 0),
            context={"option_token": 580001, "target_1": 110, "stop_loss": 95},
        )
        collector.process_event(self._event(0, 580001, ask=100, ltp=100))
        collector.process_event(self._event(1, 580001, ltp=111))
        collector.process_event(self._event(2, 580001, ltp=94))
        outcome = collector.snapshot("episode-gap")
        assert outcome is not None
        self.assertFalse(outcome["executable_data_sufficient"])
        self.assertEqual(outcome["classification"], "UNDETERMINABLE_DATA")
        self.assertTrue(outcome["ltp_target_without_executable_bid"])
        self.assertTrue(outcome["ltp_stop_without_executable_bid"])

    def test_outcome_persistence_retries_and_dead_letters(self) -> None:
        class FailingRepository:
            def record_outcome(self, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("write unavailable")

        object.__setattr__(settings, "outcome_queue_max_retries", 1)
        collector = EpisodeOutcomeCollector(
            repository=FailingRepository(),
            start_worker=False,
            persist_observations=True,
        )
        collector.register_episode(
            "episode-dead",
            state="OBSERVE",
            observed_at=datetime(2026, 7, 20, 10, 0, 0),
            context={"option_token": 580001, "target_1": 110, "stop_loss": 95},
        )
        collector.process_event(self._event(31, 580001, bid=100, ask=101, ltp=100))
        status = collector.status()
        self.assertGreaterEqual(status["persistence_retry_count"], 1)
        self.assertEqual(status["dead_letter_count"], 1)

    def test_background_evidence_queue_full_retry_recovery_and_deduplication(
        self,
    ) -> None:
        reservation = EpisodeReservationService().reserve_order_intent(
            self._signal(),
            authorized_quantity=15,
            order_mode="paper",
            risk_decision={"approved_tier": "TIER_1_BASE"},
            evidence_event_id="recover-event",
            metadata={"trigger_identifier": "recover"},
        )
        self.assertTrue(reservation["acquired"])
        queue = EvidencePersistenceQueue(max_size=1, start_worker=False)
        self.assertEqual(queue.recover_pending_order_evidence(), 1)
        rejected = queue.enqueue_decision(
            {"decision_type": "extra", "final_state": "REJECTED"}
        )
        self.assertFalse(rejected["accepted"])
        queue.start()
        self.assertTrue(queue.flush())
        queue.stop()
        session = get_session()
        try:
            self.assertEqual(
                session.query(DecisionRiskEvidenceRecord)
                .filter_by(decision_id="recover-event")
                .count(),
                1,
            )
            episode = (
                session.query(SetupEpisodeRecord)
                .filter_by(episode_key=reservation["episode_key"])
                .one()
            )
            self.assertEqual(episode.evidence_status, "PERSISTED")
        finally:
            session.close()

    def test_reservation_recovery_preserves_minimum_crash_safe_intent(self) -> None:
        service = EpisodeReservationService()
        reservation = service.reserve_order_intent(
            self._signal(),
            authorized_quantity=15,
            order_mode="live",
            risk_decision={
                "approved_risk_amount": 1000,
                "estimated_total_loss_at_stop": 90,
            },
            evidence_event_id="event-1",
            metadata={"trigger_identifier": "crash-window"},
        )
        recovered = service.recoverable_order_intents()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["state"], "ORDER_PENDING")
        self.assertEqual(recovered[0]["authorized_quantity"], 15)
        self.assertEqual(
            recovered[0]["order_intent"]["tradingsymbol"], self._signal().tradingsymbol
        )
        self.assertEqual(recovered[0]["evidence_event_id"], "event-1")
        self.assertTrue(reservation["reservation_token"])

    def test_broker_acceptance_then_local_failure_keeps_episode_locked_for_reconciliation(
        self,
    ) -> None:
        signal = self._signal()
        episode_service = EpisodeReservationService()
        reservation = episode_service.reserve_order_intent(
            signal,
            authorized_quantity=15,
            order_mode="live",
            risk_decision={"risk_per_unit": 6.0},
            evidence_event_id="live-event",
            metadata={"trigger_identifier": "live-crash"},
        )

        class ApprovedPreOrder:
            episode_reservation_service = episode_service

            def evaluate_and_reserve(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {
                    "passed": True,
                    "approved_quantity": 15,
                    "risk_decision": {"risk_per_unit": 6.0},
                    "shadow_risk_decisions": {},
                    "episode": reservation,
                }

        class Kite:
            def margins(self):
                return {"available": {"cash": 100000}}

            def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
                return {"order_id": "broker-accepted-1", "status": "submitted"}

        class FailingTrades:
            def create_trade(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("local trade persistence interrupted")

        order = OrderService(
            kite_provider=Kite(),
            trade_repository=FailingTrades(),
            pre_order_risk_service=ApprovedPreOrder(),
        )
        with self.assertRaisesRegex(RuntimeError, "local trade persistence"):
            order.place_signal_order(
                signal,
                confirm_live=True,
                order_mode="live",
                execution_quality_override={"passed": True, "reasons": []},
            )
        pending = episode_service.recoverable_order_intents()[0]
        self.assertEqual(pending["state"], "ORDER_PENDING")
        self.assertEqual(pending["broker_order_id"], "broker-accepted-1")
        self.assertEqual(pending["submission_status"], "SUBMITTED")

    def test_baseline_transition_and_continuous_policy_boundaries(self) -> None:
        baseline = CurrentBaselineEntryPolicy().evaluate(self._context(), [])
        self.assertTrue(baseline.enterable)
        active_confirmation_missing = CurrentBaselineEntryPolicy().evaluate(
            self._context(armed_confirmation_passed=False), []
        )
        self.assertFalse(active_confirmation_missing.enterable)
        self.assertIn("armed_second_confirmation", active_confirmation_missing.reasons)
        transition = TransitionPreparationShadowPolicy().evaluate(
            self._context(five_minute_structure="transitioning"), []
        )
        self.assertEqual(transition.state, "PREPARED")
        self.assertFalse(transition.enterable)
        opposed = TransitionPreparationShadowPolicy().evaluate(
            self._context(
                five_minute_structure="bearish", five_minute_strongly_opposed=True
            ),
            [],
        )
        self.assertEqual(opposed.state, "OBSERVE")

        events = [
            self._event(0, 580001, bid=100, ask=101, bid_depth=30, ask_depth=30),
            self._event(1, 260105, ltp=58011),
            self._event(1.1, 580001, bid=101, ask=102, bid_depth=30, ask_depth=30),
        ]
        continuous = ContinuousTransmissionShadowPolicy().evaluate(
            self._context(), events
        )
        self.assertTrue(continuous.enterable)
        self.assertTrue(continuous.features["prior_tick_evidence_preserved"])
        self.assertFalse(continuous.features["second_confirmation_reset_required"])

    def test_shadow_comparison_has_no_order_service_or_routing_authority(self) -> None:
        service = ShadowEntryPolicyComparisonService()
        self.assertNotIn("order", service.__dict__)
        result = service.compare(self._context(), [], persist=False)
        self.assertFalse(result["order_service_available"])
        for decision in result["decisions"].values():
            self.assertTrue(decision["shadow_only"])
            self.assertFalse(decision["can_invoke_order_service"])

    def test_event_time_replay_is_deterministic_and_hides_future_candles(self) -> None:
        replay = EventTimeReplayService()
        base = datetime(2026, 7, 20, 10, 0, 0)
        events = [
            ReplayEvent(
                "TICK",
                580001,
                base + timedelta(seconds=2),
                base + timedelta(seconds=2, milliseconds=5),
                3,
                bid=101,
                ask=102,
                ltp=101,
                bid_depth=30,
                ask_depth=30,
                instrument_master_version="im-v1",
                contract_available=True,
            ),
            ReplayEvent(
                "TICK",
                260105,
                base + timedelta(seconds=1),
                base + timedelta(seconds=1, milliseconds=5),
                2,
                ltp=58011,
                instrument_master_version="im-v1",
                contract_available=True,
            ),
            ReplayEvent(
                "TICK",
                580001,
                base,
                base + timedelta(milliseconds=5),
                1,
                bid=100,
                ask=101,
                ltp=100,
                bid_depth=30,
                ask_depth=30,
                instrument_master_version="im-v1",
                contract_available=True,
            ),
        ]
        first = replay.run(
            context=self._context(),
            events=events,
            data_version="ticks-v1",
            tick_capable=True,
            persist=False,
        )
        second = replay.run(
            context=self._context(),
            events=list(reversed(events)),
            data_version="ticks-v1",
            tick_capable=True,
            persist=False,
        )
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["event_order"], [1, 2, 3])

        candles = [
            ReplayEvent(
                "CANDLE",
                260105,
                base,
                base,
                1,
                candle_state="building",
                provenance="websocket",
                candle_complete_at=base + timedelta(minutes=1),
                instrument_master_version="im-v1",
            ),
            ReplayEvent(
                "CANDLE",
                260105,
                base,
                base + timedelta(minutes=1),
                2,
                candle_state="completed",
                provenance="generated",
                candle_complete_at=base + timedelta(minutes=1),
                instrument_master_version="im-v1",
            ),
            ReplayEvent(
                "CANDLE",
                260105,
                base + timedelta(minutes=1),
                base + timedelta(minutes=2),
                3,
                candle_state="completed",
                provenance="backfilled",
                candle_complete_at=base + timedelta(minutes=2),
                instrument_master_version="im-v1",
            ),
        ]
        visible = replay.visible_candle_events(
            candles, as_of=base + timedelta(minutes=1, seconds=1)
        )
        self.assertEqual([item.sequence for item in visible], [1, 2])
        bar_only = replay.run(
            context=self._context(),
            events=candles,
            data_version="bars-v1",
            tick_capable=False,
            persist=False,
        )
        self.assertIn(
            "continuous_option_transmission_shadow_v1", bar_only["disabled_policies"]
        )

    def test_replay_rejects_ambiguous_candles_and_unavailable_contract_ticks(
        self,
    ) -> None:
        replay = EventTimeReplayService()
        base = datetime(2026, 7, 20, 10, 0, 0)
        with self.assertRaisesRegex(ValueError, "completion timestamp"):
            replay.run(
                context=self._context(),
                events=[
                    ReplayEvent(
                        "CANDLE",
                        1,
                        base,
                        base,
                        1,
                        candle_state="completed",
                        instrument_master_version="im-v1",
                    )
                ],
                data_version="bad",
                tick_capable=False,
                persist=False,
            )
        with self.assertRaisesRegex(ValueError, "before the replay contract"):
            replay.run(
                context=self._context(),
                events=[
                    ReplayEvent(
                        "TICK",
                        1,
                        base,
                        base,
                        1,
                        instrument_master_version="im-v1",
                        contract_available=False,
                    )
                ],
                data_version="bad",
                tick_capable=True,
                persist=False,
            )

    def test_policy_timing_waterfall_and_missed_reason(self) -> None:
        base = datetime(2026, 7, 20, 10, 0, 0)
        observations = {
            stage: {
                "timestamp": base + timedelta(milliseconds=index * 100),
                "executable_ask": 100 + index,
            }
            for index, stage in enumerate(PolicyTimingWaterfallService.STAGES)
        }
        result = PolicyTimingWaterfallService().analyze(observations)
        self.assertTrue(result["complete"])
        self.assertEqual(result["stages"][-1]["ask_increase_from_first"], 10)
        reason = MissedOpportunityClassifier().classify(
            {
                "outcome": {"executable_data_sufficient": True},
                "gates": {"second_confirmation_delayed": True},
            }
        )
        self.assertEqual(reason, "FAST_PATH_DOUBLE_CONFIRMATION_DELAY")

    def test_audited_equity_uses_executable_bid_and_tracks_distinct_risks(self) -> None:
        base = datetime(2026, 7, 20, 10, 0, 0)
        open_trade = SimpleNamespace(
            id=1,
            remaining_quantity=15,
            filled_quantity=15,
            placed_quantity=15,
            requested_quantity=15,
            average_price=100,
            entry_price=100,
            stop_loss=95,
            estimated_loss_at_stop=90,
            approved_risk_amount=100,
            status="filled",
            updated_at=base,
            created_at=base,
        )
        closed_loss = SimpleNamespace(
            status="closed",
            net_pnl=-250,
            pnl=-250,
            updated_at=base,
            created_at=base,
            approved_risk_amount=100,
        )

        class Trades:
            def daily_summary(self):
                return {"pnl": -250, "trades": 1, "stop_losses": 1}

            def open_exposure_summary(self):
                return {"premium_exposure": 1500}

            def open_trades(self):
                return [open_trade]

            def today_trades(self):
                return [open_trade, closed_loss]

        class Funds:
            def available_cash(self):
                return 98500

        snapshot = AccountEquityStateService(
            trade_repository=Trades(),
            account_funds_service=Funds(),
            executable_bid_provider=lambda trade: 98,
        ).snapshot()
        expected_exit = 98 * (1 - settings.risk_allocated_exit_cost_pct / 100)
        self.assertAlmostEqual(
            snapshot.unrealized_pnl_executable_bid,
            (expected_exit - 100) * 15,
            delta=0.01,
        )
        self.assertEqual(snapshot.open_stop_risk, 90)
        self.assertEqual(snapshot.daily_planned_risk, 200)
        self.assertEqual(snapshot.daily_realized_loss, 250)
        self.assertEqual(snapshot.consecutive_losses, 1)

    def test_stop_exit_liquidity_estimates_are_named_and_do_not_change_active_proxy(
        self,
    ) -> None:
        samples = [
            {
                "executable_bid_slippage": 1 + index / 100,
                "bid_depth": 10 + index,
                "exit_time_seconds": 0.5 + index / 100,
                "spread": 2 + index / 100,
                "spread_expansion": index / 100,
                "bid_depth_disappeared": index % 5 == 0,
            }
            for index in range(100)
        ]
        result = StopExitLiquidityResearchService().estimate(
            proposed_quantity=60,
            current_bid=100,
            current_ask=102,
            current_bid_depth=30,
            historical_adverse_observations=samples,
        )
        self.assertEqual(
            set(result["estimates"]),
            {
                CURRENT_BOOK_PROXY,
                HISTORICAL_ADVERSE_MOVE_ESTIMATE,
                STRESS_EXIT_ESTIMATE,
            },
        )
        self.assertEqual(result["active_conservative_estimate"], CURRENT_BOOK_PROXY)
        self.assertFalse(result["active_behavior_changed"])
        self.assertFalse(result["estimates"][STRESS_EXIT_ESTIMATE]["guaranteed_fill"])

    def test_chronological_folds_purge_embargo_and_risk_simulation_whole_lots(
        self,
    ) -> None:
        base = datetime(2026, 1, 1, 10, 0, 0)
        episodes = []
        for index in range(30):
            timestamp = base + timedelta(days=index)
            episodes.append(
                {
                    "episode_key": f"e-{index}",
                    "policy_version": "p1",
                    "decision_at": timestamp,
                    "trading_date": timestamp.date().isoformat(),
                    "entered": True,
                    "risk_per_unit": 5,
                    "lot_size": 15,
                    "entry_price": 100,
                    "outcome": {
                        "executable_data_sufficient": True,
                        "after_cost_result_per_unit": 2 if index % 2 == 0 else -1,
                        "after_cost_result": 30 if index % 2 == 0 else -15,
                        "net_mfe_per_unit": 3,
                        "net_mae_per_unit": -2,
                    },
                }
            )
        folds = PolicyEvaluationService().chronological_folds(
            episodes, maximum_horizon_seconds=86400
        )
        self.assertTrue(
            folds["training"] and folds["validation"] and folds["out_of_sample"]
        )
        self.assertGreaterEqual(
            folds["validation"][0]["decision_at"]
            - folds["training"][-1]["decision_at"],
            timedelta(days=2),
        )
        simulations = RiskTierCounterfactualSimulationService().simulate(
            episodes, starting_equity=100000
        )
        self.assertEqual(
            set(simulations), {"1_percent", "2_percent", "3_percent", "5_percent"}
        )
        for result in simulations.values():
            self.assertFalse(result["active_policy_changed"])
            self.assertIn("sensitivity", result)
            self.assertGreater(result["trades"], 0)

    def test_final_pre_order_profile_keeps_append_only_evidence_off_critical_path(
        self,
    ) -> None:
        queue = EvidencePersistenceQueue(max_size=100, start_worker=False)
        service = PreOrderRiskService(
            risk_management_service=PassingRisk(), evidence_queue=queue
        )
        service._session_eligible = lambda: True  # type: ignore[method-assign]
        samples = []
        for index in range(40):
            signal = self._signal(token=580100 + index)
            started = time.perf_counter()
            result = service.evaluate_and_reserve(
                signal,
                order_mode="paper",
                live_requested=False,
                execution_quality={
                    "passed": True,
                    "reasons": [],
                    "best_ask": 100,
                    "best_bid": 99,
                    "entry_depth_quantity": 150,
                    "exit_depth_quantity": 150,
                },
                metadata={"trigger_identifier": f"profile-{index}"},
            )
            samples.append((time.perf_counter() - started) * 1000.0)
            self.assertTrue(result["passed"])
        p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
        # This in-suite smoke guard runs under database-heavy neighboring tests;
        # the dedicated 200-sample benchmark owns the strict 25 ms acceptance.
        self.assertLessEqual(
            p95, 50.0, f"safe final pre-order smoke p95 was {p95:.3f} ms"
        )
        self.assertEqual(queue.status()["persisted_count"], 0)


if __name__ == "__main__":
    unittest.main()
