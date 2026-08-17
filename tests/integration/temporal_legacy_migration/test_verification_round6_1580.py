"""Issue #1580 adversarial-review round 6 (Codex) findings.

Three specific, well-scoped defects, all in
``server/services/temporal_legacy_migration/verification.py``:

1. HIGH -- ``chunks.db-journal`` (a real SQLite rollback-journal sidecar
   created by ``ChunkStore``'s ``PRAGMA journal_mode=DELETE`` during an
   in-progress write, per ``storage/sqlite_chunk_store.py``) was not
   exempted by the transient-file predicate, so a verification walk
   observing it mid-write on a CHUNKS_DB target reproduces the ORIGINAL
   #1580 symptom (a legitimate in-place refresh misclassified as a
   collision) for CHUNKS_DB targets specifically. The fix
   (``_is_sqlite_sidecar_artifact``) is anchored to the EXACT root-relative
   path -- never a basename match at any depth -- since ``chunks.db``
   itself is only ever created directly at a shard's root.
2. MEDIUM -- round 5's UUID-staging regex (``r".+\\.tmp\\.[0-9a-f]{32}"``)
   is too BROAD: the ``.+`` prefix accepts ANY filename ahead of
   ``.tmp.<32-hex>``, including disguised real-content names like
   ``vector_deadbeef.json.tmp.<uuid>``. The only real production writer of
   this exact staging shape is ``temporal_projection_matrix.py``'s
   ``_atomic_replace_via_tmp``, which always stages
   ``projection_matrix.npy`` specifically -- the regex must be anchored to
   that exact filename.
3. MEDIUM -- ``_is_layout_migrated_absence`` used ``Path.exists()``, which
   silently swallows every ``OSError`` (permission denied, a stale NFS
   handle, ENOTDIR) and returns ``False`` regardless of the reason,
   collapsing "genuinely absent" and "could not be inspected" onto the
   same verdict. An inability to verify must propagate as a verification
   failure (this project's established fail-closed discipline, e.g.
   ``chunk_store_has_real_data(..., on_error="raise")``), never be
   silently coerced into either verdict. Reproduced with ``ENOTDIR``
   (a plain file occupying a path position a directory is expected to be)
   -- deterministic, privilege-independent, no permission-bit games.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    _is_layout_migrated_absence,
    _is_transient_non_content_artifact,
    verify_source_subset_of_target,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _make_sharded_json_shard(path: Path, point_id: str, vector: list) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text('{"name": "q1"}')
    record = {"id": point_id, "vector": vector, "payload": {"source": "x"}}
    (path / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_chunks_db_shard(path: Path, point_id: str, vector: list) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text(
        json.dumps({"chunks_db": {"version": 1}})
    )
    store = ChunkStore(path / "chunks.db")
    try:
        store.write_batch(
            [{"id": point_id, "vector": vector, "payload": {"source": "x"}}]
        )
    finally:
        store.close()


@pytest.fixture
def sharded_and_chunks_shards(tmp_path: Path):
    """A SHARDED_JSON source shard and a matching CHUNKS_DB target shard
    for the SAME logical point -- the shared baseline several round-6
    integration tests build on before introducing one specific anomaly.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0])
    _make_chunks_db_shard(target, "p1", [1.0, 2.0])
    return source, target


# ---------------------------------------------------------------------------
# Finding 1 (HIGH): SQLite chunks.db-journal/-wal/-shm sidecars, anchored to
# the shard ROOT (never a loose basename/substring match).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["chunks.db-journal", "chunks.db-wal", "chunks.db-shm"],
)
def test_sqlite_sidecar_names_at_root_are_recognized(name: str):
    """RED against pre-fix code: ``_is_sqlite_sidecar_artifact`` does not
    exist pre-fix (deferred import raises ``ImportError`` for this test
    specifically, without breaking collection of the whole module). Post-fix
    it must recognize all three standard SQLite sidecar names at the shard
    root.
    """
    from code_indexer.server.services.temporal_legacy_migration.verification import (
        _is_sqlite_sidecar_artifact,
    )

    assert _is_sqlite_sidecar_artifact(name) is True


