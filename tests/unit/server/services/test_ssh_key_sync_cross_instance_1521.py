"""
Regression tests for Bug #1521 -- cross-instance shared-manifest deletion vector.

Bug #1519 hardened ``SSHKeySyncService.sync()``'s manifest-WRITE path (a name is
only recorded as "managed" if this service can prove it wrote the file itself).
That closed the within-one-instance name-collision variant.

It did NOT close a second, proven-exploited variant: ``~/.ssh/.cidx-ssh-keys.json``
is a SINGLE manifest file shared by every server process pointed at the same real
``~/.ssh`` directory, but the stale-removal logic
(``stale_names = managed_names - backend_names`` then ``unlink()``) evaluated that
SHARED manifest against only the CURRENT instance's OWN backend.  A second,
independent server instance (e.g. a scratch test server with a fresh SQLite DB and
zero ``ssh_keys`` rows) therefore saw every name in the shared manifest as
unconditionally stale relative to its own empty backend, and deleted files a
DIFFERENT instance's backend legitimately owned.

This caused real, unrecoverable data loss (2026-08-03): a real
``~/.ssh/id_ed25519_gitlab`` private key file was permanently destroyed.

These tests use REAL ``SSHKeysSqliteBackend`` instances against REAL SQLite
databases and a REAL tmp_path filesystem -- never a mock of the code under test --
because the gap that let this slip through #1519's suite was precisely that every
existing test exercised a SINGLE instance.

NOTE: every test here uses a tmp_path-based ssh_dir.  Nothing in this file may
ever touch the real ``~/.ssh``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import SSHKeysSqliteBackend

MANIFEST_NAME = ".cidx-ssh-keys.json"

PRIVATE_CONTENT = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
)
PUBLIC_CONTENT = "ssh-ed25519 AAAAC3FAKE fake-comment"

# Password-free DSNs: identity derivation only needs the connection target, and
# these tests deliberately never build a credential-bearing connection string.
DSN_PRIMARY = "postgresql://db.internal:5432/cidx"
DSN_OTHER = "postgresql://other.internal:5432/cidx"


# ---------------------------------------------------------------------------
# Real-infrastructure helpers
# ---------------------------------------------------------------------------


def _real_backend(db_path: Path) -> SSHKeysSqliteBackend:
    """Build a REAL SQLite ssh_keys backend against its own database file.

    ``SSHKeysSqliteBackend`` alone does not create the ``ssh_keys`` table --
    production always bootstraps the schema first (see service_init.py), so
    tests mirror that exactly.
    """
    DatabaseSchema(str(db_path)).initialize_database()
    return SSHKeysSqliteBackend(str(db_path))


def _register_key(backend: SSHKeysSqliteBackend, ssh_dir: Path, name: str) -> None:
    backend.create_key(
        name=name,
        fingerprint=f"SHA256:fake_{name}",
        key_type="ed25519",
        private_path=str(ssh_dir / name),
        public_path=str(ssh_dir / f"{name}.pub"),
        public_key=PUBLIC_CONTENT,
        email=None,
        description=None,
        is_imported=False,
        private_key=PRIVATE_CONTENT,
    )


# `backend: Any` mirrors SSHKeySyncService's own constructor contract: it is
# structurally typed against "anything exposing list_keys()", deliberately so
# that the real SQLite backend, the real PG backend and the local test doubles
# below all satisfy it. There is no shared nominal base class to narrow to.
def _service(backend: Any, ssh_dir: Path) -> SSHKeySyncService:
    return SSHKeySyncService(ssh_keys_backend=backend, ssh_dir=str(ssh_dir))


# `Dict[str, Any]` is the precise type of a decoded JSON object: its values are
# genuinely heterogeneous (an int "version" alongside a nested backends map), so
# no narrower static type describes the manifest document as a whole.
def _manifest(ssh_dir: Path) -> Dict[str, Any]:
    parsed: Dict[str, Any] = json.loads((ssh_dir / MANIFEST_NAME).read_text())
    return parsed


def _namespaces(ssh_dir: Path) -> Dict[str, List[str]]:
    """Return the backend-id -> managed-names mapping from the v2 manifest."""
    data = _manifest(ssh_dir)
    namespaces = data.get("backends")
    assert isinstance(namespaces, dict), f"expected a v2 manifest, got: {data!r}"
    typed: Dict[str, List[str]] = namespaces
    return typed


class _FakePool:
    """Minimal stand-in for the project's PostgreSQL ConnectionPool wrapper.

    Only ``_connection_string`` matters for identity derivation; constructing a
    real pool would require a live PostgreSQL server, which this identity-only
    assertion does not need.
    """

    def __init__(self, dsn: str) -> None:
        self._connection_string = dsn


class _PgLikeBackend:
    """Structural stand-in for ``SSHKeysPostgresBackend`` (holds a ``_pool``)."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    # `Dict[str, Any]` reproduces the real SSHKeysBackend protocol's own
    # list_keys() return type verbatim (a row mapping whose values span str,
    # bool and None); narrowing it here would diverge from the contract the
    # service actually consumes.
    def list_keys(self) -> List[Dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# The proven data-loss vector
# ---------------------------------------------------------------------------


class TestCrossInstanceSharedManifestDeletionVector:
    """Two REAL services, two genuinely DIFFERENT real backends, ONE shared
    ssh_dir -- exactly the shape of the confirmed production incident."""

    def test_second_instance_with_different_backend_does_not_delete_first_key(
        self, tmp_path: Path
    ) -> None:
        shared_ssh = tmp_path / "ssh"

        # Instance A: the primary server. Its backend legitimately owns the key.
        backend_a = _real_backend(tmp_path / "primary.db")
        _register_key(backend_a, shared_ssh, "id_ed25519_gitlab")
        result_a = _service(backend_a, shared_ssh).sync()
        assert "id_ed25519_gitlab" in result_a["written"]
        assert (shared_ssh / "id_ed25519_gitlab").read_text() == PRIVATE_CONTENT

        # Instance B: an independent scratch/test server with its OWN, empty
        # backend, accidentally pointed at the same real ssh directory.
        backend_b = _real_backend(tmp_path / "scratch.db")
        assert backend_b.list_keys() == []

        result_b = _service(backend_b, shared_ssh).sync()

        # B must never delete a file its own backend never owned.
        assert result_b["removed"] == []
        assert (shared_ssh / "id_ed25519_gitlab").exists()
        assert (shared_ssh / "id_ed25519_gitlab.pub").exists()
        assert (shared_ssh / "id_ed25519_gitlab").read_text() == PRIVATE_CONTENT

    def test_second_instance_preserves_first_instances_manifest_namespace(
        self, tmp_path: Path
    ) -> None:
        """B's sync must not clobber A's provenance record -- otherwise A would
        silently lose the ability to clean up its OWN key later."""
        shared_ssh = tmp_path / "ssh"

        backend_a = _real_backend(tmp_path / "primary.db")
        _register_key(backend_a, shared_ssh, "cidx_github_key")
        svc_a = _service(backend_a, shared_ssh)
        first_result = svc_a.sync()
        assert "cidx_github_key" in first_result["written"]

        namespaces_before = _namespaces(shared_ssh)
        assert any(
            "cidx_github_key" in names for names in namespaces_before.values()
        ), namespaces_before

        backend_b = _real_backend(tmp_path / "scratch.db")
        result_b = _service(backend_b, shared_ssh).sync()
        assert result_b["removed"] == []

        namespaces_after = _namespaces(shared_ssh)
        for backend_id, names in namespaces_before.items():
            assert namespaces_after.get(backend_id) == names

        # And A still cleans up its own key when its backend genuinely drops it.
        backend_a.delete_key("cidx_github_key")
        result_a = svc_a.sync()
        assert "cidx_github_key" in result_a["removed"]
        assert not (shared_ssh / "cidx_github_key").exists()


# ---------------------------------------------------------------------------
# The LEGITIMATE cluster behaviour that must be preserved
# ---------------------------------------------------------------------------


class TestSameBackendMultiNodeCleanupPreserved:
    """In cluster mode every node shares ONE backend (the single source of
    truth) but has its OWN local ~/.ssh.  An admin deleting a key on node A
    must still cause node B's next sync() to clean up its orphaned local copy.
    """

    def test_shared_backend_two_nodes_stale_cleanup_still_works(
        self, tmp_path: Path
    ) -> None:
        shared_backend = _real_backend(tmp_path / "cluster.db")
        node_a_ssh = tmp_path / "node_a_ssh"
        node_b_ssh = tmp_path / "node_b_ssh"

        _register_key(shared_backend, node_a_ssh, "cluster_deploy_key")

        svc_node_a = _service(shared_backend, node_a_ssh)
        svc_node_b = _service(shared_backend, node_b_ssh)
        assert "cluster_deploy_key" in svc_node_a.sync()["written"]
        assert "cluster_deploy_key" in svc_node_b.sync()["written"]

        assert (node_a_ssh / "cluster_deploy_key").exists()
        assert (node_b_ssh / "cluster_deploy_key").exists()

        # Admin deletes the key on node A -> it disappears from the SHARED backend.
        shared_backend.delete_key("cluster_deploy_key")

        result_b = svc_node_b.sync()

        assert "cluster_deploy_key" in result_b["removed"]
        assert not (node_b_ssh / "cluster_deploy_key").exists()
        assert not (node_b_ssh / "cluster_deploy_key.pub").exists()

    def test_two_services_on_same_backend_share_one_manifest_namespace(
        self, tmp_path: Path
    ) -> None:
        """Backend identity must be a property of the BACKEND, not of the
        service object -- two separately-constructed services wired to the same
        backend must resolve to the same manifest namespace."""
        shared_backend = _real_backend(tmp_path / "cluster.db")
        ssh_dir = tmp_path / "ssh"
        _register_key(shared_backend, ssh_dir, "shared_key")

        assert "shared_key" in _service(shared_backend, ssh_dir).sync()["written"]
        namespaces = _namespaces(ssh_dir)
        assert len(namespaces) == 1

        # A second, independently constructed service on the SAME backend must
        # reuse that same namespace rather than creating a second one.
        second_backend = SSHKeysSqliteBackend(str(tmp_path / "cluster.db"))
        second_result = _service(second_backend, ssh_dir).sync()
        assert second_result["removed"] == []
        assert set(_namespaces(ssh_dir)) == set(namespaces)


# ---------------------------------------------------------------------------
# Fail-closed behaviour: unattributable manifests must never drive a deletion
# ---------------------------------------------------------------------------


class TestUnattributableManifestNeverDeletes:
    def test_legacy_v1_manifest_entries_are_never_adopted_or_deleted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pre-#1521 manifest carries no backend identity, so its entries have
        unknown provenance.  Adopting them would reproduce the exact data-loss
        vector on the first sync after upgrade; they must be dropped, never
        acted upon."""
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / MANIFEST_NAME).write_text(
            json.dumps({"keys": ["id_ed25519_gitlab"]})
        )
        (ssh_dir / "id_ed25519_gitlab").write_text(PRIVATE_CONTENT)
        (ssh_dir / "id_ed25519_gitlab.pub").write_text(PUBLIC_CONTENT)

        backend = _real_backend(tmp_path / "empty.db")

        with caplog.at_level(logging.WARNING):
            result = _service(backend, ssh_dir).sync()

        assert result["removed"] == []
        assert (ssh_dir / "id_ed25519_gitlab").read_text() == PRIVATE_CONTENT
        assert (ssh_dir / "id_ed25519_gitlab.pub").exists()
        assert any(
            "legacy" in record.message.lower() or "provenance" in record.message.lower()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), [r.message for r in caplog.records]

        # The legacy entries must not survive into the rewritten manifest --
        # a top-level "keys" list is exactly what an older node would read and
        # then delete against its own unrelated backend.
        assert "keys" not in _manifest(ssh_dir)

    def test_backend_without_derivable_identity_never_deletes(
        self, tmp_path: Path
    ) -> None:
        """If no stable backend identity can be derived, the service cannot
        attribute ANY manifest namespace to itself -- so it must fail closed
        and delete nothing."""
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {"version": 2, "backends": {"some-other-backend": ["victim_key"]}}
            )
        )
        (ssh_dir / "victim_key").write_text(PRIVATE_CONTENT)

        opaque_backend = MagicMock()
        opaque_backend.list_keys.return_value = []

        result = _service(opaque_backend, ssh_dir).sync()

        assert result["removed"] == []
        assert (ssh_dir / "victim_key").read_text() == PRIVATE_CONTENT
        # Another backend's namespace must be left untouched.
        assert _namespaces(ssh_dir)["some-other-backend"] == ["victim_key"]

    def test_foreign_namespace_entries_are_never_deleted(self, tmp_path: Path) -> None:
        """An explicit foreign namespace in an otherwise-valid v2 manifest is
        not eligible for this instance's staleness comparison."""
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / MANIFEST_NAME).write_text(
            json.dumps({"version": 2, "backends": {"not-my-backend": ["their_key"]}})
        )
        (ssh_dir / "their_key").write_text(PRIVATE_CONTENT)
        (ssh_dir / "their_key.pub").write_text(PUBLIC_CONTENT)

        backend = _real_backend(tmp_path / "mine.db")
        result = _service(backend, ssh_dir).sync()

        assert result["removed"] == []
        assert (ssh_dir / "their_key").exists()
        assert (ssh_dir / "their_key.pub").exists()


