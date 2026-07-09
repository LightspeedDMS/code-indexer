"""Behavior-pinning tests for Story #1155 — Login path convergence.

These tests are written BEFORE the refactor (AC8 TDD requirement) to pin:
- AC3: exact failure-path messages and types for both _authenticate() and login()
- AC5: behavior matrix — all 6 cases for both callers
- AC6: side-effect ordering — circuit-breaker pre-flight, success/failure recording order

Design constraints (per story spec):
- Stub ONLY self.session.post (the transport layer)
- NEVER mock _authenticate, login, _perform_login_request, or _complete_mfa_challenge
- Use call_args_list / call-order witnesses for AC6 ordering assertions

D3 decision documented here:
  login() PRESERVES current behavior: httpx.NetworkError and httpx.TimeoutException
  are swallowed into AuthenticationError (no NetworkError leaks to CLI).
  This is the explicit zero-behavior-change choice from D3.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int, json_data: Any = None, *, bad_json: bool = False
) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    if bad_json:
        resp.json.side_effect = json.JSONDecodeError("bad json", "", 0)
    else:
        resp.json.return_value = json_data or {}
    return resp


def _make_base_client(totp_provider=None, credentials=None):
    """Build a CIDXRemoteAPIClient with default test credentials."""
    from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

    creds = credentials or {"username": "admin", "password": "secret"}
    client = CIDXRemoteAPIClient(
        server_url="http://localhost:8000",
        credentials=creds,
        totp_provider=totp_provider,
    )
    return client


def _make_auth_client(totp_provider=None, project_root=None):
    """Build an AuthAPIClient."""
    from code_indexer.api_clients.auth_client import AuthAPIClient

    return AuthAPIClient(
        server_url="http://localhost:8000",
        project_root=project_root,
        totp_provider=totp_provider,
    )


def _stub_session(client: Any, responses: list) -> MagicMock:
    """Replace client._session with a mock whose post() yields the given responses."""
    mock: MagicMock = MagicMock()
    mock.post.side_effect = responses
    mock.is_closed = False
    client._session = mock
    return mock


def _stub_session_single(client: Any, response: Any) -> Any:
    """Replace client._session with a mock returning a single response."""
    client._session = MagicMock()
    client._session.post.return_value = response
    client._session.is_closed = False
    return client._session


# ===========================================================================
# AC3: Failure-path PARITY — exact messages and types pinned
# ===========================================================================


class TestAC3FailurePathParity:
    """Pin the exact error messages and types for both callers."""

    # --- _authenticate() exact messages ---

    def test_authenticate_malformed_200_exact_message(self) -> None:
        """_authenticate() malformed-200 must say 'No valid access token in response'."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(client, _mock_response(200, {"status": "ok"}))

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        assert str(exc.value) == "No valid access token in response"

    def test_authenticate_401_message_prefix(self) -> None:
        """_authenticate() 401 must start with 'Authentication failed: '."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(
            client, _mock_response(401, {"detail": "Invalid credentials"})
        )

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        assert str(exc.value).startswith("Authentication failed: ")

    def test_authenticate_other_non_200_message_prefix(self) -> None:
        """_authenticate() non-200/401 must start with 'Authentication error: '."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(
            client, _mock_response(503, {"detail": "Service unavailable"})
        )

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        assert str(exc.value).startswith("Authentication error: ")

    # --- login() exact messages ---

    def test_login_malformed_200_exact_message(self) -> None:
        """login() malformed-200 must say 'No access token in response' (no 'valid')."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        _stub_session_single(client, _mock_response(200, {"status": "ok"}))

        with pytest.raises(AuthenticationError) as exc:
            client.login("admin", "secret")

        assert str(exc.value) == "No access token in response"

    def test_login_401_message_prefix(self) -> None:
        """login() 401 must start with 'Authentication failed: '."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        _stub_session_single(
            client, _mock_response(401, {"detail": "Invalid username or password"})
        )

        with pytest.raises(AuthenticationError) as exc:
            client.login("admin", "wrong")

        assert str(exc.value).startswith("Authentication failed: ")

    def test_login_429_exact_message(self) -> None:
        """login() 429 must raise APIClientError with exact message."""
        from code_indexer.api_clients.base_client import APIClientError

        client = _make_auth_client()
        _stub_session_single(client, _mock_response(429, {}))

        with pytest.raises(APIClientError) as exc:
            client.login("admin", "secret")

        assert (
            str(exc.value)
            == "Too many login attempts. Please wait before trying again."
        )

    def test_login_other_non_200_message_prefix(self) -> None:
        """login() non-200/401/429 must raise APIClientError starting with 'Login failed: '."""
        from code_indexer.api_clients.base_client import APIClientError

        client = _make_auth_client()
        _stub_session_single(
            client, _mock_response(503, {"detail": "Service unavailable"})
        )

        with pytest.raises(APIClientError) as exc:
            client.login("admin", "secret")

        assert str(exc.value).startswith("Login failed: ")

    def test_authenticate_vs_login_malformed_200_messages_differ(self) -> None:
        """The two callers must have DIFFERENT malformed-200 messages (spec-documented divergence)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        base_client = _make_base_client()
        _stub_session_single(base_client, _mock_response(200, {"status": "ok"}))
        with pytest.raises(AuthenticationError) as exc_base:
            base_client._authenticate()

        auth_client = _make_auth_client()
        _stub_session_single(auth_client, _mock_response(200, {"status": "ok"}))
        with pytest.raises(AuthenticationError) as exc_auth:
            auth_client.login("admin", "secret")

        # Divergence is intentional and pinned
        assert str(exc_base.value) == "No valid access token in response"
        assert str(exc_auth.value) == "No access token in response"
        assert str(exc_base.value) != str(exc_auth.value)

    def test_authenticate_429_raises_authentication_error(self) -> None:
        """_authenticate() 429 raises AuthenticationError (falls to generic else branch)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(
            client, _mock_response(429, {"detail": "Too many attempts"})
        )

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        # Falls to else branch: "Authentication error: ..."
        assert str(exc.value).startswith("Authentication error: ")


