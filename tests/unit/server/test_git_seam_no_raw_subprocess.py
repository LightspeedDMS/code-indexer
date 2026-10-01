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

_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}
# asyncio spawns take the program and its arguments as *varargs*, not a list.
_ASYNC_EXEC = {"create_subprocess_exec"}

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
    # annotated local: `cmd: list[str] = ["git", "log", "--follow", ...]`
    ("server/routers/git.py", "log"),
    ("server/routers/inline_repos.py", "log"),
    # variable-argv reads (cmd = ["git", ...]; subprocess.run(cmd, ...)) -- only
    # visible since the guard resolves a Name to its assignment.
    ("server/services/golden_repo_branch_service.py", "for-each-ref"),
    ("server/services/golden_repo_branch_service.py", "symbolic-ref"),
    ("server/services/golden_repo_branch_service.py", "branch"),
    ("server/services/remote_branch_service.py", "ls-remote"),
}

# argv built by a helper we cannot read statically. Each entry asserts: this
# builder does not produce a git command. Keyed on (file, builder) so it survives
# edits and a NEW builder in the same file still fails until justified.
ALLOWED_UNREADABLE_ARGV = {
    # builds `cidx index ...` -- server layout flags appended to a cidx invocation
    ("server/repositories/golden_repo_manager.py", "append_server_layout_args"),
    # builds the `claude` CLI invocation
    ("server/services/claude_invoker.py", "_build_claude_command"),
    # `base_cmd + [...]` where base_cmd is the `claude` CLI invocation
    ("server/services/research_assistant_service.py", "<concatenation>"),
}

_GLOBAL_FLAGS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def _literal_elements(seq) -> List[Optional[str]]:
    """Elements of a List/Tuple node; a non-literal element becomes None."""
    out: List[Optional[str]] = []
    for el in seq.elts:
        if isinstance(el, ast.Constant) and isinstance(el.value, str):
            out.append(el.value)
        else:
            out.append(None)  # Starred, Name, JoinedStr, ...
    return out


def _resolve_name(
    name: str,
    scope: ast.AST,
    before_line: int,
    module: Optional[ast.Module] = None,
) -> Tuple[bool, Optional[List[Optional[str]]], str]:
    """Resolve `name` to the argv it was last assigned above *before_line*.

    Returns ``(assigned_here, elements)``:
      - ``(True, [...])``  a literal list/tuple -- classify it;
      - ``(True, None)``   assigned from something we cannot read -- unclassifiable;
      - ``(False, None)``  never assigned in this scope, so it is a parameter or a
        closure value. That is a generic runner being handed argv by its caller,
        and the caller is scanned on its own; flagging it here would make every
        subprocess helper a permanent violation while catching nothing.

    `cmd = ["git", "push", ...]` followed by `subprocess.run(cmd, ...)` is the
    shape a reintroduced bypass is most likely to take, and a guard that only
    reads literal call arguments cannot see it at all.
    """
    assigned = False
    best: Optional[List[Optional[str]]] = None
    builder = "<expression>"
    best_line = -1
    for n in ast.walk(scope):
        line = getattr(n, "lineno", None)
        if line is None or line >= before_line:
            continue
        # `cmd = [...]` and the annotated `cmd: list[str] = [...]` bind a name the
        # same way. This codebase is mypy-typed and annotates locals routinely, so
        # reading only ast.Assign leaves the common shape invisible.
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            targets, value = [n.target], n.value
        else:
            continue
        for tgt in targets:
            if not (isinstance(tgt, ast.Name) and tgt.id == name):
                continue
            assigned = True
            if line > best_line:
                best_line = line
                if isinstance(value, (ast.List, ast.Tuple)):
                    best = _literal_elements(value)
                else:
                    best = None
                    builder = _builder_name(value)

    # A literal whose FRONT is rewritten after assignment is no longer the argv
    # that runs, and the verb read from it would belong to a command that never
    # executes. `append`/`extend`/`+=` only add to the end, so the program and verb
    # survive and the literal is still safe to classify; `insert` and item
    # assignment can rewrite any position, so the argv becomes unreadable.
    if best is not None and _prefix_rewritten_after(
        name, scope, best_line, before_line
    ):
        return True, None, "<argv front rewritten after assignment>"

    # Not a local: a module-level constant (`_PUSH = ["git", "push"]`) used inside a
    # function is still a literal we can read, and treating it as a caller-supplied
    # parameter would hide it.
    if not assigned and module is not None and scope is not module:
        return _resolve_name(name, module, before_line=10**9)
    return assigned, best, builder


