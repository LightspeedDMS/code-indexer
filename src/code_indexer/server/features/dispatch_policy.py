"""Consume the per-tool and per-job dispatch policy features declare (SDK 0.5).

The MCP dispatcher, the job manager and the dashboard each keep a policy
table for the core's own tools and job types. A feature declares the same
for its own:

=======================================  ===================================
Contribution field                       Core consumer
=======================================  ===================================
``ToolContribution.manages_own_timeout`` ``mcp/protocol.py``: no generic
                                         dispatch timeout for the tool
``JobTypeContribution                    ``services/dashboard_service.py``:
.hidden_from_dashboard``                 not in the recent-jobs panel
``JobTypeContribution                    ``repositories/background_jobs.py``:
.repo_alias_optional``                   a missing ``repo_alias`` is expected
=======================================  ===================================

Process wiring, identical on every node (it follows ``enabled_features``),
replaced as a whole whenever an app is built, so a disabled feature's
entries disappear with its tools. Imports nothing heavy: the dispatcher and
the job manager read it on every call.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, FrozenSet, Sequence

if TYPE_CHECKING:
    from code_indexer.server.features.loader import LoadedFeature

_lock = threading.Lock()
_own_timeout_tools: FrozenSet[str] = frozenset()
_dashboard_hidden: FrozenSet[str] = frozenset()
_repo_alias_optional: FrozenSet[str] = frozenset()


def install_dispatch_policy(loaded: Sequence["LoadedFeature"]) -> None:
    """Replace the installed policy with what *loaded* declares."""
    global _own_timeout_tools, _dashboard_hidden, _repo_alias_optional
    tools = frozenset(
        tool.name
        for item in loaded
        for tool in item.contributions.tools
        if tool.manages_own_timeout
    )
    job_types = [
        job_type for item in loaded for job_type in item.contributions.job_types
    ]
    with _lock:
        _own_timeout_tools = tools
        _dashboard_hidden = frozenset(
            j.operation_type for j in job_types if j.hidden_from_dashboard
        )
        _repo_alias_optional = frozenset(
            j.operation_type for j in job_types if j.repo_alias_optional
        )


def feature_tool_manages_own_timeout(tool_name: str) -> bool:
    """Whether an installed feature tool bounds its own runtime."""
    with _lock:
        return tool_name in _own_timeout_tools


def feature_dashboard_hidden_operation_types() -> FrozenSet[str]:
    """Operation types enabled features keep out of the recent-jobs panel."""
    with _lock:
        return _dashboard_hidden


def feature_operation_omits_repo_alias(operation_type: str) -> bool:
    """Whether an enabled feature submits *operation_type* without a
    ``repo_alias`` by design."""
    with _lock:
        return operation_type in _repo_alias_optional
