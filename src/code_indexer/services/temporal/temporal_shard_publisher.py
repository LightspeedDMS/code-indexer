"""Create-if-absent-else-swap temporal shard publication (Story #1457 AC6 Fix 1).

`AliasManager.swap_alias` CANNOT be used to publish a brand-new per-quarter
namespace: it raises `RuntimeError` when the pointer file is absent AND
validates `old_target`, raising `ValueError` on mismatch -- neither is
satisfiable on a first publish (there is NO registration-time creation of
temporal per-quarter aliases; a brand-new namespace is born lazily on first
index). `publish_temporal_shard_version` is the small orchestration
function implementing the required branch, reusing `AliasManager`'s
existing primitives verbatim (never a bespoke directory `os.replace`).

This module implements ONLY the publish DECISION (create-if-absent-else-
swap). AC6's three-branch BUILD machinery (Branch A / B-bootstrap /
B-fresh) now exists in `temporal_consolidated_build.py`
(`build_fresh_consolidated_temporal_version` /
`copy_and_extend_consolidated_temporal_version`), orchestrated together
with this module's publish decision by `temporal_refresh_dispatch.py`'s
`execute_temporal_refresh_branch` -- this module's `publish_temporal_shard_
version` is that orchestrator's final step. Row-sourcing for the legacy/
in-repo branch remains injectable/pluggable (AC11's row-scan primitive is
not yet built), and callers are still expected to have read-back-verified
`new_version_path` beyond the build primitives' own count/spot-check before
calling this function.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager


def publish_temporal_shard_version(
    alias_manager: AliasManager,
    pointer_namespace: str,
    new_version_path: Path,
) -> None:
    """Publish new_version_path as pointer_namespace's current target.

    Create-if-absent-else-swap: if the pointer file does not yet exist,
    publish via `create_alias` (the first-ever publish of this quarter
    namespace). If it already exists, publish via the compare-and-swap
    `swap_alias`, using the CURRENTLY-read target as `old_target` so the
    swap is validated against the true current state.

    Args:
        alias_manager: AliasManager scoped to the aliases directory.
        pointer_namespace: Alias-prefixed pointer name, e.g.
            "{repo_alias}-temporal-{embedder_slug}-{quarter}".
        new_version_path: The freshly-built, read-back-verified version
            directory to publish.
    """
    if not alias_manager.alias_exists(pointer_namespace):
        alias_manager.create_alias(pointer_namespace, str(new_version_path))
        return

    current_target = alias_manager.read_alias(pointer_namespace)
    alias_manager.swap_alias(pointer_namespace, str(new_version_path), current_target)
