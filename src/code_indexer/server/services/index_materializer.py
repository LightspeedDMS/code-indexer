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
  it instead of trusting a partial tree. An index root holding only
  ``REBUILDABLE_LEFTOVERS`` (an evicted checkout the backfill sweeps wrote
  into again) is checked out over, the leftovers kept; any other unexplained
  index root is refused.
* A checked-out snapshot carries ``CHECKED_OUT_MARKER``. After a checkout,
  this pod's OTHER checked-out snapshots of the same repository that the alias
  no longer names (target or previous) are scheduled for removal -- only
  marked ones, so a snapshot this pod built itself is left to the snapshot
  retention it always had. Removal goes through the server's
  ``CleanupManager`` (query refcount, reader lease, minimum age), never a
  direct delete: a query still reading an old checkout keeps its files.
* Disk: with a served-index cache installed (``served_index_cache``, delegated
  only) every serve is recorded for its LRU order, room is made before a
  checkout, and a checkout larger than the cache may hold is refused
  (``ServedIndexTooLargeError``). A fast-path hit on a target being evicted
  takes the lock and looks again.
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
    CHECKED_OUT_MARKER,
    IN_PROGRESS_MARKER,
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
    "exclusive_checkout",
    "try_exclusive_checkout",
    "served_cache",
    "still_served",
    "ensure_published_index",
    "publish_index",
    "is_served_here",
    "is_base_clone",
    "clear_checked_out_marker",
    "holds_only_rebuildable_leftovers",
    "published_index_fields",
    "recorded_index",
    "run_fix_config",
    "REBUILDABLE_LEFTOVERS",
]

logger = logging.getLogger(__name__)

# IN_PROGRESS_MARKER (present while a checkout into a target runs, or
# crashed) and CHECKED_OUT_MARKER (present in a target this module checked
# out; holds the version) live in utils.index_store, which listings read.

#: issue-64616: what other writers leave in an index root that holds NO index.
#: The served-index cache evicts a checkout by renaming its whole index root
#: away; the index backfill sweeps then re-create the root for their cooldown
#: markers (``IndexBackfillPolicy.attempt_marker``), a SCIP build writes its
#: output there and a refresh that could not index writes its language
#: mappings. None of it is an index or a configuration and all of it is
#: rebuilt on demand, so a root holding only these names is checked out over
#: (they are kept: see ``_checkout``) instead of being refused as unexplained
#: state forever.
REBUILDABLE_LEFTOVERS = frozenset(
    {
        ".scip_reconcile_attempt",
        ".fts_backfill_attempt",
        ".semantic_backfill_attempt",
        "scip",
        "language-mappings.yaml",
    }
)
#: Beside the index root during a checkout over leftovers: the leftovers.
LEFTOVERS_DIRNAME = ".cidx-index-leftovers"

_SNAPSHOT_NAME_RE = re.compile(r"^v_\d+$")
_VERSIONED_DIRNAME = ".versioned"

FixConfig = Callable[[Path], None]

_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class PublishedIndexUnavailableError(RuntimeError):
    """The published index for a target cannot be served on this pod."""


def _index_dir(store: IndexStore, target: Path) -> Path:
    return store.index_root(target) / "index"


def _ready_dir(store: IndexStore, target: Path, require_index: bool) -> Path:
    """What must exist for the target's state to be complete: the index
    directory, or (``require_index=False``: state with no index of its own,
    like a composite's proxy config) the index root itself."""
    root = store.index_root(target)
    return root / "index" if require_index else root


def is_served_here(
    store: IndexStore, target: Union[str, Path], require_index: bool = True
) -> bool:
    """True when this pod holds a complete index at ``target``."""
    path = Path(target)
    return (
        _ready_dir(store, path, require_index).is_dir()
        and not (path / IN_PROGRESS_MARKER).exists()
    )


def is_base_clone(path: Union[str, Path], golden_repos_dir: Union[str, Path]) -> bool:
    """True when ``path`` is a golden repository's base clone: a directory
    directly under ``golden_repos_dir`` (``<golden_repos_dir>/<alias>``), not
    a ``.versioned`` snapshot nor cidx's own dot-directories.

    issue-64616: a base clone is not a cache entry. It is the refresh's
    incremental base and, while the golden-repos mount is on, one directory
    shared by every pod: the served-index cache never evicts it, a publish
    in place never marks it ``CHECKED_OUT_MARKER``, and the registry
    reconcile clears a marker found on it."""
    candidate = Path(os.path.normpath(str(path)))
    root = Path(os.path.normpath(str(golden_repos_dir)))
    return candidate.parent == root and not candidate.name.startswith(".")


def clear_checked_out_marker(target: Union[str, Path]) -> bool:
    """Remove ``CHECKED_OUT_MARKER`` from ``target``; whether one was there.
    Idempotent."""
    marker = Path(target) / CHECKED_OUT_MARKER
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def holds_only_rebuildable_leftovers(root: Union[str, Path]) -> bool:
    """True when the index root ``root`` is a directory holding nothing but
    ``REBUILDABLE_LEFTOVERS`` (possibly nothing at all): no index, no
    configuration. An unreadable root is not (it is refused, as before)."""
    path = Path(root)
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        names = {entry.name for entry in path.iterdir()}
    except OSError:
        return False
    return names <= REBUILDABLE_LEFTOVERS


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


