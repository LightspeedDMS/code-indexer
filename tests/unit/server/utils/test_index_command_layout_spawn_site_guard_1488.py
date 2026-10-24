"""Repo-wide, discovery-driven structural guard: every SERVER-context
`cidx index` command CONSTRUCTION is routed through
``append_server_layout_args`` (Story #1488).

Story #1488 makes the CLI/daemon default new-collection chunk-storage layout
SHARDED_JSON; the server states CHUNKS_DB explicitly by stamping every
server-context `cidx index` command with --new-collection-layout=chunks_db via
the single shared helper ``append_server_layout_args``. If a server spawn site
builds a `cidx index` command WITHOUT routing it through that helper, the child
silently reverts to the CLI SHARDED_JSON default, violating the server's
explicit-CHUNKS_DB contract (AC1).

Why this guard was REWRITTEN AGAIN (Codex Finding D3, LOW):
-----------------------------------------------------------
The prior version scanned a HARD-CODED six-module list
(``_SERVER_SPAWN_SITE_MODULES``). A brand-new server module that constructs an
unwrapped `["cidx", "index", ...]` list was INVISIBLE to the guard unless a
human ALSO remembered to append it to that list -- so the test did NOT actually
guarantee that a new unwrapped server spawn site fails CI. That is the exact
class of gap this guard exists to close.

This rewrite mirrors the repo-wide-discovery-with-explicit-exceptions pattern of
``tests/unit/storage/test_temporal_pg_env_wiring_site_enumeration_guard_1313.py``.
Discovery -- not a fixed list -- is now the AUTHORITY:

  1. Walk EVERY ``*.py`` under ``src/code_indexer/`` (skipping test files),
     AST-parse each, and collect every `cidx index` list-literal CONSTRUCTION
     (a ``list`` whose first two elements are the string constants
     ``"cidx"``, ``"index"``).
  2. Classify each discovered file by path:
       * SERVER-context  -> path under ``server/`` or ``global_repos/``. Every
         construction here MUST route through ``append_server_layout_args``.
       * CLI-side        -> an EXPLICIT, per-file justified entry in
         ``_CLI_SIDE_EXCLUSIONS`` (the standalone CLI default path, which
         intentionally uses the CLI SHARDED_JSON default and must NOT wrap).
       * anything else   -> FAILS loudly: a new `cidx index` construction has
         appeared in an unclassified location and a human must consciously
         decide whether it is a server spawn (wrap it + widen the roots) or a
         CLI-side spawn (justify it in the exclusions).

Consequence: adding a NEW server module with an unwrapped `cidx index` spawn
FAILS ``test_every_server_context_cidx_index_spawn_is_wrapped`` automatically --
no module-list edit required. The known-module table below is a redundant
drift/non-vacuity ANCHOR (it forces conscious acknowledgement of any new site),
NEVER the security gate; the wrap check is guaranteed by discovery alone.

Routing is recognised in both real shapes: a direct positional argument
(``append_server_layout_args(["cidx", "index", ...])``) or assign-then-wrap
(``cmd = ["cidx", "index", ...]; cmd = append_server_layout_args(cmd)`` -- the
shape used in ``refresh_scheduler.py``).

Why the assign-then-wrap detection is SCOPE-CORRELATED (Codex Finding, MEDIUM):
------------------------------------------------------------------------------
A prior version detected assign-then-wrap by gathering the set of variable
NAMES passed to ``append_server_layout_args`` across the ENTIRE module, then
marking EVERY `cidx index` list assigned to ANY variable whose name was in that
set as "wrapped" -- with no function/scope correlation. That produced a real
FALSE-NEGATIVE: an UNWRAPPED ``command = ["cidx", "index", ...];
subprocess.run(command)`` in one function was silently certified as wrapped
merely because a DIFFERENT function in the same module happened to pass a
variable named ``command`` to the helper. The module-wide name match cannot tell
those two ``command`` variables apart.

The fix (implemented by ``_scope_wrapped_assign_ids``): assign-then-wrap is
recognised ONLY when the list-literal's target variable is passed to
``append_server_layout_args`` WITHIN THE SAME lexical scope (the same
``FunctionDef``/``AsyncFunctionDef``, or module scope). Variable names are NEVER
matched across function boundaries. The direct-positional-arg shape
(``append_server_layout_args(["cidx", "index", ...])``) is unambiguous on its own
and remains recognised wherever it appears. ``test_wrap_detection_is_scope_
correlated_not_module_wide_name_match`` proves the exact false-negative is now
caught.

Why detection is ALSO PER-BINDING and BRANCH-AWARE (Codex Finding, MEDIUM):
---------------------------------------------------------------------------
Scope correlation alone was still not enough. WITHIN one function, a prior
version gathered ALL helper-argument names for the whole scope, then certified
EVERY same-named `cidx index` list assignment in that scope as wrapped -- with no
per-binding correlation. That produced a second real FALSE-NEGATIVE: a name
genuinely wrapped ONCE, then REBOUND to a NEW, unwrapped ``["cidx", "index", ...]``
list and spawned, was still certified wrapped merely because the SAME name was
wrapped earlier in the same function::

    command = ["cidx", "index", "--fts"]          # binding A -- wrapped below
    command = append_server_layout_args(command)  # consumes A
    subprocess.run(command)
    command = ["cidx", "index", "--clear"]         # binding B -- UNWRAPPED
    subprocess.run(command)                        # ...but certified by A's wrap

The fix (``_process_block`` + ``_process_stmt`` + ``_apply_leaf``) is a small,
branch-aware REACHING-DEFINITIONS pass over each scope's own statement tree.
Live state maps ``var_name -> {id() of each `cidx index` list literal currently
reaching that name}``; a `cidx index` construction is marked wrapped only when
its EXACT binding is the one an ``append_server_layout_args(<Name>)`` call
consumes. Within a straight-line block a reassignment KILLS the prior binding
(so binding B above is correctly flagged). But mutually-exclusive branches are
NOT treated as sequential kills: sibling ``if``/``else`` (and ``except``) binds
of the same name are UNIONed at the join, so real ``refresh_scheduler.py`` code
of the shape::

    if needs_reconcile:
        index_command = ["cidx", "index", "--fts", "--reconcile", "..."]  # A
    else:
        index_command = ["cidx", "index", "--fts", "..."]                 # B
    index_command = append_server_layout_args(index_command)              # wraps A AND B

correctly certifies BOTH alternative bindings (a naive line-order kill would
have let B's assignment falsely "kill" A and flagged A as unwrapped). Loops,
``with`` and ``try``/``finally`` are handled with the same reaching-definitions
merge semantics.
``test_wrap_detection_tracks_reassignment_per_binding_in_statement_order`` proves
the reassignment false-negative is caught; the real-tree
``test_every_server_context_cidx_index_spawn_is_wrapped`` proves the if/else
alternatives stay clean.

Why detection is a TRUE spawn-site reaching-definitions check (Codex Finding,
test-robustness):
---------------------------------------------------------------------------
Scope + per-binding + branch awareness was STILL not a true reaching-definitions
check AT THE SPAWN POINT. The prior pass marked a construction "wrapped" globally
the moment ANY ``append_server_layout_args`` consumed its binding, then compared
raw list definitions against that global set -- it never inspected the actual
spawn statement, nor required EVERY reaching definition at the spawn to be
wrapped. Two false-negative classes survived::

    # (1) spawn BEFORE wrap -- executed raw, then retroactively "certified":
    command = ["cidx", "index", "--clear"]
    subprocess.run(command)                       # RAW at this statement
    command = append_server_layout_args(command)  # later wrap -> false negative

    # (2) one-branch-only wrap -- raw whenever the branch is not taken:
    command = ["cidx", "index", "--clear"]
    if flag:
        command = append_server_layout_args(command)
    subprocess.run(command)                       # RAW when flag is False

The fix carries WRAPPEDNESS inside the reaching state (``_Reaching`` maps a name
to a set of ``(construction-id, wrapped?)`` pairs) and adds an explicit SPAWN/USE
inspection (``_scan_expr`` + ``_SPAWN_CALLEES``): walking each scope in statement
order, a raw ``var = ["cidx", "index", ...]`` binds UNWRAPPED, ``var =
append_server_layout_args(var)`` UPGRADES the current binding(s) to wrapped, and
at every recognised execution call (``subprocess.run`` / ``Popen`` /
``check_call`` / ``check_output`` and this project's popen-progress / telemetry
runner wrappers -- inventoried from the real server spawn sites) a Name argument
is a violation if ANY of its reaching definitions is unwrapped AT THAT statement.
Branch joins UNION per-branch wrappedness, so a wrap on only ONE branch leaves an
unwrapped reaching state at the join (class 2 caught), while a wrap AFTER the join
over mutually-exclusive branch bindings stays clean (the real
``refresh_scheduler.py`` shape). Both classes are reported at the CONSTRUCTION's
line (the list literal that must be wrapped before it is executed), consistent
with every existing guard test. ``test_wrap_detection_flags_spawn_before_wrap``
and ``test_wrap_detection_flags_one_branch_only_wrap`` prove both classes are now
caught; ``test_wrap_detection_wrap_then_spawn_single_binding_is_clean`` and
``test_wrap_detection_if_else_bind_then_wrap_after_join_is_clean`` prove the
positives stay green, and the real-tree wrap check still passes because every
real server construction is wrapped on every path that reaches each of its
spawns.

Why detection PROPAGATES aliases and inspects KEYWORD spawn args (Codex Finding,
test-robustness):
---------------------------------------------------------------------------
The reaching-definitions pass above still had two residual blind spots that let a
raw `cidx index` command reach a spawn while the guard certified it wrapped::

    # (3) ALIAS-state loss -- the raw binding is spawned through an alias:
    cmd = ["cidx", "index", "--clear"]
    raw_alias = cmd                        # alias captures the UNWRAPPED binding
    cmd = append_server_layout_args(cmd)   # wraps cmd, NOT raw_alias
    subprocess.run(raw_alias)              # raw_alias is spawned RAW

    # (4) KEYWORD spawn arg -- the raw binding is spawned by keyword:
    command = ["cidx", "index", "--clear"]
    subprocess.run(args=command)                  # RAW via keyword
    command = append_server_layout_args(command)  # wrapped too late

Class 3 slipped through because ``_binding_for_value`` treated a bare-``Name``
assignment (``raw_alias = cmd``) as a KILL -- it returned ``None`` for anything
that was not a `cidx index` literal or a helper call -- so the alias never
carried the source's unwrapped reaching definition to the spawn. The fix makes
``_binding_for_value`` PROPAGATE a ``target = source_name`` assignment: it copies
``source_name``'s current reaching set WITH each binding's wrapped/unwrapped state
(an untracked source still kills the target). A subsequent
``target = <new list literal>`` still KILLS/rebinds as before. Class 4 slipped
through because ``_scan_expr``'s spawn-site inspection walked only ``sub.args``
(positional Name args). The fix ALSO inspects each keyword value (``kw.value``
when it is a Name), so a command-carrying keyword argument is a violation exactly
like a positional one when any reaching definition of it is unwrapped at the
spawn. ``test_wrap_detection_flags_alias_of_unwrapped_binding_spawned`` and
``test_wrap_detection_flags_keyword_arg_unwrapped_spawn`` prove both classes are
now caught; ``test_wrap_detection_alias_of_wrapped_binding_is_clean`` and
``test_wrap_detection_keyword_arg_wrapped_spawn_is_clean`` prove the positives
(alias/keyword of a genuinely wrapped binding) stay green.

Known limitation (documented, not the primary concern): a `cidx index` command
assembled dynamically rather than as an exact ``["cidx", "index", ...]`` list
literal (e.g. built by ``.append``/``+=`` from a bare ``["cidx"]``) is not
surfaced by discovery at all. The primary defect this guard closes is the
scope-correlation false-negative above; dynamic assembly is a separate, narrower
evasion not currently exercised by any real spawn site.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Set, Tuple

import pytest
from typing_extensions import TypeGuard

_TARGET_FUNCTION = "append_server_layout_args"

# Repository source root (…/src/code_indexer). This test lives at
# tests/unit/server/utils/, so the repo root is parents[4].
_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "code_indexer"

# Path prefixes (relative to _SRC_ROOT, POSIX form) that define SERVER context.
# The current six server spawn-site modules all live under exactly these two
# roots (server/repositories, server/services, server/mcp/handlers, and
# global_repos). Every `cidx index` construction under these roots MUST wrap.
_SERVER_CONTEXT_ROOTS: Tuple[str, ...] = ("server/", "global_repos/")

# EXPLICIT, per-file justified exclusions: files that DO construct a
# `["cidx", "index", ...]` literal but are the standalone-CLI default path and
# must NOT be required to wrap (they intentionally use the CLI SHARDED_JSON
# new-collection layout). Keys are POSIX paths relative to _SRC_ROOT. Each entry
# is validated by test_cli_side_exclusions_are_specific_justified_and_nonvacuous
# to actually be discovered AND to be non-server-context -- so this set can
# never silently hide a real server spawn, nor accumulate dead entries.
_CLI_SIDE_EXCLUSIONS: Dict[str, str] = {
    "cli_provider_index.py": (
        "Standalone CLI provider-index build/rebuild path "
        "(build_provider_index / rebuild_provider_index): spawns `cidx index` "
        "and `cidx index --clear` via subprocess.run with "
        "build_cidx_subprocess_env(). This IS the CLI default path -- it must "
        "use the CLI SHARDED_JSON new-collection layout, so it deliberately "
        "does NOT stamp --new-collection-layout=chunks_db."
    ),
}

# Files CONSIDERED for exclusion but deliberately NOT listed above, because they
# construct NO discoverable `["cidx", "index", ...]` list literal and are
# therefore never surfaced by discovery in the first place (documented so a
# future reader does not "helpfully" add dead entries):
#   * services/progress_subprocess_runner.py -- a shared helper that RECEIVES a
#     pre-built `command` list; its only `["cidx", "index", ...]` text lives in
#     the module docstring's usage example, which the AST never sees as a list.
#   * cli.py -- the CHILD `cidx index` entrypoint itself (Click command),
#     never a parent that constructs a `["cidx", "index", ...]` argv literal.

# Redundant drift/non-vacuity ANCHOR (NOT the security gate): the complete set
# of server-context spawn-site files currently expected, mapped to the number of
# `cidx index` CONSTRUCTIONS (list literals, NOT helper calls) each contains.
# refresh_scheduler's two `--fts` literals are assign-then-wrapped through ONE
# helper call yet count as 2 constructions here. If discovery diverges from this
# table, a human must consciously confirm every remaining construction is
# wrapped and then update the table.
_KNOWN_SERVER_SPAWN_MODULES: Dict[str, int] = {
    "server/repositories/golden_repo_manager.py": 6,
    "server/services/activated_repo_index_manager.py": 3,
    "server/mcp/handlers/repos.py": 1,
    "server/mcp/handlers/_temporal_index_cmd.py": 1,
    "global_repos/refresh_scheduler.py": 3,
    "server/services/claude_cli_manager.py": 1,
}


# --------------------------------------------------------------------------- #
# AST primitives                                                              #
# --------------------------------------------------------------------------- #
def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_cidx_index_list(node: ast.AST) -> TypeGuard[ast.List]:
    """True for a list literal whose first two elements are the string
    constants "cidx", "index" -- i.e. a `cidx index ...` command construction."""
    return (
        isinstance(node, ast.List)
        and len(node.elts) >= 2
        and _const_str(node.elts[0]) == "cidx"
        and _const_str(node.elts[1]) == "index"
    )


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_helper_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and _callee_name(node.func) == _TARGET_FUNCTION


def _find_spawn_lists(tree: ast.Module) -> List[ast.List]:
    return [node for node in ast.walk(tree) if _is_cidx_index_list(node)]


def _direct_arg_list_ids(tree: ast.Module) -> Set[int]:
    """id() of every list literal passed as a positional arg DIRECTLY to a
    ``append_server_layout_args(...)`` call."""
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if _is_helper_call(node):
            for arg in node.args:
                if isinstance(arg, ast.List):
                    ids.add(id(arg))
    return ids


def _own_scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield every descendant of ``scope`` that belongs to ``scope``'s OWN
    lexical scope -- i.e. do NOT descend into nested ``FunctionDef`` /
    ``AsyncFunctionDef`` / ``Lambda`` definitions (each of those is a separate
    scope, analysed independently when it is itself visited by ``_scopes``).

    This is what makes wrap-detection scope-correlated: a helper call and a
    list-literal assignment are only ever considered together when they live in
    the SAME function/module scope, never across a function boundary."""

    def _rec(node: ast.AST) -> Iterator[ast.AST]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield child
            yield from _rec(child)

    yield from _rec(scope)


