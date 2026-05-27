"""Tests for the Python-to-Rust evaluator transpiler (Story #1022 spike).

Tests cover:
- Module import and basic API
- Individual Python construct transpilation (if, for, return, comparisons, list-comp)
- All 3 spike patterns transpile without errors
- Transpiled Rust compiles via xray-cli --dynlib pipeline
- Findings produced by compiled patterns match expected baseline
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SPIKE_PATTERNS_DIR = REPO_ROOT / "rust" / "spike_patterns"
XRAY_CLI = REPO_ROOT / "rust" / "target" / "release" / "xray-cli"
EVOLUTION_REPO = Path(os.environ.get("XRAY_TARGET", Path.home() / "Dev" / "evolution"))


# --------------------------------------------------------------------------
# Import guard — skip gracefully if transpiler not yet built
# --------------------------------------------------------------------------


def _import_transpiler():
    """Import transpiler module; raise ImportError with helpful message if missing."""
    try:
        from code_indexer.xray.transpiler import transpile_evaluator  # noqa: PLC0415

        return transpile_evaluator
    except ImportError as exc:
        raise ImportError(
            "code_indexer.xray.transpiler not found — run this test with "
            "PYTHONPATH=<repo>/src"
        ) from exc


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _compile_rust(rust_code: str) -> subprocess.CompletedProcess:
    """Write rust_code to a temp file and compile it via xray-cli --dynlib.

    Returns the CompletedProcess so tests can inspect returncode, stdout, stderr.
    Raises FileNotFoundError if xray-cli binary is missing.
    """
    if not XRAY_CLI.exists():
        raise FileNotFoundError(
            f"xray-cli binary not found at {XRAY_CLI}. "
            "Run: cd rust && cargo build --release"
        )
    with tempfile.NamedTemporaryFile(
        suffix=".rs", mode="w", delete=False, prefix="transpiled_"
    ) as f:
        f.write(rust_code)
        tmp_path = f.name

    env = dict(os.environ)
    # Point at a tiny subset so compilation test is fast — use evolution if available
    env["XRAY_TARGET"] = str(EVOLUTION_REPO) if EVOLUTION_REPO.exists() else "/tmp"
    try:
        result = subprocess.run(
            [str(XRAY_CLI), "--dynlib", tmp_path],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return result


def _read_spike_pattern(name: str) -> str:
    path = SPIKE_PATTERNS_DIR / name
    if not path.exists():
        pytest.skip(f"Spike pattern not found: {path}")
    return path.read_text()


# --------------------------------------------------------------------------
# Unit tests for individual transpiler constructs
# --------------------------------------------------------------------------


class TestBasicConstructs:
    """Tests that individual Python AST nodes transpile to correct Rust snippets."""

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_simple_empty_function(self):
        """A function returning empty list should produce valid Rust."""
        python_src = """
def evaluate_node(node):
    return []
"""
        rust = self.transpile(python_src)
        assert "fn evaluate_node" in rust
        assert "-> Vec<EvalFinding>" in rust
        assert "return vec![]" in rust

    def test_if_with_return_empty(self):
        """if node.kind != X: return [] → Rust if with return vec![]"""
        python_src = """
def evaluate_node(node):
    if node.kind != "catch_clause":
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert 'node.kind != "catch_clause"' in rust
        assert "return vec![]" in rust

    def test_for_loop_over_children(self):
        """for child in node.children: → Rust for child in &node.children {"""
        python_src = """
def evaluate_node(node):
    for child in node.children:
        if child.kind == "block":
            return []
    return []
"""
        rust = self.transpile(python_src)
        assert "for child in" in rust
        assert "node.children" in rust

    def test_none_comparison(self):
        """x is None → x.is_none() or Option None checks"""
        python_src = """
def evaluate_node(node):
    param = None
    if param is None:
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "fn evaluate_node" in rust
        # Should have None handling
        assert "None" in rust

    def test_list_comprehension_named_children(self):
        """[c for c in x.children if c.is_named] → iterator chain"""
        python_src = """
def evaluate_node(node):
    named = [c for c in node.children if c.is_named]
    return []
"""
        rust = self.transpile(python_src)
        assert "fn evaluate_node" in rust
        # Should use .iter() pattern
        assert ".iter()" in rust or "named_children" in rust

    def test_return_finding_dict(self):
        """return [{"pattern": ..., "line": ..., "snippet": ...}] → EvalFinding struct"""
        python_src = """
