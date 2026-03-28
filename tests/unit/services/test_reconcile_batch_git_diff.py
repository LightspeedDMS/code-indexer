"""Tests for Bug #471: --reconcile batch git diff optimization.

Bug: `cidx index --reconcile` spawns one `git diff --quiet HEAD -- <file>`
subprocess per file. For N files that is N subprocesses at 10-50ms each.

Fix: Add `_get_modified_files_set()` that runs ONE `git diff --name-only HEAD`
command and returns a set of relative file paths, then change
`_file_differs_from_committed_version()` to do a set-membership lookup
against `self._reconcile_modified_files` instead of spawning a subprocess.

Tests:
1. `_get_modified_files_set` returns correct set from unstaged git diff output
2. `_get_modified_files_set` includes staged changes
3. Both staged and unstaged changes are merged in the set
4. `_get_modified_files_set` returns empty set when no changes
5. `_get_modified_files_set` returns empty set on exception
6. `_get_modified_files_set` ignores blank lines in output
7. `_file_differs_from_committed_version` returns True when file is in cached set
8. `_file_differs_from_committed_version` returns False when file is absent from set
9. `_file_differs_from_committed_version` does not spawn subprocess when cache is present
10. Source guard: old per-file `git diff --quiet` call must be absent from production code
11. Source guard: `_get_modified_files_set` method must exist on SmartIndexer
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch


from code_indexer.services.smart_indexer import SmartIndexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(tmp_path: Path) -> SmartIndexer:
    """Build a SmartIndexer with minimal real objects (no network, no DB)."""
    from code_indexer.config import Config

    codebase_dir = tmp_path / "repo"
    codebase_dir.mkdir()

    config = Config(codebase_dir=str(codebase_dir))

    embedding_provider = MagicMock()
    vector_store_client = MagicMock()
    metadata_path = tmp_path / "metadata.json"

    indexer = SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store_client,
        metadata_path=metadata_path,
    )
    return indexer


# ---------------------------------------------------------------------------
# Tests for _get_modified_files_set
# ---------------------------------------------------------------------------


class TestGetModifiedFilesSet:
    """_get_modified_files_set should batch-query git once per category."""

    def test_returns_unstaged_modified_files(self, tmp_path):
        """Unstaged changes from `git diff --name-only HEAD` are returned in the set."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--staged" in cmd:
                result.stdout = ""
            else:
                result.stdout = "src/foo.py\nsrc/bar.py\n"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert "src/foo.py" in modified
        assert "src/bar.py" in modified

    def test_returns_staged_modified_files(self, tmp_path):
        """Staged changes from `git diff --name-only --staged HEAD` are included."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--staged" in cmd:
                result.stdout = "src/staged_only.py\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert "src/staged_only.py" in modified

    def test_merges_staged_and_unstaged(self, tmp_path):
        """Both staged and unstaged changes are combined in the result set."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--staged" in cmd:
                result.stdout = "src/staged.py\n"
            else:
                result.stdout = "src/unstaged.py\n"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert "src/staged.py" in modified
        assert "src/unstaged.py" in modified
        assert len(modified) == 2

    def test_returns_empty_set_when_no_changes(self, tmp_path):
        """Clean working tree produces an empty set."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert modified == set()

    def test_returns_empty_set_on_exception(self, tmp_path):
        """If subprocess.run raises, the method returns an empty set gracefully."""
        indexer = _make_indexer(tmp_path)

        with patch("subprocess.run", side_effect=OSError("git not found")):
            modified = indexer._get_modified_files_set()

        assert modified == set()

    def test_ignores_blank_lines_in_output(self, tmp_path):
        """Blank lines in git output do not end up in the returned set."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--staged" in cmd:
                result.stdout = ""
            else:
                result.stdout = "\nsrc/real.py\n\n"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert "" not in modified
        assert "src/real.py" in modified


# ---------------------------------------------------------------------------
# Tests for _file_differs_from_committed_version (set-lookup behaviour)
# ---------------------------------------------------------------------------


class TestFileDiffersFromCommittedVersion:
    """_file_differs_from_committed_version must use the cached set, not subprocess."""

    def test_returns_true_when_file_in_modified_set(self, tmp_path):
        """Returns True for a file present in _reconcile_modified_files."""
        indexer = _make_indexer(tmp_path)
        indexer._reconcile_modified_files = {"src/changed.py", "src/other.py"}

        assert indexer._file_differs_from_committed_version("src/changed.py") is True

    def test_returns_false_when_file_not_in_modified_set(self, tmp_path):
        """Returns False for a file absent from _reconcile_modified_files."""
        indexer = _make_indexer(tmp_path)
        indexer._reconcile_modified_files = {"src/changed.py"}

        assert indexer._file_differs_from_committed_version("src/clean.py") is False

    def test_does_not_spawn_subprocess_when_cache_present(self, tmp_path):
        """No subprocess.run call occurs when _reconcile_modified_files is set."""
        indexer = _make_indexer(tmp_path)
        indexer._reconcile_modified_files = {"src/foo.py"}

        with patch("subprocess.run") as mock_run:
            indexer._file_differs_from_committed_version("src/foo.py")
            indexer._file_differs_from_committed_version("src/bar.py")

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Source-code guards
# ---------------------------------------------------------------------------


class TestSourceCodeGuard:
    """Structural guards ensuring the per-file subprocess call is replaced."""

    def test_file_differs_no_longer_calls_git_diff_quiet_per_file(self):
        """The old per-file `git diff --quiet HEAD` must not appear in
        _file_differs_from_committed_version after the fix."""
        source = inspect.getsource(SmartIndexer._file_differs_from_committed_version)

        assert '["git", "diff", "--quiet", "HEAD"' not in source, (
            "_file_differs_from_committed_version still contains the old per-file "
            "subprocess call. It must be replaced with a set-membership lookup."
        )

    def test_get_modified_files_set_exists(self):
        """SmartIndexer must expose _get_modified_files_set as a method."""
        assert hasattr(SmartIndexer, "_get_modified_files_set"), (
            "SmartIndexer is missing the new _get_modified_files_set() method."
        )
