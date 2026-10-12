"""Bug #1575 Fix 4, re-pinned for Story #2120 -- structural (AST-based)
regression test of ``SmartIndexer._do_reconcile_with_database``.

History: the non-git reconcile once deleted each modified file's old chunks
with a per-file ``delete_by_filter()`` BEFORE ``begin_indexing()``, so every
delete took the out-of-session path and persisted ``path_index.bin`` on its
own (5.6x slower incremental refresh on a 4000-file collection; Fix 4 moved
the loop inside the session and fixed a swapped-argument call that had
never deleted anything).

Story #2120 removed that pre-delete entirely (the upsert replaces a file's
rows itself), and moved the removal of gone paths after processing, as ONE
batched store call. The guarantees pinned here:

* the reconcile makes no per-file ``delete_by_filter()`` call at all;
* the post-processing gone-path removal on the processing path runs after
  ``begin_indexing()`` -- inside the session, so ``end_indexing()`` persists
  the path index once (Bug #2124).

Behaviour is covered by the real-CLI tests in
``tests/unit/services/test_reconcile_no_upfront_delete_2120.py``; this test
parses the real source (``inspect.getsource`` + ``ast``) to lock the shape.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from code_indexer.services.smart_indexer import SmartIndexer

_TARGET_METHOD_NAME = "_do_reconcile_with_database"


def _parse_target_method() -> ast.FunctionDef:
    source = inspect.getsource(getattr(SmartIndexer, _TARGET_METHOD_NAME))
    module = ast.parse(textwrap.dedent(source))
    assert len(module.body) == 1
    func_def = module.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    assert func_def.name == _TARGET_METHOD_NAME
    return func_def


def _find_calls_to_attribute(func_def: ast.FunctionDef, attribute_name: str) -> list:
    """Every ast.Call in func_def whose callee is `<x>.<attribute_name>(...)`,
    in source (lineno) order."""
    calls = []
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attribute_name:
            calls.append(node)
    return sorted(calls, key=lambda call: call.lineno)


def test_reconcile_makes_no_per_file_delete_by_filter_call():
    func_def = _parse_target_method()
    assert _find_calls_to_attribute(func_def, "delete_by_filter") == [], (
        f"{_TARGET_METHOD_NAME} deletes rows file by file again: a modified "
        f"file's rows are replaced by its upsert, and gone paths are removed "
        f"in one batch after processing (Story #2120, Bug #2124)"
    )


def test_post_processing_gone_path_removal_is_inside_the_session():
    func_def = _parse_target_method()
    begin_calls = _find_calls_to_attribute(func_def, "begin_indexing")
    processing_calls = _find_calls_to_attribute(
        func_def, "process_branch_changes_high_throughput"
    )
    removal_calls = _find_calls_to_attribute(func_def, "_remove_reconcile_gone_paths")
    assert len(begin_calls) == 1 and len(processing_calls) == 1
    after_processing = [
        call for call in removal_calls if call.lineno > processing_calls[0].lineno
    ]
    assert len(after_processing) == 1, (
        "expected exactly one gone-path removal after the files are processed"
    )
    assert after_processing[0].lineno > begin_calls[0].lineno
