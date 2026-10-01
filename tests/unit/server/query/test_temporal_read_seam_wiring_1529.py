"""Bug #1529 item 6: AST wiring guard for the REST temporal read seam.

`SemanticQueryManager._execute_temporal_query` is where an activated-repo
temporal query stops reading the activation's own CoW clone and starts
reading the golden repo's fixed temporal root. That redirect is one call and
one keyword argument: resolve the location, then hand it to
`reconstruct_temporal_backend` as `temporal_index_dir=`.

Drop either half and NOTHING fails: `reconstruct_temporal_backend` accepts
`temporal_index_dir=None` and falls back to its legacy repo_path-derived
location, which is exactly the pre-#1529 behavior -- a frozen-at-clone-time
duplicate that silently diverges from the golden repo on every refresh. That
is the half-wiring class of defect this whole bug exists to close, so the
wiring is guarded structurally, the same way the write seam
(`test_temporal_write_side_sister_path_1529.py`) and the worker seam already
are.

Parses the REAL method source -- no mocks, no execution.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import List, Optional

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager

SEAM_METHOD = "_execute_temporal_query"
RESOLVER_METHOD = "_resolve_temporal_index_dir"
BACKEND_FACTORY = "reconstruct_temporal_backend"
LOCATION_KWARG = "temporal_index_dir"


def _seam_tree() -> ast.AST:
    source = inspect.getsource(getattr(SemanticQueryManager, SEAM_METHOD))
    return ast.parse(textwrap.dedent(source))


def _called_symbol(node: ast.Call) -> Optional[str]:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls_to(tree: ast.AST, name: str) -> List[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_symbol(node) == name
    ]


def _names_assigned_from(tree: ast.AST, called: str) -> List[str]:
    """Variables bound directly to the result of calling ``called``."""
    assigned: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        if _called_symbol(node.value) != called:
            continue
        assigned.extend(
            target.id for target in node.targets if isinstance(target, ast.Name)
        )
    return assigned


def test_read_seam_resolves_the_fixed_temporal_location() -> None:
    """The seam must ask for the fixed location at all."""
    assert _calls_to(_seam_tree(), RESOLVER_METHOD), (
        f"{SEAM_METHOD} no longer calls {RESOLVER_METHOD}; the temporal read "
        "path has reverted to deriving its location from repo_path -- an "
        "activation's own CoW clone, i.e. a frozen duplicate of the golden "
        "repo's data"
    )


def test_read_seam_forwards_the_resolved_location_to_the_backend() -> None:
    """Resolving it is useless unless it reaches the store that reads.

    Checked by VARIABLE IDENTITY, not by "some expression is passed": a
    future edit that keeps the resolver call but passes `None` (or any other
    value) for the kwarg would silently restore the pre-#1529 behavior while
    still looking wired.
    """
    tree = _seam_tree()
    resolved_names = _names_assigned_from(tree, RESOLVER_METHOD)
    assert resolved_names, (
        f"{SEAM_METHOD} calls {RESOLVER_METHOD} but binds its result to "
        "nothing, so the resolved location cannot reach the backend"
    )

    backend_calls = _calls_to(tree, BACKEND_FACTORY)
    assert backend_calls, (
        f"{SEAM_METHOD} no longer constructs the temporal backend via {BACKEND_FACTORY}"
    )

    forwarded = [
        keyword.value.id
        for call in backend_calls
        for keyword in call.keywords
        if keyword.arg == LOCATION_KWARG and isinstance(keyword.value, ast.Name)
    ]
    assert any(name in resolved_names for name in forwarded), (
        f"{BACKEND_FACTORY} is not given the resolved location as "
        f"{LOCATION_KWARG}= (forwarded names: {forwarded}, resolved names: "
        f"{resolved_names}). Without it the backend falls back to its "
        "repo_path-derived legacy location and the fix is inert."
    )
