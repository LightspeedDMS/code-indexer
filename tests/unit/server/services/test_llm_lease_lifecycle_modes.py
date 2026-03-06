"""
Unit tests for LlmLeaseLifecycleService mode transitions, status, and thread safety (Story #366).

Covers:
- on_mode_enter_subscription(): same behavior as start()
- on_mode_exit_subscription(): same behavior as stop()
- get_status(): returns current state at all lifecycle points
- Thread safety: concurrent start/stop calls do not corrupt state

Uses:
- Real LlmLeaseStateManager with tmp_path
- Real ClaudeCredentialsFileManager with tmp_path
- httpx.MockTransport for HTTP (no external mocking libraries)
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx

from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
from code_indexer.server.services.claude_credentials_file_manager import (
    ClaudeCredentialsFileManager,
)
from code_indexer.server.services.llm_creds_client import LlmCredsClient
from code_indexer.server.services.llm_lease_lifecycle import (
    LeaseLifecycleStatus,
    LlmLeaseLifecycleService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _checkout_response(
    lease_id: str = "lease-001",
    credential_id: str = "cred-001",
    access_token: str = "sk-ant-oat01-access",
    refresh_token: str = "sk-ant-ort01-refresh",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "lease_id": lease_id,
            "credential_id": credential_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
    )


def _checkin_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"status": "ok"})


def _failing_handler(request: httpx.Request) -> httpx.Response:
    """Simulate an unreachable provider."""
    raise httpx.ConnectError("Connection refused")


def _make_service(
    tmp_path: Path,
    checkout_handler=None,
    checkin_handler=None,
) -> LlmLeaseLifecycleService:
    """Build a service with real file managers and a mock HTTP transport."""

    def default_handler(request: httpx.Request) -> httpx.Response:
        if "/checkout" in str(request.url):
            if checkout_handler is not None:
                return checkout_handler(request)
            return _checkout_response()
        if "/checkin" in str(request.url):
            if checkin_handler is not None:
                return checkin_handler(request)
            return _checkin_response()
        return httpx.Response(404, text="Not found")

    transport = _make_transport(default_handler)
    client = LlmCredsClient(
        provider_url="http://fake-provider",
        api_key="test-api-key",
        transport=transport,
    )
    state_manager = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
    creds_manager = ClaudeCredentialsFileManager(
        credentials_path=tmp_path / "creds" / ".credentials.json"
    )
    return LlmLeaseLifecycleService(
        client=client,
        state_manager=state_manager,
        credentials_manager=creds_manager,
    )


# ---------------------------------------------------------------------------
# TestModeTransitions
# ---------------------------------------------------------------------------


class TestModeTransitions:
    """on_mode_enter_subscription() and on_mode_exit_subscription() behavior."""

    def test_on_mode_enter_subscription_produces_active_status(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()
        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE

    def test_on_mode_enter_subscription_writes_credentials_file(self, tmp_path):
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()
        assert creds_path.exists()

    def test_on_mode_enter_subscription_stores_lease_id(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()
        assert svc.get_status().lease_id == "lease-001"

    def test_on_mode_exit_subscription_produces_inactive_status(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()
        svc.on_mode_exit_subscription()
        assert svc.get_status().status == LeaseLifecycleStatus.INACTIVE

    def test_on_mode_exit_subscription_deletes_credentials_file(self, tmp_path):
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()
        assert creds_path.exists()

        svc.on_mode_exit_subscription()
        assert not creds_path.exists()

    def test_on_mode_exit_subscription_clears_state_file(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.on_mode_enter_subscription()

        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        assert state_mgr.load_state() is not None

        svc.on_mode_exit_subscription()
        assert state_mgr.load_state() is None


# ---------------------------------------------------------------------------
# TestGetStatus
# ---------------------------------------------------------------------------


class TestGetStatus:
    """get_status() returns current lifecycle state at all points."""

    def test_initial_status_is_inactive(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc.get_status().status == LeaseLifecycleStatus.INACTIVE

    def test_initial_lease_id_is_none(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc.get_status().lease_id is None

    def test_initial_credential_id_is_none(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc.get_status().credential_id is None

    def test_get_status_after_start_returns_active(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        status = svc.get_status()
        assert status.status == LeaseLifecycleStatus.ACTIVE
        assert status.lease_id == "lease-001"
        assert status.credential_id == "cred-001"

    def test_get_status_after_stop_returns_inactive(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        svc.stop()
        status = svc.get_status()
        assert status.status == LeaseLifecycleStatus.INACTIVE
        assert status.lease_id is None

    def test_get_status_when_degraded_has_error_message(self, tmp_path):
        transport = _make_transport(_failing_handler)
        client = LlmCredsClient(
            provider_url="http://unreachable",
            api_key="key",
            transport=transport,
        )
        state_manager = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        creds_manager = ClaudeCredentialsFileManager(
            credentials_path=tmp_path / "creds" / ".credentials.json"
        )
        svc = LlmLeaseLifecycleService(
            client=client,
            state_manager=state_manager,
            credentials_manager=creds_manager,
        )
        svc.start()

        status = svc.get_status()
        assert status.status == LeaseLifecycleStatus.DEGRADED
        assert status.error is not None
        assert len(status.error) > 0


# ---------------------------------------------------------------------------
# TestThreadSafety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Concurrent calls to start/stop must not raise or corrupt state."""

    def test_concurrent_starts_do_not_raise(self, tmp_path):
        svc = _make_service(tmp_path)
        errors = []

        def run_start():
            try:
                svc.start()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_start) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_starts_leave_valid_status(self, tmp_path):
        svc = _make_service(tmp_path)

        def run_start():
            svc.start()

        threads = [threading.Thread(target=run_start) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        status = svc.get_status().status
        assert status in (
            LeaseLifecycleStatus.ACTIVE,
            LeaseLifecycleStatus.DEGRADED,
            LeaseLifecycleStatus.INACTIVE,
        )

    def test_concurrent_start_and_stop_do_not_raise(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()  # Pre-start so stop has something to clean up

        errors = []

        def run_stop():
            try:
                svc.stop()
            except Exception as e:
                errors.append(e)

        def run_start():
            try:
                svc.start()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=run_stop),
            threading.Thread(target=run_start),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
