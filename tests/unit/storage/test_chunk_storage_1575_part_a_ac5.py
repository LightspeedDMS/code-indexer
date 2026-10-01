"""TDD tests for Bug #1575 Part A -- authoritative content enumeration +
targeted fetches for the CHUNKS_DB (sqlite) storage layout.

Covers:
  - AC5: `distinct_content_paths()` preserves the `type == "content"`
    restriction `_fetch_all_content_points()` enforced. The schema's `path`
    column has no accompanying `type` column, so this story adds one
    (backward-compatible `ALTER TABLE ADD COLUMN`, backfilled from existing
    rows) rather than relying on an unenforceable whole-codebase invariant
    about every future writer.
  - `fetch_points_for_paths()`: batched, targeted fetch by stored path --
    never a full-table scan.

RED phase: every test in this file must FAIL against the pre-Part-A
`ChunkStore` (no `type` column, no `distinct_content_paths`/
`fetch_points_for_paths` methods).
"""

import sqlite3
import zstandard

import numpy as np

from code_indexer.storage.sqlite_chunk_store import ChunkStore


VECTOR_SIZE = 8


def _make_vector(seed: int = 0):
    rng = np.random.RandomState(seed)
    return rng.rand(VECTOR_SIZE).astype(np.float32).tolist()


def _content_record(point_id: str, path: str, seed: int = 0) -> dict:
    return {
        "id": point_id,
        "vector": _make_vector(seed),
        "metadata": {"language": "python", "type": "content"},
        "payload": {"path": path, "type": "content", "hidden_branches": []},
        "chunk_text": f"chunk for {path}",
    }


def _non_content_record_with_path(point_id: str, path: str, seed: int = 1) -> dict:
    """A synthetic record whose `type` is NOT "content" but which DOES carry
    a `path` key. No production writer in this codebase currently produces
    this combination (proven separately in
    test_chunk_storage_1575_part_a_temporal_invariant.py against real
    writer code) -- this is a direct, low-level test of ChunkStore's own
    filtering contract via its real write_batch() primitive, deliberately
    constructing the one shape that would silently defeat a naive
    `distinct_paths()`-based implementation.
    """
    return {
        "id": point_id,
        "vector": _make_vector(seed),
        "metadata": {"language": "", "type": "diff"},
        "payload": {"path": path, "type": "diff"},
    }


