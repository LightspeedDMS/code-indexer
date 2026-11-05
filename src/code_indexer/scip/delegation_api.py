"""Compatibility path of the delegated SCIP build API.

A SCIP backend plugin (entry point group ``code_indexer.scip_backend``)
published before modular phase 4d imports its types from here. They are the
SDK's (``cidx_sdk.seams.scip``) plus the core's sandbox-mcp client, exactly
what ``cidx_scip.delegation_api`` exports; this module imports no feature
code, so a plugin loads whether or not the ``scip`` feature is installed.
New plugins import ``cidx_sdk.seams.scip``.
"""

from __future__ import annotations

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

from code_indexer.utils.sandbox_mcp_client import (
    SandboxMcpClient as SandboxMcpClient,
    SandboxMcpError as SandboxMcpError,
)

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
