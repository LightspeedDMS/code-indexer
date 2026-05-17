"""Story #1007 - Unit tests for HealthWatchdog.

TDD red phase: imports fail until health_watchdog.py is created.

Algorithm paths covered:
- Module-level constant values
- Constructor attribute wiring
- Health endpoint URL and timeout wiring
- Healthy response resets consecutive_failures (with recovery log)
- All failure types (non-200, connection error, request timeout) increment counter
- Threshold reached triggers systemctl restart
- All restart outcomes (success, nonzero exit, subprocess timeout) in one matrix
- Cooldown active: restart suppressed, suppression logged; expired: restart allowed
- Missing state file: defaults used, check proceeds
- Corrupted state file (invalid JSON, missing key, wrong type): defaults used
- main() entry point exists and triggers health check via external collaborators only
"""

import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Union
from unittest.mock import MagicMock, patch

import pytest
import requests

from code_indexer.server.auto_update.health_watchdog import (
    HealthWatchdog,
    DEFAULT_FAILURES_THRESHOLD,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_CHECK_TIMEOUT_SECONDS,
    HEALTH_WATCHDOG_SERVICE_NAME,
    STATE_FILE_NAME,
)

# ---------------------------------------------------------------------------
# Test constants: every value used in assertions, data fixtures, or derivations
# ---------------------------------------------------------------------------

# Expected module constant values
_EXPECTED_FAILURES_THRESHOLD = 3
_EXPECTED_COOLDOWN_SECONDS = 300
_EXPECTED_CHECK_TIMEOUT_SECONDS = 10
_EXPECTED_STATE_FILE_NAME = "health_watchdog_state.json"

# HTTP status codes
_HTTP_OK = 200
_HTTP_UNAVAILABLE = 503

# Failure count sentinels
_ZERO_FAILURES = 0
_ONE_FAILURE = 1

# Arithmetic primitives used in derived constants
_THRESHOLD_DECREMENT = 1
_COOLDOWN_DIVISOR = 5
_COOLDOWN_EXCESS_SECONDS = 100
_HIGH_THRESHOLD_MULTIPLIER = 5

# Derived from imported defaults using named primitives above
_FAILURES_BEFORE_THRESHOLD = DEFAULT_FAILURES_THRESHOLD - _THRESHOLD_DECREMENT
_WITHIN_COOLDOWN_SECONDS = DEFAULT_COOLDOWN_SECONDS // _COOLDOWN_DIVISOR
_PAST_COOLDOWN_SECONDS = DEFAULT_COOLDOWN_SECONDS + _COOLDOWN_EXCESS_SECONDS
_HIGH_THRESHOLD = DEFAULT_FAILURES_THRESHOLD * _HIGH_THRESHOLD_MULTIPLIER

# Server URLs
_TEST_SERVER_URL = "http://testhost:1234"
_ALTERNATE_SERVER_URL = "http://custom:9000"
_EXPECTED_HEALTH_URL = _TEST_SERVER_URL + "/health"

# State dict keys
_STATE_KEY_FAILURES = "consecutive_failures"
_STATE_KEY_LAST_TS = "last_restart_ts"

# Patch targets
_PATCH_REQUESTS_GET = "requests.get"
_PATCH_SUBPROCESS_RUN = "subprocess.run"
_PATCH_RESOLVE_CONFIG = (
    "code_indexer.server.auto_update.health_watchdog._resolve_config"
)

# Constructor test values — non-default so wiring is observable
_CUSTOM_THRESHOLD = 5
_CUSTOM_COOLDOWN = 600
_CUSTOM_CHECK_TIMEOUT = 15

# Check-timeout used in wiring test — distinct from default
_WIRING_CHECK_TIMEOUT = 7

# Subprocess timeout used in restart parametrize
_SYSTEMCTL_TIMEOUT = 30

# Corrupted-state fixture values — obviously non-default
_CORRUPT_EXTRA_VALUE = 99
_CORRUPT_TS_INT = 12345


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_watchdog(
    tmp_path: Path,
    *,
    server_url: str = _TEST_SERVER_URL,
    failures_threshold: int = DEFAULT_FAILURES_THRESHOLD,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    check_timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS,
) -> HealthWatchdog:
    return HealthWatchdog(
        server_url=server_url,
        state_file=tmp_path / STATE_FILE_NAME,
        failures_threshold=failures_threshold,
        cooldown_seconds=cooldown_seconds,
        check_timeout_seconds=check_timeout_seconds,
    )


def _write_state(
    tmp_path: Path,
    consecutive_failures: int,
    last_restart_ts: Optional[str],
) -> None:
    (tmp_path / STATE_FILE_NAME).write_text(
        json.dumps(
            {
                _STATE_KEY_FAILURES: consecutive_failures,
                _STATE_KEY_LAST_TS: last_restart_ts,
            }
        )
    )


