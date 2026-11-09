"""The X-Ray seam: where a caller-supplied X-Ray evaluator is compiled and run.

Entry-point group ``code_indexer.xray_backend``. ``local`` runs ``xray-cli``
in this pod (the X-Ray feature's own engine); ``delegated`` runs it wherever the
plugin does (the sandbox service). Everything above execution -- auth, validation,
candidate selection, job tracking, truncation -- stays with the feature and
never learns where the evaluator ran.

* :class:`XrayExecutionBackend` (alias :data:`XrayBackend`) -- the seam
  Protocol: execute a scan, execute a graph analysis.
* :class:`XrayInlineExecutionBackend` -- the optional extension a delegated
  backend offers to run over files this pod sends inline (a repository that
  exists only on this pod), with :class:`XrayInlineLimits`.
* :class:`XrayCacheBackend` -- the cluster store for compiled evaluators.

Moved from ``code_indexer.kernel.seam_types`` (originally
``xray/execution_backend.py`` and ``xray/rust_backend.py``); the old modules
re-export these names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

# One (matches, errors, meta) tuple per file spec -- or ONE tuple with
# file_path == "" for a whole-batch failure (see rust_backend's module
# docstring). The same alias rust_backend returns from run_batch.
BatchResult = List[
    Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
]


@dataclass(frozen=True)
class XrayScanRequest:
    """One evaluator over one batch of candidate files.

    Attributes:
        evaluator_code: Rust source defining ``evaluate_node``; validated by
            the backend before it is compiled.
        file_specs: One dict per candidate: ``file_path`` (relative to
            ``repo_path``), ``lang``, ``match_positions`` from Phase 1, and
            ``source`` (unused by the scan; xray-cli reads the file itself).
        repo_path: Base directory the ``file_path`` values are relative to;
            None means the current directory.
        timeout_seconds: Budget for the whole execution, compile included.
        on_process_spawned: Called with the spawned process so a caller can
            cancel it. Only an in-pod backend has one; others never call it.
    """

    evaluator_code: str
    file_specs: List[Dict[str, Any]]
    repo_path: Optional[str]
    timeout_seconds: int
    on_process_spawned: Optional[Callable[..., Any]] = None


@dataclass(frozen=True)
class XrayScanOutcome:
    """A scan's per-file results and the evaluator's ``debug_log()`` lines.

    ``debug_messages`` is None when no scan output was produced (validation,
    compile or transport failure), so the caller keeps whatever it last saw
    -- the behaviour ``RustNativeBackend._last_debug_messages`` always had.
    """

    results: BatchResult
    debug_messages: Optional[List[str]] = None


@dataclass(frozen=True)
class XrayGraphRequest:
    """One evaluator compiled once, then build-graph -> analyze-graph.

    Attributes:
        evaluator_code: Rust source; validated by the backend.
        repo_root: Root of the repository the graph is built over.
        file_paths: The candidate files the graph is built from.
        timeout_seconds: Budget for compile + build + analyze (+ refine).
        refine: Run the opt-in ``--refine`` phase when analyze-graph asks.
    """

    evaluator_code: str
    repo_root: str
    file_paths: List[str]
    timeout_seconds: int
    refine: bool = False


@runtime_checkable
class XrayExecutionBackend(Protocol):
    """Compiles and runs an X-Ray evaluator somewhere.

    Implementations never raise for a known failure: a scan answers with
    error tuples (``file_path == ""`` for a whole-batch failure), a graph
    analysis with the ``{"error": ...}`` dict ``_graph_error_result`` builds.
    """

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome: ...

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]: ...


#: The SDK name of the X-Ray seam Protocol (the core calls it
#: ``XrayExecutionBackend``; both names are the same class).
XrayBackend = XrayExecutionBackend


@runtime_checkable
class XrayInlineLimits(Protocol):
    """What one inline request may carry (decoded bytes)."""

    @property
    def max_files(self) -> int: ...

    @property
    def max_file_bytes(self) -> int: ...

    @property
    def max_total_bytes(self) -> int: ...


@runtime_checkable
class XrayInlineExecutionBackend(Protocol):
    """A delegated X-Ray backend that also runs over files sent inline.

    For a repository that exists only on this pod: the caller reads the
    candidate files and sends their bytes, keyed by repo-relative path; the
    evaluator still never runs on this pod. Requests stay within
    :attr:`inline_limits`; over a limit the caller refuses loudly rather than
    truncating.
    """

    @property
    def inline_limits(self) -> XrayInlineLimits: ...

    def execute_scan_inline(
        self, request: XrayScanRequest, files: Mapping[str, bytes]
    ) -> XrayScanOutcome: ...

    def execute_graph_analysis_inline(
        self, request: XrayGraphRequest, files: Mapping[str, bytes]
    ) -> Dict[str, Any]: ...


class XrayCacheBackend(Protocol):
    """Structural interface for cluster-aware evaluator cache backends.

    Implementations must support fetch (cache lookup) and store (cache write).
    All methods must be exception-safe — callers assume they never raise.

    The `so_bytes` crossing this interface is an OPAQUE BLOB, not a loadable
    `.so`: it is the integrity envelope produced by
    `wrap_so_for_cluster_cache` and checked by
    `verified_so_from_cluster_blob` (see their comments). A backend must
    store and return it byte-for-byte and must never try to interpret,
    rewrite or truncate it — any such change makes the blob unverifiable,
    which costs a recompile on every node. Equally, a backend is NOT the
    place to add an integrity check of its own: verification belongs at the
    point the bytes become a file that gets dlopen'd, which is
    `RustNativeBackend._try_pre_fill`, so every backend gets it.
    """

    def fetch(self, source_hash: str, rustc_version: str) -> Optional[bytes]:
        """Return the cached envelope if fresh and rustc_version matches, else None."""
        ...

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        """Upsert a compiled-evaluator envelope into the cache."""
        ...
