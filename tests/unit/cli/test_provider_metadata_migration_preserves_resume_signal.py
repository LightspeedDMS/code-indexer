"""
Unit tests for Bug #1624 (reverted): the legacy-metadata.json ->
metadata-{provider}.json one-time migration in _get_provider_metadata_path()
must be a byte-for-byte copy that preserves the resume signal.

Background: a prior session's fix for Bug #1624 normalized a stale
status=in_progress/failed in the migrated COPY to "completed", reasoning
that a frozen stale status would otherwise force a reconcile forever once
RefreshScheduler's status check became provider-aware (Bug #1623). Code
review REJECTED that fix as actively dangerous: `status` is the SOLE gate
on ProgressiveMetadata.can_resume_interrupted_operation()
(progressive_metadata.py), which SmartIndexer branches on to decide
whether to resume an interrupted index. Normalizing status to "completed"
silently disabled that resume path while the actual
files_to_index/current_file_index/completed_files resume payload was
still copied over intact and now unused -- meaning files that were
mid-index at the moment of migration would permanently never get
indexed, while the metadata falsely claimed "completed". A silent partial
index, forbidden by this project's CLAUDE.md ("Fail LOUD").

The premise for the "fix" was also false: complete_indexing()/
start_indexing() overwrite status on every single index run (9 call
sites in smart_indexer.py), so there is no window where a migrated stale
status persists unwritten -- the very next index run (including the
reconcile that Bug #1623 now correctly forces) heals it naturally.

This file replaces test_provider_metadata_migration_status_normalization_1624.py
(deleted) and asserts the opposite invariant: the migration copy must be
faithful and must NOT disable resume capability.
"""

import json
from pathlib import Path

import pytest

from code_indexer.cli import _get_provider_metadata_path
from code_indexer.services.progressive_metadata import ProgressiveMetadata


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".code-indexer"
    d.mkdir()
    return d


class TestResumeSignalSurvivesMigration:
    """Story #2121: the recovery signal is the stored status -- an
    in_progress/failed status routes the next run to a reconcile of the
    store against disk. The migrated provider file must load with the SAME
    status as the legacy file, and the retired stored-list fields are
    dropped on load (never replayed)."""

    @pytest.mark.parametrize("status", ["in_progress", "failed"])
    def test_interrupted_status_survives_migration(
        self, config_dir: Path, status: str
    ) -> None:
        legacy = config_dir / "metadata.json"
        legacy.write_text(
            json.dumps(
                {
                    "status": status,
                    "files_to_index": ["a.py", "b.py", "c.py"],
                    "current_file_index": 1,
                    "completed_files": ["a.py"],
                }
            )
        )

        result = _get_provider_metadata_path(config_dir, "voyage-ai")

        loaded = ProgressiveMetadata(result).metadata
        assert loaded["status"] == status, (
            "Migrating a legacy file with an interrupted status must NOT "
            "normalize it: the status is what routes the next run to the "
            "reconcile recovery (Bug #1624 revert, Story #2121)."
        )
        for retired in ("files_to_index", "current_file_index", "completed_files"):
            assert retired not in loaded

    def test_migration_is_byte_for_byte_copy_of_interrupted_metadata(
        self, config_dir: Path
    ) -> None:
        legacy = config_dir / "metadata.json"
        legacy.write_text(
            json.dumps(
                {
                    "status": "in_progress",
                    "files_to_index": ["a.py", "b.py", "c.py"],
                    "current_file_index": 1,
                    "completed_files": ["a.py"],
                    "current_commit": "abc123def",
                }
            )
        )

        result = _get_provider_metadata_path(config_dir, "voyage-ai")

        assert result.read_bytes() == legacy.read_bytes(), (
            "The migration copy must be a byte-for-byte copy of the "
            "legacy file -- no field, including status, may be mutated."
        )
        migrated = json.loads(result.read_text())
        assert migrated["status"] == "in_progress", (
            "The migration copy must NOT normalize/mutate the status "
            "field -- it must be a faithful byte-for-byte copy."
        )
        assert migrated["files_to_index"] == ["a.py", "b.py", "c.py"]
        assert migrated["current_file_index"] == 1
        assert migrated["completed_files"] == ["a.py"]
        assert migrated["current_commit"] == "abc123def"
