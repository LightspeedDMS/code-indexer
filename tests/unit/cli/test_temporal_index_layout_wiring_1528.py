"""Bug #1528 (Codex-validated compounding finding): the `cidx index
--index-commits` temporal branch constructed its own
``FilesystemVectorStore`` WITHOUT passing
``use_chunks_db_for_new_collections`` at all -- so an explicit
``--new-collection-layout`` choice (notably the server's own
``--new-collection-layout=chunks_db``, appended to EVERY server-spawned
temporal index child by ``append_server_layout_args``) was silently
discarded on the temporal path.

This is a structural (AST) guard rather than a behavioural test because the
construction lives deep inside the ``index`` command body, after config
loading, lock acquisition and temporal-migration steps that a unit test
cannot reach without running a real index. The guard parses the REAL
``cli.py`` source, isolates ONLY the ``if index_commits:`` temporal branch
(never the surrounding semantic paths), and fails if any
``FilesystemVectorStore`` construction inside it omits the layout keyword --
exactly the shape of the existing
``test_index_command_layout_spawn_site_guard_1488.py`` /
``test_temporal_child_env_spawn_site_guard_1457.py`` guards.
"""

from __future__ import annotations

import ast
import inspect
from typing import List

from code_indexer import cli as cli_module

LAYOUT_KEYWORD = "use_chunks_db_for_new_collections"


def _index_command_function_node() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(cli_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "index":
            return node
    raise AssertionError("cli.py no longer defines an `index` command function")


def _temporal_branch_nodes(index_fn: ast.FunctionDef) -> List[ast.stmt]:
    """The body of the ``if index_commits:`` branch -- the temporal path.

    Only a BARE ``if index_commits:`` test qualifies; the several
    ``if <x> and not index_commits:`` validation guards in the same function
    are deliberately excluded.
    """
    for node in ast.walk(index_fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "index_commits":
                return list(node.body)
    raise AssertionError(
        "cli.py's `index` command no longer has a bare `if index_commits:` "
        "temporal branch -- this guard has drifted from the real code"
    )


def _filesystem_vector_store_calls(nodes: List[ast.stmt]) -> List[ast.Call]:
    calls: List[ast.Call] = []
    for root in nodes:
        for candidate in ast.walk(root):
            if not isinstance(candidate, ast.Call):
                continue
            func = candidate.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name == "FilesystemVectorStore":
                calls.append(candidate)
    return calls


def test_temporal_branch_constructs_store_with_explicit_layout() -> None:
    temporal_branch = _temporal_branch_nodes(_index_command_function_node())
    calls = _filesystem_vector_store_calls(temporal_branch)
    assert calls, (
        "no FilesystemVectorStore construction found in cli.py's temporal "
        "`if index_commits:` branch -- this guard has drifted from the real code"
    )

    missing = [
        call.lineno
        for call in calls
        if not any(kw.arg == LAYOUT_KEYWORD for kw in call.keywords)
    ]
    assert missing == [], (
        f"FilesystemVectorStore constructed WITHOUT {LAYOUT_KEYWORD} in "
        f"cli.py's temporal branch at line(s) {missing}: an explicit "
        f"--new-collection-layout (including the server's own chunks_db "
        f"child arg) would be silently discarded there (Bug #1528)"
    )
