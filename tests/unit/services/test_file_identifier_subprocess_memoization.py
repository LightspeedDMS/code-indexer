"""Tests for #676 — FileIdentifier must memoize branch and commit hash.

The invariant data (current branch, current commit hash) must be fetched
exactly once per FileIdentifier instance, not once per file.  Only
`git hash-object` is legitimately per-file.

2 test cases:
  1. git branch --show-current and git rev-parse HEAD each called exactly once
     across two get_file_metadata() calls; git hash-object called once per file.
  2. Memoized branch/commit values appear correctly in both metadata results,
     with call-count assertion confirming memoization is in effect.
"""

import os
from typing import Dict, List, NamedTuple
from unittest.mock import MagicMock, patch

from src.code_indexer.services.file_identifier import FileIdentifier


# ---------------------------------------------------------------------------
# Stable per-file hash map (avoids Python hash randomization)
# ---------------------------------------------------------------------------

_FILE_HASHES: Dict[str, str] = {
    "module_a.py": "aaaa1111bbbb2222",
    "module_b.py": "cccc3333dddd4444",
    "a.py": "deadbeef12345678",
    "b.py": "cafebabe87654321",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_git_result(stdout: str) -> MagicMock:
    """Return a mock CompletedProcess with the given stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = 0
    return result


def _make_fake_runner(
    call_log: List[list],
    *,
    branch: str = "development",
    commit: str = "a38818c394deadbeef0000000000000000000000",
):
    """Return a fake run_git_command that records calls and returns the given
    branch/commit constants.  git hash-object returns a stable per-filename
    value from _FILE_HASHES."""

    def fake_run_git_command(cmd, cwd=None, check=False, **kwargs):
        call_log.append(list(cmd))
        if "hash-object" in cmd:
            fname = os.path.basename(cmd[-1])
            return _make_git_result(_FILE_HASHES.get(fname, "0000000000000000"))
        if "branch" in cmd and "--show-current" in cmd:
            return _make_git_result(branch)
        if "rev-parse" in cmd and "HEAD" in cmd and "--short" not in cmd:
            return _make_git_result(commit)
        return _make_git_result("")

    return fake_run_git_command


class GitCallCounts(NamedTuple):
    branch: int
    commit: int
    hash_object: int


def _extract_call_counts(call_log: List[list]) -> GitCallCounts:
    """Count distinct git command types from the recorded call log."""
    branch = sum(1 for c in call_log if "branch" in c and "--show-current" in c)
    commit = sum(
        1 for c in call_log if "rev-parse" in c and "HEAD" in c and "--short" not in c
    )
    hash_object = sum(1 for c in call_log if "hash-object" in c)
    return GitCallCounts(branch=branch, commit=commit, hash_object=hash_object)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFileIdentifierSubprocessMemoization:
    """FileIdentifier must memoize branch and commit hash across file calls."""

    def test_branch_and_commit_fetched_only_once_across_two_files(self, tmp_path):
        """Two get_file_metadata() calls must invoke:
        - git branch --show-current: exactly once (memoized)
        - git rev-parse HEAD: exactly once (memoized)
        - git hash-object: once per file (2 calls total)
        """
        file_a = tmp_path / "module_a.py"
        file_b = tmp_path / "module_b.py"
        file_a.write_text("# file a")
        file_b.write_text("# file b")

        identifier = FileIdentifier(project_dir=tmp_path)
        identifier.git_available = True

        call_log: List[list] = []

        with patch(
            "src.code_indexer.services.file_identifier.run_git_command",
            side_effect=_make_fake_runner(call_log),
        ):
            identifier.get_file_metadata(file_a)
            identifier.get_file_metadata(file_b)

        counts = _extract_call_counts(call_log)

        assert counts.branch == 1, (
            f"git branch --show-current must be called exactly once (memoized); "
            f"got {counts.branch}"
        )
        assert counts.commit == 1, (
            f"git rev-parse HEAD must be called exactly once (memoized); "
            f"got {counts.commit}"
        )
        assert counts.hash_object == 2, (
            f"git hash-object must be called once per file (2 files); "
            f"got {counts.hash_object}"
        )

    def test_memoized_values_appear_in_both_metadata_results(self, tmp_path):
        """The memoized branch and commit_hash values must appear in both
        metadata dicts, and the subprocess must still be called only once each
        (confirming memoization rather than re-fetching per call)."""
        file_a = tmp_path / "a.py"
        file_b = tmp_path / "b.py"
        file_a.write_text("a")
        file_b.write_text("b")

        identifier = FileIdentifier(project_dir=tmp_path)
        identifier.git_available = True

        call_log: List[list] = []
        expected_branch = "feature-branch"
        expected_commit = "cafebabecafebabecafebabecafebabe12345678"

        with patch(
            "src.code_indexer.services.file_identifier.run_git_command",
            side_effect=_make_fake_runner(
                call_log, branch=expected_branch, commit=expected_commit
            ),
        ):
            meta_a = identifier.get_file_metadata(file_a)
            meta_b = identifier.get_file_metadata(file_b)

        # Value correctness: memoized values must appear in both results
        assert meta_a["branch"] == expected_branch
        assert meta_b["branch"] == expected_branch
        assert meta_a["commit_hash"] == expected_commit
        assert meta_b["commit_hash"] == expected_commit

        # Memoization proof: subprocess called only once for branch and commit
        counts = _extract_call_counts(call_log)
        assert counts.branch == 1, (
            f"branch must be fetched once, not re-fetched per call; got {counts.branch}"
        )
        assert counts.commit == 1, (
            f"commit must be fetched once, not re-fetched per call; got {counts.commit}"
        )
