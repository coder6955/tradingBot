from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import Signal
from app.services.database import SetupEpisodeRecord, get_session
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


class EpisodeReservationService:
    """Transaction-safe episode identity and pre-order reservation authority."""

    CANONICAL_STATES = (
        "UNAVAILABLE",
        "OBSERVE",
        "PREPARED",
        "ARMED",
        "TRIGGERED",
        "ORDER_PENDING",
        "OPEN",
        "EXITING",
        "CLOSED",
        "INVALIDATED",
        "EXPIRED",
    )
    CANONICAL_TRANSITIONS = {
        "UNAVAILABLE": {"OBSERVE", "PREPARED", "INVALIDATED"},
        "OBSERVE": {"PREPARED", "INVALIDATED", "EXPIRED"},
        "PREPARED": {"ARMED", "TRIGGERED", "INVALIDATED", "EXPIRED"},
        "ARMED": {"TRIGGERED", "INVALIDATED", "EXPIRED"},
        "TRIGGERED": {"PREPARED", "ORDER_PENDING", "INVALIDATED", "EXPIRED"},
        "ORDER_PENDING": {"PREPARED", "OPEN", "INVALIDATED"},
        "OPEN": {"EXITING", "CLOSED"},
        "EXITING": {"OPEN", "CLOSED"},
        "CLOSED": set(),
        "INVALIDATED": set(),
        "EXPIRED": set(),
    }

    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    ORDER_PENDING = "ORDER_PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"

    ALLOWED_TRANSITIONS = {
        AVAILABLE: {RESERVED, INVALIDATED, EXPIRED},
        RESERVED: {AVAILABLE, ORDER_PENDING, INVALIDATED, EXPIRED},
        ORDER_PENDING: {AVAILABLE, OPEN, INVALIDATED},
        OPEN: {CLOSED},
        CLOSED: set(),
        INVALIDATED: set(),
        EXPIRED: set(),
    }
    PERSISTED_TO_CANONICAL = {
        AVAILABLE: "PREPARED",
        RESERVED: "TRIGGERED",
        ORDER_PENDING: "ORDER_PENDING",
        OPEN: "OPEN",
        CLOSED: "CLOSED",
        INVALIDATED: "INVALIDATED",
        EXPIRED: "EXPIRED",
    }

    def identity(self, signal: Signal, *, metadata: dict[str, Any] | None = None) -> dict[str, str]:
        details = dict(metadata or {})
        factors = signal.factor_scores if isinstance(signal.factor_scores, dict) else {}
        strategy = factors.get("strategy_metadata") if isinstance(factors.get("strategy_metadata"), dict) else {}
        family = factors.get("setup_family") if isinstance(factors.get("setup_family"), dict) else {}
        lineage = current_strategy_lineage()
        strategy_version = str(strategy.get("strategy_version") or lineage["strategy_version"])
        config_hash = str(strategy.get("config_hash") or lineage["config_hash"])
        setup_family = str(family.get("name") or signal.setup_type or "unclassified")
        trigger_price = self._float(details.get("trigger_price") or factors.get("entry_trigger_price") or signal.entry_price)
        trigger_identifier = str(
            details.get("trigger_identifier")
            or details.get("armed_setup_id")
            or details.get("setup_id")
            or f"{setup_family}:{round(trigger_price, 2)}"
        )
        generated_at = self._datetime(details.get("setup_generated_at") or details.get("armed_at")) or ist_now_naive()
        window = max(1, int(settings.setup_episode_window_seconds))
        bucket = int(generated_at.timestamp()) // window
        token = signal.instrument_token or (factors.get("contract", {}).get("instrument_token") if isinstance(factors.get("contract"), dict) else None)
        raw = "|".join(
            [
                str(details.get("trading_date") or generated_at.date().isoformat()),
                strategy_version,
                str(settings.risk_policy_version),
                signal.action.upper(),
                str(token or signal.tradingsymbol or "unknown").upper(),
                setup_family,
                trigger_identifier,
                str(round(trigger_price, 2)),
                str(bucket),
            ]
        )
        return {
            "episode_key": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            "setup_family": setup_family,
            "trigger_identifier": trigger_identifier,
        }

    def reserve(self, signal: Signal, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        identity = self.identity(signal, metadata=metadata)
        now = ist_now_naive()
        expires = now + timedelta(seconds=max(1, int(settings.setup_episode_reservation_seconds)))
        reservation_token = uuid.uuid4().hex
        session = get_session()
        try:
            record = session.query(SetupEpisodeRecord).filter(SetupEpisodeRecord.episode_key == identity["episode_key"]).first()
            if record is None:
                record = SetupEpisodeRecord(
                    created_at=now,
                    updated_at=now,
                    episode_key=identity["episode_key"],
                    symbol=signal.symbol.upper(),
                    action=signal.action.upper(),
                    side=signal.side.upper(),
                    tradingsymbol=signal.tradingsymbol,
                    expiry=signal.expiry,
                    strike=signal.strike,
                    trigger_price=self._float((metadata or {}).get("trigger_price") or signal.entry_price),
                    strategy_version=identity["strategy_version"],
                    config_hash=identity["config_hash"],
                    setup_family=identity["setup_family"],
                    trigger_identifier=identity["trigger_identifier"],
                    risk_policy_version=str(settings.risk_policy_version),
                    state=self.RESERVED,
                    reservation_token=reservation_token,
                    reserved_at=now,
                    reservation_expires_at=expires,
                    last_transition_reason="pre_order_reservation",
                )
                session.add(record)
                try:
                    session.commit()
                    session.refresh(record)
                    return self._result(record, acquired=True)
                except IntegrityError:
                    session.rollback()
                    record = session.query(SetupEpisodeRecord).filter(SetupEpisodeRecord.episode_key == identity["episode_key"]).first()

            if record is None:
                return {**identity, "acquired": False, "reason": "EPISODE_RESERVATION_FAILED"}
            expired_reservation = record.state == self.RESERVED and record.reservation_expires_at and record.reservation_expires_at <= now
            reservable = record.state == self.AVAILABLE or expired_reservation
            if reservable:
                previous_state = record.state
                updated = (
                    session.query(SetupEpisodeRecord)
                    .filter(SetupEpisodeRecord.id == record.id, SetupEpisodeRecord.state == previous_state)
                    .update(
                        {
                            SetupEpisodeRecord.state: self.RESERVED,
                            SetupEpisodeRecord.reservation_token: reservation_token,
                            SetupEpisodeRecord.reserved_at: now,
                            SetupEpisodeRecord.reservation_expires_at: expires,
                            SetupEpisodeRecord.updated_at: now,
                            SetupEpisodeRecord.last_transition_reason: "expired_reservation_reclaimed" if expired_reservation else "pre_order_reservation",
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if updated == 1:
                    refreshed = session.get(SetupEpisodeRecord, record.id)
                    return self._result(refreshed, acquired=True)
            return self._result(record, acquired=False, reason=f"DUPLICATE_EPISODE_{record.state}")
        finally:
            session.close()

    def status(self, episode_key: str) -> dict[str, Any] | None:
        session = get_session()
        try:
            record = session.query(SetupEpisodeRecord).filter(SetupEpisodeRecord.episode_key == episode_key).first()
            return self._result(record, acquired=False) if record is not None else None
        finally:
            session.close()

    def mark_order_pending(self, episode_key: str, reservation_token: str) -> bool:
        return self._transition(episode_key, self.RESERVED, self.ORDER_PENDING, reservation_token, "order_submission_started")

    def mark_open(self, episode_key: str, reservation_token: str, *, trade_id: int | None = None) -> bool:
        return self._transition(episode_key, self.ORDER_PENDING, self.OPEN, reservation_token, "order_accepted", active_trade_id=trade_id)

    def release(self, episode_key: str, reservation_token: str, *, reason: str) -> bool:
        session = get_session()
        try:
            updated = (
                session.query(SetupEpisodeRecord)
                .filter(
                    SetupEpisodeRecord.episode_key == episode_key,
                    SetupEpisodeRecord.reservation_token == reservation_token,
                    SetupEpisodeRecord.state.in_([self.RESERVED, self.ORDER_PENDING]),
                )
                .update(
                    {
                        SetupEpisodeRecord.state: self.AVAILABLE,
                        SetupEpisodeRecord.reservation_token: None,
                        SetupEpisodeRecord.reserved_at: None,
                        SetupEpisodeRecord.reservation_expires_at: None,
                        SetupEpisodeRecord.updated_at: ist_now_naive(),
                        SetupEpisodeRecord.last_transition_reason: reason,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return updated == 1
        finally:
            session.close()

    def close(self, episode_key: str, *, reason: str = "position_closed") -> bool:
        return self._transition(episode_key, self.OPEN, self.CLOSED, None, reason)

    def validate_transition(self, current: str, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(str(current), set())

    def canonical_state(self, persisted_state: str) -> str:
        """Map transitional persisted labels into the shared lifecycle vocabulary."""
        return self.PERSISTED_TO_CANONICAL.get(str(persisted_state), str(persisted_state))

    def validate_canonical_transition(self, current: str, target: str) -> bool:
        return str(target) in self.CANONICAL_TRANSITIONS.get(str(current), set())

    def _transition(
        self,
        episode_key: str,
        current: str,
        target: str,
        reservation_token: str | None,
        reason: str,
        *,
        active_trade_id: int | None = None,
    ) -> bool:
        if not self.validate_transition(current, target):
            return False
        session = get_session()
        try:
            query = session.query(SetupEpisodeRecord).filter(SetupEpisodeRecord.episode_key == episode_key, SetupEpisodeRecord.state == current)
            if reservation_token is not None:
                query = query.filter(SetupEpisodeRecord.reservation_token == reservation_token)
            values: dict[Any, Any] = {
                SetupEpisodeRecord.state: target,
                SetupEpisodeRecord.updated_at: ist_now_naive(),
                SetupEpisodeRecord.last_transition_reason: reason,
            }
            if active_trade_id is not None:
                values[SetupEpisodeRecord.active_trade_id] = active_trade_id
            updated = query.update(values, synchronize_session=False)
            session.commit()
            return updated == 1
        finally:
            session.close()

    def _result(self, record: SetupEpisodeRecord | None, *, acquired: bool, reason: str | None = None) -> dict[str, Any]:
        return {
            "episode_key": getattr(record, "episode_key", None),
            "episode_id": getattr(record, "id", None),
            "state": getattr(record, "state", None),
            "canonical_state": self.canonical_state(getattr(record, "state", "")),
            "reservation_token": getattr(record, "reservation_token", None) if acquired else None,
            "acquired": acquired,
            "reason": reason,
        }

    def _datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                return None
        return None

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
