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
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from code_indexer.utils.git_invoker import is_mutating_git

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"

# Directories the seam governs. The CLI runs on a developer's own machine, so
# client-side git is out of scope; auto_update/ and scripts/ are deploy tooling
# that runs outside the request path.
# SEEDS, not the whole fence. What the seam governs is what the server can
# REACH: the fence is the transitive first-party import closure of these, so a
# raw mutating git call one directory away is not invisible just because it
# lives outside `server/`. `services/git_topology_service.py` is imported by
# `server/mcp/handlers/repos.py` and was never scanned under the old rule.
_GOVERNED = ("server", "global_repos")

# Deploy tooling that runs outside the request path. Matched as a PATH PREFIX:
# as "any component" it would have exempted a future
# `server/repositories/scripts/` for free.
_EXEMPT_DIRS = ("scripts", "server/scripts")

# auto_update is NOT a blanket exemption any more. Its git calls act on the
# server's OWN source tree and on hnswlib -- never on a tenant golden repo --
# so git-mcp, which exists to hold golden-repo credentials, is the wrong
# destination for them. That justification is real, but it belongs against each
# CALL: as a directory rule it silently covered any future mutation dropped in
# the same folder, and three request-path modules import DeploymentExecutor.
# Keyed (path, verb) so a new verb in the same file fails until justified.
ALLOWED_SELF_UPDATE_MUTATIONS = {
    # `git fetch` / `git pull origin <branch>` on the cidx checkout itself
    ("server/auto_update/change_detector.py", "fetch"),
    ("server/auto_update/deployment_executor.py", "pull"),
    # `git clone <hnswlib>` to a fallback path, and submodule sync for the build
    ("server/auto_update/deployment_executor.py", "clone"),
    ("server/auto_update/deployment_executor.py", "submodule"),
    # `git config` on the server's own checkout (safe.directory / build config)
    ("server/auto_update/deployment_executor.py", "config"),
}

_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}
# asyncio spawns take the program and its arguments as *varargs*, not a list.
_ASYNC_EXEC = {"create_subprocess_exec"}
# Spawns whose argv arrives as ONE STRING the shell parses. Cheap to cover and
# invisible otherwise: `subprocess.run("git push", shell=True)` and
# `os.system("git push")` reach git exactly like a list does.
_STRING_SPAWNS = {
    "system",
    "popen",
    "getoutput",
    "getstatusoutput",
    "create_subprocess_shell",
}
# os.exec* / posix_spawn take the program plus a sequence.
_OS_EXEC = {"execv", "execve", "execvp", "execvpe", "posix_spawn"}

