import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services.database import RawTickRecord, get_session, init_db
from app.services.research_reporting_service import ResearchDatasetAuditService

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=IST)


class ResearchDatasetAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_settings = {
            "runtime_market_open_time": settings.runtime_market_open_time,
            "runtime_market_close_time": settings.runtime_market_close_time,
            "research_session_boundary_tolerance_minutes": (
                settings.research_session_boundary_tolerance_minutes
            ),
            "research_session_min_coverage_pct": (
                settings.research_session_min_coverage_pct
            ),
            "research_session_max_gap_seconds": (
                settings.research_session_max_gap_seconds
            ),
            "outcome_missing_interval_seconds": (
                settings.outcome_missing_interval_seconds
            ),
        }
        object.__setattr__(settings, "runtime_market_open_time", "09:15")
        object.__setattr__(settings, "runtime_market_close_time", "15:30")
        object.__setattr__(
            settings, "research_session_boundary_tolerance_minutes", 5
        )
        object.__setattr__(settings, "research_session_min_coverage_pct", 98.0)
        object.__setattr__(settings, "research_session_max_gap_seconds", 60.0)
        object.__setattr__(settings, "outcome_missing_interval_seconds", 5.0)

    def tearDown(self) -> None:
        for name, value in self.original_settings.items():
            object.__setattr__(settings, name, value)

    def test_complete_underlying_session_qualifies(self) -> None:
        start = _dt(3, 9, 15)
        end = _dt(3, 15, 30)
        rows = []
        timestamp = start
        while timestamp <= end:
            rows.append(("2026-08-03", timestamp, timestamp))
            timestamp += timedelta(seconds=5)

        result = ResearchDatasetAuditService()._qualify_session_rows(rows)

        self.assertEqual(result["observed_sessions"], 1)
        self.assertEqual(result["complete_sessions"], 1)
        self.assertEqual(result["partial_sessions"], 0)
        self.assertEqual(result["details"][0]["classification"], "COMPLETE")
        self.assertEqual(result["details"][0]["coverage_pct"], 100.0)

    def test_late_start_session_is_partial(self) -> None:
        rows = [
            ("2026-08-04", _dt(4, 11, 0), _dt(4, 11, 0)),
            ("2026-08-04", _dt(4, 15, 30), _dt(4, 15, 30)),
        ]

        result = ResearchDatasetAuditService()._qualify_session_rows(rows)

        self.assertEqual(result["complete_sessions"], 0)
        self.assertEqual(result["partial_sessions"], 1)
        self.assertIn("late_session_start", result["details"][0]["reasons"])

    def test_large_internal_gap_disqualifies_otherwise_full_session(self) -> None:
        start = _dt(5, 9, 15)
        end = _dt(5, 15, 30)
        gap_start = _dt(5, 10, 0)
        gap_end = _dt(5, 10, 2)
        rows = []
        timestamp = start
        while timestamp <= end:
            if not (gap_start < timestamp < gap_end):
                rows.append(("2026-08-05", timestamp, timestamp))
            timestamp += timedelta(seconds=5)

        result = ResearchDatasetAuditService()._qualify_session_rows(rows)

        self.assertEqual(result["complete_sessions"], 0)
        self.assertEqual(result["partial_sessions"], 1)
        self.assertIn("gap_exceeds_limit", result["details"][0]["reasons"])

    def test_audit_counts_only_underlying_sessions_and_reports_partial_reason(
        self,
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            temp_db_path = temp_db.name
        try:
            init_db(f"sqlite:///{temp_db_path}")
            session = get_session()
            try:
                session.add_all(
                    [
                        self._tick(
                            sequence=1,
                            symbol="BANKNIFTY",
                            timestamp=_dt(4, 11, 0),
                        ),
                        self._tick(
                            sequence=2,
                            symbol="BANKNIFTY",
                            timestamp=_dt(4, 15, 30),
                        ),
                        self._tick(
                            sequence=3,
                            symbol="BANKNIFTY26AUG58000CE",
                            timestamp=_dt(5, 9, 15),
                        ),
                        self._tick(
                            sequence=4,
                            symbol="BANKNIFTY26AUG58000CE",
                            timestamp=_dt(5, 15, 30),
                        ),
                    ]
                )
                session.commit()
            finally:
                session.close()

            result = ResearchDatasetAuditService().audit()

            self.assertEqual(result["sessions_observed"], 1)
            self.assertEqual(result["sessions_available"], 0)
            self.assertEqual(result["partial_sessions"], 1)
            self.assertEqual(
                result["session_qualification"]["partial_dates"], ["2026-08-04"]
            )
            self.assertIn(
                "late_session_start",
                result["session_qualification"]["details"][0]["reasons"],
            )
            self.assertFalse(result["preliminary_conclusions_supported"])
        finally:
            try:
                os.remove(temp_db_path)
            except PermissionError:
                pass

    def _tick(
        self,
        *,
        sequence: int,
        symbol: str,
        timestamp: datetime,
    ) -> RawTickRecord:
        return RawTickRecord(
            created_at=timestamp,
            session_date=timestamp.date().isoformat(),
            sequence=sequence,
            instrument_token=260105 if symbol == "BANKNIFTY" else 100000 + sequence,
            symbol=symbol,
            last_price=58000.0 if symbol == "BANKNIFTY" else 100.0,
            bid=99.0,
            ask=101.0,
            exchange_timestamp=timestamp,
            receive_timestamp=timestamp,
            timestamp_source="exchange",
            packet_type="full",
            capture_context="warm_market",
            strategy_version="test",
            config_hash="test",
        )


if __name__ == "__main__":
    unittest.main()
