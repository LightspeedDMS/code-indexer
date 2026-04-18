"""Regression tests for Bug #749 follow-up: no asyncio.run() wrapping of sync client calls.

Codex code review on commit 6279aa7d identified 39 additional CLI call sites using
asyncio.run(sync_client_method(...)) — same architectural mistake as the earlier
efb22806, just on the CLI side. Every sync client method call was wrapped in
asyncio.run(), which crashes with 'a coroutine was expected, got {...}' because sync
methods return dicts/lists, not coroutines.

These tests enforce that no CLI file uses asyncio.run() to wrap sync API client method
calls. The approach: parse AST of each CLI file, track which local variables are
assigned from known sync client constructors, then assert that asyncio.run() is never
called with a method call on any of those variables.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set, Tuple, Union

import pytest


# Root of the code_indexer package
PACKAGE_ROOT = Path(__file__).parent.parent.parent.parent / "src" / "code_indexer"

# CLI files that previously contained asyncio.run(client.method(...)) violations.
# These are the exact files from the Bug #749 follow-up audit.
CLI_FILES_WITH_FIXED_SYNC_CLIENT_CALLS = [
    "cli_git.py",
    "cli_cicd.py",
    "cli_keys.py",
    "cli_files.py",
    "cli_index.py",
]

# Sync API client class names (confirmed sync via httpx.Client — never async def).
_SYNC_CLIENT_CLASSES = frozenset(
    {
        "GitAPIClient",
        "CICDAPIClient",
        "SSHAPIClient",
        "FileAPIClient",
        "IndexAPIClient",
    }
)

# Union type for both function node kinds
_AnyFuncNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def _collect_sync_client_vars(func_node: _AnyFuncNode) -> Set[str]:
    """Return variable names assigned from known sync client constructors in func_node.

    Scans all assignment statements of the form:
        name = SomeClass(...)
    where SomeClass is in _SYNC_CLIENT_CLASSES.
    """
    bound: Set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        # Only single-target simple assignments: var = Expr
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        # Match: var = SomeClient(...) or var = module.SomeClient(...)
        if isinstance(value, ast.Call):
            ctor = value.func
            if isinstance(ctor, ast.Name) and ctor.id in _SYNC_CLIENT_CLASSES:
                bound.add(target.id)
            elif isinstance(ctor, ast.Attribute) and ctor.attr in _SYNC_CLIENT_CLASSES:
                bound.add(target.id)
    return bound


def _find_asyncio_run_of_sync_client(source_path: Path) -> List[Tuple[int, str]]:
    """Parse source and find asyncio.run() calls wrapping sync client method calls.

    Strategy: for each function in the file, collect variables bound to sync client
    constructors, then search for asyncio.run(<var>.method(...)) where <var> is one
    of those bound names.

    Returns list of (line_number, code_snippet) for each violation.
    """
    source = source_path.read_text()
    tree = ast.parse(source, filename=str(source_path))
    source_lines = source.splitlines()

    violations: List[Tuple[int, str]] = []

    # Collect functions at all nesting levels
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        sync_client_vars = _collect_sync_client_vars(func_node)
        if not sync_client_vars:
            continue

        # Search this function's body for asyncio.run(client_var.method(...))
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue

            # Must be asyncio.run(...)
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                continue

            if not node.args:
                continue

            arg = node.args[0]

            # Argument must be a method call: bound_var.some_method(...)
            if not isinstance(arg, ast.Call):
                continue
            if not isinstance(arg.func, ast.Attribute):
                continue
            if not isinstance(arg.func.value, ast.Name):
                continue

            obj_name = arg.func.value.id
            if obj_name in sync_client_vars:
                line_no = node.lineno
                snippet = source_lines[line_no - 1].strip()
                violations.append((line_no, snippet))

    return violations


def _count_asyncio_imports_ast(source_path: Path) -> int:
    """Count 'import asyncio' statements via AST (both top-level and inline)."""
    source = source_path.read_text()
    tree = ast.parse(source, filename=str(source_path))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    count += 1
    return count


@pytest.mark.parametrize("cli_filename", CLI_FILES_WITH_FIXED_SYNC_CLIENT_CALLS)
def test_no_asyncio_run_wrapping_sync_client_calls(cli_filename: str) -> None:
    """Assert that no sync API client method call is wrapped in asyncio.run().

    After Bug #749 follow-up fix, all asyncio.run(client.method(...)) patterns
    must be removed from the affected CLI files. Direct calls (result = client.method(...))
    are the correct pattern.

    Detection strategy: track AST assignments from known sync client constructors,
    then flag asyncio.run() calls on those bound variables.
    """
    cli_path = PACKAGE_ROOT / cli_filename
    assert cli_path.exists(), f"CLI file not found: {cli_path}"

    violations = _find_asyncio_run_of_sync_client(cli_path)

    assert violations == [], (
        f"Bug #749: Found {len(violations)} asyncio.run(client.*) call(s) in {cli_filename}. "
        f"Sync client methods must be called directly without asyncio.run().\n"
        f"Violations:\n"
        + "\n".join(f"  Line {ln}: {snippet}" for ln, snippet in violations)
    )


@pytest.mark.parametrize("cli_filename", CLI_FILES_WITH_FIXED_SYNC_CLIENT_CALLS)
def test_no_asyncio_import_in_cli_files_after_fix(cli_filename: str) -> None:
    """After fix, CLI files with only sync client calls must not import asyncio.

    When every asyncio.run() call on a sync client is removed, the 'import asyncio'
    inside each function body becomes dead code. This AST-based check enforces removal.
    """
    cli_path = PACKAGE_ROOT / cli_filename
    assert cli_path.exists(), f"CLI file not found: {cli_path}"

    count = _count_asyncio_imports_ast(cli_path)

    assert count == 0, (
        f"{cli_filename} still has {count} 'import asyncio' statement(s). "
        f"After removing all asyncio.run(client.*) calls, these imports are dead code."
    )
