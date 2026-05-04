"""User Mandate Section 7: Real-World Scenario Tests (Story #970).

Tests PythonEvaluatorSandbox against real fixture files parsed by AstSearchEngine:

  Scenario 1: SQL injection scanner (Java)
    - Fixture: fixtures/security/SqlInjection.java
    - Walks AST to find every method_invocation named 'prepareStatement'
    - Evaluator checks: is NOT descended from try_with_resources_statement
    - Expected: 2 unsafe (True) + 2 safe (False)
  Scenario 2: Hardcoded credential scanner (Python)
    - Fixture: fixtures/security/credentials.py
    - Walks AST to find every assignment where target name contains
      'password' or 'key' (case-insensitive)
    - Evaluator checks: value is non-empty string, not placeholder, file not test
    - Expected: 2 hits out of 5 assignments
  Scenario 3: Path traversal scanner (Python)
    - Fixture: fixtures/security/traversal.py
    - Walks AST to find every call to Path() or open()
    - Evaluator checks: first argument is identifier (tainted) vs string (safe)
    - Expected: 3 tainted + 4 safe; USER_PATH counts as tainted (AST limitation)
  Scenario 4: Concatenation-based string injection scanner (Python)
    - Fixture: fixtures/security/concat.py
    - Walks AST to find every assignment whose RHS is a binary_operator
    - Evaluator checks: child[0] or child[1] is identifier, or child[0] is
      binary_operator (recursive nesting for chained concatenation)
    - Expected: 3 tainted + 3 safe

  Scenario 5 (cross-language parity) lives in test_sandbox_real_world_parity.py.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import PythonEvaluatorSandbox


FIXTURES_SECURITY = Path(__file__).parent / "fixtures" / "security"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk_nodes(node, target_type: str) -> list:
    """Collect all descendant nodes (including node itself) of the given type."""
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.named_children:
        results.extend(_walk_nodes(child, target_type))
    return results


# ---------------------------------------------------------------------------
# Scenario 1: SQL injection scanner (Java)
# ---------------------------------------------------------------------------


def test_sql_injection_scanner_java() -> None:
    """SQL injection scanner correctly identifies 2 unsafe + 2 safe prepareStatement calls.

    The evaluator checks whether a method_invocation node is NOT descended from
    a try_with_resources_statement. Unsafe calls (bare, outside try-with-resources)
    return True; safe calls (inside try-with-resources) return False.

    Fixture: fixtures/security/SqlInjection.java
    """
    source = (FIXTURES_SECURITY / "SqlInjection.java").read_text()
    engine = AstSearchEngine()
    root = engine.parse(source, "java")

    sb = PythonEvaluatorSandbox()

    # Evaluator: True if the call is NOT inside a try-with-resources (unsafe)
    evaluator = "return node.is_descendant_of('try_with_resources_statement') == False"

    # Find all method_invocation nodes named 'prepareStatement'
    all_invocations = _walk_nodes(root, "method_invocation")
    prepare_statement_nodes = [
        n
        for n in all_invocations
        if n.child_by_field_name("name") is not None
        and n.child_by_field_name("name").text == "prepareStatement"
    ]

    assert len(prepare_statement_nodes) == 4, (
        f"Expected 4 prepareStatement calls in fixture, found {len(prepare_statement_nodes)}"
    )

    unsafe_count = 0
    safe_count = 0

    for node in prepare_statement_nodes:
        result = sb.run(
            evaluator,
            node=node,
            root=root,
            source=source,
            lang="java",
            file_path=str(FIXTURES_SECURITY / "SqlInjection.java"),
        )
        assert result.failure is None, (
            f"Evaluator failed for node {node.text[:60]!r}: "
            f"failure={result.failure!r}, detail={result.detail!r}"
        )
        if result.value is True:
            unsafe_count += 1
        else:
            safe_count += 1

    assert unsafe_count == 2, (
        f"Expected 2 unsafe prepareStatement calls, got {unsafe_count}"
    )
    assert safe_count == 2, f"Expected 2 safe prepareStatement calls, got {safe_count}"


# ---------------------------------------------------------------------------
# Scenario 2: Hardcoded credential scanner (Python)
# ---------------------------------------------------------------------------


def test_hardcoded_credential_scanner_python() -> None:
    """Hardcoded credential scanner identifies 2 real credentials out of 5 assignments.

    The evaluator returns True when:
      - The assignment target name contains 'password' or 'key' (case-insensitive)
      - The right-hand side is a non-empty string constant
      - The string is not a placeholder (does not start with '<')
      - The file_path does not contain 'test' (case-insensitive)

    But the file_path filter is applied externally here since file_path is fixed.
    The evaluator focuses on value quality; the name filter is applied in the walk.

    Fixture: fixtures/security/credentials.py
    Expected hits: 'password' = 'supersecret_real', 'api_key' = 'AKIA...'
    Ignored: test_password (has 'test' in var name), empty_pw (empty), placeholder (<...)
    """
    source = (FIXTURES_SECURITY / "credentials.py").read_text()
    engine = AstSearchEngine()
    root = engine.parse(source, "python")
    fixture_path = str(FIXTURES_SECURITY / "credentials.py")

    sb = PythonEvaluatorSandbox()

    # Evaluator: True if the assignment has a non-empty, non-placeholder RHS string
    # and the LHS variable name does not contain 'test'.
    # node is an assignment node; named_children[0] = identifier, [1] = string value.
    # Uses only whitelisted AST nodes: Return, BoolOp, Compare, Call, Attribute,
    # Subscript, Constant — no assignment statements.
    evaluator = (
        "return ("
        "len(node.named_children) > 1"
        " and bool(node.named_children[1].text.strip('\"').strip(\"'\"))"
        " and not node.named_children[1].text.strip('\"').strip(\"'\").startswith('<')"
        " and 'test' not in node.named_children[0].text.lower()"
        ")"
    )

    # Walk to find assignment nodes where the target (LHS) name contains
    # 'password' or 'key' (case-insensitive)
    all_assignments = _walk_nodes(root, "assignment")
    credential_candidates = []
    for assign in all_assignments:
        children = assign.named_children
        if not children:
            continue
        target_name = children[0].text.lower()
        if "password" in target_name or "key" in target_name:
            credential_candidates.append(assign)

    assert len(credential_candidates) == 5, (
        f"Expected 5 candidate assignments (all names with password/key), "
        f"got {len(credential_candidates)}: {[n.named_children[0].text for n in credential_candidates]}"
    )

    hit_count = 0
    no_hit_count = 0

    for assign in credential_candidates:
        result = sb.run(
            evaluator,
            node=assign,
            root=root,
            source=source,
            lang="python",
            file_path=fixture_path,
        )
        assert result.failure is None, (
            f"Evaluator failed for assignment {assign.text!r}: "
            f"failure={result.failure!r}, detail={result.detail!r}"
        )
        if result.value is True:
            hit_count += 1
        else:
            no_hit_count += 1

    assert hit_count == 2, (
        f"Expected 2 credential hits, got {hit_count} "
        f"(expected: password='supersecret_real', api_key='AKIA...')"
    )
    assert no_hit_count == 3, (
        f"Expected 3 non-hits, got {no_hit_count} "
        f"(expected: test_password, empty_password, password_placeholder)"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Path traversal scanner (Python)
# ---------------------------------------------------------------------------


def test_path_traversal_scanner_python() -> None:
    """Path traversal scanner identifies 3 tainted + 4 safe Path()/open() calls.

    The evaluator returns True when the first argument to Path() or open() is
    an identifier (Name node — value not known statically) rather than a string
    literal (Constant node — value known at parse time).

    Important limitation documented in fixture:
      USER_PATH is an identifier even though a human might consider it constant;
      distinguishing it from truly runtime-dynamic names requires dataflow
      analysis beyond pure AST inspection.  The test asserts this limitation
      explicitly: all 3 identifier-argument calls are flagged as tainted.

    Fixture: fixtures/security/traversal.py
    Expected: 3 tainted (identifier arg) + 4 safe (string literal arg)
    """
    source = (FIXTURES_SECURITY / "traversal.py").read_text()
    engine = AstSearchEngine()
    root = engine.parse(source, "python")
    fixture_path = str(FIXTURES_SECURITY / "traversal.py")

    sb = PythonEvaluatorSandbox()

    # Evaluator: True if first argument to the call is an identifier (tainted).
    # node = call node
    # node.named_children[0] = function name (identifier: 'Path' or 'open')
    # node.named_children[1] = argument_list
    # node.named_children[1].named_children[0] = first argument node
    evaluator = (
        "return ("
        "len(node.named_children) > 1"
        " and len(node.named_children[1].named_children) > 0"
        ' and node.named_children[1].named_children[0].type == "identifier"'
        ")"
    )

    # Walk to find all call nodes whose function name is 'Path' or 'open'
    all_calls = _walk_nodes(root, "call")
    path_open_calls = [
        c
        for c in all_calls
        if c.named_children and c.named_children[0].text in ("Path", "open")
    ]

    assert len(path_open_calls) == 7, (
        f"Expected 7 Path/open calls in traversal fixture, found {len(path_open_calls)}"
    )

    tainted_count = 0
    safe_count = 0

    for node in path_open_calls:
        result = sb.run(
            evaluator,
            node=node,
            root=root,
            source=source,
            lang="python",
            file_path=fixture_path,
        )
        assert result.failure is None, (
            f"Evaluator failed for call {node.text[:60]!r}: "
            f"failure={result.failure!r}, detail={result.detail!r}"
        )
        if result.value is True:
            tainted_count += 1
        else:
            safe_count += 1

    assert tainted_count == 3, (
        f"Expected 3 tainted calls (Path(user_input), Path(USER_PATH), open(filename)), "
        f"got {tainted_count}"
    )
    assert safe_count == 4, (
        f"Expected 4 safe calls (Path('/etc/passwd'), Path('/static/config.yaml'), "
        f"open('/var/log/app.log'), open('/static/file.txt')), got {safe_count}"
    )


# ---------------------------------------------------------------------------
# Scenario 4: Concatenation-based string injection scanner (Python)
# ---------------------------------------------------------------------------


def test_concat_injection_scanner_python() -> None:
    """Concatenation injection scanner identifies 3 tainted + 3 safe assignments.

    The test walks assignment nodes whose RHS is a binary_operator and passes
    the binary_operator node to the sandbox evaluator.  The evaluator checks
    whether the expression is tainted by an identifier:

      - child[0].type == 'identifier'         direct left-hand identifier
      - child[1].type == 'identifier'         direct right-hand identifier
      - child[0].type == 'binary_operator'    chained concat (e.g. 'a'+b+'c');
                                              the outer node's left child is itself
                                              a binary_operator that contains the
                                              identifier — flagging the outer node
                                              as tainted via structural heuristic

    Assignments whose RHS is a plain string constant are counted as safe without
    entering the sandbox (no binary_operator RHS).

    Fixture: fixtures/security/concat.py
    Expected:
      tainted (binary_operator RHS): query1, log1, msg1  -> 3
      safe    (constant string RHS): query2, log2, msg2  -> 3
    """
    source = (FIXTURES_SECURITY / "concat.py").read_text()
    engine = AstSearchEngine()
    root = engine.parse(source, "python")
    fixture_path = str(FIXTURES_SECURITY / "concat.py")

    sb = PythonEvaluatorSandbox()

    # Evaluator: node = binary_operator node.
    # Returns True if either direct child is an identifier, or if the left child
    # is itself a binary_operator (indicating chained concatenation — heuristic
    # for recursive taint without requiring actual recursion in the evaluator).
    evaluator = (
        "return ("
        'node.named_children[0].type == "identifier"'
        ' or node.named_children[1].type == "identifier"'
        ' or node.named_children[0].type == "binary_operator"'
        ")"
    )

    # Walk all assignment nodes
    all_assignments = _walk_nodes(root, "assignment")

    tainted_count = 0
    safe_count = 0

    for assign in all_assignments:
        if len(assign.named_children) < 2:
            continue
        rhs = assign.named_children[1]

        if rhs.type == "binary_operator":
            # Run evaluator on the binary_operator RHS
            result = sb.run(
                evaluator,
                node=rhs,
                root=root,
                source=source,
                lang="python",
                file_path=fixture_path,
            )
            assert result.failure is None, (
                f"Evaluator failed for assignment "
                f"{assign.named_children[0].text!r}: "
                f"failure={result.failure!r}, detail={result.detail!r}"
            )
            if result.value is True:
                tainted_count += 1
            else:
                safe_count += 1
        else:
            # Non-binary-operator RHS is a safe constant
            safe_count += 1

    assert tainted_count == 3, (
        f"Expected 3 tainted concatenations (query1, log1, msg1), got {tainted_count}"
    )
    assert safe_count == 3, (
        f"Expected 3 safe assignments (query2, log2, msg2), got {safe_count}"
    )
