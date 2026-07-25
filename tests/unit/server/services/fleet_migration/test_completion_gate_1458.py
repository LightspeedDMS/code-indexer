"""Unit tests for repo_has_zero_residual_temporal_dirs() (Story #1458 AC1 /
AC10 completion gate).

Per AC1's binding Definition of Done clause and AC10's firing condition:
"ZERO residual in-repo temporal directories of EITHER shape" is an
UNCONDITIONAL PHYSICAL ABSENCE check -- a physically-present directory of
either shape (quarter-shard `code-indexer-temporal-{slug}-YYYYQN` OR
quarter-less monolith `code-indexer-temporal-{slug}`) under
`.code-indexer/index/` FAILS this gate regardless of whether it holds
committed rows. This predicate does NOT re-derive disposition (no
row-existence scan, no hnsw_index.bin check) -- that is Story #1457 AC11 /
this story's AC1a's job. It only checks the actual on-disk outcome.
"""

from pathlib import Path

from code_indexer.server.services.fleet_migration.completion_gate import (
    invalidate_post_consolidation_snapshot_marker,
    mark_post_consolidation_snapshot_published,
    repo_has_published_post_consolidation_snapshot,
    repo_has_zero_residual_temporal_dirs,
)


class TestRepoHasZeroResidualTemporalDirs:
    def test_true_when_index_dir_has_no_temporal_directories(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        (index_path / "some_semantic_collection").mkdir()

        assert repo_has_zero_residual_temporal_dirs(index_path) is True

    def test_true_when_index_dir_does_not_exist(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created -- e.g. a repo with no indexes at all yet.
        assert repo_has_zero_residual_temporal_dirs(index_path) is True

    def test_false_when_quarter_shard_directory_present(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        (index_path / "code-indexer-temporal-voyage_code_3-2024Q1").mkdir()

        assert repo_has_zero_residual_temporal_dirs(index_path) is False

    def test_false_when_monolith_directory_present(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        (index_path / "code-indexer-temporal-voyage_code_3").mkdir()

        assert repo_has_zero_residual_temporal_dirs(index_path) is False

    def test_false_regardless_of_committed_row_content(self, tmp_path: Path) -> None:
        # Physical presence FAILS the gate even if the directory is a
        # rowless empty artifact -- disposition (migrate vs sweep) is a
        # SEPARATE concern (Story #1457 AC11 / this story's AC1a); this
        # gate never re-derives it.
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        empty_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q2"
        empty_dir.mkdir()
        # deliberately zero vector_*.json rows inside

        assert repo_has_zero_residual_temporal_dirs(index_path) is False

    def test_true_after_all_temporal_dirs_removed(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        temporal_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        temporal_dir.mkdir()
        assert repo_has_zero_residual_temporal_dirs(index_path) is False

        import shutil

        shutil.rmtree(temporal_dir)

        assert repo_has_zero_residual_temporal_dirs(index_path) is True

    def test_only_matches_files_starting_with_temporal_prefix(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        # A regular semantic collection dir must never false-positive.
        (index_path / "not_a_temporal_collection").mkdir()
        # A FILE (not a dir) named with the temporal prefix must not count.
        (index_path / "code-indexer-temporal-stray-file.txt").write_text("x")

        assert repo_has_zero_residual_temporal_dirs(index_path) is True


class TestPostConsolidationSnapshotPublishedMarker:
    """New CRITICAL finding: 'consolidation done' and 'snapshot published'
    must be DISTINCT, independently-durable states. Without this, a crash
    after consolidation+temporal-bootstrap complete but before/during the
    AC10 snapshot trigger leaves is_repo_already_migrated() reporting
    'migrated' (consolidation-wise it genuinely is) while the snapshot
    NEVER fires -- and since discovery would then skip the repo forever,
    there is no other retry path."""

    def test_absent_by_default(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_absent_when_index_dir_does_not_exist(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created.
        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_present_after_marking(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_marking_is_idempotent_and_durable_across_repeated_calls(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path)
        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_marker_creates_index_dir_if_missing(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created ahead of time.

        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_two_repos_are_independently_tracked(self, tmp_path: Path) -> None:
        index_path_a = tmp_path / "repo-a" / ".code-indexer" / "index"
        index_path_b = tmp_path / "repo-b" / ".code-indexer" / "index"
        index_path_a.mkdir(parents=True)
        index_path_b.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path_a)

        assert repo_has_published_post_consolidation_snapshot(index_path_a) is True
        assert repo_has_published_post_consolidation_snapshot(index_path_b) is False

    def test_invalidate_removes_existing_marker(self, tmp_path: Path) -> None:
        """Codex CRITICAL finding (round 4): a marker from a PRIOR
        migration generation must be durably invalidatable, so it is
        never mistaken for a NEW generation's completion."""
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        mark_post_consolidation_snapshot_published(index_path)
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

        invalidate_post_consolidation_snapshot_marker(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_invalidate_is_noop_when_marker_absent(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        assert repo_has_published_post_consolidation_snapshot(index_path) is False

        invalidate_post_consolidation_snapshot_marker(index_path)  # must not raise

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_invalidate_fsyncs_the_parent_directory_after_unlink(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex CRITICAL finding (round 5): a bare unlink() with no
        parent-directory fsync is inconsistent with the publication path
        (mark_post_consolidation_snapshot_published), which correctly
        fsyncs both the file and the directory. A power loss after the
        unlink but before the directory entry removal is durable can
        leave the OLD marker reappearing on next boot, reopening the
        exact multi-generation staleness bug this fix was supposed to
        close. Spies on the stdlib os.fsync (the actual OS syscall
        nfs_safe_fsync wraps -- a genuinely external dependency) to prove
        a directory-fd fsync is actually attempted."""
        import os as os_module

        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        mark_post_consolidation_snapshot_published(index_path)
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

        fsync_calls = []
        original_fsync = os_module.fsync

        def _spy_fsync(fd):
            fsync_calls.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os_module, "fsync", _spy_fsync)

        invalidate_post_consolidation_snapshot_marker(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False
        assert fsync_calls, (
            "Bug: invalidate_post_consolidation_snapshot_marker() did NOT "
            "fsync the parent directory after unlink() -- the deletion is "
            "not crash-durable."
        )
