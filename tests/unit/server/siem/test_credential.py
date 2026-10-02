"""The SecOps service-account credential: validation on save, and encrypted,
write-only storage in the SIEM database (SQLite AND PostgreSQL).

Encryption reuses the server's stored-secret mechanism
(``services/token_encryption.py``); the key here is derived from a test salt
exactly as the server derives it from ``.encryption_key_salt``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from code_indexer.server.services.siem_delivery.credential import (
    SiemCredentialInvalid,
    SiemCredentialStore,
    validate_service_account_json,
)
from code_indexer.server.services.siem_delivery.destination import SECOPS_TOKEN_URI
from code_indexer.server.services.siem_delivery.sender import (
    CredentialError,
    ProbeResult,
)
from code_indexer.server.services.token_encryption import derive_key_from_salt
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness

TEST_KEY = derive_key_from_salt("siem-credential-test-salt")
OTHER_KEY = derive_key_from_salt("another-salt")


def _deployed_doc(sidecar: SidecarHandle) -> Dict[str, Any]:
    doc: Dict[str, Any] = dict(sidecar.read_key_file())
    doc["token_uri"] = SECOPS_TOKEN_URI
    return doc


def _reason(text: str, *, harness_active: bool = False) -> str:
    with pytest.raises(SiemCredentialInvalid) as exc:
        validate_service_account_json(text, harness_active=harness_active)
    return exc.value.reason


# --- validation -------------------------------------------------------------


def test_valid_deployed_key_is_accepted(siem_sidecar: SidecarHandle) -> None:
    doc = _deployed_doc(siem_sidecar)
    info = validate_service_account_json(json.dumps(doc), harness_active=False)
    assert info["client_email"] == doc["client_email"]


def test_harness_token_uri_is_allowed_only_behind_the_gate(
    siem_sidecar: SidecarHandle,
) -> None:
    text = json.dumps(siem_sidecar.read_key_file())
    assert validate_service_account_json(text, harness_active=True)
    assert "token_uri" in _reason(text, harness_active=False)


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/token",
        "https://oauth2.googleapis.com/token?x=1",
        "http://192.0.2.10:8080/token",
        "http://127.0.0.1:8080/oauth/token",
    ],
)
def test_token_uri_outside_the_allowlist_is_rejected(
    siem_sidecar: SidecarHandle, uri: str
) -> None:
    doc = _deployed_doc(siem_sidecar)
    doc["token_uri"] = uri
    assert "token_uri" in _reason(json.dumps(doc), harness_active=True)


def test_json_that_does_not_parse_is_rejected() -> None:
    assert "JSON" in _reason("{not json")
    assert "JSON object" in _reason("[1, 2]")


def test_wrong_type_is_rejected(siem_sidecar: SidecarHandle) -> None:
    doc = _deployed_doc(siem_sidecar)
    doc["type"] = "authorized_user"
    assert "service_account" in _reason(json.dumps(doc))


@pytest.mark.parametrize(
    "field", ["client_email", "private_key", "private_key_id", "token_uri"]
)
def test_each_required_field_is_required(
    siem_sidecar: SidecarHandle, field: str
) -> None:
    doc = _deployed_doc(siem_sidecar)
    del doc[field]
    assert field in _reason(json.dumps(doc))


def test_private_key_that_does_not_load_is_rejected(
    siem_sidecar: SidecarHandle,
) -> None:
    doc = _deployed_doc(siem_sidecar)
    doc["private_key"] = (
        "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n"
    )
    assert "private key" in _reason(json.dumps(doc))


def _pkcs8(key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return str(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
    )


def test_non_rsa_private_keys_are_rejected(siem_sidecar: SidecarHandle) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    for key in (
        ec.generate_private_key(ec.SECP256R1()),
        ed25519.Ed25519PrivateKey.generate(),
    ):
        doc = _deployed_doc(siem_sidecar)
        doc["private_key"] = _pkcs8(key)
        assert "RSA" in _reason(json.dumps(doc))


def test_rejection_never_echoes_key_material(siem_sidecar: SidecarHandle) -> None:
    doc = _deployed_doc(siem_sidecar)
    doc["token_uri"] = "https://example.com/token"
    with pytest.raises(SiemCredentialInvalid) as exc:
        validate_service_account_json(json.dumps(doc), harness_active=False)
    pem_line = doc["private_key"].splitlines()[1]
    assert pem_line not in str(exc.value)
    assert "example.com" not in str(exc.value)


# --- storage (both backends) -------------------------------------------------


def _raw_rows(b: SiemBackendHarness) -> str:
    rows = b.db.read(lambda tx: tx.query("SELECT * FROM siem_delivery_credential"))
    return json.dumps(rows, default=str)


def test_store_round_trip_is_encrypted_and_write_only(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    store = SiemCredentialStore(siem_backend.db, TEST_KEY)
    assert store.identity() is None and store.load() is None
    doc = _deployed_doc(siem_sidecar)
    change, identity = store.set(doc, actor="alice")
    assert change == "set"
    assert identity["client_email"] == doc["client_email"]
    assert identity["private_key_id"] == doc["private_key_id"]
    assert identity["set_by"] == "alice" and identity["set_at"]
    assert "private_key" not in identity
    raw = _raw_rows(siem_backend)
    assert doc["private_key"].splitlines()[1] not in raw
    loaded = store.load()
    assert loaded is not None and loaded.info == doc
    first_id = loaded.credential_id

    change, _ = store.set(doc, actor="bob")
    assert change == "replaced"
    replaced = store.load()
    assert replaced is not None and replaced.credential_id != first_id
    assert store.identity()["set_by"] == "bob"  # type: ignore[index]

    removed = store.remove(actor="carol")
    assert removed is not None
    assert removed["private_key_id"] == doc["private_key_id"]
    assert store.identity() is None and store.load() is None
    assert store.remove(actor="carol") is None


def test_ciphertext_under_another_key_is_credential_key_mismatch(
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row stored under another encryption key is a KEY MISMATCH (the
    stored key-check value says so), never an 'invalid key'; logged once."""
    SiemCredentialStore(siem_backend.db, TEST_KEY).set(
        _deployed_doc(siem_sidecar), actor="alice"
    )
    other = SiemCredentialStore(siem_backend.db, OTHER_KEY)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(CredentialError) as exc:
                other.load()
            assert exc.value.result is ProbeResult.CREDENTIAL_KEY_MISMATCH
    logged = [r for r in caplog.records if "different encryption key" in r.message]
    assert len(logged) == 1