class TestDistinctContentPathsFiltersByType:
    """AC5: distinct_content_paths() must return ONLY paths belonging to
    type == "content" rows -- the discriminating case is a MIX of content
    and non-content rows sharing distinct paths. A test using only content
    rows would pass on both a correct (type-filtered) implementation and a
    broken one (bare distinct_paths(), no type filter) -- so this test
    deliberately mixes both.
    """

    def test_excludes_non_content_path_bearing_rows(self, tmp_path):
        db_path = tmp_path / "chunks.db"

        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    _content_record("content-1", "src/a.py", seed=1),
                    _content_record("content-2", "src/b.py", seed=2),
                    _non_content_record_with_path(
                        "noncontent-1", "src/diff_only.py", seed=3
                    ),
                ]
            )

            result = store.distinct_content_paths()

        assert result == {"src/a.py", "src/b.py"}
        assert "src/diff_only.py" not in result

    def test_returns_empty_set_for_empty_store(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            assert store.distinct_content_paths() == set()

    def test_null_path_rows_are_excluded_even_if_content_type(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            record = {
                "id": "no-path",
                "vector": _make_vector(4),
                "metadata": {"type": "content"},
                "payload": {"type": "content"},  # no "path" key
            }
            store.write_batch([record])
            assert store.distinct_content_paths() == set()


class TestTypeColumnBackwardCompatibleMigration:
    """AC5: the `type` column migration must be backward compatible
    (ADD COLUMN, backfilled) -- opening a pre-existing chunks.db that
    predates this column must not crash, and must correctly backfill the
    column from each row's decoded payload so distinct_content_paths()
    behaves correctly immediately afterward.
    """

    def _write_pre_migration_row(
        self, db_path, point_id: str, path: str, record_type: str
    ) -> None:
        """Simulate a chunks.db written by pre-#1575 code: schema with NO
        `type` column at all.
        """
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    point_id TEXT PRIMARY KEY,
                    path TEXT,
                    vector BLOB NOT NULL,
                    data BLOB NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            vector_blob = np.asarray(_make_vector(5), dtype="<f4").tobytes()
            payload = {"path": path, "type": record_type}
            data = {"metadata": {"type": record_type}, "payload": payload}
            compressor = zstandard.ZstdCompressor()
            import json as _json

            data_blob = compressor.compress(_json.dumps(data).encode("utf-8"))
            conn.execute(
                "INSERT INTO chunks (point_id, path, vector, data) VALUES (?, ?, ?, ?)",
                (point_id, path, vector_blob, data_blob),
            )
            conn.commit()
        finally:
            conn.close()

    def test_pre_migration_database_opens_without_crashing(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        self._write_pre_migration_row(db_path, "old-1", "src/legacy.py", "content")

        # Must not raise -- opening a pre-existing chunks.db lacking the
        # `type` column must self-heal via an idempotent ALTER TABLE.
        with ChunkStore(db_path) as store:
            result = store.distinct_content_paths()

        assert result == {"src/legacy.py"}

    def test_pre_migration_non_content_row_is_excluded_after_backfill(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        self._write_pre_migration_row(db_path, "old-1", "src/legacy.py", "content")
        self._write_pre_migration_row(db_path, "old-2", "src/legacy_diff.py", "diff")

        with ChunkStore(db_path) as store:
            result = store.distinct_content_paths()

        assert result == {"src/legacy.py"}
        assert "src/legacy_diff.py" not in result

    def test_type_column_exists_after_opening_pre_migration_database(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        self._write_pre_migration_row(db_path, "old-1", "src/legacy.py", "content")

        with ChunkStore(db_path):
            pass

        conn = sqlite3.connect(str(db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        finally:
            conn.close()
        assert "type" in cols


class TestDistinctContentPathsImmutableOpenOnPreMigrationDatabase:
    """Codex review follow-up (Bug #1575 Part A, CRITICAL finding 1): an
    IMMUTABLE open (used for versioned/read-only snapshots) skips
    ``_ensure_schema()`` entirely (see ``ChunkStore.__init__`` -- the
    ``if not immutable:`` guard), so a pre-migration ``chunks.db`` (created
    before the ``type`` column migration landed) opened with
    ``immutable=1`` has NO ``type`` column. ``distinct_content_paths()``
    must not crash with ``sqlite3.OperationalError: no such column: type``
    on such a database -- it must gracefully fall back to a decode-based
    content-type check, never attempt ``ALTER TABLE`` against an immutable
    connection.
    """

    def _write_pre_migration_row(
        self, db_path, point_id: str, path: str, record_type: str
    ) -> None:
        """Simulate a chunks.db written by pre-#1575 code: schema with NO
        `type` column at all (identical shape to the sibling class above).
        """
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    point_id TEXT PRIMARY KEY,
                    path TEXT,
                    vector BLOB NOT NULL,
                    data BLOB NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            vector_blob = np.asarray(_make_vector(6), dtype="<f4").tobytes()
            payload = {"path": path, "type": record_type}
            data = {"metadata": {"type": record_type}, "payload": payload}
            compressor = zstandard.ZstdCompressor()
            import json as _json

            data_blob = compressor.compress(_json.dumps(data).encode("utf-8"))
            conn.execute(
                "INSERT INTO chunks (point_id, path, vector, data) VALUES (?, ?, ?, ?)",
                (point_id, path, vector_blob, data_blob),
            )
            conn.commit()
        finally:
            conn.close()

    def test_immutable_open_on_pre_migration_database_does_not_crash(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        self._write_pre_migration_row(db_path, "old-1", "src/legacy.py", "content")
        self._write_pre_migration_row(db_path, "old-2", "src/legacy_diff.py", "diff")

        # immutable=True mirrors a versioned-snapshot open: _ensure_schema()
        # (and therefore the `type` column ALTER TABLE) is skipped entirely
        # per ChunkStore.__init__'s `if not immutable:` guard.
        store = ChunkStore(db_path, immutable=True)
        try:
            result = store.distinct_content_paths()
        finally:
            store.close()

        assert result == {"src/legacy.py"}
        assert "src/legacy_diff.py" not in result

    def test_immutable_open_never_mutates_the_database(self, tmp_path):
        """The fallback must never attempt ALTER TABLE (or any other write)
        against the immutable connection -- confirm the on-disk schema is
        still column-less afterward."""
        db_path = tmp_path / "chunks.db"
        self._write_pre_migration_row(db_path, "old-1", "src/legacy.py", "content")

        store = ChunkStore(db_path, immutable=True)
        try:
            store.distinct_content_paths()
        finally:
            store.close()

        conn = sqlite3.connect(str(db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        finally:
            conn.close()
        assert "type" not in cols, (
            "distinct_content_paths() must never ALTER TABLE an immutable "
            "connection -- the on-disk schema must remain unchanged"
        )


class TestDistinctContentPathsImmutableOpenWithNoChunksTable:
    """2nd Codex review follow-up (Bug #1575 Part A, Gap 1): a genuinely
    virgin immutable snapshot -- the ``chunks.db`` FILE exists (e.g. an
    empty file materialized by a versioned-snapshot clone taken before any
    chunk was ever written) but has NEVER been populated, so it has NO
    ``chunks`` table at all, not merely a missing ``type`` column.

    ``PRAGMA table_info(chunks)`` returns an EMPTY result set for BOTH
    "table absent" and "table present, column absent" -- indistinguishable
    from that pragma alone, so ``_has_type_column()`` returns False in both
    cases and ``distinct_content_paths()`` used to unconditionally dispatch
    to the decode-based fallback query (``SELECT path, data FROM chunks``),
    which raises ``sqlite3.OperationalError: no such table: chunks`` when
    the table is genuinely absent. A legitimately empty collection must
    return an empty set, never crash.
    """

    def test_returns_empty_set_for_table_absent_immutable_database(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        # A bare, zero-table sqlite file -- mirrors what a virgin
        # immutable snapshot looks like (immutable open skips
        # _ensure_schema() entirely, per ChunkStore.__init__).
        sqlite3.connect(str(db_path)).close()

        store = ChunkStore(db_path, immutable=True)
        try:
            result = store.distinct_content_paths()
        finally:
            store.close()

        assert result == set()

    def test_never_mutates_a_table_absent_immutable_database(self, tmp_path):
        """The table-absence guard must be a pure read (sqlite_master
        query) -- never an ALTER TABLE/CREATE TABLE against the immutable
        connection."""
        db_path = tmp_path / "chunks.db"
        sqlite3.connect(str(db_path)).close()

        store = ChunkStore(db_path, immutable=True)
        try:
            store.distinct_content_paths()
        finally:
            store.close()

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        assert tables == set(), (
            "distinct_content_paths() must never create/alter tables "
            "against an immutable connection when the chunks table is "
            "genuinely absent"
        )


class TestFetchPointsForPathsMatching:
    """fetch_points_for_paths(): batched, targeted fetch by stored path --
    never a full-table scan. The discriminating assertion is that points
    for paths NOT requested are excluded, proving this is a real filtered
    lookup rather than "return everything".
    """

    def test_returns_only_records_for_requested_paths(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    _content_record("p1", "src/a.py", seed=1),
                    _content_record("p2", "src/a.py", seed=2),
                    _content_record("p3", "src/b.py", seed=3),
                    _content_record("p4", "src/c.py", seed=4),
                ]
            )

            result = store.fetch_points_for_paths({"src/a.py", "src/c.py"})

        ids = {r["id"] for r in result}
        assert ids == {"p1", "p2", "p4"}
        for record in result:
            assert record["payload"]["path"] in {"src/a.py", "src/c.py"}

    def test_empty_paths_returns_empty_list(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch([_content_record("p1", "src/a.py")])
            assert store.fetch_points_for_paths(set()) == []

    def test_no_matching_paths_returns_empty_list(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch([_content_record("p1", "src/a.py")])
            assert store.fetch_points_for_paths({"src/does_not_exist.py"}) == []


class TestFetchPointsForPathsPayloadOnly:
    """Codex review follow-up (Bug #1575 Part A, finding 6): the
    ``FilesystemVectorStore`` caller (``fetch_points_for_paths`` in
    ``filesystem_vector_store.py``) only ever needs ``id``/``payload`` --
    it immediately discards the vector. A ``payload_only=True`` variant
    must skip vector decode entirely for this call path, while the
    default (``payload_only=False``) stays byte-identical to today.
    """

    def test_payload_only_excludes_vector_key(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    _content_record("p1", "src/a.py", seed=1),
                    _content_record("p2", "src/b.py", seed=2),
                ]
            )

            result = store.fetch_points_for_paths({"src/a.py"}, payload_only=True)

        assert len(result) == 1
        record = result[0]
        assert record["id"] == "p1"
        assert record["payload"]["path"] == "src/a.py"
        assert "vector" not in record, (
            "payload_only=True must never decode/include the vector field"
        )

    def test_payload_only_filtering_semantics_match_default(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    _content_record("p1", "src/a.py", seed=1),
                    _content_record("p2", "src/a.py", seed=2),
                    _content_record("p3", "src/b.py", seed=3),
                    _content_record("p4", "src/c.py", seed=4),
                ]
            )

            result = store.fetch_points_for_paths(
                {"src/a.py", "src/c.py"}, payload_only=True
            )

        ids = {r["id"] for r in result}
        assert ids == {"p1", "p2", "p4"}
        for record in result:
            assert record["payload"]["path"] in {"src/a.py", "src/c.py"}

    def test_payload_only_default_is_false_and_unchanged(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch([_content_record("p1", "src/a.py", seed=1)])

            result = store.fetch_points_for_paths({"src/a.py"})

        assert "vector" in result[0], (
            "default payload_only=False must remain byte-identical -- "
            "vector must still be present"
        )


class TestFetchPointsForPathsChunking:
    """Guards the chunking logic for large IN-clause queries."""

    def test_large_path_set_beyond_sqlite_variable_limit(self, tmp_path):
        """A naive single `WHERE path IN (...)` query with >999 bound
        parameters raises sqlite3.OperationalError ("too many SQL
        variables"). 1200 distinct paths forces at least one chunk
        boundary to be exercised.
        """
        db_path = tmp_path / "chunks.db"
        total = 1200
        with ChunkStore(db_path) as store:
            records = [
                _content_record(f"p{i}", f"src/file_{i}.py", seed=i % 50)
                for i in range(total)
            ]
            store.write_batch(records)

            all_paths = {f"src/file_{i}.py" for i in range(total)}
            result = store.fetch_points_for_paths(all_paths)

        assert len(result) == total


class TestFetchPointsForPathsImmutableOpenWithNoChunksTable:
    """Gap 1 (2nd Codex review, Bug #1575 Part A): the SAME table-absence
    hazard ``distinct_content_paths()`` has also applies here --
    ``fetch_points_for_paths()`` is another query "added in this Part A
    work" (per its own docstring). A non-empty ``paths`` request against a
    genuinely virgin immutable snapshot (chunks.db file exists, no
    ``chunks`` table) must return an empty list, never raise
    ``sqlite3.OperationalError: no such table: chunks``.
    """

    def test_returns_empty_list_for_table_absent_immutable_database(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        sqlite3.connect(str(db_path)).close()

        store = ChunkStore(db_path, immutable=True)
        try:
            result = store.fetch_points_for_paths({"src/a.py", "src/b.py"})
        finally:
            store.close()

        assert result == []
