from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from types import SimpleNamespace
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from app.config import settings
from app.models import Signal
from app.services.entry_timing_service import EntryTimingService
from app.services.entry_opportunity_service import EntryOpportunityService
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.risk_management_service import RiskManagementService
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import OptionContract
from app.services.armed_entry_repository import ArmedEntryRepository


logger = logging.getLogger(__name__)


@dataclass
class ArmedEntrySetup:
    setup_id: str
    symbol: str
    action: str
    side: str
    tradingsymbol: str
    exchange: str
    instrument_token: int
    option_type: str
    strike: float
    expiry: str
    current_premium_at_arming: float
    entry_trigger_price: float
    trigger_source: str
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    risk_reward: float
    quantity: int
    lot_size: int
    score: int
    probability: float | None
    confidence: float
    max_entry_chase_pct: float
    max_premium_move_from_base_pct: float
    min_remaining_risk_reward: float
    min_target1_room_pct: float
    cached_spread_pct: float
    armed_at: datetime
    valid_until: datetime
    strategy_name: str
    strategy_version: str
    order_mode: str
    reasons: list[str] = field(default_factory=list)
    latest_state: str = EntryTimingService.ARMED_FOR_ENTRY
    latest_reason: str = ""
    latest_premium: float | None = None
    latest_bid: float | None = None
    latest_ask: float | None = None
    latest_spread_pct: float | None = None
    tick_quality_count: int = 0
    tick_quality_started_at: datetime | None = None
    tick_quality_last_at: datetime | None = None
    tick_quality_last_bid: float | None = None
    tick_quality_last_ask: float | None = None
    tick_quality_confirmed: bool = False
    tick_quality_reason: str = ""
    recent_premiums: list[float] = field(default_factory=list)
    latest_normalized_chase: float | None = None
    latest_chase_scale: float | None = None
    entered_trade: dict[str, Any] | None = None
    entered_at: datetime | None = None
    cancelled_at: datetime | None = None
    rejection_saved: bool = False
    factor_scores: dict[str, Any] = field(default_factory=dict)
    risk_preflight_passed: bool = False
    risk_preflight_at: datetime | None = None
    risk_preflight_reasons: list[str] = field(default_factory=list)


