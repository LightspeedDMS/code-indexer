"""Bug #1406: enable_temporal filesystem reconciliation must be ONE-WAY.

`RefreshScheduler._reconcile_registry_with_filesystem()` used to treat
filesystem presence of real temporal data as the source of truth for
`enable_temporal` in BOTH directions -- auto-disabling when data is missing
(Bug #1390's fix, correct and preserved) AND auto-ENABLING when data is
present (wrong -- this overrides an operator's explicit disable).

This is the confirmed TRIGGER half of a production incident: an operator's
deliberate "disable temporal + restore data" recovery procedure got converted
into an unattended multi-hour full-rebuild launcher, twice.

Fix: reconciliation may only auto-DISABLE (registry/metadata True, filesystem
False -> flip to False). It must NEVER auto-ENABLE (registry/metadata False,
filesystem True -> stay False), for either the `global_repos` table or the
`golden_repos_metadata` table. `enable_scip` reconciliation is explicitly OUT
OF SCOPE and remains bidirectional, unchanged.

All tests here use REAL SQLite-backed GlobalRegistry and
GoldenRepoMetadataSqliteBackend instances (the same classes production code
uses in solo/CLI mode) -- no mocking of the units under test, matching the
pattern established in test_refresh_scheduler_temporal_reconcile_1390.py.
"""

import logging
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


# ---------------------------------------------------------------------------
# Real-backend helpers (mirroring test_refresh_scheduler_temporal_reconcile_1390.py).
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


class TestOneWayEnableTemporalCoreBehavior:
    """Core one-way reconciliation behavior: the incident-reproduction test,
    the preserved auto-disable path, and the allowed-direction mixed split."""

    def test_incident_reproduction_does_not_re_enable_restored_temporal_data(
        self, tmp_path: Path
    ) -> None:
        """Core regression test (the exact production incident): operator
        explicitly disabled temporal in BOTH tables, then restored real
        temporal data on disk. The next scheduled refresh's reconciliation
        must NOT flip either table back to True."""
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=False,
            golden_meta_temporal=False,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": True, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is False

    def test_disable_still_reconciles_when_filesystem_data_absent(
        self, tmp_path: Path
    ) -> None:
        """Preserve Bug #1390's fix: both tables True, filesystem reports no
        real data -> both must flip to False."""
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

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": False, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is False

    def test_mixed_split_allowed_direction_downgrades_registry_only(
        self, tmp_path: Path
    ) -> None:
        """Mixed split, allowed (auto-disable) direction: registry True,
        golden_meta already False, filesystem False -> registry flips to
        False (downgrade allowed); golden_meta stays False (already matches,
        no spurious write)."""
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=True,
            golden_meta_temporal=False,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": False, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is False


class TestOneWayEnableTemporalEdgeCases:
    """Blocked-direction residual split, enable_scip out-of-scope
    invariance, and the operator-facing INFO log."""

    def test_mixed_split_blocked_direction_leaves_residual_split(
        self, tmp_path: Path
    ) -> None:
        """Mixed split, BLOCKED (auto-enable) direction: registry False,
        golden_meta True, filesystem True.

        Expected final state: registry stays False (auto-enable forbidden
        for the global_repos side); golden_meta stays True (it already
        matches the filesystem truth of True, so no update fires on that
        side either). This intentionally leaves a residual split between
        the two tables -- that split is resolved ONLY by explicit operator
        action, or by a LATER tick where filesystem detection reports
        temporal:False (which would then downgrade golden_meta to False via
        the allowed auto-disable direction). It is never resolved by
        auto-enabling the registry side.
        """
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=False,
            golden_meta_temporal=True,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        scheduler._reconcile_registry_with_filesystem(
            "myrepo-global", {"temporal": True, "scip": False}
        )

        assert registry.get_global_repo("myrepo-global")["enable_temporal"] is False
        assert golden_meta.get_repo("myrepo")["enable_temporal"] is True

    def test_enable_scip_unaffected_both_directions(self, tmp_path: Path) -> None:
        """enable_scip reconciliation is explicitly out of scope for Bug
        #1406 and must remain bidirectional: both auto-enable and
        auto-disable directions still apply."""
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="repo-enable",
            registry_temporal=False,
            golden_meta_temporal=False,
            registry_scip=False,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        # scip False -> filesystem True: must flip to True (unchanged behavior).
        scheduler._reconcile_registry_with_filesystem(
            "repo-enable-global", {"temporal": False, "scip": True}
        )
        assert registry.get_global_repo("repo-enable-global")["enable_scip"] is True

        registry2 = _make_real_registry(tmp_path)
        golden_meta2 = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry2,
            golden_meta2,
            repo_name="repo-disable",
            registry_temporal=False,
            golden_meta_temporal=False,
            registry_scip=True,
        )
        scheduler2 = _make_scheduler_with_real_backends(
            tmp_path, registry2, golden_meta2
        )

        # scip True -> filesystem False: must flip to False (unchanged behavior).
        scheduler2._reconcile_registry_with_filesystem(
            "repo-disable-global", {"temporal": False, "scip": False}
        )
        assert registry2.get_global_repo("repo-disable-global")["enable_scip"] is False

    def test_info_log_emitted_when_honoring_operator_disable_with_data_present(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An INFO log must be emitted explaining why temporal is not being
        auto-enabled despite real data being present on disk, so operators
        can see why temporal is not running.

        Cardinality requirement (issue #1406 fix-item #3, new-test-plan case
        #6): EXACTLY ONE such INFO record per
        `_reconcile_registry_with_filesystem` invocation -- not one per
        table. This is proven with BOTH tables False (the exact
        incident-reproduction scenario), where a naive per-table log call
        would fire twice.
        """
        registry = _make_real_registry(tmp_path)
        golden_meta = _make_real_golden_meta(tmp_path)
        _register_repo(
            tmp_path,
            registry,
            golden_meta,
            repo_name="myrepo",
            registry_temporal=False,
            golden_meta_temporal=False,
        )
        scheduler = _make_scheduler_with_real_backends(tmp_path, registry, golden_meta)

        with caplog.at_level(logging.INFO):
            scheduler._reconcile_registry_with_filesystem(
                "myrepo-global", {"temporal": True, "scip": False}
            )

        matching_records = [
            record
            for record in caplog.records
            if "myrepo" in record.message
            and "enable_temporal is False" in record.message
            and "Bug #1406" in record.message
        ]
        assert len(matching_records) == 1, (
            "Expected EXACTLY ONE INFO log honoring the operator disable "
            "per reconciliation invocation (issue #1406 fix-item #3), "
            f"got {len(matching_records)}: "
            f"{[r.message for r in caplog.records]}"
        )
