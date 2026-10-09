"""Story S12: the worker-stall watchdog in real ``uvicorn --workers 2`` workers.

uvicorn's multiprocess supervisor SIGKILLs a worker that does not answer its
health ping within ``timeout_worker_healthcheck`` (5 s). These tests start a
real uvicorn with two workers on ``stall_watchdog_harness_app`` (whose lifespan
calls the production start/stop helpers), make one worker hold the GIL inside a
C call for 6 s, and check the evidence the watchdog leaves behind. No mocks:
real processes, real faulthandler, real files.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

# Real 2-worker uvicorn runs (~9 s for the stall fixture, ~7 s for the
# normal-load run, more on a loaded host): slow lane. slow-automation.sh Phase 2
# runs tests/unit/server/ with -m "slow ..." and a 120 s per-test timeout. The
# fast lane keeps in-process tests of the same mechanism in
# test_stall_watchdog_files.py.
pytestmark = pytest.mark.slow

_HERE = Path(__file__).resolve().parent
_SRC_DIR = Path(__file__).resolve().parents[4] / "src"
_STARTUP_DEADLINE_S = 30.0
_KILL_DEADLINE_S = 15.0
_UVICORN_HEALTHCHECK_KILL_S = 5.0
# A running worker's evidence file: worker-stall-<pid>-<start ticks>.evidence
_ARMED_RE = re.compile(r"^worker-stall-(\d+)-\d+\.evidence$")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UvicornHarness:
    """A real ``uvicorn --workers N`` process running the harness app."""

    def __init__(self, root: Path, workers: int = 2) -> None:
        self.data_dir = root / "server"
        self.logs_dir = self.data_dir / "logs"
        self.output_path = root / "uvicorn.out"
        self.workers = workers
        self.port = _free_port()
        self._proc: Optional["subprocess.Popen[bytes]"] = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True)
        env = dict(os.environ)
        env["CIDX_SERVER_DATA_DIR"] = str(self.data_dir)
        env["PYTHONPATH"] = os.pathsep.join([str(_SRC_DIR), str(_HERE)])
        with open(self.output_path, "wb") as out:
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "stall_watchdog_harness_app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--workers",
                    str(self.workers),
                ],
                cwd=str(_HERE),
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + _STARTUP_DEADLINE_S
        while self.output().count("Application startup complete.") < self.workers:
            assert self._proc.poll() is None, f"uvicorn exited:\n{self.output()}"
            assert time.monotonic() < deadline, f"uvicorn not ready:\n{self.output()}"
            time.sleep(0.1)

    def output(self) -> str:
        return self.output_path.read_text(errors="replace")

    def send_without_waiting(self, path: str) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        return sock

    def stall_files(self) -> List[Path]:
        if not self.logs_dir.is_dir():
            return []
        return sorted(self.logs_dir.glob("worker-stall-*"))

    def armed_pids(self) -> List[int]:
        pids = set()
        for path in self.stall_files():
            match = _ARMED_RE.match(path.name)
            if match:
                pids.add(int(match.group(1)))
        return sorted(pids)

    def stop(self) -> int:
        assert self._proc is not None
        if self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
                self._proc.wait(timeout=10)
        return int(self._proc.returncode)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


@dataclass
class StallScenario:
    dump_seen_after_s: Optional[float]
    dump_path: Optional[Path]
    dump_text: str
    stalled_pid: Optional[int]
    output: str
    harness: UvicornHarness


@pytest.fixture(scope="module")
def stall_scenario(tmp_path_factory: pytest.TempPathFactory) -> Iterator[StallScenario]:
    harness = UvicornHarness(tmp_path_factory.mktemp("stall"))
    harness.start()
    try:
        started = time.monotonic()
        sock = harness.send_without_waiting("/stall?seconds=6")
        dump_seen_after_s: Optional[float] = None
        dump_path: Optional[Path] = None
        dump_text = ""
        while time.monotonic() - started < _UVICORN_HEALTHCHECK_KILL_S:
            for path in harness.stall_files():
                text = _read(path)
                if "Timeout (" in text:
                    dump_seen_after_s = time.monotonic() - started
                    dump_path, dump_text = path, text
                    break
            if dump_path is not None:
                break
            time.sleep(0.05)
        stalled_pid: Optional[int] = None
        if dump_path is not None:
            match = re.match(r"^worker-stall-(\d+)", dump_path.name)
            stalled_pid = int(match.group(1)) if match else None
        # Let uvicorn's supervisor kill the stalled worker and start a new one.
        deadline = time.monotonic() + _KILL_DEADLINE_S
        while time.monotonic() < deadline:
            out = harness.output()
            if stalled_pid is not None and (
                f"Child process [{stalled_pid}] died" in out
                and out.count("Application startup complete.") > harness.workers
            ):
                break
            time.sleep(0.1)
        time.sleep(1.5)  # the replacement worker's first heartbeat tick
        sock.close()
        yield StallScenario(
            dump_seen_after_s=dump_seen_after_s,
            dump_path=dump_path,
            dump_text=dump_text,
            stalled_pid=stalled_pid,
            output=harness.output(),
            harness=harness,
        )
    finally:
        harness.stop()


def test_stalled_worker_dumps_every_thread_stack_before_uvicorn_kills_it(
    stall_scenario: StallScenario,
) -> None:
    assert stall_scenario.dump_seen_after_s is not None, (
        "no faulthandler stack dump appeared under logs/ within 5 s of the "
        f"GIL stall:\n{stall_scenario.output}"
    )
    assert stall_scenario.dump_seen_after_s < _UVICORN_HEALTHCHECK_KILL_S
    # The blocking thread's own frame is in the dump.
    assert "hold_gil_in_c_call" in stall_scenario.dump_text, stall_scenario.dump_text
    assert "stall_watchdog_harness_app.py" in stall_scenario.dump_text
    # And the scenario really is the staging one: uvicorn then killed it.
    assert f"Child process [{stall_scenario.stalled_pid}] died" in (
        stall_scenario.output
    ), stall_scenario.output


_SAMPLE_RE = re.compile(
    r"^  \d{8}T\d{9}Z rss_kb=\d+ swap_kb=\d+ mem_available_kb=\d+ "
    r"swap_free_kb=\d+ swap_in_pages_per_s=\S+ swap_out_pages_per_s=\S+ "
    r"psi_memory=\S+ psi_io=\S+ nfs=\S+ threads=\d+$",
    re.MULTILINE,
)


def test_stall_dump_contains_memory_pressure_samples(
    stall_scenario: StallScenario,
) -> None:
    # The raw dump file holds only faulthandler's output; the finalized log the
    # replacement worker writes holds the evidence, then the dump.
    pid = stall_scenario.stalled_pid
    logs_dir = re.escape(str(stall_scenario.harness.logs_dir))
    named = re.search(
        rf"({logs_dir}/worker-stall-{pid}-\d{{8}}T\d{{9}}Z\.log)$",
        stall_scenario.output,
        re.MULTILINE,
    )
    assert named, stall_scenario.output
    text = Path(named.group(1)).read_text()
    section = re.search(r"^=== faulthandler dump.*===$", text, re.MULTILINE)
    assert section, text[-2000:]
    samples = _SAMPLE_RE.findall(text[: section.start()])
    # The worker ran >= 1 s before the stall; one sample per heartbeat.
    assert len(samples) >= 1, text[: section.start()][-2000:]
    # The pre-stall evidence comes first; faulthandler's dump follows it.
    assert "hold_gil_in_c_call" in text[section.end() :]


def test_replacement_worker_reports_the_killed_workers_dump(
    stall_scenario: StallScenario,
) -> None:
    """The stalled worker is SIGKILLed mid-stall and cannot log anything; the
    worker uvicorn starts in its place finalizes the dump and logs the ERROR."""
    pid = stall_scenario.stalled_pid
    assert pid is not None
    logs_dir = stall_scenario.harness.logs_dir
    match = re.search(
        rf"^ERROR \S+ pid=\d+ Worker stall watchdog: worker pid {pid} .*? in "
        rf"({re.escape(str(logs_dir))}/worker-stall-{pid}-\d{{8}}T\d{{9}}Z\.log)$",
        stall_scenario.output,
        re.MULTILINE,
    )
    assert match, stall_scenario.output
    reporter_pid = int(re.search(r"pid=(\d+)", match.group(0)).group(1))  # type: ignore[union-attr]
    assert reporter_pid != pid, "the dead worker cannot report its own kill"
    dump = Path(match.group(1))
    text = dump.read_text()
    assert "hold_gil_in_c_call" in text and "rss_kb=" in text
    leftovers = [
        p.name
        for pattern in (f"worker-stall-{pid}-*.evidence", f"worker-stall-{pid}-*.dump")
        for p in logs_dir.glob(pattern)
    ]
    assert leftovers == [], leftovers


def _get_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=30) as response:
        return int(response.status)


def test_no_dump_and_no_kill_under_normal_load(tmp_path: Path) -> None:
    """Pure-Python CPU load longer than the dump timeout yields the GIL every
    switch interval, so the heartbeat keeps re-arming: no dump, no kill."""
    harness = UvicornHarness(tmp_path)
    harness.start()
    try:
        deadline = time.monotonic() + 10
        while len(harness.armed_pids()) < harness.workers:
            assert time.monotonic() < deadline, harness.stall_files()
            time.sleep(0.05)
        pids_before = harness.armed_pids()
        url = f"http://127.0.0.1:{harness.port}/busy?seconds=4"
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: _get_status(url), range(8)))
        assert statuses == [200] * 8
        time.sleep(1.5)  # one more heartbeat after the load

        dumps = [p.name for p in harness.stall_files() if "Timeout (" in _read(p)]
        assert dumps == [], dumps
        assert list(harness.logs_dir.glob("worker-stall-*.log")) == []
        assert harness.armed_pids() == pids_before
        output = harness.output()
        assert "died" not in output and "Worker stall watchdog" not in output, output
    finally:
        harness.stop()
    # Graceful SIGTERM runs the lifespan stop in every worker.
    leftovers = list(harness.logs_dir.glob("*.evidence")) + list(
        harness.logs_dir.glob("*.dump")
    )
    assert leftovers == [], harness.output()
