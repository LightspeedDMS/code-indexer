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
# Test 12: _sha256_hex matches Python hashlib SHA-256
# ---------------------------------------------------------------------------


def test_sha256_hex_matches_rust_algorithm():
    """RustNativeBackend._sha256_hex() must produce the same output as hashlib.sha256()."""
    import hashlib
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend(xray_cache_backend=None)
    for text in [VALID_EVALUATOR, "", "hello world"]:
        expected = hashlib.sha256(text.encode()).hexdigest()
        actual = backend._sha256_hex(text)
        assert actual == expected, f"SHA-256 mismatch for {text!r}"
        assert len(actual) == 64


# ---------------------------------------------------------------------------
# Test 13: pre-fill — .so+.meta exist before subprocess is spawned
# ---------------------------------------------------------------------------


def test_pre_fill_from_cache(tmp_path):
    """When cluster cache has a fresh .so, pre-fill writes .so + .meta before subprocess."""
    import hashlib
    import json
    from unittest.mock import MagicMock
    from code_indexer.xray.rust_backend import RustNativeBackend

    fake_so_bytes = b"\x7fELF prefill test"
    mock_cache = MagicMock()
    mock_cache.fetch.return_value = fake_so_bytes
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    source_hash = hashlib.sha256(VALID_EVALUATOR.encode()).hexdigest()
    expected_so = tmp_path / f"{source_hash}.so"
    expected_meta = tmp_path / f"{source_hash}.meta"
    popen_saw_so: list = []
    popen_saw_meta: list = []
    fake_json = json.dumps(
        {"findings": [], "compile_ms": 0, "cached": True, "error": None}
    )

    def _popen_side_effect(cmd, **kwargs):
        mock_proc = MagicMock()
        if "rustc" in str(cmd[0]):
            # rustc --version call from _get_rustc_version()
            mock_proc.communicate.return_value = ("rustc 1.91.0\n", "")
            mock_proc.returncode = 0
            return mock_proc
        # xray-cli invocation — assert pre-fill files exist at this point
        popen_saw_so.append(expected_so.exists())
        popen_saw_meta.append(expected_meta.exists())
        mock_proc.communicate.return_value = (fake_json, "")
        mock_proc.returncode = 0
        return mock_proc

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            backend.run_batch(
                evaluator_code=VALID_EVALUATOR,
                file_specs=[_spec("src/Foo.java", SIMPLE_JAVA, "java")],
                repo_path=str(REPO_ROOT),
            )

    mock_cache.fetch.assert_called_once()
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
    import hashlib
    import json
    from unittest.mock import MagicMock
    from code_indexer.xray.rust_backend import RustNativeBackend

    mock_cache = MagicMock()
    mock_cache.fetch.return_value = None
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    source_hash = hashlib.sha256(VALID_EVALUATOR.encode()).hexdigest()
    fake_so = tmp_path / f"{source_hash}.so"
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

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
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
    assert fake_so_bytes in all_args, "store() must receive the .so bytes"
    assert 350 in all_args, "store() must receive compile_ms=350"


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
        """Pretend RustNativeBackend; records xray_cache_backend kwarg."""

        def __init__(self, xray_cache_backend=None):
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
    import hashlib
    from unittest.mock import MagicMock, patch
    from code_indexer.xray.rust_backend import RustNativeBackend

    fake_so_bytes = b"\x7fELF atomic-write-test"
    mock_cache = MagicMock()
    mock_cache.fetch.return_value = fake_so_bytes
    backend = RustNativeBackend(xray_cache_backend=mock_cache)

    source_hash = hashlib.sha256(VALID_EVALUATOR.encode()).hexdigest()
    expected_so = tmp_path / f"{source_hash}.so"
    pid_tmp = tmp_path / f"{source_hash}.so.tmp.{__import__('os').getpid()}"

    with patch.object(backend, "_get_cache_dir", return_value=tmp_path):
        with patch.object(backend, "_get_rustc_version", return_value="rustc 1.91.0"):
            backend._try_pre_fill(VALID_EVALUATOR)

    # Final .so must exist with correct bytes.
    assert expected_so.exists(), ".so must exist after successful pre-fill"
    assert expected_so.read_bytes() == fake_so_bytes, ".so must contain cache bytes"
    # Temp file must not remain after atomic rename.
    assert not pid_tmp.exists(), "temp .so.tmp file must be cleaned up after rename"