# Which module each spawn belongs to. Keyed by module because the generic names
# are not unique: `asyncio.run(coro)` is the EVENT LOOP RUNNER, not a spawn, and
# matching a bare `run` on any receiver reported every `asyncio.run` in the tree
# as an unreadable git call. Only the distinctive names below may match on any
# receiver, because `loop.create_subprocess_exec(...)` is a real spawn whose
# receiver is not a module.
_MODULE_SPAWNS = {
    "subprocess": _SUBPROCESS_CALLS | {"getoutput", "getstatusoutput", "run", "Popen"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {"system", "popen"} | _OS_EXEC,
}
_DISTINCTIVE_SPAWNS = (
    _ASYNC_EXEC | _OS_EXEC | {"create_subprocess_shell", "getstatusoutput"}
)

# Verbs whose MUTATING-ness is decided by a sub-verb rather than the verb.
# `is_mutating_git(["git","remote"])` is False; `["git","remote","add",...]` is
# True -- so for these the append/extend exemption below cannot apply, because
# the appended tail is exactly where the mutation arrives.
_DUAL_FORM_VERBS = {
    "remote",
    "branch",
    "tag",
    "config",
    "stash",
    "submodule",
    "worktree",
    "notes",
    "bisect",
}

# Read-only git that deliberately runs locally. is_mutating_git() returns False
# for every entry; each is keyed by (path relative to src/code_indexer, git verb)
# so it survives line-number drift. A verb appearing here does NOT whitelist its
# mutating forms -- the assertion below re-checks each call against the
# classifier, so `git remote show` being listed cannot smuggle in `git remote add`.
ALLOWED_READS = {
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
    # Self-update reads on the server's OWN checkout (see
    # ALLOWED_SELF_UPDATE_MUTATIONS for why this directory is not delegated).
    ("server/auto_update/change_detector.py", "rev-parse"),
    ("server/auto_update/deployment_executor.py", "ls-files"),
    # Reads are listed only for the CORE (see _CORE): a read outside it is local
    # by definition and listing 50+ of them would bury the entries that matter.
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
    # Sites the fail-closed rewrite made visible. Each was triaged by reading
    # the call: none is git, and none can become git without editing the line.
    # `append_server_layout_args(["cidx", "index"])` -- the cidx CLI again
    ("server/services/claude_cli_manager.py", "append_server_layout_args"),
    # `[indexer_name, "--version"]` -- probes whether an indexer is installed
    ("server/installer.py", "<dynamic-program>"),
    # Popen of the cidx server itself during lifecycle start
    ("server/lifecycle/server_lifecycle_manager.py", "<dynamic-program>"),
    # `create_subprocess_exec(program, *args)` where program is `command.split()[0]`
    # of a caller-supplied tool-version string ("rg --version")
    ("server/services/diagnostics_service.py", "<starred-argv>"),
    # `create_subprocess_exec(*cmd)` -- cmd is a parameter holding the claude CLI
    ("server/services/scip_self_healing.py", "<starred-argv>"),
    # `os.execv(sys.executable, [sys.executable] + sys.argv)` -- dev-mode re-exec
    ("server/web/routes.py", "<dynamic-program>"),
    # Self-update tooling: builds `cidx`/`python`/`pip`/shell invocations to
    # rebuild and restart the server itself. Not git, and not reachable with a
    # tenant's credentials -- see ALLOWED_SELF_UPDATE_MUTATIONS.
    ("server/auto_update/deployment_executor.py", "<concatenation>"),
    ("server/auto_update/deployment_executor.py", "<dynamic-program>"),
    ("server/auto_update/deployment_executor.py", "<expression>"),
    ("server/auto_update/deployment_executor.py", "<shell-wrapper>"),
    # `[str(xray_cli_path), "--print-graph-extractor-extensions"]` -- the xray
    # binary, whose path is resolved at runtime (CIDX_XRAY_CLI_PATH).
    ("xray/rust_backend.py", "<dynamic-program>"),
}

# Where the READ allowlist is enforced. Mutations and unreadable argv are
# enforced across the whole reachable closure -- those are the safety
# properties -- but ALLOWED_READS exists to force a human to look at each read
# in the request path, and extending it to every reachable module would add
# ~50 entries of noise and devalue the ones that matter. A read outside the
# core runs locally either way; the seam's rule is about mutations.
_CORE = ("server/", "global_repos/")

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
    call: Optional[ast.Call] = None,
) -> Tuple[bool, Optional[List[Optional[str]]], List[str]]:
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
    builders = ["<expression>"]
    best_line = -1
    # (line, argv-or-None, builder-label) per reaching assignment. The label
    # travels with its candidate because ALLOWED_UNREADABLE_ARGV is keyed on it:
    # labelling the chosen candidate with some other assignment's builder would
    # make a justified allowlist entry silently fail to match.
    candidates: List[Tuple[int, Optional[List[Optional[str]]], str]] = []
    # Only this scope's own statements. ast.walk() would descend into nested
    # functions, so a helper's parameter would adopt a sibling function's
    # assignment of the same name -- `cmd`, `command` and `args` are common enough
    # that this reports a real-looking bypass at a location that has none.
    for n in _statements_of(scope):
        line = getattr(n, "lineno", None)
        if line is None:
            continue
        if line >= before_line and not (
            call is not None and _loop_carried(n, call, scope)
        ):
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
            if not _reaches(line, n, call, scope):
                continue
            if isinstance(value, (ast.List, ast.Tuple)):
                candidates.append((line, _literal_elements(value), "<literal>"))
            else:
                candidates.append((line, None, _builder_name(value)))
            best_line = max(best_line, line)

    # Bound by something we cannot read a literal out of -- a for-target, a
    # with-item, a walrus, tuple unpacking, an except-as, a comprehension.
    # These are BOUND, so they are this scope's responsibility, and unreadable,
    # so they must be reported rather than skipped.
    for opaque_line, label in _opaque_bindings(name, scope):
        # `<=`, not `<`: a comprehension target and a walrus bind on the same
        # line as the use that consumes them.
        if opaque_line <= before_line:
            assigned = True
            candidates.append((opaque_line, None, label))
            best_line = max(best_line, opaque_line)

    # Several assignments can reach one call -- `if force: cmd = ["git", "push"]`
    # / `else: cmd = ["git", "status"]`. Picking by highest line number reads
    # whichever branch happens to be written last, so the verdict would depend on
    # source layout. Decide by severity instead, worst first:
    #   1. a literal that mutates  -> report it; that path really does mutate.
    #   2. any unreadable candidate -> unreadable, even if a literal read is also
    #      reachable. A branch we cannot read could be a push, and letting a
    #      sibling `["git", "status"]` speak for it is exactly the masking that
    #      hides one.
    #   3. otherwise the last literal read.
    best: Optional[List[Optional[str]]] = None
    best_at = best_line
    if candidates:
        mutating = [
            c
            for c in candidates
            if c[1]
            and c[1][0] == "git"
            and is_mutating_git([a or "<dyn>" for a in c[1]])
        ]
        unreadable = [c for c in candidates if c[1] is None]
        if mutating:
            best_at, best = mutating[0][0], mutating[0][1]
            builders = [mutating[0][2]]
        elif unreadable:
            # EVERY distinct unreadable builder, not just one. Collapsing them
            # let an allowlisted builder in one branch speak for a
            # non-allowlisted one in its sibling: _scan_source checks the single
            # label it got, finds it justified and skips the call. Sorted because
            # _statements_of pops its stack, so "which one is first" is really
            # "which was written last" -- the source-layout dependence the
            # severity ordering above exists to remove.
            best_at, best = min(c[0] for c in unreadable), None
            builders = sorted({c[2] for c in unreadable})
        else:
            last = max(candidates, key=lambda c: c[0])
            best_at, best = last[0], last[1]
            builders = [last[2]]

    # A literal whose FRONT is rewritten after assignment is no longer the argv
    # that runs, and the verb read from it would belong to a command that never
    # executes. `append`/`extend`/`+=` only add to the end, so the program and verb
    # survive and the literal is still safe to classify; `insert` and item
    # assignment can rewrite any position, so the argv becomes unreadable.
    # Measured from the CHOSEN assignment, not the last one: when a mutating
    # candidate wins, a rewrite between it and a later sibling still applies to it.
    is_git_literal = bool(best) and best[0] == "git"
    if is_git_literal and _prefix_rewritten_after(name, scope, best_at, before_line):
        return True, None, ["<argv front rewritten after assignment>"]

    # A verb whose MUTATING-ness is decided by a sub-verb cannot be classified
    # from a literal that stops before the sub-verb. The append/extend exemption
    # reasoned that those only add to the END, so program and verb survive --
    # true, and beside the point for `git remote` / `git branch` / `git config`,
    # where the appended tail IS the decision. `cmd = ["git","remote"]` then
    # `cmd.extend(["add", name, url])` classified as the allowlisted read
    # `git remote`, which is precisely the smuggling this file's own docstring
    # claims is impossible. storage/shared/git_backend.py:481 is this idiom.
    if is_git_literal and _tail_extended_after(name, scope, best_at, before_line):
        verb = _verb(best)
        if verb is None or verb in _DUAL_FORM_VERBS:
            return True, None, ["<dual-form verb completed after assignment>"]

    # Not a local: a module-level constant (`_PUSH = ["git", "push"]`) used inside a
    # function is still a literal we can read, and treating it as a caller-supplied
    # parameter would hide it.
    if not assigned and module is not None and scope is not module:
        # A parameter is the caller's business and the ONLY legitimate silence.
        if name in _parameters_of(scope):
            return False, None, builders
        return _resolve_name(name, module, before_line=10**9, call=call)
    return assigned, best, builders


def _parameters_of(scope: ast.AST) -> set:
    """Parameter names of a function scope.

    The ONE legitimate silence is "argv is a parameter": the caller supplies it
    and the caller is scanned on its own, so flagging the helper would make
    every subprocess wrapper a permanent violation while catching nothing.
    That has to be decided from the SIGNATURE. Inferring it from "no Assign
    found in this scope" is what made every other binding form -- for-targets,
    with-items, walrus, tuple unpacking, except-as, comprehensions -- look like
    parameters and vanish.
    """
    args = getattr(scope, "args", None)
    if args is None:
        return set()
    names = set()
    for a in (
        list(getattr(args, "posonlyargs", []))
        + list(args.args)
        + list(getattr(args, "kwonlyargs", []))
    ):
        names.add(a.arg)
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return names


def _opaque_bindings(name: str, scope: ast.AST) -> List[Tuple[int, str]]:
    """(line, label) for every binding of *name* we cannot read a literal from.

    Bound-but-unreadable is a world away from unbound: unbound means a caller
    owns it, unreadable means a mutation could be hiding in it.
    """
    out: List[Tuple[int, str]] = []

    def note(target, label: str) -> None:
        if isinstance(target, ast.Name):
            if target.id == name:
                out.append((getattr(target, "lineno", 0), label))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for el in target.elts:
                note(el, label)

    for n in _statements_of(scope):
        if isinstance(n, (ast.For, ast.AsyncFor)):
            note(n.target, "<for-target>")
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None:
                    note(item.optional_vars, "<with-item>")
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, (ast.Tuple, ast.List)):
                    note(t, "<tuple-unpack>")
        elif isinstance(n, ast.Try):
            for h in n.handlers:
                if h.name == name:
                    out.append((getattr(h, "lineno", 0), "<except-as>"))
    # Expression-level binders live inside statements, so walk for these --
    # but stop at a nested def, same reason _statements_of does.
    for n in _walk_this_scope(scope):
        if isinstance(n, ast.NamedExpr):
            note(n.target, "<walrus>")
        elif isinstance(n, (ast.comprehension,)):
            note(n.target, "<comprehension-target>")
    return out


