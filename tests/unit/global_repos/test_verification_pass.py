"""
Unit tests for Story #724 v2 verification pass — file-edit contract.

v1 machinery (VerificationResult, fallback_reason, discovery_mode, safety guards,
envelope parsing, 30s delay, --output-format json) has been replaced.
v2 contract: Claude edits the temp file in-place, prints FILE_EDIT_COMPLETE sentinel,
raises VerificationFailed after 2 failed attempts.

Coverage: happy path, retry on subprocess exceptions, retry on postcondition failures,
both-attempts-fail scenarios, re-seed invariant.
CLI args coverage: see test_verification_pass_cli_args.py
"""

import os
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Callable
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    VerificationFailed,
    _VERIFICATION_SEMAPHORE_STATE,
)

_LOGGER = "code_indexer.global_repos.dependency_map_analyzer"
_SENTINEL = "FILE_EDIT_COMPLETE"
# Seconds to advance mtime after a simulated Claude edit to guarantee mtime-change detection
_MTIME_ADVANCE_SECONDS = 0.1


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _VerifBase(TestCase):
    """Common setup and execution helpers shared by all verification test classes."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self._tmpdir = tempfile.mkdtemp()
        self._tmp = Path(self._tmpdir)
        repos = self._tmp / "golden-repos"
        repos.mkdir(parents=True, exist_ok=True)
        self._analyzer = DependencyMapAnalyzer(
            golden_repos_root=repos,
            cidx_meta_path=self._tmp / "cidx-meta",
            pass_timeout=60,
            analysis_model="opus",
        )
        self._cfg = self._make_config()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- config helpers --

    def _make_config(
        self, timeout: int = 60, max_concurrent: int = 2, max_turns: int = 30
    ) -> MagicMock:
        cfg = MagicMock()
        cfg.fact_check_timeout_seconds = timeout
        cfg.max_concurrent_claude_cli = max_concurrent
        cfg.dependency_map_delta_max_turns = max_turns
        return cfg

    # -- file helpers --

    def _make_temp_file(self, name: str, content: str) -> Path:
        p = self._tmp / name
        p.write_text(content)
        return p

    def _write_edited(self, path: Path, content: str) -> None:
        """Write content and advance mtime by _MTIME_ADVANCE_SECONDS via os.utime.

        Ensures the mtime-change postcondition is detectable even when two writes
        occur within the same filesystem timestamp resolution window.
        """
        path.write_text(content)
        stat = path.stat()
        new_time = stat.st_mtime + _MTIME_ADVANCE_SECONDS
        os.utime(str(path), (new_time, new_time))

    # -- subprocess result helpers --

    def _ok(self, stdout: str = "") -> subprocess.CompletedProcess:
        # Use >100 chars to avoid _invoke_claude_cli's "very short stdout" WARNING
        if not stdout:
            stdout = ("x" * 120) + f"\n{_SENTINEL}\n"
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )

    def _ok_no_sentinel(self) -> subprocess.CompletedProcess:
        """Return success result whose stdout lacks the sentinel (for postcondition-fail tests)."""
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="x" * 120 + "\nno sentinel here\n", stderr=""
        )

    def _timeout(self) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(cmd=["claude"], timeout=60)

    # -- execution helpers that absorb the repeated log/patch/invoke pattern --

    def _run_with_logs(self, temp_file: Path, run_side_effect: Callable) -> list:
        """Run invoke_verification_pass with subprocess.run patched; return log output lines."""
        with self.assertLogs(_LOGGER, level="DEBUG") as cm:
            with patch("subprocess.run", side_effect=run_side_effect):
                self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)
        return cm.output

    def _run_failing_with_logs(
        self, temp_file: Path, run_side_effect: Callable
    ) -> tuple:
        """Same but expects VerificationFailed; returns (exception, log_output_lines)."""
        with self.assertLogs(_LOGGER, level="DEBUG") as cm:
            with self.assertRaises(VerificationFailed) as ctx:
                with patch("subprocess.run", side_effect=run_side_effect):
                    self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)
        return ctx.exception, cm.output

    # -- assertion helpers --

    def _warning_count(self, log_lines: list) -> int:
        # assertLogs output format: "WARNING:logger.name:message"
        return sum(1 for r in log_lines if r.startswith("WARNING:"))


# ---------------------------------------------------------------------------
# TestHappyPath
# ---------------------------------------------------------------------------


class TestHappyPath(_VerifBase):
    """invoke_verification_pass returns normally when attempt 1 succeeds."""

    def test_happy_path_no_exception(self) -> None:
        """Returns normally when subprocess edits file and emits sentinel."""
        temp_file = self._make_temp_file("happy.md", "# Domain\n\nContent.\n")

        def fake_run(cmd, **kwargs):
            temp_file.write_text("# Domain\n\nVerified.\n")
            return self._ok()

        logs = self._run_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 0)

    def test_happy_path_subscriber_subprocess_called_once(self) -> None:
        """subprocess.run called exactly once on the happy path."""
        temp_file = self._make_temp_file("happy2.md", "# Domain\n\nContent.\n")
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            temp_file.write_text("# Domain\n\nVerified.\n")
            return self._ok()

        self._run_with_logs(temp_file, fake_run)
        self.assertEqual(call_count[0], 1)


# ---------------------------------------------------------------------------
# TestRetrySubprocessException
# ---------------------------------------------------------------------------


class TestRetrySubprocessException(_VerifBase):
    """Attempt 1 raises subprocess exception; attempt 2 succeeds."""

    def _make_first_fails_then_succeeds(self, temp_file: Path, exc) -> Callable:
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise exc
            self._write_edited(temp_file, "# Domain\n\nVerified by attempt 2.\n")
            return self._ok()

        return fake_run

    def test_timeout_first_then_success(self) -> None:
        """TimeoutExpired on attempt 1; success on attempt 2; returns; exactly 1 WARNING."""
        temp_file = self._make_temp_file("retry_timeout.md", "# Domain\n\nOriginal.\n")
        logs = self._run_with_logs(
            temp_file,
            self._make_first_fails_then_succeeds(temp_file, self._timeout()),
        )
        self.assertEqual(self._warning_count(logs), 1)

    def test_exit1_first_then_success(self) -> None:
        """CalledProcessError on attempt 1; success on attempt 2; exactly 1 WARNING."""
        temp_file = self._make_temp_file("retry_exit1.md", "# Domain\n\nOriginal.\n")
        exc = subprocess.CalledProcessError(1, ["claude"], "", "auth error")
        logs = self._run_with_logs(
            temp_file,
            self._make_first_fails_then_succeeds(temp_file, exc),
        )
        self.assertEqual(self._warning_count(logs), 1)


# ---------------------------------------------------------------------------
# TestRetryPostconditionFail
# ---------------------------------------------------------------------------


class TestRetryPostconditionFail(_VerifBase):
    """Attempt 1 fails a postcondition check; attempt 2 succeeds."""

    def test_missing_sentinel_first_then_success(self) -> None:
        """Attempt 1 returns no sentinel; attempt 2 succeeds; exactly 1 WARNING."""
        temp_file = self._make_temp_file("post_sentinel.md", "# Domain\n\nOriginal.\n")
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._ok_no_sentinel()
            temp_file.write_text("# Domain\n\nVerified.\n")
            return self._ok()

        logs = self._run_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 1)

    def test_empty_file_first_then_success(self) -> None:
        """Attempt 1 leaves the file empty/whitespace-only; attempt 2 writes real content; exactly 1 WARNING."""
        temp_file = self._make_temp_file("post_empty.md", "# Domain\n\nOriginal.\n")
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Attempt 1: sentinel present, file written as whitespace-only — fails "empty_file" check
                temp_file.write_text("   \n\n\n", encoding="utf-8")
                return self._ok()
            # Attempt 2: real edit
            temp_file.write_text("# Domain\n\nVerified.\n", encoding="utf-8")
            return self._ok()

        logs = self._run_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 1)

    def test_file_missing_first_then_success(self) -> None:
        """Attempt 1 deletes the temp file (causes read-back to fail); attempt 2 succeeds; exactly 1 WARNING."""
        temp_file = self._make_temp_file("post_missing.md", "# Domain\n\nOriginal.\n")
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Attempt 1: delete the temp file so the postcondition read_text raises OSError
                # invoke_verification_pass re-seeds at the top of attempt 2 so attempt 2 recreates it
                temp_file.unlink(missing_ok=True)
                return self._ok()
            # Attempt 2: normal success — the re-seed at top of attempt 2 restores original content,
            # then fake_run mutates it to "Verified" and returns _ok().
            temp_file.write_text("# Domain\n\nVerified.\n", encoding="utf-8")
            return self._ok()

        logs = self._run_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 1)

    def test_sentinel_not_final_line_first_then_success(self) -> None:
        """Attempt 1 stdout has FILE_EDIT_COMPLETE but followed by trailing content
        (fails the 'last non-empty line equals sentinel' check); attempt 2 succeeds."""
        temp_file = self._make_temp_file(
            "post_sentinel_midline.md", "# Domain\n\nOriginal.\n"
        )
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Attempt 1: still edit the file, but stdout has sentinel mid-output
                temp_file.write_text("# Domain\n\nVerified.\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    # Pad to >100 chars to avoid the "very short stdout" WARNING;
                    # sentinel is present but NOT on the final non-empty line.
                    stdout=("x" * 100)
                    + "\nFILE_EDIT_COMPLETE\nTrailing noise after sentinel\n",
                    stderr="",
                )
            # Attempt 2: clean success with sentinel as last non-empty line
            temp_file.write_text("# Domain\n\nVerified2.\n", encoding="utf-8")
            return self._ok()

        logs = self._run_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 1)


# ---------------------------------------------------------------------------
# TestBothAttemptsFail
# ---------------------------------------------------------------------------


class TestBothAttemptsFail(_VerifBase):
    """Both attempts fail — VerificationFailed raised with exactly 2 WARNINGs."""

    def test_both_timeout_raises_with_2_warnings(self) -> None:
        """TimeoutExpired on both attempts raises VerificationFailed + 2 WARNINGs."""
        temp_file = self._make_temp_file("both_timeout.md", "# Content.\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        _, logs = self._run_failing_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 2)

    def test_both_missing_sentinel_raises_with_2_warnings(self) -> None:
        """Missing sentinel on both attempts raises VerificationFailed + 2 WARNINGs."""
        temp_file = self._make_temp_file("both_sentinel.md", "# Content.\n")

        def fake_run(cmd, **kwargs):
            return self._ok_no_sentinel()

        _, logs = self._run_failing_with_logs(temp_file, fake_run)
        self.assertEqual(self._warning_count(logs), 2)

    def test_failure_message_contains_file_path(self) -> None:
        """VerificationFailed message contains temp file path for debugging."""
        temp_file = self._make_temp_file("fail_path.md", "# Content.\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        exc, _ = self._run_failing_with_logs(temp_file, fake_run)
        self.assertIn(str(temp_file), str(exc))


# ---------------------------------------------------------------------------
# TestReseed
# ---------------------------------------------------------------------------


class TestReseed(_VerifBase):
    """Temp file is re-seeded from original_content before attempt 2."""

    def test_reseed_before_attempt2(self) -> None:
        """Attempt 1 mutates file then raises TimeoutExpired.

        Before attempt 2 subprocess call, file content must equal original (re-seeded).
        """
        original = "# ORIGINAL CONTENT\n\nThis is the baseline.\n"
        temp_file = self._make_temp_file("reseed.md", original)
        content_before_attempt2: list = []
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                temp_file.write_text("# MUTATED BY ATTEMPT 1\n\nSomething else.\n")
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
            content_before_attempt2.append(temp_file.read_text())
            temp_file.write_text("# VERIFIED\n\nAttempt 2.\n")
            return self._ok()

        self._run_with_logs(temp_file, fake_run)

        self.assertEqual(len(content_before_attempt2), 1)
        self.assertEqual(
            content_before_attempt2[0],
            original,
            f"File NOT re-seeded before attempt 2. "
            f"Got: {content_before_attempt2[0]!r}, want: {original!r}",
        )
