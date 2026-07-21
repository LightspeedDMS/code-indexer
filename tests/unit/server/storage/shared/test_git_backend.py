"""Unit tests for the GitBackend seam (EVO-64678, EVO-64616 Phase 2).

Verifies that LocalGitBackend emits the exact ``git`` command lines the raw call
sites use today — so refactoring them onto the seam is behavior-preserving — and
that the factory selects backends correctly. The ``git_runner`` helpers are
mocked, so no real git runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.shared.git_backend import (
    GitBackend,
    GitBackendFactory,
    LocalGitBackend,
)

MOD = "code_indexer.server.storage.shared.git_backend"


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


@pytest.fixture()
def backend() -> LocalGitBackend:
    return LocalGitBackend()


# --------------------------------------------------------------------------- #
# LocalGitBackend command-line fidelity
# --------------------------------------------------------------------------- #


def test_local_backend_satisfies_protocol(backend):
    assert isinstance(backend, GitBackend)


def test_clone_default(backend):
    with patch(f"{MOD}.run_git_command_with_retry") as m:
        backend.clone("https://x/y.git", "/tmp/repos/y")
        m.assert_called_once_with(
            ["git", "clone", "https://x/y.git", "/tmp/repos/y"], cwd=Path("/tmp/repos")
        )


def test_clone_branch_and_no_fsync(backend):
    with patch(f"{MOD}.run_git_command_with_retry") as m:
        backend.clone("https://x/y.git", "/tmp/repos/y", branch="dev", no_fsync=True)
        m.assert_called_once_with(
            [
                "git",
                "-c",
                "core.fsync=none",
                "clone",
                "--branch",
                "dev",
                "https://x/y.git",
                "/tmp/repos/y",
            ],
            cwd=Path("/tmp/repos"),
        )


def test_ls_remote_success_and_failure(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp()) as m:
        assert backend.ls_remote("https://x/y.git") is True
        assert m.call_args.args[0] == ["git", "ls-remote", "https://x/y.git"]
    with patch(
        f"{MOD}.run_git_command", side_effect=subprocess.CalledProcessError(128, "git")
    ):
        assert backend.ls_remote("https://x/bad.git") is False


def test_fetch_named_remote(backend):
    """cidx fetches from a 'golden' remote, not just origin."""
    with patch(f"{MOD}.run_git_command_with_retry") as m:
        backend.fetch("/r", remote="golden")
        m.assert_called_once_with(["git", "fetch", "golden"], cwd=Path("/r"))


@pytest.mark.parametrize(
    "mode,flag",
    [
        ("default", None),
        ("ff-only", "--ff-only"),
        ("no-ff", "--no-ff"),
        ("rebase", "--rebase"),
    ],
)
def test_pull_modes(backend, mode, flag):
    with patch(f"{MOD}.run_git_command_with_retry") as m:
        backend.pull("/r", remote="origin", branch="main", mode=mode)
        expected = ["git", "pull"] + ([flag] if flag else []) + ["origin", "main"]
        m.assert_called_once_with(expected, cwd=Path("/r"))


def test_merge(backend):
    with patch(f"{MOD}.run_git_command") as m:
        backend.merge("/r", "golden/main")
        m.assert_called_once_with(["git", "merge", "golden/main"], cwd=Path("/r"))


def test_checkout_variants(backend):
    with patch(f"{MOD}.run_git_command") as m:
        backend.checkout("/r", "main")
        assert m.call_args.args[0] == ["git", "checkout", "main"]
        backend.checkout("/r", "feat", create=True)
        assert m.call_args.args[0] == ["git", "checkout", "-b", "feat"]
        backend.checkout("/r", "feat", force_new=True, start_point="origin/feat")
        assert m.call_args.args[0] == ["git", "checkout", "-B", "feat", "origin/feat"]
        backend.checkout("/r", "HEAD", force=True)
        assert m.call_args.args[0] == ["git", "checkout", "-f", "HEAD"]


def test_current_branch(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp("main\n")) as m:
        assert backend.current_branch("/r") == "main"
        assert m.call_args.args[0] == ["git", "symbolic-ref", "--short", "HEAD"]


def test_list_branches(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp("  a\n  b\n")):
        assert backend.list_branches("/r", remote=True) == ["a", "b"]
    with patch(f"{MOD}.run_git_command", return_value=_cp("")) as m:
        backend.list_branches("/r", all_refs=True, fmt="%(refname)")
        assert m.call_args.args[0] == ["git", "branch", "-a", "--format=%(refname)"]


def test_rev_parse_and_rev_list_count(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp("abc123\n")) as m:
        assert backend.rev_parse("/r", "--abbrev-ref", "HEAD") == "abc123"
        assert m.call_args.args[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    with patch(f"{MOD}.run_git_command", return_value=_cp("5\n")) as m:
        assert backend.rev_list_count("/r", "HEAD..@{u}") == 5
        assert m.call_args.args[0] == ["git", "rev-list", "--count", "HEAD..@{u}"]


def test_diff_name_only_range(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp("a.py\nb.py\n")) as m:
        assert backend.diff_name_only("/r", "HEAD..golden/main") == ["a.py", "b.py"]
        assert m.call_args.args[0] == [
            "git",
            "diff",
            "--name-only",
            "HEAD..golden/main",
        ]


def test_status_porcelain_and_reset_restore(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp(" M x\n")):
        assert backend.status_porcelain("/r") == " M x\n"
    with patch(f"{MOD}.run_git_command") as m:
        backend.reset("/r", "hard", "origin/main")
        assert m.call_args.args[0] == ["git", "reset", "--hard", "origin/main"]
        backend.restore("/r")
        assert m.call_args.args[0] == ["git", "restore", "."]


def test_update_index_and_show_ref_use_check_false(backend):
    """Both tolerate non-zero exits today — the seam must not start raising."""
    with patch(f"{MOD}.run_git_command", return_value=_cp()) as m:
        backend.update_index_refresh("/r")
        assert m.call_args.kwargs["check"] is False
        backend.show_ref("/r", "--verify", "refs/heads/main")
        assert m.call_args.args[0] == ["git", "show-ref", "--verify", "refs/heads/main"]
        assert m.call_args.kwargs["check"] is False


def test_ls_files(backend):
    with patch(f"{MOD}.run_git_command", return_value=_cp("a.py\nb.py\n")) as m:
        assert backend.ls_files("/r") == ["a.py", "b.py"]
        assert m.call_args.args[0] == ["git", "ls-files"]


def test_config_set(backend):
    with patch(f"{MOD}.run_git_command") as m:
        backend.config_set("/r", "user.email", "cidx@example.com")
        m.assert_called_once_with(
            ["git", "config", "user.email", "cidx@example.com"], cwd=Path("/r")
        )


def test_remote_choreography(backend):
    """activated_repo_manager renames origin->golden, re-adds origin, etc."""
    with patch(f"{MOD}.run_git_command", return_value=_cp("https://x/y.git\n")) as m:
        assert backend.remote_get_url("/r", "origin") == "https://x/y.git"
        assert m.call_args.args[0] == ["git", "remote", "get-url", "origin"]
        backend.remote_rename("/r", "origin", "golden")
        assert m.call_args.args[0] == ["git", "remote", "rename", "origin", "golden"]
        backend.remote_add("/r", "origin", "https://x/z.git")
        assert m.call_args.args[0] == [
            "git",
            "remote",
            "add",
            "origin",
            "https://x/z.git",
        ]
        backend.remote_set_url("/r", "origin", "https://x/z2.git")
        assert m.call_args.args[0] == [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://x/z2.git",
        ]
        backend.remote_remove("/r", "golden")
        assert m.call_args.args[0] == ["git", "remote", "remove", "golden"]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_factory_local_is_default():
    assert isinstance(GitBackendFactory.create(), LocalGitBackend)
    assert isinstance(GitBackendFactory.create("local"), LocalGitBackend)


def test_factory_delegated_requires_injected_backend():
    """Never silently no-op: 'delegated' without a plugin backend is a hard error."""
    with pytest.raises(RuntimeError, match="no delegated backend"):
        GitBackendFactory.create("delegated")
    injected = MagicMock(spec=GitBackend)
    assert GitBackendFactory.create("delegated", delegated_backend=injected) is injected


def test_factory_unknown_raises():
    with pytest.raises(ValueError, match="Unknown git_backend"):
        GitBackendFactory.create("bogus")
