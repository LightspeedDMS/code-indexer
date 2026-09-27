"""
REST entry-point rejection for hostnames that fail the config-line grammar.

Reject an invalid hostname with HTTP 400 at every entry
point, including REST. ``SSHKeyManager.assign_key_to_host`` now raises
``InvalidHostnameError`` for a hostname that breaks its config line (see
tests/unit/server/services/test_ssh_key_manager_hostname_validation.py),
but the REST router's ``assign_host()`` only caught ``KeyNotFoundError`` and
``HostConflictError`` -- an ``InvalidHostnameError`` escaped uncaught, so
FastAPI turned it into a bare 500 instead of a clean 400.

Modeled directly on
tests/unit/server/routers/test_ssh_keys_public_key_gap_1526.py's stub-manager
+ monkeypatch pattern.

Discriminating RED: without that handling, this test's primary assertion
fails because ``ssh_keys.assign_host()`` raises the raw
``InvalidHostnameError`` instead of an ``HTTPException``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from code_indexer.server.routers import ssh_keys
from code_indexer.server.services.ssh_input_validation import InvalidHostnameError


class _StubManagerInvalidHostname:
    """Stand-in for SSHKeyManager whose assign_key_to_host() rejects the hostname."""

    def assign_key_to_host(self, key_name: str, hostname: str, force: bool = False):
        raise InvalidHostnameError(f"Invalid hostname: {hostname!r}")


def test_assign_host_returns_400_not_500_on_invalid_hostname(monkeypatch):
    """InvalidHostnameError must become a clean 400, never escape uncaught."""
    monkeypatch.setattr(
        ssh_keys, "get_ssh_key_manager", lambda: _StubManagerInvalidHostname()
    )

    with pytest.raises(HTTPException) as exc_info:
        ssh_keys.assign_host(
            "deploy-key_1.v2",
            ssh_keys.AssignHostRequest(hostname="example.com\nHost other"),
        )

    assert exc_info.value.status_code == 400