@pytest.mark.parametrize(
    "relative_path",
    [
        # Anchoring: a nested file sharing a sidecar name must not match.
        "sub/chunks.db-journal",
        "a/b/chunks.db-wal",
        # Exactness: no generic *-journal/*-wal/*-shm suffix pattern.
        "chunks.db",
        "other.db-journal",
        "chunks.db-journal.bak",
        "id_index.bin-journal",
    ],
)
def test_sqlite_sidecar_check_is_root_anchored_and_exact(relative_path: str):
    """The exemption must be anchored to the exact root-relative path
    (never a basename match at any depth) and must be an EXACT set of
    three names (never a generic suffix pattern) -- since ``chunks.db``
    itself is only ever created directly at a shard's root, a nested or
    unrelated file sharing part of the name is a genuine anomaly, not
    churn.
    """
    from code_indexer.server.services.temporal_legacy_migration.verification import (
        _is_sqlite_sidecar_artifact,
    )

    assert _is_sqlite_sidecar_artifact(relative_path) is False


@pytest.mark.parametrize(
    "sidecar_name", ["chunks.db-journal", "chunks.db-wal", "chunks.db-shm"]
)
def test_inflight_sidecar_files_do_not_trigger_false_collision(
    tmp_path: Path, sidecar_name: str
):
    """Integration-level reproduction through the real
    ``verify_source_subset_of_target`` entry point: a sidecar file present
    ONLY at the target (simulating a genuine in-progress write caught
    mid-flight by a verification walk) must never be treated as an
    unexpected addition.

    Deliberately SHARDED_JSON-to-SHARDED_JSON (not a real ``chunks.db``):
    ``_structural_manifest``'s file-exemption check is layout-agnostic --
    it never opens or interprets the sidecar's content, only its raw
    bytes -- so this isolates the reproduction to that check alone,
    without depending on SQLite's own (unrelated) hot-journal-recovery
    behavior when a same-named file sits next to a REAL database file (see
    ``test_inflight_chunks_db_journal_during_real_uncommitted_write_does_not_false_collide``
    below for that real-database reproduction).

    RED against pre-fix code: the sidecar file is not recognized as
    expected churn/addition and trips "unexpected file(s)".
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0])
    _make_sharded_json_shard(target, "p1", [1.0, 2.0])
    (target / sidecar_name).write_bytes(b"in-progress-sqlite-sidecar-bytes")

    verify_source_subset_of_target(source, target)  # must not raise


def test_inflight_chunks_db_journal_during_real_uncommitted_write_does_not_false_collide(
    tmp_path: Path,
):
    """Realistic, end-to-end reproduction of the actual production
    mechanism: ``chunks.db-journal`` is only ever produced by a REAL,
    currently-live, uncommitted write transaction (``PRAGMA
    journal_mode=DELETE``, per ``storage/sqlite_chunk_store.py``) -- never
    an orphaned leftover. Held open via a second, real connection for the
    duration of the verification call (empirically confirmed: this does
    NOT disturb a concurrent read-only ``ChunkStore`` open, unlike an
    orphaned/synthetic journal file, which SQLite's hot-journal-recovery
    logic tries to replay on open).

    RED against pre-fix code: the live journal file is not recognized as
    expected churn/addition and trips "unexpected file(s)".
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0])
    _make_chunks_db_shard(target, "p1", [1.0, 2.0])

    conn = sqlite3.connect(str(target / "chunks.db"))
    try:
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE _round6_inflight_probe (x INTEGER)")
        assert (target / "chunks.db-journal").exists()
        verify_source_subset_of_target(source, target)  # must not raise
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def test_nested_file_sharing_sidecar_name_is_still_a_collision(
    sharded_and_chunks_shards,
):
    """Guard-rail proving the root-anchoring discipline: a NESTED file that
    happens to share a sidecar name must NOT be exempted -- it is a
    genuine, unexpected addition and must still raise.
    """
    source, target = sharded_and_chunks_shards
    nested_dir = target / "unexpected_subdir"
    nested_dir.mkdir()
    (nested_dir / "chunks.db-journal").write_bytes(b"not-a-real-sidecar-here")

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)


