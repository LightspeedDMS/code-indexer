"""Issue #1548 round-5 secondary finding 4: NULL-ordering digest divergence.

``content_digest()`` (both ``TemporalMetadataSqliteBackend`` and
``TemporalMetadataPostgresBackend``) relied on a bare SQL ``ORDER BY`` over
columns that CAN be NULL (e.g. ``commit_hash``, ``file_path``). SQLite sorts
NULL first in an ascending ORDER BY; PostgreSQL sorts NULL last by default.
Two backends holding IDENTICAL logical rows -- at least one of which has a
NULL in an ordered column -- could therefore disagree on row order and
produce DIFFERENT digests for the SAME data, defeating
``mover.py``'s ``_metadata_scope_relocation_verified`` cross-backend
comparison.

The fix is a single canonical, NULL-order-neutral sort applied in Python
after fetching, shared by both backends, so the digest is backend-
independent by construction rather than by coincidence of two SQL engines'
default NULL-ordering happening to agree.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


class TestCanonicalContentDigestRowsOrderIndependence:
    def test_null_first_and_null_last_physical_orderings_produce_same_key_order(self):
        """The canonical sort must place rows in the SAME relative order
        regardless of whether NULLs were physically fetched first or last --
        i.e. it must not merely trust whatever order the caller already
        fetched rows in.
        """
        from code_indexer.storage.temporal_metadata_store import (
            canonical_content_digest_rows,
        )

        # Same three logical rows, NULL in the 3rd column (commit_hash) on
        # the middle row -- fed in two different physical orders, as if
        # one came from a NULL-first engine and the other a NULL-last one.
        null_first_order = [
            ("h1", "p1", None, "a.py", 0),
            ("h2", "p2", "abc123", "b.py", 0),
            ("h3", "p3", "def456", "c.py", 0),
        ]
        null_last_order = [
            ("h2", "p2", "abc123", "b.py", 0),
            ("h3", "p3", "def456", "c.py", 0),
            ("h1", "p1", None, "a.py", 0),
        ]

        assert canonical_content_digest_rows(null_first_order) == (
            canonical_content_digest_rows(null_last_order)
        )

    def test_never_raises_type_error_comparing_none_to_non_none(self):
        """A NULL-order-neutral key must never attempt to compare None
        against a real value directly (which raises TypeError in Python 3)
        -- this is exactly the naive-sort trap the fix must avoid.
        """
        from code_indexer.storage.temporal_metadata_store import (
            canonical_content_digest_rows,
        )

        rows = [
            ("h1", "p1", None, None, 0),
            ("h2", "p2", "abc", "x.py", 1),
            ("h3", "p3", None, "y.py", None),
        ]
        # Must not raise.
        result = canonical_content_digest_rows(rows)
        assert len(result) == 3


class TestSqliteBackendContentDigestIsOrderStable:
    def test_content_digest_stable_across_physically_different_insert_orders(
        self,
    ):
        """Two SQLite databases holding the SAME logical rows (one with a
        NULL commit_hash), inserted in different physical order, must
        produce the SAME content_digest -- proving the digest is computed
        from a canonical order, not incidental fetch order.
        """
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend_a = TemporalMetadataSqliteBackend(Path(tmpdir) / "a")
            backend_b = TemporalMetadataSqliteBackend(Path(tmpdir) / "b")

            backend_a.save_metadata("p1", {"commit_hash": None, "path": "a.py"})
            backend_a.save_metadata("p2", {"commit_hash": "abc123", "path": "b.py"})

            # Insert in the opposite order into backend_b.
            backend_b.save_metadata("p2", {"commit_hash": "abc123", "path": "b.py"})
            backend_b.save_metadata("p1", {"commit_hash": None, "path": "a.py"})

            assert backend_a.content_digest() == backend_b.content_digest()

    def test_content_digest_null_commit_hash_row_survives_digest_computation(
        self,
    ):
        """A row with an explicit NULL commit_hash must not crash digest
        computation and must be included in the digest (regression guard
        for the underlying SQL: payload.get("commit_hash", "") returns the
        EXPLICIT None a caller passes, not the "" default, since the key
        is present).
        """
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TemporalMetadataSqliteBackend(Path(tmpdir) / "temporal")
            backend.save_metadata("p1", {"commit_hash": None, "path": "a.py"})

            digest = backend.content_digest()
            assert isinstance(digest, str)
            assert len(digest) == 64

            conn = sqlite3.connect(backend.db_path)
            try:
                row = conn.execute(
                    "SELECT commit_hash FROM temporal_metadata WHERE point_id = 'p1'"
                ).fetchone()
            finally:
                conn.close()
            assert row[0] is None
