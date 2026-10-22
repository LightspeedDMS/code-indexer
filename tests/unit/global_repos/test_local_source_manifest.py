"""Identity-based change detection for non-git local repositories
(``local_source_manifest``), on real directories."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.local_source_manifest import (
    MANIFEST_RELATIVE_PATH,
    IncompleteSourceListingError,
    list_source,
    read_manifest,
    record_source_manifest,
    source_change_reason,
)
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.fixtures.refresh_scheduler_stores import real_metadata_store


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos" / "cidx-meta"
    (root / "memories").mkdir(parents=True)
    (root / ".code-indexer").mkdir()
    (root / "memories" / "a.md").write_text("alpha\n")
    (root / "repo-one.md").write_text("one\n")
    return root


def _publish(source: Path, name: str = "v_1000") -> Path:
    """Record the manifest as a refresh does, then snapshot the source."""
    record_source_manifest(str(source), None)
    snapshot = source.parent / ".versioned" / source.name / name
    shutil.copytree(source, snapshot, symlinks=True)
    return snapshot


class TestListSource:
    def test_hidden_entries_and_symlinked_dirs_are_not_listed(
        self, source: Path, tmp_path: Path
    ) -> None:
        (source / ".hidden.md").write_text("x")
        (source / ".code-indexer" / "metadata.json").write_text("{}")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "elsewhere.md").write_text("x")
        (source / "linked").symlink_to(outside, target_is_directory=True)

        listing = list_source(str(source))

        assert sorted(listing.files) == ["memories/a.md", "repo-one.md"]
        assert listing.unreadable == ()

    def test_dangling_symlink_is_reported_unreadable(self, source: Path) -> None:
        (source / "gone.md").symlink_to(source / "missing-target.md")

        assert list_source(str(source)).unreadable == ("gone.md",)


class TestSourceChangeReason:
    def test_unchanged_source_is_no_change(self, source: Path) -> None:
        snapshot = _publish(source)

        assert source_change_reason(str(snapshot), str(source)) is None

    def test_older_mtime_on_an_existing_file_is_a_change(self, source: Path) -> None:
        snapshot = _publish(source)
        target = source / "repo-one.md"
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns - 5_000_000_000))

        reason = source_change_reason(str(snapshot), str(source))

        assert reason is not None and "repo-one.md changed" in reason

    def test_added_file_with_an_old_mtime_is_a_change(self, source: Path) -> None:
        snapshot = _publish(source)
        added = source / "memories" / "late.md"
        added.write_text("late\n")
        os.utime(added, (1_000, 1_000))

        reason = source_change_reason(str(snapshot), str(source))

        assert reason is not None and "memories/late.md added" in reason

    def test_removed_file_is_a_change(self, source: Path) -> None:
        snapshot = _publish(source)
        (source / "memories" / "a.md").unlink()

        reason = source_change_reason(str(snapshot), str(source))

        assert reason is not None and "removed" in reason

    def test_unreadable_entry_is_a_change(self, source: Path) -> None:
        snapshot = _publish(source)
        (source / "gone.md").symlink_to(source / "missing-target.md")

        reason = source_change_reason(str(snapshot), str(source))

        assert reason is not None and "could not be read" in reason

    def test_no_published_snapshot_is_a_change(self, source: Path) -> None:
        assert source_change_reason(None, str(source)) is not None

    def test_snapshot_without_manifest_is_a_change(self, source: Path) -> None:
        snapshot = source.parent / ".versioned" / "cidx-meta" / "v_1"
        shutil.copytree(source, snapshot)

        reason = source_change_reason(str(snapshot), str(source))

        assert reason is not None and "no source manifest" in reason

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            json.dumps([1, 2]),
            json.dumps({"format": 99, "files": {}}),
            json.dumps({"format": 1}),
            json.dumps({"format": 1, "files": {"a.md": [1]}}),
            json.dumps({"format": 1, "files": {"a.md": ["1", 2]}}),
            json.dumps({"format": 1, "files": {"a.md": [True, 2]}}),
        ],
    )
    def test_invalid_manifest_is_an_unknown_baseline(
        self, source: Path, content: str
    ) -> None:
        snapshot = _publish(source)
        (snapshot / MANIFEST_RELATIVE_PATH).write_text(content)

        assert read_manifest(str(snapshot))[0] is None
        assert source_change_reason(str(snapshot), str(source)) is not None


class TestRecordSourceManifest:
    def test_incomplete_listing_fails_and_records_nothing(self, source: Path) -> None:
        snapshot = _publish(source)
        recorded = (source / MANIFEST_RELATIVE_PATH).read_bytes()
        (source / "gone.md").symlink_to(source / "missing-target.md")

        with pytest.raises(IncompleteSourceListingError, match="gone.md"):
            record_source_manifest(str(source), str(snapshot))

        assert (source / MANIFEST_RELATIVE_PATH).read_bytes() == recorded

    def test_nothing_removed_since_the_published_snapshot(self, source: Path) -> None:
        snapshot = _publish(source)
        (source / "added.md").write_text("new\n")

        assert record_source_manifest(str(source), str(snapshot)) is False
        recorded, _ = read_manifest(str(source))
        assert recorded is not None and "added.md" in recorded
        assert not (
            source / ".code-indexer" / "refresh_source_manifest.json.tmp"
        ).exists()

    def test_removed_since_the_published_snapshot(self, source: Path) -> None:
        snapshot = _publish(source)
        (source / "repo-one.md").unlink()

        assert record_source_manifest(str(source), str(snapshot)) is True

    def test_unknown_baseline_removes_gone_paths(self, source: Path) -> None:
        snapshot = source.parent / ".versioned" / "cidx-meta" / "v_1"
        shutil.copytree(source, snapshot)

        assert record_source_manifest(str(source), str(snapshot)) is True
        assert record_source_manifest(str(source), None) is True


def _scheduler(golden: Path, tmp_path: Path) -> Any:
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
    )


class TestSchedulerBaseline:
    def test_baseline_is_the_published_target_not_the_newest_snapshot(
        self, source: Path, tmp_path: Path
    ) -> None:
        published = _publish(source, "v_1000")
        (source / "repo-one.md").write_text("one, edited\n")
        # A newer snapshot that was created but never published (failed swap).
        _publish(source, "v_2000")
        scheduler = _scheduler(source.parent, tmp_path)
        scheduler.alias_manager.create_alias(
            "cidx-meta-global", str(published), repo_name="cidx-meta"
        )

        assert scheduler._has_local_changes(str(source), "cidx-meta-global") is True

    def test_alias_on_the_base_clone_is_a_change(
        self, source: Path, tmp_path: Path
    ) -> None:
        record_source_manifest(str(source), None)
        scheduler = _scheduler(source.parent, tmp_path)
        scheduler.alias_manager.create_alias(
            "cidx-meta-global", str(source), repo_name="cidx-meta"
        )

        assert scheduler._has_local_changes(str(source), "cidx-meta-global") is True

    def test_published_snapshot_matching_the_source_is_no_change(
        self, source: Path, tmp_path: Path
    ) -> None:
        published = _publish(source)
        scheduler = _scheduler(source.parent, tmp_path)
        scheduler.alias_manager.create_alias(
            "cidx-meta-global", str(published), repo_name="cidx-meta"
        )

        assert scheduler._has_local_changes(str(source), "cidx-meta-global") is False