def test_symlink_named_like_a_sidecar_is_still_rejected(tmp_path: Path):
    """Guard-rail: the Issue #1548 round-4 symlink-rejection discipline
    still applies even to a filename that matches a sidecar exemption --
    ``_structural_manifest``'s unconditional symlink check runs before the
    sidecar exemption is ever consulted.

    SHARDED_JSON-to-SHARDED_JSON (not a real ``chunks.db``) for the same
    reason as ``test_inflight_sidecar_files_do_not_trigger_false_collision``
    above -- a symlink literally named ``chunks.db-journal`` sitting next
    to a REAL database file triggers SQLite's own (unrelated) hot-journal
    recovery on open; the symlink check itself is layout-agnostic.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0])
    _make_sharded_json_shard(target, "p1", [1.0, 2.0])
    real_file = tmp_path / "elsewhere.bin"
    real_file.write_bytes(b"outside-target")
    (target / "chunks.db-journal").symlink_to(real_file)

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)


# ---------------------------------------------------------------------------
# Finding 2 (MEDIUM): the UUID-staging regex must be anchored to the exact
# real filename (``projection_matrix.npy``), not an arbitrary ``.+`` prefix.
# ---------------------------------------------------------------------------


def test_real_projection_matrix_staging_file_still_accepted():
    """Regression guard (accept direction): the real production staging
    name must still be recognized after tightening the regex.
    """
    name = f"projection_matrix.npy.tmp.{uuid.uuid4().hex}"
    assert _is_transient_non_content_artifact(name) is True


@pytest.mark.parametrize(
    "disguised_name",
    [
        "vector_deadbeef.json.tmp.{}",
        "garbage_projection_matrix.npy.tmp.{}",
        "evil.npy.tmp.{}",
        "xprojection_matrix.npy.tmp.{}",
    ],
)
def test_disguised_content_filenames_with_valid_uuid_suffix_are_rejected(
    disguised_name: str,
):
    """RED against pre-fix code: round 5's ``r".+\\.tmp\\.[0-9a-f]{32}"``
    matches ANY prefix ahead of a valid 32-hex-char suffix, so these
    disguised content filenames (real hex suffix, wrong/foreign prefix)
    were wrongly exempted from verification entirely. Only the exact real
    filename ``projection_matrix.npy`` may be followed by this staging
    suffix.
    """
    name = disguised_name.format(uuid.uuid4().hex)
    assert _is_transient_non_content_artifact(name) is False


def test_disguised_altered_content_is_detected_not_silently_tolerated(
    tmp_path: Path,
):
    """Integration-level reproduction: a disguised staging-shaped filename
    that is NOT the real projection-matrix write must be treated as
    ordinary content -- present only at target (never at source) is a
    genuine unexpected addition, never tolerated churn.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0])
    _make_sharded_json_shard(target, "p1", [1.0, 2.0])

    disguised = f"vector_deadbeef.json.tmp.{uuid.uuid4().hex}"
    (target / disguised).write_bytes(b"disguised-content-payload")

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)


# ---------------------------------------------------------------------------
# Finding 3 (MEDIUM): ``_is_layout_migrated_absence`` must fail closed
# (raise) on an ambiguous filesystem error, never silently return False.
# ---------------------------------------------------------------------------


