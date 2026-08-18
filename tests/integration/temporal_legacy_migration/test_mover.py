import json
from pathlib import Path
from typing import Tuple

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration.mover import (
    MigrationResult,
    migrate_temporal_shards,
)
from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    _is_expected_churn_file,
    peek_one_vector_dimension,
    verify_shard_copy,
    verify_source_subset_of_target,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.storage.temporal_metadata_sqlite_backend import (
    TemporalMetadataSqliteBackend,
)


def _publish_single_point_shard(
    legacy: Path, fixed: Path, name: str = "code-indexer-temporal-e-2026Q1"
) -> Tuple[Path, Path]:
    """Write a single-point SHARDED_JSON legacy shard and publish it via a
    first ``migrate_temporal_shards`` pass. Shared setup for several Issue
    #1580 churn-allowlist tests below, which all start from an
    already-published shard and then plant a foreign file at the
    fixed-root target before running a second, cleanup-authorized pass.

    Returns ``(shard, fixed_shard)``.
    """
    shard = legacy / name
    _write_vector_shard(shard, "p1", "legacy")
    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    return shard, fixed / name


def _assert_is_unresolved_collision(result: MigrationResult, shard: Path) -> None:
    """Shared assertion for the Issue #1580 churn-allowlist tests: a
    planted foreign file must force an unresolved collision -- never
    ``already_complete``, never a deletion of the legacy source.
    """
    assert result.collisions == 1
    assert result.already_complete == 0
    assert result.deleted == 0
    assert shard.exists(), "legacy data must survive an unverified target addition"


def _write_real_hnsw_index(shard_dir: Path, point_id: str, vector: list) -> None:
    """Issue #1548 round-4 fix: ``_target_is_structurally_complete`` now
    attempts a genuine ``hnswlib`` load, so test fixtures claiming to be
    a "complete" shard must carry a real, loadable index -- not a fake
    placeholder byte string.
    """
    manager = HNSWIndexManager(vector_dim=len(vector), space="cosine")
    manager.build_index(shard_dir, np.array([vector], dtype=np.float32), [point_id])


def _write_real_hnsw_index_for_points(shard_dir: Path, points: list) -> None:
    """Issue #1580: rebuild a REAL, loadable HNSW index covering MULTIPLE
    points at once -- used to simulate the ordinary temporal write path
    refreshing an already-published fixed-root shard IN PLACE (Bug #1529)
    with a newly-indexed commit, without ever losing coverage of the
    points already present. ``points`` is a list of ``(point_id, vector)``.
    """
    ids = [point_id for point_id, _ in points]
    vectors = np.array([vector for _, vector in points], dtype=np.float32)
    manager = HNSWIndexManager(vector_dim=vectors.shape[1], space="cosine")
    manager.build_index(shard_dir, vectors, ids)


def test_sharded_json_empty_fixed_root_is_published_atomically_and_second_run_is_noop(
    tmp_path: Path,
):
    """SHARDED_JSON-layout-only test (Issue #1581 AC5 relabel).

    This test used to LOOK like it exercised a ``chunks.db``-bearing
    (``CHUNKS_DB``) shard, but ``(shard / "chunks.db").write_bytes(b"sqlite-
    data")`` below is NOT a real SQLite database, and ``collection_meta.json``
    below carries no ``chunks_db`` discriminator key -- per
    ``resolve_chunk_layout()`` (``storage/shared/chunk_layout.py``), an
    absent/malformed discriminator ALWAYS resolves to ``SHARDED_JSON``. So
    this test has always silently run the legacy JSON branch only; the
    ``chunks.db`` file is an inert fixture byte string, present here purely
    to prove an arbitrary non-record file also gets copied/preserved during
    publish -- it is never opened as a real chunk store. Genuine CHUNKS_DB
    coverage (real discriminator + real ``ChunkStore``-written database +
    real HNSW index) lives in
    ``test_chunks_db_shard_migrates_and_converges_issue_1581`` below.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-embedder-2026Q1"
    shard.mkdir(parents=True)
    # Inert fixture byte string -- NOT a real SQLite file. See docstring.
    (shard / "chunks.db").write_bytes(b"sqlite-data")
    (shard / "collection_meta.json").write_text('{"name":"q1"}')
    (shard / "vector_p1.json").write_text(json.dumps({"id": "p1", "vector": [1.0]}))
    _write_real_hnsw_index(shard, "p1", [1.0])

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    assert (fixed / shard.name / "chunks.db").read_bytes() == b"sqlite-data"
    assert shard.exists()

    second = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert second.published == 0
    assert second.already_complete == 1
    assert second.failed == 0
    assert shard.exists()


def _write_vector_shard(
    shard_dir: Path, point_id: str, source: str, *, complete: bool = True
) -> None:
    """Default: a complete shard (meta+hnsw). ``complete=False``: bare records only."""
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": point_id, "vector": [1.0], "payload": {"source": source}}
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))
    if complete:
        (shard_dir / "collection_meta.json").write_text('{"name":"q1"}')
        _write_real_hnsw_index(shard_dir, point_id, [1.0])


def test_diverging_fixed_shard_is_a_collision_and_neither_side_is_touched(
    tmp_path: Path,
):
    """Issue #1548 blocker 2: the locked policy is "new shard wins" in the
    sense that the FIXED-ROOT copy is authoritative and is never
    overwritten -- but it must NEVER fall through to deleting a legacy
    shard that genuinely diverges from it. Both sides are left untouched
    and the divergence is counted as a collision for later manual review.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    legacy_shard = legacy / "code-indexer-temporal-e-2026Q1"
    fixed_shard = fixed / legacy_shard.name
    _write_vector_shard(legacy_shard, "p1", "legacy")
    _write_vector_shard(fixed_shard, "p1", "fixed")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert result.collisions == 1
    assert result.deleted == 0
    assert result.already_complete == 0
    assert result.published == 0
    assert result.failed == 0
    assert legacy_shard.exists()
    fixed_record = json.loads((fixed_shard / "vector_p1.json").read_text())
    assert fixed_record["payload"]["source"] == "fixed"
    legacy_record = json.loads((legacy_shard / "vector_p1.json").read_text())
    assert legacy_record["payload"]["source"] == "legacy"


