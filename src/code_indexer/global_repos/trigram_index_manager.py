"""Trigram inverted index for index-assisted regex search.

Stores, per repository, a mapping ``trigram -> files containing it`` in a SQLite
database under ``<repo>/.code-indexer/trigram_index/``. Given a set of trigrams
that a regex match must contain (see :mod:`regex_trigram`), the index returns the
small set of candidate files, which ripgrep then searches precisely.

Correctness contract: :meth:`query` must return a SUPERSET of the files that
could contain a match. Files that could not be trigram-indexed (unreadable,
binary, decode errors) are recorded as "always candidates" so they are never
silently excluded.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..utils.index_store import INDEX_DIRNAME, index_root
from .regex_trigram import trigrams

logger = logging.getLogger(__name__)


class TrigramIndexTimeoutError(Exception):
    """Raised by :meth:`TrigramIndexManager.exists`/:meth:`.query` when the
    underlying sqlite3 work exceeds the caller's ``timeout_seconds`` budget.

    Bug #1590: exists()/query() previously had NO timeout at all -- a raw
    sqlite3.connect() + read against an index that (in cluster mode) lives
    on shared storage could block Phase 1 of an xray_search/xray_explore
    job indefinitely, holding one of only 4 global xray concurrency slots
    for the entire hang. This is a genuine DEADLINE OVERRUN, distinct from
    "index absent" or "index corrupt" -- both of which remain safe,
    silent full-scan fallbacks (see the sqlite3.OperationalError /
    sqlite3.DatabaseError handling in exists()/query()) -- so callers must
    treat it differently: it means Phase 1 itself must stop waiting, not
    merely that the pre-filter optimization is unavailable this call.
    """


class _TrigramConnectionHolder:
    """Thread-safe one-slot holder for a watchdog worker's live sqlite3
    connection.

    Mirrors scip/database/queries.py's ``_ConnectionHolder`` (Bug #1603) --
    the same watchdog idiom reused here. ``interrupt()`` may run
    concurrently with ``publish()`` from the watchdog thread, so both are
    lock-protected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Optional["sqlite3.Connection"] = None
        self._cancelled = False

    def publish(self, conn: "sqlite3.Connection") -> None:
        """Called by the watchdog worker thread immediately after connecting."""
        with self._lock:
            self._conn = conn

    def is_cancelled(self) -> bool:
        """True once interrupt() has fired, regardless of publish timing."""
        with self._lock:
            return self._cancelled

    def interrupt(self, warning_label: str) -> None:
        """Cancel the published connection's in-flight query, if any, and
        record that a timeout occurred regardless.

        Never raises: the worker may have already finished and closed its
        own connection in the tiny window since the caller's is_alive()
        check, or may not have published yet (e.g. a still-blocked
        sqlite3.connect() call -- interrupt() has no effect on a
        connection that does not exist yet, which is exactly why
        is_cancelled() exists as a second, always-checkable signal).
        """
        with self._lock:
            self._cancelled = True
            conn = self._conn
        if conn is None:
            return
        try:
            conn.interrupt()
        except Exception as interrupt_exc:  # noqa: BLE001
            logger.debug(
                "%s: conn.interrupt() on timeout failed (worker likely "
                "already finished): %s",
                warning_label,
                interrupt_exc,
            )


def _run_with_thread_watchdog(
    work: Callable[[], Any],
    timeout_seconds: float,
    warning_label: str,
    holder: Optional[_TrigramConnectionHolder] = None,
) -> Tuple[Any, bool]:
    """Run ``work`` (a zero-arg callable) on a daemon thread with a
    join-based timeout.

    Bug #1590: reuses the exact idiom established for SCIP query timeouts
    (Bug #1603, scip/database/queries.py's ``_run_with_thread_watchdog``):
    ``signal.alarm()`` only delivers SIGALRM to the main thread of the main
    interpreter; ``threading.Thread.join(timeout=...)`` has no such
    restriction.

    ``holder`` (optional; ``work`` stays a zero-arg callable) mirrors
    scip/database/queries.py's ``conn_holder: Optional[_ConnectionHolder]
    = None`` -- it lets a caller with a live sqlite3 connection enable
    real cancellation via its ``.interrupt()`` on timeout. Round 5 LOW
    note: made optional so a pure filesystem/path-resolve caller with
    nothing to cancel doesn't need a throwaway holder just to satisfy the
    signature; the worker thread is still abandoned identically either
    way.

    Returns ``(result, timed_out)``. On timeout the underlying worker
    thread is ABANDONED (Python cannot forcibly kill a thread) -- a
    genuinely wedged call (e.g. against an unresponsive NFS mount) leaks
    one OS thread rather than truly stopping. This unblocks the caller
    well within timeout_seconds regardless.
    """
    result_holder: List[Any] = [None]
    exc_holder: List[Optional[BaseException]] = [None]

    def _target() -> None:
        try:
            result_holder[0] = work()
        except BaseException as e:  # noqa: BLE001
            exc_holder[0] = e

    t = threading.Thread(target=_target, daemon=True, name=f"{warning_label}-watchdog")
    t.start()
    t.join(timeout=timeout_seconds if timeout_seconds > 0 else 0)

    if t.is_alive():
        logger.warning(
            "%s: exceeded %.3fs timeout; abandoning the blocked worker thread",
            warning_label,
            timeout_seconds,
        )
        if holder is not None:
            holder.interrupt(warning_label)
        return None, True

    if exc_holder[0] is not None:
        raise exc_holder[0]

    return result_holder[0], False


# Maximal runs of printable ASCII (plus tab/newline/CR) of length >= 3. Trigrams
# are extracted only from these runs, so a binary file (e.g. a .class) is indexed
# by its embedded text -- exactly the content ripgrep can match -- without the
# dense noise trigrams of its raw bytes bloating the index.
_PRINTABLE_RUN = re.compile(rb"[\t\n\r\x20-\x7e]{3,}")

