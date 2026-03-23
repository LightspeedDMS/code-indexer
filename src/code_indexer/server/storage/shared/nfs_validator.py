"""NFS mount validation for ONTAP FSx shared storage.

Anti-fallback policy: if the NFS mount is unhealthy, the node is considered DOWN.
There is no graceful degradation — callers must treat an unhealthy result as fatal.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


class NfsMountValidator:
    """Validates NFS mount is accessible and healthy.

    Usage::

        validator = NfsMountValidator("/mnt/fsx")
        result = validator.validate()
        if not result["healthy"]:
            raise RuntimeError(f"NFS mount is DOWN: {result['error']}")
    """

    _PROBE_FILE_PREFIX = ".cidx_nfs_probe_"

    def __init__(self, mount_point: str) -> None:
        self._mount_point = Path(mount_point)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self) -> dict:
        """Check mount is accessible and writable.

        Returns a dict with keys:
            healthy (bool): True if mount is fully operational.
            mount_point (str): Absolute path of the mount point.
            writable (bool): True if a test file could be written and read back.
            latency_ms (float): Round-trip write+read latency in milliseconds.
            error (Optional[str]): Human-readable error message when not healthy.
        """
        result: dict = {
            "healthy": False,
            "mount_point": str(self._mount_point),
            "writable": False,
            "latency_ms": 0.0,
            "error": None,
        }

        # Step 1: mount point must exist
        if not self._mount_point.exists():
            result["error"] = f"Mount point does not exist: {self._mount_point}"
            return result

        # Step 2: must be an actual mount point (not just a plain directory)
        if not os.path.ismount(str(self._mount_point)):
            result["error"] = f"Path is not a mount point: {self._mount_point}"
            return result

        # Step 3: write/read a probe file to verify writability and measure latency
        probe_path = self._mount_point / f"{self._PROBE_FILE_PREFIX}{uuid.uuid4().hex}"
        probe_content = b"cidx-nfs-probe"
        start = time.monotonic()
        try:
            probe_path.write_bytes(probe_content)
            read_back = probe_path.read_bytes()
            elapsed_ms = (time.monotonic() - start) * 1000.0

            if read_back != probe_content:
                result["error"] = (
                    "Probe file content mismatch — NFS data integrity failure"
                )
                return result

            result["writable"] = True
            result["latency_ms"] = round(elapsed_ms, 3)
            result["healthy"] = True
        except OSError as exc:
            result["error"] = f"NFS write/read probe failed: {exc}"
            return result
        finally:
            # Best-effort cleanup; ignore errors so we don't mask the real result
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass

        return result

    def is_mounted(self) -> bool:
        """Quick check: mount point exists and is a mountpoint."""
        return self._mount_point.exists() and os.path.ismount(str(self._mount_point))

    def check_path_accessible(self, path: str, timeout: float = 5.0) -> bool:
        """Check if a specific path under the mount is accessible.

        Args:
            path: Absolute or relative (to mount root) path to check.
            timeout: Maximum seconds to wait for the stat call.  The check is
                     performed synchronously; ``timeout`` guards against NFS
                     hangs via ``signal.alarm`` on POSIX systems.  If the
                     platform does not support SIGALRM the timeout is ignored
                     and the call blocks until the OS returns.

        Returns:
            True if the path exists and is accessible, False otherwise.
        """
        import signal

        target = Path(path) if Path(path).is_absolute() else self._mount_point / path

        def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
            raise TimeoutError(f"NFS path access timed out after {timeout}s: {target}")

        has_sigalrm = hasattr(signal, "SIGALRM")
        if has_sigalrm and timeout > 0:
            old_handler = signal.signal(signal.SIGALRM, _handler)
            signal.alarm(max(1, int(timeout)))

        try:
            return target.exists()
        except (OSError, TimeoutError):
            return False
        finally:
            if has_sigalrm and timeout > 0:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
