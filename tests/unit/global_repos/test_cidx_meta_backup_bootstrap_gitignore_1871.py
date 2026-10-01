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

Follow-up gap found during staging verification of v12.60.0: the fix above
only made ``_write_gitignore``'s CONTENT correct -- it is still called from
exactly ONE call site in ``bootstrap()``, inside the ``if not
git_dir.exists()`` (first-bootstrap) branch. Every already-deployed host
has ``cidx-meta/.git`` already, so the ``already_initialized`` return path
never converges the file at all -- verified live on clustered staging, the
running ``.gitignore`` still holds only ``.code-indexer/``. The tests below
cover: (1) the already-initialized path now self-heals a stale
``.gitignore`` (the discriminating regression test for this exact gap),
(2) operator-added lines are never clobbered by convergence, (3) repeated
convergence is a true no-op (no rewrite) once the file is already correct,
and (4) the first-bootstrap path still produces the full, correct content.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

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


def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "seed",
        "GIT_AUTHOR_EMAIL": "seed@test.invalid",
        "GIT_COMMITTER_NAME": "seed",
        "GIT_COMMITTER_EMAIL": "seed@test.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _init_real_repo_with_gitignore(
    repo_path: Path, remote_url: str, gitignore_content: str
) -> None:
    """Build a REAL already-initialized cidx-meta repo (matching the
    'already_initialized' branch's preconditions: `.git` exists, `origin`
    already equals `remote_url`) whose `.gitignore` is stale, mirroring an
    already-deployed host that predates the required entry.
    """
    repo_path.mkdir(parents=True)
    (repo_path / ".gitignore").write_text(gitignore_content)
    (repo_path / "README.md").write_text("seed\n")

    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "symbolic-ref", "HEAD", "refs/heads/master"],
        check=True,
        capture_output=True,
    )
    env = _git_env()
    subprocess.run(
        ["git", "-C", str(repo_path), "add", "-A"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "remote", "add", "origin", remote_url],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "push", "origin", "master"],
        check=True,
        capture_output=True,
    )


def test_bootstrap_already_initialized_converges_missing_gitignore_entry(
    tmp_path: Path,
) -> None:
    """THE discriminating regression test for the reported gap: on a host
    where `cidx-meta/.git` already exists (so `bootstrap()` takes the
    `already_initialized` return path, never the first-bootstrap branch
    that is the only call site of `_write_gitignore` today), a
    `.gitignore` missing `.snapshot-reader-leases/` must still be repaired
    by calling `bootstrap()` again -- with the operator's own line intact.

    RED on unmodified code: `_write_gitignore` is called nowhere in the
    `already_initialized` path, so this assertion fails against current
    bootstrap.py.
    """
    remote_path = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote_path)], check=True, capture_output=True
    )
    remote_url = remote_path.as_uri()

    repo_path = tmp_path / "cidx-meta"
    _init_real_repo_with_gitignore(repo_path, remote_url, ".code-indexer/\n*.bak\n")

    result = CidxMetaBackupBootstrap().bootstrap(str(repo_path), remote_url)

    assert result == "already_initialized"
    lines = (repo_path / ".gitignore").read_text().splitlines()
    assert ".code-indexer/" in lines
    assert "*.bak" in lines, "operator-added line must survive convergence"
    assert ".snapshot-reader-leases/" in lines, (
        "the already_initialized path must also converge .gitignore -- "
        "an already-deployed host can never reach the first-bootstrap "
        "call site again"
    )


def test_write_gitignore_preserves_operator_added_lines(tmp_path: Path) -> None:
    """Convergence must be additive, never a wholesale overwrite: an
    operator-added exclusion pre-existing in the file must survive, in its
    original position, alongside both required entries appended after it.
    """
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(".code-indexer/\n*.bak\nnotes.txt\n")

    CidxMetaBackupBootstrap()._write_gitignore(str(tmp_path))

    assert (
        gitignore_path.read_text()
        == ".code-indexer/\n*.bak\nnotes.txt\n.snapshot-reader-leases/\n"
    )


def test_write_gitignore_idempotent_second_call_does_not_rewrite_file(
    tmp_path: Path,
) -> None:
    """Once converged, a repeat call must be a true no-op -- not merely
    byte-identical output, but no write syscall at all -- so running
    bootstrap repeatedly on a healthy host never touches the file's mtime
    or triggers spurious backup-sync commits.
    """
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(".code-indexer/\n")

    bootstrap = CidxMetaBackupBootstrap()
    bootstrap._write_gitignore(str(tmp_path))  # first call converges
    assert ".snapshot-reader-leases/" in gitignore_path.read_text()

    with patch.object(Path, "write_text") as mock_write_text:
        bootstrap._write_gitignore(str(tmp_path))
        mock_write_text.assert_not_called()


def test_bootstrap_fresh_init_produces_both_required_gitignore_entries(
    tmp_path: Path,
) -> None:
    """The first-bootstrap path (no pre-existing `.gitignore`) must still
    produce the full, correct content end-to-end through `bootstrap()`.
    """
    remote_path = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote_path)], check=True, capture_output=True
    )
    remote_url = remote_path.as_uri()

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("seed\n")

    result = CidxMetaBackupBootstrap().bootstrap(str(repo_path), remote_url)

    assert result == "bootstrapped"
    lines = (repo_path / ".gitignore").read_text().splitlines()
    assert ".code-indexer/" in lines
    assert ".snapshot-reader-leases/" in lines
