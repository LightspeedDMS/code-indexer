"""Bug #1871 ALSO IN SCOPE item 3: server startup must sweep the LEGACY
``.snapshot-reader-leases`` directory's individually-expired lease files
(via ``sweep_expired_lease_files()``, added in
``global_repos/snapshot_reader_lease.py``) so the throwaway churn stops
polluting backup sync and the semantic indexer over time -- WITHOUT ever
calling that synchronous, filesystem-walking function bare inside
``async def lifespan``.

At fleet scale (~900 golden repos), a bare synchronous call blocks the
whole event loop for the sweep's duration, and the cow-storage mount is
`hard` NFSv3 -- an unresponsive NFS server makes the walk block FOREVER in
uninterruptible kernel retry, i.e. a permanently hung server at boot, not
merely a slow one. This is the exact hazard class ``lifespan.py``'s
sibling ``_run_orphan_sweep`` (Story #1032 AC8 / HIGH #3) and the
versioned-snapshot reconciler sweep already establish the fix for:
``anyio.to_thread.run_sync``, with the blocking call DEFERRED inside a
`lambda:` and the offload call itself awaited.

This test file mirrors
``tests/unit/server/startup/test_lifespan_vsr_sweep_event_loop_offload.py``
exactly (same AST-ancestry technique, same rationale for why a naive
`await run_sync(sweep_expired_lease_files(...))` shape is NOT sufficient
-- Python evaluates call arguments eagerly, so the sweep would still run
synchronously on the event loop before ``run_sync`` ever receives its
already-computed result). Placed under ``tests/unit/global_repos/`` (an
owned test directory) rather than ``tests/unit/server/startup/`` (not
owned for this bug; see negotiation turns 3-5) -- pytest does not require
a test file's location to mirror the module under test.

Split across several Write/Edit calls (a few functions at a time) per
this project's per-operation method-count limit.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

# This test file lives at:
#   tests/unit/global_repos/test_lifespan_1871_lease_sweep_event_loop_offload.py
# Walking up 3 parents from this file lands on the repository root:
#   global_repos -> unit -> tests -> <repo root>
_TEST_FILE_TO_REPO_ROOT_DEPTH = 3
_REPO_ROOT = Path(__file__).resolve().parents[_TEST_FILE_TO_REPO_ROOT_DEPTH]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_OFFLOAD_CALL_NAMES = ("run_sync", "to_thread", "run_in_executor")
_SWEEP_FUNCTION_NAME = "sweep_expired_lease_files"


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _call_target_name(call_node: ast.Call) -> str:
    """The called name, whether invoked bare (``sweep_expired_lease_files(...)``,
    a direct import) or as an attribute (``snapshot_reader_lease.sweep_expired_lease_files(...)``,
    a module import) -- both are legitimate import styles and this test
    must not force one over the other."""
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _find_sweep_calls(tree: ast.AST) -> List[ast.Call]:
    """Locate EVERY ``sweep_expired_lease_files(...)`` call site -- a
    hazard fixed at one call site but reintroduced at another must still
    fail this test."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_target_name(node) == _SWEEP_FUNCTION_NAME
    ]


def test_sweep_expired_lease_files_call_site_exists() -> None:
    """RED on unmodified code: `sweep_expired_lease_files(...)` is not
    called anywhere in `lifespan.py` yet -- mission item 3's startup
    wiring has not been added."""
    tree = ast.parse(_lifespan_source())
    assert _find_sweep_calls(tree), (
        "sweep_expired_lease_files(...) is not called anywhere in "
        "lifespan.py -- the Bug #1871 startup sweep of the legacy lease "
        "directory has not been wired up"
    )


def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _assert_call_is_offloaded(
    call_node: ast.Call, parents: Dict[ast.AST, ast.AST]
) -> None:
    """Every sweep_expired_lease_files(...) call site must be:
    1. the direct body of a `lambda:` (DEFERRED -- never evaluated
       eagerly as a plain argument expression), and
    2. that lambda must be passed to a thread-offload call
       (anyio.to_thread.run_sync / asyncio.to_thread / run_in_executor)
    3. and that offload call must itself be `await`ed.
    """
    lambda_parent = parents.get(call_node)
    assert isinstance(lambda_parent, ast.Lambda) and lambda_parent.body is call_node, (
        "sweep_expired_lease_files(...) must be the deferred body of a "
        "`lambda:` passed to a thread-offload call -- found it as a plain "
        "(eagerly-evaluated) argument expression instead, which still "
        "executes the sweep synchronously on the event loop before any "
        "offload call receives it."
    )

    offload_call = parents.get(lambda_parent)
    assert isinstance(offload_call, ast.Call), (
        "the lambda wrapping sweep_expired_lease_files(...) is not itself "
        "passed as an argument to any call"
    )
    func = offload_call.func
    offload_name = (
        func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    )
    assert offload_name in _OFFLOAD_CALL_NAMES, (
        f"the lambda wrapping sweep_expired_lease_files(...) is passed to "
        f"'{offload_name}', not a recognized thread-offload primitive "
        f"({_OFFLOAD_CALL_NAMES}) -- the sweep is not being moved off the "
        f"event loop"
    )

    awaiting_node = parents.get(offload_call)
    assert isinstance(awaiting_node, ast.Await), (
        "the thread-offload call wrapping sweep_expired_lease_files(...) "
        "is not awaited -- sweep_expired_lease_files(...) is not "
        "verifiably offloaded off the event loop."
    )


def test_every_sweep_expired_lease_files_call_is_offloaded_to_a_thread() -> None:
    """Every call site found for sweep_expired_lease_files(...) in
    lifespan.py must satisfy the deferred-lambda + awaited-offload
    ancestry -- checking only the first match would miss a second,
    still-synchronous call site reintroduced elsewhere in the file."""
    tree = ast.parse(_lifespan_source())
    call_nodes = _find_sweep_calls(tree)
    assert call_nodes, "no sweep_expired_lease_files(...) call sites found"

    parents = _build_parent_map(tree)
    for call_node in call_nodes:
        _assert_call_is_offloaded(call_node, parents)


def test_sweep_is_called_against_the_legacy_lease_directory_not_the_primary_one() -> (
    None
):
    """The sweep must target the LEGACY (``.snapshot-reader-leases``, git-
    tracked) directory -- never the relocated primary ``.scratch`` root,
    which holds live, actively-renewed leases from every current-version
    node and must never be swept by this startup pass. Checked via a
    source-text proximity heuristic (the call's line must reference
    `_legacy_lease_directory` or the literal legacy directory name) since
    the actual Path argument is typically a variable, not inlineable into
    a simple AST-only assertion.
    """
    source = _lifespan_source()
    tree = ast.parse(source)
    call_nodes = _find_sweep_calls(tree)
    assert call_nodes, "no sweep_expired_lease_files(...) call sites found"

    lines = source.splitlines()
    for call_node in call_nodes:
        # Look at the call's own line plus a small window above it, where
        # the argument variable is realistically resolved/constructed.
        start = max(0, call_node.lineno - 15)
        window = "\n".join(lines[start : call_node.lineno])
        assert (
            "_legacy_lease_directory" in window or ".snapshot-reader-leases" in window
        ), (
            "sweep_expired_lease_files(...) call site does not appear to "
            "reference the legacy lease directory ('_legacy_lease_directory' "
            "or '.snapshot-reader-leases') anywhere nearby -- it must never "
            "be pointed at the relocated primary '.scratch' directory, "
            "which holds live leases from every current-version node"
        )
