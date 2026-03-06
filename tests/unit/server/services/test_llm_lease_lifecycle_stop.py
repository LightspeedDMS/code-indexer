"""
Unit tests for LlmLeaseLifecycleService.stop() (Story #366).

Covers:
- Graceful shutdown: checkin with token writeback from .credentials.json
- File cleanup: .credentials.json deleted, state file cleared
- Status transition to INACTIVE
- Edge cases: stop when INACTIVE, stop when DEGRADED

Uses:
- Real LlmLeaseStateManager with tmp_path
- Real ClaudeCredentialsFileManager with tmp_path
- httpx.MockTransport for HTTP (no external mocking libraries)
"""

from __future__ import annotations

import json
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
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    """stop() with active lease: checkin with writeback + cleanup files."""

    def test_stop_calls_checkin_with_lease_id(self, tmp_path):
        checkin_bodies = []

        def checkin_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            checkin_bodies.append(body)
            return _checkin_response()

        svc = _make_service(tmp_path, checkin_handler=checkin_handler)
        svc.start()
        svc.stop()

        assert any(b.get("lease_id") == "lease-001" for b in checkin_bodies)

    def test_stop_includes_current_tokens_in_checkin(self, tmp_path):
        """Tokens read from .credentials.json after Claude CLI may have refreshed them."""
        checkin_bodies = []

        def checkin_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            checkin_bodies.append(body)
            return _checkin_response()

        svc = _make_service(tmp_path, checkin_handler=checkin_handler)
        svc.start()

        # Simulate token refresh by overwriting .credentials.json
        creds_path = tmp_path / "creds" / ".credentials.json"
        creds_mgr = ClaudeCredentialsFileManager(credentials_path=creds_path)
        creds_mgr.write_credentials(
            access_token="refreshed-access",
            refresh_token="refreshed-refresh",
        )

        svc.stop()

        # Last checkin body (from stop) should have refreshed tokens
        stop_checkin = checkin_bodies[-1]
        assert stop_checkin["access_token"] == "refreshed-access"
        assert stop_checkin["refresh_token"] == "refreshed-refresh"

    def test_stop_deletes_credentials_file(self, tmp_path):
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = _make_service(tmp_path)
        svc.start()
        assert creds_path.exists()

        svc.stop()
        assert not creds_path.exists()

    def test_stop_clears_state_file(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()

        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        assert state_mgr.load_state() is not None  # State present after start

        svc.stop()
        assert state_mgr.load_state() is None  # State cleared after stop

    def test_stop_sets_status_to_inactive(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        svc.stop()
        assert svc.get_status().status == LeaseLifecycleStatus.INACTIVE

    def test_stop_clears_lease_id_from_status(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        assert svc.get_status().lease_id == "lease-001"

        svc.stop()
        assert svc.get_status().lease_id is None

    def test_stop_when_inactive_is_no_op(self, tmp_path):
        """stop() on a fresh (never-started) service should not raise."""
        svc = _make_service(tmp_path)
        svc.stop()  # Should not raise
        assert svc.get_status().status == LeaseLifecycleStatus.INACTIVE

    def test_stop_when_degraded_sets_inactive(self, tmp_path):
        """stop() after a failed start (DEGRADED) should not raise and sets INACTIVE."""
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
        assert svc.get_status().status == LeaseLifecycleStatus.DEGRADED

        svc.stop()  # Should not raise
        assert svc.get_status().status == LeaseLifecycleStatus.INACTIVE
