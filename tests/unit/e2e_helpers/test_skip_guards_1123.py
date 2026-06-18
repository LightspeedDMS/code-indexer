"""Unit tests for the loud skip-guard helpers added by Story #1123.

These tests cover:
  - require_voyage_key() -- skips when VOYAGE_API_KEY / E2E_VOYAGE_API_KEY absent
  - require_cohere_key() -- skips when CO_API_KEY / E2E_COHERE_API_KEY absent
  - require_xray_cli() -- skips when rustc or xray-cli binary absent
  - require_postgres() -- skips when initdb/pg_ctl absent
  - CI hard-fail policy -- guards raise RuntimeError when CIDX_E2E_REQUIRE_ALL=true

Each guard must produce a LOUD, specific skip reason so the AC1 SKIP SUMMARY can
enumerate it meaningfully.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers under test — imported late so tests can verify ImportError absence
# ---------------------------------------------------------------------------


def _import_guards():
    """Lazy import to avoid collecting the module before it exists (RED phase)."""
    from tests.e2e import helpers  # type: ignore[import]

    return helpers


# ---------------------------------------------------------------------------
# require_voyage_key
# ---------------------------------------------------------------------------


class TestRequireVoyageKey:
    def test_skips_when_both_env_vars_absent(self, monkeypatch):
        """Guard skips with a loud, specific reason when no VoyageAI key is set."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("E2E_VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        with pytest.raises(pytest.skip.Exception) as exc_info:
            helpers.require_voyage_key()

        reason = str(exc_info.value)
        assert "VOYAGE_API_KEY" in reason, f"Reason must name the missing key: {reason}"
        assert len(reason) > 20, f"Reason must be specific/loud, got: {reason!r}"

    def test_does_not_skip_when_voyage_api_key_set(self, monkeypatch):
        """Guard passes through when VOYAGE_API_KEY is present."""
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key-value")
        monkeypatch.delenv("E2E_VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        # Should not raise
        helpers.require_voyage_key()

    def test_does_not_skip_when_e2e_voyage_api_key_set(self, monkeypatch):
        """Guard passes through when E2E_VOYAGE_API_KEY is present."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("E2E_VOYAGE_API_KEY", "test-key-value")
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        helpers.require_voyage_key()

    def test_hard_fails_in_ci_when_key_absent(self, monkeypatch):
        """When CIDX_E2E_REQUIRE_ALL=true and key is absent, guard raises RuntimeError."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("E2E_VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "true")

        helpers = _import_guards()
        with pytest.raises(RuntimeError) as exc_info:
            helpers.require_voyage_key()

        msg = str(exc_info.value)
        assert "VOYAGE_API_KEY" in msg
        assert "CIDX_E2E_REQUIRE_ALL" in msg


# ---------------------------------------------------------------------------
# require_cohere_key
# ---------------------------------------------------------------------------


class TestRequireCohereKey:
    def test_skips_when_both_env_vars_absent(self, monkeypatch):
        """Guard skips with a loud reason when no Cohere key is set."""
        monkeypatch.delenv("CO_API_KEY", raising=False)
        monkeypatch.delenv("E2E_COHERE_API_KEY", raising=False)
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        with pytest.raises(pytest.skip.Exception) as exc_info:
            helpers.require_cohere_key()

        reason = str(exc_info.value)
        assert "CO_API_KEY" in reason or "COHERE" in reason.upper(), (
            f"Reason must name the missing Cohere key: {reason}"
        )
        assert len(reason) > 20

    def test_does_not_skip_when_co_api_key_set(self, monkeypatch):
        """Guard passes through when CO_API_KEY is present."""
        monkeypatch.setenv("CO_API_KEY", "test-cohere-key")
        monkeypatch.delenv("E2E_COHERE_API_KEY", raising=False)
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        helpers.require_cohere_key()

    def test_does_not_skip_when_e2e_cohere_api_key_set(self, monkeypatch):
        """Guard passes through when E2E_COHERE_API_KEY is present."""
        monkeypatch.delenv("CO_API_KEY", raising=False)
        monkeypatch.setenv("E2E_COHERE_API_KEY", "test-cohere-key")
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        helpers = _import_guards()
        helpers.require_cohere_key()

    def test_hard_fails_in_ci_when_key_absent(self, monkeypatch):
        """When CIDX_E2E_REQUIRE_ALL=true and key is absent, raises RuntimeError."""
        monkeypatch.delenv("CO_API_KEY", raising=False)
        monkeypatch.delenv("E2E_COHERE_API_KEY", raising=False)
        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "true")

        helpers = _import_guards()
        with pytest.raises(RuntimeError) as exc_info:
            helpers.require_cohere_key()

        msg = str(exc_info.value)
        assert "CIDX_E2E_REQUIRE_ALL" in msg


# ---------------------------------------------------------------------------
# require_xray_cli
# ---------------------------------------------------------------------------


