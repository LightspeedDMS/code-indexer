"""
Unit tests for LlmLeaseStateManager (Story #365).

Uses tmp_path fixture for full test isolation.
"""

import json
import os

import pytest

from code_indexer.server.config.llm_lease_state import (
    LlmLeaseState,
    LlmLeaseStateManager,
)


# ---------------------------------------------------------------------------
# load_state() — no file scenario
# ---------------------------------------------------------------------------

class TestLoadStateNoFile:
    def test_load_returns_none_when_no_file_exists(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        result = manager.load_state()
        assert result is None

    def test_load_returns_none_when_dir_does_not_exist(self, tmp_path):
        nonexistent = tmp_path / "nonexistent_subdir"
        manager = LlmLeaseStateManager(server_dir_path=str(nonexistent))
        result = manager.load_state()
        assert result is None


# ---------------------------------------------------------------------------
# save_state() + load_state() roundtrip
# ---------------------------------------------------------------------------

class TestSaveLoadRoundtrip:
    def test_roundtrip_preserves_lease_id(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="lease-abc123", credential_id="cred-xyz")
        manager.save_state(state)

        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.lease_id == "lease-abc123"

    def test_roundtrip_preserves_credential_id(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="lease-abc123", credential_id="cred-xyz789")
        manager.save_state(state)

        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.credential_id == "cred-xyz789"

    def test_roundtrip_with_long_values(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        long_id = "x" * 256
        state = LlmLeaseState(lease_id=long_id, credential_id="cred-" + "y" * 100)
        manager.save_state(state)

        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.lease_id == long_id

    def test_roundtrip_second_save_overwrites_first(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state1 = LlmLeaseState(lease_id="first-lease", credential_id="cred-1")
        state2 = LlmLeaseState(lease_id="second-lease", credential_id="cred-2")

        manager.save_state(state1)
        manager.save_state(state2)

        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.lease_id == "second-lease"
        assert loaded.credential_id == "cred-2"

    def test_two_managers_with_same_dir_share_state(self, tmp_path):
        manager1 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        manager2 = LlmLeaseStateManager(server_dir_path=str(tmp_path))

        state = LlmLeaseState(lease_id="shared-lease", credential_id="shared-cred")
        manager1.save_state(state)

        loaded = manager2.load_state()
        assert loaded is not None
        assert loaded.lease_id == "shared-lease"


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

class TestFilePermissions:
    def test_saved_file_has_0o600_permissions(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="lease-perm", credential_id="cred-perm")
        manager.save_state(state)

        state_file = tmp_path / "llm_lease_state.json"
        assert state_file.exists()
        file_mode = state_file.stat().st_mode & 0o777
        assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    def test_save_creates_parent_directory(self, tmp_path):
        subdir = tmp_path / "new_server_dir"
        manager = LlmLeaseStateManager(server_dir_path=str(subdir))
        state = LlmLeaseState(lease_id="lease-dir", credential_id="cred-dir")
        manager.save_state(state)

        assert subdir.exists()
        state_file = subdir / "llm_lease_state.json"
        assert state_file.exists()


# ---------------------------------------------------------------------------
# Encryption verification
# ---------------------------------------------------------------------------

class TestEncryption:
    def test_raw_file_content_is_not_plaintext_json(self, tmp_path):
        """The stored file must not contain plaintext lease_id or credential_id."""
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(
            lease_id="plaintext-lease-id-sentinel",
            credential_id="plaintext-cred-id-sentinel",
        )
        manager.save_state(state)

        state_file = tmp_path / "llm_lease_state.json"
        raw_content = state_file.read_text()

        assert "plaintext-lease-id-sentinel" not in raw_content
        assert "plaintext-cred-id-sentinel" not in raw_content

    def test_raw_file_is_not_valid_plain_json_with_state_keys(self, tmp_path):
        """The file should not be a JSON object with plain lease_id/credential_id keys."""
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="some-lease", credential_id="some-cred")
        manager.save_state(state)

        state_file = tmp_path / "llm_lease_state.json"
        raw_content = state_file.read_text()

        # Either the file is not valid JSON, OR it doesn't have plain state keys
        try:
            parsed = json.loads(raw_content)
            # If it IS valid JSON, it must not directly expose the sensitive fields
            assert parsed.get("lease_id") != "some-lease"
            assert parsed.get("credential_id") != "some-cred"
        except json.JSONDecodeError:
            # Non-JSON file (e.g., raw encrypted bytes encoded as base64) is fine
            pass


# ---------------------------------------------------------------------------
# clear_state()
# ---------------------------------------------------------------------------

class TestClearState:
    def test_clear_state_deletes_file(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="lease-to-clear", credential_id="cred-to-clear")
        manager.save_state(state)

        state_file = tmp_path / "llm_lease_state.json"
        assert state_file.exists()

        manager.clear_state()
        assert not state_file.exists()

    def test_clear_state_when_no_file_does_not_raise(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        # No file exists — should not raise
        manager.clear_state()

    def test_load_after_clear_returns_none(self, tmp_path):
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="lease-x", credential_id="cred-x")
        manager.save_state(state)
        manager.clear_state()

        result = manager.load_state()
        assert result is None
