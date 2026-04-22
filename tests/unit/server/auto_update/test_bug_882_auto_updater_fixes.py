"""Unit tests for Bug #882 auto-updater fixes (v9.21.1).

Bug #882 had two independent defects:

  Defect 1 — run_once.py ignored the operator-configured host/port and
    relied on DeploymentExecutor's hardcoded "http://localhost:8000" default.
    Any deployment on a non-default port (e.g., 8080) could not issue
    maintenance-mode or drain-status requests against its own server.

  Defect 2 — DeploymentExecutor._wait_for_drain() had no early-exit when
    the server was genuinely unreachable. The drain loop would spin for
    up to drain_timeout seconds (7200s fallback when the timeout endpoint
    also fails), blowing through the 120s systemd TimeoutStartSec budget
    on cidx-auto-update.service and killing the entire upgrade cycle.

The fixes:

  Fix 1 — run_once.py now loads ServerConfigManager().load_config() and
    passes `server_url` explicitly into DeploymentExecutor. When config.json
    is missing, run_once raises RuntimeError so systemd records an
    actionable failure instead of silently pointing at the wrong URL.

  Fix 2 — _wait_for_drain() tracks STRICTLY CONSECUTIVE ConnectionErrors.
    After three in a row (~30s at the default 10s poll interval) it
    returns True ("assume drained — nothing to drain if server is down").
    Any non-ConnectionError iteration outcome (HTTP response received,
    auth failure, generic exception) resets the counter so the early-exit
    is never triggered by cumulative mixed failures.
"""

import contextlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Shared helpers — keep setup duplication out of individual tests.
# ---------------------------------------------------------------------------


def _patch_config_manager(cfg):
    """Patch run_once.ServerConfigManager() so load_config() returns `cfg`."""
    from code_indexer.server.auto_update import run_once

    manager = MagicMock()
    manager.load_config.return_value = cfg
    return patch.object(run_once, "ServerConfigManager", return_value=manager)


def _make_config(host, port):
    """Build a ServerConfig-shaped stub with only the attrs _resolve_server_url reads."""
    cfg = MagicMock()
    cfg.host = host
    cfg.port = port
    return cfg


def _drain_status_response(drained):
    """Build a 200 drain-status response. drained=True/False controls the payload."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = (
        {"drained": True}
        if drained
        else {"drained": False, "running_jobs": 1, "queued_jobs": 0}
    )
    return response


@pytest.fixture
def drain_executor():
    """DeploymentExecutor configured for fast drain-loop tests (no real sleeps)."""
    from code_indexer.server.auto_update.deployment_executor import (
        DeploymentExecutor,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        yield DeploymentExecutor(
            repo_path=Path(tmpdir),
            server_url="http://127.0.0.1:8000",
            drain_poll_interval=0,
        )


@contextlib.contextmanager
def _patched_drain(executor, auth="fake-token", drain_timeout=60):
    """Patch the three collaborators the drain loop calls; yield the requests.get mock."""
    auth_kwargs = (
        {"side_effect": auth} if isinstance(auth, list) else {"return_value": auth}
    )
    with (
        patch.object(executor, "_get_drain_timeout", return_value=drain_timeout),
        patch.object(executor, "_get_auth_token", **auth_kwargs),
        patch("requests.get") as mock_get,
    ):
        yield mock_get


# ---------------------------------------------------------------------------
# Fix 1: run_once._resolve_server_url
# ---------------------------------------------------------------------------


class TestResolveServerUrl:
    """Bug #882 defect #1 — resolve server URL from config.json."""

    @pytest.mark.parametrize(
        "host,port,expected",
        [
            ("0.0.0.0", 8080, "http://0.0.0.0:8080"),
            ("127.0.0.1", 8000, "http://127.0.0.1:8000"),
            ("10.0.0.42", 9000, "http://10.0.0.42:9000"),
        ],
    )
    def test_returns_url_built_from_config(self, host, port, expected):
        """Config host/port flow through verbatim — no rewriting or normalization."""
        from code_indexer.server.auto_update import run_once

        with _patch_config_manager(_make_config(host, port)):
            assert run_once._resolve_server_url() == expected

    def test_raises_runtime_error_with_actionable_message_when_config_missing(self):
        """Messi #2 Anti-Fallback: fail loud, with operator remediation guidance."""
        from code_indexer.server.auto_update import run_once

        with _patch_config_manager(None):
            with pytest.raises(RuntimeError) as exc_info:
                run_once._resolve_server_url()

        message = str(exc_info.value)
        # Actionable remediation text must be present — proves the error tells
        # the operator what to do, not just that something went wrong.
        assert "Run the CIDX installer" in message
        assert "config.json" in message
        # Anti-regression guard: no hardcoded fallback URL may leak into the
        # error message. If someone later re-introduces a default literal,
        # this assertion fails.
        assert "http://127.0.0.1:8000" not in message
        assert "http://localhost:8000" not in message


# ---------------------------------------------------------------------------
# Fix 2: DeploymentExecutor._wait_for_drain early-exit behavior
# ---------------------------------------------------------------------------


# Parametrized table: each scenario exercises a different non-ConnectionError
# iteration outcome that MUST reset the consecutive counter. If any of these
# paths stops resetting, cumulative-but-not-consecutive failures will trigger
# the early-exit prematurely.
_RESET_COUNTER_SCENARIOS = [
    pytest.param(
        "fake-token",
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            _drain_status_response(drained=False),  # 200 response resets counter
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        6,
        id="200_response_resets",
    ),
    pytest.param(
        ["fake-token", "fake-token", None, "fake-token", "fake-token", "fake-token"],
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            # poll 3: no requests.get call — auth=None hits `continue`
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        5,
        id="auth_none_resets",
    ),
    pytest.param(
        "fake-token",
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            ValueError("synthetic non-connection failure"),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        6,
        id="generic_exception_resets",
    ),
]


class TestWaitForDrainEarlyExit:
    """Bug #882 defect #2 — early-exit on persistent ConnectionError."""

    def test_early_exits_after_three_consecutive_connection_errors(
        self, drain_executor
    ):
        """Three strictly-consecutive ConnectionErrors return True after exactly 3 polls."""
        with _patched_drain(drain_executor) as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError()
            result = drain_executor._wait_for_drain()

        assert result is True
        # Exactly 3 polls — proves the early-exit fired on the 3rd consecutive
        # ConnectionError and the loop did not iterate further.
        assert mock_get.call_count == 3

    @pytest.mark.parametrize(
        "auth,get_side_effect,expected_get_calls", _RESET_COUNTER_SCENARIOS
    )
    def test_non_connection_failure_resets_consecutive_counter(
        self, drain_executor, auth, get_side_effect, expected_get_calls
    ):
        """200-response / auth-None / generic-exception each reset the counter."""
        with _patched_drain(drain_executor, auth=auth) as mock_get:
            mock_get.side_effect = get_side_effect
            result = drain_executor._wait_for_drain()

        assert result is True
        assert mock_get.call_count == expected_get_calls

    def test_intermittent_failures_let_normal_drained_path_win(self, drain_executor):
        """ConnErr/200 alternation never fires early-exit — drained=True resolves."""
        with _patched_drain(drain_executor) as mock_get:
            mock_get.side_effect = [
                requests.exceptions.ConnectionError(),
                requests.exceptions.ConnectionError(),
                _drain_status_response(drained=False),  # resets counter → 0
                requests.exceptions.ConnectionError(),
                requests.exceptions.ConnectionError(),
                _drain_status_response(drained=True),  # normal exit: True
            ]
            result = drain_executor._wait_for_drain()

        assert result is True
        assert mock_get.call_count == 6
