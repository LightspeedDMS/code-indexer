"""Unit tests for the GitBackend seam (EVO-64678, EVO-64616 Phase 2).

Verifies that LocalGitBackend emits the exact ``git`` command lines the raw call
sites use today — so refactoring them onto the seam is behavior-preserving — and
that the config-driven timeouts those sites pass are carried through rather than
silently dropped. The ``git_runner`` helpers are mocked, so no real git runs.
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
PLAIN = f"{MOD}.run_git_command"
RETRY = f"{MOD}.run_git_command_with_retry"


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


def _cmd(mock) -> list:
    """The git argv the backend built."""
    return list(mock.call_args.args[0])


def _kw(mock) -> dict:
    return mock.call_args.kwargs


@pytest.fixture()
def backend() -> LocalGitBackend:
    return LocalGitBackend()


# --------------------------------------------------------------------------- #
# Command-line fidelity
# --------------------------------------------------------------------------- #


def test_local_backend_satisfies_protocol(backend):
    assert isinstance(backend, GitBackend)


def test_clone_default(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        backend.clone("https://x/y.git", "/tmp/repos/y")
        assert _cmd(m) == ["git", "clone", "https://x/y.git", "/tmp/repos/y"]
        # clone target does not exist yet -> run from its parent
        assert _kw(m)["cwd"] == Path("/tmp/repos")


def test_clone_branch_and_no_fsync(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        backend.clone("https://x/y.git", "/tmp/repos/y", branch="dev", no_fsync=True)
        assert _cmd(m) == [
            "git",
            "-c",
            "core.fsync=none",
            "clone",
            "--branch",
            "dev",
            "https://x/y.git",
            "/tmp/repos/y",
        ]


def test_ls_remote_success_and_failure(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        assert backend.ls_remote("https://x/y.git") is True
        assert _cmd(m) == ["git", "ls-remote", "https://x/y.git"]
    with patch(PLAIN, side_effect=subprocess.CalledProcessError(128, "git")):
        assert backend.ls_remote("https://x/bad.git") is False


def test_fetch_named_remote(backend):
    """cidx fetches from a 'golden' remote, not just origin."""
    with patch(PLAIN, return_value=_cp()) as m:
        backend.fetch("/r", remote="golden")
        assert _cmd(m) == ["git", "fetch", "golden"]
        assert _kw(m)["cwd"] == Path("/r")


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
    with patch(PLAIN, return_value=_cp()) as m:
        backend.pull("/r", remote="origin", branch="main", mode=mode)
        assert _cmd(m) == ["git", "pull"] + ([flag] if flag else []) + [
            "origin",
            "main",
        ]


def test_merge(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        backend.merge("/r", "golden/main")
        assert _cmd(m) == ["git", "merge", "golden/main"]


def test_checkout_variants(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        backend.checkout("/r", "main")
        assert _cmd(m) == ["git", "checkout", "main"]
        backend.checkout("/r", "feat", create=True)
        assert _cmd(m) == ["git", "checkout", "-b", "feat"]
        backend.checkout("/r", "feat", force_new=True, start_point="origin/feat")
        assert _cmd(m) == ["git", "checkout", "-B", "feat", "origin/feat"]
        backend.checkout("/r", "HEAD", force=True)
        assert _cmd(m) == ["git", "checkout", "-f", "HEAD"]


def test_current_branch(backend):
    with patch(PLAIN, return_value=_cp("main\n")) as m:
        assert backend.current_branch("/r") == "main"
        assert _cmd(m) == ["git", "symbolic-ref", "--short", "HEAD"]


def test_list_branches(backend):
    with patch(PLAIN, return_value=_cp("  a\n  b\n")):
        assert backend.list_branches("/r", remote=True) == ["a", "b"]
    with patch(PLAIN, return_value=_cp("")) as m:
        backend.list_branches("/r", all_refs=True, fmt="%(refname)")
        assert _cmd(m) == ["git", "branch", "-a", "--format=%(refname)"]


def test_rev_parse_and_rev_list_count(backend):
    with patch(PLAIN, return_value=_cp("abc123\n")) as m:
        assert backend.rev_parse("/r", "--abbrev-ref", "HEAD") == "abc123"
        assert _cmd(m) == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    with patch(PLAIN, return_value=_cp("5\n")) as m:
        assert backend.rev_list_count("/r", "HEAD..@{u}") == 5
        assert _cmd(m) == ["git", "rev-list", "--count", "HEAD..@{u}"]


def test_diff_name_only_range(backend):
    with patch(PLAIN, return_value=_cp("a.py\nb.py\n")) as m:
        assert backend.diff_name_only("/r", "HEAD..golden/main") == ["a.py", "b.py"]
        assert _cmd(m) == ["git", "diff", "--name-only", "HEAD..golden/main"]


def test_status_porcelain_and_reset_restore(backend):
    with patch(PLAIN, return_value=_cp(" M x\n")):
        assert backend.status_porcelain("/r") == " M x\n"
    with patch(PLAIN, return_value=_cp()) as m:
        backend.reset("/r", "hard", "origin/main")
        assert _cmd(m) == ["git", "reset", "--hard", "origin/main"]
        backend.restore("/r")
        assert _cmd(m) == ["git", "restore", "."]


def test_ls_files(backend):
    with patch(PLAIN, return_value=_cp("a.py\nb.py\n")) as m:
        assert backend.ls_files("/r") == ["a.py", "b.py"]
        assert _cmd(m) == ["git", "ls-files"]


def test_config_set(backend):
    with patch(PLAIN, return_value=_cp()) as m:
        backend.config_set("/r", "user.email", "cidx@example.com")
        assert _cmd(m) == ["git", "config", "user.email", "cidx@example.com"]


def test_remote_choreography(backend):
    """activated_repo_manager renames origin->golden, re-adds origin, etc."""
    with patch(PLAIN, return_value=_cp("https://x/y.git\n")) as m:
        assert backend.remote_get_url("/r", "origin") == "https://x/y.git"
        assert _cmd(m) == ["git", "remote", "get-url", "origin"]
        backend.remote_rename("/r", "origin", "golden")
        assert _cmd(m) == ["git", "remote", "rename", "origin", "golden"]
        backend.remote_add("/r", "origin", "https://x/z.git")
        assert _cmd(m) == ["git", "remote", "add", "origin", "https://x/z.git"]
        backend.remote_set_url("/r", "origin", "https://x/z2.git")
        assert _cmd(m) == ["git", "remote", "set-url", "origin", "https://x/z2.git"]
        backend.remote_remove("/r", "golden")
        assert _cmd(m) == ["git", "remote", "remove", "golden"]
        backend.remote_list("/r", verbose=True)
        assert _cmd(m) == ["git", "remote", "-v"]


# --------------------------------------------------------------------------- #
# check=False is part of the contract for the tolerant operations
# --------------------------------------------------------------------------- #


def test_update_index_and_show_ref_tolerate_nonzero_exit(backend):
    """Both exit non-zero routinely today — the seam must not start raising."""
    with patch(PLAIN, return_value=_cp()) as m:
        backend.update_index_refresh("/r")
        assert _kw(m)["check"] is False
        backend.show_ref("/r", "--verify", "refs/heads/main")
        assert _cmd(m) == ["git", "show-ref", "--verify", "refs/heads/main"]
        assert _kw(m)["check"] is False


def test_other_operations_check_by_default(backend):
    """Everything else runs check=True so failures surface as CalledProcessError."""
    with patch(PLAIN, return_value=_cp()) as m:
        backend.merge("/r", "golden/main")
        assert _kw(m)["check"] is True


# --------------------------------------------------------------------------- #
# Timeouts — every real call site passes a config-driven timeout
# --------------------------------------------------------------------------- #


def test_timeout_is_passed_through_not_dropped(backend):
    """Call sites pass git_clone_timeout / git_pull_timeout etc; don't lose them."""
    with patch(PLAIN, return_value=_cp()) as m:
        backend.clone("https://x/y.git", "/tmp/repos/y", timeout=1800)
        assert _kw(m)["timeout"] == 1800
        backend.fetch("/r", timeout=300)
        assert _kw(m)["timeout"] == 300
        backend.pull("/r", timeout=120)
        assert _kw(m)["timeout"] == 120
    with patch(PLAIN, return_value=_cp("x\n")) as m:
        backend.checkout("/r", "main", timeout=60)
        assert _kw(m)["timeout"] == 60
        backend.status_porcelain("/r", timeout=30)
        assert _kw(m)["timeout"] == 30
        backend.rev_parse("/r", "HEAD", timeout=15)
        assert _kw(m)["timeout"] == 15


