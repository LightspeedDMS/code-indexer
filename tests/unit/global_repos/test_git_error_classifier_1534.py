"""
Unit tests for Bug #1534: classify git fetch failures caused by a broken
"origin" remote as "permanent", not "transient".

Git emits "fatal: '<X>' does not appear to be a git repository" in (at
least) two structurally different situations, confirmed via real
`git fetch` subprocess repros (second Codex review round):

  Form 1 -- no "origin" remote configured at all. The quoted string is
  literally the remote NAME:
      fatal: 'origin' does not appear to be a git repository

  Form 2 -- an "origin" remote IS configured, but its URL resolves to a
  path that is not (or is no longer) a valid git repository. The quoted
  string is the RESOLVED PATH, not the remote name:
      fatal: '/some/stale/path' does not appear to be a git repository

Both forms are structurally permanent: the local git object database is
fine, but the configured remote itself does not point at a git
repository. No amount of retrying resolves either form -- the operator
must fix the remote configuration (or, for a golden repo whose origin
points at its own resolved_source_path per the Bug #1534 fix,
restore/relocate the source).

Both forms were empirically verified, via real subprocess repros run
against actual git binaries, to NEVER co-occur with genuinely transient
wording ("Could not resolve host", "Connection refused", "unable to
access ..."): git only emits "does not appear to be a git repository" for
a local resolve-time failure, a structurally different code path than a
network transport error. This is directly demonstrated below by
`test_transient_network_failure_is_not_misclassified_permanent`, which
runs a real `git fetch` against an unreachable local port and confirms
the resulting stderr never contains that phrase.
"""

import shutil
import subprocess

from code_indexer.global_repos.git_error_classifier import classify_fetch_error

# Subprocess timeout for every real `git` invocation in this module. All
# repros here are local-only (no real network I/O succeeds), so they
# complete in well under a second; this bound only guards against an
# unexpected hang.
_GIT_SUBPROCESS_TIMEOUT_SECONDS = 15

# Loopback address + reserved/unassigned TCP port used to deterministically
# trigger a real "Connection refused" from git, without depending on any
# external network or DNS resolution being available in the test
# environment. Port 1 (TCP port service multiplexer) is never bound by an
# HTTP git server in practice, so nothing is expected to be listening.
_UNREACHABLE_LOOPBACK_GIT_URL = "http://127.0.0.1:1/nonexistent-repo.git"


def _init_real_git_repo(path):
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )


def _real_git_fetch_stderr(repo_path, remote_name="origin"):
    """
    Run a real `git fetch <remote_name>` in repo_path and return its
    stderr. Asserts the fetch actually failed (non-zero exit) so a test
    built on top of this helper cannot pass due to unrelated stderr
    output from a fetch that unexpectedly succeeded.
    """
    result = subprocess.run(
        ["git", "fetch", remote_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode != 0, (
        f"expected 'git fetch {remote_name}' to fail, but it succeeded; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stderr


def test_does_not_appear_to_be_a_git_repository_is_permanent(tmp_path):
    """
    Form 1: no "origin" remote configured at all. Real git emits
    "fatal: 'origin' does not appear to be a git repository". A missing
    origin remote is a structurally permanent condition -- no amount of
    retrying will fix it.
    """
    repo = tmp_path / "repo_no_origin"
    _init_real_git_repo(repo)

    stderr = _real_git_fetch_stderr(repo)
    assert "'origin' does not appear to be a git repository" in stderr

    assert classify_fetch_error(stderr) == "permanent"


def test_origin_pointing_at_deleted_path_is_permanent(tmp_path):
    """
    Form 2 (the actual reported gap): an "origin" remote IS configured,
    but its URL points at a path that no longer exists / is not a valid
    git repository. Uses a REAL `git fetch` subprocess (not a hand-typed
    string) to capture git's actual wording, which quotes the RESOLVED
    PATH rather than the literal string "origin". A prior fix required
    the exact "'origin'" quoting and never matched this real-world case,
    so it fell through to the broader transient "Could not read from
    remote" pattern and was endlessly retried.
    """
    repo = tmp_path / "repo_stale_origin"
    gone_target = tmp_path / "gone_target"
    _init_real_git_repo(gone_target)
    _init_real_git_repo(repo)

    subprocess.run(
        ["git", "remote", "add", "origin", str(gone_target)],
        cwd=str(repo),
        check=True,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )

    # Delete the target AFTER configuring the remote, so origin points at
    # a path that no longer resolves to a valid git repository.
    shutil.rmtree(gone_target)

    stderr = _real_git_fetch_stderr(repo)

    # Sanity-check the real repro actually produced the expected phrase
    # and that it is NOT the literal "'origin'"-quoted form.
    assert "does not appear to be a git repository" in stderr
    assert "'origin' does not appear to be a git repository" not in stderr

    assert classify_fetch_error(stderr) == "permanent"


def test_broken_remote_with_a_different_name_is_also_permanent(tmp_path):
    """
    The "does not appear to be a git repository" failure class is not
    specific to the "origin" remote name -- any remote name pointing at a
    non-repository path produces the identical structural failure, and it
    is equally permanent regardless of the remote's name. A prior,
    over-narrow fix required the literal "'origin'" quoting, which would
    have misclassified this case as "transient" via the broader "Could
    not read from remote" pattern. Real git output for a
    differently-named broken remote (captured via a real subprocess) is
    used here to prove the widened PERMANENT_PATTERNS entry ("does not
    appear to be a git repository", without requiring the "origin"
    quoting) is the correct fix -- this collision was never observed in
    practice (see the transient test below).
    """
    repo = tmp_path / "repo_other_remote"
    gone_target = tmp_path / "gone_target_other"
    _init_real_git_repo(gone_target)
    _init_real_git_repo(repo)

    subprocess.run(
        ["git", "remote", "add", "upstream", str(gone_target)],
        cwd=str(repo),
        check=True,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )
    shutil.rmtree(gone_target)

    stderr = _real_git_fetch_stderr(repo, remote_name="upstream")
    assert "does not appear to be a git repository" in stderr

    assert classify_fetch_error(stderr) == "permanent"


def test_transient_network_failure_is_not_misclassified_permanent(tmp_path):
    """
    A genuine transient network failure (connection refused against an
    unreachable local port -- deterministic, no external network/DNS
    dependency) must classify as "transient", and its real stderr must
    NEVER contain the "does not appear to be a git repository" phrase.
    This is the empirical confirmation that widening PERMANENT_PATTERNS
    to the bare phrase does not swallow real transient failures.
    """
    repo = tmp_path / "repo_transient"
    _init_real_git_repo(repo)

    subprocess.run(
        ["git", "remote", "add", "origin", _UNREACHABLE_LOOPBACK_GIT_URL],
        cwd=str(repo),
        check=True,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )

    stderr = _real_git_fetch_stderr(repo)
    assert "does not appear to be a git repository" not in stderr

    assert classify_fetch_error(stderr) == "transient"
