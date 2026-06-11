"""_legacy._find_latest_versioned_repo shape-detection consistency (Bug #1084 B5).

``_find_latest_versioned_repo`` reconstructs ``{base}/.versioned/{name}/v_*`` and
picks the newest git repo. Phase B routes its per-directory SHAPE detection
through the single canonical predicate (``snapshot_paths.is_versioned_snapshot``)
instead of a bare ``startswith("v_")`` string test, for consistency with every
other consumer. This is behavior-neutral in production (the Step-0 alias read in
``_resolve_repo_path`` short-circuits this path), but the regression guard below
pins the canonical-shape contract: a non-``v_<digits>`` directory is NOT treated
as a snapshot even if it is a valid git repo.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.server.mcp.handlers._legacy import _find_latest_versioned_repo


def _make_git_versioned(base: Path, name: str, version_dir: str) -> Path:
    d = base / ".versioned" / name / version_dir
    (d / ".git").mkdir(parents=True)
    return d


class TestFindLatestVersionedRepo:
    def test_returns_newest_canonical_version(self, tmp_path):
        _make_git_versioned(tmp_path, "flask", "v_1700000000")
        newest = _make_git_versioned(tmp_path, "flask", "v_1717000000")

        result = _find_latest_versioned_repo(tmp_path, "flask")
        assert result == str(newest)

    def test_rejects_non_v_timestamp_directory(self, tmp_path):
        """A non-canonical dir name must NOT be selected (canonical predicate)."""
        # Only a bogus, non-v_<digits> git dir exists.
        bogus = tmp_path / ".versioned" / "flask" / "snapshot-latest"
        (bogus / ".git").mkdir(parents=True)

        result = _find_latest_versioned_repo(tmp_path, "flask")
        assert result is None, (
            "Directory 'snapshot-latest' is not a canonical v_<ts> snapshot leaf "
            "and must be rejected by the canonical predicate."
        )

    def test_rejects_v_without_digits(self, tmp_path):
        bogus = tmp_path / ".versioned" / "flask" / "v_latest"
        (bogus / ".git").mkdir(parents=True)

        result = _find_latest_versioned_repo(tmp_path, "flask")
        assert result is None

    def test_returns_none_when_no_versioned_dir(self, tmp_path):
        assert _find_latest_versioned_repo(tmp_path, "flask") is None

    def test_skips_non_git_version_dirs(self, tmp_path):
        # A v_* dir that is NOT a git repo is skipped; the git one wins.
        (tmp_path / ".versioned" / "flask" / "v_1717000001").mkdir(parents=True)
        git_one = _make_git_versioned(tmp_path, "flask", "v_1700000000")

        result = _find_latest_versioned_repo(tmp_path, "flask")
        assert result == str(git_one)
