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
