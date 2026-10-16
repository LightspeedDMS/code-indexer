"""The optional additional trusted CA for SIEM delivery: validation on save,
its displayed identity, and the combined trust it builds (default trust
PLUS the CA; verification never disabled)."""

from __future__ import annotations

import hashlib
import ssl
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from code_indexer.server.services.siem_delivery.trust import (
    SiemTrustInvalid,
    describe_ca_pem,
    ssl_context_for,
    validate_ca_pem,
)

from .tls_fixtures import make_ca, make_leaf


def _reason(text: str) -> str:
    with pytest.raises(SiemTrustInvalid) as exc:
        validate_ca_pem(text)
    return exc.value.reason


def test_one_ca_is_accepted_with_its_fingerprint() -> None:
    ca = make_ca("Example Root CA")
    trusted = validate_ca_pem("\n  " + ca.pem + "\n")
    der = ca.cert.public_bytes(serialization.Encoding.DER)
    assert trusted.fingerprint == hashlib.sha256(der).hexdigest()
    assert trusted.pem == ca.pem


def test_a_bundle_of_cas_is_accepted() -> None:
    first, second = make_ca("Example CA One"), make_ca("Example CA Two")
    trusted = validate_ca_pem(first.pem + second.pem)
    ders = b"".join(
        c.cert.public_bytes(serialization.Encoding.DER) for c in (first, second)
    )
    assert trusted.fingerprint == hashlib.sha256(ders).hexdigest()
    assert [d["subject"] for d in describe_ca_pem(trusted.pem)] == [
        "CN=Example CA One",
        "CN=Example CA Two",
    ]


def test_description_shows_subject_issuer_fingerprint_and_expiry() -> None:
    ca = make_ca("Example Root CA")
    (row,) = describe_ca_pem(ca.pem)
    der = ca.cert.public_bytes(serialization.Encoding.DER)
    assert row["subject"] == "CN=Example Root CA"
    assert row["issuer"] == "CN=Example Root CA"
    assert row["sha256"] == hashlib.sha256(der).hexdigest()
    assert row["not_after"].startswith(str(ca.cert.not_valid_after_utc.year))


def test_a_non_ca_certificate_is_rejected() -> None:
    leaf = make_leaf(make_ca())
    assert "not a CA" in _reason(leaf.pem)
    assert "not a CA" in _reason(make_ca(is_ca=False).pem)
    assert "not a CA" in _reason(make_ca(basic_constraints=False).pem)


def test_an_expired_ca_is_rejected_even_inside_a_bundle() -> None:
    good, expired = make_ca("Example Good CA"), make_ca("Example Old CA", expired=True)
    assert "expired" in _reason(expired.pem)
    assert "expired" in _reason(good.pem + expired.pem)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a certificate",
        "-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n",
    ],
)
def test_input_that_is_not_certificates_is_rejected(text: str) -> None:
    assert "X.509" in _reason(text)


def test_a_private_key_block_is_rejected() -> None:
    ca = make_ca()
    key = ca.key_pem().decode("ascii")
    assert "only certificates" in _reason(ca.pem + key)


class _RejectingConfig:
    """Config-service double: the audited publish rejects the change."""

    def get_config(self) -> Any:
        return SimpleNamespace(
            siem_delivery_config=SimpleNamespace(
                trusted_ca_pem="", trusted_ca_fingerprint=""
            )
        )

    def apply_audited_change(self, mutate: Any, **kwargs: Any) -> None:
        raise ValueError("configuration validation failed")


def test_a_rejected_config_publish_is_a_clean_trust_error() -> None:
    from code_indexer.server.services.siem_delivery.trust import set_trusted_ca

    with pytest.raises(SiemTrustInvalid) as exc:
        set_trusted_ca(_RejectingConfig(), "alice", make_ca().pem)
    assert "configuration change was rejected" in exc.value.reason


def test_combined_trust_adds_to_the_default_and_always_verifies() -> None:
    ca = make_ca()
    context = ssl_context_for(ca.pem)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # the clients' default trust (certifi, via httpx) is KEPT, plus the CA
    default = httpx.create_ssl_context().cert_store_stats()["x509_ca"]
    assert default > 0
    assert context.cert_store_stats()["x509_ca"] == default + 1
    assert ssl_context_for(ca.pem) is context  # built once per bundle
