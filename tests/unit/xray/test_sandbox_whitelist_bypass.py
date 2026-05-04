"""User Mandate Section 1: Whitelist Bypass Attempts (Story #970).

Each test asserts:
  (a) validate() returns ok=False
  (b) the rejected node type name appears in the reason string
  (c) NO subprocess was spawned — verified by monkey-patching
      multiprocessing context so Process() raises if called.

Python 3.9 is the runtime; ast.Match does not exist, so match/case tests
are skipped with an appropriate marker.
"""

from __future__ import annotations

import ast
from unittest.mock import patch

import pytest

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_rejected(code: str, expected_node_fragment: str) -> None:
    """Assert validation rejects *code* and mentions *expected_node_fragment*."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is False, f"Expected rejection but got ok=True for: {code!r}"
    assert expected_node_fragment in result.reason, (
        f"Expected {expected_node_fragment!r} in reason {result.reason!r} "
        f"for code: {code!r}"
    )


def _assert_no_subprocess_spawned(code: str) -> None:
    """Assert that run() does not spawn any subprocess when validation fails.

    Strategy: patch multiprocessing.get_context so that if it is ever called
    it raises RuntimeError.  When validation fails, get_context must NOT be
    called, so no RuntimeError is raised and the function returns normally.
    The result must be validation_failed, which also proves the subprocess
    path was never entered.
    """
    sb = PythonEvaluatorSandbox()

    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    root = engine.parse("x = 1", "python")
    node = root

    def _raise_if_called(*args, **kwargs):
        raise RuntimeError(
            "get_context called — subprocess was attempted despite validation failure"
        )

    with patch(
        "code_indexer.xray.sandbox.multiprocessing.get_context", _raise_if_called
    ):
        result = sb.run(
            code,
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure == "validation_failed", (
        f"Expected validation_failed but got {result.failure!r}"
    )


# ---------------------------------------------------------------------------
# Section 1 tests — one per pattern
# ---------------------------------------------------------------------------


class TestWhitelistBypassAttempts:
    # --- Import / ImportFrom ---

    def test_import_os_rejected(self):
        _assert_rejected("import os", "Import")
        _assert_no_subprocess_spawned("import os")

    def test_import_subprocess_rejected(self):
        _assert_rejected("import subprocess", "Import")
        _assert_no_subprocess_spawned("import subprocess")

    def test_from_os_import_system_rejected(self):
        _assert_rejected("from os import system", "ImportFrom")
        _assert_no_subprocess_spawned("from os import system")

    # --- FunctionDef ---

    def test_function_def_rejected(self):
        _assert_rejected("def helper(): return 1\nreturn helper()", "FunctionDef")
        _assert_no_subprocess_spawned("def helper(): return 1\nreturn helper()")

    # --- Lambda ---

    def test_lambda_rejected(self):
        _assert_rejected("return (lambda x: x > 0)(5)", "Lambda")
        _assert_no_subprocess_spawned("return (lambda x: x > 0)(5)")

    # --- ClassDef ---

    def test_class_def_rejected(self):
        _assert_rejected("class X: pass", "ClassDef")
        _assert_no_subprocess_spawned("class X: pass")

    # --- Try / ExceptHandler ---

    def test_try_except_rejected(self):
        code = "try:\n    x = 1\nexcept Exception:\n    x = 2"
        # Python 3.9 uses ast.Try; newer may emit TryStar
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        assert "Try" in result.reason or "ExceptHandler" in result.reason
        _assert_no_subprocess_spawned(code)

    # --- For loop ---

    def test_for_loop_rejected(self):
        _assert_rejected("for x in range(10): pass", "For")
        _assert_no_subprocess_spawned("for x in range(10): pass")

    # --- While loop ---

    def test_while_loop_rejected(self):
        _assert_rejected("while True: pass", "While")
        _assert_no_subprocess_spawned("while True: pass")

    # --- With ---

    def test_with_statement_rejected(self):
        _assert_rejected("with open('x') as f: pass", "With")
        _assert_no_subprocess_spawned("with open('x') as f: pass")

    # --- ListComp ---

    def test_list_comp_accepted(self):
        # ListComp was added to ALLOWED_NODES (field-feedback fix #7).
        # [x for x in items] is a canonical AST-search pattern.
        sb = PythonEvaluatorSandbox()
        result = sb.validate("[x for x in range(10)]")
        assert result.ok is True, (
            f"ListComp should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- DictComp ---

    def test_dict_comp_accepted(self):
        # DictComp was added to ALLOWED_NODES (field-feedback fix #7).
        # Use {x: 1 for x in range(10)} — avoids BinOp (x*2 is not whitelisted).
        sb = PythonEvaluatorSandbox()
        code = "{x: 1 for x in range(10)}"
        result = sb.validate(code)
        assert result.ok is True, (
            f"DictComp should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- SetComp ---

    def test_set_comp_accepted(self):
        # SetComp was added to ALLOWED_NODES (field-feedback fix #7).
        sb = PythonEvaluatorSandbox()
        code = "{x for x in range(10)}"
        result = sb.validate(code)
        assert result.ok is True, (
            f"SetComp should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- GeneratorExp ---

    def test_generator_exp_accepted(self):
        # GeneratorExp was added to ALLOWED_NODES (field-feedback fix #7).
        # any(n.type == 'call' for n in node.named_children) is the canonical example.
        sb = PythonEvaluatorSandbox()
        code = "return list(x for x in range(10))"
        result = sb.validate(code)
        assert result.ok is True, (
            f"GeneratorExp should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- Yield ---

    def test_yield_rejected(self):
        # yield is only syntactically valid inside a function, so wrap it.
        # However the validator rejects it either way (Yield or FunctionDef).
        code = "yield 1"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        # May fail on Yield or SyntaxError (yield outside function)
        assert result.reason is not None
        _assert_no_subprocess_spawned(code)

    # --- AsyncFunctionDef ---

    def test_async_function_def_rejected(self):
        code = "async def f(): pass"
        _assert_rejected(code, "AsyncFunctionDef")
        _assert_no_subprocess_spawned(code)

    # --- Await ---

    def test_await_rejected(self):
        # await is only valid inside an async function
        code = "async def f():\n    return await something()"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        # Will fail on AsyncFunctionDef or Await — both rejected
        assert "AsyncFunctionDef" in result.reason or "Await" in result.reason
        _assert_no_subprocess_spawned(code)

    # --- Global ---

    def test_global_rejected(self):
        code = "global x\nreturn x"
        _assert_rejected(code, "Global")
        _assert_no_subprocess_spawned(code)

    # --- Nonlocal ---

    def test_nonlocal_rejected(self):
        code = "nonlocal x\nreturn x"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        # nonlocal outside function raises SyntaxError — validation catches it
        assert result.reason is not None
        _assert_no_subprocess_spawned(code)

    # --- Delete ---

    def test_delete_rejected(self):
        code = "del x"
        _assert_rejected(code, "Delete")
        _assert_no_subprocess_spawned(code)

    # --- Raise ---

    def test_raise_rejected(self):
        code = "raise ValueError()"
        _assert_rejected(code, "Raise")
        _assert_no_subprocess_spawned(code)

    # --- Assert ---

    def test_assert_rejected(self):
        code = "assert x > 0"
        _assert_rejected(code, "Assert")
        _assert_no_subprocess_spawned(code)

    # --- Assign ---

    def test_assign_accepted(self):
        # Assign was added to ALLOWED_NODES (field-feedback fix #7).
        # x = node.named_children; return len(x) > 0 is a common bind-then-use pattern.
        sb = PythonEvaluatorSandbox()
        code = "x = 5"
        result = sb.validate(code)
        assert result.ok is True, (
            f"Assign should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- AugAssign ---

    def test_aug_assign_accepted(self):
        # AugAssign was added to ALLOWED_NODES (field-feedback fix #7).
        # count += 1 inside evaluator code is a common accumulation pattern.
        sb = PythonEvaluatorSandbox()
        code = "x += 1"
        result = sb.validate(code)
        assert result.ok is True, (
            f"AugAssign should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- AnnAssign ---

    def test_ann_assign_rejected(self):
        code = "x: int = 5"
        _assert_rejected(code, "AnnAssign")
        _assert_no_subprocess_spawned(code)

    # --- NamedExpr (walrus) ---

    def test_named_expr_walrus_rejected(self):
        # := (walrus) requires Python 3.8+; present in 3.9
        code = "return (x := 5)"
        _assert_rejected(code, "NamedExpr")
        _assert_no_subprocess_spawned(code)

    # --- JoinedStr (f-string) ---

    def test_f_string_joined_str_rejected(self):
        code = 'return f"{node.type}"'
        _assert_rejected(code, "JoinedStr")
        _assert_no_subprocess_spawned(code)

    # --- Match / MatchValue (Python 3.10+) ---

    @pytest.mark.skipif(
        not hasattr(ast, "Match"),
        reason="ast.Match only exists in Python 3.10+",
    )
    def test_match_statement_rejected(self):
        code = "match x:\n    case 1:\n        pass"
        _assert_rejected(code, "Match")
        _assert_no_subprocess_spawned(code)

    # --- Starred unpacking ---

    def test_starred_unpacking_rejected(self):
        code = "*x, y = [1, 2, 3]"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        # Fails on Assign, Starred, or Store — any is acceptable
        assert result.reason is not None
        _assert_no_subprocess_spawned(code)

    # --- If statement ---

    def test_if_statement_rejected(self):
        code = "if x: pass"
        _assert_rejected(code, "If")
        _assert_no_subprocess_spawned(code)

    # --- IfExp (ternary) ---

    def test_if_exp_ternary_accepted(self):
        # IfExp was added to ALLOWED_NODES (field-feedback fix #7).
        # result = True if node.type == 'X' else False is a useful shorthand.
        sb = PythonEvaluatorSandbox()
        code = "return x if y else z"
        result = sb.validate(code)
        assert result.ok is True, (
            f"IfExp should be accepted after whitelist expansion; "
            f"got reason={result.reason!r}"
        )

    # --- Pass ---

    def test_pass_rejected(self):
        code = "pass"
        _assert_rejected(code, "Pass")
        _assert_no_subprocess_spawned(code)

    # --- Break and Continue ---

    def test_break_rejected(self):
        # break outside loop is a SyntaxError or ast.Break node rejection
        code = "break"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        assert result.reason is not None
        _assert_no_subprocess_spawned(code)

    def test_continue_rejected(self):
        # continue outside loop is a SyntaxError or ast.Continue node rejection
        code = "continue"
        sb = PythonEvaluatorSandbox()
        result = sb.validate(code)
        assert result.ok is False
        assert result.reason is not None
        _assert_no_subprocess_spawned(code)
