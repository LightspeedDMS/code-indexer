"""
A legacy SSH key record whose stored name or hostname still fails the
(widened) config-line grammar must never be silently dropped from
``~/.ssh/config`` regeneration.

``SSHKeyManager._update_ssh_config`` logs each exclusion at ERROR, so it is
not easy to miss in server logs, and never echoes the raw invalid value.

Decisions this file reflects:
  - The ERROR log fires ONCE per key for the lifetime of the process
    (keyed by fingerprint), not on every single list_keys()/
    _update_ssh_config() call -- ``list_keys()`` may be invoked on every
    admin request, and repeating the same ERROR line on each one would
    crowd out genuine emergencies.
  - The log message never contains the raw invalid name/hostname -- only a
    safe identifier (the key's fingerprint, or its own already-valid name).
  - ``config_issues`` (a separate, short-lived surfacing field on
    ``KeyListResult``) was removed: the ERROR log is the one surfacing
    channel, matching every other exclusion/skip pattern in this codebase.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from code_indexer.server.services.ssh_key_manager import KeyMetadata, SSHKeyManager

KEY_NAME_WITH_NEWLINE = "key\nHost other"
HOSTNAME_WITH_NEWLINE = "example.com\nHost other"


@pytest.fixture(autouse=True)
def _reset_log_dedup():
    """The once-per-process log dedup set is class-level (shared across
    every SSHKeyManager instance) -- reset it before each test so a
    fingerprint string reused by another test file can never suppress
    logging here."""
    SSHKeyManager._reset_config_exclusion_log_dedup_for_tests()
    yield
    SSHKeyManager._reset_config_exclusion_log_dedup_for_tests()


@pytest.fixture
def manager(tmp_path: Path) -> SSHKeyManager:
    ssh_dir = tmp_path / "ssh"
    metadata_dir = tmp_path / "metadata"
    config_path = ssh_dir / "config"
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        config_path=config_path,
    )


def _save_key_name_with_newline_record(
    manager: SSHKeyManager, fingerprint: str
) -> None:
    manager._save_metadata(
        KeyMetadata(
            name=KEY_NAME_WITH_NEWLINE,
            fingerprint=fingerprint,
            key_type="ed25519",
            private_path=str(manager.ssh_dir / KEY_NAME_WITH_NEWLINE),
            public_path=str(manager.ssh_dir / f"{KEY_NAME_WITH_NEWLINE}.pub"),
            hosts=["github.com"],
        )
    )


def _save_hostname_with_newline_record(manager: SSHKeyManager) -> None:
    manager.create_key("legacy-good-key")
    metadata = manager._load_metadata("legacy-good-key")
    assert metadata is not None
    metadata.hosts.append(HOSTNAME_WITH_NEWLINE)
    manager._save_metadata(metadata)


# ---------------------------------------------------------------------------
# Severity: ERROR, never WARNING, and never the raw invalid content.
# ---------------------------------------------------------------------------


def test_update_ssh_config_logs_error_for_legacy_key_name_with_newline(
    manager, caplog
) -> None:
    _save_key_name_with_newline_record(manager, "SHA256:fp_test_error_key_name")
    manager.create_key("good-key")

    with caplog.at_level(logging.WARNING):
        manager.assign_key_to_host("good-key", "gitlab.example.com")

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, (
        f"expected an ERROR-level log for the legacy key name, "
        f"found: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    for record in error_records:
        message = record.getMessage()
        assert KEY_NAME_WITH_NEWLINE not in message
        assert "\n" not in message


def test_update_ssh_config_logs_error_for_legacy_hostname_with_newline(
    manager, caplog
) -> None:
    _save_hostname_with_newline_record(manager)
    manager.create_key("second-key")

    with caplog.at_level(logging.WARNING):
        manager.assign_key_to_host("second-key", "gitlab.example.com")

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, (
        f"expected an ERROR-level log for the legacy hostname, "
        f"found: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    for record in error_records:
        message = record.getMessage()
        assert HOSTNAME_WITH_NEWLINE not in message
        assert "\n" not in message


def test_update_ssh_config_still_writes_good_entries_alongside_excluded_one(
    manager,
) -> None:
    """The exclusion must not abort the whole rewrite -- co-existing
    legitimate entries still render (matches the existing
    guarantee for legacy entries)."""
    _save_key_name_with_newline_record(manager, "SHA256:fp_test_still_writes_good")
    manager.create_key("good-key-2")

    manager.assign_key_to_host("good-key-2", "gitlab.example.com")

    content = manager.config_path.read_text()
    assert "Host gitlab.example.com" in content
    assert "key\nHost" not in content
    assert "Host other" not in content


# ---------------------------------------------------------------------------
# The ERROR log fires ONCE per fingerprint per process, not once per
# list_keys()/_update_ssh_config() call.
# ---------------------------------------------------------------------------


def test_list_keys_alone_does_not_trigger_config_exclusion_log(manager, caplog) -> None:
    """P3: _list_keys_internal no longer calls _build_host_entries -- a bare
    list_keys() must never log the exclusion by itself. Only
    _update_ssh_config() (create_key/assign_key_to_host/delete_key) does."""
    fingerprint = "SHA256:fp_test_list_keys_no_log"
    _save_key_name_with_newline_record(manager, fingerprint)

    with caplog.at_level(logging.WARNING):
        result = manager.list_keys()

    assert any(k.name == KEY_NAME_WITH_NEWLINE for k in result.managed)
    matching = [r for r in caplog.records if fingerprint in r.getMessage()]
    assert matching == [], (
        f"list_keys() alone must not trigger the config-exclusion log, "
        f"found: {[(r.levelname, r.getMessage()) for r in matching]}"
    )


def test_error_log_fires_once_across_repeated_update_ssh_config_calls(
    manager, caplog
) -> None:
    fingerprint = "SHA256:fp_test_dedup_update_config"
    _save_key_name_with_newline_record(manager, fingerprint)
    manager.create_key("good-key-3")

    with caplog.at_level(logging.ERROR):
        manager._update_ssh_config()
        manager._update_ssh_config()

    matching = [r for r in caplog.records if fingerprint in r.getMessage()]
    assert len(matching) == 1, (
        f"expected exactly one ERROR log across 2 _update_ssh_config() "
        f"calls for the same fingerprint, got {len(matching)}: "
        f"{[r.getMessage() for r in matching]}"
    )


def test_error_log_for_different_fingerprints_both_fire(manager, caplog) -> None:
    """Dedup is per-fingerprint, not a global "log nothing more" latch."""
    fp_a = "SHA256:fp_test_dedup_a"
    fp_b = "SHA256:fp_test_dedup_b"
    manager._save_metadata(
        KeyMetadata(
            name="a\nHost other",
            fingerprint=fp_a,
            key_type="ed25519",
            private_path=str(manager.ssh_dir / "a\nHost other"),
            public_path=str(manager.ssh_dir / "a\nHost other.pub"),
            hosts=["github.com"],
        )
    )
    manager._save_metadata(
        KeyMetadata(
            name="b\nHost other",
            fingerprint=fp_b,
            key_type="ed25519",
            private_path=str(manager.ssh_dir / "b\nHost other"),
            public_path=str(manager.ssh_dir / "b\nHost other.pub"),
            hosts=["github.com"],
        )
    )
    manager.create_key("good-key-4")

    with caplog.at_level(logging.ERROR):
        manager._update_ssh_config()

    assert any(fp_a in r.getMessage() for r in caplog.records)
    assert any(fp_b in r.getMessage() for r in caplog.records)
