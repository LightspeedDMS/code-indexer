"""
Unit tests for _ensure_codex_mcp_http_registered in codex_mcp_registration.py.

v9.23.10: Registration now writes config.toml directly (TOML file IO) instead of
spawning subprocess `codex mcp add`. Codex 0.125 `codex mcp add` does not support
`--http-headers` / `--env-http-headers` flags, so direct TOML editing is required.

Test inventory (6 tests across 5 classes):

  TestTomlRegistrationAbsentFile (1 test)
    test_absent_config_toml_creates_file_with_section

  TestTomlRegistrationIdempotency (2 tests)
    test_already_registered_skips_write
    test_stale_config_is_replaced

  TestTomlRegistrationSectionStructure (1 test)
    test_written_section_has_correct_structure

  TestTomlRegistrationAtomicWrite (1 test)
    test_write_is_atomic_tmp_then_replace

  TestTomlRegistrationFailures (1 test)
    test_io_error_logs_warning_and_does_not_raise
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.startup.codex_mcp_registration import (
    _ensure_codex_mcp_http_registered,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_PORT = 8000
_TEST_HOST = "localhost"
_MCP_NAME = "cidx-local"
_AUTH_HEADER_ENV_VAR = "CIDX_MCP_AUTH_HEADER"


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
# Tests: absent config.toml
# ---------------------------------------------------------------------------


class TestTomlRegistrationAbsentFile:
    """When config.toml does not exist, function creates it with the section."""

    def test_absent_config_toml_creates_file_with_section(self, codex_home):
        """
        When config.toml does not exist, _ensure_codex_mcp_http_registered must
        create the file containing the [mcp_servers.cidx-local] section with
        the expected url and env_http_headers.Authorization entry.
        """
        import tomli

        config_toml = codex_home / "config.toml"
        assert not config_toml.exists(), "Precondition: config.toml must not exist"

        _ensure_codex_mcp_http_registered(
            codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
        )

        assert config_toml.exists(), "config.toml must be created"
        with open(config_toml, "rb") as f:
            data = tomli.load(f)
        section = data.get("mcp_servers", {}).get(_MCP_NAME, {})
        assert section, f"[mcp_servers.{_MCP_NAME}] section must exist in config.toml"
        assert section.get("url") == f"http://{_TEST_HOST}:{_TEST_PORT}/mcp", (
            f"url must be 'http://{_TEST_HOST}:{_TEST_PORT}/mcp'; got {section.get('url')!r}"
        )
        assert (
            section.get("env_http_headers", {}).get("Authorization")
            == _AUTH_HEADER_ENV_VAR
        ), f"env_http_headers.Authorization must be {_AUTH_HEADER_ENV_VAR!r}"


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


class TestTomlRegistrationIdempotency:
    """Second call with matching config is a no-op; stale config is replaced."""

    def test_already_registered_skips_write(self, codex_home, caplog):
        """
        When config.toml already contains the correct url AND
        env_http_headers.Authorization = 'CIDX_MCP_AUTH_HEADER',
        the second call must be a no-op (no write, INFO logged).
        """
        # First call creates the file
        _ensure_codex_mcp_http_registered(
            codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
        )
        config_toml = codex_home / "config.toml"
        mtime_after_first = config_toml.stat().st_mtime_ns

        # Second call — must be idempotent
        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.startup.codex_mcp_registration",
        ):
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
            )

        mtime_after_second = config_toml.stat().st_mtime_ns
        assert mtime_after_second == mtime_after_first, (
            "config.toml must NOT be modified on a second call with matching config"
        )
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("already registered" in m for m in info_msgs), (
            "INFO log must confirm 'already registered' on idempotent call"
        )

    def test_stale_config_is_replaced(self, codex_home):
        """
        When config.toml contains a [mcp_servers.cidx-local] section with a
        stale bearer_token_env_var (from v9.23.9), the section must be replaced
        with the new env_http_headers entry. Verified by parsing the resulting TOML.
        """
        import tomli

        config_toml = codex_home / "config.toml"
        # Write stale v9.23.9-style config
        stale_content = (
            "[mcp_servers.cidx-local]\n"
            f'url = "http://{_TEST_HOST}:{_TEST_PORT}/mcp"\n'
            'bearer_token_env_var = "CIDX_MCP_BEARER_TOKEN"\n'
        )
        config_toml.write_text(stale_content)

        _ensure_codex_mcp_http_registered(
            codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
        )

        with open(config_toml, "rb") as f:
            data = tomli.load(f)
        section = data.get("mcp_servers", {}).get(_MCP_NAME, {})
        assert "bearer_token_env_var" not in section, (
            "Stale bearer_token_env_var must be absent from replaced section"
        )
        assert (
            section.get("env_http_headers", {}).get("Authorization")
            == _AUTH_HEADER_ENV_VAR
        ), (
            f"Replaced section must have env_http_headers.Authorization = {_AUTH_HEADER_ENV_VAR!r}"
        )


# ---------------------------------------------------------------------------
# Tests: section structure
# ---------------------------------------------------------------------------


class TestTomlRegistrationSectionStructure:
    """Written section has correct TOML structure — verified by parsing."""

    def test_written_section_has_correct_structure(self, codex_home):
        """
        Parse config.toml with tomli after registration and assert:
          - data["mcp_servers"]["cidx-local"]["url"] == "http://localhost:8000/mcp"
          - data["mcp_servers"]["cidx-local"]["env_http_headers"]["Authorization"]
              == "CIDX_MCP_AUTH_HEADER"
          - "bearer_token_env_var" key is absent from the section
        """
        import tomli

        _ensure_codex_mcp_http_registered(
            codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
        )

        config_toml = codex_home / "config.toml"
        with open(config_toml, "rb") as f:
            data = tomli.load(f)

        section = data.get("mcp_servers", {}).get(_MCP_NAME, {})
        assert section, f"[mcp_servers.{_MCP_NAME}] section must exist"

        expected_url = f"http://{_TEST_HOST}:{_TEST_PORT}/mcp"
        assert section.get("url") == expected_url, (
            f"url must be {expected_url!r}; got {section.get('url')!r}"
        )

        env_headers = section.get("env_http_headers", {})
        assert env_headers.get("Authorization") == _AUTH_HEADER_ENV_VAR, (
            f"env_http_headers.Authorization must be {_AUTH_HEADER_ENV_VAR!r}; "
            f"got {env_headers.get('Authorization')!r}"
        )

        assert "bearer_token_env_var" not in section, (
            "bearer_token_env_var must NOT appear in the new registration section"
        )


# ---------------------------------------------------------------------------
# Tests: atomic write
# ---------------------------------------------------------------------------


class TestTomlRegistrationAtomicWrite:
    """Write is performed atomically via .tmp file then replace."""

    def test_write_is_atomic_tmp_then_replace(self, codex_home):
        """
        The implementation must write to config.toml.tmp then call .replace()
        to config.toml (cross-platform atomic overwrite semantics).
        Verified by wrapping Path.replace to spy on calls:
          - replace must be called at least once
          - the replace target must be config.toml (not the .tmp file)
        """
        config_toml = codex_home / "config.toml"
        replace_targets: list = []

        original_replace = Path.replace

        def _spy_replace(self, target):
            replace_targets.append(Path(target))
            return original_replace(self, target)

        with patch.object(Path, "replace", _spy_replace):
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
            )

        assert replace_targets, "Path.replace must be called during atomic write"
        assert any(t == config_toml for t in replace_targets), (
            f"replace must target {config_toml}; got {replace_targets!r}"
        )
        tmp_file = codex_home / "config.toml.tmp"
        assert not tmp_file.exists(), (
            "config.toml.tmp must NOT remain after successful atomic write"
        )


# ---------------------------------------------------------------------------
# Tests: failure handling
# ---------------------------------------------------------------------------


class TestTomlRegistrationFailures:
    """IO errors log WARNING and do not raise."""

    def test_io_error_logs_warning_and_does_not_raise(self, codex_home, caplog):
        """
        When writing config.toml fails with an IOError, a WARNING is logged
        and the function does not propagate the exception (non-fatal).
        """
        with (
            patch("pathlib.Path.replace", side_effect=IOError("disk full")),
            caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_mcp_registration",
            ),
        ):
            # Must NOT raise
            _ensure_codex_mcp_http_registered(
                codex_home=codex_home, port=_TEST_PORT, host=_TEST_HOST
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Expected WARNING when config.toml write fails"


# ---------------------------------------------------------------------------
# Tests: mode preservation and parent directory creation (Finding 2, v9.23.10)
# ---------------------------------------------------------------------------


class TestTomlRegistrationModePreservation:
    """v9.23.10 Finding 2: _write_toml_atomic must preserve file mode and create parents."""

    def test_creates_missing_parent_dirs_and_sets_mode_0600(self, tmp_path):
        """Fresh CODEX_HOME with no parent dir: creates parents and writes config.toml at 0600.

        v9.23.10: codex's config.toml is 0600 by convention. The old implementation let
        the temp file inherit the process umask (typically 0644), making the renamed file
        world-readable. Also, CODEX_HOME itself may not exist on fresh installs.
        """
        nested = tmp_path / "fresh" / "codex-home"
        config_toml = nested / "config.toml"

        _ensure_codex_mcp_http_registered(
            codex_home=nested, port=8000, host="localhost"
        )

        assert config_toml.exists(), "config.toml must be created"
        assert config_toml.parent.exists(), "parent codex-home must be created"
        mode = stat.S_IMODE(config_toml.stat().st_mode)
        assert mode == 0o600, f"config.toml must be 0600, got {oct(mode)}"

    def test_preserves_existing_mode(self, tmp_path):
        """Existing config.toml mode must be preserved through atomic write.

        v9.23.10: Operators may set a non-default mode (e.g. 0640 for group-readable).
        The atomic rewrite must not silently downgrade to umask-derived permissions.
        """
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        config_toml = codex_home / "config.toml"
        config_toml.write_text("# existing\n", encoding="utf-8")
        os.chmod(config_toml, 0o640)

        _ensure_codex_mcp_http_registered(
            codex_home=codex_home, port=8000, host="localhost"
        )

        mode = stat.S_IMODE(config_toml.stat().st_mode)
        assert mode == 0o640, f"existing mode must be preserved, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Regression guard: service_init.py singleton wiring (Finding 1, v9.23.10)
# ---------------------------------------------------------------------------


class TestServiceInitSingletonWiring:
    """v9.23.10 regression guard: textual presence check that service_init.py
    contains the MCPSelfRegistrationService.set_instance call.

    Uses inspect.getsource() — the codebase-established convention for
    structural wiring regression guards (see test_golden_repo_manager_scheduler_wiring.py,
    test_cluster_pool_wiring.py). This is a source-text substring check; it
    does not verify execution-path reachability.
    """

    def test_service_init_calls_set_instance_for_mcp_registration(self):
        """v9.23.10 regression guard: textual check that service_init.py contains
        'MCPSelfRegistrationService.set_instance'. Without this call, the singleton
        is never populated and codex_mcp_auth_header_provider raises RuntimeError on
        first invocation in production, causing silent fallback to Claude.

        Source-text substring check — matches the established codebase pattern used in
        test_golden_repo_manager_scheduler_wiring and test_cluster_pool_wiring.
        """
        import inspect

        from code_indexer.server.startup import service_init

        src = inspect.getsource(service_init)
        assert "MCPSelfRegistrationService.set_instance" in src, (
            "v9.23.10 invariant violated: service_init.py must contain "
            "MCPSelfRegistrationService.set_instance(...) so the singleton is "
            "populated for codex auth-header provider. Without this call, "
            "build_codex_mcp_auth_header_provider() raises RuntimeError on first "
            "invocation in production, causing silent fallback to Claude."
        )
