"""
Sync-service severity and content contract for config-line exclusions.

A backend row whose stored key name or hostname fails the config-line
grammar is excluded from ``~/.ssh/config`` during a sync. This file proves
two things about how that exclusion is logged:

  1. Severity is WARNING -- the sync service runs
     once per node startup, not once per admin
     request, so this is not the "silently dropped" surfacing concern the
     ERROR-once-per-fingerprint mechanism in SSHKeyManager addresses).
  2. The raw invalid name/hostname content never reaches the log line --
     a row whose NAME is invalid is identified by its fingerprint; a row
     whose HOSTNAME is invalid is identified by its (already-valid) key
     name.

Modeled on the existing fixtures/backend stub in
test_ssh_key_sync_service_revalidation.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

TEST_BACKEND_IDENTITY = "severity-regression-backend-identity"

HOSTNAME_WITH_NEWLINE = "example.com\nHost other"
KEY_NAME_WITH_NEWLINE = "key\nHost other"


def _make_backend(keys: list) -> MagicMock:
    backend = MagicMock()
    backend.list_keys.return_value = keys
    return backend


def _key_data(
    name: str,
    private_key: str = "PRIVATE_KEY_CONTENT",
    public_key: str = "ssh-ed25519 AAAA comment",
    hosts: list | None = None,
    fingerprint: str | None = None,
) -> dict:
    return {
        "name": name,
        "private_key": private_key,
        "public_key": public_key,
        "fingerprint": fingerprint or f"SHA256:fake_{abs(hash(name))}",
        "key_type": "ed25519",
        "hosts": list(hosts) if hosts else [],
    }


def _make_service(backend, ssh_dir: Path):
    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    return SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(ssh_dir),
        backend_identity=TEST_BACKEND_IDENTITY,
    )


def test_sync_config_excludes_key_with_invalid_name_at_warning_with_fingerprint(
    tmp_path: Path, caplog
) -> None:
    fingerprint = "SHA256:fp_severity_key_name_test"
    backend = _make_backend(
        [
            _key_data(
                KEY_NAME_WITH_NEWLINE, hosts=["github.com"], fingerprint=fingerprint
            )
        ]
    )
    svc = _make_service(backend, tmp_path)

    with caplog.at_level(logging.DEBUG):
        svc.sync()

    matching = [r for r in caplog.records if fingerprint in r.getMessage()]
    assert matching, (
        f"expected a log record identifying the row by fingerprint, found: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert all(r.levelname == "WARNING" for r in matching), (
        f"expected WARNING level, got: {[r.levelname for r in matching]}"
    )
    for record in matching:
        assert KEY_NAME_WITH_NEWLINE not in record.getMessage()
        assert "\n" not in record.getMessage()


def test_sync_config_excludes_invalid_hostname_at_warning_with_key_name(
    tmp_path: Path, caplog
) -> None:
    backend = _make_backend([_key_data("deploy_key", hosts=[HOSTNAME_WITH_NEWLINE])])
    svc = _make_service(backend, tmp_path)

    with caplog.at_level(logging.DEBUG):
        svc.sync()

    matching = [r for r in caplog.records if "deploy_key" in r.getMessage()]
    exclusion_records = [r for r in matching if "host mapping" in r.getMessage()]
    assert exclusion_records, (
        f"expected a log record identifying the row by key name, found: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert all(r.levelname == "WARNING" for r in exclusion_records), (
        f"expected WARNING level, got: {[r.levelname for r in exclusion_records]}"
    )
    for record in exclusion_records:
        assert HOSTNAME_WITH_NEWLINE not in record.getMessage()
        assert "\n" not in record.getMessage()