class ArmedEntryTrackerService:
    """Track near-trigger Bank Nifty option setups and fire paper entries from ticks."""

    ACTIVE_STATES = {EntryTimingService.ARMED_FOR_ENTRY, EntryTimingService.ENTER_NOW}
    PENDING_STATES = {"ORDER_PENDING"}
    TERMINAL_STATES = {"TOO_LATE", "EXPIRED", "CANCELLED", "ENTERED_PAPER"}

    def __init__(
        self,
        *,
        order_service_factory: Callable[[], Any] | None = None,
        websocket_price_feed: Any | None = None,
        rejected_opportunity_repository: RejectedOpportunityRepository | None = None,
        risk_management_service: RiskManagementService | None = None,
        market_session_provider: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        latency_metrics: Any | None = None,
        armed_entry_repository: ArmedEntryRepository | None = None,
    ) -> None:
        self.order_service_factory = order_service_factory
        self.websocket_price_feed = websocket_price_feed
        self.rejected_opportunity_repository = (
            rejected_opportunity_repository or RejectedOpportunityRepository()
        )
        self.risk_management_service = (
            risk_management_service or RiskManagementService()
        )
        self.market_session_provider = market_session_provider
        self.clock = clock or ist_now_naive
        self.latency_metrics = latency_metrics
        self.armed_entry_repository = armed_entry_repository
        self._setups: dict[str, ArmedEntrySetup] = {}
        self._setup_id_by_key: dict[str, str] = {}
        self._lock = RLock()
        self.last_triggered_at: datetime | None = None
        self.last_reason: str | None = None
        self._execution_worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="armed-entry-execution"
        )

    def register_from_scan(
        self,
        *,
        symbol: str,
        action: str,
        side: str,
        contract: OptionContract,
        prices: dict[str, float],
        entry_timing: dict[str, Any],
        score: int,
        probability: float | None,
        confidence: float,
        quantity: int,
        factor_scores: dict[str, Any],
        order_mode: str,
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        if (
            not settings.enable_event_driven_paper_entry
            and str(order_mode).lower() == "paper"
        ):
            return {"registered": False, "reason": "event_driven_paper_entry_disabled"}
        token = self._int(contract.instrument_token)
        trigger = self._float(entry_timing.get("entry_trigger_price"))
        current = self._float(
            entry_timing.get("current_premium") or contract.ask or contract.last_price
        )
        if token is None or trigger <= 0 or current <= 0:
            return {"registered": False, "reason": "armed_entry_inputs_missing"}

        now = self.clock().replace(tzinfo=None)
        risk_preflight = self.risk_management_service.evaluate_signal(
            symbol, order_mode=order_mode
        )
        if not risk_preflight.get("passed", False):
            return {
                "registered": False,
                "reason": "risk_preflight_failed",
                "risk_reasons": [
                    str(reason) for reason in risk_preflight.get("reasons", [])
                ],
            }
        valid_until = now + timedelta(
            seconds=max(5, settings.armed_entry_valid_seconds)
        )
        key = self._key(
            order_mode=order_mode,
            action=action,
            token=token,
            expiry=contract.expiry,
            strike=contract.strike,
        )
        with self._lock:
            existing_id = self._setup_id_by_key.get(key)
            setup_id = existing_id or f"armed-{uuid4().hex[:12]}"
            setup = ArmedEntrySetup(
                setup_id=setup_id,
                symbol=symbol.upper(),
                action=action,
                side=side.upper(),
                tradingsymbol=contract.tradingsymbol,
                exchange=contract.exchange,
                instrument_token=token,
                option_type=contract.option_type,
                strike=float(contract.strike),
                expiry=contract.expiry,
                current_premium_at_arming=current,
                entry_trigger_price=trigger,
                trigger_source="entry_timing_recent_high",
                stop_loss=float(prices.get("stop_loss") or 0.0),
                target_1=float(prices.get("target_1") or 0.0),
                target_2=float(prices.get("target_2") or 0.0),
                target_3=float(prices.get("target_3") or 0.0),
                risk_reward=float(prices.get("risk_reward") or 0.0),
                quantity=int(quantity or contract.lot_size or 0),
                lot_size=int(contract.lot_size or quantity or 0),
                score=int(score or 0),
                probability=float(probability) if probability is not None else None,
                confidence=float(confidence or 0.0),
                max_entry_chase_pct=settings.max_entry_chase_pct,
                max_premium_move_from_base_pct=settings.max_premium_move_from_base_pct,
                min_remaining_risk_reward=settings.min_remaining_risk_reward,
                min_target1_room_pct=settings.min_target1_room_pct,
                cached_spread_pct=self._float(entry_timing.get("spread_pct")),
                armed_at=now,
                valid_until=valid_until,
                strategy_name=settings.strategy_name,
                strategy_version=settings.strategy_version,
                order_mode=str(order_mode or "paper").lower(),
                reasons=list(reasons or entry_timing.get("reasons") or []),
                latest_reason="waiting_for_entry_trigger",
                latest_premium=current,
                recent_premiums=[current],
                factor_scores=dict(factor_scores or {}),
                risk_preflight_passed=True,
                risk_preflight_at=now,
                risk_preflight_reasons=[],
            )
            if (
                existing_id
                and existing_id in self._setups
                and self._setups[existing_id].latest_state in self.TERMINAL_STATES
            ):
                setup.setup_id = f"armed-{uuid4().hex[:12]}"
            self._setups[setup.setup_id] = setup
            self._setup_id_by_key[key] = setup.setup_id

        self._persist(setup)

        subscribe_result = self._subscribe_token(token, setup_id=setup.setup_id)
        subscription_health = self._subscription_health(setup, subscribe_result)
        if subscription_health["state"] == "failed":
            with self._lock:
                setup.latest_state = "CANCELLED"
                setup.latest_reason = str(
                    subscribe_result.get("reason") or "subscription_failed"
                )
                setup.cancelled_at = self.clock().replace(tzinfo=None)
            self._persist(setup)
            self._release_subscription(setup)
            return {
                **self._to_dict(setup),
                "registered": False,
                "reason": setup.latest_reason,
                "subscription": subscribe_result,
                "subscription_health": subscription_health,
                "websocket_tracking_enabled": False,
            }
        payload = self._to_dict(setup)
        payload.update(
            {
                "registered": True,
                "websocket_tracking_enabled": bool(
                    subscription_health["tracking_ready"]
                ),
                "subscription_state": subscription_health["state"],
                "subscription_health": subscription_health,
                "paper_event_entry_enabled": settings.enable_event_driven_paper_entry,
                "live_event_entry_blocked": setup.order_mode == "live"
                and not settings.enable_event_driven_live_entry,
                "subscription": subscribe_result,
            }
        )
        logger.info(
            "armed_entry_registered %s",
            {
                "setup_id": setup.setup_id,
                "tradingsymbol": setup.tradingsymbol,
                "trigger": setup.entry_trigger_price,
            },
        )
        return payload

    def register_from_fast_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Promote a slow-path prepared plan after zero-I/O fast validation."""
        order_mode = str(plan.get("order_mode") or "paper").lower()
        if order_mode != "paper":
            return {
                "registered": False,
                "reason": "fast_candidate_promotion_paper_only",
            }
        payload = plan.get("contract")
        if not isinstance(payload, dict):
            return {
                "registered": False,
                "reason": "fast_candidate_contract_plan_missing",
            }
        allowed = {item.name for item in fields(OptionContract)}
        try:
            contract = OptionContract(
                **{key: value for key, value in payload.items() if key in allowed}
            )
        except (TypeError, ValueError) as exc:
            return {
                "registered": False,
                "reason": "fast_candidate_contract_plan_invalid",
                "message": str(exc),
            }
        factors = dict(plan.get("factor_scores") or {})
        factors["fast_candidate_promotion"] = {
            "promoted": True,
            "source": "banknifty_fast_rally",
            "strategy_version": settings.strategy_version,
        }
        return self.register_from_scan(
            symbol=str(plan.get("symbol") or "BANKNIFTY"),
            action=str(plan.get("action") or ""),
            side=str(plan.get("side") or "BUY"),
            contract=contract,
            prices=dict(plan.get("prices") or {}),
            entry_timing=dict(plan.get("entry_timing") or {}),
            score=int(plan.get("score") or 0),
            probability=float(plan["probability"])
            if plan.get("probability") is not None
            else None,
            confidence=float(plan.get("confidence") or 0.0),
            quantity=int(plan.get("quantity") or contract.lot_size or 0),
            factor_scores=factors,
            order_mode=order_mode,
            reasons=[str(reason) for reason in (plan.get("reasons") or [])],
        )

    def on_tick(self, tick: WebSocketTick) -> list[dict[str, Any]]:
        started = time.perf_counter()
        token = self._int(tick.instrument_token)
        try:
            if token is None:
                return []
            triggered: list[dict[str, Any]] = []
            with self._lock:
                setup_ids = [
                    setup.setup_id
                    for setup in self._setups.values()
                    if setup.instrument_token == token
                    and setup.latest_state in self.ACTIVE_STATES
                ]
            for setup_id in setup_ids:
                result = self.evaluate_tick(setup_id, tick)
                if result:
                    triggered.append(result)
            return triggered
        finally:
            if self.latency_metrics is not None:
                self.latency_metrics.record(
                    "armed_tick_processing_duration",
                    (time.perf_counter() - started) * 1000.0,
                    detail={"instrument_token": token},
                )

    def evaluate_tick(
        self, setup_id: str, tick: WebSocketTick
    ) -> dict[str, Any] | None:
        with self._lock:
            setup = self._setups.get(setup_id)
            if setup is None or setup.latest_state in self.TERMINAL_STATES:
                return None

        executable = float(tick.ask or tick.price)
        bid = self._float(tick.bid)
        ask = self._float(tick.ask)
        confirmation_price = min(float(tick.price or 0.0), bid) if bid > 0 else 0.0
        spread_pct = self._spread_pct(
            bid=bid, ask=ask, price=tick.price, fallback=setup.cached_spread_pct
        )
        now = self.clock().replace(tzinfo=None)
        with self._lock:
            setup.latest_premium = executable
            setup.latest_bid = bid or None
            setup.latest_ask = ask or None
            setup.latest_spread_pct = spread_pct
            setup.recent_premiums.append(executable)
            keep = max(3, int(settings.normalized_entry_chase_lookback_ticks))
            setup.recent_premiums = setup.recent_premiums[-keep:]

        if now > setup.valid_until:
            return self._mark_rejected(
                setup.setup_id, "EXPIRED", ["armed_setup_expired"]
            )

        if executable >= setup.entry_trigger_price:
            checks = self._entry_checks(
                setup, executable_price=executable, spread_pct=spread_pct
            )
            if checks:
                return self._mark_rejected(setup.setup_id, "TOO_LATE", checks)

        if confirmation_price < setup.entry_trigger_price:
            with self._lock:
                setup.latest_state = EntryTimingService.ARMED_FOR_ENTRY
                setup.latest_reason = "waiting_for_bid_and_last_trigger_confirmation"
                self._reset_tick_quality_locked(setup)
            return self._to_dict(setup)

        tick_quality = self._tick_quality_confirmation(
            setup,
            tick=tick,
            executable_price=executable,
            spread_pct=spread_pct,
            now=now,
        )
        if not tick_quality["passed"]:
            with self._lock:
                setup.latest_state = EntryTimingService.ARMED_FOR_ENTRY
                setup.latest_reason = str(tick_quality["reason"])
                self.last_reason = setup.latest_reason
            return {**self._to_dict(setup), "tick_quality": tick_quality}

        confirmed_at = self.clock().replace(tzinfo=None)
        if self.latency_metrics is not None:
            trigger_time = (
                tick.timestamp
                if tick.timestamp_source in {"exchange_timestamp", "last_trade_time"}
                else tick.receive_timestamp
            )
            self.latency_metrics.record_between(
                "trigger_timestamp_to_confirmation",
                trigger_time,
                confirmed_at,
                detail={
                    "setup_id": setup.setup_id,
                    "timestamp_source": tick.timestamp_source,
                },
            )
            self.latency_metrics.record_between(
                "armed_state_to_confirmation",
                setup.armed_at,
                confirmed_at,
                detail={
                    "setup_id": setup.setup_id,
                    "timestamp_source": tick.timestamp_source,
                },
            )

        session = self._market_session()
        if session != "REGULAR_MARKET":
            return self._mark_rejected(
                setup.setup_id,
                "CANCELLED",
                ["event_entry_hard_gate_failed", "market_closed"],
            )

        if setup.order_mode == "live":
            with self._lock:
                setup.latest_state = EntryTimingService.ARMED_FOR_ENTRY
                setup.latest_reason = "live_trading_not_enabled_for_event_entry"
                self.last_reason = setup.latest_reason
            return {
                **self._to_dict(setup),
                "live_event_entry_blocked": True,
                "reason": setup.latest_reason,
            }

        if setup.order_mode != "paper":
            return self._mark_rejected(
                setup.setup_id,
                "CANCELLED",
                ["event_entry_hard_gate_failed", "unsupported_order_mode"],
            )

        if not settings.enable_event_driven_paper_entry:
            return self._mark_rejected(
                setup.setup_id,
                "CANCELLED",
                ["event_entry_hard_gate_failed", "event_driven_paper_entry_disabled"],
            )

        risk = self.risk_management_service.evaluate_signal(
            setup.symbol, order_mode="paper"
        )
        if not risk.get("passed", False):
            return self._mark_rejected(
                setup.setup_id,
                "CANCELLED",
                [
                    "event_entry_hard_gate_failed",
                    *[str(reason) for reason in risk.get("reasons", [])],
                ],
            )

        if bool(getattr(self.websocket_price_feed, "running", False)):
            queued_at = self.clock().replace(tzinfo=None)
            with self._lock:
                setup.latest_state = "ORDER_PENDING"
                setup.latest_reason = "event_entry_queued_for_execution"
            self._execution_worker.submit(
                self._enter_paper,
                setup.setup_id,
                executable_price=executable,
                tick=tick,
                spread_pct=spread_pct,
                queued_at=queued_at,
            )
            if self.latency_metrics is not None:
                self.latency_metrics.record_between(
                    "confirmation_to_execution_queued",
                    confirmed_at,
                    queued_at,
                    detail={"setup_id": setup.setup_id},
                )
            return {**self._to_dict(setup), "execution_queued": True}
        return self._enter_paper(
            setup.setup_id,
            executable_price=executable,
            tick=tick,
            spread_pct=spread_pct,
            queued_at=confirmed_at,
        )

    def cancel(self, setup_id: str, reason: str = "cancelled") -> dict[str, Any]:
        return self._mark_rejected(setup_id, "CANCELLED", [reason])

    def cancel_for_data_gap(
        self, event: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not settings.cancel_armed_entries_on_data_gap:
            return {"cancelled": 0, "reason": "cancel_on_gap_disabled"}
        if event and (event.get("recovered") or not event.get("entry_blocking", True)):
            return {"cancelled": 0, "reason": "non_blocking_or_recovered_gap"}
        reason = "websocket_data_gap_detected"
        if event and event.get("reason"):
            reason = str(event.get("reason"))
        affected_token = self._int(event.get("instrument_token")) if event else None
        with self._lock:
            setup_ids = [
                setup.setup_id
                for setup in self._setups.values()
                if setup.latest_state in self.ACTIVE_STATES
                and (affected_token is None or setup.instrument_token == affected_token)
            ]
        results = [
            self._mark_rejected(
                setup_id,
                "CANCELLED",
                ["event_entry_hard_gate_failed", "data_gap_detected", reason],
            )
            for setup_id in setup_ids
        ]
        return {"cancelled": len(results), "reason": reason, "results": results}

    def list_entries(self) -> dict[str, Any]:
        self.expire_stale()
        with self._lock:
            rows = [self._to_dict(setup) for setup in self._setups.values()]
        return {
            "status": "ok",
            "event_entry_enabled": settings.enable_event_driven_paper_entry,
            "live_event_entry_enabled": settings.enable_event_driven_live_entry,
            "count": len(rows),
            "active": [
                row for row in rows if row["latest_state"] in self.ACTIVE_STATES
            ],
            "expired": [row for row in rows if row["latest_state"] == "EXPIRED"],
            "entered_paper": [
                row for row in rows if row["latest_state"] == "ENTERED_PAPER"
            ],
            "too_late": [row for row in rows if row["latest_state"] == "TOO_LATE"],
            "cancelled": [row for row in rows if row["latest_state"] == "CANCELLED"],
        }

    def recover_active(self) -> dict[str, Any]:
        """Rehydrate valid armed setups and restore owner-based subscriptions."""
        if not settings.armed_entry_recovery_enabled:
            return {"recovered": 0, "reason": "armed_entry_recovery_disabled"}
        if self.armed_entry_repository is None:
            return {"recovered": 0, "reason": "armed_entry_repository_unavailable"}
        now = self.clock().replace(tzinfo=None)
        recovered: list[str] = []
        failures: list[str] = []
        try:
            payloads = self.armed_entry_repository.active(now=now)
        except Exception as exc:
            logger.exception("armed_entry_recovery_load_failed")
            return {
                "recovered": 0,
                "reason": "armed_entry_recovery_load_failed",
                "message": str(exc),
            }
        field_names = {item.name for item in fields(ArmedEntrySetup)}
        datetime_fields = {
            "armed_at",
            "valid_until",
            "tick_quality_started_at",
            "tick_quality_last_at",
            "entered_at",
            "cancelled_at",
            "risk_preflight_at",
        }
        for payload in payloads:
            try:
                values = {
                    key: value for key, value in payload.items() if key in field_names
                }
                for key in datetime_fields:
                    if values.get(key) and not isinstance(values[key], datetime):
                        values[key] = datetime.fromisoformat(str(values[key])).replace(
                            tzinfo=None
                        )
                if str(values.get("latest_state")) == "ORDER_PENDING":
                    values["latest_state"] = EntryTimingService.ARMED_FOR_ENTRY
                    values["latest_reason"] = "recovered_after_interrupted_order_queue"
                setup = ArmedEntrySetup(**values)
                if setup.valid_until < now:
                    continue
                key = self._key(
                    order_mode=setup.order_mode,
                    action=setup.action,
                    token=setup.instrument_token,
                    expiry=setup.expiry,
                    strike=setup.strike,
                )
                with self._lock:
                    self._setups[setup.setup_id] = setup
                    self._setup_id_by_key[key] = setup.setup_id
                subscription = self._subscribe_token(
                    setup.instrument_token, setup_id=setup.setup_id
                )
                if subscription.get("reason") == "subscription_failed":
                    failures.append(setup.setup_id)
                else:
                    recovered.append(setup.setup_id)
                self._persist(setup)
            except Exception:
                logger.exception("armed_entry_recovery_row_failed")
                failures.append(str(payload.get("setup_id") or "unknown"))
        return {
            "recovered": len(recovered),
            "setup_ids": recovered,
            "failures": failures,
        }

    def status(self) -> dict[str, Any]:
        entries = self.list_entries()
        active = entries["active"]
        operational = [
            row
            for row in active
            if row.get("subscription_health", {}).get("tracking_ready")
        ]
        return {
            "event_entry_enabled": settings.enable_event_driven_paper_entry,
            "event_entry_live_enabled": settings.enable_event_driven_live_entry,
            "armed_entry_count": len(active),
            "operational_armed_entry_count": len(operational),
            "degraded_armed_entry_count": len(active) - len(operational),
            "armed_entry_tokens": sorted(self.active_tokens()),
            "event_entry_last_triggered_at": self.last_triggered_at.isoformat(sep=" ")
            if self.last_triggered_at
            else None,
            "event_entry_last_reason": self.last_reason,
        }

    def active_tokens(self) -> set[int]:
        with self._lock:
            return {
                setup.instrument_token
                for setup in self._setups.values()
                if setup.latest_state in self.ACTIVE_STATES
            }

    def expire_stale(self) -> None:
        now = self.clock().replace(tzinfo=None)
        expired: list[str] = []
        with self._lock:
            for setup in self._setups.values():
                if setup.latest_state in self.ACTIVE_STATES and now > setup.valid_until:
                    expired.append(setup.setup_id)
        for setup_id in expired:
            self._mark_rejected(setup_id, "EXPIRED", ["armed_setup_expired"])

    def _enter_paper(
        self,
        setup_id: str,
        *,
        executable_price: float,
        tick: WebSocketTick,
        spread_pct: float,
        queued_at: datetime | None = None,
    ) -> dict[str, Any]:
        if self.latency_metrics is not None:
            self.latency_metrics.record_between(
                "armed_execution_queue_wait", queued_at, detail={"setup_id": setup_id}
            )
        with self._lock:
            setup = self._setups.get(setup_id)
            if setup is None or setup.latest_state in self.TERMINAL_STATES:
                return {
                    "entered": False,
                    "reason": "armed_setup_not_active",
                    "setup_id": setup_id,
                }
            setup.latest_state = EntryTimingService.ENTER_NOW
            setup.latest_reason = "event_driven_entry_triggered"

        if self.order_service_factory is None:
            return self._mark_rejected(
                setup_id,
                "CANCELLED",
                ["event_entry_hard_gate_failed", "order_service_unavailable"],
            )

        metadata = self._entry_metadata(
            setup, executable_price=executable_price, spread_pct=spread_pct
        )
        signal = self._signal_for_entry(
            setup, executable_price=executable_price, metadata=metadata
        )
        quality = {
            "passed": True,
            "reasons": [],
            "details": {
                "source": "event_driven_websocket",
                "last_price": tick.price,
                "bid": tick.bid,
                "ask": tick.ask,
                "spread_pct": round(spread_pct, 3),
                "entry_price": executable_price,
                "deviation_pct": 0.0,
            },
        }
        if self.latency_metrics is not None:
            submission_at = self.clock().replace(tzinfo=None)
            if queued_at is None:
                self.latency_metrics.record_missing(
                    "confirmation_to_order_submission",
                    detail={
                        "setup_id": setup_id,
                        "reason": "confirmation_timestamp_missing",
                    },
                )
            else:
                self.latency_metrics.record_between(
                    "confirmation_to_order_submission",
                    queued_at,
                    submission_at,
                    detail={"setup_id": setup_id, "mode": "paper"},
                )
        result = self.order_service_factory().place_signal_order(
            signal,
            confirm_live=False,
            order_mode="paper",
            metadata=metadata,
            execution_quality_override=quality,
        )
        with self._lock:
            current = self._setups[setup_id]
            current.latest_state = "ENTERED_PAPER"
            current.latest_reason = "event_driven_entry_triggered"
            current.entered_trade = result
            current.entered_at = self.clock().replace(tzinfo=None)
            self.last_triggered_at = current.entered_at
            self.last_reason = current.latest_reason
            self._persist(current)
        logger.info(
            "armed_entry_entered_paper %s",
            {
                "setup_id": setup_id,
                "tradingsymbol": setup.tradingsymbol,
                "entry": executable_price,
            },
        )
        self._release_subscription(self._setups[setup_id])
        return self._to_dict(self._setups[setup_id])

    def _entry_checks(
        self, setup: ArmedEntrySetup, *, executable_price: float, spread_pct: float
    ) -> list[str]:
        timing = (
            setup.factor_scores.get("entry_timing", {})
            if isinstance(setup.factor_scores, dict)
            else {}
        )
        timing = timing if isinstance(timing, dict) else {}
        initial = (
            timing.get("entry_opportunity", {})
            if isinstance(timing.get("entry_opportunity"), dict)
            else {}
        )
        opportunity = EntryOpportunityService().evaluate(
            current=executable_price,
            trigger=setup.entry_trigger_price,
            base=setup.current_premium_at_arming,
            stop=setup.stop_loss,
            target=setup.target_1,
            spread_pct=max(spread_pct, setup.cached_spread_pct),
            observations=setup.recent_premiums,
            volatility_scale=self._float(initial.get("opportunity_scale")),
            expected_move_coverage=self._optional_float(
                timing.get("expected_move_coverage")
            ),
            room_to_level_pct=self._optional_float(timing.get("room_to_level_pct")),
            breakout_accepted=bool(timing.get("breakout_accepted")),
        )
        setup.latest_normalized_chase = float(opportunity["normalized_chase"])
        setup.latest_chase_scale = float(opportunity["opportunity_scale"])
        return list(opportunity["blockers"])

    def _mark_rejected(
        self, setup_id: str, state: str, reasons: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            setup = self._setups.get(setup_id)
            if setup is None:
                return {"setup_id": setup_id, "latest_state": state, "reasons": reasons}
            setup.latest_state = state
            setup.latest_reason = "; ".join(dict.fromkeys(reasons))
            if state == "CANCELLED":
                setup.cancelled_at = self.clock().replace(tzinfo=None)
            self.last_reason = setup.latest_reason
            should_save = not setup.rejection_saved
            setup.rejection_saved = True
            self._persist(setup)
        if should_save:
            self._save_rejection(setup, reasons)
        self._release_subscription(setup)
        logger.info(
            "armed_entry_%s %s",
            state.lower(),
            {"setup_id": setup_id, "reasons": reasons},
        )
        return self._to_dict(setup)

    def _save_rejection(self, setup: ArmedEntrySetup, reasons: list[str]) -> None:
        try:
            contract = SimpleNamespace(
                tradingsymbol=setup.tradingsymbol,
                exchange=setup.exchange,
                instrument_token=setup.instrument_token,
                expiry=setup.expiry,
                strike=setup.strike,
                option_type=setup.option_type,
            )
            self.rejected_opportunity_repository.save_rejection(
                symbol=setup.symbol,
                side=setup.side,
                action=setup.action,
                score=setup.score,
                reasons=reasons,
                contract=contract,
                factor_scores={
                    **setup.factor_scores,
                    "armed_entry": self._to_dict(setup),
                    "event_entry_rejection_reasons": reasons,
                },
                score_breakdown=setup.factor_scores.get("score_breakdown", {}),
                rejection_source="event_driven_entry",
            )
        except Exception as exc:
            logger.warning(
                "failed_to_save_armed_entry_rejection setup_id=%s error=%s",
                setup.setup_id,
                exc,
            )

    def _signal_for_entry(
        self,
        setup: ArmedEntrySetup,
        *,
        executable_price: float,
        metadata: dict[str, Any],
    ) -> Signal:
        risk = executable_price - setup.stop_loss
        reward = setup.target_1 - executable_price
        rr = reward / risk if risk > 0 and reward > 0 else setup.risk_reward
        factor_scores = {
            **setup.factor_scores,
            "entry_source": "event_driven_websocket",
            "armed_entry": metadata,
            "decision_policy": {
                "primary_gates_passed": True,
                "event_confirmation_passed": True,
                "score_role": "ranking_only",
                "indicator_role": "diagnostic_only",
            },
        }
        return Signal(
            symbol=setup.symbol,
            action=setup.action,
            side=setup.side,
            tradingsymbol=setup.tradingsymbol,
            exchange=setup.exchange,
            instrument_token=setup.instrument_token,
            strike=setup.strike,
            expiry=setup.expiry,
            entry_price=round(executable_price, 2),
            stop_loss=setup.stop_loss,
            target_1=setup.target_1,
            target_2=setup.target_2,
            target_3=setup.target_3,
            quantity=setup.quantity,
            lot_size=setup.lot_size,
            probability=setup.probability,
            risk_reward=round(rr, 3),
            setup_type="event_driven_banknifty_option_buy",
            factor_scores=factor_scores,
            score=setup.score,
            confidence=setup.confidence,
            explanation="Event-driven paper entry from armed Bank Nifty option setup.",
        )

    def _entry_metadata(
        self, setup: ArmedEntrySetup, *, executable_price: float, spread_pct: float
    ) -> dict[str, Any]:
        now = self.clock().replace(tzinfo=None)
        delay = max(0.0, (now - setup.armed_at).total_seconds())
        chase_pct = (
            (executable_price - setup.entry_trigger_price)
            / max(setup.entry_trigger_price, 0.01)
        ) * 100
        move_from_base_pct = (
            (executable_price - setup.current_premium_at_arming)
            / max(setup.current_premium_at_arming, 0.01)
        ) * 100
        return {
            "entry_source": "event_driven_websocket",
            "armed_setup_id": setup.setup_id,
            "trigger_price": round(setup.entry_trigger_price, 2),
            "executable_entry_price": round(executable_price, 2),
            "entry_delay_seconds": round(delay, 3),
            "chase_pct": round(chase_pct, 3),
            "premium_move_from_base_pct": round(move_from_base_pct, 3),
            "spread_pct": round(spread_pct, 3),
            "tick_quality": self._tick_quality_metadata(setup),
            "normalized_chase": setup.latest_normalized_chase,
            "opportunity_scale": setup.latest_chase_scale,
        }

    def _optional_float(self, value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    def _tick_quality_confirmation(
        self,
        setup: ArmedEntrySetup,
        *,
        tick: WebSocketTick,
        executable_price: float,
        spread_pct: float,
        now: datetime,
    ) -> dict[str, Any]:
        if not settings.enable_tick_quality_confirmation:
            with self._lock:
                setup.tick_quality_confirmed = True
                setup.tick_quality_reason = "tick_quality_disabled"
            return {
                "passed": True,
                "reason": "tick_quality_disabled",
                "details": self._tick_quality_metadata(setup),
            }

        bid = self._float(tick.bid)
        ask = self._float(tick.ask)
        with self._lock:
            previous_bid = setup.tick_quality_last_bid
            if setup.tick_quality_started_at is None:
                setup.tick_quality_started_at = now
                setup.tick_quality_count = 0
            setup.tick_quality_count += 1
            setup.tick_quality_last_at = now
            setup.tick_quality_last_bid = bid if bid > 0 else None
            setup.tick_quality_last_ask = ask if ask > 0 else None
            count = setup.tick_quality_count
            started_at = setup.tick_quality_started_at

        min_ticks = max(1, int(settings.tick_quality_min_ticks_above_trigger))
        hold_seconds = max(0.0, float(settings.tick_quality_hold_seconds))
        if count >= max(min_ticks, int(settings.tick_quality_fast_min_ticks)):
            hold_seconds = min(
                hold_seconds, max(0.0, float(settings.tick_quality_fast_hold_seconds))
            )
        hold_elapsed = (
            max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
        )
        reasons: list[str] = []
        if count < min_ticks:
            reasons.append("tick_quality_waiting_for_more_ticks")
        if hold_elapsed < hold_seconds:
            reasons.append("tick_quality_hold_time_pending")
        if settings.tick_quality_require_bid_progress:
            if bid <= 0 or previous_bid is None:
                reasons.append("tick_quality_bid_progress_pending")
            elif bid < previous_bid:
                reasons.append("tick_quality_bid_not_rising")
        baseline_spread = (
            setup.cached_spread_pct
            if setup.cached_spread_pct > 0
            else settings.max_bid_ask_spread_pct
        )
        max_multiplier = max(1.0, float(settings.tick_quality_max_spread_multiplier))
        if baseline_spread > 0 and spread_pct > baseline_spread * max_multiplier:
            reasons.append("tick_quality_spread_unstable")

        passed = not reasons
        reason = (
            "tick_quality_confirmed" if passed else "; ".join(dict.fromkeys(reasons))
        )
        with self._lock:
            setup.tick_quality_confirmed = passed
            setup.tick_quality_reason = reason
        return {
            "passed": passed,
            "reason": reason,
            "details": {
                **self._tick_quality_metadata(setup),
                "executable_price": round(executable_price, 2),
                "spread_pct": round(spread_pct, 3),
                "previous_bid": previous_bid,
                "bid": bid if bid > 0 else None,
                "ask": ask if ask > 0 else None,
                "required_ticks": min_ticks,
                "hold_seconds_required": hold_seconds,
                "hold_seconds_elapsed": round(hold_elapsed, 3),
            },
        }

    def _tick_quality_metadata(self, setup: ArmedEntrySetup) -> dict[str, Any]:
        started_at = setup.tick_quality_started_at
        last_at = setup.tick_quality_last_at
        return {
            "enabled": settings.enable_tick_quality_confirmation,
            "confirmed": setup.tick_quality_confirmed,
            "reason": setup.tick_quality_reason,
            "ticks_above_trigger": setup.tick_quality_count,
            "started_at": started_at.isoformat(sep=" ") if started_at else None,
            "last_at": last_at.isoformat(sep=" ") if last_at else None,
            "last_bid": setup.tick_quality_last_bid,
            "last_ask": setup.tick_quality_last_ask,
            "min_ticks_above_trigger": settings.tick_quality_min_ticks_above_trigger,
            "hold_seconds": settings.tick_quality_hold_seconds,
            "require_bid_progress": settings.tick_quality_require_bid_progress,
            "max_spread_multiplier": settings.tick_quality_max_spread_multiplier,
        }

    def _reset_tick_quality_locked(self, setup: ArmedEntrySetup) -> None:
        setup.tick_quality_count = 0
        setup.tick_quality_started_at = None
        setup.tick_quality_last_at = None
        setup.tick_quality_last_bid = None
        setup.tick_quality_last_ask = None
        setup.tick_quality_confirmed = False
        setup.tick_quality_reason = ""

    def _subscribe_token(self, token: int, *, setup_id: str) -> dict[str, Any]:
        if self.websocket_price_feed is None:
            return {"subscribed": [], "reason": "websocket_feed_missing"}
        subscribe = getattr(self.websocket_price_feed, "subscribe", None)
        if not callable(subscribe):
            return {"subscribed": [], "reason": "websocket_subscribe_unavailable"}
        register_symbol = getattr(
            self.websocket_price_feed, "register_token_symbol", None
        )
        if callable(register_symbol):
            setup = self._setups.get(setup_id)
            if setup is not None:
                register_symbol(int(token), setup.tradingsymbol)
        try:
            try:
                return dict(
                    subscribe({int(token)}, owner=f"armed:{setup_id}", mode="full")
                )
            except TypeError:
                return dict(subscribe({int(token)}))
        except Exception as exc:
            return {
                "subscribed": [],
                "reason": "subscription_failed",
                "message": str(exc),
            }

    def _release_subscription(self, setup: ArmedEntrySetup) -> None:
        if self.websocket_price_feed is None:
            return
        release = getattr(self.websocket_price_feed, "release_owner", None)
        if not callable(release):
            return
        try:
            release(f"armed:{setup.setup_id}", {setup.instrument_token})
        except Exception:
            logger.exception(
                "failed_to_release_armed_subscription setup_id=%s", setup.setup_id
            )

    def _market_session(self) -> str:
        if self.market_session_provider is not None:
            try:
                return str(self.market_session_provider())
            except TypeError:
                return str(self.market_session_provider(self.clock()))
        now = self.clock().replace(tzinfo=None)
        if now.weekday() >= 5:
            return "WEEKEND"
        open_hour, open_minute = [
            int(part) for part in settings.market_open_time.split(":")
        ]
        close_hour, close_minute = [
            int(part) for part in settings.market_close_time.split(":")
        ]
        market_open = now.replace(
            hour=open_hour, minute=open_minute, second=0, microsecond=0
        )
        market_close = now.replace(
            hour=close_hour, minute=close_minute, second=0, microsecond=0
        )
        return (
            "REGULAR_MARKET" if market_open <= now <= market_close else "MARKET_CLOSED"
        )

    def _to_dict(self, setup: ArmedEntrySetup) -> dict[str, Any]:
        payload = asdict(setup)
        for key in (
            "armed_at",
            "valid_until",
            "entered_at",
            "cancelled_at",
            "risk_preflight_at",
        ):
            value = payload.get(key)
            payload[key] = (
                value.isoformat(sep=" ") if isinstance(value, datetime) else value
            )
        payload.pop("factor_scores", None)
        payload["selected_option"] = {
            "tradingsymbol": setup.tradingsymbol,
            "instrument_token": setup.instrument_token,
            "option_type": setup.option_type,
            "strike": setup.strike,
            "expiry": setup.expiry,
        }
        payload["current_premium"] = setup.latest_premium
        payload["tick_quality"] = self._tick_quality_metadata(setup)
        payload["subscription_health"] = self._subscription_health(setup)
        payload["subscription_state"] = payload["subscription_health"]["state"]
        payload["websocket_tracking_enabled"] = payload["subscription_health"][
            "tracking_ready"
        ]
        payload["distance_to_trigger_pct"] = (
            round(
                (
                    (setup.entry_trigger_price - float(setup.latest_premium or 0.0))
                    / max(setup.entry_trigger_price, 0.01)
                )
                * 100,
                3,
            )
            if setup.latest_premium
            else None
        )
        return payload

    def _subscription_health(
        self, setup: ArmedEntrySetup, result: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = result or {}
        reason = str(result.get("reason") or "")
        if reason in {
            "subscription_failed",
            "websocket_feed_missing",
            "websocket_subscribe_unavailable",
            "token_missing",
        }:
            return {
                "state": "failed",
                "tracking_ready": False,
                "live_verified": False,
                "owner_registered": False,
                "reason": reason,
            }
        live_verified = False
        desired = False
        owner_registered = False
        if self.websocket_price_feed is not None:
            live = getattr(self.websocket_price_feed, "is_live_verified", None)
            subscribed = getattr(self.websocket_price_feed, "is_subscribed", None)
            context = getattr(self.websocket_price_feed, "subscription_context", None)
            try:
                live_verified = (
                    bool(live(setup.instrument_token)) if callable(live) else False
                )
                desired = (
                    bool(subscribed(setup.instrument_token))
                    if callable(subscribed)
                    else bool(result.get("subscribed"))
                )
                if callable(context):
                    owners = context(setup.instrument_token).get("owners", [])
                    owner_registered = f"armed:{setup.setup_id}" in owners
                else:
                    owner_registered = bool(result.get("subscribed"))
            except Exception:
                logger.exception(
                    "armed_entry_subscription_health_failed setup_id=%s", setup.setup_id
                )
        queued = bool(result.get("queued")) or (
            desired
            and not bool(getattr(self.websocket_price_feed, "connected", False))
            and not result.get("subscribed")
        )
        if live_verified:
            state = "live_verified"
        elif queued:
            state = "queued_waiting_for_websocket"
        elif desired or result.get("subscribed"):
            state = "subscribed_waiting_for_first_fresh_tick"
        else:
            state = "unknown"
        return {
            "state": state,
            "tracking_ready": state
            in {"live_verified", "subscribed_waiting_for_first_fresh_tick"},
            "live_verified": live_verified,
            "owner_registered": owner_registered,
            "desired": desired,
            "reason": reason or None,
        }

    def _persist(self, setup: ArmedEntrySetup) -> None:
        if self.armed_entry_repository is None:
            return
        try:
            self.armed_entry_repository.upsert(asdict(setup))
        except Exception:
            logger.exception(
                "armed_entry_persistence_failed setup_id=%s", setup.setup_id
            )

    def _key(
        self, *, order_mode: str, action: str, token: int, expiry: str, strike: float
    ) -> str:
        return "|".join(
            [
                settings.strategy_version,
                str(order_mode).lower(),
                action.upper(),
                str(token),
                str(expiry),
                str(strike),
            ]
        )

    def _spread_pct(
        self, *, bid: float, ask: float, price: float, fallback: float
    ) -> float:
        if bid > 0 and ask > 0:
            return ((ask - bid) / max(price, 0.01)) * 100
        return fallback if fallback > 0 else 100.0

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None
