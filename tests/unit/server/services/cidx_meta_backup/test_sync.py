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
