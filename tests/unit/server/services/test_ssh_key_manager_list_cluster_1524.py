"""
Bug #1524: SSHKeyManager list path must be cluster-aware.

`SSHKeyManager._list_keys_internal()` classified keys as managed vs unmanaged
using ONLY node-local state (node-local SQLite, or a node-local JSON
metadata_dir) diffed against node-local filesystem discovery.  It never
consulted `self._pg_backend`, unlike this same class's create_key /
assign_key_to_host / delete_key methods, which all treat the shared
PostgreSQL backend as cluster truth.

Live consequence (clustered staging, v11.106.0, reproduced 4/4 via HAProxy
round-robin): the SAME key was reported `managed` on the node that created it
and `unmanaged` on every other node, at the same instant, for the same
cluster.

These tests construct two genuinely independent "nodes" -- separate ssh dirs,
separate metadata dirs, separate node-local SQLite databases -- that share ONE
stateful SSH keys backend, and materialize the key onto the second node using
the REAL SSHKeySyncService (exactly what happens on every cluster node at
startup).  No mocking of the code under test.

Two further classes cover the same change's own blast radius, not separate
concerns:
  * TestBackendReadFailurePolicy -- the failure policy of the NEW shared-backend
    read this fix introduces into the list path.
  * TestDeleteKeyProvenanceGuardUnweakened -- proof that Bug #1519's delete_key
    provenance refusal still holds, since delete_key lives in this same class
    and reaches the list path through _update_ssh_config().

Cluster deps are always supplied per instance (constructor params, which take
precedence over SSHKeyManager's class-level fallbacks), and the `solo_node`
fixture pins the solo cases' deps on the instance -- so nothing here mutates
shared class state.

Typing note: `pg_backend` / `fernet` are annotated `Any` here because that is
the production contract they must satisfy -- `SSHKeyManager.__init__` itself
declares them `Optional[Any]`.  The SSH keys backend is duck-typed (a
structural protocol implemented by SSHKeysPostgresBackend, SSHKeysSqliteBackend
and the in-memory FakeSSHKeysBackend alike, with no shared base class), and
`fernet` may be a real `Fernet` or None.  Narrowing these annotations here
would describe a contract the code under test does not actually enforce.

Discarded-return note: the storage backends' `create_key(...)` methods return
None by contract (they are commands, not queries) -- every such call below is
therefore followed by a `get_key(...)` read that asserts the write landed.
Filesystem writes go through `write_ssh_file(...)`, which asserts the content
reached disk rather than discarding `Path.write_text`'s byte count silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Tuple
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


def write_ssh_file(path: Path, content: str) -> Path:
    """Write a file into an ssh dir and prove it landed.

    Wraps `Path.write_text` so its byte-count return is verified against the
    content actually readable back from disk, instead of being discarded.
    """
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
    """Build a solo (non-cluster) node with instance-local dependency isolation.

    The cluster deps are pinned to None ON THE INSTANCE rather than resetting
    SSHKeyManager's class attributes, so a solo case can never be perturbed by
    -- nor perturb -- class-level state another test installed.
    """

    def _make_solo(node_name: str) -> SSHKeyManager:
        node = node_factory(node_name)
        node._pg_backend = None
        node._fernet = None
        return node

    return _make_solo


def materialize_on_node(
    manager: SSHKeyManager, backend: Any, key_fernet: Any
) -> Dict[str, Any]:
    """Run the REAL sync service to write the backend's keys into this node's ssh dir.

    Returns the sync result; callers assert it reported no errors, since a
    silent sync failure would invalidate whatever they test next.
    """
    sync_service = SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(manager.ssh_dir),
        fernet=key_fernet,
    )
    return sync_service.sync()


@pytest.fixture
def synced_cluster_pair(
    node_factory: NodeFactory, shared_backend: FakeSSHKeysBackend, fernet: Fernet
) -> Tuple[SSHKeyManager, SSHKeyManager]:
    """Two nodes on one backend: node A created 'GitLab', node B synced it.

    This is the exact state the staging reproduction observed: the creating
    node knows the key locally, every other node only has the synced file.
    """
    node_a = node_factory("node_a", shared_backend, fernet)
    created = node_a.create_key(name="GitLab", key_type="ed25519")
    assert created.name == "GitLab", "precondition: key created on node A"
    assigned = node_a.assign_key_to_host("GitLab", "gitlab.com")
    assert assigned.hosts == ["gitlab.com"], "precondition: host assignment recorded"

    node_b = node_factory("node_b", shared_backend, fernet)
    sync_result = materialize_on_node(node_b, shared_backend, fernet)
    assert sync_result["errors"] == [], "precondition: sync must succeed"
    assert (node_b.ssh_dir / "GitLab").exists(), (
        "precondition: sync must have written the key onto node B"
    )

    return node_a, node_b


class TestNodesAgreeOnManagedStatus:
    """The reproduction of the reported defect."""

    def test_both_nodes_report_the_key_as_managed(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """
        Pre-fix this fails on node B: its node-local SQLite has no row for the
        key, so `managed` is empty -- the exact staging symptom.
        """
        node_a, node_b = synced_cluster_pair

        assert [k.name for k in node_a.list_keys().managed] == ["GitLab"]
        assert [k.name for k in node_b.list_keys().managed] == ["GitLab"]

    def test_neither_node_reports_the_key_as_unmanaged(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """Pre-fix node B misclassifies the synced file as a foreign key."""
        node_a, node_b = synced_cluster_pair

        assert [k.name for k in node_a.list_keys().unmanaged] == []
        assert [k.name for k in node_b.list_keys().unmanaged] == []

    def test_key_appears_exactly_once_on_the_creating_node(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """A key present both locally and in the shared backend is not duplicated."""
        node_a, _ = synced_cluster_pair

        assert [k.name for k in node_a.list_keys().managed] == ["GitLab"]


class TestClusterEntryDescribesTheLocalNode:
    """A cluster-sourced entry must describe THIS node's own file and cluster truth."""

    def test_paths_point_at_this_nodes_materialized_files(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """
        The originating node's `private_path` is meaningless elsewhere -- the
        same convention SSHKeySyncService._sync_ssh_config documents.
        """
        node_a, node_b = synced_cluster_pair

        (entry,) = node_b.list_keys().managed
        assert entry.private_path == str(node_b.ssh_dir / "GitLab")
        assert entry.public_path == str(node_b.ssh_dir / "GitLab.pub")
        assert entry.private_path != str(node_a.ssh_dir / "GitLab")

    def test_descriptive_fields_come_from_cluster_truth(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        node_a = node_factory("node_a", shared_backend, fernet)
        created = node_a.create_key(
            name="GitLab",
            key_type="ed25519",
            email="ops@example.com",
            description="cluster deploy key",
        )
        assert created.description == "cluster deploy key"
        assigned = node_a.assign_key_to_host("GitLab", "gitlab.com")
        assert assigned.hosts == ["gitlab.com"]

        node_b = node_factory("node_b", shared_backend, fernet)
        assert materialize_on_node(node_b, shared_backend, fernet)["errors"] == []

        (entry,) = node_b.list_keys().managed
        assert entry.email == "ops@example.com"
        assert entry.description == "cluster deploy key"
        assert entry.hosts == ["gitlab.com"]
        assert entry.key_type == "ed25519"

    def test_genuinely_foreign_key_stays_unmanaged(
        self, synced_cluster_pair: Tuple[SSHKeyManager, SSHKeyManager]
    ) -> None:
        """
        Discriminating case: the cluster merge must not blanket-classify
        everything as managed. Node B holds one cluster-tracked key AND one
        unrelated personal key; only the former may become managed.
        """
        _, node_b = synced_cluster_pair

        write_ssh_file(node_b.ssh_dir / "id_personal", "PRIVATE")
        write_ssh_file(node_b.ssh_dir / "id_personal.pub", "ssh-ed25519 AAAA personal")

        result_b = node_b.list_keys()
        assert [k.name for k in result_b.managed] == ["GitLab"]
        assert [k.name for k in result_b.unmanaged] == ["id_personal"]


class TestJsonMetadataModeIsClusterAware:
    """The JSON metadata_dir storage mode must consult the shared backend too."""

    def test_node_without_local_json_metadata_reports_managed(
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

        # Cluster truth holds the key; this node has no local JSON metadata.
        # create_key is a command returning None -- the get_key read below is
        # what proves the row landed.
        shared_backend.create_key(
            name="GitLab",
            fingerprint="SHA256:clusterfp",
            key_type="ed25519",
            private_path="/home/other-node/.ssh/GitLab",
            public_path="/home/other-node/.ssh/GitLab.pub",
            public_key="ssh-ed25519 AAAA cluster",
            private_key=fernet.encrypt(b"PRIVATE").decode(),
        )
        backend_row = shared_backend.get_key("GitLab")
        assert backend_row is not None and backend_row["name"] == "GitLab"
        assert materialize_on_node(node, shared_backend, fernet)["errors"] == []

        result = node.list_keys()
        assert [k.name for k in result.managed] == ["GitLab"]
        assert result.unmanaged == []


class TestSoloModeListKeysUnchanged:
    """No `_pg_backend` => byte-identical pre-#1524 behavior."""

    def test_foreign_key_still_unmanaged(self, solo_node: NodeFactory) -> None:
        node = solo_node("solo")
        assert node._pg_backend is None

        created = node.create_key(name="managed_key", key_type="ed25519")
        assert created.name == "managed_key"
        write_ssh_file(node.ssh_dir / "foreign_key", "PRIVATE")
        write_ssh_file(node.ssh_dir / "foreign_key.pub", "ssh-ed25519 AAAA foreign")

        result = node.list_keys()
        assert [k.name for k in result.managed] == ["managed_key"]
        assert [k.name for k in result.unmanaged] == ["foreign_key"]

    def test_recorded_private_path_reported_verbatim(
        self, solo_node: NodeFactory, tmp_path: Path
    ) -> None:
        """
        Solo mode must never rewrite a recorded path to an ssh_dir/name guess:
        an imported key legitimately lives outside ssh_dir.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        imported_private = write_ssh_file(elsewhere / "imported_key", "PRIVATE")
        write_ssh_file(elsewhere / "imported_key.pub", "ssh-ed25519 AAAA imported")

        node = solo_node("solo_imported")
        assert node._sqlite_backend is not None
        # Command returning None; the get_key read below proves the row landed.
        node._sqlite_backend.create_key(
            name="imported_key",
            fingerprint="SHA256:importedfp",
            key_type="ed25519",
            private_path=str(imported_private),
            public_path=str(elsewhere / "imported_key.pub"),
            public_key="ssh-ed25519 AAAA imported",
            is_imported=True,
        )
        stored = node._sqlite_backend.get_key("imported_key")
        assert stored is not None and stored["private_path"] == str(imported_private)

        (entry,) = node.list_keys().managed
        assert entry.private_path == str(imported_private)


class TestBackendReadFailurePolicy:
    """
    Failure policy of the NEW shared-backend read added to the list path.

    Mirrors create_key / assign_key_to_host / delete_key in this same class:
    a backend failure is logged and re-raised, never masked by silently
    degrading to the node-local view -- which is precisely the divergence
    Bug #1524 is about.
    """

    def test_backend_read_failure_is_raised_not_swallowed(
        self, node_factory: NodeFactory, fernet: Fernet
    ) -> None:
        pg_backend = MagicMock()
        pg_backend.list_keys.side_effect = RuntimeError("PG list_keys failed")

        node = node_factory("failing_node", pg_backend, fernet)

        with pytest.raises(RuntimeError, match="PG list_keys failed"):
            node.list_keys()


class TestDeleteKeyProvenanceGuardUnweakened:
    """
    Bug #1519 regression guard, inside this change's own blast radius.

    delete_key lives in the same class and reaches the list path via
    _update_ssh_config(); its provenance decision is nonetheless made from its
    own direct backend/JSON reads. Making the list path cluster-aware must
    leave that refusal behavior untouched.
    """

    def test_untracked_same_named_file_is_still_refused(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        node = node_factory("guard_node", shared_backend, fernet)

        # A file this service never created, tracked by no backend.
        write_ssh_file(node.ssh_dir / "not_ours", "PRIVATE")
        write_ssh_file(node.ssh_dir / "not_ours.pub", "ssh-ed25519 AAAA not_ours")

        assert node.delete_key("not_ours") is False
        assert (node.ssh_dir / "not_ours").exists()
        assert (node.ssh_dir / "not_ours.pub").exists()
