"""RustNativeBackend: Rust-native xray evaluator backend (Story #1023).

Replaces PythonEvaluatorSandbox.run_batch() in the xray pipeline with a
Rust-native scanner backend.

Pipeline:
1. Validate Rust evaluator code via validate_rust_evaluator()
2. Write validated Rust code to a temp file
3. Invoke xray-cli subprocess with --dynlib, --json, --files flags
4. Parse JSON output and group findings by file path
5. Return List[(matches, errors, meta)] — one tuple per file spec

Error contract:
- ValidationError: all files get error tuples with error_type="ValidationError"
- Missing binary: all files get error tuples with error_type="BinaryNotFound"
- JSON error field set: all files get error tuples with the error message
- Subprocess non-zero exit + no parseable JSON: all files get error tuples
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# Sanitize server-internal paths out of error messages so they are never
# exposed to API callers.
# Rule 1: xray-cache paths — replaced with the generic name "evaluator.rs".
# Allows one optional intermediate path segment (e.g. "build-<hash>-<rand>/")
# so Bug #1425's per-invocation isolated build directory
# (xray-cache/build-<hash>-<random>/<hash>.rs — see rust/xray-core/src/compiler.rs)
# is matched alongside the original flat xray-cache/<hash>.rs shape.
_RE_XRAY_CACHE_PATH = re.compile(r"/[^\s\"']+/xray-cache/(?:[^/\s\"']+/)?[a-f0-9]+\.rs")
# Rule 2: other absolute paths under /home/, /root/, /tmp/ — replaced with a
# redaction token so callers know a path was present but cannot reconstruct it.
_RE_SERVER_PATH = re.compile(r"/(?:home|root|tmp)/[^\s\"':,\])\}]+")


def _sanitize_error_message(msg: str) -> str:
    """Replace server-internal file paths in error messages.

    Applied in order:
    1. xray-cache paths (/…/xray-cache/<hexhash>.rs, optionally nested one
       level under a Bug #1425 isolated build-*/ directory) → "evaluator.rs"
    2. Remaining /home/, /root/, /tmp/ paths → "<server-path>"
    """
    msg = _RE_XRAY_CACHE_PATH.sub("evaluator.rs", msg)
    msg = _RE_SERVER_PATH.sub("<server-path>", msg)
    return msg


class XrayCacheBackend(Protocol):
    """Structural interface for cluster-aware evaluator cache backends.

    Implementations must support fetch (cache lookup) and store (cache write).
    All methods must be exception-safe — callers assume they never raise.
    """

    def fetch(self, source_hash: str, rustc_version: str) -> Optional[bytes]:
        """Return cached .so bytes if fresh and rustc_version matches, else None."""
        ...

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        """Upsert compiled .so bytes into the cache."""
        ...


_MAX_PARENT_TRAVERSAL_DEPTH = 10

# Timeout for `rustc --version` subprocess call.
_RUSTC_VERSION_TIMEOUT_SECS = 10

# Maximum stderr bytes to include in the rustc failure log message.
_RUSTC_STDERR_LOG_LIMIT = 200

# Environment variable that overrides the CIDX data directory root.
# When set, the xray cache lives at $CIDX_DATA_DIR/xray-cache instead of
# ~/.cidx-server/xray-cache, matching the server's IPC path alignment (Bug #879).
_XRAY_CACHE_DIR_ENV = "CIDX_DATA_DIR"

# Named path segments — must match Rust's get_cache_dir() in cache.rs exactly.
_CIDX_SERVER_DIR_NAME = ".cidx-server"
_XRAY_CACHE_DIR_NAME = "xray-cache"


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains rust/ dir)."""
    path = Path(__file__).resolve().parent
    for _ in range(_MAX_PARENT_TRAVERSAL_DEPTH):
        if (path / "rust").is_dir():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    logger.warning(
        "RustNativeBackend: could not find rust/ directory within %d levels of %s;"
        " falling back to hardcoded project root",
        _MAX_PARENT_TRAVERSAL_DEPTH,
        Path(__file__),
    )
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _find_project_root()
_XRAY_CLI_DEFAULT = _PROJECT_ROOT / "rust" / "target" / "release" / "xray-cli"

# Type alias for the run_batch return type.
_BatchResult = List[
    Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
]


def _error_tuple(
    file_path: str,
    error_type: str,
    error_message: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build a single ([], [error_dict], None) result tuple."""
    return (
        [],
        [
            {
                "file_path": file_path,
                "line_number": 0,
                "error_type": error_type,
                "error_message": error_message,
            }
        ],
        None,
    )


def _error_all(
    file_specs: List[Dict[str, Any]],
    error_type: str,
    error_message: str,
) -> _BatchResult:
    """Return error tuples for every file spec with the given error."""
    return [
        _error_tuple(spec.get("file_path", ""), error_type, error_message)
        for spec in file_specs
    ]


class RustNativeBackend:
    """Rust-native xray evaluator backend.

    Validates Rust evaluator code, compiles to a dynamic library,
    and runs the Rust xray scanner for parallel AST evaluation.

    Drop-in replacement for PythonEvaluatorSandbox.run_batch() in the
    XRaySearchEngine pipeline.
    """

    def __init__(self, xray_cache_backend: Optional[XrayCacheBackend] = None) -> None:
        """Initialise backend with default xray-cli path and optional cluster cache.

        Args:
            xray_cache_backend: Optional cluster cache backend (XrayCacheBackend).
                Pass None (default) for solo-mode deployments.
        """
        self._xray_cli_path: Path = _XRAY_CLI_DEFAULT
        self._xray_cache: Optional[XrayCacheBackend] = xray_cache_backend
        self._rustc_version: Optional[str] = None
        # Side-channel populated by run_batch() — debug_log() messages from xray-cli JSON.
        # Read by XRaySearchEngine.run() to surface in result dict as debug_output[].
        self._last_debug_messages: List[str] = []

    @staticmethod
    def _sha256_hex(text: str) -> str:
        """SHA-256 hex digest of text — matches Rust's sha256_hex() byte-for-byte."""
        return hashlib.sha256(text.encode()).hexdigest()

    def _get_rustc_version(self) -> str:
        """Get rustc version string, cached for the process lifetime."""
        if self._rustc_version is None:
            try:
                result = subprocess.run(
                    ["rustc", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=_RUSTC_VERSION_TIMEOUT_SECS,
                )
                if result.returncode == 0:
                    self._rustc_version = result.stdout.strip()
                else:
                    logger.warning(
                        "RustNativeBackend: rustc --version exited %d; stderr=%r",
                        result.returncode,
                        result.stderr[:_RUSTC_STDERR_LOG_LIMIT],
                    )
                    self._rustc_version = "unknown"
            except Exception as exc:
                logger.warning("RustNativeBackend: rustc --version failed: %s", exc)
                self._rustc_version = "unknown"
        return self._rustc_version

    @staticmethod
    def _get_cache_dir() -> Path:
        """Return the local xray cache directory.

        CIDX_DATA_DIR is a server-level administrative config (same env var used
        in Bug #879 for IPC path alignment). It is set by the system operator or
        the auto-updater — NOT user-supplied input. Validation rejects non-absolute
        paths (after expanduser) to prevent directory traversal.

        Falls back to ~/.cidx-server/xray-cache when unset or invalid.
        Both branches are normalized via resolve().

        MUST produce the same path as Rust's get_cache_dir() in cache.rs so that
        Python-written pre-fill .so files are visible to the Rust compiler cache.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

        raw = os.environ.get(_XRAY_CACHE_DIR_ENV, "").strip()
        if raw:
            expanded = Path(raw).expanduser()
            if expanded.is_absolute():
                return expanded.resolve() / _XRAY_CACHE_DIR_NAME
            logger.warning(
                "RustNativeBackend: %s=%r is not absolute after expanduser; using default",
                _XRAY_CACHE_DIR_ENV,
                raw,
            )
        return (Path.home() / _CIDX_SERVER_DIR_NAME / _XRAY_CACHE_DIR_NAME).resolve()

    def run_batch(
        self,
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 4,
        timeout_seconds: int = 60,
        on_process_spawned: Optional[Callable] = None,
        repo_path: Optional[str] = None,
    ) -> _BatchResult:
        """Drop-in replacement for PythonEvaluatorSandbox.run_batch().

        Args:
            evaluator_code: Rust evaluator source code containing
                ``fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`` function.
            file_specs: List of dicts with file_path, source, lang,
                match_positions.
            worker_threads: Ignored — Rust uses rayon auto-threading.
            timeout_seconds: Timeout for the xray-cli subprocess.
            on_process_spawned: Optional callback when subprocess starts.
            repo_path: Base path for resolving relative file paths.

        Returns:
            List of (matches, errors, meta) tuples, one per file spec.
        """
        if not file_specs:
            return []

        rust_code, validation_error = self._validate_rust_code(evaluator_code)
        if validation_error is not None:
            return _error_all(file_specs, "ValidationError", validation_error)

        if not self._xray_cli_path.exists():
            msg = (
                f"xray-cli binary not found at {self._xray_cli_path}. "
                "Run: cd rust && cargo build --release"
            )
            logger.error("RustNativeBackend: %s", msg)
            return _error_all(
                file_specs, "BinaryNotFound", _sanitize_error_message(msg)
            )

        base = Path(repo_path) if repo_path else Path.cwd()
        abs_paths = [str(base / spec.get("file_path", "")) for spec in file_specs]

        stdout, invoke_error = self._invoke_xray_cli(
            rust_code, abs_paths, timeout_seconds, on_process_spawned
        )
        if invoke_error is not None:
            # Compilation/invocation errors are per-evaluator, not per-file.
            # Return a single deduplicated error entry instead of one per file.
            return [_error_tuple("", "XRayCliError", invoke_error)]

        output, parse_error = self._parse_json_output(stdout)
        if parse_error is not None:
            # JSON parse failure is a per-evaluator error — deduplicate to one entry.
            return [
                _error_tuple("", "XRayCliError", _sanitize_error_message(parse_error))
            ]

        cli_error = output.get("error")
        if cli_error:
            # Top-level CLI error (e.g. compilation failed) — deduplicate to one entry.
            logger.warning("RustNativeBackend: xray-cli error: %s", cli_error)
            return [
                _error_tuple("", "XRayCliError", _sanitize_error_message(cli_error))
            ]

        # Cluster post-fill: upload a freshly compiled .so to PG so other nodes
        # can skip compilation. Only fires when: cache is configured, the compile
        # was NOT a cache hit (cached=false), and the compile took real time.
        if (
            self._xray_cache is not None
            and output.get("cached") is False
            and output.get("compile_ms", 0) > 0
        ):
            self._try_post_fill(rust_code, output.get("compile_ms", 0))

        # Capture debug_log() messages as a side-channel before returning.
        # XRaySearchEngine.run() reads _last_debug_messages to surface in debug_output[].
        self._last_debug_messages = output.get("debug_messages", [])
        return self._build_results(file_specs, abs_paths, output.get("findings", []))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _try_post_fill(self, rust_code: str, compile_ms: int) -> None:
        """Upload freshly compiled .so to cluster cache after a successful compile.

        Reads the local .so (written by Rust's compile_evaluator) and calls
        cache.store(). All exceptions are caught and logged at WARNING — the .so
        exists locally so functionality is unimpaired even if upload fails.
        """
        try:
            if self._xray_cache is None:
                logger.warning(
                    "XrayCache: post-fill called with no cache backend — skipping"
                )
                return
            source_hash = self._sha256_hex(rust_code)
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{source_hash}.so"
            if not local_so.exists():
                logger.warning(
                    "XrayCache: post-fill skipped — .so not found at %s", local_so
                )
                return
            so_bytes = local_so.read_bytes()
            rustc_ver = self._get_rustc_version()
            self._xray_cache.store(source_hash, rustc_ver, so_bytes, compile_ms)
            logger.info(
                "XrayCache: uploaded %s to cluster cache (%d bytes)",
                source_hash[:12],
                len(so_bytes),
            )
        except Exception as exc:
            logger.warning("XrayCache: post-fill failed: %s", exc)

    def _validate_rust_code(self, evaluator_code: str) -> Tuple[str, Optional[str]]:
        """Validate Rust evaluator code. Returns (rust_code, error_msg).

        The evaluator_code is expected to already be valid Rust.  This method
        checks for required signature and forbidden constructs without any
        transformation — if valid, returns the code unchanged.
        """
        from code_indexer.xray.sandbox import validate_rust_evaluator  # noqa: PLC0415

        result = validate_rust_evaluator(evaluator_code)
        if not result.ok:
            msg = result.reason or "Rust validation failed"
            logger.warning("RustNativeBackend: %s", msg)
            return "", msg
        return evaluator_code, None

    def _try_pre_fill(self, rust_code: str) -> None:
        """Fetch .so from cluster cache and write locally so Rust skips recompile.

        Writes .meta BEFORE .so to avoid partial state: if meta write fails, .so
        is never written. On .so write failure, .meta is deleted to prevent orphan
        metadata. All exceptions are logged at WARNING — a failed pre-fill is
        non-fatal; Rust will simply compile from scratch.

        Early-returns when BOTH .so and .meta already exist for the hash.
        The epoch format '{epoch}s-since-epoch' matches Rust's is_fresh() contract.
        """
        import time  # noqa: PLC0415 — stdlib, lazy import

        assert self._xray_cache is not None, (
            "_try_pre_fill requires non-None _xray_cache"
        )
        try:
            source_hash = self._sha256_hex(rust_code)
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{source_hash}.so"
            meta_path = cache_dir / f"{source_hash}.meta"
            if local_so.exists() and meta_path.exists():
                return  # both artifacts present — no fetch needed
            rustc_ver = self._get_rustc_version()
            blob = self._xray_cache.fetch(source_hash, rustc_ver)
            if blob is None:
                return  # cluster miss — Rust will compile fresh
            cache_dir.mkdir(parents=True, exist_ok=True)
            epoch = int(time.time())
            # Write .meta first so Rust's TTL check sees a valid timestamp.
            # If this fails, .so is never written — no partial cache state.
            meta_path.write_text(
                f"source_hash={source_hash}\n"
                f"rustc_version={rustc_ver}\n"
                f"compiled_at={epoch}s-since-epoch\n"
                f"compile_ms=0\n"
            )
            import os  # noqa: PLC0415 — stdlib, lazy import

            tmp_so = cache_dir / f"{source_hash}.so.tmp.{os.getpid()}"
            try:
                tmp_so.write_bytes(blob)
                tmp_so.rename(local_so)
            except Exception:
                tmp_so.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise
            logger.info("XrayCache: pre-filled %s from cluster cache", source_hash[:12])
        except Exception as exc:
            logger.warning("XrayCache: pre-fill failed: %s", exc)

    def _invoke_xray_cli(
        self,
        rust_code: str,
        abs_paths: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
    ) -> Tuple[str, Optional[str]]:
        """Write temp file, invoke xray-cli, return (stdout, error_msg)."""
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".rs", mode="w", delete=False, prefix="xray_eval_"
        )
        try:
            tmp_file.write(rust_code)
            tmp_file.close()
            tmp_path = tmp_file.name

            # Cluster pre-fill: if PG has a fresh blob, write it locally so Rust
            # sees a local cache hit and skips compilation entirely.
            if self._xray_cache is not None:
                self._try_pre_fill(rust_code)

            cmd = [
                str(self._xray_cli_path),
                "--dynlib",
                tmp_path,
                "--files",
                *abs_paths,
                "--json",
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if on_process_spawned is not None:
                on_process_spawned(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                msg = f"xray-cli timed out after {timeout_seconds}s"
                logger.warning("RustNativeBackend: %s", msg)
                return "", _sanitize_error_message(msg)

            if proc.returncode != 0 and not stdout.strip():
                raw_msg = f"xray-cli exited with code {proc.returncode}: {stderr[:200]}"
                logger.warning("RustNativeBackend: %s", raw_msg)
                return "", _sanitize_error_message(raw_msg)
            return stdout or "", None
        except FileNotFoundError as exc:
            msg = f"xray-cli could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return "", _sanitize_error_message(msg)
        finally:
            Path(tmp_file.name).unlink(missing_ok=True)

    def _parse_json_output(self, stdout: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse JSON from xray-cli stdout. Returns (output_dict, error_msg)."""
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError as exc:
            msg = f"xray-cli produced non-JSON output: {exc}"
            logger.warning("RustNativeBackend: %s", msg)
            return {}, msg

    def _build_results(
        self,
        file_specs: List[Dict[str, Any]],
        abs_paths: List[str],
        findings: List[Dict[str, Any]],
    ) -> _BatchResult:
        """Group findings by file and build result tuples per spec."""
        findings_by_abs: Dict[str, List[Dict[str, Any]]] = {}
        for finding in findings:
            fpath = finding.get("file", "")
            if fpath not in findings_by_abs:
                findings_by_abs[fpath] = []
            findings_by_abs[fpath].append(finding)

        results: _BatchResult = []
        for spec, abs_path in zip(file_specs, abs_paths):
            spec_findings = findings_by_abs.get(abs_path, [])
            if not spec_findings:
                results.append(([], [], None))
                continue
            matches = _build_matches(spec, spec_findings, abs_path=abs_path)
            results.append((matches, [], None))

        return results


def _build_matches(
    spec: Dict[str, Any],
    spec_findings: List[Dict[str, Any]],
    abs_path: str = "",
) -> List[Dict[str, Any]]:
    """Convert xray-cli findings to match dicts for one file spec."""
    lang = spec.get("lang", "")
    rel_path = spec.get("file_path", "")
    source_lines: List[str] = []
    if abs_path:
        try:
            source = Path(abs_path).read_bytes().decode("utf-8", errors="replace")
            source_lines = source.splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to read %s for line_content enrichment: %s", abs_path, exc
            )

    matches: List[Dict[str, Any]] = []
    for finding in spec_findings:
        line_num = finding.get("line", 0)
        idx = line_num - 1
        line_content = source_lines[idx] if 0 <= idx < len(source_lines) else ""
        matches.append(
            {
                "line_number": line_num,
                "file_path": rel_path,
                "language": lang,
                "pattern": finding.get("pattern", ""),
                "snippet": finding.get("snippet", ""),
                "line_content": line_content,
            }
        )
    return matches
