"""One-off backfill after enabling cross-pod IndexStore serving.

Pointers and registrations written before cross-pod serving existed carry no
recorded index version (``index_store_alias`` / ``index_version``), so a pod
that does not hold their index fails loud on them ("no published index
version"). This brings every one of them to the published state:

* golden repos: a forced refresh (``trigger_refresh_for_repo(force_reset=
  True)``) re-indexes, publishes and records the version with the pointer.
  ``local://`` repos are not force-reset (no remote) and are reported.
* activated copies: when THIS pod holds the copy's index, it is published as
  is and recorded (cheap, exact). Otherwise, with ``rebuild_missing``, the
  copy's index state is re-seeded here from its golden repo's served index
  and a clear reindex is queued for the owner -- the job publishes it. A copy
  neither held here nor rebuilt is reported, never skipped silently: run the
  backfill on the pod that activated it, or with ``rebuild_missing``.

Idempotent: anything already recorded is left alone. ``dry_run`` reports the
plan only. Every per-item failure is reported with its cause; a registry
read failure aborts the whole run (it must never read as "nothing to do").
Refuses under the local IndexStore (there is nothing to publish).
"""

from __future__ import annotations

import logging
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_indexer.server.repositories.background_jobs import (
    DuplicateJobError,
)
from code_indexer.server.services.index_materializer import is_served_here
from code_indexer.utils.index_store import (
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    IndexStore,
    index_root,
)

logger = logging.getLogger(__name__)

#: Index types a rebuilt activated copy is re-indexed with.
REBUILD_INDEX_TYPES = ("semantic", "fts")

_REINDEX_ALREADY_RUNNING = "Another reindex job is already running/pending"

#: Marker left in the copy's index root while a backfill rebuild is in
#: progress.  A re-run checks whether the reindex job is still active;
#: if not (completed or failed) and the registration is still unrecorded,
#: the marker is cleared so the copy can be retried.
BACKFILL_REBUILD_MARKER = ".cidx-backfill-rebuild-in-progress"


class BackfillRefusedError(RuntimeError):
    """The backfill cannot run in this deployment (local IndexStore)."""


def refuse_if_local(store: IndexStore) -> None:
    """Raise ``BackfillRefusedError`` under the local IndexStore."""
    if store.is_local:
        raise BackfillRefusedError(
            "index_store_backend is local: indexes are read where they are "
            "built, there is nothing to publish"
        )


@dataclass
class BackfillReport:
    """What a backfill run did (or, with ``dry_run``, would do)."""

    pod: str
    dry_run: bool
    golden_refreshed: Dict[str, Optional[str]] = field(default_factory=dict)
    golden_skipped: Dict[str, str] = field(default_factory=dict)
    golden_failed: Dict[str, str] = field(default_factory=dict)
    golden_already_queued: Dict[str, str] = field(default_factory=dict)
    activated_published: List[str] = field(default_factory=list)
    activated_rebuilt: Dict[str, str] = field(default_factory=dict)
    activated_not_held_here: List[str] = field(default_factory=list)
    activated_failed: Dict[str, str] = field(default_factory=dict)
    activated_already_queued: Dict[str, str] = field(default_factory=dict)
    activated_rebuild_in_progress: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _recorded(document: Optional[Dict[str, Any]]) -> bool:
    fields = document or {}
    return bool(fields.get(INDEX_STORE_ALIAS_FIELD)) and bool(
        fields.get(INDEX_VERSION_FIELD)
    )


