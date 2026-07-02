"""
TDD tests for Bug 2: wait_for_repo_activation helper in tests/e2e/helpers.py.

Red phase: these tests FAIL before the helper is implemented.

Coverage:
  - Returns immediately when GET /api/repos/<alias> responds 200
  - Retries on 404 and returns when 200 eventually arrives
  - Raises TimeoutError when activation never completes within timeout
"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from tests.e2e.helpers import wait_for_repo_activation


# ---------------------------------------------------------------------------
# Named constants — no magic numbers in tests
# ---------------------------------------------------------------------------

DEFAULT_TEST_TIMEOUT: float = 5.0
"""Generous timeout for tests that expect success."""

SHORT_POLL_INTERVAL: float = 0.01
"""Fast poll interval to make tests execute quickly."""

TIGHT_TEST_DEADLINE: float = 0.05
"""Very short timeout for the TimeoutError test."""

REQUEST_TIMEOUT_SECONDS: float = 30.0
"""Per-request HTTP timeout passed to each poll call."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_response(status_code: int) -> Mock:
    """Return a mock that behaves like an httpx.Response with the given status."""
    resp = Mock(spec=httpx.Response)
    resp.status_code = status_code
    resp.raise_for_status = Mock()
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWaitForRepoActivation:
    """wait_for_repo_activation polls GET /api/repos/<alias> until 200."""

    def test_returns_immediately_on_200(self):
        """Returns as soon as GET /api/repos/<alias> responds 200.

        Verifies the helper called GET /api/repos/markupsafe exactly once
        with the correct Bearer token and per-request timeout.
        """
        client = Mock(spec=httpx.Client)
        client.request.return_value = _make_response(200)

        wait_for_repo_activation(
            client,
            alias="markupsafe",
            token="fake-token",
            timeout=DEFAULT_TEST_TIMEOUT,
            poll_interval=SHORT_POLL_INTERVAL,
        )

        client.request.assert_called_once_with(
            "GET",
            "/api/repos/markupsafe",
            headers={"Authorization": "Bearer fake-token"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def test_retries_on_404_then_succeeds(self):
        """Retries when 404 is returned, returns when 200 eventually arrives.

        Verifies the helper polled GET /api/repos/markupsafe on every attempt.
        """
        client = Mock(spec=httpx.Client)
        client.request.side_effect = [
            _make_response(404),
            _make_response(404),
            _make_response(200),
        ]

        wait_for_repo_activation(
            client,
            alias="markupsafe",
            token="fake-token",
            timeout=DEFAULT_TEST_TIMEOUT,
            poll_interval=SHORT_POLL_INTERVAL,
        )

        assert client.request.call_count == 3
        for actual_call in client.request.call_args_list:
            assert actual_call.args[0] == "GET"
            assert actual_call.args[1] == "/api/repos/markupsafe"

    def test_raises_timeout_when_never_200(self):
        """Raises TimeoutError when activation never completes within timeout.

        Verifies both the error message and that polling was attempted with
        the correct method and path before timing out.
        """
        client = Mock(spec=httpx.Client)
        client.request.return_value = _make_response(404)

        with pytest.raises(TimeoutError) as exc_info:
            wait_for_repo_activation(
                client,
                alias="markupsafe",
                token="fake-token",
                timeout=TIGHT_TEST_DEADLINE,
                poll_interval=SHORT_POLL_INTERVAL,
            )

        assert "markupsafe" in str(exc_info.value)
        assert client.request.call_count >= 1
        first_call = client.request.call_args_list[0]
        assert first_call.args[0] == "GET"
        assert first_call.args[1] == "/api/repos/markupsafe"
