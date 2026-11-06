"""Remove this pod's leftover IndexStore checkouts (``index_store_backend: delegated``).

A pod that served a copy checks its index out locally
(``index_materializer``). When the copy goes away on ANOTHER pod -- an
activated copy deactivated, a golden repository removed -- nothing tells this
pod, so the checkout would stay forever. This sweep runs on EVERY pod (the
state is pod-local, so it is not leader-gated) and removes:

* ``activated-repos/<user>/<alias>``: index state of a copy the registry no
  longer has for that user (or whose entry names another IndexStore alias) --
  a composite's members included;
* ``golden-repos/.versioned/<repo>/v_*`` and ``golden-repos/<repo>``: index
  state of a golden repository that no alias pointer names any more.

Safety:

* Only targets carrying ``CHECKED_OUT_MARKER`` are considered: the marker is
  written by a checkout and by a publish, never by anything else, so a
  directory this pod built without publishing (the local store, a
  pre-IndexStore build) is never touched. That covers the publisher too: if
  the copy was deactivated on another pod, the publisher's state is just as
  stale, and it carries the marker its publish wrote.
* Only pod STATE is removed (the index root, the markers, the incoming /
  retired swap dirs); the directory itself goes only once empty. Repository
  content never lives on a delegated pod, and anything unexpected is kept.
* Grace period: a target whose marker changed within ``grace_seconds`` is
  kept (an activation publishes before it registers; a re-activation may be
  in flight).
* Leases: the target's checkout lock (``exclusive_checkout``) is held, so a
  concurrent checkout of the same target cannot interleave; and the target is
  marked quiescing in the query tracker and skipped while any query on this
  pod holds it.
* Fail loud: a registry or alias-pointer read failure skips that part of the
  pass with an ERROR -- "could not read the registry" is never "nothing is
  registered" (that would delete everything).
* Local store: nothing runs.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from code_indexer.server.services.index_materializer import (
    CHECKED_OUT_MARKER,
    checkout_lock_path,
    exclusive_checkout,
    try_exclusive_checkout,
)
from code_indexer.server.services.served_index_cache import (
    POD_STATE_DIRS,
    POD_STATE_FILES,
    composite_members,
    is_pinned,
    iter_activated_checkouts,
    iter_golden_checkouts,
    list_subdirs,
    purge,
    readers_free,
    readers_lock_path,
    remove_dir_if_empty,
    require_positive_interval,
    retire_pod_state,
)
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    IndexStore,
    activated_index_store_alias,
)

logger = logging.getLogger(__name__)

#: What a checkout leaves in a target: removed by the sweep, nothing else is.
POD_STATE_NAMES = (INDEX_DIRNAME,) + POD_STATE_FILES + POD_STATE_DIRS

DEFAULT_GRACE_SECONDS = 15 * 60
DEFAULT_INTERVAL_SECONDS = 15 * 60

_ALIASES = "aliases"
_VERSIONED = ".versioned"
_CHECKOUT_LOCK_SUFFIX = ".checkout.lock"


@dataclass
class CheckoutSweepReport:
    """What one pass did. ``errors`` names the parts skipped (fail loud)."""

    removed: List[str] = field(default_factory=list)
    kept_in_use: List[str] = field(default_factory=list)
    kept_recent: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _server_store() -> IndexStore:
    from code_indexer.server.startup.index_store_wiring import server_index_store

    return server_index_store()


class CheckoutSweeper:
    """One pod's sweep of leftover IndexStore checkouts."""

    def __init__(
        self,
        *,
        activated_repo_manager: Any,
        activated_repos_dir: str,
        golden_repos_dir: Optional[str],
        query_tracker: Optional[Any],
        aliases_reader: Optional[
            Callable[[], Dict[str, Tuple[str, Optional[str]]]]
        ] = None,
        store_provider: Callable[[], IndexStore] = _server_store,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._arm = activated_repo_manager
        self._activated_root = Path(activated_repos_dir)
        self._golden_root = Path(golden_repos_dir) if golden_repos_dir else None
        self._tracker = query_tracker
        self._aliases_reader = aliases_reader or self._read_alias_pointers
        self._store_provider = store_provider
        self._grace = grace_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- one pass -----------------------------------------------------------

    def sweep_once(self) -> CheckoutSweepReport:
        report = CheckoutSweepReport()
        if self._store_provider().is_local:
            return report
        self._sweep_activated(report)
        if self._golden_root is not None:
            self._sweep_golden(report)
        if report.removed or report.errors:
            logger.info(
                "Checkout sweep: removed %d, in use %d, recent %d, errors %s",
                len(report.removed),
                len(report.kept_in_use),
                len(report.kept_recent),
                report.errors,
            )
        return report

    def _sweep_activated(self, report: CheckoutSweepReport) -> None:
        try:
            entries = self._arm.list_all_activated_repositories()
            registered: Dict[Tuple[str, str], Any] = {
                (str(e["username"]), str(e["user_alias"])): e for e in entries
            }
        except Exception as exc:
            logger.error(
                "Checkout sweep: the activated-repo registry could not be read "
                "(%s: %s); no activated checkout is removed this pass",
                type(exc).__name__,
                exc,
            )
            report.errors.append("activated: registry unreadable")
            return
        self._activated_residue(registered, report)
        for username, user_alias, target in self._activated_targets():
            entry = registered.get((username, user_alias))
            if entry is not None and not self._names_another_alias(
                entry, username, user_alias
            ):
                continue
            if self._has_metadata_file(username, user_alias):
                logger.warning(
                    "Checkout sweep: %s has a metadata file that the registry "
                    "could not parse; keeping its checkout",
                    target,
                )
                report.kept_recent.append(str(target))
                continue
            try:
                self._remove(target, report, members=True, parent_removable=True)
            except Exception as exc:
                logger.error(
                    "Checkout sweep: removal failed for %s (%s: %s)",
                    target,
                    type(exc).__name__,
                    exc,
                )
                report.errors.append(f"activated: removal failed for {target}")

    def _activated_residue(
        self, registered: Dict[Tuple[str, str], Any], report: CheckoutSweepReport
    ) -> None:
        """Lock files and emptied directories an earlier pass left behind
        (an rmdir that failed while the filesystem still held a file in it)
        for copies the registry no longer has."""
        for user_dir in list_subdirs(self._activated_root):
            if user_dir.name.startswith("."):
                continue  # reserved (trash, scratch): never a user's copies
            for target in _residue_targets(user_dir):
                if (user_dir.name, target.name) in registered:
                    continue
                self._clear_residue(target, report, parent_removable=True)

    def _clear_residue(
        self, target: Path, report: CheckoutSweepReport, *, parent_removable: bool
    ) -> None:
        """Remove *target*'s residue under both of its locks, never waiting:
        a checkout or a reader holding either keeps it for the next pass."""
        with try_exclusive_checkout(target) as held:
            if not held:
                return
            with readers_free([target]) as free:
                if not free:
                    return
                if _drop_residue(target, parent_removable=parent_removable):
                    report.removed.append(str(target))

    def _has_metadata_file(self, username: str, user_alias: str) -> bool:
        """True when a metadata file exists on disk for this copy, even if the
        registry could not parse it."""
        metadata = self._activated_root / username / f"{user_alias}_metadata.json"
        return metadata.is_file()

    @staticmethod
    def _names_another_alias(entry: Any, username: str, user_alias: str) -> bool:
        recorded = entry.get(INDEX_STORE_ALIAS_FIELD) if hasattr(entry, "get") else None
        return recorded is not None and recorded != activated_index_store_alias(
            username, user_alias
        )

    def _activated_targets(self) -> Iterable[Tuple[str, str, Path]]:
        return iter_activated_checkouts(self._activated_root)

    def _sweep_golden(self, report: CheckoutSweepReport) -> None:
        assert self._golden_root is not None
        try:
            pointers = self._aliases_reader()
        except Exception as exc:
            logger.error(
                "Checkout sweep: alias pointers could not be read (%s: %s); no "
                "golden checkout is removed this pass",
                type(exc).__name__,
                exc,
            )
            report.errors.append("golden: alias pointers unreadable")
            return
        named: Set[str] = set()
        repos: Set[str] = set()
        for alias, (pointed, previous) in pointers.items():
            repos.add(alias.removesuffix("-global"))
            named.add(str(Path(pointed)))
            if previous:
                named.add(str(Path(previous)))
        versioned = self._golden_root / _VERSIONED
        for namespace in list_subdirs(versioned):
            if namespace.name in repos:
                continue
            for target in _residue_targets(namespace):
                if str(target) not in named:
                    self._clear_residue(target, report, parent_removable=True)
        for repo, target in self._golden_targets():
            if repo in repos or str(target) in named:
                continue
            try:
                self._remove(
                    target,
                    report,
                    members=False,
                    parent_removable=target.parent != self._golden_root,
                )
            except Exception as exc:
                logger.error(
                    "Checkout sweep: removal failed for %s (%s: %s)",
                    target,
                    type(exc).__name__,
                    exc,
                )
                report.errors.append(f"golden: removal failed for {target}")

    def _golden_targets(self) -> Iterable[Tuple[str, Path]]:
        assert self._golden_root is not None
        return iter_golden_checkouts(self._golden_root)

    def _read_alias_pointers(self) -> Dict[str, Tuple[str, Optional[str]]]:
        from code_indexer.global_repos.alias_manager import AliasManager

        assert self._golden_root is not None
        return AliasManager(str(self._golden_root / _ALIASES)).list_pointers()

    # -- removal --------------------------------------------------------------

    def _remove(
        self,
        target: Path,
        report: CheckoutSweepReport,
        *,
        members: bool,
        parent_removable: bool,
    ) -> None:
        key = str(target)
        if self._recent(target):
            report.kept_recent.append(key)
            return
        paths = [target, *(composite_members(target) if members else [])]
        if any(is_pinned(p) for p in paths):
            # A build in progress in this process. (A pin also holds the
            # readers lock -- checked below, for every process -- but only
            # while a served-index cache is installed.)
            report.kept_in_use.append(key)
            return
        with exclusive_checkout(target):
            if not (target / CHECKED_OUT_MARKER).is_file() or self._recent(target):
                report.kept_recent.append(key)
                return
            # A read, or a pin (a build in progress), in ANY process holds a
            # readers lock SHARED.
            with readers_free(paths) as free:
                if not free:
                    report.kept_in_use.append(key)
                    return
                if not self._quiesce(key):
                    report.kept_in_use.append(key)
                    return
                try:
                    for member in paths[1:]:
                        _remove_pod_state(member)
                    _remove_pod_state(target)
                    # Still holding both locks, and no reader holds them. A
                    # composite's members (and their lock files) live inside
                    # the target: they go first, or it is never empty.
                    for member in paths[1:]:
                        _drop_residue(member, parent_removable=False)
                    _drop_residue(target, parent_removable=parent_removable)
                finally:
                    if self._tracker is not None:
                        self._tracker.clear_quiescing(key)
        report.removed.append(key)
        logger.info("Checkout sweep: removed leftover checkout %s", key)

    def _recent(self, target: Path) -> bool:
        try:
            changed = (target / CHECKED_OUT_MARKER).stat().st_mtime
        except FileNotFoundError:
            return False
        return self._clock() - changed < self._grace

    def _quiesce(self, key: str) -> bool:
        """Mark the target quiescing (no new query on this pod admits it) and
        check none holds it. False while one does (the mark cleared), or while
        another purge already holds the mark (left alone)."""
        if self._tracker is None:
            return True
        if not self._tracker.try_mark_quiescing(key):
            return False  # another purge holds the mark: never clear it
        if self._tracker.get_ref_count(key) > 0:
            self._tracker.clear_quiescing(key)
            return False
        return True

    # -- background loop ------------------------------------------------------

    def start(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        """Raises ``ValueError`` for a non-positive interval (it would spin)."""
        require_positive_interval("checkout sweep interval", interval_seconds)
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.sweep_once()
                except Exception:
                    logger.error("Checkout sweep pass failed", exc_info=True)

        self._thread = threading.Thread(
            target=loop, name="index-checkout-sweep", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _remove_pod_state(target: Path) -> None:
    """Remove a checkout's pod state from ``target`` -- renamed aside first,
    then deleted (``retire_pod_state``), so a reader never sees a half-deleted
    index that still looks served; the directory itself only once nothing
    else is left in it."""
    purge(retire_pod_state(target))
    remove_dir_if_empty(target)


def _residue_targets(directory: Path) -> List[Path]:
    """Targets in *directory* that left only residue: a checkout lock file
    beside them, and the target itself absent or an empty directory."""
    try:
        names = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.name.startswith(".") and entry.name.endswith(_CHECKOUT_LOCK_SUFFIX)
        )
    except OSError:
        return []
    targets: List[Path] = []
    for name in names:
        target = directory / name[1 : -len(_CHECKOUT_LOCK_SUFFIX)]
        if not target.exists() or (
            target.is_dir() and not target.is_symlink() and not any(target.iterdir())
        ):
            targets.append(target)
    return targets


def _drop_residue(target: Path, *, parent_removable: bool) -> bool:
    """With *target*'s checkout and readers locks held and no reader: remove
    the emptied target directory, then both lock files beside it, then an
    emptied parent (``<user>/``, ``.versioned/<repo>/``) when
    *parent_removable* (EVO-64616: they piled up with every removed copy).
    Lock waiters re-open the path (``flock_live``), so deleting a held lock
    file never lets two holders lock different files. False when the target
    still holds something (kept, with its locks)."""
    remove_dir_if_empty(target)
    if target.exists():
        return False
    for lock in (checkout_lock_path(target), readers_lock_path(target)):
        lock.unlink(missing_ok=True)
    if parent_removable:
        remove_dir_if_empty(target.parent)
    return True
