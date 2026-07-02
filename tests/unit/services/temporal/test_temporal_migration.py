"""Tests for temporal_migration module (Story #629).

TDD: Tests written BEFORE implementation to drive the design.

Covers:
- test_migration_renames_legacy_dir
- test_migration_detects_provider_from_metadata
- test_migration_probes_dimensions_1024
- test_migration_probes_dimensions_1536_ambiguous
- test_migration_uses_unknown_when_no_metadata
- test_migration_idempotent
- test_migration_skipped_no_legacy
- test_migration_sentinel_created_before_rename
- test_migration_crashed_sentinel_recovery
- test_migration_data_loss_detection
- test_migration_exdev_fallback
- test_migration_skips_versioned_paths
- test_migration_preserves_all_files
"""

import errno
import fcntl
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.temporal.temporal_migration import (
    MigrationResult,
    migrate_legacy_temporal_collection,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def voyage_config():
    """Config mock for voyage-ai provider."""
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai.model = "voyage-code-3"
    return config


@pytest.fixture
def index_path(tmp_path):
    """Standard index path fixture."""
    path = tmp_path / ".code-indexer" / "index"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def legacy_dir(index_path):
    """Pre-created legacy temporal directory."""
    d = index_path / "code-indexer-temporal"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# test_migration_renames_legacy_dir
# ---------------------------------------------------------------------------


def test_migration_renames_legacy_dir(index_path, legacy_dir, voyage_config):
    """Legacy dir is renamed to provider-aware name; legacy dir no longer exists."""
    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    assert not legacy_dir.exists(), "Legacy directory should no longer exist"
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists(), "Provider-aware directory should exist after migration"


# ---------------------------------------------------------------------------
# test_migration_detects_provider_from_metadata
# ---------------------------------------------------------------------------


def test_migration_detects_provider_from_metadata(
    index_path, legacy_dir, voyage_config
):
    """Provider detected from temporal_meta.json overrides config."""
    meta = {"provider_model": "embed-v4.0", "other": "data"}
    (legacy_dir / "temporal_meta.json").write_text(json.dumps(meta))

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    target = index_path / "code-indexer-temporal-embed_v4_0"
    assert target.exists(), "Directory named after metadata provider should exist"
    wrong_target = index_path / "code-indexer-temporal-voyage_code_3"
    assert not wrong_target.exists(), (
        "Should NOT use config provider when metadata present"
    )


# ---------------------------------------------------------------------------
# test_migration_probes_dimensions_1024
# ---------------------------------------------------------------------------


def test_migration_probes_dimensions_1024(index_path, legacy_dir, voyage_config):
    """vector_size=1024 in collection_meta.json maps to voyage-code-3."""
    collection_meta = {"vector_size": 1024, "other": "data"}
    (legacy_dir / "collection_meta.json").write_text(json.dumps(collection_meta))

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists(), "1024 dims should resolve to voyage-code-3"


# ---------------------------------------------------------------------------
# test_migration_probes_dimensions_1536_ambiguous
# ---------------------------------------------------------------------------


def test_migration_probes_dimensions_1536_ambiguous(
    index_path, legacy_dir, voyage_config, caplog
):
    """vector_size=1536 is ambiguous; config used as tiebreaker with warning logged."""
    collection_meta = {"vector_size": 1536}
    (legacy_dir / "collection_meta.json").write_text(json.dumps(collection_meta))

    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    assert any(
        "1536" in r.message or "ambiguous" in r.message.lower() for r in caplog.records
    ), "Warning about ambiguous 1536 dims should be logged"


# ---------------------------------------------------------------------------
# test_migration_uses_unknown_when_no_metadata
# ---------------------------------------------------------------------------


def test_migration_uses_unknown_when_no_metadata(index_path, voyage_config, caplog):
    """When provider cannot be determined, migrates to 'unknown' slug with a warning."""
    legacy = index_path / "code-indexer-temporal"
    legacy.mkdir()

    bad_config = MagicMock()
    bad_config.embedding_provider = "unknown-provider"

    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_temporal_collection(index_path, bad_config)

    assert result == MigrationResult.COMPLETED
    target = index_path / "code-indexer-temporal-unknown"
    assert target.exists(), (
        "Should migrate to 'unknown' slug when provider cannot be determined"
    )


# ---------------------------------------------------------------------------
# test_migration_idempotent
# ---------------------------------------------------------------------------


def test_migration_idempotent(index_path, legacy_dir, voyage_config):
    """Second call returns ALREADY_DONE; no error raised."""
    result1 = migrate_legacy_temporal_collection(index_path, voyage_config)
    assert result1 == MigrationResult.COMPLETED

    result2 = migrate_legacy_temporal_collection(index_path, voyage_config)
    assert result2 == MigrationResult.ALREADY_DONE


# ---------------------------------------------------------------------------
# test_migration_skipped_no_legacy
# ---------------------------------------------------------------------------


def test_migration_skipped_no_legacy(index_path, voyage_config):
    """No legacy dir and no sentinel → SKIPPED."""
    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.SKIPPED


# ---------------------------------------------------------------------------
# test_migration_sentinel_created_before_rename
# ---------------------------------------------------------------------------


def test_migration_sentinel_created_before_rename(
    index_path, legacy_dir, voyage_config
):
    """Sentinel file (.migrating) exists on disk before os.rename is called."""
    sentinel_exists_during_rename = []

    original_rename = os.rename

    def spy_rename(src, dst):
        migrating_sentinels = list(index_path.glob(".temporal-migration-*.migrating"))
        sentinel_exists_during_rename.append(bool(migrating_sentinels))
        original_rename(src, dst)

    with patch("os.rename", side_effect=spy_rename):
        migrate_legacy_temporal_collection(index_path, voyage_config)

    assert sentinel_exists_during_rename, "os.rename should have been called"
    assert sentinel_exists_during_rename[0], (
        "Sentinel should exist when os.rename is called"
    )


# ---------------------------------------------------------------------------
# test_migration_crashed_sentinel_recovery
# ---------------------------------------------------------------------------


def test_migration_crashed_sentinel_recovery(index_path, legacy_dir, voyage_config):
    """Pre-existing .migrating sentinel + legacy dir present → migration retries and completes."""
    sentinel = index_path / ".temporal-migration-voyage_code_3.migrating"
    sentinel.write_text(
        json.dumps(
            {
                "from": str(legacy_dir),
                "to": str(index_path / "code-indexer-temporal-voyage_code_3"),
                "model": "voyage-code-3",
            }
        )
    )

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    assert not legacy_dir.exists()
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists()


# ---------------------------------------------------------------------------
# test_migration_data_loss_detection
# ---------------------------------------------------------------------------


def test_migration_data_loss_detection(index_path, voyage_config):
    """Stale .migrating sentinel + neither source nor target exists → ERROR."""
    target_path = index_path / "code-indexer-temporal-voyage_code_3"
    sentinel = index_path / ".temporal-migration-voyage_code_3.migrating"
    sentinel.write_text(
        json.dumps(
            {
                "from": str(index_path / "code-indexer-temporal"),
                "to": str(target_path),
                "model": "voyage-code-3",
            }
        )
    )

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ERROR


# ---------------------------------------------------------------------------
# test_migration_exdev_fallback
# ---------------------------------------------------------------------------


def test_migration_exdev_fallback(index_path, legacy_dir, voyage_config):
    """When os.rename raises EXDEV, migration uses copy+delete fallback."""
    (legacy_dir / "temporal_meta.json").write_text(json.dumps({"data": "preserved"}))
    (legacy_dir / "vectors.bin").write_bytes(b"\x00\x01\x02")

    def raise_exdev(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link", src)

    with patch("os.rename", side_effect=raise_exdev):
        result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    assert not legacy_dir.exists(), "Legacy dir should be removed after copy+delete"
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists()
    assert (target / "temporal_meta.json").exists()
    assert (target / "vectors.bin").exists()


# ---------------------------------------------------------------------------
# test_migration_skips_versioned_paths
# ---------------------------------------------------------------------------


def test_migration_skips_versioned_paths(tmp_path, voyage_config):
    """Migration skips when index_path is inside a .versioned/ directory."""
    versioned_index = tmp_path / ".versioned" / "my-repo" / "v_20240101" / "index"
    versioned_index.mkdir(parents=True)
    legacy = versioned_index / "code-indexer-temporal"
    legacy.mkdir()

    result = migrate_legacy_temporal_collection(versioned_index, voyage_config)

    assert result == MigrationResult.SKIPPED
    assert legacy.exists(), "Legacy dir must be untouched in versioned path"


# ---------------------------------------------------------------------------
# test_migration_preserves_all_files
# ---------------------------------------------------------------------------


def test_migration_preserves_all_files(index_path, legacy_dir, voyage_config):
    """All files inside legacy dir are preserved after migration."""
    (legacy_dir / "temporal_meta.json").write_text(json.dumps({"version": 1}))
    (legacy_dir / "collection_meta.json").write_text(json.dumps({"vector_size": 1024}))
    subdir = legacy_dir / "vectors"
    subdir.mkdir()
    (subdir / "shard_0.bin").write_bytes(b"\xde\xad\xbe\xef")

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert (target / "temporal_meta.json").exists()
    assert (target / "collection_meta.json").exists()
    assert (target / "vectors" / "shard_0.bin").exists()
    assert (target / "vectors" / "shard_0.bin").read_bytes() == b"\xde\xad\xbe\xef"


# ---------------------------------------------------------------------------
# test_migration_target_already_exists_early_return
# ---------------------------------------------------------------------------


def test_migration_target_already_exists_early_return(
    index_path, legacy_dir, voyage_config
):
    """When target dir already exists alongside legacy dir, returns ALREADY_DONE without touching files."""
    target = index_path / "code-indexer-temporal-voyage_code_3"
    target.mkdir()

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ALREADY_DONE
    assert legacy_dir.exists(), (
        "Legacy dir should be untouched when target already exists"
    )


# ---------------------------------------------------------------------------
# test_migration_migrated_sentinel_early_return
# ---------------------------------------------------------------------------


def test_migration_migrated_sentinel_early_return(
    index_path, legacy_dir, voyage_config
):
    """When .migrated sentinel exists alongside legacy dir (no target dir), returns ALREADY_DONE."""
    # The sentinel must exist BEFORE detection runs, and its name must match what detection produces.
    # With an empty legacy dir and voyage_config, detection falls back to config → voyage-code-3.
    sentinel = index_path / ".temporal-migration-voyage-code-3.migrated"
    sentinel.write_text(
        json.dumps(
            {
                "from": "code-indexer-temporal",
                "to": "code-indexer-temporal-voyage_code_3",
            }
        )
    )

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ALREADY_DONE


# ---------------------------------------------------------------------------
# test_migration_post_lock_race_target_appeared
# ---------------------------------------------------------------------------


def test_migration_post_lock_race_target_appeared(
    index_path, legacy_dir, voyage_config
):
    """After acquiring lock, if legacy is gone and target appeared (another worker won), returns ALREADY_DONE."""
    target = index_path / "code-indexer-temporal-voyage_code_3"
    real_flock = fcntl.flock

    def flock_side_effect(fd, op):
        if op == fcntl.LOCK_EX:
            # Simulate another worker completing migration while we waited for the lock
            if legacy_dir.exists():
                legacy_dir.rename(target)
        real_flock(fd, op)

    with patch(
        "code_indexer.services.temporal.temporal_migration.fcntl.flock",
        side_effect=flock_side_effect,
    ):
        result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ALREADY_DONE


# ---------------------------------------------------------------------------
# test_migration_post_lock_race_both_gone_skipped
# ---------------------------------------------------------------------------


def test_migration_post_lock_race_both_gone_skipped(
    index_path, legacy_dir, voyage_config
):
    """After acquiring lock, if legacy is gone and target also absent and no sentinel, returns SKIPPED."""
    real_flock = fcntl.flock

    def flock_side_effect(fd, op):
        if op == fcntl.LOCK_EX:
            # Legacy dir vanishes without a trace (e.g. manual deletion)
            if legacy_dir.exists():
                legacy_dir.rmdir()
        real_flock(fd, op)

    with patch(
        "code_indexer.services.temporal.temporal_migration.fcntl.flock",
        side_effect=flock_side_effect,
    ):
        result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.SKIPPED


# ---------------------------------------------------------------------------
# test_migration_rename_non_exdev_oserror_returns_error
# ---------------------------------------------------------------------------


def test_migration_rename_non_exdev_oserror_returns_error(
    index_path, legacy_dir, voyage_config
):
    """Non-EXDEV OSError during rename propagates up and returns ERROR."""

    def raise_permission_error(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted", src)

    with patch("os.rename", side_effect=raise_permission_error):
        result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ERROR


# ---------------------------------------------------------------------------
# test_migration_exdev_copy_verification_failure_returns_error
# ---------------------------------------------------------------------------


def test_migration_exdev_copy_verification_failure_returns_error(
    index_path, legacy_dir, voyage_config
):
    """When copytree produces empty target but source has files, RuntimeError → ERROR."""
    # Put a file in legacy_dir so "source was empty" clause doesn't bypass verification
    (legacy_dir / "temporal_meta.json").write_text(
        '{"provider_model": "voyage-code-3"}'
    )

    def raise_exdev(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link", src)

    def empty_copytree(src, dst, **kwargs):
        # Create empty target dir (no files copied) to trigger verification failure
        os.makedirs(dst, exist_ok=True)

    with patch("os.rename", side_effect=raise_exdev):
        with patch("shutil.copytree", side_effect=empty_copytree):
            result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ERROR


# ---------------------------------------------------------------------------
# test_check_stale_sentinel_nondir_index_path
# ---------------------------------------------------------------------------


def test_check_stale_sentinel_nondir_index_path(tmp_path, voyage_config):
    """When index_path does not exist at all, returns SKIPPED without error."""
    nonexistent = tmp_path / "does-not-exist"

    result = migrate_legacy_temporal_collection(nonexistent, voyage_config)

    assert result == MigrationResult.SKIPPED


# ---------------------------------------------------------------------------
# test_check_stale_sentinel_migrated_found
# ---------------------------------------------------------------------------


def test_check_stale_sentinel_migrated_found(index_path, voyage_config):
    """When only a .migrated sentinel exists (no legacy dir), returns ALREADY_DONE."""
    sentinel = index_path / ".temporal-migration-voyage_code_3.migrated"
    sentinel.write_text(
        json.dumps(
            {
                "from": "code-indexer-temporal",
                "to": "code-indexer-temporal-voyage_code_3",
            }
        )
    )

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.ALREADY_DONE


# ---------------------------------------------------------------------------
# test_check_stale_sentinel_corrupt_file
# ---------------------------------------------------------------------------


def test_check_stale_sentinel_corrupt_file(index_path, voyage_config):
    """Corrupt .migrating sentinel (invalid JSON) is deleted and SKIPPED returned."""
    corrupt = index_path / ".temporal-migration-voyage_code_3.migrating"
    corrupt.write_text("NOT_VALID_JSON{{{")

    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.SKIPPED
    assert not corrupt.exists(), "Corrupt sentinel should be removed"


# ---------------------------------------------------------------------------
# test_detect_from_temporal_meta_corrupt_json
# ---------------------------------------------------------------------------


def test_detect_from_temporal_meta_corrupt_json(index_path, legacy_dir, voyage_config):
    """Corrupt temporal_meta.json falls through to next detection strategy."""
    (legacy_dir / "temporal_meta.json").write_text("INVALID{{{")
    # No collection_meta.json — will fall back to config
    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    # Falls back to config: voyage-code-3
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists()


# ---------------------------------------------------------------------------
# test_detect_from_collection_meta_corrupt_json
# ---------------------------------------------------------------------------


def test_detect_from_collection_meta_corrupt_json(
    index_path, legacy_dir, voyage_config
):
    """Corrupt collection_meta.json falls through to config fallback."""
    (legacy_dir / "collection_meta.json").write_text("INVALID{{{")
    # No temporal_meta.json either — falls back to config
    result = migrate_legacy_temporal_collection(index_path, voyage_config)

    assert result == MigrationResult.COMPLETED
    target = index_path / "code-indexer-temporal-voyage_code_3"
    assert target.exists()
