from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_

from app.config import settings
from app.models import Signal
from app.services.database import SetupEpisodeRecord, TradeRecord, get_session
from app.services.realistic_pnl_service import RealisticPnlService
from app.services.time_utils import format_ist, ist_now_naive, ist_today
from app.services.strategy_lineage_service import current_strategy_lineage


class TradeRepository:
    """Persist actual paper/live trade lifecycle events separately from opportunities."""

    def __init__(self, pnl_service: RealisticPnlService | None = None) -> None:
        self.pnl_service = pnl_service or RealisticPnlService()

    def create_trade(
        self,
        signal: Signal,
        *,
        mode: str,
        status: str,
        requested_quantity: int,
        placed_quantity: int,
        order_response: dict[str, Any] | None = None,
        broker_order_id: str | None = None,
        opportunity_id: int | None = None,
        notes: str | None = None,
    ) -> TradeRecord:
        session = get_session()
        try:
            response = order_response or asdict(signal)
            if isinstance(response, dict):
                response = dict(response)
                response.setdefault("signal_factor_scores", signal.factor_scores)
                response.setdefault("setup_type", signal.setup_type)
                family = signal.factor_scores.get("setup_family") if isinstance(signal.factor_scores, dict) else None
                if isinstance(family, dict):
                    response.setdefault("setup_family", family)
                    response.setdefault("setup_family_name", family.get("name"))
                    response.setdefault("setup_family_group", family.get("group"))
            paper_entry_price = float(response.get("entry_price") or signal.entry_price or 0.0) if isinstance(response, dict) else float(signal.entry_price or 0.0)
            initial_entry_price = paper_entry_price if mode == "paper" else float(signal.entry_price or 0.0)
            now = ist_now_naive()
            lineage = current_strategy_lineage()
            factors = signal.factor_scores if isinstance(signal.factor_scores, dict) else {}
            risk_decision = factors.get("risk_decision") if isinstance(factors.get("risk_decision"), dict) else {}
            episode = factors.get("episode") if isinstance(factors.get("episode"), dict) else {}
            record = TradeRecord(
                opportunity_id=opportunity_id,
                symbol=signal.symbol,
                tradingsymbol=str(signal.tradingsymbol or signal.symbol),
                exchange=signal.exchange,
                instrument_token=signal.instrument_token,
                action=signal.action,
                side=signal.side,
                mode=mode,
                status=status,
                broker_order_id=broker_order_id,
                requested_quantity=requested_quantity,
                placed_quantity=placed_quantity,
                filled_quantity=placed_quantity if mode == "paper" else 0,
                remaining_quantity=placed_quantity,
                entry_price=paper_entry_price if mode == "paper" else signal.entry_price,
                average_price=paper_entry_price if mode == "paper" else None,
                highest_price_during_trade=initial_entry_price if initial_entry_price > 0 else None,
                lowest_price_during_trade=initial_entry_price if initial_entry_price > 0 else None,
                mfe_points=0.0 if initial_entry_price > 0 else None,
                mfe_percent=0.0 if initial_entry_price > 0 else None,
                mae_points=0.0 if initial_entry_price > 0 else None,
                mae_percent=0.0 if initial_entry_price > 0 else None,
                time_to_mfe=0.0 if initial_entry_price > 0 else None,
                time_to_mae=0.0 if initial_entry_price > 0 else None,
                mfe_recorded_at=now if initial_entry_price > 0 else None,
                mae_recorded_at=now if initial_entry_price > 0 else None,
                stop_loss=signal.stop_loss,
                target_1=signal.target_1,
                target_2=signal.target_2,
                target_3=signal.target_3,
                order_response_json=json.dumps(response, default=str),
                notes=notes,
                strategy_version=str(lineage["strategy_version"]),
                config_hash=str(lineage["config_hash"]),
                episode_key=str(episode.get("episode_key")) if episode.get("episode_key") else None,
                risk_policy_version=str(risk_decision.get("risk_policy_version")) if risk_decision.get("risk_policy_version") else None,
                approved_risk_tier=str(risk_decision.get("approved_tier")) if risk_decision.get("approved_tier") else None,
                approved_risk_percent=float(risk_decision.get("approved_risk_percent") or 0.0),
                approved_risk_amount=float(risk_decision.get("approved_risk_amount") or 0.0),
                estimated_loss_at_stop=float(risk_decision.get("estimated_total_loss_at_stop") or 0.0),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def list_trades(self, status: str | None = None, limit: int = 100, include_artifacts: bool = False) -> list[TradeRecord]:
        session = get_session()
        try:
            query = session.query(TradeRecord).order_by(TradeRecord.id.desc())
            if not include_artifacts:
                query = query.filter(TradeRecord.mode != "test_artifact")
            if status:
                query = query.filter(TradeRecord.status == status)
            return query.limit(limit).all()
        finally:
            session.close()

    def suspected_test_artifacts(self, limit: int = 100) -> dict[str, Any]:
        session = get_session()
        try:
            rows = self._suspected_test_artifact_query(session).order_by(TradeRecord.id.desc()).limit(limit).all()
            return {
                "count": len(rows),
                "artifacts": [self._artifact_summary(row) for row in rows],
                "criteria": [
                    "opportunity_id is null",
                    "mode is paper/live",
                    "non-BANKNIFTY symbol/tradingsymbol OR broker_order_id=test-order OR target_1 missing OR empty signal_factor_scores",
                ],
            }
        finally:
            session.close()

    def quarantine_suspected_test_artifacts(self, limit: int = 100, dry_run: bool = True) -> dict[str, Any]:
        session = get_session()
        try:
            rows = self._suspected_test_artifact_query(session).order_by(TradeRecord.id.desc()).limit(limit).all()
            artifacts = [self._artifact_summary(row) for row in rows]
            if not dry_run:
                now = ist_now_naive()
                for row in rows:
                    row.mode = "test_artifact"
                    row.status = "invalid_test_data"
                    row.updated_at = now
                    prefix = "quarantined synthetic test artifact"
                    row.notes = f"{prefix}; {row.notes}" if row.notes else prefix
                session.commit()
            return {"dry_run": dry_run, "count": len(artifacts), "artifacts": artifacts}
        finally:
            session.close()

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        session = get_session()
        try:
            return session.get(TradeRecord, trade_id)
        finally:
            session.close()

    def _suspected_test_artifact_query(self, session: Any) -> Any:
        return (
            session.query(TradeRecord)
            .filter(TradeRecord.opportunity_id.is_(None))
            .filter(TradeRecord.mode.in_(["paper", "live"]))
            .filter(
                or_(
                    TradeRecord.symbol != "BANKNIFTY",
                    ~TradeRecord.tradingsymbol.like("BANKNIFTY%"),
                    TradeRecord.broker_order_id == "test-order",
                    TradeRecord.target_1.is_(None),
                    TradeRecord.order_response_json.like('%"signal_factor_scores": {}%'),
                )
            )
        )

    def _artifact_summary(self, row: TradeRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "created_at": format_ist(row.created_at),
            "symbol": row.symbol,
            "tradingsymbol": row.tradingsymbol,
            "mode": row.mode,
            "status": row.status,
            "entry_price": row.entry_price,
            "stop_loss": row.stop_loss,
            "target_1": row.target_1,
            "broker_order_id": row.broker_order_id,
            "opportunity_id": row.opportunity_id,
            "notes": row.notes,
        }

    def update_mfe_mae(
        self,
        trade_id: int,
        *,
        price: float,
        price_timestamp: datetime | None = None,
    ) -> TradeRecord | None:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            if not self._apply_mfe_mae(record, price=price, price_timestamp=price_timestamp):
                return record
            record.updated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def update_broker_status(self, trade_id: int, *, status: str, broker_payload: dict[str, Any], filled_quantity: int | None = None, average_price: float | None = None) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = status
            record.updated_at = ist_now_naive()
            record.broker_status_json = json.dumps(broker_payload, default=str)
            if filled_quantity is not None:
                record.filled_quantity = filled_quantity
            if average_price is not None:
                record.average_price = average_price
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def mark_closing(
        self,
        trade_id: int,
        *,
        outcome: str,
        exit_price: float,
        price_source: str | None = None,
        price_timestamp: datetime | None = None,
        price_age_seconds: float | None = None,
        exit_order_id: str | None = None,
        exit_order_response: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> TradeRecord | None:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            if record.status in {"closed", "closing"}:
                return None
            record.status = "closing"
            record.outcome = outcome
            record.exit_price = exit_price
            record.exit_requested_at = ist_now_naive()
            record.price_source = price_source
            record.price_timestamp = price_timestamp
            record.price_age_seconds = price_age_seconds
            if exit_order_id:
                record.exit_order_id = exit_order_id
            if exit_order_response is not None:
                record.exit_order_response_json = json.dumps(exit_order_response, default=str)
                record.exit_order_status = str(exit_order_response.get("status") or "submitted")
            record.updated_at = ist_now_naive()
            record.notes = notes or record.notes
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def try_mark_closing(
        self,
        trade_id: int,
        *,
        outcome: str,
        exit_price: float,
        price_source: str | None = None,
        price_timestamp: datetime | None = None,
        price_age_seconds: float | None = None,
        exit_rule_first_triggered: str | None = None,
        exit_triggered_rules: list[str] | None = None,
        exit_ltp: float | None = None,
        exit_best_bid: float | None = None,
        exit_best_ask: float | None = None,
        exit_executable_price: float | None = None,
        exit_depth_coverage: float | None = None,
        exit_spread_pct: float | None = None,
        exit_execution_source: str | None = None,
        exit_quote_timestamp: datetime | None = None,
        notes: str | None = None,
    ) -> TradeRecord | None:
        """Atomically claim a trade for live exit submission.

        This is intentionally a conditional DB update, not read-then-write, so
        concurrent exit evaluators cannot submit duplicate square-off orders.
        """
        session = get_session()
        try:
            now = ist_now_naive()
            updated = (
                session.query(TradeRecord)
                .filter(TradeRecord.id == trade_id)
                .filter(~TradeRecord.status.in_(["closing", "closed", "exit_failed", "reconciliation_mismatch"]))
                .filter(func.coalesce(TradeRecord.exit_attempt_count, 0) < settings.live_exit_max_retry_count)
                .update(
                    {
                        TradeRecord.status: "closing",
                        TradeRecord.outcome: outcome,
                        TradeRecord.exit_price: exit_price,
                        TradeRecord.exit_requested_at: now,
                        TradeRecord.price_source: price_source,
                        TradeRecord.price_timestamp: price_timestamp,
                        TradeRecord.price_age_seconds: price_age_seconds,
                        TradeRecord.exit_rule_first_triggered: exit_rule_first_triggered,
                        TradeRecord.exit_triggered_rules_json: json.dumps(exit_triggered_rules or []),
                        TradeRecord.exit_ltp: exit_ltp,
                        TradeRecord.exit_best_bid: exit_best_bid,
                        TradeRecord.exit_best_ask: exit_best_ask,
                        TradeRecord.exit_executable_price: exit_executable_price,
                        TradeRecord.exit_depth_coverage: exit_depth_coverage,
                        TradeRecord.exit_spread_pct: exit_spread_pct,
                        TradeRecord.exit_execution_source: exit_execution_source,
                        TradeRecord.exit_quote_timestamp: exit_quote_timestamp,
                        TradeRecord.exit_attempt_count: func.coalesce(TradeRecord.exit_attempt_count, 0) + 1,
                        TradeRecord.exit_last_error: None,
                        TradeRecord.updated_at: now,
                        TradeRecord.notes: notes,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if updated != 1:
                return None
            record = session.get(TradeRecord, trade_id)
            if record is None:
                return None
            session.refresh(record)
            return record
        finally:
            session.close()

    def update_exit_order_status(
        self,
        trade_id: int,
        *,
        status: str,
        broker_payload: dict[str, Any],
        exit_order_id: str | None = None,
        notes: str | None = None,
    ) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.exit_order_status = status
            if exit_order_id:
                record.exit_order_id = exit_order_id
            record.exit_order_response_json = json.dumps(broker_payload, default=str)
            record.updated_at = ist_now_naive()
            if status.lower() in {"complete", "filled"}:
                record.exit_confirmed_at = ist_now_naive()
            if notes:
                record.notes = notes
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def update_protective_order(
        self,
        trade_id: int,
        *,
        status: str,
        broker_payload: dict[str, Any],
        protective_order_id: str | None = None,
        trigger_price: float | None = None,
        error: str | None = None,
        cancelled: bool = False,
    ) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.protective_order_status = status
            if protective_order_id:
                record.protective_order_id = protective_order_id
            if trigger_price is not None:
                record.protective_trigger_price = trigger_price
            record.protective_order_response_json = json.dumps(broker_payload, default=str)
            record.protective_last_error = error
            if protective_order_id and record.protective_requested_at is None:
                record.protective_requested_at = ist_now_naive()
            if cancelled:
                record.protective_cancelled_at = ist_now_naive()
            record.updated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def mark_exit_failed(self, trade_id: int, *, reason: str, broker_payload: dict[str, Any] | None = None) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = "exit_failed"
            record.exit_order_status = "failed"
            record.exit_order_response_json = json.dumps(broker_payload or {"reason": reason}, default=str)
            record.exit_last_error = reason
            record.notes = reason
            record.updated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def close_trade(
        self,
        trade_id: int,
        *,
        outcome: str,
        exit_price: float,
        notes: str | None = None,
        price_source: str | None = None,
        price_timestamp: datetime | None = None,
        price_age_seconds: float | None = None,
        exit_rule_first_triggered: str | None = None,
        exit_triggered_rules: list[str] | None = None,
        exit_ltp: float | None = None,
        exit_best_bid: float | None = None,
        exit_best_ask: float | None = None,
        exit_executable_price: float | None = None,
        exit_depth_coverage: float | None = None,
        exit_spread_pct: float | None = None,
        exit_execution_source: str | None = None,
        exit_quote_timestamp: datetime | None = None,
        exit_order_id: str | None = None,
        exit_order_status: str | None = None,
        exit_order_response: dict[str, Any] | None = None,
    ) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = "closed"
            record.outcome = outcome
            record.exit_price = exit_price
            record.price_source = price_source or record.price_source
            record.price_timestamp = price_timestamp or record.price_timestamp
            record.price_age_seconds = price_age_seconds if price_age_seconds is not None else record.price_age_seconds
            record.exit_rule_first_triggered = exit_rule_first_triggered or record.exit_rule_first_triggered
            if exit_triggered_rules is not None:
                record.exit_triggered_rules_json = json.dumps(exit_triggered_rules, default=str)
            record.exit_ltp = exit_ltp if exit_ltp is not None else record.exit_ltp
            record.exit_best_bid = exit_best_bid if exit_best_bid is not None else record.exit_best_bid
            record.exit_best_ask = exit_best_ask if exit_best_ask is not None else record.exit_best_ask
            record.exit_executable_price = exit_executable_price if exit_executable_price is not None else record.exit_executable_price
            record.exit_depth_coverage = exit_depth_coverage if exit_depth_coverage is not None else record.exit_depth_coverage
            record.exit_spread_pct = exit_spread_pct if exit_spread_pct is not None else record.exit_spread_pct
            record.exit_execution_source = exit_execution_source or record.exit_execution_source
            record.exit_quote_timestamp = exit_quote_timestamp or record.exit_quote_timestamp
            record.exit_order_id = exit_order_id or record.exit_order_id
            record.exit_order_status = exit_order_status or record.exit_order_status
            record.exit_confirmed_at = ist_now_naive()
            self._apply_mfe_mae(record, price=exit_price, price_timestamp=price_timestamp)
            if exit_order_response is not None:
                record.exit_order_response_json = json.dumps(exit_order_response, default=str)
            record.updated_at = ist_now_naive()
            record.notes = notes or record.notes
            qty = record.remaining_quantity if record.remaining_quantity is not None else (record.filled_quantity or record.placed_quantity)
            if record.average_price is not None:
                pnl = self.pnl_service.calculate(
                    entry_price=float(record.average_price),
                    exit_price=float(exit_price),
                    quantity=int(qty or 0),
                    side=str(record.side),
                    include_slippage=not (settings.enable_execution_realism and str(record.mode).lower() == "paper"),
                    include_spread=not (settings.enable_execution_realism and str(record.mode).lower() == "paper"),
                )
                record.gross_pnl = pnl.gross_pnl
                record.net_pnl = pnl.net_pnl
                record.charges = pnl.charges
                record.slippage_cost = pnl.slippage_cost
                record.spread_cost = pnl.spread_cost
                record.pnl = pnl.net_pnl
                record.remaining_quantity = 0
            if record.episode_key:
                (
                    session.query(SetupEpisodeRecord)
                    .filter(SetupEpisodeRecord.episode_key == record.episode_key, SetupEpisodeRecord.state == "OPEN")
                    .update(
                        {
                            SetupEpisodeRecord.state: "CLOSED",
                            SetupEpisodeRecord.updated_at: ist_now_naive(),
                            SetupEpisodeRecord.last_transition_reason: outcome,
                        },
                        synchronize_session=False,
                    )
                )
            session.commit()
            session.refresh(record)
            self._record_counterfactual_outcome(record)
            return record
        finally:
            session.close()

    def record_partial_exit(self, trade_id: int, *, quantity: int, exit_price: float, outcome: str = "partial_target_1") -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            existing = json.loads(record.partial_exit_json or "[]")
            remaining = int(record.remaining_quantity or record.filled_quantity or record.placed_quantity or 0)
            close_qty = min(max(0, int(quantity)), remaining)
            if close_qty <= 0:
                return record
            pnl = self.pnl_service.calculate(
                entry_price=float(record.average_price or record.entry_price or 0.0),
                exit_price=float(exit_price),
                quantity=close_qty,
                side=str(record.side),
            )
            existing.append({"outcome": outcome, "quantity": close_qty, "exit_price": exit_price, **pnl.to_dict()})
            self._apply_mfe_mae(record, price=exit_price, price_timestamp=ist_now_naive())
            record.partial_exit_json = json.dumps(existing, default=str)
            record.remaining_quantity = remaining - close_qty
            record.gross_pnl = float(record.gross_pnl or 0.0) + pnl.gross_pnl
            record.net_pnl = float(record.net_pnl or 0.0) + pnl.net_pnl
            record.charges = float(record.charges or 0.0) + pnl.charges
            record.slippage_cost = float(record.slippage_cost or 0.0) + pnl.slippage_cost
            record.spread_cost = float(record.spread_cost or 0.0) + pnl.spread_cost
            record.pnl = record.net_pnl
            record.updated_at = ist_now_naive()
            if record.remaining_quantity <= 0:
                record.status = "closed"
                record.outcome = outcome
                record.exit_price = exit_price
                if record.episode_key:
                    (
                        session.query(SetupEpisodeRecord)
                        .filter(SetupEpisodeRecord.episode_key == record.episode_key, SetupEpisodeRecord.state == "OPEN")
                        .update(
                            {
                                SetupEpisodeRecord.state: "CLOSED",
                                SetupEpisodeRecord.updated_at: ist_now_naive(),
                                SetupEpisodeRecord.last_transition_reason: outcome,
                            },
                            synchronize_session=False,
                        )
                    )
            session.commit()
            session.refresh(record)
            if record.status == "closed":
                self._record_counterfactual_outcome(record)
            return record
        finally:
            session.close()

    def update_stop_loss(self, trade_id: int, *, stop_loss: float) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.stop_loss = float(stop_loss)
            record.updated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def _apply_mfe_mae(self, record: TradeRecord, *, price: float, price_timestamp: datetime | None = None) -> bool:
        try:
            observed_price = float(price)
        except (TypeError, ValueError):
            return False
        if observed_price <= 0:
            return False
        entry = self._entry_for_excursion(record)
        if entry <= 0:
            return False
        observed_at = self._normalize_timestamp(price_timestamp)
        missing_metrics = (
            record.highest_price_during_trade is None
            or record.lowest_price_during_trade is None
            or record.mfe_points is None
            or record.mae_points is None
            or record.mfe_percent is None
            or record.mae_percent is None
        )
        high = float(record.highest_price_during_trade or entry)
        low = float(record.lowest_price_during_trade or entry)
        changed = False
        if observed_price > high:
            high = observed_price
            record.highest_price_during_trade = round(high, 4)
            record.mfe_recorded_at = observed_at
            record.time_to_mfe = self._seconds_since_created(record, observed_at)
            changed = True
        elif record.highest_price_during_trade is None:
            record.highest_price_during_trade = round(high, 4)
            record.mfe_recorded_at = observed_at
            record.time_to_mfe = self._seconds_since_created(record, observed_at)
            changed = True
        if observed_price < low:
            low = observed_price
            record.lowest_price_during_trade = round(low, 4)
            record.mae_recorded_at = observed_at
            record.time_to_mae = self._seconds_since_created(record, observed_at)
            changed = True
        elif record.lowest_price_during_trade is None:
            record.lowest_price_during_trade = round(low, 4)
            record.mae_recorded_at = observed_at
            record.time_to_mae = self._seconds_since_created(record, observed_at)
            changed = True

        if str(record.side or "BUY").upper() == "SELL":
            mfe_points = max(0.0, entry - low)
            mae_points = max(0.0, high - entry)
        else:
            mfe_points = max(0.0, high - entry)
            mae_points = max(0.0, entry - low)
        record.mfe_points = round(mfe_points, 4)
        record.mae_points = round(mae_points, 4)
        record.mfe_percent = round((mfe_points / entry) * 100, 4)
        record.mae_percent = round((mae_points / entry) * 100, 4)
        return changed or missing_metrics

    def _record_counterfactual_outcome(self, record: TradeRecord) -> None:
        if not record.episode_key or record.exit_price is None:
            return
        try:
            from app.services.decision_evidence_repository import DecisionEvidenceRepository

            payload = json.loads(record.order_response_json or "{}")
            factors = payload.get("signal_factor_scores", {}) if isinstance(payload, dict) else {}
            shadow = factors.get("shadow_risk_decisions", {}) if isinstance(factors, dict) else {}
            active = factors.get("risk_decision", {}) if isinstance(factors, dict) else {}
            account_equity = float(active.get("approved_risk_amount") or 0.0) / max(float(active.get("approved_risk_percent") or 0.0) / 100.0, 0.0001)
            counterfactual = DecisionEvidenceRepository().counterfactual_tier_outcomes(
                entry_price=float(record.average_price or record.entry_price or 0.0),
                exit_price=float(record.exit_price),
                account_equity=account_equity,
                shadow_decisions=shadow if isinstance(shadow, dict) else {},
                charges_per_unit=float(record.charges or 0.0) / max(int(record.filled_quantity or record.placed_quantity or 1), 1),
            )
            DecisionEvidenceRepository().record_outcome(
                episode_key=str(record.episode_key),
                horizon="trade_lifecycle",
                outcome_source="executed_trade",
                outcome={
                    "outcome": record.outcome,
                    "entry_price": record.average_price or record.entry_price,
                    "exit_price": record.exit_price,
                    "mfe_points": record.mfe_points,
                    "mae_points": record.mae_points,
                    "time_to_mfe": record.time_to_mfe,
                    "time_to_mae": record.time_to_mae,
                    "after_cost_result": record.net_pnl,
                    "counterfactual_risk_tiers": counterfactual,
                },
            )
        except Exception:
            return

    def _entry_for_excursion(self, record: TradeRecord) -> float:
        try:
            return float(record.average_price or record.entry_price or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _normalize_timestamp(self, value: datetime | None) -> datetime:
        timestamp = value or ist_now_naive()
        if timestamp.tzinfo is None:
            return timestamp
        return timestamp.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

    def _seconds_since_created(self, record: TradeRecord, value: datetime) -> float:
        created_at = record.created_at or value
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        return round(max(0.0, (value - created_at).total_seconds()), 3)

    def today_trades(self) -> list[TradeRecord]:
        session = get_session()
        try:
            today = ist_today()
            start = datetime.combine(today, time.min)
            end = datetime.combine(today, time.max)
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.created_at >= start)
                .filter(TradeRecord.created_at <= end)
                .order_by(TradeRecord.id.desc())
                .all()
            )
        finally:
            session.close()

    def open_trades(self) -> list[TradeRecord]:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.status != "closed")
                .order_by(TradeRecord.id.desc())
                .all()
            )
        finally:
            session.close()

    def exit_alerts(self, limit: int = 100) -> list[TradeRecord]:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.status.in_(["closing", "exit_failed", "reconciliation_mismatch"]))
                .order_by(TradeRecord.updated_at.desc())
                .limit(limit)
                .all()
            )
        finally:
            session.close()

    def live_open_trades(self, limit: int = 500) -> list[TradeRecord]:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.mode == "live")
                .filter(TradeRecord.status != "closed")
                .order_by(TradeRecord.id.desc())
                .limit(limit)
                .all()
            )
        finally:
            session.close()

    def find_live_by_order_id(self, order_id: str) -> list[TradeRecord]:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.mode == "live")
                .filter(
                    (TradeRecord.broker_order_id == order_id)
                    | (TradeRecord.exit_order_id == order_id)
                    | (TradeRecord.protective_order_id == order_id)
                )
                .order_by(TradeRecord.id.desc())
                .all()
            )
        finally:
            session.close()

    def mark_reconciliation_mismatch(self, trade_id: int, *, reason: str, broker_payload: dict[str, Any] | None = None) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = "reconciliation_mismatch"
            record.exit_last_error = reason
            record.broker_status_json = json.dumps(broker_payload or {"reason": reason}, default=str)
            record.notes = reason
            record.updated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def open_trade_for_opportunity(self, opportunity_id: int) -> TradeRecord | None:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.opportunity_id == opportunity_id)
                .filter(TradeRecord.status != "closed")
                .order_by(TradeRecord.id.desc())
                .first()
            )
        finally:
            session.close()

    def open_exposure_summary(self) -> dict[str, Any]:
        trades = self.open_trades()
        by_symbol: dict[str, int] = {}
        premium_exposure = 0.0
        for trade in trades:
            by_symbol[trade.symbol] = by_symbol.get(trade.symbol, 0) + 1
            price = float(trade.average_price or trade.entry_price or 0)
            quantity = int(trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
            premium_exposure += price * quantity
        return {
            "open_trades": len(trades),
            "by_symbol": by_symbol,
            "premium_exposure": round(premium_exposure, 2),
        }

    def daily_summary(self) -> dict[str, Any]:
        trades = self.today_trades()
        closed = [trade for trade in trades if trade.status == "closed"]
        stop_losses = [trade for trade in closed if trade.outcome == "stop_loss"]
        pnl = sum(float(trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0) for trade in closed)
        return {
            "date": ist_today().isoformat(),
            "trades": len(trades),
            "open": len([trade for trade in trades if trade.status != "closed"]),
            "closed": len(closed),
            "stop_losses": len(stop_losses),
            "pnl": round(pnl, 2),
        }
