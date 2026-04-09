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
                return checkout_handler(request)  # type: ignore[no-any-return]
            return _checkout_response()
        if "/checkin" in str(request.url):
            if checkin_handler is not None:
                return checkin_handler(request)  # type: ignore[no-any-return]
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

    def test_start_succeeds_even_without_anthropic_api_key_in_env(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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
        state_mgr.save_state(
            LlmLeaseState(lease_id="old-lease", credential_id="old-cred")
        )

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
        state_mgr.save_state(
            LlmLeaseState(lease_id="old-lease", credential_id="old-cred")
        )

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
        state_mgr.save_state(
            LlmLeaseState(lease_id="old-lease", credential_id="old-cred")
        )

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
        state_mgr.save_state(
            LlmLeaseState(lease_id="old-lease", credential_id="old-cred")
        )

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


# ---------------------------------------------------------------------------
# TestStartWithApiKeyCredential
# ---------------------------------------------------------------------------


def _api_key_checkout_response(
    lease_id: str = "lease-002",
    credential_id: str = "cred-002",
    api_key: str = "sk-ant-api03-test-key",
) -> httpx.Response:
    """Checkout response for api_key credential type (no access/refresh tokens)."""
    return httpx.Response(
        200,
        json={
            "lease_id": lease_id,
            "credential_id": credential_id,
            "api_key": api_key,
        },
    )


def _make_api_key_service(
    tmp_path: Path,
    checkin_handler=None,
) -> LlmLeaseLifecycleService:
    """Build a service whose provider returns api_key credentials."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/checkout" in str(request.url):
            return _api_key_checkout_response()
        if "/checkin" in str(request.url):
            if checkin_handler is not None:
                return checkin_handler(request)  # type: ignore[no-any-return]
            return _checkin_response()
        return httpx.Response(404, text="Not found")

    transport = _make_transport(handler)
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


class TestStartWithApiKeyCredential:
    """start() when the provider returns api_key credential type (5 tests).

    Covers both the api_key path and OAuth contrast tests to document
    which behavior belongs to each credential type.
    """

    def test_start_with_api_key_sets_environ(self, tmp_path, monkeypatch):
        """When checkout returns api_key, ANTHROPIC_API_KEY must be set in os.environ."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        svc = _make_api_key_service(tmp_path)
        svc.start()
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api03-test-key"

    def test_start_with_api_key_does_not_write_credentials_file(self, tmp_path):
        """When checkout returns api_key, .credentials.json must NOT be created."""
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = _make_api_key_service(tmp_path)
        svc.start()
        assert not creds_path.exists()

    def test_start_with_oauth_writes_credentials_file(self, tmp_path):
        """Contrast: OAuth tokens (no api_key in response) still writes .credentials.json."""
        creds_path = tmp_path / "creds" / ".credentials.json"
        svc = _make_service(tmp_path)  # default handler returns OAuth tokens
        svc.start()
        assert creds_path.exists()

    def test_start_with_oauth_does_not_set_environ(self, tmp_path, monkeypatch):
        """Contrast: OAuth tokens (no api_key in response) must NOT set ANTHROPIC_API_KEY."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        svc = _make_service(tmp_path)  # default handler returns OAuth tokens
        svc.start()
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_start_with_api_key_status_is_active(self, tmp_path):
        """api_key credential type results in ACTIVE status, same as OAuth."""
        svc = _make_api_key_service(tmp_path)
        svc.start()
        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE


# ---------------------------------------------------------------------------
# TestCrashRecoveryWithApiKey (H1)
# ---------------------------------------------------------------------------


class TestCrashRecoveryWithApiKey:
    """Crash recovery must call plain checkin (not writeback) for api_key leases."""

    def test_crash_recovery_with_api_key_does_plain_checkin(self, tmp_path):
        """
        If residual state has credential_type='api_key', crash recovery must
        call _do_plain_checkin (checkin without tokens), not _do_checkin_with_writeback.

        Verified by confirming the checkin request has NO access_token/refresh_token.
        """
        # Pre-populate residual state with api_key credential type
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(
            LlmLeaseState(
                lease_id="crashed-api-key-lease",
                credential_id="crashed-cred",
                credential_type="api_key",
            )
        )

        checkin_bodies = []

        def checkin_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            checkin_bodies.append(body)
            return _checkin_response()

        # Build service with wired checkin handler, then inject the pre-populated state mgr
        svc = _make_api_key_service(tmp_path, checkin_handler=checkin_handler)
        svc._state_mgr = state_mgr

        svc.start()

        # First checkin is the crash-recovery checkin
        assert len(checkin_bodies) >= 1
        recovery_checkin = checkin_bodies[0]
        assert recovery_checkin["lease_id"] == "crashed-api-key-lease"
        # Plain checkin: must NOT include OAuth token fields
        assert "access_token" not in recovery_checkin
        assert "refresh_token" not in recovery_checkin

    def test_crash_recovery_with_api_key_state_is_cleared_then_refreshed(
        self, tmp_path
    ):
        """After crash recovery for api_key lease, state file holds the new lease."""
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(
            LlmLeaseState(
                lease_id="crashed-api-key-lease",
                credential_id="crashed-cred",
                credential_type="api_key",
            )
        )

        svc = _make_api_key_service(tmp_path)
        svc._state_mgr = state_mgr
        svc.start()

        # After start completes, a fresh state (new lease from provider) must be present
        loaded = state_mgr.load_state()
        assert loaded is not None
        assert loaded.lease_id != "crashed-api-key-lease"


# ---------------------------------------------------------------------------
# TestCredentialTypeResetOnFailedStart (H2)
# ---------------------------------------------------------------------------


class TestCredentialTypeResetOnFailedStart:
    """_credential_type must be reset to None when start() enters DEGRADED."""

    def test_credential_type_reset_on_failed_start(self, tmp_path):
        """
        After a successful api_key start, a subsequent failed start must reset
        _credential_type to None so stop() does not use the stale credential type.
        """
        # First start: succeeds with api_key
        svc = _make_api_key_service(tmp_path)
        svc.start()
        assert svc.get_status().status == LeaseLifecycleStatus.ACTIVE
        assert svc._credential_type == "api_key"

        # Second start: provider unreachable → DEGRADED
        failing_transport = _make_transport(_failing_handler)
        from code_indexer.server.services.llm_creds_client import (
            LlmCredsClient as _LlmCredsClient,
        )

        svc._client = _LlmCredsClient(
            provider_url="http://unreachable",
            api_key="key",
            transport=failing_transport,
        )

        svc.start()
        assert svc.get_status().status == LeaseLifecycleStatus.DEGRADED
        # H2 fix: _credential_type must be None after failed start
        assert svc._credential_type is None