# ===========================================================================
# AC5: Behavior matrix — 6 cases × 2 callers
# ===========================================================================


class TestAC5BehaviorMatrixAuthenticate:
    """Behavior matrix for _authenticate() — 6 cases."""

    def test_network_error_raises_network_error(self) -> None:
        """httpx.NetworkError → NetworkConnectionError (via classifier), records failure."""
        from code_indexer.api_clients.network_error_handler import (
            NetworkConnectionError,
        )

        client = _make_base_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        with pytest.raises(NetworkConnectionError):
            client._authenticate()

    def test_network_error_records_auth_failure(self) -> None:
        """httpx.NetworkError → _record_auth_failure() called, breaker affected."""
        client = _make_base_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        failures_before = client._auth_failures
        try:
            client._authenticate()
        except Exception:
            pass

        assert client._auth_failures == failures_before + 1, (
            "_record_auth_failure() must be called on NetworkError"
        )

    def test_network_error_does_not_persist_token(self) -> None:
        """httpx.NetworkError → token NOT stored."""
        client = _make_base_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        try:
            client._authenticate()
        except Exception:
            pass

        assert client._current_token is None

    def test_timeout_error_raises_network_error(self) -> None:
        """httpx.TimeoutException → NetworkTimeoutError (via classifier), records failure."""
        from code_indexer.api_clients.network_error_handler import NetworkTimeoutError

        client = _make_base_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.TimeoutException("timeout")
        client._session.is_closed = False

        with pytest.raises(NetworkTimeoutError):
            client._authenticate()

    def test_timeout_records_auth_failure(self) -> None:
        """httpx.TimeoutException → _record_auth_failure() called."""
        client = _make_base_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.TimeoutException("timeout")
        client._session.is_closed = False

        failures_before = client._auth_failures
        try:
            client._authenticate()
        except Exception:
            pass

        assert client._auth_failures == failures_before + 1

    def test_401_raises_authentication_error(self) -> None:
        """401 → AuthenticationError, records failure, no token persisted."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(
            client, _mock_response(401, {"detail": "Invalid credentials"})
        )

        failures_before = client._auth_failures
        with pytest.raises(AuthenticationError):
            client._authenticate()

        assert client._auth_failures == failures_before + 1
        assert client._current_token is None

    def test_429_raises_authentication_error_records_failure(self) -> None:
        """429 → AuthenticationError (falls to else), records failure, no token."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(client, _mock_response(429, {"detail": "rate limited"}))

        failures_before = client._auth_failures
        with pytest.raises(AuthenticationError):
            client._authenticate()

        assert client._auth_failures == failures_before + 1
        assert client._current_token is None

    def test_other_non_200_raises_authentication_error_records_failure(self) -> None:
        """non-200/401/429 → AuthenticationError, records failure, no token."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(client, _mock_response(503, {"detail": "server error"}))

        failures_before = client._auth_failures
        with pytest.raises(AuthenticationError):
            client._authenticate()

        assert client._auth_failures == failures_before + 1
        assert client._current_token is None

    def test_malformed_200_raises_authentication_error_records_failure(self) -> None:
        """Malformed 200 (no token) → AuthenticationError, records failure? No — AC9 preserves behavior.

        In current code: missing token path does NOT call _record_auth_failure().
        It raises AuthenticationError directly. Pin this.
        """
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        _stub_session_single(client, _mock_response(200, {"status": "ok"}))

        failures_before = client._auth_failures
        with pytest.raises(AuthenticationError):
            client._authenticate()

        # Current code does NOT record failure for malformed 200 — pin this behavior
        assert client._auth_failures == failures_before

    def test_success_records_auth_success(self) -> None:
        """200 with valid token → _record_auth_success() resets failure count."""
        client = _make_base_client()
        client._auth_failures = 3  # pre-set some failures
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-ok", "token_type": "bearer"}),
        )

        token = client._authenticate()

        assert token == "tok-ok"
        assert client._auth_failures == 0  # reset by _record_auth_success


class TestAC5BehaviorMatrixLogin:
    """Behavior matrix for login() — 6 cases.

    D3 decision: login() continues to surface network/timeout failures as
    AuthenticationError (swallowed, not NetworkError). Zero-behavior-change default.
    """

    def test_network_error_raises_authentication_error(self) -> None:
        """D3: httpx.NetworkError → AuthenticationError (swallowed), NOT NetworkError."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client.login("admin", "secret")

    def test_network_error_not_network_error_type_for_login(self) -> None:
        """D3: login() must NOT raise NetworkError on transport failure."""
        from code_indexer.api_clients.base_client import NetworkError

        client = _make_auth_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        try:
            client.login("admin", "secret")
            assert False, "should have raised"
        except NetworkError:
            assert False, "login() must NOT raise NetworkError (D3 decision)"
        except Exception:
            pass  # AuthenticationError or subclass — correct

    def test_network_error_does_not_call_breaker_methods(self) -> None:
        """login() must NOT touch circuit-breaker state on network error."""
        client = _make_auth_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.ConnectError("connection refused")
        client._session.is_closed = False

        failures_before = client._auth_failures
        try:
            client.login("admin", "secret")
        except Exception:
            pass

        assert client._auth_failures == failures_before, (
            "login() must NOT call _record_auth_failure() — no breaker side effects"
        )

    def test_timeout_raises_authentication_error(self) -> None:
        """D3: httpx.TimeoutException → AuthenticationError (swallowed)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.TimeoutException("timeout")
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client.login("admin", "secret")

    def test_timeout_does_not_touch_breaker(self) -> None:
        """login() TimeoutException must NOT touch circuit-breaker state."""
        client = _make_auth_client()
        client._session = MagicMock()
        client._session.post.side_effect = httpx.TimeoutException("timeout")
        client._session.is_closed = False

        failures_before = client._auth_failures
        try:
            client.login("admin", "secret")
        except Exception:
            pass

        assert client._auth_failures == failures_before

    def test_401_raises_authentication_error_no_breaker(self) -> None:
        """401 → AuthenticationError, NO breaker effect, no credentials stored."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        _stub_session_single(
            client, _mock_response(401, {"detail": "Invalid username or password"})
        )

        failures_before = client._auth_failures
        with pytest.raises(AuthenticationError):
            client.login("admin", "wrong")

        assert client._auth_failures == failures_before
        assert not client.credentials.get("username")

    def test_429_raises_api_client_error_no_breaker(self) -> None:
        """429 → APIClientError (NOT AuthenticationError), no breaker, no credentials."""
        from code_indexer.api_clients.base_client import (
            APIClientError,
            AuthenticationError,
        )

        client = _make_auth_client()
        _stub_session_single(client, _mock_response(429, {}))

        failures_before = client._auth_failures
        with pytest.raises(APIClientError) as exc:
            client.login("admin", "secret")

        # Must be APIClientError NOT AuthenticationError (429 has its own branch)
        assert not isinstance(exc.value, AuthenticationError), (
            "429 in login() must raise APIClientError, not AuthenticationError"
        )
        assert client._auth_failures == failures_before

    def test_other_non_200_raises_api_client_error(self) -> None:
        """non-200/401/429 → APIClientError, no breaker, no credentials stored."""
        from code_indexer.api_clients.base_client import APIClientError

        client = _make_auth_client()
        _stub_session_single(client, _mock_response(503, {"detail": "server error"}))

        failures_before = client._auth_failures
        with pytest.raises(APIClientError):
            client.login("admin", "secret")

        assert client._auth_failures == failures_before

    def test_malformed_200_raises_authentication_error(self) -> None:
        """Malformed 200 → AuthenticationError, no credentials stored."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        _stub_session_single(client, _mock_response(200, {"status": "ok"}))

        with pytest.raises(AuthenticationError):
            client.login("admin", "secret")

        assert not client.credentials.get("username")


# ===========================================================================
# AC6: Side-effect ordering
# ===========================================================================


class TestAC6SideEffectOrdering:
    """Pin the precise ordering of side effects."""

    def test_circuit_breaker_checked_before_post_on_authenticate(self) -> None:
        """(a) _check_circuit_breaker() must be called BEFORE any POST."""
        call_order: list[str] = []

        client = _make_base_client()

        original_check = client._check_circuit_breaker.__func__  # type: ignore[attr-defined]

        def tracking_check_cb(self) -> None:  # type: ignore[override]
            call_order.append("check_circuit_breaker")
            original_check(self)

        # Patch the method on the instance
        client._check_circuit_breaker = lambda: tracking_check_cb(client)  # type: ignore[method-assign]

        # Stub session to record call order too
        mock_session = MagicMock()
        client._session = mock_session
        client._session.is_closed = False

        def tracking_post(*args, **kwargs):
            call_order.append("post")
            return _mock_response(200, {"access_token": "tok", "token_type": "bearer"})

        mock_session.post.side_effect = tracking_post

        client._authenticate()

        assert call_order[0] == "check_circuit_breaker", (
            "_check_circuit_breaker must fire BEFORE the POST"
        )
        assert "post" in call_order

    def test_record_auth_success_before_store_token_on_non_mfa(self) -> None:
        """(b) on non-MFA success: _record_auth_success() before _store_token_persistently()."""
        call_order: list[str] = []

        client = _make_base_client()
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-order", "token_type": "bearer"}),
        )

        orig_success = client._record_auth_success.__func__  # type: ignore[attr-defined]
        orig_store = client._store_token_persistently.__func__  # type: ignore[attr-defined]

        def tracking_success(self):
            call_order.append("record_auth_success")
            orig_success(self)

        def tracking_store(self, token):
            call_order.append("store_token_persistently")
            orig_store(self, token)

        client._record_auth_success = lambda: tracking_success(client)  # type: ignore[method-assign]
        client._store_token_persistently = lambda t: tracking_store(client, t)  # type: ignore[method-assign]

        client._authenticate()

        assert "record_auth_success" in call_order
        assert "store_token_persistently" in call_order
        success_idx = call_order.index("record_auth_success")
        store_idx = call_order.index("store_token_persistently")
        assert success_idx < store_idx, (
            "_record_auth_success must come BEFORE _store_token_persistently"
        )

    def test_record_auth_success_before_store_token_on_mfa(self) -> None:
        """(b) on MFA success: _record_auth_success() before _store_token_persistently()."""
        call_order: list[str] = []

        client = _make_base_client(totp_provider=lambda: "123456")

        login_resp = _mock_response(200, {"mfa_required": True, "mfa_token": "mfa-tok"})
        verify_resp = _mock_response(
            200, {"access_token": "tok-mfa-order", "token_type": "bearer"}
        )
        _stub_session(client, [login_resp, verify_resp])

        orig_success = client._record_auth_success.__func__  # type: ignore[attr-defined]
        orig_store = client._store_token_persistently.__func__  # type: ignore[attr-defined]

        def tracking_success(self):
            call_order.append("record_auth_success")
            orig_success(self)

        def tracking_store(self, token):
            call_order.append("store_token_persistently")
            orig_store(self, token)

        client._record_auth_success = lambda: tracking_success(client)  # type: ignore[method-assign]
        client._store_token_persistently = lambda t: tracking_store(client, t)  # type: ignore[method-assign]

        client._authenticate()

        assert "record_auth_success" in call_order
        assert "store_token_persistently" in call_order
        success_idx = call_order.index("record_auth_success")
        store_idx = call_order.index("store_token_persistently")
        assert success_idx < store_idx, (
            "_record_auth_success must come BEFORE _store_token_persistently on MFA path"
        )

    def test_record_auth_success_called_exactly_once_on_success(self) -> None:
        """(b) _record_auth_success() is called exactly once on non-MFA success."""
        success_calls: list[int] = []

        client = _make_base_client()
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-once", "token_type": "bearer"}),
        )

        orig_success = client._record_auth_success.__func__  # type: ignore[attr-defined]

        def tracking_success(self):
            success_calls.append(1)
            orig_success(self)

        client._record_auth_success = lambda: tracking_success(client)  # type: ignore[method-assign]

        client._authenticate()

        assert len(success_calls) == 1, (
            "_record_auth_success must be called exactly once"
        )

    def test_record_auth_success_called_exactly_once_on_mfa(self) -> None:
        """(b) _record_auth_success() is called exactly once on MFA success."""
        success_calls: list[int] = []

        client = _make_base_client(totp_provider=lambda: "123456")
        login_resp = _mock_response(200, {"mfa_required": True, "mfa_token": "mfa-t"})
        verify_resp = _mock_response(
            200, {"access_token": "tok-mfa-once", "token_type": "bearer"}
        )
        _stub_session(client, [login_resp, verify_resp])

        orig_success = client._record_auth_success.__func__  # type: ignore[attr-defined]

        def tracking_success(self):
            success_calls.append(1)
            orig_success(self)

        client._record_auth_success = lambda: tracking_success(client)  # type: ignore[method-assign]

        client._authenticate()

        assert len(success_calls) == 1, (
            "_record_auth_success must be called exactly once on MFA success"
        )

    def test_login_does_not_call_record_auth_success(self) -> None:
        """(c) AuthAPIClient.login() must NOT call _record_auth_success()."""
        success_calls: list[int] = []
        failure_calls: list[int] = []

        client = _make_auth_client()
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-login", "token_type": "bearer"}),
        )

        orig_success = client._record_auth_success.__func__  # type: ignore[attr-defined]
        orig_failure = client._record_auth_failure.__func__  # type: ignore[attr-defined]

        def tracking_success(self):
            success_calls.append(1)
            orig_success(self)

        def tracking_failure(self):
            failure_calls.append(1)
            orig_failure(self)

        client._record_auth_success = lambda: tracking_success(client)  # type: ignore[method-assign]
        client._record_auth_failure = lambda: tracking_failure(client)  # type: ignore[method-assign]

        client.login("admin", "secret")

        assert len(success_calls) == 0, "login() must NOT call _record_auth_success()"
        assert len(failure_calls) == 0, "login() must NOT call _record_auth_failure()"

    def test_login_does_not_call_store_token_persistently(self) -> None:
        """(c) AuthAPIClient.login() must NOT call _store_token_persistently()."""
        store_calls: list[int] = []

        client = _make_auth_client()
        _stub_session_single(
            client,
            _mock_response(
                200, {"access_token": "tok-nostore", "token_type": "bearer"}
            ),
        )

        orig_store = client._store_token_persistently.__func__  # type: ignore[attr-defined]

        def tracking_store(self, token):
            store_calls.append(1)
            orig_store(self, token)

        client._store_token_persistently = lambda t: tracking_store(client, t)  # type: ignore[method-assign]

        client.login("admin", "secret")

        assert len(store_calls) == 0, (
            "login() must NOT call _store_token_persistently()"
        )

    def test_login_does_not_check_circuit_breaker(self) -> None:
        """(c) AuthAPIClient.login() must NOT call _check_circuit_breaker()."""
        breaker_calls: list[int] = []

        client = _make_auth_client()
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-nocb", "token_type": "bearer"}),
        )

        orig_check = client._check_circuit_breaker.__func__  # type: ignore[attr-defined]

        def tracking_check(self):
            breaker_calls.append(1)
            orig_check(self)

        client._check_circuit_breaker = lambda: tracking_check(client)  # type: ignore[method-assign]

        client.login("admin", "secret")

        assert len(breaker_calls) == 0, "login() must NOT call _check_circuit_breaker()"


# ===========================================================================
# AC6(d): Helper purity — will verify _perform_login_request after refactor
# These tests verify the CURRENT behavior (before refactor) is structurally
# consistent with what the helper should do post-refactor.
# After refactor: these same assertions apply to _perform_login_request.
# ===========================================================================


class TestAC6HelperPurity:
    """Pin helper purity contracts: no breaker, no persist, no credentials mutation.

    Pre-refactor: these tests verify the inline behavior inside _authenticate
    and login. Post-refactor: they verify _perform_login_request directly.
    These tests are written to be stable across both states.
    """

    def test_perform_login_request_exists_after_refactor(self) -> None:
        """After refactor, _perform_login_request must exist on CIDXRemoteAPIClient.

        This test will FAIL until the refactor adds the helper (RED phase for that
        specific method). It is included here as the AC6(d) anchor.
        """
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        client = CIDXRemoteAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "secret"},
        )
        assert hasattr(client, "_perform_login_request"), (
            "CIDXRemoteAPIClient must have _perform_login_request after refactor"
        )

    def test_login_outcome_class_exists_after_refactor(self) -> None:
        """After refactor, _LoginOutcome must be importable from base_client."""
        # This will FAIL until the refactor adds _LoginOutcome (RED for refactor)
        from code_indexer.api_clients.base_client import _LoginOutcome  # noqa: F401

        assert _LoginOutcome is not None

    def test_login_transport_error_class_exists_after_refactor(self) -> None:
        """After refactor, _LoginTransportError must be importable from base_client."""
        # This will FAIL until the refactor adds _LoginTransportError (RED for refactor)
        from code_indexer.api_clients.base_client import _LoginTransportError  # noqa: F401

        assert _LoginTransportError is not None


# ===========================================================================
# AC5 + Happy path: success stores credentials in login()
# ===========================================================================


class TestLoginSuccessContracts:
    """Pin the success-path contracts for both callers."""

    def test_login_success_sets_credentials_on_client(self) -> None:
        """login() success must set self.credentials with username/password."""
        client = _make_auth_client()
        _stub_session_single(
            client,
            _mock_response(
                200,
                {"access_token": "tok", "token_type": "bearer", "user_id": "u-1"},
            ),
        )

        result = client.login("admin", "secret")

        assert result["access_token"] == "tok"
        assert result["token_type"] == "bearer"
        assert result["user_id"] == "u-1"
        assert client.credentials["username"] == "admin"
        assert client.credentials["password"] == "secret"

    def test_login_success_returns_auth_response_with_token_type(self) -> None:
        """login() must default token_type to 'bearer' if absent from response."""
        client = _make_auth_client()
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok-bearer"}),  # no token_type key
        )

        result = client.login("admin", "secret")

        assert result["token_type"] == "bearer"

    def test_authenticate_success_returns_str_token(self) -> None:
        """_authenticate() success must return the raw str token."""
        client = _make_base_client()
        _stub_session_single(
            client,
            _mock_response(
                200, {"access_token": "jwt-raw-str", "token_type": "bearer"}
            ),
        )

        token = client._authenticate()

        assert token == "jwt-raw-str"
        assert isinstance(token, str)

    def test_login_success_stores_credentials_securely_when_project_root(
        self, tmp_path
    ) -> None:
        """login() with project_root must call _store_credentials_securely."""
        store_calls: list[tuple] = []

        client = _make_auth_client(project_root=tmp_path)
        _stub_session_single(
            client,
            _mock_response(200, {"access_token": "tok", "token_type": "bearer"}),
        )

        orig_store = client._store_credentials_securely.__func__  # type: ignore[attr-defined]

        def tracking_store(self, username, password):
            store_calls.append((username, password))
            orig_store(self, username, password)

        client._store_credentials_securely = (  # type: ignore[method-assign]
            lambda u, p: tracking_store(client, u, p)
        )

        client.login("admin", "secret")

        assert len(store_calls) == 1
        assert store_calls[0][0] == "admin"


# ===========================================================================
# M1 regression pins: no-detail 401 — each caller must use its OWN default
# These cases were hidden before the fix because _perform_login_request was
# baking in "Invalid credentials" as a default, making e.detail always TRUTHY
# and killing the `e.detail or 'caller-specific-default'` fallback in callers.
# ===========================================================================


class TestNoDetail401DefaultMessages:
    """Pin the exact no-detail-401 messages for both callers.

    When the server returns 401 with no 'detail' field (or with a non-JSON
    body), each caller must fall back to its OWN hardcoded default — NOT the
    helper's baked-in default.
    """

    # --- _authenticate(): must say "Invalid credentials" ---

    def test_authenticate_401_no_detail_json_exact_message(self) -> None:
        """_authenticate() 401 with valid JSON {} must say 'Authentication failed: Invalid credentials'."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        # Valid JSON body, but no 'detail' field
        _stub_session_single(client, _mock_response(401, {}))

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        assert str(exc.value) == "Authentication failed: Invalid credentials"

    def test_authenticate_401_bad_json_body_exact_message(self) -> None:
        """_authenticate() 401 with non-JSON body must say 'Authentication failed: Invalid credentials'."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_base_client()
        # Non-JSON / malformed body
        _stub_session_single(client, _mock_response(401, bad_json=True))

        with pytest.raises(AuthenticationError) as exc:
            client._authenticate()

        assert str(exc.value) == "Authentication failed: Invalid credentials"

    # --- login(): must say "Invalid username or password" ---

    def test_login_401_no_detail_json_exact_message(self) -> None:
        """login() 401 with valid JSON {} must say 'Authentication failed: Invalid username or password'."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        # Valid JSON body, but no 'detail' field
        _stub_session_single(client, _mock_response(401, {}))

        with pytest.raises(AuthenticationError) as exc:
            client.login("admin", "wrong")

        assert str(exc.value) == "Authentication failed: Invalid username or password"

    def test_login_401_bad_json_body_exact_message(self) -> None:
        """login() 401 with non-JSON body must say 'Authentication failed: Invalid username or password'."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_auth_client()
        # Non-JSON / malformed body
        _stub_session_single(client, _mock_response(401, bad_json=True))

        with pytest.raises(AuthenticationError) as exc:
            client.login("admin", "wrong")

        assert str(exc.value) == "Authentication failed: Invalid username or password"

    # --- Verify the two callers give DIFFERENT messages for no-detail 401 ---

    def test_no_detail_401_messages_differ_between_callers(self) -> None:
        """_authenticate and login must produce different no-detail-401 messages (intentional divergence)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        base_client = _make_base_client()
        _stub_session_single(base_client, _mock_response(401, {}))
        with pytest.raises(AuthenticationError) as exc_base:
            base_client._authenticate()

        auth_client = _make_auth_client()
        _stub_session_single(auth_client, _mock_response(401, {}))
        with pytest.raises(AuthenticationError) as exc_auth:
            auth_client.login("admin", "wrong")

        assert str(exc_base.value) == "Authentication failed: Invalid credentials"
        assert (
            str(exc_auth.value) == "Authentication failed: Invalid username or password"
        )
        assert str(exc_base.value) != str(exc_auth.value)
