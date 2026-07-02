"""Regression tests for Bug #749 follow-up: no asyncio.run() wrapping of sync client calls.

Codex code review on commit 6279aa7d identified 39 additional CLI call sites using
asyncio.run(sync_client_method(...)) — same architectural mistake as the earlier
efb22806, just on the CLI side. Every sync client method call was wrapped in
asyncio.run(), which crashes with 'a coroutine was expected, got {...}' because sync
methods return dicts/lists, not coroutines.

Codex review rd3 (Bug #749 gap closure) found additional crash sites:
  - cli.py:9664 — asyncio.run(auth_client.change_password(...))
  - cli.py:13474, 13587, 13683 — `async with JobsAPIClient(...)` on a sync client
    (JobsAPIClient inherits only sync __enter__/__exit__ from CIDXRemoteAPIClient)

Two violation rules are enforced across ALL cli*.py files:
  "asyncio_run"  — asyncio.run() wrapping a sync client method call
  "async_with"   — `async with` on a sync client (inline constructor or variable-bound)

Detection is scope-safe: nested function definitions are not descended into when
collecting variable bindings, preventing inner-function assignments from polluting
outer scope analysis. Variable-bound async-with detection uses _build_parent_map()
to find the actual enclosing function, so only that function's sync-client vars are
checked (no cross-function false positives).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Generator, List, Literal, Optional, Set, Tuple

import pytest


PACKAGE_ROOT = Path(__file__).parent.parent.parent.parent / "src" / "code_indexer"

_SYNC_CLIENT_CLASSES = frozenset(
    {
        "GitAPIClient",
        "CICDAPIClient",
        "SSHAPIClient",
        "FileAPIClient",
        "IndexAPIClient",
        "AuthAPIClient",
        "JobsAPIClient",
        "AdminAPIClient",
        "GroupAPIClient",
        "CredentialAPIClient",
        "SCIPAPIClient",
        "ReposAPIClient",
        "SyncReposAPIClient",
        "RepositoryLinkingClient",
        "RemoteQueryClient",
        "CIDXRemoteAPIClient",
    }
)

# Factory functions returning a sync client (e.g. `auth_client = create_auth_client(...)`).
_FACTORY_FUNCTIONS: Dict[str, str] = {"create_auth_client": "AuthAPIClient"}

_Rule = Literal["asyncio_run", "async_with"]

# Typed constant — avoids cast() when building the parametrize matrix.
_RULES: Tuple[_Rule, ...] = ("asyncio_run", "async_with")

_ORIGINAL_FIXED_FILES = [
    "cli_git.py",
    "cli_cicd.py",
    "cli_keys.py",
    "cli_files.py",
    "cli_index.py",
]
_ALL_CLI_FILES = sorted(p.name for p in PACKAGE_ROOT.glob("cli*.py"))


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _iter_direct_nodes(node: ast.AST) -> Generator[ast.AST, None, None]:
    """Yield descendant nodes without crossing nested function/class scope boundaries."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _iter_direct_nodes(child)


def _build_parent_map(tree: ast.AST) -> Dict[int, ast.AST]:
    """Return a mapping from id(child_node) -> parent_node for every node in tree."""
    result: Dict[int, ast.AST] = {}
    for p_node in ast.walk(tree):
        for child in ast.iter_child_nodes(p_node):
            result[id(child)] = p_node
    return result


def _collect_sync_client_vars(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Set[str]:
    """Return names bound to sync client constructors/factories within func_node's direct scope."""
    bound: Set[str] = set()
    for node in _iter_direct_nodes(func_node):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            continue
        func = node.value.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else None)
        )
        if name and (name in _SYNC_CLIENT_CLASSES or name in _FACTORY_FUNCTIONS):
            bound.add(node.targets[0].id)
    return bound


