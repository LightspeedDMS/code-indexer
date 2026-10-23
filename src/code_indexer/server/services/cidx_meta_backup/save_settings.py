"""Save the cidx-meta backup settings and bootstrap the backup.

The bootstrap (git init, stage-all, commit, remote, push) runs only while
holding the catalog write lock as the exclusive backup owner, the same
owner the refresh's backup phase uses, so no catalog writer piggybacks on
it and no two backup git sequences run at once.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from code_indexer.global_repos.catalog_lock_holder import held_catalog_backup_lock

from .paths import get_cidx_meta_path

logger = logging.getLogger(__name__)

SAVED_MESSAGE = "cidx-meta backup configuration saved"
_BUSY_NOTE = "; the catalog is busy, the next catalog refresh sets up the backup"
_RETRY_NOTE = " -- the next catalog refresh retries it"


def save_backup_settings(
    save: Callable[[], None],
    lifecycle_manager: Optional[Any],
    server_dir: Union[str, Path],
    remote_url: str,
) -> str:
    """Run the audited *save* of the backup settings; with a remote,
    bootstrap the backup holding the catalog lock as the backup owner.

    Returns the status message for the operator, which starts with
    SAVED_MESSAGE whenever *save* committed: plain when there is no remote
    or the bootstrap ran; with a busy note when another owner held the
    catalog lock (nothing is bootstrapped); with a warning naming a neutral
    reason when the bootstrap failed. In the last two cases the refresh,
    whose backup phase bootstraps, is owed to the debouncer. Raises before
    saving anything when a remote is set but the lifecycle manager has no
    refresh scheduler (the catalog lock's owner)."""
    from code_indexer.global_repos.meta_description_hook import owe_cidx_meta_refresh

    # ^ why imported at call time: the route's tests replace the class at its module
    from .bootstrap import BackupPushFailed, CidxMetaBackupBootstrap

    scheduler = (
        lifecycle_manager.refresh_scheduler if lifecycle_manager is not None else None
    )
    if not remote_url:
        save()
        return SAVED_MESSAGE
    if scheduler is None:
        raise RuntimeError(
            "the catalog refresh scheduler is not available; nothing was saved"
        )
    save()
    repo_root = get_cidx_meta_path(Path(server_dir))
    try:
        with held_catalog_backup_lock(scheduler.write_lock_manager) as acquired:
            if acquired:
                CidxMetaBackupBootstrap().bootstrap(str(repo_root), remote_url)
    except Exception as exc:
        # ^ invariant the settings are committed: report the failure, never a failed save
        # ^ security never log {exc}: git output and argv can carry the remote URL's credentials
        reason = exc.reason if isinstance(exc, BackupPushFailed) else "setup failed"
        logger.warning(
            f"cidx-meta backup bootstrap failed after the settings were saved: "
            f"{type(exc).__name__}: {reason}"
        )
        owe_cidx_meta_refresh()
        if isinstance(exc, BackupPushFailed):
            return f"{SAVED_MESSAGE}; the backup push failed: {reason}{_RETRY_NOTE}"
        return (
            f"{SAVED_MESSAGE}; the backup setup failed ({type(exc).__name__})"
            f"{_RETRY_NOTE}"
        )
    if acquired:
        return SAVED_MESSAGE
    owe_cidx_meta_refresh()
    return SAVED_MESSAGE + _BUSY_NOTE
