"""RustNativeBackend: Rust-native xray evaluator backend (Story #1023).

Replaces PythonEvaluatorSandbox.run_batch() in the xray pipeline with a
Rust-native scanner backend.

Pipeline:
1. Validate Rust evaluator code via validate_rust_evaluator()
2. Write validated Rust code to a temp file
3. Invoke xray-cli subprocess with --dynlib, --json, --files-from flags
4. Parse JSON output and group findings by file path
5. Return List[(matches, errors, meta)] — one tuple per file spec

Error contract:
- ValidationError: all files get error tuples with error_type="ValidationError"
- Missing binary: all files get error tuples with error_type="BinaryNotFound"
- JSON error field set: all files get error tuples with the error message
- Subprocess non-zero exit + no parseable JSON: all files get error tuples
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Protocol, Tuple

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


def _record_identity_failure_metric(reason: str) -> None:
    """Record a cidx.xray.cache_identity_failures OTEL counter event
    (Bug #1784 review: a WARNING log alone is insufficient observability
    at fleet scale when a node's xray-cli binary is missing/broken and
    EVERY compile silently loses the cluster cache).

    Follows the same peek_telemetry_manager() + is_active gating pattern
    as code_indexer.services.embedding_metrics_telemetry -- lazily imports
    the server-side telemetry package (this module is used by the CLI/
    solo-mode xray path too, which must never eagerly import server-layer
    modules) and never raises: telemetry failures must never break the
    xray evaluator pipeline.

    Args:
        reason: Short failure classification, e.g. "nonzero_exit",
            "incomplete_output", "exception", or "deadline_exhausted".
    """
    try:
        from code_indexer.server.telemetry.manager import (  # noqa: PLC0415
            peek_telemetry_manager,
        )
        from code_indexer.server.telemetry.metrics_instrumentation import (  # noqa: PLC0415
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_xray_cache_identity_failure(reason=reason)
    except Exception as exc:  # never break the xray evaluator pipeline
        logger.debug("Failed to record xray cache identity failure metric: %s", exc)


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


class CacheIdentityInfo(NamedTuple):
    """Bug #1784: the ONE shared cache identity plus its component fields.

    Obtained EXCLUSIVELY from `xray-cli --print-cache-identity`
    (RustNativeBackend._get_cache_identity_info) -- Python never
    independently computes any of these values, so it structurally cannot
    drift from what Rust's compile_evaluator() uses as its own cache key.
    """

    identity: str
    source_hash: str
    abi_version: int
    rustc_version: str


_MAX_PARENT_TRAVERSAL_DEPTH = 10

# Timeout for the `xray-cli --print-cache-identity` subprocess call (Bug
# #1784) -- no compilation happens on this path, just hashing, so this stays
# generous without risking a slow build hanging the caller. This is a CEILING:
# review MAJOR-2 requires the actual timeout passed to the subprocess to be
# clamped to whatever remains of the caller's operation deadline, so it is
# never exceeded but the identity probe also never blocks longer than the
# caller has left.
_CACHE_IDENTITY_TIMEOUT_SECS = 10

# Default maximum number of evaluator-source -> CacheIdentityInfo entries
# kept in a SharedIdentityCache (Bug #1784 review MAJOR-2). Identity is a
# pure function of (evaluator source, ABI version, rustc version); the
# latter two are fixed for the lifetime of one running xray-cli binary (same
# host toolchain), so caching by source text eliminates redundant
# `xray-cli --print-cache-identity` subprocess spawns across repeated calls
# with the SAME evaluator. This is the default size for a RustNativeBackend's
# own PRIVATE cache (single-repo xray_search path); a batch orchestrator
# (xray_search_batch) constructs and shares a larger instance explicitly so
# the identity for every unique evaluator source in the batch is computed
# exactly once, regardless of how many per-cell RustNativeBackend instances
# are created.
_IDENTITY_CACHE_MAX_ENTRIES = 32

# Maximum stderr bytes to include in a --print-cache-identity failure log message.
_RUSTC_STDERR_LOG_LIMIT = 200

# Maximum stderr bytes to include in an xray-cli non-zero-exit error message.
_XRAY_CLI_STDERR_ERROR_LIMIT = 200

# Environment variable that overrides the CIDX data directory root.
# When set, the xray cache lives at $CIDX_DATA_DIR/xray-cache instead of
# ~/.cidx-server/xray-cache, matching the server's IPC path alignment (Bug #879).
_XRAY_CACHE_DIR_ENV = "CIDX_DATA_DIR"

# Named path segments — must match Rust's get_cache_dir() in cache.rs exactly.
_CIDX_SERVER_DIR_NAME = ".cidx-server"
_XRAY_CACHE_DIR_NAME = "xray-cache"

# Bug #1796: directory name for X-Ray's per-invocation temp files (evaluator
# .rs, candidate-file-list .txt), kept distinct from the compile cache above.
_XRAY_TMP_DIR_NAME = "xray-tmp"


class SharedIdentityCache:
    """Bounded, thread-safe LRU mapping evaluator source text to its
    composite cache identity (Bug #1784 review blocker: MAJOR-2 was only
    PARTIALLY satisfied because `xray_search_batch` built a fresh, empty,
    per-instance identity cache for every cell instead of sharing one across
    the whole batch operation).

    An instance of this class is meant to OUTLIVE any single
    RustNativeBackend: a batch orchestrator constructs ONE
    SharedIdentityCache and passes it into every per-cell
    RustNativeBackend/XRaySearchEngine it creates, so the identity for a
    given evaluator source (constant across every repo for a given scan) is
    computed via `xray-cli --print-cache-identity` exactly once for the
    whole batch, not once per cell.

    Node-local pure-function memoization ONLY: identity = f(assembled
    source, ABI, rustc version). A cache miss simply recomputes via a fresh
    subprocess call -- this is NOT cross-request/cluster state (see the
    project's cluster-aware-state rule) and must never be promoted to a
    permanent module-level singleton shared across unrelated requests; its
    lifetime is scoped to one batch job.

    Epoch-guarded: the first successful `put()` establishes an
    (abi_version, rustc_version) epoch from the computed CacheIdentityInfo.
    A LATER `put()` reporting a DIFFERENT epoch (e.g. the auto-updater
    replaces the xray-cli binary mid-batch -- xray_search_batch jobs can run
    up to 7200s) invalidates every existing entry before storing the new
    one, so a stale identity computed under a since-replaced toolchain is
    never served. This makes a hit effectively keyed on the full
    (source, abi_version, rustc_version) identity, not source alone.
    """

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, CacheIdentityInfo]" = OrderedDict()
        self._epoch: Optional[Tuple[int, str]] = None

    def get(self, rust_code: str) -> Optional["CacheIdentityInfo"]:
        """Return the cached identity for `rust_code`, or None on a miss."""
        with self._lock:
            info = self._entries.get(rust_code)
            if info is not None:
                self._entries.move_to_end(rust_code)
            return info

    def put(self, rust_code: str, info: "CacheIdentityInfo") -> None:
        """Insert/refresh an entry, evicting LRU entries past max_entries.

        Clears every existing entry first if `info`'s (abi_version,
        rustc_version) differs from the epoch established by a prior put()
        -- see class docstring.
        """
        with self._lock:
            epoch = (info.abi_version, info.rustc_version)
            if self._epoch is not None and self._epoch != epoch:
                logger.warning(
                    "XrayCache: toolchain changed mid-batch (abi/rustc "
                    "epoch %r -> %r) -- resetting shared identity cache",
                    self._epoch,
                    epoch,
                )
                self._entries.clear()
            self._epoch = epoch
            self._entries[rust_code] = info
            self._entries.move_to_end(rust_code)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


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


def _get_xray_tmp_dir() -> Path:
    """Return the CIDX-owned directory for X-Ray's per-invocation temp files
    (Bug #1796): the evaluator .rs and candidate-file-list .txt files written
    by RustNativeBackend._invoke_xray_cli().

    Honors CIDX_DATA_DIR (the same env var RustNativeBackend._get_cache_dir()
    resolves for the compile cache) when set to an absolute path --
    $CIDX_DATA_DIR/xray-tmp -- for consistency with the rest of the
    codebase's CIDX_DATA_DIR resolution. Otherwise defaults to ~/.tmp/xray-tmp
    per this project's "tmp files: ~/.tmp, never /tmp" convention.

    NEVER falls back to the process-wide system temp directory
    (tempfile.gettempdir()) -- a directory that cannot be created or written
    to must fail loudly (Messi Rules 2/13), not silently redirect there.

    Does not create the directory itself -- _write_temp_file() does, via
    mkdir(parents=True, exist_ok=True), which is safe under concurrent
    callers (rayon threads / multiple server workers racing to create it).
    """
    import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

    raw = os.environ.get(_XRAY_CACHE_DIR_ENV, "").strip()
    if raw:
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            return expanded.resolve() / _XRAY_TMP_DIR_NAME
        logger.warning(
            "RustNativeBackend: %s=%r is not absolute after expanduser; using default",
            _XRAY_CACHE_DIR_ENV,
            raw,
        )
    return (Path.home() / ".tmp" / _XRAY_TMP_DIR_NAME).resolve()


def _write_temp_file(content: str, suffix: str, prefix: str, directory: Path) -> Any:
    """Write content to a closed NamedTemporaryFile(delete=False) inside
    `directory`, return the handle.

    Creates `directory` (and any missing parents) first via
    mkdir(parents=True, exist_ok=True) -- concurrency-safe: exist_ok=True
    means a race between multiple callers creating the same directory never
    raises. A genuine failure to create/write into `directory` (e.g.
    permission denied) propagates as-is -- this never silently redirects to
    the process-wide system temp directory (Bug #1796; Messi Rules 2/13).

    The caller is responsible for unlinking the file (via the returned
    handle's .name attribute) once done with it. If write() raises, both
    cleanup steps (unlink the on-disk file, close the file descriptor) are
    attempted independently; any cleanup-step exception is swallowed so the
    original write() exception is always what propagates -- callers never
    see a leaked temp file/descriptor, and never see a masked root cause.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", delete=False, prefix=prefix, dir=str(directory)
    )
    try:
        tmp.write(content)
    except Exception:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tmp.close()
        except Exception:
            pass
        raise
    tmp.close()
    return tmp


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

    def __init__(
        self,
        xray_cache_backend: Optional[XrayCacheBackend] = None,
        identity_cache: Optional[SharedIdentityCache] = None,
    ) -> None:
        """Initialise backend with default xray-cli path and optional cluster cache.

        Args:
            xray_cache_backend: Optional cluster cache backend (XrayCacheBackend).
                Pass None (default) for solo-mode deployments.
            identity_cache: Optional externally-owned SharedIdentityCache
                (Bug #1784 review MAJOR-2) that OUTLIVES this instance --
                e.g. one shared across every cell of an xray_search_batch
                operation, so the identity subprocess is invoked once per
                unique evaluator source across the WHOLE batch instead of
                once per cell. None (default) creates a private cache sized
                to _IDENTITY_CACHE_MAX_ENTRIES -- unchanged single-repo
                xray_search behaviour.
        """
        self._xray_cli_path: Path = _XRAY_CLI_DEFAULT
        self._xray_cache: Optional[XrayCacheBackend] = xray_cache_backend
        # Side-channel populated by run_batch() — debug_log() messages from xray-cli JSON.
        # Read by XRaySearchEngine.run() to surface in result dict as debug_output[].
        self._last_debug_messages: List[str] = []
        self._identity_cache: SharedIdentityCache = (
            identity_cache
            or SharedIdentityCache(max_entries=_IDENTITY_CACHE_MAX_ENTRIES)
        )

    def _get_cache_identity_info(
        self, rust_code: str, deadline_seconds: Optional[float] = None
    ) -> Optional[CacheIdentityInfo]:
        """Bug #1784: get the composite cache identity for `rust_code`.

        Serves from the bounded identity cache when available (identity is
        a pure function of source+ABI+rustc) -- private to this instance by
        default, or a batch-shared SharedIdentityCache when one was passed
        to __init__. Otherwise shells out to `xray-cli --print-cache-identity`
        -- the SOLE implementation of the identity formula -- with the
        subprocess timeout clamped to `deadline_seconds` (review MAJOR-2:
        never block longer than the caller's remaining operation budget; an
        already-expired deadline skips the subprocess entirely). Returns
        None (never raises) on any failure and records a
        cidx.xray.cache_identity_failures metric.
        """
        cached = self._identity_cache.get(rust_code)
        if cached is not None:
            return cached
        if deadline_seconds is not None and deadline_seconds <= 0:
            logger.warning(
                "XrayCache: skipping --print-cache-identity -- operation "
                "deadline already exhausted"
            )
            _record_identity_failure_metric("deadline_exhausted")
            return None
        info = self._fetch_identity_via_subprocess(rust_code, deadline_seconds)
        if info is not None:
            self._identity_cache.put(rust_code, info)
        return info

    def _fetch_identity_via_subprocess(
        self, rust_code: str, deadline_seconds: Optional[float]
    ) -> Optional[CacheIdentityInfo]:
        """Run `xray-cli --print-cache-identity`, parse, classify failures.

        Never raises. Records a cidx.xray.cache_identity_failures metric on
        every failure path (nonzero exit, incomplete output, exception).
        """
        try:
            timeout = self._resolve_identity_timeout(deadline_seconds)
            result = self._run_cache_identity_subprocess(rust_code, timeout)
            if result.returncode != 0:
                logger.warning(
                    "XrayCache: --print-cache-identity exited %d; stderr=%r",
                    result.returncode,
                    result.stderr[:_RUSTC_STDERR_LOG_LIMIT],
                )
                _record_identity_failure_metric("nonzero_exit")
                return None
            info = self._parse_cache_identity_output(result.stdout)
            if info is None:
                _record_identity_failure_metric("incomplete_output")
            return info
        except Exception as exc:
            logger.warning("XrayCache: cache identity computation failed: %s", exc)
            _record_identity_failure_metric("exception")
            return None

    @staticmethod
    def _resolve_identity_timeout(deadline_seconds: Optional[float]) -> float:
        """Clamp the identity subprocess timeout to the caller's remaining
        deadline (review MAJOR-2). No floor above `deadline_seconds` -- a
        floor would itself violate "never block longer than the caller has
        left". Callers with an already-expired deadline never reach this
        (see _get_cache_identity_info's early-return)."""
        if deadline_seconds is None:
            return _CACHE_IDENTITY_TIMEOUT_SECS
        return min(_CACHE_IDENTITY_TIMEOUT_SECS, deadline_seconds)

    def _run_cache_identity_subprocess(
        self, rust_code: str, timeout_seconds: float = _CACHE_IDENTITY_TIMEOUT_SECS
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [str(self._xray_cli_path), "--print-cache-identity"],
            input=rust_code,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

    @staticmethod
    def _parse_cache_identity_output(stdout: str) -> Optional[CacheIdentityInfo]:
        fields: Dict[str, str] = {}
        for line in stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                fields[key] = value
        required = ("identity", "source_hash", "abi_version", "rustc_version")
        if not all(fields.get(key) for key in required):
            logger.warning(
                "XrayCache: --print-cache-identity produced incomplete output: %r",
                stdout,
            )
            return None
        return CacheIdentityInfo(
            identity=fields["identity"],
            source_hash=fields["source_hash"],
            abi_version=int(fields["abi_version"]),
            rustc_version=fields["rustc_version"],
        )

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

        # Bug #1784 review MAJOR-2: track ONE operation deadline across this
        # whole call so every internal identity-helper invocation (pre-fill
        # AND post-fill) is bounded by what actually remains of the
        # caller's own timeout_seconds, instead of each independently
        # defaulting to the fixed _CACHE_IDENTITY_TIMEOUT_SECS ceiling.
        operation_deadline = time.monotonic() + timeout_seconds

        stdout, invoke_error = self._invoke_xray_cli(
            rust_code,
            abs_paths,
            timeout_seconds,
            on_process_spawned,
            deadline_seconds=self._remaining_seconds(operation_deadline),
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
            self._try_post_fill(
                rust_code,
                output.get("compile_ms", 0),
                deadline_seconds=self._remaining_seconds(operation_deadline),
            )

        # Capture debug_log() messages as a side-channel before returning.
        # XRaySearchEngine.run() reads _last_debug_messages to surface in debug_output[].
        self._last_debug_messages = output.get("debug_messages", [])
        return self._build_results(file_specs, abs_paths, output.get("findings", []))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _remaining_seconds(deadline: float) -> float:
        """Seconds left until `deadline` (a time.monotonic()-based instant).

        Bug #1784 review MAJOR-2: shared by every call site that needs to
        pass the CALLER's remaining operation budget into an identity-helper
        call. Never floors at zero -- a negative/zero value is meaningful
        (deadline already exhausted) and _get_cache_identity_info handles it
        by skipping the subprocess entirely rather than spawning one.
        """
        return deadline - time.monotonic()

    def _try_post_fill(
        self,
        rust_code: str,
        compile_ms: int,
        deadline_seconds: Optional[float] = None,
    ) -> None:
        """Upload freshly compiled .so to cluster cache after a successful compile.

        Reads the local .so (written by Rust's compile_evaluator, named by
        the SAME composite identity — Bug #1784) and calls cache.store(). All
        exceptions are caught and logged at WARNING — the .so exists locally
        so functionality is unimpaired even if upload fails.

        Args:
            deadline_seconds: Remaining operation budget (review MAJOR-2),
                forwarded to the identity helper's subprocess timeout. None
                preserves the previous fixed-timeout behaviour for direct/
                isolated callers.
        """
        try:
            if self._xray_cache is None:
                logger.warning(
                    "XrayCache: post-fill called with no cache backend — skipping"
                )
                return
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds
            )
            if info is None:
                logger.warning(
                    "XrayCache: post-fill skipped — could not compute cache identity"
                )
                return
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{info.identity}.so"
            if not local_so.exists():
                logger.warning(
                    "XrayCache: post-fill skipped — .so not found at %s", local_so
                )
                return
            so_bytes = local_so.read_bytes()
            self._xray_cache.store(
                info.identity, info.rustc_version, so_bytes, compile_ms
            )
            logger.info(
                "XrayCache: uploaded %s to cluster cache (%d bytes)",
                info.identity[:12],
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

    def _try_pre_fill(
        self, rust_code: str, deadline_seconds: Optional[float] = None
    ) -> None:
        """Fetch .so from cluster cache and write locally so Rust skips recompile.

        Bug #1784: keyed on the composite cache identity (not a raw
        sha256(user_code)), so a pre-filled artifact is written under
        EXACTLY the filename Rust's compile_evaluator() will look up. All
        exceptions are logged at WARNING — a failed pre-fill is non-fatal;
        Rust will simply compile from scratch.

        Args:
            deadline_seconds: Remaining operation budget (review MAJOR-2),
                forwarded to the identity helper's subprocess timeout. None
                preserves the previous fixed-timeout behaviour for direct/
                isolated callers.

        Early-returns when BOTH .so and .meta already exist for the identity.
        """
        assert self._xray_cache is not None, (
            "_try_pre_fill requires non-None _xray_cache"
        )
        try:
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds
            )
            if info is None:
                return  # cannot determine identity -- Rust will compile fresh
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{info.identity}.so"
            meta_path = cache_dir / f"{info.identity}.meta"
            if local_so.exists() and meta_path.exists():
                return  # both artifacts present — no fetch needed
            blob = self._xray_cache.fetch(info.identity, info.rustc_version)
            if blob is None:
                return  # cluster miss — Rust will compile fresh
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._write_prefilled_artifact(info, meta_path, local_so, blob)
            logger.info(
                "XrayCache: pre-filled %s from cluster cache", info.identity[:12]
            )
        except Exception as exc:
            logger.warning("XrayCache: pre-fill failed: %s", exc)

    @staticmethod
    def _write_prefilled_artifact(
        info: CacheIdentityInfo, meta_path: Path, local_so: Path, blob: bytes
    ) -> None:
        """Write .meta then .so atomically for a cluster-cache pre-fill hit.

        Writes .meta BEFORE .so to avoid partial state: if meta write fails,
        .so is never written. On .so write failure, .meta is deleted to
        prevent orphan metadata. The epoch format '{epoch}s-since-epoch'
        matches Rust's is_fresh() contract; abi_version matches Rust's
        CacheMetadata field (Bug #1784) so the freshness check on the Rust
        side accepts this pre-filled artifact as valid.

        Bug #1784 review MINOR-5: temp .so path comes from tempfile.mkstemp()
        (collision-safe across concurrent threads sharing one PID), not the
        old f"{name}.tmp.{os.getpid()}" scheme. Cleanup on failure is
        delegated to _cleanup_prefill_failure so none of its steps can mask
        the original exception, which always re-raises via the bare `raise`.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import
        import time  # noqa: PLC0415 — stdlib, lazy import

        epoch = int(time.time())
        meta_path.write_text(
            f"source_hash={info.source_hash}\n"
            f"rustc_version={info.rustc_version}\n"
            f"abi_version={info.abi_version}\n"
            f"compiled_at={epoch}s-since-epoch\n"
            f"compile_ms=0\n"
        )
        fd: Optional[int] = None
        tmp_so: Optional[Path] = None
        try:
            fd, tmp_so_name = tempfile.mkstemp(
                dir=str(local_so.parent), prefix=f"{local_so.name}.tmp."
            )
            tmp_so = Path(tmp_so_name)
            with os.fdopen(fd, "wb") as f:
                fd = None  # ownership now with `f`; its __exit__ closes it
                f.write(blob)
            tmp_so.rename(local_so)
        except Exception:
            RustNativeBackend._cleanup_prefill_failure(fd, tmp_so, meta_path)
            raise

    @staticmethod
    def _cleanup_prefill_failure(
        fd: Optional[int], tmp_so: Optional[Path], meta_path: Path
    ) -> None:
        """Best-effort cleanup after a _write_prefilled_artifact failure.

        Every step is independently wrapped so a cleanup-step failure can
        never mask the caller's original, re-raised exception (Bug #1784
        review).
        """
        import os  # noqa: PLC0415 — stdlib, lazy import

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_so is not None:
            try:
                tmp_so.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _run_xray_cli_process(
        self,
        cmd: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
    ) -> Tuple[str, Optional[str]]:
        """Spawn xray-cli, wait for completion, return (stdout, error_msg)."""
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
            raw_msg = (
                f"xray-cli exited with code {proc.returncode}: "
                f"{stderr[:_XRAY_CLI_STDERR_ERROR_LIMIT]}"
            )
            logger.warning("RustNativeBackend: %s", raw_msg)
            return "", _sanitize_error_message(raw_msg)
        return stdout or "", None

    def _invoke_xray_cli(
        self,
        rust_code: str,
        abs_paths: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
        deadline_seconds: Optional[float] = None,
        tmp_dir: Optional[Path] = None,
    ) -> Tuple[str, Optional[str]]:
        """Write temp files (evaluator + candidate list), invoke xray-cli.

        Returns (stdout, error_msg).

        Bug #1612: the candidate file list is written to a temp file and
        handed to xray-cli via --files-from instead of individual --files
        argv elements. Passing tens of thousands of candidate paths as argv
        overflows ARG_MAX (OSError: [Errno 7] Argument list too long) once a
        repo's candidate set is large enough -- the temp-file handoff has no
        such ceiling.

        Args:
            deadline_seconds: Remaining operation budget (Bug #1784 review
                MAJOR-2), forwarded to the pre-fill identity call so it
                never blocks longer than the caller has left.
            tmp_dir: Directory the two per-invocation temp files (evaluator
                .rs, candidate-list .txt) are written into (Bug #1796).
                Defaults to _get_xray_tmp_dir() -- a CIDX-owned directory
                under CIDX_DATA_DIR or ~/.tmp -- NEVER the process-wide
                system temp directory. Exposed as an injectable seam so
                callers/tests can point it at an isolated directory and
                assert both where artifacts land and that cleanup happens.
        """
        resolved_tmp_dir = tmp_dir if tmp_dir is not None else _get_xray_tmp_dir()
        tmp_file: Optional[Any] = None
        files_tmp: Optional[Any] = None
        try:
            tmp_file = _write_temp_file(
                rust_code,
                suffix=".rs",
                prefix="xray_eval_",
                directory=resolved_tmp_dir,
            )
            files_tmp = _write_temp_file(
                "\n".join(abs_paths),
                suffix=".txt",
                prefix="xray_files_",
                directory=resolved_tmp_dir,
            )

            # Cluster pre-fill: if PG has a fresh blob, write it locally so Rust
            # sees a local cache hit and skips compilation entirely.
            if self._xray_cache is not None:
                self._try_pre_fill(rust_code, deadline_seconds=deadline_seconds)

            cmd = [
                str(self._xray_cli_path),
                "--dynlib",
                tmp_file.name,
                "--files-from",
                files_tmp.name,
                "--json",
            ]
            return self._run_xray_cli_process(cmd, timeout_seconds, on_process_spawned)
        except OSError as exc:
            # Broadened from FileNotFoundError (Bug #1612): ANY OS-level
            # failure -- creating the temp files or spawning xray-cli,
            # including a residual E2BIG ("Argument list too long") -- must
            # be caught here and surfaced as a structured error tuple, never
            # propagate unhandled up through run_batch() into the MCP layer
            # as a raw -32603 internal error.
            msg = f"xray-cli could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return "", _sanitize_error_message(msg)
        finally:
            if tmp_file is not None:
                Path(tmp_file.name).unlink(missing_ok=True)
            if files_tmp is not None:
                Path(files_tmp.name).unlink(missing_ok=True)

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
