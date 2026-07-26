"""Unit tests for `chunk_store_has_real_data()` (Issue #1459 remediation
Findings 2/3/4).

Finding 2: a read-only status/health probe must never CREATE a missing
chunks.db as a side effect. `ChunkStore.__init__`'s non-immutable
`sqlite3.connect(str(db_path))` does create a missing file -- this function
opens the file with SQLite's `mode=ro` URI parameter instead, which never
creates a missing file.

Finding 3: a genuinely corrupt chunks.db must not crash a reporting surface
that is supposed to degrade gracefully (default `on_error="treat_absent"`),
while a caller that needs fail-loud behavior on uncertain state (the
destructive blank-out path) can opt in via `on_error="raise"`. A missing
file is NOT a corruption case -- it returns False regardless of on_error.

Finding 4: this is the ONE shared primitive the three call sites
(golden_repo_manager.py, repository_health_aggregator.py,
temporal_blank_out.py) route through instead of each reimplementing
"open ChunkStore -> count -> close".

`sqlite3.Connection` cannot be mocked (a read-only C extension type) --
every fault-injection test here uses real missing/corrupt files, never
`unittest.mock`.
"""

from __future__ import annotations

import sqlite3

import pytest

from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    chunk_store_has_real_data,
    open_chunk_store_for_path,
)


def _write_one_real_row(db_path) -> None:
    """Shared test setup: populate a real chunks.db with one committed row.

    Used by the round-2 remediation Finding A / Finding B tests below,
    which each need a genuinely populated store before exercising the URI
    percent-escaping and lock-contention scenarios.
    """
    with ChunkStore(db_path) as store:
        store.write_batch(
            [{"id": "p1", "vector": [0.1, 0.2, 0.3], "payload": {"path": "a.py"}}]
        )


def test_missing_chunks_db_returns_false_without_creating_file(tmp_path):
    """Finding 2: a missing chunks.db must return False AND must NOT be
    created as a side effect of the check -- before AND after the call."""
    db_path = tmp_path / "chunks.db"
    assert not db_path.exists()

    result = chunk_store_has_real_data(db_path)

    assert result is False
    assert not db_path.exists(), (
        "chunk_store_has_real_data() must be side-effect-free -- it must "
        "NOT create chunks.db as a side effect of a read-only check"
    )


def test_reproduce_old_bug_open_chunk_store_for_path_does_create_file(tmp_path):
    """Reproduces the CURRENT bug this finding fixes: the old code path
    (open_chunk_store_for_path -> ChunkStore.__init__ -> sqlite3.connect
    without mode=ro) DOES silently create the missing file. This proves
    the bug is real before proving the fix (chunk_store_has_real_data)
    does not have it."""
    db_path = tmp_path / "chunks.db"
    assert not db_path.exists()

    store = open_chunk_store_for_path(db_path, str(tmp_path))
    try:
        assert int(store.count()) == 0
    finally:
        store.close()

    assert db_path.exists(), (
        "This assertion documents the OLD bug: opening via "
        "open_chunk_store_for_path on a missing file silently creates it"
    )


def test_populated_chunks_db_returns_true(tmp_path):
    """A real chunks.db with at least one committed row returns True."""
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        store.write_batch(
            [{"id": "p1", "vector": [0.1, 0.2, 0.3], "payload": {"path": "a.py"}}]
        )

    result = chunk_store_has_real_data(db_path)

    assert result is True


def test_empty_schema_initialized_chunks_db_returns_false(tmp_path):
    """A real chunks.db that has been schema-initialized (via a normal
    ChunkStore open) but has zero rows returns False."""
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path):
        pass  # opening alone creates the schema, writes nothing

    result = chunk_store_has_real_data(db_path)

    assert result is False


def test_corrupt_file_treat_absent_returns_false_and_logs_warning(tmp_path, caplog):
    """Finding 3, default on_error='treat_absent': a genuinely corrupt file
    (not a valid SQLite database) returns False and logs a WARNING --
    never crashes a read-only reporting surface."""
    db_path = tmp_path / "chunks.db"
    db_path.write_bytes(b"not a sqlite file at all")

    import logging

    with caplog.at_level(logging.WARNING):
        result = chunk_store_has_real_data(db_path)

    assert result is False
    assert any(
        "corrupt" in record.message.lower() or "database" in record.message.lower()
        for record in caplog.records
    ), f"Expected a WARNING log for the corrupt file, got: {caplog.records}"


