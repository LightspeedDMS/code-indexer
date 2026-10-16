"""Tests for the shared subprocess-failure diagnostic formatter (Bug #1832).

Bug #1810 produced `_format_called_process_error_diagnostic()` in
refresh_scheduler.py -- the first fix of this pattern. Bug #1832 is the
sweep: the SAME shape (command, exit code, both captured streams, bounded
length) is promoted to `code_indexer.utils.subprocess_diagnostics` so every
call site across the codebase reuses ONE formatter (AC3) instead of
reimplementing "failed: {result.stderr}" per site, which degrades to an
uninformative empty message whenever the failing tool's real diagnostic
lands on stdout instead of stderr.

This module lives in `code_indexer.utils` (Story #1328 precedent, see
`utils/subprocess_env.py`) so both `global_repos/` and `server/` code can
import it without a layering violation -- it has zero dependencies beyond
the stdlib.
"""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

from code_indexer.utils.subprocess_diagnostics import (
    DEFAULT_DIAGNOSTIC_MAX_CHARS,
    format_called_process_error_diagnostic,
    format_completed_process_diagnostic,
    format_subprocess_failure_diagnostic,
)


class TestFormatSubprocessFailureDiagnostic:
    """Core formatter: cmd/returncode/stdout/stderr -> one diagnostic string."""

    def test_discriminating_case_empty_stderr_nonempty_stdout(self) -> None:
        """AC5: the discriminating case this bug is about -- stderr is
        EMPTY, the tool's real diagnostic is on stdout. A formatter that
        only reads stderr (the pre-fix behavior everywhere in the sweep)
        would produce a diagnostic with nothing useful in it. The fix must
        surface the stdout content."""
        diagnostic = format_subprocess_failure_diagnostic(
            cmd=["cidx", "index"],
            returncode=1,
            stdout="ERROR: embedding provider rejected request: quota exceeded",
            stderr="",
        )
        assert "quota exceeded" in diagnostic
        assert "exit_code=1" in diagnostic
        assert "cidx index" in diagnostic

    def test_str_cmd_not_corrupted_into_space_separated_characters(self) -> None:
        """e.cmd/result.args may be a bare str (shell=True style) -- must
        not be iterated character-by-character."""
        diagnostic = format_subprocess_failure_diagnostic(
            cmd="cidx index --fts",
            returncode=2,
            stdout="",
            stderr="boom",
        )
        assert "cidx index --fts" in diagnostic
        assert "c i d x" not in diagnostic

    def test_output_is_length_capped(self) -> None:
        """AC4: an unbounded stdout/stderr blob must never reach a log or
        API error field uncapped."""
        huge_stdout = "X" * 5000
        diagnostic = format_subprocess_failure_diagnostic(
            cmd=["cidx", "index"],
            returncode=1,
            stdout=huge_stdout,
            stderr="",
            max_chars=1000,
        )
        assert len(diagnostic) < 2500
        assert "X" * 1001 not in diagnostic

    def test_default_cap_matches_established_1000_char_precedent(self) -> None:
        """dependency_map_analyzer.py:2813 already caps at 1000 chars
        (`[:1000]`) -- match that established shape rather than inventing a
        new limit (issue body, "Length capping" section)."""
        assert DEFAULT_DIAGNOSTIC_MAX_CHARS == 1000

    def test_none_stdout_and_stderr_do_not_crash(self) -> None:
        diagnostic = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch"], returncode=None, stdout=None, stderr=None
        )
        assert "exit_code=None" in diagnostic


class TestFormatCalledProcessErrorDiagnostic:
    """Bug #1810 shape, now delegating to the shared core formatter."""

    def test_discriminating_case_empty_stderr_nonempty_stdout(self) -> None:
        e = subprocess.CalledProcessError(
            returncode=1,
            cmd=["cidx", "scip", "generate"],
            output="real diagnostic on stdout",
            stderr="",
        )
        diagnostic = format_called_process_error_diagnostic(e)
        assert "real diagnostic on stdout" in diagnostic
        assert "CalledProcessError" in diagnostic

    def test_sequence_cmd_joined_not_corrupted(self) -> None:
        e = subprocess.CalledProcessError(
            returncode=1, cmd=["git", "clone", "url"], output="", stderr="err"
        )
        diagnostic = format_called_process_error_diagnostic(e)
        assert "git clone url" in diagnostic