def test_timeout_defaults_to_none(backend):
    """None means 'no timeout', matching subprocess.run."""
    with patch(PLAIN, return_value=_cp()) as m:
        backend.merge("/r", "x")
        assert _kw(m)["timeout"] is None


def test_network_operations_do_not_retry_by_default(backend):
    """Today's call sites are single-shot subprocess.run — don't add a silent retry.

    A retried clone would hit "destination path already exists" and surface a
    different error than the one callers turn into GitOperationError.
    """
    with (
        patch(RETRY, return_value=_cp()) as retry,
        patch(PLAIN, return_value=_cp()) as plain,
    ):
        backend.clone("u", "/tmp/a/b")
        backend.fetch("/r")
        backend.pull("/r")
        assert retry.call_count == 0
        assert plain.call_count == 3


def test_retry_is_opt_in(backend):
    with patch(RETRY, return_value=_cp()) as retry, patch(PLAIN, return_value=_cp()):
        backend.clone("u", "/tmp/a/b", retry=True)
        backend.fetch("/r", retry=True)
        backend.pull("/r", retry=True)
        assert retry.call_count == 3


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


def test_local_backend_does_not_own_credentials(backend: LocalGitBackend):
    # Local git relies on the credential carried in the URL / ambient config, so
    # callers MUST keep any embedded token — golden-repo URLs are not stripped.
    assert backend.owns_credentials is False


