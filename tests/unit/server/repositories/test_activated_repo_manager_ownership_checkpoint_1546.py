"""Issue #1546 AC5: ActivatedRepoManager._clone_with_copy_on_write's
activation-clone workflow (the "registration's clone/index workflow"
lease-holding phase) must check write-lock ownership immediately after
the CoW clone completes, before any further git manipulation continues
under the lock.

Structural (AST-based) regression test -- same established pattern as
the golden_repo_manager checkpoint tests (parses real method source via
`ast`/`inspect`, no mocking of the system under test). Asserts the full
three-point ordering: create_clone_at_path < the ownership checkpoint <
the first subprocess.run call that follows the clone (the bare-repo
-detection git command) -- proving the checkpoint sits strictly between
the clone completing and any further git manipulation continuing under
the lock, not merely "somewhere after the clone".
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


def _dotted_name(node: ast.AST) -> str:
    """Reconstruct the full dotted callee expression for a Call's
    `.func` node -- e.g. `subprocess.run`,
    `self._clone_backend.create_clone_at_path`,
    `scheduler.raise_if_write_lock_ownership_lost` -- so callers can
    match a SPECIFIC qualified call, not just its trailing attribute
    name (which could collide with an unrelated `.run(...)` on some
    other object)."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _calls_in_order(source: str) -> list:
    """Return every ast.Call node's (lineno, col_offset, dotted_name)."""
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


def _first_index_of_exact_after(calls: list, exact_name: str, after_index: int) -> int:
    for i in range(after_index + 1, len(calls)):
        if calls[i][2] == exact_name:
            return i
    raise AssertionError(
        f"no {exact_name!r} call found after call-order index {after_index}"
    )


class TestOwnershipCheckpointAfterCloneStructurally:
    def test_checkpoint_sits_between_clone_and_next_subprocess_run(self):
        source = textwrap.dedent(
            inspect.getsource(ActivatedRepoManager._clone_with_copy_on_write)
        )
        calls = _calls_in_order(source)

        clone_index = _index_of_suffix(calls, "create_clone_at_path")
        checkpoint_index = _index_of_suffix(calls, "raise_if_write_lock_ownership_lost")
        next_run_index = _first_index_of_exact_after(
            calls, "subprocess.run", clone_index
        )

        assert clone_index < checkpoint_index < next_run_index, (
            f"expected create_clone_at_path ({clone_index}) < "
            f"raise_if_write_lock_ownership_lost ({checkpoint_index}) < "
            f"next subprocess.run ({next_run_index}) -- the ownership "
            f"checkpoint must sit strictly between the clone completing "
            f"and any further git manipulation continuing under the lock"
        )
