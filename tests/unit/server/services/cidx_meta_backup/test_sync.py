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


# ---------------------------------------------------------------------------
# Bug #1500: "Terminal is dumb, but EDITOR unset" during rebase --continue
# ---------------------------------------------------------------------------


def test_rebase_continue_fails_without_editor_env_bug1500(tmp_path, monkeypatch):
    """Bug #1500: a real mid-rebase conflict, resolved, then `rebase
    --continue` must succeed non-interactively even when the PROCESS
    environment (not just the subprocess env dict) has no EDITOR/GIT_EDITOR/
    VISUAL configured -- the exact systemd job context that produced
    "Terminal is dumb, but EDITOR unset" / "could not commit" in production.

    `build_non_interactive_git_env()` copies `os.environ` verbatim, so this
    test must remove these vars from `os.environ` itself (monkeypatch.delenv)
    -- merely unsetting them in the calling shell is not sufficient, since a
    developer's shell may have GIT_EDITOR=true set globally, masking the bug.

    Before the fix: git conflict-resolution commits open an editor (because
    the conflict appended a "# Conflicts:" section to COMMIT_EDITMSG,
    forcing msg_needs_editing) with no configured editor and no real tty --
    the commit fails and `_git("rebase", "--continue")` returns non-zero,
    causing `sync()` to raise RuntimeError("conflict resolution failed: ...").
    """
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("GIT_SEQUENCE_EDITOR", raising=False)
    # Force git's own "no controlling terminal" detection (rather than
    # actually spawning a real interactive `vi` against this test process's
    # tty, which would hang waiting for keystrokes) -- this is exactly the
    # code path that prints the literal "Terminal is dumb, but EDITOR
    # unset" error quoted in the production incident.
    monkeypatch.setenv("TERM", "dumb")
    # Code review hardening (non-blocking nit on Bug #1500): the delenv
    # calls above only remove EDITOR/GIT_EDITOR/VISUAL/GIT_SEQUENCE_EDITOR
    # from the process env, but build_non_interactive_git_env() copies
    # os.environ verbatim -- a host with a global `core.editor` set in
    # ~/.gitconfig (or a system-wide /etc/gitconfig) would still supply an
    # editor via git config precedence, silently passing this test on a
    # broken fix. Isolate git config entirely so the test is deterministic
    # regardless of host git config.
    empty_gitconfig = tmp_path / "empty-gitconfig"
    empty_gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")

    repo, remote = _bootstrap_repo(tmp_path)

    # Both sides modify the same line in the same file to force a genuine
    # merge conflict (mirrors test_genuine_conflict_still_resolves_via_continue).
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "shared.txt", "remote version\n", "remote: shared")
    _git(["push", "origin", "master"], divergent)

    _commit_file(repo, "shared.txt", "local version\n", "local: shared")

    def _resolving_resolver(cidx_meta_path, conflict_files, branch):
        shared = Path(cidx_meta_path) / "shared.txt"
        shared.write_text("resolved\n")
        _git(["add", "shared.txt"], Path(cidx_meta_path))
        return SimpleNamespace(success=True, error=None)

    resolver = SimpleNamespace(resolve=_resolving_resolver)

    # Must succeed non-interactively regardless of the ambient process
    # environment's EDITOR/GIT_EDITOR/VISUAL configuration.
    result = CidxMetaBackupSync(str(repo), "master", resolver).sync()

    assert result.skipped is False
    assert result.sync_failure is None

    verify = tmp_path / "verify-bug1500"
    _clone_repo(remote, verify)
    assert (verify / "shared.txt").read_text() == "resolved\n"


# ---------------------------------------------------------------------------
# Bug #1539: sync() raises a typed, data-carrying exception on an unresolved
# conflict, and this module exposes a pure resolve_upstream_target_sha()
# helper -- the actual retry/quarantine DECISION (keyed on that SHA, never
# on freeform error text) lives in RefreshScheduler (persisted,
# cross-process). See
# tests/unit/global_repos/test_refresh_scheduler_cidx_meta_conflict_quarantine_1539.py
# for that mechanism.
# ---------------------------------------------------------------------------


def _make_shared_txt_conflict(tmp_path: Path):
    repo, remote = _bootstrap_repo(tmp_path)
    divergent = tmp_path / "divergent"
    _clone_repo(remote, divergent)
    _commit_file(divergent, "shared.txt", "remote version\n", "remote: shared")
    _git(["push", "origin", "master"], divergent)
    _commit_file(repo, "shared.txt", "local version\n", "local: shared")
    return repo, remote, divergent


def _never_resolving_resolver(cidx_meta_path, conflict_files, branch):
    return SimpleNamespace(success=False, error="LLM could not resolve conflict")


