"""Serve a published index on ANY pod, not only the one that built it.

IndexStore step 2 (``utils/index_store.py``) publishes the index a pod builds
and checks a version out where readers open it. Without this module only the
PUBLISHING pod ever checked anything out: every other pod resolved the alias
(control-plane metadata, shared) to a path that exists on the publisher's disk
alone, and answered "not found". Here a reader that resolves a target the pod
does not hold checks the published version out to that same path first.

Contract:

* The alias document records WHAT is published at its target in the same
  write that moves the pointer: ``index_store_alias`` (the store's key) and
  ``index_version`` (the immutable version). ``published_index_fields``
  builds those fields; publishers pass them as ``extra``.
* ``ensure_published_index(target, document)`` is a no-op under the local
  store and when the pod already serves the target (it built or checked it
  out). Otherwise it checks the recorded version out to ``<target>/<index
  dir>``, runs ``cidx fix-config`` there (exactly as the publishing pod does
  on its snapshot) and only then lets the read proceed.
* Fail loud: a document without the fields, an unreachable store, a failed
  checkout or fix-config raise ``PublishedIndexUnavailableError`` -- never an
  empty result, never the pod's own scratch.
* One checkout per target at a time (threads AND processes, ``flock``); a
  crash mid-checkout leaves an in-progress marker so the next reader redoes
  it instead of trusting a partial tree.
* A checked-out snapshot carries ``CHECKED_OUT_MARKER``. After a checkout,
  this pod's OTHER checked-out snapshots of the same repository that the alias
  no longer names (target or previous) are scheduled for removal -- only
  marked ones, so a snapshot this pod built itself is left to the snapshot
  retention it always had. Removal goes through the server's
  ``CleanupManager`` (query refcount, reader lease, minimum age), never a
  direct delete: a query still reading an old checkout keeps its files.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, Union

from code_indexer.utils.index_store import (
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    IndexStore,
    published_index_fields,
)

__all__ = [
    "CHECKED_OUT_MARKER",
    "IN_PROGRESS_MARKER",
    "PublishedIndexUnavailableError",
    "ensure_current_index",
    "ensure_published_index",
    "publish_index",
    "is_served_here",
    "published_index_fields",
    "run_fix_config",
]

logger = logging.getLogger(__name__)

#: Present in a target while a checkout into it is running (or crashed).
IN_PROGRESS_MARKER = ".cidx-index-checkout-in-progress"
#: Present in a target this module checked out; holds the version.
CHECKED_OUT_MARKER = ".cidx-index-checked-out"

_SNAPSHOT_NAME_RE = re.compile(r"^v_\d+$")
_VERSIONED_DIRNAME = ".versioned"

FixConfig = Callable[[Path], None]

_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class PublishedIndexUnavailableError(RuntimeError):
    """The published index for a target cannot be served on this pod."""


def _index_dir(store: IndexStore, target: Path) -> Path:
    return store.index_root(target) / "index"


def is_served_here(store: IndexStore, target: Union[str, Path]) -> bool:
    """True when this pod holds a complete index at ``target``."""
    path = Path(target)
    return _index_dir(store, path).is_dir() and not (path / IN_PROGRESS_MARKER).exists()


def run_fix_config(target: Path) -> None:
    """``cidx fix-config --force`` in ``target`` so its config names ``target``.

    The same step the publishing pod runs on its own snapshot
    (``RefreshScheduler._fix_snapshot_config``). Raises on failure.
    """
    from code_indexer.server.utils.config_manager import ServerResourceConfig
    from code_indexer.utils.subprocess_env import build_cidx_subprocess_env

    subprocess.run(
        ["cidx", "fix-config", "--force"],
        cwd=str(target),
        capture_output=True,
        text=True,
        env=build_cidx_subprocess_env(),
        timeout=ServerResourceConfig().cidx_fix_config_timeout,
        check=True,
    )


@contextmanager
def _exclusive(target: Path) -> Iterator[None]:
    """Serialize checkouts of ``target`` across threads and processes."""
    key = str(target)
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(key, threading.Lock())
    with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.parent / f".{target.name}.checkout.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _recorded(document: Optional[Mapping[str, Any]], target: Path) -> Tuple[str, str]:
    fields = document or {}
    store_alias = fields.get(INDEX_STORE_ALIAS_FIELD)
    version = fields.get(INDEX_VERSION_FIELD)
    if not isinstance(store_alias, str) or not store_alias:
        store_alias = None
    if not isinstance(version, str) or not version:
        version = None
    if store_alias is None or version is None:
        raise PublishedIndexUnavailableError(
            f"The index for {target} is not on this pod and its alias records no "
            "published index version (index_store_alias/index_version). It was "
            "built before cross-pod checkout existed, or by a path that does not "
            "publish; refresh the repository to publish it."
        )
    return store_alias, version


def ensure_published_index(
    target_path: Union[str, Path],
    document: Optional[Mapping[str, Any]],
    *,
    store: IndexStore,
    fix_config: Optional[FixConfig] = None,
) -> None:
    """Make the index published at ``target_path`` readable on this pod.

    ``document`` is the alias (or registry) document naming ``target_path``.
    No-op under a local store or when the pod already serves the target.
    ``fix_config`` defaults to ``run_fix_config``.
    """
    if store.is_local:
        return
    target = Path(target_path)
    if is_served_here(store, target):
        return
    store_alias, version = _recorded(document, target)
    with _exclusive(target):
        if is_served_here(store, target):
            return
        _checkout(store, target, store_alias, version, fix_config or run_fix_config)
    _prune_superseded(target, document)


def _checkout(
    store: IndexStore,
    target: Path,
    store_alias: str,
    version: str,
    fix_config: FixConfig,
) -> None:
    root = store.index_root(target)
    marker = target / IN_PROGRESS_MARKER
    try:
        target.mkdir(parents=True)
        created = True
    except FileExistsError:
        created = False
    if root.exists():
        if not marker.exists():
            raise PublishedIndexUnavailableError(
                f"{root} exists without an index and was not left by a checkout; "
                "refusing to replace it"
            )
        shutil.rmtree(root)
    checkout_as = getattr(store, "checkout_as", None)
    if checkout_as is None:
        raise PublishedIndexUnavailableError(
            f"IndexStore {type(store).__name__} cannot check out by store alias"
        )
    try:
        marker.write_text(version)
        checkout_as(store_alias, target, version)
        fix_config(target)
        if not _index_dir(store, target).is_dir():
            raise RuntimeError(f"version {version} holds no index directory")
        (target / CHECKED_OUT_MARKER).write_text(version)
        marker.unlink()
    except Exception as exc:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
        raise PublishedIndexUnavailableError(
            f"Could not check out {store_alias} version {version} to {target}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    logger.info("Checked out %s version %s to %s", store_alias, version, target)


def _checked_out_version(target: Path) -> Optional[str]:
    try:
        return (target / CHECKED_OUT_MARKER).read_text()
    except FileNotFoundError:
        return None


def _is_current(store: IndexStore, target: Path, version: str) -> bool:
    return is_served_here(store, target) and _checked_out_version(target) == version


def ensure_current_index(
    target_path: Union[str, Path],
    document: Optional[Mapping[str, Any]],
    *,
    store: IndexStore,
    fix_config: Optional[FixConfig] = None,
) -> None:
    """``ensure_published_index`` for a target whose index CHANGES in place.

    An activated copy keeps one path while its index is re-published (every
    reindex, on whichever pod ran it). The pod serves the target only when it
    holds the version ``document`` records -- its ``CHECKED_OUT_MARKER`` says
    which one it holds; an older one is replaced (checked out beside it, then
    swapped in by rename, so a reader never sees half of either). A document
    recording nothing (an activation from before publishing existed) is
    served from what this pod holds, or fails loud.
    """
    if store.is_local:
        return
    target = Path(target_path)
    recorded = _fields(document)
    if recorded is None:
        if is_served_here(store, target):
            return
        _recorded(document, target)  # raises: nothing here, nothing published
        return
    store_alias, version = recorded
    if _is_current(store, target, version):
        return
    with _exclusive(target):
        if _is_current(store, target, version):
            return
        fix = fix_config or run_fix_config
        if is_served_here(store, target):
            _replace(store, target, store_alias, version, fix)
        else:
            _checkout(store, target, store_alias, version, fix)


def _fields(document: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    try:
        return _recorded(document, Path("."))
    except PublishedIndexUnavailableError:
        return None


#: A replacement version is checked out here, beside the index it replaces.
INCOMING_DIRNAME = ".cidx-index-incoming"
#: The replaced index, until the swap succeeded (then removed).
RETIRED_DIRNAME = ".cidx-index-retired"


def _replace(
    store: IndexStore,
    target: Path,
    store_alias: str,
    version: str,
    fix_config: FixConfig,
) -> None:
    """Swap the index this pod holds at ``target`` for ``version``."""
    checkout_as = getattr(store, "checkout_as", None)
    if checkout_as is None:
        raise PublishedIndexUnavailableError(
            f"IndexStore {type(store).__name__} cannot check out by store alias"
        )
    root = store.index_root(target)
    incoming = target / INCOMING_DIRNAME
    retired = target / RETIRED_DIRNAME
    marker = target / IN_PROGRESS_MARKER
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.rmtree(retired, ignore_errors=True)
    try:
        incoming.mkdir()
        fresh = checkout_as(store_alias, incoming, version)
        marker.write_text(version)
        os.rename(root, retired)
        os.rename(fresh, root)
        fix_config(target)
        if not _index_dir(store, target).is_dir():
            raise RuntimeError(f"version {version} holds no index directory")
        (target / CHECKED_OUT_MARKER).write_text(version)
        marker.unlink()
    except Exception as exc:
        if retired.exists():
            shutil.rmtree(root, ignore_errors=True)
            os.rename(retired, root)
            marker.unlink(missing_ok=True)
        raise PublishedIndexUnavailableError(
            f"Could not replace the index at {target} with {store_alias} "
            f"version {version}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
    logger.info(
        "Replaced the index at %s with %s version %s", target, store_alias, version
    )
    _evict_caches(target)


def _evict_caches(target: Path) -> None:
    """Drop this process's cached HNSW / chunk-store handles under ``target``:
    the files behind them were just swapped for another version."""
    from code_indexer.server.cache.snapshot_cache_invalidation import (
        invalidate_snapshot_caches,
    )

    invalidate_snapshot_caches(
        str(target),
        log_context="[index-replace]",
        is_versioned_snapshot_check=lambda _path: True,
    )


def publish_index(
    target_path: Union[str, Path],
    store_alias: str,
    *,
    store: IndexStore,
    base_version: Optional[str],
) -> Dict[str, str]:
    """Publish the index this pod built at ``target_path`` on top of
    ``base_version`` (the version it was built from; None for the first) and
    mark the pod as holding the new version. Returns the fields to record
    with the registry entry. Raises on any failure (a concurrent publish
    since ``base_version`` included)."""
    publish_on = getattr(store, "publish_on", None)
    if publish_on is None:
        raise PublishedIndexUnavailableError(
            f"IndexStore {type(store).__name__} cannot publish by store alias"
        )
    target = Path(target_path)
    version = publish_on(store_alias, store.index_root(target), base_version)
    if not version:
        raise PublishedIndexUnavailableError(
            f"Publishing the index at {target} returned no version"
        )
    (target / CHECKED_OUT_MARKER).write_text(version)
    return published_index_fields(store_alias, version)


def _prune_superseded(target: Path, document: Optional[Mapping[str, Any]]) -> None:
    """Schedule removal of this pod's checked-out snapshots of the same
    repository that the alias no longer names (``_schedule_removal``). Only
    ``.versioned/<ns>/v_<ts>`` siblings carrying ``CHECKED_OUT_MARKER`` qualify;
    a failure is logged, never raised (the read already succeeded and the
    startup snapshot sweep is the backstop)."""
    if not _SNAPSHOT_NAME_RE.match(target.name):
        return
    if target.parent.parent.name != _VERSIONED_DIRNAME:
        return
    keep = {str(target)}
    previous = (document or {}).get("previous_path")
    if isinstance(previous, str) and previous:
        keep.add(str(Path(previous)))
    try:
        siblings = list(target.parent.iterdir())
    except OSError as exc:
        logger.warning("Could not list %s to prune checkouts: %s", target.parent, exc)
        return
    for sibling in siblings:
        if str(sibling) in keep or not _SNAPSHOT_NAME_RE.match(sibling.name):
            continue
        if not (sibling / CHECKED_OUT_MARKER).is_file():
            continue
        _schedule_removal(sibling)


def _schedule_removal(snapshot: Path) -> None:
    """Hand a superseded checkout to the running server's ``CleanupManager``,
    which deletes it only once no query holds it (refcount, reader lease,
    minimum retention age). Without one (CLI, tests, before startup) the
    checkout is left for the startup snapshot sweep. Never raises."""
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    lifecycle = getattr(_running_server_app_state(), "global_lifecycle_manager", None)
    cleanup_manager = getattr(lifecycle, "cleanup_manager", None)
    if cleanup_manager is None:
        logger.debug("No cleanup manager to remove superseded checkout %s", snapshot)
        return
    try:
        cleanup_manager.schedule_cleanup(str(snapshot))
        logger.info(
            "Scheduled superseded checked-out snapshot %s for removal", snapshot
        )
    except Exception as exc:
        logger.warning("Could not schedule removal of checkout %s: %s", snapshot, exc)
