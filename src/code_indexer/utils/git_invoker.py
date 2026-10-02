"""
Pluggable git *invoker*: the EVO-64616 Phase 2 delegation seam, placed at the
one universal chokepoint every git call bottoms out at.

Why here (and not the ``GitBackend`` seam)
-------------------------------------------
EVO-64678 added a ``GitBackend`` seam, but it only wraps cidx's INTERNAL git
(golden/activated-repo lifecycle, refresh scheduler, git-sync, pull-updates).
The user-facing git-mutation TOOLS — ``git_commit``/``push``/``branch_*``/
``stage``/``reset``/``stash``/``amend``/``merge`` and the PR ops, the exact
surface the ticket says belongs to git-mcp — funnel through
``GitOperationsService``, which calls ``utils.git_runner.run_git_command``
DIRECTLY and never touches ``GitBackend``. So the ``GitBackend`` seam is a
*parallel* path the tools bypass.

``run_git_command`` is the single point BOTH paths pass through (``LocalGitBackend``
also bottoms out there). Seaming here is therefore the only way to make
*everything* flow through one delegation point: with an invoker installed, both
the internal-git path and the user-tool path delegate, with zero call-site
changes. With none installed, native ``subprocess`` runs — today's behavior, so
the cidx product is unaffected and the seam is opt-in.

The contract
------------
A plugin is a zero-argument FACTORY callable::

    def make_git_invoker() -> GitInvoker:
        ...

returning any object satisfying the ``GitInvoker`` protocol (a single ``run``).
It takes no arguments: the git-mcp endpoint/token/timeout are read from the
environment by the invoker itself, so the factory needs nothing from cidx.

Selection (first match wins), mirroring ``git_backend_plugin``:
1. ``CIDX_GIT_INVOKER`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.git_invoker`` group.
3. Nothing -- the caller runs git natively via ``subprocess``.

Read/write policy
-----------------
The seam covers *every* git call, but delegation is scoped by ``is_mutating_git``:
reads (status/diff/log/rev-parse/ls-*/…) stay in-place (the ticket's "reads need
zero cidx work / serve in-place"); mutations delegate. The split lives here, in
ONE place, rather than being duplicated across the ~20 ``run_git_command`` call
sites. Ambiguous or unknown subcommands default to *delegate* — the security-
aligned default, since cidx must never run an unclassified mutation locally.

Misconfiguration fails LOUD (``GitInvokerPluginError``) rather than silently
returning None: a deployment that configured an invoker because it must NOT run
git mutations locally would otherwise fall back to exactly the in-process git it
was avoiding.
"""

from __future__ import annotations

import logging
import os
import subprocess
from importlib import import_module
from importlib.metadata import entry_points
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Protocol,
    Sequence,
    cast,
    runtime_checkable,
)

logger = logging.getLogger(__name__)

ENV_VAR = "CIDX_GIT_INVOKER"
ENTRY_POINT_GROUP = "code_indexer.git_invoker"


@runtime_checkable
class GitInvoker(Protocol):
    """Executes a git command somewhere other than the cidx process.

    ``run`` mirrors the slice of ``subprocess.run`` that cidx callers actually
    read back: it returns a ``CompletedProcess`` whose ``returncode``/``stdout``/
    ``stderr`` reflect the delegated execution. It must NOT raise on a non-zero
    git exit — the runner applies ``check`` semantics uniformly for both the
    native and delegated paths, so the invoker just reports the result.
    """

    def run(
        self,
        cmd: Sequence[str],
        cwd: Any,
        *,
        timeout: Optional[float] = None,
    ) -> "subprocess.CompletedProcess[str]": ...


# () -> GitInvoker
GitInvokerFactory = Callable[[], GitInvoker]


class GitInvokerPluginError(RuntimeError):
    """An invoker was configured but could not be loaded or is not callable."""


# --------------------------------------------------------------------------- #
# read/write policy
# --------------------------------------------------------------------------- #
# Global git flags that appear BEFORE the subcommand and carry an argument; both
# the flag and its value are skipped when locating the verb (e.g. ``git -C /r``,
# ``git -c k=v``, ``git --git-dir=…`` handled separately below).
_GLOBAL_FLAGS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

# Subcommands that only ever READ, in every form cidx uses. Anything not here
# (or a dual-form verb whose args prove a write) defaults to delegate.
_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "cat-file",
        "for-each-ref",
        "merge-base",
        "describe",
        "blame",
        "name-rev",
        "shortlog",
        "count-objects",
        "grep",
        "whatchanged",
        "show-ref",
        "var",
        "version",
    }
)

