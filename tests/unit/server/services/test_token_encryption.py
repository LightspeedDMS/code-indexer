"""
Unit tests for shared token_encryption module (Story #999 / Finding 5).

Tests:
- Module exports five required functions (explicit hasattr checks)
- Constants PBKDF2_ITERATIONS, AES_KEY_SIZE, AES_BLOCK_SIZE have correct values
- derive_key_from_salt: same salt -> same 32-byte key, different salts -> different keys
- derive_encryption_key: salt file > cluster_secret > hostname priority
- encrypt_token/decrypt_single: roundtrip, base64 output, different IVs per call,
  plaintext not exposed in ciphertext
- Exception contracts: wrong key -> ValueError; invalid base64 -> binascii.Error;
  both-keys-fail -> ValueError
- decrypt_with_fallback: (plaintext, False) on canonical, (plaintext, True) on fallback,
  ValueError when both fail; context_label is logging-only
"""

import binascii
import os

import pytest

import src.code_indexer.server.services.token_encryption as token_encryption_module
from src.code_indexer.server.services.token_encryption import (
    decrypt_single,
    decrypt_with_fallback,
    derive_encryption_key,
    derive_key_from_salt,
    encrypt_token,
)


def _hostname() -> str:
    """Return current machine hostname (single access point for tests)."""
    return os.uname().nodename


class TestModuleExports:
    """Verify the module exports all required names."""

    def test_module_has_derive_key_from_salt(self):
        assert hasattr(token_encryption_module, "derive_key_from_salt")
        assert callable(token_encryption_module.derive_key_from_salt)

    def test_module_has_derive_encryption_key(self):
        assert hasattr(token_encryption_module, "derive_encryption_key")
        assert callable(token_encryption_module.derive_encryption_key)

    def test_module_has_encrypt_token(self):
        assert hasattr(token_encryption_module, "encrypt_token")
        assert callable(token_encryption_module.encrypt_token)

    def test_module_has_decrypt_single(self):
        assert hasattr(token_encryption_module, "decrypt_single")
        assert callable(token_encryption_module.decrypt_single)

    def test_module_has_decrypt_with_fallback(self):
        assert hasattr(token_encryption_module, "decrypt_with_fallback")
        assert callable(token_encryption_module.decrypt_with_fallback)

    def test_module_has_pbkdf2_iterations_constant(self):
        assert hasattr(token_encryption_module, "PBKDF2_ITERATIONS")
        assert token_encryption_module.PBKDF2_ITERATIONS == 100000

    def test_module_has_aes_key_size_constant(self):
        assert hasattr(token_encryption_module, "AES_KEY_SIZE")
        assert token_encryption_module.AES_KEY_SIZE == 32

    def test_module_has_aes_block_size_constant(self):
        assert hasattr(token_encryption_module, "AES_BLOCK_SIZE")
        assert token_encryption_module.AES_BLOCK_SIZE == 16


class TestDeriveKeyFromSalt:
    """Tests for derive_key_from_salt()."""

    def test_returns_32_byte_key(self):
        key = derive_key_from_salt("test-salt")
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_same_salt_produces_same_key(self):
        key1 = derive_key_from_salt("consistent-salt")
        key2 = derive_key_from_salt("consistent-salt")
        assert key1 == key2

    def test_different_salts_produce_different_keys(self):
        key1 = derive_key_from_salt("salt-a")
        key2 = derive_key_from_salt("salt-b")
        assert key1 != key2

    def test_hostname_based_key_is_deterministic(self):
        key1 = derive_key_from_salt(_hostname())
        key2 = derive_key_from_salt(_hostname())
        assert key1 == key2


