"""
Key names never coincide with OpenSSH-managed files in the ssh directory.

A key name is a bare filename under ssh_dir, so a name listed in
``RESERVED_SSH_FILE_NAMES`` (``authorized_keys``, ``rc``, ``environment``,
``config``, ``known_hosts``, ...) is rejected wherever a key is created,
materialized, or deleted by name: the shared grammar, key creation,
cluster-row visibility, deletion, and ``sync()``'s key-file writes. Every
other name that stays inside ssh_dir (e.g. "has space", "user@host") is
still written to disk; the config-line grammar only decides what is
rendered into ``~/.ssh/config``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.ssh_input_validation import (
    RESERVED_SSH_FILE_NAMES,
    is_valid_key_name,
)
from code_indexer.server.services.ssh_key_generator import (
    InvalidKeyNameError,
    SSHKeyGenerator,
)
from code_indexer.server.services.ssh_key_manager import SSHKeyManager

# The OpenSSH-managed filenames a key name may never take, stated
# independently of the production set so removing an entry there fails a
# test here instead of silently dropping that name's cases.
EXPECTED_RESERVED_SSH_FILE_NAMES = frozenset(
    {
        "config",
        "authorized_keys",
        "authorized_keys2",
        "known_hosts",
        "known_hosts.old",
        "known_hosts2",
        "environment",
        "rc",
    }
)
RESERVED_NAME_CASES = sorted(EXPECTED_RESERVED_SSH_FILE_NAMES)

# Names that pass the config-line grammar itself (is_valid_key_name).
GRAMMAR_VALID_WIDENED_NAMES = ["id_rsa", "deploy", "user@host"]

# Names that fail the config-line grammar (whitespace) but stay
# visible/deletable via the cluster-merge path and writable via sync():
# the grammar governs config lines, not key files.
CLUSTER_VISIBLE_WIDENED_NAMES = GRAMMAR_VALID_WIDENED_NAMES + ["has space"]


# ---------------------------------------------------------------------------
# Grammar level
# ---------------------------------------------------------------------------


def test_reserved_names_set_is_exactly_the_openssh_managed_files() -> None:
    assert RESERVED_SSH_FILE_NAMES == EXPECTED_RESERVED_SSH_FILE_NAMES


@pytest.mark.parametrize("name", RESERVED_NAME_CASES)
def test_is_valid_key_name_rejects_reserved_ssh_file_names(name: str) -> None:
    assert is_valid_key_name(name) is False


@pytest.mark.parametrize("name", GRAMMAR_VALID_WIDENED_NAMES)
def test_is_valid_key_name_still_accepts_ordinary_widened_names(name: str) -> None:
    assert is_valid_key_name(name) is True


def test_reserved_names_set_has_no_dot_pub_suffixed_entry() -> None:
    """A reserved name whose value itself ended in '.pub' would need extra
    handling for the public-key twin; confirm none do, so the plain
    ``name in RESERVED_SSH_FILE_NAMES`` check is sufficient."""
    assert not any(name.endswith(".pub") for name in RESERVED_SSH_FILE_NAMES)


# ---------------------------------------------------------------------------
# Creation (generator level -- REST/MCP both delegate to this same check)
# ---------------------------------------------------------------------------


@pytest.fixture
def generator(tmp_path):
    return SSHKeyGenerator(ssh_dir=tmp_path / "ssh")


@pytest.mark.parametrize("name", RESERVED_NAME_CASES)
def test_generator_rejects_reserved_ssh_file_names_at_creation(generator, name):
    with pytest.raises(InvalidKeyNameError):
        generator._validate_key_name(name)


@pytest.mark.parametrize("name", RESERVED_NAME_CASES)
def test_generator_reserved_name_error_names_the_reservation(generator, name):
    """The creation error for a reserved name says it is reserved for an
    OpenSSH file -- the name has no invalid characters."""
    with pytest.raises(InvalidKeyNameError) as excinfo:
        generator._validate_key_name(name)

    message = str(excinfo.value)
    assert message == f"Key name is reserved for an OpenSSH file: {name!r}"
    assert "invalid characters" not in message


# ---------------------------------------------------------------------------
# Cluster-row visibility: a reserved name must never be materialized here.
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path) -> SSHKeyManager:
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(parents=True, mode=0o700)
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "metadata",
        config_path=ssh_dir / "config",
    )


@pytest.mark.parametrize("name", RESERVED_NAME_CASES)
def test_local_materialized_paths_rejects_reserved_ssh_file_names(manager, name):
    assert manager._local_materialized_paths(name) is None


@pytest.mark.parametrize("name", CLUSTER_VISIBLE_WIDENED_NAMES)
def test_local_materialized_paths_still_accepts_ordinary_widened_names(manager, name):
    assert manager._local_materialized_paths(name) is not None


# ---------------------------------------------------------------------------
# Deletion: a reserved file is never removed, whatever record names a key
# after it (including one written directly to the backend).
# ---------------------------------------------------------------------------


def test_unlink_key_files_refuses_to_remove_reserved_ssh_file_names(
    manager: SSHKeyManager,
) -> None:
    real_authorized_keys = manager.ssh_dir / "authorized_keys"
    real_authorized_keys.write_text("ssh-ed25519 AAAA real-admin-key\n")
    original_content = real_authorized_keys.read_text()

    manager._unlink_key_files(
        str(real_authorized_keys), str(manager.ssh_dir / "authorized_keys.pub")
    )

    assert real_authorized_keys.exists()
    assert real_authorized_keys.read_text() == original_content


# ---------------------------------------------------------------------------
# End-to-end: sync() never writes over a reserved file, whatever backend
# row names a key after it, while ordinary names are still written.
# ---------------------------------------------------------------------------


def _make_backend(keys: list):
    from unittest.mock import MagicMock

    backend = MagicMock()
    backend.list_keys.return_value = keys
    return backend


def _key_data(name: str, hosts=None) -> dict:
    return {
        "name": name,
        "private_key": "PRIVATE_KEY_CONTENT",
        "public_key": "ssh-ed25519 AAAA comment",
        "fingerprint": f"SHA256:fake_{abs(hash(name))}",
        "key_type": "ed25519",
        "hosts": list(hosts) if hosts else [],
    }


def test_sync_never_overwrites_a_real_reserved_ssh_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A backend row named 'authorized_keys' leaves the existing file
    byte-for-byte unchanged (seeded before sync(), compared after)."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    (tmp_path / "fake_home").mkdir()

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    real_authorized_keys = ssh_dir / "authorized_keys"
    original_content = "ssh-ed25519 AAAA the-real-admin-key\n"
    real_authorized_keys.write_text(original_content)

    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    backend = _make_backend(
        [
            _key_data("authorized_keys", hosts=["github.com"]),
            _key_data("deploy", hosts=["github.com"]),
        ]
    )
    svc = SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(ssh_dir),
        backend_identity="reserved-name-test",
    )

    svc.sync()

    assert real_authorized_keys.read_text() == original_content
    # The ordinary key in the same sync is still written.
    assert (ssh_dir / "deploy").exists()


def test_sync_still_writes_ordinary_widened_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    (tmp_path / "fake_home").mkdir()

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)

    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    backend = _make_backend([_key_data("has space", hosts=["github.com"])])
    svc = SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(ssh_dir),
        backend_identity="ordinary-name-test",
    )

    svc.sync()

    assert (ssh_dir / "has space").exists()


# ---------------------------------------------------------------------------
# Stale-key sweep: a manifest entry naming a reserved file is dropped from
# the manifest without touching the file; ordinary stale keys are removed.
# ---------------------------------------------------------------------------


def test_sync_stale_sweep_never_removes_reserved_files_left_in_manifest(
    tmp_path: Path, caplog
) -> None:
    import json
    import logging

    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    identity = "reserved-stale-test"
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)

    reserved_contents = {
        "authorized_keys": "ssh-ed25519 AAAA example-authorized-key\n",
        "rc": "# example rc file\n",
    }
    for name, content in reserved_contents.items():
        (ssh_dir / name).write_text(content)
    (ssh_dir / "old-deploy").write_text("OLD PRIVATE")
    (ssh_dir / "old-deploy.pub").write_text("ssh-ed25519 AAAA old")

    manifest_path = ssh_dir / ".cidx-ssh-keys.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "backends": {identity: ["authorized_keys", "old-deploy", "rc"]},
            }
        )
    )

    svc = SSHKeySyncService(
        ssh_keys_backend=_make_backend([_key_data("deploy", hosts=["github.com"])]),
        ssh_dir=str(ssh_dir),
        backend_identity=identity,
    )

    with caplog.at_level(logging.WARNING):
        result = svc.sync()

    for name, content in reserved_contents.items():
        assert (ssh_dir / name).read_text() == content
    assert not (ssh_dir / "old-deploy").exists()
    assert not (ssh_dir / "old-deploy.pub").exists()
    assert result["removed"] == ["old-deploy"]

    managed = json.loads(manifest_path.read_text())["backends"][identity]
    assert managed == ["deploy"]

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for name in reserved_contents:
        assert any(repr(name) in message for message in warnings)
