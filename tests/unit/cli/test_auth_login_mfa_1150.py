"""CLI-level tests for Bug #1150 - cidx auth login with TOTP-enrolled admin.

Verifies that the `cidx auth login` command wires a TOTP provider so that
TOTP-enrolled admin logins complete end-to-end through the REAL command path.

Strategy:
- patch detect_current_mode → "remote" so require_mode passes
- patch find_project_root + load_remote_configuration so no real FS/config needed
- patch httpx.Client to inject a stubbed session (no real HTTP)
- do NOT mock _authenticate() or login() — the real library logic must run
- TTY test: patch click.testing._NamedTextIOWrapper.isatty → True
  (CliRunner replaces sys.stdin with _NamedTextIOWrapper; isatty() must return
   True for the CLI's isatty guard to wire the totp_provider)
- Non-TTY test: default CliRunner (_NamedTextIOWrapper.isatty returns False) →
  totp_provider=None → actionable error from the library

The TTY tests FAIL before the CLI is wired (auth_login passes no totp_provider),
and pass after the CLI fix (isatty-guarded totp_provider is threaded through).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
import click.testing

from code_indexer.cli import cli


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _make_stubbed_session(responses: list) -> MagicMock:
    """Return a mock httpx.Client whose post() yields the given responses."""
    session = MagicMock()
    session.post.side_effect = responses
    session.is_closed = False
    return session


_SERVER_URL = "http://localhost:8000"


def _remote_config() -> dict:
    return {"server_url": _SERVER_URL}


def _base_patches(tmp_path, stubbed_session):
    """Patch list common to every test (mode, project root, config, HTTP)."""
    return [
        patch(
            "code_indexer.disabled_commands.detect_current_mode",
            return_value="remote",
        ),
        patch(
            "code_indexer.mode_detection.command_mode_detector.find_project_root",
            return_value=tmp_path,
        ),
        patch(
            "code_indexer.remote.config.load_remote_configuration",
            return_value=_remote_config(),
        ),
        # Inject stubbed HTTP transport: every httpx.Client() call returns our stub
        patch("httpx.Client", return_value=stubbed_session),
    ]


# ---------------------------------------------------------------------------
# 1. TTY path: TOTP-enrolled login completes when provider yields a valid code
# ---------------------------------------------------------------------------


class TestAuthLoginMFATTYPath:
    """cidx auth login completes TOTP challenge when running in an interactive TTY."""

    def test_totp_enrolled_login_completes_on_tty(self, tmp_path) -> None:
        """
        When sys.stdin.isatty() is True, the CLI must wire a totp_provider that
        reads from interactive input.  The command must complete successfully
        (exit code 0) when /auth/login returns MFA challenge and /auth/mfa/verify
        returns the access_token.

        CliRunner replaces sys.stdin with click.testing._NamedTextIOWrapper;
        we patch _NamedTextIOWrapper.isatty to return True so the CLI's
        isatty guard activates the provider.

        This test FAILS before the CLI is wired (auth_login constructed
        AuthAPIClient without totp_provider → login() raises actionable error
        instead of completing MFA challenge).
        """
        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "cli-mfa-tok"}
        )
        verify_resp = _mock_response(
            200, {"access_token": "cli-jwt-token", "token_type": "bearer"}
        )
        stubbed_session = _make_stubbed_session([login_resp, verify_resp])

        runner = CliRunner()

        patches = _base_patches(tmp_path, stubbed_session) + [
            # Make CliRunner's stdin wrapper report isatty=True
            patch.object(
                click.testing._NamedTextIOWrapper, "isatty", return_value=True
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            # CliRunner input: password (getpass reads from input stream)
            # then the TOTP code that click.prompt inside totp_provider reads
            result = runner.invoke(
                cli,
                ["auth", "login", "--username", "admin"],
                input="adminpass\n123456\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, (
            f"Expected exit 0 for successful TOTP login, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        output_lower = result.output.lower()
        assert "successfully" in output_lower or "logged in" in output_lower, (
            f"Expected success message, got:\n{result.output}"
        )

    def test_totp_enrolled_login_verify_called_with_code_from_prompt(
        self, tmp_path
    ) -> None:
        """
        The TOTP code typed by the user must reach POST /auth/mfa/verify as
        totp_code (not empty string, not None).
        """
        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-verify-check"}
        )
        verify_resp = _mock_response(
            200, {"access_token": "jwt-verify-ok", "token_type": "bearer"}
        )
        stubbed_session = _make_stubbed_session([login_resp, verify_resp])

        runner = CliRunner()

        patches = _base_patches(tmp_path, stubbed_session) + [
            patch.object(
                click.testing._NamedTextIOWrapper, "isatty", return_value=True
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            runner.invoke(
                cli,
                ["auth", "login", "--username", "admin"],
                input="adminpass\n777888\n",
                catch_exceptions=False,
            )

        assert stubbed_session.post.call_count == 2, (
            f"Expected two HTTP calls: /auth/login then /auth/mfa/verify, "
            f"got {stubbed_session.post.call_count}"
        )
        verify_call = stubbed_session.post.call_args_list[1]
        # call_args is (args, kwargs); json body is in kwargs["json"]
        called_json = verify_call[1].get("json") or {}
        assert called_json.get("totp_code") == "777888", (
            f"TOTP code from prompt must reach /auth/mfa/verify, got: {called_json}"
        )


# ---------------------------------------------------------------------------
# 2. Non-TTY path: actionable error instead of hang
# ---------------------------------------------------------------------------


class TestAuthLoginMFANonTTYPath:
    """cidx auth login emits an actionable error (not hang) when not a TTY."""

    def test_non_tty_mfa_required_gives_actionable_error(self, tmp_path) -> None:
        """
        When stdin is NOT a TTY and the server requires MFA, the CLI must emit
        an error message (exit code != 0) that mentions TOTP/MFA.
        It must NOT hang waiting on stdin.

        CliRunner's _NamedTextIOWrapper.isatty() returns False by default —
        no extra patch needed.
        """
        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-non-tty"}
        )
        # Only one HTTP call expected: login triggers MFA, then error fires before verify
        stubbed_session = _make_stubbed_session([login_resp])

        runner = CliRunner()

        patches = _base_patches(tmp_path, stubbed_session)

        with patches[0], patches[1], patches[2], patches[3]:
            result = runner.invoke(
                cli,
                ["auth", "login", "--username", "admin", "--password", "pw"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0, (
            "Expected non-zero exit when MFA required but no TTY for TOTP prompt"
        )
        output_lower = result.output.lower()
        assert any(
            word in output_lower
            for word in ("totp", "mfa", "interactive", "terminal", "provider")
        ), f"Error message must be actionable (mention TOTP/MFA). Got:\n{result.output}"

    def test_non_tty_mfa_does_not_call_verify_endpoint(self, tmp_path) -> None:
        """
        With no TTY, /auth/mfa/verify must NOT be called
        (no half-formed request with an empty TOTP code).
        """
        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-no-verify"}
        )
        stubbed_session = _make_stubbed_session([login_resp])

        runner = CliRunner()

        patches = _base_patches(tmp_path, stubbed_session)

        with patches[0], patches[1], patches[2], patches[3]:
            runner.invoke(
                cli,
                ["auth", "login", "--username", "admin", "--password", "pw"],
                catch_exceptions=False,
            )

        # Only the /auth/login call, not /auth/mfa/verify
        assert stubbed_session.post.call_count == 1, (
            f"Expected 1 HTTP call (login only), got {stubbed_session.post.call_count}"
        )
