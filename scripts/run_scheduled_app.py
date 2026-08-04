from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import uvicorn

logger = logging.getLogger(__name__)

COMPLETION_REASON = "after_market_pipeline_completed"
COMPLETION_MESSAGE = (
    "Bank Nifty trading work is complete for today. After-market research "
    "finished and the app shut down safely. You may switch off the laptop."
)


def _after_market_completion_reached(supervisor: Any) -> bool:
    return bool(
        not supervisor.running and supervisor.last_stop_reason == COMPLETION_REASON
    )


async def _monitor_completion(
    *,
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
    supervisor: Any,
    poll_seconds: float,
) -> bool:
    while not server_task.done():
        if _after_market_completion_reached(supervisor):
            logger.info("After-market pipeline complete; requesting graceful shutdown")
            server.should_exit = True
            return True
        await asyncio.sleep(max(0.5, poll_seconds))
    return False


async def _serve(*, host: str, port: int, poll_seconds: float) -> int:
    from app.api import app, automation_supervisor_service
    from app.services.notification_service import NotificationService

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    server_task = asyncio.create_task(server.serve())
    monitor_task = asyncio.create_task(
        _monitor_completion(
            server=server,
            server_task=server_task,
            supervisor=automation_supervisor_service,
            poll_seconds=poll_seconds,
        )
    )
    try:
        await server_task
        completed = await monitor_task
    finally:
        if not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    if completed:
        result = await asyncio.to_thread(NotificationService().send, COMPLETION_MESSAGE)
        logger.info("Completion notification status=%s", result.get("status"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the scheduled trading app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(
        _serve(host=args.host, port=args.port, poll_seconds=args.poll_seconds)
    )


if __name__ == "__main__":
    raise SystemExit(main())
