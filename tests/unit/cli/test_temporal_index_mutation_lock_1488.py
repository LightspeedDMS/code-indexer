"""Story #1488 Codex Finding 1 (CRITICAL, reproduced data loss): the standalone
foreground TEMPORAL index path (``cidx index --index-commits``) must acquire the
SAME repo-scoped ``.index-mutation.lock`` the chunk migration uses, so a
foreground temporal index and a migration are MUTUALLY EXCLUSIVE.

Before this fix the temporal branch exited in ``cli.py`` BEFORE the semantic
path's lock acquisition, so a foreground temporal index ran UNLOCKED. Because
temporal shard filenames are deterministic by point-id, a concurrent temporal
writer could OVERWRITE an already-verified pathname mid-migration, and the
migration's verified-path-set cleanup would then delete a file whose content had
been replaced -- silent data loss.

Real ``CliRunner`` against a real minimal project -- no mocking of the command
under test. The lock is held EXTERNALLY (via the real
``acquire_index_mutation_lock``) while ``cidx index --index-commits`` is invoked;
the command must fail CLOSED with the lock-held message BEFORE reaching any
temporal-indexing work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.config import ConfigManager
from code_indexer.services.chunk_migration_cli import (
    MigrationLockError,
    acquire_index_mutation_lock,
)


def _make_real_project(tmp_path: Path) -> Path:
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "hello.py").write_text("x = 1\n")
    cfg_path = codebase / ".code-indexer" / "config.json"
    cm = ConfigManager(cfg_path)
    config = cm.create_default_config(codebase)
    cm.save(config)
    return codebase


def test_temporal_foreground_index_fails_closed_when_lock_held(
    tmp_path: Path, monkeypatch
) -> None:
    codebase = _make_real_project(tmp_path)

    # Absent embedding keys: absent the lock, the command would fail LATER with
    # a DIFFERENT message -- the assertions below distinguish the lock path.
    for var in ("VOYAGE_API_KEY", "VOYAGE_AI_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config_dir = codebase / ".code-indexer"

    with acquire_index_mutation_lock(config_dir):
        result = CliRunner().invoke(
            cli, ["--path", str(codebase), "index", "--index-commits"]
        )

    assert result.exit_code == 1, result.output
    out = result.output.lower()
    assert "index-mutation.lock" in out or "already running" in out
    # Must NOT have proceeded to real temporal work.
    assert "temporal indexing completed" not in out
    # The shared lock file the foreground temporal path used.
    assert (config_dir / ".index-mutation.lock").exists()


def test_temporal_foreground_index_releases_lock_after_run(
    tmp_path: Path, monkeypatch
) -> None:
    """After a foreground temporal index run completes (even failing for an
    unrelated reason), the lock must be RELEASED so a subsequent acquirer
    succeeds -- proving the lock is held only for the mutation lifecycle and a
    lone temporal index still works (single-writer regression)."""
    codebase = _make_real_project(tmp_path)
    for var in ("VOYAGE_API_KEY", "VOYAGE_AI_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config_dir = codebase / ".code-indexer"

    # No lock held: the command runs (and fails on the missing key / no git),
    # but must release the lock it took on the foreground temporal path.
    CliRunner().invoke(cli, ["--path", str(codebase), "index", "--index-commits"])

    # The lock is free now -- a fresh acquire must succeed immediately.
    with acquire_index_mutation_lock(config_dir):
        assert (config_dir / ".index-mutation.lock").exists()


def test_migration_style_acquire_refused_while_temporal_lock_held(
    tmp_path: Path,
) -> None:
    """The reverse direction: while a temporal foreground index would hold the
    shared lock, a migration-style ``acquire_index_mutation_lock`` is refused
    (fail closed, never blocks) -- the SAME lock guarantees mutual exclusion in
    both orders."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()

    with acquire_index_mutation_lock(config_dir):
        with pytest.raises(MigrationLockError):
            with acquire_index_mutation_lock(config_dir):
                pass