class TestFormatCompletedProcessDiagnostic:
    """The `result.returncode != 0` (non-raising) shape used throughout
    activated_repo_index_manager.py, installer.py, refresh_scheduler.py,
    repository_listing_manager.py, and dependency_map_analyzer.py."""

    def test_discriminating_case_empty_stderr_nonempty_stdout(self) -> None:
        result = Mock(
            args=["cidx", "index", "--index-commits"],
            returncode=1,
            stdout="Temporal indexer failed: repository has no commits reachable from HEAD",
            stderr="",
        )
        diagnostic = format_completed_process_diagnostic(result)
        assert "repository has no commits reachable from HEAD" in diagnostic
        assert "exit_code=1" in diagnostic

    def test_missing_args_attribute_degrades_gracefully(self) -> None:
        """A bare Mock() without .args configured must not raise."""
        result = Mock(spec=["returncode", "stdout", "stderr"])
        result.returncode = 1
        result.stdout = "diagnostic text"
        result.stderr = ""
        diagnostic = format_completed_process_diagnostic(result)
        assert "diagnostic text" in diagnostic
<<<<<<< HEAD


class TestCredentialRedactionInCommandText:
    """A failing git command is reported with its full argv -- and a repo URL can
    carry a PAT. The diagnostic travels into job errors, API responses and logs,
    so the secret must not survive the formatting.
    """

    _PAT = "glpat-NOTAREALTOKEN123"

    def test_pat_in_remote_url_is_redacted_from_called_process_error(self):
        url = f"https://oauth2:{self._PAT}@gitlab.com/group/repo.git"
        exc = subprocess.CalledProcessError(
            1, ["git", "remote", "add", "origin", url], output="", stderr="fatal: lock"
        )

        message = format_called_process_error_diagnostic(exc)

        assert self._PAT not in message
        assert "oauth2" not in message
        assert "https://***@gitlab.com/group/repo.git" in message
        # the useful part of the diagnostic survives
        assert "fatal: lock" in message

    def test_pat_is_redacted_from_completed_process_diagnostic_too(self):
        url = f"https://oauth2:{self._PAT}@gitlab.com/group/repo.git"
        result = subprocess.CompletedProcess(
            args=["git", "fetch", url], returncode=1, stdout="", stderr=""
        )

        message = format_completed_process_diagnostic(result)

        assert self._PAT not in message
        assert "https://***@gitlab.com/group/repo.git" in message

    def test_scp_style_remote_is_left_alone(self):
        """`git@github.com:org/repo.git` has no scheme and carries no secret;
        redacting it would destroy a useful diagnostic for no gain."""
        exc = subprocess.CalledProcessError(
            1,
            ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
            output="",
            stderr="",
        )

        assert "git@github.com:org/repo.git" in format_called_process_error_diagnostic(
            exc
        )

    def test_non_credentialed_url_is_unchanged(self):
        exc = subprocess.CalledProcessError(
            1,
            ["git", "clone", "https://gitlab.com/group/repo.git"],
            output="",
            stderr="",
        )

        assert (
            "https://gitlab.com/group/repo.git"
            in format_called_process_error_diagnostic(exc)
        )


