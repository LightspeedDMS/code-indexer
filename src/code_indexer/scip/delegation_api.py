"""The public API a delegated SCIP execution backend builds on.

Under ``scip_backend='delegated-plugin'`` an out-of-tree plugin (entry point
group ``code_indexer.scip_backend``, or ``CIDX_SCIP_BACKEND``; see
``server/services/scip_backend_plugin.py``) runs a project's SCIP indexer
somewhere else -- evo-sandbox's runs it in sandbox-mcp's ``scip`` run profile --
and hands back the index BYTES. cidx keeps everything else: project discovery,
the request, writing the index under the repository's index root (the
IndexStore path every SCIP reader uses), sha256 verification, the database
build and the success/failure semantics. The plugin imports from this module
only; the names below are stable, everything else in ``code_indexer.scip`` is
private and may change without notice.

What is here:

* :class:`ScipBuildRequest` -- what to index (cidx-discovered project, repo-root
  relative, plus the repository path the plugin derives its addressing from);
* :class:`ScipBuildOutcome` -- what came back (status, index bytes + sha256,
  indexer identity, degenerate flag, error);
* :class:`ScipExecutionBackend` -- the protocol a plugin's factory returns;
* the status vocabulary (:data:`STATUS_OK`, :data:`STATUS_FAILED`,
  :data:`STATUS_UNSUPPORTED`) and :func:`sha256_hex`;
* the sandbox-mcp transport (:class:`SandboxMcpClient`, :class:`SandboxMcpError`)
  re-exported, so a plugin need not import ``code_indexer.utils``.

A backend should not raise for a known failure: it answers with an outcome
whose ``status`` says what happened and whose ``error`` says why. cidx still
treats a raised exception, a non-outcome return value, or an ``ok`` outcome
whose bytes do not match their sha256 as a loud FAILED project -- never a local
fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from code_indexer.utils.sandbox_mcp_client import SandboxMcpClient, SandboxMcpError

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

#: An index was produced; ``index_bytes`` + ``sha256`` are set.
STATUS_OK = "ok"
#: The build failed (toolchain, dependencies, timeout, sandbox, transport...).
STATUS_FAILED = "failed"
#: The language/build system cannot be indexed by the backend -- a benign skip
#: that, like the local "no indexer for language" case, does not block freshness.
STATUS_UNSUPPORTED = "unsupported"
SCIP_STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_UNSUPPORTED)

#: The local indexers' subprocess timeout (java.py, typescript.py), and the
#: budget cidx gives a delegated build.
DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class ScipBuildRequest:
    """One project's SCIP build.

    Attributes:
        repo_path: The repository's absolute path as cidx knows it
            (``<golden root>/<alias>`` or ``<golden root>/.versioned/<alias>/v_<n>``).
            The plugin derives its own addressing from it and must refuse a path
            it cannot address; it is never read on this pod by contract.
        golden_alias: The golden alias cidx believes ``repo_path`` belongs to,
            for the plugin to cross-check (None when cidx has none).
        project_path: Project directory relative to ``repo_path`` (POSIX, ``"."``
            for the root).
        build_system: Discovered build system (``maven``, ``gradle``, ``npm``...).
        language: Discovered language (``java``, ``typescript``...).
        allow_degenerate: Accept a locals-only index for an unbuilt ts/js tree.
        timeout_seconds: Budget for the indexer itself.
    """

    repo_path: str
    golden_alias: Optional[str]
    project_path: str
    build_system: str
    language: str
    allow_degenerate: bool = False
    timeout_seconds: int = DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ScipBuildOutcome:
    """What a backend reports for one :class:`ScipBuildRequest`.

    Attributes:
        status: One of :data:`SCIP_STATUSES`.
        index_bytes: The ``index.scip`` content (``ok`` only).
        sha256: Hex sha256 of ``index_bytes`` as the PRODUCER computed it (``ok``
            only); cidx re-hashes the bytes and fails the project on a mismatch.
        degenerate: The index is locals-only (``allow_degenerate`` path).
        degraded_reason: Why it is degenerate, when it is.
        indexer_name / indexer_version: The indexer that ran, when known.
        error: Why the status is not ``ok`` (operator-facing, no secrets).
        backpressure: The failure is the backend being temporarily full
            (HTTP 429, no sandbox capacity): cidx's build queue retries later.
        duration_ms: The backend's own measure of the build, when known.
        log: Tail of the build's diagnostic output, for the operator.
    """

    status: str
    index_bytes: Optional[bytes] = None
    sha256: Optional[str] = None
    degenerate: bool = False
    degraded_reason: Optional[str] = None
    indexer_name: Optional[str] = None
    indexer_version: Optional[str] = None
    error: str = ""
    backpressure: bool = False
    duration_ms: Optional[int] = None
    log: str = ""


@runtime_checkable
class ScipExecutionBackend(Protocol):
    """Runs one project's SCIP indexer somewhere and returns the index bytes."""

    def build_index(self, request: ScipBuildRequest) -> ScipBuildOutcome: ...

    def is_available(self) -> bool: ...


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of *data* -- the digest :attr:`ScipBuildOutcome.sha256` holds."""
    return hashlib.sha256(data).hexdigest()
