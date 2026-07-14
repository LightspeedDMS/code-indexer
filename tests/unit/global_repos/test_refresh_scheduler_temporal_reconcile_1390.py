"""Bug #1390: temporal enable_temporal reconciliation cross-table + real-data-presence fix.

Two independent defects, both exercised here with REAL filesystem fixtures and
REAL SQLite backend instances (no mocking of the units under test):

1. One-sided cross-table update: `_reconcile_registry_with_filesystem` used to
   write ONLY to `self.registry` (the `global_repos` table, `-global`-suffixed
   alias). `golden_repos_metadata` (bare-alias-keyed table) was never touched,
   so the two tables could permanently disagree.

2. Name-pattern-only temporal detection: `_detect_existing_indexes` reported
   `temporal: True` for ANY directory matching the `code-indexer-temporal*`
   name pattern, even when it contained no real HNSW shard data (just a
   metadata file). This let a stale/emptied temporal directory falsely re-arm
   the scheduled-refresh trigger.

The most important test here is
`test_incident_reproduction_quarter_shards_removed_metadata_left_behind`,
which reproduces the exact real-world incident: quarter-shard data relocated
for maintenance, temporal metadata directory left behind, reconciliation
must NOT flip enable_temporal back to True on either table.

All reconciliation/incident tests use REAL SQLite-backed GlobalRegistry and
GoldenRepoMetadataSqliteBackend instances (the same classes production code
uses in solo/CLI mode) -- not stubs or mocks of the units under test.
"""

import json
from pathlib import Path
from typing import Any

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


# ---------------------------------------------------------------------------
# Filesystem fixture helpers (real files, mirroring the hnsw_orphan_sweep
# discovery test convention -- hnsw_index.bin + collection_meta.json pair is
# the structural definition of "a real HNSW collection").
# ---------------------------------------------------------------------------


def _make_real_collection(base: Path, name: str) -> Path:
    """Create a collection dir under base/.code-indexer/index/<name>/ with the
    two structural files that define a real HNSW collection."""
    coll = base / ".code-indexer" / "index" / name
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "hnsw_index.bin").write_bytes(b"fake-index-bytes")
    (coll / "collection_meta.json").write_text(json.dumps({"vector_dim": 1024}))
    return coll


def _make_metadata_only_temporal_dir(base: Path, name: str) -> Path:
    """Create a temporal-named directory with ONLY the metadata database file
    (no hnsw_index.bin, no collection_meta.json) -- the incident shape: quarter
    shards relocated elsewhere, leaving just the metadata behind."""
    coll = base / ".code-indexer" / "index" / name
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "temporal_meta.json").write_text(
        json.dumps({"max_commits": None, "total_commits": 500})
    )
    return coll


def _bare_scheduler() -> RefreshScheduler:
    """Lightweight RefreshScheduler for exercising pure filesystem-detection
    methods that don't touch registry/golden_repo_metadata state."""
    return RefreshScheduler.__new__(RefreshScheduler)


# ---------------------------------------------------------------------------
# Real-backend helpers shared by reconciliation + incident tests.
# ---------------------------------------------------------------------------


def _shared_db_path(tmp_path: Path) -> str:
    """Return a single initialized SQLite db path shared by both tables,
    mirroring production reality: golden_repos_metadata AND global_repos
    both live in the same cidx_server.db."""
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


def _make_real_registry(tmp_path: Path) -> GlobalRegistry:
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    db_path = _shared_db_path(tmp_path)
    return GlobalRegistry(
        golden_repos_dir=str(golden_repos_dir), use_sqlite=True, db_path=db_path
    )


def _make_real_golden_meta(tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
    db_path = _shared_db_path(tmp_path)
    return GoldenRepoMetadataSqliteBackend(db_path)


def _make_scheduler_with_real_backends(
    tmp_path: Path,
    registry: GlobalRegistry,
    golden_meta: GoldenRepoMetadataSqliteBackend,
) -> RefreshScheduler:
    golden_repos_dir = Path(registry.golden_repos_dir)
    config_mgr = ConfigManager(tmp_path / "config.json")
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=registry,
        golden_repo_metadata_backend=golden_meta,
    )


