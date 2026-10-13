"""The public API a delegated X-Ray execution backend builds on.

Under ``xray_backend=delegated`` an out-of-tree plugin (entry point group
``code_indexer.xray_backend``; see ``server/startup/xray_backend_wiring.py``)
runs the evaluator somewhere else and hands back what xray-cli printed. Result
SHAPING must not move with it: a delegated result has to be byte-identical to
the one this pod would have built from the same xray-cli output. So the plugin
shapes with cidx's own code, and this module is the one place it imports that
code from -- the names below are stable; everything else in ``rust_backend`` is
private and may change without notice.

What is here:

* scan shaping -- :func:`build_scan_results` / :func:`build_matches` (with an
  explicit line-content source, so no disk read can happen), the error tuples
  (:func:`error_tuple`, :func:`error_all`; ``file_path == ""`` is a whole-batch
  failure) and :func:`scan_error_type` (xray-cli's ``error_kind`` -> type);
* graph mode -- :func:`run_graph_pipeline` over a :class:`GraphStepExecutor`
  (the plugin's executor replays the steps its sandbox ran; this pod's runs
  subprocesses), and :func:`graph_error_result`;
* xray-cli step errors worded as this pod words them
  (:func:`xray_cli_timeout_message`, :func:`xray_cli_exit_message`,
  :data:`XRAY_CLI_STDERR_ERROR_LIMIT`), and :func:`sanitize_error_message`;
* parsing / validation -- :func:`parse_json_output`,
  :func:`parse_cache_identity_output`, :func:`validate_rust_code`,
  :func:`repo_relative_file_id`;
* :data:`XRAY_EVALUATOR_ABI_VERSION` -- the evaluator ABI this cidx assembles
  for, so a pod without xray-cli can still tell a sandbox which ABI to run;
* the seam types (requests, outcomes, ``BatchResult``) re-exported from
  ``execution_backend``.

The sandbox-mcp transport is ``code_indexer.utils.sandbox_mcp_client``
(``SandboxMcpClient.call_tool`` / ``call_tool_payload``).
"""

from __future__ import annotations

from code_indexer.xray.execution_backend import (
    BatchResult,
    XrayExecutionBackend,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)
from code_indexer.xray.rust_backend import (
    GRAPH_STEPS,
    CacheIdentityInfo,
    GraphStep,
    GraphStepExecutor,
    LineContentSource,
    _error_all,
    _error_tuple,
    _graph_error_result,
    _repo_relative_file_id,
    _sanitize_error_message,
    _XRAY_CLI_STDERR_ERROR_LIMIT,
    build_matches,
    build_scan_results,
    parse_cache_identity_output,
    parse_json_output,
    run_graph_pipeline,
    scan_error_type,
    validate_rust_code,
    xray_cli_exit_message,
    xray_cli_timeout_message,
)

#: The X-Ray evaluator ABI (``XRAY_ABI_VERSION`` in
#: ``rust/xray-core/src/compiler/assemble.rs``, the single source of truth).
#: Mirrored here so a pod that delegates -- and ships no xray-cli -- can still
#: say which ABI it assembles for. ``tests/unit/xray/test_xray_delegation_api.py``
#: fails the moment the Rust constant moves without this one.
XRAY_EVALUATOR_ABI_VERSION = 14

#: How much of a failed step's stderr its error message carries.
XRAY_CLI_STDERR_ERROR_LIMIT = _XRAY_CLI_STDERR_ERROR_LIMIT

error_tuple = _error_tuple
error_all = _error_all
graph_error_result = _graph_error_result
sanitize_error_message = _sanitize_error_message
repo_relative_file_id = _repo_relative_file_id

__all__ = [
    "BatchResult",
    "CacheIdentityInfo",
    "GRAPH_STEPS",
    "GraphStep",
    "GraphStepExecutor",
    "LineContentSource",
    "XRAY_CLI_STDERR_ERROR_LIMIT",
    "XRAY_EVALUATOR_ABI_VERSION",
    "XrayExecutionBackend",
    "XrayGraphRequest",
    "XrayScanOutcome",
    "XrayScanRequest",
    "build_matches",
    "build_scan_results",
    "error_all",
    "error_tuple",
    "graph_error_result",
    "parse_cache_identity_output",
    "parse_json_output",
    "repo_relative_file_id",
    "run_graph_pipeline",
    "sanitize_error_message",
    "scan_error_type",
    "validate_rust_code",
    "xray_cli_exit_message",
    "xray_cli_timeout_message",
]
