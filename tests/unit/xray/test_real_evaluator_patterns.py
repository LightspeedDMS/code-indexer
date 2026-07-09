"""Real-world evaluator pattern integration tests for the xray Rust pipeline.

Each test exercises the FULL pipeline: Rust evaluator code -> validate ->
compile via xray-cli -> execute against real Java source -> verify findings.

No mocks, no fakes.  Real Rust compilation, real execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
XRAY_CLI = REPO_ROOT / "rust" / "target" / "release" / "xray-cli"

pytestmark = pytest.mark.skipif(
    not XRAY_CLI.exists(),
    reason="xray-cli binary not found -- run: cd rust && cargo build --release",
)


# ---------------------------------------------------------------------------
# Java test fixtures -- real code with real patterns
# ---------------------------------------------------------------------------

JAVA_CATCH_RETHROW = """\
public class Demo {
    public void process() {
        try {
            doSomething();
        } catch (Exception e) {
            throw e;
        }
    }
    public void other() {
        try {
            work();
        } catch (IOException ex) {
            log(ex);
            throw new RuntimeException(ex);
        }
    }
}
"""

JAVA_ALLOCATION_IN_TRY = """\
public class ResourceManager {
    public void allocate() {
        try {
            Connection conn = new Connection();
            Statement stmt = new Statement();
        } finally {
            cleanup();
        }
    }
}
"""

JAVA_NO_PATTERN = """\
public class Clean {
    public int add(int a, int b) {
        return a + b;
    }
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_evaluator(
    evaluator_code: str,
    java_source: str,
    tmp_path: Path,
    filename: str = "Test.java",
):
    """Write Java source to tmp_path, run evaluator via RustNativeBackend.

    Returns (matches, errors, meta) for the single file.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    java_file = tmp_path / filename
    java_file.write_text(java_source, encoding="utf-8")

    backend = RustNativeBackend()
    file_specs = [
        {
            "file_path": filename,
            "source": java_source,
            "lang": "java",
            "match_positions": [],
        }
    ]
    results = backend.run_batch(
        evaluator_code=evaluator_code,
        file_specs=file_specs,
        repo_path=str(tmp_path),
    )
    assert len(results) == 1, f"Expected 1 result tuple, got {len(results)}"
    return results[0]


# ---------------------------------------------------------------------------
# Evaluator code -- real patterns that find real constructs
# ---------------------------------------------------------------------------

CATCH_RETHROW_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for cc in node.descendants_of_kind("catch_clause") {
        if let Some(body) = cc.child_by_kind("block") {
            let stmts = body.named_children();
            if stmts.len() == 1 {
                if stmts[0].kind == "throw_statement" {
                    findings.push(EvalFinding {
                        pattern: "catch-rethrow".to_string(),
                        line: cc.start_line,
                        snippet: cc.text().to_string(),
                    });
                }
            }
        }
    }
    findings
}
"""

ALLOCATION_IN_TRY_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for ts in node.descendants_of_kind("try_statement") {
        if let Some(body) = ts.child_by_kind("block") {
            let mut has_alloc = false;
            for child in body.named_children() {
                if child.has_descendant_of_kind("object_creation_expression") {
                    has_alloc = true;
                }
            }
            if has_alloc {
                findings.push(EvalFinding {
                    pattern: "allocation-in-try".to_string(),
                    line: ts.start_line,
                    snippet: ts.text().to_string(),
                });
            }
        }
    }
    findings
}
"""

CATCH_RETHROW_WITH_DICT_RETURN = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for cc in node.descendants_of_kind("catch_clause") {
        if let Some(body) = cc.child_by_kind("block") {
            let stmts = body.named_children();
            if stmts.len() == 1 {
                if stmts[0].kind == "throw_statement" {
                    findings.push(EvalFinding {
                        pattern: "catch-rethrow".to_string(),
                        line: cc.start_line,
                        snippet: cc.text().to_string(),
                    });
                }
            }
        }
    }
    findings
}
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRealEvaluatorPatterns:
    """Integration tests: real Java code + real evaluator + full Rust pipeline."""

    def test_catch_rethrow_found_in_real_java(self, tmp_path: Path):
        """Catch-rethrow pattern finds the bare rethrow but not the wrapped one."""
        matches, errors, meta = _run_evaluator(
            CATCH_RETHROW_EVALUATOR,
            JAVA_CATCH_RETHROW,
            tmp_path,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert len(matches) == 1, (
            f"Expected 1 catch-rethrow match (bare 'throw e'), got {len(matches)}: {matches}"
        )
        m = matches[0]
        assert m["pattern"] == "catch-rethrow"
        assert m["line_number"] == 5, (
            f"Expected match on line 5 (catch clause), got line {m['line_number']}"
        )

    def test_allocation_in_try_found(self, tmp_path: Path):
        """Allocation-in-try pattern finds object creation inside try block."""
        matches, errors, meta = _run_evaluator(
            ALLOCATION_IN_TRY_EVALUATOR,
            JAVA_ALLOCATION_IN_TRY,
            tmp_path,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert len(matches) >= 1, (
            f"Expected at least 1 allocation-in-try match, got {len(matches)}: {matches}"
        )
        assert matches[0]["pattern"] == "allocation-in-try"

    def test_no_false_positives_on_clean_code(self, tmp_path: Path):
        """Catch-rethrow evaluator produces 0 matches on code without try/catch."""
        matches, errors, meta = _run_evaluator(
            CATCH_RETHROW_EVALUATOR,
            JAVA_NO_PATTERN,
            tmp_path,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert len(matches) == 0, (
            f"Expected 0 matches on clean code, got {len(matches)}: {matches}"
        )

    def test_evaluator_with_dict_return_contract(self, tmp_path: Path):
        """Evaluator using standard Rust Vec<EvalFinding> return works end-to-end."""
        matches, errors, meta = _run_evaluator(
            CATCH_RETHROW_WITH_DICT_RETURN,
            JAVA_CATCH_RETHROW,
            tmp_path,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert len(matches) == 1, (
            f"Expected 1 catch-rethrow match via dict return, got {len(matches)}: {matches}"
        )
        assert matches[0]["pattern"] == "catch-rethrow"