# Dual-form verbs are read in some argument shapes and write in others; each has
# a dedicated classifier below (``git branch`` lists but ``git branch foo``
# creates; ``git tag`` lists but ``git tag v1`` creates; ``git stash`` bare saves).
# Read-selecting flags that keep a dual-form verb on the local (read) path even
# when a positional (a name/pattern) is present.
_BRANCH_READ_FLAGS = {
    "-l",
    "--list",
    "-a",
    "--all",
    "-r",
    "--remotes",
    "--contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "-v",
    "-vv",
    "--show-current",
}
_BRANCH_WRITE_FLAGS = {
    "-d",
    "-D",
    "--delete",
    "-m",
    "-M",
    "--move",
    "-c",
    "-C",
    "--copy",
    "-f",
    "--force",
    "--set-upstream-to",
    "-u",
    "--unset-upstream",
    "--edit-description",
}
_TAG_READ_FLAGS = {
    "-l",
    "--list",
    "-n",
    "--contains",
    "--no-contains",
    "--points-at",
    "--merged",
    "--no-merged",
    "--sort",
    "--format",
}
_TAG_WRITE_FLAGS = {
    "-a",
    "-s",
    "-m",
    "-d",
    "--delete",
    "-f",
    "--force",
    "--create-reflog",
    "-F",
}
# Sub-verbs of `git remote` that only READ. Everything else is treated as a
# write, including a sub-verb we do not recognise.
#
# This used to be the inverse -- a set of WRITE sub-verbs, with anything
# unrecognised falling through to "read, run it locally". That is fail-open in
# the one direction that matters: an unrecognised sub-verb is either a future
# git sub-command or one supplied at runtime, and running it locally hands it
# the credentials this seam exists to confine to git-mcp. `branch`, `tag` and
# `stash` all default the other way already; `remote` was the odd one out.
_REMOTE_READ_SUBVERBS = {
    "show",
    "get-url",
}
_CONFIG_READ_FLAGS = {
    "--get",
    "--get-all",
    "--get-regexp",
    "--get-urlmatch",
    "--list",
    "-l",
}
_CONFIG_WRITE_FLAGS = {
    "--unset",
    "--unset-all",
    "--add",
    "--replace-all",
    "--rename-section",
    "--remove-section",
    "--edit",
    "-e",
}
_STASH_READ_SUBVERBS = {"list", "show"}
_NOTES_READ_SUBVERBS = {"list", "show"}


def _positionals(rest: List[str]) -> List[str]:
    return [t for t in rest if not t.startswith("-")]


def _classify_branch(rest: List[str]) -> bool:
    if any(t in _BRANCH_WRITE_FLAGS for t in rest):
        return True
    if any(t in _BRANCH_READ_FLAGS for t in rest):
        return False
    return len(_positionals(rest)) > 0  # ``branch foo`` creates; bare ``branch`` lists


def _classify_tag(rest: List[str]) -> bool:
    if any(t in _TAG_WRITE_FLAGS for t in rest):
        return True
    if any(t in _TAG_READ_FLAGS for t in rest):
        return False
    return len(_positionals(rest)) > 0  # ``tag v1`` creates; bare ``tag`` lists


def _classify_remote(rest: List[str]) -> bool:
    pos = _positionals(rest)
    if not pos:
        return False  # bare `git remote` lists configured remotes
    return pos[0] not in _REMOTE_READ_SUBVERBS


def _classify_config(rest: List[str]) -> bool:
    if any(t in _CONFIG_WRITE_FLAGS for t in rest):
        return True
    if any(t in _CONFIG_READ_FLAGS for t in rest):
        return False
    return (
        len(_positionals(rest)) >= 2
    )  # ``config key value`` writes; ``config key`` reads


def _classify_stash(rest: List[str]) -> bool:
    pos = _positionals(rest)
    if pos and pos[0] in _STASH_READ_SUBVERBS:
        return False
    return True  # bare ``stash`` and push/pop/apply/drop/clear all mutate


def _classify_notes(rest: List[str]) -> bool:
    pos = _positionals(rest)
    if not pos or pos[0] in _NOTES_READ_SUBVERBS:
        return False
    return True


def _classify_symbolic_ref(rest: List[str]) -> bool:
    if any(t in {"-d", "--delete"} for t in rest):
        return True
    return len(_positionals(rest)) >= 2  # ``symbolic-ref HEAD refs/..`` writes


_DUAL_FORM_CLASSIFIERS = {
    "branch": _classify_branch,
    "tag": _classify_tag,
    "remote": _classify_remote,
    "config": _classify_config,
    "stash": _classify_stash,
    "notes": _classify_notes,
    "symbolic-ref": _classify_symbolic_ref,
}


def _extract_subcommand(cmd: Sequence[str]) -> Optional[str]:
    """Return the git verb, skipping leading global flags. None if there is none."""
    if not cmd or cmd[0] != "git":
        return None
    i = 1
    n = len(cmd)
    while i < n:
        tok = cmd[i]
        if tok in _GLOBAL_FLAGS_WITH_ARG:
            i += 2  # skip flag and its argument
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1  # e.g. --git-dir=/x : self-contained global flag
            continue
        if tok.startswith("-"):
            i += 1  # other bare global flag (e.g. --no-pager, --paginate)
            continue
        return tok
    return None


