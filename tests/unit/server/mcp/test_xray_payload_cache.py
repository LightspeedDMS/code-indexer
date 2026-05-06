"""Unit tests for _truncate_xray_result helper in xray MCP handler.

Tests that the helper correctly applies PayloadCache truncation to large
matches[] / evaluation_errors[] payloads in X-Ray job results.

Mocking strategy:
- PayloadCache: real dataclass-style fake (not a Mock) to exercise the
  actual truncation path without needing a live SQLite database.
- _utils.app_module.app.state: patched via unittest.mock.patch to inject
  the fake cache or None.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal fake PayloadCache — avoids SQLite, exercises real truncation logic
# ---------------------------------------------------------------------------


class _FakePayloadCacheConfig:
    preview_size_chars: int = 200


class _FakePayloadCache:
    """Minimal fake PayloadCache with the same truncate_result contract."""

    def __init__(self, preview_size_chars: int = 200) -> None:
        self.config = _FakePayloadCacheConfig()
        self.config.preview_size_chars = preview_size_chars
        self._stored: Dict[str, str] = {}
        self._counter = 0

    def store(self, content: str) -> str:
        self._counter += 1
        handle = f"fake-handle-{self._counter}"
        self._stored[handle] = content
        return handle

    def truncate_result(self, content: str) -> dict:
        """Mirror of PayloadCache.truncate_result() logic."""
        preview_size = self.config.preview_size_chars
        if len(content) > preview_size:
            cache_handle = self.store(content)
            return {
                "preview": content[:preview_size],
                "cache_handle": cache_handle,
                "has_more": True,
                "total_size": len(content),
            }
        else:
            return {
                "content": content,
                "cache_handle": None,
                "has_more": False,
            }


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_match(file_path: str, snippet: str = "x" * 50) -> Dict[str, Any]:
    return {
        "file_path": file_path,
        "line_number": 1,
        "code_snippet": snippet,
        "language": "python",
        "evaluator_decision": True,
    }


def _make_error(file_path: str) -> Dict[str, Any]:
    return {
        "file_path": file_path,
        "line_number": None,
        "error_type": "AttributeError",
        "error_message": "node has no attribute 'x'",
    }


def _make_small_result() -> Dict[str, Any]:
    """Result whose matches+errors JSON is under 200 chars."""
    return {
        "matches": [_make_match("a.py", "x")],
        "evaluation_errors": [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": 0.1,
    }


def _make_large_result(n_matches: int = 20) -> Dict[str, Any]:
    """Result whose matches+errors JSON exceeds 200 chars."""
    return {
        "matches": [_make_match(f"file_{i}.py", "x" * 80) for i in range(n_matches)],
        "evaluation_errors": [_make_error(f"err_{i}.py") for i in range(5)],
        "files_processed": n_matches,
        "files_total": n_matches,
        "elapsed_seconds": 1.5,
    }


def _import_helper():
    from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

    return _truncate_xray_result


# ---------------------------------------------------------------------------
# Tests: small result — inline, no cache
# ---------------------------------------------------------------------------


class TestTruncateXrayResultSmall:
    """Small results are returned inline without caching."""

    def test_small_result_returns_inline_no_cache_handle(self):
        """When combined payload is small, result is returned inline."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_small_result())

        assert result.get("cache_handle") is None
        assert result.get("has_more") is False
        assert result.get("truncated") is False
        assert isinstance(result.get("matches"), list)
        assert isinstance(result.get("evaluation_errors"), list)

    def test_small_result_preserves_all_matches(self):
        """Small result keeps all matches inline — no truncation to first 3."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        result_in = _make_small_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(result_in)

        assert result["matches"] == result_in["matches"]
        assert result["evaluation_errors"] == result_in["evaluation_errors"]

    def test_small_result_preserves_top_level_metadata(self):
        """Small result keeps non-match top-level fields intact."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        result_in = _make_small_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(result_in)

        assert result["files_processed"] == result_in["files_processed"]
        assert result["files_total"] == result_in["files_total"]
        assert result["elapsed_seconds"] == result_in["elapsed_seconds"]

    def test_small_result_does_not_store_in_cache(self):
        """Small results must not create a cache entry."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            helper(_make_small_result())

        assert len(fake_cache._stored) == 0


# ---------------------------------------------------------------------------
# Tests: large result — preview + cache_handle
# ---------------------------------------------------------------------------


class TestTruncateXrayResultLarge:
    """Large results return preview + cache_handle with first 3 matches inline."""

    def test_large_result_returns_cache_handle(self):
        """Large combined payload produces a non-None cache_handle."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result.get("cache_handle") is not None
        assert result["cache_handle"].startswith("fake-handle-")

    def test_large_result_has_more_true(self):
        """Large result must have has_more=True."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result["has_more"] is True

    def test_large_result_truncated_true(self):
        """Large result must have truncated=True."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result["truncated"] is True

    def test_large_result_includes_total_size(self):
        """Large result includes total_size of the full combined payload."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        expected_payload = json.dumps(
            {
                "matches": large["matches"],
                "evaluation_errors": large["evaluation_errors"],
            }
        )
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result["total_size"] == len(expected_payload)

    def test_large_result_stores_full_payload_in_cache(self):
        """The stored cache entry contains the full matches+errors JSON."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        expected_payload = json.dumps(
            {
                "matches": large["matches"],
                "evaluation_errors": large["evaluation_errors"],
            }
        )
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        handle = result["cache_handle"]
        assert fake_cache._stored[handle] == expected_payload

    def test_large_result_first_3_matches_inline(self):
        """Large result includes only the first 3 matches inline."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result(n_matches=20)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert len(result["matches"]) == 3
        assert result["matches"] == large["matches"][:3]

    def test_large_result_first_3_errors_inline(self):
        """Large result includes only the first 3 evaluation_errors inline."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert len(result["evaluation_errors"]) == 3
        assert result["evaluation_errors"] == large["evaluation_errors"][:3]

    def test_large_result_includes_preview_string(self):
        """Large result includes matches_and_errors_preview field."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert "matches_and_errors_preview" in result
        assert len(result["matches_and_errors_preview"]) <= 200

    def test_large_result_preserves_metadata_fields(self):
        """Large result preserves files_processed, files_total, elapsed_seconds."""
        fake_cache = _FakePayloadCache(preview_size_chars=200)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result["files_processed"] == large["files_processed"]
        assert result["files_total"] == large["files_total"]
        assert result["elapsed_seconds"] == large["elapsed_seconds"]


# ---------------------------------------------------------------------------
# Tests: cache unavailable — return full result unchanged
# ---------------------------------------------------------------------------


class TestTruncateXrayResultCacheUnavailable:
    """When payload_cache is absent from app.state, full result is returned."""

    def test_cache_unavailable_returns_full_result(self):
        """No payload_cache on app.state: full result dict returned unchanged."""
        mock_state = MagicMock(spec=[])  # no payload_cache attribute
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result == large

    def test_cache_none_returns_full_result(self):
        """payload_cache=None on app.state: full result dict returned unchanged."""
        mock_state = MagicMock()
        mock_state.payload_cache = None
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result == large

    def test_cache_unavailable_does_not_mutate_input(self):
        """When cache unavailable, the returned dict is the same object (no copy)."""
        mock_state = MagicMock(spec=[])
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        large_id = id(large)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert id(result) == large_id
