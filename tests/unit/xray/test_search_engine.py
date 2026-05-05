"""Unit tests for XRaySearchEngine.

Tests the two-phase X-Ray search: regex driver (Phase 1) + AST evaluator (Phase 2).

CONTRACT (file-as-unit, v10.4.0):
  - Phase 1 returns candidate FILES (any file with >=1 regex hit)
  - Phase 2 calls evaluator ONCE per candidate file with:
      node=root, root, source, lang, file_path, match_positions: List[Dict]
  - Evaluator returns {"matches": [...], "value": <anything>}
  - Each match in matches[] has at minimum line_number: int
  - Server enriches match with file_path, language, line_content (from source)
  - context_before/after derived when context_lines>0 and evaluator omits them
  - value surfaces per-file in file_metadata field

Uses real PythonEvaluatorSandbox and AstSearchEngine — no mocking of core logic.
Fixtures live in tests/unit/xray/fixtures/search_engine/.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "search_engine"


@pytest.fixture
def search_engine():
    """Instantiate XRaySearchEngine, skipping if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestXRaySearchEngineResultShape:
    """XRaySearchEngine.run() returns the documented dict shape."""

    def test_run_returns_required_keys(self, search_engine):
        """Result dict must contain all required keys from the output schema."""
        result = search_engine.run(
            repo_path=FIXTURES_DIR,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert "matches" in result
        assert "evaluation_errors" in result
        assert "files_processed" in result
        assert "files_total" in result
        assert "elapsed_seconds" in result

    def test_run_elapsed_seconds_is_positive_float(self, search_engine):
        """elapsed_seconds must be a non-negative float."""
        result = search_engine.run(
            repo_path=FIXTURES_DIR,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert isinstance(result["elapsed_seconds"], float)
        assert result["elapsed_seconds"] >= 0.0

    def test_run_files_processed_equals_candidate_count(self, search_engine):
        """files_processed must reflect actual number of files evaluated."""
        result = search_engine.run(
            repo_path=FIXTURES_DIR,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        # sample_match.py contains the pattern; files_processed must be at least 1
        assert result["files_processed"] >= 1

    def test_run_match_entry_has_required_fields(self, search_engine):
        """Each match entry must have the documented fields: file_path, line_number, language."""
        result = search_engine.run(
            repo_path=FIXTURES_DIR,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) >= 1
        match = result["matches"][0]
        assert "file_path" in match
        assert "line_number" in match
        assert "language" in match
        # line_content server-enriched from source
        assert "line_content" in match

    def test_run_no_match_when_regex_finds_nothing(self, search_engine):
        """When the regex matches nothing, matches list is empty."""
        result = search_engine.run(
            repo_path=FIXTURES_DIR,
            driver_regex=r"XYZZY_PATTERN_THAT_NEVER_EXISTS",
            evaluator_code='return {"matches": [], "value": None}',
            search_target="content",
        )
        assert result["matches"] == []
        assert result["files_processed"] == 0
        assert result["files_total"] == 0


class TestXRaySearchEngineFileAsUnit:
    """Evaluator is called ONCE per candidate file with match_positions list."""

    def test_evaluator_called_once_per_file(self, search_engine, tmp_path):
        """With 2 regex hits in one file, evaluator is called once (not twice)."""
        (tmp_path / "file.py").write_text(
            "prepareStatement(sql1)\nprepareStatement(sql2)\n"
        )
        call_count = [0]
        real_run = search_engine.sandbox.run

        def counting_run(code, **kwargs):
            call_count[0] += 1
            return real_run(code, **kwargs)

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"prepareStatement",
                evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
                search_target="content",
            )

        # One file → one evaluator call regardless of hit count
        assert call_count[0] == 1

    def test_match_positions_passed_to_evaluator(self, search_engine, tmp_path):
        """match_positions list contains all Phase 1 hits for the file."""
        (tmp_path / "file.py").write_text(
            "prepareStatement(sql1)\nprepareStatement(sql2)\n"
        )
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            # Return match for every position in match_positions list
            evaluator_code=(
                'matches = [{"line_number": p["line_number"]} for p in match_positions]\n'
                'return {"matches": matches, "value": len(match_positions)}'
            ),
            search_target="content",
        )
        # Two hits in source → two match entries returned
        assert len(result["matches"]) == 2

    def test_match_positions_is_list_of_dicts(self, search_engine, tmp_path):
        """Each entry in match_positions has line_number, line_content, column, byte_offset."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'pos = match_positions[0]\n'
                'return {"matches": [{"line_number": pos["line_number"], '
                '"has_line_content": "line_content" in pos, '
                '"has_byte_offset": "byte_offset" in pos}], "value": None}'
            ),
            search_target="content",
        )
        assert len(result["matches"]) >= 1
        m = result["matches"][0]
        assert m["line_number"] >= 1
        assert m["has_line_content"] is True
        assert m["has_byte_offset"] is True

    def test_filename_mode_match_positions_empty(self, search_engine, tmp_path):
        """In filename mode, match_positions is an empty list."""
        (tmp_path / "prepareStatement_usage.py").write_text("pass")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'return {"matches": [{"line_number": 1}], "value": len(match_positions)}'
            ),
            search_target="filename",
        )
        # value captures len(match_positions) which must be 0 in filename mode
        file_meta = result.get("file_metadata", [])
        assert any(fm["value"] == 0 for fm in file_meta)


class TestXRaySearchEngineEvaluatorReturnContract:
    """Evaluator must return a dict {matches, value}; other returns are errors."""

    def test_dict_return_with_matches_produces_match_entries(self, search_engine, tmp_path):
        """Dict return with matches list produces entries in result matches."""
        (tmp_path / "file.py").write_text("prepareStatement(sql)\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": "ok"}'
            ),
            search_target="content",
        )
        assert len(result["matches"]) == 1
        assert result["matches"][0]["line_number"] >= 1

    def test_dict_return_with_empty_matches_is_no_match(self, search_engine, tmp_path):
        """Dict return with empty matches list means file did not match."""
        (tmp_path / "file.py").write_text("prepareStatement(sql)\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [], "value": None}',
            search_target="content",
        )
        assert result["matches"] == []

    def test_dict_return_missing_matches_key_is_error(self, search_engine, tmp_path):
        """Dict without 'matches' key produces an error entry."""
        (tmp_path / "file.py").write_text("prepareStatement(sql)\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"value": 42}',  # missing 'matches'
            search_target="content",
        )
        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert "InvalidEvaluatorReturn" in err["error_type"] or "MissingMatchesKey" in err["error_type"]

    def test_bool_return_produces_error(self, search_engine, tmp_path):
        """Bool return (legacy contract) produces an error explaining dict is required."""
        (tmp_path / "file.py").write_text("prepareStatement(sql)\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",  # old bool contract
            search_target="content",
        )
        # bool return should produce InvalidEvaluatorReturn error (not a match)
        assert result["matches"] == []
        assert len(result["evaluation_errors"]) >= 1


class TestXRaySearchEngineServerEnrichment:
    """Server fills in missing fields that evaluator omits."""

    def test_server_fills_line_content_from_source(self, search_engine, tmp_path):
        """If evaluator omits line_content, server derives it from source."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            # Evaluator provides only line_number — server must fill line_content
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert "line_content" in m
        assert "prepareStatement" in m["line_content"]

    def test_server_respects_evaluator_provided_line_content(self, search_engine, tmp_path):
        """If evaluator provides line_content, server uses it as-is."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'return {"matches": [{"line_number": match_positions[0]["line_number"], '
                '"line_content": "CUSTOM_CONTENT"}], "value": None}'
            ),
            search_target="content",
        )
        assert len(result["matches"]) == 1
        assert result["matches"][0]["line_content"] == "CUSTOM_CONTENT"

    def test_server_always_adds_file_path(self, search_engine, tmp_path):
        """file_path is always server-provided (evaluator sees one file)."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert "file_path" in m
        assert str(tmp_path) in m["file_path"]

    def test_server_always_adds_language(self, search_engine, tmp_path):
        """language is always server-provided."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) == 1
        assert "language" in result["matches"][0]
        assert result["matches"][0]["language"] == "python"


class TestXRaySearchEngineValueField:
    """Per-file value surfaces in file_metadata."""

    def test_value_surfaces_in_file_metadata(self, search_engine, tmp_path):
        """value from evaluator appears in file_metadata keyed by file_path."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": "my_value"}',
            search_target="content",
        )
        assert "file_metadata" in result
        meta_list = result["file_metadata"]
        assert len(meta_list) >= 1
        assert any(fm["value"] == "my_value" for fm in meta_list)

    def test_file_metadata_entry_has_file_path(self, search_engine, tmp_path):
        """Each file_metadata entry has a file_path field."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": 42}',
            search_target="content",
        )
        for fm in result.get("file_metadata", []):
            assert "file_path" in fm
            assert "value" in fm

    def test_none_value_not_in_file_metadata(self, search_engine, tmp_path):
        """When evaluator returns value=None, file_metadata entry is omitted."""
        (tmp_path / "file.py").write_text("x = prepareStatement()\n")
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        # None value should not appear in file_metadata
        for fm in result.get("file_metadata", []):
            assert fm["value"] is not None


class TestXRaySearchEngineMaxFiles:
    """max_files parameter caps the number of candidate files evaluated."""

    def test_max_files_caps_files_evaluated(self, search_engine, tmp_path):
        """When max_files=1 and driver finds 2+ files, only 1 is evaluated."""
        (tmp_path / "file1.py").write_text("def foo(): prepareStatement()")
        (tmp_path / "file2.py").write_text("def bar(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            max_files=1,
        )
        assert result.get("partial") is True
        assert result.get("max_files_reached") is True
        assert result["files_processed"] == 1

    def test_no_partial_when_max_files_not_reached(self, search_engine, tmp_path):
        """When max_files is larger than candidates, partial is not set."""
        (tmp_path / "file1.py").write_text("def foo(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            max_files=100,
        )
        assert "partial" not in result or result.get("partial") is not True

    def test_max_files_none_does_not_cap(self, search_engine, tmp_path):
        """When max_files=None (default), all candidates are evaluated."""
        for i in range(3):
            (tmp_path / f"file{i}.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            max_files=None,
        )
        assert result["files_processed"] == 3
        assert "partial" not in result or result.get("partial") is not True


class TestXRaySearchEngineEvaluationErrors:
    """evaluation_errors populated on evaluator failures."""

    def test_evaluation_errors_on_subprocess_crash(self, search_engine, tmp_path):
        """Evaluator that crashes at runtime populates evaluation_errors."""
        (tmp_path / "file1.py").write_text("def foo(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return undefined_name_xyz",  # crashes at runtime
            search_target="content",
        )
        assert isinstance(result["evaluation_errors"], list)
        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert "file_path" in err
        assert "line_number" in err
        assert "error_type" in err
        assert "error_message" in err

    def test_evaluation_errors_do_not_fail_job(self, search_engine, tmp_path):
        """Evaluation errors do not prevent results from being returned."""
        (tmp_path / "file1.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return undefined_name_xyz",  # crashes at runtime
            search_target="content",
        )
        assert "evaluation_errors" in result
        assert "matches" in result

    def test_unsupported_language_populates_error(self, search_engine, tmp_path):
        """Files with unsupported extensions produce UnsupportedLanguage error entries."""
        (tmp_path / "file1.xyz").write_text("prepareStatement some content")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [], "value": None}',
            search_target="content",
        )
        assert isinstance(result["evaluation_errors"], list)
        error_types = [e["error_type"] for e in result["evaluation_errors"]]
        assert "UnsupportedLanguage" in error_types


class TestXRaySearchEngineSearchTarget:
    """search_target=filename searches file paths, not content."""

    def test_filename_target_matches_path_pattern(self, search_engine, tmp_path):
        """search_target=filename applies regex to file path, not content."""
        target_file = tmp_path / "prepareStatement_usage.py"
        target_file.write_text("def foo(): pass")
        other_file = tmp_path / "other.py"
        other_file.write_text("def bar(): pass")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": 1}], "value": None}',
            search_target="filename",
        )
        assert result["files_total"] == 1

    def test_content_target_matches_file_content(self, search_engine, tmp_path):
        """search_target=content applies regex to file content."""
        target_file = tmp_path / "ordinary_name.py"
        target_file.write_text("conn.prepareStatement(sql)")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert result["files_total"] == 1


class TestXRaySearchEngineIncludeExcludePatterns:
    """include_patterns and exclude_patterns filter the candidate set."""

    def test_include_patterns_filter_candidates(self, search_engine, tmp_path):
        """Only files matching include_patterns are considered."""
        (tmp_path / "match.py").write_text("prepareStatement()")
        (tmp_path / "match.java").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_patterns=["*.py"],
        )
        assert result["files_total"] == 1

    def test_exclude_patterns_remove_candidates(self, search_engine, tmp_path):
        """Files matching exclude_patterns are excluded."""
        subdir = tmp_path / "tests"
        subdir.mkdir()
        (tmp_path / "main.py").write_text("prepareStatement()")
        (subdir / "test_main.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            exclude_patterns=["tests/*"],
        )
        assert result["files_total"] == 1


class TestXRaySearchEngineCoverageEdgeCases:
    """Branch coverage for evaluator_timeout, non_dict, OSError, and bare except."""

    def test_evaluator_timeout_populates_evaluation_errors(
        self, search_engine, tmp_path
    ):
        """evaluator_timeout sandbox result produces EvaluatorTimeout error entry."""
        from code_indexer.xray.sandbox import EvalResult

        (tmp_path / "file.py").write_text("prepareStatement()")
        timeout_result = EvalResult(failure="evaluator_timeout")

        with patch.object(search_engine.sandbox, "run", return_value=timeout_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"prepareStatement",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert any(
            e["error_type"] == "EvaluatorTimeout" for e in result["evaluation_errors"]
        )

    def test_parse_exception_populates_evaluation_errors(self, search_engine, tmp_path):
        """Unexpected exception during parse is caught and added to evaluation_errors."""
        (tmp_path / "file.py").write_text("prepareStatement()")

        with patch.object(
            search_engine.ast_engine,
            "parse",
            side_effect=RuntimeError("parse failure"),
        ):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"prepareStatement",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(result["evaluation_errors"]) >= 1
        assert result["evaluation_errors"][0]["error_type"] == "RuntimeError"

    def test_evaluator_returned_non_dict_populates_error(self, search_engine, tmp_path):
        """Non-dict return from sandbox produces InvalidEvaluatorReturn error entry.

        The sandbox subprocess sends back whatever the evaluator returns.
        A dict is required; bool/int/str produces an error.
        """
        from code_indexer.xray.sandbox import EvalResult

        (tmp_path / "file.py").write_text("prepareStatement()")
        # Simulate sandbox returning success with a bool (old contract)
        bool_result = EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", return_value=bool_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"prepareStatement",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert any(
            e["error_type"] == "InvalidEvaluatorReturn"
            for e in result["evaluation_errors"]
        )


class TestXRaySearchEngineProgressCallback:
    """progress_callback is called with (percent, phase_name, phase_detail)."""

    def test_progress_callback_called_at_start_and_end(self, search_engine, tmp_path):
        """progress_callback receives at least a 0% start and 100% complete call."""
        calls = []

        def callback(percent, phase, detail):
            calls.append((percent, phase, detail))

        (tmp_path / "file.py").write_text("prepareStatement()")

        search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            progress_callback=callback,
        )
        assert len(calls) >= 2
        assert calls[0][0] == 0
        assert calls[-1][0] == 100


class TestXRaySearchEngineAstDebug:
    """include_ast_debug=True adds ast_debug field to each match."""

    def test_ast_debug_field_present_when_flag_true(self, search_engine, tmp_path):
        """Each match contains ast_debug when include_ast_debug=True."""
        (tmp_path / "file.py").write_text("def foo(): pass  # prepareStatement")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        assert len(result["matches"]) >= 1
        assert "ast_debug" in result["matches"][0]

    def test_ast_debug_absent_by_default(self, search_engine, tmp_path):
        """ast_debug is absent when include_ast_debug is not set (default)."""
        (tmp_path / "file.py").write_text("def foo(): pass  # prepareStatement")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) >= 1
        assert "ast_debug" not in result["matches"][0]

    def test_ast_debug_has_required_fields(self, search_engine, tmp_path):
        """ast_debug root node contains all documented fields."""
        (tmp_path / "file.py").write_text("def foo(): pass  # prepareStatement")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        ast_debug = result["matches"][0]["ast_debug"]
        assert "type" in ast_debug
        assert "start_byte" in ast_debug
        assert "end_byte" in ast_debug
        assert "start_point" in ast_debug
        assert "end_point" in ast_debug
        assert "text_preview" in ast_debug
        assert "child_count" in ast_debug
        assert "children" in ast_debug

    def test_ast_debug_start_point_is_two_element_list(self, search_engine, tmp_path):
        """start_point and end_point are [row, col] two-element lists."""
        (tmp_path / "file.py").write_text("def foo(): pass  # prepareStatement")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        ast_debug = result["matches"][0]["ast_debug"]
        assert isinstance(ast_debug["start_point"], list)
        assert len(ast_debug["start_point"]) == 2
        assert isinstance(ast_debug["end_point"], list)
        assert len(ast_debug["end_point"]) == 2

    def test_ast_debug_text_preview_truncated_to_80_chars(
        self, search_engine, tmp_path
    ):
        """text_preview is at most 80 characters."""
        long_line = "x = " + "a" * 200 + "  # prepareStatement\n"
        (tmp_path / "file.py").write_text(long_line)

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        assert len(result["matches"]) >= 1
        ast_debug = result["matches"][0]["ast_debug"]
        assert len(ast_debug["text_preview"]) <= 80

    def test_ast_debug_max_debug_nodes_caps_total_nodes(self, search_engine, tmp_path):
        """max_debug_nodes=2 limits total serialized nodes to at most 2."""
        (tmp_path / "file.py").write_text(
            "class Foo:\n    def bar(self): prepareStatement()\n"
        )

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
            max_debug_nodes=2,
        )
        assert len(result["matches"]) >= 1
        ast_debug = result["matches"][0]["ast_debug"]

        def count_nodes(node: dict) -> int:
            count = 1
            for child in node.get("children", []):
                if child.get("type") == "...truncated":
                    count += 1
                else:
                    count += count_nodes(child)
            return count

        total = count_nodes(ast_debug)
        assert total <= 2 + 1  # root + cap + possible truncated marker

    def test_ast_debug_truncated_marker_present_when_cap_hit(
        self, search_engine, tmp_path
    ):
        """A '...truncated' sentinel appears in children when the node cap is hit."""
        (tmp_path / "file.py").write_text(
            "class Foo:\n    def bar(self):\n        x = prepareStatement()\n"
        )

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
            max_debug_nodes=1,
        )
        assert len(result["matches"]) >= 1
        ast_debug = result["matches"][0]["ast_debug"]

        def has_truncated(node: dict) -> bool:
            for child in node.get("children", []):
                if child.get("type") == "...truncated":
                    return True
                if has_truncated(child):
                    return True
            return False

        assert has_truncated(ast_debug)

    def test_ast_debug_children_is_list(self, search_engine, tmp_path):
        """children field is always a list."""
        (tmp_path / "file.py").write_text("def foo(): pass  # prepareStatement")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        assert isinstance(result["matches"][0]["ast_debug"]["children"], list)


class TestXRaySearchEngineMatchedNode:
    """matched_node field is present when include_ast_debug=True."""

    def test_matched_node_present_when_ast_debug_true(self, search_engine, tmp_path):
        """matched_node is present when include_ast_debug=True."""
        (tmp_path / "file.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        assert len(result["matches"]) >= 1
        assert "matched_node" in result["matches"][0]

    def test_matched_node_has_required_fields(self, search_engine, tmp_path):
        """matched_node has type, start_byte, end_byte, start_point, end_point."""
        (tmp_path / "file.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        mn = result["matches"][0]["matched_node"]
        for field in ("type", "start_byte", "end_byte", "start_point", "end_point"):
            assert field in mn

    def test_matched_node_field_types(self, search_engine, tmp_path):
        """matched_node fields have correct types."""
        (tmp_path / "file.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
            include_ast_debug=True,
        )
        mn = result["matches"][0]["matched_node"]
        assert isinstance(mn["type"], str)
        assert isinstance(mn["start_byte"], int)
        assert isinstance(mn["end_byte"], int)
        assert isinstance(mn["start_point"], list)
        assert isinstance(mn["end_point"], list)

    def test_matched_node_absent_when_ast_debug_false(self, search_engine, tmp_path):
        """matched_node is absent when include_ast_debug=False (default)."""
        (tmp_path / "file.py").write_text("prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code='return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}',
            search_target="content",
        )
        assert len(result["matches"]) >= 1
        assert "matched_node" not in result["matches"][0]
