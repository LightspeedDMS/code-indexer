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
5. `_get_modified_files_set` raises on any git failure (Story #2121); an
   unborn HEAD yields an empty set
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
                result.stdout = b""
            else:
                result.stdout = b"src/foo.py\0src/bar.py\0"
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
                result.stdout = b"src/staged_only.py\0"
            else:
                result.stdout = b""
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
                result.stdout = b"src/staged.py\0"
            else:
                result.stdout = b"src/unstaged.py\0"
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
            result.stdout = b""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            modified = indexer._get_modified_files_set()

        assert modified == set()

    def test_raises_when_git_cannot_run(self, tmp_path):
        """Story #2121 (Codex P1): a git that cannot run is never read as
        "nothing modified"; the reconcile fails loudly instead."""
        import pytest

        indexer = _make_indexer(tmp_path)

        with patch("subprocess.run", side_effect=OSError("git not found")):
            with pytest.raises(RuntimeError, match="working changes"):
                indexer._get_modified_files_set()

    def test_raises_when_git_diff_exits_non_zero(self, tmp_path):
        """A failed ``git diff`` raises, naming the command."""
        import subprocess

        import pytest

        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: index broken\n")

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="git diff -z --name-only HEAD"):
                indexer._get_modified_files_set()

    def test_returns_empty_set_for_a_repository_without_commits(self, tmp_path):
        """No commit yet (unborn HEAD) is not an error: nothing has a
        committed version to differ from."""
        import subprocess

        indexer = _make_indexer(tmp_path)
        codebase_dir = Path(indexer.config.codebase_dir)
        subprocess.run(
            ["git", "init", "-q", str(codebase_dir)], check=True, capture_output=True
        )
        (codebase_dir / "a.py").write_text("print('a')\n")

        assert indexer._get_modified_files_set() == set()

    @staticmethod
    def _committed_repo(indexer) -> Path:
        """A real repository with one commit in the indexer's codebase_dir."""
        import subprocess

        codebase_dir = Path(indexer.config.codebase_dir)
        (codebase_dir / "a.py").write_text("print('a')\n")
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            [
                "-c",
                "user.name=E",
                "-c",
                "user.email=e@example.com",
                "commit",
                "-qm",
                "i",
            ],
        ):
            subprocess.run(
                ["git", *args], cwd=codebase_dir, check=True, capture_output=True
            )
        return codebase_dir

    def test_raises_when_head_ref_points_to_a_missing_object(self, tmp_path):
        """A HEAD that cannot resolve is not an unborn branch: it raises
        (Story #2121, Codex round 2)."""
        import pytest

        indexer = _make_indexer(tmp_path)
        codebase_dir = self._committed_repo(indexer)
        (codebase_dir / ".git" / "refs" / "heads" / "main").write_text("12" * 20 + "\n")

        with pytest.raises(RuntimeError, match="working changes"):
            indexer._get_modified_files_set()

    def test_raises_for_an_orphan_branch_while_other_refs_exist(self, tmp_path):
        """An unresolved branch in a repository that has other refs (an
        orphan branch, or a lost ref file) is not provably unborn: raise."""
        import subprocess

        import pytest

        indexer = _make_indexer(tmp_path)
        codebase_dir = self._committed_repo(indexer)
        subprocess.run(
            ["git", "checkout", "-q", "--orphan", "other"],
            cwd=codebase_dir,
            check=True,
            capture_output=True,
        )

        with pytest.raises(RuntimeError, match="working changes"):
            indexer._get_modified_files_set()

    def test_ignores_blank_lines_in_output(self, tmp_path):
        """Blank lines in git output do not end up in the returned set."""
        indexer = _make_indexer(tmp_path)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--staged" in cmd:
                result.stdout = b""
            else:
                result.stdout = b"\0src/real.py\0\0"
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
