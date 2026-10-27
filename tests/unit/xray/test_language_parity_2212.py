"""Bug #2212: server X-Ray language detection and parse paths.

The Rust-source parity check needs no tree-sitter and lives in
test_language_source_parity_2212.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from code_indexer.xray.languages import EXTENSION_MAP
from tests.unit.xray.new_language_fixtures_2212 import (
    MARKER,
    NEW_LANGUAGE_FILES,
    PYTHON_PARSEABLE,
    PYTHON_REFUSED,
    write_new_language_repo,
)

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")


# ^ why lazy trigram build off, not on; its daemon thread outlives teardown => logs to a closed capture stream
@pytest.fixture(autouse=True)
def _no_lazy_trigram_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


class TestEngineDetectionMap:
    def test_engine_extension_map_equals_search_detection_map(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        assert AstSearchEngine().extension_map == EXTENSION_MAP


_KIND_WALK_EVALUATOR = """fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut out = Vec::new();
    let mut stack: Vec<&OwnedNode> = vec![node];
    while let Some(n) = stack.pop() {
        if n.is_named {
            out.push(EvalFinding { pattern: "kind".to_string(), line: n.start_line, snippet: n.kind.clone() });
        }
        for c in n.children.iter().rev() {
            stack.push(c);
        }
    }
    out
}
"""


# ^ why Any, not object; tree_sitter.Node has no type stubs => mypy attr-defined on is_named/type
def _python_named_kinds(root: Any) -> List[str]:
    kinds: List[str] = []
    stack: List[Any] = [getattr(root, "_node", root)]
    while stack:
        node = stack.pop()
        if node.is_named:
            kinds.append(node.type)
        stack.extend(reversed(node.children))
    return kinds


class TestPythonParsePaths:
    def test_parse_refuses_new_languages_without_matching_grammar(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        for ext in sorted(PYTHON_REFUSED):
            name, content, label = NEW_LANGUAGE_FILES[ext]
            assert engine.detect_language(name) == label
            with pytest.raises(ValueError) as exc_info:
                engine.parse(content, label)
            assert type(exc_info.value).__name__ == "AstDumpUnsupportedError"
            message = str(exc_info.value)
            assert "AST dump not supported for this language" in message
            assert repr(label) in message

    def test_search_detection_needs_no_python_grammar(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        with patch(
            "tree_sitter_languages.get_language",
            side_effect=Exception("grammar not available"),
        ):
            engine = AstSearchEngine()
        for name, _, label in NEW_LANGUAGE_FILES.values():
            assert engine.detect_language(name) == label, name

    def test_python_dump_kinds_equal_rust_kinds(self, tmp_path: Path) -> None:
        from tests.unit.xray.conftest import require_xray_cli_binary

        require_xray_cli_binary()
        from code_indexer.xray.ast_engine import AstSearchEngine
        from code_indexer.xray.rust_backend import RustNativeBackend

        engine = AstSearchEngine()
        repo = write_new_language_repo(tmp_path)
        parseable = {
            ext
            for ext, (_, _, label) in NEW_LANGUAGE_FILES.items()
            if engine.can_parse(label)
        }
        assert parseable == PYTHON_PARSEABLE
        exts = sorted(parseable)
        specs = [
            {"file_path": NEW_LANGUAGE_FILES[e][0], "lang": "", "match_positions": []}
            for e in exts
        ]
        results = RustNativeBackend().run_batch(
            evaluator_code=_KIND_WALK_EVALUATOR,
            file_specs=specs,
            repo_path=str(repo),
        )
        assert len(results) == len(exts), results
        for ext, (matches, errors, _) in zip(exts, results):
            name, content, label = NEW_LANGUAGE_FILES[ext]
            assert errors == [], (ext, errors)
            rust_kinds = [m["snippet"] for m in matches]
            python_kinds = _python_named_kinds(engine.parse(content, label))
            assert rust_kinds, ext
            assert python_kinds == rust_kinds, ext


_ALWAYS_MATCH = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


def _run_search(repo: Path) -> Dict[str, Any]:
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine().run(
        repo_path=repo,
        driver_regex=MARKER,
        evaluator_code=_ALWAYS_MATCH,
        search_target="content",
    )


class TestScanLevelUnsupportedLanguage:
    def test_all_unsupported_candidates_report_one_scan_level_error(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        (tmp_path / "page.svelte").write_text(f"<template>{MARKER}</template>\n")
        (tmp_path / "notes.rst").write_text(f"{MARKER}\n")

        result = _run_search(tmp_path)

        scan_level = [e for e in result["evaluation_errors"] if e["file_path"] == ""]
        per_file = sorted(
            e["file_path"] for e in result["evaluation_errors"] if e["file_path"]
        )
        assert per_file == ["notes.rst", "page.svelte"]
        assert len(scan_level) == 1, result["evaluation_errors"]
        assert scan_level[0]["error_type"] == "UnsupportedLanguage"
        assert "'.rst'" in scan_level[0]["error_message"]
        assert "'.svelte'" in scan_level[0]["error_message"]
        assert result["matches"] == []
        assert xray_result_outcome(result) == "unsupported_language"

    def test_mixed_candidates_have_no_scan_level_error(self, tmp_path: Path) -> None:
        from tests.unit.xray.conftest import require_xray_cli_binary

        require_xray_cli_binary()
        name, content, label = NEW_LANGUAGE_FILES["yaml"]
        (tmp_path / name).write_text(content)
        (tmp_path / "page.svelte").write_text(f"<template>{MARKER}</template>\n")

        result = _run_search(tmp_path)

        assert [(m["file_path"], m["language"]) for m in result["matches"]] == [
            (name, label)
        ]
        assert [e["file_path"] for e in result["evaluation_errors"]] == ["page.svelte"]


_NEVER_MATCH = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    let _ = node;\n"
    "    Vec::new()\n"
    "}\n"
)
_DOOMED_NAME = "b.py"


def _run_unparsed(repo: Path) -> Dict[str, Any]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    from code_indexer.xray.search_engine import XRaySearchEngine

    doomed = repo / _DOOMED_NAME

    # ^ order unlink after Phase 1 selected the file, before xray-cli starts => xray-cli read fails for any user
    def _delete_after_phase1(_pct: int, phase: str, _msg: str) -> None:
        if phase == "phase2_evaluator":
            doomed.unlink()

    engine = XRaySearchEngine()
    result = engine.run(
        repo_path=repo,
        driver_regex=r"[xy] =",
        evaluator_code=_NEVER_MATCH,
        search_target="content",
        progress_callback=_delete_after_phase1,
    )
    assert not doomed.exists()
    assert engine.rust_backend._last_files_errored == 1
    return result


class TestUnparsedFilesAreVisible:
    def test_partial_unparse_is_a_warning_not_a_whole_run_failure(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / _DOOMED_NAME).write_text("y = 2\n")

        result = _run_unparsed(tmp_path)

        assert [e for e in result["evaluation_errors"] if e["file_path"] == ""] == []
        assert result["unparsed_files"] == 1
        [warning] = [w for w in result["warnings"] if w["type"] == "unparsed_files"]
        assert warning["count"] == 1
        assert "1 of 2" in warning["hint"]
        assert result["files_processed"] == 1
        assert xray_result_outcome(result) is None

    def test_full_unparse_is_a_whole_run_failure(self, tmp_path: Path) -> None:
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        (tmp_path / _DOOMED_NAME).write_text("y = 2\n")

        result = _run_unparsed(tmp_path)

        [error] = result["evaluation_errors"]
        assert (error["file_path"], error["error_type"]) == ("", "UnparsedFiles")
        assert "1 of 1" in error["error_message"]
        assert result["files_processed"] == 0
        assert xray_result_outcome(result) == "unparsed_files"


class TestEvaluateFileAstDebug:
    def test_evaluate_file_ast_debug_refuses_or_dumps(self, tmp_path: Path) -> None:
        from tests.unit.xray.conftest import require_xray_cli_binary

        require_xray_cli_binary()
        from code_indexer.xray.search_engine import XRaySearchEngine

        engine = XRaySearchEngine()
        repo = write_new_language_repo(tmp_path)

        def _matches(ext: str) -> List[Dict[str, Any]]:
            matches, errors, _ = engine._evaluate_file(
                repo / NEW_LANGUAGE_FILES[ext][0],
                _ALWAYS_MATCH,
                include_ast_debug=True,
                max_debug_nodes=50,
            )
            assert errors == [] and matches, (ext, errors)
            return matches

        for match in _matches("sql"):
            assert "ast_debug" not in match
            assert match["ast_debug_error"] == (
                "AST dump not supported for this language: 'sql'"
            )
        for match in _matches("yaml"):
            assert match["ast_debug"]["type"] == "stream"
            assert "ast_debug_error" not in match
