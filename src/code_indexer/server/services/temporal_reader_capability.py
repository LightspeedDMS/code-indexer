"""all_serving_nodes_reader_capable() -- Epic #1454 Story #1461 salvage
item #3 [MED, latent] gap closure.

Gap this closes: Story #1460's fleet-rollout safety gate
(fleet_migration_config.enabled / deletion_authorized) gates only the
DESTRUCTIVE deletion primitives migration/bootstrap use (semantic legacy
chunk cleanup, temporal in-repo-tree reclaim). It never gated ordinary NEW
writes -- fresh chunks.db collection creation (Story #1456) and Story
#1457 AC6's sister-location publication of brand-new temporal quarters
(temporal_relocation_trigger.py's maybe_relocate_shard_to_sister_location,
gated only by CIDX_SERVER_REFRESH_CONTEXT + the
temporal_sister_relocation_enabled operator toggle). During a rolling
deploy, an old (not-yet-upgraded) node could encounter a chunks.db-only
collection or a sister-only quarter it cannot resolve/read. Latent, not
live, today: sister publication is purely additive (the in-repo copy
survives; deletion is separately Story #1460-gated) and chunks.db writes
are currently inert in production (no call site enables them yet).

This module is deliberately LIGHTWEIGHT -- NOT Bug #1361's per-collection
epoch/generation machinery. It reuses the per-node ``server_version``
field the existing NodeMetricsWriterService already records into the
``node_metrics`` table on every write tick (the same data the admin
dashboard's health carousel already renders via
NodeMetricsBackend.get_latest_per_node()) to answer one question: "does
every node currently serving this fleet report a version at or above the
release that first shipped the dual-layout chunks.db resolver (Story
#1456) and the sister-location temporal shard resolver (Story #1457)?"

Call sites resolve ``storage_mode`` themselves (from their own
ServerConfig, the bootstrap-only field also used for the
CIDX_TEMPORAL_PG_BOOTSTRAP_DIR_ENV decision) and pass it in explicitly --
this module never queries the config service itself, so its result is a
pure function of its two inputs and is trivial to unit test without
relying on global config-service singleton state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from packaging.version import parse as parse_version

from code_indexer.server.utils.registry_factory import resolve_backend_registry_attr

logger = logging.getLogger(__name__)

#: v11.80.0 is the release that shipped both Story #1456 (SQLite chunks.db
#: dual-layout resolver, resolve_chunk_layout()) and Story #1457 (the
#: sister-location TemporalShardResolver) -- the two reader capabilities
#: this gate exists to protect. Confirmed via git history: both stories'
#: commits (195b1f4f, 3dbaafe3, bbf7b03d) landed between the 11.79.0 and
#: 11.80.0 version-bump commits on the development branch.
MIN_DUAL_LAYOUT_READER_VERSION = "11.80.0"


def all_serving_nodes_reader_capable(min_version: str, storage_mode: str) -> bool:
    """Return True iff every node currently serving this fleet can read
    data written by a node running >= min_version, or the fleet is
    provably a single node.

    Args:
        min_version: Dotted version string (e.g. "11.80.0") -- the lowest
            server_version considered reader-capable.
        storage_mode: The caller's own ServerConfig.storage_mode ("sqlite"
            or "postgres"). Any value other than "postgres" is treated as
            standalone/solo, mirroring this codebase's existing binary
            "postgres vs everything else is standalone" convention (e.g.
            resolve_backend_registry_attr's own storage_mode check) --
            not a new invented default. This also covers the caller-side
            "storage_mode unknown" case (an empty string passed through
            when the caller's own ServerConfig was unavailable).

    Returns:
        True when storage_mode != "postgres" (solo -- no fleet-version-
        skew hazard exists), or when every node reported by
        node_metrics.get_latest_per_node() has a parseable server_version
        >= min_version. False in every other case: the node_metrics
        backend is unavailable, the query raises, the snapshot list is
        empty, a node's server_version is missing/blank/unparseable, or
        any single node's version is below min_version. This is the
        deliberately conservative direction -- False only ever WITHHOLDS
        the calling feature (sister-location publication), it never
        disables anything already running, and it composes as an AND with
        an already-default-OFF operator toggle, so a false negative
        changes nothing for a deployment that has not opted in.

    Never raises.
    """
    if storage_mode != "postgres":
        return True

    backend, _postgres_mode_without_backend = resolve_backend_registry_attr(
        "node_metrics", caller_name="all_serving_nodes_reader_capable"
    )
    if backend is None:
        logger.warning(
            "all_serving_nodes_reader_capable: node_metrics backend "
            "unavailable in postgres mode -- fail-safe: not capable."
        )
        return False

    try:
        snapshots: List[Dict[str, Any]] = backend.get_latest_per_node()
    except Exception as exc:
        logger.warning(
            "all_serving_nodes_reader_capable: get_latest_per_node() "
            "failed (fail-safe: not capable): %s: %s",
            type(exc).__name__,
            exc,
        )
        return False

    if not snapshots:
        logger.warning(
            "all_serving_nodes_reader_capable: node_metrics reported zero "
            "node snapshots in postgres mode -- fail-safe: not capable."
        )
        return False

    required = parse_version(min_version)
    for snapshot in snapshots:
        raw_version = snapshot.get("server_version") or ""
        node_id = snapshot.get("node_id")
        if not raw_version:
            logger.warning(
                "all_serving_nodes_reader_capable: node '%s' reports no "
                "server_version -- fail-safe: not capable.",
                node_id,
            )
            return False
        try:
            reported = parse_version(str(raw_version))
        except Exception as exc:
            logger.warning(
                "all_serving_nodes_reader_capable: node '%s' reports "
                "unparseable server_version %r (fail-safe: not capable): "
                "%s: %s",
                node_id,
                raw_version,
                type(exc).__name__,
                exc,
            )
            return False
        if reported < required:
            logger.warning(
                "all_serving_nodes_reader_capable: node '%s' reports "
                "server_version %s < required %s -- not capable.",
                node_id,
                raw_version,
                min_version,
            )
            return False

    return True