def test_coincidentally_matching_fixed_shard_with_no_provenance_is_a_collision(
    tmp_path: Path,
):
    """Issue #1548 review finding 1: coincidentally matching content at the
    fixed root -- created independently of this migration mechanism, so it
    carries no provenance marker -- must be treated exactly like diverging
    content: a collision. "New shard wins" is unconditional; a byte-for-byte
    match is never, by itself, proof that the fixed-root copy is safe to
    treat as "our own prior verified work". Both sides survive untouched.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    legacy_shard = legacy / "code-indexer-temporal-e-2026Q1"
    fixed_shard = fixed / legacy_shard.name
    _write_vector_shard(legacy_shard, "p1", "same")
    _write_vector_shard(fixed_shard, "p1", "same")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=True
    )
    assert result.already_complete == 0
    assert result.collisions == 1
    assert result.deleted == 0
    assert result.failed == 0
    assert legacy_shard.exists(), "legacy copy must survive an unproven match"
    assert (fixed_shard / "vector_p1.json").exists()


def test_cleanup_never_happens_without_explicit_authorization(tmp_path: Path):
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    shard.mkdir(parents=True)
    (shard / "chunks.db").write_bytes(b"data")

    published = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert published.failed == 0
    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=False
    )
    assert result.deleted == 0
    assert result.failed == 0
    assert shard.exists()


def test_stray_half_baked_fixed_dir_is_never_treated_as_verified_data(
    tmp_path: Path,
):
    """Issue #1548 blocker 1 regression: a fixed-root directory containing
    nothing but a stray collection_meta.json (zero real chunk rows) must
    NEVER be treated as "already migrated". Reproduces the confirmed data
    loss where a directory-emptiness check let cleanup destroy 5 real
    legacy vector records because the target merely EXISTED.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    name = "code-indexer-temporal-voyage-2026Q1"
    shard = legacy / name
    shard.mkdir(parents=True)
    for i in range(5):
        (shard / f"vector_p{i}.json").write_text(
            json.dumps({"id": f"p{i}", "vector": [1.0]})
        )

    stray_target = fixed / name
    stray_target.mkdir(parents=True)
    (stray_target / "collection_meta.json").write_text("{}")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=True
    )

    assert result.deleted == 0
    assert result.collisions == 0
    assert result.failed == 0
    assert shard.exists()
    remaining = list(shard.glob("vector_*.json"))
    assert len(remaining) == 5, "legacy data must never be destroyed"


def test_cleanup_refuses_when_fixed_root_directory_is_missing(tmp_path: Path):
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-voyage-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=True
    )

    assert result.deleted == 0
    assert result.failed == 0
    assert shard.exists()


