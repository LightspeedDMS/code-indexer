"""Front-door integration test for the server-admin migration command."""

from pathlib import Path

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def test_server_temporal_migrate_legacy_moves_real_shard(tmp_path: Path) -> None:
    from code_indexer.server import app as app_module

    data_dir = tmp_path / "server-data"
    repo = data_dir / "golden-repos" / "demo"
    shard = repo / ".code-indexer" / "index" / "code-indexer-temporal-voyage-2026Q1"
    shard.mkdir(parents=True)
    (shard / "vector_p1.json").write_text(
        '{"id":"p1","vector":[1.0],"payload":{"source":"cli"}}'
    )
    manager = GoldenRepoManager(str(data_dir))
    assert manager.register_local_repo("demo", repo, fire_lifecycle_hooks=False)
    app_module.app.state.golden_repo_manager = manager
    result = CliRunner().invoke(
        cli, ["server", "temporal-migrate-legacy", "--alias", "demo"]
    )
    assert result.exit_code == 0, result.output
    assert "published=1" in result.output
    target = data_dir / "golden-repos" / ".temporal" / "demo" / shard.name
    assert (target / "vector_p1.json").read_text() == (
        shard / "vector_p1.json"
    ).read_text()
