import json
from pathlib import Path

from code_indexer.server.services.temporal_legacy_migration.mover import (
    migrate_temporal_shards,
)
from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    verify_shard_copy,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.storage.temporal_metadata_sqlite_backend import (
    TemporalMetadataSqliteBackend,
)


def test_empty_fixed_root_is_published_atomically_and_second_run_is_noop(
    tmp_path: Path,
):
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-embedder-2026Q1"
    shard.mkdir(parents=True)
    (shard / "chunks.db").write_bytes(b"sqlite-data")
    (shard / "collection_meta.json").write_text('{"name":"q1"}')
    (shard / "hnsw_index.bin").write_bytes(b"hnsw-data")
    (shard / "vector_p1.json").write_text(json.dumps({"id": "p1", "vector": [1.0]}))

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


def _write_vector_shard(shard_dir: Path, point_id: str, source: str) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": point_id, "vector": [1.0], "payload": {"source": source}}
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


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
