"""Production-scale hazard: `reconcile_versioned_snapshots()` is a plain,
SYNCHRONOUS `def` (versioned_snapshot_reconciler.py). Calling it directly
inside `async def lifespan` blocks the entire event loop for the sweep's
whole duration.

The sweep performs ~18 filesystem operations per `.versioned/` namespace.
Production has ~900 golden repos -- roughly 16,000 metadata ops on NFS,
synchronously on the event loop (dev's ~30 namespaces made this invisible
locally). At 1ms/op that is ~16s of a frozen server; at 5ms/op (normal
NFS under load) ~80s.

Worse: the cow-storage mount is `hard` NFSv3, so `os.stat()` blocks in
UNINTERRUPTIBLE kernel retry when the NFS server is unresponsive -- it
never times out. Called bare on the event loop, that is a permanently
hung server at boot, not merely a slow one.

lifespan.py's sibling `_run_orphan_sweep` (Story #1032 AC8 / HIGH #3)
already established the fix for this exact hazard class: never call the
blocking function bare -- offload it via `anyio.to_thread.run_sync`,
deferred inside a `lambda:`, and await the result. This test proves the
versioned-snapshot sweep's call site follows the same convention.

The assertion is deliberately about the CALL SITE'S ANCESTRY in the AST,
not merely "the sweep still gets called somewhere", and it checks EVERY
matching call site in the file (not just the first one found), and it
specifically requires each call to be DEFERRED (wrapped in a `lambda`)
rather than merely textually nested inside an offload call's argument
list -- `await run_sync(reconcile_versioned_snapshots(...))` would still
execute the reconcile call EAGERLY on the event loop (Python evaluates
call arguments before invoking the outer call), so an ancestry check that
accepted that shape would pass while the hazard remained live. Requiring
the call's direct parent to be a `Lambda` (matching the sibling
`_run_orphan_sweep`'s own `lambda: _arm.sweep_orphan_trash_dirs(...)`
idiom) rules that out: only a callable whose invocation is deferred until
the worker thread actually runs it satisfies this test.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

# This test file lives at:
#   tests/unit/server/startup/test_lifespan_vsr_sweep_event_loop_offload.py
# Walking up 4 parents from this file lands on the repository root:
#   startup -> server -> unit -> tests -> <repo root>
_TEST_FILE_TO_REPO_ROOT_DEPTH = 4
_REPO_ROOT = Path(__file__).resolve().parents[_TEST_FILE_TO_REPO_ROOT_DEPTH]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_OFFLOAD_CALL_NAMES = ("run_sync", "to_thread", "run_in_executor")


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _find_reconcile_calls(tree: ast.AST) -> List[ast.Call]:
    """Locate EVERY `reconcile_versioned_snapshots(...)` call site (never
    a reference to the bare name alone, e.g. the `from ... import`) --
    a hazard fixed at one call site but reintroduced at another must
    still fail this test."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reconcile_versioned_snapshots"
    ]


def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def test_reconcile_versioned_snapshots_call_site_exists() -> None:
    """Sanity check the hazard's premise: the call site is still present
    (has not been removed/renamed out from under this test)."""
    tree = ast.parse(_lifespan_source())
    assert _find_reconcile_calls(tree), (
        "reconcile_versioned_snapshots(...) call not found in lifespan.py "
        "-- has the Bug #1567 startup sweep been removed or renamed?"
    )


def _assert_call_is_offloaded(
    call_node: ast.Call, parents: Dict[ast.AST, ast.AST]
) -> None:
    """Every reconcile_versioned_snapshots(...) call site must be:
      1. the direct body of a `lambda:` (DEFERRED -- never evaluated
         eagerly as a plain argument expression), and
      2. that lambda must be passed to a thread-offload call
         (anyio.to_thread.run_sync / asyncio.to_thread / run_in_executor)
      3. and that offload call must itself be `await`ed.

    All three are required together: a naive `await run_sync(reconcile_
    versioned_snapshots(...))` shape satisfies (2)+(3)'s textual nesting
    but fails (1) -- the reconcile call still runs eagerly, synchronously,
    on the event loop, before run_sync ever receives its (already
    computed) argument. Only the lambda-deferred shape actually moves the
    blocking work onto a worker thread.
    """
    # Requirement (1): the reconcile call's direct parent must be a
    # Lambda whose body IS this call -- i.e. invocation is deferred.
    lambda_parent = parents.get(call_node)
    assert isinstance(lambda_parent, ast.Lambda) and lambda_parent.body is call_node, (
        "reconcile_versioned_snapshots(...) must be the deferred body of "
        "a `lambda:` passed to a thread-offload call -- found it as a "
        "plain (eagerly-evaluated) argument expression instead, which "
        "still executes the sweep synchronously on the event loop before "
        "any offload call receives it."
    )

    # Requirement (2): the lambda itself must be passed into a call whose
    # func resolves to a thread-offload primitive.
    offload_call = parents.get(lambda_parent)
    assert isinstance(offload_call, ast.Call), (
        "the lambda wrapping reconcile_versioned_snapshots(...) is not "
        "itself passed as an argument to any call"
    )
    func = offload_call.func
    offload_name = (
        func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    )
    assert offload_name in _OFFLOAD_CALL_NAMES, (
        f"the lambda wrapping reconcile_versioned_snapshots(...) is passed "
        f"to '{offload_name}', not a recognized thread-offload primitive "
        f"({_OFFLOAD_CALL_NAMES}) -- the sweep is not being moved off the "
        f"event loop"
    )

    # Requirement (3): the offload call must itself be awaited -- a
    # fire-and-ignore `run_sync(...)` with no await would still schedule
    # work without ever yielding control back correctly / observing
    # errors, and anyio's run_sync specifically requires awaiting its
    # coroutine to actually run.
    awaiting_node = parents.get(offload_call)
    assert isinstance(awaiting_node, ast.Await), (
        "the thread-offload call wrapping reconcile_versioned_snapshots("
        "...) is not awaited -- reconcile_versioned_snapshots(...) is "
        "not verifiably offloaded off the event loop."
    )


def test_every_reconcile_versioned_snapshots_call_is_offloaded_to_a_thread() -> None:
    """Every call site found for reconcile_versioned_snapshots(...) in
    lifespan.py must satisfy the deferred-lambda + awaited-offload
    ancestry -- checking only the first match would miss a second,
    still-synchronous call site reintroduced elsewhere in the file."""
    tree = ast.parse(_lifespan_source())
    call_nodes = _find_reconcile_calls(tree)
    assert call_nodes, "no reconcile_versioned_snapshots(...) call sites found"

    parents = _build_parent_map(tree)
    for call_node in call_nodes:
        _assert_call_is_offloaded(call_node, parents)
