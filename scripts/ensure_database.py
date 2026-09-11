from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from app.config import settings

_SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_$]+$")


def ensure_configured_database(database_url: str) -> str:
    """Create the configured MySQL schema if absent; never create arbitrary names."""
    url = make_url(database_url)
    if not url.drivername.startswith("mysql"):
        return "not_required"

    database_name = (url.database or "").strip()
    if not database_name or not _SAFE_DATABASE_NAME.fullmatch(database_name):
        raise RuntimeError("DATABASE_URL contains an invalid MySQL database name")

    server_url = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        query=url.query,
    )
    server_engine = create_engine(
        server_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        with server_engine.connect() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        server_engine.dispose()
    return database_name


if __name__ == "__main__":
    created_or_existing = ensure_configured_database(settings.database_url)
    print(f"database_ready={created_or_existing}")
