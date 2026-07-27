"""Bug #1461 (Epic #1454 Story #1461 salvage item #1): sister-location
temporal data must be visible to reconciliation's real-data-presence check.

`RefreshScheduler._detect_existing_indexes(repo_path)` used to compute
`temporal_exists` by scanning ONLY the in-repo `.code-indexer/index/` tree
via `iter_index_files_for_repo(repo_path)`. It never consulted the
golden-owned "sister location" that Story #1457 can relocate temporal
shards to (`{golden_repos_dir}/.versioned/{bare_alias}-temporal-{slug}[-
{quarter}]/v_*/`, published via an alias pointer file under
`{golden_repos_dir}/aliases/`).

This feeds `_reconcile_registry_with_filesystem` -> the Bug #1406 one-way
rule: stored `enable_temporal=True` + filesystem detection says "absent" ->
flip to `False` (never auto-re-enable the reverse). Once Story #1457
relocates a repo's temporal shards to the sister location and empties the
in-repo temporal tree, the OLD in-repo-only scan wrongly reported
`temporal=False` even though the data is alive and queryable at the sister
location -- silently, permanently disabling temporal refresh fleet-wide.
Same failure class as Bug #1390/#1406, re-triggered by #1457's own
relocation feature.

Fix: `_detect_existing_indexes` gained an optional `repo_alias` parameter
(default `None`, preserving byte-identical behavior for every existing
positional-only caller). When provided and the in-repo scan alone reports
absent, a new `_sister_temporal_data_exists()` helper additionally consults
`get_temporal_repo_status()` (Story #1457/#1459's existing resolver-based
union primitive -- sister pointer OR in-repo, real-data-presence only) and
ORs its `.has_data` result in. This preserves Bug #1390's real-data-presence
rule and Bug #1406's one-way auto-disable-only semantics; it only widens
WHAT counts as "temporal data present", never the reconciliation direction
rules downstream.

All tests here use REAL SQLite-backed GlobalRegistry and
GoldenRepoMetadataSqliteBackend instances (mirroring
test_refresh_scheduler_enable_temporal_one_way_1406.py's fixture helpers,
copied verbatim) plus a REAL AliasManager + real marker files for the
sister-location fixture (mirroring
tests/unit/services/temporal/test_temporal_status_1459.py's
`test_sister_relocated_data_is_detected_as_present_and_queryable` recipe) --
no mocking of the units under test, except in the dedicated fail-open test
case C, which deliberately injects a raising stand-in for
`get_temporal_repo_status` to prove the contract.
"""

import logging
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


# ---------------------------------------------------------------------------
# Real-backend helpers (copied verbatim from
# test_refresh_scheduler_enable_temporal_one_way_1406.py).
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


def _register_repo(
    tmp_path: Path,
    registry: GlobalRegistry,
    golden_meta: GoldenRepoMetadataSqliteBackend,
    *,
    repo_name: str,
    registry_temporal: bool,
    golden_meta_temporal: bool,
    registry_scip: bool = False,
) -> None:
    """Register the same logical repo in both tables with independently
    specified enable_temporal values, mirroring how the two tables can drift."""
    clone_path = str(tmp_path / "golden-repos" / repo_name)
    registry.register_global_repo(
        repo_name=repo_name,
        alias_name=f"{repo_name}-global",
        repo_url=f"https://example.com/{repo_name}.git",
        index_path=clone_path,
        enable_temporal=registry_temporal,
        enable_scip=registry_scip,
    )
    golden_meta.add_repo(
        alias=repo_name,
        repo_url=f"https://example.com/{repo_name}.git",
        default_branch="main",
        clone_path=clone_path,
        created_at="2024-01-01T00:00:00Z",
        enable_temporal=golden_meta_temporal,
    )