def _walk_this_scope(scope: ast.AST):
    """ast.walk, but not descending into nested function/class scopes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _dead_blocks(scope: ast.AST):
    """(owner, body) for branches a constant test makes unreachable.

    Blaming `if False:` names a command the program cannot run, and an
    unexplainable finding is how a guard gets deleted.
    """
    for n in _statements_of(scope):
        if isinstance(n, ast.If) and isinstance(n.test, ast.Constant):
            yield n, (n.body if not n.test.value else n.orelse)


def _exclusive_regions(scope: ast.AST):
    """(owner, body) for each mutually exclusive branch arm in *scope*."""
    for n in _statements_of(scope):
        if isinstance(n, ast.If):
            yield n, n.body
            yield n, n.orelse
        elif isinstance(n, ast.Try):
            for h in n.handlers:
                yield n, h.body
            yield n, n.orelse
        elif isinstance(n, ast.Match):
            for case in n.cases:
                yield n, case.body


def _in_block(node: ast.AST, body: List[ast.AST]) -> bool:
    if not body:
        return False
    lines = [getattr(b, "lineno", None) for b in body]
    ends = [getattr(b, "end_lineno", None) or getattr(b, "lineno", None) for b in body]
    lo = min(x for x in lines if x is not None)
    hi = max(x for x in ends if x is not None)
    ln = getattr(node, "lineno", None)
    return ln is not None and lo <= ln <= hi


def _enclosing_loops(scope: ast.AST):
    """(owner, body) for each loop in *scope*; a loop body has a back edge."""
    for n in _statements_of(scope):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            yield n, n.body


def _loop_carried(assign_node: ast.AST, call: ast.Call, scope: ast.AST) -> bool:
    """True if a textually-later assignment can still supply *call*'s argv.

    Resolution stops at the call's line, which is sound for straight-line code
    and wrong inside a loop: on the second iteration the call runs the argv
    assigned at the BOTTOM of the first.
    """
    for _owner, body in _enclosing_loops(scope):
        if _in_block(assign_node, body) and _in_block(call, body):
            return True
    return False


def _reaches(assign_line: int, assign_node: ast.AST, call: ast.Call, scope) -> bool:
    """Can the argv bound at *assign_node* be the one *call* runs?

    Severity ordering alone picked the worst candidate anywhere in the function,
    with no notion of which assignments a call can actually see. That blamed a
    `git push` in one arm of an if/else on the `tar` call in the other arm --
    naming a command that line cannot run, which is how a guard gets deleted.

    An assignment in a branch the call is NOT in still reaches the call when the
    call comes after the whole statement (the classic `if/else` then `run(cmd)`),
    but not when the call sits inside the sibling arm.
    """
    for owner, body in _dead_blocks(scope):
        if _in_block(assign_node, body):
            return False
    for owner, body in _exclusive_regions(scope):
        if _in_block(assign_node, body) and not _in_block(call, body):
            owner_end = getattr(owner, "end_lineno", None) or getattr(
                owner, "lineno", 0
            )
            if getattr(call, "lineno", 0) <= owner_end:
                return False
    return True


def _statements_of(scope: ast.AST) -> List[ast.AST]:
    """Statements belonging to *scope* itself, not to functions nested in it.

    Control flow inside the scope (if/try/for/with) is followed, because an
    assignment in a branch still reaches the call; a nested def is not, because
    its body is a different scope with its own parameters.
    """
    out: List[ast.AST] = []
    stack = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            stack.extend(getattr(node, field, []) or [])
        for handler in getattr(node, "handlers", []) or []:
            stack.extend(handler.body)
        # match/case arms are branches of this scope exactly like if/else; the
        # generic field loop above misses them because they hang off `cases`.
        for case in getattr(node, "cases", []) or []:
            stack.extend(case.body)
    return out


def _tail_extended_after(name: str, scope: ast.AST, after: int, before: int) -> bool:
    """True if *name* gains trailing elements between *after* and *before*."""
    for n in _walk_this_scope(scope):
        line = getattr(n, "lineno", None)
        if line is None or not (after < line < before):
            continue
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("append", "extend")
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == name
        ):
            return True
        if (
            isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            return True
    return False


def _prefix_rewritten_after(name: str, scope: ast.AST, after: int, before: int) -> bool:
    """True if *name*'s leading elements may change between *after* and *before*."""
    for n in _walk_this_scope(scope):
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
) -> Tuple[Optional[List[Optional[str]]], Optional[List[str]]]:
    """argv of a spawn call on git, or None if it is provably not one.

    FAIL CLOSED. The recurring defect in this guard was that every path it
    could not read returned ``(None, None)``, which `_scan_source` cannot tell
    apart from "this call is not git at all" -- so an unreadable spawn was
    indistinguishable from an irrelevant one, and 41 shapes of raw `git push`
    scanned clean. Only two things may be silent now: argv that is a parameter
    of this scope, and argv whose program is a LITERAL that is not git.
    """
    args = list(node.args)
    if not args:
        # `subprocess.run(args=[...], check=True)` is ordinary Python; skipping
        # the whole call because `node.args` was empty was a one-line hole.
        for kw in node.keywords:
            if kw.arg in ("args", "cmd", "command", "popenargs"):
                args = [kw.value]
                break
    if not args:
        return None, None

    if any(isinstance(a, ast.Starred) for a in args):
        return [None], ["<starred-argv>"]

    if kind == "string":
        return _argv_from_string(args[0])

    if kind == "list_if_literal_git":
        first = args[0]
        if not isinstance(first, (ast.List, ast.Tuple)):
            return None, None
        argv = _literal_elements(first)
        if not argv or argv[0] != "git":
            return None, None
        return argv, None

    if kind == "varargs":
        argv = _literal_elements(ast.List(elts=args, ctx=ast.Load()))
    else:
        first = args[0]
        if isinstance(first, (ast.List, ast.Tuple)):
            argv = _literal_elements(first)
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            return _argv_from_string(first)
        elif isinstance(first, ast.Name):
            assigned, resolved, builders = _resolve_name(
                first.id, scope, node.lineno, module, call=node
            )
            if not assigned:
                return None, None  # generic runner: argv comes from its caller
            if resolved is None:
                return [None], builders
            argv = resolved
        elif isinstance(first, ast.BinOp) and isinstance(first.op, ast.Add):
            # `["git", "log", ...] + files`. The literal HEAD carries the
            # program and verb, and `+` only appends, so this is classifiable
            # -- the same reasoning the append/extend exemption rests on, and
            # it inherits the same dual-form safeguard below. Treating it as
            # opaque pushed real reads into an allowlist keyed on the builder,
            # where a later `["git","push"] + args` in the same file would have
            # been covered by the existing entry.
            head = first.left
            while isinstance(head, ast.BinOp) and isinstance(head.op, ast.Add):
                head = head.left
            if not isinstance(head, (ast.List, ast.Tuple)):
                return [None], [_builder_name(first)]
            argv = _literal_elements(head)
            if argv and argv[0] == "git":
                verb = _verb(argv)
                if verb is None or verb in _DUAL_FORM_VERBS:
                    return [None], ["<dual-form verb completed by concatenation>"]
        else:
            # An inline expression -- `self._cmd`, `CMDS["push"]`. Binding the
            # identical expression to a name was reported while passing it
            # inline was not, so deleting one line hid a bypass.
            return [None], [_builder_name(first)]

    if not argv:
        return None, None
    if argv[0] is None:
        # Dynamic PROGRAM. A dynamic verb was already reported; this was not,
        # because `argv[0] != "git"` conflates "unknown" with "definitely not
        # git". diagnostics_service.py spawns exactly this shape.
        return [None], ["<dynamic-program>"]
    if argv[0] in ("sh", "bash", "/bin/sh", "/bin/bash"):
        # `["sh", "-c", "git push ..."]` hides the real command one level down,
        # exactly as the single-string form does.
        for i, tok in enumerate(argv[1:], 1):
            if tok == "-c" and i + 1 < len(argv) and argv[i + 1] is not None:
                return _argv_from_string(ast.Constant(value=argv[i + 1]))
        return [None], ["<shell-wrapper>"]
    if argv[0] != "git":
        return None, None  # a literal non-git program: provably not our business
    return argv, None


