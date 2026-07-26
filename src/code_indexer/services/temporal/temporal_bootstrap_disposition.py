"""classify_bootstrap_disposition() -- AC11's discovery/decision classifier
(Story #1457 AC11, Finding 6).

A pure, side-effect-free, lock-free per-namespace disposition classifier
for AC11's one-time proactive bootstrap. AC11 itself remains structurally
blocked on Story #1458 (its own spec text binds it to Story #1458's
fleet-migration job, per-repo write lock, and in-process QueryTracker/
CleanupManager access) -- but this classification decision requires none
of that: it is safe to build and test now, and the eventual full AC11
sweep (once Story #1458's orchestration context exists) can call it
unchanged.

Reuses the SAME primitives AC6/AC8 already established (never a
duplicate check): AliasManager.alias_exists() is the create_alias
resume-idempotency discriminant (round-8 N2), and
temporal_shard_has_committed_rows() is the Row-Existence-Not-Queryability
scan (round-10 Change 1) -- never an hnsw_index.bin-presence check.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)


class BootstrapDisposition(Enum):
    """AC11 Finding 6's four-way classification, collapsed to the three
    dispositions a single namespace's decision resolves to (the fourth,
    "NEEDS_BOOTSTRAP for the quarter-less monolith shape", is the SAME
    NEEDS_BOOTSTRAP value -- the monolith/quarter-shard distinction is a
    namespace-naming concern, not a disposition concern)."""

    #: alias_exists(namespace) is True -- a prior bootstrap already
    #: succeeded. Do NOT rebuild or republish; cleanup-only (verify/ensure
    #: the old in-repo tree is deleted).
    ALREADY_PUBLISHED = "already_published"

    #: No alias pointer exists, but the legacy in-repo shard holds at
    #: least one committed row. Build fresh, read-back verify,
    #: create_alias, then delete the in-repo tree.
    NEEDS_BOOTSTRAP = "needs_bootstrap"

    #: No alias pointer exists, and the legacy in-repo shard holds ZERO
    #: committed rows (a failed/empty prior indexing attempt that left
    #: directory structure behind). Directly removed (rmtree) -- nothing
    #: to migrate, nothing to lose.
    EMPTY_ARTIFACT = "empty_artifact"


def classify_bootstrap_disposition(
    alias_manager: AliasManager,
    pointer_namespace: str,
    legacy_shard_dir: Path,
) -> BootstrapDisposition:
    """Classify ONE (embedder, quarter) namespace's AC11 bootstrap
    disposition.

    Args:
        alias_manager: AliasManager scoped to the sister location's
            aliases directory (the SAME instance AC6/AC8 use).
        pointer_namespace: The alias-prefixed pointer name, e.g.
            "{repo_alias}-temporal-{embedder_slug}[-{quarter}]".
        legacy_shard_dir: The in-repo legacy shard directory for this
            namespace (may not exist on disk -- that is a valid input,
            not an error, when the alias already exists).

    Returns:
        The BootstrapDisposition. alias_exists() is checked FIRST
        (round-8 N2 resume-idempotency discriminant) -- a namespace with
        an existing pointer is ALWAYS classified ALREADY_PUBLISHED,
        regardless of local row state.

    Raises:
        ValueError: if alias_manager is None, pointer_namespace is
            None/empty, or legacy_shard_dir is None.
    """
    if alias_manager is None:
        raise ValueError("classify_bootstrap_disposition: alias_manager is required")
    if not pointer_namespace:
        raise ValueError(
            "classify_bootstrap_disposition: pointer_namespace is required"
        )
    if legacy_shard_dir is None:
        raise ValueError("classify_bootstrap_disposition: legacy_shard_dir is required")

    if alias_manager.alias_exists(pointer_namespace):
        return BootstrapDisposition.ALREADY_PUBLISHED

    if temporal_shard_has_committed_rows(legacy_shard_dir):
        return BootstrapDisposition.NEEDS_BOOTSTRAP

    return BootstrapDisposition.EMPTY_ARTIFACT
