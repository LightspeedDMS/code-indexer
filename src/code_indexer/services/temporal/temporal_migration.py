"""Legacy temporal collection migration (Story #629).

Transparently migrates the legacy 'code-indexer-temporal' collection to
provider-aware naming on first startup after upgrade.
"""

import errno
import fcntl
import json
import logging
import os
import shutil
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Named constants for vector dimensions to avoid magic numbers.
_VOYAGE_CODE_3_DIMS = 1024
_AMBIGUOUS_DIMS = 1536  # Could be voyage-3-large, embed-v4.0, or other providers


class MigrationResult(Enum):
    COMPLETED = "completed"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"
    ERROR = "error"


def migrate_legacy_temporal_collection(
    index_path: Path,
    config: Any,
) -> MigrationResult:
    """Migrate legacy 'code-indexer-temporal' to provider-aware name.

    Crash-safe with parent-directory sentinel. Multi-process safe with
    advisory lock (flock). Handles cross-filesystem moves (EXDEV).

    Args:
        index_path: Path to .code-indexer/index/ directory
        config: CIDX Config object (for fallback provider detection)

    Returns:
        MigrationResult indicating outcome
    """
    from .temporal_collection_naming import (
        LEGACY_TEMPORAL_COLLECTION,
        resolve_temporal_collection_name,
    )

    index_path = Path(index_path)

    if ".versioned" in str(index_path):
        logger.warning(
            "Temporal migration: skipping — index path is inside .versioned/ "
            "(immutable content). Migration will apply on next snapshot."
        )
        return MigrationResult.SKIPPED

    legacy_dir = index_path / LEGACY_TEMPORAL_COLLECTION
    if not legacy_dir.exists() or not legacy_dir.is_dir():
        return _check_stale_sentinel(index_path, config)

    model_name = _detect_provider_from_disk(legacy_dir, config)
    target_name = resolve_temporal_collection_name(model_name)
    target_dir = index_path / target_name

    if target_dir.exists() and target_dir.is_dir():
        logger.info(
            "Temporal migration: target '%s' already exists, skipping", target_name
        )
        return MigrationResult.ALREADY_DONE

    sentinel_migrated = index_path / f".temporal-migration-{model_name}.migrated"
    if sentinel_migrated.exists():
        return MigrationResult.ALREADY_DONE

    return _run_locked_migration(
        index_path, legacy_dir, target_dir, target_name, model_name, config
    )


def _run_locked_migration(
    index_path: Path,
    legacy_dir: Path,
    target_dir: Path,
    target_name: str,
    model_name: str,
    config: Any,
) -> MigrationResult:
    """Execute migration under an advisory file lock."""
    from .temporal_collection_naming import LEGACY_TEMPORAL_COLLECTION

    sentinel_migrating = index_path / f".temporal-migration-{model_name}.migrating"
    sentinel_migrated = index_path / f".temporal-migration-{model_name}.migrated"
    lock_path = index_path / ".temporal-migration.lock"

    index_path.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        post_lock = _handle_post_lock_recheck(
            legacy_dir, target_dir, target_name, sentinel_migrating, sentinel_migrated
        )
        if post_lock is not None:
            return post_lock

        sentinel_migrating.write_text(
            json.dumps(
                {"from": str(legacy_dir), "to": str(target_dir), "model": model_name}
            )
        )

        logger.info(
            "Temporal migration: renaming '%s' -> '%s' (one-way migration, "
            "downgrade will require cidx index --index-commits --force)",
            LEGACY_TEMPORAL_COLLECTION,
            target_name,
        )

        _perform_rename_with_exdev_fallback(legacy_dir, target_dir)

        sentinel_migrating.unlink(missing_ok=True)
        sentinel_migrated.write_text(
            json.dumps(
                {
                    "from": LEGACY_TEMPORAL_COLLECTION,
                    "to": target_name,
                    "model": model_name,
                }
            )
        )

        logger.info("Temporal migration completed successfully: %s", target_name)
        return MigrationResult.COMPLETED

    except Exception as e:
        logger.error("Temporal migration failed: %s. Original data preserved.", e)
        return MigrationResult.ERROR
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _handle_post_lock_recheck(
    legacy_dir: Path,
    target_dir: Path,
    target_name: str,
    sentinel_migrating: Path,
    sentinel_migrated: Path,
) -> Optional[MigrationResult]:
    """Re-check state after acquiring lock; return result if already settled, else None."""
    from .temporal_collection_naming import LEGACY_TEMPORAL_COLLECTION

    if legacy_dir.exists():
        return None  # Still need to migrate

    if target_dir.exists():
        sentinel_migrating.unlink(missing_ok=True)
        sentinel_migrated.write_text(
            json.dumps({"from": LEGACY_TEMPORAL_COLLECTION, "to": target_name})
        )
        return MigrationResult.ALREADY_DONE

    if sentinel_migrating.exists():
        logger.error(
            "Temporal migration data loss detected: both source and target "
            "directories are missing. Run cidx index --index-commits --force to rebuild."
        )
        sentinel_migrating.unlink(missing_ok=True)
        return MigrationResult.ERROR

    return MigrationResult.SKIPPED