def test_verifier_detects_field_corruption_after_copy(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    record = {"id": "p1", "vector": [1.0], "payload": {"text": "source"}}

    (source / "vector_p1.json").write_text(json.dumps(record))
    (target / "vector_p1.json").write_text(
        json.dumps({**record, "payload": {"text": "corrupt"}})
    )
    try:
        verify_shard_copy(source, target)
    except VerificationError:
        pass
    else:
        raise AssertionError("field corruption must fail verification")


def test_metadata_scope_is_copied_once_per_repo_and_deleted_only_after_full_cleanup(
    tmp_path: Path,
):
    """Issue #1548 blocker 3: the bookkeeping-collection path is
    ``legacy_root/code-indexer-temporal`` (shared per repo across every
    shard), never a per-shard path -- and delete_collection_scope() must
    only fire once every legacy shard for the repo has been verified gone.
    Uses the REAL SQLite metadata backend end-to-end.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")
    meta_dir = legacy / LEGACY_TEMPORAL_COLLECTION
    backend = TemporalMetadataSqliteBackend(meta_dir)
    backend.save_metadata("p1", {"commit_hash": "abc", "path": "f.py"})

    first = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )
    assert first.published == 1
    assert first.failed == 0
    fixed_meta = TemporalMetadataSqliteBackend(fixed / LEGACY_TEMPORAL_COLLECTION)
    assert fixed_meta.count_entries() == 1
    assert shard.exists(), "cleanup was not authorized yet"

    second = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()
    assert not (meta_dir / TemporalMetadataSqliteBackend.METADATA_DB_NAME).exists()
    # The re-keyed copy at the fixed root must survive the legacy delete.
    assert (
        fixed
        / LEGACY_TEMPORAL_COLLECTION
        / TemporalMetadataSqliteBackend.METADATA_DB_NAME
    ).exists()
    assert (
        TemporalMetadataSqliteBackend(
            fixed / LEGACY_TEMPORAL_COLLECTION
        ).count_entries()
        == 1
    )


def test_metadata_delete_is_withheld_while_a_sibling_shard_is_not_yet_migrated(
    tmp_path: Path,
):
    """The shared metadata scope must not be deleted while ANY shard of
    the same repo still needs it -- deleting it early would destroy rows
    a not-yet-migrated sibling shard still requires.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard_a = legacy / "code-indexer-temporal-e-2026Q1"
    shard_b = legacy / "code-indexer-temporal-e-2026Q2"
    _write_vector_shard(shard_a, "p1", "legacy")
    _write_vector_shard(shard_b, "p1", "legacy")
    meta_dir = legacy / LEGACY_TEMPORAL_COLLECTION
    backend = TemporalMetadataSqliteBackend(meta_dir)
    backend.save_metadata("p1", {"commit_hash": "abc", "path": "f.py"})

    # shard_b has already diverged at the fixed root (a collision) so it
    # can never be cleaned up automatically -- shard_a alone must not be
    # enough to trigger metadata deletion.
    _write_vector_shard(fixed / shard_b.name, "p1", "fixed-diverged")

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )

    assert result.collisions == 1
    assert result.failed == 0
    assert (meta_dir / TemporalMetadataSqliteBackend.METADATA_DB_NAME).exists()
    assert shard_b.exists()
    assert not shard_a.exists()


def test_verifier_compares_real_chunks_db_records(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for root in (source, target):
        (root / "collection_meta.json").write_text('{"chunks_db":{"version":1}}')
        store = ChunkStore(root / "chunks.db")
        store.write_batch(
            [{"id": "p1", "vector": [1.0, 2.0], "payload": {"text": "ok"}}]
        )
        store.close()
    verify_shard_copy(source, target)
    target_store = ChunkStore(target / "chunks.db")
    target_store.write_batch(
        [{"id": "p1", "vector": [1.0, 2.0], "payload": {"text": "bad"}}]
    )
    target_store.close()
    try:
        verify_shard_copy(source, target)
    except VerificationError:
        pass
    else:
        raise AssertionError("chunk record corruption must fail verification")


def test_orphaned_staging_directory_is_swept_before_a_new_pass(tmp_path: Path):
    """Issue #1548 blocker 8: a crash between copytree and rename leaves an
    orphaned ``.{name}.staging-{uuid}`` directory. The next pass must sweep
    it away rather than leaving it permanently on disk.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    name = "code-indexer-temporal-voyage-2026Q1"
    shard = legacy / name
    _write_vector_shard(shard, "p1", "legacy")
    fixed.mkdir(parents=True)
    orphan = fixed / f".{name}.staging-deadbeef"
    orphan.mkdir(parents=True)
    (orphan / "leftover.txt").write_text("orphaned from a crashed process")

    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)

    assert result.published == 1
    assert result.failed == 0
    assert not orphan.exists()
    assert (fixed / name / "vector_p1.json").exists()


def test_one_bad_shard_does_not_abort_the_rest_of_the_pass(tmp_path: Path):
    """Issue #1548 blocker 8: per-shard failures must be isolated."""
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    broken_shard = legacy / "code-indexer-temporal-broken-2026Q1"
    broken_shard.mkdir(parents=True)
    # An invalid record (a list, not a dict with a string "id") makes
    # verify_shard_copy raise VerificationError mid-publish.
    (broken_shard / "vector_bad.json").write_text(json.dumps(["not", "a", "dict"]))

    good_shard = legacy / "code-indexer-temporal-good-2026Q1"
    _write_vector_shard(good_shard, "p1", "legacy")

    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)

    assert result.failed == 1
    assert result.published == 1
    assert (fixed / good_shard.name / "vector_p1.json").exists()
    assert broken_shard.exists()


