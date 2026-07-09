"""
Research Assistant Workspace Cleanup Service (Bug #1085).

Automatically reclaims Research Assistant session workspaces under
``~/.cidx-server/research/<uuid>/`` that are NOT backed by a live
``research_sessions`` registry row (orphans) and have aged past a configurable
retention period.

Mirrors the SCIP ``WorkspaceCleanupService`` pattern:
- Periodic sweep + startup reconciliation (both call ``cleanup()``).
- Configurable retention (``research_session_retention_days``).
- Live-row protection: a dir mapping to a live session row is NEVER deleted.
- Recent-modification protection: skip dirs touched within a threshold so an
  in-flight ``create_session()`` is never raced.
- Anti-silent-failure (Messi #13): deletion failures are logged with the path
  and recorded, never swallowed or raised.
- Anti-unbounded-loop (Messi #14): the scan is capped at ``max_dirs_per_run``.

NEVER deletes a directory it cannot prove is an orphan (no live row).
"""

import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

logger = logging.getLogger(__name__)

# Default sweep cadence: hourly. Mirrors the data-retention cadence scale.
DEFAULT_RESEARCH_CLEANUP_INTERVAL_SECONDS = 3600

# Non-UUID session dir name that must ALWAYS be preserved (the default chat).
DEFAULT_SESSION_DIR_NAME = "default"


def _is_session_dir_name(name: str) -> bool:
    """True iff ``name`` is a valid Research Assistant session folder name.

    A session folder is either the literal ``"default"`` chat directory or a
    name that parses as a ``uuid.UUID`` (the session-id shape used by
    ``create_session()``). ANY other name (e.g. ``important-do-not-delete``)
    is NOT a session dir and must never be a deletion candidate (Bug #1085
    BLOCKING-2: blast radius = every aged immediate child of the research base).
    """
    if name == DEFAULT_SESSION_DIR_NAME:
        return False  # 'default' is a session dir, but NEVER reapable -> exclude
    try:
        uuid.UUID(name)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def make_backend_live_folder_provider(
    backend_supplier: Callable[[], Any],
) -> Callable[[], Set[str]]:
    """Build a live-folder provider backed by the ACTIVE research_sessions store.

    Bug #1085 BLOCKING-1: the live set MUST come from the same backend the
    writers use -- ``ResearchSessionsBackend`` from ``backend_registry`` -- which
    is PostgreSQL in cluster/``storage_mode: postgres`` and SQLite in solo. The
    previous SQLite-only provider read a table that is EMPTY in postgres mode and
    silently returned ``set()`` (no exception), so EVERY aged research dir was
    treated as an orphan and DELETED -- including live users' sessions.

    ``backend_supplier()`` resolves the active backend lazily on each sweep
    (the registry is only available after startup wiring). Two FAIL-SAFE rules:

    * If the supplier returns ``None`` (registry/backend not wired in a mode
      where sessions are expected) -> raise ``RuntimeError``.
    * If ``list_sessions()`` raises -> the exception PROPAGATES.

    Either way ``ResearchCleanupService.cleanup()`` catches the exception and
    aborts the sweep with ZERO deletions. An untrustworthy live set is ALWAYS
    treated as "delete nothing", never "everything is an orphan".
    """

    def _provider() -> Set[str]:
        backend = backend_supplier()
        if backend is None:
            raise RuntimeError(
                "research_sessions backend unavailable; refusing to compute a "
                "live set (fail-safe: no deletions)"
            )
        sessions = backend.list_sessions()
        return {
            str(s["folder_path"]) for s in sessions if s.get("folder_path") is not None
        }

    return _provider


def make_db_live_folder_provider(db_path: str) -> Callable[[], Set[str]]:
    """
    Build a provider that returns the set of live ``research_sessions``
    folder_path strings from the main server DB.

    Opens a short-lived **read-only** connection per call (``mode=ro`` URI) so
    it always observes committed rows (even those written by other
    processes/nodes) and never creates or mutates the DB.

    Read errors PROPAGATE intentionally: ``ResearchCleanupService.cleanup()``
    catches a provider exception and aborts the sweep with ZERO deletions. This
    guarantees that an unreadable registry is treated as "delete nothing",
    never as "everything is an orphan" -- the only safe failure mode.
    """

    def _provider() -> Set[str]:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                "SELECT folder_path FROM research_sessions "
                "WHERE folder_path IS NOT NULL"
            )
            return {str(row[0]) for row in cursor.fetchall()}
        finally:
            conn.close()

    return _provider


