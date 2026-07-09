"""Unit tests for Story #926 cidx-meta backup sync."""

import subprocess
from pathlib import Path
from types import SimpleNamespace


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _init_bare(tmp_path: Path, name: str = "origin.git") -> Path:
    bare = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True
    )
    return bare


def _clone_repo(remote: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-m", message], repo)


def _bootstrap_repo(tmp_path: Path) -> tuple[Path, Path]:
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    remote = _init_bare(tmp_path)
    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n")
    CidxMetaBackupBootstrap().bootstrap(str(repo), remote.as_uri())
    return repo, remote


def _resolver() -> SimpleNamespace:
    return SimpleNamespace(
        resolve=lambda cidx_meta_path, conflict_files, branch: SimpleNamespace(
            success=True, error=None
        )
    )


def test_sync_commits_and_pushes_local_changes(tmp_path):
    """# Story #926 AC2: local changes are committed and pushed to the configured remote."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    (repo / "local.txt").write_text("local change\n")

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is False
    assert result.sync_failure is None

    clone = tmp_path / "verify"
    _clone_repo(remote, clone)
    assert (clone / "local.txt").read_text() == "local change\n"


def test_sync_skips_when_clean_and_no_remote_drift(tmp_path):
    """# Story #926 AC2: sync reports skipped=True when there are no local or remote changes."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is True
    assert result.sync_failure is None


def test_sync_rebases_on_remote_drift(tmp_path):
    """# Story #926 AC3: sync fetches and rebases local work onto remote drift before push."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "remote.txt", "remote\n", "remote change")
    _git(["push", "origin", "master"], divergent)

    (repo / "local.txt").write_text("local\n")

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is False
    assert result.sync_failure is None
    clone = tmp_path / "verify-rebase"
    _clone_repo(remote, clone)
    assert (clone / "remote.txt").read_text() == "remote\n"
    assert (clone / "local.txt").read_text() == "local\n"


def test_sync_captures_fetch_failure_as_sync_failure(tmp_path):
    """# Story #926 AC6: fetch failure is returned as deferred sync_failure, not raised."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)
    _git(["remote", "set-url", "origin", "file:///definitely/missing/repo.git"], repo)

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is False
    assert result.sync_failure is not None
    assert result.sync_failure.startswith("fetch failed:")


def test_sync_captures_push_failure_as_sync_failure(tmp_path):
    """# Story #926 AC6: push failure is returned as deferred sync_failure after local indexing may proceed."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    (repo / "local.txt").write_text("local\n")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is False
    assert result.sync_failure is not None
    assert result.sync_failure.startswith("push failed:")


def test_sync_result_skipped_false_when_local_committed(tmp_path):
    """# Story #926 AC2: a local auto-commit forces skipped=False even without remote drift."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)
    (repo / "local-only.txt").write_text("change\n")

    result = CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    assert result.skipped is False


# ---------------------------------------------------------------------------
# Bug #1186: rebase failed-to-start vs genuine conflict disambiguation
# ---------------------------------------------------------------------------


def _make_rebase_abort_hook(repo: Path) -> None:
    """Install a pre-rebase hook that exits 1 WITHOUT creating rebase-state dirs.

    git invokes this hook before creating .git/rebase-merge/, so the working
    tree is completely clean after the hook fires — exactly the failed-to-start
    scenario described in Bug #1186.
    """
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "pre-rebase"
    hook.write_text(
        "#!/bin/sh\necho 'pre-rebase: simulated startup failure' >&2\nexit 1\n"
    )
    hook.chmod(0o755)


def test_rebase_failed_to_start_raises_original_error(tmp_path):
    """Bug #1186: rebase exits non-zero WITHOUT leaving rebase state on disk.

    The RuntimeError must contain the original hook stderr
    ("pre-rebase: simulated startup failure"), NOT a secondary "no rebase in
    progress" error that the buggy code produces by calling --continue when
    there is nothing to continue.
    """
    import pytest
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)

    # Create remote drift so sync reaches the rebase path.
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "remote.txt", "remote\n", "remote drift")
    _git(["push", "origin", "master"], divergent)

    # Commit a local change so sync doesn't short-circuit at the remote-changed check.
    _commit_file(repo, "local.txt", "local\n", "local change")

    # Install hook that aborts the rebase BEFORE any rebase state is created.
    _make_rebase_abort_hook(repo)

    with pytest.raises(RuntimeError) as exc_info:
        CidxMetaBackupSync(str(repo), "master", _resolver()).sync()

    error_msg = str(exc_info.value)

    # The hook-specific stderr must be surfaced, not a masked "no rebase in progress".
    assert "pre-rebase: simulated startup failure" in error_msg, (
        f"Expected original hook stderr in error, got: {error_msg!r}"
    )

    # No rebase state directories should exist after the call.
    assert not (repo / ".git" / "rebase-merge").exists()
    assert not (repo / ".git" / "rebase-apply").exists()


def test_genuine_conflict_still_resolves_via_continue(tmp_path):
    """Bug #1186: genuine mid-rebase conflict (rebase-merge dir IS created) still uses --continue.

    When a real merge conflict stops the rebase, .git/rebase-merge/ exists on disk.
    The fix must NOT suppress the resolver + --continue path for this case.
    Verified end-to-end: the resolved content reaches the remote.
    """
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)

    # Both sides modify the same line in the same file to force a merge conflict.
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "shared.txt", "remote version\n", "remote: shared")
    _git(["push", "origin", "master"], divergent)

    _commit_file(repo, "shared.txt", "local version\n", "local: shared")

    # Resolver that resolves the conflict by staging the file with the final content.
    def _resolving_resolver(cidx_meta_path, conflict_files, branch):
        shared = Path(cidx_meta_path) / "shared.txt"
        shared.write_text("resolved\n")
        _git(["add", "shared.txt"], Path(cidx_meta_path))
        return SimpleNamespace(success=True, error=None)

    resolver = SimpleNamespace(resolve=_resolving_resolver)

    result = CidxMetaBackupSync(str(repo), "master", resolver).sync()

    # Sync must complete without failure — this confirms --continue was called
    # and succeeded (otherwise sync would raise, not return).
    assert result.skipped is False
    assert result.sync_failure is None

    # Verify the resolved content was pushed to the remote.
    verify = tmp_path / "verify"
    _clone_repo(remote, verify)
    assert (verify / "shared.txt").read_text() == "resolved\n"