_DB_NAME = "trigrams.db"
# Build temp files are named ``trigrams.<random>.db.building`` (see build()).
_TEMP_PREFIX = "trigrams."
_TEMP_SUFFIX = ".db.building"
# Bump whenever the on-disk index schema changes (a new table/column, different
# posting semantics, ...). An index whose stamped version differs is treated as
# absent by exists(), so a stale/old-format index (e.g. a golden-repo refresh
# swapped the alias to a snapshot indexed by an older build) yields a clean
# full-scan + rebuild instead of a caught "no such column" query error.
_SCHEMA_VERSION = 1
# ^ why hex of the name bytes, not TEXT; sqlite refuses a surrogate-escaped str. An older index lacks the table = no such names
_UNSTORABLE_TABLE = "unstorable_names"


def _unstorable_names(conn: "sqlite3.Connection") -> List[str]:
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_UNSTORABLE_TABLE,),
    ).fetchone()
    if present is None:
        return []
    return [
        os.fsdecode(bytes.fromhex(row[0]))
        for row in conn.execute(f"SELECT name_hex FROM {_UNSTORABLE_TABLE}")
    ]


# Skip trigram extraction for files larger than this (still recorded as an
# always-candidate so matches inside them are never missed). Keeps build I/O and
# db size bounded; large files are rare and ripgrep handles them in the pass.
_MAX_INDEX_BYTES = 5 * 1024 * 1024
_INSERT_BATCH = 5000
# Commit (and fsync) every N files so dirty database pages are flushed and
# reclaimed during a large build instead of accumulating against the container
# memory limit.
_COMMIT_EVERY_FILES = 2000
# A build temp untouched for this long is a leftover of a dead build. A live
# build modifies its temp throughout: every _COMMIT_EVERY_FILES commit plus
# page-cache spills while loading, then index/table writes. The longest silent
# stretch is the external sort inside CREATE INDEX idx_postings_tc, which
# writes only SQLite's own temp files: measured at 11.9 s for a 0.26 GB and
# 36.6 s for a 1.05 GB loaded postings table (~35 s/GB, local disk). For the
# largest known index (~32 GB of postings, ~19 min of silent sort) six hours is
# a ~19x margin. The costs are asymmetric, so the margin is not larger:
# wrongly removing a live build's temp only fails that build (the published
# index is untouched and the next refresh rebuilds it), while keeping a dead
# build's temp (up to tens of GB per killed build) until the threshold passes
# lets repeated deploy restarts refill the disk before cleanup can run.
_STALE_BUILD_FILE_AGE_SECONDS = 6 * 60 * 60


def trigram_prefilter_available() -> bool:
    """Whether a trigram index can be built and trusted on this server.

    With a local content backend the index is built by listing (``rg
    --files``) and reading every file on THIS disk. Under a delegated content
    backend the repository is not here, so that build would list nothing and an
    index built while the files WERE here would steer searches at paths this
    pod cannot read. There the pre-filter exists only when ripgrep is delegated
    too: refresh then builds the index from ``rg --files`` and bytes fetched
    where the repository is (:func:`build_trigram_index_through_backends`), and
    the candidates go back there as explicit ``/content/rg`` paths. False when
    the setting cannot be read -- a missing pre-filter costs time, a wrong one
    costs results.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        content_backend_name_or_none,
    )

    name = content_backend_name_or_none()
    if name == "local":
        return True
    return name == "delegated" and delegated_trigram_backends() is not None


def delegated_trigram_backends() -> Optional[Tuple[Any, Any]]:
    """``(content backend, ripgrep backend)`` when BOTH are delegated on the
    running server -- the pair a trigram index is built through and searched
    with on a pod that does not hold the repositories -- else None."""
    from code_indexer.server.startup.content_backend_wiring import (
        server_content_backend,
    )
    from code_indexer.server.startup.ripgrep_backend_wiring import (
        server_ripgrep_backend,
    )

    content = server_content_backend()
    ripgrep = server_ripgrep_backend()
    if content is None or content.is_local or ripgrep is None or ripgrep.is_local:
        return None
    return content, ripgrep


#: ``rg --files`` output above this is refused rather than truncated: a
#: truncated listing would make the index a SUBSET of what ripgrep searches.
_RG_FILES_MAX_OUTPUT_BYTES = 256 * 1024 * 1024
_RG_FILES_TIMEOUT_SECONDS = 600.0
#: The ceilings a DELEGATED ripgrep backend accepts: it sends the run to
#: workspace-mcp's ``POST /content/rg`` as ``timeout_ms`` / ``max_output_bytes``,
#: and workspace-mcp rejects anything over ``CONTENT_RG_MAX_TIMEOUT_MS``
#: (300 000 ms) or ``CONTENT_RG_MAX_OUTPUT_BYTES`` (64 MiB, content-rg.service.ts)
#: with a 400 -- which failed EVERY delegated trigram build while the listing
#: asked for 600 s / 256 MiB (EVO-64616). Applied only at that boundary
#: (:func:`_listing_limits`); a local rg keeps the full budget.
_DELEGATED_RG_MAX_TIMEOUT_SECONDS = 300.0
_DELEGATED_RG_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
#: Directories the change-detection manifest skips: ``rg --files`` never lists
#: them (``.git`` and cidx's own hidden index state).
_MANIFEST_PRUNE_DIRS: Tuple[str, ...] = (INDEX_DIRNAME, ".git")


class TrigramListingError(RuntimeError):
    """``rg --files`` through the ripgrep backend did not produce a complete
    listing, so no index may be built from it."""


#: The listing argv. workspace-mcp's ``/content/rg`` admits only the regex
#: search's own shape (``--json -e PATTERN ... -- PATH``), never ``--files``,
#: so the walk is listed by a search that matches each searchable file exactly
#: once: ``\A[\s\S]`` in multiline mode is the file's first character. Its
#: ``begin`` events are the walk -- ripgrep's own gitignore, hidden-file and
#: binary rules, which explicit candidate paths would otherwise bypass. An
#: empty file is not listed, and cannot hold a match either.
_LISTING_PATTERN = r"\A[\s\S]"


def _listing_argv(root: Path, paths_only: bool) -> List[str]:
    """The listing search's argv: ``--files-with-matches`` prints just the paths of
    the files that match, ``--json`` also carries the matched text of every file
    (205 MB for a repository of 28 thousand files, where the paths are 2.4 MB)."""
    return [
        "rg",
        "--files-with-matches" if paths_only else "--json",
        "--multiline",
        "-e",
        _LISTING_PATTERN,
        "-g",
        f"!{INDEX_DIRNAME}/**",
        "-g",
        "!.git/**",
        "--",
        str(root),
    ]


def _json_text(value: Any) -> Optional[str]:
    """ripgrep's ``{"text": ...}`` or base64 ``{"bytes": ...}`` as a str."""
    import base64

    if not isinstance(value, dict):
        return None
    if isinstance(value.get("text"), str):
        return str(value["text"])
    if isinstance(value.get("bytes"), str):
        raw = base64.b64decode(value["bytes"])
        return raw.decode("utf-8", errors="surrogateescape")
    return None


