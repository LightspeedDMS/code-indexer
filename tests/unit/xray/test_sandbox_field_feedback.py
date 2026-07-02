"""Tests for Story #993 field feedback improvements to PythonEvaluatorSandbox.

Covers:
  Improvement 1: Sandbox whitelist expansion
  AC1.1: import re validates OK and re.search/findall/compile work in sandbox
  AC1.2: import os fails with descriptive error about "os" not being whitelisted
  AC1.3: def helper(node): ... validates OK and can be called
  AC1.4: lambda n: n.type == 'if_statement' validates OK and works
  AC1.5: import collections, import itertools, import functools all work
  AC1.6: from os.path import join fails (top-level "os" not whitelisted)
"""

from __future__ import annotations

import textwrap

from code_indexer.xray.sandbox import PythonEvaluatorSandbox, ValidationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "def foo(): pass", lang: str = "python"):
    """Return (node, root) for the given source."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _validate_ok(code: str) -> ValidationResult:
    """Assert that code passes validation and return the result."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is True, (
        f"Expected ok=True but got ok=False, reason={result.reason!r} "
        f"for code: {code!r}"
    )
    return result


def _validate_fails(code: str, expected_fragment: str) -> None:
    """Assert that code fails validation and reason contains expected_fragment."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is False, f"Expected ok=False but got ok=True for code: {code!r}"
    assert expected_fragment in (result.reason or ""), (
        f"Expected {expected_fragment!r} in reason {result.reason!r} for code: {code!r}"
    )


def _run_ok(code: str, source: str = "def foo(): pass"):
    """Run code in the sandbox and assert no failure."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root(source)
    result = sb.run(
        code,
        node=node,
        root=root,
        source=source,
        lang="python",
        file_path="/test/foo.py",
        match_positions=[],
    )
    assert result.failure is None, (
        f"Expected success but got failure={result.failure!r}, detail={result.detail!r} "
        f"for code: {code!r}"
    )
    return result


def _dedent(code: str) -> str:
    """Remove leading newline and common indent from multiline code strings."""
    return textwrap.dedent(code).strip()


# Source fixture for execution tests: contains a known function definition
_FIXTURE_SOURCE = "def foo(): pass"


# ===========================================================================
# AC1.1: import re validates OK and re.search/findall/compile work in sandbox
# ===========================================================================


class TestImportReAC1_1:
    """AC1.1: import re validates OK and re.search/findall/compile work in sandbox."""

    def test_import_re_validates_ok(self):
        _validate_ok("import re\nreturn {'matches': [], 'value': None}")

    def test_import_re_search_executes(self):
        # source is "def foo(): pass" — re.search(r'def \w+') must match
        code = _dedent("""
            import re
            result = re.search(r'def \\w+', source)
            return {'matches': [], 'value': result is not None}
        """)
        result = _run_ok(code, source=_FIXTURE_SOURCE)
        assert result.value["value"] is True

    def test_import_re_findall_executes(self):
        # source is "def foo(): pass" — words are ['def', 'foo', 'pass']
        code = _dedent("""
            import re
            found = re.findall(r'\\w+', source)
            return {'matches': [], 'value': len(found)}
        """)
        result = _run_ok(code, source=_FIXTURE_SOURCE)
        assert result.value["value"] == 3

    def test_import_re_compile_executes(self):
        # source is "def foo(): pass" — pattern 'foo' must match
        code = _dedent("""
            import re
            pattern = re.compile(r'foo')
            m = pattern.search(source)
            return {'matches': [], 'value': m is not None}
        """)
        result = _run_ok(code, source=_FIXTURE_SOURCE)
        assert result.value["value"] is True


# ===========================================================================
# AC1.2: import os fails with descriptive error about "os" not being whitelisted
# ===========================================================================


class TestImportOsBlockedAC1_2:
    """AC1.2: import os fails with descriptive error about 'os' not being whitelisted."""

    def test_import_os_fails_with_os_in_reason(self):
        _validate_fails("import os\nreturn {'matches': [], 'value': None}", "os")


# ===========================================================================
# AC1.3: def helper(node): ... validates OK and can be called
# ===========================================================================


class TestFunctionDefAC1_3:
    """AC1.3: def helper(node): ... validates OK and can be called."""

    def test_function_def_helper_node_validates_ok(self):
        # helper parameter named 'node' — the exact convention from the AC
        code = _dedent("""
            def helper(node):
                return node.type == 'function_definition'
            return {'matches': [], 'value': helper(node)}
        """)
        _validate_ok(code)

    def test_function_def_helper_node_executes_and_returns_correct_value(self):
        # Root of "def foo(): pass" is 'module' — helper(node) returns False
        code = _dedent("""
            def helper(node):
                return node.type == 'function_definition'
            return {'matches': [], 'value': helper(node)}
        """)
        result = _run_ok(code, source=_FIXTURE_SOURCE)
        # Root node type is 'module', not 'function_definition'
        assert result.value["value"] is False


# ===========================================================================
# AC1.4: lambda n: n.type == 'if_statement' validates OK and works
# ===========================================================================


class TestLambdaAC1_4:
    """AC1.4: lambda n: n.type == 'if_statement' validates OK and works."""

    def test_lambda_validates_ok(self):
        _validate_ok(
            "f = lambda n: n.type == 'if_statement'\n"
            "return {'matches': [], 'value': f(node)}"
        )

    def test_lambda_executes_and_returns_correct_value(self):
        # Root of "def foo(): pass" is 'module', not 'if_statement'
        code = (
            "f = lambda n: n.type == 'if_statement'\n"
            "return {'matches': [], 'value': f(node)}"
        )
        result = _run_ok(code, source=_FIXTURE_SOURCE)
        assert result.value["value"] is False


# ===========================================================================
# AC1.5: import collections, import itertools, import functools all work
# ===========================================================================


class TestWhitelistedModulesAC1_5:
    """AC1.5: import collections, import itertools, import functools all work."""

    def test_import_collections_validates_ok(self):
        _validate_ok("import collections\nreturn {'matches': [], 'value': None}")

    def test_import_itertools_validates_ok(self):
        _validate_ok("import itertools\nreturn {'matches': [], 'value': None}")

    def test_import_functools_validates_ok(self):
        _validate_ok("import functools\nreturn {'matches': [], 'value': None}")

    def test_import_collections_counter_executes(self):
        code = _dedent("""
            import collections
            counter = collections.Counter(['a', 'b', 'a'])
            return {'matches': [], 'value': counter['a']}
        """)
        result = _run_ok(code)
        assert result.value["value"] == 2

    def test_import_itertools_chain_executes(self):
        code = _dedent("""
            import itertools
            chained = list(itertools.chain([1, 2], [3, 4]))
            return {'matches': [], 'value': len(chained)}
        """)
        result = _run_ok(code)
        assert result.value["value"] == 4

    def test_import_functools_reduce_executes(self):
        code = _dedent("""
            import functools
            total = functools.reduce(lambda a, b: a + b, [1, 2, 3])
            return {'matches': [], 'value': total}
        """)
        result = _run_ok(code)
        assert result.value["value"] == 6


# ===========================================================================
# AC1.6: from os.path import join fails (top-level "os" not whitelisted)
# ===========================================================================


class TestFromImportBlockedAC1_6:
    """AC1.6: from os.path import join fails (top-level 'os' not whitelisted)."""

    def test_from_os_path_import_join_fails_with_os_in_reason(self):
        _validate_fails(
            "from os.path import join\nreturn {'matches': [], 'value': None}",
            "os",
        )