def evaluate_node(node):
    return [{"pattern": "catch-rethrow", "line": node.start_line, "snippet": node.text}]
"""
        rust = self.transpile(python_src)
        assert "EvalFinding" in rust
        assert '"catch-rethrow"' in rust
        assert "start_line" in rust

    def test_string_startswith_mapping(self):
        """x.startswith('y') → x.starts_with('y')"""
        python_src = """
def evaluate_node(node):
    if node.text.startswith("get"):
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "starts_with" in rust

    def test_string_endswith_mapping(self):
        """x.endswith('y') → x.ends_with('y')"""
        python_src = """
def evaluate_node(node):
    if node.text.endswith("Exception"):
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "ends_with" in rust

    def test_len_call_mapping(self):
        """len(x) → x.len()"""
        python_src = """
def evaluate_node(node):
    if len(node.children) == 0:
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert ".len()" in rust

    def test_boolean_constants(self):
        """True/False → true/false"""
        python_src = """
def evaluate_node(node):
    found = False
    if node.is_named:
        found = True
    return []
"""
        rust = self.transpile(python_src)
        assert "true" in rust or "false" in rust

    def test_named_children_method_call(self):
        """.named_children() call transpiles (it's a method in Rust too)"""
        python_src = """
def evaluate_node(node):
    for child in node.named_children():
        if child.kind == "block":
            return []
    return []
"""
        rust = self.transpile(python_src)
        assert "named_children()" in rust

    def test_augmented_assignment(self):
        """x = x + 1 or x += 1 transpiles to Rust increment"""
        python_src = """
def evaluate_node(node):
    count = 0
    for child in node.children:
        count = count + 1
    return []
"""
        rust = self.transpile(python_src)
        assert "fn evaluate_node" in rust

    def test_boolean_and_operator(self):
        """and → &&"""
        python_src = """
def evaluate_node(node):
    if node.kind == "a" and node.is_named:
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "&&" in rust

    def test_boolean_or_operator(self):
        """or → ||"""
        python_src = """
def evaluate_node(node):
    if node.kind == "a" or node.kind == "b":
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "||" in rust

    def test_not_operator(self):
        """not x → !x"""
        python_src = """
def evaluate_node(node):
    if not node.is_named:
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "!" in rust

    def test_gt_comparison(self):
        """stmt_count > 50 transpiles"""
        python_src = """
def evaluate_node(node):
    if len(node.children) > 50:
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "> 50" in rust or ">50" in rust

    def test_has_descendant_of_kind_preserved(self):
        """.has_descendant_of_kind() call passes through"""
        python_src = """
def evaluate_node(node):
    if node.has_descendant_of_kind("object_creation_expression"):
        return []
    return []
"""
        rust = self.transpile(python_src)
        assert "has_descendant_of_kind" in rust

    def test_descendants_of_kind_call(self):
        """node.descendants_of_kind('X') transpiles to Rust method call."""
        python_src = """
def evaluate_node(node):
    for c in node.descendants_of_kind("catch_clause"):
        if c.kind == "catch_clause":
            return []
    return []
"""
        rust = self.transpile(python_src)
        assert 'descendants_of_kind("catch_clause")' in rust

    def test_descendants_of_kind_in_for_loop(self):
        """for x in node.descendants_of_kind('kind') transpiles as for-loop iterator."""
        python_src = """
def evaluate_node(node):
    findings = []
    for ts in node.descendants_of_kind("try_statement"):
        findings.append({"pattern": "try", "line": ts.start_line, "snippet": ts.text})
    return findings
"""
        rust = self.transpile(python_src)
        assert 'descendants_of_kind("try_statement")' in rust
        assert "for ts in" in rust

    def test_return_dict_with_matches_extracts_matches(self):
        """return {'matches': X, 'value': Y} extracts X as the return value."""
        python_src = """
def evaluate_node(node):
    matches = []
    return {"matches": matches, "value": None}
"""
        rust = self.transpile(python_src)
        assert "return matches;" in rust

    def test_line_number_maps_to_line(self):
        """line_number key in finding dict maps to line field in EvalFinding."""
        python_src = """
def evaluate_node(node):
    return [{"pattern": "test", "line_number": node.start_line, "snippet": node.text}]
"""
        rust = self.transpile(python_src)
        assert "EvalFinding" in rust
        assert "line: node.start_line" in rust

    def test_slice_no_ampersand(self):
        """node.text[:N] in snippet should not produce & prefix in slice output."""
        python_src = """
def evaluate_node(node):
    return [{"pattern": "test", "line": node.start_line, "snippet": node.text[:80]}]
"""
        rust = self.transpile(python_src)
        # The snippet should use truncate_snippet, not &node.text[...]
        assert "&node.text[" not in rust

    def test_break_statement(self):
        """break inside for loop transpiles"""
        python_src = """
def evaluate_node(node):
    for child in node.children:
        if child.kind == "block":
            break
    return []
"""
        rust = self.transpile(python_src)
        assert "break" in rust

    def test_append_to_findings(self):
        """findings.append(...) → findings.push(...)"""
        python_src = """
def evaluate_node(node):
    findings = []
    findings.append({"pattern": "test", "line": node.start_line, "snippet": ""})
    return findings
"""
        rust = self.transpile(python_src)
        assert "push" in rust or "extend" in rust


