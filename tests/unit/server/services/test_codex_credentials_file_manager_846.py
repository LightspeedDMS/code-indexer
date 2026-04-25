"""
Unit tests for CodexCredentialsFileManager and _provider_response_to_auth_json
(Story #846).

Uses tmp_path fixture for full test isolation.
Covers: file writes, schema validation, 0o600 permissions, atomic write pattern,
read roundtrip, None on missing/corrupt, idempotent delete, exists(), provider
response transformer (field mapping, SPIKE fallbacks, ValueError on bad inputs).
No live network calls — all tests use filesystem only.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Generator, List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services.codex_credentials_file_manager import (
    CodexCredentialsFileManager,
    _provider_response_to_auth_json,
)


# ---------------------------------------------------------------------------
# Neutral test constants — must NOT resemble real credentials
# ---------------------------------------------------------------------------

TEST_ACCESS_TOKEN = "test_access_token"
TEST_REFRESH_TOKEN = "test_refresh_token"
TEST_ACCOUNT_ID = "test_account_id"
TEST_ID_TOKEN = "test_id_token"
TEST_API_KEY = "test_api_key"
TEST_LEASE_ID = "test_lease_id"
TEST_CREDENTIAL_ID = "test_credential_id"

_FULL_PROVIDER_RESPONSE = {
    "lease_id": TEST_LEASE_ID,
    "credential_id": TEST_CREDENTIAL_ID,
    "access_token": TEST_ACCESS_TOKEN,
    "refresh_token": TEST_REFRESH_TOKEN,
    "custom_fields": {
        "account_id": TEST_ACCOUNT_ID,
        "id_token": TEST_ID_TOKEN,
    },
}

_MINIMAL_PROVIDER_RESPONSE = {
    "lease_id": TEST_LEASE_ID,
    "credential_id": TEST_CREDENTIAL_ID,
    "access_token": TEST_ACCESS_TOKEN,
    "refresh_token": TEST_REFRESH_TOKEN,
    # custom_fields absent — exercises SPIKE fallback behaviour
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manager(tmp_path):
    """Return (CodexCredentialsFileManager, auth_path) using a temp directory."""
    auth_path = tmp_path / "auth.json"
    mgr = CodexCredentialsFileManager(auth_json_path=auth_path)
    return mgr, auth_path


@pytest.fixture()
def written_manager(manager):
    """Return (manager, auth_path) after writing valid credentials."""
    mgr, auth_path = manager
    mgr.write_credentials(
        auth_mode="chatgpt",
        access_token=TEST_ACCESS_TOKEN,
        refresh_token=TEST_REFRESH_TOKEN,
        account_id=TEST_ACCOUNT_ID,
        id_token=TEST_ID_TOKEN,
        openai_api_key=TEST_API_KEY,
    )
    return mgr, auth_path


@pytest.fixture()
def replace_tracker(manager) -> Generator[Tuple[CodexCredentialsFileManager, list], None, None]:
    """
    Fixture that wraps os.replace with a call-tracker for atomic-write tests.

    Yields (manager, replace_calls) where replace_calls is populated after
    write_credentials() is called inside the test.
    """
    mgr, auth_path = manager
    replace_calls: List[Tuple[str, str]] = []
    real_replace = __import__("os").replace

    def tracking_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    with patch("os.replace", side_effect=tracking_replace):
        yield mgr, auth_path, replace_calls


# ---------------------------------------------------------------------------
# Default / custom path
# ---------------------------------------------------------------------------


class TestDefaultPath:
    def test_default_path_has_auth_json_name(self):
        mgr = CodexCredentialsFileManager()
        assert mgr.auth_json_path.name == "auth.json"

    def test_default_path_is_under_codex_home(self):
        mgr = CodexCredentialsFileManager()
        assert "codex-home" in str(mgr.auth_json_path)

    def test_custom_path_overrides_default(self, manager):
        mgr, auth_path = manager
        assert mgr.auth_json_path == auth_path


# ---------------------------------------------------------------------------
# write_credentials() — file creation
# ---------------------------------------------------------------------------


class TestWriteCredentialsCreation:
    def test_write_creates_file(self, written_manager):
        _, auth_path = written_manager
        assert auth_path.exists()

    def test_write_creates_parent_directory_if_missing(self, tmp_path):
        auth_path = tmp_path / "codex-home" / "auth.json"
        mgr = CodexCredentialsFileManager(auth_json_path=auth_path)
        mgr.write_credentials(
            auth_mode="chatgpt",
            access_token=TEST_ACCESS_TOKEN,
            refresh_token=TEST_REFRESH_TOKEN,
            account_id="",
            id_token="",
            openai_api_key="",
        )
        assert auth_path.exists()

    def test_write_overwrites_existing_file(self, written_manager):
        mgr, auth_path = written_manager
        mgr.write_credentials(
            auth_mode="chatgpt",
            access_token="updated_access",
            refresh_token="updated_refresh",
            account_id="",
            id_token="",
            openai_api_key="",
        )
        data = json.loads(auth_path.read_text())
        assert data["tokens"]["access_token"] == "updated_access"


# ---------------------------------------------------------------------------
# write_credentials() — top-level schema keys
# ---------------------------------------------------------------------------


class TestWriteCredentialsSchemaTopLevel:
    def test_write_stores_auth_mode(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert data["auth_mode"] == "chatgpt"

    def test_write_stores_openai_api_key(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert data["OPENAI_API_KEY"] == TEST_API_KEY

    def test_all_required_top_level_keys_present(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        for key in ("auth_mode", "tokens", "OPENAI_API_KEY", "last_refresh"):
            assert key in data, f"Missing top-level key: {key}"


# ---------------------------------------------------------------------------
# write_credentials() — tokens sub-schema
# ---------------------------------------------------------------------------


class TestWriteCredentialsSchemaTokens:
    def test_write_stores_access_token(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert data["tokens"]["access_token"] == TEST_ACCESS_TOKEN

    def test_write_stores_refresh_token(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert data["tokens"]["refresh_token"] == TEST_REFRESH_TOKEN

    def test_all_tokens_sub_keys_present(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        for sub_key in ("access_token", "refresh_token", "account_id", "id_token"):
            assert sub_key in data["tokens"], f"Missing tokens.{sub_key}"


# ---------------------------------------------------------------------------
# write_credentials() — timestamp
# ---------------------------------------------------------------------------


class TestWriteCredentialsTimestamp:
    def test_last_refresh_is_present(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert "last_refresh" in data

    def test_last_refresh_is_iso_format(self, written_manager):
        _, auth_path = written_manager
        data = json.loads(auth_path.read_text())
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data["last_refresh"])


# ---------------------------------------------------------------------------
# write_credentials() — security / atomicity
# ---------------------------------------------------------------------------


class TestWriteCredentialsSecurity:
    def test_sets_0o600_permissions(self, written_manager):
        _, auth_path = written_manager
        file_mode = auth_path.stat().st_mode & 0o777
        assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    def test_uses_atomic_os_replace(self, replace_tracker):
        mgr, auth_path, replace_calls = replace_tracker
        mgr.write_credentials(
            auth_mode="chatgpt",
            access_token=TEST_ACCESS_TOKEN,
            refresh_token=TEST_REFRESH_TOKEN,
            account_id="",
            id_token="",
            openai_api_key="",
        )
        assert len(replace_calls) == 1, "Expected exactly one os.replace call"
        _, dst = replace_calls[0]
        assert dst == str(auth_path)

    def test_atomic_temp_file_is_sibling(self, replace_tracker):
        mgr, auth_path, replace_calls = replace_tracker
        mgr.write_credentials(
            auth_mode="chatgpt",
            access_token=TEST_ACCESS_TOKEN,
            refresh_token=TEST_REFRESH_TOKEN,
            account_id="",
            id_token="",
            openai_api_key="",
        )
        src, dst = replace_calls[0]
        assert src != dst, "Temp file must differ from destination"
        assert str(auth_path.parent) in src, "Temp file must be in same dir as auth.json"


# ---------------------------------------------------------------------------
# write_credentials() — validation / ValueError
# ---------------------------------------------------------------------------


class TestWriteCredentialsValidation:
    def test_empty_auth_mode_raises_value_error(self, manager):
        mgr, _ = manager
        with pytest.raises(ValueError, match="auth_mode"):
            mgr.write_credentials(
                auth_mode="",
                access_token=TEST_ACCESS_TOKEN,
                refresh_token=TEST_REFRESH_TOKEN,
                account_id="",
                id_token="",
                openai_api_key="",
            )

    def test_empty_access_token_raises_value_error(self, manager):
        mgr, _ = manager
        with pytest.raises(ValueError, match="access_token"):
            mgr.write_credentials(
                auth_mode="chatgpt",
                access_token="",
                refresh_token=TEST_REFRESH_TOKEN,
                account_id="",
                id_token="",
                openai_api_key="",
            )

    def test_empty_refresh_token_raises_value_error(self, manager):
        mgr, _ = manager
        with pytest.raises(ValueError, match="refresh_token"):
            mgr.write_credentials(
                auth_mode="chatgpt",
                access_token=TEST_ACCESS_TOKEN,
                refresh_token="",
                account_id="",
                id_token="",
                openai_api_key="",
            )


# ---------------------------------------------------------------------------
# read_credentials() — missing / corrupt
# ---------------------------------------------------------------------------


class TestReadCredentialsMissing:
    def test_returns_none_when_file_missing(self, manager):
        mgr, _ = manager
        assert mgr.read_credentials() is None

    def test_returns_none_on_corrupt_json(self, manager):
        mgr, auth_path = manager
        auth_path.write_text("{not valid json!!!")
        assert mgr.read_credentials() is None


# ---------------------------------------------------------------------------
# read_credentials() — roundtrip
# ---------------------------------------------------------------------------


class TestReadCredentialsRoundtrip:
    def test_returns_access_token(self, written_manager):
        mgr, _ = written_manager
        result = mgr.read_credentials()
        assert result is not None
        assert result["tokens"]["access_token"] == TEST_ACCESS_TOKEN

    def test_returns_refresh_token(self, written_manager):
        mgr, _ = written_manager
        result = mgr.read_credentials()
        assert result is not None
        assert result["tokens"]["refresh_token"] == TEST_REFRESH_TOKEN

    def test_returns_auth_mode(self, written_manager):
        mgr, _ = written_manager
        result = mgr.read_credentials()
        assert result is not None
        assert result["auth_mode"] == "chatgpt"


# ---------------------------------------------------------------------------
# delete_credentials()
# ---------------------------------------------------------------------------


class TestDeleteCredentials:
    def test_delete_removes_file(self, written_manager):
        mgr, auth_path = written_manager
        mgr.delete_credentials()
        assert not auth_path.exists()

    def test_delete_is_idempotent_when_file_absent(self, manager):
        mgr, _ = manager
        mgr.delete_credentials()  # Must not raise

    def test_read_after_delete_returns_none(self, written_manager):
        mgr, _ = written_manager
        mgr.delete_credentials()
        assert mgr.read_credentials() is None


# ---------------------------------------------------------------------------
# exists()
# ---------------------------------------------------------------------------


class TestExists:
    def test_returns_false_when_file_absent(self, manager):
        mgr, _ = manager
        assert mgr.exists() is False

    def test_returns_true_after_write(self, written_manager):
        mgr, _ = written_manager
        assert mgr.exists() is True

    def test_returns_false_after_delete(self, written_manager):
        mgr, _ = written_manager
        mgr.delete_credentials()
        assert mgr.exists() is False


# ---------------------------------------------------------------------------
# _provider_response_to_auth_json() — core field mapping
# ---------------------------------------------------------------------------


class TestProviderResponseCoreMapping:
    def test_sets_auth_mode_to_chatgpt(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert result["auth_mode"] == "chatgpt"

    def test_maps_access_token(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert result["tokens"]["access_token"] == TEST_ACCESS_TOKEN

    def test_maps_refresh_token(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert result["tokens"]["refresh_token"] == TEST_REFRESH_TOKEN


# ---------------------------------------------------------------------------
# _provider_response_to_auth_json() — custom_fields present
# ---------------------------------------------------------------------------


class TestProviderResponseCustomFieldsPresent:
    def test_maps_account_id_from_custom_fields(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert result["tokens"]["account_id"] == TEST_ACCOUNT_ID

    def test_maps_id_token_from_custom_fields(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert result["tokens"]["id_token"] == TEST_ID_TOKEN

    def test_defaults_account_id_when_key_absent_inside_custom_fields(self):
        """SPIKE fallback: custom_fields present but account_id key missing."""
        response = {**_FULL_PROVIDER_RESPONSE, "custom_fields": {"id_token": TEST_ID_TOKEN}}
        result = _provider_response_to_auth_json(response)
        assert result["tokens"]["account_id"] == ""


# ---------------------------------------------------------------------------
# _provider_response_to_auth_json() — custom_fields fallback (SPIKE)
# ---------------------------------------------------------------------------


class TestProviderResponseCustomFieldsFallback:
    def test_defaults_account_id_to_empty_when_custom_fields_absent(self):
        """SPIKE fallback: entire custom_fields key missing."""
        result = _provider_response_to_auth_json(_MINIMAL_PROVIDER_RESPONSE)
        assert result["tokens"]["account_id"] == ""

    def test_defaults_id_token_to_empty_when_custom_fields_absent(self):
        """SPIKE fallback: entire custom_fields key missing."""
        result = _provider_response_to_auth_json(_MINIMAL_PROVIDER_RESPONSE)
        assert result["tokens"]["id_token"] == ""


# ---------------------------------------------------------------------------
# _provider_response_to_auth_json() — output structure
# ---------------------------------------------------------------------------


class TestProviderResponseStructure:
    def test_result_has_all_required_top_level_keys(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        for key in ("auth_mode", "tokens", "OPENAI_API_KEY", "last_refresh"):
            assert key in result, f"Missing top-level key: {key}"

    def test_result_tokens_has_all_sub_keys(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        for sub_key in ("access_token", "refresh_token", "account_id", "id_token"):
            assert sub_key in result["tokens"], f"Missing tokens.{sub_key}"

    def test_last_refresh_is_iso_timestamp(self):
        result = _provider_response_to_auth_json(_FULL_PROVIDER_RESPONSE)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["last_refresh"])


# ---------------------------------------------------------------------------
# _provider_response_to_auth_json() — validation / ValueError
# ---------------------------------------------------------------------------


class TestProviderResponseValidation:
    def test_raises_value_error_when_access_token_missing(self):
        bad = {
            "lease_id": TEST_LEASE_ID,
            "credential_id": TEST_CREDENTIAL_ID,
            "refresh_token": TEST_REFRESH_TOKEN,
        }
        with pytest.raises(ValueError, match="access_token"):
            _provider_response_to_auth_json(bad)

    def test_raises_value_error_when_refresh_token_missing(self):
        bad = {
            "lease_id": TEST_LEASE_ID,
            "credential_id": TEST_CREDENTIAL_ID,
            "access_token": TEST_ACCESS_TOKEN,
        }
        with pytest.raises(ValueError, match="refresh_token"):
            _provider_response_to_auth_json(bad)
