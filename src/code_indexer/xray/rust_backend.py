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
import uuid
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

# R2-4 (consolidated review, Issue #1811/Bug #1812, Codex re-review):
# process-wide, deadline-aware semaphore bounding concurrent
# rustc-invoking xray-cli launches. The graph admission limiter (H8)
# bounds concurrent GRAPH JOBS, but nothing previously bounded concurrent
# COMPILES specifically -- multiple users triggering many simultaneous
# evaluator compiles (each a real rustc process) can exhaust CPU/RAM/
# temp-disk/NFS at fleet scale (~900 repos). Hardcoded rather than a new
# Web UI setting (project convention: no new config surface to gate a bug
# fix; mirrors the documented hardcoded constants in
# graph::analyze::process.rs -- POLL_INTERVAL/STDOUT_DRAIN_TIMEOUT/
# REAP_RETRY_COUNT). Revisit if fleet telemetry shows a need to tune it
# live.
_MAX_CONCURRENT_COMPILES = 4
_compile_semaphore = threading.Semaphore(_MAX_CONCURRENT_COMPILES)


def _acquire_compile_slot(timeout_seconds: float) -> bool:
    """Acquire a compile slot, waiting at most `timeout_seconds`.

    Deadline-aware: returns False (never blocks indefinitely) once the
    caller's own remaining operation budget is exhausted, so a saturated
    compile queue degrades to a clear timeout error rather than hanging
    the request.
    """
    return _compile_semaphore.acquire(timeout=timeout_seconds)


def _release_compile_slot() -> None:
    """Release a previously-acquired compile slot."""
    _compile_semaphore.release()


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


