from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.config import settings
from app.services.database import ArmedEntryRecord, get_session
from app.services.time_utils import ist_now_naive


class ArmedEntryRepository:
    """Durable source of truth for armed setup recovery and audit."""

    def upsert(self, payload: dict[str, Any]) -> None:
        setup_id = str(payload["setup_id"])
        session = get_session()
        try:
            row = session.query(ArmedEntryRecord).filter(ArmedEntryRecord.setup_id == setup_id).one_or_none()
            values = {
                "strategy_version": str(payload.get("strategy_version") or settings.strategy_version),
                "symbol": str(payload.get("symbol") or "BANKNIFTY"),
                "tradingsymbol": str(payload.get("tradingsymbol") or ""),
                "instrument_token": int(payload.get("instrument_token") or 0),
                "order_mode": str(payload.get("order_mode") or "paper"),
                "state": str(payload.get("latest_state") or "ARMED_FOR_ENTRY"),
                "armed_at": self._datetime(payload.get("armed_at")),
                "valid_until": self._datetime(payload.get("valid_until")),
                "updated_at": ist_now_naive(),
                "payload_json": json.dumps(payload, default=self._json_default, separators=(",", ":")),
            }
            if row is None:
                row = ArmedEntryRecord(setup_id=setup_id, **values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            session.commit()
        finally:
            session.close()

    def active(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        point = (now or ist_now_naive()).replace(tzinfo=None)
        session = get_session()
        try:
            rows = (
                session.query(ArmedEntryRecord)
                .filter(
                    ArmedEntryRecord.strategy_version == settings.strategy_version,
                    ArmedEntryRecord.state.in_(("ARMED_FOR_ENTRY", "ENTER_NOW", "ORDER_PENDING")),
                    ArmedEntryRecord.valid_until >= point,
                )
                .order_by(ArmedEntryRecord.armed_at.asc())
                .all()
            )
            return [json.loads(row.payload_json) for row in rows]
        finally:
            session.close()

    def _datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if value:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        return ist_now_naive()

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        raise TypeError(f"unsupported armed-entry payload type: {type(value).__name__}")
