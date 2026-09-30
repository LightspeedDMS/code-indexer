"""
Format-time backstop for ~/.ssh/config Host blocks.

``SSHConfigManager._format_host_block`` must refuse any host, hostname or
key path containing a newline or other control character, so every line it
writes is exactly one intended OpenSSH directive.

Discriminating RED: without that check, ``write_config`` would write the
extra line to disk. These tests fail on that
behavior, not on a missing symbol.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.ssh_config_manager import (
    HostEntry,
    ParsedConfig,
    SSHConfigManager,
)
from code_indexer.server.services.ssh_input_validation import SSHConfigFormatError


@pytest.fixture
def manager() -> SSHConfigManager:
    return SSHConfigManager()


def test_write_config_rejects_hostname_with_newline(
    manager: SSHConfigManager, tmp_path: Path
) -> None:
    config_path = tmp_path / "config"
    unsafe_entry = HostEntry(
        host="example.com\nHost other",
        hostname="example.com\nHost other",
        key_path=str(tmp_path / "id_ed25519"),
    )

    with pytest.raises(SSHConfigFormatError):
        manager.write_config(config_path, ParsedConfig(), [unsafe_entry])

    # Never let a rejected value reach ~/.ssh/config: the file must not
    # exist at all (atomic write never got as far as the temp-file rename).
    assert not config_path.exists()


def test_write_config_rejects_hostname_with_newline_only(
    manager: SSHConfigManager, tmp_path: Path
) -> None:
    """host is a clean literal but hostname alone breaks its config line --
    both fields must be independently validated."""
    config_path = tmp_path / "config"
    unsafe_entry = HostEntry(
        host="example.com",
        hostname="example.com\nHost other",
        key_path=str(tmp_path / "id_ed25519"),
    )

    with pytest.raises(SSHConfigFormatError):
        manager.write_config(config_path, ParsedConfig(), [unsafe_entry])

    assert not config_path.exists()


def test_write_config_rejects_control_characters_in_key_path(
    manager: SSHConfigManager, tmp_path: Path
) -> None:
    config_path = tmp_path / "config"
    unsafe_entry = HostEntry(
        host="example.com",
        hostname="example.com",
        key_path=str(tmp_path) + "/id_ed25519\nHost other",
    )

    with pytest.raises(SSHConfigFormatError):
        manager.write_config(config_path, ParsedConfig(), [unsafe_entry])

    assert not config_path.exists()


def test_write_config_still_accepts_legitimate_entries(
    manager: SSHConfigManager, tmp_path: Path
) -> None:
    config_path = tmp_path / "config"
    entries = [
        HostEntry(
            host="github.com",
            hostname="github.com",
            key_path=str(tmp_path / "id_ed25519"),
        ),
        HostEntry(
            host="gitlab.example.com",
            hostname="gitlab.example.com",
            key_path=str(tmp_path / "id_rsa"),
        ),
        HostEntry(
            host="192.0.2.10",
            hostname="192.0.2.10",
            key_path=str(tmp_path / "id_ecdsa"),
        ),
    ]

    manager.write_config(config_path, ParsedConfig(), entries)

    content = config_path.read_text()
    assert "Host github.com" in content
    assert "HostName github.com" in content
    assert "Host gitlab.example.com" in content
    assert "Host 192.0.2.10" in content
    assert "ProxyCommand" not in content
