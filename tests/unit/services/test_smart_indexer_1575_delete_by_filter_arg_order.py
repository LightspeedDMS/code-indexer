"""Bug #1575 follow-up -- the two remaining `delete_by_filter()` call sites in
``services/smart_indexer.py`` carry the identical swapped-argument bug Fix 4
found and fixed at the ``_do_reconcile_with_database`` call site (see
``test_smart_indexer_1575_fix4_ast_delete_order.py``).

The real ``FilesystemVectorStore.delete_by_filter`` signature is::

    def delete_by_filter(self, collection_name: str, filter_conditions: Dict[str, Any]) -> bool:

Both ``_process_files_with_metadata`` and ``delete_file_branch_aware`` called
it as ``delete_by_filter(filter_dict, collection_name)`` -- the WRONG order.
Reproduced against the real ``FilesystemVectorStore``: the swapped call
raises ``TypeError: unhashable type: 'dict'`` deep inside ``scroll_points``
(the filter dict lands in the ``collection_name`` slot, and dicts aren't a
valid dict key -- the internal per-collection lock lookup hashes it), caught
by ``delete_by_filter``'s own broad ``except Exception`` and turned into a
silent ``return False``. Both call sites' chunk-deletion has therefore never
actually deleted anything.

Structural (AST-based) regression test, deliberately mirroring the
established pattern in ``test_smart_indexer_1575_fix4_ast_delete_order.py``
and ``tests/unit/server/services/test_activated_repo_index_manager_branch_delta_semantic_only_1457.py``:
parses the REAL, unmodified source of each target method (via
``inspect.getsource`` + ``ast.parse``) and asserts on the call-site SHAPE and
ORDER directly -- no mocking, no fakes, no execution of any indexing
machinery, and critically no mocking of the SmartIndexer methods under test.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.services.smart_indexer import SmartIndexer

_DELETE_METHOD_NAME = "delete_by_filter"


def _parse_method(method_name: str) -> ast.FunctionDef:
    source = inspect.getsource(getattr(SmartIndexer, method_name))
    module = ast.parse(textwrap.dedent(source))
    assert len(module.body) == 1
    func_def = module.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    assert func_def.name == method_name
    return func_def


def _find_calls_to_attribute(func_def: ast.FunctionDef, attribute_name: str) -> list:
    """Return every ast.Call node in func_def whose callee is
    `<something>.<attribute_name>(...)`, in source (lineno) order."""
    calls = []
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attribute_name:
            calls.append(node)
    return sorted(calls, key=lambda call: call.lineno)


def _assert_first_positional_arg_is_collection_name(
    method_name: str, call: ast.Call
) -> None:
    assert len(call.args) >= 1, (
        f"{method_name}'s delete_by_filter() call has no positional arguments"
    )
    first_arg = call.args[0]
    assert isinstance(first_arg, ast.Name) and first_arg.id == "collection_name", (
        f"expected {method_name}'s delete_by_filter() call to pass "
        f"`collection_name` as its FIRST positional argument, got "
        f"{ast.dump(first_arg)!r} -- this is the swapped-argument bug: "
        f"passing (filter_dict, collection_name) instead of "
        f"(collection_name, filter_dict) against the real "
        f"FilesystemVectorStore.delete_by_filter(self, collection_name, "
        f"filter_conditions) signature"
    )


def test_process_files_with_metadata_delete_by_filter_call_passes_collection_name_first():
    """`_process_files_with_metadata`'s per-modified-file cleanup loop must
    call delete_by_filter(collection_name, filter_dict) -- never the swapped
    (filter_dict, collection_name) order."""
    method_name = "_process_files_with_metadata"
    func_def = _parse_method(method_name)
    delete_calls = _find_calls_to_attribute(func_def, _DELETE_METHOD_NAME)

    assert len(delete_calls) == 1, (
        f"expected exactly ONE delete_by_filter() call site in {method_name}, "
        f"found {len(delete_calls)} -- if this method's structure changed, "
        f"update this test to target the correct call site explicitly"
    )
    _assert_first_positional_arg_is_collection_name(method_name, delete_calls[0])


def test_delete_file_branch_aware_delete_by_filter_call_passes_collection_name_first():
    """`delete_file_branch_aware`'s non-git-aware hard-delete branch must call
    delete_by_filter(collection_name, filter_dict) -- never the swapped
    (filter_dict, collection_name) order."""
    method_name = "delete_file_branch_aware"
    func_def = _parse_method(method_name)
    delete_calls = _find_calls_to_attribute(func_def, _DELETE_METHOD_NAME)

    assert len(delete_calls) == 1, (
        f"expected exactly ONE delete_by_filter() call site in {method_name}, "
        f"found {len(delete_calls)} -- if this method's structure changed, "
        f"update this test to target the correct call site explicitly"
    )
    _assert_first_positional_arg_is_collection_name(method_name, delete_calls[0])
