"""
Git-aware watch handler that integrates file system monitoring with git branch awareness.

Provides seamless git-aware file monitoring that behaves identically to the index command
but with continuous watching capabilities.
"""

import logging
import os
import time
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Dict,
    FrozenSet,
    Generic,
    Hashable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)
from watchdog.events import FileSystemEventHandler

from .watch_metadata import WatchMetadata, GitStateMonitor
from .smart_indexer import SmartIndexer
from .git_topology_service import GitTopologyService
from ..utils.never_indexed_paths import is_never_indexed

if TYPE_CHECKING:
    from ..indexing import FileFinder
    from .tantivy_index_manager import TantivyIndexManager

logger = logging.getLogger(__name__)

# ^ why a code constant, not a setting; one operator, no knobs; bounds a permanently failing path to one attempt per 5 min
_RETRY_BACKOFF_CAP_SECONDS = 300.0
_RETRY_BACKOFF_MAX_DOUBLINGS = 32

# ^ why publish only when the queue is quiet, not per cycle; a burst of writes costs one publish (a full rebuild on large or epoch-dirty collections)
# ^ invariant oldest unpublished change age <= HNSW_PUBLISH_MAX_DELAY_SECONDS before its publish starts
HNSW_PUBLISH_MAX_DELAY_SECONDS = 30.0
# ^ perf bounds publishing to <= 1/(1+4) of the watch thread's time on a repo whose rebuild is slow; a small repo still publishes at once
HNSW_PUBLISH_IDLE_FACTOR = 4.0
# ^ invariant a busy processing thread outlives this join and drains itself; see _process_changes_loop
STOP_JOIN_SECONDS = 5.0
_OBSERVER_JOIN_SECONDS = 5.0
_TRANSITION_KEY = "transition"

_Key = TypeVar("_Key", bound=Hashable)


_stat_workspace: Callable[[Path], os.stat_result] = os.stat


