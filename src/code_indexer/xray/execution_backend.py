"""Where an X-Ray evaluator is compiled and run.

X-Ray compiles caller-supplied Rust and runs it over a repository through
``xray-cli``. That EXECUTION is the part that may move out of the cidx pod (to
a plugin backend, e.g. evo-sandbox's, which runs it in sandbox-mcp); everything
above it -- auth, validation for
UX, candidate selection, job tracking, truncation -- stays in cidx and never
learns where the evaluator ran. This module is the seam between the two:

* :class:`XrayExecutionBackend` -- "execute a scan" and "execute a graph
  analysis", carrying exactly the inputs ``xray-cli`` needs today and
  returning today's result shapes.
* ``LocalXrayExecutionBackend`` (in ``rust_backend``) -- the in-pod
  implementation: compile semaphore, rlimits, env allowlist and the local +
  Postgres compile caches all live there.
* a delegated implementation -- a plugin on the ``code_indexer.xray_backend``
  entry point, shaping its results with ``xray/delegation_api.py``.

``RustNativeBackend`` (what the search engine and handlers construct) keeps
its ``run_batch`` / ``run_graph_analysis`` signatures and dispatches to the
backend chosen by ``xray_backend`` (see
``server/startup/xray_backend_wiring.py``), so callers do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
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