def test_corrupt_file_raise_reraises_database_error(tmp_path):
    """Finding 3, on_error='raise': a genuinely corrupt file re-raises the
    original sqlite3.DatabaseError verbatim -- the fail-loud contract the
    destructive blank-out path requires."""
    db_path = tmp_path / "chunks.db"
    db_path.write_bytes(b"not a sqlite file at all")

    with pytest.raises(sqlite3.DatabaseError):
        chunk_store_has_real_data(db_path, on_error="raise")


def test_missing_file_with_on_error_raise_still_returns_false(tmp_path):
    """A missing file is NOT a corruption case -- on_error='raise' must
    still return False silently for a genuinely-absent file (no data yet
    is not the same failure mode as corrupt data)."""
    db_path = tmp_path / "does-not-exist" / "chunks.db"
    result = chunk_store_has_real_data(db_path, on_error="raise")
    assert result is False


def test_missing_chunks_table_on_valid_sqlite_file_returns_false_regardless_of_on_error(
    tmp_path,
):
    """Regression safety (round 2 remediation): a genuinely-valid SQLite
    file that simply has no "chunks" table yet (e.g. an incomplete
    mid-creation file) must return False unconditionally -- this is a "no
    data yet" state, NOT a "raise-worthy" operational problem, even when
    on_error='raise'."""
    db_path = tmp_path / "chunks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

    assert chunk_store_has_real_data(db_path, on_error="treat_absent") is False
    assert chunk_store_has_real_data(db_path, on_error="raise") is False


# ---------------------------------------------------------------------------
# Round 2 remediation, Finding A: SQLite URI percent-escaping
# ---------------------------------------------------------------------------


