"""Put a cidx-meta catalog file in place atomically.

The content is staged in a temp file in ``STAGING_DIR_NAME``, a hidden
sibling of the catalog root (same filesystem, outside the catalog tree),
fsync'd, then moved over the destination with ``os.replace``: a snapshot
copy or the backup sync's ``git add -A`` of the catalog sees the previous
file or the new one, never a partial file and never a temp file.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: Hidden staging directory beside the real catalog root (same filesystem,
#: outside the catalog tree) for catalog temp files.
STAGING_DIR_NAME = ".cidx-meta-staging"
#: A staging temp file older than this was left by a crashed writer.
STALE_STAGING_SECONDS = 15 * 60
#: Descriptions and X-Ray patterns: a snapshot copy made as another OS user
#: must be able to read them.
SHARED_CATALOG_FILE_MODE = 0o644


def _sweep_stale_staging(staging_dir: Path) -> None:
    """Remove staging temp files (``.*.tmp``) older than
    ``STALE_STAGING_SECONDS``: a crashed writer's leftovers. Only the staging
    directory is scanned (the destination lives in the catalog), and a file
    another writer removes concurrently is tolerated. Cleanup never blocks the
    write: any other filesystem error ends the sweep with a WARNING."""
    cutoff = time.time() - STALE_STAGING_SECONDS
    try:
        with os.scandir(staging_dir) as entries:
            for entry in entries:
                if not (entry.name.startswith(".") and entry.name.endswith(".tmp")):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False) and (
                        entry.stat(follow_symlinks=False).st_mtime < cutoff
                    ):
                        os.unlink(entry.path)
                        logger.info("Removed stale catalog staging file %s", entry.path)
                except FileNotFoundError:
                    continue
    except OSError as exc:
        logger.warning(
            "Catalog staging sweep of %s stopped (%s: %s); the write proceeds",
            staging_dir,
            type(exc).__name__,
            getattr(exc, "filename", None) or staging_dir,
        )


def write_catalog_file(
    catalog_root: Path, target_path: Path, content: str, *, mode: int
) -> bool:
    """Stage *content* outside *catalog_root*, fsync, and move it over
    *target_path* (a file inside the catalog; missing parents are created)
    with exactly *mode* -- each writer states its own, e.g. owner-only for
    memories, SHARED_CATALOG_FILE_MODE for descriptions and X-Ray patterns.

    Returns False when the content is in place but its directory could not
    be fsync'd (logged): the caller then owes the refresh. Any failure before
    the move removes the temp file and propagates."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_dir = Path(os.path.realpath(target_path.parent))
    staging_dir = Path(os.path.realpath(catalog_root)).parent / STAGING_DIR_NAME
    staging_dir.mkdir(exist_ok=True)
    _sweep_stale_staging(staging_dir)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(staging_dir), prefix=f".{target_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # ^ invariant the mode is set before the move, so the file never appears wider
            os.fchmod(fh.fileno(), mode)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(target_dir / target_path.name))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_exc:
            logger.debug(
                "write_catalog_file: temp file cleanup failed (non-fatal, the "
                "original exception propagates): %s",
                cleanup_exc,
            )
        raise
    try:
        dir_fd = os.open(str(target_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        logger.warning(
            "Catalog file %s is in place but %s could not be fsync'd (%s: %s); "
            "the refresh is owed to the debouncer",
            target_path.name,
            target_dir,
            type(exc).__name__,
            exc,
        )
        return False
    return True
