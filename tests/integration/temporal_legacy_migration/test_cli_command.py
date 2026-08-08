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


def _register_demo_repo(data_dir: Path) -> Path:
    repo = data_dir / "golden-repos" / "demo"
    shard = repo / ".code-indexer" / "index" / "code-indexer-temporal-voyage-2026Q1"
    shard.mkdir(parents=True)
    (shard / "vector_p1.json").write_text(
        '{"id":"p1","vector":[1.0],"payload":{"source":"cli"}}'
    )
    manager = GoldenRepoManager(str(data_dir))
    assert manager.register_local_repo("demo", repo, fire_lifecycle_hooks=False)

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
