"""
Cluster-merge visibility for legacy key names.

``SSHKeyManager._local_materialized_paths`` decides whether a row read from
the shared cluster backend can be materialized (and therefore
listed/deleted) on this node. It must not apply the config-line grammar: a
LEGACY key whose stored name fails that grammar (a space, for example) must
still show up as ``managed`` in ``list_keys()`` and be deletable, instead of
falling through to the Bug #1519 "untracked file" refusal and leaving its
backend row deleted but its on-disk file orphaned.

The only concrete hazard here is a config-line one: an invalid name/hostname
must never reach a ``Host``/``HostName``/``IdentityFile`` line. Listing and
deleting a key is a different surface entirely and must behave exactly as
it always did -- gated only by the pre-existing bare-filename
and containment checks (Bug #1519's own guards), never by the config-line
grammar.

Uses the real ``SSHKeyManager`` against ``FakeSSHKeysBackend``, a faithful
stateful stand-in for the shared PostgreSQL backend (see
test_ssh_key_manager_cluster.py), so a write via one method (create_key on
the backend) is genuinely observable via another (list_keys/delete_key) --
exactly as in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.ssh_key_manager import SSHKeyManager
from tests.unit.server.services.test_ssh_key_manager_cluster import (
    FakeSSHKeysBackend,
)

LEGACY_KEY_NAMES = ["good-key", "has space", "hash#1", "user@host"]

# The only one of the four that still fails the config-line grammar ('#'
# and '@' are legitimate); a bare space
# remains a genuine hazard for a HostName/IdentityFile line.
STILL_CONFIG_UNSAFE_NAME = "has space"


@pytest.fixture
def shared_backend() -> FakeSSHKeysBackend:
    return FakeSSHKeysBackend()


@pytest.fixture
def manager(tmp_path: Path, shared_backend: FakeSSHKeysBackend) -> SSHKeyManager:
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(parents=True, mode=0o700)
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "metadata",
        config_path=ssh_dir / "config",
        pg_backend=shared_backend,
        fernet=None,
    )


def _seed_cluster_key(
    shared_backend: FakeSSHKeysBackend, manager: SSHKeyManager, name: str
) -> None:
    """Seed a key that exists ONLY in the shared backend + this node's
    filesystem -- exactly the state a cluster node is in for a key created
    (or synced) before this node ever ran ``create_key`` locally."""
    private_path = manager.ssh_dir / name
    public_path = manager.ssh_dir / f"{name}.pub"
    shared_backend.create_key(
        name=name,
        fingerprint=f"SHA256:fake_{abs(hash(name))}",
        key_type="ed25519",
        private_path=str(private_path),
        public_path=str(public_path),
    )
    private_path.write_text("PRIVATE_KEY_CONTENT")
    public_path.write_text("ssh-ed25519 AAAA fake")


@pytest.mark.parametrize("name", LEGACY_KEY_NAMES)
def test_list_keys_reports_legacy_cluster_key_as_managed(
    manager: SSHKeyManager, shared_backend: FakeSSHKeysBackend, name: str
) -> None:
    _seed_cluster_key(shared_backend, manager, name)

    result = manager.list_keys()

    managed_names = {k.name for k in result.managed}
    assert name in managed_names, f"{name!r} must be listed as managed"


@pytest.mark.parametrize("name", LEGACY_KEY_NAMES)
def test_delete_key_removes_legacy_cluster_key_row_and_file(
    manager: SSHKeyManager, shared_backend: FakeSSHKeysBackend, name: str
) -> None:
    _seed_cluster_key(shared_backend, manager, name)

    assert manager.delete_key(name) is True, (
        f"delete_key({name!r}) must succeed, not fall through to the "
        "untracked-file refusal"
    )
    assert shared_backend.get_key(name) is None
    assert not (manager.ssh_dir / name).exists()
    assert not (manager.ssh_dir / f"{name}.pub").exists()


def test_legacy_cluster_key_with_hazardous_name_excluded_only_from_config(
    manager: SSHKeyManager, shared_backend: FakeSSHKeysBackend
) -> None:
    """The one name in the set that still fails the config-line grammar
    (a bare space) must be visible via list_keys() and deletable, but its
    Host block must never appear in the generated ~/.ssh/config."""
    name = STILL_CONFIG_UNSAFE_NAME
    _seed_cluster_key(shared_backend, manager, name)
    shared_backend.assign_host(name, "example.com")

    result = manager.list_keys()
    assert name in {k.name for k in result.managed}

    manager._update_ssh_config()
    content = manager.config_path.read_text() if manager.config_path.exists() else ""
    assert name not in content
    assert "example.com" not in content

    assert manager.delete_key(name) is True
    assert not (manager.ssh_dir / name).exists()


@pytest.mark.parametrize("name", ["good-key", "hash#1", "user@host"])
def test_legacy_cluster_key_with_legitimate_name_appears_in_config(
    manager: SSHKeyManager, shared_backend: FakeSSHKeysBackend, name: str
) -> None:
    """The three names that ARE legitimate under the widened grammar must
    render into the generated config exactly like any other managed key."""
    _seed_cluster_key(shared_backend, manager, name)
    shared_backend.assign_host(name, "example.com")

    manager._update_ssh_config()

    content = manager.config_path.read_text()
    assert "HostName example.com" in content
