"""
Unit tests for Bug #849 fix: FILE_UNCHANGED sentinel prevents false retries.

When Claude correctly determines no changes are needed in a delta dep-map update,
it should print FILE_UNCHANGED instead of FILE_EDIT_COMPLETE. The analyzer must
detect this and return _DELTA_NOOP so the retry loop knows not to retry.

Tests:
1. _build_file_based_instructions includes FILE_UNCHANGED as a valid completion signal
2. invoke_delta_merge_file returns _DELTA_NOOP when FILE_UNCHANGED appears in stdout
"""

from unittest.mock import patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _DELTA_NOOP,
)

_SUBPROCESS_PATH = "code_indexer.global_repos.dependency_map_analyzer.subprocess.run"
_TEST_TIMEOUT = 60
_TEST_MAX_TURNS = 5


@pytest.fixture
def analyzer(tmp_path):
    """Create DependencyMapAnalyzer instance."""
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta_path = tmp_path / "cidx-meta"
    cidx_meta_path.mkdir()

    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta_path,
        pass_timeout=600,
    )


class TestBuildFileBasedInstructionsFileUnchangedSentinel:
    """
    Bug #849: _build_file_based_instructions must include FILE_UNCHANGED
    as a valid completion signal for the case where no edits are needed.
    """

    def test_build_file_based_instructions_includes_file_unchanged_sentinel(
        self, analyzer, tmp_path
    ):
        """_build_file_based_instructions must include FILE_UNCHANGED as a valid
        completion signal so Claude can signal an intentional no-op."""
        temp_file = tmp_path / "test_domain.md"
        temp_file.write_text("# Test domain content")

        instructions = analyzer._build_file_based_instructions(temp_file)

        assert "FILE_UNCHANGED" in instructions, (
            "Instructions must include FILE_UNCHANGED as a valid completion signal "
            "for the no-changes-needed case. Without this, Claude cannot communicate "
            "an intentional no-op and the retry loop treats it as a failure."
        )

    def test_build_file_based_instructions_still_includes_file_edit_complete(
        self, analyzer, tmp_path
    ):
        """_build_file_based_instructions must still include FILE_EDIT_COMPLETE
        for the normal 'made edits' path."""
        temp_file = tmp_path / "test_domain.md"
        temp_file.write_text("# Test domain content")

        instructions = analyzer._build_file_based_instructions(temp_file)

        assert "FILE_EDIT_COMPLETE" in instructions, (
            "Instructions must still include FILE_EDIT_COMPLETE for the normal "
            "edits-made path."
        )

    def test_build_file_based_instructions_distinguishes_two_completion_paths(
        self, analyzer, tmp_path
    ):
        """Instructions must present both FILE_EDIT_COMPLETE and FILE_UNCHANGED
        as distinct options, so Claude can choose the appropriate one."""
        temp_file = tmp_path / "test_domain.md"
        temp_file.write_text("# Test domain content")

        instructions = analyzer._build_file_based_instructions(temp_file)

        assert "FILE_EDIT_COMPLETE" in instructions
        assert "FILE_UNCHANGED" in instructions
        edit_pos = instructions.index("FILE_EDIT_COMPLETE")
        noop_pos = instructions.index("FILE_UNCHANGED")
        assert edit_pos != noop_pos, (
            "FILE_EDIT_COMPLETE and FILE_UNCHANGED must appear at different positions — "
            "they are distinct completion options, not duplicates."
        )


