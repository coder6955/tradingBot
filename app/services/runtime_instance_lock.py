from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import BinaryIO


class RuntimeInstanceLock:
    """Hold one OS-level lock so two trading runtimes cannot start together."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self.acquired = False

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise RuntimeError(
                f"Cannot open the Bank Nifty runtime lock file: {self.path}"
            ) from exc

        try:
            # Do not read byte zero before acquiring it. On Windows, reading a
            # byte locked by another process raises PermissionError, which used
            # to make every overlapping/stale startup look like a file ACL
            # failure. File size can be inspected without touching the locked
            # byte, and the byte only needs to be initialized once.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"Cannot initialize the Bank Nifty runtime lock file: {self.path}"
            ) from exc

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "Another Bank Nifty trading app instance is already running"
            ) from exc
        self._handle = handle
        self.acquired = True

    @staticmethod
    def assert_canonical_port_free(
        host: str = "127.0.0.1", port: int = 8000, timeout_seconds: float = 0.5
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(max(0.1, timeout_seconds))
            if probe.connect_ex((host, int(port))) == 0:
                raise RuntimeError(
                    f"Another Bank Nifty app already owns {host}:{int(port)}"
                )

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            self.acquired = False
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            self.acquired = False

    def status(self) -> dict[str, object]:
        return {
            "acquired": self.acquired,
            "process_id": os.getpid(),
            "lock_file": self.path.name,
        }