# ---------------------------------------------------------------------------
# Part 2: `_detect_existing_indexes` real-data-presence temporal detection.
# ---------------------------------------------------------------------------


class TestTemporalDetectionRequiresRealData:
    def test_temporal_named_dir_with_real_shard_data_detected_true(
        self, tmp_path: Path
    ) -> None:
        """A temporal-named collection with a genuine hnsw_index.bin +
        collection_meta.json pair must be detected as temporal:True."""
        _make_real_collection(tmp_path, "code-indexer-temporal-voyage_code_3-2024Q1")

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["temporal"] is True

    def test_temporal_named_dir_with_only_metadata_detected_false(
        self, tmp_path: Path
    ) -> None:
        """Bug #1390 core fix: a temporal-named directory with ONLY the
        metadata database (no real quarter-shard hnsw_index.bin/
        collection_meta.json) must be detected as temporal:False, not True."""
        _make_metadata_only_temporal_dir(
            tmp_path, "code-indexer-temporal-voyage_code_3"
        )

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["temporal"] is False

    def test_temporal_dir_with_hnsw_bin_but_no_collection_meta_detected_false(
        self, tmp_path: Path
    ) -> None:
        """hnsw_index.bin alone (no sibling collection_meta.json) is not a
        real collection per iter_index_files_for_repo's own contract."""
        coll = (
            tmp_path / ".code-indexer" / "index" / "code-indexer-temporal-voyage_code_3"
        )
        coll.mkdir(parents=True)
        (coll / "hnsw_index.bin").write_bytes(b"fake")
        # No collection_meta.json sibling.

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["temporal"] is False

    def test_no_temporal_dir_at_all_detected_false(self, tmp_path: Path) -> None:
        """Baseline: no temporal-named directory at all -> False (unaffected
        by the fix)."""
        _make_real_collection(tmp_path, "voyage-code-3")

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["temporal"] is False
        assert detected["semantic"] is True

    def test_semantic_detection_unaffected_by_temporal_fix(
        self, tmp_path: Path
    ) -> None:
        """Regression: semantic collection detection must be untouched by the
        temporal real-data-presence change."""
        _make_real_collection(tmp_path, "voyage-code-3")
        _make_metadata_only_temporal_dir(
            tmp_path, "code-indexer-temporal-voyage_code_3"
        )

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["semantic"] is True
        assert detected["temporal"] is False

    def test_legacy_monolith_temporal_name_with_real_data_detected_true(
        self, tmp_path: Path
    ) -> None:
        """Legacy (pre-provider-aware) 'code-indexer-temporal' name with real
        shard data must still be detected True."""
        _make_real_collection(tmp_path, "code-indexer-temporal")

        detected = _bare_scheduler()._detect_existing_indexes(tmp_path)

        assert detected["temporal"] is True


# ---------------------------------------------------------------------------
# Part 1: golden_repo_metadata lazy-resolution property on RefreshScheduler.
# ---------------------------------------------------------------------------


class TestGoldenRepoMetadataPropertyResolution:
    def _make_scheduler(self, tmp_path: Path, **kwargs: Any) -> RefreshScheduler:
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        config_mgr = ConfigManager(tmp_path / "config.json")
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=QueryTracker(),
            cleanup_manager=CleanupManager(QueryTracker()),
            **kwargs,
        )

    def test_injected_backend_returned_as_is(self, tmp_path: Path) -> None:
        sentinel_backend = object()
        scheduler = self._make_scheduler(
            tmp_path, golden_repo_metadata_backend=sentinel_backend
        )

        assert scheduler.golden_repo_metadata is sentinel_backend

    def test_resolves_real_sqlite_backend_in_solo_mode(self, tmp_path: Path) -> None:
        """No injected backend, no app.state cluster backend -> falls back to
        a per-node GoldenRepoMetadataSqliteBackend, matching the `registry`
        property's existing solo-mode fallback pattern."""
        scheduler = self._make_scheduler(tmp_path)

        backend = scheduler.golden_repo_metadata

        assert isinstance(backend, GoldenRepoMetadataSqliteBackend)
        # Table must be usable (ensure_table_exists was called).
        backend.add_repo(
            alias="probe-repo",
            repo_url="https://example.com/probe.git",
            default_branch="main",
            clone_path="/data/probe-repo",
            created_at="2024-01-01T00:00:00Z",
        )
        assert backend.get_repo("probe-repo") is not None

    def test_setter_allows_explicit_reinjection(self, tmp_path: Path) -> None:
        scheduler = self._make_scheduler(tmp_path)
        replacement = object()

        scheduler.golden_repo_metadata = replacement

        assert scheduler.golden_repo_metadata is replacement

    def test_cached_after_first_resolution_in_solo_mode(self, tmp_path: Path) -> None:
        scheduler = self._make_scheduler(tmp_path)

        first = scheduler.golden_repo_metadata
        second = scheduler.golden_repo_metadata

        assert first is second


