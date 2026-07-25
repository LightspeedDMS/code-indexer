"""Fleet-wide golden-repo migration candidate discovery (Story #1458,
Technical Implementation Details -- Background Jobs Checklist).

`run_fleet_migration_for_repo()` (orchestrator.py) is real and tested but
takes explicit per-repo arguments (semantic_collection_dirs,
temporal_namespaces, sister_root, sister_alias_manager) -- this module
computes THOSE from real golden-repo enumeration + real on-disk directory
structure, reusing golden_repo_manager.list_golden_repos()/
get_actual_repo_path() (the SAME primitives hnsw_orphan_sweep/discovery.py
reuses) and the SAME pointer_namespace-construction formula Story #1457's
own production maybe_relocate_shard_to_sister_location trigger uses
(temporal_relocation_trigger.py).

`golden_repo_manager` is injected as a MagicMock() collaborator -- the
IDENTICAL, established precedent tests/unit/server/services/
hnsw_orphan_sweep/test_discovery_1360.py already uses for its own
golden_mgr/activated_mgr fixtures (an external collaborator, not the SUT
or the storage layer). Everything discovery.py itself does -- real temp-dir
filesystem walks, real AliasManager construction, real
write_chunks_db_discriminator/resolve_chunk_layout -- is genuinely real,
with no mocking of the code under test.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.fleet_migration.completion_gate import (
    mark_post_consolidation_snapshot_published,
)
from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
    is_repo_already_migrated,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    TemporalNamespaceSpec,
)
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_collection_meta(collection_dir: Path, **extra) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    meta = {"name": collection_dir.name, "vector_size": 16, "metadata": {}}
    meta.update(extra)
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _write_fully_migrated_collection(collection_dir: Path) -> None:
    """A genuinely COMPLETE consolidated collection, produced via the REAL
    consolidate_collection_in_place() flow (not hand-constructed) so it
    automatically carries whatever real migrated state requires: chunks.db,
    discriminator, zero legacy files, AND (Codex CRITICAL finding round 4)
    the crash-durable content-integrity manifest -- hand-building
    chunks.db+discriminator directly (as this fixture used to) produces
    exactly the kind of unmanifested state verify_collection_fully_migrated
    now correctly refuses to trust."""
    from code_indexer.storage.shared.collection_migration import (
        consolidate_collection_in_place,
    )

    _write_collection_meta(collection_dir)
    shard_dir = collection_dir / "mi" / "gr"
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "migrated1",
        "vector": [0.1, 0.2],
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "migrated content",
    }
    (shard_dir / "vector_migrated1.json").write_text(json.dumps(record))

    result = consolidate_collection_in_place(collection_dir)
    assert result.status == "consolidated"


def _golden_repo_manager(entries, path_by_alias):
    mock = MagicMock()
    mock.list_golden_repos.return_value = entries
    mock.get_actual_repo_path.side_effect = lambda alias: path_by_alias[alias]
    return mock


class TestEnumerateFleetMigrationCandidates:
    def test_skips_dangling_registration_with_no_resolvable_path(self, tmp_path):
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "ghost-repo"}], path_by_alias={}
        )
        golden_repo_manager.get_actual_repo_path.side_effect = Exception(
            "no clone on disk"
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert candidates == []

    def test_skips_registration_whose_resolved_path_does_not_exist(self, tmp_path):
        missing_path = tmp_path / "does-not-exist"
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(missing_path)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert candidates == []

    def test_skips_registration_that_resolves_to_an_immutable_versioned_snapshot(
        self, tmp_path
    ):
        """Codex Finding #1 (CRITICAL): GoldenRepoManager.get_actual_repo_path()
        falls back to the immutable `.versioned/{alias}/v_<ts>/` snapshot
        path when the mutable base-clone metadata path is absent (project
        CLAUDE.md "Golden Repo Versioned Path" invariant). Discovery must
        NEVER feed such a path to the destructive migration engine -- it
        must skip the candidate entirely, proven via the REAL canonical
        `is_immutable_versioned_snapshot()` predicate on the exact shape
        the real resolver produces."""
        from code_indexer.server.services.query_path_cache import (
            is_immutable_versioned_snapshot,
        )

        golden_repos_dir = tmp_path / "golden-repos"
        immutable_snapshot_path = (
            golden_repos_dir / ".versioned" / "repo-a" / "v_1700000000"
        )
        immutable_snapshot_path.mkdir(parents=True)
        (immutable_snapshot_path / ".code-indexer" / "index").mkdir(parents=True)
        # Test-setup invariant: the fixture path must genuinely be
        # recognized as immutable by the real production predicate, or this
        # test would not actually exercise the hazard it's proving.
        assert is_immutable_versioned_snapshot(str(immutable_snapshot_path)) is True

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}],
            path_by_alias={"repo-a": str(immutable_snapshot_path)},
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert candidates == [], (
            "Bug: discovery yielded a candidate whose base_clone_path is an "
            "IMMUTABLE .versioned/ snapshot -- feeding this to the "
            "destructive migration engine would violate the project's "
            "hardened 'NEVER modify/checkout/index inside .versioned/' "
            "invariant."
        )

    def test_skips_registration_whose_path_is_an_unresolved_symlink_into_the_versioned_tree(
        self, tmp_path
    ):
        """Codex's follow-up gap: get_actual_repo_path() can return an
        UNRESOLVED symlink whose real target lands inside .versioned/ --
        the string itself doesn't structurally match the canonical
        predicate's shape, so the check must resolve the path first."""
        from code_indexer.server.services.query_path_cache import (
            is_immutable_versioned_snapshot,
        )

        golden_repos_dir = tmp_path / "golden-repos"
        immutable_snapshot_path = (
            golden_repos_dir / ".versioned" / "repo-a" / "v_1700000000"
        )
        immutable_snapshot_path.mkdir(parents=True)
        (immutable_snapshot_path / ".code-indexer" / "index").mkdir(parents=True)

        # A symlink whose OWN (unresolved) path string does not match the
        # canonical .versioned/{ns}/v_<ts> shape, but whose REAL target
        # does. Test-setup invariant: the string form must NOT already
        # trip the predicate (else this test would not exercise the gap).
        symlink_path = golden_repos_dir / "repo-a"
        symlink_path.symlink_to(immutable_snapshot_path, target_is_directory=True)
        assert is_immutable_versioned_snapshot(str(symlink_path)) is False
        assert is_immutable_versioned_snapshot(str(symlink_path.resolve())) is True

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}],
            path_by_alias={"repo-a": str(symlink_path)},
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert candidates == [], (
            "Bug: discovery yielded a candidate whose base_clone_path is an "
            "UNRESOLVED SYMLINK into the IMMUTABLE .versioned/ tree -- the "
            "string-level check alone missed it."
        )

    def test_discovers_semantic_collection_dirs(self, tmp_path):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        _write_collection_meta(index_path / "coll_one")
        _write_collection_meta(index_path / "coll_two")
        # A stray directory with no collection_meta.json must NOT be treated
        # as a real semantic collection.
        (index_path / "not_a_collection").mkdir(parents=True)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.golden_alias == "repo-a"
        assert candidate.base_clone_path == repo_root
        assert candidate.index_path == index_path
        assert sorted(p.name for p in candidate.semantic_collection_dirs) == [
            "coll_one",
            "coll_two",
        ]
        assert candidate.sister_root == repo_root.parent
        assert candidate.temporal_namespaces == []

    def test_rejects_a_symlinked_semantic_collection_directory(self, tmp_path):
        """Codex CRITICAL finding (round 5): a semantic collection
        directory that is itself a SYMLINK (e.g. into the immutable
        .versioned/ tree) must never be accepted -- consolidate_collection_
        in_place() would write straight through the symlink into whatever
        it resolves to, bypassing the base_clone_path-level
        is_immutable_versioned_snapshot() check entirely (that check only
        inspects the repo's OWN base clone path, not each individual
        nested collection directory)."""
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        real_target = tmp_path / "elsewhere" / "real_collection"
        _write_collection_meta(real_target)

        symlinked_collection = index_path / "coll_symlinked"
        symlinked_collection.symlink_to(real_target, target_is_directory=True)
        # Test-setup invariant: the symlink genuinely resolves to a
        # directory that would otherwise pass the
        # (entry / _META_FILENAME).is_file() check, proving this test
        # exercises the real bypass rather than a vacuous no-op.
        assert (symlinked_collection / "collection_meta.json").is_file()

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert len(candidates) == 1
        assert candidates[0].semantic_collection_dirs == [], (
            "Bug: a SYMLINKED collection directory was accepted into "
            "semantic_collection_dirs -- consolidate_collection_in_place() "
            "would write straight through the symlink into whatever it "
            "resolves to."
        )

    def test_rejects_a_symlinked_temporal_namespace_directory(self, tmp_path):
        """Same bypass, temporal branch: a symlinked
        code-indexer-temporal-* directory must not be surfaced as a
        legacy_shard_dir either."""
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        real_target = tmp_path / "elsewhere" / "real_shard"
        real_target.mkdir(parents=True)

        symlinked_shard = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        symlinked_shard.symlink_to(real_target, target_is_directory=True)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert len(candidates) == 1
        assert candidates[0].temporal_namespaces == [], (
            "Bug: a SYMLINKED temporal namespace directory was accepted "
            "as a legacy_shard_dir."
        )

    def test_discovers_temporal_quarter_shard_namespace(self, tmp_path):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        quarter_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        quarter_dir.mkdir(parents=True)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert len(candidates) == 1
        namespaces = candidates[0].temporal_namespaces
        assert namespaces == [
            TemporalNamespaceSpec(
                pointer_namespace="repo-a-temporal-voyage_code_3-2024Q1",
                legacy_shard_dir=quarter_dir,
                embedder_slug="voyage_code_3",
            )
        ]
        # Never mistaken for a semantic collection.
        assert candidates[0].semantic_collection_dirs == []

    def test_discovers_temporal_monolith_namespace_without_quarter_suffix(
        self, tmp_path
    ):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        monolith_dir = index_path / "code-indexer-temporal-voyage_code_3"
        monolith_dir.mkdir(parents=True)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        namespaces = candidates[0].temporal_namespaces
        assert namespaces == [
            TemporalNamespaceSpec(
                pointer_namespace="repo-a-temporal-voyage_code_3",
                legacy_shard_dir=monolith_dir,
                embedder_slug="voyage_code_3",
            )
        ]

    def test_candidates_are_sorted_by_alias_for_a_stable_scan_order(self, tmp_path):
        paths = {}
        for alias in ("zebra-repo", "alpha-repo", "mid-repo"):
            root = tmp_path / "golden-repos" / alias
            (root / ".code-indexer" / "index").mkdir(parents=True)
            paths[alias] = str(root)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": a} for a in paths], path_by_alias=paths
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert [c.golden_alias for c in candidates] == [
            "alpha-repo",
            "mid-repo",
            "zebra-repo",
        ]

    def test_sister_alias_manager_is_scoped_to_golden_repos_dir_aliases(self, tmp_path):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        (repo_root / ".code-indexer" / "index").mkdir(parents=True)

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )

        candidates = list(enumerate_fleet_migration_candidates(golden_repo_manager))

        assert candidates[0].sister_alias_manager.aliases_dir == (
            repo_root.parent / "aliases"
        )


