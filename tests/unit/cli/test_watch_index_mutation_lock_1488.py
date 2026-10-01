"""Story #1488 Codex Finding 1 (watch standalone): a standalone ``cidx watch``
session mutates chunks (initial ``smart_index`` sync + the git-aware / FTS /
temporal watch handlers), so it must acquire the SAME repo-scoped
``.index-mutation.lock`` the chunk migration uses and hold it for the watch
session -- mutually exclusive with a migration and a foreground index.

Real ``CliRunner`` against a real minimal project -- no mocking of the command
under test. The lock is held EXTERNALLY (via the real
``acquire_index_mutation_lock``) while ``cidx watch`` is invoked; the command
must fail CLOSED with the lock-held message BEFORE starting a watch session.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.config import ConfigManager
from code_indexer.services.chunk_migration_cli import acquire_index_mutation_lock


def _make_real_project(tmp_path: Path) -> Path:
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "hello.py").write_text("x = 1\n")
    cfg_path = codebase / ".code-indexer" / "config.json"
    cm = ConfigManager(cfg_path)
    config = cm.create_default_config(codebase)
    cm.save(config)
    # Minimal semantic index dir so auto-detection reports an index to watch
    # (so the failure is the lock, not "No indexes found").
    (codebase / ".code-indexer" / "index" / "code-indexer-HEAD").mkdir(parents=True)
    return codebase


def test_standalone_watch_fails_closed_when_index_mutation_lock_held(
    tmp_path: Path, monkeypatch
) -> None:
    codebase = _make_real_project(tmp_path)
    for var in ("VOYAGE_API_KEY", "VOYAGE_AI_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config_dir = codebase / ".code-indexer"

    with acquire_index_mutation_lock(config_dir):
        result = CliRunner().invoke(cli, ["--path", str(codebase), "watch"])

    assert result.exit_code == 1, result.output
    out = result.output.lower()
    assert "index-mutation.lock" in out or "already running" in out
    # Must NOT have started a watch session.
    assert "starting watch" not in out
    assert (config_dir / ".index-mutation.lock").exists()
