"""Unit tests for cidx-meta backup sync (Story #926; mirror semantics per Bug #1555)."""

import subprocess
from pathlib import Path

# Number of consecutive divergence/sync cycles exercised by
# test_sync_self_heals_across_repeated_divergence_1555 -- named so the
# repetition count is self-documenting rather than a bare literal.
_REPEATED_DIVERGENCE_CYCLES = 3


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


def test_sync_commits_and_pushes_local_changes(tmp_path):
    """# Story #926 AC2: local changes are committed and pushed to the configured remote."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    (repo / "local.txt").write_text("local change\n")

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.skipped is False
    assert result.sync_failure is None

    clone = tmp_path / "verify"
    _clone_repo(remote, clone)
    assert (clone / "local.txt").read_text() == "local change\n"


def test_sync_skips_when_clean_and_no_remote_drift(tmp_path):
    """# Story #926 AC2: sync reports skipped=True when there are no local or remote changes."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.skipped is True
    assert result.sync_failure is None


def test_sync_captures_fetch_failure_as_sync_failure(tmp_path):
    """# Story #926 AC6: fetch failure is returned as deferred sync_failure, not raised."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)
    _git(["remote", "set-url", "origin", "file:///definitely/missing/repo.git"], repo)

    result = CidxMetaBackupSync(str(repo), "master").sync()

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

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.skipped is False
    assert result.sync_failure is not None
    assert result.sync_failure.startswith("push failed:")


def test_sync_result_skipped_false_when_local_committed(tmp_path):
    """# Story #926 AC2: a local auto-commit forces skipped=False even without remote drift."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, _remote = _bootstrap_repo(tmp_path)
    (repo / "local-only.txt").write_text("change\n")

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.skipped is False


# ---------------------------------------------------------------------------
# Bug #1555: the remote is a passive backup MIRROR, never a peer -- sync()
# publishes local HEAD directly (force-with-lease) instead of rebasing onto
# the remote, so a diverged remote (whether the divergence is a harmless
# non-conflicting commit or a genuine content conflict) is always resolved
# by local winning, deterministically, with no conflict-resolution step and
# no possibility of getting stuck.
# ---------------------------------------------------------------------------


def test_sync_overwrites_non_conflicting_remote_drift(tmp_path):
    """A remote-only commit on an unrelated file is NOT preserved -- mirror
    semantics discard whatever the remote holds that local does not have,
    exactly as instructed ("I don't care what you do to the remote")."""
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "remote.txt", "remote\n", "remote change")
    _git(["push", "origin", "master"], divergent)

    (repo / "local.txt").write_text("local\n")

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.skipped is False
    assert result.sync_failure is None
    clone = tmp_path / "verify-mirror-drift"
    _clone_repo(remote, clone)
    assert (clone / "local.txt").read_text() == "local\n"
    # The remote-only commit is gone: mirror, not merge.
    assert not (clone / "remote.txt").exists()


def _make_shared_txt_conflict(tmp_path: Path):
    repo, remote = _bootstrap_repo(tmp_path)
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "shared.txt", "remote version\n", "remote: shared")
    _git(["push", "origin", "master"], divergent)
    _commit_file(repo, "shared.txt", "local version\n", "local: shared")
    return repo, remote, divergent


def test_sync_mirrors_local_over_diverged_remote_content_conflict_1555(tmp_path):
    """Bug #1555 (root cause): the remote is a passive BACKUP MIRROR of
    cidx-meta-global, never a peer whose independent history must be
    preserved (explicit product decision -- local is always
    authoritative). A genuine, structurally unresolvable CONTENT
    conflict between a local and a remote commit touching the SAME
    machine-generated file (exactly what happened in production: 59+
    hours QUARANTINED against one unchanged upstream SHA) must resolve
    itself deterministically on every sync() call -- local wins, the
    remote is overwritten -- with no conflict-resolution step ever
    running: the fix removes the rebase entirely, so there is no
    conflict left to resolve.
    """
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote, _divergent = _make_shared_txt_conflict(tmp_path)

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.sync_failure is None
    assert result.skipped is False

    verify = tmp_path / "verify-mirror-1555"
    _clone_repo(remote, verify)
    assert (verify / "shared.txt").read_text() == "local version\n"


def test_sync_self_heals_across_repeated_divergence_1555(tmp_path):
    """Bug #1555: a diverged remote never accumulates a stuck state. Three
    consecutive sync() cycles, each preceded by a fresh conflicting remote
    commit, must all succeed with no exception and no growing failure
    state -- there is no quarantine bookkeeping left for a failure to
    accumulate into.
    """
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    repo, remote = _bootstrap_repo(tmp_path)
    sync = CidxMetaBackupSync(str(repo), "master")

    for i in range(_REPEATED_DIVERGENCE_CYCLES):
        divergent = tmp_path / f"divergent-{i}"
        _clone_repo(remote, divergent)
        _commit_file(divergent, "shared.txt", f"remote version {i}\n", "remote drift")
        _git(["push", "origin", "master"], divergent)

        _commit_file(repo, "shared.txt", f"local version {i}\n", "local change")

        result = sync.sync()

        assert result.sync_failure is None, f"cycle {i} failed: {result.sync_failure}"
        assert result.skipped is False

    verify = tmp_path / "verify-self-heal"
    _clone_repo(remote, verify)
    last_index = _REPEATED_DIVERGENCE_CYCLES - 1
    assert (verify / "shared.txt").read_text() == f"local version {last_index}\n"