def _listed_paths(output_file: str, root: Path) -> List[str]:
    """Repo-relative paths of the ``begin`` events in a listing run's output."""
    import json

    root_text = os.path.normpath(str(root))
    paths: List[str] = []
    with open(output_file, "rb") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise TrigramListingError(f"unparseable listing output: {exc}") from exc
            if not isinstance(event, dict) or event.get("type") != "begin":
                continue
            path = _json_text((event.get("data") or {}).get("path"))
            if path is None:
                raise TrigramListingError("listing event without a path")
            paths.append(_repo_relative(path, root_text))
    return paths


def _repo_relative(path: str, root_text: str) -> str:
    """*path* as listed by ripgrep, relative to the repository root; a path that
    is not under it is refused (the index would name files it cannot serve)."""
    rel = os.path.relpath(os.path.normpath(path), root_text)
    if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
        raise TrigramListingError(f"listed path outside the root: {path!r}")
    return rel.replace(os.sep, "/")


def _listed_paths_only(output_file: str, root: Path) -> List[str]:
    """Repo-relative paths of a ``--files-with-matches`` run: one per line."""
    root_text = os.path.normpath(str(root))
    paths: List[str] = []
    with open(output_file, "rb") as fh:
        for raw in fh:
            line = raw.rstrip(b"\r\n")
            if line:
                paths.append(
                    _repo_relative(
                        line.decode("utf-8", errors="surrogateescape"), root_text
                    )
                )
    return paths


def _listing_limits(ripgrep_backend: Any) -> Tuple[float, int]:
    """The listing's ``(timeout_seconds, max_output_bytes)`` for
    *ripgrep_backend*: the full budget for a local rg, capped at
    workspace-mcp's ``/content/rg`` limits otherwise (a backend that does not
    say it is local is treated as delegated)."""
    if getattr(ripgrep_backend, "is_local", False) is True:
        return _RG_FILES_TIMEOUT_SECONDS, _RG_FILES_MAX_OUTPUT_BYTES
    return (
        min(_RG_FILES_TIMEOUT_SECONDS, _DELEGATED_RG_MAX_TIMEOUT_SECONDS),
        min(_RG_FILES_MAX_OUTPUT_BYTES, _DELEGATED_RG_MAX_OUTPUT_BYTES),
    )


class _PathsOnlyListingRefused(TrigramListingError):
    """The ripgrep backend could not run the paths-only listing at all (a
    transport or protocol failure, no exit code): a server that predates it
    answers 400. The JSON listing is tried instead."""


def list_ripgrep_files(ripgrep_backend: Any, root: Path) -> List[str]:
    """Exactly the files ripgrep searches at *root*, listed WHERE ripgrep runs.

    Candidates go back to ripgrep as explicit paths, which bypass its ignore
    rules, so the index must cover exactly ripgrep's own walk -- this asks
    ripgrep for it (see ``_LISTING_PATTERN``). A run that timed out, was
    capped or failed raises: a partial listing would make the index a SUBSET
    of what ripgrep searches, and silently drop matches.

    The listing prints only the paths (``--files-with-matches``). A backend that
    cannot run that -- a workspace-mcp that predates it -- gets the ``--json``
    listing instead, which selects the same files but is far larger.
    """
    try:
        return _list_ripgrep_files(ripgrep_backend, root, paths_only=True)
    except _PathsOnlyListingRefused as exc:
        _log_paths_only_refused_once(exc)
    return _list_ripgrep_files(ripgrep_backend, root, paths_only=False)


_paths_only_refusal_logged = False


def _log_paths_only_refused_once(exc: Exception) -> None:
    """Said once per process, not once per repository per refresh."""
    global _paths_only_refusal_logged
    if _paths_only_refusal_logged:
        return
    _paths_only_refusal_logged = True
    logger.warning(
        "The paths-only trigram listing was refused (%s); using the --json "
        "listing, whose output can exceed the delegated size cap on a large "
        "repository",
        exc,
    )


def _list_ripgrep_files(
    ripgrep_backend: Any, root: Path, *, paths_only: bool
) -> List[str]:
    import asyncio
    import concurrent.futures

    timeout_seconds, max_output_bytes = _listing_limits(ripgrep_backend)
    fd, out_name = tempfile.mkstemp(prefix="rg_listing_", suffix=".jsonl")
    os.close(fd)
    try:

        def _run() -> Any:
            return asyncio.run(
                ripgrep_backend.run(
                    argv=_listing_argv(root, paths_only),
                    working_dir=str(root),
                    output_file_path=out_name,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                )
            )

        # A fresh thread owns the event loop: the caller may itself be running
        # inside one (asyncio.run refuses to nest).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_run).result()
        if result.timed_out or result.output_capped:
            raise TrigramListingError(
                "ripgrep listing did not complete (timed out or output capped)"
            )
        if result.found_no_matches:
            return []
        if result.is_error:
            message = (
                f"ripgrep listing failed: exit_code={result.exit_code} "
                f"{(result.stderr_output or result.error_message)[:300]}"
            )
            if paths_only and result.exit_code is None:
                raise _PathsOnlyListingRefused(message)
            raise TrigramListingError(message)
        if paths_only:
            try:
                return _listed_paths_only(out_name, root)
            except TrigramListingError as exc:
                # One path per line cannot carry every name (a newline in one);
                # the JSON listing can, and refuses what is truly outside the root.
                raise _PathsOnlyListingRefused(f"unreadable listing: {exc}") from exc
        return _listed_paths(out_name, root)
    finally:
        try:
            os.unlink(out_name)
        except OSError:
            pass


