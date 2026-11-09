"""Keep this pod's served-index checkouts inside its disk (``index_store_backend: delegated``).

Without the shared golden-repos mount every pod checks a published index out
to its own disk the first time it serves it (``index_materializer``,
``temporal_index_store``) and nothing ever removed a checkout to make room:
the leftover sweep (``checkout_sweep``) only drops copies whose alias is gone.
At ~180 MB per repository and ~900 repositories a pod's 20 GiB disk fills.
This module is a cache over those checkouts, evicting the least recently
served one first.

Scope -- what can be evicted:

* only directories carrying ``CHECKED_OUT_MARKER`` (the temporal marker for a
  shard root): a checkout or a publish wrote it, so the version is in the
  IndexStore and the next read checks it out again. A pod's own unpublished
  build carries no marker and is never a candidate; neither is anything under
  the local store (no cache is installed: every hook is a no-op);
* golden snapshots (``.versioned/<repo>/v_*``) -- never a base clone
  (``golden-repos/<repo>`` itself, issue-64616: the refresh's incremental base),
  activated copies (a composite and its member indexes as ONE entry), and the
  temporal shard roots (``.temporal/<repo>``).

Policy: least recently served first. Access is recorded in memory on every
serve (``note_hit``; one ``stat`` of the index root) and, at most once per ``STAMP_INTERVAL_SECONDS``
per checkout, as the mtime of a stamp file on this pod's own disk, so the order
survives a restart and is shared by the server's processes. A checkout never
served since it was written counts from its marker's mtime.

Limits (``ServedIndexCacheLimits``): a high and a low watermark, as a total
byte size of the cached checkouts and/or a minimum free-disk percentage. Over
the high watermark (or before a checkout that would cross it) entries are
evicted until the low one holds. A single checkout larger than what the cache
may ever hold is refused loudly (``ServedIndexTooLargeError``) instead of
evicting everything else for it, and the refusal is remembered per version so
it is not downloaded again.

Never evicted (the entry is skipped, the pass goes on):

* pinned: a build in progress on it (``pinned_served_index`` -- activated
  reindex, branch delta, the HNSW orphan repair). A pin holds the target's
  readers lock SHARED for its whole duration, so an eviction in ANY server
  process skips it, and taking the pin waits out an eviction already running;
* building: the golden repository's write lock is held (a refresh builds the
  base clone's index and the temporal shards in place);
* in use: a query on this pod holds it (``QueryTracker``), or a read in any
  process holds its readers lock (``served_index_reads``); it is marked
  quiescing while it is evicted, so no new tracked query is admitted;
* quiescing: another purge (a deactivation, the leftover sweep) already marked
  it -- an eviction clears only a mark it set itself;
* leased: a live snapshot reader lease (a cached HNSW/FTS handle);
* recent: served within ``min_idle_seconds``;
* busy: its checkout lock is held (a checkout or swap running) -- eviction
  only ever TRIES the lock, so it can never deadlock with a checkout that is
  making room;
* error: retiring it raised ``OSError`` -- logged, whatever it renamed aside
  is deleted, and the pass (and the admission that ran it) goes on.

Eviction holds the target's checkout lock (``exclusive_checkout``), RENAMES the
index state aside first and deletes it after: a racing reader either opened its
files before (and keeps them) or finds the target not served and checks it out
again. A reader that passed its fast "already served" check while an eviction
of that target is running, or that lost the race to one that already
finished, is told so by ``note_hit`` and waits for the lock and re-checks.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from code_indexer.server.services.index_materializer import (
    flock_live,
    CHECKED_OUT_MARKER,
    IN_PROGRESS_MARKER,
    INCOMING_DIRNAME,
    RETIRED_DIRNAME,
    is_base_clone,
    PublishedIndexUnavailableError,
    try_exclusive_checkout,
)
from code_indexer.utils.index_store import INDEX_DIRNAME, LAST_SERVED_STAMP

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

# LAST_SERVED_STAMP (touched, mtime, when a checkout is served; at most once
# per interval) lives in utils.index_store, which listings read.
#: An evicted checkout's index state, renamed aside until it is deleted.
EVICTED_PREFIX = ".cidx-index-evicted-"
STAMP_INTERVAL_SECONDS = 300.0

#: A golden repo's temporal shards: markers beside ``.temporal/<repo>``.
TEMPORAL_DIRNAME = ".temporal"
TEMPORAL_MARKER_SUFFIX = ".temporal-checked-out"
TEMPORAL_IN_PROGRESS_SUFFIX = ".temporal-in-progress"
TEMPORAL_STAMP_SUFFIX = ".temporal-last-served"
TEMPORAL_EVICTED_INFIX = ".temporal-evicted-"

#: What a checkout leaves in a target besides its index root.
POD_STATE_FILES = (CHECKED_OUT_MARKER, IN_PROGRESS_MARKER, LAST_SERVED_STAMP)
POD_STATE_DIRS = (INCOMING_DIRNAME, RETIRED_DIRNAME)

KIND_GOLDEN = "golden"
KIND_ACTIVATED = "activated"
KIND_TEMPORAL = "temporal"

GIB = 1024**3
#: Defaults for a 20 GiB pod disk; ``0`` disables a limit.
DEFAULT_MAX_BYTES = 14 * GIB
DEFAULT_LOW_BYTES = 12 * GIB
DEFAULT_MIN_FREE_PERCENT = 15.0
DEFAULT_TARGET_FREE_PERCENT = 25.0
DEFAULT_MIN_IDLE_SECONDS = 900.0
DEFAULT_INTERVAL_SECONDS = 60.0

_RESERVED_USER_DIRS = (".trash",)
_VERSIONED = ".versioned"
_ALIASES = "aliases"

SKIP_PINNED = "pinned"
SKIP_BUILDING = "building"
SKIP_IN_USE = "in_use"
SKIP_LEASED = "leased"
SKIP_RECENT = "recent"
SKIP_BUSY = "busy"
SKIP_GONE = "gone"
#: issue-64616: a golden base clone (``<golden_repos_dir>/<alias>``) is never
#: evicted: it is the refresh's incremental base, not a checkout.
SKIP_BASE_CLONE = "base_clone"
#: Another purge of the same key (deactivation, leftover sweep) is running.
SKIP_QUIESCING = "quiescing"
#: Retiring it failed (an ``OSError``): logged, left for the next pass.
SKIP_ERROR = "error"


class ServedIndexTooLargeError(PublishedIndexUnavailableError):
    """One checkout alone is larger than the served-index cache may hold."""


class DiskUsage(NamedTuple):
    total: int
    free: int


# ── discovery (shared with checkout_sweep) ───────────────────────────────────


def list_subdirs(root: Path) -> List[Path]:
    """Real subdirectories of ``root`` (no symlinks), sorted; [] if unreadable."""
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            logger.warning("Served-index scan: cannot list %s (%s)", root, exc)
        return []


def is_marked(target: Path) -> bool:
    return (target / CHECKED_OUT_MARKER).is_file()


def iter_activated_checkouts(activated_root: Path) -> Iterator[Tuple[str, str, Path]]:
    """``(username, user_alias, target)`` of every marked activated copy."""
    for user_dir in list_subdirs(activated_root):
        if user_dir.name in _RESERVED_USER_DIRS:
            continue
        for target in list_subdirs(user_dir):
            if is_marked(target):
                yield user_dir.name, target.name, target


def iter_golden_checkouts(golden_root: Path) -> Iterator[Tuple[str, Path]]:
    """``(repo, target)`` of every marked golden clone path and snapshot."""
    for entry in list_subdirs(golden_root):
        if entry.name == _VERSIONED:
            for namespace in list_subdirs(entry):
                for snapshot in list_subdirs(namespace):
                    if is_marked(snapshot):
                        yield namespace.name, snapshot
        elif entry.name != _ALIASES and is_marked(entry):
            yield entry.name, entry


def temporal_marker(root: Path) -> Path:
    return root.parent / f".{root.name}{TEMPORAL_MARKER_SUFFIX}"


def iter_temporal_checkouts(golden_root: Path) -> Iterator[Tuple[str, Path]]:
    """``(repo, shard root)`` of every temporal root this pod holds a marked
    version of."""
    container = golden_root / TEMPORAL_DIRNAME
    try:
        names = sorted(p.name for p in container.iterdir())
    except OSError:
        return
    for name in names:
        if name.startswith(".") and name.endswith(TEMPORAL_MARKER_SUFFIX):
            repo = name[1 : -len(TEMPORAL_MARKER_SUFFIX)]
            if repo:
                yield repo, container / repo


def composite_members(target: Path) -> List[Path]:
    """The marked member checkouts inside an activated copy (a composite)."""
    return [member for member in list_subdirs(target) if is_marked(member)]


@dataclass(frozen=True)
class ServedCheckout:
    """One evictable unit: a target's index state (a composite with its members)."""

    kind: str
    path: Path
    label: str
    repo: Optional[str] = None
    parts: Tuple[Path, ...] = ()
    versioned: bool = False

    @property
    def key(self) -> str:
        return str(self.path)

    def all_paths(self) -> Tuple[Path, ...]:
        return (self.path,) + self.parts