class IndexStoreBackfill:
    """Bring pre-existing golden pointers and activated copies to published."""

    def __init__(
        self,
        *,
        store: IndexStore,
        alias_records: Callable[[], Dict[str, Dict[str, Any]]],
        golden_repos: Callable[[], Dict[str, str]],
        refresh: Callable[[str, str], Optional[str]],
        activated_repo_manager: Any,
        index_manager: Any,
        pod: Optional[str] = None,
    ) -> None:
        """
        Args:
            store: the server's IndexStore.
            alias_records: every golden alias document, by global alias.
            golden_repos: golden alias -> repo_url (for ``local://``).
            refresh: ``(global_alias, submitter) -> job id`` forced refresh.
            activated_repo_manager: ``ActivatedRepoManager``.
            index_manager: ``ActivatedRepoIndexManager`` (queues reindexes).
            pod: this pod's name in the report (default: hostname).
        """
        self._store = store
        self._alias_records = alias_records
        self._golden_repos = golden_repos
        self._refresh = refresh
        self._arm = activated_repo_manager
        self._index_manager = index_manager
        self._pod = pod or socket.gethostname()

    def run(
        self, *, submitter: str, dry_run: bool = True, rebuild_missing: bool = False
    ) -> BackfillReport:
        refuse_if_local(self._store)
        report = BackfillReport(pod=self._pod, dry_run=dry_run)
        self._golden(report, submitter, dry_run)
        self._activated(report, dry_run, rebuild_missing)
        logger.info("IndexStore backfill on %s: %s", self._pod, report.as_dict())
        return report

    # -- golden ------------------------------------------------------------

    def _golden(self, report: BackfillReport, submitter: str, dry_run: bool) -> None:
        records = self._alias_records()
        urls = self._golden_repos()
        for global_alias, document in sorted(records.items()):
            if not global_alias.endswith("-global") or _recorded(document):
                continue
            alias = global_alias[: -len("-global")]
            url = urls.get(alias)
            if url is None:
                report.golden_skipped[global_alias] = "no golden repo registered"
                continue
            if url.startswith("local://"):
                report.golden_skipped[global_alias] = (
                    "local:// repo: no remote to force-reset; published by its "
                    "next refresh with a change (pod-local by design otherwise)"
                )
                continue
            if dry_run:
                report.golden_refreshed[global_alias] = None
                continue
            try:
                report.golden_refreshed[global_alias] = self._refresh(
                    global_alias, submitter
                )
            except Exception as exc:
                if _is_already_running(exc):
                    report.golden_already_queued[global_alias] = str(exc)
                else:
                    report.golden_failed[global_alias] = (
                        f"{type(exc).__name__}: {exc}"
                    )

    # -- activated -----------------------------------------------------------

    def _activated(
        self, report: BackfillReport, dry_run: bool, rebuild_missing: bool
    ) -> None:
        for entry in self._arm.list_all_activated_repositories():
            username, user_alias = entry.get("username"), entry.get("user_alias")
            if not username or not user_alias:
                continue
            key = f"{username}/{user_alias}"
            try:
                metadata = self._arm.registration(username, user_alias)
                if metadata is None or _recorded(metadata):
                    continue
                path = Path(self._arm.get_activated_repo_path(username, user_alias))
                if is_served_here(self._store, path):
                    if _has_rebuild_marker(path):
                        if self._reindex_job_active(username):
                            report.activated_rebuild_in_progress.append(key)
                            continue
                        _clear_rebuild_marker(path)
                        logger.info(
                            "Cleared stale rebuild marker for %s (no active job)",
                            key,
                        )
                    if not dry_run:
                        self._arm.publish_index(username, user_alias, fresh=True)
                        report.activated_published.append(key)
                    else:
                        report.activated_published.append(key)
                elif rebuild_missing and not metadata.get("is_composite"):
                    report.activated_rebuilt[key] = (
                        "planned" if dry_run else self._rebuild(username, user_alias)
                    )
                else:
                    report.activated_not_held_here.append(key)
            except Exception as exc:
                if _is_already_running(exc):
                    report.activated_already_queued[key] = str(exc)
                else:
                    report.activated_failed[key] = (
                        f"{type(exc).__name__}: {exc}"
                    )

    def _reindex_job_active(self, username: str) -> bool:
        """True when a reindex job is running or pending for *username*."""
        try:
            for status in ("running", "pending"):
                jobs = self._index_manager.background_job_manager.list_jobs(
                    username=username, status_filter=status, limit=50
                )
                if any(
                    j.get("operation_type") == "reindex"
                    for j in jobs.get("jobs", [])
                ):
                    return True
        except Exception:
            return True
        return False

    def _rebuild(self, username: str, user_alias: str) -> str:
        """Seed the copy's index state here from its golden repo's served
        index, then queue a clear reindex for the owner (the job publishes).

        If trigger_reindex fails, the seeded index is removed so a re-run
        cannot publish it as the copy's version with ``fresh=True``."""
        self._arm.seed_index_from_golden(username, user_alias)
        copy_path = Path(self._arm.get_activated_repo_path(username, user_alias))
        _set_rebuild_marker(copy_path)
        try:
            job_id: str = self._index_manager.trigger_reindex(
                user_alias, list(REBUILD_INDEX_TYPES), True, username
            )
        except Exception:
            _remove_seeded_index(copy_path)
            raise
        return job_id


def _is_already_running(exc: BaseException) -> bool:
    if isinstance(exc, DuplicateJobError):
        return True
    return isinstance(exc, ValueError) and _REINDEX_ALREADY_RUNNING in str(exc)


def _has_rebuild_marker(copy_path: Path) -> bool:
    return (index_root(copy_path) / BACKFILL_REBUILD_MARKER).exists()


def _clear_rebuild_marker(copy_path: Path) -> None:
    (index_root(copy_path) / BACKFILL_REBUILD_MARKER).unlink(missing_ok=True)


def _set_rebuild_marker(copy_path: Path) -> None:
    marker = index_root(copy_path) / BACKFILL_REBUILD_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _remove_seeded_index(copy_path: Path) -> None:
    idx = index_root(copy_path)
    if idx.is_dir():
        shutil.rmtree(idx, ignore_errors=True)
