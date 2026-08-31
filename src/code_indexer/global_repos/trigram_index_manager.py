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
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Set, Tuple

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
# Bump whenever the on-disk index schema changes (a new table/column, different
# posting semantics, ...). An index whose stamped version differs is treated as
# absent by exists(), so a stale/old-format index (e.g. a golden-repo refresh
# swapped the alias to a snapshot indexed by an older build) yields a clean
# full-scan + rebuild instead of a caught "no such column" query error.
_SCHEMA_VERSION = 1
# Skip trigram extraction for files larger than this (still recorded as an
# always-candidate so matches inside them are never missed). Keeps build I/O and
# db size bounded; large files are rare and ripgrep handles them in the pass.
_MAX_INDEX_BYTES = 5 * 1024 * 1024
_INSERT_BATCH = 5000
# Commit (and fsync) every N files so dirty database pages are flushed and
# reclaimed during a large build instead of accumulating against the container
# memory limit.
_COMMIT_EVERY_FILES = 2000


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
            dir=str(self._dir), prefix="trigrams.", suffix=".db.building"
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
            batch: List[tuple] = []
            for rel in rel_files:
                file_id = count + 1
                tris, indexed = self._file_trigrams(repo_path / rel)
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
                if count % _COMMIT_EVERY_FILES == 0:
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
            conn.commit()
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
        logger.info(
            "TrigramIndexManager: built index for %s (%d files) at %s",
            repo_path,
            count,
            self._db_path,
        )
        return count

    def _enumerate_files(self, repo_path: Path) -> List[str]:
        """List repo-relative files ripgrep would search (gitignore-aware)."""
        try:
            proc = subprocess.run(
                ["rg", "--files"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode not in (0, 1):
                logger.warning(
                    "rg --files failed (%s); trigram build empty", proc.returncode
                )
                return []
            return [line for line in proc.stdout.splitlines() if line]
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
        tris: Set[str] = set()
        for m in _PRINTABLE_RUN.finditer(data):
            tris |= trigrams(m.group().decode("ascii").lower())
        return tris, True

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
                ]
                # A required trigram present in no file -> no indexed file can
                # contain the literal; only always-candidates remain.
                if df_map[ordered[0]] == 0:
                    return always

                # Rarest-first intersection in a connection-local temp table.
                conn.execute("PRAGMA temp_store=FILE")
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