# ---------------------------------------------------------------------------
# Part 3: cross-table reconciliation -- REAL SQLite-backed registry +
# golden_repos_metadata backend (no stubs/mocks of the units under test).
# ---------------------------------------------------------------------------


class TestReconciliationUpdatesBothTables:
    def test_reconcile_updates_both_tables_when_temporal_flips_true(
        self, tmp_path: Path
    ) -> None:
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="myrepo",
            alias_name="myrepo-global",
            repo_url="https://example.com/myrepo.git",
            index_path=str(tmp_path / "golden-repos" / "myrepo"),
            enable_temporal=False,
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="myrepo",
            repo_url="https://example.com/myrepo.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "myrepo"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=False,
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": True, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is True
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is True

    def test_reconcile_updates_both_tables_when_temporal_flips_false(
        self, tmp_path: Path
    ) -> None:
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="myrepo",
            alias_name="myrepo-global",
            repo_url="https://example.com/myrepo.git",
            index_path=str(tmp_path / "golden-repos" / "myrepo"),
            enable_temporal=True,
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="myrepo",
            repo_url="https://example.com/myrepo.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "myrepo"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=True,
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": False, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is False

    def test_reconcile_updates_golden_repos_metadata_even_when_global_repos_already_matches(
        self, tmp_path: Path
    ) -> None:
        """The defect's core symptom: golden_repos_metadata and global_repos
        can drift INDEPENDENTLY. Here global_repos already agrees with the
        filesystem (no update needed on that side) but golden_repos_metadata
        is stale -- reconciliation must still correct it."""
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="myrepo",
            alias_name="myrepo-global",
            repo_url="https://example.com/myrepo.git",
            index_path=str(tmp_path / "golden-repos" / "myrepo"),
            enable_temporal=True,  # already correct
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="myrepo",
            repo_url="https://example.com/myrepo.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "myrepo"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=False,  # drifted/stale
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": True, "scip": False}
        )

        assert golden_meta.get_repo("myrepo")["enable_temporal"] is True

    def test_reconcile_no_op_when_both_tables_already_match_filesystem(
        self, tmp_path: Path
    ) -> None:
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="myrepo",
            alias_name="myrepo-global",
            repo_url="https://example.com/myrepo.git",
            index_path=str(tmp_path / "golden-repos" / "myrepo"),
            enable_temporal=True,
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="myrepo",
            repo_url="https://example.com/myrepo.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "myrepo"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=True,
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": True, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is True
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is True

    def test_reconcile_alias_normalization_matches_bug_1373_pattern(
        self, tmp_path: Path
    ) -> None:
        """Bug #1373 normalization: bare_alias strips exactly one trailing
        '-global'; global_alias is always re-derived from bare_alias (never
        blindly re-suffixed) -- proven here by calling reconcile with an
        alias_name that is ALREADY '-global'-suffixed (the real call-site
        shape) and confirming golden_repos_metadata is updated with the BARE
        form while global_repos keeps the '-global' form."""
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="evolution",
            alias_name="evolution-global",
            repo_url="https://example.com/evolution.git",
            index_path=str(tmp_path / "golden-repos" / "evolution"),
            enable_temporal=False,
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="evolution",
            repo_url="https://example.com/evolution.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "evolution"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=False,
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "evolution-global", {"temporal": True, "scip": False}
        )

        assert golden_meta.get_repo("evolution")["enable_temporal"] is True
        assert golden_meta.get_repo("evolution-global") is None
        assert registry.get_global_repo("evolution-global")["enable_temporal"] is True


