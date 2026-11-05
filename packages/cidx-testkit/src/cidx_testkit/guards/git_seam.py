"""The git-seam guard's engine: find raw git spawns and classify their argv.

The policy it enforces (moved here from the core's
``tests/unit/server/test_git_seam_no_raw_subprocess.py``, which keeps the
test entry point and the code_indexer-specific allowlists): a git MUTATION
that does not go through the core's chokepoint (``run_git_command``, which
delegates mutations to git-mcp) is a violation wherever the server can reach
it; a raw spawn whose argv cannot be read statically is reported, because it
could become a mutation unnoticed; reads in the request-path core must be
justified one by one.

What is generic -- reading argv out of the AST (literals, assignments that
reach the call, concatenations, builders), recognising every spawn shape, and
walking the server's reachable import closure across distributions
(:class:`~cidx_testkit.guards.source_tree.SourceTree`) -- lives here, so any
distribution (a backend, a feature) can be checked by the same engine with its
own :class:`GitSeamPolicy`. The classifier that decides whether an argv
mutates is the core's own (``is_mutating_git``): pass it to
:func:`configure_classifier`, so the guard and the runtime never disagree.
"""

from __future__ import annotations

import ast
import shlex
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from cidx_testkit.guards.source_tree import SourceTree


def _unconfigured(cmd: Sequence[str]) -> bool:
    raise RuntimeError(
        "the git-seam guard has no classifier: call configure_classifier() "
        "with the core's is_mutating_git first"
    )


#: The core's ``is_mutating_git``; set with :func:`configure_classifier`.
is_mutating_git: Callable[[Sequence[str]], bool] = _unconfigured


def configure_classifier(classifier: Callable[[Sequence[str]], bool]) -> None:
    """Use *classifier* (argv -> mutates?) for every scan."""
    global is_mutating_git
    is_mutating_git = classifier


@dataclass(frozen=True)
class GitSeamPolicy:
    """What a tree is allowed to do with raw git.

    Attributes:
        allowed_reads: ``(key, verb)`` raw reads justified in the core.
        allowed_unreadable_argv: ``(key, builder)`` argv builders asserted to
            never produce a git command.
        allowed_self_update_mutations: ``(key, verb)`` mutations of the
            server's OWN checkout, justified per call.
        core_prefixes: key prefixes where every raw read must be listed.
    """

    allowed_reads: FrozenSet[Tuple[str, str]] = frozenset()
    allowed_unreadable_argv: FrozenSet[Tuple[str, str]] = frozenset()
    allowed_self_update_mutations: FrozenSet[Tuple[str, str]] = frozenset()
    core_prefixes: Tuple[str, ...] = dataclasses.field(default=())


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
    is_git_literal = best is not None and bool(best) and best[0] == "git"
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
    if (
        best is not None
        and is_git_literal
        and _tail_extended_after(name, scope, best_at, before_line)
    ):
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


def _in_block(node: Optional[ast.AST], body: List[ast.AST]) -> bool:
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


def _loop_carried(
    assign_node: ast.AST, call: Optional[ast.Call], scope: ast.AST
) -> bool:
    """True if a textually-later assignment can still supply *call*'s argv.

    Resolution stops at the call's line, which is sound for straight-line code
    and wrong inside a loop: on the second iteration the call runs the argv
    assigned at the BOTTOM of the first.
    """
    for _owner, body in _enclosing_loops(scope):
        if _in_block(assign_node, body) and _in_block(call, body):
            return True
    return False


def _reaches(
    assign_line: int, assign_node: ast.AST, call: Optional[ast.Call], scope
) -> bool:
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
            owner_end: int = (
                getattr(owner, "end_lineno", None) or getattr(owner, "lineno", 0) or 0
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
        if start <= getattr(node, "lineno", 0) <= end:
            best = f
    return best


def scan_source(
    rel: str,
    source: str,
    policy: GitSeamPolicy,
    apply_self_update_allowlist: bool = True,
) -> Tuple[List[str], List[str]]:
    """Scan one module's source against *policy*: ``(violations, unknown)``.

    Split out from the tree scan so a guard's own behaviour can be tested
    against synthetic sources rather than only against whatever the tree
    happens to contain today."""
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
            unlisted = [
                b for b in builders if (rel, b) not in policy.allowed_unreadable_argv
            ]
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
                (rel, verb) in policy.allowed_self_update_mutations
            ):
                continue
            violations.append(f"{where}  MUTATING: git {verb}")
        elif (
            rel.startswith(policy.core_prefixes)
            and (
                rel,
                verb,
            )
            not in policy.allowed_reads
        ):
            violations.append(f"{where}  unlisted read: git {verb}")
    return violations, unknown


def is_exempt(key: str, exempt: Sequence[str]) -> bool:
    """True if *key* lies under one of the *exempt* path prefixes."""
    return any(key == d or key.startswith(d + "/") for d in exempt)


def governed_files(
    tree: SourceTree, seeds: Sequence[str], exempt: Sequence[str] = ()
) -> List[Path]:
    """Every module the server can reach by import from *seeds*, across the
    tree's distributions, skipping *exempt* prefixes (deploy tooling).

    Absolute imports only, without ancestor packages: the reachability rule
    this guard was written and allowlisted against."""
    return list(
        tree.closure(
            seeds, exempt, relative=False, imported_names=False, ancestors=False
        )
    )


def scan_tree(
    tree: SourceTree,
    seeds: Sequence[str],
    policy: GitSeamPolicy,
    exempt: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """``(violations, unknown_argv)`` across every governed file."""
    violations: List[str] = []
    unknown: List[str] = []
    for path in governed_files(tree, seeds, exempt):
        v, u = scan_source(tree.key(path), path.read_text(encoding="utf-8"), policy)
        violations.extend(v)
        unknown.extend(u)
    return violations, unknown