def discover_served_checkouts(
    activated_root: Optional[Path], golden_root: Optional[Path]
) -> List[ServedCheckout]:
    found: List[ServedCheckout] = []
    if activated_root is not None:
        for username, user_alias, target in iter_activated_checkouts(activated_root):
            found.append(
                ServedCheckout(
                    KIND_ACTIVATED,
                    target,
                    f"{username}/{user_alias}",
                    parts=tuple(composite_members(target)),
                )
            )
    if golden_root is not None:
        for repo, target in iter_golden_checkouts(golden_root):
            # issue-64616: a base clone is the refresh's incremental base (and,
            # with the golden-repos mount on, shared by every pod), never a
            # cache entry -- whatever marker it carries.
            if is_base_clone(target, golden_root):
                continue
            versioned = target.parent.parent.name == _VERSIONED
            label = f"{repo}@{target.name}" if versioned else repo
            found.append(
                ServedCheckout(KIND_GOLDEN, target, label, repo, versioned=versioned)
            )
        for repo, root in iter_temporal_checkouts(golden_root):
            found.append(
                ServedCheckout(KIND_TEMPORAL, root, f"{repo} (temporal)", repo)
            )
    return found


# ── removal (shared with checkout_sweep) ────────────────────────────────────


def retire_pod_state(target: Path) -> Optional[Path]:
    """Take a checkout's pod state out of ``target``: the index root is RENAMED
    aside (returned, for ``purge``), then the markers and swap dirs go. A reader
    sees the target either served or not, never half-deleted."""
    root = target / INDEX_DIRNAME
    retired: Optional[Path] = None
    if root.is_dir() and not root.is_symlink():
        retired = target / f"{EVICTED_PREFIX}{uuid.uuid4().hex}"
        os.rename(root, retired)
    elif root.is_symlink():
        root.unlink()
    for name in POD_STATE_FILES:
        (target / name).unlink(missing_ok=True)
    for name in POD_STATE_DIRS:
        path = target / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    return retired


def purge(retired: Optional[Path]) -> None:
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


def remove_dir_if_empty(target: Path) -> None:
    try:
        os.rmdir(target)
    except OSError:
        pass  # something else lives there: kept


