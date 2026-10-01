"""Tests for RustNativeBackend graph-mode driver — Story #1811 (S5, AC2).

Covers `RustNativeBackend.run_graph_analysis()`: compiles an evaluator once
(reusing the existing compile/cache machinery), drives `--build-graph` then
`--analyze-graph`, and returns a structured result that honestly surfaces
`fact_graph_complete`/degradation counters (AC3's "no findings" vs "index
too incomplete to trust a negative" distinction) — never a raw exception
(Bug #1612's rule).

These are genuine component tests against the REAL compiled xray-cli
release binary (skipped if not built) — no subprocess mocking, mirroring
the existing `_require_xray_cli_binary()` pattern in test_rust_backend.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.xray.rust_backend import RustNativeBackend, _XRAY_CLI_DEFAULT

# Cross-file graph-mode evaluator: `helper()` is declared ONLY in B.java and
# called ONLY from A.java. A single-file (legacy) scan of B.java alone would
# report it dead code -- this evaluator can only produce a correct "not
# dead" answer for helper() when the graph spans BOTH files. This is the
# story's own required discriminating test, mirrored in Python from the
# Rust-level test at rust/xray-cli/src/main.rs::tests::
# build_graph_then_analyze_graph_finds_cross_file_reference.
CROSS_FILE_DEAD_CODE_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut i: u32 = 0;
    while i < 64 {
        if let Some(sym) = g.resolve_symbol(i) {
            if g.is_definitely_dead_code(i) == Some(false) {
                let sig = g.signature_for(i).unwrap_or("").to_string();
                result.findings.push(ReduceFinding {
                    pattern: "not_dead".to_string(),
                    message: sig.clone(),
                    involved: vec![sym],
                    signatures: vec![sig],
                });
            }
        }
        i += 1;
    }
    result
}
"""

# Evaluator with a forbidden construct -- must be rejected by
# validate_rust_evaluator() before any subprocess is ever spawned.
UNSAFE_GRAPH_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    unsafe {}
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"""


def _require_xray_cli_binary() -> None:
    """Skip this test if the real xray-cli release binary is not built locally."""
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def _write_cross_file_fixture(repo_root: Path) -> None:
    (repo_root / "A.java").write_text("class A { void run() { helper(); } }\n")
    (repo_root / "B.java").write_text("class B { void helper() {} }\n")


def test_run_graph_analysis_cross_file_reference_end_to_end(tmp_path: Path) -> None:
    """THE central end-to-end proof of AC2's wiring: compile once, build a
    real multi-file graph, analyze it, and find helper() (declared in
    B.java, called only from A.java) correctly reported as NOT dead code --
    provable only when both files were indexed together.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    backend = RustNativeBackend(xray_cache_backend=None)
    result = backend.run_graph_analysis(
        evaluator_code=CROSS_FILE_DEAD_CODE_EVALUATOR,
        repo_root=str(repo_root),
        file_paths=["A.java", "B.java"],
        timeout_seconds=60,
    )

    assert result["ok"] is True, f"expected success, got error={result.get('error')}"
    assert result["status"] == "ran_ok"
    assert result["fact_graph_complete"] is True, (
        "a clean two-file build must be complete"
    )

    findings = result["findings"]
    b_findings = [
        f
        for f in findings
        if any("helper" in str(sig) for sig in f.get("signatures", []))
    ]
    assert len(b_findings) == 1, (
        f"expected exactly one helper() finding, got: {findings}"
    )


