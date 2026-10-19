"""Every door that activates a repository refuses the internal catalog for
users outside the admins group.

An AST scan of the server package lists every ``.activate_repository(``
call with its enclosing function. A new caller fails this test until it
applies the shared guard (services/repo_access_guard.require_activation_allowed)
and is added below.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

import code_indexer.server as server_pkg
from code_indexer.server.mcp.protocol import _NEW_ALIAS_PARAM_TOOLS

SERVER_DIR = Path(server_pkg.__file__).parent
MANAGER_FILE = "repositories/activated_repo_manager.py"
GUARDS = {"require_activation_allowed", "enforce_activation_allowed"}

MCP_CALLERS = {
    ("mcp/handlers/repos.py", "activate_repository"): "activate_repository",
    (
        "mcp/handlers/repos.py",
        "manage_composite_repository",
    ): "manage_composite_repository",
}
GUARDED_CALLERS = {
    ("routers/inline_repos.py", "activate_repository"),
    ("services/activated_repo_audited_ops.py", "activate_repository_for_user"),
}


def _activation_callers() -> Dict[Tuple[str, str], ast.AST]:
    """(relative file, innermost enclosing function) -> that function node."""
    callers: Dict[Tuple[str, str], ast.AST] = {}
    for path in sorted(SERVER_DIR.rglob("*.py")):
        rel = path.relative_to(SERVER_DIR).as_posix()
        if rel == MANAGER_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nested = {
                id(n)
                for inner in ast.walk(func)
                if inner is not func
                and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                for n in ast.walk(inner)
            }
            for node in ast.walk(func):
                if (
                    id(node) not in nested
                    and isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "activate_repository"
                ):
                    callers[(rel, func.name)] = func
    return callers


def _called_names(func: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_every_activation_caller_is_a_known_guarded_door():
    callers = _activation_callers()

    assert set(callers) == set(MCP_CALLERS) | GUARDED_CALLERS, sorted(callers)


def test_mcp_activation_tools_get_the_dispatcher_refusal():
    assert set(MCP_CALLERS.values()) <= _NEW_ALIAS_PARAM_TOOLS


def test_other_activation_doors_call_the_shared_guard():
    callers = _activation_callers()

    for key in GUARDED_CALLERS:
        assert _called_names(callers[key]) & GUARDS, key