def _make_sister_temporal_fixture(
    golden_repos_dir: Path,
    repo_name: str,
    *,
    embedder_slug: str = "voyage_code_3",
    quarter: str = "2024Q1",
) -> None:
    """Build a real sister-location temporal namespace: a version directory
    with a real hnsw_index.bin marker, published via a real AliasManager
    pointer file -- mirroring
    test_temporal_status_1459.py::test_sister_relocated_data_is_detected_as_present_and_queryable.
    """
    namespace = f"{repo_name}-temporal-{embedder_slug}-{quarter}"
    sister_version_dir = golden_repos_dir / ".versioned" / namespace / "v_1700000000"
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    aliases_dir = golden_repos_dir / "aliases"
    AliasManager(str(aliases_dir)).create_alias(namespace, str(sister_version_dir))


class TestSisterTemporalDetectionFeedsReconciliation:
    """Test case A: the actual bug -- sister-relocated data must be detected
    and must prevent the Bug #1406 one-way auto-disable from firing."""

    def test_sister_relocated_temporal_data_prevents_false_downgrade(
        self, tmp_path: Path
    ) -> None:
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=True,
            golden_meta_temporal=True,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        golden_repos_dir = Path(registry.golden_repos_dir)
        # In-repo temporal tree is EMPTY (relocated away) -- only the
        # sister-location namespace has real data.
        repo_clone_path = golden_repos_dir / "myrepo"
        (repo_clone_path / ".code-indexer" / "index").mkdir(parents=True)
        _make_sister_temporal_fixture(golden_repos_dir, "myrepo")

        detected = scheduler._detect_existing_indexes(
            repo_clone_path, repo_alias="myrepo"
        )
        assert detected["temporal"] is True, (
            "Sister-location temporal data must be detected as present via "
            "get_temporal_repo_status(), not misreported as absent because "
            "the in-repo scan alone found nothing"
        )

        scheduler._reconcile_registry_with_filesystem("myrepo-global", detected)

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is True
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is True


class TestGenuineAbsenceStillDowngrades:
    """Test case B: regression guard -- when temporal data is genuinely gone
    from BOTH the in-repo tree AND the sister location, Bug #1406's
    auto-disable must still fire exactly as before."""

    def test_genuinely_absent_temporal_data_still_downgrades_both_tables(
        self, tmp_path: Path
    ) -> None:
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=True,
            golden_meta_temporal=True,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        golden_repos_dir = Path(registry.golden_repos_dir)
        repo_clone_path = golden_repos_dir / "myrepo"
        (repo_clone_path / ".code-indexer" / "index").mkdir(parents=True)
        # No sister fixture created at all -- genuinely no data anywhere.

        detected = scheduler._detect_existing_indexes(
            repo_clone_path, repo_alias="myrepo"
        )
        assert detected["temporal"] is False

        scheduler._reconcile_registry_with_filesystem("myrepo-global", detected)

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is False


class TestSisterLookupFailsOpen:
    """Test case C: the sister-location lookup must fail open -- any error
    must never crash reconciliation, must log a WARNING, and must fall back
    to the in-repo-only result."""

    def test_sister_lookup_error_falls_back_to_in_repo_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=True,
            golden_meta_temporal=True,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        golden_repos_dir = Path(registry.golden_repos_dir)
        repo_clone_path = golden_repos_dir / "myrepo"
        (repo_clone_path / ".code-indexer" / "index").mkdir(parents=True)

        def _raising_get_temporal_repo_status(*args, **kwargs):
            raise RuntimeError("simulated sister-location lookup failure")

        monkeypatch.setattr(
            "code_indexer.services.temporal.temporal_status.get_temporal_repo_status",
            _raising_get_temporal_repo_status,
        )

        with caplog.at_level(logging.WARNING):
            detected = scheduler._detect_existing_indexes(
                repo_clone_path, repo_alias="myrepo"
            )

        assert detected["temporal"] is False, (
            "A sister-location lookup failure must fall back to the "
            "in-repo-only result (False here, since in-repo is empty), "
            "never raise and never crash reconciliation"
        )
        assert any(
            "sister-location" in record.message.lower()
            or "sister_temporal" in record.message.lower()
            or "myrepo" in record.message
            for record in caplog.records
        ), (
            "Expected a WARNING log documenting the fail-open sister-location "
            f"lookup failure, got: {[r.message for r in caplog.records]}"
        )
