"""Bug #1526: `SSHKeyManager.get_public_key` must be cluster-aware.

Sibling of Bug #1524, which made only the list/classification path
(`_list_keys_internal`) consult the shared cluster backend.  `get_public_key`
was deliberately left out of that fix and still resolves `key_name` from
node-local state ONLY (node-local SQLite row, or a node-local JSON metadata
file), so it raises KeyNotFoundError for a key that IS cluster-managed -- and
which #1524's fixed `list_keys()` now reports as `managed` on this very node.
The same node therefore answers "this key is managed" and "this key does not
exist" about the same key, at the same instant.

The cluster topology (two independent nodes, one shared backend, node B
materialized by the REAL SSHKeySyncService) comes from this package's conftest.
Invariants shared with `assign_key_to_host` -- unknown name, untrusted cluster
key name, backend read failure, solo mode -- live in
`test_cluster_lookup_guards_1526.py`, parametrized over both operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import (
    PublicKeyNotFoundError,
    SSHKeyManager,
)
from tests.unit.server.services.test_ssh_key_manager_cluster import (
    SENTINEL_PUBLIC_KEY,
    FakeKeyGenerator,
)
from tests.unit.server.services.ssh_key_cluster_1526._support import (
    CLUSTER_PUBLIC_KEY,
    FakeSSHKeysBackend,
    seed_cluster_only_key,
    write_text_verified,
)
from tests.unit.server.services.ssh_key_cluster_1526.conftest import NodeFactory

NODE_B_PUBLIC_KEY = "ssh-ed25519 AAAAnodeB nodeb-materialized"


class TestSyncedNodeResolvesPublicKey:
    """Reproduction: the key was created elsewhere and synced onto this node."""

    def test_both_nodes_return_the_public_key(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """
        Pre-fix this raises KeyNotFoundError on node B: its node-local SQLite has
        no row, even though the synced `GitLab.pub` sits in its own ssh dir.
        """
        node_a, node_b = synced_cluster_pair

        assert node_a.get_public_key("GitLab") == SENTINEL_PUBLIC_KEY
        assert node_b.get_public_key("GitLab") == SENTINEL_PUBLIC_KEY

    def test_reads_this_nodes_own_materialized_file(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """
        The answer must come from THIS node's file, not from the path recorded by
        whichever node originally created the key -- the same rebasing convention
        #1524 established for the list path.
        """
        _, node_b = synced_cluster_pair
        written = write_text_verified(
            node_b.ssh_dir / "GitLab.pub", NODE_B_PUBLIC_KEY + "\n"
        )

        assert node_b.get_public_key("GitLab") == written.strip()
        assert node_b.get_public_key("GitLab") == NODE_B_PUBLIC_KEY


class TestNotYetMaterializedKey:
    """A cluster-managed key whose files this node has not written yet.

    E.g. before SSHKeySyncService.sync() next runs, or on a node that never
    independently pulled the key down.  The shared backend row IS cluster truth
    for the public key, so it must be served rather than denied.
    """

    def test_returns_the_cluster_public_key(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        seed_cluster_only_key(shared_backend, fernet)
        node = node_factory("fresh_node", shared_backend, fernet)
        assert not (node.ssh_dir / "GitLab.pub").exists()

        assert node.get_public_key("GitLab") == CLUSTER_PUBLIC_KEY

    def test_json_metadata_mode_is_cluster_aware_too(
        self, tmp_path: Path, shared_backend: FakeSSHKeysBackend, fernet: Fernet
    ) -> None:
        """The JSON metadata_dir storage mode must consult cluster truth too."""
        node_root = tmp_path / "json_node"
        ssh_dir = node_root / "ssh"
        ssh_dir.mkdir(parents=True)
        metadata_dir = node_root / "meta"
        metadata_dir.mkdir(parents=True)

        node = SSHKeyManager(
            ssh_dir=ssh_dir,
            metadata_dir=metadata_dir,
            use_sqlite=False,
            pg_backend=shared_backend,
            fernet=fernet,
        )
        node.key_generator = FakeKeyGenerator(ssh_dir=ssh_dir)
        seed_cluster_only_key(shared_backend, fernet)

        assert node.get_public_key("GitLab") == CLUSTER_PUBLIC_KEY


class TestPublicMaterialMissing:
    """'The key exists but its public material is missing' is a DIFFERENT answer
    from 'no such key', and callers distinguish the two exceptions."""

    def test_cluster_key_without_public_material_raises_public_key_not_found(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        seed_cluster_only_key(shared_backend, fernet, public_key=None)
        node = node_factory("no_material_node", shared_backend, fernet)
        assert not (node.ssh_dir / "GitLab.pub").exists()

        with pytest.raises(PublicKeyNotFoundError):
            node.get_public_key("GitLab")

    def test_local_key_with_deleted_pub_file_raises_public_key_not_found(
        self, solo_node: NodeFactory
    ) -> None:
        """Regression guard: a locally-tracked key whose .pub vanished must keep
        raising PublicKeyNotFoundError, never become a cluster lookup."""
        node = solo_node("solo_no_file")
        created = node.create_key(name="solo_key", key_type="ed25519")
        assert created.name == "solo_key"
        (node.ssh_dir / "solo_key.pub").unlink()

        with pytest.raises(PublicKeyNotFoundError):
            node.get_public_key("solo_key")
