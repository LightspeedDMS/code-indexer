"""Regression test for the cluster node-id resolution mismatch discovered via
a real e2e-automation.sh Phase 6 (PostgreSQL Parity) run.

Root cause (evidence-based, traced through the actual source):

``service_init.py`` constructs ``JobTracker(node_id=_node_id)`` -- the value
stamped onto every job row's ``executing_node`` column at creation time
(``register_job_if_no_conflict`` / ``_atomic_insert_impl``, default
``stamp_executing_node=True``). When ``config.json`` has no ``cluster``
section (a legitimate, supported single-node ``storage_mode=postgres``
deployment -- exactly what the Phase 6 E2E harness's
``write_pg_bootstrap_config`` writes), the OLD code defaulted this to the
literal string ``"local"``.

``lifespan.py``'s cluster-services block separately constructs
``NodeHeartbeatService(node_id=_node_id)`` (which is what populates
``cluster_nodes`` / ``get_active_nodes()``) and ``DistributedJobClaimer``.
When ``cluster.node_id`` is absent there too, it defaults to
``f"{hostname}-cidx"`` -- a DIFFERENT string than ``"local"``.

Because the two defaults disagree, a ``lifecycle_registration`` job
(``GoldenRepoManager._register_lifecycle_after_registration``, which
transitions its own job row to ``status='running'`` via
``JobTracker.update_status`` while executing synchronously in-process, and
NEVER sets ``claimed_at``) gets its ``executing_node`` stamped as
``"local"`` -- a value that is NEVER a member of
``NodeHeartbeatService.get_active_nodes()`` (which only ever contains
``f"{hostname}-cidx"``-shaped identities). ``JobReconciliationService.
_reclaim_dead_node_jobs``'s grace-period guard
(``claimed_at IS NULL OR claimed_at < NOW() - grace``) is unconditionally
satisfied when ``claimed_at`` is NULL, so the job is reclaimed to
``pending`` on the very next 5-second sweep -- even though the node is
alive and the job is genuinely, correctly still running.
``DistributedJobWorkerService`` then claims the now-pending row and
immediately fails it because ``lifecycle_registration`` is (correctly, for
genuinely-abandoned jobs) not in ``RETRYABLE_JOB_TYPES``.

The fix is a SINGLE shared resolver, ``resolve_cluster_node_id()``, used by
BOTH ``service_init.py`` and ``lifespan.py``'s cluster-services block, so
the two call sites can never diverge again.
"""

from __future__ import annotations

import socket

import pytest
from typing import Any, Dict, List, Optional

from code_indexer.server.utils.cluster_node_id import resolve_cluster_node_id


class TestResolveClusterNodeId:
    def test_explicit_configured_node_id_wins(self) -> None:
        raw_config = {"cluster": {"node_id": "my-explicit-node"}}
        assert resolve_cluster_node_id(raw_config) == "my-explicit-node"

    def test_missing_cluster_key_falls_back_to_hostname_cidx(self) -> None:
        raw_config = {"storage_mode": "postgres", "postgres_dsn": "postgresql:///x"}
        expected = f"{socket.gethostname()}-cidx"
        assert resolve_cluster_node_id(raw_config) == expected

    def test_empty_raw_config_falls_back_to_hostname_cidx(self) -> None:
        expected = f"{socket.gethostname()}-cidx"
        assert resolve_cluster_node_id({}) == expected
        assert resolve_cluster_node_id(None) == expected

    def test_empty_string_node_id_falls_back_to_hostname_cidx(self) -> None:
        raw_config = {"cluster": {"node_id": ""}}
        expected = f"{socket.gethostname()}-cidx"
        assert resolve_cluster_node_id(raw_config) == expected

    def test_non_dict_cluster_value_falls_back_to_hostname_cidx(self) -> None:
        raw_config = {"cluster": "not-a-dict"}
        expected = f"{socket.gethostname()}-cidx"
        assert resolve_cluster_node_id(raw_config) == expected

    def test_never_returns_the_literal_string_local(self) -> None:
        """Regression guard for the exact divergent default that caused the
        reclaim bug: this resolver must never silently produce "local" as a
        fallback, since that string can never appear in
        NodeHeartbeatService.get_active_nodes()."""
        configs: List[Optional[Dict[str, Any]]] = [
            None,
            {},
            {"cluster": {}},
            {"cluster": {"node_id": ""}},
        ]
        for raw_config in configs:
            assert resolve_cluster_node_id(raw_config) != "local"


# ── configured_node_id: the explicit value or "" -- never a crash ────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "not a dict",
        {},
        {"cluster": None},  # what a standalone server writes
        {"cluster": []},
        {"cluster": {}},
        {"cluster": {"node_id": None}},
        {"cluster": {"node_id": 7}},
        {"cluster": {"node_id": ""}},
    ],
)
def test_configured_node_id_is_empty_for_every_unconfigured_shape(raw) -> None:
    from code_indexer.server.utils.cluster_node_id import configured_node_id

    assert configured_node_id(raw) == ""


def test_configured_node_id_returns_the_explicit_value() -> None:
    from code_indexer.server.utils.cluster_node_id import configured_node_id

    assert configured_node_id({"cluster": {"node_id": "cidx-3"}}) == "cidx-3"


def test_the_dep_map_sentinel_node_id_survives_a_null_cluster(
    tmp_path, monkeypatch, caplog
) -> None:
    """``"cluster": null`` used to raise inside _get_node_id -- swallowed, so the
    value was right only by accident. It must now be READ, not swallowed, and
    the fallback must stay the bare hostname the startup sentinel sweep
    expects, so locks written before and after this fix still match."""
    import json
    import logging
    import socket

    from code_indexer.server.services.dependency_map_service import (
        DependencyMapService,
    )

    (tmp_path / ".cidx-server").mkdir()
    (tmp_path / ".cidx-server" / "config.json").write_text(
        json.dumps({"cluster": None})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    service = DependencyMapService.__new__(DependencyMapService)

    with caplog.at_level(logging.DEBUG):
        assert service._get_node_id() == socket.gethostname()
    assert not [r for r in caplog.records if "could not read" in r.getMessage()]

    (tmp_path / ".cidx-server" / "config.json").write_text(
        json.dumps({"cluster": {"node_id": "cidx-2"}})
    )
    assert service._get_node_id() == "cidx-2"
