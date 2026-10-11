"""Versioned-snapshot path resolution for provider-index background jobs --
extracted from repos.py.

Both helpers map an immutable ``.versioned/`` snapshot path to its mutable
base clone so a provider indexing job never writes into a snapshot (Bug #1084
B1: detection goes through the canonical ``is_versioned_snapshot`` predicate).
Extracted purely to keep repos.py under the 2500-line anti-file-bloat limit
(Messi Rule #6) -- no behavior change from the pre-extraction version.

Re-exported from repos.py so existing call sites and tests (which import them
from ``code_indexer.server.mcp.handlers.repos``) keep working unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot

logger = logging.getLogger(__name__)


def _resolve_provider_job_repo_path(repo_path: str, repo_alias: str) -> tuple:
    """Resolve the actual indexing path for a provider background job.

    When repo_path is inside a .versioned/ directory (immutable), return the
    base clone path instead. Returns (actual_path, resolved_alias, is_versioned).
    """
    if not repo_path or not isinstance(repo_path, str):
        return repo_path, repo_alias, False

    # Bug #1084 B1: detect snapshots via the single canonical predicate
    # (replaces the local-only ".versioned" in parts test).
    is_versioned = is_versioned_snapshot(repo_path)
    if not is_versioned:
        return repo_path, repo_alias, False

    parts = Path(repo_path).parts
    versioned_idx = parts.index(".versioned")
    if versioned_idx + 1 >= len(parts):
        logger.warning("_resolve_provider_job_repo_path: malformed path %s", repo_path)
        return repo_path, repo_alias, True

    alias_name = parts[versioned_idx + 1]
    # Reject traversal or separator segments in alias_name
    if (
        not alias_name
        or alias_name in (".", "..")
        or "/" in alias_name
        or "\\" in alias_name
    ):
        logger.warning(
            "_resolve_provider_job_repo_path: unsafe alias_name '%s'", alias_name
        )
        return repo_path, repo_alias, True

    golden_repos_dir = Path(*parts[:versioned_idx])
    base_clone = golden_repos_dir / alias_name

    # Verify resolved path stays within golden_repos_dir
    try:
        base_clone.resolve().relative_to(golden_repos_dir.resolve())
    except ValueError:
        logger.warning(
            "_resolve_provider_job_repo_path: path escape detected for %s", repo_path
        )
        return repo_path, repo_alias, True

    resolved_alias = repo_alias or f"{alias_name}-global"

    if not base_clone.exists():
        # Bug #1084 B1: the base clone for a cow-daemon repo lives at
        # golden_repos_dir/{ns}, not under the mount, so this local-shaped
        # arithmetic cannot reach it. Return the (non-existent) base_clone path
        # rather than the snapshot path: this is NEVER a writable snapshot, and
        # the consumer's `actual_path == repo_path and not exists()` guard would
        # have been DEFEATED on cow-daemon (the snapshot path DOES exist).
        logger.warning(
            "_resolve_provider_job_repo_path: base clone %s for versioned "
            "snapshot %s does not exist -- refusing to index the snapshot.",
            base_clone,
            repo_path,
        )
        return str(base_clone), resolved_alias, True

    logger.info(
        "Provider job: using base clone %s instead of versioned snapshot %s",
        base_clone,
        repo_path,
    )
    return str(base_clone), resolved_alias, True


def _resolve_versioned_to_base_clone(repo_path: str, repo_alias: str) -> tuple:
    """Resolve versioned snapshot path to base clone for background jobs.

    Uses direct path arithmetic (not _resolve_golden_repo_base_clone) because
    background workers have no access to server app state.

    Returns (actual_path, repo_alias, is_versioned, error_msg).
    error_msg is None on success.
    """
    if not repo_path or not isinstance(repo_path, str):
        return repo_path, repo_alias, False, "Invalid repo_path"
    if not Path(repo_path).is_absolute():
        return repo_path, repo_alias, False, f"repo_path must be absolute: {repo_path}"

    resolved = Path(repo_path).resolve()
    # Bug #1084 B1: detect snapshots via the single canonical predicate
    # (replaces the local-only ".versioned" in parts test).
    is_versioned = is_versioned_snapshot(str(resolved))
    if not is_versioned:
        return str(resolved), repo_alias, False, None

    parts = resolved.parts
    versioned_idx = parts.index(".versioned")
    if versioned_idx + 1 >= len(parts):
        return str(resolved), repo_alias, True, f"Malformed versioned path: {repo_path}"

    alias_name = parts[versioned_idx + 1]
    golden_repos_dir = Path(*parts[:versioned_idx])
    base_clone = golden_repos_dir / alias_name

    try:
        base_clone.resolve().relative_to(golden_repos_dir.resolve())
    except ValueError:
        return (
            str(resolved),
            repo_alias,
            True,
            f"Path escape in versioned path: {repo_path}",
        )

    if not repo_alias:
        repo_alias = f"{alias_name}-global"

    if not base_clone.exists():
        return (
            str(resolved),
            repo_alias,
            True,
            (
                f"Base clone not found at {base_clone} for versioned snapshot {repo_path}. "
                "Cannot index versioned snapshot (immutable)."
            ),
        )

    logger.info(
        "Provider index: using base clone %s instead of versioned snapshot %s",
        base_clone,
        repo_path,
    )
    return str(base_clone), repo_alias, True, None