@dataclass
class ResearchCleanupResult:
    """Summary of a research workspace cleanup sweep."""

    dirs_scanned: int = 0
    dirs_deleted: int = 0
    dirs_preserved: int = 0
    space_reclaimed_bytes: int = 0
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class ResearchCleanupService:
    """
    Garbage-collects orphaned, aged Research Assistant session workspaces.

    A directory is deleted ONLY when ALL of the following hold:
      1. retention is enabled (``retention_days > 0``);
      2. it has NO live ``research_sessions`` row (orphan);
      3. it has not been modified within ``recent_modification_hours``;
      4. it is older than ``retention_days``.

    Any directory mapping to a live session row is always preserved.
    """

    def __init__(
        self,
        research_base_dir: Path,
        retention_days: float,
        live_folder_provider: Callable[[], Set[str]],
        max_dirs_per_run: int = 50_000,
        recent_modification_hours: float = 24.0,
    ) -> None:
        """
        Args:
            research_base_dir: Root dir containing ``<uuid>`` session folders.
            retention_days: Age threshold in days. ``<= 0`` disables the sweep.
            live_folder_provider: Callable returning the set of folder_path
                strings for live ``research_sessions`` rows.
            max_dirs_per_run: Upper bound on directories scanned per sweep
                (Messi #14 — provable termination).
            recent_modification_hours: Skip dirs modified within this window.
        """
        self.research_base_dir = Path(research_base_dir)
        self.retention_days = retention_days
        self._live_folder_provider = live_folder_provider
        self.max_dirs_per_run = max_dirs_per_run
        self.recent_modification_hours = recent_modification_hours
        self.last_cleanup_time: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scan_dirs(self) -> List[Path]:
        """Return up to ``max_dirs_per_run`` immediate subdirectories."""
        dirs: List[Path] = []
        if not self.research_base_dir.exists():
            return dirs
        try:
            for item in self.research_base_dir.iterdir():
                if item.is_dir():
                    dirs.append(item)
                    if len(dirs) >= self.max_dirs_per_run:
                        logger.warning(
                            "Research cleanup hit scan cap (%d) under %s; "
                            "remaining dirs deferred to next sweep",
                            self.max_dirs_per_run,
                            self.research_base_dir,
                        )
                        break
        except OSError as e:
            logger.error(
                "Research cleanup: failed to scan %s: %s",
                self.research_base_dir,
                e,
            )
        return dirs

    def _walk_no_follow(self, path: Path):
        """Yield ``os.DirEntry`` for every entry under ``path``, symlink-safe.

        Uses ``os.scandir`` and NEVER descends into a symlinked directory
        (``entry.is_dir(follow_symlinks=False)``). A symlink entry itself is
        yielded (so its OWN lstat mtime/size is considered) but its target is
        never traversed -- so the ``code-indexer`` / ``issue_manager.py``
        symlinks to real repos are not walked (review N-1). Bounded: a tree of
        non-symlinked real dirs only.
        """
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        yield entry
                        # Only descend into REAL subdirectories, never symlinks.
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
            except OSError:
                # Dir vanished / unreadable mid-scan; skip it, keep going.
                continue

    def _is_recently_modified(self, path: Path) -> bool:
        """True if the dir (or any non-symlinked entry within) was modified
        recently. Symlink-safe: symlink entries are lstat'd (their own mtime),
        and symlinked subdirs are NOT descended into (review N-1)."""
        threshold_seconds = self.recent_modification_hours * 3600
        now = time.time()
        try:
            if (now - os.lstat(path).st_mtime) < threshold_seconds:
                return True
        except OSError as e:
            # On stat error, be conservative: treat as recently modified so we
            # do NOT delete a dir we cannot prove is safe.
            logger.warning(
                "Research cleanup: mtime check failed for %s (%s); preserving",
                path,
                e,
            )
            return True

        for entry in self._walk_no_follow(path):
            try:
                # follow_symlinks=False -> lstat the entry itself, never its
                # (possibly fresh, real-repo) target.
                st = entry.stat(follow_symlinks=False)
            except OSError:
                # Entry vanished mid-scan; ignore and keep checking.
                continue
            if (now - st.st_mtime) < threshold_seconds:
                return True
        return False

    def _age_days(self, path: Path) -> Optional[float]:
        """Age of the dir in days, or None if it cannot be determined."""
        try:
            return (time.time() - path.stat().st_mtime) / (24 * 3600)
        except OSError as e:
            logger.warning(
                "Research cleanup: age check failed for %s (%s); preserving",
                path,
                e,
            )
            return None

    def _dir_size(self, path: Path) -> int:
        """Symlink-safe size: counts real files only, never following symlinks
        into (or descending symlinked subdirs toward) real-repo targets."""
        total = 0
        for entry in self._walk_no_follow(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return total

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------

    def cleanup(self) -> ResearchCleanupResult:
        """
        Run one reconciliation + TTL sweep over the research base dir.

        Used both for startup reconciliation and periodic sweeps. Never raises
        on a per-directory failure; failures are recorded in ``result.errors``.
        """
        start = time.time()
        result = ResearchCleanupResult()

        # Kill switch: retention disabled.
        if self.retention_days is None or self.retention_days <= 0:
            logger.debug(
                "Research cleanup disabled (retention_days=%s)", self.retention_days
            )
            self.last_cleanup_time = datetime.now(timezone.utc)
            result.duration_seconds = time.time() - start
            return result

        # Resolve live session folders once per sweep.
        try:
            live_folders = {str(Path(p)) for p in self._live_folder_provider()}
        except Exception as e:  # noqa: BLE001 — never let a registry read crash GC
            logger.error(
                "Research cleanup: failed to read live sessions; aborting sweep "
                "(no deletions to stay safe): %s",
                e,
                exc_info=True,
            )
            result.duration_seconds = time.time() - start
            return result

        dirs = self._scan_dirs()
        result.dirs_scanned = len(dirs)

        for path in dirs:
            # Safety 0 (Bug #1085 BLOCKING-2): only uuid-shaped session dirs are
            # EVER deletion candidates. 'default' and any non-UUID name (e.g. a
            # stray 'important-do-not-delete') are preserved + logged, never
            # rmtree'd -- this bounds the blast radius to real session folders.
            if not _is_session_dir_name(path.name):
                result.dirs_preserved += 1
                if path.name == DEFAULT_SESSION_DIR_NAME:
                    # Well-known non-reapable dir: expected on every sweep.
                    # Log at DEBUG only — WARNING here would fire every hour
                    # and mask genuine unexpected-directory warnings (Bug #1099).
                    logger.debug(
                        "Research cleanup: skipping well-known default dir %s; preserving",
                        path,
                    )
                else:
                    # Genuinely unexpected non-session directory: keep WARNING
                    # so operators notice stray dirs inside the research root.
                    logger.warning(
                        "Research cleanup: skipping non-session dir %s "
                        "(name is not a session-id / 'default'); preserving",
                        path,
                    )
                continue

            # Safety 1: never delete a dir mapping to a live registry row.
            if str(path) in live_folders:
                result.dirs_preserved += 1
                continue

            # Safety 2: never delete a recently-modified dir (in-flight create).
            if self._is_recently_modified(path):
                result.dirs_preserved += 1
                continue

            # TTL: only delete orphans older than retention.
            age = self._age_days(path)
            if age is None or age <= self.retention_days:
                result.dirs_preserved += 1
                continue

            size = self._dir_size(path)
            try:
                shutil.rmtree(path)
            except OSError as e:
                # Anti-silent-failure (#13): record + log, never swallow/raise.
                msg = f"Failed to delete research workspace {path}: {e}"
                logger.error(msg)
                result.errors.append(msg)
                continue

            result.dirs_deleted += 1
            result.space_reclaimed_bytes += size
            logger.info(
                "Research cleanup deleted orphan workspace %s (%d bytes)",
                path,
                size,
            )

        self.last_cleanup_time = datetime.now(timezone.utc)
        result.duration_seconds = time.time() - start
        logger.info(
            "Research cleanup sweep: scanned=%d deleted=%d preserved=%d "
            "reclaimed=%d bytes errors=%d duration=%.2fs",
            result.dirs_scanned,
            result.dirs_deleted,
            result.dirs_preserved,
            result.space_reclaimed_bytes,
            len(result.errors),
            result.duration_seconds,
        )
        return result


class ResearchCleanupScheduler:
    """
    Daemon scheduler for the Research Assistant workspace GC (Bug #1085).

    Mirrors ``DataRetentionScheduler``: runs ``cleanup()`` immediately on start
    (startup reconciliation) and then every ``interval_seconds``. The retention
    threshold is read LIVE via ``retention_days_provider`` each cycle so Web-UI
    config changes (including disabling via 0) take effect without a restart.
    Per-cycle errors are logged, never raised.
    """

    def __init__(
        self,
        research_base_dir: Path,
        retention_days_provider: Callable[[], float],
        live_folder_provider: Callable[[], Set[str]],
        interval_seconds: int = DEFAULT_RESEARCH_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self.research_base_dir = Path(research_base_dir)
        self._retention_days_provider = retention_days_provider
        self._live_folder_provider = live_folder_provider
        self.interval_seconds = interval_seconds

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Start the daemon thread; the first sweep runs immediately."""
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="ResearchCleanupScheduler",
        )
        self._thread.start()
        logger.info("ResearchCleanupScheduler started")

    def stop(self) -> None:
        """Signal stop and join the thread."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("ResearchCleanupScheduler stopped")

    def is_running(self) -> bool:
        return self._running

    def _run_one_sweep(self) -> ResearchCleanupResult:
        """Build a service with the live retention value and run one sweep."""
        try:
            retention_days = float(self._retention_days_provider())
        except Exception as e:  # noqa: BLE001 — config read must not crash GC
            logger.warning(
                "ResearchCleanupScheduler: failed to read retention config "
                "(%s); skipping this cycle",
                e,
            )
            return ResearchCleanupResult()

        service = ResearchCleanupService(
            research_base_dir=self.research_base_dir,
            retention_days=retention_days,
            live_folder_provider=self._live_folder_provider,
        )
        return service.cleanup()

    def _run_loop(self) -> None:
        """Run cleanup immediately, then wait the interval and repeat."""
        while not self._stop_event.is_set():
            try:
                self._run_one_sweep()
            except Exception as e:  # noqa: BLE001 — never let the daemon die
                logger.error(
                    "ResearchCleanupScheduler: unexpected error in sweep: %s",
                    e,
                    exc_info=True,
                )
            self._stop_event.wait(timeout=self.interval_seconds)
