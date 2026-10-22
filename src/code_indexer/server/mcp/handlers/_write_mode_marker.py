"""Write-mode marker and lock outcome for the catalog write-mode tools.

The marker (``golden_repos_dir/.write_mode/{alias}.json``, shared storage)
records the write lock's owner token at enter, so the exit -- on whichever
worker serves it -- releases exactly that lock. It is a release capability,
so it is written private to the service user (directory 0700, file 0600).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

WRITE_MODE_OWNER = "mcp_write_mode"
_MARKER_DIR_MODE = 0o700
_MARKER_FILE_MODE = 0o600


def marker_path(golden_repos_dir: Path, alias: str) -> Path:
    return Path(golden_repos_dir) / ".write_mode" / f"{alias}.json"


def write_marker(
    golden_repos_dir: Path, alias: str, source_path: str, owner_token: str
) -> None:
    """Atomically write a private marker (0600 in a 0700 directory)."""
    from datetime import datetime, timezone

    target = marker_path(golden_repos_dir, alias)
    target.parent.mkdir(mode=_MARKER_DIR_MODE, parents=True, exist_ok=True)
    if stat.S_IMODE(target.parent.stat().st_mode) != _MARKER_DIR_MODE:
        os.chmod(target.parent, _MARKER_DIR_MODE)
    payload = json.dumps(
        {
            "alias": alias,
            "source_path": source_path,
            "owner_token": owner_token,
            "entered_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    ).encode()
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(
        str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, _MARKER_FILE_MODE
    )
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(temporary), str(target))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_marker(
    golden_repos_dir: Path, alias: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The marker as a dict, or (None, why it could not be used)."""
    try:
        data = json.loads(marker_path(golden_repos_dir, alias).read_text())
    except (OSError, ValueError) as exc:
        return None, f"unreadable write-mode marker ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "write-mode marker is not a JSON object"
    return data, None


def renew_write_mode_lease(
    refresh_scheduler: Any, golden_repos_dir: Path, alias: str
) -> bool:
    """Extend the write-mode lock's lease by the token the enter recorded.
    True only when ownership is confirmed; refused, lapsed, taken over or a
    store error is False (an abandoned session still lapses)."""
    marker, problem = read_marker(golden_repos_dir, alias)
    token = marker.get("owner_token") if marker else None
    if not isinstance(token, str) or not token:
        logger.warning(
            "Write-mode lease for %s not renewed: %s",
            alias,
            problem or "marker carries no lock token",
        )
        return False
    try:
        return bool(
            refresh_scheduler.write_lock_manager.renew(
                alias, owner_name=WRITE_MODE_OWNER, owner_token=token
            )
        )
    except Exception as exc:
        logger.warning(
            "Write-mode lease for %s not renewed: %s: %s",
            alias,
            type(exc).__name__,
            exc,
        )
        return False


def renew_app_write_mode_lease(golden_repos_dir: Path, alias: str) -> bool:
    """FileCRUDService's write-mode renewer: the running app's refresh
    scheduler renews by the marker's token; no scheduler confirms nothing."""
    from ._utils import _get_app_refresh_scheduler

    scheduler = _get_app_refresh_scheduler()
    if scheduler is None:
        logger.warning("Write-mode lease for %s not renewed: no scheduler", alias)
        return False
    return renew_write_mode_lease(scheduler, golden_repos_dir, alias)


def finish_write_mode(
    refresh_scheduler: Any,
    repo_alias: str,
    golden_repos_dir: Path,
    alias: str,
    stop_watch: Callable[[str], None],
) -> Dict[str, Any]:
    """End write mode: release the lock with the marker's token, then remove
    the marker, stop its auto-watch and refresh. The marker is removed only
    after a confirmed release (a marker without a usable token is dropped);
    a release that fails or is refused, or a failed refresh, is an error --
    never reported as a completed refresh."""
    marker, problem = read_marker(golden_repos_dir, alias)
    source_path = marker.get("source_path") if marker else None

    def drop_marker() -> None:
        marker_path(golden_repos_dir, alias).unlink(missing_ok=True)
        if source_path:
            stop_watch(source_path)

    token = marker.get("owner_token") if marker else None
    if not isinstance(token, str) or not token:
        drop_marker()
        reason = problem or "write-mode marker carries no lock token"
        logger.warning(
            "exit_write_mode for %s: %s; its write lock is released only when "
            "its lease lapses",
            repo_alias,
            reason,
        )
        return {"success": False, "error": f"{reason}; write lock not released"}
    try:
        released = refresh_scheduler.release_write_lock(
            alias, owner_name=WRITE_MODE_OWNER, owner_token=token
        )
    except Exception as exc:
        logger.warning("exit_write_mode for %s: release failed: %s", repo_alias, exc)
        return {
            "success": False,
            "error": (
                f"releasing the write lock failed ({type(exc).__name__}: {exc}); "
                f"write mode is still active, call exit_write_mode again"
            ),
        }
    if released:
        drop_marker()
    else:
        return {
            "success": False,
            "error": (
                f"write lock for '{alias}' is no longer held by this write mode "
                f"(its lease lapsed and another writer holds it); refresh not run"
            ),
        }
    result = refresh_scheduler._execute_refresh(repo_alias) or {}
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error") or result.get("message") or "refresh failed",
        }
    return {
        "success": True,
        "message": f"Refresh complete, write mode exited for '{repo_alias}'",
    }
