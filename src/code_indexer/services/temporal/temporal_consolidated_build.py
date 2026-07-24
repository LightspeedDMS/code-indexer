"""Shared "build-fresh-consolidated-version" primitive (Story #1457 AC6).

AC6's three build branches (A / B-bootstrap / B-fresh) and AC11's bootstrap
all reuse ONE shared primitive that builds a NEW, collision-safe version
directory containing a consolidated `chunks.db`-based temporal shard, from a
MULTI-SOURCE row input: AC11 bootstrap calls it with `[legacy_scan]`; AC6
Branch B-bootstrap with `[legacy_scan, new_delta]`; AC6 Branch B-fresh with
`[new_delta]`.

Reuses existing, already-tested infrastructure verbatim rather than
reimplementing it:
  - `ChunkStore` (Story #1455) for the `chunks.db` write.
  - `HNSWIndexManager.rebuild_from_vectors(..., layout_override=CHUNKS_DB)`
    (Story #1456) for the HNSW index build -- `layout_override` exists
    specifically "for the fresh-CHUNKS_DB-build orchestrator, which knows
    FOR CERTAIN it just wrote chunks.db but has not yet committed the
    on-disk discriminator."
  - `write_chunks_db_discriminator` (Story #1456) for the discriminator
    commit, invoked ONLY after the HNSW index is fully built and durable --
    matching that function's own documented ordering contract exactly.

Also provides `copy_and_extend_consolidated_temporal_version` for AC6
Branch A (pointer EXISTS): reflink/copy the current v_* into a fresh
collision-safe v_{unix_ts} via `VersionedSnapshotManager.create_snapshot`
(the Story #1034 AC15 anti-orphan-approved abstraction -- NEVER a direct
`subprocess.run(["cp", "--reflink=auto", ...])`, which the lint gate bans
in production code outside that one designated owner), then apply ONLY the
new-commit delta rows into the copy's chunk store and rebuild the HNSW
index -- the historical rows survive via the copy, never re-streamed.

NOTE (honest scope disclosure): this module implements ONLY the build
mechanics given already-prepared row dicts. It does NOT implement:
  - Row SOURCING (reading legacy `vector_*.json` files via a side-effect-free
    scan, or computing new-commit delta rows from git) -- callers must
    supply already-prepared `row_sources`/`delta_rows`.
  - The three-branch DECISION logic (which branch to take, per AC6's
    resolver-driven dispatch) -- callers decide which function/row sources
    to use.
  - Field-for-field read-back verification beyond count/spot-check (the
    caller is expected to perform the story's mandated full verification
    before publishing).
  - Publication -- see `temporal_shard_publisher.publish_temporal_shard_version`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

import numpy as np

from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore


def _extract_commit_hashes(records: Iterable[Dict[str, Any]]) -> List[str]:
    """Parse commit hashes out of point_ids using the SAME
    "{project}:commit:{hash}:{j}" scheme `_reconcile_shard_chunks_db`
    (temporal_reconciliation.py) already parses -- kept in sync
    deliberately so a commit this function marks complete is exactly the
    commit reconciliation later checks for a completion marker.
    """
    hashes = []
    for record in records:
        point_id = record.get("id", "")
        parts = point_id.split(":")
        if len(parts) == 4 and parts[1] == "commit":
            hashes.append(parts[2])
    return hashes


def _verify_consolidated_version(
    version_dir: Path, expected_records: List[Dict[str, Any]], vector_dim: int
) -> None:
    """Read-back, field-for-field verification BEFORE the discriminator
    commit (Story #1457 CRITICAL #4, 2026-07-23 code review).

    Re-opens the just-written chunks.db and confirms every expected
    record round-trips exactly (row count, id present, vector matches,
    payload matches) -- verify-then-commit, never commit-then-verify.
    Raises loudly on any mismatch rather than publishing a version whose
    on-disk durability was never actually confirmed.
    """
    # ChunkStore.write_batch is INSERT OR REPLACE, keyed by id -- a row
    # legitimately appearing in more than one row_source (the documented
    # "known, accepted redundancy" in temporal_relocation_trigger.py's
    # Branch B-bootstrap path) collapses to ONE stored row, last-write-
    # wins. Verification must compare against the SAME deduplicated set,
    # not the raw concatenated list, or it false-positives on this
    # legitimate, already-accepted overlap.
    deduplicated: Dict[str, Dict[str, Any]] = {}
    for record in expected_records:
        record_id = record.get("id")
        if not record_id:
            raise RuntimeError(
                f"Read-back verification failed for {version_dir}: an "
                f"expected record is missing a valid 'id'. Refusing to "
                f"publish."
            )
        deduplicated[record_id] = record
    unique_expected_records = list(deduplicated.values())

    store = ChunkStore(version_dir / "chunks.db", expected_dim=vector_dim)
    try:
        actual_count = store.count()
        if actual_count != len(unique_expected_records):
            raise RuntimeError(
                f"Read-back verification failed for {version_dir}: "
                f"expected {len(unique_expected_records)} unique rows, "
                f"chunks.db contains {actual_count}. Refusing to publish."
            )
        for record in unique_expected_records:
            point_id = record.get("id")
            stored = store.read(point_id)
            if stored is None:
                raise RuntimeError(
                    f"Read-back verification failed for {version_dir}: "
                    f"row {point_id!r} was written but is not readable "
                    f"back from chunks.db. Refusing to publish."
                )
            if stored.get("id") != point_id:
                raise RuntimeError(
                    f"Read-back verification failed for {version_dir}: "
                    f"row read back under key {point_id!r} has id "
                    f"{stored.get('id')!r} instead. Refusing to publish."
                )
            stored_vector = stored.get("vector")
            expected_vector = record.get("vector")
            if stored_vector is None or expected_vector is None:
                raise RuntimeError(
                    f"Read-back verification failed for {version_dir}: "
                    f"row {point_id!r} is missing a vector. Refusing to "
                    f"publish."
                )
            # ChunkStore quantizes vectors to float32 (<f4, AC3) -- exact
            # equality would spuriously fail on precision loss alone, so
            # compare within float32 tolerance.
            if not np.allclose(
                np.asarray(stored_vector, dtype=np.float32),
                np.asarray(expected_vector, dtype=np.float32),
                rtol=1e-5,
                atol=1e-6,
            ):
                raise RuntimeError(
                    f"Read-back verification failed for {version_dir}: "
                    f"row {point_id!r}'s vector does not match what was "
                    f"written. Refusing to publish."
                )
            if stored.get("payload") != record.get("payload"):
                raise RuntimeError(
                    f"Read-back verification failed for {version_dir}: "
                    f"row {point_id!r}'s payload does not match what was "
                    f"written. Refusing to publish."
                )
    finally:
        store.close()


if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )
    from code_indexer.storage.filesystem_vector_store import PathIndex

#: Story #1457 AC9-consistent bounded retry count for collision-checked
#: version-id generation (mirrors snapshot_manager.py / golden_repo_manager.py).
_MAX_VERSION_ID_COLLISION_RETRIES = 100


def _populate_path_index(records: Iterable[Dict[str, Any]]) -> "PathIndex":
    """Build a PathIndex from records' payload path field(s).

    Story #1457 CRITICAL #4 (2026-07-23 code review): mirrors the real
    per-commit payload shape (temporal_point_builder.py's `paths: List[str]`
    / `primary_path: str`) exactly -- no other field names. A record with
    no path field at all is skipped (no duplicate-prevention entry needed
    for it).
    """
    from code_indexer.storage.filesystem_vector_store import PathIndex

    path_index = PathIndex()
    for record in records:
        point_id = record.get("id")
        if not point_id:
            continue
        payload = record.get("payload") or {}
        paths = payload.get("paths")
        if not paths:
            single_path = payload.get("primary_path")
            paths = [single_path] if single_path else []
        for file_path in paths:
            path_index.add_point(file_path, point_id)
    return path_index


def _write_temporal_metadata(
    version_dir: Path, records: Iterable[Dict[str, Any]]
) -> None:
    """Write real point_id-to-hash-prefix mappings into temporal_metadata.db.

    Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review, Codex):
    a genuine AC6 solo-format requirement -- TemporalMetadataStore.detect_
    format() (dashboard_service.py's get_temporal_index_status) checks
    ONLY this file's existence, so an absent/empty temporal_metadata.db
    misreports a healthy CHUNKS_DB collection as legacy v1. Mirrors the
    SAME (point_id, payload) shape filesystem_vector_store.py's
    upsert_points already writes for the legacy sharded write path
    (TemporalMetadataSqliteBackend reads payload["commit_hash"],
    payload["path"], payload["chunk_index"]).
    """
    rows = [
        (record["id"], record.get("payload") or {})
        for record in records
        if record.get("id")
    ]
    if not rows:
        return
    store = TemporalMetadataStore(version_dir)
    store.save_metadata_batch(rows)
    store.checkpoint_wal()


def build_fresh_consolidated_temporal_version(
    sister_root: Path,
    pointer_namespace: str,
    row_sources: List[Iterable[Dict[str, Any]]],
    vector_dim: int,
    embedder_slug: Optional[str] = None,
) -> Path:
    """Build a new consolidated temporal shard version from row_sources.

    Args:
        sister_root: Root directory under which `.versioned/{ns}/v_*` lives
            (the sister location, outside the golden repo's cloned tree).
        pointer_namespace: The (embedder, quarter)-qualified namespace, e.g.
            "{repo_alias}-temporal-{embedder_slug}-{quarter}".
        row_sources: One or more iterables of chunk record dicts (each with
            at least "id" and "vector", per `ChunkStore.write_batch`). All
            sources are written into the SAME fresh `chunks.db`.
        vector_dim: Expected vector dimension for both `ChunkStore` and the
            HNSW index.
        embedder_slug: Story #1457 CRITICAL #4 -- sanitized embedder model
            slug, e.g. "voyage_code_3". When provided, the v2
            temporal_structure.json marker is also written (mandatory for
            every real production caller, both of which already have this
            value on hand). None (only used by pre-existing unit tests that
            predate this parameter) skips the marker -- this is the ONLY
            file this parameter gates; path_index.bin and
            projection_matrix.npy are always built regardless.

    Returns:
        The path to the new version directory. NOT YET published -- the
        caller must read-back verify and then call
        `publish_temporal_shard_version`.

    Raises:
        RuntimeError: collision-safe version-id generation exhausted its
            bounded retry count.
    """
    ns_dir = Path(sister_root) / ".versioned" / pointer_namespace
    ns_dir.mkdir(parents=True, exist_ok=True)

    candidate_ts = int(time.time())
    version_dir = ns_dir / f"v_{candidate_ts}"
    for _attempt in range(_MAX_VERSION_ID_COLLISION_RETRIES):
        if not version_dir.exists():
            break
        candidate_ts += 1
        version_dir = ns_dir / f"v_{candidate_ts}"
    else:
        raise RuntimeError(
            f"Failed to generate a collision-free version id for namespace "
            f"'{pointer_namespace}' after {_MAX_VERSION_ID_COLLISION_RETRIES} "
            f"attempts"
        )
    version_dir.mkdir(parents=True)

    # Base collection_meta.json (no discriminator yet) -- rebuild_from_vectors
    # reads "vector_dim" from this file.
    meta_path = version_dir / "collection_meta.json"
    # "vector_dim" is what HNSWIndexManager.rebuild_from_vectors reads;
    # "vector_size" is what FilesystemVectorStore.collection_exists() checks
    # for -- both are written so this version directory is recognized by
    # both consumers (Story #1457 AC7 needs collection_exists() to work
    # against these directories for reconciliation).
    meta_path.write_text(
        json.dumps({"vector_dim": vector_dim, "vector_size": vector_dim})
    )

    all_records: List[Dict[str, Any]] = []
    store = ChunkStore(version_dir / "chunks.db", expected_dim=vector_dim)
    try:
        for source in row_sources:
            batch = list(source)
            if batch:
                store.write_batch(batch)
                all_records.extend(batch)
    finally:
        store.close()

    # HNSW build: layout_override tells rebuild_from_vectors to stream from
    # chunks.db even though the discriminator is not committed yet (this IS
    # the documented purpose of layout_override -- the fresh-build
    # orchestrator knowing for certain it just wrote chunks.db).
    hnsw_manager = HNSWIndexManager(vector_dim=vector_dim)
    hnsw_manager.rebuild_from_vectors(
        version_dir, layout_override=ChunkLayout.CHUNKS_DB
    )

    # Story #1457 CRITICAL #4 (2026-07-23 code review): the published
    # version must be a COMPLETE constituent-file set, not just
    # chunks.db + hnsw_index.bin + discriminator.
    path_index = _populate_path_index(all_records)
    path_index.save(version_dir / "path_index.bin")

    # Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review, Codex):
    # temporal_metadata.db is a genuine AC6 solo-format requirement --
    # TemporalMetadataStore.detect_format() (used by
    # dashboard_service.py's get_temporal_index_status) checks ONLY for
    # this file's existence, and its absence causes a perfectly healthy
    # CHUNKS_DB collection to be misreported as legacy v1 needing
    # reindex. Write real point_id-to-hash-prefix mappings (not just
    # create the file) so detect_format() and dashboard/status reporting
    # both work correctly against sister-location versions.
    if all_records:
        _write_temporal_metadata(version_dir, all_records)

    from code_indexer.storage.projection_matrix_manager import (
        ProjectionMatrixManager,
    )

    matrix_manager = ProjectionMatrixManager()
    projection_matrix = matrix_manager.create_projection_matrix(
        input_dim=vector_dim, output_dim=64
    )
    matrix_manager.save_matrix(projection_matrix, version_dir)

    if embedder_slug:
        from code_indexer.services.temporal.temporal_structure_marker import (
            write_structure_marker,
        )

        write_structure_marker(version_dir, embedder_slug)

    # Story #1457 CRITICAL #4 (2026-07-23 code review): verify-then-
    # commit, never commit-then-verify. Read back and field-for-field
    # confirm every written record BEFORE any commit signal (progress
    # metadata, discriminator) is written.
    _verify_consolidated_version(version_dir, all_records, vector_dim)

    # AC6/AC7 integration: mark every consolidated commit complete so
    # reconciliation does not treat a freshly-built commit as partial
    # (points present, no completion marker) on its very next pass.
    commit_hashes = _extract_commit_hashes(all_records)
    if commit_hashes:
        TemporalProgressiveMetadata(version_dir).mark_completed(commit_hashes)

    # Discriminator commit is the MANDATORY, TRULY FINAL step -- only
    # after chunks.db, its HNSW index, verification, AND progress
    # metadata are all fully written and durable.
    write_chunks_db_discriminator(version_dir)

    return version_dir


def copy_and_extend_consolidated_temporal_version(
    snapshot_manager: "VersionedSnapshotManager",
    pointer_namespace: str,
    source_version_path: Path,
    delta_rows: Iterable[Dict[str, Any]],
    vector_dim: int,
    force_rebuild: bool = False,
) -> Path:
    """Build AC6 Branch A: pointer EXISTS -- reflink/copy the current v_*
    into a fresh collision-safe v_{unix_ts}, then apply ONLY this refresh's
    new-commit delta rows into the copy's consolidated chunk store.

    Reuses `VersionedSnapshotManager.create_snapshot` (the Story #1034 AC15
    anti-orphan-approved abstraction) for the copy step -- never a direct
    `cp --reflink` call. The historical rows already in source_version_path's
    chunks.db survive UNTOUCHED via the copy; only delta_rows are written.

    Args:
        snapshot_manager: VersionedSnapshotManager rooted at the sister
            location (its `versioned_base` must equal the sister_root
            passed to `build_fresh_consolidated_temporal_version`).
        pointer_namespace: The (embedder, quarter)-qualified namespace.
        source_version_path: The CURRENT published version directory
            (resolved via `TemporalShardResolver.resolve`) to copy from.
        delta_rows: This refresh's new-commit delta rows ONLY (never the
            full historical set -- those survive via the copy).
        vector_dim: Expected vector dimension.
        force_rebuild: Story #1457 HIGH #11 (2026-07-23 code review). When
            True, rebuild the copy's HNSW index even if delta_rows is
            empty -- propagates a local-repair signal (Bug #1407-style
            `was_stale`/`force_full_rebuild`) into the republished sister
            version. Without this, a locally-repaired-but-commit-delta-empty
            shard would republish a stale sister copy forever, since the
            copy otherwise just inherits the source's unmodified
            hnsw_index.bin unchanged.

    Returns:
        The path to the new, extended version directory. NOT YET
        published.
    """
    # Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review, Codex):
    # read the SOURCE's records BEFORE create_snapshot, not the copy's
    # records AFTER -- reading from the copy made verification circular
    # (comparing the copy to itself can never catch create_snapshot
    # itself producing an incomplete/corrupted copy). Reading from the
    # immutable source gives verification a genuinely independent
    # expected set.
    source_store = ChunkStore(
        source_version_path / "chunks.db", expected_dim=vector_dim, immutable=True
    )
    try:
        historical_records = list(source_store.stream_all())
    finally:
        source_store.close()

    new_version_path = Path(
        snapshot_manager.create_snapshot(pointer_namespace, str(source_version_path))
    )

    delta_batch = list(delta_rows)
    if delta_batch or force_rebuild:
        store = ChunkStore(new_version_path / "chunks.db", expected_dim=vector_dim)
        try:
            store.write_batch(delta_batch)
        finally:
            store.close()

        hnsw_manager = HNSWIndexManager(vector_dim=vector_dim)
        hnsw_manager.rebuild_from_vectors(
            new_version_path, layout_override=ChunkLayout.CHUNKS_DB
        )

        # Story #1457 CRITICAL #4: the copied path_index.bin (inherited
        # via create_snapshot) must be UPDATED with the new delta rows'
        # paths, not left stale.
        path_index_file = new_version_path / "path_index.bin"
        from code_indexer.storage.filesystem_vector_store import PathIndex

        path_index = PathIndex.load(path_index_file)
        path_index.merge_from(_populate_path_index(delta_batch))
        path_index.save(path_index_file)

        # Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review,
        # Codex): the copy inherits the source's temporal_metadata.db via
        # create_snapshot (whole-directory copy) -- only the NEW delta
        # rows' point_id mappings need appending here.
        _write_temporal_metadata(new_version_path, delta_batch)

        # AC6/AC7 integration: mark the delta commits complete -- the
        # copy already carries over the source version's existing
        # temporal_progress.json (historical commits stay marked), only
        # the NEW delta commits need marking here.
        commit_hashes = _extract_commit_hashes(delta_batch)
        if commit_hashes:
            TemporalProgressiveMetadata(new_version_path).mark_completed(commit_hashes)

    # Story #1457 CRITICAL #4 remaining gap: verify-then-return
    # UNCONDITIONALLY -- read back and field-for-field confirm the FULL
    # set (historical + delta) BEFORE this result is ever returned to a
    # caller for publish, even when delta_batch is empty and
    # force_rebuild is False (the pure reflink-copy no-op case). Without
    # this, temporal_refresh_dispatch.py could publish a copy whose
    # create_snapshot step silently produced incomplete/corrupted data.
    _verify_consolidated_version(
        new_version_path, historical_records + delta_batch, vector_dim
    )

    return new_version_path
