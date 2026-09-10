"""
Unit tests for GitPullUpdater fixes for Bug #1830 and the git_pull_updater.py
slice of Bug #1832.

Bug #1830: `has_changes()` collapsed ANY `subprocess.TimeoutExpired` from any
of its three git subprocess calls into one generic, unclassified
`RuntimeError`. A fetch that TIMES OUT must be classified via the SAME
`classify_fetch_error()` a fetch that fails with a non-zero exit code already
uses (Story #295), so it participates in the repeated-transient-failure ->
re-clone escalation. Timeouts on the other two commands (`symbolic-ref`,
`git log`) must remain plain RuntimeErrors, never misclassified as fetch
errors.

Bug #1832 (this file's slice): several `raise RuntimeError(...)` /
`logger.warning(...)` diagnostics interpolated ONLY `result.stderr`,
discarding the exit code and `result.stdout`. When the failing git
subprocess writes its real diagnostic to stdout (empty stderr), the message
degrades to a body with no useful content. The discriminating test case per
the issue's AC5 is EMPTY stderr + non-empty stdout.
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.git_error_classifier import GitFetchError
from code_indexer.global_repos.git_pull_updater import (
    GitPullUpdater,
    _format_subprocess_failure_diagnostic,
    _format_timeout_diagnostic,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path(tmp_path):
    """Create a temporary repository directory."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    return repo