def test_corrupt_ciphertext_under_the_right_key_is_credential_invalid(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    store = SiemCredentialStore(siem_backend.db, TEST_KEY)
    store.set(_deployed_doc(siem_sidecar), actor="alice")
    siem_backend.raw("UPDATE siem_delivery_credential SET encrypted_key = 'AAAA'")
    with pytest.raises(CredentialError) as exc:
        store.load()
    assert exc.value.result is ProbeResult.CREDENTIAL_INVALID


def test_cluster_nodes_with_different_local_salts_share_one_credential(
    pg_pool: Any, siem_sidecar: SidecarHandle, tmp_path: Path
) -> None:
    """Cluster mode: the key derives from the SHARED JWT secret row in
    cluster_secrets, never the node-local .encryption_key_salt."""
    from code_indexer.server.services.siem_delivery import lifecycle
    from code_indexer.server.services.siem_delivery.db import SiemDb

    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cluster_secrets (key_name TEXT PRIMARY KEY, "
            "key_value TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())"
        )
        conn.execute(
            "INSERT INTO cluster_secrets (key_name, key_value) VALUES "
            "('jwt_secret', 'example-shared-jwt-secret') "
            "ON CONFLICT (key_name) DO NOTHING"
        )
    nodes = []
    for name in ("node-a", "node-b"):
        server_dir = tmp_path / name
        server_dir.mkdir()
        (server_dir / ".encryption_key_salt").write_text(f"salt-of-{name}")
        config = SimpleNamespace(
            config_manager=SimpleNamespace(server_dir=str(server_dir)),
            get_config=lambda: SimpleNamespace(storage_mode="postgres"),
        )
        nodes.append(lifecycle._credential_store(SiemDb.postgres(pg_pool), config))
    nodes[0].set(_deployed_doc(siem_sidecar), actor="alice")
    loaded = nodes[1].load()
    assert loaded is not None
    assert (
        loaded.info["private_key_id"] == _deployed_doc(siem_sidecar)["private_key_id"]
    )
