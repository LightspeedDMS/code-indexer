"""The optional additional trusted CA for SIEM delivery's outbound TLS.

An operator may add CA certificates (PEM, one or more) for a TLS-inspecting
proxy or a test emulator.  They ADD to the default trust the clients
already use (httpx's default context: certifi, or ``SSL_CERT_FILE``); they
never replace it, and verification is never disabled -- there is no
"skip verify" anywhere.  The combined context is built once per CA bundle
(per config version that changes it) and reused for every request.

The PEM is not secret (public certificates); it lives in the
``siem_delivery`` config section beside its bundle fingerprint, which the
config-change audit records as a value.
"""

from __future__ import annotations

import hashlib
import re
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_CA_PEM_BYTES = 256 * 1024
_PEM_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")


class SiemTrustInvalid(ValueError):
    """A submitted CA bundle is unusable (the reason names no value)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"trusted CA rejected: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class TrustedCa:
    pem: str  # normalised: the certificates re-encoded, nothing else
    fingerprint: str  # SHA-256 over the DER of every certificate, in order


def _load(text: str) -> List[Any]:
    from cryptography import x509

    if len(text.encode("utf-8")) > MAX_CA_PEM_BYTES:
        raise SiemTrustInvalid("the CA bundle is larger than 256 KiB")
    blocks = _PEM_BLOCK_RE.findall(text)
    if not blocks:
        raise SiemTrustInvalid("the input is not X.509 certificates in PEM form")
    if any(kind != "CERTIFICATE" for kind in blocks):
        raise SiemTrustInvalid("the bundle must contain only certificates")
    try:
        certs = list(x509.load_pem_x509_certificates(text.encode("utf-8")))
    except ValueError:
        raise SiemTrustInvalid(
            "the input is not X.509 certificates in PEM form"
        ) from None
    if len(certs) != len(blocks):
        raise SiemTrustInvalid("the input is not X.509 certificates in PEM form")
    return certs


def _is_ca(cert: Any) -> bool:
    from cryptography import x509

    try:
        ext = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    except x509.ExtensionNotFound:
        return False
    return bool(ext.value.ca)


def _der(cert: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return bytes(cert.public_bytes(serialization.Encoding.DER))


def _pem(cert: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return str(cert.public_bytes(serialization.Encoding.PEM).decode("ascii"))


def validate_ca_pem(text: str, *, now: Optional[datetime] = None) -> TrustedCa:
    """The normalised bundle when every certificate is an unexpired CA."""
    certs = _load(text.strip())
    moment = now or datetime.now(timezone.utc)
    for index, cert in enumerate(certs, start=1):
        if not _is_ca(cert):
            raise SiemTrustInvalid(f"certificate {index} is not a CA (CA=true)")
        if cert.not_valid_after_utc < moment:
            raise SiemTrustInvalid(f"certificate {index} has expired")
    fingerprint = hashlib.sha256(b"".join(_der(c) for c in certs)).hexdigest()
    return TrustedCa("".join(_pem(c) for c in certs), fingerprint)


def describe_ca_pem(pem: str) -> List[Dict[str, str]]:
    """Subject, issuer, SHA-256 fingerprint and expiry of each certificate."""
    if not pem.strip():
        return []
    return [
        {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "sha256": hashlib.sha256(_der(cert)).hexdigest(),
            "not_after": cert.not_valid_after_utc.isoformat(),
        }
        for cert in _load(pem.strip())
    ]


def check_stored_ca_pem(pem: str) -> None:
    """Use-time check of a STORED bundle: certificates and CAs only (expiry
    was enforced on save; an expired CA simply fails verification)."""
    for index, cert in enumerate(_load(pem.strip()), start=1):
        if not _is_ca(cert):
            raise SiemTrustInvalid(f"certificate {index} is not a CA (CA=true)")


def _apply_ca(config_service: Any, actor: str, pem: str, fingerprint: str) -> None:
    """ONE audited config change of the siem_delivery section (the
    config_changed row records the fingerprint's before/after values)."""

    def _mutate(candidate: Any) -> None:
        section = candidate.siem_delivery_config
        section.trusted_ca_pem = pem
        section.trusted_ca_fingerprint = fingerprint

    try:
        config_service.apply_audited_change(
            _mutate, actor=actor, target_id="siem_delivery"
        )
    except ValueError:
        # the section (with the CA) failed configuration validation; nothing
        # was published (the audited path records the failure row)
        raise SiemTrustInvalid(
            "the configuration change was rejected by validation"
        ) from None


def set_trusted_ca(config_service: Any, actor: str, text: str) -> Dict[str, Any]:
    """Validate and set (or replace) the trusted CA bundle; the response
    carries the fingerprint and each certificate's description."""
    trusted = validate_ca_pem(text)
    current = config_service.get_config().siem_delivery_config
    change = "replaced" if current.trusted_ca_pem else "set"
    _apply_ca(config_service, actor, trusted.pem, trusted.fingerprint)
    return {
        "change": change,
        "fingerprint": trusted.fingerprint,
        "certificates": describe_ca_pem(trusted.pem),
    }


def remove_trusted_ca(config_service: Any, actor: str) -> Dict[str, Any]:
    current = config_service.get_config().siem_delivery_config
    if not current.trusted_ca_pem:
        raise SiemTrustInvalid("no additional trusted CA is configured")
    fingerprint = current.trusted_ca_fingerprint
    _apply_ca(config_service, actor, "", "")
    return {"change": "removed", "fingerprint": fingerprint, "certificates": []}


# Per-process wiring: the combined context for the current bundle (keyed by
# the bundle text), rebuilt only when the configured bundle changes.
_context_lock = threading.Lock()
_context: List[Tuple[str, ssl.SSLContext]] = []


def ssl_context_for(pem: str) -> ssl.SSLContext:
    """Default trust PLUS *pem*; certificate and hostname verification ON."""
    import httpx

    with _context_lock:
        if _context and _context[0][0] == pem:
            return _context[0][1]
        context = httpx.create_ssl_context()  # the clients' default trust
        context.load_verify_locations(cadata=pem)
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise RuntimeError(
                "SIEM delivery TLS context would not verify certificates"
            )
        _context[:] = [(pem, context)]
        return context
