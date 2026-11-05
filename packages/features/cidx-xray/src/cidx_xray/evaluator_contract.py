"""Single source of truth for evaluator construct allowlists.

Both sandbox.py (validator) and transpiler.py (emitter) should reference
these definitions to prevent drift.  Any construct in TRANSPILABLE_NODES
must have a working emitter in transpiler.py; any construct NOT listed
here must be rejected by the validator.

This module is imported by the parity test suite (test_construct_parity.py)
to verify coherence at CI time.
"""

import ast


TRANSPILABLE_NODES: tuple[type, ...] = (
    ast.Call,
    ast.Name,
    ast.Attribute,
    ast.Constant,
    ast.Subscript,
    ast.Slice,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Return,
    ast.Expr,
    ast.Module,
    ast.Load,
    ast.boolop,
    ast.cmpop,
    ast.unaryop,
    ast.expr_context,
    ast.Assign,
    ast.AugAssign,
    ast.operator,
    ast.comprehension,
    ast.GeneratorExp,
    ast.ListComp,
    ast.IfExp,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.BinOp,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
)

TRANSPILABLE_BUILTINS: frozenset[str] = frozenset(
    {
        "len",
        "any",
        "all",
        "range",
        "enumerate",
        "sorted",
        "min",
        "max",
    }
)
