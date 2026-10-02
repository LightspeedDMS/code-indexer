"""
Pluggable git *invoker* — EVO-64616 Phase 2 (the seam the ticket names at
``run_git_command``).

Unlike ``git_backend_plugin`` (EVO-64678), which seams only cidx's INTERNAL git
(golden/activated lifecycle, refresh, sync), this seams the ONE universal
chokepoint every git call bottoms out at: ``utils.git_runner.run_git_command``.
Because ``GitOperationsService`` (which backs every user-facing git-mutation
tool: commit/push/branch/stage/reset/stash/amend/merge/PR) reaches
``run_git_command`` DIRECTLY — never via ``GitBackend`` — a seam here is the only
one through which *everything* flows. A deployment that must not run git
mutations in the cidx image supplies a ``GitInvoker`` here; with nothing
configured the resolver returns None and native ``subprocess`` runs (today).

These tests pin:
  * the mutation-vs-read policy (``is_mutating_git``) — reads stay in-place,
    ambiguous/dual-form subcommands default to delegate (the safe, security-
    aligned default: cidx must never run a mutation locally);
  * no plugin -> factory is None (native default unchanged);
  * an env-var plugin -> resolved and instantiated;
  * the Python 3.9 real ``entry_points()`` signature (zero params) works; and
  * misconfiguration fails loud (``GitInvokerPluginError``) rather than silently
    returning None.
"""

from __future__ import annotations

import pytest

from code_indexer.utils import git_invoker
from code_indexer.utils.git_invoker import (
    ENV_VAR,
    GitInvokerPluginError,
    get_git_invoker_factory,
    is_mutating_git,
    resolve_git_invoker,
)


def _fake_entry_points_no_plugin(group=None):
    if group is None:
        return {}
    return []


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(git_invoker, "entry_points", _fake_entry_points_no_plugin)
    git_invoker.reset_cache()
    yield
    git_invoker.reset_cache()


class _StubInvoker:
    def __init__(self):
        self.made = True


def make_stub_invoker():
    return _StubInvoker()


not_callable = "nope"

_THIS = "tests.unit.utils.test_git_invoker"


# --------------------------------------------------------------------------- #
# is_mutating_git policy — the centralized read/write split
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "commit", "-m", "x"],
        ["git", "push", "origin", "HEAD"],
        ["git", "fetch", "origin"],
        ["git", "pull"],
        ["git", "merge", "feature"],
        ["git", "rebase", "main"],
        ["git", "reset", "--hard"],
        ["git", "checkout", "-b", "new"],
        ["git", "switch", "main"],
        ["git", "restore", "file.py"],
        ["git", "clean", "-fd"],
        ["git", "add", "-A"],
        ["git", "rm", "file.py"],
        ["git", "stash", "push"],
        ["git", "cherry-pick", "abc"],
        ["git", "revert", "abc"],
        ["git", "apply", "p.patch"],
        ["git", "clone", "url", "dest"],
        ["git", "init"],
        ["git", "-C", "/repo", "commit", "-m", "x"],  # global -C/-c flags skipped
        ["git", "branch", "-d", "feature"],  # dual-form: delete = mutation
        ["git", "branch", "-D", "feature"],
        ["git", "branch", "new-feature"],  # bare positional = create = mutation
        ["git", "tag", "v1"],  # tag create = mutation
        ["git", "tag", "-a", "v1", "-m", "msg"],
        ["git", "remote", "add", "origin", "url"],
        ["git", "remote", "set-url", "origin", "url2"],
        ["git", "config", "user.name", "x"],  # config write = mutation
        ["git", "stash"],  # bare stash = push = mutation
        ["git", "notes", "add", "-m", "n"],
        ["git", "symbolic-ref", "HEAD", "refs/heads/x"],  # set = mutation
        ["git", "update-ref", "refs/heads/x", "abc"],
    ],
)
def test_mutating_commands_are_delegated(cmd):
    assert is_mutating_git(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "status", "--porcelain"],
        ["git", "diff", "--name-only"],
        ["git", "log", "-1"],
        ["git", "show", "HEAD"],
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-list", "--count", "HEAD"],
        ["git", "ls-files"],
        ["git", "ls-remote", "url"],
        ["git", "ls-tree", "HEAD"],
        ["git", "cat-file", "-p", "HEAD"],
        ["git", "for-each-ref"],
        ["git", "merge-base", "a", "b"],
        ["git", "branch", "--list"],  # dual-form: list = read
        ["git", "branch"],  # bare branch = list = read
        ["git", "branch", "--list", "feat*"],  # list with pattern positional = read
        ["git", "tag"],  # bare tag = list = read
        ["git", "tag", "-l"],
        ["git", "remote", "-v"],  # remote list = read
        ["git", "remote"],  # bare remote = list = read
        ["git", "remote", "get-url", "origin"],
        ["git", "config", "--get", "remote.origin.url"],  # config read
        ["git", "config", "user.name"],  # single positional = read
        ["git", "stash", "list"],  # stash list = read
        ["git", "symbolic-ref", "HEAD"],  # single positional = read (HEAD lookup)
        ["git", "-C", "/repo", "status"],  # global flag skipped, read
    ],
)
def test_read_commands_stay_local(cmd):
    assert is_mutating_git(cmd) is False


def test_unknown_subcommand_defaults_to_delegate():
    # Safe default: if we can't prove it's a pure read, treat it as a mutation
    # so cidx never runs an un-classified git operation locally.
    assert is_mutating_git(["git", "some-future-plumbing", "--do-it"]) is True


def test_empty_or_nonsense_is_not_mutating():
    # No git verb at all -> nothing to delegate; runner will reject it anyway.
    assert is_mutating_git(["git"]) is False
    assert is_mutating_git([]) is False


