"""Production-scale hazard: `reconcile_golden_repo_registry()` is a plain,
SYNCHRONOUS `def` (golden_repo_reconciler.py). Calling it directly inside
`async def lifespan` blocks the entire event loop for the sweep's whole
duration.

`_run_sweep()` loops `for repo_row in repo_rows:
golden_repo_manager.get_actual_repo_path(alias)` -- one filesystem
resolution PER GOLDEN REPO. Production has ~900 golden repos, so this is
~900 blocking metadata ops, synchronously on the event loop (dev's much
smaller fleet made this invisible locally).

Worse: the cow-storage mount is `hard` NFSv3, so `os.stat()` blocks in
UNINTERRUPTIBLE kernel retry when the NFS server is unresponsive -- it
never times out. Called bare on the event loop, that is a permanently
hung server at boot, not merely a slow one.

lifespan.py's sibling `_run_vsr_sweep` (Bug #1567, itself modeled on the
`_run_orphan_sweep` / Story #1032 AC8 precedent) already established the
fix for this exact hazard class: never call the blocking function bare --
offload it via `anyio.to_thread.run_sync`, deferred inside a `lambda:`,
and await the result. This test proves the golden-repo registry-orphan
reconcile sweep's call site (Bug #1317) follows the same convention.

The assertion is deliberately about the CALL SITE'S ANCESTRY in the AST,
not merely "the sweep still gets called somewhere", and it checks EVERY
matching call site in the file (not just the first one found), and it
specifically requires each call to be DEFERRED (wrapped in a `lambda`)
rather than merely textually nested inside an offload call's argument
list -- `await run_sync(reconcile_golden_repo_registry(...))` would still
execute the reconcile call EAGERLY on the event loop (Python evaluates
call arguments before invoking the outer call), so an ancestry check that
accepted that shape would pass while the hazard remained live. Requiring
the call's direct parent to be a `Lambda` (matching the sibling
`_run_vsr_sweep`'s own `lambda: reconcile_versioned_snapshots(...)`
idiom) rules that out: only a callable whose invocation is deferred until
the worker thread actually runs it satisfies this test.

The offload-call check resolves the callee's FULL qualified dotted path
via the file's actual `import`/`from ... import` statements (including
aliases, e.g. `import anyio.to_thread as _to_thread`), rather than
matching on the trailing attribute name alone -- a name-only match would
accept an unrelated `fake.run_sync(...)` or `custom.to_thread(...)` call
as if it were a genuine thread-offload primitive, which would let the
test pass while the sweep still ran synchronously on the event loop.

`run_in_executor` is deliberately NOT recognized here (unlike the more
permissive name-only list in some earlier drafts of this hazard class):
it is invoked as a method on a live event-loop object obtained via
`asyncio.get_event_loop()`/`get_running_loop()`, never via an importable
name, so there is no import statement to verify it against -- accepting
it on trailing-attribute-name alone would let an unrelated
`fake.run_in_executor(...)` call satisfy this test. The actual fix in
lifespan.py (matching its sibling `_run_vsr_sweep` exactly) uses
`anyio.to_thread.run_sync`, which IS import-verifiable, so narrowing to
only the two import-resolvable primitives guards the real hazard without
an unverifiable loophole.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional

# This test file lives at:
#   tests/unit/server/startup/test_lifespan_golden_repo_reconcile_event_loop_offload.py
# Walking up 4 parents from this file lands on the repository root:
#   startup -> server -> unit -> tests -> <repo root>
_TEST_FILE_TO_REPO_ROOT_DEPTH = 4
_REPO_ROOT = Path(__file__).resolve().parents[_TEST_FILE_TO_REPO_ROOT_DEPTH]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# Fully-qualified dotted paths recognized as genuine thread-offload
# primitives. Both are resolvable through this file's actual import
# statements (including aliasing), so a false-positive unrelated call
# (e.g. `fake.run_sync(...)`) cannot satisfy this set.
_RECOGNIZED_QUALIFIED_OFFLOAD_CALLS = {
    "anyio.to_thread.run_sync",
    "asyncio.to_thread",
}


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _find_reconcile_calls(tree: ast.AST) -> List[ast.Call]:
    """Locate EVERY bare-name `reconcile_golden_repo_registry(...)` call
    site (i.e. an invocation of the plain `ast.Name`, never a mere
    reference such as the `from ... import` statement itself) -- a
    hazard fixed at one call site but reintroduced at another bare-name
    call must still fail this test.

    Deliberately scoped to bare-name calls only, matching this file's
    single production call site, which is reached via
    `from code_indexer.server.services.golden_repo_reconciler import
    reconcile_golden_repo_registry` followed by a direct, unqualified
    call -- the exact same shape the sibling
    `test_lifespan_vsr_sweep_event_loop_offload.py` guards for
    `reconcile_versioned_snapshots(...)`. A qualified/aliased call form
    (e.g. `module.reconcile_golden_repo_registry(...)` or a renamed
    import binding) is out of scope for this test: every lazy `from ...
    import X` in this module follows the same unqualified-call
    convention, so detecting a hypothetical qualified/aliased form would
    require resolving arbitrary aliasing across the whole module without
    guarding any additional real hazard.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reconcile_golden_repo_registry"
    ]