@pytest.fixture
def updater(repo_path):
    """Create a GitPullUpdater for the test repo."""
    return GitPullUpdater(str(repo_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(returncode=0, stdout="", stderr="", args=None):
    result = Mock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    result.args = args if args is not None else ["git"]
    return result


# ---------------------------------------------------------------------------
# Helper-level tests: _format_subprocess_failure_diagnostic /
# _format_timeout_diagnostic (Bug #1832 / Bug #1830 formatting primitives)
# ---------------------------------------------------------------------------


class TestFormatSubprocessFailureDiagnostic:
    """
    Bug #1832 AC1/AC2/AC4/AC5: diagnostic must include command, exit code,
    and BOTH streams, bounded in length -- the discriminating case is empty
    stderr with non-empty stdout.
    """

    def test_includes_stdout_when_stderr_is_empty(self):
        """
        AC5 discriminating case: stderr empty, stdout carries the real
        diagnostic. The formatted string must still be informative.
        """
        result = _proc(
            returncode=1,
            stdout="fatal: real diagnostic landed on stdout only",
            stderr="",
            args=["git", "log", "HEAD..@{upstream}", "--oneline"],
        )

        diagnostic = _format_subprocess_failure_diagnostic(result)

        assert "fatal: real diagnostic landed on stdout only" in diagnostic
        assert "exit_code=1" in diagnostic
        assert "git log" in diagnostic

    def test_caps_stdout_and_stderr_length(self):
        """
        AC4: an unbounded stdout/stderr blob must never reach the diagnostic
        uncapped.

        Uses "A"/"B" filler (not "x"/"y") because the fixed diagnostic
        template itself contains a literal "x" (in "exit_code") -- a filler
        character that collides with the template text would make a
        substring count assert the wrong thing.
        """
        result = _proc(
            returncode=1,
            stdout="A" * 5000,
            stderr="B" * 5000,
            args=["git", "pull"],
        )

        diagnostic = _format_subprocess_failure_diagnostic(result)

        assert diagnostic.count("A") <= 1000
        assert diagnostic.count("B") <= 1000


class TestFormatTimeoutDiagnostic:
    """
    Bug #1830 AC2: diagnostic must identify WHICH command timed out, the
    timeout value, and any partial stdout/stderr captured before the kill.
    """

    def test_names_command_timeout_value_and_partial_output(self):
        e = subprocess.TimeoutExpired(
            cmd=["git", "fetch", "origin"],
            timeout=30,
            output="partial-stdout-before-kill",
            stderr="partial-stderr-before-kill",
        )

        diagnostic = _format_timeout_diagnostic(e)

        assert "git fetch origin" in diagnostic
        assert "30" in diagnostic
        assert "partial-stdout-before-kill" in diagnostic
        assert "partial-stderr-before-kill" in diagnostic


# ---------------------------------------------------------------------------
# Bug #1830 AC1/AC3/AC4: fetch timeout classification
# ---------------------------------------------------------------------------


class TestFetchTimeoutClassification:
    def test_fetch_timeout_raises_classified_git_fetch_error(self, updater):
        """
        AC1/AC4 (discriminating): a fetch that TIMES OUT must raise the
        typed GitFetchError classified via classify_fetch_error(), exactly
        like a fetch that fails with a non-zero exit code -- NOT a generic
        RuntimeError. On unmodified code this raises plain RuntimeError, so
        `pytest.raises(GitFetchError)` fails to catch it.
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(
                    cmd=cmd,
                    timeout=30,
                    output="",
                    stderr="ssh: connect to host github.com port 22: Connection timed out",
                )
            raise AssertionError(
                f"unexpected subprocess call after fetch timeout: {cmd}"
            )

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

        assert exc_info.value.category == "transient"

    def test_fetch_timeout_diagnostic_names_command_and_timeout(self, updater):
        """AC2: the raised GitFetchError's message names the fetch command
        and the timeout duration, not just the repo path."""

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            raise AssertionError(
                f"unexpected subprocess call after fetch timeout: {cmd}"
            )

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

        message = str(exc_info.value)
        assert "fetch" in message
        assert "30" in message


class TestNonFetchTimeoutsNotMisclassified:
    """AC3: symbolic-ref and git log timeouts stay plain RuntimeError,
    distinguishable from fetch timeouts, never raised as GitFetchError."""

    def test_symbolic_ref_timeout_raises_plain_runtime_error(self, updater):
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(returncode=0, args=cmd)
            if cmd[:2] == ["git", "symbolic-ref"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError) as exc_info:
                updater.has_changes()

        assert not isinstance(exc_info.value, GitFetchError)
        assert "symbolic-ref" in str(exc_info.value)

    def test_git_log_timeout_raises_plain_runtime_error(self, updater):
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(returncode=0, args=cmd)
            if cmd[:2] == ["git", "symbolic-ref"]:
                return _proc(returncode=0, stdout="refs/heads/main\n", args=cmd)
            if cmd[:2] == ["git", "log"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError) as exc_info:
                updater.has_changes()

        assert not isinstance(exc_info.value, GitFetchError)
        assert "log" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bug #1832 (git_pull_updater.py slice): stderr-only diagnostics fixed at
# representative call sites in each affected method.
# ---------------------------------------------------------------------------


class TestStderrOnlyDiagnosticsFixedInHasChanges:
    def test_git_log_failure_with_empty_stderr_reports_stdout(self, updater):
        """
        AC5 discriminating case wired into has_changes(): git log fails with
        empty stderr and a real diagnostic on stdout. On unmodified code the
        message interpolates only log_result.stderr ("") -- silently empty.
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(returncode=0, args=cmd)
            if cmd[:2] == ["git", "symbolic-ref"]:
                return _proc(returncode=0, stdout="refs/heads/main\n", args=cmd)
            if cmd[:2] == ["git", "log"]:
                return _proc(
                    returncode=128,
                    stdout="fatal: ambiguous argument 'HEAD..@{upstream}': real diagnostic",
                    stderr="",
                    args=cmd,
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError) as exc_info:
                updater.has_changes()

        message = str(exc_info.value)
        assert "ambiguous argument" in message
        assert "exit_code=128" in message


class TestStderrOnlyDiagnosticsFixedInFetchAndReset:
    def test_fetch_failure_with_empty_stderr_reports_stdout(self, updater):
        """
        AC5 discriminating case wired into _fetch_and_reset(): fetch fails
        with empty stderr and a real diagnostic on stdout.
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(
                    returncode=1,
                    stdout="remote: repository temporarily unavailable (stdout diagnostic)",
                    stderr="",
                    args=cmd,
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError) as exc_info:
                updater._fetch_and_reset("main")

        message = str(exc_info.value)
        assert "repository temporarily unavailable" in message
        assert "exit_code=1" in message

    def test_final_reset_failure_with_empty_stderr_reports_stdout(self, updater):
        """
        AC5 discriminating case wired into _fetch_and_reset()'s final reset
        failure path (not the untracked-file retry branch).
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(returncode=0, args=cmd)
            if cmd[:3] == ["git", "reset", "--hard"]:
                return _proc(
                    returncode=1,
                    stdout="error: real reset failure diagnostic on stdout",
                    stderr="",
                    args=cmd,
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError) as exc_info:
                updater._fetch_and_reset("main")

        message = str(exc_info.value)
        assert "real reset failure diagnostic on stdout" in message
        assert "exit_code=1" in message


class TestStderrOnlyDiagnosticsFixedInUpdate:
    def test_final_pull_failure_with_empty_stderr_reports_stdout(self, updater):
        """
        AC5 discriminating case wired into update()'s final (non-divergent,
        non-untracked-file) git pull failure path.
        """
        git_status_ok = _proc(
            returncode=0, stdout="", args=["git", "status", "--porcelain"]
        )
        git_pull_fail = _proc(
            returncode=1,
            stdout="fatal: real pull failure diagnostic on stdout only",
            stderr="",
            args=["git", "pull"],
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_fail]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        message = str(exc_info.value)
        assert "real pull failure diagnostic on stdout only" in message
        assert "exit_code=1" in message


# ---------------------------------------------------------------------------
# Coordinator follow-up: GitFetchError widening wiring (Bug #1832 AC3 gap --
# refresh_scheduler.py's error.stderr-only sites at ~1036/~1124 need
# GitFetchError to carry stdout/returncode/cmd too).
# ---------------------------------------------------------------------------


class TestGitFetchErrorWidenedFieldsWiredAtNonTimeoutSite:
    def test_non_timeout_fetch_failure_populates_stdout_returncode_cmd(self, updater):
        """
        The non-zero-exit fetch failure raise site in has_changes() must
        populate GitFetchError's new stdout/returncode/cmd fields from the
        real fetch_result, not just stderr/category.
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return _proc(
                    returncode=128,
                    stdout="remote: diagnostic on stdout",
                    stderr="fatal: Could not read from remote repository.",
                    args=cmd,
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

        error = exc_info.value
        assert error.stdout == "remote: diagnostic on stdout"
        assert error.returncode == 128
        assert error.cmd == ["git", "fetch", "origin"]
        # .stderr semantics unchanged: raw, uncapped.
        assert error.stderr == "fatal: Could not read from remote repository."


class TestGitFetchErrorWidenedFieldsWiredAtTimeoutSite:
    def test_fetch_timeout_populates_stdout_cmd_and_none_returncode(self, updater):
        """
        The fetch-TIMEOUT raise site must also populate stdout/cmd. There is
        no exit code for a killed-by-timeout process, so returncode must be
        None (never a fabricated value).
        """

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(
                    cmd=cmd,
                    timeout=30,
                    output="partial stdout before kill",
                    stderr="partial stderr before kill",
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

        error = exc_info.value
        assert error.stdout == "partial stdout before kill"
        assert error.returncode is None
        assert error.cmd == ["git", "fetch", "origin"]

    def test_fetch_timeout_stderr_is_not_capped(self, updater):
        """
        Regression guard: GitFetchError.stderr must stay RAW/uncapped for
        the timeout path too, matching the non-timeout site's established
        uncapped semantics (coordinator directive) -- catches the earlier
        implementation of this fix, which capped it via
        _cap_diagnostic_text() before storing it on the exception.
        """
        long_stderr = "y" * 5000

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(
                    cmd=cmd, timeout=30, output="", stderr=long_stderr
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

        assert exc_info.value.stderr == long_stderr
        assert len(exc_info.value.stderr) == 5000
