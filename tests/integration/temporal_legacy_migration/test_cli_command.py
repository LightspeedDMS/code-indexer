"""Front-door integration test for the server-admin migration command."""

from pathlib import Path

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)
from code_indexer.server.utils.config_manager import ServerConfigManager


def _make_real_refresh_scheduler(data_dir: Path):
    """Issue #1548 review finding 3: the CLI command now requires a real
    RefreshScheduler surface (write_lock_manager + check_refresh_not_in_
    progress + release_write_lock) -- built the SAME way
    test_orchestrator_1458.py's own ``_make_scheduler`` builds one for its
    real-systems-only integration tests, no mocking of the lock/scheduler
    layer under test.
    """
    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

    golden_repos_dir = data_dir / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    versioned_base = data_dir / "versioned"
    versioned_base.mkdir(parents=True, exist_ok=True)
    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        snapshot_manager=VersionedSnapshotManager(versioned_base=str(versioned_base)),
        job_tracker=None,
    )


def _register_demo_repo(data_dir: Path) -> Path:
    repo = data_dir / "golden-repos" / "demo"
    shard = repo / ".code-indexer" / "index" / "code-indexer-temporal-voyage-2026Q1"
    shard.mkdir(parents=True)
    (shard / "vector_p1.json").write_text(
        '{"id":"p1","vector":[1.0],"payload":{"source":"cli"}}'
    )
    manager = GoldenRepoManager(str(data_dir))
    assert manager.register_local_repo("demo", repo, fire_lifecycle_hooks=False)
    manager._refresh_scheduler = _make_real_refresh_scheduler(data_dir)

    from code_indexer.server import app as app_module

    app_module.app.state.golden_repo_manager = manager
    return shard


def _config_service_with_flags(
    tmp_path: Path, *, relocation_enabled: bool, cleanup_authorized: bool
) -> ConfigService:
    manager = ServerConfigManager(str(tmp_path / "config-store"))
    svc = ConfigService(config_manager=manager)
    svc.update_setting(
        "temporal_legacy_migration", "relocation_enabled", relocation_enabled
    )
    svc.update_setting(
        "temporal_legacy_migration", "cleanup_authorized", cleanup_authorized
    )
    return svc


def test_server_temporal_migrate_legacy_moves_real_shard_when_enabled(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "server-data"
    shard = _register_demo_repo(data_dir)
    set_config_service(
        _config_service_with_flags(
            tmp_path, relocation_enabled=True, cleanup_authorized=False
        )
    )
    try:
        result = CliRunner().invoke(
            cli, ["server", "temporal-migrate-legacy", "--alias", "demo"]
        )
        assert result.exit_code == 0, result.output
        assert "published=1" in result.output
        target = data_dir / "golden-repos" / ".temporal" / "demo" / shard.name
        assert (target / "vector_p1.json").read_text() == (
            shard / "vector_p1.json"
        ).read_text()
    finally:
        reset_config_service()


def test_server_temporal_migrate_legacy_does_nothing_when_relocation_disabled(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "server-data"
    shard = _register_demo_repo(data_dir)
    set_config_service(
        _config_service_with_flags(
            tmp_path, relocation_enabled=False, cleanup_authorized=False
        )
    )
    try:
        result = CliRunner().invoke(
            cli, ["server", "temporal-migrate-legacy", "--alias", "demo"]
        )
        assert result.exit_code == 0, result.output
        assert "disabled" in result.output.lower()
        target = data_dir / "golden-repos" / ".temporal" / "demo" / shard.name
        assert not target.exists()
        assert shard.exists()
    finally:
        reset_config_service()


def test_server_temporal_migrate_legacy_cleanup_requires_config_authorization(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "server-data"
    shard = _register_demo_repo(data_dir)
    set_config_service(
        _config_service_with_flags(
            tmp_path, relocation_enabled=True, cleanup_authorized=False
        )
    )
    try:
        result = CliRunner().invoke(
            cli,
            ["server", "temporal-migrate-legacy", "--alias", "demo", "--cleanup"],
        )
        assert result.exit_code == 0, result.output
        assert "not" in result.output.lower() or "false" in result.output.lower()
        assert "deleted=0" in result.output
        assert shard.exists(), "legacy shard must survive when cleanup is unauthorized"
    finally:
        reset_config_service()


def test_server_temporal_migrate_legacy_output_reports_collisions_and_failures(
    tmp_path: Path,
) -> None:
    """Issue #1548 review finding 7: the CLI must print collision and
    failure counts, not just successes.
    """
    data_dir = tmp_path / "server-data"
    shard = _register_demo_repo(data_dir)
    # Pre-populate a genuinely diverging fixed-root copy so this pass
    # produces a real, non-zero collision count.
    fixed_shard = data_dir / "golden-repos" / ".temporal" / "demo" / shard.name
    fixed_shard.mkdir(parents=True)
    (fixed_shard / "vector_p1.json").write_text(
        '{"id":"p1","vector":[1.0],"payload":{"source":"diverged"}}'
    )
    set_config_service(
        _config_service_with_flags(
            tmp_path, relocation_enabled=True, cleanup_authorized=False
        )
    )
    try:
        result = CliRunner().invoke(
            cli, ["server", "temporal-migrate-legacy", "--alias", "demo"]
        )
        assert result.exit_code == 0, result.output
        assert "collisions=1" in result.output
        assert "failed=0" in result.output
        assert shard.exists(), "legacy copy must survive a collision"
    finally:
        reset_config_service()


def test_server_temporal_migrate_legacy_skips_repo_when_write_lock_held(
    tmp_path: Path,
) -> None:
    """Issue #1548 review finding 3: the CLI must honor the repo's
    refresh-safe write lock -- a repo whose lock is already held by another
    writer is skipped this pass, never raced.
    """
    data_dir = tmp_path / "server-data"
    shard = _register_demo_repo(data_dir)

    from code_indexer.server import app as app_module

    manager = app_module.app.state.golden_repo_manager
    refresh_scheduler = manager._refresh_scheduler
    assert refresh_scheduler.write_lock_manager.acquire("demo", owner_name="other")

    set_config_service(
        _config_service_with_flags(
            tmp_path, relocation_enabled=True, cleanup_authorized=False
        )
    )
    try:
        result = CliRunner().invoke(
            cli, ["server", "temporal-migrate-legacy", "--alias", "demo"]
        )
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output.lower()
        target = data_dir / "golden-repos" / ".temporal" / "demo" / shard.name
        assert not target.exists(), "a locked repo must never be migrated"
        assert shard.exists()
    finally:
        refresh_scheduler.release_write_lock("demo", owner_name="other")
        reset_config_service()
