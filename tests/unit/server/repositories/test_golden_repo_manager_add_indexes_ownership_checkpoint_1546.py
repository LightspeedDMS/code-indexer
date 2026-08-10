"""Issue #1546 AC5: GoldenRepoManager.add_indexes_to_golden_repo's
post-indexing-loop CoW-snapshot-and-alias-swap sequence must check
write-lock ownership immediately before publishing the swap.

Structural (AST-based) regression test -- same established pattern as
test_golden_repo_manager_ownership_loss_checkpoint_1546.py (parses real
method source via `ast`/`inspect`, no mocking of the system under test).
The publish sequence lives inside this method's nested `background_worker`
closure; `inspect.getsource()` on the outer method still captures the
closure's full body, so the same textual-order check applies.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _call_names_in_order(source: str) -> list:
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


class TestOwnershipCheckpointPrecedesAddIndexesSwapStructurally:
    def test_checkpoint_call_appears_before_swap_alias_call(self):
        source = textwrap.dedent(
            inspect.getsource(GoldenRepoManager.add_indexes_to_golden_repo)
        )
        names = _call_names_in_order(source)

        assert "raise_if_write_lock_ownership_lost" in names, (
            "add_indexes_to_golden_repo() must call "
            "scheduler.raise_if_write_lock_ownership_lost() before the "
            "post-loop snapshot+swap (Issue #1546 AC5)"
        )
        assert "swap_alias" in names, (
            "add_indexes_to_golden_repo() must call "
            "scheduler.alias_manager.swap_alias(...) -- if this "
            "assertion fails the method was refactored and this test "
            "needs updating, not silently dropped"
        )

        checkpoint_index = names.index("raise_if_write_lock_ownership_lost")
        swap_index = names.index("swap_alias")
        assert checkpoint_index < swap_index, (
            f"raise_if_write_lock_ownership_lost must be called BEFORE "
            f"swap_alias in add_indexes_to_golden_repo() -- found "
            f"checkpoint at call-order index {checkpoint_index}, swap at "
            f"{swap_index}"
        )
