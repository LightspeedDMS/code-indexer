"""
Unit tests for Story #724 verification pass — timeout + 30s delay + retry (AC8).

Tests: TestTimeoutRetry
"""

import subprocess
import tempfile
from pathlib import Path
from typing import List
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    VERIFICATION_RETRY_DELAY_SECONDS,
    DependencyMapAnalyzer,
    VerificationResult,
    _VERIFICATION_SEMAPHORE_STATE,
)

# Timeout value used in config; the real value is irrelevant because
# subprocess.run is mocked.
_FAKE_TIMEOUT: int = 10

_FAKE_STDOUT_VALID = '{"is_error": false, "result": "{}"}'


def _make_analyzer() -> DependencyMapAnalyzer:
    """Return a minimally configured analyzer using a portable temp directory."""
    tmp = Path(tempfile.gettempdir()) / "test-verification-timeout"
    return DependencyMapAnalyzer(
        golden_repos_root=tmp / "golden-repos",
        cidx_meta_path=tmp / "cidx-meta",
        pass_timeout=60,
        analysis_model="opus",
    )


def _make_config(max_concurrent: int = 2) -> MagicMock:
    cfg = MagicMock()
    cfg.fact_check_timeout_seconds = _FAKE_TIMEOUT
    cfg.max_concurrent_claude_cli = max_concurrent
    return cfg


def _make_success_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=_FAKE_STDOUT_VALID, stderr=""
    )


def _make_timeout_exc() -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(cmd=[], timeout=_FAKE_TIMEOUT)


def _make_recording_run(call_log: List[str], outcomes: list) -> MagicMock:
    """Return a MagicMock for subprocess.run that records each call to call_log.

    `outcomes` is a list where each entry is either an exception to raise or a
    CompletedProcess to return, consumed in order. Using a stateful MagicMock
    with side_effect as a single iterator guarantees the recording callable
    executes for every invocation.
    """
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_log.append("run")
        idx = call_count[0]
        call_count[0] += 1
        outcome = outcomes[idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    mock = MagicMock(side_effect=side_effect)
    return mock


class TestTimeoutRetry(TestCase):
    """Tests for AC8: timeout handling, 30s delay, and single retry."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()

    def test_first_timeout_triggers_single_retry_with_30s_delay(self) -> None:
        """On first timeout, time.sleep(30) is called once, subprocess.run called twice."""
        config = _make_config()
        analyzer = _make_analyzer()

        with (
            patch(
                "subprocess.run",
                side_effect=[_make_timeout_exc(), _make_success_result()],
            ) as mock_run,
            patch("time.sleep") as mock_sleep,
        ):
            analyzer.invoke_verification_pass(
                document_content="original doc " * 10,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=config,
            )

        self.assertEqual(
            mock_run.call_count, 2, "subprocess.run should be called exactly twice"
        )
        mock_sleep.assert_called_once_with(VERIFICATION_RETRY_DELAY_SECONDS)

    def test_double_timeout_returns_fallback_with_original_document(self) -> None:
        """When both attempts time out, fallback_reason is 'double_timeout' and
        verified_document equals the original document_content."""
        config = _make_config()
        analyzer = _make_analyzer()
        original = "original content to verify " * 5

        with (
            patch(
                "subprocess.run",
                side_effect=[_make_timeout_exc(), _make_timeout_exc()],
            ),
            patch("time.sleep"),
        ):
            result = analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=config,
            )

        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "double_timeout")
        self.assertEqual(result.verified_document, original)

    def test_sleep_called_before_retry_not_after(self) -> None:
        """time.sleep must be called between the two subprocess.run invocations.

        A shared call_log records both 'run' entries (via stateful side_effect
        callable) and 'sleep(N)' (via fake_sleep). Asserting the exact order
        proves sleep happens before the retry, not after.
        """
        config = _make_config()
        analyzer = _make_analyzer()
        call_log: List[str] = []

        run_mock = _make_recording_run(
            call_log,
            outcomes=[_make_timeout_exc(), _make_success_result()],
        )

        def fake_sleep(seconds: int) -> None:
            call_log.append(f"sleep({seconds})")

        with (
            patch("subprocess.run", run_mock),
            patch("time.sleep", side_effect=fake_sleep),
        ):
            analyzer.invoke_verification_pass(
                document_content="doc " * 10,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=config,
            )

        self.assertEqual(
            call_log,
            ["run", f"sleep({VERIFICATION_RETRY_DELAY_SECONDS})", "run"],
            f"Unexpected call order: {call_log}",
        )

    def test_semaphore_released_before_sleep(self) -> None:
        """The semaphore must be released BEFORE time.sleep(30) is called.

        Uses max_concurrent=1 so the semaphore has exactly one slot. Inside the
        mocked time.sleep, we attempt a non-blocking acquire on the public API.
        If it succeeds, the caller released before sleeping (correct behavior).
        If it fails, the caller is holding the semaphore while sleeping (bug).
        """
        config = _make_config(max_concurrent=1)
        analyzer = _make_analyzer()

        acquire_succeeded_during_sleep: List[bool] = []

        def fake_sleep(seconds: int) -> None:
            from code_indexer.global_repos.dependency_map_analyzer import (
                _get_verification_semaphore,
            )

            sem = _get_verification_semaphore(1)
            # Non-blocking acquire via public API: True = slot is free
            got_it = sem.acquire(blocking=False)
            acquire_succeeded_during_sleep.append(got_it)
            if got_it:
                sem.release()  # restore so the retry can re-acquire

        with (
            patch(
                "subprocess.run",
                side_effect=[_make_timeout_exc(), _make_success_result()],
            ),
            patch("time.sleep", side_effect=fake_sleep),
        ):
            analyzer.invoke_verification_pass(
                document_content="doc " * 10,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=config,
            )

        self.assertEqual(
            len(acquire_succeeded_during_sleep),
            1,
            "time.sleep should have been called exactly once",
        )
        self.assertTrue(
            acquire_succeeded_during_sleep[0],
            "Semaphore slot was NOT free during sleep — "
            "caller is holding the semaphore while sleeping, violating AC5",
        )
