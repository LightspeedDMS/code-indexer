"""Enumerating structural guard: every server-side temporal-child spawn site
genuinely imports AND calls build_temporal_child_env (Story #1457 AC6
Finding-1).

CIDX_SERVER_REFRESH_CONTEXT is only ever set by build_temporal_child_env. If
a future temporal-child Popen call site is added (or an existing one is
refactored) WITHOUT calling that shared builder, the child silently loses
the server-context signal. This guard parses each known spawn site's SOURCE
FILE with the ``ast`` module (not a substring/comment match) and asserts
BOTH a real import-from statement AND at least one genuine ``ast.Call`` node
invoking ``build_temporal_child_env``, so a bypass fails CI instead of
regressing silently. Mirrors the structural guard the
CIDX_TEMPORAL_PG_BOOTSTRAP_DIR wiring (Bug #1313) already depends on.

The five known spawn sites (per the story's AC6 Finding-1 text):
  1. global_repos/refresh_scheduler.py             (scheduled refresh)
  2. server/repositories/golden_repo_manager.py     (registration)
  3. server/repositories/golden_repo_manager.py     (add-index / reindex)
  4. server/services/activated_repo_index_manager.py (activated-repo temporal)
  5. server/mcp/handlers/repos.py                   (MCP provider indexing)

golden_repo_manager.py hosts TWO of the five (registration AND
add-index/reindex), so it must show >= 2 distinct call sites.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import List

import pytest

_TARGET_FUNCTION = "build_temporal_child_env"

_MODULES_WITH_MIN_CALL_COUNT = [
    ("code_indexer.global_repos.refresh_scheduler", 1),
    ("code_indexer.server.repositories.golden_repo_manager", 2),
    ("code_indexer.server.services.activated_repo_index_manager", 1),
    ("code_indexer.server.mcp.handlers.repos", 1),
]


def _parse_module(module_name: str) -> ast.Module:
    module = importlib.import_module(module_name)
    assert module.__file__ is not None, f"{module_name} has no source file"
    source_path = Path(module.__file__)
    return ast.parse(source_path.read_text(), filename=str(source_path))


def _imports_target_function(tree: ast.Module) -> bool:
    """True if an ``ImportFrom`` node imports build_temporal_child_env,
    anywhere in the module (including inside a function body, which is
    this codebase's established lazy-import convention)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == _TARGET_FUNCTION for alias in node.names):
                return True
    return False


def _count_target_calls(tree: ast.Module) -> int:
    """Count genuine ast.Call nodes whose callee is build_temporal_child_env
    -- immune to a mere substring match inside a comment/docstring."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == _TARGET_FUNCTION:
                count += 1
    return count


@pytest.mark.parametrize("module_name, min_calls", _MODULES_WITH_MIN_CALL_COUNT)
def test_temporal_child_spawn_site_imports_and_calls_shared_builder(
    module_name: str, min_calls: int
):
    tree = _parse_module(module_name)

    assert _imports_target_function(tree), (
        f"{module_name} must genuinely `from ...temporal_child_wiring import "
        f"{_TARGET_FUNCTION}` (Story #1457 AC6 Finding-1) -- a spawn site "
        "that constructs its env dict without this import bypasses the "
        "CIDX_SERVER_REFRESH_CONTEXT server-context signal"
    )

    actual_calls = _count_target_calls(tree)
    assert actual_calls >= min_calls, (
        f"{module_name} must invoke {_TARGET_FUNCTION}(...) at least "
        f"{min_calls} time(s) as a genuine ast.Call (found {actual_calls}) "
        "-- a bypassed spawn site never sets CIDX_SERVER_REFRESH_CONTEXT"
    )


def test_all_five_known_spawn_sites_are_covered_by_this_guard():
    """Sanity check on the guard's own coverage: the sum of expected call
    sites across all modules must be exactly five (the story's own count),
    so this guard cannot silently under-cover a known site."""
    total_expected: List[int] = [
        min_calls for _module, min_calls in _MODULES_WITH_MIN_CALL_COUNT
    ]
    assert sum(total_expected) == 5
