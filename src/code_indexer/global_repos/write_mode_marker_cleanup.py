"""Cleanup of one write-mode marker (``golden_repos_dir/.write_mode/{alias}.json``).

The marker lives on shared storage, so a live write-mode session of another
node or worker survives this process's restart. A marker's lock is released
only when the lock has no live holder (its lease lapsed, or its holder process
on this host is gone); a live holder keeps both lock and marker, whether or
not the cleanup is forced. The release itself re-checks staleness in the same
atomic step (``release_if_stale``), so a lease renewed after the check keeps
lock and marker. The decision and the release token come from ONE read of the
marker, and only that exact marker is ever removed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

WRITE_MODE_OWNER = "mcp_write_mode"


def _older_than(content: Optional[dict], ttl_seconds: float, name: str) -> bool:
    """True when the marker is past its TTL; unreadable or missing
    timestamps count as past it."""
    entered_at = content.get("entered_at") if content else None
    if not entered_at:
        logger.warning(f"Write mode marker {name} is corrupt or has no 'entered_at'")
        return True
    try:
        moment = datetime.fromisoformat(entered_at)
    except (TypeError, ValueError):
        logger.warning(f"Write mode marker {name} has unparseable 'entered_at'")
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() > ttl_seconds


def _remove_if_unchanged(marker_path: Path, evaluated: str) -> bool:
    """Remove the marker only if it still holds the evaluated content: claim
    it by an atomic rename, compare, and restore it without clobbering a
    newer marker when a new session replaced it meanwhile."""
    claimed = marker_path.with_name(f".{marker_path.name}.cleanup-{uuid.uuid4().hex}")
    try:
        os.rename(marker_path, claimed)
    except FileNotFoundError:
        return False
    try:
        # ^ why sweep_leftover_claims ages a claim from the claim, not from the marker's write
        os.utime(claimed)
    except OSError as exc:
        logger.warning(f"Could not touch marker claim {claimed.name}: {exc}")
    try:
        if claimed.read_text() == evaluated:
            return True
        try:
            os.link(claimed, marker_path)
        except FileExistsError:
            pass  # an even newer marker is already in place
        logger.info(f"Write mode marker {marker_path.name} changed; kept the new one")
        return False
    finally:
        claimed.unlink(missing_ok=True)


def sweep_leftover_claims(write_mode_dir: Path, ttl_seconds: float) -> None:
    """Remove claim files a crashed cleanup left behind (renamed, never
    unlinked) once they are older than the marker TTL."""
    for claimed in write_mode_dir.glob(".*.json.cleanup-*"):
        try:
            modified = claimed.stat().st_mtime
        except FileNotFoundError:
            continue
        # ^ why a claim that may still be restored holds a marker written after the evaluating read => fresh mtime
        if time.time() - modified > ttl_seconds:
            claimed.unlink(missing_ok=True)
            logger.info(f"Removed leftover write mode marker claim {claimed.name}")


def cleanup_write_mode_marker(
    marker_path: Path,
    alias: str,
    *,
    force: bool,
    ttl_seconds: float,
    is_locked: Callable[[str], bool],
    release: Callable[..., Any],
) -> None:
    """Release a gone holder's write-mode lock and remove its marker.

    ``force`` (startup) skips only the marker-age filter; it never releases a
    lock that still has a live holder."""
    try:
        evaluated = marker_path.read_text()
    except FileNotFoundError:
        return
    try:
        parsed = json.loads(evaluated)
        content = parsed if isinstance(parsed, dict) else None
    except ValueError:
        content = None
    if not force and not _older_than(content, ttl_seconds, marker_path.name):
        return
    if is_locked(alias):
        logger.debug(f"Write mode marker {marker_path.name}: lock has a live holder")
        return
    token = (content.get("owner_token") or None) if content else None
    if token is None:
        logger.warning(f"Write mode marker {marker_path.name} has no lock token")
    if not release(alias, WRITE_MODE_OWNER, owner_token=token):
        logger.warning(
            f"Keeping write mode marker {marker_path.name}: release of its lock "
            f"was refused (another holder owns it or its lease was renewed)"
        )
        return
    if _remove_if_unchanged(marker_path, evaluated):
        logger.info(
            f"Cleaned up write mode marker {marker_path.name} (alias={alias!r})"
        )


def cleanup_write_mode_markers(
    write_mode_dir: Path,
    *,
    force: bool,
    ttl_seconds: float,
    is_locked: Callable[[str], bool],
    release: Callable[..., Any],
) -> None:
    """Clean every marker in ``write_mode_dir`` (one failure never stops the
    sweep), then remove leftover claims."""
    if not write_mode_dir.exists():
        return
    for marker_path in write_mode_dir.glob("*.json"):
        try:
            cleanup_write_mode_marker(
                marker_path,
                marker_path.stem,
                force=force,
                ttl_seconds=ttl_seconds,
                is_locked=is_locked,
                release=release,
            )
        except Exception as exc:
            logger.warning(
                f"Unexpected error cleaning write mode marker {marker_path.name}: "
                f"{type(exc).__name__}: {exc}"
            )
    sweep_leftover_claims(write_mode_dir, ttl_seconds)
