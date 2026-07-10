"""
Tests for Bug #1343: Activation CoW clone re-hashes entire tracked tree on
every activation.

Root cause (proven by strace): `cp --reflink=auto` always allocates a new
inode (and ctime) for every copied file. Under git's DEFAULT
`core.checkStat=default`, inode/ctime/dev/uid/gid are compared in addition
to mtime+size when deciding whether an index entry is stat-dirty, so the
inode change ALONE invalidates the copied `.git/index` even when mtime is
preserved. Only `-a` (preserve mtimes) AND `core.checkStat=minimal`
(mtime+size only) together yield zero re-hashed files.

AC-1 (performance): activation CoW clone opens ZERO tracked regular files
during `git update-index --refresh`.
AC-2 (solo path): `LocalCloneBackend`-backed activation preserves mtimes AND
sets `core.checkStat=minimal` BEFORE the refresh.

Real git + real strace, NO mocking of git subprocess calls.
"""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import List, Set
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

STRACE_AVAILABLE = shutil.which("strace") is not None
N_FILES = 60
# Aged mtime well in the past to avoid git's racy-git same-second heuristic.
PAST_DATE = "2020-01-01 00:00:00"


def _run(cmd: List[str], cwd: str, timeout: int = 60, check: bool = True):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{cmd} failed in {cwd}: {result.stderr}")
    return result


def _build_settled_source_repo(root: str) -> List[str]:
    """Create a git repo with N_FILES tracked files, aged mtimes, settled index.

    Returns the list of tracked relative file paths.
    """
    os.makedirs(root, exist_ok=True)
    _run(["git", "init"], cwd=root)
    _run(["git", "config", "user.name", "Test User"], cwd=root)
    _run(["git", "config", "user.email", "test@example.com"], cwd=root)

    relpaths = []
    for i in range(N_FILES):
        relpath = f"trackedfile_{i:04d}.dat"
        path = os.path.join(root, relpath)
        with open(path, "w") as f:
            f.write(f"content for file {i}\n" * 5)
        relpaths.append(relpath)

    # Age all mtimes BEFORE `git add` so the resulting index entries are
    # non-racy (mtime not within the same second as the index write).
    for relpath in relpaths:
        _run(["touch", "-d", PAST_DATE, relpath], cwd=root)

    _run(["git", "add", "."], cwd=root)
    _run(["git", "commit", "-m", "initial commit"], cwd=root)

    # Settle the SOURCE index (non-racy requirement).
    _run(["git", "update-index", "--refresh"], cwd=root, check=False)
    _run(["git", "status"], cwd=root)

    return relpaths