# ---------------------------------------------------------------------------
# Backend identity derivation contract
# ---------------------------------------------------------------------------


class TestSqliteBackendIdentityDerivation:
    def test_distinct_sqlite_databases_yield_distinct_identities(
        self, tmp_path: Path
    ) -> None:
        a = SSHKeySyncService(
            ssh_keys_backend=_real_backend(tmp_path / "a.db"),
            ssh_dir=str(tmp_path / "ssh"),
        )
        b = SSHKeySyncService(
            ssh_keys_backend=_real_backend(tmp_path / "b.db"),
            ssh_dir=str(tmp_path / "ssh"),
        )
        assert a.backend_identity is not None
        assert b.backend_identity is not None
        assert a.backend_identity != b.backend_identity

    def test_same_sqlite_database_yields_same_identity(self, tmp_path: Path) -> None:
        db_path = tmp_path / "same.db"
        _real_backend(db_path)
        a = SSHKeySyncService(
            ssh_keys_backend=SSHKeysSqliteBackend(str(db_path)),
            ssh_dir=str(tmp_path / "ssh_a"),
        )
        b = SSHKeySyncService(
            ssh_keys_backend=SSHKeysSqliteBackend(str(db_path)),
            ssh_dir=str(tmp_path / "ssh_b"),
        )
        assert a.backend_identity == b.backend_identity

    def test_explicit_backend_identity_overrides_derivation(
        self, tmp_path: Path
    ) -> None:
        backend = _real_backend(tmp_path / "explicit.db")
        svc = SSHKeySyncService(
            ssh_keys_backend=backend,
            ssh_dir=str(tmp_path / "ssh"),
            backend_identity="explicit-test-identity",
        )
        assert svc.backend_identity == "explicit-test-identity"


