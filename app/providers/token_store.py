from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = ROOT / ".env"
TOKEN_FILE = ROOT / "access_token.txt"
IST = ZoneInfo("Asia/Kolkata")


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def _trading_date(now: datetime | None = None) -> str:
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(IST).date().isoformat()


def save_access_token(
    token: str,
    *,
    created_at: datetime | None = None,
    source: str = "kite_session",
) -> None:
    """Atomically save a token scoped to the current India trading date."""
    clean_token = _clean_value(token)
    if not clean_token:
        raise ValueError("Kite access token is empty")
    now = created_at or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    payload = {
        "access_token": clean_token,
        "created_at": now.isoformat(),
        "trading_date": now.date().isoformat(),
        "source": str(source or "kite_session"),
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TOKEN_FILE.with_name(f"{TOKEN_FILE.name}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(temporary, TOKEN_FILE)


def load_access_token(
    *, require_today: bool = True, now: datetime | None = None
) -> str | None:
    """Load the token file, rejecting undated or stale tokens by default."""
    payload = _read_token_payload()
    token = _clean_value(str(payload.get("access_token") or ""))
    if not token:
        return None
    if require_today and str(payload.get("trading_date") or "") != _trading_date(now):
        return None
    return token


def token_status(*, now: datetime | None = None) -> dict[str, Any]:
    payload = _read_token_payload()
    stored_date = str(payload.get("trading_date") or "") or None
    today = _trading_date(now)
    return {
        "file_exists": TOKEN_FILE.exists(),
        "token_present": bool(_clean_value(str(payload.get("access_token") or ""))),
        "trading_date": stored_date,
        "valid_for_today": bool(stored_date and stored_date == today),
        "source": payload.get("source"),
    }


def load_legacy_env_access_token() -> str | None:
    """Read the old .env token only for one-time migration by auto-login."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("KITE_ACCESS_TOKEN="):
            return _clean_value(line.split("=", 1)[1])
    return None


def _read_token_payload() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        return {}
    try:
        parsed = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