def test_run_graph_analysis_flags_incomplete_and_surfaces_unsupported_language_counter_for_non_java_repo(
    tmp_path: Path,
) -> None:
    """Consolidated review finding C2 (Issue #1811/Bug #1812): the graph
    extractor supports ONLY Java. A repo containing a well-formed,
    recognized-extension non-Java file (Python here) must report
    `fact_graph_complete=False` and surface a nonzero
    `degradation["files_with_unsupported_language"]` count -- never the
    dishonest `fact_graph_complete: True` with all-zero degradation this
    bug produced (a confident false "verified clean" reading for any
    non-Java repo, per the tool's own documented honesty-signal contract).
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "A.java").write_text("class A { void run() {} }\n")
    (repo_root / "script.py").write_text("def totally_unused():\n    pass\n")

    backend = RustNativeBackend(xray_cache_backend=None)
    result = backend.run_graph_analysis(
        evaluator_code=CROSS_FILE_DEAD_CODE_EVALUATOR,
        repo_root=str(repo_root),
        file_paths=["A.java", "script.py"],
        timeout_seconds=60,
    )

    assert result["ok"] is True, f"expected success, got error={result.get('error')}"
    assert result["fact_graph_complete"] is False, (
        "a repo containing a file whose language has no graph extractor "
        "must never report fact_graph_complete=True"
    )
    assert result["degradation"]["files_with_unsupported_language"] == 1, (
        f"expected the Python file to be counted, got degradation={result['degradation']}"
    )


_H9_COMPILE_TIMEOUT_SECONDS = 60


def test_get_cache_identity_info_graph_mode_matches_real_compiled_identity(
    tmp_path: Path,
) -> None:
    """Consolidated review finding H9 (Issue #1811/Bug #1812, Codex): a
    graph-mode-aware identity call must report EXACTLY the identity
    `xray-cli --compile-only` actually uses as the real compiled `.so`
    filename for a graph-mode evaluator -- otherwise a cluster-cache
    pre-fill/post-fill keyed on the wrong (legacy) identity can never be
    consumed by, or correctly upload for, the graph compile path.

    Note: `xray-cli --compile-only` is mode-AGNOSTIC at the CLI level --
    `compile_evaluator_impl`'s `detect_evaluator_mode` classifies Graph vs
    Legacy purely from the evaluator's SOURCE CONTENT (whether it defines
    `collect_facts`+`analyze_graph` vs `evaluate_node`), never a flag. So
    compiling `CROSS_FILE_DEAD_CODE_EVALUATOR` (a real graph-mode source)
    via `--compile-only` genuinely produces the real graph-mode `.so` and
    identity this test needs as its ground truth.
    """
    _require_xray_cli_binary()

    backend = RustNativeBackend(xray_cache_backend=None)
    real_output, error = backend._run_compile_only_subprocess(
        str(_write_temp_eval_file(tmp_path, CROSS_FILE_DEAD_CODE_EVALUATOR)),
        _H9_COMPILE_TIMEOUT_SECONDS,
    )
    assert error is None, f"expected a real compile, got error: {error}"
    real_so_stem = Path(real_output["so_path"]).stem

    info = backend._get_cache_identity_info(
        CROSS_FILE_DEAD_CODE_EVALUATOR, graph_mode=True
    )
    assert info is not None, "expected a real CacheIdentityInfo, got None"
    assert info.identity == real_so_stem, (
        f"graph-mode identity {info.identity!r} must match the real "
        f"compiled .so stem {real_so_stem!r}"
    )


def _write_temp_eval_file(tmp_path: Path, source: str) -> Path:
    eval_path = tmp_path / "eval.rs"
    eval_path.write_text(source)
    return eval_path


def test_compile_for_graph_mode_passes_graph_mode_to_pre_and_post_fill(
    tmp_path: Path,
) -> None:
    """Consolidated review finding H9 (Issue #1811/Bug #1812, Codex): the
    graph-mode identity fix in `_get_cache_identity_info` is unreachable
    from production unless `_compile_for_graph_mode` actually passes
    `graph_mode=True` down through `_try_pre_fill`/`_try_post_fill`. This
    proves the real production call path, not just the identity function
    in isolation.
    """
    from unittest.mock import MagicMock, patch

    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # A UNIQUE evaluator source (fresh UUID comment) guarantees a genuine
    # local xray-cache MISS -- reusing CROSS_FILE_DEAD_CODE_EVALUATOR would
    # risk a `cached=True` result if another test in this file already
    # compiled that exact shared fixture, which would skip _try_post_fill
    # entirely (by design: post-fill only fires on a real fresh compile)
    # and make this assertion flaky depending on test execution order.
    unique_evaluator_code = (
        f"// unique-test-marker: {uuid.uuid4().hex}\n" + CROSS_FILE_DEAD_CODE_EVALUATOR
    )

    backend = RustNativeBackend(xray_cache_backend=MagicMock())
    eval_path = _write_temp_eval_file(tmp_path, unique_evaluator_code)

    with (
        patch.object(backend, "_try_pre_fill") as mock_pre_fill,
        patch.object(backend, "_try_post_fill") as mock_post_fill,
    ):
        so_path, compile_info, error = backend._compile_for_graph_mode(
            unique_evaluator_code, str(eval_path), deadline_seconds=None
        )

    assert error is None, f"expected a real compile, got error: {error}"
    mock_pre_fill.assert_called_once()
    assert mock_pre_fill.call_args.kwargs.get("graph_mode") is True, (
        f"_try_pre_fill must be called with graph_mode=True, "
        f"got kwargs={mock_pre_fill.call_args.kwargs}"
    )
    mock_post_fill.assert_called_once()
    assert mock_post_fill.call_args.kwargs.get("graph_mode") is True, (
        f"_try_post_fill must be called with graph_mode=True, "
        f"got kwargs={mock_post_fill.call_args.kwargs}"
    )


class TestBuildGraphAnalysisResultSchemaValidation:
    """Consolidated review finding H11 (Issue #1811/Bug #1812, Codex):
    `_build_graph_analysis_result` must validate the shape of the JSON
    `xray-cli --analyze-graph` reports, not silently default missing
    required fields into a plausible success. A payload of just
    `{"status": "ran_ok"}` (no "result" key at all, or a "result" missing
    "findings"/"refine") must produce a structured MalformedCliOutput
    error, never `ok: True` with quietly-empty findings/refine.
    """

    _BUILD_RESULT_STUB = {
        "fact_graph_complete": True,
        "files_with_parse_errors": 0,
        "unreadable_or_unsupported_files": 0,
        "files_with_read_errors": 0,
        "files_with_extractor_panics": 0,
        "files_with_collector_panics": 0,
        "files_with_unsupported_language": 0,
        "truncated_by_max_files": False,
    }

    @pytest.mark.parametrize(
        "analyze_output",
        [
            {"status": "ran_ok"},  # no "result" key at all
            {"status": "ran_ok", "result": {}},  # missing BOTH required keys
            {"status": "ran_ok", "result": {"refine": []}},  # missing findings only
            {"status": "ran_ok", "result": {"findings": []}},  # missing refine only
        ],
        ids=["no_result_key", "empty_result", "missing_findings", "missing_refine"],
    )
    def test_ran_ok_with_incomplete_result_schema_is_rejected(
        self, analyze_output: Dict[str, Any]
    ) -> None:
        """A flawed fix validating only ONE required key, or none at all,
        must not pass every parametrized case here."""
        built = RustNativeBackend._build_graph_analysis_result(
            analyze_output, "ok", self._BUILD_RESULT_STUB, {}
        )

        assert built["ok"] is False, (
            f"an incomplete result schema must not be treated as success, got: {built}"
        )
        assert built["error"]["error_type"] == "MalformedCliOutput"

    def test_ran_ok_with_a_genuinely_complete_result_still_succeeds(self) -> None:
        """Regression guard: the new validation must not reject a REAL,
        well-formed ran_ok payload -- an evaluator that legitimately finds
        nothing still reports empty (present, not missing) arrays."""
        analyze_output = {
            "status": "ran_ok",
            "result": {"findings": [], "refine": []},
        }

        built = RustNativeBackend._build_graph_analysis_result(
            analyze_output, "ok", self._BUILD_RESULT_STUB, {}
        )

        assert built["ok"] is True, f"got: {built}"
        assert built["findings"] == []
        assert built["refine"] == []


def test_run_graph_analysis_validation_error_returns_structured_error(
    tmp_path: Path,
) -> None:
    """A graph-mode evaluator with a forbidden construct must be rejected
    BEFORE any subprocess is spawned -- structured ValidationError, never
    an unhandled exception (Bug #1612's rule).
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    backend = RustNativeBackend(xray_cache_backend=None)
    result = backend.run_graph_analysis(
        evaluator_code=UNSAFE_GRAPH_EVALUATOR,
        repo_root=str(repo_root),
        file_paths=["A.java", "B.java"],
        timeout_seconds=60,
    )

    assert result["ok"] is False
    assert result["error"]["error_type"] == "ValidationError"
    assert "unsafe" in result["error"]["error_message"].lower()


def test_run_graph_analysis_missing_binary_returns_structured_error(
    tmp_path: Path,
) -> None:
    """A missing xray-cli binary must produce a structured BinaryNotFound
    error, never an unhandled FileNotFoundError propagating to the caller.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    backend = RustNativeBackend(xray_cache_backend=None)
    backend._xray_cli_path = tmp_path / "does-not-exist" / "xray-cli"

    result = backend.run_graph_analysis(
        evaluator_code=CROSS_FILE_DEAD_CODE_EVALUATOR,
        repo_root=str(repo_root),
        file_paths=["A.java", "B.java"],
        timeout_seconds=60,
    )

    assert result["ok"] is False
    assert result["error"]["error_type"] == "BinaryNotFound"


def test_run_graph_analysis_never_raises_for_a_nonexistent_repo_root(
    tmp_path: Path,
) -> None:
    """An invalid --repo-root must surface as a structured error via the
    build-graph status, never an unhandled exception or process crash.
    """
    _require_xray_cli_binary()
    nonexistent_repo_root = tmp_path / "does-not-exist"

    backend = RustNativeBackend(xray_cache_backend=None)
    result = backend.run_graph_analysis(
        evaluator_code=CROSS_FILE_DEAD_CODE_EVALUATOR,
        repo_root=str(nonexistent_repo_root),
        file_paths=["A.java"],
        timeout_seconds=60,
    )

    assert result["ok"] is False
    assert result["error"] is not None
    assert result["build_status"] == "repo_root_invalid"
