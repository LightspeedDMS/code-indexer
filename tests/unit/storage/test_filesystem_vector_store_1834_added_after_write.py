"""Bug #1834: a point_id must be recorded as 'added' only AFTER the
chunks.db write that persists it has actually succeeded -- not while the
record list is still being built.

AC1 (RED on the unfixed tree): a fatal chunks.db write failure must leave
the failed point_id ABSENT from the session 'added' set, and the failure
must still propagate loudly (never swallowed, never a new retry/timeout).

The failure is a REAL one, not a mock: the ``chunks.db`` path is
pre-created as a directory, so ``sqlite3.connect()`` genuinely cannot open
it and raises ``sqlite3.OperationalError: unable to open database file``
(verified empirically). That is-a ``sqlite3.DatabaseError`` whose message
does not contain a lock-contention substring, so
``is_fatal_chunk_store_write_error`` classifies it FATAL --
``_write_chunks_db_with_retry`` raises on attempt 0 with zero retry sleep,
keeping this test fast and deterministic without patching any production
code or object.

AC2: the success path is unchanged -- a normal write still records every
id exactly once.
"""

import sqlite3

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


def _points(n: int, prefix: str = "vec") -> list:
    rng = np.random.default_rng(3)
    return [
        {
            "id": f"{prefix}_{i}",
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {"path": f"{prefix}_{i}.py", "language": "python"},
        }
        for i in range(n)
    ]


@pytest.fixture
def indexing_session(tmp_path):
    """A CHUNKS_DB-mode collection with an active indexing session."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    return store


class TestAddedRecordedOnlyAfterWriteSucceeds:
    def test_failed_write_leaves_added_set_empty_and_raises(self, indexing_session):
        """AC1: a fatal chunks.db open/write failure must not mark any
        point_id as 'added', and the failure must propagate uncaught."""
        store = indexing_session
        collection_path = store._get_collection_path("coll")
        # Real fault: chunks.db can never be opened as a sqlite file
        # because a directory sits at that exact path.
        (collection_path / "chunks.db").mkdir()

        try:
            store.upsert_points("coll", _points(3))
            raised = False
        except sqlite3.DatabaseError:
            raised = True

        assert raised, (
            "the chunks.db open/write failure must propagate loudly, never be swallowed"
        )

        changes = store._indexing_session_changes["coll"]
        assert changes["added"] == set(), (
            "point_ids must not be marked 'added' when the chunks.db write "
            "never succeeded"
        )

    def test_successful_write_still_records_every_id_exactly_once(
        self, indexing_session
    ):
        """AC2: the success path is unchanged by the fix."""
        store = indexing_session
        store.upsert_points("coll", _points(3))

        changes = store._indexing_session_changes["coll"]
        assert changes["added"] == {"vec_0", "vec_1", "vec_2"}