class TestIsRepoAlreadyMigrated:
    def test_repo_with_no_collections_at_all_needs_the_snapshot_marker_too(
        self, tmp_path
    ):
        """New CRITICAL finding: 'nothing to consolidate' is NOT the same
        as 'the AC10 snapshot has been published' -- a fresh candidate
        (even with zero collections) is not yet migrated until the
        orchestrator's marker proves the pass actually completed."""
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is False

        mark_post_consolidation_snapshot_published(index_path)

        assert is_repo_already_migrated(candidate) is True

    def test_repo_with_unconsolidated_semantic_collection_is_not_migrated(
        self, tmp_path
    ):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        _write_collection_meta(index_path / "coll_one")  # no chunks_db flag
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is False

    def test_repo_with_all_collections_consolidated_and_no_temporal_dirs_is_migrated(
        self, tmp_path
    ):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        coll_dir = index_path / "coll_one"
        _write_fully_migrated_collection(coll_dir)
        mark_post_consolidation_snapshot_published(index_path)
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is True

    def test_repo_with_consolidated_collections_but_residual_temporal_dir_is_not_migrated(
        self, tmp_path
    ):
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        coll_dir = index_path / "coll_one"
        _write_fully_migrated_collection(coll_dir)
        (index_path / "code-indexer-temporal-voyage_code_3-2024Q1").mkdir(parents=True)
        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is False

    def test_repo_with_discriminator_set_but_legacy_files_still_present_is_not_migrated(
        self, tmp_path
    ):
        """Codex CRITICAL Finding #2 (round 2): a crash between the
        durable discriminator flip and cleanup completing must NEVER be
        reported as 'already migrated' -- otherwise the scheduler
        permanently skips this repo and the real resume/cleanup verifier
        in consolidate_collection_in_place() is never invoked again."""
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        coll_dir = index_path / "coll_one"
        _write_collection_meta(coll_dir)
        record = {
            "id": "leftover1",
            "vector": [0.1, 0.2],
            "metadata": {},
            "payload": {"path": "src/a.py"},
            "chunk_text": "leftover",
        }
        with ChunkStore(coll_dir / "chunks.db") as store:
            store.write_batch([record])
        write_chunks_db_discriminator(coll_dir)
        # Legacy sharded file for the SAME record is still on disk --
        # cleanup (step 5) never ran before the crash.
        shard_dir = coll_dir / "le" / "ft"
        shard_dir.mkdir(parents=True)
        (shard_dir / "vector_leftover1.json").write_text(json.dumps(record))

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is False

    def test_repo_fully_consolidated_but_snapshot_marker_missing_is_not_migrated(
        self, tmp_path
    ):
        """New CRITICAL finding: consolidation being genuinely, verifiably
        complete is NOT sufficient -- a crash before/during the AC10
        snapshot trigger must also make this repo NOT YET migrated, so the
        scheduler retries it (and this time the snapshot fires)."""
        repo_root = tmp_path / "golden-repos" / "repo-a"
        index_path = repo_root / ".code-indexer" / "index"
        coll_dir = index_path / "coll_one"
        _write_fully_migrated_collection(coll_dir)
        # Deliberately NO mark_post_consolidation_snapshot_published() call.

        golden_repo_manager = _golden_repo_manager(
            [{"alias": "repo-a"}], path_by_alias={"repo-a": str(repo_root)}
        )
        candidate = next(
            iter(enumerate_fleet_migration_candidates(golden_repo_manager))
        )

        assert is_repo_already_migrated(candidate) is False
