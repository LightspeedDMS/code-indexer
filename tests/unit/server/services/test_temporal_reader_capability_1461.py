"""Epic #1454 Story #1461 salvage item #3 [MED, latent].

Gap: Story #1460's rollout gate only protects DELETION (migration/bootstrap
cleanup). It never gated ordinary NEW writes -- fresh chunks.db collection
creation (#1456) and Story #1457 AC6's sister-location publication of
brand-new temporal quarters. During a rolling deploy, an old (not-yet-
upgraded) node could encounter a chunks.db-only collection or a sister-only
quarter it cannot read.

`all_serving_nodes_reader_capable(min_version, storage_mode)` is a
lightweight helper (deliberately NOT #1361's epoch machinery) that reuses
the per-node `server_version` already recorded in the `node_metrics` table
(the same NodeMetricsWriterService / get_latest_per_node() mechanism the
admin dashboard already reads) to answer: "can every node currently
serving this fleet read data written by a node running >= min_version?"

Contract (documented here because it drives every test below):
  - storage_mode != "postgres" (solo/standalone) -> True, unconditionally.
    A solo server IS the whole fleet by definition -- there is no
    version-skew hazard to protect against. This mirrors the rest of the
    codebase's own binary "postgres vs everything else is standalone"
    convention (e.g. resolve_backend_registry_attr's own storage_mode
    check), not a new invented default.
  - storage_mode == "postgres": every OTHER outcome (node_metrics backend
    unavailable, get_latest_per_node() raising, an empty snapshot list, a
    missing/blank server_version, an unparseable server_version, or any
    single node reporting a version below min_version) resolves to
    False. This is the deliberately conservative direction: a False
    result only ever WITHHOLDS the calling feature (temporal
    sister-location publication), it never disables anything already
    running, and it composes as an AND with an already-default-OFF
    operator toggle (temporal_sister_relocation_enabled) -- so a false
    negative changes nothing for the overwhelming majority of
    deployments that have not opted in yet.
  - This function never raises.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.server.services.temporal_reader_capability import (
    MIN_DUAL_LAYOUT_READER_VERSION,
    all_serving_nodes_reader_capable,
)

_MODULE = "code_indexer.server.services.temporal_reader_capability"


def _backend_returning(snapshots):
    backend = MagicMock()
    backend.get_latest_per_node.return_value = snapshots
    return backend


def test_non_postgres_storage_mode_returns_true():
    """Solo/standalone (storage_mode='sqlite') is trivially reader-capable
    -- the node_metrics backend must never even be consulted."""
    with patch(f"{_MODULE}.resolve_backend_registry_attr") as mock_resolve:
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="sqlite"
        )

    assert result is True
    mock_resolve.assert_not_called()


def test_empty_storage_mode_treated_as_standalone_returns_true():
    """An empty storage_mode string (caller had no ServerConfig to read,
    e.g. build_temporal_child_env's server_config=None case) must resolve
    the same as any other non-'postgres' value."""
    with patch(f"{_MODULE}.resolve_backend_registry_attr") as mock_resolve:
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode=""
        )

    assert result is True
    mock_resolve.assert_not_called()


def test_postgres_all_nodes_at_or_above_min_version_returns_true():
    snapshots = [
        {"node_id": "node-1", "server_version": "11.80.0"},
        {"node_id": "node-2", "server_version": "11.81.2"},
    ]
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning(snapshots), False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is True


def test_postgres_one_node_below_min_version_returns_false():
    snapshots = [
        {"node_id": "node-1", "server_version": "11.80.0"},
        {"node_id": "node-2", "server_version": "11.79.0"},
    ]
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning(snapshots), False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False


def test_postgres_exact_min_version_match_returns_true():
    """>= min_version, not strictly greater -- a node reporting exactly
    the min version is capable."""
    snapshots = [{"node_id": "node-1", "server_version": "11.80.0"}]
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning(snapshots), False),
    ):
        result = all_serving_nodes_reader_capable("11.80.0", storage_mode="postgres")

    assert result is True


def test_postgres_missing_server_version_returns_false():
    snapshots = [{"node_id": "node-1", "server_version": ""}]
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning(snapshots), False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False


def test_postgres_unparseable_server_version_returns_false():
    snapshots = [{"node_id": "node-1", "server_version": "not-a-version!!"}]
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning(snapshots), False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False


def test_postgres_empty_snapshot_list_returns_false():
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(_backend_returning([]), False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False


def test_postgres_backend_unavailable_returns_false():
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(None, True),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False


def test_postgres_query_exception_returns_false_not_raises():
    backend = MagicMock()
    backend.get_latest_per_node.side_effect = RuntimeError("db unavailable")
    with patch(
        f"{_MODULE}.resolve_backend_registry_attr",
        return_value=(backend, False),
    ):
        result = all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, storage_mode="postgres"
        )

    assert result is False
