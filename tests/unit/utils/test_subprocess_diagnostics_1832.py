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
