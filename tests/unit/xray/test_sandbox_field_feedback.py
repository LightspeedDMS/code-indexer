"""Tests for Story #993 field feedback improvements to PythonEvaluatorSandbox.

Following the Rust-only migration, all constructs removed from ALLOWED_NODES
(imports, lambda, FunctionDef) are now rejected.  Tests updated accordingly.

Covers:
  Improvement 1: Sandbox whitelist — rejection of non-transpilable constructs
  AC1.1: import re now fails (Import removed from ALLOWED_NODES)
  AC1.2: import os still fails
  AC1.3: def helper(node): ... now fails (FunctionDef removed from ALLOWED_NODES)
  AC1.4: lambda n: n.type == 'if_statement' now fails (Lambda removed)
  AC1.5: import collections, itertools, functools all fail
  AC1.6: from os.path import join still fails
"""

from __future__ import annotations

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_fails(code: str, expected_fragment: str) -> None:
    """Assert that code fails validation and reason contains expected_fragment."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is False, f"Expected ok=False but got ok=True for code: {code!r}"
    assert expected_fragment in (result.reason or ""), (
        f"Expected {expected_fragment!r} in reason {result.reason!r} for code: {code!r}"
    )


# ===========================================================================
# AC1.1: import re now fails — Import removed from ALLOWED_NODES
# ===========================================================================


class TestImportReAC1_1:
    """AC1.1: import re now fails (Import removed from ALLOWED_NODES)."""

    def test_import_re_validates_rejected(self):
        _validate_fails("import re\nreturn {'matches': [], 'value': None}", "Import")


# ===========================================================================
# AC1.2: import os fails
# ===========================================================================


class TestImportOsBlockedAC1_2:
    """AC1.2: import os fails — Import not in ALLOWED_NODES."""

    def test_import_os_fails(self):
        _validate_fails("import os\nreturn {'matches': [], 'value': None}", "Import")


# ===========================================================================
# AC1.3: def helper(node): ... now fails — FunctionDef removed from ALLOWED_NODES
# ===========================================================================


class TestFunctionDefAC1_3:
    """AC1.3: def helper(node): ... validates OK — FunctionDef remains in ALLOWED_NODES."""

    def test_function_def_helper_node_validates_ok(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate(
            "def helper(node):\n"
            "    return node.type == 'function_definition'\n"
            "return {'matches': [], 'value': helper(node)}"
        )
        assert result.ok is True


# ===========================================================================
# AC1.4: lambda n: ... now fails — Lambda removed from ALLOWED_NODES
# ===========================================================================


class TestLambdaAC1_4:
    """AC1.4: lambda n: n.type == 'if_statement' now fails (Lambda removed)."""

    def test_lambda_validates_rejected(self):
        _validate_fails(
            "f = lambda n: n.type == 'if_statement'\n"
            "return {'matches': [], 'value': f(node)}",
            "Lambda",
        )


# ===========================================================================
# AC1.5: import collections/itertools/functools now fail
# ===========================================================================


class TestWhitelistedModulesAC1_5:
    """AC1.5: import collections, import itertools, import functools all fail."""

    def test_import_collections_validates_rejected(self):
        _validate_fails(
            "import collections\nreturn {'matches': [], 'value': None}", "Import"
        )

    def test_import_itertools_validates_rejected(self):
        _validate_fails(
            "import itertools\nreturn {'matches': [], 'value': None}", "Import"
        )

    def test_import_functools_validates_rejected(self):
        _validate_fails(
            "import functools\nreturn {'matches': [], 'value': None}", "Import"
        )


# ===========================================================================
# AC1.6: from os.path import join still fails
# ===========================================================================


class TestFromImportBlockedAC1_6:
    """AC1.6: from os.path import join fails — Import not in ALLOWED_NODES."""

    def test_from_os_path_import_join_fails(self):
        _validate_fails(
            "from os.path import join\nreturn {'matches': [], 'value': None}",
            "Import",
        )
