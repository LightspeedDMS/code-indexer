"""Shared cluster topology for the Bug #1526 tests.

Every module in this package needs the same two-node cluster: two genuinely
independent SSHKeyManager "nodes" (separate ssh dirs, metadata dirs and
node-local SQLite databases) sharing ONE stateful SSH keys backend, with the
second node materialized by the REAL SSHKeySyncService -- exactly what happens
on every cluster node at startup.  Nothing about the code under test is mocked.

Cluster deps are always supplied per instance (constructor params take
precedence over SSHKeyManager's class-level fallbacks) and the solo factory
pins them to None on the INSTANCE, so nothing here mutates shared class state.

Typing note: `pg_backend` / `fernet` are annotated `Any`, matching the
production contract -- `SSHKeyManager.__init__` declares them `Optional[Any]`
and the SSH keys backend is duck-typed (a structural protocol satisfied by
SSHKeysPostgresBackend, SSHKeysSqliteBackend and FakeSSHKeysBackend alike).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Tuple

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import SSHKeyManager
from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService
from tests.unit.server.services.test_ssh_key_manager_cluster import _make_manager
from tests.unit.server.services.ssh_key_cluster_1526._support import (
    FakeSSHKeysBackend,
)

NodeFactory = Callable[..., SSHKeyManager]


@pytest.fixture
def fernet() -> Fernet:
    """A real Fernet instance -- cluster mode encrypts private keys with one."""
    return Fernet(Fernet.generate_key())


@pytest.fixture
def shared_backend() -> FakeSSHKeysBackend:
    """One stateful SSH keys backend, shared by every node (production topology)."""
    return FakeSSHKeysBackend()


@pytest.fixture
def node_factory(tmp_path: Path) -> NodeFactory:
    """Build a cluster 'node': its own ssh dir, metadata dir and SQLite db."""

    def _make_node(
        node_name: str, pg_backend: Any = None, key_fernet: Any = None
    ) -> SSHKeyManager:
        node_root = tmp_path / node_name
        node_root.mkdir(parents=True, exist_ok=True)
        return _make_manager(node_root, pg_backend=pg_backend, fernet=key_fernet)

    return _make_node


@pytest.fixture
def solo_node(node_factory: NodeFactory) -> NodeFactory:
    """Build a solo (non-cluster) node, isolating deps on the INSTANCE.

    Pinning `_pg_backend`/`_fernet` to None on the instance (rather than
    resetting SSHKeyManager's class attributes) means a solo case can neither be
    perturbed by, nor perturb, class-level state installed elsewhere.
    """

    def _make_solo(node_name: str) -> SSHKeyManager:
        node = node_factory(node_name)
        node._pg_backend = None
        node._fernet = None
        return node

    return _make_solo


@pytest.fixture
def synced_cluster_pair(
    node_factory: NodeFactory, shared_backend: FakeSSHKeysBackend, fernet: Fernet
) -> Tuple[SSHKeyManager, SSHKeyManager]:
    """Two nodes on one backend: node A created 'GitLab', node B synced it.

    The exact cluster state Bug #1524's staging reproduction observed: the
    creating node knows the key node-locally, every other node has only the
    synced files plus the shared backend row.  No host is assigned yet, so the
    assignment tests start from a clean host set.
    """
    node_a = node_factory("node_a", shared_backend, fernet)
    created = node_a.create_key(name="GitLab", key_type="ed25519")
    assert created.name == "GitLab", "precondition: key created on node A"

    node_b = node_factory("node_b", shared_backend, fernet)
    sync_service = SSHKeySyncService(
        ssh_keys_backend=shared_backend,
        ssh_dir=str(node_b.ssh_dir),
        fernet=fernet,
    )
    assert sync_service.sync()["errors"] == [], "precondition: sync must succeed"
    assert (node_b.ssh_dir / "GitLab.pub").exists(), (
        "precondition: sync must have written the public key onto node B"
    )
    assert node_b._sqlite_backend is not None
    assert node_b._sqlite_backend.get_key("GitLab") is None, (
        "precondition: node B must NOT know the key node-locally"
    )

    return node_a, node_b
