from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.services.database import RuntimeJobRunRecord, get_session
from app.services.time_utils import ist_now_naive


class RuntimeJobRepository:
    """Durable idempotency/status store for runtime jobs."""

    def latest_run(self, *, job_name: str) -> dict[str, Any] | None:
        session = get_session()
        try:
            row = (
                session.query(RuntimeJobRunRecord)
                .filter(RuntimeJobRunRecord.job_name == job_name)
                .order_by(RuntimeJobRunRecord.id.desc())
                .first()
            )
            return self.to_dict(row) if row else None
        finally:
            session.close()

    def latest(self, *, job_name: str, trading_date: str) -> dict[str, Any] | None:
        session = get_session()
        try:
            row = (
                session.query(RuntimeJobRunRecord)
                .filter(RuntimeJobRunRecord.job_name == job_name)
                .filter(RuntimeJobRunRecord.trading_date == trading_date)
                .order_by(RuntimeJobRunRecord.id.desc())
                .first()
            )
            return self.to_dict(row) if row else None
        finally:
            session.close()

    def count(self, *, job_name: str, trading_date: str, status: str | None = None) -> int:
        session = get_session()
        try:
            query = (
                session.query(RuntimeJobRunRecord)
                .filter(RuntimeJobRunRecord.job_name == job_name)
                .filter(RuntimeJobRunRecord.trading_date == trading_date)
            )
            if status is not None:
                query = query.filter(RuntimeJobRunRecord.status == status)
            return int(query.count())
        finally:
            session.close()

    def start(self, *, job_name: str, trading_date: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        session = get_session()
        try:
            now = ist_now_naive()
            row = RuntimeJobRunRecord(
                job_name=job_name,
                trading_date=trading_date,
                status="running",
                started_at=now,
                metadata_json=json.dumps(metadata or {}, default=str),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self.to_dict(row)
        finally:
            session.close()

    def finish(
        self,
        *,
        run_id: int,
        status: str,
        metadata: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        session = get_session()
        try:
            row = session.query(RuntimeJobRunRecord).filter(RuntimeJobRunRecord.id == int(run_id)).first()
            if row is None:
                return None
            completed_at = ist_now_naive()
            row.status = status
            row.completed_at = completed_at
            if row.started_at is not None:
                row.duration_ms = int(max(0.0, (completed_at - row.started_at).total_seconds()) * 1000)
            row.error_message = error_message
            if metadata is not None:
                row.metadata_json = json.dumps(metadata, default=str)
            session.commit()
            session.refresh(row)
            return self.to_dict(row)
        finally:
            session.close()

    def to_dict(self, row: RuntimeJobRunRecord | None) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "id": row.id,
            "job_name": row.job_name,
            "trading_date": row.trading_date,
            "status": row.status,
            "started_at": self._format_dt(row.started_at),
            "completed_at": self._format_dt(row.completed_at),
            "duration_ms": row.duration_ms,
            "error_message": row.error_message,
            "metadata": self._json(row.metadata_json),
        }

    def _format_dt(self, value: datetime | None) -> str | None:
        return value.isoformat(sep=" ") if value else None

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
