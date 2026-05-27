"""Python-to-Rust evaluator transpiler (Story #1022 spike).

Translates a Python evaluator function written against the XRayNode API
to equivalent Rust code that can be compiled by the xray-core compiler
pipeline (which adds OwnedNode/EvalFinding preamble and #[no_mangle] epilogue).

Supported Python subset:
- Function definitions (fn evaluate_node + named helpers)
- if / elif / else
- for loops over node fields (children, named_children())
- while loops
- return statements (including finding dicts)
- Variable assignments (let mut)
- Augmented assignment (x = x + 1)
- Comparisons: ==, !=, <, >, <=, >=, is, is not, in, not in
- BoolOp: and → &&, or → ||
- UnaryOp: not → !
- Function calls with mappings (len → .len(), append → .push(), etc.)
- Attribute access with XRayNode property/method mappings
- List comprehensions → iterator chains
- List literals → vec![]
- Constants: str, int, float, True/False/None
- break / continue
- Subscript: x[0] → x[0]
- Expr statements

Forbidden constructs (raise TranspileError):
- import / from ... import
- class definitions
- Source without evaluate_node function
- with / async / await / yield / global / nonlocal / del / try / raise / assert

Usage::

    from code_indexer.xray.transpiler import transpile_evaluator
    rust_code = transpile_evaluator(python_source)
    # rust_code is user code only — xray-core compiler adds preamble + epilogue
"""

from __future__ import annotations

import ast
import textwrap
from typing import List


