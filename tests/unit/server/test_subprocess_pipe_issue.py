"""
Test to confirm subprocess pipe buffer issue.
"""

import os
import subprocess
import time
import json
import sys

import pytest
import requests

pytestmark = pytest.mark.slow


def _wait_for_server_ready(port: int, timeout: float = 20.0) -> None:
    """Poll a freshly-spawned server subprocess's /health endpoint until it
    responds or the timeout elapses.

    Bug #1719: a fixed short sleep is not reliable here -- a real
    concurrently-running cidx-server (or a sibling subprocess in this same
    test) can contend for primary_instance.lock, and
    acquire_primary_instance_lock() blocks for up to ~5s before giving up
    (src/code_indexer/server/utils/primary_instance_lock.py). That wait
    happens during ASGI lifespan startup, before the app can serve any
    request, so a process can still be mid-startup well past a fixed 3s
    sleep. Mirrors test_server_startup_fix_integration.py's
    _wait_for_server_ready() helper (added by Bug #1707) for the identical
    root-cause class.
    """
    deadline = time.time() + timeout
    last_error: Exception = TimeoutError("no attempt made")
    while time.time() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
            return
        except requests.exceptions.ConnectionError as exc:
            last_error = exc
            time.sleep(0.5)
    raise AssertionError(
        f"Server on port {port} never became reachable within {timeout}s: {last_error}"
    )


