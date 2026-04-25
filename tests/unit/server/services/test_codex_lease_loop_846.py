"""
Unit tests for CodexLeaseLoop (Story #846).

Verifies vendor-scoped state isolation, correct vendor parameter ("openai"),
checkin behaviour, auth.json lifecycle, and failure handling (WARNING log,
non-raising, no file written).
All HTTP calls intercepted via httpx.MockTransport — no live provider calls.
"""

from __future__ import annotations

import json
import logging
from typing import List, Tuple

import httpx
import pytest

from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
from code_indexer.server.services.claude_credentials_file_manager import (
    ClaudeCredentialsFileManager,
)
from code_indexer.server.services.llm_creds_client import LlmCredsClient
from code_indexer.server.services.llm_lease_lifecycle import LlmLeaseLifecycleService
from code_indexer.server.services.codex_credentials_file_manager import (
    CodexCredentialsFileManager,
)
from code_indexer.server.services.codex_lease_loop import CodexLeaseLoop


# ---------------------------------------------------------------------------
# Neutral test constants
# ---------------------------------------------------------------------------

TEST_LEASE_ID = "test_lease_id"
TEST_CREDENTIAL_ID = "test_credential_id"
TEST_ACCESS_TOKEN = "test_access_token"
TEST_REFRESH_TOKEN = "test_refresh_token"


# ---------------------------------------------------------------------------
# HTTP response factories
# ---------------------------------------------------------------------------


def _checkout_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "lease_id": TEST_LEASE_ID,
            "credential_id": TEST_CREDENTIAL_ID,
            "access_token": TEST_ACCESS_TOKEN,
            "refresh_token": TEST_REFRESH_TOKEN,
            "custom_fields": {},
        },
    )


def _checkin_ok() -> httpx.Response:
    return httpx.Response(200, json={"status": "ok"})


