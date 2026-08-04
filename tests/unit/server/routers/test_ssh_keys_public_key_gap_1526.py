"""
Router-level regression test for a gap left by GitHub issue #1526.

`SSHKeyManager.get_public_key()` was extended (#1526) to resolve cluster-managed
keys via the shared PostgreSQL backend. That change introduced a new failure
mode: `PublicKeyNotFoundError` is raised when a key is known cluster-wide but
has no public key material available on the current node yet (see
`tests/unit/server/services/ssh_key_cluster_1526/test_get_public_key_1526.py`,
class `TestPublicMaterialMissing`, which already proves this at the service
layer). The same exception was also always reachable pre-#1526 for a
locally-tracked key whose `.pub` file was manually deleted from disk.

The REST router's `get_public_key()` handler
(`src/code_indexer/server/routers/ssh_keys.py`) only catches
`KeyNotFoundError` -- `PublicKeyNotFoundError` escapes uncaught, so FastAPI
turns it into a bare 500 Internal Server Error with no useful detail instead
of a clean 404.

Pre-fix: this test's primary assertion fails because
`ssh_keys.get_public_key()` raises the raw `PublicKeyNotFoundError` instead of
an `HTTPException` -- proving the unhandled-exception (bare-500) behavior.
Post-fix: `get_public_key()` catches `PublicKeyNotFoundError` and raises
`HTTPException(status_code=404, ...)`.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from code_indexer.server.routers import ssh_keys
from code_indexer.server.services.ssh_key_manager import (
    KeyNotFoundError,
    PublicKeyNotFoundError,
)


class _StubManagerPublicKeyMissing:
    """Stand-in for SSHKeyManager whose get_public_key() raises PublicKeyNotFoundError."""

    def get_public_key(self, name: str) -> str:
        raise PublicKeyNotFoundError(
            f"Public key material for '{name}' is not available on this node"
        )


class _StubManagerKeyNotFound:
    """Stand-in for SSHKeyManager whose get_public_key() raises KeyNotFoundError."""

    def get_public_key(self, name: str) -> str:
        raise KeyNotFoundError(f"Key '{name}' does not exist")


def test_get_public_key_returns_404_not_500_when_public_material_missing(
    monkeypatch,
):
    """PublicKeyNotFoundError must become a clean 404, never escape uncaught."""
    monkeypatch.setattr(
        ssh_keys, "get_ssh_key_manager", lambda: _StubManagerPublicKeyMissing()
    )

    with pytest.raises(HTTPException) as exc_info:
        ssh_keys.get_public_key("GitLab")

    assert exc_info.value.status_code == 404
    assert "GitLab" in str(exc_info.value.detail)


def test_get_public_key_still_404s_on_key_not_found(monkeypatch):
    """Non-regression: the pre-existing KeyNotFoundError -> 404 mapping is unchanged."""
    monkeypatch.setattr(
        ssh_keys, "get_ssh_key_manager", lambda: _StubManagerKeyNotFound()
    )

    with pytest.raises(HTTPException) as exc_info:
        ssh_keys.get_public_key("GitLab")

    assert exc_info.value.status_code == 404
    assert "GitLab" in str(exc_info.value.detail)
