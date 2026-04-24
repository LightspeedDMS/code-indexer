"""Unit tests for run_once.py main() ordering fix (Bug #884).

Validates that:
- DeploymentExecutor is constructed BEFORE _resolve_server_url() is called
- _should_retry_on_startup() runs BEFORE _resolve_server_url()
- URL failure inside the retry branch exits cleanly (no crash-loop)
- Healthy path resolves URL once and passes it to the executor
- Smoke test gate in execute() aborts self-restart when new code is broken
- Smoke test gate in execute() allows self-restart when new code is healthy
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def normal_executor():
    """Return a fake DeploymentExecutor that reports no pending retry."""
    m = MagicMock()
    m._should_retry_on_startup.return_value = False
    return m


@pytest.fixture()
def _patch_infra():
    """Patch ChangeDetector, DeploymentLock, AutoUpdateService for all tests."""
    with (
        patch("code_indexer.server.auto_update.run_once.ChangeDetector"),
        patch("code_indexer.server.auto_update.run_once.DeploymentLock"),
        patch("code_indexer.server.auto_update.run_once.AutoUpdateService"),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests: retry branch ordering
# ---------------------------------------------------------------------------


class TestMainRetryPathSurvivesUrlFailure:
    """Retry branch must be reachable even when _resolve_server_url() raises."""

    def test_main_retry_path_survives_url_failure(self, _patch_infra):
        """Retry branch must run despite _resolve_server_url raising RuntimeError.

        Bug #884: URL resolution used to be called BEFORE _should_retry_on_startup,
        so any failure would crash-loop the process without ever entering recovery.
        The executor reports pending_restart; URL resolution then fails inside the
        branch; status must be written as 'failed' and process must exit 1.
        """
        write_status_calls = []
        retry_check_called = []
        url_resolve_called = []

        fake_executor = MagicMock()
        fake_executor._should_retry_on_startup.side_effect = lambda: (
            retry_check_called.append(True) or True
        )
        fake_executor._write_status_file.side_effect = (
            lambda s, d="": write_status_calls.append(s)
        )
        fake_executor.execute.return_value = False

        def fail_url():
            url_resolve_called.append(True)
            raise RuntimeError("no config.json")

        with (
            patch(
                "code_indexer.server.auto_update.run_once.DeploymentExecutor",
                return_value=fake_executor,
            ),
            patch(
                "code_indexer.server.auto_update.run_once._resolve_server_url",
                side_effect=fail_url,
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                from code_indexer.server.auto_update import run_once

                run_once.main()

        assert len(retry_check_called) == 1, "_should_retry_on_startup must be called"
        assert len(url_resolve_called) == 1, "_resolve_server_url must be attempted"
        assert "failed" in write_status_calls, "status must be written as 'failed'"
        assert exc_info.value.code == 1

    def test_executor_constructed_before_url_resolution(self, _patch_infra):
        """DeploymentExecutor must be constructed BEFORE _resolve_server_url is called."""
        call_order = []

        def make_executor(**kwargs):
            call_order.append("executor_constructed")
            m = MagicMock()
            m._should_retry_on_startup.return_value = False
            return m

        def track_url():
            call_order.append("url_resolved")
            return "http://localhost:8000"

        with (
            patch(
                "code_indexer.server.auto_update.run_once.DeploymentExecutor",
                side_effect=make_executor,
            ),
            patch(
                "code_indexer.server.auto_update.run_once._resolve_server_url",
                side_effect=track_url,
            ),
        ):
            with pytest.raises(SystemExit):
                from code_indexer.server.auto_update import run_once

                run_once.main()

        assert "executor_constructed" in call_order
        assert "url_resolved" in call_order
        assert call_order.index("executor_constructed") < call_order.index(
            "url_resolved"
        ), "DeploymentExecutor must be constructed before _resolve_server_url"


# ---------------------------------------------------------------------------
# Tests: normal (non-retry) path behavior
# ---------------------------------------------------------------------------


class TestMainHealthyPathPreservesBehavior:
    """Normal path: URL must be resolved and injected before poll_once()."""

    def test_main_healthy_path_preserves_behavior(self, normal_executor, _patch_infra):
        """Normal path must call poll_once() with resolved URL set on executor."""
        resolved_url = "http://0.0.0.0:8080"
        fake_service = MagicMock()

        with (
            patch(
                "code_indexer.server.auto_update.run_once.DeploymentExecutor",
                return_value=normal_executor,
            ),
            patch(
                "code_indexer.server.auto_update.run_once._resolve_server_url",
                return_value=resolved_url,
            ),
            patch(
                "code_indexer.server.auto_update.run_once.AutoUpdateService",
                return_value=fake_service,
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                from code_indexer.server.auto_update import run_once

                run_once.main()

        assert exc_info.value.code == 0
        fake_service.poll_once.assert_called_once()
        assert normal_executor.server_url == resolved_url, (
            f"executor.server_url must be '{resolved_url}', got '{normal_executor.server_url}'"
        )

    def test_url_resolved_once_on_normal_path(self, normal_executor, _patch_infra):
        """_resolve_server_url must be called exactly once on the normal poll path."""
        url_calls = []

        with (
            patch(
                "code_indexer.server.auto_update.run_once.DeploymentExecutor",
                return_value=normal_executor,
            ),
            patch(
                "code_indexer.server.auto_update.run_once._resolve_server_url",
                side_effect=lambda: url_calls.append(1) or "http://localhost:8000",
            ),
        ):
            with pytest.raises(SystemExit):
                from code_indexer.server.auto_update import run_once

                run_once.main()

        assert sum(url_calls) == 1, (
            f"_resolve_server_url must be called exactly once, was called {sum(url_calls)} times"
        )


# ---------------------------------------------------------------------------
# Tests: smoke-test guard in DeploymentExecutor.execute()
# ---------------------------------------------------------------------------


class TestSmokeTestGuard:
    """Smoke-test subprocess must gate self-restart in execute()."""

    @staticmethod
    def _setup_repo(tmp_path: Path) -> tuple:
        """Create minimal repo layout with real .py file for hash computation.

        Returns (repo_path, target_py, status_file, marker_path).
        """
        repo_path = tmp_path / "repo"
        auto_update_dir = repo_path / "src" / "code_indexer" / "server" / "auto_update"
        auto_update_dir.mkdir(parents=True)
        target_py = auto_update_dir / "run_once.py"
        target_py.write_text("# original content\n")
        return (
            repo_path,
            target_py,
            tmp_path / "auto-update-status.json",
            tmp_path / "pending-redeploy",
        )

    @staticmethod
    def _make_subprocess_router(smoke_returncode: int, target_py: Path) -> tuple:
        """Return (router_fn, calls_list) for subprocess.run patching.

        git pull side effect writes new content to target_py (driving hash change).
        Smoke test call returns smoke_returncode. All other calls succeed.
        """
        calls: list = []

        def router(cmd, **kwargs):
            calls.append(list(cmd) if isinstance(cmd, list) else cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = b"ok"
            result.stderr = b""
            if not isinstance(cmd, list):
                return result
            if cmd[0] == sys.executable and len(cmd) >= 2 and cmd[1] == "-c":
                result.returncode = smoke_returncode
                result.stderr = b"SyntaxError: bad" if smoke_returncode != 0 else b""
                return result
            if len(cmd) >= 2 and cmd[:2] == ["git", "pull"]:
                target_py.write_text("# updated content after pull\n")
            return result

        return router, calls

    @staticmethod
    def _read_status_file(status_file: Path) -> dict:
        """Read and parse status JSON; fail test immediately on decode error."""
        if not status_file.exists():
            return {}
        raw = status_file.read_text()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(f"status file contained invalid JSON: {exc}\nRaw: {raw!r}")

    @staticmethod
    def _run_execute_with_hash_change(smoke_returncode: int, tmp_path: Path) -> dict:
        """Orchestrate execute() with hash-change scenario; return observed effects.

        Patches only true external boundaries: subprocess.run, AUTO_UPDATE_STATUS_FILE,
        PENDING_REDEPLOY_MARKER (both as real Paths in tmp_path). Hash change is
        driven by the git-pull side effect writing new content to real .py files.

        Returns dict with "systemctl_called", "status", "execute_result", "marker_exists".
        """
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        repo_path, target_py, status_file, marker_path = TestSmokeTestGuard._setup_repo(
            tmp_path
        )
        router, calls = TestSmokeTestGuard._make_subprocess_router(
            smoke_returncode, target_py
        )
        executor = DeploymentExecutor(repo_path=repo_path)

        with (
            patch("subprocess.run", side_effect=router),
            patch(
                "code_indexer.server.auto_update.deployment_executor"
                ".AUTO_UPDATE_STATUS_FILE",
                status_file,
            ),
            patch(
                "code_indexer.server.auto_update.deployment_executor"
                ".PENDING_REDEPLOY_MARKER",
                marker_path,
            ),
        ):
            execute_result = executor.execute()

        systemctl_called = any(
            isinstance(cmd, list)
            and len(cmd) >= 4
            and cmd[:4] == ["sudo", "systemctl", "restart", "cidx-auto-update"]
            for cmd in calls
        )
        return {
            "systemctl_called": systemctl_called,
            "status": TestSmokeTestGuard._read_status_file(status_file).get("status"),
            "execute_result": execute_result,
            "marker_exists": marker_path.exists(),
        }

    def test_smoke_test_failure_aborts_self_restart(self, tmp_path):
        """When smoke test returns non-zero, systemctl restart must NOT be called.

        Bug #884 secondary fix: execute() verifies new auto-updater code imports
        cleanly before self-restarting. On failure: no restart, no marker,
        status written as 'failed'.
        """
        result = self._run_execute_with_hash_change(
            smoke_returncode=1, tmp_path=tmp_path
        )

        assert not result["systemctl_called"], (
            "systemctl restart must NOT be called when smoke test fails"
        )
        assert not result["marker_exists"], (
            "PENDING_REDEPLOY_MARKER must NOT exist when smoke test fails"
        )
        assert result["status"] == "failed", (
            f"status must be 'failed' when smoke test fails, got: {result['status']}"
        )

    def test_smoke_test_pass_allows_self_restart(self, tmp_path):
        """When smoke test returns 0, happy path must not regress.

        Hash change detected -> smoke passes -> pending_restart written ->
        marker created -> systemctl restart called -> execute() returns True.
        """
        result = self._run_execute_with_hash_change(
            smoke_returncode=0, tmp_path=tmp_path
        )

        assert result["systemctl_called"], (
            "systemctl restart must be called when smoke test passes"
        )
        assert result["marker_exists"], (
            "PENDING_REDEPLOY_MARKER must exist when smoke test passes"
        )
        assert result["status"] == "pending_restart", (
            f"status must be 'pending_restart' when smoke test passes, "
            f"got: {result['status']}"
        )
        assert result["execute_result"] is True, (
            "execute() must return True when smoke test passes"
        )
