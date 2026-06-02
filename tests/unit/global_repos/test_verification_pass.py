"""
Unit tests for Bug #1038: verification pass — sentinel removed, bool return, single attempt.

Previous v2 contract (Story #724): raises VerificationFailed after 2 failed attempts,
requires FILE_EDIT_COMPLETE as final line of stdout.

New contract (Bug #1038): best-effort, single attempt, returns bool.
  - Returns True when dispatcher succeeds and file is readable + non-empty.
  - Returns False when dispatcher fails or postconditions fail (never raises).
  - No retry loop — exactly one attempt.
  - No FILE_EDIT_COMPLETE sentinel check (removed).

Coverage: happy path, dispatcher failure, empty file, single-attempt invariant.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _VERIFICATION_SEMAPHORE_STATE,
)

_LOGGER = "code_indexer.global_repos.dependency_map_analyzer"


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

    def _make_config(
        self, timeout: int = 60, max_concurrent: int = 2, max_turns: int = 30
    ) -> MagicMock:
        cfg = MagicMock()
        cfg.fact_check_timeout_seconds = timeout
        cfg.max_concurrent_claude_cli = max_concurrent
        cfg.dependency_map_delta_max_turns = max_turns
        return cfg

    def _make_temp_file(self, name: str, content: str) -> Path:
        p = self._tmp / name
        p.write_text(content)
        return p

    def _make_dispatcher_result(self, success: bool, output: str = "") -> Any:
        """Build a mock dispatcher result."""
        result = MagicMock()
        result.success = success
        result.output = output
        result.error = "" if success else "simulated failure"
        return result

    def _run_with_dispatcher(self, temp_file: Path, dispatcher_result: Any) -> bool:
        """Run invoke_verification_pass with dispatcher patched; return bool result."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = dispatcher_result

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            result: bool = self._analyzer.invoke_verification_pass(
                temp_file, [], self._cfg
            )
            return result


# ---------------------------------------------------------------------------
# TestHappyPath
# ---------------------------------------------------------------------------


class TestHappyPath(_VerifBase):
    """invoke_verification_pass returns True when dispatcher succeeds and file is non-empty."""

    def test_happy_path_returns_true(self) -> None:
        """Returns True when dispatcher reports success and file is readable and non-empty."""
        temp_file = self._make_temp_file("happy.md", "# Domain\n\nContent.\n")
        dispatcher_result = self._make_dispatcher_result(
            success=True, output="some output"
        )
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertTrue(result)

    def test_happy_path_dispatcher_called_exactly_once(self) -> None:
        """Dispatcher called exactly once — single attempt, no retry."""
        temp_file = self._make_temp_file("happy2.md", "# Domain\n\nContent.\n")
        dispatcher_result = self._make_dispatcher_result(success=True, output="ok")
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = dispatcher_result

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)

        self.assertEqual(mock_dispatcher.dispatch.call_count, 1)

    def test_happy_path_no_sentinel_required(self) -> None:
        """Returns True even when stdout lacks FILE_EDIT_COMPLETE — sentinel check removed."""
        temp_file = self._make_temp_file("no_sentinel.md", "# Domain\n\nContent.\n")
        # Output has no FILE_EDIT_COMPLETE at all — must still return True
        dispatcher_result = self._make_dispatcher_result(
            success=True, output="Just some trailing text without any sentinel"
        )
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertTrue(result)

    def test_happy_path_trailing_text_after_sentinel_does_not_fail(self) -> None:
        """Returns True when Claude appends trailing text after FILE_EDIT_COMPLETE.

        This is the root cause of Bug #1038: newer models append text after the sentinel,
        but the result is still valid — the file content is the work product.
        """
        temp_file = self._make_temp_file("trailing.md", "# Domain\n\nContent.\n")
        dispatcher_result = self._make_dispatcher_result(
            success=True,
            output="FILE_EDIT_COMPLETE\nTrailing noise from newer model\nMore trailing text",
        )
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# TestDispatcherFailure
# ---------------------------------------------------------------------------