def test_populated_chunks_db_at_uri_special_char_path_returns_true_no_stray_files(
    tmp_path,
):
    """Finding A: a real, populated chunks.db living at a path containing
    URI-special characters ('?', '#', '%', a space, and a unicode
    character) must still be correctly read as having real data -- a
    naive `f"file:{path}?mode=ro"` string gets misparsed by SQLite's URI
    parser (the literal '?'/'#' inside the path are interpreted as the
    START of the query/fragment, truncating the path), producing a false
    negative AND, since the truncated "path" is opened in default
    read-write-create mode instead of the intended mode=ro, stray files
    created at the misparsed sub-paths -- reproducing the EXACT
    "must never create files" contract violation this function exists to
    prevent (round 1 Finding 2), just via a different mechanism.
    """
    special_dir = tmp_path / "repo?a#b c%d_üñí"
    special_dir.mkdir()
    db_path = special_dir / "chunks.db"

    _write_one_real_row(db_path)

    # Snapshot the ENTIRE tmp_path tree (not just tmp_path's direct
    # children) immediately before the read-only call under test, so any
    # stray file created at a misparsed sub-path -- inside OR outside
    # special_dir -- is caught, matching Codex's own reproduction method.
    before = sorted(str(p) for p in tmp_path.rglob("*"))

    result = chunk_store_has_real_data(db_path)

    after = sorted(str(p) for p in tmp_path.rglob("*"))

    assert result is True, (
        "URI-special characters in the path must not cause a false "
        "negative via SQLite URI misparsing"
    )
    assert before == after, (
        "chunk_store_has_real_data must never create stray files/dirs "
        f"from URI misparsing; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Round 2 remediation, Finding B: blanket `except sqlite3.OperationalError`
# defeats the on_error="raise" fail-loud contract for non-"missing data"
# operational errors (e.g. a locked database).
# ---------------------------------------------------------------------------


def test_locked_database_with_on_error_raise_raises_operational_error(tmp_path):
    """Finding B: a genuinely populated chunks.db held under a REAL
    exclusive lock by a second real sqlite3 connection must cause
    on_error='raise' callers to actually raise -- not silently wait out
    SQLite's busy-timeout and return False, which would let
    temporal_blank_out.py's destructive delete-decision path proceed on a
    false "no data" reading when the real answer is "we don't know, the
    database is locked" (Messi Rule #13)."""
    db_path = tmp_path / "chunks.db"
    _write_one_real_row(db_path)

    locker_conn = sqlite3.connect(str(db_path))
    locker_conn.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            chunk_store_has_real_data(db_path, on_error="raise")
    finally:
        locker_conn.rollback()
        locker_conn.close()


def test_locked_database_with_on_error_treat_absent_logs_and_returns_false(
    tmp_path, caplog
):
    """Finding B, graceful-degradation side: the two pure reporting call
    sites (golden_repo_manager.py, repository_health_aggregator.py) using
    the default on_error='treat_absent' must still degrade gracefully --
    log a WARNING and return False -- for the SAME real lock contention,
    never crash."""
    import logging

    db_path = tmp_path / "chunks.db"
    _write_one_real_row(db_path)

    locker_conn = sqlite3.connect(str(db_path))
    locker_conn.execute("BEGIN EXCLUSIVE")
    try:
        with caplog.at_level(logging.WARNING):
            result = chunk_store_has_real_data(db_path, on_error="treat_absent")
    finally:
        locker_conn.rollback()
        locker_conn.close()

    assert result is False
    assert any(
        "locked" in record.message.lower() or "operational" in record.message.lower()
        for record in caplog.records
    ), f"Expected a WARNING log for the lock contention, got: {caplog.records}"


# ---------------------------------------------------------------------------
# Round 4 remediation: "unable to open database file" is SQLite's message
# for BOTH a genuinely-missing file AND a permission-denied (but existing)
# file -- pure message substring matching cannot distinguish them. A
# permission-denied file must be treated as a genuine operational failure
# (dispatched through on_error), NOT silently classified as "no data yet".
# ---------------------------------------------------------------------------


def test_permission_denied_database_with_on_error_raise_raises_operational_error(
    tmp_path,
):
    """Round 4: a real, populated chunks.db that genuinely EXISTS on disk
    but is unreadable (chmod 000) must cause on_error='raise' callers to
    actually raise -- not be silently misclassified as "file doesn't
    exist yet" just because SQLite emits the identical "unable to open
    database file" message for both cases. Same fail-loud contract as the
    locked-database case above."""
    db_path = tmp_path / "chunks.db"
    _write_one_real_row(db_path)

    db_path.chmod(0o000)
    try:
        with pytest.raises(sqlite3.OperationalError):
            chunk_store_has_real_data(db_path, on_error="raise")
    finally:
        db_path.chmod(0o644)  # restore so tmp_path cleanup can remove it


# ---------------------------------------------------------------------------
# Round 2 remediation (flagged post-review): ChunkStore's OWN immutable-open
# branch has the same unescaped-URI defect Finding A fixed for
# chunk_store_has_real_data's mode=ro URI, just for the `?immutable=1` URI
# instead. A db_path containing '?'/'#'/'%'/space/unicode misparses under
# SQLite's URI rules the same way -- the literal special character truncates
# the path, so the immutable connection either fails to find the real file
# (false negative) or -- since the truncated "path" carries no immutable=1
# once the '?' onward is lost -- opens in SQLite's default read-write-create
# mode, creating a stray file. Same class of bug as Finding A, different
# call site (ChunkStore._open_connection's immutable branch,
# `sqlite_chunk_store.py` ~line 150), fixed with the same technique.
# ---------------------------------------------------------------------------


def test_immutable_chunk_store_at_uri_special_char_path_reads_real_data_no_stray_files(
    tmp_path,
):
    """A real, populated chunks.db living at a path containing URI-special
    characters must still open correctly and read real data when opened
    IMMUTABLE (open_chunk_store_for_path's immutable=True branch) -- mirrors
    test_populated_chunks_db_at_uri_special_char_path_returns_true_no_stray_files
    above, but for ChunkStore's own immutable-open URI construction rather
    than chunk_store_has_real_data's mode=ro URI."""
    special_dir = tmp_path / "repo?a#b c%d_üñí"
    special_dir.mkdir()
    db_path = special_dir / "chunks.db"

    _write_one_real_row(db_path)

    before = sorted(str(p) for p in tmp_path.rglob("*"))

    store = ChunkStore(db_path, immutable=True)
    try:
        result_count = store.count()
    finally:
        store.close()

    after = sorted(str(p) for p in tmp_path.rglob("*"))

    assert result_count == 1, (
        "URI-special characters in the path must not prevent the immutable "
        "ChunkStore from reading real, already-committed data"
    )
    assert before == after, (
        "Opening an immutable ChunkStore must never create stray "
        f"files/dirs from URI misparsing; before={before} after={after}"
    )