def test_pre_publish_hook_runs_before_atomic_rename(tmp_path: Path):
    """Issue #1548 blocker 7: the production env-var busy-wait is replaced
    by an explicit, test-only injected callable.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")

    observed = []

    def hook() -> None:
        observed.append((fixed / shard.name).exists())

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, pre_publish_hook=hook
    )

    assert result.published == 1
    assert result.failed == 0
    assert observed == [False], "hook must run before the target is published"


def test_genuine_two_pass_migration_converges_via_provenance_digest(tmp_path: Path):
    """Issue #1548 review finding 1: a LEGITIMATE multi-pass migration (this
    migration mechanism itself relocates now, cleanup authorized on a later
    call) must still converge -- the content-bound provenance marker written
    by ``_publish()`` is what distinguishes this from the coincidental-match
    collision case above.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    assert shard.exists(), "cleanup was not authorized yet"

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.already_complete == 1
    assert second.collisions == 0
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()
    assert (fixed / shard.name / "vector_p1.json").exists()


def _write_chunks_db_shard(shard_dir: Path, point_id: str, vector: list) -> None:
    """Build a GENUINE ``CHUNKS_DB``-layout legacy shard end to end (Issue
    #1581): a real ``{"chunks_db": {"version": 1}}`` discriminator (verified
    by callers against ``resolve_chunk_layout`` rather than assumed), a real
    ``chunks.db`` populated via ``ChunkStore.write_batch(...)`` (never a fake
    byte string), and a real, loadable HNSW index built over that same data
    via ``HNSWIndexManager`` -- mirroring ``_write_real_hnsw_index``'s intent
    for the ``CHUNKS_DB`` case. ``HNSWIndexManager._update_metadata`` merges
    into (rather than overwrites) an existing ``collection_meta.json``, so
    writing the discriminator first and building the index second preserves
    the ``chunks_db`` key.
    """
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "collection_meta.json").write_text(
        json.dumps({"chunks_db": {"version": 1}})
    )
    store = ChunkStore(shard_dir / "chunks.db")
    try:
        store.write_batch(
            [{"id": point_id, "vector": vector, "payload": {"source": "legacy"}}]
        )
    finally:
        store.close()
    _write_real_hnsw_index(shard_dir, point_id, vector)


def _pin_failure_to_peek_one_vector_dimension(
    fixed_shard: Path, point_id: str, vector: list
) -> None:
    """Issue #1581: prove the PUBLISHED record's real vector type is
    ``numpy.ndarray`` (never a ``list``), then show
    ``peek_one_vector_dimension()`` fails to report its dimension for
    exactly that reason -- pinning the failure to this specific function
    rather than an unrelated fixture mistake.
    """
    store = ChunkStore(fixed_shard / "chunks.db")
    try:
        raw_record = store.read(point_id)
    finally:
        store.close()
    assert raw_record is not None
    raw_vector = raw_record["vector"]
    assert isinstance(raw_vector, np.ndarray), (
        "fixture invalid: ChunkStore must return a numpy.ndarray vector "
        "for a genuine CHUNKS_DB record -- this test would otherwise be "
        "repeating the same fixture mistake Issue #1581 documents"
    )
    assert not isinstance(raw_vector, list)

    dim = peek_one_vector_dimension(fixed_shard)
    assert dim == len(vector), (
        f"peek_one_vector_dimension() returned {dim!r} for a genuine "
        f"CHUNKS_DB shard whose record's real vector type is "
        f"{type(raw_vector)!r} -- isinstance(vector, list) unconditionally "
        f"rejects numpy.ndarray (Issue #1581)"
    )