# --------------------------------------------------------------------------
# Spike pattern transpilation tests (no compilation)
# --------------------------------------------------------------------------


class TestSpikePatterns:
    """Tests that each spike pattern transpiles without raising exceptions."""

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_pattern1_catch_rethrow_transpiles(self):
        """pattern1_catch_rethrow.py must transpile without error."""
        src = _read_spike_pattern("pattern1_catch_rethrow.py")
        rust = self.transpile(src)
        assert "fn evaluate_node" in rust
        assert "EvalFinding" in rust
        assert "catch_clause" in rust

    def test_pattern2_allocation_in_try_transpiles(self):
        """pattern2_allocation_in_try.py must transpile without error."""
        src = _read_spike_pattern("pattern2_allocation_in_try.py")
        rust = self.transpile(src)
        assert "fn evaluate_node" in rust
        assert "EvalFinding" in rust
        assert "try_statement" in rust

    def test_pattern3_method_too_long_transpiles(self):
        """pattern3_method_too_long.py must transpile without error."""
        src = _read_spike_pattern("pattern3_method_too_long.py")
        rust = self.transpile(src)
        assert "fn evaluate_node" in rust
        assert "EvalFinding" in rust
        assert "method_declaration" in rust

    def test_all_patterns_contain_eval_finding_struct(self):
        """All 3 patterns must reference EvalFinding in output."""
        for name in [
            "pattern1_catch_rethrow.py",
            "pattern2_allocation_in_try.py",
            "pattern3_method_too_long.py",
        ]:
            src = _read_spike_pattern(name)
            rust = self.transpile(src)
            assert "EvalFinding" in rust, f"{name} missing EvalFinding"

    def test_all_patterns_produce_vec_return(self):
        """All 3 patterns must return Vec<EvalFinding> (via vec![] or Vec::new())."""
        for name in [
            "pattern1_catch_rethrow.py",
            "pattern2_allocation_in_try.py",
            "pattern3_method_too_long.py",
        ]:
            src = _read_spike_pattern(name)
            rust = self.transpile(src)
            assert "vec!" in rust or "Vec::new()" in rust, (
                f"{name} missing vec! macro or Vec::new() constructor"
            )