def _find_violations(source_path: Path, rule: _Rule) -> List[Tuple[int, str]]:
    """Return (lineno, snippet) pairs for every violation of rule in source_path.

    asyncio_run: asyncio.run(<sync_var>.method(...)) in any function scope.
    async_with:  `async with <sync_client>` — inline constructor or variable-bound.
                 Variable-bound check uses _build_parent_map() to find the exact
                 enclosing function, so only that function's sync vars are tested.
    """
    source = source_path.read_text()
    tree = ast.parse(source, filename=str(source_path))
    lines = source.splitlines()
    violations: List[Tuple[int, str]] = []

    if rule == "asyncio_run":
        for func_node in ast.walk(tree):
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sync_vars = _collect_sync_client_vars(func_node)
            if not sync_vars:
                continue
            for node in _iter_direct_nodes(func_node):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"
                    and node.args
                    and isinstance(node.args[0], ast.Call)
                    and isinstance(node.args[0].func, ast.Attribute)
                    and isinstance(node.args[0].func.value, ast.Name)
                    and node.args[0].func.value.id in sync_vars
                ):
                    continue
                violations.append((node.lineno, lines[node.lineno - 1].strip()))

    else:  # async_with
        parent_map = _build_parent_map(tree)

        def _enclosing_func(
            node: ast.AST,
        ) -> Optional[ast.FunctionDef | ast.AsyncFunctionDef]:
            cur: Optional[ast.AST] = parent_map.get(id(node))
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return cur
                cur = parent_map.get(id(cur))
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            for item in node.items:
                ctx = item.context_expr
                violated = False
                if isinstance(ctx, ast.Call):
                    ctor = ctx.func
                    name = (
                        ctor.id
                        if isinstance(ctor, ast.Name)
                        else (ctor.attr if isinstance(ctor, ast.Attribute) else None)
                    )
                    violated = bool(name and name in _SYNC_CLIENT_CLASSES)
                elif isinstance(ctx, ast.Name):
                    enc = _enclosing_func(node)
                    if enc is not None:
                        violated = ctx.id in _collect_sync_client_vars(enc)
                if violated:
                    violations.append((node.lineno, lines[node.lineno - 1].strip()))
                    break  # one report per AsyncWith node

    return violations


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_clean(cli_path: Path, rule: _Rule, label: str) -> None:
    assert cli_path.exists(), f"CLI file not found: {cli_path}"
    violations = _find_violations(cli_path, rule)
    rule_desc = (
        "asyncio.run(client.*) — sync method must be called directly"
        if rule == "asyncio_run"
        else "`async with SyncClient` — use `with` for sync context managers"
    )
    assert violations == [], (
        f"Bug #749 [{label}] {cli_path.name}: {len(violations)} violation(s) of rule '{rule}'.\n"
        f"Rule: {rule_desc}\n" + "\n".join(f"  Line {ln}: {s}" for ln, s in violations)
    )


# ---------------------------------------------------------------------------
# Original Bug #749 follow-up: 5 files — asyncio_run rule + dead-import check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cli_filename", _ORIGINAL_FIXED_FILES)
def test_rule_original_files(cli_filename: str) -> None:
    """Original-fixed CLI files must have no asyncio.run(client.*) violations.

    Also checks that the 'import asyncio' dead code was removed after the fix:
    once asyncio.run() calls are gone, the import itself becomes dead code.
    """
    cli_path = PACKAGE_ROOT / cli_filename
    _assert_clean(cli_path, "asyncio_run", "follow-up")

    source = cli_path.read_text()
    tree = ast.parse(source, filename=str(cli_path))
    dead_imports = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "asyncio"
    )
    assert dead_imports == 0, (
        f"{cli_filename} still has {dead_imports} 'import asyncio' statement(s) — dead code."
    )


# ---------------------------------------------------------------------------
# Bug #749 rd3 gap regressions: change_password (gap 1) + jobs (gap 2)
# ---------------------------------------------------------------------------


def test_gap1_change_password_not_wrapped_in_asyncio_run() -> None:
    """Gap 1: auth_client.change_password() must not be wrapped in asyncio.run()."""
    _assert_clean(PACKAGE_ROOT / "cli.py", "asyncio_run", "gap-1 change-password")


def test_gap2_jobs_client_not_used_with_async_with() -> None:
    """Gap 2: JobsAPIClient must not be used as `async with` (sync CM only)."""
    _assert_clean(PACKAGE_ROOT / "cli.py", "async_with", "gap-2 jobs-commands")


# ---------------------------------------------------------------------------
# Comprehensive forward guard: ALL cli*.py files, both rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli_filename,rule",
    [(f, r) for f in _ALL_CLI_FILES for r in _RULES],
)
def test_rule_all_cli_files(cli_filename: str, rule: _Rule) -> None:
    """Comprehensive guard: no cli*.py file violates either sync-client usage rule."""
    _assert_clean(PACKAGE_ROOT / cli_filename, rule, "comprehensive")