def _prefix_rewritten_after(name: str, scope: ast.AST, after: int, before: int) -> bool:
    """True if *name*'s leading elements may change between *after* and *before*."""
    for n in ast.walk(scope):
        line = getattr(n, "lineno", None)
        if line is None or not (after < line < before):
            continue
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "insert"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == name
        ):
            return True
        if (
            isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Store)
            and isinstance(n.value, ast.Name)
            and n.value.id == name
        ):
            return True
    return False


def _builder_name(node: ast.AST) -> str:
    """A stable label for whatever produced an argv we cannot read.

    Keying the allowlist on the builder rather than a line number means the entry
    survives edits above it, and a NEW way of building argv in the same file is a
    new key -- so it fails until someone justifies it.
    """
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    if isinstance(node, ast.BinOp):
        return "<concatenation>"
    return "<expression>"


def _git_argv(
    node: ast.Call, kind: str, scope: ast.AST, module: Optional[ast.Module] = None
) -> Tuple[Optional[List[Optional[str]]], Optional[str]]:
    """argv of a spawn call on git, or None if it is not one.

    Returns a list whose unknowable elements are None. A caller that gets back a
    list starting with something other than "git" is not our business.
    """
    if not node.args:
        return None, None
    if kind == "varargs":
        argv = _literal_elements(ast.List(elts=list(node.args), ctx=ast.Load()))
    else:
        first = node.args[0]
        if isinstance(first, (ast.List, ast.Tuple)):
            argv = _literal_elements(first)
        elif isinstance(first, ast.Name):
            assigned, resolved, builder = _resolve_name(
                first.id, scope, node.lineno, module
            )
            if not assigned:
                return None, None  # generic runner: argv comes from its caller
            if resolved is None:
                # built locally in a way we cannot read -- it could be git, and a
                # future `cmd = build(["git", "push", ...])` must not slip through.
                return [None], builder
            argv = resolved
        else:
            return None, None
    if not argv or argv[0] != "git":
        return None, None
    return argv, None


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


def _direct_import_aliases(tree: ast.Module) -> set:
    """Names bound by `from subprocess import run [as r]` in this module.

    Without this, a module that imports the spawn function directly is invisible
    to the guard even though it reaches git the same way.
    """
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in ("subprocess", "asyncio"):
            for al in n.names:
                if al.name in _SUBPROCESS_CALLS or al.name in _ASYNC_EXEC:
                    out.add(al.asname or al.name)
    return out