def is_mutating_git(cmd: Sequence[str]) -> bool:
    """True if *cmd* may change repo/remote/working-tree state (=> delegate).

    Reads stay local (False). Dual-form verbs are classified by their args.
    Unknown verbs default to True — cidx must never run an unclassified git
    operation locally.
    """
    sub = _extract_subcommand(cmd)
    if sub is None:
        return False  # bare ``git`` / no verb: nothing to delegate

    classifier = _DUAL_FORM_CLASSIFIERS.get(sub)
    if classifier is not None:
        rest = list(cmd[cmd.index(sub) + 1 :])
        return classifier(rest)

    if sub in _READ_ONLY_SUBCOMMANDS:
        return False
    return True  # safe default: delegate


# --------------------------------------------------------------------------- #
# factory resolution (mirrors git_backend_plugin)
# --------------------------------------------------------------------------- #
_NO_PLUGIN = object()
_cached: object = None


def _load_from_path(spec: str) -> GitInvokerFactory:
    if ":" not in spec:
        raise GitInvokerPluginError(
            f"{ENV_VAR}={spec!r} is not in 'package.module:factory' form"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise GitInvokerPluginError(
            f"{ENV_VAR}={spec!r}: cannot import module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, attr, None)
    if factory is None:
        raise GitInvokerPluginError(
            f"{ENV_VAR}={spec!r}: module {module_name!r} has no attribute {attr!r}"
        )
    if not callable(factory):
        raise GitInvokerPluginError(f"{ENV_VAR}={spec!r}: {attr!r} is not callable")
    return cast(GitInvokerFactory, factory)


def _load_from_entry_points() -> Optional[GitInvokerFactory]:
    # entry_points() gained the `group=` keyword in 3.10; on 3.9 it takes no
    # params and returns a dict of group -> list. Support both (CI targets 3.9).
    import sys

    candidates: List[Any]
    if sys.version_info >= (3, 10):
        candidates = list(entry_points(group=ENTRY_POINT_GROUP))
    else:
        candidates = list(entry_points().get(ENTRY_POINT_GROUP, []))
    selected = next(iter(candidates), None)
    if selected is None:
        return None
    try:
        factory = selected.load()
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise GitInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            f"failed to load: {exc}"
        ) from exc
    if not callable(factory):
        raise GitInvokerPluginError(
            f"entry point {selected.name!r} in group {ENTRY_POINT_GROUP!r} "
            "did not resolve to a callable"
        )
    return cast(GitInvokerFactory, factory)


def get_git_invoker_factory() -> Optional[GitInvokerFactory]:
    """Return the configured git-invoker factory, or None for the native default.

    Raises:
        GitInvokerPluginError: an invoker was configured but is unusable. Raised
            rather than silently returning None -- see module docstring.
    """
    global _cached
    if _cached is not None:
        return None if _cached is _NO_PLUGIN else cast(GitInvokerFactory, _cached)

    spec = os.environ.get(ENV_VAR, "").strip()
    factory: Optional[GitInvokerFactory]
    if spec:
        factory = _load_from_path(spec)
        logger.info("git invoker plugin loaded from %s=%s", ENV_VAR, spec)
    else:
        factory = _load_from_entry_points()
        if factory is not None:
            logger.info(
                "git invoker plugin loaded from entry-point group %r",
                ENTRY_POINT_GROUP,
            )

    _cached = factory if factory is not None else _NO_PLUGIN
    return factory


def resolve_git_invoker() -> Optional[GitInvoker]:
    """Instantiate the configured GitInvoker, or None if none is configured.

    A configured-but-broken factory still raises (fail loud, never a silent
    native fallback).
    """
    factory = get_git_invoker_factory()
    if factory is None:
        return None
    return factory()


_cached_instance: object = None


def get_git_invoker() -> Optional[GitInvoker]:
    """Memoized GitInvoker instance for the hot path (``run_git_command``).

    ``run_git_command`` is called per git operation, so the invoker is resolved
    and instantiated ONCE and cached -- the factory (which may build an HTTP
    client to git-mcp) is not re-run on every git call. Returns None when no
    plugin is configured (the native default).
    """
    global _cached_instance
    if _cached_instance is None:
        inst = resolve_git_invoker()
        _cached_instance = inst if inst is not None else _NO_PLUGIN
    return (
        None if _cached_instance is _NO_PLUGIN else cast(GitInvoker, _cached_instance)
    )


def reset_cache() -> None:
    """Clear the resolved plugin + instance. For tests."""
    global _cached, _cached_instance
    _cached = None
    _cached_instance = None
