"""
Unit tests for _ensure_codex_mcp_http_registered in codex_cli_startup.py.

Closes Story #848 follow-up: Codex MCP launcher gap — HTTP transport.

Test inventory (8 tests across 4 classes):

  TestHttpRegistrationCommandStructure (2 tests)
    test_command_structure_with_url_and_bearer_token_env_var
    test_codex_home_env_passed_to_subprocess

  TestHttpRegistrationIdempotency (2 tests)
    test_already_registered_skips_add_call
    test_not_registered_calls_add_after_get

  TestHttpRegistrationFailures (3 tests)
    test_nonzero_add_exit_logs_warning_and_does_not_raise
    test_timeout_expired_logs_warning_and_does_not_raise
    test_file_not_found_logs_warning_and_does_not_raise

  TestHttpRegistrationWiring (1 test)
    test_initialize_wires_http_registration_with_port
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.startup.codex_cli_startup import (
    initialize_codex_manager_on_startup,
)
from code_indexer.server.startup.codex_mcp_registration import (
    _ensure_codex_mcp_http_registered,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_PORT = 8000
_TEST_PORT_ALT = 9123
_MCP_NAME = "cidx-local"
_BEARER_ENV_VAR = "CIDX_MCP_BEARER_TOKEN"
_SUBPROCESS_TIMEOUT = 30
_TEST_API_KEY = "test-api-key-sentinel"  # non-secret sentinel value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def codex_home(tmp_path: Path) -> Path:
    """Return a created codex-home directory under tmp_path."""
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_result(returncode: int, stderr: bytes = b"") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stderr = stderr
    return mock


def _get_returncode_for_cmd(cmd: list, get_rc: int, add_rc: int) -> int:
    """Return the appropriate returncode based on the command being run."""
    if cmd[:3] == ["codex", "mcp", "get"]:
        return get_rc
    if cmd[:3] == ["codex", "mcp", "add"]:
        return add_rc
    return 0


# ---------------------------------------------------------------------------
# Tests: HTTP registration command structure
# ---------------------------------------------------------------------------


class TestHttpRegistrationCommandStructure:
    """_ensure_codex_mcp_http_registered builds the correct codex command."""

    def test_command_structure_with_url_and_bearer_token_env_var(self, codex_home):
        """
        With port=8000 and get returning rc=1 (absent), the commands must be in order:
          1. ["codex", "mcp", "get", "cidx-local"]          -- pre-check
          2. ["codex", "mcp", "remove", "cidx-local"]        -- defensive remove (non-fatal when absent)
          3. ["codex", "mcp", "add", "cidx-local",
              "--url", "http://localhost:8000/mcp",
              "--bearer-token-env-var", "CIDX_MCP_BEARER_TOKEN"]  -- register

        Asserts call ORDER: get -> remove -> add (exactly 3 calls).
        """

        def _fake_run(cmd, **kwargs):
            rc = _get_returncode_for_cmd(cmd, get_rc=1, add_rc=0)
            return _make_run_result(rc)

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert len(all_cmds) == 3, (
            f"Expected exactly 3 subprocess calls (get + remove + add); got {len(all_cmds)}: {all_cmds!r}"
        )
        # First call: get pre-check
        assert all_cmds[0] == [
            "codex",
            "mcp",
            "get",
            _MCP_NAME,
        ], f"First subprocess call must be get pre-check; got {all_cmds[0]!r}"
        # Second call: defensive remove (non-fatal when absent)
        assert all_cmds[1] == [
            "codex",
            "mcp",
            "remove",
            _MCP_NAME,
        ], f"Second subprocess call must be remove; got {all_cmds[1]!r}"
        # Third call: add with HTTP URL and bearer env var
        expected_add = [
            "codex",
            "mcp",
            "add",
            _MCP_NAME,
            "--url",
            f"http://localhost:{_TEST_PORT}/mcp",
            "--bearer-token-env-var",
            _BEARER_ENV_VAR,
        ]
        assert all_cmds[2] == expected_add, (
            f"Third subprocess call must be add command {expected_add!r}; got {all_cmds[2]!r}"
        )

    def test_codex_home_env_passed_to_subprocess(self, codex_home):
        """
        CODEX_HOME must be set in the env dict passed to subprocess.run
        for every subprocess call (both get-check and add).
        """

        def _fake_run(cmd, **kwargs):
            rc = _get_returncode_for_cmd(cmd, get_rc=1, add_rc=0)
            return _make_run_result(rc)

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        assert mock_run.call_args_list, "At least one subprocess call expected"
        for c in mock_run.call_args_list:
            env = c.kwargs.get("env", {})
            assert env.get("CODEX_HOME") == str(codex_home), (
                f"CODEX_HOME must be {str(codex_home)!r} in call {c.args[0]!r}; "
                f"got {env.get('CODEX_HOME')!r}"
            )


# ---------------------------------------------------------------------------
# Tests: idempotency via pre-check
# ---------------------------------------------------------------------------


class TestHttpRegistrationIdempotency:
    """Pre-check via `codex mcp get cidx-local` determines whether add runs."""

    def test_already_registered_skips_add_call(self, codex_home):
        """
        When `codex mcp get cidx-local` returns rc=0 AND stdout contains the expected
        URL (http://...:{port}/mcp) and bearer env var (CIDX_MCP_BEARER_TOKEN), the
        registration is current. Neither `codex mcp remove` nor `codex mcp add` must
        be called. Only one subprocess call occurs (the get pre-check).
        """
        expected_stdout = (
            f"name: cidx-local\n"
            f"url: http://localhost:{_TEST_PORT}/mcp\n"
            f"bearer-token-env-var: {_BEARER_ENV_VAR}\n"
        ).encode()

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "get"]:
                m = _make_run_result(0)
                m.stdout = expected_stdout
                return m
            return _make_run_result(0)

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert len(all_cmds) == 1, (
            f"Expected exactly 1 subprocess call (get only) when already registered; "
            f"got {len(all_cmds)}: {all_cmds!r}"
        )
        assert all_cmds[0] == [
            "codex",
            "mcp",
            "get",
            _MCP_NAME,
        ], f"First call must be get pre-check; got {all_cmds[0]!r}"
        add_calls = [c for c in all_cmds if c[:3] == ["codex", "mcp", "add"]]
        assert not add_calls, (
            f"codex mcp add must NOT be called when already registered; got {add_calls!r}"
        )
        remove_calls = [c for c in all_cmds if c[:3] == ["codex", "mcp", "remove"]]
        assert not remove_calls, (
            f"codex mcp remove must NOT be called when already registered; got {remove_calls!r}"
        )

    def test_not_registered_calls_add_after_get(self, codex_home):
        """
        When `codex mcp get cidx-local` returns rc=1 (not registered),
        the implementation must call remove (defensive, non-fatal no-op) then add,
        in order: get -> remove -> add (exactly 3 calls).
        """

        def _fake_run(cmd, **kwargs):
            rc = _get_returncode_for_cmd(cmd, get_rc=1, add_rc=0)
            return _make_run_result(rc)

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert len(all_cmds) == 3, (
            f"Expected exactly 3 calls (get then remove then add); got {len(all_cmds)}: {all_cmds!r}"
        )
        assert all_cmds[0][:3] == ["codex", "mcp", "get"], (
            f"First call must be get pre-check; got {all_cmds[0]!r}"
        )
        assert all_cmds[1][:3] == ["codex", "mcp", "remove"], (
            f"Second call must be remove; got {all_cmds[1]!r}"
        )
        add_cmds = [c for c in all_cmds if c[:3] == ["codex", "mcp", "add"]]
        assert len(add_cmds) == 1, (
            f"codex mcp add must be called exactly once; got {len(add_cmds)} calls"
        )
        assert all_cmds[2][:3] == ["codex", "mcp", "add"], (
            f"Third call must be add; got {all_cmds[2]!r}"
        )


# ---------------------------------------------------------------------------
# Tests: failure cases
# ---------------------------------------------------------------------------


class TestHttpRegistrationFailures:
    """Non-zero returncode, timeout, and FileNotFoundError all log WARNING and do not raise."""

    def test_nonzero_add_exit_logs_warning_and_does_not_raise(self, codex_home, caplog):
        """
        When codex mcp add returns nonzero, a WARNING is logged with truncated
        stderr text, and the function does not raise.
        """
        stderr_text = "cidx-local already exists with different config"

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "get"]:
                return _make_run_result(1)  # not yet registered
            return _make_run_result(1, stderr_text.encode("utf-8"))

        with (
            patch("subprocess.run", side_effect=_fake_run),
            caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ),
        ):
            # Must NOT raise
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Expected WARNING on nonzero codex mcp add exit"
        combined = " ".join(r.message for r in warnings)
        assert stderr_text in combined, (
            f"WARNING must include stderr text {stderr_text!r}; got {combined!r}"
        )

    def test_timeout_expired_logs_warning_and_does_not_raise(self, codex_home, caplog):
        """
        When subprocess.run raises TimeoutExpired during mcp add, a WARNING is
        logged and the function does not propagate the exception.
        """

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "get"]:
                return _make_run_result(1)  # not registered
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=_SUBPROCESS_TIMEOUT)

        with (
            patch("subprocess.run", side_effect=_fake_run),
            caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ),
        ):
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Expected WARNING on TimeoutExpired during codex mcp add"

    def test_file_not_found_logs_warning_and_does_not_raise(self, codex_home, caplog):
        """
        When subprocess.run raises FileNotFoundError (codex not installed),
        a WARNING is logged and the function does not propagate the exception.
        This applies even during the get pre-check (codex binary absent).
        """
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("codex not found")),
            caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ),
        ):
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Expected WARNING when codex binary is not found"


# ---------------------------------------------------------------------------
# Tests: wiring into initialize_codex_manager_on_startup
# ---------------------------------------------------------------------------


class TestConfigAwareIdempotency:
    """Config-aware idempotency: stale registration triggers remove + re-add."""

    def test_stale_registration_triggers_remove_and_readd(self, codex_home):
        """
        When `codex mcp get cidx-local` returns rc=0 but stdout does NOT contain
        http://...:{port}/mcp and CIDX_MCP_BEARER_TOKEN, the registration is stale.
        _ensure_codex_mcp_http_registered must:
          1. Run `codex mcp remove cidx-local`
          2. Then run `codex mcp add` with the correct URL and bearer env var.
        """
        stale_stdout = b"type: stdio\ncommand: cidx mcp serve\n"

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "get"]:
                result = _make_run_result(0)
                result.stdout = stale_stdout
                return result
            if cmd[:3] == ["codex", "mcp", "remove"]:
                return _make_run_result(0)
            return _make_run_result(0)  # add

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host="localhost"
            )

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        remove_cmds = [c for c in all_cmds if c[:3] == ["codex", "mcp", "remove"]]
        add_cmds = [c for c in all_cmds if c[:3] == ["codex", "mcp", "add"]]
        assert remove_cmds, (
            "codex mcp remove must be called when registered transport is stale"
        )
        assert add_cmds, (
            "codex mcp add must be called after removing stale registration"
        )
        expected_url = f"http://localhost:{_TEST_PORT}/mcp"
        assert expected_url in add_cmds[0], (
            f"add command must include URL {expected_url!r}; got {add_cmds[0]!r}"
        )

    def test_matching_registration_skips_add(self, codex_home):
        """
        When stdout from `codex mcp get cidx-local` contains the expected URL
        (http://...:{port}/mcp) and CIDX_MCP_BEARER_TOKEN, registration is current.
        Neither remove nor add should be called.
        """
        port = _TEST_PORT
        matching_stdout = (
            f"url: http://localhost:{port}/mcp\nbearer_token_env: {_BEARER_ENV_VAR}\n"
        ).encode()

        def _fake_run(cmd, **kwargs):
            result = _make_run_result(0)
            result.stdout = matching_stdout
            return result

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=port, host="localhost"
            )

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        remove_cmds = [c for c in all_cmds if c[:3] == ["codex", "mcp", "remove"]]
        add_cmds = [c for c in all_cmds if c[:3] == ["codex", "mcp", "add"]]
        assert not remove_cmds, (
            f"codex mcp remove must NOT be called on matching registration; got {remove_cmds!r}"
        )
        assert not add_cmds, (
            f"codex mcp add must NOT be called on matching registration; got {add_cmds!r}"
        )


class TestHttpRegistrationWiring:
    """HTTP registration is wired into initialize_codex_manager_on_startup with the server port."""

    def test_initialize_wires_http_registration_with_port(self, tmp_path):
        """
        When Codex is enabled with api_key mode and server_config.port is set,
        initialize_codex_manager_on_startup must invoke the HTTP MCP registration
        using the correct port in the URL.

        This is a unit-level wiring test: subprocess.run is patched to intercept
        the commands without spawning real processes.
        """
        from code_indexer.server.utils.config_manager import CodexIntegrationConfig

        codex_cfg = CodexIntegrationConfig(
            enabled=True,
            credential_mode="api_key",
            api_key=_TEST_API_KEY,
        )
        server_config = MagicMock()
        server_config.codex_integration_config = codex_cfg
        server_config.port = _TEST_PORT_ALT
        server_config.host = "localhost"

        seen_cmds: list = []

        def _fake_run(cmd, **kwargs):
            seen_cmds.append(list(cmd))
            rc = _get_returncode_for_cmd(cmd, get_rc=1, add_rc=0)
            return _make_run_result(rc)

        with patch("subprocess.run", side_effect=_fake_run):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )

        get_cmds = [c for c in seen_cmds if c[:3] == ["codex", "mcp", "get"]]
        add_cmds = [c for c in seen_cmds if c[:3] == ["codex", "mcp", "add"]]
        assert get_cmds, "codex mcp get (pre-check) must have been called"
        assert add_cmds, "codex mcp add must have been called"

        expected_url = f"http://localhost:{_TEST_PORT_ALT}/mcp"
        assert expected_url in add_cmds[0], (
            f"add command must include URL {expected_url!r}; got {add_cmds[0]!r}"
        )
        assert _BEARER_ENV_VAR in add_cmds[0], (
            f"add command must include bearer env var {_BEARER_ENV_VAR!r}; got {add_cmds[0]!r}"
        )
