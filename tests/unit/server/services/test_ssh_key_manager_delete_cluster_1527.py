"""
Bug #1527: SSHKeyManager.delete_key must be cluster-aware.

Third sibling of Bug #1524 (list path) and Bug #1526 (get_public_key /
assign_key_to_host).  Unlike those two read-path false negatives, this one sits
on the DELETE/write path: `delete_key()` consulted ONLY `self._sqlite_backend`
(node-local) and, on a miss, went straight to `_has_untracked_conflicting_file()`
-- never `self._pg_backend`.  A genuinely cluster-managed key (tracked in the
shared backend, correctly reported `managed` since #1524) therefore hit the Bug
#1519 provenance refusal on every node that had not itself created it.

Live consequence (clustered staging, v11.107.0): deleting one real managed key
through HAProxy round-robin succeeded on 2 of 6 attempts and was refused on the
other 4 with "an untracked file of that name exists ... and this server has no
record of creating it".

The safety constraint this fix must NOT weaken: when the shared backend ALSO has
no record of the name, the #1519/#1521 refusal is correct and must fire exactly
as before.  `TestForeignFileRefusalPreserved`, `TestUntrustedClusterRowRefused`
and `TestSoloModeDeleteUnchanged` are that regression guard and are as
load-bearing here as the fix's own tests.

Topology: two genuinely independent "nodes" -- separate ssh dirs, metadata dirs
and node-local SQLite databases -- sharing ONE stateful SSH keys backend, with
node B materialized by the REAL SSHKeySyncService (exactly what happens on every
cluster node at startup).  Nothing about the code under test is mocked.

Typing note: `pg_backend` / `fernet` are annotated `Any`, matching the
production contract -- `SSHKeyManager.__init__` declares them `Optional[Any]`
and the SSH keys backend is duck-typed (a structural protocol satisfied by
SSHKeysPostgresBackend, SSHKeysSqliteBackend and FakeSSHKeysBackend alike).

Discarded-return note: the backends' `create_key(...)` are commands returning
None, so every such call is followed by a `get_key(...)` read proving the write
landed; filesystem writes go through `write_ssh_file(...)`, which verifies
`Path.write_text`'s byte count against the content read back from disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Tuple
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import SSHKeyManager
from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService
from tests.unit.server.services.test_ssh_key_manager_cluster import (
    FakeKeyGenerator,
    FakeSSHKeysBackend,
    _make_manager,
)

NodeFactory = Callable[..., SSHKeyManager]

CLUSTER_KEY_NAME = "GitLab"


def write_ssh_file(path: Path, content: str) -> Path:
    """Write a file into an ssh dir and prove it landed."""
    written = path.write_text(content)
    assert written == len(content)
    assert path.read_text() == content
    return path


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


def materialize_on_node(manager: SSHKeyManager, backend: Any, key_fernet: Any) -> None:
    """Run the REAL sync service to write the backend's keys into this node's ssh dir."""
    sync_service = SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(manager.ssh_dir),
        fernet=key_fernet,
    )
    assert sync_service.sync()["errors"] == [], "precondition: sync must succeed"


@pytest.fixture
def synced_cluster_pair(
    node_factory: NodeFactory, shared_backend: FakeSSHKeysBackend, fernet: Fernet
) -> Tuple[SSHKeyManager, SSHKeyManager]:
    """Two nodes on one backend: node A created the key, node B only synced it.

    The exact staging state: node B holds the materialized files plus the shared
    backend row, and NO node-local record -- asserted as a precondition, since it
    is the entire premise of every test below.
    """
    node_a = node_factory("node_a", shared_backend, fernet)
    created = node_a.create_key(name=CLUSTER_KEY_NAME, key_type="ed25519")
    assert created.name == CLUSTER_KEY_NAME, "precondition: key created on node A"
    assigned = node_a.assign_key_to_host(CLUSTER_KEY_NAME, "gitlab.com")
    assert assigned.hosts == ["gitlab.com"], "precondition: host assignment recorded"

    node_b = node_factory("node_b", shared_backend, fernet)
    materialize_on_node(node_b, shared_backend, fernet)
    assert (node_b.ssh_dir / CLUSTER_KEY_NAME).exists(), (
        "precondition: sync must have written the private key onto node B"
    )
    assert node_b._sqlite_backend is not None
    assert node_b._sqlite_backend.get_key(CLUSTER_KEY_NAME) is None, (
        "precondition: node B must NOT know the key node-locally"
    )

    return node_a, node_b


class TestClusterManagedKeyDeletableFromAnyNode:
    """The reproduction of the reported defect."""

    def test_delete_on_non_creating_node_succeeds(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """Pre-fix this returns False on node B -- the exact staging refusal."""
        _, node_b = synced_cluster_pair

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True

    def test_delete_on_non_creating_node_removes_local_files(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """The node's own materialized copy must actually be gone from disk."""
        _, node_b = synced_cluster_pair

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True
        assert not (node_b.ssh_dir / CLUSTER_KEY_NAME).exists()
        assert not (node_b.ssh_dir / f"{CLUSTER_KEY_NAME}.pub").exists()

    def test_delete_on_non_creating_node_removes_shared_backend_row(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """Without the PG row gone the key resurrects on the next sync()."""
        _, node_b = synced_cluster_pair
        backend = node_b._pg_backend
        assert backend is not None
        assert backend.get_key(CLUSTER_KEY_NAME) is not None

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True

        assert backend.get_key(CLUSTER_KEY_NAME) is None


class TestClusterDeletePropagatesFleetWide:
    """A delete accepted on one node must be cluster-wide, not node-local."""

    def test_key_disappears_from_the_deleting_nodes_list_keys(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """Cross-checked through #1524's now-cluster-aware list view.

        Scope note: the CREATING node's own node-local SQLite row survives, and
        deliberately so -- no node can reach into another node's node-local
        store, and #1524's merge only ever ADDS shared-backend names to a node's
        local view.  Cluster truth (asserted here) is what every node without a
        local row answers from, and what SSHKeySyncService materializes from.
        """
        node_a, node_b = synced_cluster_pair
        assert [k.name for k in node_a.list_keys().managed] == [CLUSTER_KEY_NAME]
        backend = node_b._pg_backend
        assert backend is not None

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True

        assert [k.name for k in node_b.list_keys().managed] == []
        assert backend.list_keys() == []

    def test_delete_is_not_resurrected_by_a_subsequent_sync(
        self,
        synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager],
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """End-to-end: the REAL sync service must not rewrite the deleted key."""
        _, node_b = synced_cluster_pair

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True
        materialize_on_node(node_b, shared_backend, fernet)

        assert not (node_b.ssh_dir / CLUSTER_KEY_NAME).exists()

    def test_delete_succeeds_when_node_never_materialized_the_files(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """A node that never ran sync() has no file to unlink -- still not a refusal."""
        node_a = node_factory("node_a", shared_backend, fernet)
        assert node_a.create_key(name=CLUSTER_KEY_NAME).name == CLUSTER_KEY_NAME

        node_c = node_factory("node_c", shared_backend, fernet)
        assert not (node_c.ssh_dir / CLUSTER_KEY_NAME).exists()

        assert node_c.delete_key(CLUSTER_KEY_NAME) is True
        assert shared_backend.get_key(CLUSTER_KEY_NAME) is None


class TestJsonMetadataModeDeleteIsClusterAware:
    """The JSON metadata_dir storage mode must consult the shared backend too."""

    def test_node_without_local_json_metadata_can_delete(
        self, tmp_path: Path, shared_backend: FakeSSHKeysBackend, fernet: Fernet
    ) -> None:
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

        shared_backend.create_key(
            name=CLUSTER_KEY_NAME,
            fingerprint="SHA256:clusterfp",
            key_type="ed25519",
            private_path=f"/home/other-node/.ssh/{CLUSTER_KEY_NAME}",
            public_path=f"/home/other-node/.ssh/{CLUSTER_KEY_NAME}.pub",
            public_key="ssh-ed25519 AAAA cluster",
            private_key=fernet.encrypt(b"PRIVATE").decode(),
        )
        row = shared_backend.get_key(CLUSTER_KEY_NAME)
        assert row is not None and row["name"] == CLUSTER_KEY_NAME
        materialize_on_node(node, shared_backend, fernet)
        assert (ssh_dir / CLUSTER_KEY_NAME).exists()

        assert node.delete_key(CLUSTER_KEY_NAME) is True
        assert not (ssh_dir / CLUSTER_KEY_NAME).exists()
        assert not (ssh_dir / f"{CLUSTER_KEY_NAME}.pub").exists()
        # Local files alone are not enough: a surviving shared row would
        # resurrect the key on this node's next sync().
        assert shared_backend.get_key(CLUSTER_KEY_NAME) is None


class TestForeignFileRefusalPreserved:
    """
    Bug #1519 / #1521 regression guard -- the critical constraint on this fix.

    A same-named file that NEITHER node-local state NOR the shared backend has
    any record of is genuinely foreign, and the provenance refusal must fire
    exactly as it did before delete_key learned to consult the cluster.
    """

    def test_untracked_file_with_no_cluster_record_is_still_refused(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        node = node_factory("guard_node", shared_backend, fernet)
        private = write_ssh_file(node.ssh_dir / "id_personal", "PERSONAL_PRIVATE")
        public = write_ssh_file(
            node.ssh_dir / "id_personal.pub", "ssh-ed25519 AAAA personal"
        )

        assert node.delete_key("id_personal") is False

        assert private.read_text() == "PERSONAL_PRIVATE"
        assert public.read_text() == "ssh-ed25519 AAAA personal"

    def test_refusal_holds_while_an_unrelated_key_is_cluster_managed(
        self,
        synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager],
    ) -> None:
        """
        Discriminating case: a populated shared backend must not blanket-authorize
        deletion.  Node B holds one cluster-managed key AND one foreign personal
        key; only the former may be deleted.
        """
        _, node_b = synced_cluster_pair
        foreign = write_ssh_file(node_b.ssh_dir / "id_personal", "PERSONAL_PRIVATE")

        assert node_b.delete_key("id_personal") is False
        assert foreign.read_text() == "PERSONAL_PRIVATE"

        assert node_b.delete_key(CLUSTER_KEY_NAME) is True


class TestUntrustedClusterRowRefused:
    """A shared-backend row is untrusted input, not a deletion authorization."""

    def test_untrusted_cluster_key_name_is_refused(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
        tmp_path: Path,
    ) -> None:
        """A name that is not a bare filename must never delete outside ssh_dir."""
        node = node_factory("traversal_node", shared_backend, fernet)
        outside = write_ssh_file(tmp_path / "victim", "VICTIM")

        shared_backend.create_key(
            name="../victim",
            fingerprint="SHA256:evil",
            key_type="ed25519",
            private_path=str(outside),
            public_path=f"{outside}.pub",
            public_key="ssh-ed25519 AAAA evil",
            private_key=fernet.encrypt(b"PRIVATE").decode(),
        )
        row = shared_backend.get_key("../victim")
        assert row is not None and row["name"] == "../victim"

        assert node.delete_key("../victim") is False
        assert outside.read_text() == "VICTIM"

    def test_truly_nonexistent_key_remains_an_idempotent_no_op(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """No record anywhere AND no file on disk -- a silent success, as before."""
        node = node_factory("noop_node", shared_backend, fernet)

        assert node.delete_key("never_existed") is True


class TestSoloModeDeleteUnchanged:
    """No `_pg_backend` => byte-identical pre-#1527 behavior."""

    def test_untracked_file_still_refused(self, solo_node: NodeFactory) -> None:
        node = solo_node("solo_guard")
        assert node._pg_backend is None
        private = write_ssh_file(node.ssh_dir / "id_personal", "PERSONAL_PRIVATE")

        assert node.delete_key("id_personal") is False
        assert private.read_text() == "PERSONAL_PRIVATE"

    def test_tracked_key_still_deleted(self, solo_node: NodeFactory) -> None:
        node = solo_node("solo_tracked")
        created = node.create_key(name="managed_key", key_type="ed25519")
        assert created.name == "managed_key"

        assert node.delete_key("managed_key") is True
        assert not (node.ssh_dir / "managed_key").exists()

    def test_nonexistent_key_still_idempotent(self, solo_node: NodeFactory) -> None:
        node = solo_node("solo_noop")

        assert node.delete_key("never_existed") is True


class TestBackendReadFailurePolicy:
    """
    Failure policy of the NEW shared-backend read this fix adds to delete_key.

    Mirrors create_key / assign_key_to_host / the #1524 list path: a backend
    failure is logged and re-raised, never masked -- silently degrading to the
    node-local view would reintroduce the exact false refusal being fixed.
    """

    def test_backend_read_failure_is_raised_not_swallowed(
        self, node_factory: NodeFactory, fernet: Fernet
    ) -> None:
        pg_backend = MagicMock()
        pg_backend.get_key.side_effect = RuntimeError("PG get_key failed")

        node = node_factory("failing_node", pg_backend, fernet)
        write_ssh_file(node.ssh_dir / "id_personal", "PERSONAL_PRIVATE")

        with pytest.raises(RuntimeError, match="PG get_key failed"):
            node.delete_key("id_personal")
