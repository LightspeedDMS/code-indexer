import os
import site
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence, Tuple

import httpx

from tests.e2e._stub_systemd_unit import write_stub_systemd_unit

from .conftest import PROJECT_ROOT
from .secret_scan import Arg, StepResult, run_step

RUN_PY = PROJECT_ROOT / "scripts" / "analysis" / "perf_suite" / "run.py"
SERVER_READY_DEADLINE_S = 90.0
READY_POLL_S = 0.5
HEALTH_PROBE_TIMEOUT_S = 2.0
SERVER_STOP_TIMEOUT_S = 30
LOG_TAIL_CHARS = 3000
SERVER_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_tail(log_path: Path) -> str:
    return log_path.read_text()[-LOG_TAIL_CHARS:]


def _stop(proc: "subprocess.Popen[bytes]") -> None:
    proc.terminate()
    try:
        proc.wait(timeout=SERVER_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=SERVER_STOP_TIMEOUT_S)


@contextmanager
def running_cidx_server_process(
    home: Path,
) -> Iterator[Tuple[str, "subprocess.Popen[bytes]"]]:
    data_dir = home / ".cidx-server"
    data_dir.mkdir(parents=True)
    port = free_port()
    unit_dir = data_dir / "systemd-units"
    write_stub_systemd_unit(unit_dir, port)
    # ^ security explicit allow-list, not os.environ; no provider key or other secret reaches the server
    env = {k: os.environ[k] for k in SERVER_ENV_ALLOW if k in os.environ}
    env.update(
        HOME=str(home),
        # ^ external isolated HOME hides ~/.local site-packages unless PYTHONUSERBASE pins them
        PYTHONUSERBASE=site.getuserbase(),
        PYTHONPATH=str(PROJECT_ROOT / "src"),
        CIDX_TEST_FAST_SQLITE="1",
        CIDX_SERVER_DATA_DIR=str(data_dir),
        CIDX_DATA_DIR=str(data_dir),
        SYSTEMD_UNIT_DIR=str(unit_dir),
    )
    log_path = home / "server.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "code_indexer.server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--workers",
                "1",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(home),
        )
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + SERVER_READY_DEADLINE_S
        try:
            while True:
                if proc.poll() is not None:
                    raise AssertionError(
                        "local cidx server exited: " + _log_tail(log_path)
                    )
                try:
                    probe = httpx.get(f"{url}/healthz", timeout=HEALTH_PROBE_TIMEOUT_S)
                    if probe.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "local cidx server never became ready: " + _log_tail(log_path)
                    )
                time.sleep(READY_POLL_S)
            yield url, proc
        finally:
            _stop(proc)


@contextmanager
def running_cidx_server(home: Path) -> Iterator[str]:
    with running_cidx_server_process(home) as (url, _):
        yield url


def run_cli(args: Sequence[Arg], timeout: float) -> StepResult:
    return run_step("perf suite CLI", [sys.executable, str(RUN_PY), *args], timeout)
