"""Tests for RustNativeBackend — Story #1023.

Covers:
- run_batch() return format: list of (matches, errors, meta) tuples
- Transpilation errors produce per-file error tuples with clear messages
- Missing xray-cli binary produces error tuples with clear message
- Subprocess JSON output is parsed and findings grouped by file
- Match dicts contain required fields: line_number, file_path, language
- Files with no findings return ([], [], None)
- line_content derived from source when finding line available
- snippet field preserved in match dict
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(
    file_path: str,
    source: str,
    lang: str,
    match_positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a file-spec dict for run_batch()."""
    return {
        "file_path": file_path,
        "source": source,
        "lang": lang,
        "match_positions": match_positions if match_positions is not None else [],
    }


SIMPLE_JAVA = """\
public class Foo {
    void method() {
        System.out.println("hello");
    }
}
"""

EVALUATOR_WITH_IMPORT = """\
import os

def evaluate_node(node):
    return {"matches": [], "value": None}
"""

VALID_EVALUATOR = """\
def evaluate_node(node):
    return {"matches": [], "value": None}
"""


# ---------------------------------------------------------------------------
# Test 1: Transpilation error returns error tuples for all files
# ---------------------------------------------------------------------------


def test_transpilation_error_returns_error_tuples_for_all_files():
    """When evaluator_code has forbidden constructs (import), all files get error tuples."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]
    results = backend.run_batch(
        evaluator_code=EVALUATOR_WITH_IMPORT,
        file_specs=specs,
    )

    assert len(results) == 2
    for matches, errors, meta in results:
        assert matches == []
        assert len(errors) == 1
        err = errors[0]
        assert err["error_type"] == "TranspileError"
        msg = err["error_message"].lower()
        assert "import" in msg or "transpil" in msg
        assert meta is None


# ---------------------------------------------------------------------------
# Test 2: Empty file_specs returns empty list
# ---------------------------------------------------------------------------


def test_run_batch_empty_file_specs_returns_empty_list():
    """run_batch with empty file_specs returns empty list."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    results = backend.run_batch(
        evaluator_code=VALID_EVALUATOR,
        file_specs=[],
    )
    assert results == []


# ---------------------------------------------------------------------------
# Test 3: Missing binary returns one error tuple per file spec
# ---------------------------------------------------------------------------


def test_missing_binary_returns_one_error_tuple_per_spec():
    """When xray-cli binary is missing, each file spec gets exactly one error tuple."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
        _spec("src/Baz.java", SIMPLE_JAVA, "java"),
    ]

    with patch.object(backend, "_xray_cli_path", Path("/nonexistent/xray-cli")):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
        )

    assert len(results) == 3
    for matches, errors, meta in results:
        assert matches == []
        assert len(errors) == 1
        err = errors[0]
        assert err["error_type"] in (
            "BinaryNotFound",
            "SubprocessError",
            "XRayCliError",
        )
        assert meta is None


# ---------------------------------------------------------------------------
# Test 4: Findings grouped by file from JSON output
# ---------------------------------------------------------------------------


def test_findings_grouped_by_file_from_json_output():
    """JSON output findings are correctly split per file_spec."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [
                {
                    "pattern": "allocation-in-try",
                    "file": str(REPO_ROOT / "src/Foo.java"),
                    "line": 3,
                    "snippet": "System.out.println",
                },
            ],
            "files_parsed": 2,
            "files_errored": 0,
            "parse_scan_ms": 5,
            "compile_ms": 235,
            "cached": True,
            "error": None,
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) == 2
    foo_matches, foo_errors, foo_meta = results[0]
    bar_matches, bar_errors, bar_meta = results[1]

    assert len(foo_matches) == 1
    assert foo_errors == []
    assert foo_meta is None

    assert bar_matches == []
    assert bar_errors == []
    assert bar_meta is None


# ---------------------------------------------------------------------------
# Test 5: Match dicts have required fields
# ---------------------------------------------------------------------------


