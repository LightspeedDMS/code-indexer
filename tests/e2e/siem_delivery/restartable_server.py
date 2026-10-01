"""A server process the TEST owns, so a scenario can SIGKILL and restart it.

Same bootstrap as the Phase 7 server (fault-injection gate ON, isolated data
dir under ~/.tmp, its own free loopback port) and the same sidecar.  Restart
keeps the data dir, so anything the server persisted survives the crash and
anything it only held in memory does not -- which is the point.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, Optional

import httpx

from tests.fixtures.secops_sidecar.harness import REPO_ROOT, find_free_port

READY_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.5
PROBE_TIMEOUT_SECONDS = 5.0
SCRATCH_ROOT = Path.home() / ".tmp" / "cidx-e2e-siem-restartable"


class RestartableServer:
    """A fault-injection-gated server on loopback that can be killed and restarted."""

    def __init__(self, admin_user: str, admin_pass: str) -> None:
        self.admin_user, self.admin_pass = admin_user, admin_pass
        self.port = find_free_port()
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(tempfile.mkdtemp(prefix="server-", dir=str(SCRATCH_ROOT)))
        self.process: Optional["subprocess.Popen[bytes]"] = None
        self._log: Optional[IO[bytes]] = None
        (self.data_dir / "config.json").write_text(
            json.dumps(
                {
                    "server_dir": str(self.data_dir),
                    "host": "127.0.0.1",
                    "port": self.port,
                    "fault_injection_enabled": True,
                    "fault_injection_nonprod_ack": True,
                }
            ),
            encoding="utf-8",
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "CIDX_TEST_FAST_SQLITE": "1",
            "CIDX_SERVER_DATA_DIR": str(self.data_dir),
            "CIDX_DATA_DIR": str(self.data_dir),
            "SYSTEMD_UNIT_DIR": str(self.data_dir / "no-systemd-units"),
        }
        self._log = open(self.data_dir / "server.log", "ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "code_indexer.server.app:app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning", "--workers", "1"],
            env=env, stdout=self._log, stderr=self._log,
        )  # fmt: skip
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            assert self.process is not None and self.process.poll() is None, (
                f"server exited during startup; see {self.data_dir / 'server.log'}"
            )
            try:
                login = httpx.post(
                    f"{self.url}/auth/login",
                    json={"username": self.admin_user, "password": self.admin_pass},
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                if login.status_code == 200:
                    return
            except httpx.TransportError:
                pass  # not listening yet: keep polling until the deadline
            time.sleep(POLL_SECONDS)
        raise AssertionError(f"server not ready within {READY_TIMEOUT_SECONDS:.0f}s")

    def kill(self) -> None:
        """SIGKILL: a crash, no shutdown hooks run."""
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        self._close_log()

    def close(self) -> None:
        """Bounded stop (TERM, then KILL), then remove the data dir."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        self._close_log()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
