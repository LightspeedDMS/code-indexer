"""Bug #1526: `SSHKeyManager.assign_key_to_host` must be cluster-aware.

Sibling of `get_public_key` (same class, same defect): the key is resolved from
node-local state ONLY, so assigning a host to a genuinely cluster-managed key
raises KeyNotFoundError on every node that has not materialized it locally --
even though Bug #1524's fixed `list_keys()` reports that key as `managed` right
there.

The shared backend is what SSHKeySyncService reads to materialize Host blocks
fleet-wide (the concern Bug #1504 established for the locally-tracked path), so
these tests assert the real downstream artifact -- a regenerated ~/.ssh/config
with a Host block pointing at THIS node's own key file -- not merely that a call
was made.

The cluster topology (two independent nodes, one shared backend, node B
materialized by the REAL SSHKeySyncService) comes from this package's conftest.
Invariants shared with `get_public_key` -- unknown name, untrusted cluster key
name, backend read failure, solo mode -- live in
`test_cluster_lookup_guards_1526.py`, parametrized over both operations.
"""

from __future__ import annotations

from typing import Tuple

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import (
    HostConflictError,
    SSHKeyManager,
)
from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService
from tests.unit.server.services.ssh_key_cluster_1526._support import (
    ASSIGNED_HOST,
    FakeSSHKeysBackend,
    seed_cluster_only_key,
    write_text_verified,
)
from tests.unit.server.services.ssh_key_cluster_1526.conftest import NodeFactory

SECOND_HOST = "gitlab.internal"
USER_SECTION_CONFIG = f"Host {ASSIGNED_HOST}\n  HostName {ASSIGNED_HOST}\n"


class TestSyncedNodeAssignsHost:
    """Reproduction: assigning on a node that did not create the key.

    Pre-fix every method here raises KeyNotFoundError on node B.
    """

    def test_returns_metadata_carrying_the_new_host(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        _, node_b = synced_cluster_pair

        result = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)

        assert result.name == "GitLab"
        assert ASSIGNED_HOST in result.hosts

    def test_shared_backend_holds_the_host_row(
        self,
        synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager],
        shared_backend: FakeSSHKeysBackend,
    ) -> None:
        """The assignment must reach cluster truth -- the store every other node
        reads -- not merely this node's own state."""
        _, node_b = synced_cluster_pair

        result = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)
        assert ASSIGNED_HOST in result.hosts

        row = shared_backend.get_key("GitLab")
        assert row is not None
        assert row["hosts"] == [ASSIGNED_HOST]

    def test_returned_metadata_describes_this_nodes_files(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """Same rebasing convention #1524 established for the list path: the
        originating node's recorded paths are meaningless here."""
        node_a, node_b = synced_cluster_pair

        result = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)

        assert result.private_path == str(node_b.ssh_dir / "GitLab")
        assert result.public_path == str(node_b.ssh_dir / "GitLab.pub")
        assert result.private_path != str(node_a.ssh_dir / "GitLab")


class TestAssignmentReachesSshConfig:
    """The real downstream artifact: ssh must actually offer the identity."""

    def test_this_nodes_config_gains_the_host_block(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        _, node_b = synced_cluster_pair

        result = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)
        assert ASSIGNED_HOST in result.hosts

        config_text = node_b.config_path.read_text()
        assert f"Host {ASSIGNED_HOST}" in config_text
        assert result.private_path in config_text

    def test_sync_on_a_third_node_emits_the_host_block(
        self,
        synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager],
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """Fleet-wide proof: an assignment made on a node that knows the key only
        through cluster truth must still reach every other node's ~/.ssh/config
        via the REAL sync service -- pointing at THAT node's own key file, since
        the sync service rebases IdentityFile per node."""
        _, node_b = synced_cluster_pair
        assigned = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)
        assert ASSIGNED_HOST in assigned.hosts

        node_c = node_factory("node_c", shared_backend, fernet)
        sync_result = SSHKeySyncService(
            ssh_keys_backend=shared_backend,
            ssh_dir=str(node_c.ssh_dir),
            fernet=fernet,
        ).sync()
        assert sync_result["errors"] == []

        config_text = (node_c.ssh_dir / "config").read_text()
        assert f"Host {ASSIGNED_HOST}" in config_text
        assert str(node_c.ssh_dir / "GitLab") in config_text
        assert assigned.private_path not in config_text


class TestClusterOnlyKeyAssignment:
    """A cluster-managed key whose files this node has not written yet."""

    def test_not_yet_materialized_key_is_assignable(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """The case issue #1526 names explicitly: before SSHKeySyncService.sync()
        next runs, the key is real but absent from this node's disk."""
        seed_cluster_only_key(shared_backend, fernet)
        node = node_factory("fresh_node", shared_backend, fernet)
        assert not (node.ssh_dir / "GitLab").exists()

        result = node.assign_key_to_host("GitLab", ASSIGNED_HOST)

        assert result.hosts == [ASSIGNED_HOST]

    def test_second_assignment_accumulates_hosts(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """A second assignment must add to the host set, not replace it."""
        _, node_b = synced_cluster_pair

        first = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)
        assert first.hosts == [ASSIGNED_HOST]

        second = node_b.assign_key_to_host("GitLab", SECOND_HOST)
        assert sorted(second.hosts) == sorted([ASSIGNED_HOST, SECOND_HOST])


class TestUserSectionConflictGuard:
    """The cluster-resolved path must apply the SAME guard as the node-local
    paths: a cluster-managed key is not a licence to shadow a hand-written
    ~/.ssh/config entry."""

    def test_conflict_is_refused(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        _, node_b = synced_cluster_pair
        write_text_verified(node_b.config_path, USER_SECTION_CONFIG)

        with pytest.raises(HostConflictError):
            node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)

    def test_no_host_row_written_when_refused(
        self,
        synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager],
        shared_backend: FakeSSHKeysBackend,
    ) -> None:
        """A refusal must leave cluster truth untouched -- proving the guard runs
        BEFORE the backend mirror, not after it.  Otherwise the next sync would
        materialize the very Host block that was just rejected."""
        _, node_b = synced_cluster_pair
        write_text_verified(node_b.config_path, USER_SECTION_CONFIG)

        with pytest.raises(HostConflictError):
            node_b.assign_key_to_host("GitLab", ASSIGNED_HOST)

        row = shared_backend.get_key("GitLab")
        assert row is not None
        assert row["hosts"] == []

    def test_force_overrides_the_conflict(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        _, node_b = synced_cluster_pair
        write_text_verified(node_b.config_path, USER_SECTION_CONFIG)

        result = node_b.assign_key_to_host("GitLab", ASSIGNED_HOST, force=True)

        assert ASSIGNED_HOST in result.hosts