def _argv_from_string(
    node: ast.AST,
) -> Tuple[Optional[List[Optional[str]]], Optional[List[str]]]:
    """argv for a spawn whose command arrives as one shell string."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return [None], ["<dynamic-shell-string>"]
    try:
        parts: List[Optional[str]] = list(shlex.split(node.value))
    except ValueError:
        return [None], ["<unparsable-shell-string>"]
    if not parts:
        return None, None
    # `sh -c "git push ..."` hides the real command one level down.
    if parts[0] in ("sh", "bash", "/bin/sh", "/bin/bash") and len(parts) >= 3:
        for i, tok in enumerate(parts[1:], 1):
            if tok == "-c" and i + 1 < len(parts):
                return _argv_from_string(ast.Constant(value=parts[i + 1]))
    if parts[0] != "git":
        return None, None
    return parts, None


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


def _direct_import_aliases(tree: ast.Module) -> Dict[str, str]:
    """Local name -> calling convention, for `from subprocess import run [as r]`.

    Without this, a module that imports the spawn function directly is invisible
    to the guard even though it reaches git the same way.

    The convention is decided from the IMPORTED name, never the local one: with
    `from asyncio import create_subprocess_exec as cse`, keying off `cse` finds
    nothing in _ASYNC_EXEC and reads a varargs spawn as list-style, so argv[0]
    is taken to be the whole command and `cse("git", "push")` scans as clean.
    """
    out: Dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in (
            "subprocess",
            "asyncio",
            "os",
        ):
            for al in n.names:
                kind = _kind_for_spawn_name(al.name)
                if kind is not None:
                    out[al.asname or al.name] = kind
        # `import subprocess as sp` rebinds the MODULE, so sp.run(...) is a
        # spawn too. Reading only ImportFrom made that whole style invisible.
        if isinstance(n, ast.ImportFrom) and n.module in (
            "subprocess",
            "asyncio",
            "os",
        ):
            # `from subprocess import *` binds every public name with no alias
            # to record, so a bare `run(...)` had nothing to match against.
            if any(al.name == "*" for al in n.names):
                for spawn in _MODULE_SPAWNS.get(n.module, ()):
                    kind = _kind_for_spawn_name(spawn)
                    if kind is not None:
                        out.setdefault(spawn, kind)
        elif isinstance(n, ast.Import):
            for al in n.names:
                if al.asname and al.name in ("subprocess", "asyncio", "os"):
                    out.setdefault("__module__" + al.asname, al.name)
    return out


def _kind_for_spawn_name(name: str) -> Optional[str]:
    """Calling convention for a spawn function, or None if it is not one."""
    if name in _ASYNC_EXEC:
        return "varargs"
    if name in _STRING_SPAWNS:
        return "string"
    if name in _OS_EXEC:
        return "varargs"
    if name in _SUBPROCESS_CALLS:
        return "list"
    return None


def _partial_spawn(node: ast.Call, aliases: Dict[str, str]):
    """(kind, args) when *node* is `partial(<spawn>, argv, ...)`, else None.

    `functools.partial` defers the call but binds argv at this line, so the
    site is where a reviewer would look -- and the call whose func is `partial`
    is not itself a spawn, so it was invisible.
    """
    fn = node.func
    name = (
        fn.id
        if isinstance(fn, ast.Name)
        else fn.attr
        if isinstance(fn, ast.Attribute)
        else None
    )
    if name != "partial" or not node.args:
        return None
    inner = node.args[0]
    if not isinstance(inner, (ast.Name, ast.Attribute)):
        return None
    probe = ast.Call(func=inner, args=list(node.args[1:]), keywords=list(node.keywords))
    ast.copy_location(probe, node)
    kind = _call_kind(probe, aliases)
    return (kind, probe) if kind is not None else None


def _call_kind(node: ast.Call, aliases: Dict[str, str]) -> Optional[str]:
    """ "list" if argv arrives as one sequence, "varargs" if spread, "string" if
    the shell parses it, else None.

    Keyed on the FUNCTION NAME, not the receiver. Requiring a receiver literally
    named `subprocess` meant `import subprocess as sp` and a stashed `self._sp`
    were both invisible. Matching on the attribute alone is safe because argv
    must still resolve to a command starting with `git` before anything is
    reported -- an unrelated `self.queue.run(...)` resolves to a non-git literal
    and is dropped.
    """
    fn = node.func
    if isinstance(fn, ast.Attribute):
        if fn.attr in _DISTINCTIVE_SPAWNS:
            return _kind_for_spawn_name(fn.attr)
        base = fn.value
        base_name = base.id if isinstance(base, ast.Name) else None
        if base_name is not None:
            module = aliases.get("__module__" + base_name, base_name)
            if fn.attr in _MODULE_SPAWNS.get(module, ()):
                return _kind_for_spawn_name(fn.attr)
        # A generic spawn name on a receiver we cannot identify -- `self._sp.run`,
        # a stashed module, an injected runner. Scanned, but only reported when
        # argv READS as a literal git command: an unidentified receiver cannot
        # carry the fail-closed rule, because `asyncio.run(coro)` and every other
        # method in the tree that happens to be called `run` would land here.
        if fn.attr in _SUBPROCESS_CALLS:
            return "list_if_literal_git"
    if isinstance(fn, ast.Name):
        kind = aliases.get(fn.id)
        return kind if kind and not kind.startswith("__") else None
    return None


_PARSE_CACHE: Dict[str, ast.Module] = {}


def _parse_cached(rel: str, source: str) -> ast.Module:
    """Parse once per file per run.

    The tree is walked more than once -- the two production assertions plus the
    allowlist-staleness checks, which re-scan with an allowlist emptied -- and
    this guard runs in CI, where parsing 775 modules repeatedly was the whole
    runtime. Keyed on the source as well as the path so synthetic sources in
    the self-tests never collide with the real file at that path.
    """
    key = f"{rel}\0{hash(source)}"
    tree = _PARSE_CACHE.get(key)
    if tree is None:
        tree = ast.parse(source)
        _PARSE_CACHE[key] = tree
    return tree


def _is_exempt(rel: str) -> bool:
    """True if *rel* is deploy tooling outside the request path."""
    return any(rel == d or rel.startswith(d + "/") for d in _EXEMPT_DIRS)


def _module_path(mod: str) -> Optional[Path]:
    direct = _SRC / (mod.replace(".", "/") + ".py")
    if direct.exists():
        return direct
    pkg = _SRC / mod.replace(".", "/") / "__init__.py"
    return pkg if pkg.exists() else None


def _first_party_imports(path: Path) -> set:
    out: set = set()
    try:
        tree = _parse_cached(str(path), path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            if n.module.startswith("code_indexer."):
                out.add(n.module[len("code_indexer.") :])
        elif isinstance(n, ast.Import):
            for al in n.names:
                if al.name.startswith("code_indexer."):
                    out.add(al.name[len("code_indexer.") :])
    return out


_GOVERNED_CACHE: Optional[List[Path]] = None


def _governed_files() -> List[Path]:
    """Every first-party module the server can reach by import.

    Seeded from _GOVERNED and closed over imports, so moving a file out of
    `server/` no longer moves it out of the seam's scope. Client-only modules
    the server never imports stay out: the CLI runs on a developer's machine
    with their own credentials, and widening the fence to the whole package
    would bury real findings under code the seam was never about.
    """
    global _GOVERNED_CACHE
    if _GOVERNED_CACHE is not None:
        return _GOVERNED_CACHE
    seeds: List[Path] = []
    for top in _GOVERNED:
        root = _SRC / top
        if root.is_dir():
            seeds.extend(sorted(root.rglob("*.py")))
    seen: dict = {}
    stack = list(seeds)
    while stack:
        path = stack.pop()
        rel = str(path.relative_to(_SRC))
        if rel in seen or _is_exempt(rel):
            continue
        seen[rel] = path
        for mod in _first_party_imports(path):
            nxt = _module_path(mod)
            if nxt is not None and str(nxt.relative_to(_SRC)) not in seen:
                stack.append(nxt)
    _GOVERNED_CACHE = [seen[k] for k in sorted(seen)]
    return _GOVERNED_CACHE


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


def _scan_source(
    rel: str, source: str, apply_self_update_allowlist: bool = True
) -> Tuple[List[str], List[str]]:
    """Scan one module's source. Split out from :func:`_scan` so the guard's own
    behaviour can be tested against synthetic sources rather than only against
    whatever the tree happens to contain today."""
    violations: List[str] = []
    unknown: List[str] = []
    tree = _parse_cached(rel, source)
    aliases = _direct_import_aliases(tree)
    spans = _scope_index(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _call_kind(node, aliases)
        target = node
        if kind is None:
            deferred = _partial_spawn(node, aliases)
            if deferred is None:
                continue
            kind, target = deferred
        argv, builders = _git_argv(
            target, kind, _enclosing_scope(tree, spans, node), tree
        )
        if argv is None:
            continue
        where = f"{rel}:{node.lineno}"
        if builders is not None:
            # Justified only when EVERY reaching builder is justified. One
            # allowlisted entry must not clear a call that another, unlisted
            # builder also reaches.
            unlisted = [b for b in builders if (rel, b) not in ALLOWED_UNREADABLE_ARGV]
            if not unlisted:
                continue
            unknown.extend(
                f"{where}  argv built by {b!r}, not statically readable"
                for b in unlisted
            )
            continue
        verb = _verb(argv)
        if verb is None:
            unknown.append(f"{where}  argv not statically readable")
            continue
        # Keep positions: a dynamic element is a placeholder, never dropped,
        # so a global flag cannot swallow the verb during classification.
        concrete = [a if a is not None else "<dynamic>" for a in argv]
        if is_mutating_git(concrete):
            if apply_self_update_allowlist and (
                (rel, verb) in ALLOWED_SELF_UPDATE_MUTATIONS
            ):
                continue
            violations.append(f"{where}  MUTATING: git {verb}")
        elif rel.startswith(_CORE) and (rel, verb) not in ALLOWED_READS:
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


def test_unreadable_allowlist_has_no_stale_entries() -> None:
    """Keep ALLOWED_UNREADABLE_ARGV honest too.

    Mutation testing found that adding a fabricated entry here left all tests
    green: the staleness check below only ever subtracted from ALLOWED_READS.
    An allowlist nobody audits is how a justified exemption outlives the code
    that justified it.

    Scans ONCE with the allowlist emptied rather than re-scanning per entry --
    this runs in CI, and a per-entry rescan made the file take a minute.
    """
    import unittest.mock as _mock

    with _mock.patch.object(sys.modules[__name__], "ALLOWED_UNREADABLE_ARGV", set()):
        _, raw_unknown = _scan()

    stale = {
        (rel, builder)
        for rel, builder in ALLOWED_UNREADABLE_ARGV
        if not any(u.startswith(f"{rel}:") and repr(builder) in u for u in raw_unknown)
    }
    assert not stale, (
        "these ALLOWED_UNREADABLE_ARGV entries no longer match any call site -- "
        "remove them so the allowlist keeps meaning something:\n"
        + "\n".join(f"  - {e}" for e in sorted(map(str, stale)))
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

    def test_sibling_function_assignment_does_not_leak_into_a_runner(self) -> None:
        """A helper's parameter must not adopt another function's variable.

        `cmd`, `command` and `args` are common names. Resolving the module with
        ast.walk() reached into every function body, so a generic runner was
        reported as a bypass at a line that has none -- and the only escape was an
        allowlist entry for code that was never a bypass.
        """
        src = (
            "import subprocess\n"
            "def run_it(cmd):\n"
            "    subprocess.run(cmd)\n"
            "def other():\n"
            '    cmd = ["git", "push", "origin", "main"]\n'
            "    run_it(cmd)\n"
        )
        assert not _violations(src)
        assert not _unknowns(src)

    def test_mutation_hidden_in_one_branch_is_still_caught(self) -> None:
        """Taking the last assignment by line number read the else-branch and
        missed the push. Any reachable literal that mutates wins instead."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    else:\n"
            '        cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_unreadable_branch_is_not_masked_by_a_literal_read(self) -> None:
        """An unreadable branch outranks a readable read, whichever comes first.

        Severity, not line order, picks the candidate. Taking the highest line
        let a literal `git status` in the else-branch stand in for a builder the
        guard cannot read, so whether a possible `git push` got reported came
        down to which branch the author happened to write first.
        """
        unreadable_first = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_push_cmd()\n"
            "    else:\n"
            '        cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        unreadable_last = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "status"]\n'
            "    else:\n"
            "        cmd = build_push_cmd()\n"
            "    subprocess.run(cmd)\n"
        )
        for src in (unreadable_first, unreadable_last):
            unknowns = _unknowns(src)
            assert unknowns, f"unreadable branch masked in:\n{src}"
            # Named by ITS OWN builder: ALLOWED_UNREADABLE_ARGV is keyed on the
            # builder, so borrowing a different candidate's label would make a
            # justified allowlist entry silently fail to match.
            assert any("build_push_cmd" in u for u in unknowns), unknowns

    def test_mutating_literal_outranks_an_unreadable_branch(self) -> None:
        """Severity order is mutating > unreadable > last read: a command we can
        prove mutates stays the violation it is, rather than being softened into
        an unknown by an unreadable sibling branch."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_something()\n"
            "    else:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_aliased_varargs_import_is_not_read_as_list_style(self) -> None:
        """`from asyncio import create_subprocess_exec as cse` is still varargs.

        Deciding the convention from the LOCAL name made `cse` unknown to
        _ASYNC_EXEC, so the call was read as list-style: argv[0] became the whole
        command and a varargs `git push` scanned as clean.
        """
        src = (
            "from asyncio import create_subprocess_exec as cse\n"
            "async def f():\n"
            '    await cse("git", "push", "origin", "main")\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_aliased_list_style_import_still_resolves(self) -> None:
        """The same mapping must not mis-file `from subprocess import run as r`
        as varargs -- that would take argv[0] as the verb and miss everything."""
        src = (
            "from subprocess import run as r\n"
            "def f():\n"
            '    r(["git", "push", "origin", "main"])\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_assignment_inside_a_match_case_reaches_the_call(self) -> None:
        """match/case arms are branches of the scope like if/else.

        They hang off `cases` rather than `body`/`orelse`, so the generic field
        walk dropped them and an argv built in a case arm was invisible.
        """
        src = (
            "import subprocess\n"
            "def f(mode):\n"
            "    match mode:\n"
            '        case "sync":\n'
            '            cmd = ["git", "push", "origin", "main"]\n'
            "        case _:\n"
            '            cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_allowlisted_builder_does_not_clear_an_unlisted_sibling(self) -> None:
        """One justified builder must not speak for an unjustified one.

        `claude_invoker.py` really does have `_build_claude_command` allowlisted.
        Collapsing the reaching builders to a single label meant that whichever
        one happened to be visited first decided the call: if the allowlisted one
        won, the site was skipped and the unlisted builder -- which could be
        building `git push` -- was never reported. Both orders are asserted
        because _statements_of pops its stack, so "first" tracks source layout.
        """
        rel = "server/services/claude_invoker.py"
        assert (rel, "_build_claude_command") in ALLOWED_UNREADABLE_ARGV
        allowed_first = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = _build_claude_command()\n"
            "    else:\n"
            "        cmd = build_push_cmd()\n"
            "    subprocess.run(cmd)\n"
        )
        allowed_last = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_push_cmd()\n"
            "    else:\n"
            "        cmd = _build_claude_command()\n"
            "    subprocess.run(cmd)\n"
        )
        for src in (allowed_first, allowed_last):
            _, unknown = _scan_source(rel, src)
            assert any("build_push_cmd" in u for u in unknown), (
                f"unlisted builder masked by the allowlisted sibling:\n{src}"
            )
            assert not any("_build_claude_command" in u for u in unknown), (
                f"the allowlisted builder must stay justified: {unknown}"
            )

    def test_every_unlisted_builder_is_reported(self) -> None:
        """Two unlisted builders reaching one call are two findings, not one --
        silencing the pair takes two justifications, not whichever is reported."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_a()\n"
            "    else:\n"
            "        cmd = build_b()\n"
            "    subprocess.run(cmd)\n"
        )
        unknown = _unknowns(src)
        assert any("build_a" in u for u in unknown), unknown
        assert any("build_b" in u for u in unknown), unknown


class TestGuardFailsClosed:
    """The contract: anything the guard cannot statically read is REPORTED.

    Six review rounds each closed the one shape the reviewer named, and an
    adversarial pass then found 41 more. These tests pin the CLASSES instead,
    because the recurring defect was never a missing shape -- it was
    `_git_argv` returning ``(None, None)``, which `_scan_source` cannot tell
    apart from "this call is not git at all". Silence is the bug.
    """

    def test_bindings_other_than_assignment_are_not_mistaken_for_parameters(
        self,
    ) -> None:
        """A name bound by any construct is bound; only a PARAMETER is a
        caller's business. Reading only Assign/AnnAssign made every other
        binding form look unbound, and unbound was treated as "generic runner,
        skip" -- so argv bound by a for-loop, a with-item or a walrus reached
        subprocess.run with no report at all."""
        shapes = {
            "for": (
                "import subprocess\n"
                "def f():\n"
                '    for cmd in [["git", "push", "origin", "main"]]:\n'
                "        subprocess.run(cmd)\n"
            ),
            "with": (
                "import subprocess\n"
                "def f(ctx):\n"
                "    with ctx as cmd:\n"
                "        subprocess.run(cmd)\n"
            ),
            "walrus": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run(cmd := ["git", "push", "origin", "main"])\n'
            ),
            "tuple-unpack": (
                "import subprocess\n"
                "def f():\n"
                '    cmd, env = (["git", "push", "origin", "main"], {})\n'
                "    subprocess.run(cmd)\n"
            ),
            "except-as": (
                "import subprocess\n"
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except OSError as cmd:\n"
                "        subprocess.run(cmd)\n"
            ),
            "comprehension": (
                "import subprocess\n"
                "def f(xs):\n"
                "    [subprocess.run(cmd) for cmd in xs]\n"
            ),
        }
        for name, src in shapes.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: argv reached a spawn with no report"

    def test_a_parameter_shadowing_a_module_constant_is_not_the_constant(
        self,
    ) -> None:
        """Pins `_parameters_of`, which mutation testing found unpinned.

        The module-level fallback exists so `_PUSH = ["git","push"]` used inside
        a function is still read. But a PARAMETER of the same name is the
        caller's value, not the module's -- without consulting the signature the
        fallback adopts the constant and reports a mutation at a generic runner
        that never runs one.
        """
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def run_it(_PUSH):\n"
            "    subprocess.run(_PUSH)\n"
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"a parameter was read as the module constant: {v + u}"

    def test_a_module_constant_is_still_read(self) -> None:
        """Negative control for the test above: the fallback must still work."""
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def f():\n"
            "    subprocess.run(_PUSH)\n"
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert any("MUTATING: git push" in x for x in v), v

    def test_a_real_parameter_is_still_skipped(self) -> None:
        """Negative control for the test above. A generic runner whose argv IS
        a parameter must stay silent -- its callers are scanned on their own,
        and flagging it would make every subprocess helper a permanent
        violation while catching nothing. This is the one legitimate silence,
        and it must be decided from the signature, not inferred from the
        absence of an assignment."""
        src = "import subprocess\ndef run_it(cmd):\n    subprocess.run(cmd)\n"
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"generic runner wrongly reported: {v + u}"

    def test_append_cannot_smuggle_a_mutating_subverb(self) -> None:
        """THE critical one, and it refutes this file's own docstring.

        The append/extend exemption reasoned that those only add to the END, so
        the program and verb survive and the literal stays safe to classify.
        True -- and incomplete. For a dual-form verb the SUB-verb decides:
        `is_mutating_git(["git","remote"])` is False but
        `is_mutating_git(["git","remote","add",...])` is True. So the exempted
        append is exactly where the mutation arrives, and `git remote` is an
        allowlisted read in lifespan.py. This is the house idiom --
        storage/shared/git_backend.py:481 is literally `cmd = ["git","remote"]`
        followed by a conditional append.
        """
        for rel, head, tail in (
            ("server/startup/lifespan.py", '["git", "remote"]', '["add", n, u]'),
            (
                "server/services/git_operations_service.py",
                '["git", "config"]',
                '["--unset", k]',
            ),
            (
                "server/repositories/activated_repo_manager.py",
                '["git", "branch"]',
                '["-D", n]',
            ),
        ):
            src = (
                "import subprocess\n"
                "def f(n, u, k):\n"
                f"    cmd = {head}\n"
                f"    cmd.extend({tail})\n"
                "    subprocess.run(cmd)\n"
            )
            v, u_ = _scan_source(rel, src)
            assert v or u_, f"{rel}: mutating sub-verb smuggled past {head}"

    def test_append_still_trusts_an_unambiguous_read(self) -> None:
        """The exemption exists for a reason and must survive: a verb with no
        mutating form cannot be turned into one by appending."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "status"]\n'
            '    cmd.append("--short")\n'
            "    subprocess.run(cmd)\n"
        )
        _, u = _scan_source("server/repositories/activated_repo_manager.py", src)
        assert not u, f"an unambiguous read was made unreadable: {u}"

    def test_keyword_argv_is_scanned(self) -> None:
        """`if not node.args: return None, None` skipped the entire call.
        `args=` is ordinary Python and appears in governed code."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    subprocess.run(args=["git", "push", "origin", "main"], check=True)\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert v or u

    def test_spawn_is_recognised_by_function_not_receiver(self) -> None:
        """`_call_kind` required a receiver literally named `subprocess`, and
        the alias map was built from ImportFrom only -- so `import subprocess
        as sp` was invisible, as was a stashed `self._sp`."""
        for name, src in {
            "import-as": (
                "import subprocess as sp\n"
                "def f():\n"
                '    sp.run(["git", "push", "origin", "main"])\n'
            ),
            # An unidentified receiver is scanned, but only a LITERAL git argv
            # is reported there -- see _call_kind. Reporting unreadable argv on
            # any receiver named `run` flagged every `asyncio.run(coro)` in the
            # tree, which is the cry-wolf failure that gets a guard deleted.
            "attribute-receiver": (
                "class C:\n"
                "    def f(self):\n"
                '        self._sp.run(["git", "push", "origin", "main"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: spawn not recognised"

    def test_dynamic_program_is_unreadable_not_invisible(self) -> None:
        """The guard only failed closed AFTER proving argv[0] was literally
        "git". A dynamic verb was caught; a dynamic PROGRAM was silently
        skipped, because `argv[0] != "git"` is indistinguishable from "not a
        git call". diagnostics_service.py:631 has this shape today."""
        for name, src in {
            "list": (
                "import subprocess\n"
                "def f(p):\n"
                '    subprocess.run([p, "push", "origin", "main"])\n'
            ),
            "varargs": (
                "import asyncio\n"
                "async def f(p):\n"
                '    await asyncio.create_subprocess_exec(p, "push", "origin")\n'
            ),
            "hoisted-const": (
                "import subprocess\n"
                'GIT = "git"\n'
                "def f():\n"
                '    subprocess.run([GIT, "push", "origin", "main"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: dynamic program silently skipped"

    def test_starred_and_inline_expression_argv_are_unreadable(self) -> None:
        """Binding the expression to a name was reported; passing the SAME
        expression inline was not. Deleting one line made a bypass invisible."""
        for name, src in {
            "starred": (
                "import subprocess\n"
                "def f():\n"
                '    a = (["git", "push", "origin", "main"],)\n'
                "    subprocess.run(*a)\n"
            ),
            "inline-concat": (
                "import subprocess\n"
                'BASE = ["git"]\n'
                "def f():\n"
                '    subprocess.run(BASE + ["push", "origin", "main"])\n'
            ),
            "inline-subscript": (
                'import subprocess\ndef f(cmds):\n    subprocess.run(cmds["push"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: inline argv silently skipped"

    def test_shell_and_other_spawn_families_are_seen(self) -> None:
        """A string argv and the os.* spawn family were entirely unknown."""
        for name, src in {
            "shell-string": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run("git push origin main", shell=True)\n'
            ),
            "sh-c": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run(["sh", "-c", "git push origin main"])\n'
            ),
            "os-system": (
                'import os\ndef f():\n    os.system("git push origin main")\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: spawn family not seen"


class TestGuardDoesNotCryWolf:
    """A guard that reports a command a line cannot run gets disabled.

    These are the cost of the round-5 severity ordering: it picks the worst
    candidate reaching a call with no notion of which assignments actually
    DOMINATE that call.
    """

    def test_a_mutation_in_one_branch_does_not_taint_the_other(self) -> None:
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "        subprocess.run(cmd)\n"
            "    else:\n"
            '        cmd = ["tar", "xf", "a.tar"]\n'
            "        subprocess.run(cmd)\n"
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert len(v) == 1, f"the tar call was blamed for the push: {v}"

    def test_a_nested_def_does_not_poison_the_outer_call(self) -> None:
        """`_prefix_rewritten_after` still uses ast.walk, so it descends into
        nested defs -- the exact leak `_statements_of` was written to stop."""
        src = (
            "import subprocess\n"
            "def f(repo):\n"
            '    cmd = ["git", "rev-parse", "HEAD"]\n'
            "    def helper():\n"
            '        inner = ["a", "b"]\n'
            '        inner.insert(0, "c")\n'
            "    subprocess.run(cmd)\n"
        )
        _, u = _scan_source("server/routers/git.py", src)
        assert not u, f"an unrelated nested local poisoned the outer call: {u}"


def test_scan_is_live() -> None:
    """Liveness control for `_scan()` itself.

    Mutation testing found that making `_scan()` return ``([], [])`` -- the
    guard going completely blind -- left all 26 self-tests passing, because the
    two production assertions are vacuous while the tree is clean. Nothing
    proved the live scan path still executed. This asserts it does real work.
    """
    files = _governed_files()
    assert len(files) > 50, f"the governed tree should be substantial, got {len(files)}"
    scanned = 0
    for path in files:
        rel = str(path.relative_to(_SRC))
        _scan_source(rel, path.read_text(encoding="utf-8"))
        scanned += 1
    assert scanned == len(files)
    # and the production path must still classify a known bypass
    v, _ = _scan_source(
        "server/services/synthetic.py",
        'import subprocess\ndef f():\n    subprocess.run(["git", "push", "o", "m"])\n',
    )
    assert any("MUTATING: git push" in x for x in v), "the scan path is not classifying"


class TestRemainingBypassClasses:
    """The tail an adversarial pass found after the fail-closed rewrite."""

    def test_partial_bound_argv_is_seen(self) -> None:
        """`functools.partial(subprocess.run, argv)` defers the spawn but binds
        argv now. The call whose func is `partial` is not itself a spawn, so the
        site was invisible -- and `partial` appears 35x in governed roots."""
        for src in (
            "import functools, subprocess\n"
            "def f():\n"
            '    r = functools.partial(subprocess.run, ["git", "push", "o", "m"])\n'
            "    r()\n",
            "from functools import partial\n"
            "import subprocess\n"
            "def f():\n"
            '    r = partial(subprocess.run, ["git", "push", "o", "m"])\n'
            "    r()\n",
        ):
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"partial-bound argv not seen:\n{src}"

    def test_star_import_binds_the_spawn(self) -> None:
        """`from subprocess import *` binds `run` with no alias to record."""
        src = (
            "from subprocess import *\n"
            "def f():\n"
            '    run(["git", "push", "origin", "main"])\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert v or u

    def test_an_assignment_later_in_the_loop_reaches_the_call(self) -> None:
        """A loop back-edge makes a textually-later assignment reachable.

        Resolution stops at `line >= call.lineno`, which is sound for straight
        line code and wrong inside a loop: on iteration 2 the call runs the argv
        assigned at the BOTTOM of iteration 1. Reported as the harmless
        `rev-parse` while the push was never mentioned.
        """
        src = (
            "import subprocess\n"
            "def f(items):\n"
            '    cmd = ["git", "rev-parse", "HEAD"]\n'
            "    for i in items:\n"
            "        subprocess.run(cmd)\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert any("push" in x for x in v + u), f"loop back-edge missed: {v + u}"

    def test_a_statically_dead_branch_is_not_reported(self) -> None:
        """`if False:` cannot run, so blaming its contents names a command the
        program will never execute."""
        src = (
            "import subprocess\n"
            "def f():\n"
            "    if False:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    else:\n"
            '        cmd = ["cidx", "index"]\n'
            "    subprocess.run(cmd)\n"
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"dead branch reported: {v + u}"


class TestTheFenceIsReachabilityNotDirectory:
    """What the seam governs is what the SERVER can reach, not two directories.

    `_GOVERNED` named `server/` and `global_repos/`, so a raw mutating git call
    one directory outside was invisible with no allowlist entry needed --
    `services/git_topology_service.py` is imported by
    `server/mcp/handlers/repos.py` and was never scanned. A directory boundary
    pretending to be a reachability boundary is defeated by moving a file.
    """

    def test_a_module_reachable_only_by_import_is_governed(self) -> None:
        governed = {str(p.relative_to(_SRC)) for p in _governed_files()}
        assert "services/git_topology_service.py" in governed, (
            "a module the server imports must be scanned, wherever it lives"
        )

    def test_client_only_modules_stay_out_of_scope(self) -> None:
        """The CLI runs on a developer's machine with their own credentials, so
        client-side git is deliberately not the seam's business. Widening the
        fence to the whole package would bury real findings."""
        governed = {str(p.relative_to(_SRC)) for p in _governed_files()}
        assert "sync/conflict_resolution.py" not in governed

    def test_exempt_dirs_match_a_path_prefix_not_any_component(self) -> None:
        """`_EXEMPT_DIRS` matched ANY path component, so a future
        `server/repositories/scripts/` would be exempt for free."""
        assert not _is_exempt("server/repositories/scripts/thing.py")
        assert _is_exempt("scripts/thing.py")


class TestSelfUpdateMutationsAreJustifiedPerCall:
    """auto_update's git calls are exempt per CALL, not per directory.

    They operate on the server's OWN source tree and on hnswlib -- not on a
    tenant golden repo -- so git-mcp, which exists to hold golden-repo
    credentials, is the wrong destination for them. That is a real
    justification, and it belongs against each call so a NEW mutating call in
    the same directory still fails until someone justifies it too.
    """

    def test_every_self_update_entry_is_live(self) -> None:
        live = set()
        for path in _governed_files():
            rel = str(path.relative_to(_SRC))
            violations, _ = _scan_source(
                rel, path.read_text(encoding="utf-8"), apply_self_update_allowlist=False
            )
            for v in violations:
                for arel, averb in ALLOWED_SELF_UPDATE_MUTATIONS:
                    if v.startswith(f"{arel}:") and v.endswith(f"git {averb}"):
                        live.add((arel, averb))
        stale = ALLOWED_SELF_UPDATE_MUTATIONS - live
        assert not stale, f"stale self-update exemptions: {sorted(map(str, stale))}"

    def test_an_unjustified_mutation_in_the_same_dir_still_fails(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    subprocess.run(["git", "push", "origin", "main"])\n'
        )
        v, _ = _scan_source("server/auto_update/deployment_executor.py", src)
        assert any("MUTATING: git push" in x for x in v), v


class TestLiteralHeadConcatenationIsReadable:
    """`["git", "log", ...] + files` carries its verb in the literal head.

    Reporting it as opaque pushed real, classifiable reads into
    ALLOWED_UNREADABLE_ARGV, where they stop being checked against the
    classifier at all -- the allowlist is keyed on the BUILDER, so an entry for
    `<concatenation>` in a file would also cover a future
    `["git","push"] + args` in it. Classifying is strictly safer than exempting.
    """

    def test_a_concatenated_read_is_classified_not_exempted(self) -> None:
        src = (
            "import subprocess\n"
            "def f(files):\n"
            '    subprocess.run(["git", "log", "--format=%H", "--"] + files)\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not u, f"should be readable, not opaque: {u}"
        assert any("unlisted read: git log" in x for x in v), v

    def test_a_concatenated_mutation_is_caught(self) -> None:
        src = (
            "import subprocess\n"
            "def f(args):\n"
            '    subprocess.run(["git", "push"] + args)\n'
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert any("MUTATING: git push" in x for x in v), v

    def test_a_dual_form_head_stays_opaque(self) -> None:
        """The appended tail is exactly where `git remote`'s sub-verb arrives,
        so the same rule that governs append/extend must govern `+`."""
        src = (
            "import subprocess\n"
            "def f(args):\n"
            '    subprocess.run(["git", "remote"] + args)\n'
        )
        v, u = _scan_source("server/startup/lifespan.py", src)
        assert v or u, "a dual-form verb completed by the tail must not read as clean"
        assert not any("unlisted read" in x for x in v), v