class TestInvokeDeltaMergeFileNoopSignal:
    """
    Bug #849: invoke_delta_merge_file must return _DELTA_NOOP when Claude
    outputs FILE_UNCHANGED — without checking mtime.
    """

    def _call(self, analyzer, tmp_path, existing="# Domain content\n\nOriginal body"):
        return analyzer.invoke_delta_merge_file(
            domain_name="test-domain",
            existing_content=existing,
            merge_prompt="merge prompt",
            timeout=_TEST_TIMEOUT,
            max_turns=_TEST_MAX_TURNS,
            temp_dir=tmp_path,
        )

    def _make_file_unchanged_no_edit_side_effect(self):
        """Return a side_effect that returns FILE_UNCHANGED without touching the file."""
        import subprocess

        def _side_effect(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="Analyzed the domain. No updates are needed. FILE_UNCHANGED",
                stderr="",
            )

        return _side_effect

    def test_invoke_delta_merge_file_returns_noop_on_file_unchanged_signal(
        self, analyzer, tmp_path
    ):
        """invoke_delta_merge_file returns _DELTA_NOOP when Claude outputs
        FILE_UNCHANGED — this is the key fix for Bug #849."""
        with patch(
            _SUBPROCESS_PATH,
            side_effect=self._make_file_unchanged_no_edit_side_effect(),
        ):
            result = self._call(analyzer, tmp_path)

        assert result == _DELTA_NOOP, (
            f"Expected _DELTA_NOOP when FILE_UNCHANGED in stdout, got {result!r}. "
            "This is the core Bug #849 fix: Claude's intentional no-op must be "
            "distinguished from an invocation failure (which also returns None)."
        )

    def test_delta_noop_is_not_none(self):
        """_DELTA_NOOP sentinel must be a non-None value so callers can distinguish
        it from invocation failures (which return None)."""
        assert _DELTA_NOOP is not None, (
            "_DELTA_NOOP must be non-None to distinguish intentional no-op "
            "from invocation failure."
        )

    def test_delta_noop_is_a_string(self):
        """_DELTA_NOOP sentinel must be a string constant."""
        assert isinstance(_DELTA_NOOP, str), (
            "_DELTA_NOOP must be a string constant for easy comparison."
        )

    def test_file_unchanged_takes_priority_over_mtime_check(self, analyzer, tmp_path):
        """When FILE_UNCHANGED is in stdout, _DELTA_NOOP is returned even though
        mtime is unchanged (which would normally return None)."""
        with patch(
            _SUBPROCESS_PATH,
            side_effect=self._make_file_unchanged_no_edit_side_effect(),
        ):
            result = self._call(analyzer, tmp_path)

        assert result == _DELTA_NOOP
        assert result is not None, (
            "FILE_UNCHANGED must return _DELTA_NOOP, not None. "
            "None would trigger retries; _DELTA_NOOP signals intentional no-op."
        )

    def test_file_unchanged_mid_output_still_detected(self, analyzer, tmp_path):
        """FILE_UNCHANGED can appear anywhere in Claude's output, not just at end."""
        import subprocess

        def _side_effect(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "I reviewed the domain document carefully.\n"
                    "The changed repository does not affect any dependencies.\n"
                    "FILE_UNCHANGED\n"
                    "No further action needed."
                ),
                stderr="",
            )

        with patch(_SUBPROCESS_PATH, side_effect=_side_effect):
            result = self._call(analyzer, tmp_path)

        assert result == _DELTA_NOOP

    def test_normal_edit_path_unaffected(self, analyzer, tmp_path):
        """When Claude edits the file (no FILE_UNCHANGED), returns updated content
        as before — the normal path is unaffected by the bug fix."""
        import subprocess
        import time

        updated_content = "# Domain content\n\nUpdated body after delta merge"

        def _side_effect(*args, **kwargs):
            time.sleep(0.02)
            matched = list(tmp_path.glob("_delta_merge_*.md"))
            assert len(matched) == 1
            matched[0].write_text(updated_content)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""
            )

        with patch(_SUBPROCESS_PATH, side_effect=_side_effect):
            result = self._call(analyzer, tmp_path)

        assert result == updated_content, (
            "Normal edit path must still return updated content; "
            "the bug fix must not break the success path."
        )

    def test_invocation_failure_still_returns_none(self, analyzer, tmp_path):
        """When subprocess raises an exception (invocation failure), returns None
        — not _DELTA_NOOP. Callers need None to know to retry."""
        with patch(_SUBPROCESS_PATH, side_effect=RuntimeError("CLI failed")):
            result = self._call(analyzer, tmp_path)

        assert result is None, (
            "Invocation failure must return None (triggering retries), "
            "not _DELTA_NOOP (which would suppress retries)."
        )