def _graph_error_result(
    error_type: str,
    error_message: str,
    build_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Story #1811 (S5, AC2): the structured error shape every failure path
    of `RustNativeBackend.run_graph_analysis` returns -- never a raw
    exception (Bug #1612's rule). Mirrors `_error_tuple`'s role for the
    legacy `run_batch` path, adapted to graph mode's single-result (not
    per-file) shape. Every non-error field is set to an honest "unknown/
    not reached" value (`None`/empty/`False`) rather than a value that
    could be misread as a real, successful outcome.
    """
    return {
        "ok": False,
        "error": {
            "error_type": error_type,
            "error_message": _sanitize_error_message(error_message),
        },
        "status": None,
        "findings": [],
        "refine": [],
        "fact_graph_complete": None,
        "build_status": build_status,
        "degradation": None,
        "cached": False,
        "compile_ms": 0,
    }


def _safe_unlink_graph_temp_path(path: Optional[Any]) -> None:
    """Best-effort cleanup for one graph-mode temp path -- catches and
    LOGS `OSError` rather than letting a cleanup failure escape a
    `finally` block, which would otherwise override the real return value
    `run_graph_analysis`'s `try` already computed (a correctness bug, not
    merely an aesthetic one: `finally` exceptions replace whatever the
    `try`/`except` returned). `path` may be `None` (nothing to clean up
    yet) or any path-like value accepted by `Path(...)`.
    """
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "RustNativeBackend: graph-mode temp cleanup failed for %s: %s", path, exc
        )


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
        self,
        rust_code: str,
        deadline_seconds: Optional[float] = None,
        graph_mode: bool = False,
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

        H9 (consolidated review, Issue #1811/Bug #1812): `graph_mode` must
        be True for a graph-mode evaluator (fn collect_facts + fn
        analyze_graph) -- it selects the graph-mode-aware identity formula
        (`cache_identity_info_graph` on the Rust side, via `--graph-mode`),
        which is the ONLY identity that matches what `compile_evaluator`
        actually uses as the real `.so` filename for that evaluator. The
        cache key is mode-qualified (`"graph:" + rust_code` vs plain
        `rust_code`) so a legacy and a graph-mode call sharing one
        SharedIdentityCache instance can never collide on identical source
        text mapping to two different real identities.
        """
        cache_key = f"graph:{rust_code}" if graph_mode else rust_code
        cached = self._identity_cache.get(cache_key)
        if cached is not None:
            return cached
        if deadline_seconds is not None and deadline_seconds <= 0:
            logger.warning(
                "XrayCache: skipping --print-cache-identity -- operation "
                "deadline already exhausted"
            )
            _record_identity_failure_metric("deadline_exhausted")
            return None
        info = self._fetch_identity_via_subprocess(
            rust_code, deadline_seconds, graph_mode=graph_mode
        )
        if info is not None:
            self._identity_cache.put(cache_key, info)
        return info

    def _fetch_identity_via_subprocess(
        self,
        rust_code: str,
        deadline_seconds: Optional[float],
        graph_mode: bool = False,
    ) -> Optional[CacheIdentityInfo]:
        """Run `xray-cli --print-cache-identity`, parse, classify failures.

        Never raises. Records a cidx.xray.cache_identity_failures metric on
        every failure path (nonzero exit, incomplete output, exception).
        """
        try:
            timeout = self._resolve_identity_timeout(deadline_seconds)
            result = self._run_cache_identity_subprocess(
                rust_code, timeout, graph_mode=graph_mode
            )
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
        self,
        rust_code: str,
        timeout_seconds: float = _CACHE_IDENTITY_TIMEOUT_SECS,
        graph_mode: bool = False,
    ) -> "subprocess.CompletedProcess[str]":
        cmd = [str(self._xray_cli_path), "--print-cache-identity"]
        if graph_mode:
            cmd.append("--graph-mode")
        return subprocess.run(
            cmd,
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
        graph_mode: bool = False,
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
            graph_mode: H9 (consolidated review, Issue #1811/Bug #1812) --
                must be True when `rust_code` is a graph-mode evaluator, so
                the identity computed here (used to locate the local .so to
                upload) matches the one `compile_evaluator` actually used
                (`_compile_for_graph_mode` always passes True).
        """
        try:
            if self._xray_cache is None:
                logger.warning(
                    "XrayCache: post-fill called with no cache backend — skipping"
                )
                return
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds, graph_mode=graph_mode
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
        self,
        rust_code: str,
        deadline_seconds: Optional[float] = None,
        graph_mode: bool = False,
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
            graph_mode: H9 (consolidated review, Issue #1811/Bug #1812) --
                must be True when `rust_code` is a graph-mode evaluator, so
                the identity computed here matches the one
                `compile_evaluator` actually uses (`_compile_for_graph_mode`
                always passes True).

        Early-returns when BOTH .so and .meta already exist for the identity.
        """
        assert self._xray_cache is not None, (
            "_try_pre_fill requires non-None _xray_cache"
        )
        try:
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds, graph_mode=graph_mode
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
        acquire_compile_slot: bool,
    ) -> Tuple[str, Optional[str]]:
        """Spawn xray-cli, wait for completion, return (stdout, error_msg).

        R2-3 (Codex re-review): spawned in its OWN process group
        (`start_new_session=True`) so that on timeout the ENTIRE group --
        including any grandchild the direct child spawned (e.g. rustc
        launched by xray-cli) -- is killed via `os.killpg`, never just the
        direct child via a bare `proc.kill()`. SIGKILL cannot be caught or
        forwarded by the process it kills, so a bare `proc.kill()` leaves
        any grandchild reparented to init, running FOREVER as an orphan
        that keeps consuming CPU/RAM.

        R2-4 (Codex re-review): `acquire_compile_slot` has NO DEFAULT --
        every call site must state whether this invocation can trigger a
        fresh rustc compile (the legacy scan path: True) or only runs an
        already-compiled dylib (--build-graph/--analyze-graph: False), so
        a future call site can never silently inherit an unthrottled
        default. When True, a process-wide compile slot is acquired
        (deadline-aware, bounded by `timeout_seconds`) BEFORE spawning the
        subprocess -- a saturated compile queue returns a clear timeout
        error instead of an unbounded Nth concurrent rustc process, and
        the slot is released in `finally` regardless of outcome.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean
        import signal  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

        if acquire_compile_slot and not _acquire_compile_slot(
            timeout_seconds=float(timeout_seconds)
        ):
            msg = (
                f"xray-cli compile queue full: timed out waiting "
                f"{timeout_seconds}s for a compile slot"
            )
            logger.warning("RustNativeBackend: %s", msg)
            return "", _sanitize_error_message(msg)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

            if on_process_spawned is not None:
                on_process_spawned(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                # `start_new_session=True` makes proc.pid both the process
                # ID AND the process group ID (new session/group leader),
                # so `os.killpg(proc.pid, ...)` reaches the direct child
                # AND every descendant it spawned in one signal.
                # ProcessLookupError means the whole group already exited
                # between TimeoutExpired firing and this line (a benign
                # race, not an error). PermissionError is a second, rarer
                # benign race: the group leader already exited and its
                # PGID was reused by an unrelated process this session
                # cannot signal. Neither must prevent proc.wait() below
                # from reaping the direct child.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
                msg = f"xray-cli timed out after {timeout_seconds}s"
                logger.warning("RustNativeBackend: %s", msg)
                return "", _sanitize_error_message(msg)

            # H12 (consolidated review, Issue #1811/Bug #1812, Codex):
            # treat EVERY non-zero return code as failure, including
            # stderr in the message -- a crashed subprocess that happened
            # to emit plausible-looking (partial/stale) JSON to stdout
            # before exiting non-zero must never be silently parsed and
            # accepted as a real result just because stdout was non-empty.
            if proc.returncode != 0:
                raw_msg = (
                    f"xray-cli exited with code {proc.returncode}: "
                    f"{stderr[:_XRAY_CLI_STDERR_ERROR_LIMIT]}"
                )
                logger.warning("RustNativeBackend: %s", raw_msg)
                return "", _sanitize_error_message(raw_msg)
            return stdout or "", None
        finally:
            if acquire_compile_slot:
                _release_compile_slot()

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
            return self._run_xray_cli_process(
                cmd, timeout_seconds, on_process_spawned, acquire_compile_slot=True
            )
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

    # ------------------------------------------------------------------
    # Story #1811 (S5, AC2): graph-mode driver
    # ------------------------------------------------------------------

    def _compile_for_graph_mode(
        self,
        rust_code: str,
        eval_path: str,
        deadline_seconds: Optional[float],
    ) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
        """Compile `rust_code` (already written to `eval_path`) to a `.so`
        via `xray-cli --compile-only`, reusing the EXISTING cluster-cache
        pre-fill/post-fill machinery unchanged (Bug #1784: identity comes
        ONLY from `--print-cache-identity`, never re-derived here).

        Honours `deadline_seconds` the way `_get_cache_identity_info`
        already does: an already-exhausted deadline skips the subprocess
        entirely rather than spawning one doomed to time out immediately.

        Returns `(so_path, {"cached": bool, "compile_ms": int},
        error_message)`. `so_path` is `None` iff `error_message` is not
        `None`.
        """
        if self._xray_cache is not None:
            self._try_pre_fill(
                rust_code, deadline_seconds=deadline_seconds, graph_mode=True
            )

        if deadline_seconds is not None and deadline_seconds <= 0:
            return None, {}, "operation deadline already exhausted before compile"
        timeout = int(deadline_seconds) + 1 if deadline_seconds is not None else 300

        output, error = self._run_compile_only_subprocess(eval_path, timeout)
        if error is not None:
            return None, {}, error
        compile_error = output.get("error")
        if compile_error:
            return None, {}, str(compile_error)
        so_path = output.get("so_path")
        if not so_path:
            return None, {}, "xray-cli --compile-only produced no so_path"

        compile_info = {
            "cached": bool(output.get("cached", False)),
            "compile_ms": int(output.get("compile_ms", 0)),
        }
        if (
            self._xray_cache is not None
            and not compile_info["cached"]
            and compile_info["compile_ms"] > 0
        ):
            self._try_post_fill(
                rust_code,
                compile_info["compile_ms"],
                deadline_seconds=deadline_seconds,
                graph_mode=True,
            )
        return so_path, compile_info, None

    def _run_compile_only_subprocess(
        self, eval_path: str, timeout_seconds: int
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Spawn `xray-cli --compile-only --dynlib <eval_path>` and parse its
        JSON `CompileOnlyOutput`. Reuses `_run_xray_cli_process` (subprocess
        spawn/timeout handling) and `_parse_json_output` (JSON parsing) --
        never reimplements either. Returns `(output_dict, error_msg)` --
        `error_msg` is `None` on a successful subprocess run (the COMPILE
        itself may still have failed; check `output_dict["error"]`).
        """
        cmd = [str(self._xray_cli_path), "--compile-only", "--dynlib", eval_path]
        stdout, error = self._run_xray_cli_process(
            cmd, timeout_seconds, None, acquire_compile_slot=True
        )
        if error is not None:
            return {}, error
        return self._parse_json_output(stdout)

    def _run_graph_subcommand(
        self, args: List[str], timeout_seconds: int
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Spawn `xray-cli <args...>` and parse its JSON report -- shared
        by `--build-graph` and `--analyze-graph` (Rule 4).
        """
        cmd = [str(self._xray_cli_path)] + args
        stdout, error = self._run_xray_cli_process(
            cmd, timeout_seconds, None, acquire_compile_slot=False
        )
        if error is not None:
            return {}, error
        return self._parse_json_output(stdout)

    @staticmethod
    def _build_graph_analysis_result(
        analyze_output: Dict[str, Any],
        build_status: str,
        build_result: Dict[str, Any],
        compile_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Story #1811 (AC3): assembles the final result, surfacing
        `AnalysisCompleteness` HONESTLY. Every degradation counter comes
        from the real `BuildGraphResult` via a bare `.get(key)` -- NO
        default: `build_result` is only passed here once the build's
        status was confirmed `"ok"`, at which point every field is always
        present; a missing key means malformed JSON, surfacing as `None`
        (distinct from a genuine `0`/`False`), never a silently-masked
        "clean" reading.
        """
        status = analyze_output.get("status")
        raw_result = analyze_output.get("result")
        result = raw_result if isinstance(raw_result, dict) else {}
        reported_ran_ok = status == "ran_ok"

        # H11 (consolidated review, Issue #1811/Bug #1812, Codex): a
        # ran_ok status is only trustworthy if "result" is a REAL dict
        # carrying BOTH required fields -- `.get("x", [])`-style
        # defaulting on a missing/malformed payload would otherwise accept
        # `{"status": "ran_ok"}` alone as a plausible, quietly-empty
        # success. Missing required fields under a ran_ok status is
        # malformed CLI output, never a legitimate "nothing found".
        schema_complete = (
            isinstance(raw_result, dict)
            and "findings" in raw_result
            and "refine" in raw_result
        )
        ok = reported_ran_ok and schema_complete

        if reported_ran_ok and not schema_complete:
            error = {
                "error_type": "MalformedCliOutput",
                "error_message": _sanitize_error_message(
                    "--analyze-graph reported status=ran_ok but result is "
                    "missing required findings/refine fields"
                ),
            }
        elif not ok:
            error = {
                "error_type": "GraphAnalysisError",
                "error_message": _sanitize_error_message(
                    f"--analyze-graph reported status={status}"
                ),
            }
        else:
            error = None

        degradation_keys = (
            "files_with_parse_errors",
            "unreadable_or_unsupported_files",
            "files_with_read_errors",
            "files_with_extractor_panics",
            "files_with_collector_panics",
            "files_with_unsupported_language",
            "truncated_by_max_files",
        )
        return {
            "ok": ok,
            "error": error,
            "status": status,
            "findings": result.get("findings", []),
            "refine": result.get("refine", []),
            "fact_graph_complete": build_result.get("fact_graph_complete"),
            "build_status": build_status,
            "degradation": {key: build_result.get(key) for key in degradation_keys},
            "cached": compile_info.get("cached", False),
            "compile_ms": compile_info.get("compile_ms", 0),
        }

    def _run_build_graph_step(
        self,
        repo_root: str,
        files_from_path: str,
        so_path: str,
        graph_out: Path,
        facts_out: Path,
        deadline: float,
    ) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], Optional[Dict[str, Any]]]:
        """Runs `--build-graph`. Returns `((status, result), None)` on a
        real `"ok"` build, or `(None, error_result)` on any failure --
        including an already-exhausted deadline, which never spawns a
        doomed subprocess.
        """
        remaining = self._remaining_seconds(deadline)
        if remaining <= 0:
            return None, _graph_error_result(
                "Timeout", "deadline exhausted before --build-graph"
            )
        args = [
            "--build-graph",
            "--repo-root",
            repo_root,
            "--files-from",
            files_from_path,
            "--dylib",
            so_path,
            "--graph-out",
            str(graph_out),
            "--facts-out",
            str(facts_out),
        ]
        output, error = self._run_graph_subcommand(args, int(remaining) + 1)
        if error is not None:
            return None, _graph_error_result("XRayCliError", error)
        status = output.get("status")
        result = output.get("result") or {}
        if status != "ok":
            return None, _graph_error_result(
                "GraphBuildError",
                f"--build-graph reported status={status}",
                build_status=status,
            )
        return (status, result), None

    def _run_analyze_graph_step(
        self,
        graph_out: Path,
        so_path: str,
        facts_out: Path,
        deadline: float,
        build_status: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Runs `--analyze-graph`. Returns `(output, None)` on a real
        subprocess success (the ChildReport's OWN status may still be
        non-`ran_ok`; that is handled by `_build_graph_analysis_result`,
        not here), or `(None, error_result)` on a subprocess-level failure
        or an already-exhausted deadline.
        """
        remaining = self._remaining_seconds(deadline)
        if remaining <= 0:
            return None, _graph_error_result(
                "Timeout",
                "deadline exhausted before --analyze-graph",
                build_status=build_status,
            )
        args = [
            "--analyze-graph",
            "--graph-in",
            str(graph_out),
            "--dylib",
            so_path,
            "--facts-in",
            str(facts_out),
        ]
        output, error = self._run_graph_subcommand(args, int(remaining) + 1)
        if error is not None:
            return None, _graph_error_result(
                "XRayCliError", error, build_status=build_status
            )
        return output, None

    def _compile_build_and_analyze(
        self,
        rust_code: str,
        repo_root: str,
        eval_path: str,
        files_from_path: str,
        graph_out: Path,
        facts_out: Path,
        deadline: float,
    ) -> Dict[str, Any]:
        """Compiles `rust_code` ONCE via `_compile_for_graph_mode`, then
        drives `_run_build_graph_step` -> `_run_analyze_graph_step` ->
        `_build_graph_analysis_result` -- the whole graph-mode pipeline in
        one short orchestrator. `assert`s below are real type-narrowing
        (Optional -> non-Optional), never a suppression: each one directly
        follows the `is not None` check on the sibling error-result value
        that guarantees it.
        """
        so_path, compile_info, compile_error = self._compile_for_graph_mode(
            rust_code, eval_path, self._remaining_seconds(deadline)
        )
        if compile_error is not None:
            return _graph_error_result("CompileError", compile_error)
        assert so_path is not None

        build_pair, build_error_result = self._run_build_graph_step(
            repo_root, files_from_path, so_path, graph_out, facts_out, deadline
        )
        if build_error_result is not None:
            return build_error_result
        assert build_pair is not None
        build_status, build_result = build_pair

        analyze_output, analyze_error_result = self._run_analyze_graph_step(
            graph_out, so_path, facts_out, deadline, build_status
        )
        if analyze_error_result is not None:
            return analyze_error_result
        assert analyze_output is not None

        return self._build_graph_analysis_result(
            analyze_output, build_status, build_result, compile_info
        )

    def run_graph_analysis(
        self,
        *,
        evaluator_code: str,
        repo_root: str,
        file_paths: List[str],
        timeout_seconds: int = 60,
    ) -> Dict[str, Any]:
        """Story #1811 (S5, AC2): compile the evaluator ONCE (reusing the
        existing compile/cache machinery, Bug #1784), then --build-graph ->
        --analyze-graph. Every KNOWN failure returns a structured
        `_graph_error_result`; the temp-file+subprocess flow is wrapped in
        `except OSError` (mirrors `_invoke_xray_cli` above) -- the MCP front
        door is the final backstop for anything truly unexpected (Bug #1612).
        """
        if timeout_seconds <= 0 or not repo_root or not file_paths:
            return _graph_error_result(
                "InvalidArgument", "invalid timeout_seconds/repo_root/file_paths"
            )

        rust_code, validation_error = self._validate_rust_code(evaluator_code)
        if validation_error is not None:
            return _graph_error_result("ValidationError", validation_error)
        if not self._xray_cli_path.exists():
            msg = f"xray-cli binary not found at {self._xray_cli_path}."
            logger.error("RustNativeBackend: %s", msg)
            return _graph_error_result("BinaryNotFound", msg)

        tmp_eval = tmp_files_from = graph_out = facts_out = None
        try:
            deadline = time.monotonic() + timeout_seconds
            tmp_dir = _get_xray_tmp_dir()
            tmp_dir.mkdir(parents=True, exist_ok=True)
            graph_out = tmp_dir / f"xray_graph_out_{uuid.uuid4().hex}.bin"
            facts_out = tmp_dir / f"xray_graph_facts_{uuid.uuid4().hex}.json"
            tmp_eval = _write_temp_file(rust_code, ".rs", "xray_graph_eval_", tmp_dir)
            tmp_files_from = _write_temp_file(
                "\n".join(file_paths), ".txt", "xray_graph_files_", tmp_dir
            )
            return self._compile_build_and_analyze(
                rust_code,
                repo_root,
                tmp_eval.name,
                tmp_files_from.name,
                graph_out,
                facts_out,
                deadline,
            )
        except OSError as exc:
            msg = f"xray-cli graph analysis could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return _graph_error_result("XRayCliError", msg)
        finally:
            eval_name = tmp_eval.name if tmp_eval is not None else None
            files_name = tmp_files_from.name if tmp_files_from is not None else None
            _safe_unlink_graph_temp_path(eval_name)
            _safe_unlink_graph_temp_path(files_name)
            _safe_unlink_graph_temp_path(graph_out)
            _safe_unlink_graph_temp_path(facts_out)


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