class TestCredentialRedactionInCapturedStreams:
    """Redaction must happen at the FORMATTING BOUNDARY, not only on argv.

    `redact_url_credentials` was applied inside `_format_cmd` only, so
    `command=` was clean while `stdout=`/`stderr=` -- which go through
    `_cap()` -- carried the secret VERBATIM. git is the worst case: it
    echoes back the credentialed URL it was handed, so the exact argv this
    module redacts reappears unredacted in the stream it does not:

        stderr="fatal: unable to access
                'https://oauth2:<PAT>@gitlab.com/o/r.git/': 403"

    Every public entry point of this module builds strings that travel into
    job errors, API responses and logs, so every channel must be covered.
    """

    _PAT = "glpat-NOTAREALTOKEN123"

    @property
    def _url(self) -> str:
        return f"https://oauth2:{self._PAT}@gitlab.com/group/repo.git"

    def test_pat_in_stderr_is_redacted(self) -> None:
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch", "origin"],
            returncode=128,
            stdout="",
            stderr=f"fatal: unable to access '{self._url}/': The requested URL returned error: 403",
        )

        assert self._PAT not in message
        assert "oauth2" not in message
        assert "https://***@gitlab.com/group/repo.git" in message
        # the actionable half of the diagnostic must survive redaction
        assert "returned error: 403" in message

    def test_pat_in_stdout_is_redacted(self) -> None:
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "ls-remote"],
            returncode=1,
            stdout=f"probing {self._url} ...",
            stderr="",
        )

        assert self._PAT not in message
        assert "https://***@gitlab.com/group/repo.git" in message

    def test_pat_in_both_streams_is_redacted(self) -> None:
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch"],
            returncode=128,
            stdout=f"stdout mentions {self._url}",
            stderr=f"stderr mentions {self._url}",
        )

        assert self._PAT not in message
        assert message.count("https://***@gitlab.com/group/repo.git") == 2

    def test_pat_in_argv_and_stderr_simultaneously_is_redacted_in_both(self) -> None:
        """The real shape of the leak: the SAME credentialed URL is both the
        argument git was given and the text git echoes back on failure. The
        argv half was already clean; the stream half was not."""
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch", self._url],
            returncode=128,
            stdout="",
            stderr=f"fatal: unable to access '{self._url}/': 403",
        )

        assert self._PAT not in message
        assert "oauth2" not in message
        # both channels present, both redacted
        assert "command='git fetch https://***@gitlab.com/group/repo.git'" in message
        assert message.count("https://***@gitlab.com/group/repo.git") == 2

    def test_github_style_bare_token_userinfo_in_stream_is_redacted(self) -> None:
        """GitHub remotes are built as `https://<token>@host/...` (userinfo
        with no colon) -- see remote_branch_service._build_effective_url."""
        token = "ghp_NOTAREALTOKEN0000000000000000000000"
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "push"],
            returncode=128,
            stdout="",
            stderr=f"remote: Invalid username or password for https://{token}@github.com/org/repo.git",
        )

        assert token not in message
        assert "https://***@github.com/org/repo.git" in message

    def test_called_process_error_streams_are_redacted(self) -> None:
        exc = subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            output=f"stdout {self._url}",
            stderr=f"stderr {self._url}",
        )

        message = format_called_process_error_diagnostic(exc)

        assert self._PAT not in message
        assert message.count("https://***@gitlab.com/group/repo.git") == 2

    def test_completed_process_streams_are_redacted(self) -> None:
        result = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=1,
            stdout=f"stdout {self._url}",
            stderr=f"stderr {self._url}",
        )

        message = format_completed_process_diagnostic(result)

        assert self._PAT not in message
        assert message.count("https://***@gitlab.com/group/repo.git") == 2

    def test_redaction_runs_before_truncation_so_no_partial_token_survives(
        self,
    ) -> None:
        """Order matters. Cap-then-redact lets the cap sever the URL before
        the `@` the pattern anchors on, so the regex no longer matches and a
        PREFIX of the live token survives in the output. Redact-then-cap
        cannot leak a partial token, and is never longer (`***` is shorter
        than any userinfo it replaces)."""
        # tuned so a cap at 1000 would land 10 characters into the token
        noise = "X" * (1000 - len("https://oauth2:") - 10)
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr=noise + self._url,
            max_chars=1000,
        )

        assert self._PAT not in message
        assert self._PAT[:10] not in message
        assert "glpat" not in message

    def test_scp_style_remote_in_stream_survives_intact(self) -> None:
        """`git@github.com:org/repo.git` is a schemeless scp-form remote: it
        carries no secret, and mangling it would destroy the most useful line
        in the diagnostic. It must come through byte-identical."""
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr=(
                "fatal: Could not read from remote repository "
                "git@github.com:org/repo.git\n"
                "Please make sure you have the correct access rights."
            ),
        )

        assert "git@github.com:org/repo.git" in message
        assert "***" not in message

    def test_benign_stream_output_is_not_mangled(self) -> None:
        benign = (
            "remote: Enumerating objects: 1234, done.\n"
            "error: failed to push some refs to 'https://gitlab.com/group/repo.git'\n"
            "hint: Updates were rejected because the tip of your current branch is behind\n"
            "Contact: ops@example.com"
        )
        message = format_subprocess_failure_diagnostic(
            cmd=["git", "push"], returncode=1, stdout="", stderr=benign
        )

        assert "https://gitlab.com/group/repo.git" in message
        assert "ops@example.com" in message
        assert "Enumerating objects: 1234, done." in message
        assert "***" not in message


class TestRedactionAcceptsWhateverAnFStringWouldAccept:
    """`redact_url_credentials` sits where an f-string interpolation used to be,
    so it must accept everything an f-string accepts.

    Captured streams are not reliably `str`: `subprocess` hands back `bytes`
    when `text=False`, a stream is `None` when it was not captured, and test
    doubles pass Mocks. The regex raises `TypeError: expected string or
    bytes-like object` on all three, which turns a message-formatting path into
    a crash -- strictly worse than the leak it was added to stop, because it
    fires on the error path where the real diagnostic is being built.
    """

    def test_non_string_inputs_do_not_raise(self) -> None:
        from unittest.mock import Mock

        from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

        for value in (None, 42, b"bytes output", Mock(), ["a"], object()):
            redact_url_credentials(value)  # type: ignore[arg-type]

    def test_non_string_renders_as_an_f_string_would(self) -> None:
        """Rendering must not change what operators already read in logs."""
        from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

        for value in (None, 42, "plain"):
            assert redact_url_credentials(value) == f"{value}"  # type: ignore[arg-type]

    def test_a_credential_in_bytes_is_still_redacted(self) -> None:
        from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

        pat = "glpat-AAAAAAAAAAAAAAAAAAAA"
        out = redact_url_credentials(
            f"https://oauth2:{pat}@gitlab.com/o/r.git".encode()
        )
        assert pat not in out
=======
>>>>>>> upstream/master