def test_unresolved_conflict_raises_typed_error_with_data_1539(tmp_path):
    """Bug #1539: an unresolved conflict raises ConflictResolutionFailedError
    (not a bare RuntimeError) carrying the conflicted file list and raw
    failure detail, so a caller (RefreshScheduler) can log diagnostics
    without re-parsing the message string. The actual quarantine key is
    the upstream target SHA (resolve_upstream_target_sha), resolved by the
    caller independently -- this exception's data is diagnostics only.
    """
    import pytest
    from code_indexer.server.services.cidx_meta_backup.sync import (
        CidxMetaBackupSync,
        ConflictResolutionFailedError,
    )

    repo, _remote, _divergent = _make_shared_txt_conflict(tmp_path)
    resolver = SimpleNamespace(resolve=_never_resolving_resolver)

    with pytest.raises(ConflictResolutionFailedError) as exc_info:
        CidxMetaBackupSync(str(repo), "master", resolver).sync()

    exc = exc_info.value
    assert exc.conflict_files == ["shared.txt"]
    assert exc.detail == "LLM could not resolve conflict"
    assert str(exc).startswith("conflict resolution failed: ")
    # Also a plain RuntimeError, so any pre-existing generic handler upstream
    # keeps working unchanged.
    assert isinstance(exc, RuntimeError)


def test_resolve_upstream_target_sha_matches_real_rev_parse_1539(tmp_path):
    """Bug #1539 (Codex round-3 SHA-based redesign): resolve_upstream_target_sha
    returns the SAME commit SHA a direct `git rev-parse origin/{branch}`
    would -- this is the stable identity RefreshScheduler keys quarantine
    on, replacing the rejected text-fingerprint approach entirely.
    """
    from code_indexer.server.services.cidx_meta_backup.sync import (
        resolve_upstream_target_sha,
    )

    repo, remote = _bootstrap_repo(tmp_path)

    resolved = resolve_upstream_target_sha(str(repo), "master")

    expected = _git(["rev-parse", "origin/master"], repo)
    assert resolved == expected


def test_resolve_upstream_target_sha_none_on_fetch_failure_1539(tmp_path):
    """A fetch failure (unreachable remote) must return None, never raise
    -- the caller treats None as "cannot determine, proceed with sync()"."""
    from code_indexer.server.services.cidx_meta_backup.sync import (
        resolve_upstream_target_sha,
    )

    repo, _remote = _bootstrap_repo(tmp_path)
    _git(["remote", "set-url", "origin", "file:///definitely/missing/repo.git"], repo)

    assert resolve_upstream_target_sha(str(repo), "master") is None


def test_resolve_upstream_target_sha_none_on_missing_branch_1539(tmp_path):
    """A branch that does not exist on the remote must return None (the
    rev-parse step fails), never raise."""
    from code_indexer.server.services.cidx_meta_backup.sync import (
        resolve_upstream_target_sha,
    )

    repo, _remote = _bootstrap_repo(tmp_path)

    assert resolve_upstream_target_sha(str(repo), "no-such-branch") is None


def test_resolve_upstream_target_sha_none_on_invalid_input_1539():
    """Bug #1539 Codex round-4 finding 2: invalid inputs must return None,
    never raise ValueError -- the function's whole contract is "never
    raises, caller treats None as cannot-determine"."""
    from code_indexer.server.services.cidx_meta_backup.sync import (
        resolve_upstream_target_sha,
    )

    assert resolve_upstream_target_sha("", "master") is None
    assert resolve_upstream_target_sha("   ", "master") is None
    assert resolve_upstream_target_sha("/some/path", "") is None
    assert resolve_upstream_target_sha(None, "master") is None  # type: ignore[arg-type]
    assert resolve_upstream_target_sha("/some/path", None) is None  # type: ignore[arg-type]


def test_resolve_upstream_target_sha_none_on_timeout_1539(tmp_path, monkeypatch):
    """Bug #1539 Codex round-4 finding 2: a hung `git fetch` (dead remote
    that accepts a connection but never responds) must be bounded by
    `_GIT_SUBPROCESS_TIMEOUT_SECONDS` and return None quickly, never hang
    the whole scheduler cycle indefinitely.
    """
    import os
    import shutil
    import time

    import code_indexer.server.services.cidx_meta_backup.sync as sync_module

    # Patched timeout: short enough to keep this test fast.
    PATCHED_TIMEOUT_SECONDS = 1
    # Fake git's simulated hang: comfortably longer than the patched
    # timeout, so a correct implementation never actually waits for it.
    FAKE_HANG_SECONDS = 30
    # Upper bound on observed wall-clock time: generous slack above the
    # patched timeout for process-spawn overhead, but far below
    # FAKE_HANG_SECONDS -- proves the timeout fired, not the sleep completing.
    MAX_EXPECTED_ELAPSED_SECONDS = 10

    monkeypatch.setattr(
        sync_module, "_GIT_SUBPROCESS_TIMEOUT_SECONDS", PATCHED_TIMEOUT_SECONDS
    )

    repo, _remote = _bootstrap_repo(tmp_path)

    real_git = shutil.which("git")
    assert real_git is not None, "git executable not found on PATH"

    # A fake `git` on PATH that hangs forever on `fetch`, ahead of the real
    # git so subprocess.run's PATH lookup finds it first.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "fetch" ]; then\n'
        f"  sleep {FAKE_HANG_SECONDS}\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    start = time.monotonic()
    result = sync_module.resolve_upstream_target_sha(str(repo), "master")
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < MAX_EXPECTED_ELAPSED_SECONDS
