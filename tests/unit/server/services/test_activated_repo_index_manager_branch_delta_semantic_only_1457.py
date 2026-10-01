"""AC12: run_branch_delta_index's semantic-only behavior is INTENTIONAL,
not an artifact to "complete" later (Story #1457).

`ActivatedRepoIndexManager.run_branch_delta_index` calls ONLY
`_execute_semantic_indexing`, never `_execute_temporal_indexing`, on
activation/branch-switch/sync. This was historically an accidental
omission in the call graph -- AC12 makes it a DELIBERATE design decision
(temporal data belongs exclusively to the golden repo's shared sister
location, AC1-AC11) and locks it in with this regression test, so a future
refactor cannot silently "complete" the call graph by wiring in
`_execute_temporal_indexing` without hitting this test.

Implemented as a structural (AST-based) guard on the REAL method source --
never mocking or patching the system under test -- proving both that
`_execute_temporal_indexing` is absent from the call graph AND that
`_execute_semantic_indexing` genuinely IS present (so this guard cannot
pass vacuously against an empty/stub method body).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)


def _called_method_names(func) -> set:
    """Return the set of `self.<name>(...)` method names called anywhere in
    func's body, via real AST parsing of the function's own source."""
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                names.add(node.func.attr)
    return names


def test_run_branch_delta_index_never_calls_execute_temporal_indexing():
    called = _called_method_names(ActivatedRepoIndexManager.run_branch_delta_index)

    assert "_execute_temporal_indexing" not in called, (
        "run_branch_delta_index must NEVER call _execute_temporal_indexing "
        "-- temporal data is owned exclusively by the golden repo's shared "
        "sister location (Story #1457 AC12); this is intentional, not an "
        "incomplete call graph to be 'fixed' later"
    )
    assert "_execute_semantic_indexing" in called, (
        "sanity check: this guard must not pass vacuously -- "
        "run_branch_delta_index must genuinely call _execute_semantic_indexing"
    )
