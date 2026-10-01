"""
SSHKeyManager entry-point validation for hostnames.

Discriminating RED: without hostname validation in ``assign_key_to_host``,
a hostname containing a newline would pass
``_raise_on_user_section_conflict``, be persisted into the key's metadata,
and be materialized into ``~/.ssh/config`` verbatim by
``_update_ssh_config``. These tests use a REAL ``SSHKeyManager`` (real
ssh-keygen subprocess, real JSON metadata store, real SSHConfigManager)
pointed entirely at ``tmp_path`` -- never the real ``~/.ssh`` -- so they fail
on the actual behavior (unsafe data reaching disk), not for a missing
symbol.

Additional cases:
  - The trailing-newline and scoped-IPv6 cases at this (manager) layer: a
    trailing-newline or scoped-IPv6 hostname must be rejected by
    ``assign_key_to_host`` exactly like any other config-line-breaking
    value.
  - ``_update_ssh_config`` validated per-hostname but never validated
    the LEGACY key NAME itself when regenerating the config from node-local
    metadata -- a legacy key named e.g. ``key%h`` renders straight into the
    ``IdentityFile`` line, and a newline-bearing legacy name trips
    SSHConfigManager's format-time backstop and aborts the WHOLE rewrite,
    silently dropping every OTHER legitimate key's Host block too.

``_local_materialized_paths`` does not apply the
config-line grammar -- listing and deleting a cluster-tracked key is a
different surface from writing a config line, and must behave exactly as
it always did (bare-filename + containment only). A legacy key name that fails the
config-line grammar (e.g. one containing a newline) is therefore visible
and manageable again; only ``_build_host_entries``/``_update_ssh_config``
still exclude it from the generated config.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from code_indexer.server.services.ssh_key_manager import KeyMetadata, SSHKeyManager
from code_indexer.server.services.ssh_input_validation import InvalidHostnameError

HOSTNAME_WITH_NEWLINE = "example.com\nHost other"


@pytest.fixture
def manager(tmp_path: Path) -> SSHKeyManager:
    """Real SSHKeyManager, entirely confined to tmp_path -- never ~/.ssh."""
    ssh_dir = tmp_path / "ssh"
    metadata_dir = tmp_path / "metadata"
    config_path = ssh_dir / "config"
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        config_path=config_path,
    )


def test_assign_key_to_host_rejects_hostname_with_newline(
    manager: SSHKeyManager,
) -> None:
    manager.create_key("deploy-key_1.v2")

    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("deploy-key_1.v2", HOSTNAME_WITH_NEWLINE)

    # Never let a rejected value reach ~/.ssh/config.
    assert not manager.config_path.exists()

    # Never let it reach storage either: the key's own metadata must show
    # zero assigned hosts.
    reloaded = manager._load_metadata("deploy-key_1.v2")
    assert reloaded is not None
    assert reloaded.hosts == []


def test_assign_key_to_host_rejects_hostname_with_newline_with_force(
    manager: SSHKeyManager,
) -> None:
    """force=True only bypasses the user-section conflict guard -- it must
    NOT bypass hostname grammar validation."""
    manager.create_key("deploy-key_1.v2")

    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("deploy-key_1.v2", HOSTNAME_WITH_NEWLINE, force=True)

    assert not manager.config_path.exists()


@pytest.mark.parametrize(
    "hostname",
    ["github.com", "gitlab.example.com", "192.0.2.10"],
)
def test_assign_key_to_host_accepts_legitimate_hostnames_end_to_end(
    manager: SSHKeyManager, hostname: str
) -> None:
    manager.create_key("deploy-key_1.v2")

    metadata = manager.assign_key_to_host("deploy-key_1.v2", hostname)

    assert hostname in metadata.hosts
    content = manager.config_path.read_text()
    assert f"Host {hostname}" in content
    assert f"HostName {hostname}" in content
    assert "ProxyCommand" not in content


# ---------------------------------------------------------------------------
# Trailing-newline and scoped-IPv6 cases at the manager entry point too.
# ---------------------------------------------------------------------------


def test_assign_key_to_host_rejects_trailing_newline_hostname(
    manager: SSHKeyManager,
) -> None:
    manager.create_key("deploy-key_1.v2")
    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("deploy-key_1.v2", "github.com\n")
    assert not manager.config_path.exists()


def test_assign_key_to_host_rejects_trailing_cr_hostname(
    manager: SSHKeyManager,
) -> None:
    manager.create_key("deploy-key_1.v2")
    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("deploy-key_1.v2", "github.com\r")
    assert not manager.config_path.exists()


def test_assign_key_to_host_rejects_scoped_ipv6_hostname(
    manager: SSHKeyManager,
) -> None:
    """Scoped IPv6 with an OpenSSH %h token as the zone id -- must be
    rejected, not accepted as a "valid IPv6 literal"."""
    manager.create_key("deploy-key_1.v2")
    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("deploy-key_1.v2", "fe80::1%h")
    assert not manager.config_path.exists()


def test_local_materialized_paths_accepts_legacy_key_name_with_newline(
    manager: SSHKeyManager,
) -> None:
    """A key name arriving from the
    shared cluster backend with an embedded newline is not a directory-
    traversal name (no '/'), so it passes the bare-filename + containment
    checks that gate cluster-key visibility.
    Only the config-line grammar (exercised separately, in
    _build_host_entries/_update_ssh_config) still excludes it from
    ~/.ssh/config."""
    name_with_newline = "key\nHost other"
    result = manager._local_materialized_paths(name_with_newline)
    assert result is not None
    private_path, public_path = result
    assert private_path.endswith(name_with_newline)
    assert public_path.endswith(f"{name_with_newline}.pub")


def test_local_materialized_paths_accepts_legitimate_cluster_key_name(
    manager: SSHKeyManager,
) -> None:
    result = manager._local_materialized_paths("deploy-key_1.v2")
    assert result is not None
    private_path, public_path = result
    assert private_path.endswith("deploy-key_1.v2")
    assert public_path.endswith("deploy-key_1.v2.pub")


def test_local_materialized_paths_rejects_directory_traversal_name(
    manager: SSHKeyManager, caplog
) -> None:
    """The bare-filename check (independent of the config-line grammar
    above) still refuses a name carrying a path separator, logged at
    WARNING -- this check is not deduped, so ERROR would fire on every
    list_keys() call for a cluster row with a genuine traversal name."""
    with caplog.at_level(logging.WARNING):
        result = manager._local_materialized_paths("../other")

    assert result is None
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], (
        f"expected WARNING, not ERROR: "
        f"{[(r.levelname, r.getMessage()) for r in error_records]}"
    )


# ---------------------------------------------------------------------------
# Legacy LOCAL metadata key-name validation.
# ---------------------------------------------------------------------------


def test_update_ssh_config_excludes_legacy_local_key_name_with_newline_but_writes_good_key(
    manager: SSHKeyManager,
) -> None:
    """Simulates node-local JSON metadata persisted before key-name
    validation existed (or written directly, without create_key's
    validation): a key record whose NAME itself breaks a config line.
    _update_ssh_config must exclude that
    one entry rather than let SSHConfigManager's format-time backstop
    abort the ENTIRE rewrite -- which would silently drop the co-existing
    good key's Host block too.

    Uses _save_metadata directly (not create_key, which now validates the
    name) -- exactly how a legacy record would already exist on disk.
    """
    name_with_newline = "key\nHost other"
    manager._save_metadata(
        KeyMetadata(
            name=name_with_newline,
            fingerprint="SHA256:fakefingerprint000000000000000000000000000",
            key_type="ed25519",
            private_path=str(manager.ssh_dir / name_with_newline),
            public_path=str(manager.ssh_dir / f"{name_with_newline}.pub"),
            hosts=["github.com"],
        )
    )

    manager.create_key("good-key_1")
    manager.assign_key_to_host("good-key_1", "gitlab.example.com")

    assert manager.config_path.exists()
    content = manager.config_path.read_text()
    assert "Host gitlab.example.com" in content
    assert "HostName gitlab.example.com" in content
    assert "key\nHost" not in content
    assert "Host other" not in content
    assert "ProxyCommand" not in content


def test_update_ssh_config_excludes_legacy_key_name_with_percent_h(
    manager: SSHKeyManager,
) -> None:
    """A legacy key literally named with an OpenSSH %h token must never
    render into the IdentityFile line."""
    name_with_percent = "key%h"
    manager._save_metadata(
        KeyMetadata(
            name=name_with_percent,
            fingerprint="SHA256:fakefingerprint111111111111111111111111111",
            key_type="ed25519",
            private_path=str(manager.ssh_dir / name_with_percent),
            public_path=str(manager.ssh_dir / f"{name_with_percent}.pub"),
            hosts=["github.com"],
        )
    )

    manager.create_key("good-key_2")
    manager.assign_key_to_host("good-key_2", "gitlab.example.com")

    content = manager.config_path.read_text()
    assert "Host gitlab.example.com" in content
    assert "%h" not in content