def test_chunks_db_shard_migrates_and_converges_issue_1581(tmp_path: Path):
    """Issue #1581 regression: ``peek_one_vector_dimension()`` used
    ``isinstance(vector, list)``, which is ALWAYS ``False`` for a
    ``CHUNKS_DB`` shard's record -- ``ChunkStore._row_to_record`` /
    ``_decode_vector`` always return a ``numpy.ndarray``, never a ``list``.
    That made ``_hnsw_index_structurally_valid()`` -> ``_target_is_
    structurally_complete()`` permanently ``False`` for every genuinely
    valid ``CHUNKS_DB`` shard, so a shard correctly published on pass 1 was
    reclassified as a ``collision`` (never ``already_complete``) on every
    subsequent pass, forever -- exactly the confirmed production symptom of
    19 permanently stuck shards.

    Drives the ACTUAL ``migrate_temporal_shards()`` entry point (not
    ``verify_shard_copy()`` in isolation) across two passes, mirroring
    ``test_genuine_two_pass_migration_converges_via_provenance_digest``'s
    JSON-layout shape exactly, so the SAME test is RED against the current
    (unpatched) code and GREEN once the fix lands.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    vector = [1.0, 2.0, 3.0]
    _write_chunks_db_shard(shard, "p1", vector)

    # Fixture sanity: this MUST resolve to CHUNKS_DB, never SHARDED_JSON --
    # the exact fixture mistake
    # test_empty_fixed_root_is_published_atomically_and_second_run_is_noop
    # made (a fake chunks.db byte string + a discriminator-less meta file).
    assert resolve_chunk_layout(shard) is ChunkLayout.CHUNKS_DB

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    assert shard.exists(), "cleanup was not authorized yet"

    fixed_shard = fixed / shard.name
    assert resolve_chunk_layout(fixed_shard) is ChunkLayout.CHUNKS_DB
    _pin_failure_to_peek_one_vector_dimension(fixed_shard, "p1", vector)

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.already_complete == 1
    assert second.collisions == 0
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()
    assert (fixed_shard / "chunks.db").exists()


def test_chunks_db_fresh_publish_passes_completeness_gate_under_cleanup_authorized_issue_1581(
    tmp_path: Path,
):
    """Codex review gap on Issue #1581 AC3 (closes it): AC3 requires proof
    that a CHUNKS_DB shard "publish[es] cleanly, pass[es] the post-publish
    completeness gate under cleanup_authorized=True (no VerificationError)".

    ``test_chunks_db_shard_migrates_and_converges_issue_1581`` above never
    actually exercised that gate on a FRESH publish: its first call omits
    ``cleanup_authorized``, and by the time its second call passes
    ``cleanup_authorized=True`` the shard is already published, so
    ``_process_one_shard`` classifies it as ``already_complete`` -- a
    different code branch that never runs the
    ``outcome == "published" and cleanup_authorized`` gate in
    ``mover._process_one_shard`` at all.

    This test calls ``migrate_temporal_shards(..., cleanup_authorized=True)``
    as the FIRST and ONLY call, so ``cleanup_authorized=True`` is present on
    the fresh-publish call itself. Under the pre-#1581 bug --
    ``peek_one_vector_dimension()``'s ``isinstance(vector, list)`` check,
    which is unconditionally ``False`` for a real ``ChunkStore``-returned
    ``numpy.ndarray`` -- ``_hnsw_index_structurally_valid`` (and therefore
    ``_target_is_structurally_complete``) would be ``False`` immediately
    after a genuinely valid fresh publish, so ``_process_one_shard`` raises
    ``VerificationError`` for this exact shard; ``_run_shard_pass`` catches
    it and folds it into ``MigrationResult.failed`` rather than
    ``published`` -- so this test is RED against the unpatched code (proven
    below) and GREEN against the fix.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    vector = [1.0, 2.0, 3.0]
    _write_chunks_db_shard(shard, "p1", vector)

    # Fixture sanity: must be a genuine CHUNKS_DB shard, not the fake-bytes
    # SHARDED_JSON fixture mistake documented at the top of this file.
    assert resolve_chunk_layout(shard) is ChunkLayout.CHUNKS_DB

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )

    assert result.published == 1, (
        "a fresh, genuinely valid CHUNKS_DB publish must pass the "
        "post-publish completeness gate under cleanup_authorized=True "
        "without being reclassified as a failure"
    )
    assert result.collisions == 0
    assert result.failed == 0
    assert result.deleted == 1
    assert not shard.exists(), "cleanup_authorized=True must delete the source"
    fixed_shard = fixed / shard.name
    assert resolve_chunk_layout(fixed_shard) is ChunkLayout.CHUNKS_DB
    assert (fixed_shard / "chunks.db").exists()