# ---------------------------------------------------------------------------
# Part 4: THE direct incident-reproduction regression test.
# ---------------------------------------------------------------------------


class TestIncidentReproduction1390:
    def test_incident_reproduction_quarter_shards_removed_metadata_left_behind(
        self, tmp_path: Path
    ) -> None:
        """Reproduces the exact real-world incident from issue #1390:

        1. Temporal indexing was genuinely enabled (both tables True).
        2. Quarter-shard data is relocated/sidelined for a maintenance
           operation, leaving ONLY the temporal metadata directory behind
           (no hnsw_index.bin anywhere under the temporal collection name).
        3. Reconciliation runs (as it does at both START and END of every
           scheduled refresh cycle).

        Must assert:
        - Detection reports temporal:False (real-data-presence, not name-only).
        - BOTH global_repos AND golden_repos_metadata enable_temporal flip to
          False (cross-table fix) -- NOT left split (the incident's exact
          "registry=True, golden_repos_metadata=False" split, just mirrored).
        - The scheduled-refresh trigger reads `global_repos.enable_temporal`
          (refresh_scheduler.py's `_index_source`, lines ~2263-2280) -- since
          that flag is now False, the trigger does not fire on next refresh.
        """
        repo_root = tmp_path / "golden-repos" / "some-repo"
        repo_root.mkdir(parents=True)

        # Step 2: only the metadata directory remains -- quarter shards gone.
        _make_metadata_only_temporal_dir(
            repo_root, "code-indexer-temporal-voyage_code_3"
        )

        # Step 1: both tables previously recorded temporal as truly enabled.
        registry = _make_real_registry(tmp_path)
        registry.register_global_repo(
            repo_name="some-repo",
            alias_name="some-repo-global",
            repo_url="https://example.com/some-repo.git",
            index_path=str(repo_root),
            enable_temporal=True,
        )
        golden_meta = _make_real_golden_meta(tmp_path)
        golden_meta.add_repo(
            alias="some-repo",
            repo_url="https://example.com/some-repo.git",
            default_branch="main",
            clone_path=str(repo_root),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=True,
        )

        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        # Step 3: reconciliation, exactly as _execute_refresh() invokes it.
        detected = scheduler._detect_existing_indexes(repo_root)
        assert detected["temporal"] is False, (
            "Real-data-presence detection must report False for a "
            "metadata-only temporal directory (no quarter-shard data)."
        )

        scheduler._reconcile_registry_with_filesystem("some-repo-global", detected)

        # Cross-table fix: BOTH tables must now be False, not split.
        repo_info_registry = registry.get_global_repo("some-repo-global")
        assert repo_info_registry["enable_temporal"] is False, (
            "global_repos.enable_temporal must be reconciled to False -- "
            "this is the exact flag _index_source() reads to decide whether "
            "to launch 'cidx index --index-commits'."
        )
        repo_info_golden_meta = golden_meta.get_repo("some-repo")
        assert repo_info_golden_meta["enable_temporal"] is False, (
            "golden_repos_metadata.enable_temporal must ALSO be reconciled "
            "to False -- the pre-fix bug left this table split from "
            "global_repos indefinitely."
        )

        # Explicit proof the scheduled-refresh trigger will not fire: the
        # trigger's own gating expression (refresh_scheduler.py _index_source)
        # is `enable_temporal = repo_info.get("enable_temporal", False)`.
        trigger_would_fire = repo_info_registry.get("enable_temporal", False)
        assert trigger_would_fire is False, (
            "Scheduled-refresh trigger must NOT be re-armed by a "
            "metadata-only temporal directory."
        )