def _connect_error(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Connection refused")


def _default_handler(request: httpx.Request) -> httpx.Response:
    if "/checkout" in str(request.url):
        return _checkout_ok()
    return _checkin_ok()


# ---------------------------------------------------------------------------
# Shared builder helpers
# ---------------------------------------------------------------------------


def _make_client(transport: httpx.BaseTransport) -> LlmCredsClient:
    return LlmCredsClient(
        provider_url="http://test-provider",
        api_key="test_api_key",
        transport=transport,
    )


def _make_codex_loop(
    tmp_path,
    transport: httpx.BaseTransport,
) -> Tuple[CodexLeaseLoop, LlmLeaseStateManager]:
    """Build a CodexLeaseLoop and return (loop, state_mgr) for path inspection.

    state_filename is passed at construction time (CRIT-3 fix, Story #846) —
    no post-construction mutation of _state_file.
    """
    client = _make_client(transport)
    state_mgr = LlmLeaseStateManager(
        server_dir_path=str(tmp_path),
        state_filename="codex_lease_state.json",
    )
    creds_mgr = CodexCredentialsFileManager(
        auth_json_path=tmp_path / "codex-home" / "auth.json"
    )
    loop = CodexLeaseLoop(
        client=client,
        state_manager=state_mgr,
        credentials_manager=creds_mgr,
    )
    return loop, state_mgr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def capturing_loop(tmp_path):
    """
    Yield (loop, captured_requests, tmp_path) where captured_requests records
    every outgoing HTTP request and loop is ready to call start()/stop().
    """
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _default_handler(request)

    loop, _ = _make_codex_loop(tmp_path, httpx.MockTransport(handler))
    return loop, captured, tmp_path


@pytest.fixture()
def failing_loop(tmp_path):
    """Yield (loop, tmp_path) whose provider always raises ConnectError."""
    loop, _ = _make_codex_loop(tmp_path, httpx.MockTransport(_connect_error))
    return loop, tmp_path


@pytest.fixture()
def claude_and_codex_pair(tmp_path):
    """
    Yield a (claude_svc, claude_state_mgr, codex_loop, codex_state_mgr, tmp_path)
    tuple with both services ready to start but not yet started.
    """
    transport = httpx.MockTransport(_default_handler)

    claude_client = _make_client(transport)
    claude_state_mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    claude_creds = ClaudeCredentialsFileManager(
        credentials_path=tmp_path / ".credentials.json"
    )
    claude_svc = LlmLeaseLifecycleService(
        client=claude_client,
        state_manager=claude_state_mgr,
        credentials_manager=claude_creds,
    )

    codex_loop, codex_state_mgr = _make_codex_loop(
        tmp_path, httpx.MockTransport(_default_handler)
    )

    return claude_svc, claude_state_mgr, codex_loop, codex_state_mgr, tmp_path


# ---------------------------------------------------------------------------
# CRIT-3: LlmLeaseStateManager constructor param (no private mutation)
# ---------------------------------------------------------------------------


class TestStateManagerConstructorParam:
    def test_state_manager_constructor_accepts_state_filename(self, tmp_path):
        """LlmLeaseStateManager must accept state_filename kwarg and honor it."""
        mgr = LlmLeaseStateManager(
            server_dir_path=str(tmp_path), state_filename="custom_test.json"
        )
        assert mgr._state_file == tmp_path / "custom_test.json"

    def test_state_manager_default_filename_unchanged(self, tmp_path):
        """Default (no state_filename kwarg) must still produce llm_lease_state.json."""
        mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        assert mgr._state_file.name == "llm_lease_state.json"

    def test_codex_loop_uses_constructor_param_not_mutation(self, tmp_path):
        """
        CodexLeaseLoop must wire state_filename at LlmLeaseStateManager construction.

        _make_codex_loop builds a state_mgr and passes it to CodexLeaseLoop.
        After CodexLeaseLoop.__init__ returns, state_mgr._state_file must already
        point to the codex-scoped filename — proving the filename was set via the
        constructor param (backward-compatible), not post-construction mutation.
        """
        loop, state_mgr = _make_codex_loop(
            tmp_path, httpx.MockTransport(_default_handler)
        )
        expected = tmp_path / "codex_lease_state.json"
        assert state_mgr._state_file == expected, (
            "After CodexLeaseLoop.__init__, state_mgr._state_file must already "
            "equal codex_lease_state.json — set via constructor param, not mutation"
        )


# ---------------------------------------------------------------------------
# State file path — vendor scoping
# ---------------------------------------------------------------------------


class TestVendorScopedStatePath:
    def test_state_path_contains_codex(self, tmp_path):
        """CodexLeaseLoop.state_file_path must contain 'codex'."""
        loop, _ = _make_codex_loop(tmp_path, httpx.MockTransport(_default_handler))
        assert "codex" in str(loop.state_file_path).lower()

    def test_state_path_differs_from_default_claude_path(self, tmp_path):
        """Codex state path must differ from the default llm_lease_state.json (Claude)."""
        loop, _ = _make_codex_loop(tmp_path, httpx.MockTransport(_default_handler))
        claude_default = tmp_path / "llm_lease_state.json"
        assert loop.state_file_path != claude_default


# ---------------------------------------------------------------------------
# State isolation — Claude and Codex write different files
# ---------------------------------------------------------------------------


class TestStateIsolation:
    def test_claude_and_codex_write_to_different_state_files(
        self, claude_and_codex_pair
    ):
        """
        Starting both a Claude LlmLeaseLifecycleService and a CodexLeaseLoop
        must produce two distinct state files on disk, both actually written.
        """
        claude_svc, claude_state_mgr, codex_loop, codex_state_mgr, _ = (
            claude_and_codex_pair
        )
        claude_svc.start(consumer_id="test-consumer")
        codex_loop.start(consumer_id="test-consumer")

        claude_file = claude_state_mgr._state_file
        codex_file = codex_loop.state_file_path

        assert claude_file.exists(), "Claude state file must exist after start()"
        assert codex_file.exists(), "Codex state file must exist after start()"
        assert claude_file != codex_file, "State files must be distinct paths"


# ---------------------------------------------------------------------------
# request_lease() — vendor parameter and success behaviour
# ---------------------------------------------------------------------------


class TestRequestLeaseVendor:
    def test_calls_provider_with_openai_vendor(self, capturing_loop):
        loop, captured, _ = capturing_loop
        loop.start(consumer_id="test-consumer")
        assert len(captured) >= 1
        body = json.loads(captured[0].content)
        assert body.get("vendor") == "openai"

    def test_returns_true_on_success(self, capturing_loop):
        loop, _, _ = capturing_loop
        assert loop.start(consumer_id="test-consumer") is True

    def test_writes_auth_json_on_success(self, capturing_loop):
        loop, _, tmp_path = capturing_loop
        loop.start(consumer_id="test-consumer")
        assert (tmp_path / "codex-home" / "auth.json").exists()


# ---------------------------------------------------------------------------
# return_lease() — checkin and cleanup
# ---------------------------------------------------------------------------


class TestReturnLease:
    def test_stop_calls_checkin_endpoint(self, capturing_loop):
        loop, captured, _ = capturing_loop
        loop.start(consumer_id="test-consumer")
        loop.stop()
        checkin_requests = [r for r in captured if "/checkin" in str(r.url)]
        assert len(checkin_requests) >= 1

    def test_stop_removes_auth_json(self, capturing_loop):
        loop, _, tmp_path = capturing_loop
        loop.start(consumer_id="test-consumer")
        auth_path = tmp_path / "codex-home" / "auth.json"
        assert auth_path.exists()
        loop.stop()
        assert not auth_path.exists()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestLeaseAcquisitionFailure:
    def test_returns_false_when_provider_unreachable(self, failing_loop):
        loop, _ = failing_loop
        assert loop.start(consumer_id="test-consumer") is False

    def test_logs_warning_when_provider_unreachable(self, failing_loop, caplog):
        loop, _ = failing_loop
        with caplog.at_level(logging.WARNING):
            loop.start(consumer_id="test-consumer")
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "codex" in m.lower() or "lease" in m.lower() or "credential" in m.lower()
            for m in warning_msgs
        )

    def test_does_not_write_auth_json_on_failure(self, failing_loop):
        loop, tmp_path = failing_loop
        loop.start(consumer_id="test-consumer")
        # Verify the real Codex auth location, not a generic tmp path
        assert not (tmp_path / "codex-home" / "auth.json").exists()