def _directory_identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = _stat_workspace(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (stat.st_dev, stat.st_ino)


def hnsw_publish_due(
    now: float,
    owed_since: float,
    last_publish_end: float,
    last_publish_seconds: float,
    changes_queued: bool,
) -> bool:
    """Whether a watch cycle publishes its owed HNSW now (#2093)."""
    if now - last_publish_end < HNSW_PUBLISH_IDLE_FACTOR * last_publish_seconds:
        return False
    return not changes_queued or now - owed_since >= HNSW_PUBLISH_MAX_DELAY_SECONDS


class _RetryBackoff(Generic[_Key]):
    """Per-key retry schedule: the n-th consecutive failure of a key waits
    base * 2**(n-1) seconds, capped; a success clears the key."""

    def __init__(self, base_seconds: float) -> None:
        self._base = base_seconds
        self._lock = threading.Lock()
        self._failures: Dict[_Key, int] = {}
        self._due: Dict[_Key, float] = {}

    def record_failure(self, key: _Key, now: float) -> None:
        with self._lock:
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            doublings = min(failures - 1, _RETRY_BACKOFF_MAX_DOUBLINGS)
            delay = min(_RETRY_BACKOFF_CAP_SECONDS, self._base * 2**doublings)
            self._due[key] = now + delay

    def is_due(self, key: _Key, now: float) -> bool:
        with self._lock:
            return now >= self._due.get(key, now)

    def clear(self, key: _Key) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._due.pop(key, None)

    def due_keys(self, now: float) -> List[_Key]:
        with self._lock:
            return [key for key, due in self._due.items() if now >= due]

    def scheduled(self) -> Set[_Key]:
        with self._lock:
            return set(self._due)


class GitAwareWatchHandler(FileSystemEventHandler):
    """Git-aware file system event handler for watch operations."""

    def __init__(
        self,
        config,
        smart_indexer: SmartIndexer,
        git_topology_service: GitTopologyService,
        watch_metadata: WatchMetadata,
        debounce_seconds: float = 2.0,
        mutation_lock: Optional[threading.RLock] = None,
        extra_indexers: Sequence[SmartIndexer] = (),
        maintain_fts: bool = False,
        hold_indexing_lock: bool = False,
    ):
        """Initialize git-aware watch handler.

        Args:
            config: Application configuration
            smart_indexer: SmartIndexer instance for git-aware processing
            git_topology_service: Git topology service for branch monitoring
            watch_metadata: Persistent metadata manager
            debounce_seconds: Time to wait before processing accumulated changes
            mutation_lock: Optional daemon-wide chunk-mutation RLock (Codex
                Finding, Story #1488). When injected by the daemon
                (DaemonWatchManager -> here), each per-event mutation cycle in
                `_process_pending_changes` acquires this SAME lock so an
                ongoing watch re-index cannot race a concurrent manual
                `cidx index` / `clean_data` on the same collection. Defaults
                to None for standalone (non-daemon) `cidx watch`, which then
                runs unaffected/unlocked -- byte-identical to before this
                parameter existed.
        """
        super().__init__()
        self.config = config
        self.smart_indexer = smart_indexer
        self.indexers: List[SmartIndexer] = [smart_indexer, *extra_indexers]
        self.git_topology_service = git_topology_service
        self.watch_metadata = watch_metadata
        self.debounce_seconds = debounce_seconds
        self._mutation_lock = mutation_lock
        # ^ hazard True beside cidx watch's FTSWatchHandler => two writers on one Tantivy index; only the server watch sets it
        self._maintain_fts = maintain_fts
        self._hold_indexing_lock = hold_indexing_lock

        self._finder: Optional["FileFinder"] = None
        self._finder_lock = threading.Lock()

        # Thread-safe change tracking
        self.pending_changes: Set[Path] = set()
        self.change_lock = threading.Lock()

        # Git state monitoring
        self.git_monitor = GitStateMonitor(git_topology_service)
        self.git_monitor.register_branch_change_callback(self._handle_branch_change)

        # Processing state
        self.processing_in_progress = False
        self.processing_thread: Optional[threading.Thread] = None
        self._stop_processing = threading.Event()  # Signal to stop processing thread

        # Observer for file system events
        self.observer: Optional[Any] = None

        # Statistics
        self.files_processed_count = 0
        self.indexing_cycles_count = 0

        self._pending_unhides: Dict[str, Tuple[str, str]] = {}
        self._pending_git_state: Optional[Tuple[str, str]] = None
        self._clock: Callable[[], float] = time.monotonic
        self._unhide_backoff: _RetryBackoff[str] = _RetryBackoff(debounce_seconds)
        self._change_backoff: _RetryBackoff[Path] = _RetryBackoff(debounce_seconds)
        self._publish_owed_since: Optional[float] = None
        self._last_publish_end = float("-inf")
        self._last_publish_seconds = 0.0
        self._in_flight: FrozenSet[Path] = frozenset()
        self._deferred_branch_change: Optional[Dict[str, Any]] = None
        self._change_generation = 0
        self._queued_at: Dict[Path, int] = {}
        self._branch_backoff: _RetryBackoff[str] = _RetryBackoff(debounce_seconds)
        self._workspace_identity = _directory_identity(Path(config.codebase_dir))
        self._workspace_removal_logged = False
        self._workspace_probe_failing = False
        self._discarding = threading.Event()

    def request_discard(self) -> None:
        """The workspace is being removed: no new cycle, publish, drain or
        metadata write from now on."""
        self._discarding.set()
        self._stop_processing.set()

    def _workspace_removed(self) -> bool:
        try:
            stat = _stat_workspace(Path(self.config.codebase_dir))
        except (FileNotFoundError, NotADirectoryError):
            removed = True
        except OSError as e:
            # ^ invariant ESTALE/EIO = cannot tell this cycle; never removal, probed again next cycle (#2323)
            if not self._workspace_probe_failing:
                self._workspace_probe_failing = True
                logger.warning(
                    f"Watch of {self.config.codebase_dir} cannot check its workspace: {e}"
                )
            return False
        else:
            self._workspace_probe_failing = False
            identity = (stat.st_dev, stat.st_ino)
            with self.change_lock:
                recorded = self._workspace_identity
                if recorded is None or recorded[0] != identity[0]:
                    # ^ invariant another st_dev = a remount of the same storage, not removal; the identity is re-recorded
                    self._workspace_identity = identity
                    removed = False
                else:
                    # ^ invariant same st_dev + another inode at the same path = a re-activated workspace, never this watch's
                    removed = recorded[1] != identity[1]
        with self.change_lock:
            first_report = removed and not self._workspace_removal_logged
            self._workspace_removal_logged |= removed
        if first_report:
            logger.info(
                f"Watch of {self.config.codebase_dir} stops: its workspace was "
                f"removed; nothing more is indexed or written"
            )
        return removed

    def start_watching(self):
        """Start the git-aware watch process."""
        logger.info("Starting git-aware watch handler")
        if self._discarding.is_set():
            return

        # Reset stop event
        self._stop_processing.clear()

        # ^ invariant work an earlier watch could not finish is re-queued; its HNSW publish is owed too (#2093)
        owed = self.watch_metadata.get_recovery_files()
        if owed:
            logger.info(f"Re-queueing {len(owed)} change(s) owed by the last watch")
            self.queue_changes([Path(p) for p in owed])
        if self.watch_metadata.processing_interrupted:
            self._publish_owed_since = self._clock()
        if self.watch_metadata.pending_branch_change is not None:
            self._deferred_branch_change = dict(
                self.watch_metadata.pending_branch_change
            )
            logger.info("Replaying the branch transition owed by the last watch")

        # Start git monitoring
        if self.git_monitor.start_monitoring():
            logger.info(
                f"Git monitoring started for branch: {self.git_monitor.current_branch}"
            )
            self._retarget_restored_transition()
        else:
            logger.warning("Git not available - proceeding with file-only monitoring")

        # Create and start Observer for file system events
        from watchdog.observers import Observer

        self.observer = Observer()
        self.observer.schedule(self, str(self.config.codebase_dir), recursive=True)
        self.observer.start()
        logger.info(f"File system observer started for {self.config.codebase_dir}")

        # Start change processing thread
        self.processing_thread = threading.Thread(
            target=self._process_changes_loop, daemon=True
        )
        if self.processing_thread:
            self.processing_thread.start()

        logger.info("Git-aware watch handler started successfully")

    def _retarget_restored_transition(self) -> None:
        """A transition owed by the last watch replays from its saved base
        to the branch the repository is on now."""
        transition = self._deferred_branch_change
        current = (self.git_monitor.current_branch, self.git_monitor.current_commit)
        if transition is None or current[0] is None:
            return
        # ^ hazard replaying the saved destination => the monitor starts at the current branch and never reports the switch made while stopped
        if (transition["new_branch"], transition.get("new_commit")) != current:
            logger.info(
                f"Owed branch transition retargeted from {transition['new_branch']} "
                f"to the current branch {current[0]}"
            )
            self._deferred_branch_change = {
                **transition,
                "new_branch": current[0],
                "new_commit": current[1],
            }

    def stop_watching(self, discard: bool = False):
        """Stop the git-aware watch process. discard=True (the workspace is
        being removed) does no new work and returns only once the processing
        thread has exited."""
        logger.info("Stopping git-aware watch handler")

        # Signal processing thread to stop
        if discard:
            self.request_discard()
        self._stop_processing.set()
        self._stop_observing()

        if discard:
            if self.processing_thread is not None:
                # ^ invariant finite wait, no clock (#1218): the in-flight cycle ends, then every step returns at once
                self.processing_thread.join()
        elif self.processing_thread is None:
            self._drain_on_stop()
        else:
            # ^ invariant the processing thread drains on exit (_process_changes_loop); a bounded join, never a clock on its indexing (#1218)
            self.processing_thread.join(timeout=STOP_JOIN_SECONDS)
            if self.processing_thread.is_alive():
                logger.info(
                    "Watch processing thread is finishing a cycle; it drains the "
                    "queue and publishes before it exits"
                )
                # ^ why no lock, not change_lock; _in_flight is a frozenset the cycle replaces, never mutates
                self._persist_owed_changes(set(self._in_flight))

        logger.info("Git-aware watch handler stopped")

    def _stop_observing(self) -> None:
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=_OBSERVER_JOIN_SECONDS)
            logger.info("File system observer stopped")
        self.git_monitor.stop_monitoring()

    def _drain_on_stop(self) -> None:
        """The watch's last work: process every queued and retry-scheduled
        change and publish, then durably record whatever is still owed."""
        if self._discarding.is_set():
            return
        try:
            self._process_pending_changes(final_cleanup=True)
        finally:
            self._persist_owed_changes(set())

    def _persist_owed_changes(self, extra: Set[Path]) -> None:
        """Record in watch_metadata.json the changes this watch has not
        indexed (the next watch start re-queues them), or clear the record."""
        if self._discarding.is_set() or self._workspace_removed():
            return
        with self.change_lock:
            owed = set(self.pending_changes) | extra
        owed |= self._change_backoff.scheduled()
        transition = self._deferred_branch_change
        self.watch_metadata.pending_branch_change = transition
        self.watch_metadata.files_being_processed = sorted(str(p) for p in owed)
        if owed or transition is not None:
            self.watch_metadata.mark_processing_interrupted(
                f"{len(owed)} change(s) and "
                f"{'a' if transition else 'no'} branch transition owed when the "
                f"watch stopped"
            )
        try:
            self.watch_metadata.save_to_disk(
                Path(self.config.codebase_dir)
                / ".code-indexer"
                / "watch_metadata.json",
                create_parents=False,
            )
        except OSError as e:
            if self._workspace_removed():
                return
            logger.error(f"Could not record the watch's owed changes: {e}")

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)
        print(f"🔧 MODIFIED: {file_path}")  # Simple debug
        self._add_pending_change(file_path, "modified")

    def on_deleted(self, event):
        """Handle file deletion events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)
        print(f"🗑️  WATCH MODE: File deletion detected: {file_path}")
        self._add_pending_change(file_path, "deleted")

    def on_created(self, event):
        """Handle file creation events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)
        print(f"✨ CREATED: {file_path}")  # Simple debug
        self._add_pending_change(file_path, "created")

    def on_moved(self, event):
        """Handle file move events."""
        if event.is_directory:
            return

        # Treat move as delete + create
        old_path = Path(event.src_path)
        new_path = Path(event.dest_path)

        self._add_pending_change(old_path, "deleted")
        self._add_pending_change(new_path, "created")

    def queue_changes(self, paths: Sequence[Path]) -> None:
        """Queue writes the observer may not have seen (made before it was
        scheduled), under the same rules as observer events."""
        for path in paths:
            self._add_pending_change(path, "modified" if path.exists() else "deleted")

    def _add_pending_change(self, file_path: Path, change_type: str):
        """Add a file change to pending queue if it should be indexed."""
        try:
            # Ignore atomic write temp files created by _atomic_write_file().
            # These have prefix ".tmp_" (e.g. .tmp_abc123_somefile.md).
            # Path.suffix returns ".md" for such files, so extension checks
            # alone are insufficient — we must filter by basename prefix.
            if file_path.name.startswith(".tmp_"):
                logger.debug(f"Ignoring atomic write temp file: {file_path}")
                return

            # For deletion events, always include the file because we can't check
            # file properties on non-existent files
            if change_type != "deleted":
                # Check if file should be included in indexing
                if not self._should_include_file(file_path):
                    print(f"🔍 File excluded from indexing: {file_path}")
                    return
            else:
                # For deleted files, check if the extension would have been included
                if not self._should_include_deleted_file(file_path):
                    print(f"🔍 Deleted file excluded from indexing: {file_path}")
                    return

            with self.change_lock:
                self.pending_changes.add(file_path)
                self._change_generation += 1
                self._queued_at[file_path] = self._change_generation
                print(f"📂 Added pending change: {change_type} {file_path}")
                logger.debug(f"Added pending change: {change_type} {file_path}")

        except Exception as e:
            print(f"❌ Failed to add pending change for {file_path}: {e}")
            logger.warning(f"Failed to add pending change for {file_path}: {e}")

    def _file_finder(self) -> "FileFinder":
        with self._finder_lock:
            if self._finder is None:
                from ..indexing import FileFinder

                self._finder = FileFinder(self.config)
            return self._finder

    def _should_include_file(self, file_path: Path) -> bool:
        """Check if file should be included in indexing."""
        try:
            return bool(self._file_finder()._should_include_file(file_path))
        except Exception as e:
            logger.warning(f"Failed to check if file should be included: {e}")
            return False

    def _should_include_deleted_file(self, file_path: Path) -> bool:
        """Whether the full walk would have indexed `file_path` (gone, so no
        size or content check): extension, exclude patterns, override."""
        try:
            extension = file_path.suffix.lstrip(".")
            if extension not in self.config.file_extensions:
                return False
            try:
                relative_path = file_path.relative_to(self.config.codebase_dir)
            except ValueError:
                return False
            if is_never_indexed(str(relative_path)):
                return False
            finder = self._file_finder()
            # ^ invariant same exclusion as find_files (#1467); else the store's own .code-indexer files are queued as deleted sources (#2093)
            base_result = not finder.matches_exclude_pattern(str(relative_path))
            if finder.override_filter_service is not None:
                return bool(
                    finder.override_filter_service.should_include_file(
                        relative_path, base_result
                    )
                )
            return base_result
        except Exception as e:
            logger.warning(f"Failed to check if deleted file should be included: {e}")
            return False

    def _process_changes_loop(self):
        """Main loop for processing file changes with debouncing."""
        try:
            self._run_changes_loop()
        finally:
            try:
                self._drain_on_stop()
            finally:
                # ^ invariant always: whatever the drain or the workspace probe did, the observer never outlives this thread
                self._stop_observing()

    def _run_changes_loop(self) -> None:
        while not self._stop_processing.is_set():
            try:
                # Use wait instead of sleep to be interruptible
                if self._stop_processing.wait(timeout=self.debounce_seconds):
                    break  # Stop signal received

                # ^ order before the git check; a removed repository reads as a branch change (#2323)
                if self._workspace_removed():
                    break

                # Check for git state changes before processing files
                git_change = self.git_monitor.check_for_changes()
                if git_change:
                    # Branch change detected - handle it and skip this cycle
                    continue

                # Process pending file changes
                self._process_pending_changes()

            except Exception as e:
                logger.error(f"Error in change processing loop: {e}")
                # Use wait instead of sleep to be interruptible
                if self._stop_processing.wait(timeout=5):
                    break  # Stop signal received during error wait

    def _mutation_lock_ctx(self) -> ContextManager[object]:
        # ^ see Story #1488; every watch store mutation holds the daemon mutation lock when one is injected
        return self._mutation_lock if self._mutation_lock is not None else nullcontext()

    def _process_pending_changes(self, final_cleanup: bool = False):
        """Process all pending file changes using git-aware indexing."""
        if self._discarding.is_set():
            return
        if (
            self._deferred_branch_change is not None
            and not self.processing_in_progress
            and self._branch_backoff.is_due(_TRANSITION_KEY, self._clock())
        ):
            # ^ order before the queue snapshot and before this cycle's lock; the lock is not re-entrant
            self._handle_branch_change(self._deferred_branch_change)
        retry_due = (
            self._change_backoff.scheduled()
            if final_cleanup
            else self._change_backoff.due_keys(self._clock())
        )
        with self.change_lock:
            self.pending_changes.update(retry_due)
            changes_to_process = self.pending_changes.copy()
            self.pending_changes.clear()
            self._queued_at.clear()

        # ^ hazard taking the mutation lock before this check => stop's final cleanup waits forever on a stuck cycle
        if self.processing_in_progress:
            # Re-add changes back to queue if already processing
            with self.change_lock:
                self.pending_changes.update(changes_to_process)
            return

        if (
            not changes_to_process
            and not final_cleanup
            and not self._pending_unhides
            and self._publish_owed_since is None
        ):
            return
        if self._workspace_removed():
            return

        self.processing_in_progress = True
        try:
            with self._repository_lock() as locked:
                if locked:
                    self._run_cycle(changes_to_process, final_cleanup)
                else:
                    self._requeue_failed_changes(set(changes_to_process))
        finally:
            self.processing_in_progress = False

    @contextmanager
    def _repository_lock(self) -> Iterator[bool]:
        """The repository indexing lock `cidx index` holds, for one cycle
        (server watch only); yields False when another run holds it."""
        if not self._hold_indexing_lock:
            yield True
            return
        if self._workspace_removed():
            yield False
            return
        from .indexing_lock import IndexingLockError, create_indexing_lock

        # ^ hazard create_parents=True => a lock taken after deactivation recreates the workspace (#2323)
        lock = create_indexing_lock(
            Path(self.config.codebase_dir) / ".code-indexer", create_parents=False
        )
        try:
            lock.acquire(str(self.config.codebase_dir))
        except IndexingLockError as e:
            # ^ invariant never two writers on one repository's stores (#2093); the changes wait their backoff
            logger.info(f"Watch cycle deferred, the repository is being indexed: {e}")
            yield False
            return
        try:
            yield True
        finally:
            lock.release()

    def _run_cycle(self, changes_to_process: Set[Path], final_cleanup: bool) -> None:
        self._in_flight = frozenset(changes_to_process)
        try:
            with self._mutation_lock_ctx():
                self._retry_pending_unhides()
            if changes_to_process or final_cleanup:
                self.watch_metadata.mark_processing_start(
                    [str(p) for p in changes_to_process]
                )
                failed_changes = self._index_changes(changes_to_process)
                self._finish_cycle(failed_changes, len(changes_to_process))
        except Exception as e:
            logger.error(f"Failed to process file changes: {e}")
            # ^ hazard re-adding at once => a permanently broken store re-pays every embedding each cycle; back off
            self._requeue_failed_changes(set(changes_to_process))
            self.watch_metadata.mark_processing_interrupted(str(e))
        finally:
            self._in_flight = frozenset()
            self._publish_hnsw_if_due(final_cleanup)

    def _relative_changes(self, changes: Set[Path]) -> Dict[str, Path]:
        by_relative: Dict[str, Path] = {}
        for file_path in changes:
            try:
                by_relative[str(file_path.relative_to(self.config.codebase_dir))] = (
                    file_path
                )
            except ValueError:
                # File outside codebase directory
                continue
        return by_relative

    def _index_changes(self, changes: Set[Path]) -> Set[Path]:
        """Run the cycle's changes through every indexer (one per configured
        provider; the primary first). Returns the changes any indexer failed."""
        by_relative = self._relative_changes(changes)
        if not by_relative:
            return set()
        relative_paths = list(by_relative)
        print(
            f"🔄 Processing {len(relative_paths)} file changes using git-aware indexing"
        )
        logger.info(f"Processing {len(relative_paths)} file changes")
        # ^ see Story #1488; held for this mutation only, never the watch lifetime
        with self._mutation_lock_ctx():
            # ^ invariant every write leaves an HNSW publish owed until _publish_hnsw_if_due runs (#2093)
            if self._publish_owed_since is None:
                self._publish_owed_since = self._clock()
            all_stats = [
                indexer.process_files_incrementally(
                    relative_paths, force_reprocess=False, quiet=False, watch_mode=True
                )
                for indexer in self.indexers
            ]
            fts_failed = (
                self._update_fts(list(by_relative.values()))
                if self._maintain_fts
                else set()
            )
        # ^ why the primary's counts, not a sum over indexers; every indexer processes the same paths
        primary = all_stats[0]
        self.files_processed_count += primary.files_processed
        self.indexing_cycles_count += 1
        print(
            f"📝 Processed {primary.files_processed} files: {', '.join(relative_paths)}"
        )
        logger.info(f"Processed {primary.files_processed} files in git-aware mode")
        failed: Set[Path] = set(fts_failed)
        for stats in all_stats:
            failed |= self._failed_changes(stats, by_relative)
        for path in set(by_relative.values()) - failed:
            self._change_backoff.clear(path)
        return failed

    def _update_fts(self, paths: List[Path]) -> Set[Path]:
        """One FTS writer for the cycle (#2093): replace each path's
        documents with its current chunks (none once deleted), through the
        same decision and settling as `cidx index --fts`. Returns the paths
        it could not index. A failure leaves the content marker dropped, so
        the next run rebuilds the index from disk.

        Raises:
            FtsIndexError: the index could not be opened or committed.
        """
        from .fts_lifecycle import fts_index_dir, open_fts_index_for_run

        if not (fts_index_dir(self.config) / "meta.json").exists():
            return set()
        # ^ order a failed open raises before the try: no writer exists to close
        fts_manager, rebuilt = open_fts_index_for_run(self.config, force_full=False)
        try:
            failed = self._write_fts_documents(fts_manager, rebuilt, paths)
        finally:
            fts_manager.close()
        return failed & set(paths)

    def _write_fts_documents(
        self, fts_manager: "TantivyIndexManager", rebuilt: bool, paths: List[Path]
    ) -> Set[Path]:
        from .fts_file_documents import FileFtsDocuments, FtsReplaceError
        from .fts_lifecycle import bootstrap_fts_from_disk, finish_fts_run

        source_files: Optional[List[Path]] = None
        if rebuilt:
            source_files = list(self._file_finder().find_files())
            failed = bootstrap_fts_from_disk(fts_manager, self.config, source_files)
        else:
            failed = []
            documents = FileFtsDocuments(self.config)
            for path in paths:
                try:
                    documents.replace_in_index(fts_manager, path)
                except (ValueError, OSError, FtsReplaceError) as e:
                    logger.error(f"FTS could not index {path}: {e}")
                    failed.append((path, str(e)))
        finish_fts_run(
            fts_manager,
            self.config,
            run_raised=False,
            failed_files=failed,
            source_files=(
                source_files
                if source_files is not None
                else self._file_finder().find_files()
            ),
        )
        return {Path(path) for path, _error in failed}

    def _finish_cycle(self, failed_changes: Set[Path], change_count: int) -> None:
        # ^ invariant a failed change => re-queued, cycle not successful
        if failed_changes:
            self._requeue_failed_changes(failed_changes)
            return
        self.watch_metadata.update_after_sync_cycle(files_processed=change_count)
        # ^ invariant processing_interrupted stays set while any watch retry is owed
        owed = len(self._change_backoff.scheduled()) + len(self._pending_unhides)
        if owed:
            self.watch_metadata.mark_processing_interrupted(
                f"{owed} watch retr(ies) still owed"
            )

    def _publish_hnsw_if_due(self, final_cleanup: bool) -> None:
        """Publish every indexer's owed HNSW (#2093): debounced while the
        watch runs (hnsw_publish_due), always on its final cleanup. A failed
        publish stays owed; the durable pending signal also makes the next
        `cidx index` publish it."""
        if self._discarding.is_set():
            return
        owed_since = self._publish_owed_since
        start = self._clock()
        if not final_cleanup:
            if owed_since is None:
                return
            with self.change_lock:
                queued = bool(self.pending_changes)
            if not hnsw_publish_due(
                start,
                owed_since,
                self._last_publish_end,
                self._last_publish_seconds,
                queued,
            ):
                return
        try:
            with self._mutation_lock_ctx():
                for indexer in self.indexers:
                    indexer.publish_owed_hnsw()
        except Exception as e:
            logger.error(f"Publishing the watch's HNSW changes failed: {e}")
            self.watch_metadata.mark_processing_interrupted(
                f"HNSW publish failed; retried next cycle: {e}"
            )
            return
        finally:
            self._last_publish_end = self._clock()
            self._last_publish_seconds = self._last_publish_end - start
        self._publish_owed_since = None

    def _handle_branch_change(self, change_event: Dict[str, Any]):
        """Handle git branch change events."""
        old_branch = change_event["old_branch"]
        new_branch = change_event["new_branch"]

        logger.info(f"Git branch change detected: {old_branch} → {new_branch}")

        owed = self._deferred_branch_change
        # ^ invariant an unapplied transition keeps its base: A->B then B->C is applied as A->C, since the index still reflects A
        transition = (
            change_event
            if owed is None or owed is change_event
            else {
                **change_event,
                "old_branch": owed["old_branch"],
                "old_commit": owed.get("old_commit"),
            }
        )
        # ^ invariant owed until applied; the git monitor has already moved on and never reports it again
        self._deferred_branch_change = transition
        with self._repository_lock() as locked:
            if not locked:
                self.watch_metadata.mark_processing_interrupted(
                    f"Branch change to {new_branch} deferred: the repository is being indexed"
                )
                return
            try:
                self._apply_branch_change(transition)
            except Exception as e:
                logger.error(f"Failed to handle branch change, retried later: {e}")
                self._branch_backoff.record_failure(_TRANSITION_KEY, self._clock())
                self.watch_metadata.mark_processing_interrupted(
                    f"Branch change error: {e}"
                )
                return
            self._deferred_branch_change = None
            self._branch_backoff.clear(_TRANSITION_KEY)

    def _apply_branch_change(self, change_event: Dict[str, Any]) -> None:
        old_branch = change_event["old_branch"]
        new_branch = change_event["new_branch"]
        with self.change_lock:
            started_at_generation = self._change_generation
        # ^ invariant transition recorded only after every un-hide succeeded; a newer transition supersedes owed un-hides
        self._pending_git_state = (
            new_branch or "unknown",
            change_event["new_commit"] or "unknown",
        )
        self._pending_unhides = {}
        self._unhide_backoff = _RetryBackoff(self.debounce_seconds)
        reindex_failed: Set[Path] = set()
        covered: Set[Path] = set()

        if old_branch and new_branch:
            analysis = self.git_topology_service.analyze_branch_change(
                old_branch,
                new_branch,
                old_commit=change_event.get("old_commit"),
                new_commit=change_event.get("new_commit"),
            )
            by_relative = {
                p: self.config.codebase_dir / p for p in analysis.files_to_reindex
            }
            covered = set(by_relative.values()) | {
                self.config.codebase_dir / p for p in analysis.files_to_update_metadata
            }
            with self._mutation_lock_ctx():
                if analysis.files_to_reindex:
                    for indexer in self.indexers:
                        stats = indexer.process_files_incrementally(
                            analysis.files_to_reindex
                        )
                        reindex_failed |= self._failed_changes(stats, by_relative)
                for file_path in analysis.files_to_update_metadata:
                    self._unhide_in_every_collection(file_path, new_branch)
                if self._maintain_fts:
                    # ^ why the raw diff, not only the analysis; the analysis drops files the new branch removed, whose FTS documents must go
                    removed = {
                        self.config.codebase_dir / p
                        for p in self.git_topology_service.files_changed_between(
                            old_branch, new_branch
                        )
                        if not (self.config.codebase_dir / p).exists()
                    }
                    reindex_failed |= self._update_fts(sorted(covered | removed))
            if self._publish_owed_since is None:
                self._publish_owed_since = self._clock()
            logger.info(
                f"Branch transition complete: {len(analysis.files_to_reindex)} "
                f"file(s) re-indexed, {len(analysis.files_to_update_metadata)} "
                f"made visible"
            )

        # ^ invariant only covered changes queued before the transition started leave the queue; a later edit was not read by it
        with self.change_lock:
            for path in covered:
                if self._queued_at.get(path, 0) <= started_at_generation:
                    self.pending_changes.discard(path)
                    self._queued_at.pop(path, None)

        self._record_transition_if_complete()
        self._requeue_failed_changes(reindex_failed)
        self._publish_hnsw_if_due(final_cleanup=True)

    def _collection_of(self, indexer: SmartIndexer) -> str:
        return str(
            indexer.vector_store_client.resolve_collection_name(
                indexer.config, indexer.embedding_provider
            )
        )

    def _unhide_in_every_collection(self, file_path: str, branch: str) -> None:
        try:
            for indexer in self.indexers:
                indexer._ensure_file_visible_in_branch_thread_safe(
                    file_path, branch, self._collection_of(indexer)
                )
        except Exception as e:
            logger.warning(f"Failed to update metadata for {file_path}: {e}")
            self._pending_unhides[file_path] = (
                branch,
                self._collection_of(self.smart_indexer),
            )
            self._unhide_backoff.record_failure(file_path, self._clock())

    def _retry_pending_unhides(self) -> None:
        now = self._clock()
        for file_path, (branch, _collection) in list(self._pending_unhides.items()):
            # ^ hazard un-hiding a file gone from disk => its stale points return to search; the deletion path owns it
            if not (self.config.codebase_dir / file_path).exists():
                logger.info(f"Dropping the owed un-hide of removed file {file_path}")
                del self._pending_unhides[file_path]
                self._unhide_backoff.clear(file_path)
                continue
            if not self._unhide_backoff.is_due(file_path, now):
                continue
            try:
                # ^ why every collection, not the recorded one; an un-hide is idempotent and any provider's may have failed
                for indexer in self.indexers:
                    indexer._ensure_file_visible_in_branch_thread_safe(
                        file_path, branch, self._collection_of(indexer)
                    )
            except Exception as e:
                logger.warning(f"Retrying the un-hide of {file_path} failed: {e}")
                self._unhide_backoff.record_failure(file_path, now)
                continue
            del self._pending_unhides[file_path]
            self._unhide_backoff.clear(file_path)
        self._record_transition_if_complete()

    def _record_transition_if_complete(self) -> None:
        if self._pending_git_state is None:
            return
        if self._pending_unhides:
            self.watch_metadata.mark_processing_interrupted(
                f"{len(self._pending_unhides)} un-hide(s) owed by the branch change; "
                "retried next cycle"
            )
            return
        if self._workspace_removed():
            return
        self.watch_metadata.update_git_state(*self._pending_git_state)
        metadata_path = (
            self.config.codebase_dir / ".code-indexer" / "watch_metadata.json"
        )
        self.watch_metadata.save_to_disk(metadata_path, create_parents=False)
        self._pending_git_state = None

    @staticmethod
    def _failed_changes(stats: Any, by_relative: Dict[str, Path]) -> Set[Path]:
        # ^ invariant a failure without an attributed path => every path of the batch is retried
        failed_relative = set(stats.failed_paths)
        if stats.failed_files > len(failed_relative):
            failed_relative = set(by_relative)
        return {by_relative[p] for p in failed_relative if p in by_relative}

    def _requeue_failed_changes(self, failed: Set[Path]) -> None:
        if not failed:
            return
        # ^ invariant a failed change waits its backoff (next cycle, then doubling to the cap); a new event for it is processed at once
        now = self._clock()
        for path in failed:
            self._change_backoff.record_failure(path, now)
        self.watch_metadata.mark_processing_interrupted(
            f"{len(failed)} file change(s) failed; retried next cycle"
        )

    def is_watching(self) -> bool:
        """Check if actively watching.

        Returns:
            True if processing thread is alive and running, False otherwise
        """
        return self.processing_thread is not None and self.processing_thread.is_alive()

    def get_stats(self) -> Dict[str, Any]:
        """Get watch statistics.

        Returns:
            Dictionary with current watch statistics
        """
        return {
            "files_processed": self.files_processed_count,
            "indexing_cycles": self.indexing_cycles_count,
            "current_branch": (
                self.git_monitor.current_branch if self.git_monitor else None
            ),
            "pending_changes": len(self.pending_changes),
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Get watch session statistics."""
        base_stats = self.watch_metadata.get_statistics()

        # Add handler-specific statistics
        base_stats.update(
            {
                "handler_files_processed": self.files_processed_count,
                "handler_indexing_cycles": self.indexing_cycles_count,
                "pending_changes": len(self.pending_changes),
                "processing_in_progress": self.processing_in_progress,
                "git_monitoring_active": self.git_monitor._monitoring,
                "current_git_branch": self.git_monitor.current_branch,
            }
        )

        return base_stats