def test_forged_sentinel_marker_with_no_matching_digest_is_still_a_collision(
    tmp_path: Path,
):
    """A bare marker FILE (no digest content, or a digest that does not
    match) must never be sufficient to authorize "already_complete" --
    provenance is content-bound, not presence-bound. This closes the
    forgery gap a plain sentinel-file marker would have left open.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    legacy_shard = legacy / "code-indexer-temporal-e-2026Q1"
    fixed_shard = fixed / legacy_shard.name
    _write_vector_shard(legacy_shard, "p1", "same")
    _write_vector_shard(fixed_shard, "p1", "same")
    # Plant a marker file with an unrelated/garbage digest value.
    (fixed_shard / ".legacy-migration-provenance").write_text("not-a-real-digest")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=False, cleanup_authorized=True
    )
    assert result.already_complete == 0
    assert result.collisions == 1
    assert result.deleted == 0
    assert legacy_shard.exists()


def test_metadata_cleanup_refuses_when_repo_has_zero_shards_and_never_relocated(
    tmp_path: Path,
):
    """Issue #1548 review finding 5: a legacy root with literally zero
    temporal shards vacuously satisfies "all shards gone" -- that alone
    must never authorize deleting a metadata scope that was never actually
    relocated (relocation_enabled=False means the copy step never even ran).
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    meta_dir = legacy / LEGACY_TEMPORAL_COLLECTION
    backend = TemporalMetadataSqliteBackend(meta_dir)
    backend.save_metadata("p1", {"commit_hash": "abc", "path": "f.py"})
    legacy.mkdir(parents=True, exist_ok=True)

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=False,
        cleanup_authorized=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )

    assert result.failed == 0
    assert (meta_dir / TemporalMetadataSqliteBackend.METADATA_DB_NAME).exists(), (
        "metadata must survive -- it was never relocated to the fixed root"
    )


def test_target_legitimately_refreshed_in_place_converges_instead_of_permanent_collision_1580(
    tmp_path: Path,
):
    """Issue #1580 regression: post-Bug #1529, the fixed-root target is
    refreshed IN PLACE by the ordinary temporal write path -- new commits
    keep landing there after this mechanism's original verified publish.
    Any such legitimate refresh changes ``manifest_digest(target)``, so the
    OLD classification rule (marker digest must equal BOTH the source's
    AND the target's CURRENT digest) permanently reclassified an
    already-published, fully-preserved shard as a "collision" forever --
    confirmed in production as 293 WARNINGs/23h for one repo (69 shards,
    zero progress across every pass). A target that still contains
    EVERYTHING this mechanism verified at publish time (additive evolution
    only -- nothing lost or altered) must converge instead of colliding
    forever.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    assert shard.exists(), "cleanup was not authorized yet"

    # Simulate a legitimate in-place refresh: the ordinary temporal write
    # path indexed a NEW commit at the fixed root, adding point p2 --
    # p1 (the data this migration already verified) is untouched.
    fixed_shard = fixed / shard.name
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _write_real_hnsw_index_for_points(fixed_shard, [("p1", [1.0]), ("p2", [2.0])])

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.collisions == 0, (
        "a legitimately-evolved target (source data fully preserved) must "
        "never be classified as an unresolvable collision"
    )
    assert second.already_complete == 1
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists(), "the stale legacy source must be reclaimed"
    # The evolved target's data (both the original and the new point) must
    # survive untouched -- this migration only ever deletes the legacy
    # source, never rewrites the fixed-root target.
    assert (fixed_shard / "vector_p1.json").exists()
    assert (fixed_shard / "vector_p2.json").exists()

    # A THIRD pass over the (now nonexistent) legacy source must be a
    # true no-op -- genuine convergence, not merely "one pass got lucky".
    third = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert third.collisions == 0
    assert third.failed == 0


def test_target_missing_previously_verified_record_is_still_a_collision_1580(
    tmp_path: Path,
):
    """Issue #1580 fix guard-rail: the relaxation above must be strictly
    ADDITIVE. A target that has LOST or ALTERED a record this mechanism
    already verified at publish time must still be treated as an
    unresolvable collision -- never silently authorized for legacy-source
    deletion just because it currently looks structurally complete.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0

    # The fixed-root target's own p1 record is altered post-publish (data
    # loss/corruption at the target, not an additive refresh) while still
    # remaining a structurally-complete, loadable shard on its own terms.
    fixed_shard = fixed / shard.name
    (fixed_shard / "vector_p1.json").write_text(
        json.dumps({"id": "p1", "vector": [1.0], "payload": {"source": "altered"}})
    )

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.collisions == 1
    assert second.already_complete == 0
    assert second.deleted == 0
    assert shard.exists(), "legacy data must survive an unproven target"