class TestPostgresBackendIdentityDerivation:
    def test_postgres_pool_identity_is_shared_across_nodes(self) -> None:
        """Cluster mode: every node builds its own backend object around its own
        pool object, but all point at the SAME PostgreSQL DSN -- so the derived
        identity must match, which is what keeps multi-node cleanup working."""
        node_1 = SSHKeySyncService(
            ssh_keys_backend=_PgLikeBackend(_FakePool(DSN_PRIMARY)),
            ssh_dir="/nonexistent/a",
        )
        node_2 = SSHKeySyncService(
            ssh_keys_backend=_PgLikeBackend(_FakePool(DSN_PRIMARY)),
            ssh_dir="/nonexistent/b",
        )
        other = SSHKeySyncService(
            ssh_keys_backend=_PgLikeBackend(_FakePool(DSN_OTHER)),
            ssh_dir="/nonexistent/c",
        )

        assert node_1.backend_identity is not None
        assert node_1.backend_identity == node_2.backend_identity
        assert node_1.backend_identity != other.backend_identity

    def test_identity_is_an_opaque_digest_never_the_raw_connection_string(
        self, tmp_path: Path
    ) -> None:
        """The connection string a DSN-based identity derives from must never be
        embedded verbatim in the manifest, which is a plain file on disk -- only
        an opaque digest of it may be persisted."""
        ssh_dir = tmp_path / "ssh"
        svc = SSHKeySyncService(
            ssh_keys_backend=_PgLikeBackend(_FakePool(DSN_PRIMARY)),
            ssh_dir=str(ssh_dir),
        )
        result = svc.sync()
        assert result["removed"] == []

        identity = svc.backend_identity
        assert identity is not None
        assert DSN_PRIMARY not in identity
        assert all(char in "0123456789abcdef" for char in identity), identity
        assert DSN_PRIMARY not in (ssh_dir / MANIFEST_NAME).read_text()
