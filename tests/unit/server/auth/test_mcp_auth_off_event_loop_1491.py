"""Story #1491 AC1: MCP auth dependency no longer runs bcrypt and
synchronous DB work on the event loop (Finding B1, CRITICAL, report rank 1).

get_current_user_for_mcp (async def, depended on at protocol.py:966-970)
must offload its synchronous, blocking work -- MCPCredentialManager's
bcrypt-based verify_credential, elevated_session_manager.create's sync DB
round-trip, and get_current_user's sync user/blacklist DB read -- to a
worker thread via anyio.to_thread.run_sync, so a second concurrent MCP
request is served without waiting for the first authentication to finish.

These tests prove the actual thread the blocking call runs on (not just
that a mock was called), the same "record threading.current_thread().ident"
technique test_invoke_handler_executor.py uses for the sync-dispatch branch.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import MagicMock

import pytest

import code_indexer.server.app  # noqa: F401 -- import-time side effect only

import code_indexer.server.auth.dependencies as _deps
from code_indexer.server.auth.user_manager import User, UserRole

_TEST_USERNAME = "alice"
_DUMMY_HASH = "not-a-real-hash"
_CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Precomputed (not dynamically assembled at test time) base64 encoding of
# the fixed non-sensitive placeholder pair "test-client-thread-check:
# test-client-secret-value", used only to exercise the Basic-auth parsing
# branch of get_mcp_user_from_credentials.
_BASIC_AUTH_HEADER_VALUE = (
    "Basic dGVzdC1jbGllbnQtdGhyZWFkLWNoZWNrOnRlc3QtY2xpZW50LXNlY3JldC12YWx1ZQ=="
)


def _make_admin_user() -> User:
    return User(
        username=_TEST_USERNAME,
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
        created_at=_CREATED_AT,
    )


def _make_basic_auth_request() -> MagicMock:
    """MagicMock Request with a precomputed Basic-auth Authorization header."""
    req = MagicMock()
    req.method = "GET"
    req.headers.get.return_value = _BASIC_AUTH_HEADER_VALUE
    req.state.user_jti = None
    return req


@pytest.fixture
def _wire_deps() -> Generator[dict, None, None]:
    """Wire mcp_credential_manager/user_manager/elevated_session_manager
    module globals; restore originals after each test."""
    orig_cred = _deps.mcp_credential_manager
    orig_um = _deps.user_manager
    orig_esm = _deps.elevated_session_manager

    mock_cred = MagicMock()
    mock_um = MagicMock()
    mock_esm = MagicMock()

    _deps.mcp_credential_manager = mock_cred
    _deps.user_manager = mock_um
    _deps.elevated_session_manager = mock_esm

    yield {"cred": mock_cred, "um": mock_um, "esm": mock_esm}

    _deps.mcp_credential_manager = orig_cred
    _deps.user_manager = orig_um
    _deps.elevated_session_manager = orig_esm


def test_verify_credential_runs_off_event_loop_thread(_wire_deps) -> None:
    """MCPCredentialManager.verify_credential (bcrypt, 100-300ms CPU) must
    run on a worker thread, not the event loop thread that dispatches
    get_mcp_user_from_credentials."""
    event_loop_thread_id = threading.current_thread().ident
    observed_thread_ids: list = []

    def recording_verify_credential(client_id, client_secret):
        observed_thread_ids.append(threading.current_thread().ident)
        return _TEST_USERNAME

    _wire_deps["cred"].verify_credential.side_effect = recording_verify_credential
    _wire_deps["um"].get_user.return_value = _make_admin_user()

    req = _make_basic_auth_request()
    result = asyncio.get_event_loop().run_until_complete(
        _deps.get_mcp_user_from_credentials(req)
    )

    assert result is not None and result.username == _TEST_USERNAME
    assert len(observed_thread_ids) == 1
    assert observed_thread_ids[0] != event_loop_thread_id, (
        "verify_credential (bcrypt) must run off the event loop thread"
    )


def test_elevated_session_create_runs_off_event_loop_thread(_wire_deps) -> None:
    """elevated_session_manager.create's synchronous DB round-trip must run
    on a worker thread, not the event loop thread."""
    event_loop_thread_id = threading.current_thread().ident
    observed_thread_ids: list = []

    def recording_create(**kwargs):
        observed_thread_ids.append(threading.current_thread().ident)

    _wire_deps["cred"].verify_credential.return_value = _TEST_USERNAME
    _wire_deps["um"].get_user.return_value = _make_admin_user()
    _wire_deps["esm"].create.side_effect = recording_create

    req = _make_basic_auth_request()
    asyncio.get_event_loop().run_until_complete(
        _deps.get_mcp_user_from_credentials(req)
    )

    assert len(observed_thread_ids) == 1
    assert observed_thread_ids[0] != event_loop_thread_id, (
        "elevated_session_manager.create must run off the event loop thread"
    )


def test_bearer_jwt_fallback_runs_off_event_loop_thread() -> None:
    """get_current_user_for_mcp's fallback Bearer/JWT path calls the sync
    get_current_user(), whose real external dependencies -- jwt_manager
    (JWT validation, called once inside get_current_user and again for jti
    extraction) and user_manager (user DB read) -- must ALL run on a
    worker thread, not the event loop thread. get_current_user itself is
    never patched: only its external boundaries are."""
    event_loop_thread_id = threading.current_thread().ident
    jwt_observed_thread_ids: list = []
    get_user_observed_thread_ids: list = []
    admin_user = _make_admin_user()

    mock_jwt_manager = MagicMock()

    def recording_validate_token(token):
        jwt_observed_thread_ids.append(threading.current_thread().ident)
        return {"username": _TEST_USERNAME}  # no "jti" -> blacklist check skipped

    mock_jwt_manager.validate_token.side_effect = recording_validate_token

    mock_user_manager = MagicMock()

    def recording_get_user(username):
        get_user_observed_thread_ids.append(threading.current_thread().ident)
        return admin_user

    mock_user_manager.get_user.side_effect = recording_get_user

    orig_jwt_manager = _deps.jwt_manager
    orig_user_manager = _deps.user_manager
    orig_oauth_manager = _deps.oauth_manager
    _deps.jwt_manager = mock_jwt_manager
    _deps.user_manager = mock_user_manager
    _deps.oauth_manager = None  # force the JWT fallback branch
    try:
        req = MagicMock()
        req.method = "GET"
        req.headers.get.return_value = "Bearer sometoken"
        req.cookies.get.return_value = None
        req.state.user_jti = None

        result = asyncio.get_event_loop().run_until_complete(
            _deps.get_current_user_for_mcp(req)
        )
    finally:
        _deps.jwt_manager = orig_jwt_manager
        _deps.user_manager = orig_user_manager
        _deps.oauth_manager = orig_oauth_manager

    assert result.username == _TEST_USERNAME
    # jwt_manager.validate_token is called at least once inside
    # get_current_user, and again for jti extraction in
    # get_current_user_for_mcp -- every call must be off the event loop.
    assert len(jwt_observed_thread_ids) >= 1
    assert all(tid != event_loop_thread_id for tid in jwt_observed_thread_ids), (
        "every jwt_manager.validate_token call must run off the event loop thread"
    )
    assert len(get_user_observed_thread_ids) == 1
    assert get_user_observed_thread_ids[0] != event_loop_thread_id, (
        "user_manager.get_user must run off the event loop thread"
    )


def test_mcp_invalid_credentials_still_raise_401(_wire_deps) -> None:
    """A verify_credential result of None (invalid credentials) must still
    propagate as HTTPException(401) even though the call is now offloaded
    to a worker thread."""
    from fastapi import HTTPException

    _wire_deps["cred"].verify_credential.return_value = None

    req = _make_basic_auth_request()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )

    assert exc_info.value.status_code == 401


def test_worker_thread_exception_propagates_to_caller(_wire_deps) -> None:
    """An exception raised INSIDE the offloaded verify_credential call
    (worker thread) must propagate to the awaiting caller unchanged, not
    be swallowed by the thread-offload mechanism."""

    def failing_verify_credential(client_id, client_secret):
        raise RuntimeError("simulated bcrypt backend failure")

    _wire_deps["cred"].verify_credential.side_effect = failing_verify_credential

    req = _make_basic_auth_request()

    with pytest.raises(RuntimeError, match="simulated bcrypt backend failure"):
        asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )
