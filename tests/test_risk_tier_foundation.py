import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import fields
from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.config import settings
from app.models import Signal
from app.providers.kite_feed import KiteFeed
from app.services.database import (
    Candle,
    DecisionRiskEvidenceRecord,
    SetupEpisodeRecord,
    get_session,
    init_db,
)
from app.services.decision_evidence_repository import DecisionEvidenceRepository
from app.services.episode_reservation_service import EpisodeReservationService
from app.services.option_premium_confirmation_service import (
    OptionPremiumConfirmationService,
)
from app.services.price_action_service import PriceActionService
from app.services.pre_order_risk_service import PreOrderRiskService
from app.services.risk_policy_service import (
    RISK_BUDGET_BELOW_MINIMUM_LOT,
    RISK_EXCEEDS_ABSOLUTE_MAXIMUM,
    RISK_EXCEEDS_DAILY_CAP,
    RISK_EXCEEDS_OPEN_RISK_CAP,
    RISK_TIER_DOWNGRADED,
    RiskDecisionContext,
    RiskPolicyService,
    TIER_1_BASE,
    TIER_2_STRONG,
    TIER_3_HIGH,
    TIER_4_EXCEPTIONAL,
)
from app.services.trade_setup_service import OptionContract
from app.services.time_utils import ist_now_naive


class PassingAccountRisk:
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
            "limits": {"available_cash": 100000.0},
            "risk_state": {
                "realized_daily_pnl": 0.0,
                "unrealized_daily_pnl": 0.0,
                "current_drawdown_pct": 0.0,
                "consecutive_losses": 0,
                "planned_risk_today": 0.0,
                "total_open_risk": 0.0,
                "banknifty_open_risk": 0.0,
            },
        }


class PremiumFeed:
    def __init__(self, candles):  # type: ignore[no-untyped-def]
        self.candles = candles

    def get_current_session_premium_candles(self, token, limit=10):  # type: ignore[no-untyped-def]
        return self.candles[-limit:]

    def premium_candle_status(self, token):  # type: ignore[no-untyped-def]
        return {
            "subscribed": True,
            "ticks_seen": 20,
            "current_session_candle_count": len(self.candles),
        }


class RiskTierFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings_snapshot = {
            item.name: getattr(settings, item.name) for item in fields(type(settings))
        }
        for key, value in {
            "active_paper_max_risk_tier": TIER_4_EXCEPTIONAL,
            "active_live_max_risk_tier": TIER_1_BASE,
            "shadow_max_risk_tier": TIER_4_EXCEPTIONAL,
            "max_daily_planned_risk_percent": 10.0,
            "max_realized_daily_loss_percent": 10.0,
            "max_total_open_risk_percent": 10.0,
            "max_banknifty_open_risk_percent": 10.0,
            "max_risk_per_trade_percent": 5.0,
            "absolute_max_risk_per_trade_percent": 5.0,
            "enable_validated_higher_risk_active": True,
        }.items():
            object.__setattr__(settings, key, value)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        for key, value in self.settings_snapshot.items():
            object.__setattr__(settings, key, value)
        try:
            os.remove(self.temp_db.name)
        except (PermissionError, FileNotFoundError):
            pass

    def _evidence(self, tier: str) -> dict[str, object]:
        return {
            "validated": True,
            "validation_source": "strategy_validation_repository",
            "independent_chronological_oos": True,
            "after_cost_expectancy_pct": 0.25,
            "out_of_sample_trades": 400,
            "out_of_sample_sessions": 60,
            "fold_count": 6,
            "profit_factor": 1.6,
            "max_drawdown_pct": 8.0,
            "stable_across_folds": True,
            "stable_across_regimes": True,
            "acceptable_mae_and_loss_streak": True,
            "current_distribution_match": True,
            "unvalidated_event_or_recovery": False,
            "policy_permitted_tiers": [
                tier,
                TIER_2_STRONG,
                TIER_3_HIGH,
                TIER_4_EXCEPTIONAL,
            ],
            "evidence_version": "oos-test-v1",
        }

    def _context(self, tier: str, **overrides):  # type: ignore[no-untyped-def]
        payload = {
            "account_equity": 100000.0,
            "expected_entry": 100.0,
            "stop_price": 95.0,
            "lot_size": 15,
            "requested_tier": tier,
            "order_mode": "paper",
            "policy_mode": "active",
            "setup_quality_evidence": self._evidence(tier),
        }
        payload.update(overrides)
        return RiskDecisionContext(**payload)

    def _signal(self) -> Signal:
        return Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY99DEC58000CE",
            exchange="NFO",
            instrument_token=580001,
            strike=58000,
            expiry="2099-12-31",
            entry_price=100,
            stop_loss=95,
            target_1=120,
            quantity=150,
            lot_size=15,
            score=99,
            factor_scores={
                "strategy_metadata": {"strategy_version": settings.strategy_version},
                "risk_request": {"requested_tier": TIER_1_BASE},
                "contract": {"instrument_token": 580001, "expiry": "2099-12-31"},
            },
        )

    def test_base_strong_high_and_exceptional_tier_sizing(self) -> None:
        service = RiskPolicyService()
        decisions = [service.evaluate(self._context(tier)) for tier in service.TIERS]
        self.assertEqual(
            [row.approved_risk_percent for row in decisions], [1.0, 2.0, 3.0, 5.0]
        )
        self.assertEqual([row.approved_tier for row in decisions], list(service.TIERS))
        self.assertTrue(
            all(
                row.estimated_total_loss_at_stop <= row.approved_risk_amount
                for row in decisions
            )
        )
        self.assertEqual(
            sorted(row.maximum_quantity for row in decisions),
            [row.maximum_quantity for row in decisions],
        )

    def test_configuration_above_five_percent_is_rejected(self) -> None:
        object.__setattr__(settings, "absolute_max_risk_per_trade_percent", 5.1)
        result = RiskPolicyService().evaluate(self._context(TIER_1_BASE))
        self.assertFalse(result.passed)
        self.assertIn(RISK_EXCEEDS_ABSOLUTE_MAXIMUM, result.rejection_reasons)

    def test_daily_loss_and_per_trade_configuration_conflict_is_reported(self) -> None:
        object.__setattr__(settings, "max_risk_per_trade_percent", 5.0)
        object.__setattr__(settings, "max_realized_daily_loss_percent", 2.0)
        object.__setattr__(settings, "max_daily_planned_risk_percent", 2.0)
        service = RiskPolicyService()
        conflicts = service.configuration_conflicts()
        self.assertIn(
            "POLICY_CONFLICT_PER_TRADE_MAX_EXCEEDS_REALIZED_DAILY_LOSS_CAP", conflicts
        )
        self.assertIn(
            "POLICY_CONFLICT_PER_TRADE_MAX_EXCEEDS_DAILY_PLANNED_RISK_CAP", conflicts
        )
        result = service.evaluate(self._context(TIER_4_EXCEPTIONAL))
        self.assertFalse(result.passed)
        self.assertIn(RISK_EXCEEDS_DAILY_CAP, result.rejection_reasons)

    def test_one_lot_not_fitting_returns_zero(self) -> None:
        result = RiskPolicyService().evaluate(
            self._context(TIER_1_BASE, account_equity=1000.0)
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.maximum_quantity, 0)
        self.assertIn(RISK_BUDGET_BELOW_MINIMUM_LOT, result.rejection_reasons)

    def test_slippage_can_make_one_lot_infeasible(self) -> None:
        result = RiskPolicyService().evaluate(
            self._context(
                TIER_1_BASE,
                account_equity=10000.0,
                expected_entry_slippage=4.0,
                expected_exit_slippage=4.0,
            )
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.maximum_quantity, 0)

    def test_unvalidated_higher_tier_is_downgraded_to_base(self) -> None:
        result = RiskPolicyService().evaluate(
            self._context(TIER_4_EXCEPTIONAL, setup_quality_evidence={})
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.approved_tier, TIER_1_BASE)
        self.assertIn(RISK_TIER_DOWNGRADED, result.downgrade_reasons)

    def test_drawdown_and_loss_streak_disable_higher_tiers(self) -> None:
        drawdown = RiskPolicyService().evaluate(
            self._context(TIER_3_HIGH, current_drawdown_pct=6.0)
        )
        loss = RiskPolicyService().evaluate(
            self._context(TIER_3_HIGH, consecutive_losses=1)
        )
        unrealized = RiskPolicyService().evaluate(
            self._context(TIER_4_EXCEPTIONAL, unrealized_daily_pnl=-6000.0)
        )
        self.assertEqual(drawdown.approved_tier, TIER_1_BASE)
        self.assertEqual(loss.approved_tier, TIER_1_BASE)
        self.assertEqual(unrealized.approved_tier, TIER_1_BASE)

    def test_daily_and_open_risk_caps_reject(self) -> None:
        object.__setattr__(settings, "max_daily_planned_risk_percent", 2.0)
        daily = RiskPolicyService().evaluate(self._context(TIER_3_HIGH))
        self.assertIn(RISK_EXCEEDS_DAILY_CAP, daily.rejection_reasons)
        object.__setattr__(settings, "max_daily_planned_risk_percent", 10.0)
        object.__setattr__(settings, "max_total_open_risk_percent", 2.0)
        opened = RiskPolicyService().evaluate(
            self._context(TIER_2_STRONG, total_open_risk=1000.0)
        )
        self.assertIn(RISK_EXCEEDS_OPEN_RISK_CAP, opened.rejection_reasons)

    def test_paper_and_live_use_identical_active_risk_math(self) -> None:
        service = RiskPolicyService()
        paper = service.evaluate(self._context(TIER_1_BASE, order_mode="paper"))
        live = service.evaluate(self._context(TIER_1_BASE, order_mode="live"))
        self.assertEqual(paper.approved_risk_amount, live.approved_risk_amount)
        self.assertEqual(paper.maximum_quantity, live.maximum_quantity)

    def test_episode_reservation_prevents_duplicate_and_validates_transitions(
        self,
    ) -> None:
        service = EpisodeReservationService()
        signal = self._signal()
        first = service.reserve(signal, metadata={"trigger_identifier": "episode-a"})
        second = service.reserve(signal, metadata={"trigger_identifier": "episode-a"})
        self.assertTrue(first["acquired"])
        self.assertFalse(second["acquired"])
        self.assertTrue(
            service.mark_order_pending(first["episode_key"], first["reservation_token"])
        )
        self.assertTrue(
            service.mark_open(
                first["episode_key"], first["reservation_token"], trade_id=1
            )
        )
        self.assertFalse(service.validate_transition("OPEN", "RESERVED"))
        self.assertTrue(service.close(first["episode_key"]))

    def test_concurrent_duplicate_attempts_have_one_winner(self) -> None:
        service = EpisodeReservationService()
        barrier = threading.Barrier(2)
        results: list[dict[str, object]] = []

        def reserve() -> None:
            barrier.wait()
            results.append(
                service.reserve(
                    self._signal(), metadata={"trigger_identifier": "concurrent"}
                )
            )

        threads = [threading.Thread(target=reserve), threading.Thread(target=reserve)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for row in results if row.get("acquired")), 1)

    def test_concurrent_expired_reservation_reclaim_has_one_winner(self) -> None:
        service = EpisodeReservationService()
        signal = self._signal()
        first = service.reserve(
            signal, metadata={"trigger_identifier": "expired-concurrent"}
        )
        session = get_session()
        try:
            record = (
                session.query(SetupEpisodeRecord)
                .filter(SetupEpisodeRecord.episode_key == first["episode_key"])
                .one()
            )
            record.reservation_expires_at = ist_now_naive() - timedelta(seconds=1)
            session.commit()
        finally:
            session.close()

        barrier = threading.Barrier(6)
        results: list[dict[str, object]] = []

        def reclaim() -> None:
            barrier.wait()
            results.append(
                service.reserve(
                    signal, metadata={"trigger_identifier": "expired-concurrent"}
                )
            )

        threads = [threading.Thread(target=reclaim) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for row in results if row.get("acquired")), 1)

    def test_pre_order_persists_active_and_shadow_without_shadow_quantity_authority(
        self,
    ) -> None:
        service = PreOrderRiskService(risk_management_service=PassingAccountRisk())
        result = service.evaluate_and_reserve(
            self._signal(),
            order_mode="paper",
            live_requested=False,
            execution_quality={
                "passed": True,
                "reasons": [],
                "details": {
                    "ask": 100,
                    "bid": 99.5,
                    "spread_pct": 0.5,
                    "bid_depth_quantity": 1000,
                    "ask_depth_quantity": 1000,
                },
            },
            metadata={"trigger_identifier": "pre-order-evidence"},
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["risk_decision"]["approved_tier"], TIER_1_BASE)
        self.assertGreater(
            result["shadow_risk_decisions"][TIER_4_EXCEPTIONAL][
                "counterfactual_maximum_quantity"
            ],
            result["approved_quantity"],
        )
        self.assertFalse(
            result["shadow_risk_decisions"][TIER_4_EXCEPTIONAL][
                "counterfactual_can_reach_order_router"
            ]
        )
        self.assertLessEqual(
            result["approved_quantity"], result["risk_decision"]["maximum_quantity"]
        )
        self.assertIsNotNone(
            DecisionEvidenceRepository().get_decision(result["evidence"]["decision_id"])
        )

    def test_decision_evidence_is_append_only(self) -> None:
        repo = DecisionEvidenceRepository()
        first = repo.record_decision(
            decision_type="test", final_state="OBSERVE", context={"value": 1}
        )
        second = repo.record_decision(
            decision_type="test", final_state="PREPARED", context={"value": 2}
        )
        session = get_session()
        try:
            self.assertEqual(session.query(DecisionRiskEvidenceRecord).count(), 2)
            self.assertNotEqual(first["decision_id"], second["decision_id"])
        finally:
            session.close()

    def test_counterfactual_tiers_are_hypothetical_and_episode_outcomes_deduplicate(
        self,
    ) -> None:
        policy = RiskPolicyService()
        context = self._context(TIER_1_BASE)
        shadow = policy.shadow_tier_evaluations(context)
        repository = DecisionEvidenceRepository()
        result = repository.counterfactual_tier_outcomes(
            entry_price=100.0,
            exit_price=108.0,
            account_equity=context.account_equity,
            shadow_decisions=shadow,
            charges_per_unit=0.5,
        )
        self.assertEqual(set(result["tiers"]), set(policy.TIERS))
        self.assertTrue(
            all(item["hypothetical_only"] for item in result["tiers"].values())
        )
        self.assertIsNone(result["risk_of_ruin"])
        first = repository.record_outcome(
            episode_key="episode-counterfactual",
            horizon="5_minutes",
            outcome_source="unit_test",
            outcome=result,
        )
        second = repository.record_outcome(
            episode_key="episode-counterfactual",
            horizon="5_minutes",
            outcome_source="unit_test",
            outcome=result,
        )
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["id"], second["id"])

    def test_canonical_lifecycle_mapping_and_transition_rules(self) -> None:
        service = EpisodeReservationService()
        self.assertEqual(service.canonical_state(service.AVAILABLE), "PREPARED")
        self.assertEqual(service.canonical_state(service.RESERVED), "TRIGGERED")
        self.assertTrue(service.validate_canonical_transition("ARMED", "TRIGGERED"))
        self.assertTrue(service.validate_canonical_transition("OPEN", "EXITING"))
        self.assertFalse(service.validate_canonical_transition("CLOSED", "OPEN"))

    def test_schema_contains_episode_and_immutable_evidence_tables(self) -> None:
        session = get_session()
        try:
            schema = inspect(session.get_bind())
            self.assertIn("setup_episodes", schema.get_table_names())
            self.assertIn("decision_risk_evidence", schema.get_table_names())
            self.assertIn("decision_outcomes", schema.get_table_names())
            episode_columns = {
                item["name"] for item in schema.get_columns("setup_episodes")
            }
            self.assertTrue(
                {"state", "reservation_token", "risk_policy_version"}.issubset(
                    episode_columns
                )
            )
        finally:
            session.close()

    def test_legacy_episode_schema_receives_reservation_columns(self) -> None:
        legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        legacy.close()
        connection = sqlite3.connect(legacy.name)
        try:
            connection.execute(
                "CREATE TABLE setup_episodes (id INTEGER PRIMARY KEY, created_at DATETIME, "
                "updated_at DATETIME, episode_key VARCHAR(64), symbol VARCHAR(50), action VARCHAR(20), "
                "side VARCHAR(10), tradingsymbol VARCHAR(100), expiry VARCHAR(30), strike REAL, trigger_price REAL, "
                "strategy_version VARCHAR(100), config_hash VARCHAR(64))"
            )
            connection.execute(
                "CREATE TABLE decision_outcomes (id INTEGER PRIMARY KEY, episode_key VARCHAR(64), "
                "horizon VARCHAR(30), outcome_source VARCHAR(50))"
            )
            connection.commit()
        finally:
            connection.close()
        try:
            init_db(f"sqlite:///{legacy.name}")
            session = get_session()
            try:
                columns = {
                    item["name"]
                    for item in inspect(session.get_bind()).get_columns(
                        "setup_episodes"
                    )
                }
                self.assertTrue(
                    {
                        "state",
                        "reservation_token",
                        "reservation_expires_at",
                        "risk_policy_version",
                        "order_intent_json",
                        "submission_status",
                        "broker_order_id",
                        "evidence_status",
                    }.issubset(columns)
                )
                self.assertTrue(
                    {
                        "episode_observations",
                        "shadow_policy_decisions",
                        "replay_runs",
                        "account_equity_snapshots",
                    }.issubset(set(inspect(session.get_bind()).get_table_names()))
                )
                unique_names = {
                    str(item.get("name"))
                    for item in (
                        inspect(session.get_bind()).get_unique_constraints(
                            "decision_outcomes"
                        )
                        + inspect(session.get_bind()).get_indexes("decision_outcomes")
                    )
                }
                self.assertIn("uq_episode_horizon_outcome_source", unique_names)
            finally:
                session.close()
        finally:
            init_db(f"sqlite:///{self.temp_db.name}")
            try:
                os.remove(legacy.name)
            except PermissionError:
                pass

    def test_missing_indicators_are_unknown_not_bearish_evidence(self) -> None:
        snapshot = {
            "symbol": "BANKNIFTY",
            "price": 58000,
            "ema_alignment": None,
            "macd_positive": None,
            "rsi": 50,
        }
        bullish = PriceActionService().evaluate(snapshot, "bullish", "BUY")
        bearish = PriceActionService().evaluate(snapshot, "bearish", "BUY")
        self.assertIn("EMA trend alignment is unavailable", bullish["reasons"])
        self.assertIn("EMA trend alignment is unavailable", bearish["reasons"])
        self.assertEqual(bullish["score"], bearish["score"])

    def test_generated_candle_provenance_and_building_state_are_explicit(self) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        candles = []
        for index, close in enumerate([100, 102, 104, 106, 108, 112]):
            candles.append(
                type(
                    "PremiumCandle",
                    (),
                    {
                        "timestamp": now - timedelta(minutes=5 - index),
                        "open_price": close - 1,
                        "high_price": close,
                        "low_price": close - 2,
                        "close_price": close,
                        "volume": 1000 if index < 5 else 2000,
                        "source": "websocket_builder",
                    },
                )()
            )
        contract = OptionContract(
            "BANKNIFTY99DEC58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-12-31",
            58000,
            "CE",
            15,
            112,
            100000,
            100000,
            111.5,
            112,
            bid_quantity=1000,
            ask_quantity=1000,
        )
        result = OptionPremiumConfirmationService(PremiumFeed(candles)).evaluate(
            contract=contract
        )
        self.assertTrue(result["details"]["building_candle_used"])
        self.assertEqual(result["details"]["last_candle_state"], "building")
        self.assertEqual(
            result["details"]["candle_provenance"][-1]["state"], "building"
        )

    def test_initial_snapshot_loader_excludes_generated_structure_candles(self) -> None:
        session = get_session()
        try:
            start = (ist_now_naive() - timedelta(days=1)).replace(
                hour=9, minute=15, second=0, microsecond=0
            )
            for index in range(6):
                session.add(
                    Candle(
                        symbol="BANKNIFTY",
                        timeframe="5minute",
                        timestamp=start + timedelta(minutes=index * 5),
                        open_price=58000 + index,
                        high_price=58010 + index,
                        low_price=57990 + index,
                        close_price=58005 + index,
                        volume=1000,
                        is_generated=0,
                    )
                )
            session.add(
                Candle(
                    symbol="BANKNIFTY",
                    timeframe="5minute",
                    timestamp=start + timedelta(minutes=30),
                    open_price=1,
                    high_price=1,
                    low_price=1,
                    close_price=1,
                    volume=0,
                    is_generated=1,
                )
            )
            session.commit()
        finally:
            session.close()
        rows = KiteFeed.__new__(KiteFeed)._recent_stored_candles("BANKNIFTY")
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(not row["is_generated"] for row in rows))


if __name__ == "__main__":
    unittest.main()
