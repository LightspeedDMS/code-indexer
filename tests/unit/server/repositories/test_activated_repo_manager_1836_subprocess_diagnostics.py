"""Bug #1836: sweep the remaining 27 `result.stderr`-only subprocess-failure
diagnostics in activated_repo_manager.py through the shared #1832 helper
(`format_completed_process_diagnostic`).

Each site currently interpolates ONLY `result.stderr` into a log message or
raised exception. When the failing subprocess writes its real diagnostic to
stdout instead (common for CLI tools), the message degrades to a bare prefix
with an EMPTY tail.

Discriminating case (per AC2/AC5): every test below drives the target
subprocess call to fail with EMPTY stderr and NON-EMPTY stdout. A test using
non-empty stderr would pass unmodified and prove nothing -- see
`feedback_tdd_red_must_be_discriminating.md`.

Mocking strategy: `subprocess.run` is replaced for the duration of each test
with a fake dispatcher (`_make_run`) that returns a canned Mock response for
specific commands (matched by exact argv, optionally scoped by `cwd` when a
method issues the identical argv against two different working directories)
and a generic success Mock (returncode=0, empty stdout/stderr) for anything
else -- mirroring the pattern already established in
`test_activated_repo_manager_1832_subprocess_diagnostics.py`.

Site 1905 (`sync_with_golden_repository`'s "Golden repository not
accessible" raise) is swept through the shared helper for AC1 consistency
but is NOT given its own discriminating test here: that branch is guarded by
`if "not a git repository" in fetch_result.stderr.lower()`, which is
structurally unreachable with an EMPTY stderr -- the discriminating case for
THIS bug can never take that branch. It is exercised indirectly by every
other test's default-success stderr="" responses never tripping that guard.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
    GitOperationError,
)

_DISCRIMINATING_STDOUT = (
    "actual diagnostic text the failing tool wrote to stdout, not stderr"
)
_MODULE = "code_indexer.server.repositories.activated_repo_manager"


def _ok(cmd, stdout: str = "") -> Mock:
    return Mock(args=list(cmd), returncode=0, stdout=stdout, stderr="")


def _fail_stdout_only(cmd, stdout: str = _DISCRIMINATING_STDOUT) -> Mock:
    """The discriminating failure shape: non-zero exit, EMPTY stderr,
    NON-EMPTY stdout."""
    return Mock(args=list(cmd), returncode=1, stdout=stdout, stderr="")


def _fail_with_stderr(cmd, stderr: str) -> Mock:
    """A failure whose stderr content drives a pre-existing, unrelated
    control-flow branch (e.g. an "already exists" check) -- used only to
    steer flow toward the target site, never as the site under test."""
    return Mock(args=list(cmd), returncode=1, stdout="", stderr=stderr)


def _make_run(overrides: Dict[Any, Mock]):
    """Build a subprocess.run side_effect.

    Override keys may be either `tuple(cmd)` (matches regardless of cwd) or
    `(tuple(cmd), cwd_str)` (matches only that exact cwd) -- the latter is
    needed where a method issues the identical argv against two different
    working directories (e.g. `_detect_and_migrate_legacy_remotes`'s two
    `git remote get-url origin` calls, one per repo). Unmatched commands get
    a generic success response.
    """

    def _run(cmd, *args, **kwargs):
        cwd = kwargs.get("cwd")
        scoped_key: Tuple[Tuple[str, ...], Optional[str]] = (
            tuple(cmd),
            str(cwd) if cwd is not None else None,
        )
        if scoped_key in overrides:
            return overrides[scoped_key]
        key = tuple(cmd)
        if key in overrides:
            return overrides[key]
        return _ok(cmd)

    return _run


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        m = ActivatedRepoManager(
            data_dir=tmp,
            golden_repo_manager=Mock(),
            background_job_manager=Mock(),
        )
        yield m


def _activate_metadata(repo_dir: str, **extra: Any) -> Dict[str, Any]:
    metadata = {"path": repo_dir, "current_branch": "master"}
    metadata.update(extra)
    return metadata


class TestSwitchBranchDiagnostics:
    """switch_branch: sites ~1329 (fetch warning), ~1400 (create warning)."""

    def test_fetch_failed_warning_surfaces_stdout(self, manager, tmp_path, caplog):
        username, user_alias = "u1", "repo1"
        repo_dir = Path(manager.activated_repos_dir) / username / user_alias
        repo_dir.mkdir(parents=True)
        manager._save_metadata(username, user_alias, _activate_metadata(str(repo_dir)))

        overrides = {
            ("git", "remote", "get-url", "origin"): _ok(
                ("git", "remote", "get-url", "origin"),
                stdout="https://example.com/repo.git\n",
            ),
            ("git", "fetch", "origin"): _fail_stdout_only(("git", "fetch", "origin")),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            result = manager.switch_branch(username, user_alias, "master")

        assert result["success"] is True
        warnings = [
            r.message for r in caplog.records if "Git fetch failed" in r.message
        ]
        assert warnings, f"expected a git-fetch warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0]

    def test_create_branch_failed_warning_surfaces_stdout(
        self, manager, tmp_path, caplog
    ):
        username, user_alias, branch = "u2", "repo2", "feature-x"
        repo_dir = Path(manager.activated_repos_dir) / username / user_alias
        repo_dir.mkdir(parents=True)
        manager._save_metadata(username, user_alias, _activate_metadata(str(repo_dir)))

        overrides = {
            ("git", "checkout", branch): _fail_with_stderr(
                ("git", "checkout", branch), stderr="not found"
            ),
            (
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{branch}",
            ): _fail_with_stderr((), stderr="not found"),
            ("git", "show-ref", branch): _fail_with_stderr((), stderr="not found"),
            ("git", "checkout", "-b", branch): _fail_stdout_only(
                ("git", "checkout", "-b", branch)
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            with pytest.raises(GitOperationError):
                manager.switch_branch(username, user_alias, branch, create=True)

        warnings = [
            r.message for r in caplog.records if "Failed to create branch" in r.message
        ]
        assert warnings, f"expected a create-branch warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0]


class TestDetectConflictPathsDiagnostics:
    """_detect_conflict_paths: site ~1581."""

    def test_diff_failure_surfaces_stdout(self, manager, tmp_path, caplog):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        overrides = {
            ("git", "diff", "--name-only", "--diff-filter=U"): _fail_stdout_only(
                ("git", "diff", "--name-only", "--diff-filter=U")
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            result = manager._detect_conflict_paths(str(repo_dir), "u", "alias")

        assert result == (None, None)
        warnings = [
            r.message
            for r in caplog.records
            if "diff --diff-filter=U failed" in r.message
        ]
        assert warnings, f"expected a diff-failure warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0]


class TestSyncWithGoldenRepositoryDiagnostics:
    """sync_with_golden_repository: sites ~1909 (fetch), ~1959 (merge)."""

    def test_fetch_golden_failed_warning_surfaces_stdout(
        self, manager, tmp_path, caplog
    ):
        username, user_alias = "u3", "repo3"
        repo_dir = Path(manager.activated_repos_dir) / username / user_alias
        (repo_dir / ".git").mkdir(parents=True)
        manager._save_metadata(username, user_alias, _activate_metadata(str(repo_dir)))

        overrides = {
            ("git", "fetch", "golden"): _fail_stdout_only(("git", "fetch", "golden")),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            result = manager.sync_with_golden_repository(username, user_alias)

        assert result["success"] is True
        warnings = [
            r.message for r in caplog.records if "Git fetch failed" in r.message
        ]
        assert warnings, f"expected a git-fetch warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0]

    def test_merge_failed_raise_surfaces_stdout(self, manager, tmp_path):
        username, user_alias = "u4", "repo4"
        repo_dir = Path(manager.activated_repos_dir) / username / user_alias
        (repo_dir / ".git").mkdir(parents=True)
        manager._save_metadata(username, user_alias, _activate_metadata(str(repo_dir)))

        overrides = {
            ("git", "fetch", "golden"): _ok(("git", "fetch", "golden")),
            (
                "git",
                "diff",
                "HEAD..golden/master",
                "--name-only",
            ): _ok((), stdout="some_file.py\n"),
            ("git", "merge", "golden/master"): _fail_stdout_only(
                ("git", "merge", "golden/master")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(GitOperationError) as exc_info:
                manager.sync_with_golden_repository(username, user_alias)

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)


class TestListRepositoryBranchesDiagnostics:
    """list_repository_branches: site ~2059."""

    def test_list_local_branches_failure_surfaces_stdout(self, manager, tmp_path):
        username, user_alias = "u5", "repo5"
        repo_dir = Path(manager.activated_repos_dir) / username / user_alias
        (repo_dir / ".git").mkdir(parents=True)
        manager._save_metadata(username, user_alias, _activate_metadata(str(repo_dir)))

        overrides = {
            ("git", "branch", "--format=%(refname:short)"): _fail_stdout_only(
                ("git", "branch", "--format=%(refname:short)")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(GitOperationError) as exc_info:
                manager.list_repository_branches(username, user_alias)

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)


class TestDoActivateRepositoryDiagnostics:
    """_do_activate_repository: sites ~2556 (branch checkout), ~2591/~2604
    (git config user.email/user.name)."""

    def _configured_manager(self, tmp_path) -> ActivatedRepoManager:
        golden_repo_manager = Mock()
        fake_golden_repo = Mock()
        fake_golden_repo.default_branch = "master"
        fake_golden_repo.repo_url = "https://example.com/repo.git"
        fake_golden_repo.clone_path = str(tmp_path / "golden")
        golden_repo_manager.get_golden_repo.return_value = fake_golden_repo
        golden_repo_manager.get_actual_repo_path.return_value = str(tmp_path / "golden")
        m = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"),
            golden_repo_manager=golden_repo_manager,
            background_job_manager=Mock(),
        )

        def _fake_create_clone(src, dst, **kwargs):
            import os

            os.makedirs(dst, exist_ok=True)
            os.makedirs(os.path.join(dst, ".code-indexer"), exist_ok=True)
            return dst

        m._clone_backend = Mock()
        m._clone_backend.create_clone_at_path.side_effect = _fake_create_clone
        return m

    def test_branch_checkout_failure_surfaces_stdout(self, tmp_path):
        m = self._configured_manager(tmp_path)
        branch = "feature"

        overrides = {
            ("git", "checkout", "-B", branch, f"origin/{branch}"): _fail_stdout_only(
                ("git", "checkout", "-B", branch, f"origin/{branch}")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                m._do_activate_repository(
                    username="u6",
                    golden_repo_alias="golden1",
                    branch_name=branch,
                    user_alias="repo6",
                )

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)

    def test_git_config_email_and_name_warnings_surface_stdout(self, tmp_path, caplog):
        m = self._configured_manager(tmp_path)
        branch = "feature"
        committer_service = Mock()
        committer_service.resolve_committer_email.return_value = (
            "git@example.com",
            None,
        )

        overrides = {
            ("git", "config", "user.email", "git@example.com"): _fail_stdout_only(
                ("git", "config", "user.email", "git@example.com"),
                stdout="email diagnostic on stdout",
            ),
            ("git", "config", "user.name", "CIDX User"): _fail_stdout_only(
                ("git", "config", "user.name", "CIDX User"),
                stdout="name diagnostic on stdout",
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            patch(
                f"{_MODULE}.CommitterResolutionService", return_value=committer_service
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = m._do_activate_repository(
                username="u7",
                golden_repo_alias="golden1",
                branch_name=branch,
                user_alias="repo7",
            )

        assert result["success"] is True

        email_warnings = [
            r.message
            for r in caplog.records
            if "Failed to set git config user.email" in r.message
        ]
        name_warnings = [
            r.message
            for r in caplog.records
            if "Failed to set git config user.name" in r.message
        ]
        assert email_warnings, f"expected email warning, got: {caplog.records}"
        assert name_warnings, f"expected name warning, got: {caplog.records}"
        assert "email diagnostic on stdout" in email_warnings[0]
        assert "name diagnostic on stdout" in name_warnings[0]


class TestStopCompositeServicesDiagnostics:
    """_stop_composite_services: site ~3384."""

    def test_service_stop_nonzero_surfaces_stdout(self, manager, tmp_path, caplog):
        repo_path = tmp_path / "composite"
        (repo_path / ".code-indexer").mkdir(parents=True)

        overrides = {
            ("cidx", "stop"): _fail_stdout_only(("cidx", "stop")),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.DEBUG),
        ):
            manager._stop_composite_services(repo_path)

        debugs = [
            r.message
            for r in caplog.records
            if "Service stop returned non-zero" in r.message
        ]
        assert debugs, f"expected a service-stop debug log, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in debugs[0]


class TestCloneWithCopyOnWriteAdditionalDiagnostics:
    """_clone_with_copy_on_write: sites ~3734 (core.bare=false, fatal),
    ~3772 (checkout -f HEAD, non-fatal), ~3803/~3820 (checkstat /
    update-index --refresh, non-fatal). Site ~3879 (cidx fix-config) was
    already fixed by Bug #1832 -- untouched here."""

    def _manager_with_fake_clone(self, tmp_path) -> ActivatedRepoManager:
        m = ActivatedRepoManager(
            data_dir=str(tmp_path / "data"), golden_repo_manager=Mock()
        )

        def _fake_create_clone(src, dst, **kwargs):
            import os

            os.makedirs(dst, exist_ok=True)
            os.makedirs(os.path.join(dst, ".code-indexer"), exist_ok=True)
            return dst

        m._clone_backend = Mock()
        m._clone_backend.create_clone_at_path.side_effect = _fake_create_clone
        return m

    def test_core_bare_false_failure_surfaces_stdout(self, tmp_path):
        m = self._manager_with_fake_clone(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        overrides = {
            ("git", "rev-parse", "--is-bare-repository"): _ok((), stdout="true\n"),
            ("git", "config", "core.bare", "false"): _fail_stdout_only(
                ("git", "config", "core.bare", "false")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                m._clone_with_copy_on_write(str(source), str(dest))

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)

    def test_checkout_f_head_failure_surfaces_stdout(self, tmp_path, caplog):
        m = self._manager_with_fake_clone(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        overrides = {
            ("git", "rev-parse", "--is-bare-repository"): _ok((), stdout="true\n"),
            ("git", "checkout", "-f", "HEAD"): _fail_stdout_only(
                ("git", "checkout", "-f", "HEAD")
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            result = m._clone_with_copy_on_write(str(source), str(dest))

        assert result is True
        warnings = [
            r.message
            for r in caplog.records
            if "git checkout -f HEAD failed" in r.message
        ]
        assert warnings, f"expected a checkout-f-HEAD warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0]

    def test_checkstat_and_update_index_failures_surface_stdout(self, tmp_path, caplog):
        m = self._manager_with_fake_clone(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        # Bypass the bare-conversion branch entirely: is_bare stays False
        # (default success, empty stdout != "true"), but the fake clone
        # still creates a .git dir directly so Step 3 (checkstat /
        # update-index) is reached.
        def _fake_create_clone(src, dst, **kwargs):
            import os

            os.makedirs(dst, exist_ok=True)
            os.makedirs(os.path.join(dst, ".git"), exist_ok=True)
            os.makedirs(os.path.join(dst, ".code-indexer"), exist_ok=True)
            return dst

        m._clone_backend = Mock()
        m._clone_backend.create_clone_at_path.side_effect = _fake_create_clone

        overrides = {
            (
                "git",
                "config",
                "--local",
                "core.checkStat",
                "minimal",
            ): _fail_stdout_only(
                ("git", "config", "--local", "core.checkStat", "minimal"),
                stdout="checkstat diagnostic on stdout",
            ),
            ("git", "update-index", "--refresh"): _fail_stdout_only(
                ("git", "update-index", "--refresh"),
                stdout="update-index diagnostic on stdout",
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            result = m._clone_with_copy_on_write(str(source), str(dest))

        assert result is True
        checkstat_warnings = [
            r.message
            for r in caplog.records
            if "core.checkStat=minimal failed" in r.message
        ]
        update_index_warnings = [
            r.message
            for r in caplog.records
            if "update-index --refresh failed" in r.message
        ]
        assert checkstat_warnings, f"expected checkstat warning, got: {caplog.records}"
        assert update_index_warnings, (
            f"expected update-index warning, got: {caplog.records}"
        )
        assert "checkstat diagnostic on stdout" in checkstat_warnings[0]
        assert "update-index diagnostic on stdout" in update_index_warnings[0]


class TestSetupOriginRemoteForLocalRepoDiagnostics:
    """_setup_origin_remote_for_local_repo: sites ~3958, ~3973."""

    def test_remote_verify_and_fetch_warnings_surface_stdout(
        self, manager, tmp_path, caplog
    ):
        source = tmp_path / "source"
        dest = tmp_path / "dest"

        overrides = {
            ("git", "remote", "-v"): _fail_stdout_only(
                ("git", "remote", "-v"), stdout="remote -v diagnostic on stdout"
            ),
            ("git", "fetch", "origin"): _fail_stdout_only(
                ("git", "fetch", "origin"), stdout="fetch diagnostic on stdout"
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.WARNING),
        ):
            manager._setup_origin_remote_for_local_repo(str(source), str(dest))

        verify_warnings = [
            r.message
            for r in caplog.records
            if "Could not verify git remotes" in r.message
        ]
        fetch_warnings = [
            r.message
            for r in caplog.records
            if "Could not fetch from origin" in r.message
        ]
        assert verify_warnings, f"expected verify warning, got: {caplog.records}"
        assert fetch_warnings, f"expected fetch warning, got: {caplog.records}"
        assert "remote -v diagnostic on stdout" in verify_warnings[0]
        assert "fetch diagnostic on stdout" in fetch_warnings[0]


class TestAddOrUpdateRemoteDiagnostics:
    """_add_or_update_remote: sites ~4020 (update after exists), ~4024
    (add, non "already exists" failure)."""

    def test_add_failure_surfaces_stdout(self, manager, tmp_path):
        repo_path = tmp_path / "repo"

        overrides = {
            (
                "git",
                "remote",
                "add",
                "origin",
                "https://example.com/repo.git",
            ): _fail_stdout_only(
                ("git", "remote", "add", "origin", "https://example.com/repo.git")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._add_or_update_remote(
                    str(repo_path), "origin", "https://example.com/repo.git"
                )

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)

    def test_update_after_exists_failure_surfaces_stdout(self, manager, tmp_path):
        repo_path = tmp_path / "repo"

        overrides = {
            (
                "git",
                "remote",
                "add",
                "origin",
                "https://example.com/repo.git",
            ): _fail_with_stderr(
                (),
                stderr="fatal: remote origin already exists.",
            ),
            (
                "git",
                "remote",
                "set-url",
                "origin",
                "https://example.com/repo.git",
            ): _fail_stdout_only(
                ("git", "remote", "set-url", "origin", "https://example.com/repo.git")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._add_or_update_remote(
                    str(repo_path), "origin", "https://example.com/repo.git"
                )

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)


class TestConfigureGitStructureDiagnostics:
    """_configure_git_structure: sites ~4089 (fetch golden), ~4103 (git
    status)."""

    def test_fetch_golden_failure_surfaces_stdout(self, manager, tmp_path):
        source = tmp_path / "source"
        dest = tmp_path / "dest"

        overrides = {
            ("git", "fetch", "golden"): _fail_stdout_only(("git", "fetch", "golden")),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._configure_git_structure(str(source), str(dest))

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)

    def test_git_status_failure_surfaces_stdout(self, manager, tmp_path):
        source = tmp_path / "source"
        dest = tmp_path / "dest"

        overrides = {
            ("git", "fetch", "golden"): _ok(("git", "fetch", "golden")),
            ("git", "status"): _fail_stdout_only(("git", "status")),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._configure_git_structure(str(source), str(dest))

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)


class TestDetectAndMigrateLegacyRemotesDiagnostics:
    """_detect_and_migrate_legacy_remotes: sites ~4218 (remove golden),
    ~4232 (rename origin to golden)."""

    def test_remove_existing_golden_remote_failure_surfaces_stdout(
        self, manager, tmp_path
    ):
        repo_dir = tmp_path / "repo"
        golden_repo_path = tmp_path / "golden"

        overrides = {
            (("git", "remote", "get-url", "origin"), str(repo_dir)): _ok(
                (), stdout="/legacy/local/path\n"
            ),
            (("git", "remote", "get-url", "origin"), str(golden_repo_path)): _ok(
                (), stdout="https://example.com/repo.git\n"
            ),
            (("git", "remote", "get-url", "golden"), str(repo_dir)): _ok(
                (), stdout="old-golden-url\n"
            ),
            ("git", "remote", "remove", "golden"): _fail_stdout_only(
                ("git", "remote", "remove", "golden")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._detect_and_migrate_legacy_remotes(
                    str(repo_dir), str(golden_repo_path)
                )

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)

    def test_rename_origin_to_golden_failure_surfaces_stdout(self, manager, tmp_path):
        repo_dir = tmp_path / "repo"
        golden_repo_path = tmp_path / "golden"

        overrides = {
            (("git", "remote", "get-url", "origin"), str(repo_dir)): _ok(
                (), stdout="/legacy/local/path\n"
            ),
            (("git", "remote", "get-url", "origin"), str(golden_repo_path)): _ok(
                (), stdout="https://example.com/repo.git\n"
            ),
            (("git", "remote", "get-url", "golden"), str(repo_dir)): _fail_with_stderr(
                (), stderr="no such remote"
            ),
            ("git", "remote", "rename", "origin", "golden"): _fail_stdout_only(
                ("git", "remote", "rename", "origin", "golden")
            ),
        }

        with patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)):
            with pytest.raises(ActivatedRepoError) as exc_info:
                manager._detect_and_migrate_legacy_remotes(
                    str(repo_dir), str(golden_repo_path)
                )

        assert _DISCRIMINATING_STDOUT in str(exc_info.value)


class TestSwitchToRemoteTrackingBranchDiagnostics:
    """_switch_to_remote_tracking_branch: site ~4548."""

    def test_checkout_failure_surfaces_stdout(self, manager, tmp_path, caplog):
        repo_dir = tmp_path / "repo"
        branch = "feature"

        overrides = {
            ("git", "checkout", "-B", branch, f"origin/{branch}"): _fail_stdout_only(
                ("git", "checkout", "-B", branch, f"origin/{branch}")
            ),
        }

        with (
            patch(f"{_MODULE}.subprocess.run", side_effect=_make_run(overrides)),
            caplog.at_level(logging.DEBUG),
        ):
            result = manager._switch_to_remote_tracking_branch(
                str(repo_dir), branch, "alias"
            )

        assert result is False
        debugs = [
            r.message
            for r in caplog.records
            if "Failed to switch to remote tracking branch" in r.message
        ]
        assert debugs, f"expected a debug log, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in debugs[0]
