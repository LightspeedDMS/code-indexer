"""Bug #1871 ALSO IN SCOPE item 1: the cidx-meta bootstrap ``.gitignore``
writer must also ignore the legacy in-tree lease directory
(``.snapshot-reader-leases/``), or a rolling-upgrade migration window --
old-version nodes still writing leases there while new-version nodes have
already relocated to ``golden-repos/.scratch/`` -- leaves those throwaway
files picked up by backup sync's ``git add -A`` (``cidx_meta_backup/
sync.py:77-83``).

Placed under ``tests/unit/global_repos/`` rather than the module's
conventional ``tests/unit/server/services/cidx_meta_backup/`` location:
this bug's owned test directories (negotiated turns 3-5) don't include the
latter, and pytest does not require a test file's location to mirror the
module under test -- only the import path matters.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.server.services.cidx_meta_backup.bootstrap import (
    CidxMetaBackupBootstrap,
)


def test_write_gitignore_adds_legacy_lease_directory_to_existing_gitignore(
    tmp_path: Path,
) -> None:
    """RED on unmodified code: models the exact staging state quoted in
    the issue (`cidx-meta/.gitignore` currently contains exactly
    `.code-indexer/`). `_write_gitignore()` today rewrites that same
    single-line content unconditionally, discarding any hand-added entry
    and never adding `.snapshot-reader-leases/` itself -- so a rolling
    upgrade's legacy lease churn keeps polluting `git add -A` regardless
    of what an operator hand-edits into the file.
    """
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(".code-indexer/\n")

    CidxMetaBackupBootstrap()._write_gitignore(str(tmp_path))

    content = gitignore_path.read_text()
    assert ".code-indexer/" in content
    assert ".snapshot-reader-leases/" in content, (
        "the bootstrap .gitignore writer must also ignore the legacy "
        "in-tree lease directory so backup sync's `git add -A` never "
        "picks up an old-version node's throwaway lease files during a "
        "rolling-upgrade migration window"
    )
