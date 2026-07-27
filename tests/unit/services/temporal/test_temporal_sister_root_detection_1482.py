"""GitHub Issue #1482 (extension, site 4 -- LOW priority/future-proofing):
standalone CLI query/status, daemon, and watch-mode temporal paths never
consult the TemporalShardResolver at all -- they only ever scan the local
`.code-indexer/index/` directory. Pure standalone CLI usage against an
arbitrary user repository has NO golden-owned sister location, and this
must never be invented for that case (Story #1460 R6 accepted boundary).

The ONE genuine standalone-with-a-real-sister-root case is an operator
running `cidx query`/`cidx status`/watch mode/the daemon directly inside a
golden repo's OWN clone on the server filesystem (bypassing the server) --
that clone lives at one of exactly two pre-existing, well-established
structural layouts this project already relies on elsewhere
(diagnostics_service.py, golden_repo_manager.py,
VersionedSnapshotManager):
  - flat:      <golden_repos_dir>/<alias>/
  - versioned: <golden_repos_dir>/.versioned/<alias>/v_*/

detect_golden_repo_sister_root() recognizes ONLY these two exact shapes --
never a heuristic guess -- and returns None for anything else.
"""

from __future__ import annotations

import pytest

from code_indexer.services.temporal.temporal_sister_root_detection import (
    detect_golden_repo_sister_root,
)


class TestDetectGoldenRepoSisterRootPositive:
    def test_flat_layout_detected(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        repo_dir = golden_repos_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        result = detect_golden_repo_sister_root(repo_dir)

        assert result is not None
        assert result.golden_repos_dir.resolve() == golden_repos_dir.resolve()
        assert result.repo_alias == "myrepo"

    def test_versioned_layout_detected(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        version_dir = golden_repos_dir / ".versioned" / "myrepo" / "v_1785164318"
        version_dir.mkdir(parents=True)

        result = detect_golden_repo_sister_root(version_dir)

        assert result is not None
        assert result.golden_repos_dir.resolve() == golden_repos_dir.resolve()
        assert result.repo_alias == "myrepo"


class TestDetectGoldenRepoSisterRootNegative:
    @pytest.mark.parametrize(
        "relative_parts",
        [
            ("home", "user", "my-project"),  # ordinary standalone repo
            ("golden-repos",),  # repo literally named golden-repos, no nesting
            (
                "golden-repos",
                "snapshots",
                "myrepo",
                "v_123",
            ),  # wrong middle segment name
        ],
    )
    def test_non_golden_repo_shapes_return_none(self, tmp_path, relative_parts):
        repo_dir = tmp_path
        for part in relative_parts:
            repo_dir = repo_dir / part
        repo_dir.mkdir(parents=True)

        assert detect_golden_repo_sister_root(repo_dir) is None

    def test_none_input_returns_none(self):
        assert detect_golden_repo_sister_root(None) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
