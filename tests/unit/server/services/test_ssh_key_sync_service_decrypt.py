"""
Unit tests for SSHKeySyncService Fernet decrypt support (Bug #1072, Chunk 3, Step 7).

Tests:
- Fernet-encrypted private key is decrypted before being written to disk (mode 0600).
- Bad ciphertext: error recorded, key file NOT written.
- Solo passthrough: fernet=None → plaintext written as-is.
- No private content: only public key written, no crash.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(keys: list) -> MagicMock:
    backend = MagicMock()
    backend.list_keys.return_value = keys
    return backend


def _make_service(backend, ssh_dir: Path, fernet=None):
    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    return SSHKeySyncService(
        ssh_keys_backend=backend, ssh_dir=str(ssh_dir), fernet=fernet
    )


# ---------------------------------------------------------------------------
# Test: decrypt-on-read
# ---------------------------------------------------------------------------


class TestDecryptOnRead:
    def test_decrypt_on_read_writes_plaintext(self, tmp_path: Path) -> None:
        """Backend stores Fernet-encrypted private key; sync writes the decrypted text."""
        fernet = Fernet(Fernet.generate_key())
        plaintext = b"PRIVATE_CONTENT"
        encrypted = fernet.encrypt(plaintext).decode()

        backend = _make_backend(
            [
                {
                    "name": "mykey",
                    "private_key": encrypted,
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)

        result = svc.sync()

        private_file = tmp_path / "mykey"
        assert private_file.exists(), "Private key file should be written"
        assert private_file.read_text() == plaintext.decode()
        assert result["written"] == ["mykey"]
        assert result["errors"] == []

    def test_decrypt_on_read_file_mode_600(self, tmp_path: Path) -> None:
        """Decrypted private key file must have mode 0600."""
        fernet = Fernet(Fernet.generate_key())
        encrypted = fernet.encrypt(b"PRIVATE_CONTENT").decode()

        backend = _make_backend(
            [
                {
                    "name": "mykey",
                    "private_key": encrypted,
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)
        svc.sync()

        mode = stat.S_IMODE(os.stat(tmp_path / "mykey").st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Test: bad ciphertext
# ---------------------------------------------------------------------------


class TestBadCiphertextHandled:
    def test_bad_ciphertext_records_error(self, tmp_path: Path) -> None:
        """Invalid Fernet ciphertext: error recorded in result, no exception raised."""
        fernet = Fernet(Fernet.generate_key())

        backend = _make_backend(
            [
                {
                    "name": "badkey",
                    "private_key": "not-valid-fernet",
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)

        result = svc.sync()

        assert len(result["errors"]) == 1, f"Expected 1 error, got: {result['errors']}"
        assert "badkey" in result["errors"][0]
        assert "decrypt" in result["errors"][0].lower()

    def test_bad_ciphertext_does_not_write_private_key(self, tmp_path: Path) -> None:
        """With bad ciphertext, the private key file must NOT be created."""
        fernet = Fernet(Fernet.generate_key())

        backend = _make_backend(
            [
                {
                    "name": "badkey",
                    "private_key": "not-valid-fernet",
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)
        svc.sync()

        assert not (tmp_path / "badkey").exists(), "Corrupt key must NOT be written"

    def test_bad_ciphertext_does_not_raise(self, tmp_path: Path) -> None:
        """sync() must not raise even when decryption fails."""
        fernet = Fernet(Fernet.generate_key())

        backend = _make_backend(
            [
                {
                    "name": "badkey",
                    "private_key": "not-valid-fernet",
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)

        # Must not raise
        result = svc.sync()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Test: solo passthrough (fernet=None)
# ---------------------------------------------------------------------------


class TestSoloPassthrough:
    def test_fernet_none_writes_plaintext_as_is(self, tmp_path: Path) -> None:
        """When fernet=None, plaintext private key written unchanged."""
        backend = _make_backend(
            [
                {
                    "name": "solokey",
                    "private_key": "PLAINTEXT_KEY",
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=None)

        result = svc.sync()

        assert (tmp_path / "solokey").read_text() == "PLAINTEXT_KEY"
        assert result["errors"] == []

    def test_fernet_none_no_decrypt_error(self, tmp_path: Path) -> None:
        """fernet=None must not attempt decryption or raise any error."""
        backend = _make_backend(
            [
                {
                    "name": "solokey",
                    "private_key": "not-valid-fernet-but-fernet-is-none",
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=None)
        result = svc.sync()

        assert result["errors"] == []
        assert (
            tmp_path / "solokey"
        ).read_text() == "not-valid-fernet-but-fernet-is-none"


# ---------------------------------------------------------------------------
# Test: no private content
# ---------------------------------------------------------------------------


class TestNoPrivateContent:
    def test_none_private_key_writes_only_public(self, tmp_path: Path) -> None:
        """private_key=None: public key file written, private key file skipped."""
        fernet = Fernet(Fernet.generate_key())

        backend = _make_backend(
            [
                {
                    "name": "pubonly",
                    "private_key": None,
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)

        result = svc.sync()

        assert (tmp_path / "pubonly.pub").exists()
        assert not (tmp_path / "pubonly").exists()
        assert result["errors"] == []

    def test_none_private_key_no_crash(self, tmp_path: Path) -> None:
        """Ensure no exception when private_key is None even with fernet set."""
        fernet = Fernet(Fernet.generate_key())

        backend = _make_backend(
            [
                {
                    "name": "pubonly",
                    "private_key": None,
                    "public_key": "ssh-ed25519 AAAA comment",
                    "fingerprint": "SHA256:fake",
                    "key_type": "ed25519",
                    "hosts": [],
                }
            ]
        )
        svc = _make_service(backend, tmp_path, fernet=fernet)

        # Must not raise
        result = svc.sync()
        assert isinstance(result, dict)
