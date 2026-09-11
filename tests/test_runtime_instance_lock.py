import os
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

    @unittest.skipUnless(os.name == "nt", "Windows byte-lock regression")
    def test_acquire_does_not_read_lock_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "banknifty-app.lock"
            path.write_bytes(b"0")
            original_open = Path.open
            opened_handles: list[Mock] = []

            def open_without_read(target: Path, *args: object, **kwargs: object) -> Mock:
                real_handle = original_open(target, *args, **kwargs)
                proxy = Mock(wraps=real_handle)
                proxy.read.side_effect = AssertionError("lock byte must not be read")
                opened_handles.append(proxy)
                return proxy

            runtime_lock = RuntimeInstanceLock(path)
            with patch.object(Path, "open", autospec=True, side_effect=open_without_read):
                runtime_lock.acquire()
            try:
                opened_handles[0].read.assert_not_called()
            finally:
                runtime_lock.release()

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
