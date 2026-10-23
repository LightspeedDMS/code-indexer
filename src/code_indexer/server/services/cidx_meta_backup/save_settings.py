"""Save the cidx-meta backup settings and bootstrap the backup.

The bootstrap (git init, stage-all, commit, remote, push) runs only while
holding the catalog write lock as the exclusive backup owner, the same
owner the refresh's backup phase uses, so no catalog writer piggybacks on
it and no two backup git sequences run at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Union

from code_indexer.global_repos.catalog_lock_holder import held_catalog_backup_lock

from .paths import get_cidx_meta_path

SAVED_MESSAGE = "cidx-meta backup configuration saved"
_BUSY_NOTE = "; the catalog is busy, the next catalog refresh sets up the backup"


def save_backup_settings(
    save: Callable[[], None],
    lifecycle_manager: Optional[Any],
    server_dir: Union[str, Path],
    remote_url: str,
) -> str:
    """Run the audited *save* of the backup settings; with a remote,
    bootstrap the backup holding the catalog lock as the backup owner.

    Returns the status message for the operator: SAVED_MESSAGE when there is
    no remote or the bootstrap ran; with a busy note when another owner held
    the catalog lock (the settings are saved, nothing is bootstrapped, and
    the refresh, whose backup phase bootstraps, is owed to the debouncer).
    Raises before saving anything when a remote is set but the lifecycle
    manager has no refresh scheduler (the catalog lock's owner)."""
    from code_indexer.global_repos.meta_description_hook import owe_cidx_meta_refresh

    # ^ why imported at call time: the route's tests replace the class at its module
    from .bootstrap import CidxMetaBackupBootstrap

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
    with held_catalog_backup_lock(scheduler.write_lock_manager) as acquired:
        if acquired:
            CidxMetaBackupBootstrap().bootstrap(str(repo_root), remote_url)
    if acquired:
        return SAVED_MESSAGE
    owe_cidx_meta_refresh()
    return SAVED_MESSAGE + _BUSY_NOTE