def _thread_lock(target: Path) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(str(target), threading.Lock())


def checkout_lock_path(target: Path) -> Path:
    """Beside a checkout target: the lock its checkouts hold EXCLUSIVE."""
    return target.parent / f".{target.name}.checkout.lock"


def flock_live(lock_path: Path, operation: int) -> int:
    """Open *lock_path* (created when absent) and ``flock`` it; the held
    descriptor. Lock files are deleted once their target is gone (the
    checkout sweep, under both locks -- issue-64616), so a waiter can be granted
    a lock on a file that no longer is the path: it then re-opens the path and
    locks again, so two holders never lock different files. A non-blocking
    *operation* that would wait raises ``BlockingIOError``; the parent missing
    raises ``FileNotFoundError``."""
    while True:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, operation)
        except BaseException:
            os.close(fd)
            raise
        try:
            held, current = os.fstat(fd), os.stat(lock_path)
            if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                return fd
        except FileNotFoundError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _lock_fd(target: Path, operation: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    return flock_live(checkout_lock_path(target), operation)


@contextmanager
def _exclusive(target: Path) -> Iterator[None]:
    """Serialize checkouts of ``target`` across threads and processes."""
    with _thread_lock(target):
        fd = _lock_fd(target, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


exclusive_checkout = _exclusive


@contextmanager
def try_exclusive_checkout(target: Union[str, Path]) -> Iterator[bool]:
    """``exclusive_checkout`` without waiting: yields False (nothing held)
    when another thread or process holds the target's lock. For work that
    must never block a checkout (served-index eviction)."""
    path = Path(target)
    lock = _thread_lock(path)
    if not lock.acquire(blocking=False):
        yield False
        return
    try:
        try:
            fd = _lock_fd(path, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        lock.release()


def served_cache() -> Any:
    """This pod's served-index cache (``served_index_cache``); None under the
    local store, the CLI and tests that install none."""
    from code_indexer.server.services.served_index_cache import (
        active_served_index_cache,
    )

    return active_served_index_cache()


def still_served(cache: Any, target: Path) -> bool:
    """A fast-path hit: True unless the target is being evicted right now (the
    caller then takes the checkout lock and looks again)."""
    return cache is None or bool(cache.note_hit(target))


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
    No-op under a local store or when the pod already serves the recorded
    version: the pod holds an index there and either its
    ``CHECKED_OUT_MARKER`` names that version, or it carries no marker (an
    index built here before markers existed). A target is re-published in
    place only by a repair (HNSW orphan sweep), so a marker naming another
    version means this pod holds the pre-repair index: it is swapped for the
    recorded one. ``fix_config`` defaults to ``run_fix_config``.
    """
    if store.is_local:
        return
    target = Path(target_path)
    cache = served_cache()
    if _serves_recorded(store, target, document) and still_served(cache, target):
        return
    store_alias, version = _recorded(document, target)
    fix = fix_config or run_fix_config
    with _exclusive(target):
        if _serves_recorded(store, target, document):
            still_served(cache, target)
            return
        if cache is not None:
            cache.admit(target, version)
        if is_served_here(store, target):
            _replace(store, target, store_alias, version, fix)
        else:
            _checkout(store, target, store_alias, version, fix)
        if cache is not None:
            cache.admitted(target, version)
    _prune_superseded(target, document)


def _checkout(
    store: IndexStore,
    target: Path,
    store_alias: str,
    version: str,
    fix_config: FixConfig,
    require_index: bool = True,
) -> None:
    root = store.index_root(target)
    marker = target / IN_PROGRESS_MARKER
    checkout_as = getattr(store, "checkout_as", None)
    if checkout_as is None:
        raise PublishedIndexUnavailableError(
            f"IndexStore {type(store).__name__} cannot check out by store alias"
        )
    try:
        target.mkdir(parents=True)
        created = True
    except FileExistsError:
        created = False
    leftovers = _clear_index_root(root, marker)
    try:
        marker.write_text(version)
        checkout_as(store_alias, target, version)
        fix_config(target)
        if not _ready_dir(store, target, require_index).is_dir():
            raise RuntimeError(f"version {version} holds no index directory")
        _restore_leftovers(leftovers, root)
        (target / CHECKED_OUT_MARKER).write_text(version)
        marker.unlink()
    except Exception as exc:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
            _put_back_leftovers(leftovers, root, marker)
        raise PublishedIndexUnavailableError(
            f"Could not check out {store_alias} version {version} to {target}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    logger.info("Checked out %s version %s to %s", store_alias, version, target)


def _clear_index_root(root: Path, marker: Path) -> Optional[Path]:
    """Make way for a checkout into ``root``. Nothing there: nothing to do.
    A crashed checkout's (``marker``): removed. Only rebuildable leftovers:
    moved aside (returned, for ``_restore_leftovers``). Anything else is
    state this module cannot account for and is refused, never replaced."""
    if not root.exists():
        return None
    if marker.exists():
        shutil.rmtree(root)
        return None
    if not holds_only_rebuildable_leftovers(root):
        raise PublishedIndexUnavailableError(
            f"{root} exists without an index and was not left by a checkout; "
            "refusing to replace it"
        )
    aside = root.parent / LEFTOVERS_DIRNAME
    shutil.rmtree(aside, ignore_errors=True)
    os.rename(root, aside)
    logger.info(
        "%s held no index, only rebuildable leftovers; checking out over them",
        root,
    )
    return aside


def _restore_leftovers(aside: Optional[Path], root: Path) -> None:
    """Put back each leftover the checked-out version does not carry itself
    (a backfill's cooldown marker, a SCIP index), so checking out over them
    re-runs nothing; then drop the rest."""
    if aside is None:
        return
    for entry in aside.iterdir():
        if not (root / entry.name).exists():
            os.rename(entry, root / entry.name)
    shutil.rmtree(aside, ignore_errors=True)


def _put_back_leftovers(aside: Optional[Path], root: Path, marker: Path) -> None:
    """After a failed checkout over leftovers: the target as it was before it
    (the leftovers, no in-progress marker). Best effort -- the checkout's own
    failure is what the caller raises; leftovers are rebuildable."""
    if aside is None:
        return
    try:
        os.rename(aside, root)
        marker.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not put the leftovers back at %s: %s", root, exc)


def _checked_out_version(target: Path) -> Optional[str]:
    try:
        return (target / CHECKED_OUT_MARKER).read_text()
    except FileNotFoundError:
        return None


def _is_current(
    store: IndexStore, target: Path, version: str, require_index: bool = True
) -> bool:
    return (
        is_served_here(store, target, require_index)
        and _checked_out_version(target) == version
    )


def ensure_current_index(
    target_path: Union[str, Path],
    document: Optional[Mapping[str, Any]],
    *,
    store: IndexStore,
    fix_config: Optional[FixConfig] = None,
    require_index: bool = True,
) -> None:
    """``ensure_published_index`` for a target whose index CHANGES in place.

    An activated copy keeps one path while its index is re-published (every
    reindex, on whichever pod ran it). The pod serves the target only when it
    holds the version ``document`` records -- its ``CHECKED_OUT_MARKER`` says
    which one it holds; an older one is replaced (checked out beside it, then
    swapped in by rename, so a reader never sees half of either). A document
    recording nothing (an activation from before publishing existed) is
    served from what this pod holds, or fails loud. ``require_index=False``
    for state with no index directory of its own (a composite's proxy config).
    """
    if store.is_local:
        return
    target = Path(target_path)
    recorded = _fields(document)
    if recorded is None:
        if is_served_here(store, target, require_index):
            return
        _recorded(document, target)  # raises: nothing here, nothing published
        return
    store_alias, version = recorded
    cache = served_cache()
    if _is_current(store, target, version, require_index) and still_served(
        cache, target
    ):
        return
    with _exclusive(target):
        if _is_current(store, target, version, require_index):
            still_served(cache, target)
            return
        fix = fix_config or run_fix_config
        if cache is not None:
            cache.admit(target, version)
        if is_served_here(store, target, require_index):
            _replace(store, target, store_alias, version, fix, require_index)
        else:
            _checkout(store, target, store_alias, version, fix, require_index)
        if cache is not None:
            cache.admitted(target, version)


def _fields(document: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    try:
        return _recorded(document, Path("."))
    except PublishedIndexUnavailableError:
        return None


def recorded_index(document: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    """``(index_store_alias, index_version)`` an alias or registry document
    records, or None when it records no published index."""
    return _fields(document)


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
    require_index: bool = True,
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
        if not _ready_dir(store, target, require_index).is_dir():
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
    newest = _snapshot_ts(target.name)
    try:
        siblings = list(target.parent.iterdir())
    except OSError as exc:
        logger.warning("Could not list %s to prune checkouts: %s", target.parent, exc)
        return
    for sibling in siblings:
        if str(sibling) in keep or not _SNAPSHOT_NAME_RE.match(sibling.name):
            continue
        if _snapshot_ts(sibling.name) > newest:
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


def _serves_recorded(
    store: IndexStore, target: Path, document: Optional[Mapping[str, Any]]
) -> bool:
    """This pod holds ``target``'s index and nothing says it is not the
    version ``document`` records."""
    if not is_served_here(store, target):
        return False
    held = _checked_out_version(target)
    recorded = _fields(document)
    return held is None or recorded is None or held == recorded[1]


def _snapshot_ts(name: str) -> int:
    """The ``<ts>`` of a ``v_<ts>`` snapshot name."""
    return int(name.split("_", 1)[1])


def mark_published_here(target: Union[str, Path], fields: Mapping[str, Any]) -> None:
    """Record that this pod holds, at ``target``, exactly the version it just
    published (``fields``: ``published_index_fields``). Every publisher marks
    its own copy, so a later in-place republish (an HNSW repair) is told apart
    from it, as a checkout is."""
    _, version = _recorded(fields, Path(target))
    (Path(target) / CHECKED_OUT_MARKER).write_text(version)