def _terminate_and_wait(process: subprocess.Popen, timeout: float = 20.0) -> None:
    """Terminate a subprocess and wait for it to exit.

    Bug #1719: a hardcoded short `wait(timeout=...)` after `terminate()`
    can raise `subprocess.TimeoutExpired` under primary_instance_lock
    contention (startup/shutdown both touch the lock). This gives the
    process a more generous window to exit gracefully, and escalates to
    SIGKILL if it still hasn't exited after `timeout` seconds so a
    genuinely wedged process can never hang the test suite.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _isolated_server_env(data_dir) -> dict:
    """Build a subprocess env pointing CIDX_SERVER_DATA_DIR at an isolated,
    per-process directory.

    Bug #1719 root cause: without this override the spawned server
    defaults to the real ~/.cidx-server data directory -- the SAME
    directory the actual local dev cidx-server uses -- so the two
    processes contend for ~/.cidx-server/primary_instance.lock. Pointing
    the subprocess at its own directory eliminates that contention
    entirely rather than merely tolerating it. The subprocess bootstraps
    its own default config.json there on first access
    (ConfigService.load_config() -> ServerConfigManager.create_default_config()
    + save_config(), both of which mkdir the directory as needed), so it
    does not need to be pre-populated.
    """
    env = dict(os.environ)
    env["CIDX_SERVER_DATA_DIR"] = str(data_dir)
    return env


@pytest.mark.e2e
class TestSubprocessPipeIssue:
    """Test subprocess pipe buffer issues."""

    def test_server_with_pipes_vs_without_pipes(self, tmp_path):
        """Test server startup with and without subprocess pipes."""
        server_dir = tmp_path / "test-server"
        server_dir.mkdir()

        # Create valid configuration
        config = {"server_dir": str(server_dir), "host": "127.0.0.1", "port": 9002}

        config_file = server_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        cmd = [
            sys.executable,
            "-m",
            "code_indexer.server.main",
            "--host",
            "127.0.0.1",
            "--port",
            "9002",
        ]

        print("Testing server with PIPES (like ServerLifecycleManager)...")

        # Test 1: With pipes (problematic approach)
        process_with_pipes = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_isolated_server_env(tmp_path / "data-with-pipes"),
        )

        print(f"Process with pipes PID: {process_with_pipes.pid}")

        # Wait for the server to actually become reachable instead of
        # blindly sleeping a fixed 3s (Bug #1719). This step is purely
        # diagnostic (no liveness assertion follows), so a process that
        # never becomes reachable (e.g. a genuine pipe-buffer deadlock)
        # just falls through to the poll_result check below.
        try:
            _wait_for_server_ready(9002, timeout=20.0)
        except AssertionError as exc:
            print(f"Process with pipes never became reachable: {exc}")

        # Check if it's still running
        poll_result = process_with_pipes.poll()
        print(f"Process with pipes poll result: {poll_result}")

        if poll_result is not None:
            stdout, stderr = process_with_pipes.communicate()
            print(f"Process with pipes died with code: {poll_result}")
            print(f"STDOUT: {stdout.decode()[:500]}...")
            print(f"STDERR: {stderr.decode()[:500]}...")

        # Clean up -- tolerate primary_instance_lock contention rather than
        # a hardcoded 5s wait (Bug #1719).
        _terminate_and_wait(process_with_pipes, timeout=20.0)

        print("\nTesting server WITHOUT pipes (better approach)...")

        # Test 2: Without pipes (better approach)
        process_without_pipes = subprocess.Popen(
            cmd,
            stdout=None,  # Inherit parent's stdout
            stderr=None,  # Inherit parent's stderr
            start_new_session=True,
            env=_isolated_server_env(tmp_path / "data-without-pipes"),
        )

        print(f"Process without pipes PID: {process_without_pipes.pid}")

        # Wait until the server is actually reachable instead of blindly
        # sleeping a fixed 3s (Bug #1719).
        _wait_for_server_ready(9002, timeout=20.0)

        # Check if it's still running
        poll_result = process_without_pipes.poll()
        print(f"Process without pipes poll result: {poll_result}")

        # This should still be running
        assert poll_result is None, (
            f"Process without pipes died with code: {poll_result}"
        )

        # Wait a bit longer to be sure it stays alive. This is a stability
        # check over time, not a startup-readiness wait -- it is unrelated
        # to Bug #1719's flake (which was specifically about the fixed
        # startup/shutdown waits racing lock contention).
        time.sleep(5)
        poll_result = process_without_pipes.poll()
        print(f"Process without pipes poll result after 8 seconds: {poll_result}")

        assert poll_result is None, (
            f"Process without pipes died after 8 seconds with code: {poll_result}"
        )

        # Clean up
        _terminate_and_wait(process_without_pipes, timeout=20.0)

        print("Test completed successfully!")

    def test_server_with_devnull_pipes(self, tmp_path):
        """Test server startup with pipes redirected to devnull."""
        server_dir = tmp_path / "test-server"
        server_dir.mkdir()

        # Create valid configuration
        config = {"server_dir": str(server_dir), "host": "127.0.0.1", "port": 9003}

        config_file = server_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        cmd = [
            sys.executable,
            "-m",
            "code_indexer.server.main",
            "--host",
            "127.0.0.1",
            "--port",
            "9003",
        ]

        print("Testing server with DEVNULL pipes...")

        # Test with pipes redirected to devnull
        with open("/dev/null", "w") as devnull:
            process_devnull = subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                start_new_session=True,
                env=_isolated_server_env(tmp_path / "data-devnull"),
            )

        print(f"Process with devnull PID: {process_devnull.pid}")

        # Wait until the server is actually reachable instead of blindly
        # sleeping a fixed 3s (Bug #1719).
        _wait_for_server_ready(9003, timeout=20.0)

        # Check if it's still running
        poll_result = process_devnull.poll()
        print(f"Process with devnull poll result: {poll_result}")

        assert poll_result is None, (
            f"Process with devnull died with code: {poll_result}"
        )

        # Wait longer (stability check over time, not a startup-readiness
        # wait -- unrelated to Bug #1719's flake).
        time.sleep(5)
        poll_result = process_devnull.poll()
        print(f"Process with devnull poll result after 8 seconds: {poll_result}")

        assert poll_result is None, (
            f"Process with devnull died after 8 seconds with code: {poll_result}"
        )

        # Clean up
        _terminate_and_wait(process_devnull, timeout=20.0)

        print("Devnull test completed successfully!")