def _read_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / STATE_FILE_NAME).read_text())


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = _HTTP_OK
    return resp


def _error_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = _HTTP_UNAVAILABLE
    return resp


def _ts_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _run_check(tmp_path: Path, wdog: HealthWatchdog, *, healthy: bool) -> MagicMock:
    """Run check_once() with ok/error response; return mock subprocess.run."""
    get_patch = patch(
        _PATCH_REQUESTS_GET,
        return_value=_ok_response() if healthy else _error_response(),
    )
    with get_patch:
        with patch(_PATCH_SUBPROCESS_RUN) as mock_run:
            mock_run.return_value = MagicMock(returncode=_ZERO_FAILURES)
            wdog.check_once()
    return mock_run


def _failures(tmp_path: Path) -> int:
    return _read_state(tmp_path)[_STATE_KEY_FAILURES]


def _last_ts(tmp_path: Path) -> Optional[str]:
    return _read_state(tmp_path)[_STATE_KEY_LAST_TS]


# ---------------------------------------------------------------------------
# TestDefaults
# ---------------------------------------------------------------------------


class TestDefaults:
    @pytest.mark.parametrize(
        "constant,expected",
        [
            (DEFAULT_FAILURES_THRESHOLD, _EXPECTED_FAILURES_THRESHOLD),
            (DEFAULT_COOLDOWN_SECONDS, _EXPECTED_COOLDOWN_SECONDS),
            (DEFAULT_CHECK_TIMEOUT_SECONDS, _EXPECTED_CHECK_TIMEOUT_SECONDS),
        ],
    )
    def test_numeric_defaults(self, constant: int, expected: int) -> None:
        assert constant == expected

    def test_state_file_name(self) -> None:
        assert STATE_FILE_NAME == _EXPECTED_STATE_FILE_NAME

    def test_service_name_is_non_empty_string(self) -> None:
        assert isinstance(HEALTH_WATCHDOG_SERVICE_NAME, str)
        assert HEALTH_WATCHDOG_SERVICE_NAME


# ---------------------------------------------------------------------------
# TestConstructor
# ---------------------------------------------------------------------------


class TestConstructor:
    @pytest.mark.parametrize(
        "attr,kwarg,value",
        [
            ("server_url", "server_url", _ALTERNATE_SERVER_URL),
            ("failures_threshold", "failures_threshold", _CUSTOM_THRESHOLD),
            ("cooldown_seconds", "cooldown_seconds", _CUSTOM_COOLDOWN),
            ("check_timeout_seconds", "check_timeout_seconds", _CUSTOM_CHECK_TIMEOUT),
        ],
    )
    def test_attribute_wiring(
        self, tmp_path: Path, attr: str, kwarg: str, value: Union[str, int]
    ) -> None:
        # Build a typed lookup dict and pass each field explicitly to avoid
        # spreading a dict[str, Union[str, int]] into keyword-only typed args.
        lookup: dict[str, Union[str, int]] = {
            "server_url": _TEST_SERVER_URL,
            "failures_threshold": DEFAULT_FAILURES_THRESHOLD,
            "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
            "check_timeout_seconds": DEFAULT_CHECK_TIMEOUT_SECONDS,
        }
        lookup[kwarg] = value
        wdog = HealthWatchdog(
            server_url=str(lookup["server_url"]),
            state_file=tmp_path / STATE_FILE_NAME,
            failures_threshold=int(lookup["failures_threshold"]),
            cooldown_seconds=int(lookup["cooldown_seconds"]),
            check_timeout_seconds=int(lookup["check_timeout_seconds"]),
        )
        assert getattr(wdog, attr) == value

    def test_sets_state_file_path(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path)
        assert wdog.state_file == tmp_path / STATE_FILE_NAME


# ---------------------------------------------------------------------------
# TestHealthCheckWiring
# ---------------------------------------------------------------------------


class TestHealthCheckWiring:
    def test_hits_health_endpoint(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path, server_url=_TEST_SERVER_URL)
        with patch(_PATCH_REQUESTS_GET) as mock_get:
            mock_get.return_value = _ok_response()
            wdog.check_once()
        assert mock_get.call_args[0][0] == _EXPECTED_HEALTH_URL

    def test_passes_configured_timeout(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path, check_timeout_seconds=_WIRING_CHECK_TIMEOUT)
        with patch(_PATCH_REQUESTS_GET) as mock_get:
            mock_get.return_value = _ok_response()
            wdog.check_once()
        assert mock_get.call_args[1].get("timeout") == _WIRING_CHECK_TIMEOUT


# ---------------------------------------------------------------------------
# TestHealthyResponse
# ---------------------------------------------------------------------------


