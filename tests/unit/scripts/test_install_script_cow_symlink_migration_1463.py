"""Tests for Bug #1463: install-cidx-server.sh's
_reconcile_existing_cow_symlink_entry() must safely MIGRATE a real
non-empty golden-repos/activated-repos directory into a `.legacy.bug1337`
(or `.legacy.bug1052` for activated-repos) backup + fresh CoW-mount symlink,
instead of only warning forever with no convergence (the exact
staging-cluster regression this bug fixes).

These tests run the ACTUAL bash function (sourced from the real script,
`main()` never invoked thanks to the script's own BASH_SOURCE guard) against
REAL temp directories -- no mocking of the filesystem, matching this
project's Anti-Mock rule and this codebase's existing precedent for
executing real shell scripts under test (see
tests/unit/scripts/test_cluster_migrate_cow_daemon.py).

AC1: real non-empty directory -> migrated to a `.legacy.bug<N>` backup
     (content preserved, never deleted) AND a fresh symlink is created at
     the original path pointing at the CoW-mount target, all performed
     atomically by `_migrate_real_dir_to_cow_symlink()` (mirroring
     deployment_executor.py's Python twin of the same name EXACTLY: mkdir
     target FIRST, then mv link->legacy, then ln -s target->link, with a
     rollback -- legacy->link -- if the symlink step fails). The reconcile
     function fully handles this outcome and returns 0 (caller must NOT
     also try to create the symlink).
AC2: a pre-existing `.legacy.bug1337` backup blocks the migration -- WARNING
     logged, source directory untouched, existing backup untouched (no
     silent overwrite, no partial state).
AC3 (Bug #1463 follow-up, Finding 2): the legacy-backup suffix is
     bug-number-aware per directory type -- `bug1337` for golden-repos,
     `bug1052` for activated-repos -- matching
     deployment_executor.py:5413's `bug_number="1052"` call for
     activated-repos exactly.
AC4 (Bug #1463 follow-up, Finding 1): when the migration's mkdir(target) or
     ln -s step fails (e.g. a transiently unwritable CoW mount -- the exact
     NFS-permission scenario Bug #1462 fixed), the original real directory
     is preserved/restored at the original path -- never left in a
     migrated-but-unlinked broken state with no self-recovery. Driven
     through the REAL caller `ensure_cow_symlink()`, not just the isolated
     `_reconcile_existing_cow_symlink_entry()` function.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "install-cidx-server.sh"
)

skip_if_no_script = pytest.mark.skipif(
    not _SCRIPT_PATH.exists(), reason="install-cidx-server.sh not found"
)


def _run_reconcile(
    link_path: Path, target: Path, link_name: str = "golden-repos"
) -> subprocess.CompletedProcess:
    """Source the real script (functions only -- main() is guarded by
    BASH_SOURCE) and invoke _reconcile_existing_cow_symlink_entry() with an
    `if` wrapper so the script's own `set -e` (inherited from sourcing)
    does not abort on the function's meaningful non-zero return value."""
    bash_snippet = f"""
source {str(_SCRIPT_PATH)!r}
if _reconcile_existing_cow_symlink_entry {str(link_path)!r} {str(target)!r} {link_name!r}; then
    echo "__RECONCILE_RC__:0"
else
    echo "__RECONCILE_RC__:1"
fi
"""
    return subprocess.run(
        ["bash", "-c", bash_snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )


@skip_if_no_script
class TestMigratesRealDirectoryWithContentBug1463:
    def test_real_directory_with_content_is_migrated_to_legacy_backup(
        self, tmp_path: Path
    ) -> None:
        link_path = tmp_path / "golden-repos"
        target = tmp_path / "cow-storage" / "golden-repos"
        link_path.mkdir(parents=True)
        sentinel = link_path / "metadata.json"
        sentinel.write_text('{"repos": []}')

        result = _run_reconcile(link_path, target)

        assert result.returncode == 0, (
            f"bash invocation itself must not fail: stderr={result.stderr!r}"
        )
        assert "__RECONCILE_RC__:0" in result.stdout, (
            "function must return 0 (fully handled here -- it now performs "
            "the whole migration including symlink creation itself, so the "
            f"caller must NOT also try to create the symlink) -- stdout={result.stdout!r}"
        )

        legacy_path = tmp_path / "golden-repos.legacy.bug1337"
        legacy_sentinel = legacy_path / "metadata.json"
        assert link_path.is_symlink(), (
            "the reconcile function must create the symlink itself (mirroring "
            "deployment_executor.py's _migrate_real_dir_to_cow_symlink, which "
            "performs mkdir+mv+ln as one atomic unit), not defer it to the caller"
        )
        assert os.readlink(str(link_path)) == str(target), (
            f"symlink must point at {target}"
        )
        assert legacy_path.is_dir(), (
            "content must be preserved at a .legacy.bug1337 backup, never deleted"
        )
        assert legacy_sentinel.read_text() == '{"repos": []}', (
            "user data must survive the migration unmodified"
        )
        assert "Bug #1463" in result.stdout or "migrated" in result.stdout, (
            f"migration must be logged: stdout={result.stdout!r}"
        )


@skip_if_no_script
class TestRefusesWhenLegacyBackupAlreadyExistsBug1463:
    def test_preexisting_legacy_backup_blocks_migration(self, tmp_path: Path) -> None:
        link_path = tmp_path / "golden-repos"
        target = tmp_path / "cow-storage" / "golden-repos"
        link_path.mkdir(parents=True)
        current_sentinel = link_path / "metadata.json"
        current_sentinel.write_text('{"repos": ["current"]}')

        legacy_path = tmp_path / "golden-repos.legacy.bug1337"
        legacy_path.mkdir(parents=True)
        legacy_sentinel = legacy_path / "old-metadata.json"
        legacy_sentinel.write_text('{"repos": ["stale-from-prior-run"]}')

        result = _run_reconcile(link_path, target)

        assert result.returncode == 0
        assert "__RECONCILE_RC__:0" in result.stdout, (
            "function must return 0 (fully handled -- refused, not deferred "
            f"to caller): stdout={result.stdout!r}"
        )
        assert link_path.exists() and not link_path.is_symlink(), (
            "current directory must be completely untouched"
        )
        assert current_sentinel.read_text() == '{"repos": ["current"]}'
        assert not target.exists(), (
            "the CoW-mount target must NEVER be created when the migration "
            "is refused -- no partial state"
        )
        assert legacy_sentinel.read_text() == '{"repos": ["stale-from-prior-run"]}', (
            "pre-existing backup must not be overwritten or merged"
        )
        assert not (legacy_path / "metadata.json").exists(), (
            "current directory's content must not have been merged into "
            "the pre-existing backup"
        )
        assert "WARN" in result.stderr, (
            f"a backup collision must be logged loudly: stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Bug #1463 follow-up (code review Finding 2, LOW): the legacy-backup suffix
# must be bug-number-aware per directory type, matching
# deployment_executor.py:5413's `bug_number="1052"` call for
# activated-repos exactly (golden-repos stays `bug1337`).
# ---------------------------------------------------------------------------


@skip_if_no_script
class TestBugNumberSuffixPerDirectoryTypeBug1463Finding2:
    def test_golden_repos_uses_bug1337_suffix(self, tmp_path: Path) -> None:
        link_path = tmp_path / "golden-repos"
        target = tmp_path / "cow-storage" / "golden-repos"
        link_path.mkdir(parents=True)
        (link_path / "metadata.json").write_text("{}")

        _run_reconcile(link_path, target, link_name="golden-repos")

        assert (tmp_path / "golden-repos.legacy.bug1337").is_dir(), (
            "golden-repos must be backed up with the .legacy.bug1337 suffix"
        )
        assert not (tmp_path / "golden-repos.legacy.bug1052").exists(), (
            "golden-repos must NEVER use the activated-repos bug number"
        )

    def test_activated_repos_uses_bug1052_suffix(self, tmp_path: Path) -> None:
        link_path = tmp_path / "activated-repos"
        target = tmp_path / "cow-storage" / "activated-repos"
        link_path.mkdir(parents=True)
        (link_path / "metadata.json").write_text("{}")

        _run_reconcile(link_path, target, link_name="activated-repos")

        assert (tmp_path / "activated-repos.legacy.bug1052").is_dir(), (
            "activated-repos must be backed up with the .legacy.bug1052 suffix, "
            "matching deployment_executor.py:5413's bug_number='1052'"
        )
        assert not (tmp_path / "activated-repos.legacy.bug1337").exists(), (
            "activated-repos must NEVER use the golden-repos bug number "
            "(this was the exact Finding 2 defect: hardcoded bug1337 for both)"
        )


# ---------------------------------------------------------------------------
# Bug #1463 follow-up (code review Finding 1, MEDIUM): when the CoW-mount
# target is transiently unwritable, the migration must roll back / never
# touch the original real directory -- the node must self-heal, not be left
# with NO golden-repos/activated-repos entry at all. Driven through the REAL
# caller ensure_cow_symlink(), not just the isolated reconcile function --
# the reviewer specifically flagged that the prior test suite never drove
# this failure path through the real caller.
# ---------------------------------------------------------------------------


def _run_ensure_cow_symlink(
    data_dir: Path,
    nfs_mount: Path,
    link_name: str = "golden-repos",
    pre_call_snippet: str = "",
) -> subprocess.CompletedProcess:
    """Source the real script and invoke the real caller ensure_cow_symlink()
    (not the isolated reconcile/migrate helpers), with DATA_DIR/NFS_MOUNT
    overridden to point at test tmp_path fixtures after sourcing resets them
    to their script defaults."""
    bash_snippet = f"""
source {str(_SCRIPT_PATH)!r}
DATA_DIR={str(data_dir)!r}
NFS_MOUNT={str(nfs_mount)!r}
COW_LOCAL_BIND=false
DRY_RUN=false
{pre_call_snippet}
if ensure_cow_symlink {link_name!r}; then
    echo "__ENSURE_RC__:0"
else
    echo "__ENSURE_RC__:1"
fi
"""
    return subprocess.run(
        ["bash", "-c", bash_snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )


@skip_if_no_script
class TestMigrationRollbackOnTargetMkdirFailureBug1463Finding1:
    def test_target_mkdir_failure_leaves_original_directory_untouched(
        self, tmp_path: Path
    ) -> None:
        """Real filesystem failure: NFS_MOUNT itself exists as a plain FILE
        (not a directory), so `mkdir -p "${NFS_MOUNT}/golden-repos"` fails
        for real -- the exact category of transient-unwritable-CoW-mount
        failure Bug #1462 addressed. Because the fix orders mkdir(target)
        BEFORE the mv, the original directory must never even be touched
        (strictly stronger than a rename-then-rollback: nothing was moved)."""
        data_dir = tmp_path / "data-dir"
        link_path = data_dir / "data" / "golden-repos"
        link_path.mkdir(parents=True)
        sentinel = link_path / "metadata.json"
        sentinel.write_text('{"repos": ["real-data"]}')

        nfs_mount = tmp_path / "nfs-mount-blocked-by-file"
        nfs_mount.write_text("this is a plain file, not a directory")

        result = _run_ensure_cow_symlink(data_dir, nfs_mount)

        assert result.returncode == 0, (
            f"bash invocation itself must not fail: stderr={result.stderr!r}"
        )
        assert link_path.is_dir() and not link_path.is_symlink(), (
            "the original real directory must still be present and untouched "
            f"at {link_path} -- the node must self-heal, never be left with "
            f"no golden-repos entry at all. stderr={result.stderr!r}"
        )
        assert sentinel.read_text() == '{"repos": ["real-data"]}', (
            "original user data must be completely unmodified"
        )
        assert not (data_dir / "data" / "golden-repos.legacy.bug1337").exists(), (
            "no legacy backup should be created when mkdir(target) fails "
            "before any mv is attempted"
        )
        assert "WARN" in result.stderr, (
            f"the mkdir(target) failure must be logged loudly: stderr={result.stderr!r}"
        )


@skip_if_no_script
class TestMigrationRollbackOnSymlinkFailureBug1463Finding1:
    def test_symlink_failure_after_successful_move_rolls_back_to_original(
        self, tmp_path: Path
    ) -> None:
        """Forces the `ln -s` step specifically to fail AFTER a real
        mkdir(target) and a real mv(link->legacy) have already succeeded, by
        shadowing the external `ln` command with a bash function for this
        one invocation only (a deliberate, minimal fault-injection
        substitution of exactly one external command -- not a mock of any
        of the production bash functions under test, which still execute
        their real logic and observe a real non-zero exit status, exactly
        as they would for a genuine permission-denied/ENOSPC failure).
        Verifies the rollback (legacy -> link) restores the original real
        directory at link_path, mirroring
        deployment_executor.py's `except OSError: legacy_path.rename(link_path)`."""
        data_dir = tmp_path / "data-dir"
        link_path = data_dir / "data" / "golden-repos"
        link_path.mkdir(parents=True)
        sentinel = link_path / "metadata.json"
        sentinel.write_text('{"repos": ["real-data"]}')

        nfs_mount = tmp_path / "nfs-mount"

        force_ln_failure_stub = """
ln() {
    if [[ "$1" == "-s" ]]; then
        return 1
    fi
    command ln "$@"
}
"""
        result = _run_ensure_cow_symlink(
            data_dir, nfs_mount, pre_call_snippet=force_ln_failure_stub
        )

        assert result.returncode == 0, (
            f"bash invocation itself must not fail: stderr={result.stderr!r}"
        )
        legacy_path = data_dir / "data" / "golden-repos.legacy.bug1337"
        assert not legacy_path.exists(), (
            "the rollback must move the legacy backup back to link_path -- "
            f"no leftover legacy backup should remain. stderr={result.stderr!r}"
        )
        assert link_path.is_dir() and not link_path.is_symlink(), (
            "the original real directory must be RESTORED at link_path after "
            "the symlink-creation failure -- never left in a "
            f"migrated-but-unlinked broken state. stderr={result.stderr!r}"
        )
        assert sentinel.read_text() == '{"repos": ["real-data"]}', (
            "original user data must survive the failed migration + rollback "
            "completely unmodified"
        )
        assert "WARN" in result.stderr, (
            f"the symlink failure and rollback must be logged loudly: "
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Bug #1464 (shell parity): _resolve_cow_symlink_target has the same harmful
# daemon-host special case as deployment_executor.py's
# _resolve_golden_repos_symlink_target had before Bug #1464's Part 1 fix --
# it special-cases COW_LOCAL_BIND=true + a resolved COW_DAEMON_STORAGE_PATH
# to use the daemon-local form instead of {NFS_MOUNT}/{link_name}. That
# assumes the code-indexer service account can locally traverse the daemon
# operator's storage path, which is false on at least one real cluster node
# (0700 home dir owned by a different user). The target must always be the
# NFS mount_point form, matching the Python fix exactly.
# ---------------------------------------------------------------------------


def _run_resolve_target(
    nfs_mount: Path,
    link_name: str = "golden-repos",
    cow_local_bind: bool = False,
    daemon_storage_path: str = "",
) -> subprocess.CompletedProcess:
    """Source the real script and invoke _resolve_cow_symlink_target()
    directly, capturing its stdout (the resolved target path)."""
    bash_snippet = f"""
source {str(_SCRIPT_PATH)!r}
NFS_MOUNT={str(nfs_mount)!r}
COW_LOCAL_BIND={"true" if cow_local_bind else "false"}
COW_DAEMON_STORAGE_PATH={daemon_storage_path!r}
_resolve_cow_symlink_target {link_name!r}
"""
    return subprocess.run(
        ["bash", "-c", bash_snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )


@skip_if_no_script
class TestResolveTargetAlwaysUsesNfsMountBug1464:
    def test_resolve_target_ignores_daemon_local_bind_and_storage_path(
        self, tmp_path: Path
    ) -> None:
        nfs_mount = tmp_path / "mnt-cow-storage"
        daemon_storage_path = tmp_path / "srv-cow-xfs"

        result = _run_resolve_target(
            nfs_mount,
            link_name="golden-repos",
            cow_local_bind=True,
            daemon_storage_path=str(daemon_storage_path),
        )

        assert result.returncode == 0, (
            f"bash invocation itself must not fail: stderr={result.stderr!r}"
        )
        resolved = result.stdout.strip()
        assert resolved == str(nfs_mount / "golden-repos"), (
            "target must always be {NFS_MOUNT}/golden-repos, even with "
            f"COW_LOCAL_BIND=true and COW_DAEMON_STORAGE_PATH set: "
            f"got {resolved!r}"
        )

    def test_resolve_target_for_activated_repos_ignores_daemon_local_bind(
        self, tmp_path: Path
    ) -> None:
        """Same fix, applied to activated-repos too -- _resolve_cow_symlink_target
        is a single function shared by both link types."""
        nfs_mount = tmp_path / "mnt-cow-storage"
        daemon_storage_path = tmp_path / "srv-cow-xfs"

        result = _run_resolve_target(
            nfs_mount,
            link_name="activated-repos",
            cow_local_bind=True,
            daemon_storage_path=str(daemon_storage_path),
        )

        assert result.returncode == 0
        resolved = result.stdout.strip()
        assert resolved == str(nfs_mount / "activated-repos"), f"got {resolved!r}"


# ---------------------------------------------------------------------------
# Bug #1464 (shell parity): _reconcile_existing_cow_symlink_entry must
# SELF-HEAL a mismatched symlink target (atomic re-point), not only WARN
# forever -- mirroring deployment_executor.py's
# _reconcile_existing_golden_repos_symlink fix. The repair must never touch
# real directory data on either the old or new target side.
# ---------------------------------------------------------------------------


@skip_if_no_script
class TestReconcileSelfHealsMismatchedSymlinkBug1464:
    def test_mismatched_symlink_is_repaired_to_new_target(self, tmp_path: Path) -> None:
        old_target = tmp_path / "old-daemon-local" / "golden-repos"
        old_target.mkdir(parents=True)
        old_sentinel = old_target / "some-repo-data.txt"
        old_sentinel.write_text("real data at the old (stale) target")

        new_target = tmp_path / "mnt-cow-storage" / "golden-repos"
        new_target.mkdir(parents=True)
        new_sentinel = new_target / "some-repo-data.txt"
        new_sentinel.write_text("real data at the new (correct) target")

        link_path = tmp_path / "golden-repos"
        os.symlink(str(old_target), str(link_path))

        result = _run_reconcile(link_path, new_target)

        assert result.returncode == 0, (
            f"bash invocation itself must not fail: stderr={result.stderr!r}"
        )
        assert "__RECONCILE_RC__:0" in result.stdout, (
            f"function must return 0 (fully handled): stdout={result.stdout!r}"
        )
        assert os.readlink(str(link_path)) == str(new_target), (
            "symlink must be repaired to point at the new target: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert old_sentinel.read_text() == "real data at the old (stale) target", (
            "the old target's real data must never be touched, moved, or deleted"
        )
        assert new_sentinel.read_text() == "real data at the new (correct) target", (
            "the new target's real data must never be touched, moved, or deleted"
        )
