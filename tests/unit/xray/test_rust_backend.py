"""Tests for RustNativeBackend — Story #1023 / Epic #1019 (pure Rust xray engine).

Covers:
- run_batch() return format: list of (matches, errors, meta) tuples
- Validation errors (forbidden Rust constructs) produce per-file error tuples
- Missing xray-cli binary produces error tuples with clear message (binary path, valid evaluator)
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

# Rust evaluator with forbidden construct — triggers ValidationError.
EVALUATOR_WITH_UNSAFE = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unsafe {}
    Vec::new()
}
"""

# Minimal valid Rust evaluator.
VALID_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"""


# ---------------------------------------------------------------------------
# Test 1: Validation error returns error tuples for all files
# ---------------------------------------------------------------------------


def test_validation_error_returns_error_tuples_for_all_files():
    """When evaluator_code has forbidden Rust constructs, all files get error tuples."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]
    results = backend.run_batch(
        evaluator_code=EVALUATOR_WITH_UNSAFE,
        file_specs=specs,
    )

    assert len(results) == 2
    for matches, errors, meta in results:
        assert matches == []
        assert len(errors) == 1
        err = errors[0]
        assert err["error_type"] == "ValidationError"
        msg = err["error_message"].lower()
        assert "unsafe" in msg or "forbidden" in msg or "validation" in msg
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
# Tests the binary-not-found path: valid evaluator passes validation,
# then the binary check fails because the path does not exist.
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

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (fake_json, "")
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
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

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (fake_json, "")
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
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
    """When JSON output has non-null 'error' field, a single deduplicated error
    tuple is returned (not one per file). Uses subprocess.Popen — the correct
    mock target for _invoke_xray_cli. Error message with /home/ path proves
    sanitization runs on this code path.
    """
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
            "error": "compilation failed: unknown function at /home/user/project/evaluator.rs",
        }
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (fake_json, "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    # Deduplication: cli_error is per-evaluator, not per-file — one entry total.
    assert len(results) == 1, (
        f"Expected 1 deduplicated error result for JSON error field, got {len(results)}"
    )
    matches, errors, meta = results[0]
    assert matches == []
    assert len(errors) == 1
    err = errors[0]
    assert (
        "compilation failed" in err["error_message"]
        or "unknown function" in err["error_message"]
    )
    # Path must be sanitized — /home/ must not appear in the returned message.
    assert "/home/" not in err["error_message"], (
        f"/home/ path must be sanitized from error message. Got: {err['error_message']!r}"
    )
    assert meta is None


# ---------------------------------------------------------------------------
# Test 20: JSON error field paths are sanitized (xray-cache path → evaluator.rs)
# ---------------------------------------------------------------------------


def test_cli_error_json_field_paths_are_sanitized():
    """Compiler errors in the JSON 'error' field must have xray-cache paths replaced
    with 'evaluator.rs' and /home/ paths stripped before returning to callers.
    Verifies sanitization on the cli_error code path (Issue 3).
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
        _spec("src/Bar.java", SIMPLE_JAVA, "java"),
    ]

    raw_error = (
        "error[E0308]: mismatched types"
        " --> /home/user/.cidx-server/xray-cache/abc123def456789.rs:5:10"
    )
    fake_json = json.dumps(
        {
            "findings": [],
            "files_parsed": 0,
            "files_errored": 0,
            "parse_scan_ms": 0,
            "compile_ms": 0,
            "cached": False,
            "error": raw_error,
        }
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (fake_json, "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    # Deduplication: exactly one entry regardless of number of file specs.
    assert len(results) == 1, (
        f"Expected 1 deduplicated result for cli_error path, got {len(results)}"
    )
    matches, errors, meta = results[0]
    assert matches == []
    assert len(errors) == 1
    msg = errors[0]["error_message"]

    # xray-cache path must be replaced with evaluator.rs.
    assert "/home/" not in msg, (
        f"/home/ path must be sanitized from error_message. Got: {msg!r}"
    )
    assert "xray-cache" not in msg, (
        f"xray-cache path must be sanitized from error_message. Got: {msg!r}"
    )
    assert "evaluator.rs" in msg, (
        f"Expected 'evaluator.rs' substitution in error_message. Got: {msg!r}"
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


def test_match_gets_line_content_from_source(tmp_path):
    """line_content is derived from file on disk when finding line is available."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()

    # Create real file so _build_matches can read line_content from disk
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    java_file = src_dir / "Foo.java"
    java_file.write_text(SIMPLE_JAVA)

    specs = [
        _spec("src/Foo.java", SIMPLE_JAVA, "java"),
    ]

    fake_json = json.dumps(
        {
            "findings": [
                {
                    "pattern": "some-pattern",
                    "file": str(tmp_path / "src/Foo.java"),
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

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (fake_json, "")
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(tmp_path),
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

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (fake_json, "")
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
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


# ---------------------------------------------------------------------------
# Sentinel cache — raises if any cache method is called
# ---------------------------------------------------------------------------


class _NoCacheAllowed:
    """Sentinel: raises AssertionError if fetch() or store() are called."""

    def fetch(self, *args, **kwargs):
        raise AssertionError("fetch() must not be called in this test scenario")

    def store(self, *args, **kwargs):
        raise AssertionError("store() must not be called in this test scenario")


# ---------------------------------------------------------------------------
# Test 11: Solo mode — binary missing before cache code runs → no cache calls
# ---------------------------------------------------------------------------


def test_run_batch_solo_no_cache_calls():
    """When _xray_cache is replaced by a sentinel, empty file_specs must not call it.

    run_batch() returns [] immediately for empty file_specs, before any cache code
    runs. If the sentinel _NoCacheAllowed.fetch() or .store() are called,
    AssertionError is raised and the test fails, proving cache is skipped.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    assert backend._xray_cache is None

    # _xray_cache is typed Optional[object]; _NoCacheAllowed is a valid object.
    # Sentinel raises if any cache method is accidentally called.
    backend._xray_cache = _NoCacheAllowed()

    # Empty file_specs → immediate [] return, no cache interaction
    results = backend.run_batch(
        evaluator_code=VALID_EVALUATOR,
        file_specs=[],
    )
    assert results == [], "empty file_specs must return [] without calling cache"


# ---------------------------------------------------------------------------
# Bug #1784: _get_cache_identity_info() delegates to the SAME Rust identity
# formula compile_evaluator() uses, via `xray-cli --print-cache-identity`.
# ---------------------------------------------------------------------------


def test_get_cache_identity_info_parses_well_formed_output():
    """A well-formed 4-line xray-cli output parses into a CacheIdentityInfo."""
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend, CacheIdentityInfo

    backend = RustNativeBackend(xray_cache_backend=None)
    fake_stdout = (
        "identity=" + "a" * 64 + "\n"
        "source_hash=" + "b" * 64 + "\n"
        "abi_version=2\n"
        "rustc_version=rustc 1.91.0\n"
    )
    mock_result = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        info = backend._get_cache_identity_info(VALID_EVALUATOR)

    assert info == CacheIdentityInfo(
        identity="a" * 64,
        source_hash="b" * 64,
        abi_version=2,
        rustc_version="rustc 1.91.0",
    )
    mock_run.assert_called_once()
    _, call_kwargs = mock_run.call_args
    assert call_kwargs.get("input") == VALID_EVALUATOR


def test_get_cache_identity_info_returns_none_on_nonzero_exit():
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    mock_result = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=mock_result):
        info = backend._get_cache_identity_info(VALID_EVALUATOR)

    assert info is None


def test_get_cache_identity_info_returns_none_on_incomplete_output():
    """Missing any of the 4 required fields must return None, never a partial object."""
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    # abi_version line missing
    fake_stdout = (
        "identity="
        + "a" * 64
        + "\nsource_hash="
        + "b" * 64
        + "\nrustc_version=rustc 1.91.0\n"
    )
    mock_result = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
    with patch("subprocess.run", return_value=mock_result):
        info = backend._get_cache_identity_info(VALID_EVALUATOR)

    assert info is None


# ---------------------------------------------------------------------------
# Bug #1784 review MAJOR-2: identity subprocess timeout must never exceed
# the caller's remaining operation deadline.
# ---------------------------------------------------------------------------


def _identity_ok_result() -> "MagicMock":
    from unittest.mock import MagicMock

    fake_stdout = (
        "identity=" + "a" * 64 + "\n"
        "source_hash=" + "b" * 64 + "\n"
        "abi_version=2\n"
        "rustc_version=rustc 1.91.0\n"
    )
    return MagicMock(returncode=0, stdout=fake_stdout, stderr="")


def test_get_cache_identity_info_bounds_timeout_to_remaining_deadline():
    """When deadline_seconds is SMALLER than _CACHE_IDENTITY_TIMEOUT_SECS,
    the subprocess timeout must be clamped to the remaining deadline --
    never block longer than the caller has left (review MAJOR-2)."""
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    with patch("subprocess.run", return_value=_identity_ok_result()) as mock_run:
        backend._get_cache_identity_info(VALID_EVALUATOR, deadline_seconds=0.5)

    _, call_kwargs = mock_run.call_args
    assert call_kwargs.get("timeout") <= 0.5, (
        "must never block longer than the caller's remaining deadline"
    )


def test_get_cache_identity_info_uses_default_timeout_without_deadline():
    """No deadline_seconds supplied (e.g. a direct/isolated call) preserves
    the previous fixed-timeout behaviour."""
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import (
        RustNativeBackend,
        _CACHE_IDENTITY_TIMEOUT_SECS,
    )

    backend = RustNativeBackend(xray_cache_backend=None)
    with patch("subprocess.run", return_value=_identity_ok_result()) as mock_run:
        backend._get_cache_identity_info(VALID_EVALUATOR)

    _, call_kwargs = mock_run.call_args
    assert call_kwargs.get("timeout") == _CACHE_IDENTITY_TIMEOUT_SECS


def test_get_cache_identity_info_deadline_larger_than_default_stays_capped():
    """A generous remaining deadline must not INCREASE the timeout past the
    existing _CACHE_IDENTITY_TIMEOUT_SECS ceiling."""
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import (
        RustNativeBackend,
        _CACHE_IDENTITY_TIMEOUT_SECS,
    )

    backend = RustNativeBackend(xray_cache_backend=None)
    with patch("subprocess.run", return_value=_identity_ok_result()) as mock_run:
        backend._get_cache_identity_info(VALID_EVALUATOR, deadline_seconds=9999.0)

    _, call_kwargs = mock_run.call_args
    assert call_kwargs.get("timeout") == _CACHE_IDENTITY_TIMEOUT_SECS


# ---------------------------------------------------------------------------
# Bug #1784 review MAJOR-2: bounded process-local (instance) cache keyed by
# evaluator source -- identity is a pure function of source+ABI+rustc.
# ---------------------------------------------------------------------------


def test_get_cache_identity_info_instance_cache_avoids_second_subprocess_call():
    """Calling _get_cache_identity_info twice with the SAME rust_code on the
    SAME backend instance must invoke the subprocess only once."""
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    with patch("subprocess.run", return_value=_identity_ok_result()) as mock_run:
        first = backend._get_cache_identity_info(VALID_EVALUATOR)
        second = backend._get_cache_identity_info(VALID_EVALUATOR)

    assert first == second
    mock_run.assert_called_once()


def test_get_cache_identity_info_cache_is_per_evaluator_source():
    """Different evaluator source text must NOT share a cache entry."""
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    other_evaluator = (
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
        '    debug_log("different");\n'
        "    Vec::new()\n"
        "}\n"
    )
    other_stdout = (
        "identity=" + "c" * 64 + "\n"
        "source_hash=" + "d" * 64 + "\n"
        "abi_version=2\n"
        "rustc_version=rustc 1.91.0\n"
    )
    backend = RustNativeBackend(xray_cache_backend=None)
    results = [
        _identity_ok_result(),
        MagicMock(returncode=0, stdout=other_stdout, stderr=""),
    ]
    with patch("subprocess.run", side_effect=results) as mock_run:
        first = backend._get_cache_identity_info(VALID_EVALUATOR)
        second = backend._get_cache_identity_info(other_evaluator)

    assert first != second
    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Bug #1784 review MAJOR-2 (observability): identity helper failures must
# increment the cidx.xray.cache_identity_failures counter.
# ---------------------------------------------------------------------------


def test_get_cache_identity_info_failure_records_telemetry_counter():
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend
    from tests.unit.server.telemetry.otel_test_support import (
        active_application_metrics_singleton,
        find_metric,
    )

    backend = RustNativeBackend(xray_cache_backend=None)
    mock_result = MagicMock(returncode=1, stdout="", stderr="binary missing")
    with active_application_metrics_singleton() as (_metrics, reader):
        with patch("subprocess.run", return_value=mock_result):
            info = backend._get_cache_identity_info(VALID_EVALUATOR)

    assert info is None
    metric = find_metric(reader, "cidx.xray.cache_identity_failures")
    assert metric is not None, (
        "a failure must record the cidx.xray.cache_identity_failures counter"
    )
    dp = list(metric.data.data_points)[0]
    assert dp.value == 1


# ---------------------------------------------------------------------------
# Bug #1784 review MAJOR-2: solo/CLI mode (no cluster cache configured) must
# never pay the identity-subprocess cost at all.
# ---------------------------------------------------------------------------


def test_run_batch_solo_mode_never_invokes_identity_subprocess():
    """With xray_cache_backend=None, run_batch() must never call
    `xray-cli --print-cache-identity` -- only the main compile+eval
    subprocess (Popen) is spawned."""
    import json
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    fake_json = json.dumps(
        {"findings": [], "compile_ms": 100, "cached": False, "error": None}
    )

    def _run_raises(*args, **kwargs):
        raise AssertionError(
            "subprocess.run (identity helper) must never be called in solo mode"
        )

    with patch("subprocess.run", side_effect=_run_raises):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (fake_json, "")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            results = backend.run_batch(
                evaluator_code=VALID_EVALUATOR,
                file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
                repo_path=str(REPO_ROOT),
            )

    assert results  # completed normally, no exception raised above


# ---------------------------------------------------------------------------
# Test 13: pre-fill — .so+.meta exist before subprocess is spawned
# ---------------------------------------------------------------------------


def test_pre_fill_from_cache(tmp_path):
    """When cluster cache has a fresh .so, pre-fill writes .so + .meta before subprocess."""
    import json
    from unittest.mock import MagicMock
    from code_indexer.xray.rust_backend import RustNativeBackend

    fake_so_bytes = b"\x7fELF prefill test"
    mock_cache = MagicMock()
    mock_cache.fetch.return_value = fake_so_bytes
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    identity = "a" * 64
    identity_stdout = f"identity={identity}\nsource_hash={'b' * 64}\nabi_version=2\nrustc_version=rustc 1.91.0\n"
    expected_so = tmp_path / f"{identity}.so"
    expected_meta = tmp_path / f"{identity}.meta"
    popen_saw_so: list = []
    popen_saw_meta: list = []
    fake_json = json.dumps(
        {"findings": [], "compile_ms": 0, "cached": True, "error": None}
    )

    def _popen_side_effect(cmd, **kwargs):
        mock_proc = MagicMock()
        # xray-cli invocation — assert pre-fill files exist at this point
        popen_saw_so.append(expected_so.exists())
        popen_saw_meta.append(expected_meta.exists())
        mock_proc.communicate.return_value = (fake_json, "")
        mock_proc.returncode = 0
        return mock_proc

    mock_identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.run", return_value=mock_identity_result):
            with patch("subprocess.Popen", side_effect=_popen_side_effect):
                backend.run_batch(
                    evaluator_code=VALID_EVALUATOR,
                    file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
                    repo_path=str(REPO_ROOT),
                )

    mock_cache.fetch.assert_called_once_with(identity, "rustc 1.91.0")
    assert popen_saw_so == [True], ".so must exist before subprocess is spawned"
    assert popen_saw_meta == [True], ".meta must exist before subprocess is spawned"
    assert expected_so.read_bytes() == fake_so_bytes


# ---------------------------------------------------------------------------
# Test 14: no post-fill on cache hit (cached=true in JSON output)
# ---------------------------------------------------------------------------


def test_no_post_fill_on_cache_hit():
    """When JSON output has cached=true, cache.store() must NOT be called."""
    import json
    from unittest.mock import MagicMock
    from code_indexer.xray.rust_backend import RustNativeBackend

    mock_cache = MagicMock()
    mock_cache.fetch.return_value = None
    backend = RustNativeBackend(xray_cache_backend=mock_cache)
    fake_json = json.dumps(
        {"findings": [], "compile_ms": 0, "cached": True, "error": None}
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (fake_json, "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
            repo_path=str(REPO_ROOT),
        )

    mock_cache.store.assert_not_called()


# ---------------------------------------------------------------------------
# Test 15: post-fill — fresh compile uploads bytes + compile_ms to cluster cache
# ---------------------------------------------------------------------------


def test_post_fill_after_fresh_compile(tmp_path):
    """When JSON output has cached=false and compile_ms=350, cache.store() is called
    with the .so bytes and compile_ms=350."""
    import json
    from unittest.mock import MagicMock
    from code_indexer.xray.rust_backend import RustNativeBackend

    mock_cache = MagicMock()
    mock_cache.fetch.return_value = None
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    identity = "c" * 64
    identity_stdout = f"identity={identity}\nsource_hash={'d' * 64}\nabi_version=2\nrustc_version=rustc 1.91.0\n"
    fake_so = tmp_path / f"{identity}.so"
    fake_so_bytes = b"\x7fELF postfill test"
    fake_so.write_bytes(fake_so_bytes)

    fake_json = json.dumps(
        {
            "findings": [],
            "compile_ms": 350,
            "cached": False,
            "error": None,
        }
    )
    mock_identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.run", return_value=mock_identity_result):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.communicate.return_value = (fake_json, "")
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc
                backend.run_batch(
                    evaluator_code=VALID_EVALUATOR,
                    file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
                    repo_path=str(REPO_ROOT),
                )

    mock_cache.store.assert_called_once()
    store_args, store_kwargs = mock_cache.store.call_args
    all_args = list(store_args) + list(store_kwargs.values())
    assert identity in all_args, (
        "store() must receive the composite identity as its key"
    )
    assert fake_so_bytes in all_args, "store() must receive the .so bytes"
    assert 350 in all_args, "store() must receive compile_ms=350"


# ---------------------------------------------------------------------------
# Bug #1784 review MAJOR-2: run_batch's own timeout_seconds must flow
# through to EVERY internal identity call as a bounded deadline_seconds --
# pre-fill and post-fill must never independently default to the fixed
# ceiling regardless of how little of the caller's budget remains.
# ---------------------------------------------------------------------------


def test_run_batch_propagates_deadline_to_identity_calls(tmp_path):
    import json
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend, CacheIdentityInfo

    mock_cache = MagicMock()
    mock_cache.fetch.return_value = None
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    real_info = CacheIdentityInfo(
        identity="7" * 64,
        source_hash="8" * 64,
        abi_version=2,
        rustc_version="rustc 1.91.0",
    )
    fake_json = json.dumps(
        {"findings": [], "compile_ms": 200, "cached": False, "error": None}
    )
    seen_deadlines: list = []

    def _fake_get_identity(rust_code, deadline_seconds=None, graph_mode=False):
        seen_deadlines.append(deadline_seconds)
        return real_info

    with patch.object(
        backend, "_get_cache_identity_info", side_effect=_fake_get_identity
    ):
        with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
            (tmp_path / f"{real_info.identity}.so").write_bytes(b"\x7fELF fake")
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.communicate.return_value = (fake_json, "")
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc
                backend.run_batch(
                    evaluator_code=VALID_EVALUATOR,
                    file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
                    repo_path=str(REPO_ROOT),
                    timeout_seconds=5,
                )

    # Pre-fill AND post-fill both call the identity helper.
    assert len(seen_deadlines) >= 2, (
        "both pre-fill and post-fill must call the identity helper"
    )
    for deadline in seen_deadlines:
        assert deadline is not None, (
            "run_batch's timeout_seconds must be propagated as a real deadline"
        )
        assert 0 < deadline <= 5, (
            f"deadline {deadline} must be bounded by run_batch's own "
            "timeout_seconds=5, never left as an independent fixed default"
        )


# ---------------------------------------------------------------------------
# Test 16: XRaySearchEngine wires cluster cache in postgres mode
# ---------------------------------------------------------------------------

# Sentinel DSN — clearly fake, never points at real infrastructure
_FAKE_POSTGRES_DSN = "postgresql://cidx-test-sentinel:unused@test-sentinel/cidxdb"


def test_search_engine_passes_cache_to_rust_backend():
    """In postgres mode, XRaySearchEngine must pass a non-None xray_cache_backend
    to RustNativeBackend.__init__().

    Resets the module-level singleton state before the test so that
    _get_cluster_cache() runs through the full initialization path inside the
    patched context, regardless of what earlier tests may have triggered.

    Patches `code_indexer.xray.search_engine.RustNativeBackend` — the name as it
    is looked up inside XRaySearchEngine.__init__ — so the patch is stable
    regardless of whether the module was already imported earlier in the session.
    """
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    from unittest.mock import MagicMock, patch
    import code_indexer.xray.search_engine as _se

    captured: dict = {}

    class _CapturingBackend:
        """Pretend RustNativeBackend; records xray_cache_backend kwarg.

        Also accepts identity_cache=None (Bug #1784: XRaySearchEngine now
        forwards an optional shared identity cache to RustNativeBackend) so
        the real call signature doesn't raise TypeError against this stand-in.
        """

        def __init__(self, xray_cache_backend=None, identity_cache=None):
            captured["xray_cache_backend"] = xray_cache_backend

    mock_config = MagicMock()
    mock_config.storage_mode = "postgres"
    mock_config.postgres_dsn = _FAKE_POSTGRES_DSN
    mock_config_service = MagicMock()
    mock_config_service.get_config.return_value = mock_config

    mock_pg_backend = MagicMock()

    # Save and reset the module-level singleton so _get_cluster_cache() runs
    # its full initialization path inside the patched context.
    saved_initialized = _se._cluster_cache_initialized
    saved_singleton = _se._cluster_cache_singleton
    _se._cluster_cache_initialized = False
    _se._cluster_cache_singleton = None
    try:
        with (
            # Patch the class on the already-cached module. The local import inside
            # XRaySearchEngine.__init__ (`from code_indexer.xray.rust_backend import
            # RustNativeBackend`) resolves from sys.modules cache and picks up the
            # patched class.  Stable regardless of prior test imports.
            patch(
                "code_indexer.xray.rust_backend.RustNativeBackend",
                new=_CapturingBackend,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=mock_config_service,
            ),
            patch(
                "code_indexer.server.storage.postgres.xray_cache_backend.XrayCachePostgresBackend",
                return_value=mock_pg_backend,
            ),
        ):
            from code_indexer.xray.search_engine import XRaySearchEngine

            XRaySearchEngine()  # construction side-effect populates `captured`
    finally:
        # Restore singleton state so other tests in the session are unaffected.
        _se._cluster_cache_initialized = saved_initialized
        _se._cluster_cache_singleton = saved_singleton

    assert captured.get("xray_cache_backend") is not None, (
        "XRaySearchEngine must pass a non-None xray_cache_backend to "
        "RustNativeBackend in postgres mode"
    )


# ---------------------------------------------------------------------------
# Test 18: error messages must not leak server-internal xray-cache paths
# ---------------------------------------------------------------------------


def test_error_message_sanitizes_xray_cache_paths():
    """Server-internal xray-cache paths in error messages must be replaced with
    'evaluator.rs'. Prevents leaking /home/user/.cidx-server/xray-cache/hash.rs
    to API callers (Issues 4 and 5).
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [_spec("src/Foo.java", SIMPLE_JAVA, "java")]

    raw_stderr = (
        "error[E0425]: cannot find value `x` in this scope\n"
        " --> /home/jsbattig/.cidx-server/xray-cache/59d0fc1a2b3c4d.rs:3:5\n"
        "  |\n"
        "3 |     x + 1\n"
        "  |     ^ not found in this scope"
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", raw_stderr)
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) >= 1
    _matches, errors, _meta = results[0]
    assert len(errors) == 1
    msg = errors[0]["error_message"]
    assert "/home/jsbattig/.cidx-server/xray-cache/" not in msg, (
        f"xray-cache path must be sanitized from error message. Got: {msg!r}"
    )
    assert "evaluator.rs" in msg, (
        f"Expected 'evaluator.rs' substitution in message. Got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 18b: nested build-dir xray-cache paths (Bug #1425) must still redact to
# 'evaluator.rs', not just the weaker generic '<server-path>' fallback.
# ---------------------------------------------------------------------------


def test_error_message_sanitizes_nested_build_dir_xray_cache_paths():
    """Bug #1425's per-invocation build-directory isolation fix (Rust side,
    compiler.rs) moved the compiled .rs source from a flat
    'xray-cache/<hash>.rs' path into an isolated
    'xray-cache/build-<hash>-<random>/<hash>.rs' subdirectory, so that
    concurrent compiles of the SAME evaluator hash never share rustc's -o
    output directory. rustc error messages now reference this nested path
    shape — the sanitizer's xray-cache-specific rule must still redact it to
    'evaluator.rs', not silently fall through to the weaker generic
    '<server-path>' rule.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [_spec("src/Foo.java", SIMPLE_JAVA, "java")]

    raw_stderr = (
        "error[E0425]: cannot find value `x` in this scope\n"
        " --> /home/jsbattig/.cidx-server/xray-cache/"
        "build-59d0fc1a2b3c4d-9k2pQz/59d0fc1a2b3c4d.rs:3:5\n"
        "  |\n"
        "3 |     x + 1\n"
        "  |     ^ not found in this scope"
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", raw_stderr)
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) >= 1
    _matches, errors, _meta = results[0]
    assert len(errors) == 1
    msg = errors[0]["error_message"]
    assert "/home/jsbattig/.cidx-server/xray-cache/" not in msg, (
        f"xray-cache path must be sanitized from error message. Got: {msg!r}"
    )
    assert "evaluator.rs" in msg, (
        f"Expected 'evaluator.rs' substitution for the nested build-dir "
        f"path shape. Got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 19: error messages must not leak other absolute server paths (/home, /root, /tmp)
# ---------------------------------------------------------------------------


def test_error_message_sanitizes_home_paths():
    """Absolute /home/, /root/, /tmp/ paths (non-cache) in error messages must be
    replaced with '<server-path>' (Issues 4 and 5 — general path leakage).

    Uses a non-xray-cache path so this test validates the general sanitizer,
    not the xray-cache-specific rule.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()

    # --- /home/ non-cache path ---
    for raw_path in [
        "/home/jsbattig/project/evaluator_custom.rs",
        "/root/tmp/evaluator_build.rs",
        "/tmp/evaluator_work.rs",
    ]:
        specs = [_spec("src/Foo.java", SIMPLE_JAVA, "java")]
        raw_stderr = f"thread 'main' panicked at {raw_path}:10:5\nnote: backtrace"

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", raw_stderr)
            mock_proc.returncode = 1
            mock_popen.return_value = mock_proc

            results = backend.run_batch(
                evaluator_code=VALID_EVALUATOR,
                file_specs=specs,
                repo_path=str(REPO_ROOT),
            )

        assert len(results) >= 1
        _matches, errors, _meta = results[0]
        assert len(errors) == 1
        msg = errors[0]["error_message"]
        assert raw_path not in msg, (
            f"{raw_path!r} must be sanitized from error message. Got: {msg!r}"
        )
        assert "<server-path>" in msg, (
            f"Expected '<server-path>' in sanitized message for {raw_path!r}. Got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Test 17: compile error returns single error entry, not one per file
# ---------------------------------------------------------------------------


def test_compile_error_returns_single_error_not_per_file():
    """When xray-cli fails to compile (non-zero exit, no JSON), run_batch() must return
    exactly ONE error entry total, not one per file spec (Issue 2 deduplication).

    Uses subprocess.Popen (the correct mock target for _invoke_xray_cli).
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [
        _spec("src/A.java", SIMPLE_JAVA, "java"),
        _spec("src/B.java", SIMPLE_JAVA, "java"),
        _spec("src/C.java", SIMPLE_JAVA, "java"),
        _spec("src/D.java", SIMPLE_JAVA, "java"),
    ]

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (
            "",
            "Evaluator compilation failed: expected identifier",
        )
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    # Must deduplicate: exactly ONE error entry total (not 4 per file)
    assert len(results) == 1, (
        f"Expected 1 deduplicated error result for compile failure, got {len(results)}"
    )
    matches, errors, meta = results[0]
    assert matches == []
    assert len(errors) == 1
    err = errors[0]
    assert err["error_type"] == "XRayCliError"
    assert (
        "compilation" in err["error_message"].lower()
        or "xray-cli" in err["error_message"].lower()
    )
    assert meta is None


# ---------------------------------------------------------------------------
# Test C1-a: _build_matches reads line_content from abs_path (not from spec source)
# ---------------------------------------------------------------------------


def test_build_matches_reads_from_abs_path(tmp_path):
    """_build_matches must read the file at abs_path to populate line_content.

    After Fix C1, _build_matches accepts abs_path: str instead of reading
    source from spec. It reads the file on-demand only when there are findings.
    """
    from code_indexer.xray.rust_backend import _build_matches

    content = "public class Foo {\n    void bar() {}\n    int x = 1;\n}\n"
    src_file = tmp_path / "Foo.java"
    src_file.write_text(content)

    spec = {
        "file_path": "src/Foo.java",
        "lang": "java",
        "match_positions": [],
    }
    findings = [{"pattern": "alloc", "line": 3, "snippet": "int x"}]

    matches = _build_matches(spec, findings, abs_path=str(src_file))

    assert len(matches) == 1
    m = matches[0]
    assert m["line_number"] == 3
    assert m["line_content"] == "    int x = 1;"
    assert m["snippet"] == "int x"
    assert m["language"] == "java"
    assert m["file_path"] == "src/Foo.java"


# ---------------------------------------------------------------------------
# Test C1-b: _build_matches uses empty line_content when file is missing
# ---------------------------------------------------------------------------


def test_build_matches_missing_file_uses_empty_line_content():
    """When the file at abs_path does not exist, _build_matches must not crash.

    line_content must be empty string for all findings; a warning is logged
    but no exception propagates.
    """
    from code_indexer.xray.rust_backend import _build_matches

    spec = {
        "file_path": "src/Ghost.java",
        "lang": "java",
        "match_positions": [],
    }
    findings = [{"pattern": "alloc", "line": 1, "snippet": ""}]

    matches = _build_matches(spec, findings, abs_path="/nonexistent/path/Ghost.java")

    assert len(matches) == 1
    assert matches[0]["line_content"] == ""
    assert matches[0]["line_number"] == 1


# ---------------------------------------------------------------------------
# Test M1: _DEFAULT_EVALUATOR_CODE must be valid Rust (passes validate_rust_evaluator)
# ---------------------------------------------------------------------------


def test_default_evaluator_code_passes_rust_validation():
    """_DEFAULT_EVALUATOR_CODE in xray handler must pass validate_rust_evaluator().

    After Fix M1, the default is Rust (not Python), so the validator must
    accept it without errors.
    """
    from code_indexer.xray.sandbox import validate_rust_evaluator
    from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE

    result = validate_rust_evaluator(_DEFAULT_EVALUATOR_CODE)
    assert result.ok, (
        f"_DEFAULT_EVALUATOR_CODE failed Rust validation: "
        f"{result.reason!r} (construct={result.offending_construct!r})"
    )


# ---------------------------------------------------------------------------
# Test C3: _try_pre_fill writes .so atomically via temp file + rename
# ---------------------------------------------------------------------------


def test_try_pre_fill_atomic_write_via_temp_file(tmp_path):
    """_try_pre_fill must write .so atomically: write to .tmp file first, then rename.

    After Fix C3, no partial .so can exist if a concurrent worker races on the
    same hash. The temp file must not exist after a successful pre-fill, and
    the final .so must contain the correct bytes.
    """
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    fake_so_bytes = b"\x7fELF atomic-write-test"
    mock_cache = MagicMock()
    mock_cache.fetch.return_value = fake_so_bytes
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    identity = "e" * 64
    identity_stdout = f"identity={identity}\nsource_hash={'f' * 64}\nabi_version=2\nrustc_version=rustc 1.91.0\n"
    expected_so = tmp_path / f"{identity}.so"
    pid_tmp = tmp_path / f"{identity}.so.tmp.{__import__('os').getpid()}"
    mock_identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.run", return_value=mock_identity_result):
            backend._try_pre_fill(VALID_EVALUATOR)

    # Final .so must exist with correct bytes.
    assert expected_so.exists(), ".so must exist after successful pre-fill"
    assert expected_so.read_bytes() == fake_so_bytes, ".so must contain cache bytes"
    # Temp file must not remain after atomic rename.
    assert not pid_tmp.exists(), "temp .so.tmp file must be cleaned up after rename"


# ---------------------------------------------------------------------------
# Bug #1784 review MINOR-5: pre-fill temp filename must be collision-safe
# across concurrent threads in the SAME process, not just PID-unique.
# ---------------------------------------------------------------------------


def test_try_pre_fill_uses_mkstemp_not_pid_suffix_bug_1784_minor5(tmp_path):
    """_write_prefilled_artifact must call tempfile.mkstemp() to create its
    temp .so path -- collision-safe across concurrent threads sharing the
    SAME PID, unlike the old f"{name}.tmp.{os.getpid()}" scheme. Spies on
    the real tempfile.mkstemp (wraps=) so this genuinely discriminates
    against an implementation that still uses the PID-suffix scheme but
    happens to leave no leftover file behind -- a state-only assertion
    cannot tell the two implementations apart."""
    import os
    import tempfile as tempfile_module
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    fake_so_bytes = b"\x7fELF mkstemp-test"
    mock_cache = MagicMock()
    mock_cache.fetch.return_value = fake_so_bytes
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    identity = "5" * 64
    identity_stdout = f"identity={identity}\nsource_hash={'6' * 64}\nabi_version=2\nrustc_version=rustc 1.91.0\n"
    expected_so = tmp_path / f"{identity}.so"
    old_style_pid_tmp = tmp_path / f"{identity}.so.tmp.{os.getpid()}"
    mock_identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.run", return_value=mock_identity_result):
            with patch(
                "tempfile.mkstemp", wraps=tempfile_module.mkstemp
            ) as mock_mkstemp:
                backend._try_pre_fill(VALID_EVALUATOR)

    mock_mkstemp.assert_called_once()
    _, mkstemp_kwargs = mock_mkstemp.call_args
    assert mkstemp_kwargs.get("dir") == str(tmp_path), (
        "mkstemp must create the temp file INSIDE the cache dir (same "
        "filesystem as the final .so, required for an atomic rename)"
    )

    assert expected_so.exists(), ".so must exist after successful pre-fill"
    assert expected_so.read_bytes() == fake_so_bytes
    assert not old_style_pid_tmp.exists(), (
        "must not use the old f'{name}.tmp.{pid}' naming scheme"
    )
    leftover = [
        p
        for p in tmp_path.iterdir()
        if p.name not in (f"{identity}.so", f"{identity}.meta")
    ]
    assert leftover == [], f"no temp files must remain after pre-fill: {leftover}"


# ---------------------------------------------------------------------------
# Bug #1784 cluster guard: a stale PG row keyed under the pre-fix raw
# sha256(user_code) must never be served to a node computing the new
# composite identity.
# ---------------------------------------------------------------------------


def test_cluster_old_raw_hash_artifact_not_served_to_new_identity_node(tmp_path):
    """A cluster cache row that exists ONLY under the pre-fix raw hash key
    must be a MISS for a node computing the new composite identity."""
    import hashlib
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    old_raw_hash = hashlib.sha256(VALID_EVALUATOR.encode()).hexdigest()
    new_identity = "9" * 64
    assert new_identity != old_raw_hash

    def _fetch_side_effect(key, rustc_version):
        # Simulates a PG row that exists ONLY under the pre-fix raw-hash key.
        if key == old_raw_hash:
            return b"\x7fELF STALE ARTIFACT FROM OLD RAW-HASH KEY"
        return None

    mock_cache = MagicMock()
    mock_cache.fetch.side_effect = _fetch_side_effect
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    identity_stdout = f"identity={new_identity}\nsource_hash={'1' * 64}\nabi_version=2\nrustc_version=rustc 1.91.0\n"
    mock_identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.run", return_value=mock_identity_result):
            backend._try_pre_fill(VALID_EVALUATOR)

    mock_cache.fetch.assert_called_once_with(new_identity, "rustc 1.91.0")
    assert not (tmp_path / f"{new_identity}.so").exists(), (
        "must not write a local .so from a stale old-key row"
    )
    assert not (tmp_path / f"{old_raw_hash}.so").exists(), (
        "must never touch the old raw-hash path"
    )


# ---------------------------------------------------------------------------
# AC3: _last_debug_messages populated from xray-cli JSON output
# ---------------------------------------------------------------------------


def test_last_debug_messages_populated_from_json_output(tmp_path):
    """After run_batch, _last_debug_messages holds debug_messages from JSON output."""
    import json
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    fake_json = json.dumps(
        {
            "findings": [],
            "files_parsed": 1,
            "files_errored": 0,
            "parse_scan_ms": 5,
            "compile_ms": 100,
            "cached": False,
            "error": None,
            "debug_messages": ["first message", "second message"],
        }
    )

    # Create a real file so the binary-existence check passes.
    fake_file = tmp_path / "test.java"
    fake_file.write_text("class T {}")
    spec = {
        "file_path": str(fake_file),
        "source": "class T {}",
        "lang": "java",
        "match_positions": [],
    }

    with patch.object(backend, "_invoke_xray_cli", return_value=(fake_json, None)):
        with patch.object(
            backend, "_validate_rust_code", return_value=(VALID_EVALUATOR, None)
        ):
            # Patch exists() check on the binary path.
            with patch.object(
                type(backend._xray_cli_path), "exists", return_value=True
            ):
                backend.run_batch(evaluator_code=VALID_EVALUATOR, file_specs=[spec])

    messages = getattr(backend, "_last_debug_messages", None)
    assert messages is not None, (
        "_last_debug_messages attribute must be set after run_batch"
    )
    assert messages == ["first message", "second message"], (
        f"_last_debug_messages must match JSON output: {messages}"
    )


def test_last_debug_messages_empty_when_no_debug_output(tmp_path):
    """When JSON output has no debug_messages key, _last_debug_messages is empty list."""
    import json
    from unittest.mock import patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    # JSON without debug_messages field (old binary compatibility).
    fake_json = json.dumps(
        {
            "findings": [],
            "files_parsed": 1,
            "files_errored": 0,
            "parse_scan_ms": 5,
            "compile_ms": 0,
            "cached": True,
            "error": None,
        }
    )

    fake_file = tmp_path / "test.java"
    fake_file.write_text("class T {}")
    spec = {
        "file_path": str(fake_file),
        "source": "class T {}",
        "lang": "java",
        "match_positions": [],
    }

    with patch.object(backend, "_invoke_xray_cli", return_value=(fake_json, None)):
        with patch.object(
            backend, "_validate_rust_code", return_value=(VALID_EVALUATOR, None)
        ):
            with patch.object(
                type(backend._xray_cli_path), "exists", return_value=True
            ):
                backend.run_batch(evaluator_code=VALID_EVALUATOR, file_specs=[spec])

    messages = getattr(backend, "_last_debug_messages", None)
    assert messages is not None, (
        "_last_debug_messages must be set even when absent from JSON"
    )
    assert messages == [], (
        f"_last_debug_messages must be empty list when not in JSON: {messages}"
    )


# ---------------------------------------------------------------------------
# Bug #1612: large candidate file lists must not overflow argv (E2BIG).
# ---------------------------------------------------------------------------

# Multiplier applied to os.sysconf('SC_ARG_MAX') so the generated candidate
# list comfortably overflows real argv limits regardless of per-platform
# pointer-table/environment overhead not fully captured by ARG_MAX alone.
_ARG_MAX_SAFETY_MARGIN = 3

# Every argv string carries one NUL terminator in the kernel's exec() byte
# accounting, in addition to the string's own characters.
_NUL_TERMINATOR_LENGTH = 1

# Generous timeout for scanning a very large (tens of thousands) candidate
# list of nonexistent files -- rayon-parallel stat/read misses are cheap but
# the sheer count needs headroom on slower CI hosts.
_LARGE_LIST_TIMEOUT_SECONDS = 120

# Timeout for a single-file real xray-cli invocation used by the Bug #1796
# temp-directory-seam tests below -- generous enough for a cold rustc
# compile of the trivial VALID_EVALUATOR on a slower CI host.
_BUG_1796_INVOKE_TIMEOUT_SECONDS = 30


def _require_xray_cli_binary() -> None:
    """Skip this test if the real xray-cli release binary is not built locally.

    This is a genuine component test (no subprocess mocking) -- it needs the
    real compiled binary to prove the fix works at the OS process-exec layer.
    """
    from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT

    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def test_python_identity_matches_real_compiled_artifact_filename(tmp_path):
    """Bug #1784: Python's identity must be byte-identical to the REAL
    compiled artifact's filename -- not just "the same value the CLI prints
    twice", but the actual filename compile_evaluator() used for a real
    compile. No mocking: exercises the real binary end-to-end.
    """
    _require_xray_cli_binary()
    import os
    import subprocess
    from code_indexer.xray.rust_backend import RustNativeBackend, _XRAY_CLI_DEFAULT

    backend = RustNativeBackend(xray_cache_backend=None)
    python_info = backend._get_cache_identity_info(VALID_EVALUATOR)
    assert python_info is not None, "real xray-cli must answer --print-cache-identity"

    eval_file = tmp_path / "eval.rs"
    eval_file.write_text(VALID_EVALUATOR)
    files_list = tmp_path / "files.txt"
    files_list.write_text("")
    cache_data_dir = tmp_path / "cidx-data"

    env = dict(os.environ)
    env["CIDX_DATA_DIR"] = str(cache_data_dir)
    proc = subprocess.run(
        [
            str(_XRAY_CLI_DEFAULT),
            "--dynlib",
            str(eval_file),
            "--files-from",
            str(files_list),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=_LARGE_LIST_TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, f"real xray-cli compile must succeed: {proc.stderr}"

    so_files = list((cache_data_dir / "xray-cache").glob("*.so"))
    assert len(so_files) == 1, f"exactly one compiled .so expected, found: {so_files}"
    real_identity = so_files[0].stem

    assert python_info.identity == real_identity, (
        "Python's identity must be byte-identical to the REAL compiled artifact's filename"
    )


def test_large_candidate_list_does_not_overflow_argv_bug_1612():
    """Bug #1612: xray_search filename-mode search with a large candidate set
    fails with '[Errno 7] Argument list too long' because candidate file
    paths were passed to xray-cli via argv instead of stdin/a temp file.

    Builds a candidate list sized to comfortably exceed os.sysconf('SC_ARG_MAX')
    if encoded as argv, and drives it through the REAL RustNativeBackend ->
    real xray-cli subprocess path (no subprocess mocking). Before the fix,
    this raises an unhandled OSError: [Errno 7] Argument list too long. After
    the fix (file-based candidate handoff), the call completes normally and
    returns one result tuple per file spec.
    """
    _require_xray_cli_binary()
    import os
    from code_indexer.xray.rust_backend import RustNativeBackend

    arg_max = os.sysconf("SC_ARG_MAX")
    path_template = "/nonexistent/xray_bug_1612_" + ("x" * 60) + "/File_{:07d}.java"
    approx_path_len = len(path_template.format(0)) + _NUL_TERMINATOR_LENGTH
    num_paths = (arg_max * _ARG_MAX_SAFETY_MARGIN) // approx_path_len

    backend = RustNativeBackend()
    specs = [_spec(path_template.format(i), "", "java") for i in range(num_paths)]

    # Must not raise OSError -- proves candidate paths are no longer passed via argv.
    results = backend.run_batch(
        evaluator_code=VALID_EVALUATOR,
        file_specs=specs,
        repo_path="/",
        timeout_seconds=_LARGE_LIST_TIMEOUT_SECONDS,
    )

    assert len(results) == num_paths, (
        f"Expected {num_paths} result tuples (one per candidate), got {len(results)}"
    )
    # None of these files exist -- every spec resolves to a clean empty result,
    # never an exception and never a spurious per-file error.
    for matches, errors, meta in results:
        assert matches == []
        assert errors == []
        assert meta is None


# ---------------------------------------------------------------------------
# Bug #1612: any OSError from the xray-cli subprocess invocation (e.g. E2BIG)
# must become a structured tool error, never an unhandled exception.
# ---------------------------------------------------------------------------


def test_subprocess_oserror_becomes_structured_error_not_unhandled_bug_1612():
    """Bug #1612: an OSError raised by subprocess.Popen (e.g. E2BIG /
    'Argument list too long') must be caught inside RustNativeBackend and
    surfaced as a structured error tuple -- never propagate out of run_batch()
    as an unhandled exception (which is what turns into an unhandled MCP
    -32603 internal error instead of a graceful xray_search tool error).
    """
    import errno
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    specs = [_spec("src/Foo.java", SIMPLE_JAVA, "java")]

    with patch(
        "subprocess.Popen",
        side_effect=OSError(errno.E2BIG, "Argument list too long"),
    ):
        results = backend.run_batch(
            evaluator_code=VALID_EVALUATOR,
            file_specs=specs,
            repo_path=str(REPO_ROOT),
        )

    assert len(results) == 1
    matches, errors, meta = results[0]
    assert matches == []
    assert len(errors) == 1
    err = errors[0]
    assert err["error_type"] == "XRayCliError"
    msg = err["error_message"].lower()
    assert "argument list too long" in msg or "e2big" in msg, (
        f"Expected E2BIG/argument-list-too-long detail in error message, got: {msg!r}"
    )
    assert meta is None


# ---------------------------------------------------------------------------
# Bug #1796: temp files must land in an injectable, CIDX-owned directory
# (never the process-wide system temp dir), and cleanup must actually happen
# on both the success and failure paths.
# ---------------------------------------------------------------------------


def _xray_temp_names(directory: Path) -> List[str]:
    """Sorted xray_eval_*/xray_files_* file names currently in `directory`."""
    return sorted(p.name for p in directory.glob("xray_*"))


def _xray_temp_paths_in_system_tmp() -> Any:
    """Set of xray_eval_*/xray_files_* paths currently in the system temp dir."""
    import tempfile as tempfile_module

    system_tmp = Path(tempfile_module.gettempdir())
    return set(system_tmp.glob("xray_eval_*")) | set(system_tmp.glob("xray_files_*"))


def test_invoke_xray_cli_writes_temp_files_into_injected_dir_and_cleans_up(tmp_path):
    """_invoke_xray_cli must accept a `tmp_dir` seam: both per-invocation temp
    artifacts must be created inside that directory while the real xray-cli
    subprocess runs, no NEW artifact may appear in the system temp dir, and
    the directory must be empty again after a successful call.
    """
    _require_xray_cli_binary()
    from code_indexer.xray.rust_backend import RustNativeBackend

    isolated_dir = tmp_path / "xray-tmp-seam"
    backend = RustNativeBackend()
    seen_during_call: List[str] = []

    def _capture(proc: Any) -> None:
        seen_during_call.extend(_xray_temp_names(isolated_dir))

    system_tmp_before = _xray_temp_paths_in_system_tmp()
    stdout, error = backend._invoke_xray_cli(
        VALID_EVALUATOR,
        [str(REPO_ROOT / "README.md")],
        timeout_seconds=_BUG_1796_INVOKE_TIMEOUT_SECONDS,
        on_process_spawned=_capture,
        tmp_dir=isolated_dir,
    )

    assert error is None, f"real xray-cli invocation must succeed: {error}"
    assert stdout, "expected non-empty JSON stdout from a real xray-cli run"
    assert len(seen_during_call) == 2, seen_during_call
    assert any(name.startswith("xray_eval_") for name in seen_during_call)
    assert any(name.startswith("xray_files_") for name in seen_during_call)

    leaked = _xray_temp_paths_in_system_tmp() - system_tmp_before
    assert leaked == set(), f"xray temp files leaked into system temp dir: {leaked}"
    assert list(isolated_dir.iterdir()) == [], (
        "temp files not cleaned up from the injected directory after success"
    )


def test_invoke_xray_cli_cleans_up_injected_dir_when_invocation_raises(tmp_path):
    """Cleanup must run in a `finally` covering the failure path too: when
    the external subprocess.Popen boundary raises a non-OSError exception
    (which therefore propagates instead of becoming a structured error
    tuple), the injected temp directory must still end up empty.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    isolated_dir = tmp_path / "xray-tmp-seam-raise"
    backend = RustNativeBackend()

    with patch("subprocess.Popen", side_effect=RuntimeError("boom-1796")):
        with pytest.raises(RuntimeError, match="boom-1796"):
            backend._invoke_xray_cli(
                VALID_EVALUATOR,
                [str(REPO_ROOT / "README.md")],
                timeout_seconds=_BUG_1796_INVOKE_TIMEOUT_SECONDS,
                on_process_spawned=None,
                tmp_dir=isolated_dir,
            )

    assert isolated_dir.exists(), "injected directory must have been created"
    assert list(isolated_dir.iterdir()) == [], (
        "temp files must be cleaned up even when the invocation raises"
    )


# ---------------------------------------------------------------------------
# Consolidated review finding H12 (Issue #1811/Bug #1812, Codex): "treat
# EVERY non-zero return code as failure, including stderr in the message."
# ---------------------------------------------------------------------------


def test_nonzero_exit_with_nonempty_stdout_is_still_reported_as_failure():
    """A crashed subprocess (returncode != 0) that happened to write
    plausible-looking JSON to stdout before crashing must be reported as a
    failure -- never silently parsed and accepted as a real result."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (
            '{"findings": [], "error": null}',  # plausible but from a crash
            "fatal runtime error: stack overflow",
        )
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        stdout, error = backend._run_xray_cli_process(
            ["xray-cli", "--json"],
            timeout_seconds=30,
            on_process_spawned=None,
            acquire_compile_slot=False,
        )

    assert error is not None, (
        f"a nonzero exit code must always be reported as a failure, even "
        f"with non-empty stdout, got: stdout={stdout!r}, error={error!r}"
    )
    assert stdout == "", "no stdout should be returned on a reported failure"


def test_nonzero_exit_with_nonempty_stdout_error_message_includes_stderr():
    """Per H12's own stated fix text ("including stderr in the message"):
    the failure error message for THIS newly-universal non-zero-exit path
    (nonzero exit + non-empty stdout) must include the real stderr content,
    not just the exit code."""
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (
            '{"findings": []}',
            "fatal runtime error: stack overflow",
        )
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        _stdout, error = backend._run_xray_cli_process(
            ["xray-cli", "--json"],
            timeout_seconds=30,
            on_process_spawned=None,
            acquire_compile_slot=False,
        )

    assert error is not None
    assert "stack overflow" in error, (
        f"expected stderr content in error, got: {error!r}"
    )
