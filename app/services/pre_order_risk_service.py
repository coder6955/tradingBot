from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import Signal
from app.services.decision_evidence_repository import DecisionEvidenceRepository
from app.services.episode_reservation_service import EpisodeReservationService
from app.services.risk_policy_service import RiskDecisionContext, RiskPolicyService, TIER_1_BASE
from app.services.time_utils import ist_now_naive


class PreOrderRiskService:
    """One final risk authority shared by paper and live order routing."""

    def __init__(
        self,
        *,
        risk_management_service: Any,
        risk_policy_service: RiskPolicyService | None = None,
        episode_reservation_service: EpisodeReservationService | None = None,
        evidence_repository: DecisionEvidenceRepository | None = None,
        evidence_queue: Any | None = None,
        outcome_collector: Any | None = None,
        latency_metrics: Any | None = None,
    ) -> None:
        self.risk_management_service = risk_management_service
        self.risk_policy_service = risk_policy_service or RiskPolicyService()
        self.episode_reservation_service = episode_reservation_service or EpisodeReservationService()
        self.evidence_repository = evidence_repository or DecisionEvidenceRepository()
        self.evidence_queue = evidence_queue
        self.outcome_collector = outcome_collector
        self.latency_metrics = latency_metrics

    def evaluate_and_reserve(
        self,
        signal: Signal,
        *,
        order_mode: str,
        live_requested: bool,
        execution_quality: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        account_equity_override: float | None = None,
        live_safety: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validation_started = time.perf_counter()
        mode = str(order_mode or "paper").lower()
        identity_started = time.perf_counter()
        identity = self.episode_reservation_service.identity(signal, metadata=metadata)
        self._record_latency("pre_order_episode_identity_duration", identity_started, {"mode": mode})
        account_started = time.perf_counter()
        risk_guard = self.risk_management_service.evaluate_signal(signal.symbol)
        self._record_latency("pre_order_account_state_loading_duration", account_started, {"mode": mode})
        limits = risk_guard.get("limits", {}) if isinstance(risk_guard, dict) else {}
        risk_state = risk_guard.get("risk_state", {}) if isinstance(risk_guard, dict) else {}
        equity = float(
            account_equity_override
            or risk_state.get("current_audited_equity")
            or limits.get("current_audited_equity")
            or limits.get("available_cash")
            or (settings.account_equity if mode == "paper" else 0.0)
        )
        factors = signal.factor_scores if isinstance(signal.factor_scores, dict) else {}
        risk_request = factors.get("risk_request") if isinstance(factors.get("risk_request"), dict) else {}
        requested_tier = str((metadata or {}).get("requested_risk_tier") or risk_request.get("requested_tier") or TIER_1_BASE)
        quality_details = execution_quality.get("details", {}) if isinstance(execution_quality, dict) else {}
        context = RiskDecisionContext(
            account_equity=equity,
            expected_entry=float(quality_details.get("ask") or signal.entry_price or 0.0),
            stop_price=float(signal.stop_loss or 0.0),
            lot_size=int(signal.lot_size or 0),
            requested_tier=requested_tier,
            order_mode=mode,
            policy_mode="active",
            symbol=signal.symbol,
            strategy_version=self._strategy_version(factors),
            setup_family=self._setup_family(signal, factors),
            market_regime=self._factor_value(factors, "market_regime", "regime"),
            session_phase=self._session_phase(),
            direction=signal.action,
            target_structure={"target_1": signal.target_1, "target_2": signal.target_2, "target_3": signal.target_3},
            remaining_risk_reward=float(signal.risk_reward or 0.0),
            option_spread_pct=float(quality_details.get("spread_pct") or 0.0),
            entry_depth_quantity=self._optional_int(quality_details.get("ask_depth_quantity")),
            stop_exit_depth_quantity=self._optional_int(quality_details.get("bid_depth_quantity")),
            contract_dte=self._dte(signal.expiry),
            realized_daily_pnl=float(risk_state.get("realized_daily_pnl") or 0.0),
            unrealized_daily_pnl=float(risk_state.get("unrealized_daily_pnl") or 0.0),
            current_drawdown_pct=float(risk_state.get("current_drawdown_pct") or 0.0),
            consecutive_losses=int(risk_state.get("consecutive_losses") or 0),
            trades_taken_today=int((risk_guard.get("summary") or {}).get("trades") or 0),
            open_positions=int((risk_guard.get("open_exposure") or {}).get("open_trades") or 0),
            existing_premium_exposure=float((risk_guard.get("open_exposure") or {}).get("premium_exposure") or 0.0),
            planned_risk_today=float(risk_state.get("planned_risk_today") or 0.0),
            total_open_risk=float(risk_state.get("total_open_risk") or 0.0),
            banknifty_open_risk=float(risk_state.get("banknifty_open_risk") or 0.0),
            correlated_banknifty_positions=int((risk_guard.get("open_exposure") or {}).get("by_symbol", {}).get("BANKNIFTY", 0)),
            setup_quality_evidence=dict(factors.get("setup_quality_evidence") or {}),
        )
        policy_started = time.perf_counter()
        active_decision = self.risk_policy_service.evaluate(context)
        shadow_decisions = self.risk_policy_service.shadow_tier_evaluations(context)
        self._record_latency("risk_tier_evaluation_duration", policy_started, {"mode": mode, "requested_tier": requested_tier})
        rejection_reasons = list(active_decision.rejection_reasons)
        if not risk_guard.get("passed", False):
            rejection_reasons.extend(["ACCOUNT_RISK_GATES_FAILED", *[str(reason) for reason in risk_guard.get("reasons", [])]])
        if not execution_quality.get("passed", False):
            rejection_reasons.append("EXECUTION_QUALITY_FAILED")
        if settings.enforce_market_hours and not self._session_eligible():
            rejection_reasons.append("SESSION_NOT_ELIGIBLE")
        if live_requested:
            if not settings.live_trading_mode or settings.paper_trading_mode:
                rejection_reasons.append("LIVE_TRADING_CONFIGURATION_NOT_AUTHORIZED")
            if live_safety and live_safety.get("blocked"):
                rejection_reasons.append("LIVE_RECONCILIATION_OR_BROKER_SAFETY_FAILED")
            if settings.require_broker_protective_stop_for_live_entry and not settings.enable_broker_emergency_sl:
                rejection_reasons.append("LIVE_PROTECTIVE_STOP_REQUIRED")

        quantity_started = time.perf_counter()
        maximum_quantity = int(active_decision.maximum_quantity)
        requested_quantity = int(signal.quantity or 0)
        approved_quantity = min(requested_quantity, maximum_quantity)
        lot_size = int(signal.lot_size or 0)
        approved_quantity = (approved_quantity // lot_size) * lot_size if lot_size > 0 else 0
        if approved_quantity <= 0 and "RISK_BUDGET_BELOW_MINIMUM_LOT" not in rejection_reasons:
            rejection_reasons.append("RISK_BUDGET_BELOW_MINIMUM_LOT")
        estimated_loss = approved_quantity * float(active_decision.risk_per_unit)
        if estimated_loss > float(active_decision.approved_risk_amount) + 0.01:
            rejection_reasons.append("ESTIMATED_STOP_LOSS_EXCEEDS_APPROVED_RISK")
        self._record_latency("pre_order_quantity_authorization_duration", quantity_started, {"mode": mode})

        evidence_event_id = uuid.uuid4().hex
        evidence_context = {
            "trading_date": ist_now_naive().date().isoformat(),
            "session_phase": context.session_phase,
            "strategy_version": context.strategy_version,
            "signal": asdict(signal),
            "risk_context": asdict(context),
            "execution_quality": execution_quality,
            "account_risk": risk_guard,
            "requested_quantity": requested_quantity,
            "approved_quantity": approved_quantity,
            "estimated_total_loss_at_stop_for_approved_quantity": round(estimated_loss, 2),
            "metadata": metadata or {},
        }
        evidence_payload = {
            "decision_type": "pre_order_risk",
            "final_state": "REJECTED" if rejection_reasons else "PREPARED",
            "symbol": signal.symbol,
            "tradingsymbol": signal.tradingsymbol,
            "episode_key": identity["episode_key"],
            "context": evidence_context,
            "gate_results": {"rejection_reasons": list(dict.fromkeys(rejection_reasons))},
            "active_risk_decision": active_decision.to_dict(),
            "shadow_risk_decisions": shadow_decisions,
            "transition_timestamps": {"pre_order_evaluated_at": ist_now_naive().isoformat(sep=" ")},
        }
        if rejection_reasons:
            # A duplicate may also make account gates fail (for example the
            # first order already consumed the open-position limit). Resolve
            # the more actionable duplicate reason only on this rejected path;
            # the authorized path relies on the atomic reservation itself.
            existing_episode = self.episode_reservation_service.status(identity["episode_key"])
            if existing_episode and existing_episode.get("state") in {"RESERVED", "ORDER_PENDING", "OPEN", "CLOSED"}:
                rejection_reasons.insert(0, f"DUPLICATE_EPISODE_{existing_episode.get('state')}")
                evidence_payload["gate_results"] = {"rejection_reasons": list(dict.fromkeys(rejection_reasons))}
            self._register_outcome_episode(
                identity["episode_key"], signal, context, state="REJECTED", approved_quantity=0, metadata=metadata
            )
            evidence_started = time.perf_counter()
            evidence = self._persist_evidence(
                evidence_payload,
                event_id=evidence_event_id,
                episode_key=None,
            )
            self._record_latency("pre_order_evidence_enqueue_duration", evidence_started, {"mode": mode, "authorized": False})
            self._record_latency("final_pre_order_validation_duration", validation_started, {"mode": mode, "passed": False})
            return {
                "passed": False,
                "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
                "risk_decision": active_decision.to_dict(),
                "shadow_risk_decisions": shadow_decisions,
                "approved_quantity": 0,
                "episode": {**identity, "acquired": False},
                "evidence": evidence,
            }

        reservation_started = time.perf_counter()
        reservation = self.episode_reservation_service.reserve_order_intent(
            signal,
            authorized_quantity=approved_quantity,
            order_mode=mode,
            risk_decision=active_decision.to_dict(),
            evidence_event_id=evidence_event_id,
            metadata=metadata,
            identity_override=identity,
        )
        self._record_latency("atomic_episode_reservation_duration", reservation_started, {"mode": mode})
        if not reservation.get("acquired"):
            duplicate_reason = str(reservation.get("reason") or "DUPLICATE_EPISODE")
            duplicate_payload = {
                **evidence_payload,
                "decision_type": "pre_order_reservation",
                "final_state": "REJECTED",
                "gate_results": {"rejection_reasons": [duplicate_reason]},
                "transition_timestamps": {"reservation_rejected_at": ist_now_naive().isoformat(sep=" ")},
            }
            duplicate_evidence = self._persist_evidence(
                duplicate_payload,
                event_id=uuid.uuid4().hex,
                episode_key=None,
            )
            self._register_outcome_episode(
                identity["episode_key"], signal, context, state="REJECTED", approved_quantity=0, metadata=metadata
            )
            self._record_latency("final_pre_order_validation_duration", validation_started, {"mode": mode, "passed": False})
            return {
                "passed": False,
                "rejection_reasons": [duplicate_reason],
                "risk_decision": active_decision.to_dict(),
                "shadow_risk_decisions": shadow_decisions,
                "approved_quantity": 0,
                "episode": reservation,
                "evidence": duplicate_evidence,
            }
        evidence_started = time.perf_counter()
        self._register_outcome_episode(
            identity["episode_key"], signal, context, state="ORDER_PENDING", approved_quantity=approved_quantity, metadata=metadata
        )
        evidence = self._persist_evidence(
            evidence_payload,
            event_id=evidence_event_id,
            episode_key=identity["episode_key"],
        )
        self._record_latency("pre_order_evidence_enqueue_duration", evidence_started, {"mode": mode, "authorized": True})
        if not evidence.get("accepted", True):
            self.episode_reservation_service.release(
                identity["episode_key"],
                str(reservation.get("reservation_token") or ""),
                reason=str(evidence.get("reason") or "EVIDENCE_QUEUE_UNAVAILABLE"),
            )
            self._record_latency("final_pre_order_validation_duration", validation_started, {"mode": mode, "passed": False})
            return {
                "passed": False,
                "rejection_reasons": [str(evidence.get("reason") or "EVIDENCE_QUEUE_UNAVAILABLE")],
                "risk_decision": active_decision.to_dict(),
                "shadow_risk_decisions": shadow_decisions,
                "approved_quantity": 0,
                "episode": {**reservation, "acquired": False},
                "evidence": evidence,
            }
        self._record_latency("final_pre_order_validation_duration", validation_started, {"mode": mode, "passed": True})
        return {
            "passed": True,
            "rejection_reasons": [],
            "risk_decision": active_decision.to_dict(),
            "shadow_risk_decisions": shadow_decisions,
            "approved_quantity": approved_quantity,
            "episode": reservation,
            "evidence": evidence,
        }

    def _persist_evidence(
        self,
        payload: dict[str, Any],
        *,
        event_id: str,
        episode_key: str | None,
    ) -> dict[str, Any]:
        if self.evidence_queue is not None:
            return self.evidence_queue.enqueue_decision(payload, episode_key=episode_key, event_id=event_id)
        result = self.evidence_repository.record_decision(**payload, decision_id=event_id)
        return {**result, "accepted": True, "event_id": event_id, "reason": None}

    def _register_outcome_episode(
        self,
        episode_key: str,
        signal: Signal,
        context: RiskDecisionContext,
        *,
        state: str,
        approved_quantity: int,
        metadata: dict[str, Any] | None,
    ) -> None:
        if self.outcome_collector is None:
            return
        factors = signal.factor_scores if isinstance(signal.factor_scores, dict) else {}
        contract = factors.get("contract") if isinstance(factors.get("contract"), dict) else {}
        try:
            self.outcome_collector.register_episode(
                episode_key,
                state=state,
                observed_at=self._metadata_time(metadata, "setup_generated_at") or self._metadata_time(metadata, "armed_at"),
                context={
                    "underlying_token": (metadata or {}).get("underlying_token") or factors.get("underlying_token"),
                    "option_token": signal.instrument_token or contract.get("instrument_token"),
                    "instrument_token": signal.instrument_token or contract.get("instrument_token"),
                    "tradingsymbol": signal.tradingsymbol,
                    "strike": signal.strike,
                    "expiry": signal.expiry,
                    "lot_size": signal.lot_size,
                    "quantity": approved_quantity,
                    "entry_price": context.expected_entry,
                    "stop_loss": signal.stop_loss,
                    "target_1": signal.target_1,
                    "target_2": signal.target_2,
                    "target_3": signal.target_3,
                    "account_equity": context.account_equity,
                    "setup_family": context.setup_family,
                    "market_regime": context.market_regime,
                    "session_phase": context.session_phase,
                },
            )
        except Exception:
            # Research tracking must never weaken the final safety decision.
            return

    def _metadata_time(self, metadata: dict[str, Any] | None, key: str) -> datetime | None:
        value = (metadata or {}).get(key)
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                return None
        return None

    def _session_eligible(self) -> bool:
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() >= 5:
            return False
        start_hour, start_minute = (int(value) for value in settings.market_open_time.split(":", 1))
        end_hour, end_minute = (int(value) for value in settings.market_close_time.split(":", 1))
        start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        return start <= now <= end

    def _session_phase(self) -> str:
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.time() < now.replace(hour=9, minute=45).time():
            return "opening"
        if now.time() >= now.replace(hour=15, minute=0).time():
            return "late_session"
        return "regular"

    def _dte(self, expiry: str | None) -> int | None:
        if not expiry:
            return None
        try:
            return (datetime.fromisoformat(str(expiry)).date() - ist_now_naive().date()).days
        except ValueError:
            return None

    def _strategy_version(self, factors: dict[str, Any]) -> str:
        metadata = factors.get("strategy_metadata") if isinstance(factors.get("strategy_metadata"), dict) else {}
        return str(metadata.get("strategy_version") or settings.strategy_version)

    def _setup_family(self, signal: Signal, factors: dict[str, Any]) -> str:
        family = factors.get("setup_family") if isinstance(factors.get("setup_family"), dict) else {}
        return str(family.get("name") or signal.setup_type or "unclassified")

    def _factor_value(self, factors: dict[str, Any], name: str, key: str) -> str:
        value = factors.get(name) if isinstance(factors.get(name), dict) else {}
        return str(value.get(key) or "unknown")

    def _optional_int(self, value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _record_latency(self, name: str, started: float, detail: dict[str, Any]) -> None:
        if self.latency_metrics is None:
            return
        self.latency_metrics.record(name, (time.perf_counter() - started) * 1000.0, detail=detail)