# --------------------------------------------------------------------------- #
# History (temporal indexing and queries): raw bytes, the local argv
# --------------------------------------------------------------------------- #


def _cpb(stdout: bytes = b"", returncode: int = 0, stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


FMT = "%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e"


def test_log_raw_is_the_temporal_history_argv_as_bytes(backend):
    with patch(PLAIN, return_value=_cpb(b"\xff\x1e")) as m:
        out = backend.log_raw("/r", FMT, ["abc"], all_refs=True, since="2024-01-01")
    assert out == b"\xff\x1e"
    assert _cmd(m) == [
        "git",
        "log",
        f"--format={FMT}",
        "--all",
        "--since",
        "2024-01-01",
        "abc",
    ]
    assert _kw(m)["text"] is False and _kw(m)["cwd"] == Path("/r")


def test_log_raw_pages_with_skip_and_n(backend):
    with patch(PLAIN, return_value=_cpb()) as m:
        backend.log_raw("/r", "%ct", ["abc"], skip=3, max_count=4)
    assert _cmd(m) == ["git", "log", "--format=%ct", "--skip=3", "-n", "4", "abc"]


def test_log_raw_failure_raises_with_text_stderr(backend):
    with patch(PLAIN, return_value=_cpb(returncode=128, stderr=b"fatal: bad")):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            backend.log_raw("/r", "%H")
    assert exc.value.returncode == 128 and exc.value.stderr == "fatal: bad"


def test_diff_commits_runs_the_aggregator_argv_per_pair(backend):
    with patch(PLAIN, side_effect=[_cpb(b"d1"), _cpb(b"d2")]) as m:
        out = backend.diff_commits("/r", [("a", "b"), ("b", "c")], 5)
    assert out == [b"d1", b"d2"]
    assert [list(c.args[0]) for c in m.call_args_list] == [
        ["git", "diff", "-M", "-U5", "--full-index", "a", "b"],
        ["git", "diff", "-M", "-U5", "--full-index", "b", "c"],
    ]


def test_diff_commits_raises_on_a_failing_pair(backend):
    with patch(PLAIN, return_value=_cpb(returncode=128, stderr=b"bad object")):
        with pytest.raises(subprocess.CalledProcessError):
            backend.diff_commits("/r", [("a", "b")], 5)


def test_show_objects_is_check_false_per_spec(backend):
    with patch(
        PLAIN, side_effect=[_cpb(b"\x00x"), _cpb(returncode=128, stderr=b"no path")]
    ) as m:
        out = backend.show_objects("/r", [("a", "x.py"), ("b", "gone.py")])
    assert [list(c.args[0]) for c in m.call_args_list] == [
        ["git", "show", "a:x.py"],
        ["git", "show", "b:gone.py"],
    ]
    assert (out[0].returncode, out[0].stdout) == (0, b"\x00x")
    assert (out[1].returncode, out[1].stderr) == (128, "no path")