def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _build_import_alias_map(tree: ast.AST) -> Dict[str, str]:
    """Map every locally-bound import name (module-level OR inside any
    function -- this codebase's convention is deferred, function-local
    imports) to its fully-qualified dotted source, so an aliased import
    like `import anyio.to_thread as _to_thread` resolves `_to_thread` ->
    `anyio.to_thread`."""
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # Bare `import X.Y` binds only the top-level name `X`
                    # in the local namespace; access is `X.Y....`.
                    top_level = alias.name.split(".")[0]
                    aliases[top_level] = top_level
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound_name = alias.asname or alias.name
                aliases[bound_name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted_path(node: ast.AST) -> Optional[List[str]]:
    """Flatten a Name/Attribute chain into its dotted parts, root-first.
    Returns None if the chain contains anything else (e.g. a call, a
    subscript) -- such a callee cannot be a plain import-resolvable
    reference."""
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return list(reversed(parts))
    return None


def _resolve_qualified_call_name(
    call_func: ast.AST, import_aliases: Dict[str, str]
) -> Optional[str]:
    """Resolve a Call's `func` to its real, import-verified dotted path,
    e.g. `_to_thread.run_sync` with `_to_thread` aliased from
    `anyio.to_thread` resolves to `"anyio.to_thread.run_sync"`."""
    parts = _dotted_path(call_func)
    if not parts:
        return None
    root, rest = parts[0], parts[1:]
    resolved_root = import_aliases.get(root)
    if resolved_root is None:
        # Unresolvable root (not a name bound by any import statement in
        # this file) -- cannot verify, so treat as unrecognized.
        return ".".join(parts)
    return ".".join([resolved_root, *rest]) if rest else resolved_root


def test_reconcile_golden_repo_registry_call_site_exists() -> None:
    """Sanity check the hazard's premise: the call site is still present
    (has not been removed/renamed out from under this test)."""
    tree = ast.parse(_lifespan_source())
    assert _find_reconcile_calls(tree), (
        "reconcile_golden_repo_registry(...) call not found in "
        "lifespan.py -- has the Bug #1317 startup sweep been removed or "
        "renamed?"
    )


def _assert_call_is_offloaded(
    call_node: ast.Call,
    parents: Dict[ast.AST, ast.AST],
    import_aliases: Dict[str, str],
) -> None:
    """Every reconcile_golden_repo_registry(...) call site must be:
      1. the direct body of a `lambda:` (DEFERRED -- never evaluated
         eagerly as a plain argument expression), and
      2. that lambda must be passed to a call that import-resolves, via
         this file's actual import statements, to a recognized
         thread-offload primitive (anyio.to_thread.run_sync or
         asyncio.to_thread) -- not merely a call whose trailing
         attribute name happens to match one of those strings, and
      3. that offload call must itself be `await`ed.

    All three are required together: a naive `await run_sync(reconcile_
    golden_repo_registry(...))` shape satisfies (2)+(3)'s textual nesting
    but fails (1) -- the reconcile call still runs eagerly, synchronously,
    on the event loop, before run_sync ever receives its (already
    computed) argument. Only the lambda-deferred shape actually moves the
    blocking work onto a worker thread. And a call like `fake.run_sync(...)`
    would satisfy a name-only check but fails the import-resolution in (2).
    """
    # Requirement (1): the reconcile call's direct parent must be a
    # Lambda whose body IS this call -- i.e. invocation is deferred.
    lambda_parent = parents.get(call_node)
    assert isinstance(lambda_parent, ast.Lambda) and lambda_parent.body is call_node, (
        "reconcile_golden_repo_registry(...) must be the deferred body of "
        "a `lambda:` passed to a thread-offload call -- found it as a "
        "plain (eagerly-evaluated) argument expression instead, which "
        "still executes the sweep synchronously on the event loop before "
        "any offload call receives it."
    )

    # Requirement (2): the lambda itself must be passed into a call that
    # import-resolves to a recognized thread-offload primitive.
    offload_call = parents.get(lambda_parent)
    assert isinstance(offload_call, ast.Call), (
        "the lambda wrapping reconcile_golden_repo_registry(...) is not "
        "itself passed as an argument to any call"
    )
    qualified_name = _resolve_qualified_call_name(offload_call.func, import_aliases)
    assert qualified_name in _RECOGNIZED_QUALIFIED_OFFLOAD_CALLS, (
        f"the lambda wrapping reconcile_golden_repo_registry(...) is "
        f"passed to '{qualified_name}', which does not import-resolve to "
        f"a recognized thread-offload primitive "
        f"({sorted(_RECOGNIZED_QUALIFIED_OFFLOAD_CALLS)}) -- the sweep is "
        f"not verifiably moved off the event loop"
    )

    # Requirement (3): the offload call must itself be awaited -- a
    # fire-and-ignore `run_sync(...)` with no await would still schedule
    # work without ever yielding control back correctly / observing
    # errors, and anyio's run_sync specifically requires awaiting its
    # coroutine to actually run.
    awaiting_node = parents.get(offload_call)
    assert isinstance(awaiting_node, ast.Await), (
        "the thread-offload call wrapping reconcile_golden_repo_registry("
        "...) is not awaited -- reconcile_golden_repo_registry(...) is "
        "not verifiably offloaded off the event loop."
    )


def test_every_reconcile_golden_repo_registry_call_is_offloaded_to_a_thread() -> None:
    """Every call site found for reconcile_golden_repo_registry(...) in
    lifespan.py must satisfy the deferred-lambda + import-verified,
    awaited-offload ancestry -- checking only the first match would miss
    a second, still-synchronous call site reintroduced elsewhere in the
    file."""
    tree = ast.parse(_lifespan_source())
    call_nodes = _find_reconcile_calls(tree)
    assert call_nodes, "no reconcile_golden_repo_registry(...) call sites found"

    parents = _build_parent_map(tree)
    import_aliases = _build_import_alias_map(tree)
    for call_node in call_nodes:
        _assert_call_is_offloaded(call_node, parents, import_aliases)