class TranspileError(Exception):
    """Raised when a Python construct cannot be transpiled to Rust."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transpile_evaluator(python_source: str) -> str:
    """Parse and transpile a Python evaluator to Rust user code.

    Args:
        python_source: Python source containing an ``evaluate_node(node)``
            function and optional helpers.

    Returns:
        Rust source string (user code only, without preamble/epilogue).

    Raises:
        TranspileError: If the source contains unsupported constructs or is
            missing the required ``evaluate_node`` function.
        SyntaxError: If the Python source has syntax errors.
    """
    tree = ast.parse(textwrap.dedent(python_source))
    _validate_top_level(tree)
    visitor = _RustEmitter()
    rust = visitor.emit_module(tree)
    if "fn evaluate_node" not in rust:
        raise TranspileError(
            "Transpiled output is missing required function 'evaluate_node'. "
            "The Python source must define a top-level 'def evaluate_node(node):' function."
        )
    return rust


# ---------------------------------------------------------------------------
# Validation pass — reject forbidden top-level constructs early
# ---------------------------------------------------------------------------

_FORBIDDEN_TOP_LEVEL = (
    ast.Import,
    ast.ImportFrom,
    ast.ClassDef,
    ast.AsyncFunctionDef,
    ast.With,
    ast.AsyncWith,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Try,
    ast.Raise,
    ast.Assert,
)


_FORBIDDEN_NAMES = {
    ast.Import: "import statement",
    ast.ImportFrom: "from...import statement",
    ast.ClassDef: "class definition",
    ast.AsyncFunctionDef: "async function definition",
    ast.With: "with statement",
    ast.AsyncWith: "async with statement",
    ast.Delete: "del statement",
    ast.Global: "global statement",
    ast.Nonlocal: "nonlocal statement",
    ast.Try: "try/except statement",
    ast.Raise: "raise statement",
    ast.Assert: "assert statement",
}


def _validate_top_level(tree: ast.Module) -> None:
    """Walk the entire AST and reject any forbidden node types."""
    for node in ast.walk(tree):
        for forbidden_type, label in _FORBIDDEN_NAMES.items():
            if isinstance(node, forbidden_type):
                line = getattr(node, "lineno", "?")
                raise TranspileError(
                    f"Unsupported construct '{label}' at line {line}. "
                    f"The transpiler does not support {label}s."
                )


# ---------------------------------------------------------------------------
# Rust emitter — AST visitor
# ---------------------------------------------------------------------------


class _RustEmitter:
    """Walks the Python AST and emits Rust source code."""

    def __init__(self) -> None:
        # Track variables that are Option<&OwnedNode> so we can emit
        # correct None comparisons and unwrap calls.
        self._option_vars: set[str] = set()
        # Track variables that are Vec<EvalFinding>
        self._findings_vars: set[str] = set()
        # Track variables that are counter/numeric
        self._count_vars: set[str] = set()
        # Track variables that are string
        self._string_vars: set[str] = set()
        # Track variables that are boolean
        self._bool_vars: set[str] = set()
        # Track variables that have been declared (let mut) during emit phase
        self._emitted_vars: set[str] = set()
        # Current indentation depth
        self._indent: int = 0

    # ------------------------------------------------------------------
    # Module entry point
    # ------------------------------------------------------------------

    def emit_module(self, tree: ast.Module) -> str:
        parts: List[str] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef):
                parts.append(self._emit_function(stmt))
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                # Module-level docstring — emit as comment
                doc = stmt.value.value
                if isinstance(doc, str):
                    for line in doc.splitlines():
                        parts.append(f"// {line}")
            # Other top-level statements already rejected by validation pass
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Function definition
    # ------------------------------------------------------------------

    def _emit_function(self, node: ast.FunctionDef) -> str:
        # Reset per-function state
        self._option_vars = set()
        self._findings_vars = set()
        self._count_vars = set()
        self._string_vars = set()
        self._bool_vars = set()
        self._emitted_vars = set()

        func_name = node.name

        # Build parameter list
        params = _build_param_list(node)

        # Return type: evaluate_node always Vec<EvalFinding>; helpers inferred from body
        if func_name == "evaluate_node":
            ret_type = "Vec<EvalFinding>"
        else:
            ret_type = self._infer_return_type(node)

        lines: List[str] = []
        # Docstring as comment
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            doc = node.body[0].value.value
            for doc_line in doc.strip().splitlines():
                lines.append(f"// {doc_line.strip()}")
            body_stmts = node.body[1:]
        else:
            body_stmts = node.body

        lines.append(f"fn {func_name}({params}) -> {ret_type} {{")
        self._indent = 1

        # Pre-scan body to identify variable types before emitting
        self._prescan_body(body_stmts)

        for stmt in body_stmts:
            lines.extend(self._emit_stmt(stmt))

        lines.append("}")
        return "\n".join(lines)

    def _infer_return_type(self, func_node: ast.FunctionDef) -> str:
        """Infer the Rust return type of a helper function from its return statements.

        Rules (first match wins across all return statements):
        - Returns a comparison, BoolOp, UnaryOp(Not), bool constant, or is_none/is_some
          call → ``bool``
        - Returns an integer constant (non-bool) → ``usize``
        - Returns a string constant → ``String``
        - Default → ``Vec<EvalFinding>``
        """
        return_values: list[ast.expr] = []
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and node.value is not None:
                return_values.append(node.value)

        for val in return_values:
            # Bool constant
            if isinstance(val, ast.Constant) and isinstance(val.value, bool):
                return "bool"
            # Comparison expression → bool
            if isinstance(val, ast.Compare):
                return "bool"
            # BoolOp (and/or) → bool
            if isinstance(val, ast.BoolOp):
                return "bool"
            # UnaryOp not → bool
            if isinstance(val, ast.UnaryOp) and isinstance(val.op, ast.Not):
                return "bool"
            # Method call .is_none() / .is_some() → bool
            if (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr in ("is_none", "is_some")
            ):
                return "bool"
            # Integer constant (not bool — bool is subclass of int, already handled above)
            if isinstance(val, ast.Constant) and isinstance(val.value, int):
                return "usize"
            # String constant
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                return "String"

        return "Vec<EvalFinding>"

    def _prescan_body(self, stmts: list) -> None:
        """Pre-scan statements to identify variable names and their types."""
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        val = stmt.value
                        if isinstance(val, ast.Constant) and val.value is None:
                            self._option_vars.add(name)
                        elif isinstance(val, ast.List) and not val.elts:
                            # Empty list — could be findings list
                            if "finding" in name.lower() or name in (
                                "findings",
                                "result",
                            ):
                                self._findings_vars.add(name)
                        elif isinstance(val, ast.Constant) and isinstance(
                            val.value, bool
                        ):
                            self._bool_vars.add(name)
                        elif isinstance(val, ast.Constant) and isinstance(
                            val.value, int
                        ):
                            self._count_vars.add(name)
                        elif isinstance(val, ast.Constant) and isinstance(
                            val.value, str
                        ):
                            self._string_vars.add(name)
                        elif isinstance(val, ast.Attribute) and val.attr == "text":
                            # var = something.text → this is a string
                            if name in self._option_vars:
                                self._string_vars.add(name)
            elif isinstance(stmt, (ast.For, ast.While, ast.If)):
                # Recurse into sub-bodies
                sub_bodies = []
                if isinstance(stmt, ast.For):
                    sub_bodies = [stmt.body, stmt.orelse]
                elif isinstance(stmt, ast.While):
                    sub_bodies = [stmt.body, stmt.orelse]
                elif isinstance(stmt, ast.If):
                    sub_bodies = [stmt.body, stmt.orelse]
                for body in sub_bodies:
                    self._prescan_body(body)

    # ------------------------------------------------------------------
    # Statement emitters
    # ------------------------------------------------------------------

    def _emit_stmt(self, stmt: ast.stmt) -> List[str]:
        indent = "    " * self._indent
        if isinstance(stmt, ast.Return):
            return [indent + self._emit_return(stmt)]
        if isinstance(stmt, ast.Assign):
            return [indent + self._emit_assign(stmt)]
        if isinstance(stmt, ast.AugAssign):
            return [indent + self._emit_augassign(stmt)]
        if isinstance(stmt, ast.If):
            return self._emit_if(stmt)
        if isinstance(stmt, ast.For):
            return self._emit_for(stmt)
        if isinstance(stmt, ast.While):
            return self._emit_while(stmt)
        if isinstance(stmt, ast.Break):
            return [indent + "break;"]
        if isinstance(stmt, ast.Continue):
            return [indent + "continue;"]
        if isinstance(stmt, ast.Expr):
            expr_str = self._emit_expr(stmt.value)
            if expr_str:
                return [indent + expr_str + ";"]
            return []
        if isinstance(stmt, ast.Pass):
            return []
        # Unknown statement — fail loudly
        raise TranspileError(f"Unsupported statement: {type(stmt).__name__}")

    def _emit_return(self, node: ast.Return) -> str:
        if node.value is None:
            return "return vec![];"
        val = node.value
        # return [] → return vec![]
        if isinstance(val, ast.List) and not val.elts:
            return "return vec![];"
        # return [{ ... }] → return vec![EvalFinding { ... }]
        if isinstance(val, ast.List) and val.elts:
            items = [self._emit_expr(e) for e in val.elts]
            return f"return vec![{', '.join(items)}];"
        # return {'matches': X, 'value': Y} -> return X (extract matches field)
        if isinstance(val, ast.Dict):
            for key, dval in zip(val.keys, val.values):
                if isinstance(key, ast.Constant) and key.value == "matches":
                    return f"return {self._emit_expr(dval)};"
        # return variable
        return f"return {self._emit_expr(val)};"

    def _emit_assign(self, node: ast.Assign) -> str:
        if len(node.targets) != 1:
            # Multi-target assign — unsupported, emit comment
            return "// TODO: multi-target assign"
        target = node.targets[0]
        val = node.value

        if isinstance(target, ast.Name):
            name = target.id
            rust_val = self._emit_expr(val)

            # Determine if this is a first-time declaration or reassignment
            # We use a heuristic: if the name is in option_vars or findings_vars,
            # it was declared as let mut already in pre-scan context.
            # We emit let mut for first assignments.

            # None → Option type
            if isinstance(val, ast.Constant) and val.value is None:
                self._option_vars.add(name)
                if name in self._string_vars:
                    return f"let mut {name}: Option<String> = None;"
                return f"let mut {name}: Option<&OwnedNode> = None;"

            # Empty list → Vec
            if isinstance(val, ast.List) and not val.elts:
                if "finding" in name.lower() or name in ("findings", "result"):
                    self._findings_vars.add(name)
                    return f"let mut {name}: Vec<EvalFinding> = Vec::new();"
                return f"let mut {name} = Vec::new();"

            # Boolean (must check before int — bool subclasses int in Python)
            if isinstance(val, ast.Constant) and isinstance(val.value, bool):
                bval = "true" if val.value else "false"
                if name in self._emitted_vars:
                    return f"{name} = {bval};"
                self._bool_vars.add(name)
                self._emitted_vars.add(name)
                return f"let mut {name} = {bval};"

            # Integer literal → usize counter
            if isinstance(val, ast.Constant) and isinstance(val.value, int):
                self._count_vars.add(name)
                return f"let mut {name}: usize = {val.value};"

            # String literal
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                self._string_vars.add(name)
                escaped = val.value.replace('"', '\\"')
                return f'let mut {name} = "{escaped}".to_string();'

            # child_by_kind() returns Option<&OwnedNode>
            if (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr == "child_by_kind"
            ):
                self._option_vars.add(name)
                return f"let mut {name} = {rust_val};"

            # List comprehension
            if isinstance(val, ast.ListComp):
                comp_rust = self._emit_listcomp(val)
                return f"let {name} = {comp_rust};"

            # Reassignment to already-declared variables
            if name in self._option_vars:
                # Check if the value is a .text access (String value)
                if isinstance(val, ast.Attribute) and val.attr == "text":
                    return f"{name} = Some({rust_val}.clone());"
                return f"{name} = Some({rust_val});"
            if name in self._bool_vars:
                return f"{name} = {rust_val};"
            if name in self._count_vars:
                return f"{name} = {rust_val};"
            if name in self._string_vars:
                return f"{name} = {rust_val}.to_string();"
            if name in self._findings_vars:
                return f"{name} = {rust_val};"
            # Subscript indexing (x[0]) — borrow to avoid move from Vec
            if isinstance(val, ast.Subscript):
                return f"let {name} = &{rust_val};"
            # First-time declaration — complex expression
            return f"let mut {name} = {rust_val};"

        if isinstance(target, ast.Subscript):
            target_str = self._emit_expr(target)
            return f"{target_str} = {self._emit_expr(val)};"

        raise TranspileError(f"Unsupported assignment target: {type(target).__name__}")

    def _emit_augassign(self, node: ast.AugAssign) -> str:
        target = self._emit_expr(node.target)
        val = self._emit_expr(node.value)
        op = _aug_op(node.op)
        return f"{target} {op}= {val};"

    def _emit_if(self, node: ast.If) -> List[str]:
        indent = "    " * self._indent
        lines: List[str] = []
        cond = self._emit_expr(node.test)
        lines.append(f"{indent}if {cond} {{")
        self._indent += 1
        for s in node.body:
            lines.extend(self._emit_stmt(s))
        self._indent -= 1
        if node.orelse:
            # elif chain
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                # elif
                lines.append(f"{indent}}} else {{")
                self._indent += 1
                lines.extend(self._emit_if(node.orelse[0]))
                self._indent -= 1
            else:
                lines.append(f"{indent}}} else {{")
                self._indent += 1
                for s in node.orelse:
                    lines.extend(self._emit_stmt(s))
                self._indent -= 1
                lines.append(f"{indent}}}")
        else:
            lines.append(f"{indent}}}")
        return lines

    def _emit_for(self, node: ast.For) -> List[str]:
        indent = "    " * self._indent
        lines: List[str] = []
        target = self._emit_expr(node.target)
        iter_expr = self._emit_for_iter(node.iter)
        lines.append(f"{indent}for {target} in {iter_expr} {{")
        self._indent += 1
        for s in node.body:
            lines.extend(self._emit_stmt(s))
        self._indent -= 1
        lines.append(f"{indent}}}")
        return lines

    def _emit_for_iter(self, iter_node: ast.expr) -> str:
        """Emit Rust iteration expression for a for-loop iterator."""
        # node.children → &node.children
        if isinstance(iter_node, ast.Attribute):
            obj = self._emit_expr(iter_node.value)
            attr = iter_node.attr
            if (
                isinstance(iter_node.value, ast.Name)
                and iter_node.value.id in self._option_vars
            ):
                obj = f"{obj}.unwrap()"
            if attr == "children":
                return f"&{obj}.children"
            if attr == "named_children":
                # named_children as property (shouldn't happen with new API, but handle)
                return f"{obj}.named_children()"
        # node.named_children() call
        if isinstance(iter_node, ast.Call):
            func = iter_node.func
            if isinstance(func, ast.Name) and func.id == "range":
                args = iter_node.args
                if len(args) == 1:
                    return f"0..{self._emit_expr(args[0])}"
                if len(args) == 2:
                    return f"{self._emit_expr(args[0])}..{self._emit_expr(args[1])}"
            if isinstance(func, ast.Name) and func.id == "enumerate":
                if iter_node.args:
                    inner = self._emit_for_iter(iter_node.args[0])
                    return f"{inner}.iter().enumerate()"
            if isinstance(func, ast.Name) and func.id == "sorted":
                if iter_node.args:
                    inner = self._emit_for_iter(iter_node.args[0])
                    return f"{{ let mut v = {inner}.to_vec(); v.sort(); v }}"
            if isinstance(func, ast.Attribute) and func.attr == "named_children":
                obj = self._emit_expr(func.value)
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id in self._option_vars
                ):
                    obj = f"{obj}.unwrap()"
                return f"{obj}.named_children()"
            if isinstance(func, ast.Attribute) and func.attr == "descendants_of_kind":
                obj = self._emit_expr(func.value)
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id in self._option_vars
                ):
                    obj = f"{obj}.unwrap()"
                if not iter_node.args:
                    raise TranspileError(
                        "descendants_of_kind() requires a kind argument"
                    )
                return (
                    f"{obj}.descendants_of_kind({self._emit_expr(iter_node.args[0])})"
                )
            # body.named_children().into_iter().take(N) already handled in expr
        return self._emit_expr(iter_node)

    def _emit_while(self, node: ast.While) -> List[str]:
        indent = "    " * self._indent
        lines: List[str] = []
        cond = self._emit_expr(node.test)
        lines.append(f"{indent}while {cond} {{")
        self._indent += 1
        for s in node.body:
            lines.extend(self._emit_stmt(s))
        self._indent -= 1
        lines.append(f"{indent}}}")
        return lines

    # ------------------------------------------------------------------
    # Expression emitters
    # ------------------------------------------------------------------

    def _emit_expr(self, node: ast.expr) -> str:  # noqa: C901 (complexity ok for dispatcher)
        if isinstance(node, ast.Constant):
            return _emit_constant(node)
        if isinstance(node, ast.Name):
            return _emit_name(node)
        if isinstance(node, ast.Attribute):
            return self._emit_attribute(node)
        if isinstance(node, ast.Call):
            return self._emit_call(node)
        if isinstance(node, ast.Compare):
            return self._emit_compare(node)
        if isinstance(node, ast.BoolOp):
            return self._emit_boolop(node)
        if isinstance(node, ast.UnaryOp):
            return self._emit_unaryop(node)
        if isinstance(node, ast.BinOp):
            return self._emit_binop(node)
        if isinstance(node, ast.Subscript):
            return self._emit_subscript(node)
        if isinstance(node, ast.List):
            return self._emit_list_literal(node)
        if isinstance(node, ast.ListComp):
            return self._emit_listcomp(node)
        if isinstance(node, ast.Dict):
            return self._emit_dict_as_finding(node)
        if isinstance(node, ast.IfExp):
            test = self._emit_expr(node.test)
            body = self._emit_expr(node.body)
            orelse = self._emit_expr(node.orelse)
            return f"if {test} {{ {body} }} else {{ {orelse} }}"
        if isinstance(node, ast.Tuple):
            items = [self._emit_expr(e) for e in node.elts]
            return f"({', '.join(items)})"
        if isinstance(node, ast.Slice):
            lower = self._emit_expr(node.lower) if node.lower else "0"
            upper = self._emit_expr(node.upper) if node.upper else ""
            if node.step:
                raise TranspileError("Step slices are not supported")
            if upper:
                return f"{lower}..{upper}"
            return f"{lower}.."
        raise TranspileError(f"Unsupported expression: {type(node).__name__}")

    def _emit_attribute(self, node: ast.Attribute) -> str:
        obj = self._emit_expr(node.value)
        attr = node.attr
        # Unwrap Option variables before field access
        if isinstance(node.value, ast.Name) and node.value.id in self._option_vars:
            obj = f"{obj}.unwrap()"
        # XRayNode API mapping: Python .kind / .type → Rust .kind (field)
        # Note: patterns use .kind (Rust API) directly, but handle .type too
        if attr == "type":
            return f"{obj}.kind"
        if attr in ("kind", "text", "is_named", "start_line", "start_byte", "end_byte"):
            return f"{obj}.{attr}"
        if attr == "children":
            return f"{obj}.children"
        if attr == "named_children":
            # As a property access (Python style) — in Rust it's a method
            return f"{obj}.named_children()"
        if attr == "start_point":
            # start_point[0] + 1 pattern handled via subscript → use start_line
            return f"{obj}.start_point"
        # String methods — defer to call emitter but handle attribute form
        if attr in ("startswith", "endswith", "strip", "lower", "upper", "split"):
            return f"{obj}.{attr}"
        return f"{obj}.{attr}"

    def _emit_call(self, node: ast.Call) -> str:  # noqa: C901
        func = node.func
        args = node.args

        # len(x) → x.len()
        if isinstance(func, ast.Name) and func.id == "len":
            if args:
                obj = self._emit_expr(args[0])
                return f"{obj}.len()"
            return "0"

        # any(pred for c in x) → x.iter().any(|c| pred)
        if isinstance(func, ast.Name) and func.id == "any":
            if args and isinstance(args[0], ast.GeneratorExp):
                return self._emit_any_genexp(args[0])
            if args:
                return f"{self._emit_expr(args[0])}.iter().any(|x| x)"
            return "false"

        # all(pred for c in x)
        if isinstance(func, ast.Name) and func.id == "all":
            if args and isinstance(args[0], ast.GeneratorExp):
                return self._emit_all_genexp(args[0])
            return "true"

        # Method calls on objects
        if isinstance(func, ast.Attribute):
            obj = self._emit_expr(func.value)
            method = func.attr
            # Unwrap Option variables before method calls
            if isinstance(func.value, ast.Name) and func.value.id in self._option_vars:
                obj = f"{obj}.unwrap()"

            # list.append(x) → vec.push(x)
            if method == "append":
                if args:
                    arg = self._emit_expr(args[0])
                    return f"{obj}.push({arg})"
                return f"{obj}.push(())"

            # list.extend(x) → vec.extend(x)
            if method == "extend":
                if args:
                    return f"{obj}.extend({self._emit_expr(args[0])})"
                return f"{obj}.extend(Vec::<EvalFinding>::new())"

            # str.startswith(x) → str.starts_with(x)
            if method == "startswith":
                if args:
                    return f"{obj}.starts_with({self._emit_expr(args[0])})"
                return f'{obj}.starts_with("")'

            # str.endswith(x) → str.ends_with(x)
            if method == "endswith":
                if args:
                    return f"{obj}.ends_with({self._emit_expr(args[0])})"
                return f'{obj}.ends_with("")'

            # .named_children() — pass through
            if method == "named_children":
                return f"{obj}.named_children()"

            # .child_by_kind(x) — pass through
            if method == "child_by_kind":
                if args:
                    return f"{obj}.child_by_kind({self._emit_expr(args[0])})"
                return f'{obj}.child_by_kind("")'

            # .has_descendant_of_kind(x) — pass through
            if method == "has_descendant_of_kind":
                if args:
                    return f"{obj}.has_descendant_of_kind({self._emit_expr(args[0])})"
                return f'{obj}.has_descendant_of_kind("")'

            # .descendants_of_kind(x) — pass through
            if method == "descendants_of_kind":
                if args:
                    return f"{obj}.descendants_of_kind({self._emit_expr(args[0])})"
                return f'{obj}.descendants_of_kind("")'

            # .iter().take(n) chaining
            if method == "take":
                if args:
                    return f"{obj}.take({self._emit_expr(args[0])})"
                return obj

            # General method call
            arg_strs = [self._emit_expr(a) for a in args]
            return f"{obj}.{method}({', '.join(arg_strs)})"

        # Plain function call
        if isinstance(func, ast.Name):
            arg_strs = [self._emit_expr(a) for a in args]
            return f"{func.id}({', '.join(arg_strs)})"

        return f"/* unsupported call: {ast.dump(node)[:40]} */"

    def _emit_compare(self, node: ast.Compare) -> str:
        parts = [self._emit_expr(node.left)]
        for op, comparator in zip(node.ops, node.comparators):
            if (
                isinstance(op, ast.Is)
                and isinstance(comparator, ast.Constant)
                and comparator.value is None
            ):
                # x is None → x.is_none()
                left_expr = self._emit_expr(node.left)
                return f"{left_expr}.is_none()"
            if (
                isinstance(op, ast.IsNot)
                and isinstance(comparator, ast.Constant)
                and comparator.value is None
            ):
                # x is not None → x.is_some()
                left_expr = self._emit_expr(node.left)
                return f"{left_expr}.is_some()"
            if isinstance(op, (ast.In, ast.NotIn)):
                left_expr = self._emit_expr(node.left)
                if isinstance(comparator, (ast.Tuple, ast.List)):
                    if not comparator.elts:
                        return "true" if isinstance(op, ast.NotIn) else "false"
                    parts = [
                        f"{left_expr} == {self._emit_expr(e)}" for e in comparator.elts
                    ]
                    chain = " || ".join(parts)
                    result = f"({chain})"
                    if isinstance(op, ast.NotIn):
                        return f"!{result}"
                    return result
                raise TranspileError("'in'/'not in' requires a literal tuple or list")
            rust_op = _cmp_op(op)
            comp_str = self._emit_expr(comparator)
            # Handle comparison with Option<String> variables
            left_name = node.left
            if (
                isinstance(left_name, ast.Name)
                and left_name.id in self._option_vars
                and left_name.id in self._string_vars
            ):
                # Option<String> == expr → var.as_deref().unwrap_or("") == expr
                left_expr = self._emit_expr(node.left)
                return f'{left_expr}.as_deref().unwrap_or("") {rust_op} {comp_str}'
            if (
                isinstance(comparator, ast.Name)
                and comparator.id in self._option_vars
                and comparator.id in self._string_vars
            ):
                # expr == Option<String> → expr == var.as_deref().unwrap_or("")
                left_expr = self._emit_expr(node.left)
                return f'{left_expr} {rust_op} {comp_str}.as_deref().unwrap_or("")'
            parts.append(f"{rust_op} {comp_str}")
        return " ".join(parts)

    def _emit_boolop(self, node: ast.BoolOp) -> str:
        op = "&&" if isinstance(node.op, ast.And) else "||"
        parts = [self._emit_expr(v) for v in node.values]
        return f" {op} ".join(parts)

    def _emit_unaryop(self, node: ast.UnaryOp) -> str:
        operand = self._emit_expr(node.operand)
        if isinstance(node.op, ast.Not):
            return f"!{operand}"
        if isinstance(node.op, ast.USub):
            return f"-{operand}"
        if isinstance(node.op, ast.UAdd):
            return operand
        return f"/* unary op */{operand}"

    def _emit_binop(self, node: ast.BinOp) -> str:
        left = self._emit_expr(node.left)
        right = self._emit_expr(node.right)
        op = _bin_op(node.op)
        return f"{left} {op} {right}"

    def _emit_subscript(self, node: ast.Subscript) -> str:
        obj = self._emit_expr(node.value)
        # Handle start_point[0] → start_line (already 1-based in Rust)
        if isinstance(node.value, ast.Attribute) and node.value.attr == "start_point":
            return f"{self._emit_expr(node.value.value)}.start_line"
        if isinstance(node.slice, ast.Slice):
            slice_expr = self._emit_expr(node.slice)
            return f"{obj}[{slice_expr}]"
        idx = self._emit_expr(node.slice)
        return f"{obj}[{idx}]"

    def _emit_list_literal(self, node: ast.List) -> str:
        if not node.elts:
            return "vec![]"
        items = [self._emit_expr(e) for e in node.elts]
        return f"vec![{', '.join(items)}]"

    def _emit_listcomp(self, node: ast.ListComp) -> str:
        """Emit a list comprehension as a Rust iterator chain.

        [c for c in x if cond] →
            x.iter().filter(|c| cond).cloned().collect::<Vec<_>>()

        [c for c in x] →
            x.iter().cloned().collect::<Vec<_>>()
        """
        if len(node.generators) != 1:
            return "/* unsupported: multi-generator listcomp */"

        gen = node.generators[0]
        target = self._emit_expr(gen.target)
        iter_expr = self._emit_for_iter(gen.iter)

        if gen.ifs:
            cond = self._emit_expr(gen.ifs[0])
            return (
                f"{iter_expr}.iter()"
                f".filter(|{target}| {cond})"
                f".cloned().collect::<Vec<_>>()"
            )
        return f"{iter_expr}.iter().cloned().collect::<Vec<_>>()"

    def _emit_dict_as_finding(self, node: ast.Dict) -> str:
        """Emit a dict literal as an EvalFinding struct literal.

        Expected keys: "pattern", "line", "snippet".
        """
        fields: dict[str, str] = {}
        for key, val in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                fields[key.value] = self._emit_expr(val)

        pattern = fields.get("pattern", '""')
        line = fields.get("line", fields.get("line_number", "0"))
        snippet = fields.get("snippet", '""')

        # pattern: convert to owned String
        pattern_rust = f"{pattern}.to_string()"

        # snippet: if it's node.text, use it directly; if string literal add .to_string()
        if snippet.startswith('"') and snippet.endswith('"'):
            snippet_rust = f"{snippet}.to_string()"
        elif "truncate_snippet" in snippet:
            snippet_rust = snippet
        else:
            # For node.text style references: clone to String
            snippet_rust = f"{snippet}.to_string()"

        # Handle text slice: node.text[:80] → truncate_snippet(&node.text, 80)
        # Already handled by subscript emitter if needed, but also handle
        # direct .text references here
        if "text[" in snippet_rust:
            # Convert text[:N] slice to truncate_snippet
            import re  # noqa: PLC0415

            m = re.match(r"(.*)\.text\[:(\d+)\]\.to_string\(\)", snippet_rust)
            if m:
                obj_path = m.group(1)
                max_len = m.group(2)
                snippet_rust = f"truncate_snippet(&{obj_path}.text, {max_len})"

        return (
            f"EvalFinding {{ "
            f"pattern: {pattern_rust}, "
            f"line: {line}, "
            f"snippet: {snippet_rust} "
            f"}}"
        )

    def _emit_any_genexp(self, node: ast.GeneratorExp) -> str:
        if len(node.generators) != 1:
            return "/* unsupported any() with multiple generators */"
        gen = node.generators[0]
        target = self._emit_expr(gen.target)
        iter_expr = self._emit_for_iter(gen.iter)
        elt = self._emit_expr(node.elt)
        if gen.ifs:
            cond = self._emit_expr(gen.ifs[0])
            return f"{iter_expr}.iter().any(|{target}| {cond} && {elt})"
        return f"{iter_expr}.iter().any(|{target}| {elt})"

    def _emit_all_genexp(self, node: ast.GeneratorExp) -> str:
        if len(node.generators) != 1:
            return "true"
        gen = node.generators[0]
        target = self._emit_expr(gen.target)
        iter_expr = self._emit_for_iter(gen.iter)
        elt = self._emit_expr(node.elt)
        return f"{iter_expr}.iter().all(|{target}| {elt})"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _build_param_list(node: ast.FunctionDef) -> str:
    """Build Rust parameter list from Python function def."""
    args = node.args.args
    if not args:
        return ""
    params = []
    for arg in args:
        name = arg.arg
        if name == "node":
            params.append("node: &OwnedNode")
        elif name == "self":
            continue
        else:
            # Unknown param — use generic reference
            params.append(f"{name}: &OwnedNode")
    return ", ".join(params)


def _emit_constant(node: ast.Constant) -> str:
    val = node.value
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return repr(val)


def _emit_name(node: ast.Name) -> str:
    name = node.id
    if name == "True":
        return "true"
    if name == "False":
        return "false"
    if name == "None":
        return "None"
    return name


def _cmp_op(op: ast.cmpop) -> str:
    if isinstance(op, (ast.In, ast.NotIn)):
        raise TranspileError(
            "'in' / 'not in' operators are not supported in evaluators"
        )
    mapping = {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "==",
        ast.IsNot: "!=",
    }
    return mapping.get(type(op), "==")


def _aug_op(op: ast.operator) -> str:
    mapping = {
        ast.Add: "+",
        ast.Sub: "-",
        ast.Mult: "*",
        ast.Div: "/",
        ast.Mod: "%",
        ast.BitAnd: "&",
        ast.BitOr: "|",
        ast.BitXor: "^",
    }
    return mapping.get(type(op), "+")


def _bin_op(op: ast.operator) -> str:
    mapping = {
        ast.Add: "+",
        ast.Sub: "-",
        ast.Mult: "*",
        ast.Div: "/",
        ast.Mod: "%",
        ast.BitAnd: "&",
        ast.BitOr: "|",
        ast.BitXor: "^",
        ast.LShift: "<<",
        ast.RShift: ">>",
    }
    return mapping.get(type(op), "+")