class TestDeriveEncryptionKey:
    """Tests for derive_encryption_key() priority logic."""

    def test_uses_salt_file_when_server_dir_and_file_present(self, tmp_path):
        """Salt file content takes priority when server_dir and .encryption_key_salt exist."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        (server_dir / ".encryption_key_salt").write_text("my-unique-salt-value")

        key = derive_encryption_key(
            server_dir_for_salt=server_dir, cluster_secret="other-secret"
        )
        assert key == derive_key_from_salt("my-unique-salt-value")

    def test_uses_cluster_secret_when_no_salt_file(self, tmp_path):
        """cluster_secret is used when server_dir has no .encryption_key_salt."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        key = derive_encryption_key(
            server_dir_for_salt=server_dir, cluster_secret="cluster-shared-secret"
        )
        assert key == derive_key_from_salt("cluster-shared-secret")

    def test_falls_back_to_hostname_when_no_server_dir_no_cluster_secret(self):
        """Without server_dir and cluster_secret, uses hostname."""
        key = derive_encryption_key(server_dir_for_salt=None, cluster_secret=None)
        assert key == derive_key_from_salt(_hostname())

    def test_falls_back_to_hostname_when_cluster_secret_is_none_and_no_salt_file(
        self, tmp_path
    ):
        """Falls back to hostname when no salt file and cluster_secret is None."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        key = derive_encryption_key(server_dir_for_salt=server_dir, cluster_secret=None)
        assert key == derive_key_from_salt(_hostname())


class TestEncryptDecryptSingle:
    """Tests for encrypt_token() and decrypt_single() roundtrip."""

    def _get_test_key(self) -> bytes:
        return derive_key_from_salt("test-key-salt")

    def test_encrypt_returns_non_empty_base64_string(self):
        import re

        key = self._get_test_key()
        result = encrypt_token("sample-secret-payload", key)
        assert isinstance(result, str)
        assert len(result) > 0
        assert re.match(r"^[A-Za-z0-9+/=]+$", result)

    def test_encrypt_does_not_expose_plaintext(self):
        key = self._get_test_key()
        plaintext = "super-secret-token-value"
        result = encrypt_token(plaintext, key)
        assert plaintext not in result

    def test_roundtrip_recovers_original(self):
        key = self._get_test_key()
        plaintext = "test-secret-payload-alpha"
        encrypted = encrypt_token(plaintext, key)
        recovered = decrypt_single(encrypted, key)
        assert recovered == plaintext

    def test_different_encryptions_of_same_value_differ(self):
        """Each encrypt call generates a random IV, so ciphertexts differ."""
        key = self._get_test_key()
        plaintext = "test-secret-payload-beta"
        enc1 = encrypt_token(plaintext, key)
        enc2 = encrypt_token(plaintext, key)
        assert enc1 != enc2

    def test_decrypt_single_raises_value_error_on_wrong_key(self):
        """Wrong key causes PKCS7 unpadding to fail with ValueError."""
        key_a = derive_key_from_salt("key-a")
        key_b = derive_key_from_salt("key-b")
        encrypted = encrypt_token("some-secret-token", key_a)
        with pytest.raises(ValueError):
            decrypt_single(encrypted, key_b)

    def test_decrypt_single_raises_binascii_error_on_invalid_base64(self):
        """Input that is not valid base64 raises binascii.Error (Python stdlib contract)."""
        key = self._get_test_key()
        with pytest.raises(binascii.Error):
            decrypt_single("@@@@not-valid-base64@@@@", key)


class TestDecryptWithFallback:
    """Tests for decrypt_with_fallback() — tries canonical key then hostname fallback."""

    def test_returns_plaintext_and_false_when_canonical_key_works(self):
        key = derive_key_from_salt("canonical-salt")
        encrypted = encrypt_token("my-secret-token-value", key)
        plaintext, used_fallback = decrypt_with_fallback(
            encrypted, canonical_key=key, context_label="test"
        )
        assert plaintext == "my-secret-token-value"
        assert used_fallback is False

    def test_returns_plaintext_and_true_when_fallback_needed(self):
        """When canonical key fails, tries hostname-derived fallback key."""
        hostname_key = derive_key_from_salt(_hostname())
        encrypted = encrypt_token("my-secret-token-value", hostname_key)

        different_canonical = derive_key_from_salt("some-other-salt")
        plaintext, used_fallback = decrypt_with_fallback(
            encrypted, canonical_key=different_canonical, context_label="test"
        )
        assert plaintext == "my-secret-token-value"
        assert used_fallback is True

    def test_raises_value_error_when_both_keys_fail(self):
        """If both canonical and hostname keys are wrong, raises ValueError (PKCS7 unpadding)."""
        third_key = derive_key_from_salt("third-unrelated-salt")
        encrypted = encrypt_token("my-secret-token-value", third_key)

        canonical = derive_key_from_salt("canonical-not-matching")
        with pytest.raises(ValueError):
            decrypt_with_fallback(
                encrypted, canonical_key=canonical, context_label="test-raises"
            )

    def test_context_label_does_not_affect_result(self):
        """context_label is for logging only — same result with different labels."""
        key = derive_key_from_salt("shared-key-for-label-test")
        encrypted = encrypt_token("sample-test-token", key)

        result_a = decrypt_with_fallback(encrypted, key, context_label="manager-A")
        result_b = decrypt_with_fallback(encrypted, key, context_label="manager-B")
        assert result_a == result_b