def retire_temporal_root(root: Path) -> Optional[Path]:
    """``retire_pod_state`` for a temporal shard root (a symlink to its versioned
    directory, or a directory): the root disappears in one step, the data is
    renamed aside, the markers beside it go."""
    aside = root.parent / f".{root.name}{TEMPORAL_EVICTED_INFIX}{uuid.uuid4().hex}"
    retired: Optional[Path] = None
    if root.is_symlink():
        data = Path(os.path.realpath(root))
        root.unlink()
        if data.is_dir() and data.parent == root.parent:  # else: not ours, kept
            os.rename(data, aside)
            retired = aside
    elif root.is_dir():
        os.rename(root, aside)
        retired = aside
    for suffix in (
        TEMPORAL_MARKER_SUFFIX,
        TEMPORAL_IN_PROGRESS_SUFFIX,
        TEMPORAL_STAMP_SUFFIX,
    ):
        (root.parent / f".{root.name}{suffix}").unlink(missing_ok=True)
    return retired


def purge_leftovers(activated_root: Optional[Path], golden_root: Optional[Path]) -> int:
    """Delete evicted state a crash left renamed aside (startup)."""
    removed = 0
    targets: List[Path] = []
    if activated_root is not None:
        for user_dir in list_subdirs(activated_root):
            for copy in list_subdirs(user_dir):
                targets.append(copy)
                targets.extend(list_subdirs(copy))  # a composite's members
    if golden_root is not None:
        for entry in list_subdirs(golden_root):
            if entry.name == _VERSIONED:
                for namespace in list_subdirs(entry):
                    targets.extend(list_subdirs(namespace))
            else:
                targets.append(entry)
    for target in targets:
        for leftover in list_subdirs(target):
            if leftover.name.startswith(EVICTED_PREFIX) or (
                target.name == TEMPORAL_DIRNAME
                and TEMPORAL_EVICTED_INFIX in leftover.name
            ):
                shutil.rmtree(leftover, ignore_errors=True)
                removed += 1
    return removed


# ── sizes and stamps ─────────────────────────────────────────────────────────


def tree_bytes(path: Path) -> int:
    """Apparent size of every file under ``path`` (symlinks not followed below
    the top; a symlinked top -- a temporal root -- is resolved)."""
    total = 0
    top = Path(os.path.realpath(path)) if path.is_symlink() else path
    for dirpath, _dirs, files in os.walk(top):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _checkout_bytes(entry: ServedCheckout) -> int:
    if entry.kind == KIND_TEMPORAL:
        return tree_bytes(entry.path)
    return sum(tree_bytes(path / INDEX_DIRNAME) for path in entry.all_paths())


def _marker_of(kind: str, path: Path) -> Path:
    return temporal_marker(path) if kind == KIND_TEMPORAL else path / CHECKED_OUT_MARKER


def _stamp_of(kind: str, path: Path) -> Path:
    if kind == KIND_TEMPORAL:
        return path.parent / f".{path.name}{TEMPORAL_STAMP_SUFFIX}"
    return path / LAST_SERVED_STAMP


def _kind_of(path: Path) -> str:
    """A reader hook's target: a temporal shard root or a checkout target."""
    return KIND_TEMPORAL if path.parent.name == TEMPORAL_DIRNAME else KIND_ACTIVATED


def _holds_index(path: Path) -> bool:
    """The index state an eviction renames away is still at ``path``."""
    if _kind_of(path) == KIND_TEMPORAL:
        return path.exists()
    return (path / INDEX_DIRNAME).exists()


def _purge_renamed_aside(entry: ServedCheckout) -> None:
    """Delete what a retirement that failed midway already renamed aside (the
    entry's checkout locks are held: nothing else renames into these names)."""
    if entry.kind == KIND_TEMPORAL:
        prefix = f".{entry.path.name}{TEMPORAL_EVICTED_INFIX}"
        homes = [(entry.path.parent, prefix)]
    else:
        homes = [(path, EVICTED_PREFIX) for path in entry.all_paths()]
    for home, prefix in homes:
        for leftover in list_subdirs(home):
            if leftover.name.startswith(prefix):
                shutil.rmtree(leftover, ignore_errors=True)


#: Beside a checkout target (``.<name>.readers.lock``): readers hold it SHARED,
#: an eviction EXCLUSIVE -- across every server process on the pod.
READERS_LOCK_SUFFIX = ".readers.lock"


def readers_lock_path(path: Path) -> Path:
    return path.parent / f".{path.name}{READERS_LOCK_SUFFIX}"


def _lock_readers(path: Path, operation: int) -> Optional[int]:
    """The readers lock of *path*, held with *operation* (``flock_live``:
    never a lock on a file the checkout sweep already deleted); None when the
    target's parent is not there (nothing to protect yet). A non-blocking
    operation that would wait raises ``BlockingIOError``."""
    try:
        return flock_live(readers_lock_path(path), operation)
    except FileNotFoundError:
        return None


