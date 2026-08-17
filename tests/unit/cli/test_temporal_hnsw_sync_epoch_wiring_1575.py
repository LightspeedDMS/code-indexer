"""Bug #1575 Part C review fix (Defect 3a, remaining temporal-path gap):
the `cidx index --index-commits` temporal branch constructs its own
``FilesystemVectorStore`` directly (bypassing ``BackendFactory`` /
``FilesystemBackend.get_vector_store_client()``, which already resolves
``hnsw_sync_epoch_enabled`` correctly for the SEMANTIC `--fts` path via
``is_postgres_storage_mode()`` / the ``CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE``
env var). Per this project's own architecture notes (Bug #1529), temporal
collections use the SAME ``FilesystemVectorStore``/``HNSWIndexManager``
machinery as semantic collections ("collection naming, shard discovery,
HNSW, projection matrices, progress metadata and reconcile all work
unchanged") -- so the epoch-sync mechanism is NOT architecturally
irrelevant to temporal, and leaving its construction unwired is a genuine
bypass of the AC46 cluster fail-closed gate for temporal collections
specifically.

This is a structural (AST) guard, mirroring
test_temporal_index_layout_wiring_1528.py exactly: the construction lives
deep inside the ``index`` command body, unreachable by a unit test without
running a real index.
"""

from __future__ import annotations

import ast
import inspect
from typing import List

from code_indexer import cli as cli_module

EPOCH_KEYWORD = "hnsw_sync_epoch_enabled"


def _index_command_function_node() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(cli_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "index":
            return node
    raise AssertionError("cli.py no longer defines an `index` command function")


def _temporal_branch_nodes(index_fn: ast.FunctionDef) -> List[ast.stmt]:
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


def test_temporal_branch_constructs_store_with_explicit_hnsw_sync_epoch_flag() -> None:
    temporal_branch = _temporal_branch_nodes(_index_command_function_node())
    calls = _filesystem_vector_store_calls(temporal_branch)
    assert calls, (
        "no FilesystemVectorStore construction found in cli.py's temporal "
        "`if index_commits:` branch -- this guard has drifted from the real code"
    )

    missing = [
        call.lineno
        for call in calls
        if not any(kw.arg == EPOCH_KEYWORD for kw in call.keywords)
    ]
    assert missing == [], (
        f"FilesystemVectorStore constructed WITHOUT {EPOCH_KEYWORD} in "
        f"cli.py's temporal branch at line(s) {missing}: a server spawning "
        f"this child in postgres/cluster storage mode has no way to "
        f"disable the AC46 epoch-sync mechanism for the temporal "
        f"collection, unlike the semantic `--fts` path (which resolves it "
        f"via BackendFactory/FilesystemBackend)"
    )
