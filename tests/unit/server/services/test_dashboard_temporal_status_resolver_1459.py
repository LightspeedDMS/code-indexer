"""GitHub Issue #1459 AC4: DashboardService.get_temporal_index_status()'s
temporal-presence detection must route through the shared
TemporalShardResolver-based get_temporal_repo_status() helper as a fallback
when the local-clone scan finds nothing -- so a repo whose temporal data
has relocated to Story #1457's sister location is still reported as
active, never "none".

Unlike test_dashboard_temporal_status.py's pre-existing heavy Path-module
mocking (fragile, preserved as-is for regression coverage of the
local-clone-found case), these tests drive the method with a REAL
filesystem layout and a REAL AliasManager -- never a mock of
AliasManager/TemporalShardResolver/the filesystem itself (Messi Rule #1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.server.services.dashboard_service import DashboardService


class _FakeActivatedRepoManager:
    def __init__(self, data_dir: Path, activated_repos_dir: Path, repo_info: dict):
        self.data_dir = str(data_dir)
        self.activated_repos_dir = str(activated_repos_dir)
        self._repo_info = repo_info

    def get_repository(self, username: str, user_alias: str, *, touch: bool = True):
        return self._repo_info


def test_activated_repo_sister_relocated_temporal_data_is_detected(tmp_path):
    """THE ACTUAL BUG FIX (activated-repo branch): local clone scan finds
    nothing, but the backing golden repo's temporal data has relocated to
    the sister location -- must report format != "none"."""
    data_dir = tmp_path / "server-data"
    activated_repos_dir = data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    golden_repos_dir = data_dir / "golden-repos"
    # index_dir = Path(data_dir) / "index" -- pre-existing derivation,
    # preserved as-is; ensure it's empty (no local temporal copy).
    (data_dir / "index").mkdir(parents=True)

    # Bug #1529: temporal data lives at the FIXED server-owned root
    # ({golden_repos_dir}/.temporal/{alias}/), not a .versioned
    # snapshot behind an alias pointer. The behavior under test is
    # unchanged: status must detect data OUTSIDE the repo tree.
    sister_version_dir = (
        server_temporal_index_root(golden_repos_dir, "backing-golden")
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    # A real committed row: status keys off DATA presence, not merely
    # the presence of an index file.
    (sister_version_dir / "vector_aaaa1111.json").write_text("{}")

    manager = _FakeActivatedRepoManager(
        data_dir,
        activated_repos_dir,
        repo_info={
            "alias": "myactivated",
            "path": "/fake/repo/path",
            "golden_repo_alias": "backing-golden",
        },
    )

    service = DashboardService()
    with patch.object(service, "_get_activated_repo_manager", return_value=manager):
        result = service.get_temporal_index_status("alice", "myactivated")

    assert result["format"] != "none"


def test_local_dir_found_without_hnsw_index_falls_through_to_resolver(tmp_path):
    """Issue #1459 Finding 1b Site A: a locally-found temporal collection
    directory (matched by NAME only) with real committed data but NO
    hnsw_index.bin must NOT be reported as a positive v1/v2 status --
    it must fall through to the existing resolver-based fallback block
    (same as if nothing were found locally at all). With no
    golden_repo_alias configured here, the fallback correctly reports
    format == "none" -- never a false-positive "v1"/"v2"."""
    data_dir = tmp_path / "server-data"
    activated_repos_dir = data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    index_dir = data_dir / "index"
    temporal_dir = index_dir / "code-indexer-temporal-voyage_code_3"
    temporal_dir.mkdir(parents=True)
    # Real committed row present (v1-format vector file), but NO
    # hnsw_index.bin -- the exact crash-window state Finding 1/1b guard
    # against being reported as queryable/found.
    (temporal_dir / "vector_abcdef0123456789.json").write_text('{"id": "p1"}')

    manager = _FakeActivatedRepoManager(
        data_dir,
        activated_repos_dir,
        repo_info={
            "alias": "myactivated",
            "path": "/fake/repo/path",
            # No golden_repo_alias -- fallback must gracefully resolve to
            # "none" rather than crash, isolating this test to ONLY prove
            # the local-dir-found branch no longer short-circuits past the
            # hnsw_index.bin check.
        },
    )

    service = DashboardService()
    from code_indexer.server import app as app_module

    with patch.object(app_module, "activated_repo_manager", manager, create=True):
        result = service.get_temporal_index_status("alice", "myactivated")

    assert result["format"] == "none"


def test_activated_repo_no_golden_repo_alias_falls_back_to_none(tmp_path):
    """When no golden_repo_alias is tracked (e.g. composite repo), behavior
    gracefully falls back to the pre-existing "none" result -- no crash,
    no false positive."""
    data_dir = tmp_path / "server-data"
    activated_repos_dir = data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    (data_dir / "index").mkdir(parents=True)

    manager = _FakeActivatedRepoManager(
        data_dir,
        activated_repos_dir,
        repo_info={
            "alias": "myactivated",
            "path": "/fake/repo/path",
        },
    )

    service = DashboardService()
    with patch.object(service, "_get_activated_repo_manager", return_value=manager):
        result = service.get_temporal_index_status("alice", "myactivated")

    assert result["format"] == "none"


def test_global_repo_sister_relocated_temporal_data_is_detected(tmp_path):
    """THE ACTUAL BUG FIX (_global branch): local clone scan (against the
    global repo's index_path) finds nothing, but temporal data has
    relocated to the sister location -- must report format != "none"."""
    data_dir = tmp_path / "server-data"
    activated_repos_dir = data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    golden_repos_dir = data_dir / "golden-repos"

    global_repo_root = tmp_path / "global-clone"
    (global_repo_root / ".code-indexer" / "index").mkdir(parents=True)

    # Bug #1529: temporal data lives at the FIXED server-owned root
    # ({golden_repos_dir}/.temporal/{alias}/), not a .versioned
    # snapshot behind an alias pointer. The behavior under test is
    # unchanged: status must detect data OUTSIDE the repo tree.
    sister_version_dir = (
        server_temporal_index_root(golden_repos_dir, "myrepo")
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    # A real committed row: status keys off DATA presence, not merely
    # the presence of an index file.
    (sister_version_dir / "vector_aaaa1111.json").write_text("{}")

    manager = _FakeActivatedRepoManager(data_dir, activated_repos_dir, repo_info={})

    class _FakeGlobalRepos:
        def list_repos(self):
            return {
                "myrepo": {"index_path": str(global_repo_root)},
            }

    class _FakeBackendRegistry:
        global_repos = _FakeGlobalRepos()

    service = DashboardService()
    with patch.object(service, "_get_activated_repo_manager", return_value=manager):
        from code_indexer.server import app as app_module

        with patch.object(
            app_module.app.state,
            "backend_registry",
            _FakeBackendRegistry(),
            create=True,
        ):
            result = service.get_temporal_index_status("_global", "myrepo")

    assert result["format"] != "none"


def test_get_activated_repo_manager_logs_warning_on_real_import_failure(caplog):
    """Round 2 remediation, Finding C: `_get_activated_repo_manager`'s bare
    `except Exception: return None` silently swallowed the real exception,
    which is now load-bearing for the AC4 resolver-based fallback above --
    a genuine resolution failure previously produced a false-negative "no
    temporal index" status with zero diagnostic trace. This forces a REAL
    ImportError at the exact `from ..app import activated_repo_manager`
    statement (by deleting the real module attribute, not by mocking the
    method under test -- Messi Rule #1) and asserts a WARNING-level record
    naming activated_repo_manager AND carrying the real exception text was
    logged, while the graceful None-return behavior is preserved
    unchanged."""
    from code_indexer.server import app as app_module

    assert hasattr(app_module, "activated_repo_manager"), (
        "Precondition: app_module must have the attribute for this test "
        "to genuinely exercise its removal"
    )
    original = app_module.activated_repo_manager
    delattr(app_module, "activated_repo_manager")

    service = DashboardService()
    try:
        with caplog.at_level(logging.WARNING):
            result = service._get_activated_repo_manager()
    finally:
        app_module.activated_repo_manager = original

    assert result is None
    matching = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "activated_repo_manager" in record.message
    ]
    assert matching, (
        f"Expected a WARNING record naming activated_repo_manager, got: "
        f"{caplog.records}"
    )
    assert any(
        "cannot import name" in record.message.lower()
        or "import" in record.message.lower()
        for record in matching
    ), f"Expected the real exception text in the WARNING message, got: {matching}"