def test_migrated_absence_regression_baseline(tmp_path: Path):
    """Regression guard bundling the pre-existing, unaffected behaviors:
    genuine absence (ENOENT, both root-level and under a real, healthy
    subdirectory) is still tolerated as a migrated absence; an existing
    file is never treated as absent; the non-CHUNKS_DB layout gate still
    short-circuits; a symlink at the exact checked path is still handled
    by the pre-existing ``is_symlink()`` early-return (never routed into
    the new fail-closed stat path).
    """
    target = tmp_path / "target"
    target.mkdir()
    healthy_subdir = target / "real"
    healthy_subdir.mkdir()

    assert (
        _is_layout_migrated_absence(target, "id_index.bin", ChunkLayout.CHUNKS_DB)
        is True
    )
    assert (
        _is_layout_migrated_absence(target, "vector_p1.json", ChunkLayout.CHUNKS_DB)
        is True
    )
    assert (
        _is_layout_migrated_absence(
            target, "real/vector_p1.json", ChunkLayout.CHUNKS_DB
        )
        is True
    )

    (target / "id_index.bin").write_bytes(b"present")
    assert (
        _is_layout_migrated_absence(target, "id_index.bin", ChunkLayout.CHUNKS_DB)
        is False
    )

    assert (
        _is_layout_migrated_absence(target, "vector_p1.json", ChunkLayout.SHARDED_JSON)
        is False
    )

    (target / "linked.bin").symlink_to(target / "does-not-exist-anywhere")
    assert (
        _is_layout_migrated_absence(target, "linked.bin", ChunkLayout.CHUNKS_DB)
        is False
    )


def test_not_a_directory_error_raises_instead_of_returning_false(tmp_path: Path):
    """RED against pre-fix code: ``os.stat`` on a path that treats a plain
    FILE as if it were a directory component raises ``NotADirectoryError``
    (errno.ENOTDIR), never ``ENOENT``. Pre-fix, ``Path.exists()`` swallows
    this and returns ``False`` (not absent, or falls through and reports
    True, tolerated); either way no exception is raised, silently coercing
    an unverifiable state into a definite verdict. Deterministic and
    privilege-independent -- unlike a permission-bit test, ENOTDIR cannot
    be bypassed by running as root.
    """
    target = tmp_path / "target"
    target.mkdir()
    (target / "not_a_dir").write_bytes(b"i-am-a-file-not-a-directory")

    with pytest.raises(VerificationError):
        _is_layout_migrated_absence(
            target, "not_a_dir/vector_p1.json", ChunkLayout.CHUNKS_DB
        )


def test_ambiguous_absence_check_propagates_through_full_verification_pipeline(
    tmp_path: Path,
):
    """Integration-level reproduction through the real
    ``verify_source_subset_of_target`` entry point: a source-side nested
    ``vector_*.json`` path whose corresponding location at a CHUNKS_DB
    target is blocked by a non-ENOENT filesystem error (ENOTDIR) must
    propagate as ``VerificationError`` -- an inability to prove the legacy
    data survived the storage-layout migration must never silently
    authorize treating it as safe.

    The blocking file is named with a ``.tmp`` suffix so it is excluded
    from ``_structural_manifest`` entirely (pre-existing, unrelated
    machinery) on BOTH sides -- this isolates the reproduction to the
    absence-check fix alone, without tripping the unrelated "unexpected
    file(s)" check for an unrelated reason.

    RED against pre-fix code: ``Path.exists()`` swallows the error and
    returns False, so the pre-fix code falls through to the vector_*.json
    pattern match and tolerates it as a "migrated absence" -- no exception
    is raised, and this test's ``pytest.raises`` fails.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"

    source.mkdir(parents=True)
    (source / "collection_meta.json").write_text('{"name": "q1"}')
    shard_dir = source / "shard.tmp"
    shard_dir.mkdir()
    record = {"id": "p1", "vector": [1.0, 2.0], "payload": {"source": "x"}}
    (shard_dir / "vector_p1.json").write_text(json.dumps(record))

    _make_chunks_db_shard(target, "p1", [1.0, 2.0])
    (target / "shard.tmp").write_bytes(b"not-a-directory")

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)
