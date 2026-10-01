"""Bug #1526: guards on the cluster lookup shared by BOTH fixed methods.

`get_public_key` and `assign_key_to_host` now resolve an unknown-locally key
through the same shared-backend lookup (`_cluster_managed_key_metadata` ->
`_cluster_row_to_local_metadata` -> `_local_materialized_paths`).  Every
invariant that must hold identically for both is asserted HERE, parametrized
over the two operations, instead of being duplicated in each method's module:

  * an unknown name is still a genuine miss -- the fix must not invent keys;
  * a cluster row name is untrusted input and must pass the same bare-filename
    + ssh_dir containment validation Bug #1519/#1524 established;
  * a shared-backend read failure propagates rather than silently degrading to
    the node-local view -- that degradation IS the bug;
  * solo mode (no `_pg_backend`) is byte-identical to pre-#1526 behavior.

The cluster topology comes from this package's conftest; method-specific
behavior lives in `test_get_public_key_1526.py` / `test_assign_host_1526.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import KeyNotFoundError
from tests.unit.server.services.ssh_key_cluster_1526._support import (
    ASSIGNED_HOST,
    CLUSTER_LOOKUP_OPERATIONS,
    ClusterLookupOperation,
    FakeSSHKeysBackend,
    seed_cluster_only_key,
    write_text_verified,
)
from tests.unit.server.services.ssh_key_cluster_1526.conftest import NodeFactory

OPERATION_IDS = list(CLUSTER_LOOKUP_OPERATIONS)
OPERATIONS = list(CLUSTER_LOOKUP_OPERATIONS.values())

# A cluster row name that resolves outside ssh_dir; `evil.pub` is planted at the
# traversal target so a containment failure would be observable, not theoretical.
TRAVERSAL_NAME = "../evil"
OUTSIDE_CONTENT = "ssh-ed25519 AAAAoutside must-never-be-returned"


class TestUnknownKeyIsStillAMiss:
    """The fix must consult the cluster, not blanket-approve every name."""

    @pytest.mark.parametrize("operation", OPERATIONS, ids=OPERATION_IDS)
    def test_unknown_name_raises_key_not_found(
        self,
        operation: ClusterLookupOperation,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """Discriminating: cluster truth genuinely holds a DIFFERENT key, so a
        blanket 'yes' would pass while a real lookup correctly misses."""
        seed_cluster_only_key(shared_backend, fernet, name="GitLab")
        node = node_factory("node", shared_backend, fernet)

        with pytest.raises(KeyNotFoundError, match="nonexistent"):
            operation(node, "nonexistent")


class TestClusterNameContainment:
    """A cluster row's ``name`` is untrusted input reaching this node from
    another node's write, so it must keep passing the bare-filename +
    ssh_dir-containment validation Bug #1519/#1524 established."""

    @pytest.mark.parametrize("operation", OPERATIONS, ids=OPERATION_IDS)
    def test_traversal_name_is_refused(
        self,
        operation: ClusterLookupOperation,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        seed_cluster_only_key(shared_backend, fernet, name=TRAVERSAL_NAME)
        node = node_factory("guard_node", shared_backend, fernet)

        with pytest.raises(KeyNotFoundError):
            operation(node, TRAVERSAL_NAME)

    def test_get_public_key_does_not_leak_the_outside_file(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """Refusal is not enough: the file the traversal name points at, OUTSIDE
        ssh_dir, must never be read and returned."""
        seed_cluster_only_key(shared_backend, fernet, name=TRAVERSAL_NAME)
        node = node_factory("leak_node", shared_backend, fernet)
        planted = write_text_verified(node.ssh_dir.parent / "evil.pub", OUTSIDE_CONTENT)

        with pytest.raises(KeyNotFoundError) as excinfo:
            node.get_public_key(TRAVERSAL_NAME)

        assert planted not in str(excinfo.value)

    def test_assign_writes_no_host_row_for_a_refused_name(
        self,
        node_factory: NodeFactory,
        shared_backend: FakeSSHKeysBackend,
        fernet: Fernet,
    ) -> None:
        """Refusal must also leave cluster truth untouched, or the next sync
        would act on a name this node just rejected."""
        seed_cluster_only_key(shared_backend, fernet, name=TRAVERSAL_NAME)
        node = node_factory("guard_node", shared_backend, fernet)

        with pytest.raises(KeyNotFoundError):
            node.assign_key_to_host(TRAVERSAL_NAME, ASSIGNED_HOST)

        row = shared_backend.get_key(TRAVERSAL_NAME)
        assert row is not None
        assert row["hosts"] == []


class TestUntrustedBackendRowShape:
    """Both fixed methods read the shared backend, so both must survive a
    malformed row and must never mask a backend failure."""

    @pytest.mark.parametrize("operation", OPERATIONS, ids=OPERATION_IDS)
    def test_non_string_row_name_is_refused(
        self,
        operation: ClusterLookupOperation,
        node_factory: NodeFactory,
        fernet: Fernet,
    ) -> None:
        """A row whose `name` is not a string must be a clean miss, not a
        TypeError from path arithmetic on an int."""
        pg_backend = MagicMock()
        pg_backend.get_key.return_value = {
            "name": 123,
            "fingerprint": "SHA256:x",
            "key_type": "ed25519",
            "private_path": "/elsewhere/123",
            "public_path": "/elsewhere/123.pub",
            "public_key": "ssh-ed25519 AAAA malformed",
            "hosts": [],
        }
        node = node_factory("malformed_node", pg_backend, fernet)

        with pytest.raises(KeyNotFoundError):
            operation(node, "GitLab")

    @pytest.mark.parametrize("operation", OPERATIONS, ids=OPERATION_IDS)
    def test_backend_read_failure_is_raised_not_swallowed(
        self,
        operation: ClusterLookupOperation,
        node_factory: NodeFactory,
        fernet: Fernet,
    ) -> None:
        """Mirrors every other backend interaction in this class: log and
        re-raise.  Degrading to the node-local view IS the bug."""
        pg_backend = MagicMock()
        pg_backend.get_key.side_effect = RuntimeError("PG get_key failed")

        node = node_factory("failing_node", pg_backend, fernet)

        with pytest.raises(RuntimeError, match="PG get_key failed"):
            operation(node, "GitLab")


class TestSoloModeUnchanged:
    """No `_pg_backend` => byte-identical pre-#1526 behavior.  The cluster
    lookup is reachable only when a shared backend actually exists."""

    @pytest.mark.parametrize("operation", OPERATIONS, ids=OPERATION_IDS)
    def test_unknown_name_raises_key_not_found(
        self, operation: ClusterLookupOperation, solo_node: NodeFactory
    ) -> None:
        node = solo_node("solo_missing")
        assert node._pg_backend is None

        with pytest.raises(KeyNotFoundError, match="ghost"):
            operation(node, "ghost")

    def test_get_public_key_of_a_local_key_still_works(
        self, solo_node: NodeFactory
    ) -> None:
        node = solo_node("solo_get")
        created = node.create_key(name="solo_key", key_type="ed25519")
        assert created.public_key is not None

        assert node.get_public_key("solo_key") == created.public_key

    def test_assign_of_a_local_key_still_works(self, solo_node: NodeFactory) -> None:
        node = solo_node("solo_assign")
        created = node.create_key(name="solo_key", key_type="ed25519")
        assert created.hosts == []

        result = node.assign_key_to_host("solo_key", ASSIGNED_HOST)

        assert result.name == "solo_key"
        assert ASSIGNED_HOST in result.hosts
