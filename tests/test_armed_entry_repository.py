import os
import tempfile
from datetime import datetime, timedelta

from app.config import settings
from app.services.armed_entry_repository import ArmedEntryRepository
from app.services.database import init_db


def test_armed_entry_repository_round_trip_and_version_filter() -> None:
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    original_version = settings.strategy_version
    try:
        init_db(f"sqlite:///{handle.name}")
        repo = ArmedEntryRepository()
        now = datetime(2026, 7, 21, 10, 30)
        payload = {
            "setup_id": "armed-persisted",
            "strategy_version": original_version,
            "symbol": "BANKNIFTY",
            "tradingsymbol": "BANKNIFTY26JUL58000CE",
            "instrument_token": 580001,
            "order_mode": "paper",
            "latest_state": "ARMED_FOR_ENTRY",
            "armed_at": now,
            "valid_until": now + timedelta(minutes=2),
            "factor_scores": {"market_regime": {"regime": "trend_expansion"}},
        }
        repo.upsert(payload)

        rows = repo.active(now=now + timedelta(seconds=30))

        assert len(rows) == 1
        assert rows[0]["setup_id"] == "armed-persisted"
        assert rows[0]["factor_scores"]["market_regime"]["regime"] == "trend_expansion"
        object.__setattr__(settings, "strategy_version", "different-version")
        assert repo.active(now=now + timedelta(seconds=30)) == []
    finally:
        object.__setattr__(settings, "strategy_version", original_version)
        try:
            os.remove(handle.name)
        except PermissionError:
            pass