# --------------------------------------------------------------------------
# Compilation tests — require xray-cli binary and evolution repo
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not XRAY_CLI.exists(),
    reason="xray-cli binary not found — run cargo build --release first",
)
@pytest.mark.skipif(
    not EVOLUTION_REPO.exists(),
    reason="Evolution repo not found — set XRAY_TARGET env var",
)
class TestCompilation:
    """Tests that transpiled Rust actually compiles and produces findings."""

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_pattern1_compiles_and_produces_findings(self):
        """Pattern 1 (catch-rethrow) must compile and produce > 0 findings."""
        src = _read_spike_pattern("pattern1_catch_rethrow.py")
        rust = self.transpile(src)
        result = _compile_rust(rust)
        assert result.returncode == 0, (
            f"Pattern 1 compilation failed.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "Findings:" in combined, "Expected 'Findings:' in output"
        # Extract finding count
        for line in combined.splitlines():
            if line.startswith("Findings:"):
                count_str = line.split(":")[1].strip().split()[0]
                count = int(count_str)
                assert count > 0, f"Pattern 1 must produce > 0 findings, got {count}"
                break

    def test_pattern2_compiles_and_produces_findings(self):
        """Pattern 2 (allocation-in-try) must compile and produce > 0 findings."""
        src = _read_spike_pattern("pattern2_allocation_in_try.py")
        rust = self.transpile(src)
        result = _compile_rust(rust)
        assert result.returncode == 0, (
            f"Pattern 2 compilation failed.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "Findings:" in combined
        for line in combined.splitlines():
            if line.startswith("Findings:"):
                count_str = line.split(":")[1].strip().split()[0]
                count = int(count_str)
                assert count > 0, f"Pattern 2 must produce > 0 findings, got {count}"
                break

    def test_pattern3_compiles_and_produces_findings(self):
        """Pattern 3 (method-too-long) must compile and produce >= 0 findings."""
        src = _read_spike_pattern("pattern3_method_too_long.py")
        rust = self.transpile(src)
        result = _compile_rust(rust)
        assert result.returncode == 0, (
            f"Pattern 3 compilation failed.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "Findings:" in combined

    def test_pattern1_and_2_combined_baseline(self):
        """Patterns 1+2 combined as single evaluator must produce 270 findings.

        This is the critical spike validation: if the combined pattern
        produces 270 findings (106 allocation-in-try + 164 catch-rethrow),
        the transpiler is verified as correct.
        """
        src1 = _read_spike_pattern("pattern1_catch_rethrow.py")
        src2 = _read_spike_pattern("pattern2_allocation_in_try.py")

        rust1 = self.transpile(src1)
        rust2 = self.transpile(src2)

        # Rename the evaluate_node functions to helpers, add combined dispatcher
        combined_rust = _merge_evaluators_for_test(rust1, rust2)
        result = _compile_rust(combined_rust)

        assert result.returncode == 0, (
            f"Combined patterns 1+2 compilation failed.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        combined_output = result.stdout + result.stderr
        assert "Findings:" in combined_output
        for line in combined_output.splitlines():
            if line.startswith("Findings:"):
                count_str = line.split(":")[1].strip().split()[0]
                count = int(count_str)
                assert count == 270, (
                    f"Expected 270 findings (baseline), got {count}.\n"
                    f"Output:\n{combined_output}"
                )
                break


def _merge_evaluators_for_test(rust1: str, rust2: str) -> str:
    """Merge two transpiled evaluators into one that calls both.

    Renames each pattern's evaluate_node to a helper name, then adds a
    combined evaluate_node that calls both and concatenates results.
    """
    helper1 = rust1.replace("fn evaluate_node(", "fn eval_catch_rethrow(")
    helper2 = rust2.replace("fn evaluate_node(", "fn eval_allocation_in_try(")
    combined_fn = """
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    findings.extend(eval_catch_rethrow(node));
    findings.extend(eval_allocation_in_try(node));
    findings
}
"""
    return helper1 + "\n" + helper2 + "\n" + combined_fn


# --------------------------------------------------------------------------
# Option variable handling tests (Bug fixes for compilation errors)
# --------------------------------------------------------------------------


class TestOptionVarFixes:
    """Tests for correct Option<T> variable handling in transpiled Rust.

    These tests cover three classes of bugs that cause Rust compilation failures:
    Bug 1 - Variable shadowing: reassignment to an outer Option var in a loop must
            emit `varname = Some(child);` not `let mut varname = child;`.
    Bug 2 - Field access on Option: accessing .children/.kind/.text on an Option var
            must emit `varname.unwrap().field` not `varname.field`.
    Bug 3 - String type inference: when an Option var is assigned a .text value,
            its type must be Option<String> and assignment must use Some(child.text.clone()).
    """

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_option_var_reassignment_in_loop_uses_some(self):
        """Bug 1: param = child inside loop must emit `param = Some(child);` not `let mut param = child;`"""
        python_src = """
def evaluate_node(node):
    param = None
    for child in node.children:
        if child.kind == "catch_formal_parameter":
            param = child
            break
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT shadow the outer variable with a new let binding
        assert "let mut param = child" not in rust, (
            "Bug 1: 'let mut param = child' shadows outer Option var — should be 'param = Some(child)'"
        )
        # Must assign using Some(...)
        assert "param = Some(child)" in rust, (
            "Bug 1: reassignment to outer Option var must use 'param = Some(child)'"
        )

    def test_option_var_field_access_emits_unwrap(self):
        """Bug 2: accessing .children on an Option var must emit param.unwrap().children"""
        python_src = """
def evaluate_node(node):
    param = None
    for child in node.children:
        if child.kind == "catch_formal_parameter":
            param = child
            break
    if param is None:
        return []
    for child in param.children:
        if child.kind == "identifier":
            return []
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT emit bare param.children (Option has no children field)
        assert "param.children" not in rust, (
            "Bug 2: 'param.children' is invalid — Option<&OwnedNode> has no children field"
        )
        # Must unwrap before field access
        assert "param.unwrap().children" in rust, (
            "Bug 2: must emit 'param.unwrap().children' to access field on Option"
        )

    def test_option_var_named_children_iter_emits_unwrap(self):
        """Bug 2: iterating over body.named_children() where body is Option must unwrap"""
        python_src = """
def evaluate_node(node):
    body = None
    for child in node.children:
        if child.kind == "block":
            body = child
            break
    if body is None:
        return []
    for stmt in body.named_children():
        if stmt.kind == "local_variable_declaration":
            return []
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT iterate over bare body.named_children() — body is Option
        assert "body.named_children()" not in rust, (
            "Bug 2: 'body.named_children()' is invalid — Option<&OwnedNode> has no named_children method"
        )
        # Must unwrap before calling named_children
        assert "body.unwrap().named_children()" in rust, (
            "Bug 2: must emit 'body.unwrap().named_children()' to call method on Option"
        )

    def test_option_string_var_gets_correct_type(self):
        """Bug 3: param_name = None then param_name = child.text must yield Option<String>"""
        python_src = """
def evaluate_node(node):
    param = None
    for child in node.children:
        if child.kind == "catch_formal_parameter":
            param = child
            break
    if param is None:
        return []
    param_name = None
    for child in param.children:
        if child.kind == "identifier":
            param_name = child.text
    if param_name is None:
        return []
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT declare param_name as Option<&OwnedNode>
        assert "param_name: Option<&OwnedNode>" not in rust, (
            "Bug 3: param_name stores .text (a String), must be Option<String> not Option<&OwnedNode>"
        )
        # Must declare as Option<String>
        assert "param_name: Option<String>" in rust, (
            "Bug 3: param_name must be declared as 'Option<String>'"
        )

    def test_option_string_var_assignment_uses_some_clone(self):
        """Bug 3: param_name = child.text must emit `param_name = Some(child.text.clone());`"""
        python_src = """
def evaluate_node(node):
    param = None
    for child in node.children:
        if child.kind == "catch_formal_parameter":
            param = child
            break
    if param is None:
        return []
    param_name = None
    for child in param.children:
        if child.kind == "identifier":
            param_name = child.text
    if param_name is None:
        return []
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT shadow param_name with new let binding
        assert "let mut param_name = child.text" not in rust, (
            "Bug 3: 'let mut param_name = child.text' shadows outer Option var"
        )
        # Must assign using Some(child.text.clone())
        assert "param_name = Some(child.text.clone())" in rust, (
            "Bug 3: must emit 'param_name = Some(child.text.clone())' for text assignment to Option"
        )

    def test_bool_var_reassignment_in_loop_no_let(self):
        """Bug 1 (bool variant): has_finally = True inside loop must NOT emit `let mut has_finally = true`"""
        python_src = """
def evaluate_node(node):
    has_finally = False
    for child in node.children:
        if child.kind == "finally_clause":
            has_finally = True
            break
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT shadow the outer bool var with a new let binding
        assert "let mut has_finally = true" not in rust, (
            "Bug 1 (bool): 'let mut has_finally = true' shadows outer var — should be 'has_finally = true'"
        )
        # Must reassign directly
        assert "has_finally = true" in rust, (
            "Bug 1 (bool): must emit 'has_finally = true;' for reassignment in loop"
        )

    def test_counter_var_reassignment_in_loop_no_let(self):
        """Bug 1 (counter variant): count = count + 1 inside loop must NOT emit `let mut count = ...`"""
        python_src = """
def evaluate_node(node):
    count = 0
    for child in node.children:
        count = count + 1
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT shadow the outer counter var with a new let binding
        assert "let mut count = count + 1" not in rust, (
            "Bug 1 (counter): 'let mut count = count + 1' shadows outer var — should be 'count = count + 1'"
        )
        # Must reassign directly
        assert "count = count + 1" in rust, (
            "Bug 1 (counter): must emit 'count = count + 1;' for counter increment"
        )

    def test_string_var_reassignment_in_loop_no_let(self):
        """Bug 1 (string variant): method_name = child.text inside loop must NOT emit `let mut method_name = child.text`"""
        python_src = """
def evaluate_node(node):
    method_name = ""
    for child in node.children:
        if child.kind == "identifier":
            method_name = child.text
            break
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT shadow the outer string var with a new let binding
        assert "let mut method_name = child.text" not in rust, (
            "Bug 1 (string): 'let mut method_name = child.text' shadows outer var"
        )
        # Must reassign using .to_string() since child.text is &str and method_name is String
        assert "method_name = child.text.to_string()" in rust, (
            "Bug 1 (string): must emit 'method_name = child.text.to_string();' for reassignment"
        )

    def test_option_var_children_listcomp_emits_unwrap(self):
        """Bug 2 (listcomp): [c for c in body.children if c.is_named] where body is Option must unwrap"""
        python_src = """
def evaluate_node(node):
    body = None
    for child in node.children:
        if child.kind == "block":
            body = child
            break
    if body is None:
        return []
    named = [c for c in body.children if c.is_named]
    return []
"""
        rust = self.transpile(python_src)
        # Must NOT use bare body.children in list comp — body is Option
        # The iter source must unwrap body first
        assert "body.unwrap().children" in rust, (
            "Bug 2 (listcomp): list comp over body.children must use body.unwrap().children"
        )

    def test_pattern1_option_string_comparison_correct(self):
        """Bug 3 (compare): expr.text == param_name where param_name is Option<String> must use .as_deref().unwrap_or"""
        python_src = """
def evaluate_node(node):
    param_name = None
    for child in node.children:
        if child.kind == "identifier":
            param_name = child.text
    if param_name is None:
        return []
    expr = node
    if expr.kind == "identifier" and expr.text == param_name:
        return [{"pattern": "catch-rethrow", "line": expr.start_line, "snippet": expr.text}]
    return []
"""
        rust = self.transpile(python_src)
        # Verify the comparison uses unwrapped Option, not direct comparison
        assert (
            "param_name.as_deref().unwrap_or(" in rust or "param_name.unwrap()" in rust
        ), "Bug 3 (compare): must unwrap Option<String> before string comparison"


# --------------------------------------------------------------------------
# TranspileError tests
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Return type inference tests (Finding H3)
# --------------------------------------------------------------------------


class TestReturnTypeInference:
    """Tests that helper function return types are inferred from body analysis.

    H3: ALL functions must NOT blindly get -> Vec<EvalFinding>.
    Helpers returning bool/int/str must get the correct Rust return type.
    """

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_helper_returning_bool_gets_bool_return_type(self):
        """H3: Helper function returning bool should get -> bool."""
        import textwrap  # noqa: PLC0415

        source = textwrap.dedent("""\
            def is_try_block(node):
                return node.kind == "try_statement"

            def evaluate_node(node):
                if is_try_block(node):
                    return [{"pattern": "test", "line": node.start_line, "snippet": ""}]
                return []
        """)
        rust = self.transpile(source)
        assert "fn is_try_block" in rust
        assert "-> bool" in rust  # Helper returns bool, not Vec<EvalFinding>

    def test_helper_returning_usize_gets_usize_return_type(self):
        """H3: Helper function returning int literal should get -> usize."""
        import textwrap  # noqa: PLC0415

        source = textwrap.dedent("""\
            def count_children(node):
                return 0

            def evaluate_node(node):
                return []
        """)
        rust = self.transpile(source)
        assert "fn count_children" in rust
        assert "-> usize" in rust

    def test_evaluate_node_always_returns_vec_evalfinding(self):
        """H3: evaluate_node must always return Vec<EvalFinding> regardless of body."""
        import textwrap  # noqa: PLC0415

        source = textwrap.dedent("""\
            def evaluate_node(node):
                return []
        """)
        rust = self.transpile(source)
        assert "fn evaluate_node" in rust
        assert "-> Vec<EvalFinding>" in rust


class TestTranspilerErrors:
    """Tests that the transpiler raises clear errors for unsupported constructs."""

    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_import_statement_rejected(self):
        """import os → TranspileError with clear message"""
        from code_indexer.xray.transpiler import TranspileError  # noqa: PLC0415

        python_src = """
def evaluate_node(node):
    import os
    return []
"""
        with pytest.raises(TranspileError) as exc_info:
            self.transpile(python_src)
        assert "import" in str(exc_info.value).lower() or "Import" in str(
            exc_info.value
        )

    def test_class_definition_rejected(self):
        """class X: ... → TranspileError"""
        from code_indexer.xray.transpiler import TranspileError  # noqa: PLC0415

        python_src = """
class Helper:
    pass

def evaluate_node(node):
    return []
"""
        with pytest.raises(TranspileError) as exc_info:
            self.transpile(python_src)
        assert "class" in str(exc_info.value).lower() or "ClassDef" in str(
            exc_info.value
        )

    def test_missing_evaluate_node_rejected(self):
        """Source without evaluate_node function → TranspileError"""
        from code_indexer.xray.transpiler import TranspileError  # noqa: PLC0415

        python_src = """
def helper(x):
    return x
"""
        with pytest.raises(TranspileError) as exc_info:
            self.transpile(python_src)
        assert "evaluate_node" in str(exc_info.value)

    def test_error_message_contains_construct_name(self):
        """Error messages name the unsupported construct."""
        from code_indexer.xray.transpiler import TranspileError  # noqa: PLC0415

        python_src = """
def evaluate_node(node):
    import os
    return []
"""
        with pytest.raises(TranspileError) as exc_info:
            self.transpile(python_src)
        # The error must tell the user WHAT failed
        msg = str(exc_info.value)
        assert len(msg) > 10, "Error message too short to be helpful"


# --------------------------------------------------------------------------
# In/NotIn operator tests
# --------------------------------------------------------------------------


class TestInNotInOperators:
    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_in_tuple_literal(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                if node.kind in ("throw_statement", "raise_statement"):
                    return [{"pattern": "found", "line": node.start_line, "snippet": node.text}]
                return []
        """)
        rust = self.transpile(code)
        assert "==" in rust
        assert "||" in rust

    def test_not_in_list_literal(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                if node.kind not in ["block", "comment"]:
                    return [{"pattern": "found", "line": node.start_line, "snippet": node.text}]
                return []
        """)
        rust = self.transpile(code)
        assert "!" in rust

    def test_in_variable_rhs_raises(self):
        from code_indexer.xray.transpiler import TranspileError  # noqa: PLC0415

        code = textwrap.dedent("""\
            def evaluate_node(node):
                kinds = ["a", "b"]
                if node.kind in kinds:
                    return []
                return []
        """)
        with pytest.raises(TranspileError, match="literal tuple or list"):
            self.transpile(code)


# --------------------------------------------------------------------------
# Slice transpilation tests
# --------------------------------------------------------------------------


class TestSliceTranspilation:
    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_slice_upper_only(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                snippet = node.text[:80]
                return [{"pattern": "found", "line": node.start_line, "snippet": snippet}]
        """)
        rust = self.transpile(code)
        assert "0..80" in rust

    def test_slice_both_bounds(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                x = source[10:20]
                return []
        """)
        rust = self.transpile(code)
        assert "10..20" in rust


# --------------------------------------------------------------------------
# Range transpilation tests
# --------------------------------------------------------------------------


class TestRangeTranspilation:
    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_range_single_arg(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                for i in range(10):
                    pass
                return []
        """)
        rust = self.transpile(code)
        assert "0..10" in rust

    def test_range_two_args(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                for i in range(1, 5):
                    pass
                return []
        """)
        rust = self.transpile(code)
        assert "1..5" in rust


# --------------------------------------------------------------------------
# Enumerate transpilation tests
# --------------------------------------------------------------------------


class TestEnumerateTranspilation:
    def setup_method(self):
        self.transpile = _import_transpiler()

    def test_enumerate_children(self):
        code = textwrap.dedent("""\
            def evaluate_node(node):
                for i, child in enumerate(node.children):
                    pass
                return []
        """)
        rust = self.transpile(code)
        assert "enumerate" in rust
