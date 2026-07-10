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
from pathlib import Path
from typing import Iterable, List, Optional, Set

from .regex_trigram import trigrams

logger = logging.getLogger(__name__)

# Maximal runs of printable ASCII (plus tab/newline/CR) of length >= 3. Trigrams
# are extracted only from these runs, so a binary file (e.g. a .class) is indexed
# by its embedded text -- exactly the content ripgrep can match -- without the
# dense noise trigrams of its raw bytes bloating the index.
_PRINTABLE_RUN = re.compile(rb"[\t\n\r\x20-\x7e]{3,}")

_DB_NAME = "trigrams.db"
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

    def exists(self) -> bool:
        """True when a populated index database is present."""
        if not self._db_path.exists():
            return False
        try:
            with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
                return bool(row and row[0] > 0)
        except sqlite3.Error:
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
        tmp_path = self._db_path.with_suffix(".db.building")
        if tmp_path.exists():
            tmp_path.unlink()

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
            conn.commit()
            # Document frequency per trigram + total file count, so the query can
            # order the required trigrams rarest-first.
            conn.execute(
                "CREATE TABLE trigram_df AS "
                "SELECT trigram, COUNT(*) AS df FROM postings GROUP BY trigram"
            )
            conn.execute("CREATE INDEX idx_trigram_df ON trigram_df(trigram)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('file_count', ?)", (count,)
            )
            conn.commit()
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
    def query(self, required: Set[str]) -> Optional[List[str]]:
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
        """
        if not required:
            return None
        tris = list({t.lower() for t in required})
        ph_all = ",".join("?" for _ in tris)
        try:
            with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
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
        except sqlite3.Error as exc:
            logger.warning("trigram query failed (%s); caller should full-scan", exc)
            return None
