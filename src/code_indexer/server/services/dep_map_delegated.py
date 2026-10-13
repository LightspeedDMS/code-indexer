"""
Dependency-map analysis under a delegated content backend: refused, loudly.

Every dependency-map WRITE -- full and delta analysis, refinement, the repair
executor (including its graph-repair dry run) and the lifecycle pre-flight --
runs a Claude agent with its working directory in the golden-repos directory
and lets it read the repositories with its own file tools. A pod with
``content_backend: delegated`` does not hold the repositories, so the agent
would see an empty tree and the run would publish an empty or partial map that
looks successful.

Until the agent gets its repository context through cidx's MCP tools (or a
checkout), those runs are refused with :data:`ANALYSIS_UNSUPPORTED` before any
sentinel is claimed or file written. Reading the existing map (MCP depmap
tools, dashboard, health) keeps working through the content seam. Under the
local backend nothing here applies.
"""

from __future__ import annotations

from typing import Optional

ANALYSIS_UNSUPPORTED = (
    "Dependency-map analysis is not supported with content_backend='delegated': "
    "its Claude agent reads the golden repositories from this pod's disk, which "
    "holds none, so it would produce an empty or partial map. The existing map "
    "stays readable. Run analysis and repair on a server with the repositories "
    "mounted until the agent reads them through cidx's MCP tools."
)


class DependencyMapUnsupportedError(RuntimeError):
    """A dependency-map write was asked for on a mount-less pod."""

    def __init__(self, action: str) -> None:
        super().__init__(f"{action}: {ANALYSIS_UNSUPPORTED}")
        self.action = action


def analysis_refusal() -> Optional[str]:
    """Why dependency-map analysis cannot run on this server, or None."""
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    return ANALYSIS_UNSUPPORTED if is_content_seam_delegated() else None


def refuse_analysis(action: str) -> None:
    """Raise DependencyMapUnsupportedError when analysis cannot run here."""
    if analysis_refusal() is not None:
        raise DependencyMapUnsupportedError(action)
