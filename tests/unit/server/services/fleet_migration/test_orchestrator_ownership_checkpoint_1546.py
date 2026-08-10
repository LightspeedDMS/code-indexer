"""Issue #1546 AC5: fleet_migration/orchestrator.py's _run_migration_sequence
must check write-lock ownership immediately before firing the AC10
post-consolidation snapshot trigger -- the point at which the migration's
work becomes durably published and query-visible.

Structural (AST-based) regression test -- same established pattern as the
golden_repo_manager/activated_repo_manager checkpoint tests (parses real
function source via `ast`/`inspect`, no mocking of the system under
test). Asserts strict call-order ADJACENCY (no other call in between),
matching the wiring where the checkpoint is the statement immediately
preceding the snapshot trigger.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.server.services.fleet_migration import orchestrator


def _dotted_name(node: ast.AST) -> str:
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _calls_in_order(source: str) -> list:
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append((node.lineno, node.col_offset, _dotted_name(node.func)))
    calls.sort(key=lambda c: (c[0], c[1]))
    return calls


def _index_of_suffix(calls: list, suffix: str) -> int:
    for i, (_, _, name) in enumerate(calls):
        if name == suffix or name.endswith("." + suffix):
            return i
    raise AssertionError(f"no call ending in {suffix!r} found")


class TestOwnershipCheckpointBeforeSnapshotTriggerStructurally:
    def test_checkpoint_call_is_immediately_before_snapshot_trigger(self):
        source = textwrap.dedent(
            inspect.getsource(orchestrator._run_migration_sequence)
        )
        calls = _calls_in_order(source)

        checkpoint_index = _index_of_suffix(calls, "raise_if_write_lock_ownership_lost")
        trigger_index = _index_of_suffix(calls, "trigger_post_consolidation_snapshot")

        assert trigger_index == checkpoint_index + 1, (
            f"raise_if_write_lock_ownership_lost must be called "
            f"IMMEDIATELY before trigger_post_consolidation_snapshot in "
            f"_run_migration_sequence() with no other call in between -- "
            f"found checkpoint at call-order index {checkpoint_index}, "
            f"trigger at {trigger_index} (calls in between: "
            f"{[c[2] for c in calls[checkpoint_index + 1 : trigger_index]]})"
        )