class TestDispatcherFailure(_VerifBase):
    """invoke_verification_pass returns False (not raises) on dispatcher failure."""

    def test_dispatcher_failure_returns_false(self) -> None:
        """Returns False when dispatcher reports failure — no exception raised."""
        temp_file = self._make_temp_file("fail.md", "# Domain\n\nContent.\n")
        dispatcher_result = self._make_dispatcher_result(success=False)
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertFalse(result)

    def test_dispatcher_failure_single_attempt_only(self) -> None:
        """Dispatcher called exactly once even on failure — no retry loop."""
        temp_file = self._make_temp_file("fail_once.md", "# Domain\n\nContent.\n")
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = self._make_dispatcher_result(
            success=False
        )

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)

        self.assertEqual(mock_dispatcher.dispatch.call_count, 1)

    def test_dispatcher_exception_returns_false(self) -> None:
        """Returns False when dispatcher raises an exception — no propagation."""
        temp_file = self._make_temp_file("exc.md", "# Domain\n\nContent.\n")
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.side_effect = RuntimeError("network error")

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            result = self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# TestPostconditionChecks
# ---------------------------------------------------------------------------


class TestPostconditionChecks(_VerifBase):
    """Postcondition checks: file readable and non-empty (sentinel check removed)."""

    def test_empty_file_after_dispatch_returns_false(self) -> None:
        """Returns False when dispatcher succeeds but leaves file empty/whitespace-only."""
        temp_file = self._make_temp_file("empty.md", "# Domain\n\nContent.\n")

        # Simulate dispatcher that empties the file
        def _empty_file_dispatch(**kwargs: Any) -> Any:
            temp_file.write_text("   \n\n\n", encoding="utf-8")
            return self._make_dispatcher_result(success=True, output="done")

        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.side_effect = _empty_file_dispatch

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            result = self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)
        self.assertFalse(result)

    def test_missing_sentinel_does_not_cause_false(self) -> None:
        """Returns True when FILE_EDIT_COMPLETE is absent from stdout — sentinel check removed."""
        temp_file = self._make_temp_file("no_sentinel_ok.md", "# Domain\n\nContent.\n")
        dispatcher_result = self._make_dispatcher_result(
            success=True, output="no sentinel here at all"
        )
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertTrue(result)

    def test_non_empty_file_with_no_sentinel_returns_true(self) -> None:
        """Returns True: file has content and dispatcher succeeded, regardless of stdout."""
        temp_file = self._make_temp_file("content_ok.md", "# Real content\n\nBody.\n")
        dispatcher_result = self._make_dispatcher_result(
            success=True, output="anything at all"
        )
        result = self._run_with_dispatcher(temp_file, dispatcher_result)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# TestSingleAttemptInvariant
# ---------------------------------------------------------------------------


class TestSingleAttemptInvariant(_VerifBase):
    """Single-attempt invariant: dispatcher called exactly once in all failure scenarios."""

    def test_empty_file_does_not_trigger_retry(self) -> None:
        """Dispatcher called exactly once even when file is empty — no retry."""
        temp_file = self._make_temp_file("empty_no_retry.md", "# Content\n")
        call_count = [0]

        def _clear_and_dispatch(**kwargs: Any) -> Any:
            call_count[0] += 1
            temp_file.write_text("", encoding="utf-8")
            return self._make_dispatcher_result(success=True, output="")

        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.side_effect = _clear_and_dispatch

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)

        self.assertEqual(call_count[0], 1)

    def test_never_raises_verification_failed(self) -> None:
        """invoke_verification_pass never raises — VerificationFailed class removed."""
        temp_file = self._make_temp_file("no_raise.md", "# Content\n")
        # All failure modes: dispatcher fails AND file is empty
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = self._make_dispatcher_result(
            success=False
        )

        with patch.object(
            self._analyzer,
            "_get_cached_dispatcher",
            return_value=mock_dispatcher,
        ):
            try:
                self._analyzer.invoke_verification_pass(temp_file, [], self._cfg)
            except Exception as exc:
                self.fail(
                    f"invoke_verification_pass raised {type(exc).__name__}: {exc}"
                )
