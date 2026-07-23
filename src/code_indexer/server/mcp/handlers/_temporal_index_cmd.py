"""Temporal indexing command builder — extracted from repos.py (Story #1404 follow-up).

Centralizes construction of the ``cidx index --index-commits`` subprocess
command line used by golden-repo provider temporal indexing jobs. Extracted
out of repos.py purely to keep that file under the 2500-line anti-file-bloat
limit (Messi Rule #6) -- no behavior change from the pre-extraction version.

Re-exported from repos.py as ``_build_temporal_index_cmd`` so existing
call sites and tests (which import it directly from
``code_indexer.server.mcp.handlers.repos``) keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _build_temporal_index_cmd(
    clear: bool,
    temporal_options: dict,
    all_branches_gate_enabled: bool = False,
    alias: str = "",
    global_floor_date: Optional[str] = None,
) -> list:
    """Build the cidx index --index-commits command with optional temporal flags.

    Story #1412: --all-branches is only appended when all_branches_gate_enabled
    is True (defaults to False -- fail-closed). When temporal_options requests
    all_branches but the gate is off, the flag is skipped and a WARNING is
    logged naming the repo so the downgrade to single-branch is observable.

    Story #1404: global_floor_date (the global temporal indexing floor date,
    resolved by the caller via temporal_floor_date.resolve_temporal_floor_date)
    composes with temporal_options["since_date"] (the pre-existing per-repo
    override) as "more restrictive wins" via resolve_effective_floor_date --
    exactly one --since-date flag is ever emitted, never two, and the flag is
    omitted entirely when both are unset (byte-identical unbounded no-op).

    Bug fix (found during the Story #1404 launch-site sweep): the per-repo
    since_date option previously emitted "--since <value>", but cli.py only
    defines "--since-date" -- every provider temporal rebuild for a golden
    repo with a per-repo since_date crashed the child process with an
    invalid-option error. Fixed here as part of this same change.
    """
    cmd = ["cidx", "index", "--index-commits", "--progress-json"]
    if clear:
        cmd.append("--clear")
    if not temporal_options or not isinstance(temporal_options, dict):
        temporal_options = {}
    diff_context = temporal_options.get("diff_context")
    if diff_context is not None:
        cmd.extend(["--diff-context", str(diff_context)])
    if temporal_options.get("all_branches"):
        if all_branches_gate_enabled:
            cmd.append("--all-branches")
        else:
            logger.warning(
                "all_branches requested for golden '%s' but "
                "temporal_all_branches_enabled=false; indexing single-branch",
                alias or "<unknown>",
            )
    max_commits = temporal_options.get("max_commits")
    if max_commits is not None:
        cmd.extend(["--max-commits", str(max_commits)])
    from code_indexer.server.services.temporal_floor_date import (
        resolve_effective_floor_date,
    )

    effective_since_date = resolve_effective_floor_date(
        global_floor_date, temporal_options.get("since_date")
    )
    if effective_since_date:
        cmd.extend(["--since-date", str(effective_since_date)])
    return cmd
