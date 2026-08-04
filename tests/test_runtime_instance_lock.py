import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.runtime_instance_lock import RuntimeInstanceLock


class RuntimeInstanceLockTests(unittest.TestCase):
    def test_second_runtime_cannot_acquire_same_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "banknifty-app.lock"
            first = RuntimeInstanceLock(path)
            second = RuntimeInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            self.assertTrue(second.status()["acquired"])
            second.release()

    @patch("app.services.runtime_instance_lock.socket.socket")
    def test_existing_canonical_port_blocks_before_startup_side_effects(
        self, socket_factory: Mock
    ) -> None:
        probe = socket_factory.return_value.__enter__.return_value
        probe.connect_ex.return_value = 0

        with self.assertRaisesRegex(RuntimeError, "already owns"):
            RuntimeInstanceLock.assert_canonical_port_free()


if __name__ == "__main__":
    unittest.main()