def _perform_rename_with_exdev_fallback(legacy_dir: Path, target_dir: Path) -> None:
    """Rename legacy_dir to target_dir, using copy+delete on cross-device error."""
    try:
        os.rename(str(legacy_dir), str(target_dir))
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        logger.info("Temporal migration: cross-filesystem detected, using copy+delete")
        shutil.copytree(str(legacy_dir), str(target_dir))
        if target_dir.exists() and (
            any(target_dir.iterdir()) or not any(legacy_dir.iterdir())
        ):
            shutil.rmtree(str(legacy_dir))
        else:
            shutil.rmtree(str(target_dir))
            raise RuntimeError("Migration copy verification failed")


def _check_stale_sentinel(index_path: Path, config: Any) -> MigrationResult:
    """Check for stale migration sentinels from crashed migrations."""
    if not index_path.is_dir():
        return MigrationResult.SKIPPED

    for f in index_path.iterdir():
        if f.name.startswith(".temporal-migration-") and f.name.endswith(".migrating"):
            try:
                data = json.loads(f.read_text())
                target = Path(data.get("to", ""))
                if target.exists() and target.is_dir():
                    f.replace(f.with_suffix(".migrated"))
                    return MigrationResult.ALREADY_DONE
                logger.error(
                    "Temporal migration data loss detected: stale sentinel '%s' "
                    "but neither source nor target exist. Run cidx index "
                    "--index-commits --force to rebuild.",
                    f.name,
                )
                f.unlink(missing_ok=True)
                return MigrationResult.ERROR
            except Exception as e:
                logger.debug("Failed to read stale sentinel '%s': %s", f.name, e)
                f.unlink(missing_ok=True)

    for f in index_path.iterdir():
        if f.name.startswith(".temporal-migration-") and f.name.endswith(".migrated"):
            return MigrationResult.ALREADY_DONE

    return MigrationResult.SKIPPED


def _detect_provider_from_disk(legacy_dir: Path, config: Any) -> str:
    """Detect embedding provider from on-disk metadata.

    Priority:
    1. temporal_meta.json provider_model field
    2. Vector dimension probing (1024 -> voyage-code-3)
    3. Current config's primary provider (with warning)
    4. 'unknown' (last resort)
    """
    model = _detect_from_temporal_meta(legacy_dir)
    if model:
        return model

    model = _detect_from_collection_meta(legacy_dir, config)
    if model:
        return model

    return _detect_from_config(config)


def _detect_from_temporal_meta(legacy_dir: Path) -> Optional[str]:
    """Read provider_model from temporal_meta.json if present."""
    meta_file = legacy_dir / "temporal_meta.json"
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text())
        provider_model = meta.get("provider_model")
        if provider_model:
            logger.info(
                "Temporal migration: detected provider '%s' from metadata",
                provider_model,
            )
            return str(provider_model)
    except Exception as e:
        logger.debug("Failed to read temporal_meta.json: %s", e)
    return None


def _detect_from_collection_meta(legacy_dir: Path, config: Any) -> Optional[str]:
    """Probe vector dimensions from collection_meta.json."""
    collection_meta = legacy_dir / "collection_meta.json"
    if not collection_meta.exists():
        return None
    try:
        coll = json.loads(collection_meta.read_text())
        vector_size = coll.get("vector_size")
        if vector_size == _VOYAGE_CODE_3_DIMS:
            logger.info(
                "Temporal migration: detected %d dims -> voyage-code-3",
                _VOYAGE_CODE_3_DIMS,
            )
            return "voyage-code-3"
        if vector_size == _AMBIGUOUS_DIMS:
            return _resolve_ambiguous_1536(config)
    except Exception as e:
        logger.debug("Failed to read collection_meta.json: %s", e)
    return None


def _resolve_ambiguous_1536(config: Any) -> str:
    """Resolve ambiguous 1536-dimension case using config as tiebreaker."""
    from .temporal_collection_naming import get_model_name_for_provider

    provider_name = getattr(config, "embedding_provider", "voyage-ai")
    model_name = get_model_name_for_provider(provider_name, config)
    logger.warning(
        "Temporal migration: detected %d dims (ambiguous). "
        "Assuming legacy temporal index was created with %s. "
        "If incorrect, run cidx index --index-commits --force.",
        _AMBIGUOUS_DIMS,
        model_name,
    )
    return model_name


def _detect_from_config(config: Any) -> str:
    """Fall back to current config provider, or 'unknown' as last resort."""
    try:
        from .temporal_collection_naming import get_model_name_for_provider

        provider_name = getattr(config, "embedding_provider", "voyage-ai")
        model_name = get_model_name_for_provider(provider_name, config)
        logger.warning(
            "Temporal migration: no metadata found. Using config provider '%s'. "
            "If incorrect, run cidx index --index-commits --force.",
            model_name,
        )
        return model_name
    except Exception as e:
        logger.debug("Failed to detect provider from config: %s", e)

    logger.warning(
        "Temporal migration: could not determine provider. "
        "Collection migrated as 'unknown'. Run cidx index --index-commits --force."
    )
    return "unknown"