def _strace_tracked_opens(dest_path: str, relpaths: List[str], tmp_dir: str) -> int:
    """Run `strace -f -e trace=openat,read git update-index --refresh -q`
    inside dest_path and count how many DISTINCT tracked files were opened.
    """
    strace_log = os.path.join(tmp_dir, f"strace_{os.path.basename(dest_path)}.log")
    subprocess.run(
        [
            "strace",
            "-f",
            "-e",
            "trace=openat,read",
            "-o",
            strace_log,
            "git",
            "update-index",
            "--refresh",
            "-q",
        ],
        cwd=dest_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    opened: Set[str] = set()
    open_re = re.compile(r'openat\([^,]+,\s*"([^"]+)"')
    with open(strace_log, "r", errors="replace") as f:
        for line in f:
            if "openat(" not in line:
                continue
            m = open_re.search(line)
            if not m:
                continue
            raw_path = m.group(1)
            if "/.git/" in raw_path or raw_path.endswith("/.git"):
                continue
            for relpath in relpaths:
                if raw_path == relpath or raw_path.endswith("/" + relpath):
                    opened.add(relpath)
                    break
    return len(opened)


def _set_checkstat(dest_path: str, mode: str) -> None:
    _run(["git", "config", "--local", "core.checkStat", mode], cwd=dest_path)


@pytest.mark.skipif(
    not STRACE_AVAILABLE, reason="strace binary not available in this environment"
)
class TestActivationCowNoRehashBug1343:
    """AC-1 / AC-2: activation CoW clone must not re-hash the tracked tree."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def source_repo(self, tmp_dir):
        root = os.path.join(tmp_dir, "golden")
        relpaths = _build_settled_source_repo(root)
        return root, relpaths

    @pytest.fixture
    def activated_repo_manager(self, tmp_dir, source_repo):
        source_path, _ = source_repo
        golden_repo_manager_mock = MagicMock()
        golden_repo = GoldenRepo(
            alias="test-repo",
            repo_url="https://github.com/example/test-repo.git",
            default_branch="master",
            clone_path=source_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        golden_repo_manager_mock.golden_repos = {"test-repo": golden_repo}
        # Avoid MagicMock auto-attribute leaking a Mock into
        # resource_config.cow_clone_timeout (used as a subprocess timeout=
        # value) -- force the real code path to fall back to the numeric
        # _COW_CLONE_TIMEOUT_DEFAULT.
        golden_repo_manager_mock.resource_config = None
        background_job_manager_mock = MagicMock()
        return ActivatedRepoManager(
            data_dir=os.path.join(tmp_dir, "data"),
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=LocalCloneBackend(),
        )

    def test_activation_cow_clone_opens_zero_tracked_files_on_refresh(
        self, activated_repo_manager, source_repo, tmp_dir
    ):
        """AC-1: production `_clone_with_copy_on_write` yields ZERO tracked-file opens."""
        source_path, relpaths = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        # AC-2: checkStat must be minimal in the activated clone's local
        # config, and it must persist across the two call sites
        # (_clone_with_copy_on_write's own refresh AND
        # _configure_git_structure's git status, which both already ran
        # by this point).
        result = _run(
            ["git", "config", "--local", "--get", "core.checkStat"], cwd=dest_path
        )
        assert result.stdout.strip() == "minimal", (
            "Activation must set core.checkStat=minimal BEFORE the refresh (AC-2)"
        )

        opened_count = _strace_tracked_opens(dest_path, relpaths, tmp_dir)
        assert opened_count == 0, (
            f"Expected zero tracked-file opens after activation CoW clone, "
            f"got {opened_count} (AC-1 FAIL -- re-hash regression)"
        )

    def test_negative_control_no_preserve_attrs_with_minimal_checkstat_rehashes_all(
        self, source_repo, tmp_dir
    ):
        """Negative control: -r (mtime NOT preserved) + checkStat=minimal must
        still re-hash ALL files -- proves checkStat alone is insufficient and
        the harness can detect a non-zero re-hash."""
        source_path, relpaths = source_repo
        dest_path = os.path.join(tmp_dir, "control_r_minimal")

        LocalCloneBackend().create_clone_at_path(
            source_path, dest_path, preserve_attrs=False
        )
        _set_checkstat(dest_path, "minimal")

        opened_count = _strace_tracked_opens(dest_path, relpaths, tmp_dir)
        assert opened_count == len(relpaths), (
            f"Negative control (-r + minimal) expected ALL {len(relpaths)} files "
            f"re-hashed, got {opened_count} -- test harness broken"
        )

    def test_negative_control_preserve_attrs_with_default_checkstat_rehashes_all(
        self, source_repo, tmp_dir
    ):
        """Negative control: -a (mtime preserved) + core.checkStat DEFAULT
        (unset) must still re-hash ALL files -- proves -a alone (the
        originally-proposed, now-superseded fix) is insufficient."""
        source_path, relpaths = source_repo
        dest_path = os.path.join(tmp_dir, "control_a_default")

        LocalCloneBackend().create_clone_at_path(
            source_path, dest_path, preserve_attrs=True
        )
        # Deliberately do NOT set core.checkStat -- leave git default.

        opened_count = _strace_tracked_opens(dest_path, relpaths, tmp_dir)
        assert opened_count == len(relpaths), (
            f"Negative control (-a + default checkStat) expected ALL "
            f"{len(relpaths)} files re-hashed, got {opened_count} -- "
            f"test harness broken"
        )


def _make_activated_repo_manager(
    tmp_dir: str, source_path: str
) -> ActivatedRepoManager:
    """Shared factory: build an ActivatedRepoManager wired to LocalCloneBackend
    with a mocked GoldenRepoManager pointing at *source_path*."""
    golden_repo_manager_mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="master",
        clone_path=source_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repo_manager_mock.golden_repos = {"test-repo": golden_repo}
    golden_repo_manager_mock.resource_config = None
    background_job_manager_mock = MagicMock()
    return ActivatedRepoManager(
        data_dir=os.path.join(tmp_dir, "data"),
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=LocalCloneBackend(),
    )


class TestActivationCowCorrectnessBug1343:
    """AC-4/AC-5: core.checkStat=minimal must not mask real edits; tracked
    symlinks must survive activation as symlinks, not be dereferenced."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def source_repo_with_symlink(self, tmp_dir):
        root = os.path.join(tmp_dir, "golden")
        relpaths = _build_settled_source_repo(root)

        symlink_name = "tracked_symlink.dat"
        os.symlink(relpaths[0], os.path.join(root, symlink_name))
        # touch -h ages the symlink's own mtime (not the target's).
        _run(["touch", "-h", "-d", PAST_DATE, symlink_name], cwd=root)
        _run(["git", "add", symlink_name], cwd=root)
        _run(["git", "commit", "-m", "add tracked symlink"], cwd=root)
        _run(["git", "update-index", "--refresh"], cwd=root, check=False)

        return root, relpaths, symlink_name

    @pytest.fixture
    def activated_repo_manager(self, tmp_dir, source_repo_with_symlink):
        source_path, _, _ = source_repo_with_symlink
        return _make_activated_repo_manager(tmp_dir, source_path)

    def test_edit_after_activation_is_detected_as_modified(
        self, activated_repo_manager, source_repo_with_symlink, tmp_dir
    ):
        """AC-4: core.checkStat=minimal must NOT hide a genuine content edit --
        `git status --porcelain` must report the file as modified (M)."""
        source_path, relpaths, _ = source_repo_with_symlink
        dest_path = os.path.join(tmp_dir, "activated")

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        edited_relpath = relpaths[0]
        with open(os.path.join(dest_path, edited_relpath), "a") as f:
            f.write("EDITED CONTENT AFTER ACTIVATION\n")

        result = _run(["git", "status", "--porcelain"], cwd=dest_path)
        matching_lines = [
            line for line in result.stdout.splitlines() if edited_relpath in line
        ]
        assert len(matching_lines) == 1, (
            f"Expected exactly one status line for {edited_relpath}, "
            f"got: {result.stdout!r}"
        )
        assert matching_lines[0].strip().startswith("M"), (
            f"core.checkStat=minimal must not mask a genuine edit (AC-4); "
            f"status line was: {matching_lines[0]!r}"
        )

    def test_tracked_symlink_survives_activation(
        self, activated_repo_manager, source_repo_with_symlink, tmp_dir
    ):
        """AC-5: a tracked symlink must remain a symlink post-activation, not
        be dereferenced into a regular file copy."""
        source_path, _, symlink_name = source_repo_with_symlink
        dest_path = os.path.join(tmp_dir, "activated")

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        activated_symlink_path = os.path.join(dest_path, symlink_name)
        assert os.path.islink(activated_symlink_path), (
            "Tracked symlink must remain a symlink after CoW activation (AC-5), "
            f"got a {'regular file' if os.path.isfile(activated_symlink_path) else 'missing path'}"
        )


class TestActivationCowRestoreGatingBug1343:
    """AC-6: `git restore .` must not be an unconditional post-clone step --
    on a byte-identical CoW clone it is a pure no-op, and unconditionally it
    would silently discard real content if content ever differed."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def source_repo(self, tmp_dir):
        root = os.path.join(tmp_dir, "golden")
        relpaths = _build_settled_source_repo(root)
        return root, relpaths

    @pytest.fixture
    def activated_repo_manager(self, tmp_dir, source_repo):
        source_path, _ = source_repo
        return _make_activated_repo_manager(tmp_dir, source_path)

    def test_byte_identical_clone_produces_clean_tree_and_unmutated_bytes(
        self, activated_repo_manager, source_repo, tmp_dir
    ):
        """AC-6: byte-identical clone behavior is unchanged -- clean tree, no
        data loss, file bytes untouched."""
        source_path, relpaths = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        original_bytes = {}
        for relpath in relpaths:
            with open(os.path.join(source_path, relpath), "rb") as f:
                original_bytes[relpath] = f.read()

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        result = _run(["git", "status", "--porcelain"], cwd=dest_path)
        assert result.stdout.strip() == "", (
            f"Expected clean tree after byte-identical CoW activation, "
            f"got: {result.stdout!r}"
        )

        for relpath in relpaths:
            with open(os.path.join(dest_path, relpath), "rb") as f:
                assert f.read() == original_bytes[relpath], (
                    f"File {relpath} bytes mutated by activation -- data loss "
                    f"risk (AC-6)"
                )

    def test_git_restore_not_invoked_unconditionally(
        self, activated_repo_manager, source_repo, tmp_dir, monkeypatch
    ):
        """AC-6: the success path must not unconditionally shell out to
        `git restore .` -- with AC-1 satisfied it would be a pure no-op, and
        unconditionally it risks discarding real content."""
        source_path, relpaths = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        original_run = subprocess.run
        restore_calls: List[List[str]] = []

        def _spy_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[:2] == ["git", "restore"]:
                restore_calls.append(cmd)
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy_run)

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True
        assert restore_calls == [], (
            f"git restore . must not be invoked unconditionally on a "
            f"byte-identical clone (AC-6); calls made: {restore_calls}"
        )


class TestActivationCowPorcelainWarnGatingBug1347:
    """Bug #1347: the post-clone `git status --porcelain` sanity check (Step
    2b, Bug #1343 AC-6) must ignore UNTRACKED porcelain lines (e.g.
    `?? .code-indexer/`, which is ALWAYS present after activation since the
    index directory is untracked) and warn ONLY when there are genuine
    TRACKED-change lines. `git restore` only ever affects tracked files, so
    an untracked entry was never at risk -- warning on it every single
    activation is a false positive that trips the Phase 3 E2E log-audit
    gate (Story #1122)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def source_repo(self, tmp_dir):
        root = os.path.join(tmp_dir, "golden")
        relpaths = _build_settled_source_repo(root)
        return root, relpaths

    @pytest.fixture
    def activated_repo_manager(self, tmp_dir, source_repo):
        source_path, _ = source_repo
        return _make_activated_repo_manager(tmp_dir, source_path)

    @staticmethod
    def _patch_status_porcelain(monkeypatch, porcelain_stdout: str) -> None:
        """Intercept ONLY the `git status --porcelain` subprocess call made
        by the Step 2b sanity check and force its stdout; every other
        subprocess.run invocation (including the real git plumbing that
        drives the CoW clone) passes through unchanged."""
        original_run = subprocess.run

        def _fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=porcelain_stdout, stderr=""
                )
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _fake_run)

    @staticmethod
    def _sanity_check_warnings(warning_mock: MagicMock) -> List[str]:
        return [
            call.args[0]
            for call in warning_mock.call_args_list
            if call.args and "changes after CoW clone refresh" in call.args[0]
        ]

    def test_untracked_only_porcelain_does_not_warn(
        self, activated_repo_manager, source_repo, tmp_dir, monkeypatch
    ):
        """GIVEN porcelain output containing ONLY untracked (`??`) lines,
        the sanity check must emit NO warning."""
        source_path, _ = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        self._patch_status_porcelain(
            monkeypatch, "?? .code-indexer/\n?? .code-indexer/index/\n"
        )
        warning_mock = MagicMock()
        monkeypatch.setattr(activated_repo_manager.logger, "warning", warning_mock)

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        assert self._sanity_check_warnings(warning_mock) == [], (
            "Untracked-only porcelain output (e.g. `?? .code-indexer/`) must "
            "NOT trigger the CoW sanity-check warning (Bug #1347); "
            f"warning calls: {warning_mock.call_args_list!r}"
        )

    def test_tracked_change_porcelain_warns_with_tracked_lines_only(
        self, activated_repo_manager, source_repo, tmp_dir, monkeypatch
    ):
        """GIVEN porcelain output with a TRACKED modification alongside an
        untracked line, the sanity check DOES warn, and the message contains
        only the tracked line."""
        source_path, _ = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        tracked_line = " M src/foo.py"
        untracked_line = "?? .code-indexer/"
        self._patch_status_porcelain(monkeypatch, f"{tracked_line}\n{untracked_line}\n")
        warning_mock = MagicMock()
        monkeypatch.setattr(activated_repo_manager.logger, "warning", warning_mock)

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        sanity_calls = self._sanity_check_warnings(warning_mock)
        assert len(sanity_calls) == 1, (
            f"Expected exactly one sanity-check warning for tracked changes, "
            f"got: {warning_mock.call_args_list!r}"
        )
        message = sanity_calls[0]
        assert tracked_line.strip() in message, (
            f"Warning message must include the tracked change line, got: {message!r}"
        )
        assert untracked_line not in message, (
            f"Warning message must NOT include untracked lines, got: {message!r}"
        )

    def test_empty_porcelain_does_not_warn(
        self, activated_repo_manager, source_repo, tmp_dir, monkeypatch
    ):
        """GIVEN empty porcelain output, the sanity check must emit NO
        warning (baseline clean-tree case, unchanged from before the fix)."""
        source_path, _ = source_repo
        dest_path = os.path.join(tmp_dir, "activated")

        self._patch_status_porcelain(monkeypatch, "")
        warning_mock = MagicMock()
        monkeypatch.setattr(activated_repo_manager.logger, "warning", warning_mock)

        success = activated_repo_manager._clone_with_copy_on_write(
            source_path, dest_path
        )
        assert success is True

        assert self._sanity_check_warnings(warning_mock) == [], (
            f"Empty porcelain output must not trigger a warning; "
            f"warning calls: {warning_mock.call_args_list!r}"
        )