class TestHealthyResponse:
    def test_resets_failures_to_zero(self, tmp_path: Path) -> None:
        _write_state(tmp_path, _FAILURES_BEFORE_THRESHOLD, None)
        wdog = _make_watchdog(tmp_path)
        _run_check(tmp_path, wdog, healthy=True)
        assert _failures(tmp_path) == _ZERO_FAILURES

    def test_saves_state_file(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path)
        _run_check(tmp_path, wdog, healthy=True)
        assert (tmp_path / STATE_FILE_NAME).exists()

    def test_does_not_trigger_restart(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path)
        mock_run = _run_check(tmp_path, wdog, healthy=True)
        mock_run.assert_not_called()

    def test_logs_recovery_when_prior_failures(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        _write_state(tmp_path, _FAILURES_BEFORE_THRESHOLD, None)
        wdog = _make_watchdog(tmp_path)
        with caplog.at_level(logging.INFO):
            _run_check(tmp_path, wdog, healthy=True)
        assert any("recover" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# TestFailureIncrement
# ---------------------------------------------------------------------------


class TestFailureIncrement:
    @pytest.mark.parametrize(
        "use_error_response,get_side_effect,description",
        [
            (True, None, "non-200 response"),
            (False, requests.ConnectionError("refused"), "connection error"),
            (False, requests.Timeout("timed out"), "request timeout"),
        ],
    )
    def test_increments_consecutive_failures(
        self,
        tmp_path: Path,
        use_error_response: bool,
        get_side_effect: Optional[Exception],
        description: str,
    ) -> None:
        _write_state(tmp_path, _ZERO_FAILURES, None)
        wdog = _make_watchdog(tmp_path, failures_threshold=_HIGH_THRESHOLD)
        if use_error_response:
            ctx = patch(_PATCH_REQUESTS_GET, return_value=_error_response())
        else:
            ctx = patch(_PATCH_REQUESTS_GET, side_effect=get_side_effect)
        with ctx:
            wdog.check_once()
        assert _failures(tmp_path) == _ONE_FAILURE, description

    def test_below_threshold_does_not_restart(self, tmp_path: Path) -> None:
        _write_state(tmp_path, _ONE_FAILURE, None)
        wdog = _make_watchdog(tmp_path)
        mock_run = _run_check(tmp_path, wdog, healthy=False)
        mock_run.assert_not_called()

    def test_saves_state_after_failure(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path, failures_threshold=_HIGH_THRESHOLD)
        _run_check(tmp_path, wdog, healthy=False)
        assert _failures(tmp_path) == _ONE_FAILURE


# ---------------------------------------------------------------------------
# TestRestartTrigger
# ---------------------------------------------------------------------------


class TestRestartTrigger:
    def test_calls_systemctl_restart_at_threshold(self, tmp_path: Path) -> None:
        _write_state(tmp_path, _FAILURES_BEFORE_THRESHOLD, None)
        wdog = _make_watchdog(tmp_path)
        mock_run = _run_check(tmp_path, wdog, healthy=False)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert "systemctl" in cmd
        assert "restart" in cmd
        assert HEALTH_WATCHDOG_SERVICE_NAME in cmd

    @pytest.mark.parametrize(
        "subprocess_side,is_timeout,expected_failures,ts_set,description",
        [
            (
                MagicMock(returncode=_ZERO_FAILURES),
                False,
                _ZERO_FAILURES,
                True,
                "success",
            ),
            (
                MagicMock(returncode=_ONE_FAILURE),
                False,
                DEFAULT_FAILURES_THRESHOLD,
                False,
                "nonzero exit",
            ),
            (
                subprocess.TimeoutExpired(cmd="systemctl", timeout=_SYSTEMCTL_TIMEOUT),
                True,
                DEFAULT_FAILURES_THRESHOLD,
                False,
                "subprocess timeout",
            ),
        ],
    )
    def test_restart_outcome_state(
        self,
        tmp_path: Path,
        # Union covers both parametrized arms: MagicMock result or real TimeoutExpired.
        subprocess_side: Union[MagicMock, subprocess.TimeoutExpired],
        is_timeout: bool,
        expected_failures: int,
        ts_set: bool,
        description: str,
    ) -> None:
        _write_state(tmp_path, _FAILURES_BEFORE_THRESHOLD, None)
        wdog = _make_watchdog(tmp_path)
        if is_timeout:
            proc_ctx = patch(_PATCH_SUBPROCESS_RUN, side_effect=subprocess_side)
        else:
            proc_ctx = patch(_PATCH_SUBPROCESS_RUN, return_value=subprocess_side)
        with patch(_PATCH_REQUESTS_GET, return_value=_error_response()):
            with proc_ctx:
                wdog.check_once()
        state = _read_state(tmp_path)
        assert state[_STATE_KEY_FAILURES] == expected_failures, description
        if ts_set:
            assert state[_STATE_KEY_LAST_TS] is not None, description
            datetime.fromisoformat(state[_STATE_KEY_LAST_TS])
        else:
            assert state[_STATE_KEY_LAST_TS] is None, description


# ---------------------------------------------------------------------------
# TestCooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    @pytest.mark.parametrize(
        "ago_seconds,expected_restart,description",
        [
            (_WITHIN_COOLDOWN_SECONDS, False, "within cooldown"),
            (_PAST_COOLDOWN_SECONDS, True, "past cooldown"),
        ],
    )
    def test_cooldown_boundary(
        self,
        tmp_path: Path,
        ago_seconds: int,
        expected_restart: bool,
        description: str,
    ) -> None:
        _write_state(tmp_path, _FAILURES_BEFORE_THRESHOLD, _ts_ago(ago_seconds))
        wdog = _make_watchdog(tmp_path)
        mock_run = _run_check(tmp_path, wdog, healthy=False)
        assert mock_run.called == expected_restart, description

    def test_active_cooldown_still_increments_failures(self, tmp_path: Path) -> None:
        _write_state(
            tmp_path, _FAILURES_BEFORE_THRESHOLD, _ts_ago(_WITHIN_COOLDOWN_SECONDS)
        )
        wdog = _make_watchdog(tmp_path)
        _run_check(tmp_path, wdog, healthy=False)
        assert _failures(tmp_path) == DEFAULT_FAILURES_THRESHOLD

    def test_active_cooldown_logs_suppression(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        _write_state(
            tmp_path, _FAILURES_BEFORE_THRESHOLD, _ts_ago(_WITHIN_COOLDOWN_SECONDS)
        )
        wdog = _make_watchdog(tmp_path)
        with caplog.at_level(logging.WARNING):
            _run_check(tmp_path, wdog, healthy=False)
        assert any("cooldown" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# TestStateFileMissing
# ---------------------------------------------------------------------------


class TestStateFileMissing:
    def test_missing_state_creates_defaults_and_proceeds(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path)
        mock_run = _run_check(tmp_path, wdog, healthy=False)
        mock_run.assert_not_called()
        assert _failures(tmp_path) == _ONE_FAILURE
        assert _last_ts(tmp_path) is None

    def test_missing_state_healthy_saves_zero_failures(self, tmp_path: Path) -> None:
        wdog = _make_watchdog(tmp_path)
        _run_check(tmp_path, wdog, healthy=True)
        assert _failures(tmp_path) == _ZERO_FAILURES


# ---------------------------------------------------------------------------
# TestCorruptedStateFile
# ---------------------------------------------------------------------------


class TestCorruptedStateFile:
    @pytest.mark.parametrize(
        "raw_content,description",
        [
            ("not valid json {{{", "invalid JSON"),
            (
                json.dumps({"unexpected_key": _CORRUPT_EXTRA_VALUE}),
                "missing expected keys",
            ),
            (
                json.dumps(
                    {_STATE_KEY_FAILURES: "bad", _STATE_KEY_LAST_TS: _CORRUPT_TS_INT}
                ),
                "wrong types",
            ),
        ],
    )
    def test_corrupted_state_creates_defaults(
        self, tmp_path: Path, raw_content: str, description: str
    ) -> None:
        (tmp_path / STATE_FILE_NAME).write_text(raw_content)
        wdog = _make_watchdog(tmp_path, failures_threshold=_HIGH_THRESHOLD)
        _run_check(tmp_path, wdog, healthy=False)  # must not raise
        assert _failures(tmp_path) == _ONE_FAILURE, description


# ---------------------------------------------------------------------------
# TestMainEntryPoint
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_main_function_exists(self) -> None:
        from code_indexer.server.auto_update import health_watchdog

        assert callable(health_watchdog.main)

    def test_main_triggers_health_check(self, tmp_path: Path) -> None:
        """main() must perform a health check: verify requests.get is called
        with the health endpoint from the resolved config.

        Only external collaborators are mocked (requests.get, subprocess.run,
        _resolve_config); the real HealthWatchdog.check_once() runs.
        """
        from code_indexer.server.auto_update.health_watchdog import main

        with patch(_PATCH_RESOLVE_CONFIG) as mock_cfg:
            mock_cfg.return_value = (
                _TEST_SERVER_URL,
                tmp_path / STATE_FILE_NAME,
                DEFAULT_FAILURES_THRESHOLD,
                DEFAULT_COOLDOWN_SECONDS,
                DEFAULT_CHECK_TIMEOUT_SECONDS,
                "cidx-server",
            )
            with patch(_PATCH_REQUESTS_GET) as mock_get:
                mock_get.return_value = _ok_response()
                main()

        assert mock_get.called
        assert mock_get.call_args[0][0] == _EXPECTED_HEALTH_URL