# --------------------------------------------------------------------------- #
# factory resolution
# --------------------------------------------------------------------------- #
def test_no_plugin_returns_none():
    assert get_git_invoker_factory() is None
    assert resolve_git_invoker() is None


def test_env_var_plugin_is_resolved_and_instantiated(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub_invoker")
    factory = get_git_invoker_factory()
    assert factory is not None
    invoker = resolve_git_invoker()
    # Duck-typed, not isinstance: loading via the env spec re-imports this test
    # module under its dotted name, so the factory's _StubInvoker is a distinct
    # class object from this frame's. Identity by name + attribute is enough.
    assert type(invoker).__name__ == "_StubInvoker"
    assert getattr(invoker, "made", False) is True


def test_env_spec_without_colon_fails_loud(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "no_colon_here")
    with pytest.raises(GitInvokerPluginError):
        get_git_invoker_factory()


def test_env_spec_missing_attribute_fails_loud(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{_THIS}:does_not_exist")
    with pytest.raises(GitInvokerPluginError):
        get_git_invoker_factory()


def test_env_spec_not_callable_fails_loud(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{_THIS}:not_callable")
    with pytest.raises(GitInvokerPluginError):
        get_git_invoker_factory()


def test_result_is_cached(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub_invoker")
    first = get_git_invoker_factory()
    # Remove the env var; a cached resolution must NOT re-resolve to None.
    monkeypatch.delenv(ENV_VAR, raising=False)
    second = get_git_invoker_factory()
    assert first is second is not None
    git_invoker.reset_cache()
    assert get_git_invoker_factory() is None


class TestRemoteSubverbFailsClosed:
    """`git remote <unknown>` must classify as a MUTATION, like its siblings.

    `_classify_remote` asked whether the sub-verb was in a WRITE set, so any
    sub-verb it did not recognise -- a newly added git sub-command, or a
    sub-verb supplied at runtime -- classified as a read and ran LOCALLY, with
    the credentials the seam exists to keep in git-mcp. `branch`, `tag` and
    `stash` all default the other way: unknown positional means write.

    The guard that enforces the seam statically shares this classifier, so a
    fail-open here is a fail-open there too.
    """

    def test_an_unrecognised_subverb_is_a_mutation(self) -> None:
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "remote", "<dynamic>", "https://host/r.git"],
            ["git", "remote", "set-url-ish", "origin"],
            ["git", "remote", "some-future-subcommand"],
        ):
            assert is_mutating_git(argv), f"fail-open on {argv}"

    def test_known_reads_stay_reads(self) -> None:
        """The fix must not delegate every `remote` call -- a read needs no PAT
        and delegating it would cost a round trip per call."""
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "remote"],
            ["git", "remote", "-v"],
            ["git", "remote", "show", "origin"],
            ["git", "remote", "get-url", "origin"],
        ):
            assert not is_mutating_git(argv), f"read wrongly delegated: {argv}"

    def test_known_writes_stay_writes(self) -> None:
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "remote", "add", "origin", "https://host/r.git"],
            ["git", "remote", "set-url", "origin", "https://host/r.git"],
            ["git", "remote", "remove", "origin"],
        ):
            assert is_mutating_git(argv), f"write misclassified: {argv}"


class TestEqualsJoinedFlagsAreNotInvisible:
    """`--flag=value` and `--flag value` are the same flag to git.

    The classifiers test membership of each raw token in a flag set, so an
    `=`-joined long option never matches: `git branch --set-upstream-to=origin/x`
    classified as a READ and ran locally, while the space-separated form
    correctly classified as a write. A caller choosing the other spelling
    silently bypassed the seam.
    """

    def test_equals_joined_write_flags_classify_as_writes(self) -> None:
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "branch", "--set-upstream-to=origin/x"],
            ["git", "config", "--replace-all=core.hooksPath", "/tmp"],
            ["git", "branch", "--force=x"],
        ):
            spaced = argv[:2] + argv[2].split("=", 1) + argv[3:]
            assert is_mutating_git(argv) == is_mutating_git(spaced), (
                f"{' '.join(argv)} and {' '.join(spaced)} must classify alike"
            )
            assert is_mutating_git(argv), f"fail-open on {argv}"

    def test_equals_joined_read_flags_stay_reads(self) -> None:
        """The normalisation must not turn reads into writes and cost a round
        trip on every call."""
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "config", "--get=core.editor"],
            ["git", "branch", "--contains=HEAD"],
            ["git", "log", "--max-count=5"],
        ):
            assert not is_mutating_git(argv), f"read wrongly delegated: {argv}"


class TestHashObjectWritesOnlyWithW:
    """`git hash-object` computes an object id; it only WRITES with `-w`.

    Classifying the read form as a mutation is a fail-CLOSED error, so it costs
    correctness nothing -- but it sends a hot per-file batch call
    (`--stdin-paths` over every changed file in a repo) through git-mcp for no
    reason, and it makes the seam guard report a legitimate read as a bypass,
    which is how exemptions get written for code that never needed one.
    """

    def test_without_w_it_is_a_read(self) -> None:
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "hash-object", "--stdin-paths"],
            ["git", "hash-object", "somefile"],
            ["git", "hash-object", "--stdin"],
        ):
            assert not is_mutating_git(argv), f"read misclassified: {argv}"

    def test_with_w_it_is_a_write(self) -> None:
        from code_indexer.utils.git_invoker import is_mutating_git

        for argv in (
            ["git", "hash-object", "-w", "somefile"],
            ["git", "hash-object", "-w", "--stdin-paths"],
            ["git", "hash-object", "--stdin-paths", "-w"],
        ):
            assert is_mutating_git(argv), f"write misclassified: {argv}"
