"""Story #1488 Codex Finding 1(a): the standalone foreground ``cidx index``
mutation path must acquire the SAME repo-scoped index-mutation lock the chunk
migration uses, so a foreground index and a migration are MUTUALLY EXCLUSIVE
(a migration can never delete a legacy point a concurrent foreground index
just wrote).

Real CliRunner against a real minimal project -- no mocking of the command
under test. The lock is held EXTERNALLY (via the real
``acquire_index_mutation_lock``) while ``cidx index`` is invoked; the command
must fail CLOSED with the lock-held message BEFORE reaching any
embedding-provider / network work. A second test proves the foreground path
RELEASES the lock (held only for the mutation lifecycle, never leaked).
"""

from __future__ import annotations

import os
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
    return codebase


def test_foreground_index_fails_closed_when_index_mutation_lock_held(
    tmp_path: Path, monkeypatch
) -> None:
    codebase = _make_real_project(tmp_path)

    # Ensure no embedding key is available so that, absent the lock, the
    # command would fail LATER with a DIFFERENT (service-unavailable) message
    # -- the assertion below distinguishes the lock path from that.
    for var in ("VOYAGE_API_KEY", "VOYAGE_AI_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config_dir = codebase / ".code-indexer"

    # Hold the shared index-mutation lock exactly as a concurrent migration
    # (or another foreground index) would.
    with acquire_index_mutation_lock(config_dir):
        result = CliRunner().invoke(cli, ["--path", str(codebase), "index"])

    assert result.exit_code == 1, result.output
    out = result.output.lower()
    assert "index-mutation.lock" in out or "already running" in out
    # The lock file the foreground path used is the shared one.
    assert (config_dir / ".index-mutation.lock").exists()


def test_foreground_index_releases_lock_after_run(tmp_path: Path, monkeypatch) -> None:
    """After a foreground index run completes (even failing on a missing
    embedding key), the lock must be RELEASED so a subsequent acquirer
    succeeds -- proving the lock is held only for the mutation lifecycle,
    not leaked."""
    codebase = _make_real_project(tmp_path)
    for var in ("VOYAGE_API_KEY", "VOYAGE_AI_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config_dir = codebase / ".code-indexer"

    # No lock held here: the command runs (and fails on the missing key), but
    # must release the lock it took on the foreground path.
    CliRunner().invoke(cli, ["--path", str(codebase), "index"])

    # The lock is free now -- a fresh acquire must succeed immediately.
    with acquire_index_mutation_lock(config_dir):
        assert (config_dir / ".index-mutation.lock").exists()

    assert os.path.exists(config_dir / ".index-mutation.lock")