@contextmanager
def _readers_free(paths: Sequence[Path]) -> Iterator[bool]:
    """Hold every path's readers lock EXCLUSIVE without waiting: False (nothing
    held) while any reader in any process holds one SHARED."""
    held: List[int] = []
    try:
        for path in paths:
            try:
                fd = _lock_readers(path, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            if fd is None:
                continue
            held.append(fd)
        yield True
    finally:
        for fd in held:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


#: Public name for the leftover sweep (``checkout_sweep``), which must see a
#: reader in any process exactly as an eviction does.
readers_free = _readers_free


def require_positive_interval(name: str, interval_seconds: float) -> None:
    """Raise ``ValueError`` unless a background loop's interval is positive."""
    if not interval_seconds > 0:
        raise ValueError(
            f"{name} must be a positive number of seconds, got {interval_seconds!r}"
        )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ── read scopes: an index read holds what it served until it ends ───────────


class _ReadScope:
    """The checkouts one read has served, each held by its readers lock
    SHARED until the read ends -- so no eviction, in any server process, can
    take them away mid-read. Shared by the threads that inherit the context."""

    def __init__(self) -> None:
        self._fds: Dict[str, int] = {}
        self._guard = threading.Lock()

    def keep(self, key: str, fd: int) -> bool:
        """Take ownership of ``fd`` (a SHARED readers lock on ``key``); False
        when the scope already holds ``key`` (the caller closes ``fd``)."""
        with self._guard:
            if key in self._fds:
                return False
            self._fds[key] = fd
            return True

    def release(self) -> None:
        with self._guard:
            fds = list(self._fds.values())
            self._fds.clear()
        for fd in fds:
            os.close(fd)

    def duplicate(self) -> "_ReadScope":
        """A new scope holding every lock this one holds: ``os.dup`` shares
        the open file description, so each SHARED lock stays held until BOTH
        scopes released it -- never dropped in between."""
        copy = _ReadScope()
        with self._guard:
            for key, fd in self._fds.items():
                copy._fds[key] = os.dup(fd)
        return copy


_read_scope: ContextVar[Optional[_ReadScope]] = ContextVar(
    "served_index_read_scope", default=None
)


@contextmanager
def served_index_reads() -> Iterator[None]:
    """Hold every checkout served inside this block (by this thread, or a
    thread that inherits its context) until the block ends. Re-entrant: an
    inner block joins the outer one. Free under the local store (nothing is
    served through the cache, so nothing is held)."""
    if _read_scope.get() is not None:
        yield
        return
    scope = _ReadScope()
    token = _read_scope.set(scope)
    try:
        yield
    finally:
        _read_scope.reset(token)
        scope.release()


F = TypeVar("F", bound=Callable[..., Any])


def reads_served_indexes(fn: F) -> F:
    """Decorator: run ``fn`` (sync or async) inside ``served_index_reads``.

    The body is called through the wrapper's ``__wrapped__`` (what
    ``functools.wraps`` records), so a test can stand a reading body in for
    the real one behind the real entry point."""
    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with served_index_reads():
                return await async_wrapper.__wrapped__(*args, **kwargs)

        return cast(F, async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with served_index_reads():
            return wrapper.__wrapped__(*args, **kwargs)

    return cast(F, wrapper)


class CarriedReads:
    """Work that outlives the read that started it -- an abandoned watchdog
    thread, a fire-and-forget audit -- run while still holding every checkout
    that read held (``carry_served_index_reads``). Calling it runs ``fn`` in
    the carried scope and releases the scope when ``fn`` returns; ``release``
    frees it when ``fn`` will never run (a rejected submit). Idempotent."""

    def __init__(self, fn: Callable[..., Any], scope: Optional[_ReadScope]) -> None:
        self._fn = fn
        self._scope = scope

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._scope is None:
            return self._fn(*args, **kwargs)
        token = _read_scope.set(self._scope)
        try:
            return self._fn(*args, **kwargs)
        finally:
            _read_scope.reset(token)
            self.release()

    def release(self) -> None:
        if self._scope is not None:
            self._scope.release()


def carry_served_index_reads(fn: Callable[..., Any]) -> CarriedReads:
    """Bind ``fn`` to a copy, taken NOW, of every checkout the running read
    scope holds, so they stay held until ``fn`` returns even after the read
    that started it has ended (and anything ``fn`` serves is held too). Must
    be called inside the read, before handing ``fn`` to another thread. No
    read scope running (local store, CLI): ``fn`` runs unchanged."""
    scope = _read_scope.get()
    return CarriedReads(fn, scope.duplicate() if scope is not None else None)


def _keep_in_scope(key: str, fd: int) -> bool:
    """Hand a SHARED readers lock to the running read scope; False when there
    is none or it already holds ``key``."""
    scope = _read_scope.get()
    return scope is not None and scope.keep(key, fd)


def _hold_in_scope(target: Path) -> None:
    """Hold ``target``'s readers lock SHARED in the running read scope (after
    a checkout: its checkout lock is held, so no eviction of it is running)."""
    scope = _read_scope.get()
    if scope is None:
        return
    try:
        fd = _lock_readers(target, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    if fd is None:
        return
    if not scope.keep(str(target), fd):
        os.close(fd)  # already held by this read


# ── pins: builds in progress on a served checkout ───────────────────────────

_pins: Dict[str, int] = {}
_pins_guard = threading.Lock()


@contextmanager
def pinned_served_index(*paths: PathLike) -> Iterator[None]:
    """Keep the checkouts at ``paths`` from being evicted for the duration (a
    build in progress on them). Re-entrant; free under any store.

    Two holds, both for the whole block:

    * the in-memory pin (``is_pinned``), which this process's eviction and
      ``CheckoutSweeper`` check first;
    * each path's readers lock SHARED -- which every eviction, in ANY server
      process, must take EXCLUSIVE (without waiting) before it retires
      anything. Taking it WAITS for an eviction already running on the path
      to finish; once it is held no eviction can start, so a pin can never
      interleave with a retire. A path whose parent does not exist yet has
      nothing to evict and takes no lock, and under the local store (no cache
      installed) no lock is taken at all.
    """
    keys = [str(Path(p)) for p in paths if p]
    with _pins_guard:
        for key in keys:
            _pins[key] = _pins.get(key, 0) + 1
    held: List[int] = []
    # Under the local store (no cache installed) nothing is ever evicted:
    # no lock is taken and no file touched -- today's behaviour exactly.
    lock_keys = keys if active_served_index_cache() is not None else []
    try:
        for key in lock_keys:
            # Waits out a running eviction.
            fd = _lock_readers(Path(key), fcntl.LOCK_SH)
            if fd is None:
                continue
            held.append(fd)
        yield
    finally:
        for fd in held:
            os.close(fd)  # releases the shared lock
        with _pins_guard:
            for key in keys:
                left = _pins.get(key, 0) - 1
                if left > 0:
                    _pins[key] = left
                else:
                    _pins.pop(key, None)


def pin_count() -> int:
    """How many paths are pinned right now."""
    with _pins_guard:
        return len(_pins)


def is_pinned(path: PathLike) -> bool:
    with _pins_guard:
        return _pins.get(str(Path(path)), 0) > 0


# ── the cache ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ServedIndexCacheLimits:
    """High/low watermarks; ``0`` disables that limit."""

    max_bytes: int = DEFAULT_MAX_BYTES
    low_bytes: int = DEFAULT_LOW_BYTES
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT
    target_free_percent: float = DEFAULT_TARGET_FREE_PERCENT
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS

    def __post_init__(self) -> None:
        if self.max_bytes < 0 or self.low_bytes < 0:
            raise ValueError("served_index_cache byte limits must be >= 0")
        if self.max_bytes and not 0 < self.low_bytes <= self.max_bytes:
            raise ValueError(
                "served_index_cache_low_bytes must be > 0 and <= "
                f"served_index_cache_max_bytes ({self.low_bytes} > {self.max_bytes})"
            )
        if (
            not 0 <= self.min_free_percent < 100
            or not 0 <= self.target_free_percent < 100
        ):
            raise ValueError(
                "served_index_cache free-disk percentages must be in [0, 100)"
            )
        if self.min_free_percent and self.target_free_percent < self.min_free_percent:
            raise ValueError(
                "served_index_cache_target_free_percent must be >= "
                "served_index_cache_min_free_percent"
            )
        if self.min_idle_seconds <= 0:
            # The only guard for a query that takes its ref with a plain
            # ``QueryTracker.increment_ref`` (the MCP search path, not the
            # quiescing-aware one): its serve records the access under the
            # cache lock first, so the eviction sees it as recent.
            raise ValueError("served_index_cache_min_idle_seconds must be > 0")


@dataclass
class _Sized:
    size: int
    #: What the size was measured for: each part's path, marker mtime and
    #: recorded version -- a member added, or a version swapped in within
    #: one mtime tick, still invalidates it.
    signature: Tuple[Tuple[str, float, str], ...] = ()


def _signature(entry: ServedCheckout) -> Tuple[Tuple[str, float, str], ...]:
    parts = []
    for path in entry.all_paths():
        marker = _marker_of(entry.kind, path)
        try:
            version = marker.read_text()
        except OSError:
            version = ""
        parts.append((str(path), _mtime(marker), version))
    return tuple(parts)


@dataclass
class EvictionReport:
    evicted: List[str] = field(default_factory=list)
    freed_bytes: int = 0
    skipped: Dict[str, str] = field(default_factory=dict)
    satisfied: bool = True


def _disk_usage_of(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(usage.total, usage.free)


class ServedIndexCache:
    """LRU eviction of this pod's served-index checkouts by disk usage."""

    def __init__(
        self,
        *,
        activated_repos_dir: Optional[PathLike],
        golden_repos_dir: Optional[PathLike],
        limits: ServedIndexCacheLimits = ServedIndexCacheLimits(),
        query_tracker: Optional[Any] = None,
        build_locked: Optional[Callable[[str], bool]] = None,
        lease_root: Optional[Path] = None,
        disk_usage: Optional[Callable[[], DiskUsage]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._activated_root = (
            Path(activated_repos_dir) if activated_repos_dir else None
        )
        self._golden_root = Path(golden_repos_dir) if golden_repos_dir else None
        self.limits = limits
        self._tracker = query_tracker
        self._build_locked = build_locked
        self._lease_root = lease_root
        disk_path = self._golden_root or self._activated_root or Path("/")
        self._disk_usage = disk_usage or (lambda: _disk_usage_of(disk_path))
        self._clock = clock
        self._lock = threading.Lock()
        self._evict_lock = threading.Lock()
        self._access: Dict[str, float] = {}
        self._stamped: Dict[str, float] = {}
        self._evicting: Set[str] = set()
        self._readers: Dict[str, List[int]] = {}
        self._sizes: Dict[str, _Sized] = {}
        self._last_size: Dict[str, int] = {}
        self._refused: Dict[str, Tuple[str, int]] = {}
        self._entries: List[ServedCheckout] = []
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.evicted_bytes = 0
        self.refusals = 0
        self.skips: Dict[str, int] = {}
        self.last_pass_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- reader hooks (index_materializer / temporal_index_store) -------------

    def note_hit(self, path: PathLike) -> bool:
        """A reader found ``path`` served. False while it is being evicted, or
        when an eviction that finished after the reader looked took its index
        away: the caller must take the checkout lock and look again.

        Authoritative because it is decided under ``self._lock``: an eviction
        marks ``_evicting`` under the same lock BEFORE it reads the access
        time, so either this hit is recorded first (the eviction sees it as
        recent and skips) or the eviction is visible here (marked, or the
        index root already renamed away)."""
        target = Path(path)
        key = str(target)
        fd: Optional[int] = None
        if _marker_of(_kind_of(target), target).is_file():
            try:
                fd = _lock_readers(target, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another process (or thread) is evicting it right now.
                return False
        try:
            now = self._clock()
            with self._lock:
                if key in self._evicting or not _holds_index(target):
                    return False
                self.hits += 1
                stamp_due = self._record_access_locked(key, now)
            if stamp_due:
                # Under the shared lock: an eviction in another process reads
                # this stamp only after it holds the lock exclusively.
                self._stamp(target)
            if fd is not None and _keep_in_scope(key, fd):
                fd = None  # held by the read scope until the read ends
            return True
        finally:
            if fd is not None:
                os.close(fd)  # releases the shared lock

    def hold_if_present(self, path: PathLike) -> bool:
        """For a read that must not check anything out (a diagnostics sweep
        over every repository): True when this pod holds the index at
        ``path`` -- then held, by the running read scope, until the read
        ends -- and False when it does not (evicted, never checked out, or
        being evicted right now). Records no access: looking is not serving."""
        target = Path(path)
        if not _holds_index(target):
            return False  # not here: create no readers lock file for it
        try:
            fd = _lock_readers(target, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if fd is None:
            return False
        with self._lock:
            present = str(target) not in self._evicting and _holds_index(target)
        if not present or not _keep_in_scope(str(target), fd):
            os.close(fd)
        return present

    def note_used(self, path: PathLike) -> None:
        """A build on this pod just used the index at ``path`` -- a refresh's
        incremental base, the publisher's ``golden-repos/<repo>``, which no
        query ever serves: without this its age is its first publish and it is
        always the first victim, though evicting it costs the next refresh a
        full rebuild. Recorded like a serve; not counted as a hit."""
        target = Path(path)
        with self._lock:
            stamp_due = self._record_access_locked(str(target), self._clock())
        if stamp_due:
            self._stamp(target)

    def stamp_interval(self) -> float:
        """How often a serve re-stamps a checkout: at most every
        ``STAMP_INTERVAL_SECONDS``, and at most a third of the idle floor, so
        a serve in ANOTHER process keeps it from eviction for at least two
        thirds of ``min_idle_seconds``."""
        return min(STAMP_INTERVAL_SECONDS, self.limits.min_idle_seconds / 3)

    # -- in-flight queries, across processes (QueryTracker hooks) -------------

    def hold_reader(self, path: str) -> None:
        """A query on this process started on ``path``: hold its readers lock
        SHARED until ``release_reader`` (an eviction in any process skips it)."""
        fd = _lock_readers(Path(path), fcntl.LOCK_SH)
        if fd is None:
            return
        with self._lock:
            self._readers.setdefault(path, []).append(fd)

    def release_reader(self, path: str) -> None:
        with self._lock:
            fds = self._readers.get(path)
            fd = fds.pop() if fds else None
            if fds == []:
                self._readers.pop(path, None)
        if fd is not None:
            os.close(fd)

    def _record_access_locked(self, key: str, now: float) -> bool:
        """Record an access (``self._lock`` held); True when the stamp is due."""
        self._access[key] = now
        stamp_due = now - self._stamped.get(key, 0.0) >= self.stamp_interval()
        if stamp_due:
            self._stamped[key] = now
        return stamp_due

    def _stamp(self, path: Path) -> None:
        kind = _kind_of(path)
        if not _marker_of(kind, path).is_file():
            return  # not a checkout: nothing of ours to order
        stamp = _stamp_of(kind, path)
        try:
            stamp.touch()
        except OSError as exc:
            logger.debug("Served-index cache: could not stamp %s (%s)", stamp, exc)

    def admit(self, path: PathLike, version: str) -> None:
        """Before a checkout of ``version`` to ``path`` (its checkout lock
        held): refuse a version already known to be too large, and evict
        others until the expected size fits under the high watermark."""
        key = str(Path(path))
        with self._lock:
            self.misses += 1
            refused = self._refused.get(key)
        if refused is not None and refused[0] == version:
            raise self._too_large(key, version, refused[1])
        with self._evict_lock:
            self._refresh()
            expected = self._expected_size(key)
            if self._over(self._limit_state(expected), high=True):
                self._evict_to_low(expected, "admission", exclude=Path(path))

    def admitted(
        self, path: PathLike, version: str, *, kind: str = KIND_GOLDEN
    ) -> None:
        """After a checkout of ``version`` to ``path`` (lock still held): a
        checkout larger than the cache may hold is removed and refused."""
        target = Path(path)
        key = str(target)
        if kind == KIND_TEMPORAL:
            size = tree_bytes(target)
        else:
            size = tree_bytes(target / INDEX_DIRNAME)
        ceiling = self.single_checkout_ceiling()
        if ceiling is not None and size > ceiling:
            if kind == KIND_TEMPORAL:
                purge(retire_temporal_root(target))
            else:
                purge(retire_pod_state(target))
            with self._lock:
                self._refused[key] = (version, size)
                self.refusals += 1
            raise self._too_large(key, version, size)
        now = self._clock()
        with self._lock:
            self._access[key] = now
            self._refused.pop(key, None)
        _hold_in_scope(target)

    def _too_large(self, key: str, version: str, size: int) -> ServedIndexTooLargeError:
        ceiling = self.single_checkout_ceiling()
        logger.error(
            "Served-index cache: refusing %s version %s: %d bytes alone exceed "
            "the cache limit of %s bytes",
            key,
            version,
            size,
            ceiling,
        )
        return ServedIndexTooLargeError(
            f"The index at {key} (version {version}) is {size} bytes, more than "
            f"this pod's served-index cache may hold ({ceiling} bytes: "
            "served_index_cache_max_bytes / served_index_cache_min_free_percent). "
            "Raise the limit or the pod's disk; it is not checked out again "
            "until a new version is published."
        )

    def single_checkout_ceiling(self) -> Optional[int]:
        """The most one checkout may take: the high byte watermark, and the
        disk less the free space the cache must leave."""
        bounds: List[int] = []
        if self.limits.max_bytes:
            bounds.append(self.limits.max_bytes)
        if self.limits.min_free_percent:
            total = self._disk_usage().total
            bounds.append(int(total * (1 - self.limits.min_free_percent / 100)))
        return min(bounds) if bounds else None

    def _expected_size(self, key: str) -> int:
        with self._lock:
            if key in self._last_size:
                return self._last_size[key]
            sizes = [s.size for s in self._sizes.values()]
        return sum(sizes) // len(sizes) if sizes else 0

    # -- accounting -----------------------------------------------------------

    def _refresh(self) -> List[ServedCheckout]:
        entries = discover_served_checkouts(self._activated_root, self._golden_root)
        live: Dict[str, _Sized] = {}
        for entry in entries:
            signature = _signature(entry)
            known = self._sizes.get(entry.key)
            if known is None or known.signature != signature:
                known = _Sized(_checkout_bytes(entry), signature)
            live[entry.key] = known
        with self._lock:
            self._sizes = live
            for key, sized in live.items():
                self._last_size[key] = sized.size
            self._entries = entries
        return entries

    def cache_bytes(self) -> int:
        with self._lock:
            return sum(s.size for s in self._sizes.values())

    def _limit_state(self, extra: int) -> Tuple[int, int, int]:
        usage = self._disk_usage()
        return self.cache_bytes() + extra, usage.free - extra, usage.total

    def _over(self, state: Tuple[int, int, int], *, high: bool) -> bool:
        used, free, total = state
        limits = self.limits
        byte_limit = limits.max_bytes if high else limits.low_bytes
        free_floor = limits.min_free_percent if high else limits.target_free_percent
        if limits.max_bytes and used > byte_limit:
            return True
        if limits.min_free_percent and total > 0 and free * 100 < free_floor * total:
            return True
        return False

    def last_access(self, entry: ServedCheckout) -> float:
        times = [
            max(_mtime(_marker_of(entry.kind, p)), _mtime(_stamp_of(entry.kind, p)))
            for p in entry.all_paths()
        ]
        with self._lock:
            times.extend(self._access.get(str(p), 0.0) for p in entry.all_paths())
        return max(times)

    # -- eviction ---------------------------------------------------------------

    def run_pass(self) -> EvictionReport:
        """One background pass: re-scan, and evict to the low watermark when
        over the high one."""
        with self._evict_lock:
            self._refresh()
            self.last_pass_at = self._clock()
            if not self._over(self._limit_state(0), high=True):
                return EvictionReport()
            return self._evict_to_low(0, "watermark")

    def _evict_to_low(
        self, extra: int, reason: str, exclude: Optional[Path] = None
    ) -> EvictionReport:
        report = EvictionReport()
        excluded = {str(exclude), str(exclude.parent)} if exclude is not None else set()
        with self._lock:
            entries = list(self._entries)
        ordered = sorted(
            (e for e in entries if not excluded.intersection(map(str, e.all_paths()))),
            key=self.last_access,
        )
        for entry in ordered:
            if not self._over(self._limit_state(extra), high=False):
                break
            try:
                freed = self._evict(entry, reason, report)
            except OSError as exc:
                # One victim that cannot be retired (permissions, a vanished
                # path) must not fail the caller's unrelated checkout nor
                # wedge every later pass: it is skipped, the next one tried.
                logger.warning(
                    "Served-index cache: could not evict %s (%s): %s",
                    entry.label,
                    entry.key,
                    exc,
                )
                self._skip(entry, SKIP_ERROR, report)
                continue
            if freed is not None:
                report.evicted.append(entry.key)
                report.freed_bytes += freed
        report.satisfied = not self._over(self._limit_state(extra), high=True)
        if not report.satisfied:
            logger.warning(
                "Served-index cache: still over the high watermark after %s "
                "eviction (cache %d bytes, %d protected checkouts: %s)",
                reason,
                self.cache_bytes(),
                len(report.skipped),
                sorted(set(report.skipped.values())),
            )
        return report

    def _skip(self, entry: ServedCheckout, why: str, report: EvictionReport) -> None:
        report.skipped[entry.key] = why
        with self._lock:
            self.skips[why] = self.skips.get(why, 0) + 1

    def _protected(self, entry: ServedCheckout) -> Optional[str]:
        """Why ``entry`` may not be evicted now (checked before its lock)."""
        if self._golden_root is not None and is_base_clone(
            entry.path, self._golden_root
        ):
            return SKIP_BASE_CLONE
        if any(is_pinned(p) for p in entry.all_paths()):
            return SKIP_PINNED
        if self._in_use(entry):
            return SKIP_IN_USE
        if entry.repo is not None and self._building(entry.repo):
            return SKIP_BUILDING
        if entry.versioned and self._leased(entry.path):
            return SKIP_LEASED
        return None

    def _in_use(self, entry: ServedCheckout) -> bool:
        if self._tracker is None:
            return False
        return int(self._tracker.get_ref_count(entry.key)) > 0

    def _building(self, repo: str) -> bool:
        if self._build_locked is None:
            return False
        try:
            return bool(self._build_locked(repo))
        except Exception as exc:
            logger.warning(
                "Served-index cache: cannot read the write lock of %s (%s); "
                "keeping its checkouts",
                repo,
                exc,
            )
            return True

    def _leased(self, path: Path) -> bool:
        if self._lease_root is None:
            return False
        from code_indexer.global_repos.snapshot_reader_lease import (
            snapshot_has_live_reader,
        )

        try:
            return snapshot_has_live_reader(str(path), lease_root=self._lease_root)
        except Exception as exc:
            logger.debug(
                "Served-index cache: lease state of %s unknown (%s)", path, exc
            )
            return True

    def _evict(
        self, entry: ServedCheckout, reason: str, report: EvictionReport
    ) -> Optional[int]:
        why = self._protected(entry)
        if why is not None:
            self._skip(entry, why, report)
            return None
        with self._all_locks(entry.all_paths()) as locked:
            if not locked:
                self._skip(entry, SKIP_BUSY, report)
                return None
            with _readers_free(entry.all_paths()) as free:
                if not free:  # a query holds it, in this process or another
                    self._skip(entry, SKIP_IN_USE, report)
                    return None
                return self._evict_locked(entry, reason, report)

    @contextmanager
    def _all_locks(self, paths: Sequence[Path]) -> Iterator[bool]:
        with try_exclusive_checkout(paths[0]) as first:
            if not first:
                yield False
                return
            if len(paths) == 1:
                yield True
                return
            with self._all_locks(paths[1:]) as rest:
                yield rest

    def _evict_locked(
        self, entry: ServedCheckout, reason: str, report: EvictionReport
    ) -> Optional[int]:
        key = entry.key
        paths = {str(p) for p in entry.all_paths()}
        with self._lock:
            self._evicting.update(paths)
        quiesced = False
        try:
            if any(is_pinned(p) for p in entry.all_paths()):
                self._skip(entry, SKIP_PINNED, report)
                return None
            if not _marker_of(entry.kind, entry.path).is_file():
                self._skip(entry, SKIP_GONE, report)
                return None
            idle = self._clock() - self.last_access(entry)
            if idle < self.limits.min_idle_seconds:
                self._skip(entry, SKIP_RECENT, report)
                return None
            if self._tracker is not None:
                # Another purge (a deactivation, the leftover sweep) already
                # holds the mark: never clear it from under that purge.
                if not self._tracker.try_mark_quiescing(key):
                    self._skip(entry, SKIP_QUIESCING, report)
                    return None
                quiesced = True
                if self._tracker.get_ref_count(key) > 0:
                    self._skip(entry, SKIP_IN_USE, report)
                    return None
            # Final pin re-check just before the irreversible retire — a pin
            # added between the first check and here would otherwise be missed.
            if any(is_pinned(p) for p in entry.all_paths()):
                self._skip(entry, SKIP_PINNED, report)
                return None
            with self._lock:
                size = self._sizes.get(key, _Sized(0)).size
            try:
                retired = self._retire(entry)
            except OSError:
                # Half retired: the index may already be renamed aside (so it
                # is not served -- ``is_served_here`` needs the index dir) with
                # its marker left behind. Free what was renamed; the stale
                # marker vouches for nothing and the next checkout rewrites it.
                _purge_renamed_aside(entry)
                for path in entry.all_paths():
                    _invalidate_caches(path)
                if not _holds_index(entry.path):
                    with self._lock:
                        self._sizes.pop(key, None)  # its bytes are gone
                raise
        finally:
            if quiesced and self._tracker is not None:
                self._tracker.clear_quiescing(key)
            with self._lock:
                self._evicting.difference_update(paths)
        for path in entry.all_paths():
            _invalidate_caches(path)
        purge_all(retired)
        with self._lock:
            self._sizes.pop(key, None)
            for path in entry.all_paths():
                self._access.pop(str(path), None)
                self._stamped.pop(str(path), None)
            self.evictions += 1
            self.evicted_bytes += size
        logger.info(
            "Served-index cache: evicted %s (%s) %d bytes, reason=%s, idle %.0fs",
            entry.label,
            key,
            size,
            reason,
            idle,
        )
        return size

    @staticmethod
    def _retire(entry: ServedCheckout) -> List[Optional[Path]]:
        if entry.kind == KIND_TEMPORAL:
            return [retire_temporal_root(entry.path)]
        # The target first: once it is not served, no reader starts on a member.
        return [retire_pod_state(path) for path in entry.all_paths()]

    # -- observability ----------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        try:
            usage: Optional[DiskUsage] = self._disk_usage()
        except OSError:
            usage = None
        with self._lock:
            lookups = self.hits + self.misses
            return {
                "entries": len(self._sizes),
                "cache_bytes": sum(s.size for s in self._sizes.values()),
                "disk_total_bytes": usage.total if usage else None,
                "disk_free_bytes": usage.free if usage else None,
                "max_bytes": self.limits.max_bytes,
                "low_bytes": self.limits.low_bytes,
                "min_free_percent": self.limits.min_free_percent,
                "target_free_percent": self.limits.target_free_percent,
                "hits": self.hits,
                "misses": self.misses,
                "hit_ratio": (self.hits / lookups) if lookups else 0.0,
                "evictions": self.evictions,
                "evicted_bytes": self.evicted_bytes,
                "refusals": self.refusals,
                "skips": dict(self.skips),
                "last_pass_at": self.last_pass_at,
            }

    # -- background loop --------------------------------------------------------

    def start(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        """Start the background pass every ``interval_seconds``.

        Raises:
            ValueError: ``interval_seconds`` is not positive (the loop would
                spin, re-scanning the disk without pause).
        """
        require_positive_interval(
            "served_index_cache_interval_seconds", interval_seconds
        )
        if self._thread is not None:
            return
        self._stop.clear()
        removed = purge_leftovers(self._activated_root, self._golden_root)
        if removed:
            logger.info("Served-index cache: removed %d interrupted evictions", removed)

        def loop() -> None:
            while True:
                try:
                    self.run_pass()
                except Exception:
                    logger.error("Served-index cache pass failed", exc_info=True)
                if self._stop.wait(interval_seconds):
                    return

        self._thread = threading.Thread(
            target=loop, name="served-index-cache", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def purge_all(retired: Sequence[Optional[Path]]) -> None:
    for path in retired:
        purge(path)


def _invalidate_caches(path: Path) -> None:
    """Close this process's cached handles on the evicted files: a deleted
    file still held open keeps its disk space."""
    from code_indexer.server.cache.snapshot_cache_invalidation import (
        invalidate_snapshot_caches,
    )

    try:
        invalidate_snapshot_caches(
            str(path),
            log_context="[served-index-evict]",
            is_versioned_snapshot_check=lambda _path: True,
        )
    except Exception as exc:
        logger.warning(
            "Served-index cache: cache invalidation for %s failed: %s", path, exc
        )


# ── the pod's cache (installed by index_store_wiring under delegated) ───────

_active: Optional[ServedIndexCache] = None


def install_served_index_cache(cache: Optional[ServedIndexCache]) -> None:
    """Install the pod's cache for the readers' hooks, and its in-flight
    reader locks into ``QueryTracker`` (cleared with None)."""
    from code_indexer.global_repos import query_tracker

    global _active
    _active = cache
    if cache is None:
        query_tracker.set_reader_hooks(None)
    else:
        query_tracker.set_reader_hooks((cache.hold_reader, cache.release_reader))


def active_served_index_cache() -> Optional[ServedIndexCache]:
    """The running pod's cache; None under the local store, the CLI and tests."""
    return _active


__all__ = [
    "CarriedReads",
    "DiskUsage",
    "ServedCheckout",
    "ServedIndexCache",
    "ServedIndexCacheLimits",
    "ServedIndexTooLargeError",
    "active_served_index_cache",
    "carry_served_index_reads",
    "discover_served_checkouts",
    "install_served_index_cache",
    "iter_activated_checkouts",
    "iter_golden_checkouts",
    "iter_temporal_checkouts",
    "pinned_served_index",
    "readers_free",
    "require_positive_interval",
    "retire_pod_state",
    "retire_temporal_root",
]
