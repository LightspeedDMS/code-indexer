"""Every cached-content handle is created with its creator recorded.

The generic retrieval doors serve an entry only to the user recorded as its
owner, so a producer that stores without ``owner=`` creates handles nobody
can fetch. This structural test scans the server source: every call to a
handle-producing PayloadCache method or truncation helper passes an
``owner=`` keyword, and no such helper is handed around uncalled (e.g. as a
``run_sync`` callback), where the keyword could not be checked.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional

SERVER_SRC = Path(__file__).resolve().parents[4] / "src/code_indexer/server"

# PayloadCache methods that create a handle for a caller.
HANDLE_PRODUCING_METHODS = {"store_batch", "store_batch_with_keys", "truncate_result"}

# Helpers that store into PayloadCache on behalf of a caller.
HANDLE_PRODUCING_HELPERS = {
    "_apply_payload_truncation",
    "_apply_fts_payload_truncation",
    "_apply_regex_payload_truncation",
    "_apply_temporal_payload_truncation",
    "_apply_scip_payload_truncation",
    "_apply_rest_semantic_truncation",
    "_apply_rest_fts_truncation",
    "_apply_search_truncation",
    "truncate_result_fields",
    "store_pages",
    "_finalize_truncated",
    "_truncate_field",
    "_truncate_regex_field",
    "_truncate_xray_result",
    "_truncate_xray_batch_result",
    "_truncate_graph_result",
    "mcp_truncate",
    "truncate_and_cache",
    "_apply_multi_truncation",
    "_apply_multi_scip_truncation",
    "_run_multi_repo_analyze_graph",
}

# The storage layer defines these methods; the backends' own store methods
# take the owner from the facade.
EXCLUDED_DIRS = ("storage",)


def _callee_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _receiver_text(func: ast.expr) -> str:
    return ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""


def _is_producer_call(call: ast.Call) -> bool:
    name = _callee_name(call.func)
    if name in HANDLE_PRODUCING_HELPERS or name in HANDLE_PRODUCING_METHODS:
        return True
    return name == "store" and "payload_cache" in _receiver_text(call.func)


def _offenders_in(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    called_funcs = set()
    offenders: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called_funcs.add(id(node.func))
            if _is_producer_call(node) and not any(
                kw.arg == "owner" for kw in node.keywords
            ):
                offenders.append(f"{path}:{node.lineno} {ast.unparse(node.func)}()")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        name = _callee_name(node)
        if (
            name in HANDLE_PRODUCING_HELPERS
            and id(node) not in called_funcs
            and isinstance(node.ctx, ast.Load)
        ):
            offenders.append(f"{path}:{node.lineno} {name} referenced uncalled")
    return offenders


def test_every_handle_producing_call_records_the_creator() -> None:
    offenders: List[str] = []
    for path in sorted(SERVER_SRC.rglob("*.py")):
        relative = path.relative_to(SERVER_SRC)
        if relative.parts[0] in EXCLUDED_DIRS:
            continue
        offenders.extend(_offenders_in(path))

    assert not offenders, (
        "Handle-producing calls must pass owner= (the creating user):\n"
        + "\n".join(offenders)
    )


def test_scanner_detects_a_call_without_owner(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(payload_cache, rows, user):\n"
        "    payload_cache.store_batch(rows)\n"
        "    payload_cache.store('x')\n"
        "    _apply_payload_truncation(rows, owner=user.username)\n"
        "    run_sync(_truncate_graph_result, rows)\n"
    )

    found = _offenders_in(sample)

    assert len(found) == 3
    assert any("store_batch" in f for f in found)
    assert any("payload_cache.store()" in f for f in found)
    assert any("_truncate_graph_result referenced uncalled" in f for f in found)
