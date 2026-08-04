import asyncio
import unittest
from types import SimpleNamespace

from scripts.run_scheduled_app import (
    COMPLETION_REASON,
    _after_market_completion_reached,
    _monitor_completion,
)


class FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


class ScheduledAppRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_completion_requires_expected_stop_reason(self) -> None:
        self.assertTrue(
            _after_market_completion_reached(
                SimpleNamespace(running=False, last_stop_reason=COMPLETION_REASON)
            )
        )
        self.assertFalse(
            _after_market_completion_reached(
                SimpleNamespace(running=False, last_stop_reason="requested")
            )
        )
        self.assertFalse(
            _after_market_completion_reached(
                SimpleNamespace(running=True, last_stop_reason=COMPLETION_REASON)
            )
        )

    async def test_monitor_requests_graceful_server_exit(self) -> None:
        server = FakeServer()
        server_task = asyncio.create_task(asyncio.sleep(30))
        supervisor = SimpleNamespace(running=False, last_stop_reason=COMPLETION_REASON)
        try:
            completed = await _monitor_completion(
                server=server,
                server_task=server_task,
                supervisor=supervisor,
                poll_seconds=0.01,
            )
        finally:
            server_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await server_task

        self.assertTrue(completed)
        self.assertTrue(server.should_exit)


if __name__ == "__main__":
    unittest.main()