class TestRequireXrayCli:
    def test_skips_when_rustc_absent(self, monkeypatch):
        """Guard skips with loud reason when rustc is not on PATH."""
        helpers = _import_guards()

        def _raise(*args, **kwargs):
            raise FileNotFoundError("rustc not found")

        with patch.object(subprocess, "run", side_effect=_raise):
            monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)
            with pytest.raises(pytest.skip.Exception) as exc_info:
                helpers.require_xray_cli()

        reason = str(exc_info.value)
        assert "rustc" in reason.lower() or "xray" in reason.lower(), (
            f"Reason must mention rustc or xray: {reason}"
        )
        assert len(reason) > 20

    def test_skips_when_xray_cli_binary_absent(self, monkeypatch, tmp_path):
        """Guard skips when rustc is present but xray-cli binary is missing."""
        helpers = _import_guards()
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        # Patch subprocess.run to succeed for rustc check
        import subprocess as sp

        original_run = sp.run

        def _mock_run(args, **kwargs):
            if args and "rustc" in str(args[0]):

                class FakeResult:
                    returncode = 0

                return FakeResult()
            return original_run(args, **kwargs)

        with patch.object(sp, "run", side_effect=_mock_run):
            # Patch the binary path lookup so xray-cli doesn't exist
            with patch.object(
                helpers,
                "_xray_cli_path",
                return_value=tmp_path / "rust" / "target" / "release" / "xray-cli",
            ):
                with pytest.raises(pytest.skip.Exception) as exc_info:
                    helpers.require_xray_cli()

        reason = str(exc_info.value)
        assert "xray" in reason.lower() or "xray-cli" in reason.lower()

    def test_hard_fails_in_ci_when_absent(self, monkeypatch):
        """When CIDX_E2E_REQUIRE_ALL=true and xray-cli absent, raises RuntimeError."""
        helpers = _import_guards()
        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "true")

        def _raise(*args, **kwargs):
            raise FileNotFoundError("rustc not found")

        with patch.object(subprocess, "run", side_effect=_raise):
            with pytest.raises(RuntimeError) as exc_info:
                helpers.require_xray_cli()

        msg = str(exc_info.value)
        assert "CIDX_E2E_REQUIRE_ALL" in msg


# ---------------------------------------------------------------------------
# require_postgres
# ---------------------------------------------------------------------------


class TestRequirePostgres:
    def test_skips_when_initdb_absent(self, monkeypatch):
        """Guard skips with loud reason when initdb is not on PATH."""
        helpers = _import_guards()
        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)

        def _raise(*args, **kwargs):
            raise FileNotFoundError("initdb not found")

        with patch.object(subprocess, "run", side_effect=_raise):
            with pytest.raises(pytest.skip.Exception) as exc_info:
                helpers.require_postgres()

        reason = str(exc_info.value)
        assert (
            "postgres" in reason.lower()
            or "initdb" in reason.lower()
            or "pg_ctl" in reason.lower()
        ), f"Reason must mention postgres/initdb/pg_ctl: {reason}"
        assert len(reason) > 20

    def test_hard_fails_in_ci_when_absent(self, monkeypatch):
        """When CIDX_E2E_REQUIRE_ALL=true and postgres absent, raises RuntimeError."""
        helpers = _import_guards()
        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "true")

        def _raise(*args, **kwargs):
            raise FileNotFoundError("initdb not found")

        with patch.object(subprocess, "run", side_effect=_raise):
            with pytest.raises(RuntimeError) as exc_info:
                helpers.require_postgres()

        msg = str(exc_info.value)
        assert "CIDX_E2E_REQUIRE_ALL" in msg


# ---------------------------------------------------------------------------
# CI marker documentation
# ---------------------------------------------------------------------------


class TestCiMarkerDocumentation:
    def test_ci_marker_constant_exported(self):
        """The chosen CI marker constant should be exported from helpers."""
        helpers = _import_guards()
        assert hasattr(helpers, "CI_REQUIRE_ALL_ENV_VAR"), (
            "helpers.CI_REQUIRE_ALL_ENV_VAR must be exported so callers know "
            "which env var triggers hard-fail mode"
        )
        assert helpers.CI_REQUIRE_ALL_ENV_VAR == "CIDX_E2E_REQUIRE_ALL"

    def test_is_ci_require_all_reflects_env(self, monkeypatch):
        """is_ci_require_all() returns True only when CIDX_E2E_REQUIRE_ALL=true."""
        helpers = _import_guards()

        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "true")
        assert helpers.is_ci_require_all() is True

        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "1")
        assert helpers.is_ci_require_all() is True

        monkeypatch.setenv("CIDX_E2E_REQUIRE_ALL", "false")
        assert helpers.is_ci_require_all() is False

        monkeypatch.delenv("CIDX_E2E_REQUIRE_ALL", raising=False)
        assert helpers.is_ci_require_all() is False
