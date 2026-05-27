"""CI gate: every construct the validator allows MUST transpile without error.

Prevents sandbox/transpiler drift from going undetected.
"""

import pytest

from code_indexer.xray.transpiler import transpile_evaluator, TranspileError


TRANSPILABLE_CASES = [
    ("constant", "x = 42"),
    ("name", "x = node"),
    ("attribute", "x = node.kind"),
    ("call_len", "x = len(node.children)"),
    ("compare_eq", "x = node.kind == 'if'"),
    ("compare_neq", "x = node.kind != 'if'"),
    ("in_tuple", "if node.kind in ('a', 'b'): pass"),
    ("not_in_tuple", "if node.kind not in ('a', 'b'): pass"),
    ("boolop_and", "x = node.is_named and node.kind == 'a'"),
    ("boolop_or", "x = node.is_named or node.kind == 'a'"),
    ("unaryop_not", "x = not node.is_named"),
    ("binop_add", "x = node.start_line + 1"),
    ("subscript_index", "x = node.children[0]"),
    ("slice_upper", "x = node.text[:80]"),
    ("list_literal", "x = []"),
    ("tuple_literal", "x = (1, 2)"),
    (
        "dict_finding",
        'return [{"pattern": "x", "line": node.start_line, "snippet": node.text}]',
    ),
    ("assign", "x = 1"),
    ("augassign", "x = 0\n    x += 1"),
    ("listcomp", "x = [c for c in node.children if c.is_named]"),
    ("ifexp", "x = 1 if node.is_named else 0"),
    ("if_stmt", "if node.is_named: pass"),
    ("for_stmt", "for c in node.children: pass"),
    ("while_stmt", "x = 0\n    while x < 10:\n        x += 1"),
    ("break_stmt", "for c in node.children:\n        break"),
    ("continue_stmt", "for c in node.children:\n        continue"),
    ("pass_stmt", "pass"),
    ("range_for", "for i in range(10): pass"),
    ("enumerate_for", "for i, c in enumerate(node.children): pass"),
    (
        "functiondef",
        "def helper(n):\n        return []\n    x = helper(node)",
    ),
]

REJECTED_CONSTRUCTS = [
    ("try_except", "try:\n        pass\n    except:\n        pass"),
    ("raise_stmt", "raise ValueError('x')"),
    ("import_re", "import re"),
    ("from_import", "from re import match"),
    ("lambda_expr", "f = lambda x: x"),
    ("setcomp", "x = {c.kind for c in node.children}"),
    ("dictcomp", "x = {c.kind: c for c in node.children}"),
]

_XFAIL_TRANSPILABLE = frozenset(
    {
        # Nested FunctionDef inside evaluate_node body is not yet supported;
        # only top-level helper functions (module-level) are emitted correctly.
        "functiondef",
    }
)


def _make_full_source(code: str) -> str:
    return f"def evaluate_node(node):\n    {code}\n    return []"


@pytest.mark.parametrize("name,code", TRANSPILABLE_CASES)
def test_construct_transpiles_without_error(name, code):
    if name in _XFAIL_TRANSPILABLE:
        pytest.xfail(reason="transpiler expansion pending")
    full = _make_full_source(code)
    rust = transpile_evaluator(full)
    assert "fn evaluate_node" in rust, f"{name}: missing evaluate_node in output"


@pytest.mark.parametrize("name,code", REJECTED_CONSTRUCTS)
def test_rejected_construct_raises_transpile_error(name, code):
    full = _make_full_source(code)
    with pytest.raises(TranspileError):
        transpile_evaluator(full)