def test_match_dicts_have_required_fields():
    """Each match dict must have line_number, file_path, and language fields."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [
                {
                    "pattern": "allocation-in-try",
                    "file": str(REPO_ROOT / "src/Foo.java"),
                    "line": 3,
                    "snippet": "System.out.println",
                },
            ],
            "files_parsed": 1,
            "files_errored": 0,
            "parse_scan_ms": 5,
            "compile_ms": 100,
            "cached": False,
            "error": None,
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    matches, errors, meta = results[0]
    assert len(matches) == 1
    m = matches[0]

    assert "line_number" in m
    assert "file_path" in m
    assert "language" in m
    assert m["line_number"] == 3
    assert m["file_path"] == "src/Foo.java"
    assert m["language"] == "java"


# ---------------------------------------------------------------------------
# Test 6: JSON error field returns error tuples for all files
# ---------------------------------------------------------------------------


def test_json_error_field_returns_error_tuples_for_all_files():
    """When JSON output has non-null 'error' field, all files get error tuples."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [],
            "files_parsed": 0,
            "files_errored": 0,
            "parse_scan_ms": 0,
            "compile_ms": 0,
            "cached": False,
            "error": "compilation failed: unknown function",
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) == 2
    for matches, errors, meta in results:
        assert matches == []
        assert len(errors) == 1
        err = errors[0]
        assert (
            "compilation failed" in err["error_message"]
            or "unknown function" in err["error_message"]
        )
        assert meta is None


# ---------------------------------------------------------------------------
# Test 7: Files with no findings get ([], [], None)
# ---------------------------------------------------------------------------


def test_files_with_no_findings_get_empty_tuples():
    """Files that have no findings in JSON output get ([], [], None)."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [],
            "files_parsed": 2,
            "files_errored": 0,
            "parse_scan_ms": 3,
            "compile_ms": 100,
            "cached": True,
            "error": None,
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) == 2
    for matches, errors, meta in results:
        assert matches == []
        assert errors == []
        assert meta is None


# ---------------------------------------------------------------------------
# Test 8: line_content derived from source when available
# ---------------------------------------------------------------------------


def test_match_gets_line_content_from_source():
    """line_content is derived from source when finding line is available."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
    ]

    # Line 3 of SIMPLE_JAVA (1-indexed) is the third line
    fake_json = json.dumps(
        {
            "findings": [
                {
                    "pattern": "some-pattern",
                    "file": str(REPO_ROOT / "src/Foo.java"),
                    "line": 3,
                    "snippet": "void bar",
                },
            ],
            "files_parsed": 1,
            "files_errored": 0,
            "parse_scan_ms": 2,
            "compile_ms": 80,
            "cached": True,
            "error": None,
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    matches, errors, meta = results[0]
    assert len(matches) == 1
    m = matches[0]
    expected_line = SIMPLE_JAVA.splitlines()[2]  # line 3 is index 2
    assert m["line_content"] == expected_line


# ---------------------------------------------------------------------------
# Test 9: snippet field preserved in match
# ---------------------------------------------------------------------------


def test_snippet_field_preserved_in_match():
    """snippet from the finding is included in the match dict."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [
                {
                    "pattern": "some-pattern",
                    "file": str(REPO_ROOT / "src/Foo.java"),
                    "line": 3,
                    "snippet": "void bar() special-snippet",
                },
            ],
            "files_parsed": 1,
            "files_errored": 0,
            "parse_scan_ms": 2,
            "compile_ms": 80,
            "cached": True,
            "error": None,
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_json
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    matches, _errors, _meta = results[0]
    assert len(matches) == 1
    assert matches[0]["snippet"] == "void bar() special-snippet"
    assert matches[0]["pattern"] == "some-pattern"


# ---------------------------------------------------------------------------
# Test 10: XRaySearchEngine.__init__ creates rust_backend attribute
# ---------------------------------------------------------------------------


def test_search_engine_init_has_rust_backend_attribute():
    """XRaySearchEngine.__init__ must create self.rust_backend as RustNativeBackend."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.rust_backend import RustNativeBackend
    from code_indexer.xray.search_engine import XRaySearchEngine

    engine = XRaySearchEngine()
    assert hasattr(engine, "rust_backend"), (
        "XRaySearchEngine must have a rust_backend attribute after __init__"
    )
    assert isinstance(engine.rust_backend, RustNativeBackend)
