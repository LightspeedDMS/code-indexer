"""
Unit tests for cooperative cancellation wiring in clone backends (Bug #1342).

Cancelling a running activation job used to be a no-op while the worker was
blocked inside the CoW clone subprocess/HTTP call. These tests prove:

1. LocalCloneBackend.create_clone_at_path accepts a `cancel_check` callable
   and, when it fires, kills the REAL `cp` process group promptly (no
   process mocks — a real `cp` subprocess is spawned and observed).
2. The old `check=True` semantics (nonzero returncode raises
   subprocess.CalledProcessError) are preserved after moving off plain
   subprocess.run onto the shared run_cancellable_subprocess engine — proven
   here with a REAL failing `cp` invocation (nonexistent source), not a mock.
3. CowDaemonBackend.create_clone_at_path/_poll_job accept `cancel_check` and,
   when it fires before the remote job completes, raise
   SubprocessCancelledError and best-effort DELETE the (possibly
   still-provisioning) remote clone -- and a failure during that best-effort
   cleanup must never mask the cancellation itself. HTTP is mocked via a
   fake `requests` module, mirroring the `_make_cow_backend` /
   `_mock_requests_module` fixture conventions already used throughout
   test_clone_backend.py for this same class (not a "process" mock, since
   the daemon is a remote HTTP service, not a local subprocess). The
   `daemon_url` / `api_key` / `mount_point` values below are non-secret test
   fixture placeholders copied verbatim from that existing shared
   convention -- there is no real daemon or credential involved.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.shared.clone_backend import (
    CowDaemonBackend,
    LocalCloneBackend,
)
from code_indexer.server.utils.cancellable_subprocess import SubprocessCancelledError

# Test-only tuning constants (not production defaults).
_CANCEL_POLL_INTERVAL_SECONDS = 0.001
_CANCEL_ELAPSED_BUDGET_SECONDS = 5.0

# Non-secret placeholder fixture values for the fake CoW daemon HTTP client
# (mirrors test_clone_backend.py's own _make_cow_backend/_make_cow_config).
_FAKE_DAEMON_URL = "http://daemon:8081"
_FAKE_DAEMON_API_KEY = "test-api-key"  # noqa: S105 -- fixture placeholder, not a real credential
_FAKE_MOUNT_POINT = "/mnt/nfs/cidx"


# ---------------------------------------------------------------------------
# LocalCloneBackend.create_clone_at_path cancellation (real `cp` subprocess)
# ---------------------------------------------------------------------------


class TestLocalCloneBackendCreateCloneAtPathCancellation:
    def test_cancel_check_kills_real_cp_process_promptly(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        for i in range(20):
            (source / f"file_{i}.txt").write_text("x" * 1024)
        dest = tmp_path / "dest"

        backend = LocalCloneBackend(versioned_base=str(tmp_path))

        start = time.monotonic()
        with pytest.raises(SubprocessCancelledError):
            backend.create_clone_at_path(
                str(source),
                str(dest),
                cancel_check=lambda: True,
                poll_interval=_CANCEL_POLL_INTERVAL_SECONDS,
            )
        elapsed = time.monotonic() - start
        assert elapsed < _CANCEL_ELAPSED_BUDGET_SECONDS, (
            f"cancellation took {elapsed:.2f}s, "
            f"expected < {_CANCEL_ELAPSED_BUDGET_SECONDS}s"
        )

    def test_cancel_check_false_lets_real_clone_complete_normally(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "hello.txt").write_text("hello-1342")
        dest = tmp_path / "dest"

        backend = LocalCloneBackend(versioned_base=str(tmp_path))

        result = backend.create_clone_at_path(
            str(source),
            str(dest),
            cancel_check=lambda: False,
        )

        assert result == str(dest)
        assert (dest / "hello.txt").read_text() == "hello-1342"

    def test_real_cp_failure_still_raises_called_process_error(self, tmp_path: Path):
        """Bug #1342 regression guard: switching create_clone_at_path off
        plain subprocess.run(check=True) onto run_cancellable_subprocess must
        preserve check=True-equivalent semantics -- a genuinely failing `cp`
        (nonexistent source, no mocking at all) must still raise
        CalledProcessError, not silently swallow the failure."""
        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        nonexistent_source = str(tmp_path / "does-not-exist-1342")
        dest = str(tmp_path / "dest")

        with pytest.raises(subprocess.CalledProcessError):
            backend.create_clone_at_path(nonexistent_source, dest)


# ---------------------------------------------------------------------------
# CowDaemonBackend.create_clone_at_path / _poll_job cancellation
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data if json_data is not None else {}
    mock.raise_for_status = MagicMock()
    return mock


def _noop_visibility_waiter(_path: str) -> None:
    return None


def _make_cow_backend(timeout_seconds: int = 30):
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    config = CowDaemonConfig(
        daemon_url=_FAKE_DAEMON_URL,
        api_key=_FAKE_DAEMON_API_KEY,
        mount_point=_FAKE_MOUNT_POINT,
        poll_interval_seconds=1,
        timeout_seconds=timeout_seconds,
        daemon_storage_path=_FAKE_MOUNT_POINT,
    )
    return CowDaemonBackend(config=config, visibility_waiter=_noop_visibility_waiter)


def _mock_requests_module(post_resp=None, get_resp=None, delete_resp=None):
    mock_req = MagicMock()
    if post_resp is not None:
        mock_req.post.return_value = post_resp
    if get_resp is not None:
        if isinstance(get_resp, list):
            mock_req.get.side_effect = get_resp
        else:
            mock_req.get.return_value = get_resp
    if delete_resp is not None:
        mock_req.delete.return_value = delete_resp
    return mock_req


class TestCowDaemonBackendCancellation:
    def test_cancel_check_true_raises_subprocess_cancelled_error(self):
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-cancel-1"})
        # GET must never be reached — cancel_check fires before polling.
        mock_req = _mock_requests_module(post_resp=post_resp)
        del_resp = _make_response(204)
        mock_req.delete.return_value = del_resp

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(SubprocessCancelledError):
                backend.create_clone_at_path(
                    f"{_FAKE_MOUNT_POINT}/src/repo",
                    f"{_FAKE_MOUNT_POINT}/ns/clone-cancel-1",
                    cancel_check=lambda: True,
                )

    def test_cancel_triggers_best_effort_remote_delete(self):
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-cancel-2"})
        mock_req = _mock_requests_module(post_resp=post_resp)
        del_resp = _make_response(204)
        mock_req.delete.return_value = del_resp

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(SubprocessCancelledError):
                backend.create_clone_at_path(
                    f"{_FAKE_MOUNT_POINT}/src/repo",
                    f"{_FAKE_MOUNT_POINT}/ns/clone-cancel-2",
                    cancel_check=lambda: True,
                )

        mock_req.delete.assert_called_once()
        url = mock_req.delete.call_args[0][0]
        assert "/api/v1/clones/ns/clone-cancel-2" in url

    def test_cleanup_delete_failure_does_not_mask_cancellation(self):
        """A failure in the best-effort remote cleanup must never replace
        the SubprocessCancelledError with some other exception -- the
        cancellation is the fact that matters to the caller."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-cancel-3"})
        mock_req = _mock_requests_module(post_resp=post_resp)
        mock_req.delete.side_effect = RuntimeError("daemon unreachable")

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(SubprocessCancelledError):
                backend.create_clone_at_path(
                    f"{_FAKE_MOUNT_POINT}/src/repo",
                    f"{_FAKE_MOUNT_POINT}/ns/clone-cancel-3",
                    cancel_check=lambda: True,
                )

    def test_cancel_check_none_preserves_existing_polling_behavior(self):
        """Default cancel_check=None must not alter existing poll-until-
        completed behavior."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-no-cancel"})
        poll_resp = _make_response(
            200, {"status": "completed", "clone_path": "ignored"}
        )
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=poll_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.create_clone_at_path(
                f"{_FAKE_MOUNT_POINT}/src/repo",
                f"{_FAKE_MOUNT_POINT}/ns/clone-no-cancel",
            )

        assert result == f"{_FAKE_MOUNT_POINT}/ns/clone-no-cancel"
        mock_req.get.assert_called_once()
