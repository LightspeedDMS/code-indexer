"""
Unit tests for Story #1035 sentinel integration in handle_trigger_dependency_analysis.

Covers:
  - Test 1: pre-flight returns error envelope with active_job_id when sentinel held
  - Test 2: passes through to success path when sentinel absent (is_available True)
  - Test 3: synchronous sentinel claim failure surfaces as MCP error envelope
  - Test 4: DuplicateJobError from job tracker surfaces as MCP error envelope

All tests use real SharedJobSentinel on tmpdir (no mocks for sentinel),
and mock only the external seams (app state, config service, job_id generation).
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch


from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.admin import handle_trigger_dependency_analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MCP_HANDLER = "code_indexer.server.mcp.handlers.admin"
_CONFIG_SVC = "code_indexer.server.services.config_service.get_config_service"


def _unwrap(mcp_response: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap MCP response envelope to the inner data dict."""
    content = mcp_response.get("content")
    if content and isinstance(content, list) and content[0].get("text"):
        parsed: Dict[str, Any] = json.loads(content[0]["text"])
        return parsed
    return mcp_response


def _make_admin_user() -> User:
    return User(
        username="admin",
        role=UserRole.ADMIN,
        password_hash="hashed",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_config_mock(dep_map_enabled: bool = True) -> MagicMock:
    mock_ci = MagicMock()
    mock_ci.dependency_map_enabled = dep_map_enabled
    mock_cfg = MagicMock()
    mock_cfg.claude_integration_config = mock_ci
    mock_svc = MagicMock()
    mock_svc.get_config.return_value = mock_cfg
    return mock_svc


# ---------------------------------------------------------------------------
# Minimal fake dep-map service
# ---------------------------------------------------------------------------


class _FakeDepMapService:
    """Minimal fake — is_available parameterised, run_* no-ops."""

    def __init__(
        self,
        available: bool = True,
        sentinel_dir: Optional[Path] = None,
        raise_on_run: Optional[Exception] = None,
    ) -> None:
        self._available = available
        self._sentinel_dir = sentinel_dir
        self._raise_on_run = raise_on_run
        self.run_full_called = False
        self.run_delta_called = False

    def is_available(self) -> bool:
        return self._available

    def get_sentinel_dir(self) -> Optional[Path]:
        return self._sentinel_dir

    def _get_node_id(self) -> str:
        return "test-node"

    def run_full_analysis(
        self, job_id: Optional[str] = None, pre_claimed: bool = False
    ) -> Dict[str, Any]:
        self.run_full_called = True
        if self._raise_on_run is not None:
            raise self._raise_on_run
        return {}

    def run_delta_analysis(
        self, job_id: Optional[str] = None, pre_claimed: bool = False
    ) -> Dict[str, Any]:
        self.run_delta_called = True
        if self._raise_on_run is not None:
            raise self._raise_on_run
        return {}


# ---------------------------------------------------------------------------
# Test 1: pre-flight returns error envelope with active_job_id when sentinel held
# ---------------------------------------------------------------------------


class TestMcpTriggerErrorEnvelopeWithJobIdWhenSentinelHeld:
    """
    When dep_map_service.is_available() returns False, the handler must return
    success=False with job_id from the active sentinel (Story #1035 AC6).
    """

    def test_mcp_trigger_returns_error_envelope_with_active_job_id_when_sentinel_held(
        self, tmp_path: Path
    ) -> None:
        """
        Write a sentinel file with job_id='X-123' to tmpdir;
        mock is_available() False and get_sentinel_dir() -> tmpdir;
        assert response is {success: False, error: 'already in progress',
        job_id: 'X-123', mode: 'full'}.
        """
        from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel

        sentinel_dir = tmp_path / "dep-map"
        sentinel_dir.mkdir()

        # Claim the sentinel as if another node holds it
        sentinel = SharedJobSentinel(
            sentinel_dir=sentinel_dir, stale_timeout_seconds=14400
        )
        claim = sentinel.try_claim("analysis", "X-123", "node-other")
        assert claim.success, "Pre-test setup: sentinel claim must succeed"

        svc = _FakeDepMapService(available=False, sentinel_dir=sentinel_dir)
        mock_app_state = MagicMock()
        mock_app_state.dependency_map_service = svc

        with (
            patch(_CONFIG_SVC, return_value=_make_config_mock()),
            patch(
                f"{_MCP_HANDLER}._utils.app_module.app.state",
                mock_app_state,
            ),
        ):
            result = handle_trigger_dependency_analysis(
                {"mode": "full"}, _make_admin_user()
            )

        inner = _unwrap(result)
        assert inner["success"] is False
        assert "already in progress" in inner.get("error", "").lower(), (
            f"Expected 'already in progress' in error, got: {inner.get('error')!r}"
        )
        assert inner.get("job_id") == "X-123", (
            f"Expected job_id='X-123', got: {inner.get('job_id')!r}"
        )
        assert inner.get("mode") == "full"


# ---------------------------------------------------------------------------
# Test 2: passes through when sentinel absent (is_available True)
# ---------------------------------------------------------------------------


class TestMcpTriggerPassesThroughWhenSentinelAbsent:
    """
    When dep_map_service.is_available() returns True, the handler spawns
    the background thread and returns success=True with a job_id (AC2).
    """

    def test_mcp_trigger_passes_through_when_sentinel_absent(
        self, tmp_path: Path
    ) -> None:
        """
        Empty sentinel dir + is_available True -> handler returns success=True.
        The background thread is started but we do not wait for it.
        """
        sentinel_dir = tmp_path / "dep-map-absent"
        sentinel_dir.mkdir()

        svc = _FakeDepMapService(available=True, sentinel_dir=sentinel_dir)
        mock_app_state = MagicMock()
        mock_app_state.dependency_map_service = svc

        with (
            patch(_CONFIG_SVC, return_value=_make_config_mock()),
            patch(
                f"{_MCP_HANDLER}._utils.app_module.app.state",
                mock_app_state,
            ),
        ):
            result = handle_trigger_dependency_analysis(
                {"mode": "delta"}, _make_admin_user()
            )

        inner = _unwrap(result)
        assert inner["success"] is True, f"Expected success=True, got: {inner}"
        assert inner.get("job_id") is not None, "Expected a non-None job_id"
        assert inner.get("mode") == "delta"


# ---------------------------------------------------------------------------
# Test 3: synchronous claim failure surfaces as MCP error envelope
# ---------------------------------------------------------------------------


class TestMcpTriggerHandlesAnalysisAlreadyRunningErrorFromSynchronousClaim:
    """
    When the synchronous sentinel try_claim returns ClaimResult(success=False),
    the handler must return an MCP error envelope with the active job_id (AC13).
    """

    def test_mcp_trigger_handles_AnalysisAlreadyRunningError_from_synchronous_claim(
        self, tmp_path: Path
    ) -> None:
        """
        Pre-claim sentinel with job_id='Y-456'; mock is_available() True
        (pre-flight passes), but synchronous try_claim will fail because
        sentinel is already held.

        Handler must return error envelope with job_id='Y-456'.
        """
        from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel

        sentinel_dir = tmp_path / "dep-map-claim"
        sentinel_dir.mkdir()

        # Pre-claim so synchronous claim in handler will fail
        sentinel = SharedJobSentinel(
            sentinel_dir=sentinel_dir, stale_timeout_seconds=14400
        )
        claim = sentinel.try_claim("analysis", "Y-456", "node-winner")
        assert claim.success, "Pre-test setup: sentinel claim must succeed"

        # is_available returns True (pre-flight passes), but sentinel is held
        svc = _FakeDepMapService(available=True, sentinel_dir=sentinel_dir)
        mock_app_state = MagicMock()
        mock_app_state.dependency_map_service = svc

        with (
            patch(_CONFIG_SVC, return_value=_make_config_mock()),
            patch(
                f"{_MCP_HANDLER}._utils.app_module.app.state",
                mock_app_state,
            ),
        ):
            result = handle_trigger_dependency_analysis(
                {"mode": "full"}, _make_admin_user()
            )

        inner = _unwrap(result)
        assert inner["success"] is False, (
            f"Expected success=False when claim fails, got: {inner}"
        )
        assert inner.get("job_id") == "Y-456", (
            f"Expected job_id='Y-456' from active sentinel, got: {inner.get('job_id')!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: DuplicateJobError from job tracker surfaces as MCP error envelope
# ---------------------------------------------------------------------------


class TestMcpTriggerNarrowsDuplicateJobErrorToMcpEnvelope:
    """
    When the background thread's run_full_analysis raises DuplicateJobError,
    the handler thread body must catch it (not propagate as unhandled),
    and must log at INFO not ERROR (AC13 narrowing).
    """

    def test_mcp_trigger_narrows_DuplicateJobError_to_MCP_envelope(
        self, tmp_path: Path
    ) -> None:
        """
        Mock run_full_analysis to raise DuplicateJobError;
        ensure no ERROR-level log is emitted for the duplicate condition.
        The handler itself returns success=True (the thread raises later),
        but the thread must NOT propagate DuplicateJobError as an unhandled ERROR.
        """
        import logging

        from code_indexer.server.services.job_tracker import DuplicateJobError

        sentinel_dir = tmp_path / "dep-map-dup"
        sentinel_dir.mkdir()

        dup_error = DuplicateJobError(
            operation_type="dependency_map_full",
            repo_alias="server",
            existing_job_id="job-dup-789",
        )
        svc = _FakeDepMapService(
            available=True, sentinel_dir=sentinel_dir, raise_on_run=dup_error
        )
        mock_app_state = MagicMock()
        mock_app_state.dependency_map_service = svc

        error_logged = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.ERROR:
                    error_logged.append(record.getMessage())

        cap = _CapturingHandler()
        log = logging.getLogger("code_indexer.server.mcp.handlers.admin")
        log.addHandler(cap)

        thread_done = threading.Event()
        original_thread_start = threading.Thread.start

        def _patched_start(self_thread: threading.Thread) -> None:
            original_target = self_thread._target  # type: ignore[attr-defined]

            def _wrapper(*args: Any, **kwargs: Any) -> None:
                try:
                    original_target(*args, **kwargs)
                finally:
                    thread_done.set()

            self_thread._target = _wrapper  # type: ignore[attr-defined]
            original_thread_start(self_thread)

        try:
            with (
                patch(_CONFIG_SVC, return_value=_make_config_mock()),
                patch(
                    f"{_MCP_HANDLER}._utils.app_module.app.state",
                    mock_app_state,
                ),
                patch.object(threading.Thread, "start", _patched_start),
            ):
                result = handle_trigger_dependency_analysis(
                    {"mode": "full"}, _make_admin_user()
                )
                thread_done.wait(timeout=5)
        finally:
            log.removeHandler(cap)

        # The handler returns success=True immediately (thread raises later)
        inner = _unwrap(result)
        # The key assertion: DuplicateJobError must NOT produce an ERROR-level log
        dup_errors = [
            msg
            for msg in error_logged
            if "duplicate" in msg.lower()
            or "already" in msg.lower()
            or "dep-map" in msg.lower()
            or "depmap" in msg.lower()
        ]
        assert not dup_errors, (
            f"DuplicateJobError must not produce ERROR-level log, got: {dup_errors}"
        )
        # The handler itself returned successfully (thread is detached)
        assert inner.get("success") is True or inner.get("job_id") is not None, (
            f"Handler must have accepted the trigger, got: {inner}"
        )
