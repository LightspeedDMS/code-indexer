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
