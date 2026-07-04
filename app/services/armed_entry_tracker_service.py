from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from app.config import settings
from app.models import Signal
from app.services.entry_timing_service import EntryTimingService
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.risk_management_service import RiskManagementService
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import OptionContract


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
    probability: float
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
    entered_trade: dict[str, Any] | None = None
    entered_at: datetime | None = None
    cancelled_at: datetime | None = None
    rejection_saved: bool = False
    factor_scores: dict[str, Any] = field(default_factory=dict)


class ArmedEntryTrackerService:
    """Track near-trigger Bank Nifty option setups and fire paper entries from ticks."""

    ACTIVE_STATES = {EntryTimingService.ARMED_FOR_ENTRY, EntryTimingService.ENTER_NOW}
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
    ) -> None:
        self.order_service_factory = order_service_factory
        self.websocket_price_feed = websocket_price_feed
        self.rejected_opportunity_repository = rejected_opportunity_repository or RejectedOpportunityRepository()
        self.risk_management_service = risk_management_service or RiskManagementService()
        self.market_session_provider = market_session_provider
        self.clock = clock or ist_now_naive
        self._setups: dict[str, ArmedEntrySetup] = {}
        self._setup_id_by_key: dict[str, str] = {}
        self._lock = RLock()
        self.last_triggered_at: datetime | None = None
        self.last_reason: str | None = None

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
        probability: float,
        confidence: float,
        quantity: int,
        factor_scores: dict[str, Any],
        order_mode: str,
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        if not settings.enable_event_driven_paper_entry and str(order_mode).lower() == "paper":
            return {"registered": False, "reason": "event_driven_paper_entry_disabled"}
        token = self._int(contract.instrument_token)
        trigger = self._float(entry_timing.get("entry_trigger_price"))
        current = self._float(entry_timing.get("current_premium") or contract.ask or contract.last_price)
        if token is None or trigger <= 0 or current <= 0:
            return {"registered": False, "reason": "armed_entry_inputs_missing"}

        now = self.clock().replace(tzinfo=None)
        valid_until = now + timedelta(seconds=max(5, settings.armed_entry_valid_seconds))
        key = self._key(order_mode=order_mode, action=action, token=token, expiry=contract.expiry, strike=contract.strike)
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
                probability=float(probability or 0.0),
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
                factor_scores=dict(factor_scores or {}),
            )
            if existing_id and existing_id in self._setups and self._setups[existing_id].latest_state in self.TERMINAL_STATES:
                setup.setup_id = f"armed-{uuid4().hex[:12]}"
            self._setups[setup.setup_id] = setup
            self._setup_id_by_key[key] = setup.setup_id

        subscribe_result = self._subscribe_token(token)
        payload = self._to_dict(setup)
        payload.update(
            {
                "registered": True,
                "websocket_tracking_enabled": bool(settings.enable_kite_websocket),
                "paper_event_entry_enabled": settings.enable_event_driven_paper_entry,
                "live_event_entry_blocked": setup.order_mode == "live" and not settings.enable_event_driven_live_entry,
                "subscription": subscribe_result,
            }
        )
        logger.info("armed_entry_registered %s", {"setup_id": setup.setup_id, "tradingsymbol": setup.tradingsymbol, "trigger": setup.entry_trigger_price})
        return payload

    def on_tick(self, tick: WebSocketTick) -> list[dict[str, Any]]:
        token = self._int(tick.instrument_token)
        if token is None:
            return []
        triggered: list[dict[str, Any]] = []
        with self._lock:
            setup_ids = [
                setup.setup_id
                for setup in self._setups.values()
                if setup.instrument_token == token and setup.latest_state in self.ACTIVE_STATES
            ]
        for setup_id in setup_ids:
            result = self.evaluate_tick(setup_id, tick)
            if result:
                triggered.append(result)
        return triggered

    def evaluate_tick(self, setup_id: str, tick: WebSocketTick) -> dict[str, Any] | None:
        with self._lock:
            setup = self._setups.get(setup_id)
            if setup is None or setup.latest_state in self.TERMINAL_STATES:
                return None

        executable = float(tick.ask or tick.price)
        bid = self._float(tick.bid)
        ask = self._float(tick.ask)
        spread_pct = self._spread_pct(bid=bid, ask=ask, price=tick.price, fallback=setup.cached_spread_pct)
        now = self.clock().replace(tzinfo=None)
        with self._lock:
            setup.latest_premium = executable
            setup.latest_bid = bid or None
            setup.latest_ask = ask or None
            setup.latest_spread_pct = spread_pct

        if now > setup.valid_until:
            return self._mark_rejected(setup.setup_id, "EXPIRED", ["armed_setup_expired"])

        if executable < setup.entry_trigger_price:
            with self._lock:
                setup.latest_state = EntryTimingService.ARMED_FOR_ENTRY
                setup.latest_reason = "waiting_for_entry_trigger"
            return self._to_dict(setup)

        checks = self._entry_checks(setup, executable_price=executable, spread_pct=spread_pct)
        if checks:
            return self._mark_rejected(setup.setup_id, "TOO_LATE", checks)

        session = self._market_session()
        if session != "REGULAR_MARKET":
            return self._mark_rejected(setup.setup_id, "CANCELLED", ["event_entry_hard_gate_failed", "market_closed"])

        if setup.order_mode == "live":
            with self._lock:
                setup.latest_state = EntryTimingService.ARMED_FOR_ENTRY
                setup.latest_reason = "live_trading_not_enabled_for_event_entry"
                self.last_reason = setup.latest_reason
            return {**self._to_dict(setup), "live_event_entry_blocked": True, "reason": setup.latest_reason}

        if setup.order_mode != "paper":
            return self._mark_rejected(setup.setup_id, "CANCELLED", ["event_entry_hard_gate_failed", "unsupported_order_mode"])

        if not settings.enable_event_driven_paper_entry:
            return self._mark_rejected(setup.setup_id, "CANCELLED", ["event_entry_hard_gate_failed", "event_driven_paper_entry_disabled"])

        risk = self.risk_management_service.evaluate_signal(setup.symbol)
        if not risk.get("passed", False):
            return self._mark_rejected(setup.setup_id, "CANCELLED", ["event_entry_hard_gate_failed", *[str(reason) for reason in risk.get("reasons", [])]])

        return self._enter_paper(setup.setup_id, executable_price=executable, tick=tick, spread_pct=spread_pct)

    def cancel(self, setup_id: str, reason: str = "cancelled") -> dict[str, Any]:
        return self._mark_rejected(setup_id, "CANCELLED", [reason])

    def cancel_for_data_gap(self, event: dict[str, Any] | None = None) -> dict[str, Any]:
        if not settings.cancel_armed_entries_on_data_gap:
            return {"cancelled": 0, "reason": "cancel_on_gap_disabled"}
        reason = "websocket_data_gap_detected"
        if event and event.get("reason"):
            reason = str(event.get("reason"))
        with self._lock:
            setup_ids = [setup.setup_id for setup in self._setups.values() if setup.latest_state in self.ACTIVE_STATES]
        results = [
            self._mark_rejected(setup_id, "CANCELLED", ["event_entry_hard_gate_failed", "data_gap_detected", reason])
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
            "active": [row for row in rows if row["latest_state"] in self.ACTIVE_STATES],
            "expired": [row for row in rows if row["latest_state"] == "EXPIRED"],
            "entered_paper": [row for row in rows if row["latest_state"] == "ENTERED_PAPER"],
            "too_late": [row for row in rows if row["latest_state"] == "TOO_LATE"],
            "cancelled": [row for row in rows if row["latest_state"] == "CANCELLED"],
        }

    def status(self) -> dict[str, Any]:
        entries = self.list_entries()
        active = entries["active"]
        return {
            "event_entry_enabled": settings.enable_event_driven_paper_entry,
            "event_entry_live_enabled": settings.enable_event_driven_live_entry,
            "armed_entry_count": len(active),
            "armed_entry_tokens": sorted(self.active_tokens()),
            "event_entry_last_triggered_at": self.last_triggered_at.isoformat(sep=" ") if self.last_triggered_at else None,
            "event_entry_last_reason": self.last_reason,
        }

    def active_tokens(self) -> set[int]:
        with self._lock:
            return {setup.instrument_token for setup in self._setups.values() if setup.latest_state in self.ACTIVE_STATES}

    def expire_stale(self) -> None:
        now = self.clock().replace(tzinfo=None)
        expired: list[str] = []
        with self._lock:
            for setup in self._setups.values():
                if setup.latest_state in self.ACTIVE_STATES and now > setup.valid_until:
                    expired.append(setup.setup_id)
        for setup_id in expired:
            self._mark_rejected(setup_id, "EXPIRED", ["armed_setup_expired"])

    def _enter_paper(self, setup_id: str, *, executable_price: float, tick: WebSocketTick, spread_pct: float) -> dict[str, Any]:
        with self._lock:
            setup = self._setups.get(setup_id)
            if setup is None or setup.latest_state in self.TERMINAL_STATES:
                return {"entered": False, "reason": "armed_setup_not_active", "setup_id": setup_id}
            setup.latest_state = EntryTimingService.ENTER_NOW
            setup.latest_reason = "event_driven_entry_triggered"

        if self.order_service_factory is None:
            return self._mark_rejected(setup_id, "CANCELLED", ["event_entry_hard_gate_failed", "order_service_unavailable"])

        metadata = self._entry_metadata(setup, executable_price=executable_price, spread_pct=spread_pct)
        signal = self._signal_for_entry(setup, executable_price=executable_price, metadata=metadata)
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
        logger.info("armed_entry_entered_paper %s", {"setup_id": setup_id, "tradingsymbol": setup.tradingsymbol, "entry": executable_price})
        return self._to_dict(self._setups[setup_id])

    def _entry_checks(self, setup: ArmedEntrySetup, *, executable_price: float, spread_pct: float) -> list[str]:
        reasons: list[str] = []
        chase_pct = ((executable_price - setup.entry_trigger_price) / max(setup.entry_trigger_price, 0.01)) * 100
        move_from_base_pct = ((executable_price - setup.current_premium_at_arming) / max(setup.current_premium_at_arming, 0.01)) * 100
        target1_room_pct = ((setup.target_1 - executable_price) / max(executable_price, 0.01)) * 100 if setup.target_1 > executable_price else 0.0
        risk = executable_price - setup.stop_loss
        reward = setup.target_1 - executable_price
        remaining_rr = reward / risk if risk > 0 and reward > 0 else 0.0
        if chase_pct > setup.max_entry_chase_pct:
            reasons.extend(["entry_too_late", "chase_risk_high"])
        if move_from_base_pct > setup.max_premium_move_from_base_pct:
            reasons.extend(["entry_too_late", "chase_risk_high"])
        if target1_room_pct < setup.min_target1_room_pct:
            reasons.append("insufficient_target_room_after_entry")
        if remaining_rr < setup.min_remaining_risk_reward:
            reasons.append("remaining_rr_compressed")
        if spread_pct > settings.max_bid_ask_spread_pct:
            reasons.append("spread_widened_after_trigger")
        return list(dict.fromkeys(reasons))

    def _mark_rejected(self, setup_id: str, state: str, reasons: list[str]) -> dict[str, Any]:
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
        if should_save:
            self._save_rejection(setup, reasons)
        logger.info("armed_entry_%s %s", state.lower(), {"setup_id": setup_id, "reasons": reasons})
        return self._to_dict(setup)

    def _save_rejection(self, setup: ArmedEntrySetup, reasons: list[str]) -> None:
        try:
            contract = SimpleNamespace(
                tradingsymbol=setup.tradingsymbol,
                exchange=setup.exchange,
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
            logger.warning("failed_to_save_armed_entry_rejection setup_id=%s error=%s", setup.setup_id, exc)

    def _signal_for_entry(self, setup: ArmedEntrySetup, *, executable_price: float, metadata: dict[str, Any]) -> Signal:
        risk = executable_price - setup.stop_loss
        reward = setup.target_1 - executable_price
        rr = reward / risk if risk > 0 and reward > 0 else setup.risk_reward
        factor_scores = {
            **setup.factor_scores,
            "entry_source": "event_driven_websocket",
            "armed_entry": metadata,
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

    def _entry_metadata(self, setup: ArmedEntrySetup, *, executable_price: float, spread_pct: float) -> dict[str, Any]:
        now = self.clock().replace(tzinfo=None)
        delay = max(0.0, (now - setup.armed_at).total_seconds())
        chase_pct = ((executable_price - setup.entry_trigger_price) / max(setup.entry_trigger_price, 0.01)) * 100
        move_from_base_pct = ((executable_price - setup.current_premium_at_arming) / max(setup.current_premium_at_arming, 0.01)) * 100
        return {
            "entry_source": "event_driven_websocket",
            "armed_setup_id": setup.setup_id,
            "trigger_price": round(setup.entry_trigger_price, 2),
            "executable_entry_price": round(executable_price, 2),
            "entry_delay_seconds": round(delay, 3),
            "chase_pct": round(chase_pct, 3),
            "premium_move_from_base_pct": round(move_from_base_pct, 3),
            "spread_pct": round(spread_pct, 3),
        }

    def _subscribe_token(self, token: int) -> dict[str, Any]:
        if self.websocket_price_feed is None:
            return {"subscribed": [], "reason": "websocket_feed_missing"}
        subscribe = getattr(self.websocket_price_feed, "subscribe", None)
        if not callable(subscribe):
            return {"subscribed": [], "reason": "websocket_subscribe_unavailable"}
        try:
            return dict(subscribe({int(token)}))
        except Exception as exc:
            return {"subscribed": [], "reason": "subscription_failed", "message": str(exc)}

    def _market_session(self) -> str:
        if self.market_session_provider is not None:
            try:
                return str(self.market_session_provider())
            except TypeError:
                return str(self.market_session_provider(self.clock()))
        now = self.clock().replace(tzinfo=None)
        if now.weekday() >= 5:
            return "WEEKEND"
        open_hour, open_minute = [int(part) for part in settings.market_open_time.split(":")]
        close_hour, close_minute = [int(part) for part in settings.market_close_time.split(":")]
        market_open = now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
        market_close = now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        return "REGULAR_MARKET" if market_open <= now <= market_close else "MARKET_CLOSED"

    def _to_dict(self, setup: ArmedEntrySetup) -> dict[str, Any]:
        payload = asdict(setup)
        for key in ("armed_at", "valid_until", "entered_at", "cancelled_at"):
            value = payload.get(key)
            payload[key] = value.isoformat(sep=" ") if isinstance(value, datetime) else value
        payload.pop("factor_scores", None)
        payload["selected_option"] = {
            "tradingsymbol": setup.tradingsymbol,
            "instrument_token": setup.instrument_token,
            "option_type": setup.option_type,
            "strike": setup.strike,
            "expiry": setup.expiry,
        }
        payload["current_premium"] = setup.latest_premium
        payload["distance_to_trigger_pct"] = (
            round(((setup.entry_trigger_price - float(setup.latest_premium or 0.0)) / max(setup.entry_trigger_price, 0.01)) * 100, 3)
            if setup.latest_premium
            else None
        )
        return payload

    def _key(self, *, order_mode: str, action: str, token: int, expiry: str, strike: float) -> str:
        return "|".join([settings.strategy_version, str(order_mode).lower(), action.upper(), str(token), str(expiry), str(strike)])

    def _spread_pct(self, *, bid: float, ask: float, price: float, fallback: float) -> float:
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
