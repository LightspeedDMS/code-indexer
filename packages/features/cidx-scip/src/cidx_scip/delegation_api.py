"""The public API a delegated SCIP execution backend builds on.

Under ``scip_backend='delegated-plugin'`` an out-of-tree plugin (entry point
group ``code_indexer.scip_backend``, or ``CIDX_SCIP_BACKEND``; see
``server/services/scip_backend_plugin.py``) runs a project's SCIP indexer
somewhere else -- evo-sandbox's runs it in the sandbox service's ``scip`` run profile --
and hands back the index BYTES. cidx keeps everything else: project discovery,
the request, writing the index under the repository's index root (the
IndexStore path every SCIP reader uses), sha256 verification, the database
build and the success/failure semantics. The plugin imports from this module
only; the names below are stable, everything else in ``cidx_scip`` is
private and may change without notice.

What is here:

* :class:`ScipBuildRequest` -- what to index (cidx-discovered project, repo-root
  relative, plus the repository path the plugin derives its addressing from);
* :class:`ScipBuildOutcome` -- what came back (status, index bytes + sha256,
  indexer identity, degenerate flag, error);
* :class:`ScipExecutionBackend` -- the protocol a plugin's factory returns;
* the status vocabulary (:data:`STATUS_OK`, :data:`STATUS_FAILED`,
  :data:`STATUS_UNSUPPORTED`) and :func:`sha256_hex`;
* the the sandbox service transport (:class:`SandboxMcpClient`, :class:`SandboxMcpError`)
  re-exported, so a plugin need not import ``code_indexer.utils``.

A backend should not raise for a known failure: it answers with an outcome
whose ``status`` says what happened and whose ``error`` says why. cidx still
treats a raised exception, a non-outcome return value, or an ``ok`` outcome
whose bytes do not match their sha256 as a loud FAILED project -- never a local
fallback.
"""

from __future__ import annotations

# The seam's types and vocabulary live in the SDK (``cidx_sdk.seams.scip``);
# re-exported here, the import path existing plugins use.
from cidx_sdk.seams.scip import (
    DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS as DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS,
    SCIP_STATUSES as SCIP_STATUSES,
    STATUS_FAILED as STATUS_FAILED,
    STATUS_OK as STATUS_OK,
    STATUS_UNSUPPORTED as STATUS_UNSUPPORTED,
    ScipBuildOutcome as ScipBuildOutcome,
    ScipBuildRequest as ScipBuildRequest,
    ScipExecutionBackend as ScipExecutionBackend,
    sha256_hex as sha256_hex,
)
from cidx_scip._platform import SandboxMcpClient, SandboxMcpError

__all__ = [
    "DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS",
    "SCIP_STATUSES",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_UNSUPPORTED",
    "SandboxMcpClient",
    "SandboxMcpError",
    "ScipBuildOutcome",
    "ScipBuildRequest",
    "ScipExecutionBackend",
    "sha256_hex",
]