def test_evolved_target_no_longer_withholds_metadata_scope_copy_1580(tmp_path: Path):
    """Issue #1580 item 3 corollary: since a legitimately-evolved target is
    no longer counted as a collision (see the two tests above), the
    shared per-repo metadata scope must no longer be withheld on its
    account -- ``withhold_copy`` is derived from ``counts["collision"]``,
    which now correctly excludes this case.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", "legacy")
    meta_dir = legacy / LEGACY_TEMPORAL_COLLECTION
    backend = TemporalMetadataSqliteBackend(meta_dir)
    backend.save_metadata("p1", {"commit_hash": "abc", "path": "f.py"})

    first = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )
    assert first.published == 1
    assert first.failed == 0

    fixed_shard = fixed / shard.name
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _write_real_hnsw_index_for_points(fixed_shard, [("p1", [1.0]), ("p2", [2.0])])

    second = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )

    assert second.collisions == 0
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()
    assert not (meta_dir / TemporalMetadataSqliteBackend.METADATA_DB_NAME).exists(), (
        "metadata scope must be relocated+deleted once cleanup converges -- "
        "not withheld on account of a non-existent collision"
    )
    fixed_meta = TemporalMetadataSqliteBackend(fixed / LEGACY_TEMPORAL_COLLECTION)
    assert fixed_meta.count_entries() == 1


def test_shard_collision_withholds_metadata_scope_copy(tmp_path: Path):
    """Issue #1548 review finding 2: when a shard collision is detected,
    the shared per-repo metadata scope must NOT be copied at all this pass
    -- an ``INSERT OR REPLACE`` re-key could otherwise silently overwrite
    rows belonging to whatever independently produced the colliding data.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    legacy_shard = legacy / "code-indexer-temporal-e-2026Q1"
    fixed_shard = fixed / legacy_shard.name
    _write_vector_shard(legacy_shard, "p1", "legacy")
    _write_vector_shard(fixed_shard, "p1", "diverged")
    meta_dir = legacy / LEGACY_TEMPORAL_COLLECTION
    backend = TemporalMetadataSqliteBackend(meta_dir)
    backend.save_metadata("p1", {"commit_hash": "abc", "path": "f.py"})

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        metadata_backend_factory=lambda path: TemporalMetadataSqliteBackend(path),
    )

    assert result.collisions == 1
    assert result.failed == 0
    fixed_meta_db = (
        fixed
        / LEGACY_TEMPORAL_COLLECTION
        / (TemporalMetadataSqliteBackend.METADATA_DB_NAME)
    )
    assert not fixed_meta_db.exists(), "metadata copy must be withheld on collision"


def test_nested_hnsw_index_bin_is_not_recognized_as_churn_1580(tmp_path: Path):
    """Issue #1580 adversarial-review round-2 critical finding:
    ``_is_expected_churn_file`` matched by bare basename, unanchored to
    location -- so ``nested/hnsw_index.bin`` (or ``collection_meta.json``
    or ``chunks.db`` at any depth) was silently accepted as legitimate
    refresh churn. A foreign file planted at a nested location using an
    allowed basename must force a collision, not silently authorize
    legacy-source deletion.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard, fixed_shard = _publish_single_point_shard(legacy, fixed)

    # Legitimate in-place refresh (Bug #1529): a new commit adds p2.
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _write_real_hnsw_index_for_points(fixed_shard, [("p1", [1.0]), ("p2", [2.0])])
    # Foreign file planted at a NESTED location using an allowed basename.
    nested_dir = fixed_shard / "nested"
    nested_dir.mkdir()
    (nested_dir / "hnsw_index.bin").write_bytes(b"attacker-controlled bytes")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    _assert_is_unresolved_collision(result, shard)


def test_foreign_chunks_db_at_sharded_json_root_is_rejected_1580(tmp_path: Path):
    """Issue #1580 adversarial-review round-2 critical finding: ``chunks.db``
    was accepted as expected churn purely by basename, regardless of the
    target's actual resolved chunk layout. A SHARDED_JSON-layout target
    never legitimately owns a ``chunks.db`` artifact -- planting one at
    the shard root must force a collision.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard, fixed_shard = _publish_single_point_shard(legacy, fixed)
    assert resolve_chunk_layout(shard) is ChunkLayout.SHARDED_JSON
    assert resolve_chunk_layout(fixed_shard) is ChunkLayout.SHARDED_JSON

    (fixed_shard / "chunks.db").write_bytes(b"attacker-controlled bytes")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    _assert_is_unresolved_collision(result, shard)


