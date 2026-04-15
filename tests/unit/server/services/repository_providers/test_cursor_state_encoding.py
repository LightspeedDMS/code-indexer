"""
Tests for Bug #685: Cursor _encode_cursor / _decode_cursor behavior.

Covers only cursor serialization/deserialization:
- Round-trip encode/decode for GitHub and GitLab providers
- Silent restart on malformed, wrong-version, wrong-platform, missing-key cursors
"""

import base64
import json
import pytest
from unittest.mock import MagicMock


def _make_provider(platform):
    """Create a minimally configured provider; token field uses a dummy value."""
    from code_indexer.server.services.ci_token_manager import TokenData

    token_data = TokenData(platform=platform, token="dummy", base_url=None)
    token_manager = MagicMock()
    token_manager.get_token.return_value = token_data
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = []

    if platform == "github":
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        return GitHubProvider(
            token_manager=token_manager, golden_repo_manager=golden_repo_manager
        )

    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )

    return GitLabProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _encode_raw(payload_dict):
    """Encode a dict as base64-JSON to simulate arbitrary cursor payloads."""
    return base64.b64encode(json.dumps(payload_dict).encode()).decode()


def _encode_raw_bytes(payload_bytes):
    """Encode raw bytes as base64 to produce invalid JSON cursors."""
    return base64.b64encode(payload_bytes).decode()


@pytest.mark.parametrize("platform", ["github", "gitlab"])
class TestCursorRoundTrip:
    """Cursor encode/decode round-trip for both platforms."""

    def test_encode_produces_valid_base64_json_with_version(self, platform):
        provider = _make_provider(platform)
        token = provider._encode_cursor(source="any_source", skip=0)
        decoded = json.loads(base64.b64decode(token))
        assert decoded["v"] == 1
        assert decoded["platform"] == platform
        assert decoded["source"] == "any_source"
        assert decoded["skip"] == 0

    def test_string_source_round_trips(self, platform):
        provider = _make_provider(platform)
        token = provider._encode_cursor(source="Y3Vyc29yOnYxOk==", skip=5)
        state = provider._decode_cursor(token)
        assert state is not None
        assert state.source == "Y3Vyc29yOnYxOk=="
        assert state.skip == 5

    def test_integer_source_round_trips(self, platform):
        provider = _make_provider(platform)
        token = provider._encode_cursor(source=7, skip=12)
        state = provider._decode_cursor(token)
        assert state is not None
        assert state.source == 7
        assert state.skip == 12

    def test_none_cursor_returns_none(self, platform):
        provider = _make_provider(platform)
        assert provider._decode_cursor(None) is None


@pytest.mark.parametrize("platform", ["github", "gitlab"])
class TestCursorDecodeErrorHandling:
    """Invalid cursors must return None silently, never raise."""

    def test_malformed_base64_returns_none(self, platform):
        provider = _make_provider(platform)
        assert provider._decode_cursor("!!!not_valid_base64!!!") is None

    def test_invalid_json_returns_none(self, platform):
        provider = _make_provider(platform)
        bad = _encode_raw_bytes(b"not json at all")
        assert provider._decode_cursor(bad) is None

    def test_truncated_json_returns_none(self, platform):
        provider = _make_provider(platform)
        bad = _encode_raw_bytes(b'{"v": 1, "platform":')
        assert provider._decode_cursor(bad) is None

    def test_wrong_version_returns_none(self, platform):
        provider = _make_provider(platform)
        token = _encode_raw({"v": 99, "platform": platform, "source": "abc", "skip": 0})
        assert provider._decode_cursor(token) is None

    def test_wrong_platform_returns_none(self, platform):
        provider = _make_provider(platform)
        other = "gitlab" if platform == "github" else "github"
        token = _encode_raw({"v": 1, "platform": other, "source": "abc", "skip": 0})
        assert provider._decode_cursor(token) is None

    def test_missing_source_key_returns_none(self, platform):
        provider = _make_provider(platform)
        token = _encode_raw({"v": 1, "platform": platform, "skip": 0})
        assert provider._decode_cursor(token) is None

    def test_missing_skip_key_returns_none(self, platform):
        provider = _make_provider(platform)
        token = _encode_raw({"v": 1, "platform": platform, "source": "abc"})
        assert provider._decode_cursor(token) is None
