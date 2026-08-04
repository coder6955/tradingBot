from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.notification_service import NotificationService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send the scheduled runner's last-resort crash alert"
    )
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    timestamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )
    reason = str(args.reason or "unknown failure").strip().replace("\n", " ")[:240]
    result = NotificationService().send(
        "Bank Nifty app PROCESS FAILURE.\n"
        f"Time: {timestamp}\n"
        f"Issue: {reason}\n"
        "The outer scheduled launcher detected that the app process did not exit "
        "normally. Check the latest scheduled-app logs.",
        kind="process_failure",
    )
    return 0 if result.get("status") in {"sent", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
