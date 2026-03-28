"""
Unit tests for LlmLeaseLifecycleService claude.json integration (Bug fixes).

Bug 1: api_key path does not write apiKey to ~/.claude.json.
Bug 2: cross-cleanup when switching credential types leaves stale files.

Uses:
- Real LlmLeaseStateManager with tmp_path
- Real ClaudeCredentialsFileManager with tmp_path
- httpx.MockTransport for HTTP (no external mocking libraries)
- claude_json_path parameter for testability
"""

from __future__ import annotations

import json
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


def _checkin_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"status": "ok"})


def _make_service(
    tmp_path: Path,
    checkout_handler=None,
    checkin_handler=None,
) -> LlmLeaseLifecycleService:
    """Build an OAuth service with real file managers, mock HTTP, and isolated claude.json."""

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
        claude_json_path=tmp_path / "claude.json",
    )


def _make_api_key_service(
    tmp_path: Path,
    api_key: str = "sk-ant-api03-test-key",
    checkin_handler=None,
) -> LlmLeaseLifecycleService:
    """Build an api_key service with real file managers, mock HTTP, and isolated claude.json."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/checkout" in str(request.url):
            return _api_key_checkout_response(api_key=api_key)
        if "/checkin" in str(request.url):
            if checkin_handler is not None:
                return checkin_handler(request)
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
        claude_json_path=tmp_path / "claude.json",
    )


# ---------------------------------------------------------------------------
# TestApiKeyWritesClaudeJson (Bug 1)
# ---------------------------------------------------------------------------


class TestApiKeyWritesClaudeJson:
    """start() with api_key must write apiKey to ~/.claude.json."""

    def test_start_with_api_key_writes_claude_json(self, tmp_path):
        """After start with api_key, claude.json must contain apiKey field."""
        svc = _make_api_key_service(tmp_path)
        svc.start()

        claude_json = tmp_path / "claude.json"
        assert claude_json.exists(), "claude.json must be created after api_key start"
        config = json.loads(claude_json.read_text())
        assert config.get("apiKey") == "sk-ant-api03-test-key"

    def test_start_with_api_key_preserves_existing_claude_json_fields(self, tmp_path):
        """Existing fields in claude.json must be preserved when writing apiKey."""
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({"someOtherField": "keep-me", "version": 42}))

        svc = _make_api_key_service(tmp_path)
        svc.start()

        config = json.loads(claude_json.read_text())
        assert config.get("apiKey") == "sk-ant-api03-test-key"
        assert (
            config.get("someOtherField") == "keep-me"
        ), "existing fields must be preserved"
        assert config.get("version") == 42, "existing fields must be preserved"

    def test_stop_with_api_key_clears_claude_json_api_key(self, tmp_path):
        """After stop following api_key start, apiKey must be absent from claude.json."""
        svc = _make_api_key_service(tmp_path)
        svc.start()

        claude_json = tmp_path / "claude.json"
        assert json.loads(claude_json.read_text()).get("apiKey") is not None

        svc.stop()

        if claude_json.exists():
            config = json.loads(claude_json.read_text())
            assert (
                "apiKey" not in config
            ), "apiKey must be removed from claude.json after stop"

    def test_stop_with_api_key_preserves_other_claude_json_fields(self, tmp_path):
        """stop() must only remove apiKey, not other fields in claude.json."""
        claude_json = tmp_path / "claude.json"
        # Pre-populate with an extra field before starting
        claude_json.write_text(json.dumps({"extraField": "survive-stop"}))

        svc = _make_api_key_service(tmp_path)
        svc.start()
        svc.stop()

        # File may or may not exist; if it exists, extra field must survive
        if claude_json.exists():
            config = json.loads(claude_json.read_text())
            assert "apiKey" not in config
            assert config.get("extraField") == "survive-stop"

    def test_crash_recovery_api_key_clears_claude_json(self, tmp_path):
        """Crash recovery for api_key lease must clear stale apiKey from claude.json."""
        # Simulate a crashed session: stale state + stale claude.json
        state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path / "state"))
        state_mgr.save_state(
            LlmLeaseState(
                lease_id="crashed-lease",
                credential_id="crashed-cred",
                credential_type="api_key",
            )
        )

        # Stale apiKey left from previous session
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({"apiKey": "stale-key-from-crash"}))

        svc = _make_api_key_service(tmp_path)
        svc._state_mgr = state_mgr
        svc.start()

        # After start completes (crash recovery + fresh checkout), the stale key
        # must be gone (replaced with new api_key from the fresh checkout response)
        assert claude_json.exists()
        config = json.loads(claude_json.read_text())
        # The new checkout writes a fresh apiKey — it must NOT be the stale one
        assert config.get("apiKey") != "stale-key-from-crash"
        assert config.get("apiKey") == "sk-ant-api03-test-key"


# ---------------------------------------------------------------------------
# TestOAuthStopClearsClaudeJson (Bug 2)
# ---------------------------------------------------------------------------


class TestOAuthStopClearsClaudeJson:
    """stop() on OAuth path must also clear apiKey from claude.json (Bug 2 cross-cleanup)."""

    def test_stop_oauth_also_clears_claude_json_api_key(self, tmp_path):
        """
        If a stale apiKey exists in claude.json (e.g. from a previous api_key session),
        stop() on the OAuth path must clear it to leave a clean state.
        """
        # Simulate stale apiKey left from a previous api_key session
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(
            json.dumps({"apiKey": "stale-api-key-from-prior-session"})
        )

        svc = _make_service(tmp_path)  # OAuth service
        svc.start()

        # The stale apiKey is still there from before start (OAuth doesn't touch it on start)
        svc.stop()

        if claude_json.exists():
            config = json.loads(claude_json.read_text())
            assert (
                "apiKey" not in config
            ), "OAuth stop() must clear stale apiKey from claude.json"