def _call_kind(node: ast.Call, aliases: set) -> Optional[str]:
    """ "list" if argv arrives as one sequence, "varargs" if spread, else None."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        if (
            isinstance(fn.value, ast.Name)
            and fn.value.id == "subprocess"
            and fn.attr in _SUBPROCESS_CALLS
        ):
            return "list"
        # asyncio.create_subprocess_exec(...) / loop.create_subprocess_exec(...)
        if fn.attr in _ASYNC_EXEC:
            return "varargs"
    if isinstance(fn, ast.Name) and fn.id in aliases:
        return "varargs" if fn.id in _ASYNC_EXEC else "list"
    return None


def _governed_files() -> List[Path]:
    out: List[Path] = []
    for top in _GOVERNED:
        for path in sorted((_SRC / top).rglob("*.py")):
            rel = path.relative_to(_SRC)
            if any(part in _EXEMPT_DIRS for part in rel.parts):
                continue
            out.append(path)
    return out


def _scope_index(tree: ast.Module) -> List[Tuple[int, int, ast.AST]]:
    """(start, end, node) for every function in *tree*, innermost-last.

    Built once per file: resolving a variable argv needs the enclosing function,
    and re-walking the tree for every call turns the guard quadratic.
    """
    spans: List[Tuple[int, int, ast.AST]] = []
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = max(getattr(x, "end_lineno", f.lineno) for x in ast.walk(f))
            spans.append((f.lineno, end, f))
    spans.sort(key=lambda t: t[0])
    return spans


def _enclosing_scope(tree: ast.Module, spans, node: ast.AST) -> ast.AST:
    """Innermost function containing *node*, else the module."""
    best: ast.AST = tree
    for start, end, f in spans:
        if start <= node.lineno <= end:
            best = f
    return best


def _scan() -> Tuple[List[str], List[str]]:
    """Return (violations, unknown_argv) across every governed file."""
    violations: List[str] = []
    unknown: List[str] = []
    for path in _governed_files():
        rel = path.relative_to(_SRC).as_posix()
        v, u = _scan_source(rel, path.read_text(encoding="utf-8"))
        violations.extend(v)
        unknown.extend(u)
    return violations, unknown


def _scan_source(rel: str, source: str) -> Tuple[List[str], List[str]]:
    """Scan one module's source. Split out from :func:`_scan` so the guard's own
    behaviour can be tested against synthetic sources rather than only against
    whatever the tree happens to contain today."""
    violations: List[str] = []
    unknown: List[str] = []
    tree = ast.parse(source)
    aliases = _direct_import_aliases(tree)
    spans = _scope_index(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _call_kind(node, aliases)
        if kind is None:
            continue
        argv, builder = _git_argv(node, kind, _enclosing_scope(tree, spans, node), tree)
        if argv is None:
            continue
        if builder is not None and (rel, builder) in ALLOWED_UNREADABLE_ARGV:
            continue
        where = f"{rel}:{node.lineno}"
        verb = _verb(argv)
        if verb is None:
            unknown.append(
                f"{where}  argv built by {builder!r}, not statically readable"
            )
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
        aliases = _direct_import_aliases(tree)
        spans = _scope_index(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kind = _call_kind(node, aliases)
                if kind is None:
                    continue
                argv, _b = _git_argv(
                    node, kind, _enclosing_scope(tree, spans, node), tree
                )
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


# ---------------------------------------------------------------------------
# The guard's own behaviour.
#
# Everything above asserts something about the tree as it stands today, which
# means a guard that silently sees nothing passes just as loudly as one that
# works. These feed the scanner synthetic sources instead, so each shape it
# claims to cover is actually proven to be reported -- and a regression in the
# scanner fails here rather than going unnoticed until the next bypass ships.
# ---------------------------------------------------------------------------

_REL = "server/services/synthetic.py"


def _violations(source: str) -> List[str]:
    return _scan_source(_REL, source)[0]


def _unknowns(source: str) -> List[str]:
    return _scan_source(_REL, source)[1]


class TestGuardSeesEveryCallShape:
    def test_literal_argv(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["git", "push", "origin", "main"])\n'
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_in_a_plain_variable(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_in_an_annotated_variable(self) -> None:
        """The shape that slipped through the first version of this guard."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd: list[str] = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_from_a_module_level_constant(self) -> None:
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def f():\n"
            "    subprocess.run(_PUSH)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_directly_imported_spawn_function(self) -> None:
        src = (
            "from subprocess import check_call as cc\n"
            "def f():\n"
            '    cc(["git", "reset", "--hard", "HEAD"])\n'
        )
        assert any("MUTATING: git reset" in v for v in _violations(src))

    def test_asyncio_create_subprocess_exec_varargs(self) -> None:
        src = (
            "import asyncio\n"
            "async def f():\n"
            '    await asyncio.create_subprocess_exec("git", "push", "origin", "main")\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_check_call_is_in_the_call_set(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.check_call(["git", "commit", "-m", "x"])\n'
        assert any("MUTATING: git commit" in v for v in _violations(src))


class TestGuardClassifiesCorrectly:
    def test_unlisted_read_is_reported(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["git", "status"])\n'
        assert any("unlisted read: git status" in v for v in _violations(src))

    def test_append_and_extend_keep_the_verb_readable(self) -> None:
        """They add to the end, so the program and verb survive."""
        src = (
            "import subprocess\n"
            "def f(path):\n"
            '    cmd = ["git", "status"]\n'
            "    cmd.append(path)\n"
            '    cmd.extend(["--", path])\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("unlisted read: git status" in v for v in _violations(src))
        assert not _unknowns(src)

    def test_insert_rewrites_the_front_so_argv_becomes_unreadable(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "status"]\n'
            '    cmd.insert(1, "push")\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("front rewritten" in u for u in _unknowns(src))

    def test_argv_built_by_a_helper_is_reported_not_skipped(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            "    cmd = build_it()\n"
            "    subprocess.run(cmd)\n"
        )
        assert any("build_it" in u for u in _unknowns(src))

    def test_generic_runner_taking_argv_from_its_caller_is_skipped(self) -> None:
        """Its caller is scanned on its own; flagging here would make every
        subprocess helper a permanent violation while catching nothing."""
        src = "import subprocess\ndef run_it(command):\n    subprocess.run(command)\n"
        assert not _violations(src)
        assert not _unknowns(src)

    def test_non_git_commands_are_ignored(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["cidx", "index", "--fts"])\n'
        assert not _violations(src)
        assert not _unknowns(src)

    def test_global_flags_do_not_hide_the_verb(self) -> None:
        src = (
            "import subprocess\n"
            "def f(repo):\n"
            '    subprocess.run(["git", "-C", repo, "push", "origin", "main"])\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))
