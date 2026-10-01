"""EVO-64678: the git seam owns every server-side git invocation.

``utils/git_runner.run_git_command`` is the ONE chokepoint git calls bottom out
at: it asks ``is_mutating_git()`` whether a command mutates and, when an invoker
is installed, delegates the mutating ones to git-mcp. Reads stay local -- that is
a deliberate, credential-driven policy (a read needs the working tree, already in
the pod on shared NFS, and no PAT; a mutation needs the PAT, which EVO-64616
confines to git-mcp).

That policy only holds if the chokepoint is the only door. A raw
``subprocess.run(["git", ...])`` under ``server/`` or ``global_repos/`` skips both
the delegation decision AND ``get_git_environment()``'s safe.directory handling.

Upstream has no reason to know the seam exists, so each upstream snapshot can
reintroduce raw calls in code that merges without conflict -- the v12.78.0
snapshot brought four of them into ``golden_repo_manager.py`` alone. This test
turns that from archaeology into a build failure.

To add a site here you must justify it in ``ALLOWED_READS``. A MUTATING command
is never allowed: route it through ``run_git_command``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Tuple

from code_indexer.utils.git_invoker import is_mutating_git

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"

# Directories the seam governs. The CLI runs on a developer's own machine, so
# client-side git is out of scope; auto_update/ and scripts/ are deploy tooling
# that runs outside the request path.
_GOVERNED = ("server", "global_repos")
_EXEMPT_DIRS = ("auto_update", "scripts")

_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "call"}

# Read-only git that deliberately runs locally. is_mutating_git() returns False
# for every entry; each is keyed by (path relative to src/code_indexer, git verb)
# so it survives line-number drift. A verb appearing here does NOT whitelist its
# mutating forms -- the assertion below re-checks each call against the
# classifier, so `git remote show` being listed cannot smuggle in `git remote add`.
ALLOWED_READS = {
    ("global_repos/git_pull_updater.py", "symbolic-ref"),
    ("global_repos/git_pull_updater.py", "log"),
    ("global_repos/refresh_scheduler.py", "branch"),
    ("global_repos/refresh_scheduler.py", "symbolic-ref"),
    ("global_repos/refresh_scheduler.py", "rev-parse"),
    ("server/services/git_operations_service.py", "config"),
    ("server/services/golden_repo_reconciler.py", "rev-parse"),
    ("server/services/cidx_meta_backup/branch_detect.py", "remote"),
    ("server/startup/lifespan.py", "remote"),
    ("server/repositories/activated_repo_manager.py", "branch"),
    ("server/repositories/activated_repo_manager.py", "diff"),
    ("server/repositories/activated_repo_manager.py", "rev-parse"),
    ("server/repositories/activated_repo_manager.py", "log"),
    ("server/repositories/activated_repo_manager.py", "status"),
    ("server/repositories/golden_repo_manager.py", "config"),
    ("server/repositories/golden_repo_manager.py", "rev-parse"),
    ("server/repositories/repository_listing_manager.py", "ls-remote"),
    ("server/routers/activated_repos.py", "branch"),
    ("server/routers/git.py", "rev-parse"),
    ("server/routers/git.py", "show"),
    ("server/routers/git.py", "blame"),
    ("server/routers/inline_repos.py", "log"),
}

_GLOBAL_FLAGS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def _git_argv(node: ast.Call) -> Optional[List[Optional[str]]]:
    """Literal argv of a subprocess call on git, or None if it is not one.

    A non-literal element (an f-string, a name, a splat) becomes None, which
    marks the argv as not statically knowable.
    """
    if not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, (ast.List, ast.Tuple)):
        return None
    argv: List[Optional[str]] = []
    for el in first.elts:
        if isinstance(el, ast.Constant) and isinstance(el.value, str):
            argv.append(el.value)
        else:
            argv.append(None)  # Starred, Name, JoinedStr, ...
    if not argv or argv[0] != "git":
        return None
    return argv


def _verb(argv: List[Optional[str]]) -> Optional[str]:
    """First non-flag token after `git`, skipping global flags."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok is None:
            return None
        if tok in _GLOBAL_FLAGS_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


def _is_subprocess_call(node: ast.Call) -> bool:
    fn = node.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr in _SUBPROCESS_CALLS
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "subprocess"
    )


def _governed_files() -> List[Path]:
    out: List[Path] = []
    for top in _GOVERNED:
        for path in sorted((_SRC / top).rglob("*.py")):
            rel = path.relative_to(_SRC)
            if any(part in _EXEMPT_DIRS for part in rel.parts):
                continue
            out.append(path)
    return out


def _scan() -> Tuple[List[str], List[str]]:
    """Return (violations, unknown_argv) as human-readable locations."""
    violations: List[str] = []
    unknown: List[str] = []
    for path in _governed_files():
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
                continue
            argv = _git_argv(node)
            if argv is None:
                continue
            where = f"{rel}:{node.lineno}"
            verb = _verb(argv)
            if verb is None:
                unknown.append(f"{where}  (git verb not statically knowable)")
                continue
            # Keep positions: a dynamic element is a placeholder, never dropped,
            # so a global flag cannot swallow the verb during classification.
            concrete = [a if a is not None else "<dynamic>" for a in argv]
            if is_mutating_git(concrete):
                violations.append(f"{where}  MUTATING: git {verb}")
            elif (rel, verb) not in ALLOWED_READS:
                violations.append(f"{where}  unlisted read: git {verb}")
    return violations, unknown


def test_no_raw_mutating_git_outside_the_seam() -> None:
    """No server-side git mutation may bypass run_git_command()."""
    violations, _ = _scan()
    assert not violations, (
        "git calls under server/ or global_repos/ must go through "
        "code_indexer.utils.git_runner.run_git_command() so the seam can decide "
        "whether to delegate (EVO-64678).\n\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nA MUTATING call is never allowed here: route it through "
        "run_git_command(). A genuinely read-only call may be added to "
        "ALLOWED_READS in this file, with a reason."
    )


def test_no_raw_git_with_unknowable_argv() -> None:
    """A raw call whose argv is built at runtime cannot be classified.

    Such a site can silently become a mutation later, so it must go through the
    chokepoint, where is_mutating_git() sees the real argv.
    """
    _, unknown = _scan()
    assert not unknown, (
        "these raw git calls build argv at runtime, so no static check can tell "
        "whether they mutate -- route them through run_git_command() and let "
        "is_mutating_git() classify each call (EVO-64678).\n\n"
        + "\n".join(f"  - {u}" for u in unknown)
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Keep ALLOWED_READS honest as call sites are removed or seamed."""
    seen = set()
    for path in _governed_files():
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_call(node):
                argv = _git_argv(node)
                if argv is None:
                    continue
                verb = _verb(argv)
                if verb is not None:
                    seen.add((rel, verb))
    stale = sorted(ALLOWED_READS - seen)
    assert not stale, (
        "ALLOWED_READS entries no longer match any raw git call -- delete them:\n"
        + "\n".join(f"  - {s[0]}  git {s[1]}" for s in stale)
    )
