"""Bug #1575 Fix 4 -- smart_indexer.py's non-git-aware incremental reconcile
path, structural (AST-based) regression test.

``_do_reconcile_with_database`` (``services/smart_indexer.py``) cleans up a
modified file's old chunks via ``delete_by_filter()`` BEFORE calling
``begin_indexing()`` -- so every one of those per-file deletes takes the
out-of-session-persist path (measured: 5.6x slower incremental refresh on
a 4000-file collection; see
``tests/unit/storage/test_filesystem_vector_store_1575_fix4_incremental_delete_session_bracket.py``
for the underlying FilesystemVectorStore-level mechanism this exploits).

While investigating this call site, a SEPARATE, genuine, independently
confirmed bug was found and fixed here too: the ``delete_by_filter()`` call
passed its arguments in the WRONG order --
``delete_by_filter(filter_dict, collection_name)`` -- against the real
``FilesystemVectorStore.delete_by_filter(self, collection_name,
filter_conditions)`` signature. Reproduced live against a real
``FilesystemVectorStore``: the swapped call raises ``TypeError: unhashable
type: 'dict'`` deep inside ``scroll_points``, caught by
``delete_by_filter``'s own broad ``except Exception`` and turned into a
silent ``return False`` -- meaning the non-git reconcile modified-file
cleanup has never actually deleted anything. This fixes ONLY the ONE call
site Fix 4 touches; the other two ``delete_by_filter`` call sites in this
file carry the identical bug and are OUT OF SCOPE here (see the TDD
engineer's final report).

This is a STRUCTURAL test, deliberately AST-based rather than a live
SmartIndexer run: it parses the REAL, unmodified source of
``_do_reconcile_with_database`` (via ``inspect.getsource`` + ``ast.parse``)
and asserts on the call-site SHAPE and ORDER directly -- no mocking, no
fakes, no execution of any indexing machinery at all. This mirrors the
established pattern this codebase already uses for exactly this class of
guarantee (e.g.
``tests/unit/server/services/test_activated_repo_index_manager_branch_delta_semantic_only_1457.py``,
which parses a method's real source via ``ast``/``inspect`` to lock in a
structural invariant a live-execution test cannot cleanly express without
either mocking the system under test or driving the full, expensive
embedding pipeline for an assertion that is really about source shape).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.services.smart_indexer import SmartIndexer

_TARGET_METHOD_NAME = "_do_reconcile_with_database"
_DELETE_METHOD_NAME = "delete_by_filter"
_BEGIN_METHOD_NAME = "begin_indexing"


def _parse_target_method() -> ast.FunctionDef:
    source = inspect.getsource(getattr(SmartIndexer, _TARGET_METHOD_NAME))
    module = ast.parse(textwrap.dedent(source))
    assert len(module.body) == 1
    func_def = module.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    assert func_def.name == _TARGET_METHOD_NAME
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


def test_delete_by_filter_call_passes_collection_name_first():
    """The vector_store_client.delete_by_filter() call in
    _do_reconcile_with_database must pass collection_name as its FIRST
    positional argument -- never the filter conditions dict. This is the
    argument-order bug: swapping the two makes every real
    FilesystemVectorStore.delete_by_filter(self, collection_name,
    filter_conditions) call silently fail (TypeError caught internally,
    turned into a bare `return False`).
    """
    func_def = _parse_target_method()
    delete_calls = _find_calls_to_attribute(func_def, _DELETE_METHOD_NAME)

    assert len(delete_calls) == 1, (
        f"expected exactly ONE delete_by_filter() call site in "
        f"{_TARGET_METHOD_NAME} (the modified-file cleanup loop), found "
        f"{len(delete_calls)} -- if this method's structure changed, "
        f"update this test to target the correct call site explicitly"
    )
    call = delete_calls[0]
    assert len(call.args) >= 1, "delete_by_filter() call has no positional arguments"

    first_arg = call.args[0]
    assert isinstance(first_arg, ast.Name) and first_arg.id == "collection_name", (
        f"expected delete_by_filter()'s FIRST positional argument to be "
        f"the `collection_name` variable, got "
        f"{ast.dump(first_arg)!r} -- this is the swapped-argument bug: "
        f"passing (filter_dict, collection_name) instead of "
        f"(collection_name, filter_dict) against the real "
        f"FilesystemVectorStore.delete_by_filter(self, collection_name, "
        f"filter_conditions) signature"
    )


def test_delete_by_filter_call_is_after_begin_indexing_call():
    """Fix 4 Option (a): the modified-file delete_by_filter() call must be
    located AFTER the begin_indexing() call in source order -- the delete
    loop must be bracketed INSIDE the indexing session (so it is tracked
    via _indexing_session_changes and persisted ONCE at end_indexing(),
    instead of persisting path_index.bin/id_index.bin on its own,
    out-of-session, once per deleted file).
    """
    func_def = _parse_target_method()
    begin_calls = _find_calls_to_attribute(func_def, _BEGIN_METHOD_NAME)
    delete_calls = _find_calls_to_attribute(func_def, _DELETE_METHOD_NAME)

    assert begin_calls, f"no begin_indexing() call found in {_TARGET_METHOD_NAME}"
    assert delete_calls, f"no delete_by_filter() call found in {_TARGET_METHOD_NAME}"

    first_begin_lineno = begin_calls[0].lineno
    first_delete_lineno = delete_calls[0].lineno

    assert first_delete_lineno > first_begin_lineno, (
        f"expected delete_by_filter() (line {first_delete_lineno}) to "
        f"appear AFTER begin_indexing() (line {first_begin_lineno}) in "
        f"{_TARGET_METHOD_NAME}'s source -- the modified-file delete loop "
        f"must be hoisted inside the begin_indexing()/end_indexing() "
        f"session bracket, not run before it starts"
    )
