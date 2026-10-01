"""
Unit tests for Bug #1591 (part 2): config_fixer.py's GitStateDetector must
record the FULL 40-char git SHA in `current_commit`, not an abbreviated
7-char SHA.

Two independent producers write metadata.json's `current_commit` field:
  - git_topology_service.py's _get_current_commit() -> `git rev-parse HEAD`
    -> full 40-char SHA (the normal indexing path).
  - config_fixer.py's GitStateDetector.detect_git_state() -> used to call
    `git rev-parse --short HEAD` -> abbreviated 7-char SHA.

The consumer, refresh_scheduler.py's _check_stale_index_metadata(), reads
this field as if it always holds a full SHA. Even with Bug #1591's
prefix-tolerant comparison fix, having two producers disagree on format is
an avoidable footgun -- this test locks GitStateDetector to the same
full-SHA format as the other producer so newly-written metadata never
needs the prefix-tolerance fallback at all.

Real git repository used throughout -- no subprocess mocking.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from code_indexer.services.config_fixer import GitStateDetector


@pytest.fixture
def real_git_repo():
    repo_dir = Path(tempfile.mkdtemp(prefix="test_config_fixer_full_sha_1591_"))
    try:
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "file1.txt").write_text("content1")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        yield repo_dir
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def _actual_head(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class TestGitStateDetectorRecordsFullSha:
    def test_detect_git_state_records_full_forty_char_sha(self, real_git_repo):
        actual_head = _actual_head(real_git_repo)

        git_state = GitStateDetector.detect_git_state(real_git_repo)

        assert git_state["git_available"] is True
        assert git_state["current_commit"] == actual_head, (
            "GitStateDetector must record the FULL SHA (matching "
            "git_topology_service.py's producer format), not an "
            "abbreviated short SHA -- Bug #1591 part 2."
        )
        assert len(git_state["current_commit"]) == 40

    def test_detect_git_state_non_git_directory_still_reports_unknown(self, tmp_path):
        """Regression guard: the pre-existing 'not a git repo' failure path
        must be unaffected by the full-SHA fix."""
        non_git_dir = tmp_path / "not_a_repo"
        non_git_dir.mkdir()

        git_state = GitStateDetector.detect_git_state(non_git_dir)

        assert git_state["git_available"] is False
        assert git_state["current_commit"] == "unknown"
