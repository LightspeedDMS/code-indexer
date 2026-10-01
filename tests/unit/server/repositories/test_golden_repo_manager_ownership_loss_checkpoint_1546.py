"""Issue #1546 AC5: GoldenRepoManager.change_branch() must check write-lock
ownership immediately before the alias swap.

Structural (AST-based) regression test -- the SAME established pattern
this codebase already uses for "call X strictly before Y inside method
Z" guarantees without mocking any part of the system under test (see
CLAUDE.md's documented precedent,
test_activated_repo_index_manager_branch_delta_semantic_only_1457.py,
which parses real method source via `ast`/`inspect`). Parses
change_branch's real source and asserts a call to
raise_if_write_lock_ownership_lost appears before the call to
_cb_swap_alias.

Runtime behavior (the checkpoint actually raising and change_branch's
existing Bug #469 rollback catching it, since AliasLockOwnershipLostError
is itself a RuntimeError subclass) is covered by
raise_if_write_lock_ownership_lost's own unit tests
(test_refresh_scheduler_ownership_loss_checkpoint_1546.py) and by
change_branch's pre-existing rollback test
(test_golden_repo_manager_branch_rollback.py), which is not duplicated
here.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _call_names_in_order(source: str) -> list:
    """Return every `ast.Call` node's callee-name (or attribute name for
    `x.y(...)` calls) found in `source`, in the order they appear in the
    source text -- ast.walk() is a breadth-first traversal, so results
    are additionally stabilized by (lineno, col_offset)."""
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append((node.lineno, node.col_offset, func.attr))
            elif isinstance(func, ast.Name):
                calls.append((node.lineno, node.col_offset, func.id))
    calls.sort(key=lambda c: (c[0], c[1]))
    return [name for _, _, name in calls]


class TestOwnershipCheckpointPrecedesSwapStructurally:
    def test_checkpoint_call_appears_before_swap_call(self):
        source = textwrap.dedent(inspect.getsource(GoldenRepoManager.change_branch))
        names = _call_names_in_order(source)

        assert "raise_if_write_lock_ownership_lost" in names, (
            "change_branch() must call "
            "scheduler.raise_if_write_lock_ownership_lost() somewhere in "
            "its body (Issue #1546 AC5 branch-change swap checkpoint)"
        )
        assert "_cb_swap_alias" in names, (
            "change_branch() must call self._cb_swap_alias(...) -- if "
            "this assertion fails the method itself was refactored and "
            "this test needs updating, not silently dropped"
        )

        checkpoint_index = names.index("raise_if_write_lock_ownership_lost")
        swap_index = names.index("_cb_swap_alias")
        assert checkpoint_index < swap_index, (
            f"raise_if_write_lock_ownership_lost must be called BEFORE "
            f"_cb_swap_alias in change_branch() -- found checkpoint at "
            f"call-order index {checkpoint_index}, swap at {swap_index}"
        )
