"""AliasLockCoordinator: drop-in WriteLockManager replacement dispatching
to the DB-backed alias lock (Issue #1546 Phase 2).

Installed as ``RefreshScheduler.write_lock_manager`` in place of a bare
``WriteLockManager`` instance (wiring: refresh_scheduler.py's __init__).
Every real call site in this codebase already reaches the lock
exclusively through one of:

    - ``scheduler.acquire_write_lock()`` / ``release_write_lock()`` /
      ``is_write_locked()`` (the RefreshScheduler facade methods, which
      delegate 1:1 to ``self.write_lock_manager.acquire/release/is_locked``)
    - ``scheduler.write_lock_manager.acquire/release/renew`` directly (the
      two documented TTL-bypass sites in refresh_scheduler.py,
      fleet_migration/orchestrator.py's 24h-TTL acquire, and
      temporal_legacy_migration/locking.py's acquire + heartbeat renew)

so installing ONE coordinator instance here rewires all of them at once,
with zero changes required at any of those call sites.

Synchronization: TWO separate locks, deliberately different scopes.
``self._alias_locks`` is a PER-ALIAS ``threading.Lock`` dict (mirroring
``WriteLockManager``'s own ``_intra_process_guards`` pattern) -- the lock
for a given alias is held across that alias's ENTIRE ``acquire()`` call
(cross-mechanism conflict check, the actual ``store.try_acquire()``/
``file_manager.acquire()`` call, and the handle-dict insert), so two
concurrent ``acquire()`` calls for the SAME alias FROM THIS SAME PROCESS
-- even straddling an operator's flag flip -- are fully serialized
against each other and cannot both succeed under different mechanisms.
Concurrent ``acquire()`` calls for DIFFERENT aliases use different lock
objects and never contend with each other -- critically, this means
holding a per-alias lock across a potentially multi-second store
contention wait (or an hours-long real hold) never blocks unrelated
aliases, unlike a single process-wide lock would (an earlier design used
one, and its own concurrency test hung under contention during
development -- see git history). ``self._handles_lock`` is a separate,
always-brief, DICT-ONLY lock used by ``release()``/``renew()``/
``is_locked()``/``get_lock_info()`` for their own dict lookup, and
briefly inside ``acquire()``'s per-alias critical section for the actual
dict read/insert -- it is never held across a call into the store or
file manager.

IMPORTANT -- this ``threading.Lock`` is process-local. It provides NO
ordering guarantee whatsoever between two DIFFERENT processes (the
normal case in a multi-node cluster, or even two workers on one node).
Do not read the paragraph above as a cross-process guarantee: it isn't
one. See "Cross-mechanism conflict detection" below for what actually
protects against a different process using the OTHER mechanism, and its
honestly-stated residual.

Cross-mechanism conflict detection (Codex Fix 2 review): a genuinely
ATOMIC guarantee that a file lock and a DB lock for the same alias can
never both be held simultaneously ACROSS PROCESSES is not achievable
here -- the two mechanisms are independent stores (a filesystem lock
file, created via ``WriteLockManager``'s own atomic ``O_CREAT|O_EXCL``,
and a database row/transaction) with no third, shared arbiter a
check-then-act sequence across them could be made atomic against. Two
best-effort mitigations narrow, but do not close, that window:

    - Flag OFF: ``acquire()`` consults the store's authoritative
      ``is_held(alias)`` (whenever a ``store_resolver`` is configured at
      all, independent of the live flag) BEFORE granting the file lock --
      closing the specific gap where the process-local ``_handles`` dict
      cannot see a DB lock held by a DIFFERENT process.
    - Flag ON: ``_acquire_db_backed()`` re-checks
      ``file_manager.is_locked()`` immediately AFTER its DB insert
      completes and rolls the DB lock back if a file lock is now present
      -- narrowing (to roughly one extra store round-trip / filesystem
      stat) the window in which a concurrent file-mode acquire from
      another process could land undetected between the PRE-check and
      the DB insert.

Both are check-then-act, not compare-and-swap: a second process's
acquire under the OTHER mechanism landing in the remaining, now much
narrower window between a check and the corresponding act is still
possible in principle and is NOT eliminated by this code. This residual
is accepted, not ignored, for two reasons: (1) the DB-backed rollout is
gated operationally -- ``AliasLockConfig.db_backed_enabled`` is meant to
be flipped fleet-wide only after confirming every node runs this code
(mirrors Story #1460's rollout gate), so the window where two DIFFERENT
mechanisms could genuinely be attempted concurrently for the SAME alias
is itself bounded to the flip transition, not steady-state operation;
(2) within EITHER mechanism alone, the underlying primitive (the DB
transaction's atomic INSERT, or the file lock's atomic O_CREAT|O_EXCL)
still fully serializes every caller of THAT mechanism against each
other, on every node -- what remains open is specifically two callers of
DIFFERENT mechanisms colliding within the narrowed window, not a general
locking failure.

Dispatch rule for LIFECYCLE calls: the rollout flag is consulted ONLY at
``acquire()`` time -- the one place a NEW lock is created. Which
mechanism a later ``release()``/``renew()`` call for a given alias uses is
determined by HOW that alias's lock was acquired -- tracked in
``_handles``, popped on release -- never by re-reading the live flag.

Dispatch rule for READ-ONLY probes (``is_locked()``/``get_lock_info()``):
these check BOTH sources whenever both are POSSIBLY valid (the file
manager always; the DB store whenever a ``store_resolver`` was configured
at all, independent of the live flag) and report "held" if EITHER says
so -- a read-only probe must never report "not held" for a lock that is
genuinely held under the OTHER mechanism just because the flag changed.

``ttl_seconds`` is accepted (for API/call-site compatibility with every
existing caller) but is a pure no-op in DB-backed mode: the accepted
design is a session-held transaction, not a row with a TTL (see
``alias_lock_store/base.py``'s module docstring) -- there is deliberately
no TTL, no reaper, no local-clock-vs-remote-mtime comparison anywhere
(Issue #1546 AC6).

Typing note: ``AliasLockStore``/``AliasLockHandle`` are imported only
under ``TYPE_CHECKING`` (never at runtime) so this module -- which
``RefreshScheduler`` loads unconditionally, including in pure-CLI/solo
processes -- never eagerly pulls in the server-only ``alias_lock_store``
package (Bug #1468 lazy-import discipline). The one runtime import of
``AliasLockOwnershipLostError`` in ``_release_db_backed()`` is local for
the same reason.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, DefaultDict, Dict, Optional

from .write_lock_manager import DEFAULT_LOCK_TTL_SECONDS, WriteLockManager

if TYPE_CHECKING:
    from code_indexer.server.services.alias_lock_store.base import (
        AliasLockHandle,
        AliasLockStore,
    )

logger = logging.getLogger(__name__)


class AliasLockCoordinator:
    """Facade preserving WriteLockManager's bool-based public API
    (acquire/release/renew/is_locked/get_lock_info -- the same five
    methods WriteLockManager itself exposes, since this is a drop-in
    replacement for it), per-alias dispatching between the legacy
    file-based lock and a DB-backed AliasLockStore.
    """

    def __init__(
        self,
        golden_repos_dir: Path,
        *,
        db_backed_enabled_getter: Optional[Callable[[], bool]] = None,
        store_resolver: Optional[Callable[[], "AliasLockStore"]] = None,
    ) -> None:
        """
        Args:
            golden_repos_dir: Forwarded unchanged to the wrapped
                WriteLockManager (always constructed -- it is the default
                and the fallback for any alias not tracked as DB-acquired).
            db_backed_enabled_getter: Callable returning the CURRENT value
                of the operator-controlled rollout flag
                (AliasLockConfig.db_backed_enabled). `None` (the default)
                behaves as an always-False getter -- pure file-based mode,
                byte-identical to a bare WriteLockManager. Consulted only
                inside acquire().
            store_resolver: Callable returning the AliasLockStore to use
                for a DB-backed acquire, and for the read-only is_locked()/
                get_lock_info() probes (see server/services/alias_lock_store/
                factory.py for the production resolver, which dispatches
                PostgreSQL/SQLite via is_postgres_storage_mode()). `None`
                means no DB store is wired at all.
        """
        self._file_manager = WriteLockManager(golden_repos_dir=golden_repos_dir)
        self._db_backed_enabled_getter = db_backed_enabled_getter or (lambda: False)
        self._store_resolver = store_resolver
        self._handles: Dict[str, "AliasLockHandle"] = {}
        self._handles_lock = threading.Lock()

        # Per-alias locks (same pattern as WriteLockManager's own
        # `_intra_process_guards`): serialize the cross-mechanism-check
        # -then-dispatch sequence in acquire() for a GIVEN alias, without
        # serializing unrelated aliases against each other -- see
        # acquire() for why this is held across that whole per-alias
        # critical section while `_handles_lock` above stays a brief,
        # dict-only lock used by release()/renew()/is_locked()/
        # get_lock_info().
        self._alias_locks: DefaultDict[str, threading.Lock] = defaultdict(
            threading.Lock
        )
        self._alias_locks_guard = threading.Lock()

    def _db_backed(self) -> bool:
        return bool(self._db_backed_enabled_getter())

    def _get_alias_lock(self, alias: str) -> threading.Lock:
        with self._alias_locks_guard:
            return self._alias_locks[alias]

    def acquire(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Non-blocking acquire. See WriteLockManager.acquire() for the
        file-mode contract this preserves exactly. In DB-backed mode,
        `ttl_seconds` is accepted but ignored (see module docstring).

        Held under this alias's OWN per-process lock (never the global
        `_handles_lock`) for the ENTIRE cross-mechanism-check-then
        -dispatch sequence: two concurrent acquire() calls for the SAME
        alias -- even straddling an operator's flag flip -- are fully
        serialized against EACH OTHER, so at most one can ever proceed
        past the cross-mechanism check before the other observes its
        result. Concurrent acquire() calls for DIFFERENT aliases use
        DIFFERENT lock objects and never contend with each other.
        """
        alias_lock = self._get_alias_lock(alias)
        with alias_lock:
            if self._db_backed():
                if self._file_manager.is_locked(alias):
                    return False
                return self._acquire_db_backed(alias, owner_name, owner_token)

            with self._handles_lock:
                already_db_tracked = alias in self._handles
            if already_db_tracked:
                return False
            # Codex Fix 2: the flag-OFF path used to check ONLY the
            # process-local `_handles` dict above -- which can NEVER see
            # another PROCESS's DB-acquired lock (that dict is per
            # coordinator INSTANCE). Whenever a store_resolver is
            # configured at all (independent of the live flag, mirroring
            # is_locked()/get_lock_info()'s existing "check both sources"
            # rule), consult the store's own authoritative is_held()
            # before granting the file lock -- see the module docstring's
            # "Cross-mechanism conflict detection" section.
            if self._store_resolver is not None and self._store_resolver().is_held(
                alias
            ):
                return False
            return self._file_manager.acquire(
                alias,
                owner_name=owner_name,
                ttl_seconds=ttl_seconds,
                owner_token=owner_token,
            )

    def _acquire_db_backed(
        self, alias: str, owner_name: str, owner_token: Optional[str]
    ) -> bool:
        """Called from acquire() while this alias's per-alias lock is
        held. `store.try_acquire()` can legitimately block up to the
        store's configured contention timeout (or, on success, hold a
        transaction for hours) -- that block only delays OTHER acquire()
        attempts for this SAME alias (already serialized above), never
        attempts for unrelated aliases.

        Codex Fix 2: re-checks the file lock immediately AFTER the DB
        insert and rolls back on conflict -- see the module docstring's
        "Cross-mechanism conflict detection" section for the full
        rationale and the honestly-documented residual window.
        """
        assert self._store_resolver is not None, (
            "AliasLockCoordinator: db_backed_enabled_getter() returned True "
            "but no store_resolver was configured -- wiring bug"
        )
        store = self._store_resolver()
        token = owner_token or str(uuid.uuid4())
        handle = store.try_acquire(alias, operation=owner_name, owner_token=token)
        if handle is None:
            return False
        with self._handles_lock:
            self._handles[alias] = handle

        if self._file_manager.is_locked(alias):
            with self._handles_lock:
                self._handles.pop(alias, None)
            rollback_ok = self._release_db_backed(alias, owner_name, handle)
            logger.warning(
                f"AliasLockCoordinator._acquire_db_backed: detected "
                f"cross-mechanism conflict for alias={alias!r} (a file "
                f"lock appeared during the DB acquire) -- rolled back the "
                f"just-acquired DB lock (rollback_ok={rollback_ok})"
            )
            return False

        return True

    def release(
        self,
        alias: str,
        owner_name: str = "refresh_scheduler",
        owner_token: Optional[str] = None,
    ) -> bool:
        """Release the lock for `alias`. If this alias was acquired via a
        tracked DB-backed handle, releases through that handle's own
        store (exact-token DELETE, per base.py); a zero-rows-affected
        ownership-loss is logged and reported as False (matching
        WriteLockManager.release()'s own owner-mismatch False contract),
        never raised -- release() is a terminal call, not a checkpoint.
        Otherwise (never DB-acquired, or already released) falls through
        to the file manager's own idempotent contract."""
        with self._handles_lock:
            handle = self._handles.pop(alias, None)

        if handle is not None:
            return self._release_db_backed(alias, owner_name, handle)

        return self._file_manager.release(
            alias, owner_name=owner_name, owner_token=owner_token
        )

    def _release_db_backed(
        self, alias: str, owner_name: str, handle: "AliasLockHandle"
    ) -> bool:
        from code_indexer.server.services.alias_lock_store.base import (
            AliasLockOwnershipLostError,
        )

        try:
            handle._store.release(handle)
            return True
        except AliasLockOwnershipLostError:
            logger.warning(
                f"AliasLockCoordinator.release: ownership already lost for "
                f"alias={alias!r} owner={owner_name!r}"
            )
            return False

    def renew(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Diagnostic-only heartbeat/ownership-loss checkpoint.

        DB-backed mode (tracked handle present): delegates to the
        handle's store.renew(), which RAISES AliasLockOwnershipLostError
        on loss (AC5) -- deliberately NOT translated to a bool here, so
        it propagates unwrapped to callers such as
        temporal_legacy_migration/locking.py's heartbeat thread.

        File mode (no tracked handle): delegates to WriteLockManager's
        own bool-returning renew() unchanged.
        """
        with self._handles_lock:
            handle = self._handles.get(alias)

        if handle is not None:
            handle._store.renew(handle)
            return True

        return self._file_manager.renew(
            alias,
            owner_name=owner_name,
            ttl_seconds=ttl_seconds,
            owner_token=owner_token,
        )

    def is_locked(self, alias: str) -> bool:
        """True if `alias` is currently held by ANYONE, under EITHER
        mechanism -- this coordinator's own tracked DB handle, another
        DB-backed holder (checked whenever a store is wired at all, not
        gated by the live flag, so a flag flip after acquisition never
        produces a false negative), or a file-based holder."""
        with self._handles_lock:
            if alias in self._handles:
                return True

        if self._file_manager.is_locked(alias):
            return True

        if self._store_resolver is not None:
            return bool(self._store_resolver().is_held(alias))

        return False

    def get_lock_info(self, alias: str) -> Optional[Dict[str, object]]:
        """Best-effort metadata for observability (mcp/handlers/files.py's
        write-mode "who holds this lock" display). Checks the same two
        sources as is_locked(), in the same order, for the same
        false-negative-avoidance reason. DB-backed mode cannot observe
        another holder's identity (the row is invisible while its
        transaction is open) -- returns a minimal, non-empty dict when
        held so `info.get("owner", "unknown")`-style callers still get a
        truthy result, distinguishable from "not held" (None)."""
        with self._handles_lock:
            handle = self._handles.get(alias)
        if handle is not None:
            return {
                "owner": handle.operation,
                "owner_token": handle.owner_token,
                "db_backed": True,
            }

        file_info = self._file_manager.get_lock_info(alias)
        if file_info is not None:
            return file_info

        if self._store_resolver is not None and self._store_resolver().is_held(alias):
            return {"owner": "unknown", "db_backed": True}

        return None
