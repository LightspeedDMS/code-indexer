"""Phase 7 (SIEM delivery) wiring in e2e-automation.sh, exercised for real.

The script is source-safe: sourcing defines functions and defaults without
running the suite, so these tests call the real helpers in a bash subprocess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from tests.fixtures.secops_sidecar.harness import find_free_port

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
E2E_SCRIPT = REPO_ROOT / "e2e-automation.sh"
BASH_TIMEOUT_SECONDS = 60


def _bash(snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    script = f'source "{E2E_SCRIPT}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=BASH_TIMEOUT_SECONDS,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home()), **env},
    )


@pytest.fixture()
def scratch() -> Iterator[Path]:
    root = Path.home() / ".tmp" / "siem-phase-wiring-tests"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(root)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


PHASE7_HELPERS = REPO_ROOT / "tests" / "e2e" / "siem_delivery" / "phase7_helpers.sh"
PHASE7_FUNCTIONS = (
    "write_siem_bootstrap_config",
    "generate_secops_sidecar_keys",
    "start_secops_sidecar",
    "wait_for_secops_sidecar",
    "start_siem_server",
    "wait_for_siem_server",
    "stop_pid_bounded",
    "cleanup_phase7_servers",
    "run_phase7",
)


def test_phase7_helpers_live_in_their_own_sourced_file() -> None:
    main_script = E2E_SCRIPT.read_text("utf-8")
    helpers = PHASE7_HELPERS.read_text("utf-8")
    for name in PHASE7_FUNCTIONS:
        assert f"{name}() {{" in helpers, f"{name} must be defined in phase7_helpers.sh"
        assert f"{name}() {{" not in main_script, (
            f"{name} must not grow e2e-automation.sh"
        )
    assert "phase7_helpers.sh" in main_script


def test_every_readiness_curl_has_a_per_request_timeout() -> None:
    curl_lines = [
        line
        for line in PHASE7_HELPERS.read_text("utf-8").splitlines()
        if "curl " in line
    ]
    assert curl_lines
    assert all("--max-time" in line for line in curl_lines), curl_lines


def test_stop_pid_bounded_kills_a_process_that_ignores_sigterm() -> None:
    snippet = (
        "bash -c 'trap \"\" TERM; while :; do sleep 1; done' & pid=$!; "
        'sleep 0.3; start=$(date +%s); stop_pid_bounded "$pid" stubborn; '
        'echo "elapsed=$(( $(date +%s) - start ))"; '
        'if kill -0 "$pid" 2>/dev/null; then echo ALIVE; else echo GONE; fi'
    )
    result = _bash(snippet, E2E_STOP_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert "GONE" in result.stdout
    elapsed = int(result.stdout.split("elapsed=")[1].split()[0])
    assert elapsed <= 3


def test_phase_seven_is_accepted_and_eight_is_not() -> None:
    bad = subprocess.run(
        ["bash", str(E2E_SCRIPT), "--phase", "8"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=BASH_TIMEOUT_SECONDS,
    )  # fmt: skip
    assert bad.returncode == 1
    assert "1, 2, 3, 4, 5, 6, or 7" in bad.stderr


def test_phase_defs_and_port_defaults() -> None:
    result = _bash(
        'printf "%s\\n" "${PHASE_DEFS[@]}"; '
        'echo "ports=$E2E_SECOPS_SIDECAR_PORT/$E2E_SECOPS_SIDECAR_CONTROL_PORT/$E2E_SIEM_SERVER_PORT"'
    )
    assert result.returncode == 0, result.stderr
    assert "7|SIEM Delivery (SecOps sidecar)|tests/e2e/siem_delivery" in result.stdout
    assert "ports=8902/8903/8904" in result.stdout


def test_siem_bootstrap_config_enables_the_fault_injection_gate(scratch: Path) -> None:
    result = _bash("write_siem_bootstrap_config", E2E_SIEM_SERVER_DATA_DIR=str(scratch))
    assert result.returncode == 0, result.stderr
    config = json.loads((scratch / "config.json").read_text("utf-8"))
    assert config["fault_injection_enabled"] is True
    assert config["fault_injection_nonprod_ack"] is True
    assert config["port"] == 8904 and config["host"] == "127.0.0.1"


PHASE7_PREFIX = "cidx-e2e-phase7-"


def test_sidecar_helpers_start_it_and_cleanup_stops_it(scratch: Path) -> None:
    ingest, control = find_free_port(), find_free_port()
    snippet = (
        "trap cleanup_phase7_servers EXIT; "  # no leaked sidecar even on failure
        'make_phase7_scratch_dirs && echo "dirs=$E2E_SECOPS_SIDECAR_DIR|$E2E_SIEM_SERVER_DATA_DIR" '
        "&& generate_secops_sidecar_keys && start_secops_sidecar && wait_for_secops_sidecar "
        f'&& curl -s --max-time 5 http://127.0.0.1:{control}/_control/health && echo "" '
        "&& cleanup_all_servers && echo CLEANED"
    )
    result = _bash(
        snippet,
        E2E_SECOPS_SIDECAR_PORT=str(ingest),
        E2E_SECOPS_SIDECAR_CONTROL_PORT=str(control),
        E2E_PG_DATA=str(scratch / "no-pg"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    key_dir, server_dir = result.stdout.split("dirs=")[1].split()[0].split("|")
    for owned in (key_dir, server_dir):
        assert owned.startswith(str(Path.home() / ".tmp" / PHASE7_PREFIX)), owned
        assert not Path(owned).exists(), f"{owned} must be removed on cleanup"
    assert '{"ingest_listening":true,"received_count":0}' in result.stdout
    assert "CLEANED" in result.stdout
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{control}/_control/health", timeout=2)


def test_safe_rm_refuses_paths_that_are_not_owned_phase7_scratch(
    scratch: Path,
) -> None:
    foreign_tmp = Path(tempfile.mkdtemp(prefix="foreign-", dir="/tmp"))
    try:
        for path in ("", str(foreign_tmp), str(scratch)):
            result = _bash(f'safe_rm_phase7_dir "{path}"')
            assert result.returncode != 0, f"deleted {path!r}"
            assert "refusing to delete" in result.stdout + result.stderr
        assert foreign_tmp.exists() and scratch.exists()
    finally:
        shutil.rmtree(foreign_tmp, ignore_errors=True)


def test_safe_rm_removes_an_owned_phase7_scratch_dir() -> None:
    result = _bash(
        'make_phase7_scratch_dirs && d="$E2E_SECOPS_SIDECAR_DIR" && '
        'safe_rm_phase7_dir "$E2E_SECOPS_SIDECAR_DIR" && '
        'safe_rm_phase7_dir "$E2E_SIEM_SERVER_DATA_DIR" && '
        '[[ ! -e "$d" ]] && echo REMOVED'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REMOVED" in result.stdout


def test_readiness_fails_when_our_server_dies_and_another_owns_the_port(
    scratch: Path,
) -> None:
    """A foreign listener on the port must never be mistaken for our server."""
    port = find_free_port()
    foreign = subprocess.Popen(
        ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        result = _bash(
            "trap cleanup_phase7_servers EXIT; start_siem_server; "
            "if wait_for_siem_server; then echo READY; else echo NOT_READY; fi",
            E2E_SIEM_SERVER_PORT=str(port),
            E2E_SIEM_SERVER_DATA_DIR=str(scratch),
            E2E_SIEM_SERVER_READINESS_TIMEOUT="30",
            E2E_ADMIN_USER="admin",
            E2E_ADMIN_PASS="admin",
        )
    finally:
        foreign.terminate()
        foreign.wait(timeout=10)
    assert "NOT_READY" in result.stdout, result.stdout + result.stderr
    assert "exited during startup" in result.stdout + result.stderr


def test_siem_server_refuses_a_non_loopback_bind(scratch: Path) -> None:
    result = _bash(
        # set -e (from the sourced script) would abort on the refusal itself;
        # the EXIT trap stops anything that was started anyway (no leaks).
        "trap cleanup_phase7_servers EXIT; "
        'if start_siem_server; then echo STARTED; else echo "rc=1 pid=[$SIEM_SERVER_PID]"; fi',
        E2E_SIEM_SERVER_HOST="0.0.0.0",
        E2E_SIEM_SERVER_DATA_DIR=str(scratch),
    )
    assert "rc=1 pid=[]" in result.stdout, result.stdout + result.stderr
    assert "must bind to loopback" in result.stdout + result.stderr