def manifest_facts(content_backend: Any, root: Path) -> Dict[str, ManifestFacts]:
    """Per-path size, mtime and (when sent) blob hash, without hashing.

    Asks for ``hash: false`` -- change detection needs size and mtime, and the
    hash is the expensive part of a manifest -- through
    ``query_manifest_pages``, so a backend that cannot skip it still works.
    """
    from code_indexer.server.storage.shared.content_backend import (
        query_manifest_pages,
    )

    facts: Dict[str, ManifestFacts] = {}
    for page in query_manifest_pages(
        content_backend, str(root), prune_dirs=_MANIFEST_PRUNE_DIRS, want_hash=False
    ):
        for entry in page.entries:
            if entry.is_symlink:
                continue
            facts[entry.path] = ManifestFacts(
                size=int(entry.size or 0),
                mtime_ns=entry.mtime_ns,
                blob_sha=entry.blob_sha,
            )
    return facts


def build_trigram_index_through_backends(
    index_dir: Path, content_backend: Any, ripgrep_backend: Any, root: Path
) -> DelegatedBuildStats:
    """Build *index_dir*'s trigram index for the repository at *root* without
    it being on this pod: list with ripgrep where it is, fetch only the files
    whose size/mtime changed since the index on this pod was built."""
    files = list_ripgrep_files(ripgrep_backend, root)
    facts = manifest_facts(content_backend, root)
    return TrigramIndexManager(index_dir).build_delegated(
        content_backend, files, root, facts
    )


def trigram_index_dir(repo_path: Path) -> Path:
    """Return the trigram index directory of the repository at ``repo_path``."""
    return index_root(repo_path) / "trigram_index"


def trigram_index_present(repo_path: Path) -> bool:
    """True when ``repo_path`` has a populated, current-schema trigram index."""
    return TrigramIndexManager(trigram_index_dir(repo_path)).exists()


def remove_leftover_build_files(
    index_dir: Path, min_age_seconds: float = _STALE_BUILD_FILE_AGE_SECONDS
) -> int:
    """Remove STALE build temp files (``trigrams.*.db.building``) in ``index_dir``.

    A build killed before it could publish or clean up (deploy restart,
    SIGKILL) leaves its temp behind; a large repository's temp can be tens of
    GB, and a versioned snapshot of the directory would copy and pin it.

    Liveness is decided lock-free, by staleness: a live build keeps modifying
    its temp (see ``_STALE_BUILD_FILE_AGE_SECONDS``), so only a temp whose
    mtime is at least ``min_age_seconds`` old can be a leftover. That makes
    this safe for any caller on any node, whether or not it holds a lock and
    whichever process -- refresh or lazy build, local or remote -- owns a
    concurrent build in the same directory.

    Returns the number of files removed; a missing directory removes nothing.
    Any other ``OSError`` propagates to the caller.
    """
    try:
        entries = list(os.scandir(index_dir))
    except FileNotFoundError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:  # bounded: one pass over a finite directory listing
        name = entry.name
        if not (name.startswith(_TEMP_PREFIX) and name.endswith(_TEMP_SUFFIX)):
            continue
        try:
            st = entry.stat(follow_symlinks=False)  # lstat: never follow a link
            if not stat.S_ISREG(st.st_mode):
                continue  # only regular files are build temps
            if now - st.st_mtime < min_age_seconds:
                continue  # recently written: may belong to a live build
            os.unlink(entry.path)
        except FileNotFoundError:
            continue  # already removed by another cleanup
        removed += 1
    if removed:
        logger.info(
            "TrigramIndexManager: removed %d leftover build file(s) from %s",
            removed,
            index_dir,
        )
    return removed


def _data_trigrams(data: bytes) -> Set[str]:
    """Trigrams of *data*'s printable text runs, lower-cased (see the build)."""
    tris: Set[str] = set()
    for m in _PRINTABLE_RUN.finditer(data):
        tris |= trigrams(m.group().decode("ascii").lower())
    return tris


@dataclass(frozen=True)
class ManifestFacts:
    """What the content manifest says about one file, for change detection."""

    size: int
    mtime_ns: Optional[int]
    blob_sha: Optional[str]

    @property
    def change_key(self) -> Optional[str]:
        """Equal across two builds only if the file is unchanged: size and
        mtime when the manifest has an mtime, else the blob hash, else None
        (unknown -- always fetched)."""
        if self.mtime_ns is not None:
            return f"s{self.size}:m{self.mtime_ns}"
        if self.blob_sha:
            return f"b{self.blob_sha}"
        return None


@dataclass(frozen=True)
class DelegatedBuildStats:
    """How a delegated build got its files."""

    files: int
    carried: int
    fetched: int
    oversized: int


