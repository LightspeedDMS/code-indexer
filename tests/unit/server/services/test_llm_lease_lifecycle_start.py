"""
Unit tests for LlmLeaseLifecycleService.start() (Story #366).

Covers:
- Happy path: checkout success with no residual state
- ANTHROPIC_API_KEY removal from os.environ
- Crash recovery: residual state causes checkin before fresh checkout
- Provider unreachable: DEGRADED status, server not blocked

Uses:
- Real LlmLeaseStateManager with tmp_path
- Real ClaudeCredentialsFileManager with tmp_path
- httpx.MockTransport for HTTP (no external mocking libraries)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from code_indexer.server.config.llm_lease_state import (
    LlmLeaseState,
    LlmLeaseStateManager,
)
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
# TestStartCheckoutSuccess
# ---------------------------------------------------------------------------


class TestStartCheckoutSuccess:
    """start() with no residual state, provider succeeds."""

    def test_status_is_active_after_start(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE

    def test_lease_id_stored_in_status(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        assert svc.get_status().lease_id == "lease-001"

    def test_credential_id_stored_in_status(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        assert svc.get_status().credential_id == "cred-001"

    def test_state_file_written_after_start(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state = state_mgr.load_state()
        assert state is not None
        assert state.lease_id == "lease-001"
        assert state.credential_id == "cred-001"

    def test_credentials_file_written_after_start(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        creds_path = tmp_path / "creds" / ".credentials.json"
        assert creds_path.exists()

    def test_credentials_file_contains_access_token(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        creds_mgr = ClaudeCredentialsFileManager(
            credentials_path=tmp_path / "creds" / ".credentials.json"
        )
        tokens = creds_mgr.read_credentials()
        assert tokens is not None
        assert tokens["access_token"] == "sk-ant-oat01-access"

    def test_credentials_file_contains_refresh_token(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        creds_mgr = ClaudeCredentialsFileManager(
            credentials_path=tmp_path / "creds" / ".credentials.json"
        )
        tokens = creds_mgr.read_credentials()
        assert tokens is not None
        assert tokens["refresh_token"] == "sk-ant-ort01-refresh"


# ---------------------------------------------------------------------------
# TestStartRemovesAnthropicApiKey
# ---------------------------------------------------------------------------


class TestStartRemovesAnthropicApiKey:
    """start() must remove ANTHROPIC_API_KEY from os.environ."""

    def test_anthropic_api_key_removed_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-old-key")
        svc = _make_service(tmp_path)
        svc.start()
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_start_succeeds_even_without_anthropic_api_key_in_env(self, tmp_path):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        svc = _make_service(tmp_path)
        svc.start()  # Should not raise
        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE


# ---------------------------------------------------------------------------
# TestStartWithCrashRecovery
# ---------------------------------------------------------------------------


class TestStartWithCrashRecovery:
    """start() with residual state from a previous crashed session."""

    def test_old_lease_checked_in_before_fresh_checkout(self, tmp_path):
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(LlmLeaseState(lease_id="old-lease", credential_id="old-cred"))

        creds_path = tmp_path / "creds" / ".credentials.json"
        creds_mgr = ClaudeCredentialsFileManager(credentials_path=creds_path)
        creds_mgr.write_credentials(
            access_token="old-access-token",
            refresh_token="old-refresh-token",
        )

        checkin_bodies = []

        def checkin_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            checkin_bodies.append(body)
            return _checkin_response()

        svc = _make_service(tmp_path, checkin_handler=checkin_handler)
        svc.start()

        assert len(checkin_bodies) == 1
        assert checkin_bodies[0]["lease_id"] == "old-lease"

    def test_old_lease_checkin_includes_token_writeback(self, tmp_path):
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(LlmLeaseState(lease_id="old-lease", credential_id="old-cred"))

        creds_path = tmp_path / "creds" / ".credentials.json"
        creds_mgr = ClaudeCredentialsFileManager(credentials_path=creds_path)
        creds_mgr.write_credentials(
            access_token="current-access-token",
            refresh_token="current-refresh-token",
        )

        checkin_bodies = []

        def checkin_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            checkin_bodies.append(body)
            return _checkin_response()

        svc = _make_service(tmp_path, checkin_handler=checkin_handler)
        svc.start()

        assert checkin_bodies[0]["access_token"] == "current-access-token"
        assert checkin_bodies[0]["refresh_token"] == "current-refresh-token"

    def test_fresh_checkout_performed_after_crash_recovery(self, tmp_path):
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(LlmLeaseState(lease_id="old-lease", credential_id="old-cred"))

        checkout_count = [0]
        new_lease_id = "new-lease-after-recovery"

        def checkout_handler(request: httpx.Request) -> httpx.Response:
            checkout_count[0] += 1
            return _checkout_response(
                lease_id=new_lease_id,
                credential_id="new-cred",
                access_token="new-access",
                refresh_token="new-refresh",
            )

        svc = _make_service(tmp_path, checkout_handler=checkout_handler)
        svc.start()

        assert checkout_count[0] == 1
        assert svc.get_status().lease_id == new_lease_id

    def test_status_is_active_after_crash_recovery(self, tmp_path):
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(LlmLeaseState(lease_id="old-lease", credential_id="old-cred"))

        svc = _make_service(tmp_path)
        svc.start()

        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE


# ---------------------------------------------------------------------------
# TestStartProviderUnreachable
# ---------------------------------------------------------------------------


class TestStartProviderUnreachable:
    """start() when provider is not reachable — must NOT block server startup."""

    def _make_failing_service(self, tmp_path: Path) -> LlmLeaseLifecycleService:
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
        return LlmLeaseLifecycleService(
            client=client,
            state_manager=state_manager,
            credentials_manager=creds_manager,
        )

    def test_status_is_degraded_when_provider_unreachable(self, tmp_path):
        svc = self._make_failing_service(tmp_path)
        svc.start()  # Must NOT raise
        assert svc.get_status().status == LeaseLifecycleStatus.DEGRADED

    def test_error_message_captured_in_status(self, tmp_path):
        svc = self._make_failing_service(tmp_path)
        svc.start()
        status = svc.get_status()
        assert status.error is not None
        assert len(status.error) > 0

    def test_credentials_file_not_created_when_degraded(self, tmp_path):
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = self._make_failing_service(tmp_path)
        svc.start()
        assert not creds_path.exists()
