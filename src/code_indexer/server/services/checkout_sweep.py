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
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from code_indexer.server.services.index_materializer import (
    CHECKED_OUT_MARKER,
    IN_PROGRESS_MARKER,
    INCOMING_DIRNAME,
    RETIRED_DIRNAME,
    exclusive_checkout,
)
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    IndexStore,
    activated_index_store_alias,
)

logger = logging.getLogger(__name__)

#: What a checkout leaves in a target: removed by the sweep, nothing else is.
POD_STATE_NAMES = (
    INDEX_DIRNAME,
    CHECKED_OUT_MARKER,
    IN_PROGRESS_MARKER,
    INCOMING_DIRNAME,
    RETIRED_DIRNAME,
)

DEFAULT_GRACE_SECONDS = 15 * 60
DEFAULT_INTERVAL_SECONDS = 15 * 60

#: activated-repos entries that are not usernames (ActivatedRepoManager's).
_RESERVED_USER_DIRS = (".trash",)
_VERSIONED = ".versioned"
_ALIASES = "aliases"


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
                self._remove(target, report, members=True)
            except Exception as exc:
                logger.error(
                    "Checkout sweep: removal failed for %s (%s: %s)",
                    target,
                    type(exc).__name__,
                    exc,
                )
                report.errors.append(f"activated: removal failed for {target}")

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
        for user_dir in _subdirs(self._activated_root):
            if user_dir.name in _RESERVED_USER_DIRS:
                continue
            for target in _subdirs(user_dir):
                if (target / CHECKED_OUT_MARKER).is_file():
                    yield user_dir.name, target.name, target

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
        for repo, target in self._golden_targets():
            if repo in repos or str(target) in named:
                continue
            try:
                self._remove(target, report, members=False)
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
        for entry in _subdirs(self._golden_root):
            if entry.name == _VERSIONED:
                for namespace in _subdirs(entry):
                    for snapshot in _subdirs(namespace):
                        if (snapshot / CHECKED_OUT_MARKER).is_file():
                            yield namespace.name, snapshot
            elif entry.name != _ALIASES and (entry / CHECKED_OUT_MARKER).is_file():
                yield entry.name, entry

    def _read_alias_pointers(self) -> Dict[str, Tuple[str, Optional[str]]]:
        from code_indexer.global_repos.alias_manager import AliasManager

        assert self._golden_root is not None
        return AliasManager(str(self._golden_root / _ALIASES)).list_pointers()

    # -- removal --------------------------------------------------------------

    def _remove(
        self, target: Path, report: CheckoutSweepReport, *, members: bool
    ) -> None:
        key = str(target)
        if self._recent(target):
            report.kept_recent.append(key)
            return
        with exclusive_checkout(target):
            if not (target / CHECKED_OUT_MARKER).is_file() or self._recent(target):
                report.kept_recent.append(key)
                return
            if not self._quiesce(key):
                report.kept_in_use.append(key)
                return
            try:
                if members:
                    for member in _subdirs(target):
                        if (member / CHECKED_OUT_MARKER).is_file():
                            _remove_pod_state(member)
                _remove_pod_state(target)
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
        check none holds it. False (mark cleared) while one does."""
        if self._tracker is None:
            return True
        self._tracker.mark_quiescing(key)
        if self._tracker.get_ref_count(key) > 0:
            self._tracker.clear_quiescing(key)
            return False
        return True

    # -- background loop ------------------------------------------------------

    def start(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
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


def _subdirs(root: Path) -> List[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            logger.warning("Checkout sweep: cannot list %s (%s)", root, exc)
        return []


def _remove_pod_state(target: Path) -> None:
    """Remove a checkout's pod state from ``target``; the directory itself only
    once nothing else is left in it."""
    for name in POD_STATE_NAMES:
        path = target / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    try:
        os.rmdir(target)
    except OSError:
        pass  # something else lives there: kept
