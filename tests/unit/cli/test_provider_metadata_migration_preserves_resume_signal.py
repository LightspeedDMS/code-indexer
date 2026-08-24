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
    """The reviewer's exact proof-of-concept: can_resume_interrupted_operation()
    must return the SAME result on the migrated provider file as it did on
    the legacy file before migration."""

    def test_in_progress_resume_signal_survives_migration(
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
                }
            )
        )

        legacy_resume = ProgressiveMetadata(legacy).can_resume_interrupted_operation()
        assert legacy_resume is True, "test fixture sanity check"

        result = _get_provider_metadata_path(config_dir, "voyage-ai")

        provider_resume = ProgressiveMetadata(result).can_resume_interrupted_operation()
        assert provider_resume is True, (
            "Migrating a legacy file with status=in_progress and a "
            "populated resume payload must NOT disable resume capability "
            "-- can_resume_interrupted_operation() must return True on "
            "the migrated provider file, matching the legacy file "
            "(Bug #1624 revert)."
        )

    def test_failed_resume_signal_survives_migration(self, config_dir: Path) -> None:
        legacy = config_dir / "metadata.json"
        legacy.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "files_to_index": ["a.py", "b.py"],
                    "current_file_index": 0,
                    "completed_files": [],
                }
            )
        )

        legacy_resume = ProgressiveMetadata(legacy).can_resume_interrupted_operation()
        assert legacy_resume is True, "test fixture sanity check"

        result = _get_provider_metadata_path(config_dir, "voyage-ai")

        provider_resume = ProgressiveMetadata(result).can_resume_interrupted_operation()
        assert provider_resume is True

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