def _scopes(tree: ast.Module) -> List[ast.AST]:
    """The module scope plus every ``FunctionDef`` / ``AsyncFunctionDef`` scope.
    Each is analysed with ``_own_scope_nodes`` so their contents never mix."""
    scopes: List[ast.AST] = [tree]
    scopes.extend(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return scopes


def _assign_targets_value(node: ast.AST) -> Tuple[List[ast.AST], ast.AST | None]:
    """(targets, value) for an ``Assign``/``AnnAssign``; ([], None) otherwise."""
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


# Ways a server site actually EXECUTES a `cidx index` command list. A Name
# argument that reaches one of these calls while holding an UNWRAPPED `cidx
# index` construction is spawned raw -- a Story #1488 violation AT THAT
# statement, regardless of whether the same construction is wrapped elsewhere
# (before, after, or on a sibling branch). Inventoried from the real server
# spawn sites: the ``subprocess`` family plus this project's popen-progress /
# telemetry runner wrappers. ``append_server_layout_args`` is deliberately NOT
# here -- it is the WRAP, never a spawn.
_SPAWN_CALLEES: FrozenSet[str] = frozenset(
    {
        "run",
        "Popen",
        "check_call",
        "check_output",
        "run_with_popen_progress",
        "_run_with_popen_progress",
        "_run_with_popen_progress_shared",
        "_run_popen_with_telemetry",
        "_run_popen_c",
        "_run_popen_c_with_telemetry",
        "_run_subprocess_with_telemetry",
        "_run_provider_subprocess",
    }
)


# Live reaching-definitions state carrying WRAPPEDNESS per binding:
# ``var_name -> {(id() of a `cidx index` list literal, wrapped?) ...}``. Each
# reaching definition records whether it is wrapped AT THIS POINT, so the
# spawn-site check can require EVERY reaching definition of a spawned variable to
# be wrapped at the statement where it is executed -- not merely "wrapped
# somewhere in this scope". A set (not a single value) so mutually-exclusive
# branches that bind the SAME name both survive to a later consumer.
_Reaching = Dict[str, Set[Tuple[int, bool]]]


class _Analysis:
    """Accumulators threaded through the per-scope reaching-definitions walk.

    ``wrapped_ever``      -- ids of constructions wrapped on SOME path (a direct
                             positional arg to the helper, or an assign-then-wrap
                             upgrade). Drives the "every construction must be
                             wrapped somewhere" completeness rule.
    ``spawned_unwrapped`` -- ids of constructions that reach a recognised
                             SPAWN/USE call while still UNWRAPPED on at least one
                             reaching path. Drives the true spawn-site teeth
                             (spawn-before-wrap and one-branch-only-wrap)."""

    def __init__(self) -> None:
        self.wrapped_ever: Set[int] = set()
        self.spawned_unwrapped: Set[int] = set()


def _copy_reaching(live: _Reaching) -> _Reaching:
    return {name: set(binds) for name, binds in live.items()}


def _merge_reaching(left: _Reaching, right: _Reaching) -> _Reaching:
    """UNION of two branch outcomes: after a control-flow join, a name's reaching
    definitions (each carrying its own wrappedness) are the union of those from
    every predecessor branch -- so a wrap on only ONE branch leaves the sibling
    branch's UNWRAPPED reaching definition alive at the join (a real violation if
    that name is then spawned), while a wrap AFTER the join over mutually-exclusive
    branch bindings certifies BOTH (the ``refresh_scheduler.py`` shape)."""
    merged: _Reaching = {name: set(binds) for name, binds in left.items()}
    for name, binds in right.items():
        merged.setdefault(name, set()).update(binds)
    return merged


def _binding_for_value(
    value: ast.AST, live: _Reaching, analysis: _Analysis
) -> Set[Tuple[int, bool]] | None:
    """The reaching set a target should hold AFTER ``target = value``:

    * ``value`` is a raw `cidx index` list literal -> a NEW, UNWRAPPED binding
      ``{(id(value), False)}`` (this KILLS any prior binding of the target).
    * ``value`` is ``append_server_layout_args(...)`` -> a WRAPPED binding: each
      `cidx index` list-literal arg and each construction currently reaching a
      Name arg is marked ``wrapped_ever`` and carried forward as ``(id, True)``.
    * ``value`` is a bare ``Name`` (an ALIAS: ``target = source_name``) -> a COPY
      of ``source_name``'s CURRENT reaching set, PRESERVING each binding's
      wrapped/unwrapped state. This does NOT kill ``target`` to empty (the
      Codex alias-state-loss false-negative): an alias of an unwrapped binding
      stays spawnable-raw, an alias of a wrapped binding stays clean. If the
      source Name holds no `cidx index` construction, there is nothing to
      propagate and the target is killed (``None``).
    * anything else -> ``None`` (the target no longer holds a `cidx index`
      construction; the caller KILLS its binding)."""
    if _is_cidx_index_list(value):
        return {(id(value), False)}
    if _is_helper_call(value):
        result: Set[Tuple[int, bool]] = set()
        for arg in value.args:
            if _is_cidx_index_list(arg):
                analysis.wrapped_ever.add(id(arg))
                result.add((id(arg), True))
            elif isinstance(arg, ast.Name):
                for cid, _wrapped in live.get(arg.id, set()):
                    analysis.wrapped_ever.add(cid)
                    result.add((cid, True))
        return result
    if isinstance(value, ast.Name):
        source = live.get(value.id)
        # Propagate the alias source's reaching definitions (with wrappedness);
        # an untracked source (no `cidx index` construction) kills the target.
        return set(source) if source else None
    return None


def _scan_expr(node: ast.AST, live: _Reaching, analysis: _Analysis) -> None:
    """Scan an expression (and its OWN-scope descendants, never nested function
    scopes) for the two events that matter at THIS point in the flow:

      * a SPAWN/USE call (callee in ``_SPAWN_CALLEES``) with a command-carrying
        Name argument -- POSITIONAL (``subprocess.run(cmd)``) OR KEYWORD
        (``subprocess.run(args=cmd)``): every construction reaching that Name
        UNWRAPPED right now is recorded in ``spawned_unwrapped`` -- the true
        spawn-site check. Both arg shapes are inspected, so a command passed by
        keyword is never silently skipped (the Codex keyword-arg false-negative).
      * a direct positional `cidx index` list literal passed to
        ``append_server_layout_args(...)``: recorded in ``wrapped_ever``."""
    for sub in (node, *_own_scope_nodes(node)):
        if _is_helper_call(sub):
            for arg in sub.args:
                if _is_cidx_index_list(arg):
                    analysis.wrapped_ever.add(id(arg))
        elif isinstance(sub, ast.Call) and _callee_name(sub.func) in _SPAWN_CALLEES:
            name_args: List[ast.Name] = [
                arg for arg in sub.args if isinstance(arg, ast.Name)
            ]
            name_args.extend(
                kw.value for kw in sub.keywords if isinstance(kw.value, ast.Name)
            )
            for arg in name_args:
                for cid, wrapped in live.get(arg.id, set()):
                    if not wrapped:
                        analysis.spawned_unwrapped.add(cid)


def _apply_leaf(stmt: ast.AST, live: _Reaching, analysis: _Analysis) -> _Reaching:
    """Process a NON-compound statement in flow order: FIRST scan its expressions
    for spawn/use + direct-arg wraps against the CURRENT reaching state, THEN
    apply its assignment binding (rebind/upgrade/kill). Mutates and returns
    ``live``.

      * ``var = ["cidx", "index", ...]`` -> new UNWRAPPED binding (KILLS prior).
      * ``var = append_server_layout_args(...)`` -> WRAPPED binding.
      * ``var = <anything else>`` -> KILL (``var`` no longer holds a construction).

    A ``cmd.append(...)`` / ``cmd.extend(...)`` mutation is an expression
    statement, not an assignment, so it never rebinds or kills."""
    _scan_expr(stmt, live, analysis)
    targets, value = _assign_targets_value(stmt)
    if value is not None and targets:
        binding = _binding_for_value(value, live, analysis)
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                if binding is not None:
                    live[tgt.id] = set(binding)
                else:
                    live.pop(tgt.id, None)  # reassigned to a non-cidx value: KILL
    return live


def _process_block(
    stmts: List[ast.stmt], live: _Reaching, analysis: _Analysis
) -> _Reaching:
    """Reaching-definitions over a straight-line statement list, returning the
    live state at its end. A private copy is threaded so callers' state is never
    mutated across branch forks."""
    live = _copy_reaching(live)
    for stmt in stmts:
        live = _process_stmt(stmt, live, analysis)
    return live


def _process_stmt(stmt: ast.stmt, live: _Reaching, analysis: _Analysis) -> _Reaching:
    """Branch-aware reaching-definitions for ONE statement.

    Sibling control-flow branches (``if``/``else``, ``except`` handlers, a loop
    body that may run zero times) are processed from the SAME incoming ``live``
    and their outcomes UNIONed at the join -- so a name bound in one branch does
    NOT kill a binding of the same name in a sibling branch (the
    ``refresh_scheduler.py`` ``if needs_reconcile: ... else: ...`` shape, where a
    single ``append_server_layout_args`` after the join genuinely wraps BOTH
    alternatives). Straight-line reassignment inside one block still KILLS
    (the Codex reassignment false-negative). Nested function/class scopes are NOT
    descended -- each is analysed independently by ``_scopes``."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return live  # separate lexical scope, analysed on its own
    if isinstance(stmt, ast.If):
        _scan_expr(stmt.test, live, analysis)
        body_live = _process_block(stmt.body, live, analysis)
        else_live = _process_block(stmt.orelse, live, analysis)
        return _merge_reaching(body_live, else_live)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        _scan_expr(stmt.iter, live, analysis)
        body_live = _process_block(stmt.body, live, analysis)
        after_body = _process_block(stmt.orelse, body_live, analysis)
        return _merge_reaching(live, after_body)  # 0 iters -> live; >=1 -> body
    if isinstance(stmt, ast.While):
        _scan_expr(stmt.test, live, analysis)
        body_live = _process_block(stmt.body, live, analysis)
        after_body = _process_block(stmt.orelse, body_live, analysis)
        return _merge_reaching(live, after_body)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            _scan_expr(item.context_expr, live, analysis)
        return _process_block(stmt.body, live, analysis)
    if isinstance(stmt, ast.Try):
        body_live = _process_block(stmt.body, live, analysis)
        else_live = _process_block(stmt.orelse, body_live, analysis)
        result = else_live
        for handler in stmt.handlers:
            handler_live = _process_block(
                handler.body, _merge_reaching(live, body_live), analysis
            )
            result = _merge_reaching(result, handler_live)
        return _process_block(stmt.finalbody, result, analysis)  # finally always
    return _apply_leaf(stmt, live, analysis)


def _analyze(tree: ast.Module) -> _Analysis:
    """Run the per-scope reaching-definitions pass over the WHOLE module: the
    module scope plus every ``FunctionDef``/``AsyncFunctionDef`` scope, each with
    its own fresh, empty live state so no name ever leaks across a scope
    boundary."""
    analysis = _Analysis()
    for scope in _scopes(tree):
        _process_block(list(getattr(scope, "body", [])), {}, analysis)
    return analysis


def _unwrapped_spawn_lines(source: str) -> List[int]:
    """Line numbers of every `cidx index` construction in ``source`` that is a
    Story #1488 violation, by TRUE reaching-definitions dataflow:

      * NEVER-WRAPPED -- the construction is not wrapped on any path (direct arg
        or assign-then-wrap), so it can only ever be spawned raw; OR
      * SPAWNED-WHILE-UNWRAPPED -- the construction reaches a recognised SPAWN/USE
        call while still unwrapped on at least one path (spawn-before-wrap, or a
        wrap on only one branch), even though it is wrapped on some OTHER path.

    Both are reported at the CONSTRUCTION's line (the list literal that must be
    wrapped before it is executed) -- consistent with every existing guard test.
    A construction wrapped on every path that reaches each of its spawns is
    clean."""
    tree = ast.parse(source)
    constructions = {id(node): node for node in _find_spawn_lists(tree)}
    analysis = _analyze(tree)
    wrapped_ever = analysis.wrapped_ever | _direct_arg_list_ids(tree)
    never_wrapped = {cid for cid in constructions if cid not in wrapped_ever}
    spawned_unwrapped = analysis.spawned_unwrapped & set(constructions)
    violations = never_wrapped | spawned_unwrapped
    return sorted(constructions[cid].lineno for cid in violations)


# --------------------------------------------------------------------------- #
# Repo-wide discovery                                                          #
# --------------------------------------------------------------------------- #
def _is_test_path(rel_posix: str) -> bool:
    parts = Path(rel_posix).parts
    return "tests" in parts or Path(rel_posix).name.startswith("test_")


def _discover_spawn_files() -> Dict[str, str]:
    """{relative-POSIX-path: source-text} for every non-test ``*.py`` under
    src/code_indexer that constructs at least one `["cidx", "index", ...]`
    list literal."""
    assert _SRC_ROOT.is_dir(), f"source root not found: {_SRC_ROOT}"
    found: Dict[str, str] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if _is_test_path(rel):
            continue
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        if _find_spawn_lists(tree):
            found[rel] = source
    return found


def _is_server_context(rel_posix: str) -> bool:
    return rel_posix.startswith(_SERVER_CONTEXT_ROOTS)


# Computed once at import so it can drive parametrization. This is the AUTHORITY
# for which files are wrap-checked -- never a hardcoded list.
_DISCOVERED: Dict[str, str] = _discover_spawn_files()
_SERVER_SPAWN_FILES: List[str] = sorted(
    rel for rel in _DISCOVERED if _is_server_context(rel)
)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_repo_wide_discovery_classifies_every_construction():
    """D3 AUTHORITY: every discovered `cidx index` construction must be either
    server-context (wrap-required, checked elsewhere) or an explicitly justified
    CLI-side exclusion. Anything else -- a `cidx index` list literal appearing in
    an unclassified location -- fails loudly, forcing a conscious decision. This
    is what makes a NEW unwrapped server spawn site impossible to add silently."""
    unclassified = sorted(
        rel
        for rel in _DISCOVERED
        if not _is_server_context(rel) and rel not in _CLI_SIDE_EXCLUSIONS
    )
    assert not unclassified, (
        "Discovered `cidx index` list construction(s) in file(s) that are "
        f"neither server-context (under {_SERVER_CONTEXT_ROOTS}) nor listed in "
        f"_CLI_SIDE_EXCLUSIONS: {unclassified}. Story #1488: if a listed file "
        "is a SERVER spawn site, route its command through "
        f"{_TARGET_FUNCTION}(...) and add its root to _SERVER_CONTEXT_ROOTS; if "
        "it is a standalone-CLI spawn, add it to _CLI_SIDE_EXCLUSIONS with a "
        "specific justification. It must never remain unclassified."
    )


@pytest.mark.parametrize("rel_path", _SERVER_SPAWN_FILES)
def test_every_server_context_cidx_index_spawn_is_wrapped(rel_path: str):
    """POSITIVE completeness over DISCOVERED server-context files: every
    `cidx index` construction is routed through append_server_layout_args.
    Discovery -- not a fixed module list -- selects the files, so a new server
    module with an unwrapped spawn is caught here automatically."""
    source = _DISCOVERED[rel_path]
    unwrapped_lines = _unwrapped_spawn_lines(source)
    assert not unwrapped_lines, (
        f"{rel_path} builds {len(unwrapped_lines)} `cidx index` command "
        f"construction(s) at line(s) {unwrapped_lines} WITHOUT routing them "
        f"through {_TARGET_FUNCTION}(...) (Story #1488). Each such child "
        "subprocess silently reverts to the CLI SHARDED_JSON new-collection "
        "layout instead of the explicit server CHUNKS_DB layout. Wrap the "
        "command list with append_server_layout_args(...) (direct arg or "
        "assign-then-wrap)."
    )


def test_discovery_is_non_vacuous_and_finds_known_server_spawns():
    """Prove discovery is not broken/empty: it must find EXACTLY the recorded
    set of server-context spawn files, and each must contain exactly the
    recorded number of `cidx index` constructions. Equality (not superset) makes
    both a silently-added AND a silently-removed server spawn site loud, so a
    human consciously confirms wrapping. (The wrap SECURITY is guaranteed by
    test_every_server_context_cidx_index_spawn_is_wrapped regardless of this
    anchor.)"""
    assert set(_SERVER_SPAWN_FILES) == set(_KNOWN_SERVER_SPAWN_MODULES), (
        "Discovered server-context `cidx index` spawn files "
        f"{sorted(_SERVER_SPAWN_FILES)} do not match the recorded anchor "
        f"{sorted(_KNOWN_SERVER_SPAWN_MODULES)}. If you added/removed a server "
        "spawn site, confirm every remaining construction is wrapped and update "
        "_KNOWN_SERVER_SPAWN_MODULES."
    )
    for rel_path, expected in _KNOWN_SERVER_SPAWN_MODULES.items():
        found = len(_find_spawn_lists(ast.parse(_DISCOVERED[rel_path])))
        assert found == expected, (
            f"{rel_path} has {found} `cidx index` construction(s); the anchor "
            f"expected {expected}. Update _KNOWN_SERVER_SPAWN_MODULES after "
            "confirming every construction is wrapped."
        )


def test_cli_side_exclusions_are_specific_justified_and_nonvacuous():
    """The CLI-side exclusions set must be SPECIFIC and never able to hide a
    server spawn: every entry (a) is actually discovered as a `cidx index`
    construction site (no dead entries), (b) is NOT under a server-context root
    (an exclusion can never mask a real server spawn), and (c) carries a
    non-empty justification."""
    for rel_path, reason in _CLI_SIDE_EXCLUSIONS.items():
        assert rel_path in _DISCOVERED, (
            f"_CLI_SIDE_EXCLUSIONS lists {rel_path!r}, but discovery found no "
            "`cidx index` construction there -- remove the dead exclusion "
            "entry (this predicate only ever considers files that actually "
            "construct the literal)."
        )
        assert not _is_server_context(rel_path), (
            f"_CLI_SIDE_EXCLUSIONS lists {rel_path!r}, which is under a "
            f"server-context root {_SERVER_CONTEXT_ROOTS}. A server spawn site "
            "may NEVER be excluded -- it must wrap via append_server_layout_args."
        )
        assert isinstance(reason, str) and reason.strip(), (
            f"_CLI_SIDE_EXCLUSIONS entry {rel_path!r} must carry a specific, "
            "non-empty justification for why it is a CLI-side (not server) spawn."
        )


def test_wrap_detection_has_teeth():
    """RED/GREEN self-test proving the wrap assertion actually catches an
    unwrapped server-style construction. A synthetic UNWRAPPED `cidx index`
    build must be reported unwrapped in BOTH real shapes; the WRAPPED
    counterparts must be reported clean. Without this, a broken detector could
    make test_every_server_context_cidx_index_spawn_is_wrapped pass vacuously."""
    # Shape 1: direct-arg wrap.
    unwrapped_direct = 'def f():\n    run(["cidx", "index", "--clear"])\n'
    wrapped_direct = (
        'def f():\n    run(append_server_layout_args(["cidx", "index", "--clear"]))\n'
    )
    # Shape 2: assign-then-wrap.
    unwrapped_assign = (
        'def f():\n    cmd = ["cidx", "index", "--index-commits"]\n    run(cmd)\n'
    )
    wrapped_assign = (
        "def f():\n"
        '    cmd = ["cidx", "index", "--index-commits"]\n'
        "    cmd = append_server_layout_args(cmd)\n"
        "    run(cmd)\n"
    )

    assert _unwrapped_spawn_lines(unwrapped_direct) == [2], (
        "teeth check FAILED: a synthetic UNWRAPPED direct `cidx index` "
        "construction was not flagged -- the detector is blind and the wrap "
        "guard would pass vacuously."
    )
    assert _unwrapped_spawn_lines(unwrapped_assign) == [2], (
        "teeth check FAILED: a synthetic UNWRAPPED assign-then-run `cidx index` "
        "construction was not flagged."
    )
    assert _unwrapped_spawn_lines(wrapped_direct) == [], (
        "teeth check FAILED: a correctly direct-wrapped construction was "
        "wrongly flagged as unwrapped (false positive)."
    )
    assert _unwrapped_spawn_lines(wrapped_assign) == [], (
        "teeth check FAILED: a correctly assign-then-wrapped construction was "
        "wrongly flagged as unwrapped (false positive)."
    )


def test_wrap_detection_is_scope_correlated_not_module_wide_name_match():
    """Codex Finding (MEDIUM) reproduction: the wrap detection must be
    SCOPE-CORRELATED. Two functions in the SAME module reuse the variable name
    ``command``: function ``x`` genuinely assign-then-wraps it through
    append_server_layout_args; function ``y`` builds an UNWRAPPED `cidx index`
    command and spawns it directly, reusing that same name.

    Under the OLD module-wide-name-match logic, ``y``'s construction was
    FALSELY certified as wrapped (RED) purely because ``x`` -- a DIFFERENT
    function -- passed a variable also named ``command`` to the helper. The
    scope-correlated logic must flag ONLY ``y``'s construction (GREEN), never
    ``x``'s."""
    module_src = (
        "def x():\n"  # line 1
        '    command = ["cidx", "index", "--fts", "--progress-json"]\n'  # line 2
        "    command = append_server_layout_args(command)\n"  # line 3
        "    subprocess.run(command)\n"  # line 4
        "\n"  # line 5
        "def y():\n"  # line 6
        '    command = ["cidx", "index", "--clear"]\n'  # line 7 -- UNWRAPPED
        "    subprocess.run(command)\n"  # line 8
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [7], (
        "scope-correlation teeth check FAILED: expected ONLY function y's "
        "unwrapped `cidx index` construction at line 7 to be flagged, got "
        f"{unwrapped}. A result of [] means the module-wide name-match "
        "false-negative has regressed -- y's `command` was falsely certified "
        "as wrapped by x's same-named-but-different-scope `command`. A result "
        "including line 2 means x's genuinely-wrapped construction was wrongly "
        "flagged (false positive)."
    )


def test_wrap_detection_tracks_reassignment_per_binding_in_statement_order():
    """Codex Finding (MEDIUM) reproduction: within a SINGLE function, wrap
    detection must correlate each `cidx index` list construction with the exact
    binding that ``append_server_layout_args`` actually consumed -- in statement
    order -- NOT merely with the variable NAME.

    Function ``f`` reuses the name ``command`` for TWO separate bindings: the
    FIRST is genuinely assign-then-wrapped and spawned; the SECOND rebinds
    ``command`` to a NEW, UNWRAPPED ``["cidx", "index", "--clear"]`` list and
    spawns it directly.

    Under the OLD per-function-name-set logic, the SECOND (unwrapped)
    construction was FALSELY certified as wrapped (RED) purely because the name
    ``command`` was wrapped EARLIER in the same function. Statement-order
    per-binding tracking must flag ONLY the second construction (GREEN) while
    leaving the first (genuinely wrapped) binding clean."""
    module_src = (
        "def f():\n"  # line 1
        '    command = ["cidx", "index", "--fts", "--progress-json"]\n'  # line 2 -- wrapped below
        "    command = append_server_layout_args(command)\n"  # line 3
        "    subprocess.run(command)\n"  # line 4
        '    command = ["cidx", "index", "--clear"]\n'  # line 5 -- UNWRAPPED reassignment
        "    subprocess.run(command)\n"  # line 6
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [5], (
        "statement-order per-binding teeth check FAILED: expected ONLY the "
        "second, UNWRAPPED `cidx index` reassignment at line 5 to be flagged, "
        f"got {unwrapped}. A result of [] means the intra-function "
        "reassignment false-negative has regressed -- the second `command` "
        "binding was falsely certified as wrapped merely because the SAME name "
        "was wrapped earlier at lines 2-3. A result including line 2 means the "
        "first, genuinely assign-then-wrapped binding was wrongly flagged "
        "(false positive)."
    )


def test_wrap_detection_does_not_leak_helper_names_from_nested_functions():
    """Scope isolation must not leak the helper-arg name of a NESTED function
    up to its enclosing scope. An outer function builds an UNWRAPPED `cidx
    index` bound to ``cmd`` and spawns it; an inner nested function wraps its
    OWN, unrelated ``cmd``. The outer construction must still be flagged
    unwrapped -- the nested helper call must not certify it."""
    module_src = (
        "def outer():\n"  # line 1
        '    cmd = ["cidx", "index", "--clear"]\n'  # line 2 -- UNWRAPPED
        "    def inner():\n"  # line 3
        '        cmd = ["cidx", "index", "--fts"]\n'  # line 4 -- wrapped below
        "        cmd = append_server_layout_args(cmd)\n"  # line 5
        "        subprocess.run(cmd)\n"  # line 6
        "    inner()\n"  # line 7
        "    subprocess.run(cmd)\n"  # line 8
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [2], (
        "nested-scope isolation FAILED: expected ONLY outer's unwrapped `cidx "
        "index` at line 2 to be flagged (inner's line 4 is genuinely wrapped "
        f"in its own scope), got {unwrapped}. If line 2 is missing, the nested "
        "function's helper-arg name `cmd` leaked up and falsely certified "
        "outer's construction."
    )


def test_wrap_detection_flags_spawn_before_wrap():
    """Codex test-robustness Finding, false-negative CLASS 1 (spawn BEFORE wrap):
    a `cidx index` list is spawned RAW and only wrapped on a LATER statement. The
    later wrap must NOT retroactively certify the earlier raw execution. A true
    reaching-definitions check requires the construction to be wrapped AT the
    spawn statement, so the raw spawn is a violation reported at the construction
    line.

    Under the prior "ever wrapped in this scope" logic this returned [] (the line
    4 wrap globally marked the construction wrapped) -- the exact false negative
    this rewrite closes. The spawn-site check must now flag it."""
    module_src = (
        "def f():\n"  # 1
        '    command = ["cidx", "index", "--clear"]\n'  # 2 -- constructed raw
        "    subprocess.run(command)\n"  # 3 -- SPAWNED RAW here
        "    command = append_server_layout_args(command)\n"  # 4 -- wrapped too late
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [2], (
        "spawn-before-wrap teeth check FAILED: expected the `cidx index` "
        "construction at line 2 to be flagged because it is spawned RAW at line "
        f"3 before the line-4 wrap, got {unwrapped}. A result of [] means the "
        "later wrap falsely certified the earlier raw execution -- the exact "
        "reaching-definitions false negative this check closes."
    )


def test_wrap_detection_flags_one_branch_only_wrap():
    """Codex test-robustness Finding, false-negative CLASS 2 (one-branch-only
    wrap): the construction is wrapped ONLY inside an ``if`` body, so on the path
    where the branch is not taken it reaches the spawn RAW. The UNION of reaching
    states at the join therefore still contains an UNWRAPPED definition, and the
    spawn is a violation reported at the construction line.

    Under the prior logic the single-branch wrap globally marked the construction
    wrapped and this returned [] -- a false negative. Branch-aware wrappedness
    must now flag it."""
    module_src = (
        "def f(flag):\n"  # 1
        '    command = ["cidx", "index", "--clear"]\n'  # 2 -- constructed raw
        "    if flag:\n"  # 3
        "        command = append_server_layout_args(command)\n"  # 4 -- wrapped on ONE branch only
        "    subprocess.run(command)\n"  # 5 -- RAW when flag is False
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [2], (
        "one-branch-only-wrap teeth check FAILED: expected the `cidx index` "
        "construction at line 2 to be flagged because the else path reaches the "
        f"line-5 spawn unwrapped, got {unwrapped}. A result of [] means the "
        "single-branch wrap falsely certified the raw else-path execution."
    )


def test_wrap_detection_wrap_then_spawn_single_binding_is_clean():
    """Positive control: a single binding wrapped BEFORE it is spawned is clean.
    The reaching definition at the spawn statement is fully wrapped, so nothing
    is flagged (guards against the spawn-site check over-firing)."""
    module_src = (
        "def f():\n"  # 1
        '    command = ["cidx", "index", "--index-commits"]\n'  # 2
        "    command = append_server_layout_args(command)\n"  # 3 -- wrapped first
        "    subprocess.run(command)\n"  # 4 -- spawned wrapped
    )

    assert _unwrapped_spawn_lines(module_src) == [], (
        "false positive: a construction wrapped before it is spawned must be "
        "clean, but it was flagged."
    )


def test_wrap_detection_flags_alias_of_unwrapped_binding_spawned():
    """Codex test-robustness Finding, false-negative CLASS 3 (ALIAS-state loss):
    a raw `cidx index` list is bound to ``cmd``, then ALIASED to ``raw_alias``
    (``raw_alias = cmd``), then ``cmd`` -- but NOT the alias -- is wrapped, and the
    STILL-UNWRAPPED ``raw_alias`` is spawned::

        cmd = ["cidx", "index", "--clear"]
        raw_alias = cmd
        cmd = append_server_layout_args(cmd)
        subprocess.run(raw_alias)          # raw_alias holds the UNWRAPPED binding

    Under the prior logic ``raw_alias = cmd`` was treated as a KILL (a Name value
    was not a `cidx index` literal nor a helper call, so ``_binding_for_value``
    returned None and the target's binding was dropped) -- so ``raw_alias`` never
    reached the spawn with the unwrapped construction and this returned []. A true
    reaching-definitions pass must PROPAGATE ``cmd``'s current reaching set (with
    its unwrapped state) to ``raw_alias``, so the alias spawn is flagged at the
    construction line."""
    module_src = (
        "def f():\n"  # 1
        '    cmd = ["cidx", "index", "--clear"]\n'  # 2 -- constructed raw
        "    raw_alias = cmd\n"  # 3 -- alias captures the UNWRAPPED binding
        "    cmd = append_server_layout_args(cmd)\n"  # 4 -- wraps cmd, NOT raw_alias
        "    subprocess.run(raw_alias)\n"  # 5 -- spawns the UNWRAPPED alias
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [2], (
        "alias-propagation teeth check FAILED: expected the `cidx index` "
        "construction at line 2 to be flagged because its UNWRAPPED binding is "
        "aliased to raw_alias (line 3) and raw_alias is spawned raw (line 5), got "
        f"{unwrapped}. A result of [] means ``raw_alias = cmd`` killed the alias's "
        "tracked state instead of propagating cmd's unwrapped reaching definition."
    )


def test_wrap_detection_flags_keyword_arg_unwrapped_spawn():
    """Codex test-robustness Finding, false-negative CLASS 4 (keyword spawn arg):
    a `cidx index` construction is passed to a recognised spawn callee as a
    KEYWORD argument (``subprocess.run(args=command)``) while still unwrapped, and
    is only wrapped on a LATER statement::

        command = ["cidx", "index", "--clear"]
        subprocess.run(args=command)                  # RAW via keyword
        command = append_server_layout_args(command)  # wrapped too late

    Under the prior logic the spawn-site check inspected ONLY positional Name
    args (``sub.args``), never keyword values (``sub.keywords[*].value``), so the
    raw keyword spawn was invisible; the later wrap put the construction in
    ``wrapped_ever`` so the never-wrapped rule did not catch it either -> []. The
    spawn-site check must inspect keyword Name values too, flagging the raw spawn
    at the construction line."""
    module_src = (
        "def f():\n"  # 1
        '    command = ["cidx", "index", "--clear"]\n'  # 2 -- constructed raw
        "    subprocess.run(args=command)\n"  # 3 -- SPAWNED RAW via keyword
        "    command = append_server_layout_args(command)\n"  # 4 -- wrapped too late
    )

    unwrapped = _unwrapped_spawn_lines(module_src)

    assert unwrapped == [2], (
        "keyword-arg-spawn teeth check FAILED: expected the `cidx index` "
        "construction at line 2 to be flagged because it is spawned RAW as the "
        f"keyword arg ``args=command`` at line 3, got {unwrapped}. A result of [] "
        "means keyword spawn arguments were not inspected -- only positional Name "
        "args were checked."
    )


def test_wrap_detection_alias_of_wrapped_binding_is_clean():
    """Positive control for alias propagation: aliasing a WRAPPED binding then
    spawning the alias is clean. The alias must inherit ``cmd``'s WRAPPED reaching
    state (not merely be marked present-but-unwrapped), so nothing is flagged --
    guards against a propagation fix that drops wrappedness."""
    module_src = (
        "def f():\n"  # 1
        '    cmd = ["cidx", "index", "--index-commits"]\n'  # 2
        "    cmd = append_server_layout_args(cmd)\n"  # 3 -- wrapped first
        "    raw_alias = cmd\n"  # 4 -- alias captures the WRAPPED binding
        "    subprocess.run(raw_alias)\n"  # 5 -- spawns the WRAPPED alias
    )

    assert _unwrapped_spawn_lines(module_src) == [], (
        "false positive: aliasing a WRAPPED binding then spawning the alias must "
        "be clean, but a construction was flagged -- alias propagation dropped "
        "the wrapped state."
    )


def test_wrap_detection_keyword_arg_wrapped_spawn_is_clean():
    """Positive control for keyword-arg inspection: a construction wrapped BEFORE
    it is spawned as a keyword argument is clean. The reaching definition at the
    keyword spawn is fully wrapped, so nothing is flagged -- guards against the
    keyword check over-firing."""
    module_src = (
        "def f():\n"  # 1
        '    cmd = ["cidx", "index", "--index-commits"]\n'  # 2
        "    cmd = append_server_layout_args(cmd)\n"  # 3 -- wrapped first
        "    subprocess.run(args=cmd)\n"  # 4 -- spawned wrapped via keyword
    )

    assert _unwrapped_spawn_lines(module_src) == [], (
        "false positive: a construction wrapped before it is spawned as a keyword "
        "argument must be clean, but it was flagged."
    )


def test_wrap_detection_if_else_bind_then_wrap_after_join_is_clean():
    """Positive control mirroring the REAL ``refresh_scheduler.py`` shape: two
    mutually-exclusive ``if``/``else`` bindings of the same name, a SINGLE
    ``append_server_layout_args`` after the join wrapping BOTH alternatives, then
    the spawn. Every reaching definition at the spawn is wrapped -- nothing may be
    flagged (a naive line-order kill or a branch-unaware check would wrongly flag
    one alternative)."""
    module_src = (
        "def f(needs_reconcile):\n"  # 1
        "    if needs_reconcile:\n"  # 2
        '        index_command = ["cidx", "index", "--fts", "--reconcile", "--progress-json"]\n'  # 3
        "    else:\n"  # 4
        '        index_command = ["cidx", "index", "--fts", "--progress-json"]\n'  # 5
        "    index_command = append_server_layout_args(index_command)\n"  # 6 -- wraps BOTH
        "    subprocess.run(index_command)\n"  # 7
    )

    assert _unwrapped_spawn_lines(module_src) == [], (
        "false positive: the refresh_scheduler-style if/else-bind-then-wrap-"
        "after-join pattern wraps BOTH alternatives before the spawn and must be "
        "clean, but a construction was flagged."
    )