def test_target_deleted_previously_verified_record_is_still_a_collision_1580(
    tmp_path: Path,
):
    """Issue #1580 test-gap guard-rail (adversarial review round 2, already
    passing before AND after this session's allowlist fix): the
    pre-existing "missing record" test above actually ALTERS p1's
    content, which fails identically under both the OLD exact-digest
    -equality rule and the NEW additive-evolution rule -- it never
    discriminated between them. This test instead DELETES p1 outright
    while keeping the target structurally complete via a different
    record (p2). A deleted record always changes
    ``manifest_digest(target)``, so the OLD exact-match rule already
    rejected this case too -- this test exists to positively confirm the
    additive-evolution relaxation stayed strictly additive, never
    silently tolerating a subtractive change, rather than to reproduce a
    bug.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard, fixed_shard = _publish_single_point_shard(legacy, fixed)

    (fixed_shard / "vector_p1.json").unlink()
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _write_real_hnsw_index_for_points(fixed_shard, [("p2", [2.0])])

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    _assert_is_unresolved_collision(result, shard)


def test_chunks_db_target_planted_extra_vector_json_is_rejected_1580(tmp_path: Path):
    """Issue #1580 adversarial-review round-2 critical finding: for a
    CHUNKS_DB target, an added vector_*.json file is not even included in
    _manifest() (it takes the SQLite branch) -- so it bypasses LOGICAL
    verification entirely. The old basename-only structural churn check
    also silently accepted it regardless of layout. It must instead be
    rejected for a CHUNKS_DB target, where such a file can never be
    verified.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    vector = [1.0, 2.0, 3.0]
    _write_chunks_db_shard(shard, "p1", vector)
    assert resolve_chunk_layout(shard) is ChunkLayout.CHUNKS_DB

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0

    fixed_shard = fixed / shard.name
    assert resolve_chunk_layout(fixed_shard) is ChunkLayout.CHUNKS_DB
    # Planted foreign vector JSON -- invisible to _manifest() for this
    # layout, so unless the structural churn check is layout-aware it
    # bypasses inspection entirely.
    (fixed_shard / "vector_planted.json").write_text(
        json.dumps({"id": "planted", "vector": [9.9]})
    )

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    _assert_is_unresolved_collision(result, shard)


def test_is_expected_churn_file_rejects_directory_with_allowed_basename_1580(
    tmp_path: Path,
):
    """Issue #1580 adversarial-review round-2 critical finding: a
    DIRECTORY using one of the allowed churn filenames must never be
    treated as a valid churn artifact -- ``_is_expected_churn_file`` must
    positively confirm the candidate is a real regular file, not merely
    match on name.
    """
    target = tmp_path / "target"
    target.mkdir()
    (target / "hnsw_index.bin").mkdir()

    assert not _is_expected_churn_file(
        target, "hnsw_index.bin", ChunkLayout.SHARDED_JSON
    )


def test_is_expected_churn_file_rejects_symlink_with_allowed_basename_1580(
    tmp_path: Path,
):
    """Issue #1580 adversarial-review round-2 critical finding: a SYMLINK
    using one of the allowed churn filenames must never be treated as a
    valid churn artifact -- matches the existing Issue #1548 round-4
    symlink-rejection discipline used elsewhere in this module.
    """
    target = tmp_path / "target"
    target.mkdir()
    real_file = tmp_path / "real_target.bin"
    real_file.write_bytes(b"data")
    (target / "hnsw_index.bin").symlink_to(real_file)

    assert not _is_expected_churn_file(
        target, "hnsw_index.bin", ChunkLayout.SHARDED_JSON
    )


def test_empty_source_manifest_never_authorizes_convergence_1580(tmp_path: Path):
    """Issue #1580 test gap (adversarial review round 2): an empty source
    point-record manifest must never vacuously satisfy subset
    verification. An empty source proves nothing was ever verified there
    in the first place, so it can never be positive proof that "nothing
    was lost" during a legitimate refresh -- it must be refused, not
    silently treated as trivially converged/safe to delete.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    # Both sides genuinely hold zero point records.
    (source / "collection_meta.json").write_text('{"name":"q1"}')
    (target / "collection_meta.json").write_text('{"name":"q1"}')

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)