class TrigramIndexManager:
    """Build and query a per-repository trigram inverted index."""

    def __init__(self, index_dir: Path) -> None:
        self._dir = Path(index_dir)
        self._db_path = self._dir / _DB_NAME

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._db_path

    def exists(self, timeout_seconds: Optional[float] = None) -> bool:
        """True when a populated, CURRENT-schema index database is present.

        An index whose stamped ``schema_version`` differs from this build's (or
        that predates the ``meta`` stamp) is reported as absent, so the caller
        full-scans and a rebuild regenerates it -- rather than the query failing
        on a missing table/column and silently degrading forever.

        Bug #1590 (AC1/AC7): when ``timeout_seconds`` is given, the sqlite3
        connect+read is bounded by a thread-based watchdog (see
        ``_run_with_thread_watchdog``) -- raises ``TrigramIndexTimeoutError``
        on a genuine deadline overrun rather than blocking Phase 1 forever.
        When omitted (default ``None``), behavior is unchanged: unbounded,
        for callers with no deadline of their own. A genuinely CORRUPT
        index file (e.g. "file is not a database") is distinguished from a
        routine missing-table/stale-schema index via ``sqlite3.DatabaseError``
        vs. the more specific ``sqlite3.OperationalError`` -- corruption is
        logged at ERROR (not silently folded into the same "absent, will
        rebuild" path a normal schema migration takes).

        Bug #1590 review round 3 finding B2: the ``self._db_path.exists()``
        presence check runs INSIDE ``_work()`` (not before it) specifically
        so it is covered by the same watchdog as the sqlite3 calls below --
        on the `hard` NFSv3 golden-repo mount this is a real ``os.stat()``
        that can block in uninterruptible kernel retry and never return.
        Checking it before the watchdog is armed would recreate exactly the
        unbounded-hang class this whole method exists to eliminate.
        """

        def _work() -> bool:
            if not self._db_path.exists():
                return False
            # Issue #1459 code-review sweep: a naive f"file:{path}?mode=ro"
            # string mis-parses any path containing a URI-special character
            # ('?', '#', '%', spaces) -- SQLite's URI parser reads a literal
            # '?'/'#' in the path as the start of the query string, truncating
            # the path before "mode=ro" is even seen. Path.resolve().as_uri()
            # produces a correctly percent-encoded file:// URI (same fix as
            # storage/sqlite_chunk_store.py's ChunkStore._open_connection /
            # chunk_store_has_real_data).
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                holder.publish(conn)
                if holder.is_cancelled():
                    raise TrigramIndexTimeoutError(
                        f"trigram index existence check for {self._db_path} "
                        f"cancelled after timeout"
                    )
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if not row or row[0] != _SCHEMA_VERSION:
                    return False  # missing stamp or wrong version -> rebuild
                populated = conn.execute("SELECT COUNT(*) FROM files").fetchone()
                return bool(populated and populated[0] > 0)

        holder = _TrigramConnectionHolder()
        try:
            if timeout_seconds is not None:
                result, timed_out = _run_with_thread_watchdog(
                    _work, timeout_seconds, "trigram_index_exists", holder
                )
                if timed_out:
                    raise TrigramIndexTimeoutError(
                        f"trigram index existence check for {self._db_path} "
                        f"exceeded {timeout_seconds}s timeout"
                    )
                return bool(result)
            return _work()
        except sqlite3.OperationalError as exc:
            # Missing tables / pre-stamp format -> routine stale-schema
            # case, expected to self-heal via rebuild. NOT corruption.
            #
            # KNOWN LIMITATION (review round 2 finding F5, deliberately
            # deferred): SQLITE_IOERR / SQLITE_CANTOPEN (genuine disk/mount
            # failures, e.g. a wedged NFS mount) ALSO raise
            # sqlite3.OperationalError, so they currently land in this same
            # routine DEBUG bucket rather than being flagged as an error. A
            # precise fix would inspect exc.sqlite_errorcode, but that
            # attribute is Python 3.11+ only (this project's CI `lint` job
            # and mypy target pin Python 3.9 -- see CLAUDE.md's CI section),
            # so a version-gated workaround was judged not worth the added
            # complexity for this low-priority gap; noted here for a future
            # fix once the minimum Python version moves past 3.9.
            logger.debug(
                "trigram index at %s uses a stale/incompatible schema (%s); "
                "treating as absent -- caller will full-scan and rebuild",
                self._db_path,
                exc,
            )
            return False
        except (sqlite3.DatabaseError, sqlite3.InterfaceError) as exc:
            # Bug #1590 AC7: genuine corruption (e.g. "file is not a
            # database", "database disk image is malformed") -- distinct
            # from the routine stale-schema case above. Still falls back
            # to "absent" (safe: caller full-scans), but surfaced loudly
            # so a corrupt index is operationally visible. Review round 2
            # finding F5: sqlite3.InterfaceError is a SIBLING branch of
            # the exception hierarchy (Error -> InterfaceError), NOT a
            # DatabaseError subclass -- included here so it degrades
            # gracefully too, instead of relying solely on the sole
            # caller's own broad except to avoid crashing.
            logger.error(
                "trigram index at %s appears CORRUPT (%s); treating as "
                "absent and falling back to a full scan -- this is NOT a "
                "routine cold-start/stale-schema condition",
                self._db_path,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def build(self, repo_path: Path, file_list: Optional[Iterable[str]] = None) -> int:
        """Build (or rebuild) the index for ``repo_path``.

        ``file_list`` is an optional iterable of repo-relative file paths (e.g.
        the set the indexer already enumerated). When omitted, files are listed
        with ``rg --files`` so the set matches exactly what ripgrep searches.
        Returns the number of files recorded.
        """
        repo_path = Path(repo_path)
        rel_files = (
            list(file_list)
            if file_list is not None
            else self._enumerate_files(repo_path)
        )
        rows = ((rel, *self._file_trigrams(repo_path / rel)) for rel in rel_files)
        count = self._write_index(rows)
        logger.info(
            "TrigramIndexManager: built index for %s (%d files) at %s",
            repo_path,
            count,
            self._db_path,
        )
        return count

    def _write_index(
        self,
        rows: Iterable["tuple[str, Set[str], bool]"],
        *,
        carry_from: Optional[Path] = None,
        carry_paths: Optional[Iterable[str]] = None,
        change_keys: Optional[Dict[str, str]] = None,
    ) -> int:
        """Write a new index from ``(path, trigrams, indexed)`` rows and publish it.

        ``carry_from``/``carry_paths``: an earlier index whose rows for exactly
        those paths (unchanged files) are copied over with their postings, so
        their bytes need not be read again. ``change_keys`` is stored per path
        (``file_keys``) for the next incremental build to compare against; an
        index without it simply carries nothing forward. Neither changes what
        :meth:`exists` or :meth:`query` read, so the schema version stands.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        # Build into a UNIQUE temp file (not a fixed ".db.building" name): in
        # cluster mode this index dir lives on shared NFS under the golden repo,
        # so a lazy rebuild on another pod -- or a scheduled refresh racing a
        # lazy build -- can target it concurrently. A shared temp name let one
        # build's unlink()/write corrupt the other and publish a
        # partially-populated index that passes exists() and silently drops
        # matches. With a unique temp per build, os.replace still atomically
        # publishes the last writer and neither build damages the other's file.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._dir), prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX
        )
        os.close(fd)
        tmp_path = Path(tmp_name)

        conn = sqlite3.connect(str(tmp_path))
        try:
            # Memory-frugal build: a bounded page cache, disk-backed temp store
            # (so the final index sort spills to disk instead of RAM), no rollback
            # journal (we publish atomically via a temp file), and periodic
            # commits that fsync so dirty db pages are flushed and reclaimed
            # instead of accumulating against the container memory limit.
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-8000")  # ~8 MB
            conn.execute("PRAGMA temp_store=FILE")
            conn.execute("PRAGMA mmap_size=0")
            conn.executescript(
                """
                CREATE TABLE files (
                    id      INTEGER PRIMARY KEY,
                    path    TEXT NOT NULL,
                    indexed INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE postings (
                    trigram TEXT NOT NULL,
                    file_id INTEGER NOT NULL
                );
                """
            )
            count = 0
            if carry_from is not None and carry_paths is not None:
                count = self._carry_rows(conn, carry_from, carry_paths)
            count = self._insert_rows(conn, rows, count)
            self._finalize(conn, count, change_keys)
        except BaseException:
            # A failed build must not leave its half-written temp behind. Each
            # build's temp is unique, so this only ever removes our own file --
            # never a concurrent build's in-progress database.
            conn.close()
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        finally:
            conn.close()

        os.replace(tmp_path, self._db_path)  # atomic publish
        return count

    @staticmethod
    def _carry_rows(
        conn: "sqlite3.Connection", previous: Path, paths: Iterable[str]
    ) -> int:
        """Copy *paths*' rows and postings from the index at *previous*.

        One sequential pass over the old postings (a join on path), never a
        per-file lookup: the old index has no ``file_id`` index to seek by.
        Returns the number of files carried; new ids follow on from them.
        """
        conn.execute("ATTACH DATABASE ? AS prev", (str(previous),))
        try:
            conn.execute("CREATE TEMP TABLE carry (path TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT OR IGNORE INTO carry (path) VALUES (?)",
                ((p,) for p in paths),
            )
            conn.execute(
                "INSERT INTO files (path, indexed) "
                "SELECT f.path, f.indexed FROM prev.files f "
                "JOIN carry c ON c.path = f.path ORDER BY f.id"
            )
            # A path index for the id mapping below, dropped again so the
            # published index is the same shape as a full build's.
            conn.execute("CREATE INDEX carry_files_path ON files(path)")
            conn.execute(
                "CREATE TEMP TABLE id_map (old INTEGER PRIMARY KEY, new INTEGER)"
            )
            conn.execute(
                "INSERT INTO id_map (old, new) "
                "SELECT pf.id, f.id FROM prev.files pf JOIN main.files f "
                "ON f.path = pf.path"
            )
            conn.execute(
                "INSERT INTO postings (trigram, file_id) "
                "SELECT p.trigram, m.new FROM prev.postings p "
                "JOIN id_map m ON m.old = p.file_id"
            )
            carried = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.commit()
            conn.execute("DROP TABLE carry")
            conn.execute("DROP TABLE id_map")
            conn.execute("DROP INDEX carry_files_path")
        finally:
            conn.execute("DETACH DATABASE prev")
        return int(carried)

    @staticmethod
    def _insert_rows(
        conn: "sqlite3.Connection",
        rows: Iterable["tuple[str, Set[str], bool]"],
        count: int,
    ) -> int:
        """Insert ``(path, trigrams, indexed)`` rows after the first *count* ids."""
        batch: List[tuple] = []
        added = 0
        for rel, tris, indexed in rows:
            file_id = count + 1
            conn.execute(
                "INSERT INTO files (id, path, indexed) VALUES (?, ?, ?)",
                (file_id, rel, 1 if indexed else 0),
            )
            for t in tris:
                batch.append((t, file_id))
                if len(batch) >= _INSERT_BATCH:
                    conn.executemany(
                        "INSERT INTO postings (trigram, file_id) VALUES (?, ?)",
                        batch,
                    )
                    batch.clear()
            count += 1
            added += 1
            if added % _COMMIT_EVERY_FILES == 0:
                if batch:
                    conn.executemany(
                        "INSERT INTO postings (trigram, file_id) VALUES (?, ?)",
                        batch,
                    )
                    batch.clear()
                conn.commit()  # flush dirty pages, bound memory
        if batch:
            conn.executemany(
                "INSERT INTO postings (trigram, file_id) VALUES (?, ?)", batch
            )
        conn.commit()
        return count

    @staticmethod
    def _finalize(
        conn: "sqlite3.Connection",
        count: int,
        change_keys: Optional[Dict[str, str]],
    ) -> None:
        """Indexes, document frequencies, the meta stamp and the change keys."""
        # Composite (trigram, file_id) index: serves both "file_ids of the
        # rarest trigram" (WHERE trigram=?) and the per-candidate membership
        # seek (WHERE trigram=? AND file_id=?) that the rarest-first
        # intersection relies on -- so checking "does file X contain trigram
        # T" is O(1), not a scan of T's whole posting list.
        conn.execute("CREATE INDEX idx_postings_tc ON postings(trigram, file_id)")
        # Index the always-candidate flag so fetching unindexed files is a
        # seek, not a full scan of the (large) files table.
        conn.execute("CREATE INDEX idx_files_indexed ON files(indexed)")
        conn.commit()
        # Document frequency per trigram + total file count, so the query can
        # order the required trigrams rarest-first.
        conn.execute(
            "CREATE TABLE trigram_df AS "
            "SELECT trigram, COUNT(*) AS df FROM postings GROUP BY trigram"
        )
        conn.execute("CREATE INDEX idx_trigram_df ON trigram_df(trigram)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("file_count", count), ("schema_version", _SCHEMA_VERSION)],
        )
        if change_keys is not None:
            conn.execute(
                "CREATE TABLE file_keys (path TEXT PRIMARY KEY, change_key TEXT)"
            )
            conn.executemany(
                "INSERT INTO file_keys (path, change_key) VALUES (?, ?)",
                change_keys.items(),
            )
        conn.commit()

    def previous_change_keys(self) -> Dict[str, str]:
        """The change keys the current index was built with, or {} when it has
        none (absent, stale-schema, or built from the local disk)."""
        if not self.exists():
            return {}
        try:
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                # Only files that WERE indexed: an always-candidate row may be
                # one the backend failed to read last time, and carrying it
                # forward would keep it unindexed for as long as it is unchanged.
                return {
                    str(path): str(key)
                    for path, key in conn.execute(
                        "SELECT k.path, k.change_key FROM file_keys k "
                        "JOIN files f ON f.path = k.path WHERE f.indexed = 1"
                    )
                    if key is not None
                }
        except sqlite3.Error:
            return {}

    def build_delegated(
        self,
        content_backend: Any,
        file_list: Sequence[str],
        root: Path,
        manifest: Dict[str, "ManifestFacts"],
    ) -> "DelegatedBuildStats":
        """Build the index from bytes fetched through *content_backend*.

        For a pod that does not hold the repository: *file_list* is exactly
        what ripgrep would search at *root* (``rg --files`` run where the
        repository is), *manifest* the content manifest's facts per path.
        A file whose change key (size + mtime, or blob hash) equals the one
        the current index recorded keeps its postings; only the rest are
        fetched, in byte-budgeted batches. A file too large to index, or one
        the backend could not read, is an always-candidate, as in the local
        build. A transport failure raises and leaves the current index.
        """
        from ..indexing.fetch_planner import plan_fetch_batches

        previous = self.previous_change_keys()
        keys: Dict[str, str] = {}
        carried: List[str] = []
        to_fetch: List[str] = []
        oversized: List[str] = []
        for rel in file_list:
            facts = manifest.get(rel)
            key = facts.change_key if facts is not None else None
            if key is not None:
                keys[rel] = key
            if key is not None and previous.get(rel) == key:
                carried.append(rel)
            elif facts is not None and facts.size > _MAX_INDEX_BYTES:
                oversized.append(rel)
            else:
                to_fetch.append(rel)

        def size_of(rel: str) -> Optional[int]:
            facts = manifest.get(rel)
            return facts.size if facts is not None else None

        def rows() -> Iterator["tuple[str, Set[str], bool]"]:
            for rel in oversized:
                yield rel, set(), False  # large -> always-candidate (rg scans it)
            for batch in plan_fetch_batches(to_fetch, size_of):
                for record in content_backend.fetch(str(root), batch):
                    if record.error is not None or len(record.data) > _MAX_INDEX_BYTES:
                        yield record.path, set(), False
                        continue
                    yield record.path, _data_trigrams(record.data), True

        count = self._write_index(
            rows(),
            carry_from=self._db_path if carried else None,
            carry_paths=carried if carried else None,
            change_keys=keys,
        )
        logger.info(
            "TrigramIndexManager: built index for %s through the content backend "
            "(%d files: %d carried over, %d fetched, %d too large) at %s",
            root,
            count,
            len(carried),
            len(to_fetch),
            len(oversized),
            self._db_path,
        )
        return DelegatedBuildStats(
            files=count,
            carried=len(carried),
            fetched=len(to_fetch),
            oversized=len(oversized),
        )

    def _enumerate_files(self, repo_path: Path) -> List[str]:
        """List repo-relative files ripgrep would search (gitignore-aware)."""
        try:
            proc = subprocess.run(
                ["rg", "--files", "--null"],
                cwd=str(repo_path),
                capture_output=True,
                timeout=300,
            )
            if proc.returncode not in (0, 1):
                logger.warning(
                    "rg --files failed (%s); trigram build empty", proc.returncode
                )
                return []
            # ^ hazard strict UTF-8 decode => one non-UTF-8 name fails the whole build; test:test_build_skips_non_utf8_name_and_indexes_the_rest
            return [os.fsdecode(name) for name in proc.stdout.split(b"\0") if name]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("rg --files unavailable (%s); trigram build empty", exc)
            return []

    @staticmethod
    def _file_trigrams(abs_path: Path) -> "tuple[Set[str], bool]":
        """Return ``(trigrams, indexed)`` for a file.

        ``indexed`` is False only for files that cannot be trigram-indexed here
        (too large, or unreadable); such files carry no postings and are treated
        as always-candidates at query time so matches are never missed. Files
        with binary content are still indexed (latin-1) because ripgrep searches
        their text too.
        """
        try:
            if abs_path.stat().st_size > _MAX_INDEX_BYTES:
                return set(), False  # large -> always-candidate (rg still scans)
            data = abs_path.read_bytes()
        except OSError:
            return set(), False  # unreadable -> always-candidate
        # Extract trigrams from printable text runs only. This indexes a binary
        # file (e.g. a .class) by its embedded text -- the content ripgrep can
        # match -- so it becomes prunable, without the dense random-byte trigrams
        # of its raw bytes bloating the index. Correct: a match's required
        # literals are printable and contiguous, so they fall within one run and
        # their trigrams are captured here.
        return _data_trigrams(data), True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def query(
        self, required: Set[str], timeout_seconds: Optional[float] = None
    ) -> Optional[List[str]]:
        """Return repo-relative candidate paths that contain ALL ``required``
        trigrams, plus every always-candidate (unindexed) file.

        The result is a guaranteed superset of real matches (ripgrep does the
        exact match over it). Returns ``None`` when ``required`` is empty (no
        pruning possible) so the caller falls back to a full scan.

        Requiring ALL trigrams is what makes the candidate set small; the
        intersection is computed rarest-first so it stays cheap: seed the
        candidate set from the rarest trigram's posting list, then for each
        remaining trigram (in increasing document frequency) drop candidates that
        lack it via an O(1) ``(trigram, file_id)`` index seek -- never scanning a
        common trigram's full posting list.

        Bug #1590 (AC1/AC7): when ``timeout_seconds`` is given, the sqlite3
        connect+query is bounded by the same thread-based watchdog used by
        ``exists()`` -- raises ``TrigramIndexTimeoutError`` on a genuine
        deadline overrun instead of blocking Phase 1 forever. When omitted
        (default ``None``), behavior is unchanged. A genuinely CORRUPT
        index is logged at ERROR (distinct from other sqlite failures,
        which stay at the pre-existing WARNING level).
        """
        if not required:
            return None
        tris = list({t.lower() for t in required})
        ph_all = ",".join("?" for _ in tris)

        def _work() -> List[str]:
            # Issue #1459 code-review sweep: see the identical fix + comment
            # in exists() above -- Path.resolve().as_uri() correctly
            # percent-encodes URI-special characters that a naive
            # f"file:{path}?mode=ro" string would mis-parse.
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                holder.publish(conn)
                if holder.is_cancelled():
                    raise TrigramIndexTimeoutError(
                        f"trigram index query for {self._db_path} cancelled "
                        f"after timeout"
                    )
                # EVO-66189: the trigram DB is ~1.2 GB on shared NFS; cold queries were
                # dominated by NFS page seeks (measured 4-9s cold vs ~0.2s warm). mmap it
                # so cold pages fault through the OS page cache (shared across queries, no
                # per-read() syscall), and give the connection a larger page cache. The
                # rarest-first intersection's temp table is kept in RAM (below) rather than
                # written back to NFS.
                conn.execute("PRAGMA mmap_size=2147483648")  # 2 GiB: map the whole DB
                conn.execute("PRAGMA cache_size=-65536")  # ~64 MB page cache
                # Order the required trigrams rarest-first (df 0 == absent).
                df_map = {t: 0 for t in tris}
                for t, df in conn.execute(
                    f"SELECT trigram, df FROM trigram_df WHERE trigram IN ({ph_all})",
                    tris,
                ):
                    df_map[t] = df
                ordered = sorted(tris, key=lambda t: df_map[t])

                always = [
                    r[0]
                    for r in conn.execute("SELECT path FROM files WHERE indexed = 0")
                ] + _unstorable_names(conn)
                # A required trigram present in no file -> no indexed file can
                # contain the literal; only always-candidates remain.
                if df_map[ordered[0]] == 0:
                    return always

                # Rarest-first intersection in a connection-local temp table, kept in RAM
                # (temp_store=MEMORY) instead of written back to shared NFS (temp_store=FILE).
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("CREATE TEMP TABLE cand (id INTEGER PRIMARY KEY)")
                try:
                    conn.execute(
                        "INSERT INTO cand SELECT file_id FROM postings WHERE trigram = ?",
                        (ordered[0],),
                    )
                    for t in ordered[1:]:
                        conn.execute(
                            "DELETE FROM cand WHERE NOT EXISTS ("
                            "  SELECT 1 FROM postings"
                            "  WHERE trigram = ? AND file_id = cand.id)",
                            (t,),
                        )
                        if not conn.execute(
                            "SELECT EXISTS(SELECT 1 FROM cand)"
                        ).fetchone()[0]:
                            break
                    indexed = [
                        r[0]
                        for r in conn.execute(
                            "SELECT f.path FROM files f JOIN cand ON f.id = cand.id"
                        )
                    ]
                finally:
                    conn.execute("DROP TABLE cand")
            return always + indexed

        holder = _TrigramConnectionHolder()
        try:
            if timeout_seconds is not None:
                result, timed_out = _run_with_thread_watchdog(
                    _work, timeout_seconds, "trigram_index_query", holder
                )
                if timed_out:
                    raise TrigramIndexTimeoutError(
                        f"trigram index query for {self._db_path} exceeded "
                        f"{timeout_seconds}s timeout"
                    )
                return list(result) if result is not None else None
            return _work()
        except sqlite3.OperationalError as exc:
            # KNOWN LIMITATION (review round 2 finding F5, deliberately
            # deferred -- same gap as exists()'s identical comment above):
            # SQLITE_IOERR / SQLITE_CANTOPEN (genuine disk/mount failures,
            # e.g. a wedged NFS mount) ALSO raise sqlite3.OperationalError,
            # so they currently land in this same routine WARNING bucket
            # rather than being flagged as an error. A precise fix would
            # inspect exc.sqlite_errorcode, but that attribute is Python
            # 3.11+ only (this project's CI `lint` job and mypy target pin
            # Python 3.9 -- see CLAUDE.md's CI section), so a
            # version-gated workaround was judged not worth the added
            # complexity for this low-priority gap; noted here for a
            # future fix once the minimum Python version moves past 3.9.
            logger.warning("trigram query failed (%s); caller should full-scan", exc)
            return None
        except (sqlite3.DatabaseError, sqlite3.InterfaceError) as exc:
            # Bug #1590 AC7: genuine corruption -- distinct, elevated
            # severity from the routine OperationalError case above.
            # Review round 2 finding F5: sqlite3.InterfaceError is a
            # SIBLING branch (Error -> InterfaceError), NOT a
            # DatabaseError subclass -- included here so it degrades
            # gracefully too, matching exists()'s identical fix.
            logger.error(
                "trigram index at %s appears CORRUPT (%s); caller should "
                "full-scan -- this is NOT a routine query failure",
                self._db_path,
                exc,
            )
            return None
