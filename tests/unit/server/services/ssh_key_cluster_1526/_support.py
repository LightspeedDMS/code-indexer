"""Non-fixture helpers shared by the Bug #1526 test modules.

`CLUSTER_LOOKUP_OPERATIONS` exists so the invariants that must hold for BOTH
methods this bug covers -- an unknown name is still a miss, an untrusted cluster
key name is refused, a backend read failure propagates, solo mode is unchanged
-- are asserted once, parametrized over the two operations, instead of being
duplicated per method.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Union

from cryptography.fernet import Fernet

from code_indexer.server.services.ssh_key_manager import KeyMetadata, SSHKeyManager
from tests.unit.server.services.test_ssh_key_manager_cluster import FakeSSHKeysBackend

__all__ = [
    "ASSIGNED_HOST",
    "CLUSTER_LOOKUP_OPERATIONS",
    "CLUSTER_PUBLIC_KEY",
    "ClusterLookupOperation",
    "FakeSSHKeysBackend",
    "seed_cluster_only_key",
    "write_text_verified",
]


def write_text_verified(path: Path, content: str) -> str:
    """Write a test fixture file and prove it landed.

    ``Path.write_text`` returns the character count it wrote (it forwards
    ``f.write(data)``), so that value is checked against ``content`` rather than
    discarded, and the content is read back from disk -- a silently failed write
    would otherwise leave a test passing for the wrong reason.  Same technique as
    ``test_ssh_key_manager_list_cluster_1524.py::write_ssh_file``.  Returns the
    content so call sites can use it in assertions.
    """
    written = path.write_text(content)
    assert written == len(content)
    assert path.read_text() == content
    return content


CLUSTER_PUBLIC_KEY = "ssh-ed25519 AAAAcluster cluster@example.com"
ASSIGNED_HOST = "gitlab.com"

# The two operations Bug #1526 covers: `get_public_key` answers with the public
# key string, `assign_key_to_host` answers with the updated KeyMetadata.  The
# union is their genuine combined return type -- the shared invariant tests below
# only ever assert on the RAISED exception, never on a returned value.
ClusterLookupOperation = Callable[[SSHKeyManager, str], Union[str, KeyMetadata]]


def seed_cluster_only_key(
    backend: FakeSSHKeysBackend,
    fernet: Fernet,
    name: str = "GitLab",
    public_key: Optional[str] = CLUSTER_PUBLIC_KEY,
) -> None:
    """Put a key into cluster truth only -- no node has materialized it yet.

    The recorded paths deliberately name a DIFFERENT node's ssh dir, because the
    originating node's paths are meaningless on any other node.  `create_key` is
    a command returning None, so the `get_key` read below is what proves the row
    actually landed.
    """
    backend.create_key(
        name=name,
        fingerprint="SHA256:clusterfp",
        key_type="ed25519",
        private_path=f"/home/other-node/.ssh/{name}",
        public_path=f"/home/other-node/.ssh/{name}.pub",
        public_key=public_key,
        email="ops@example.com",
        description="cluster deploy key",
        private_key=fernet.encrypt(b"PRIVATE").decode(),
    )
    row = backend.get_key(name)
    assert row is not None and row["name"] == name, "precondition: cluster row landed"


def _invoke_get_public_key(
    manager: SSHKeyManager, key_name: str
) -> Union[str, KeyMetadata]:
    return manager.get_public_key(key_name)


def _invoke_assign_key_to_host(
    manager: SSHKeyManager, key_name: str
) -> Union[str, KeyMetadata]:
    return manager.assign_key_to_host(key_name, ASSIGNED_HOST)


CLUSTER_LOOKUP_OPERATIONS: Dict[str, ClusterLookupOperation] = {
    "get_public_key": _invoke_get_public_key,
    "assign_key_to_host": _invoke_assign_key_to_host,
}
