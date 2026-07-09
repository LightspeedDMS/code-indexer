"""
Unit tests for unified repository_status MCP handler (Story #990).

Covers:
- HANDLER_REGISTRY contains 'repository_status' (AC1)
- Tool doc loaded with correct schema (AC3)
- Auto-detection: alias ending in '-global' -> kind='global' (AC5)
- Auto-detection: alias without '-global' -> kind='activated' (AC5)
- detail='basic' returns status only, no statistics field (AC4)
- detail='stats' returns status + statistics (AC4)
- Pinned envelope shape for all 4 combinations (AC4)
- kind discriminator correctness (AC4)
- Missing alias returns error (AC6)
- Invalid detail value returns error (AC6)
- Alias not found returns error envelope (AC6)
- Retired tools NOT in registry: get_repository_status, get_repository_statistics,
  global_repo_status (AC2)

TDD: Written BEFORE implementation — all tests FAIL until
  handle_repository_status() is added to repos.py and registered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOLDEN_REPOS_DIR_PATH = "code_indexer.server.mcp.handlers._utils._get_golden_repos_dir"
# GlobalRepoOperations is imported lazily inside _handle_repository_status_global();
# patch must target the source module, not the importer.
_GLOBAL_OPS_PATH = "code_indexer.global_repos.shared_operations.GlobalRepoOperations"
_REPO_LISTING_MGR_PATH = (
    "code_indexer.server.mcp.handlers._utils.app_module.repository_listing_manager"
)


def _get_tool_docs():
    """Return all tool docs dict (name -> ToolDoc) using the real loader singleton."""
    from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

    return _get_tool_doc_loader().get_all_docs()


def _parse(result) -> dict:
    """Parse the MCP response envelope to a plain dict.

    _mcp_response returns {"content": [{"type": "text", "text": "<JSON>"}]}.
    """
    import json as _json

    text = result["content"][0]["text"]
    return _json.loads(text)  # type: ignore[no-any-return]


@pytest.fixture
def admin_user():
    return User(
        username="admin_test",
        password_hash="$2b$12$hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def normal_user():
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


def _make_activated_status():
    """Sample activated repo status dict (like repository_listing_manager returns)."""
    return {
        "user_alias": "my-repo",
        "golden_repo_alias": "backend",
        "repo_url": "https://github.com/example/backend.git",
        "activation_status": "activated",
        "file_count": 42,
        "index_size": 1024,
        "last_updated": "2024-01-01T00:00:00Z",
        "enable_temporal": True,
    }


def _make_global_status():
    """Sample GlobalRepoOperations.get_status() return dict."""
    return {
        "alias": "backend-global",
        "repo_name": "backend",
        "url": "https://github.com/example/backend.git",
        "last_refresh": "2024-01-01T12:00:00Z",
        "enable_temporal": True,
    }


def _make_statistics():
    """Sample stats service response."""

    class FakeStats:
        def model_dump(self, mode="json"):
            return {
                "repository_id": "repo-123",
                "files": {"total": 100, "indexed": 95},
                "storage": {"repository_size_bytes": 2048},
                "health": {"score": 0.95, "issues": []},
            }

    return FakeStats()


# ---------------------------------------------------------------------------
# AC1 / AC2: Registry presence and retirement
# ---------------------------------------------------------------------------


class TestRegistryPresence:
    """HANDLER_REGISTRY must contain repository_status; old tools must be gone."""

    def test_repository_status_in_registry(self):
        """repository_status must be registered after Story #990 (AC1)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "repository_status" in HANDLER_REGISTRY, (
            "repository_status must be registered in HANDLER_REGISTRY"
        )

    def test_get_repository_status_removed_from_registry(self):
        """get_repository_status must be REMOVED from registry (AC2 hard-cut)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "get_repository_status" not in HANDLER_REGISTRY, (
            "get_repository_status must be removed — use repository_status instead"
        )

    def test_get_repository_statistics_removed_from_registry(self):
        """get_repository_statistics must be REMOVED from registry (AC2 hard-cut)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "get_repository_statistics" not in HANDLER_REGISTRY, (
            "get_repository_statistics must be removed — use repository_status instead"
        )

    def test_global_repo_status_removed_from_registry(self):
        """global_repo_status must be REMOVED from registry (AC2 hard-cut)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "global_repo_status" not in HANDLER_REGISTRY, (
            "global_repo_status must be removed — use repository_status instead"
        )

    def test_get_all_repositories_status_still_registered(self):
        """get_all_repositories_status must be UNTOUCHED (Story #990 constraint)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "get_all_repositories_status" in HANDLER_REGISTRY, (
            "get_all_repositories_status must remain registered — it is NOT retired"
        )


# ---------------------------------------------------------------------------
# AC3: Tool doc / schema validation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def all_tool_docs():
    """Loaded tool docs dict (name -> ToolDoc) using the real loader singleton."""
    return _get_tool_docs()


@pytest.fixture(scope="module")
def repo_status_tool_doc(all_tool_docs):
    """ToolDoc for 'repository_status', or None if not yet registered."""
    return all_tool_docs.get("repository_status")


class TestToolDoc:
    """repository_status tool doc must be loadable with correct schema."""

    def test_tool_doc_loaded(self, all_tool_docs):
        """repository_status must appear in the MCP tool doc loader."""
        assert "repository_status" in all_tool_docs, (
            "repository_status tool doc must be present"
        )

    def test_tool_doc_has_alias_param(self, repo_status_tool_doc):
        """Tool schema must have required 'alias' parameter."""
        assert repo_status_tool_doc is not None
        schema = repo_status_tool_doc.inputSchema or {}
        props = schema.get("properties", {})
        assert "alias" in props, "Tool schema must have 'alias' property"
        required = schema.get("required", [])
        assert "alias" in required, "'alias' must be required in tool schema"

    def test_tool_doc_has_detail_param(self, repo_status_tool_doc):
        """Tool schema must have optional 'detail' parameter."""
        assert repo_status_tool_doc is not None
        schema = repo_status_tool_doc.inputSchema or {}
        props = schema.get("properties", {})
        assert "detail" in props, "Tool schema must have 'detail' property"

    def test_old_tool_docs_removed(self, all_tool_docs):
        """Retired tools must NOT appear in the MCP tool doc loader."""
        for retired in (
            "get_repository_status",
            "get_repository_statistics",
            "global_repo_status",
        ):
            assert retired not in all_tool_docs, (
                f"{retired} tool doc must be removed (Story #990 hard-cut)"
            )


# ---------------------------------------------------------------------------
# AC5: Auto-detection routing
# ---------------------------------------------------------------------------


class TestAutoDetectionRouting:
    """Alias suffix determines kind: -global -> 'global', else -> 'activated'."""

    def test_alias_without_global_suffix_routes_to_activated(self, normal_user):
        """Alias 'my-repo' routes to kind='activated' path."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status(
                {"alias": "my-repo", "detail": "basic"}, normal_user
            )

        data = _parse(result)
        assert data.get("kind") == "activated"

    def test_alias_with_global_suffix_routes_to_global(self, normal_user):
        """Alias 'backend-global' routes to kind='global' path."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        global_status = _make_global_status()
        mock_ops = MagicMock()
        mock_ops.get_status.return_value = global_status

        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
        ):
            result = handle_repository_status(
                {"alias": "backend-global", "detail": "basic"}, normal_user
            )

        data = _parse(result)
        assert data.get("kind") == "global"

    def test_alias_global_suffix_case_sensitive(self, normal_user):
        """'-Global' (uppercase G) does NOT trigger global routing."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status(
                {"alias": "my-repo-Global", "detail": "basic"}, normal_user
            )

        data = _parse(result)
        # -Global (capital G) is not the suffix, goes to activated
        assert data.get("kind") == "activated"


# ---------------------------------------------------------------------------
# AC4: Pinned envelope shape
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    """Verify the exact envelope returned by all 4 combinations."""

    def test_activated_basic_envelope(self, normal_user):
        """kind=activated, detail=basic: success, kind, detail, status — no statistics."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status({"alias": "my-repo"}, normal_user)

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "activated"
        assert data["detail"] == "basic"
        assert "status" in data
        assert "statistics" not in data, "detail=basic must NOT include statistics"
        # Status fields must come from listing manager
        assert data["status"]["user_alias"] == "my-repo"

    def test_activated_stats_envelope(self, normal_user):
        """kind=activated, detail=stats: success, kind, detail, status, statistics."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        fake_stats = _make_statistics()

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
            patch(
                "code_indexer.server.services.stats_service.stats_service"
            ) as mock_svc,
        ):
            mock_app.repository_listing_manager = mock_listing_mgr
            mock_svc.get_repository_stats.return_value = fake_stats
            result = handle_repository_status(
                {"alias": "my-repo", "detail": "stats"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "activated"
        assert data["detail"] == "stats"
        assert "status" in data
        assert "statistics" in data
        assert data["statistics"]["repository_id"] == "repo-123"

    def test_global_basic_envelope(self, normal_user):
        """kind=global, detail=basic: success, kind, detail, status — no statistics."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        global_status = _make_global_status()
        mock_ops = MagicMock()
        mock_ops.get_status.return_value = global_status

        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
        ):
            result = handle_repository_status(
                {"alias": "backend-global", "detail": "basic"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "global"
        assert data["detail"] == "basic"
        assert "status" in data
        assert "statistics" not in data
        # Fields nested under status (not at top level)
        assert data["status"]["alias"] == "backend-global"
        assert data["status"]["enable_temporal"] is True
        # Verify NOT flat top-level (old global_repo_status behaviour)
        assert "alias" not in data or data.get("alias") is None or "status" in data

    def test_global_stats_envelope(self, normal_user):
        """kind=global, detail=stats: success, kind, detail, status, statistics."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        global_status = _make_global_status()
        mock_ops = MagicMock()
        mock_ops.get_status.return_value = global_status

        global_stats_dict = {
            "repository_alias": "backend-global",
            "is_global": True,
        }
        # _build_global_repo_statistics returns a plain dict (no MCP wrapping).
        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.repos._build_global_repo_statistics",
                return_value=global_stats_dict,
            ),
        ):
            result = handle_repository_status(
                {"alias": "backend-global", "detail": "stats"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "global"
        assert data["detail"] == "stats"
        assert "status" in data
        assert "statistics" in data
        assert data["statistics"]["repository_alias"] == "backend-global"

    def test_detail_defaults_to_basic(self, normal_user):
        """Missing 'detail' param defaults to 'basic'."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status({"alias": "my-repo"}, normal_user)

        data = _parse(result)

        assert data["detail"] == "basic"
        assert "statistics" not in data


# ---------------------------------------------------------------------------
# AC6: Parameter validation and error handling
# ---------------------------------------------------------------------------


class TestParameterValidation:
    """Missing or invalid parameters must return error envelopes."""

    def test_missing_alias_returns_error(self, normal_user):
        """Missing alias returns success=False with error message."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        result = handle_repository_status({}, normal_user)

        data = _parse(result)

        assert data["success"] is False
        assert "error" in data
        assert "alias" in data["error"].lower() or "missing" in data["error"].lower()

    def test_empty_alias_returns_error(self, normal_user):
        """Empty alias returns success=False with error message."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        result = handle_repository_status({"alias": ""}, normal_user)

        data = _parse(result)

        assert data["success"] is False
        assert "error" in data

    def test_invalid_detail_value_returns_error(self, normal_user):
        """detail='invalid' returns success=False with error message."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        result = handle_repository_status(
            {"alias": "my-repo", "detail": "invalid"}, normal_user
        )

        data = _parse(result)

        assert data["success"] is False
        assert "error" in data
        assert "detail" in data["error"].lower() or "invalid" in data["error"].lower()

    def test_activated_alias_not_found_returns_error(self, normal_user):
        """Activated alias that raises exception returns success=False."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.side_effect = ValueError(
            "Repository 'nonexistent' not found"
        )

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status(
                {"alias": "nonexistent", "detail": "basic"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is False
        assert "error" in data

    def test_global_alias_not_found_returns_error(self, normal_user):
        """Global alias that raises ValueError returns success=False."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        mock_ops = MagicMock()
        mock_ops.get_status.side_effect = ValueError(
            "Global repo 'nonexistent-global' not found"
        )

        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
        ):
            result = handle_repository_status(
                {"alias": "nonexistent-global", "detail": "basic"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is False
        assert "error" in data


# ---------------------------------------------------------------------------
# Field preservation: global status fields are nested under 'status'
# ---------------------------------------------------------------------------


class TestFieldPreservation:
    """Global status fields must be nested under 'status', not flat at top level."""

    def test_global_status_fields_nested_under_status_key(self, normal_user):
        """Fields from GlobalRepoOperations.get_status() appear under 'status' key."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        global_status = _make_global_status()
        mock_ops = MagicMock()
        mock_ops.get_status.return_value = global_status

        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
        ):
            result = handle_repository_status({"alias": "backend-global"}, normal_user)

        data = _parse(result)

        # All global_status keys must appear under status, not at top level
        for field in ("alias", "repo_name", "url", "last_refresh", "enable_temporal"):
            assert field in data["status"], (
                f"Field '{field}' must be nested under 'status' key"
            )

    def test_activated_status_fields_preserved(self, normal_user):
        """All fields from listing manager are preserved in status."""
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status({"alias": "my-repo"}, normal_user)

        data = _parse(result)

        for field in (
            "user_alias",
            "golden_repo_alias",
            "repo_url",
            "activation_status",
        ):
            assert field in data["status"], (
                f"Field '{field}' must be preserved in 'status'"
            )


# ---------------------------------------------------------------------------
# Bug #1204: repository_status omits next_refresh + enable_scip for global repos
# ---------------------------------------------------------------------------


def _make_global_status_with_all_fields():
    """Sample GlobalRepoOperations.get_status() return dict WITH the two new fields."""
    return {
        "alias": "backend-global",
        "repo_name": "backend",
        "url": "https://github.com/example/backend.git",
        "last_refresh": "2024-01-01T12:00:00Z",
        "enable_temporal": True,
        "next_refresh": "1735736400.5",
        "enable_scip": True,
    }


class TestGlobalStatusFieldsBug1204:
    """Bug #1204: next_refresh and enable_scip must appear in global repository_status.

    RED: these tests fail before the fix (fields absent from get_status() output).
    GREEN: pass after get_status() copies the two fields from the already-loaded record.
    """

    def test_get_status_returns_next_refresh(self, tmp_path):
        """GlobalRepoOperations.get_status() must include 'next_refresh' from the record."""
        from code_indexer.global_repos.shared_operations import GlobalRepoOperations

        raw_record = {
            "alias_name": "backend-global",
            "repo_name": "backend",
            "repo_url": "https://github.com/example/backend.git",
            "last_refresh": "2024-01-01T12:00:00Z",
            "enable_temporal": True,
            "next_refresh": "1735736400.5",
            "enable_scip": True,
        }
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = raw_record

        ops = GlobalRepoOperations(str(tmp_path / "golden-repos"))
        ops._registry = mock_registry  # inject directly to bypass lazy resolution

        status = ops.get_status("backend-global")

        assert "next_refresh" in status, (
            "get_status() must return 'next_refresh' — field is in the loaded record "
            "but was not being copied (Bug #1204)"
        )
        assert status["next_refresh"] == "1735736400.5"

    def test_get_status_returns_enable_scip(self, tmp_path):
        """GlobalRepoOperations.get_status() must include 'enable_scip' from the record."""
        from code_indexer.global_repos.shared_operations import GlobalRepoOperations

        raw_record = {
            "alias_name": "backend-global",
            "repo_name": "backend",
            "repo_url": "https://github.com/example/backend.git",
            "last_refresh": "2024-01-01T12:00:00Z",
            "enable_temporal": False,
            "next_refresh": None,
            "enable_scip": True,
        }
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = raw_record

        ops = GlobalRepoOperations(str(tmp_path / "golden-repos"))
        ops._registry = mock_registry

        status = ops.get_status("backend-global")

        assert "enable_scip" in status, (
            "get_status() must return 'enable_scip' — field is in the loaded record "
            "but was not being copied (Bug #1204)"
        )
        assert status["enable_scip"] is True

    def test_get_status_next_refresh_none_when_not_scheduled(self, tmp_path):
        """get_status() must return next_refresh=None when record has None."""
        from code_indexer.global_repos.shared_operations import GlobalRepoOperations

        raw_record = {
            "alias_name": "backend-global",
            "repo_name": "backend",
            "repo_url": "https://github.com/example/backend.git",
            "last_refresh": "2024-01-01T12:00:00Z",
            "enable_temporal": False,
            "next_refresh": None,
            "enable_scip": False,
        }
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = raw_record

        ops = GlobalRepoOperations(str(tmp_path / "golden-repos"))
        ops._registry = mock_registry

        status = ops.get_status("backend-global")

        assert "next_refresh" in status
        assert status["next_refresh"] is None

    def test_repository_status_handler_returns_next_refresh_and_enable_scip(
        self, normal_user
    ):
        """handler repository_status() response must include next_refresh + enable_scip
        for a global alias — sourced from the already-loaded record (no extra DB query).
        """
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        full_status = _make_global_status_with_all_fields()
        mock_ops = MagicMock()
        mock_ops.get_status.return_value = full_status

        with (
            patch(_GLOBAL_OPS_PATH, return_value=mock_ops),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
        ):
            result = handle_repository_status(
                {"alias": "backend-global", "detail": "basic"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "global"
        assert "next_refresh" in data["status"], (
            "'next_refresh' must appear in status for global repos (Bug #1204)"
        )
        assert "enable_scip" in data["status"], (
            "'enable_scip' must appear in status for global repos (Bug #1204)"
        )
        assert data["status"]["next_refresh"] == "1735736400.5"
        assert data["status"]["enable_scip"] is True

    def test_activated_repo_status_unchanged_no_new_fields(self, normal_user):
        """Activated repo status must NOT gain next_refresh or enable_scip fields
        from the global branch — regression guard for the activated path.
        """
        from code_indexer.server.mcp.handlers.repos import handle_repository_status

        activated_status = _make_activated_status()
        mock_listing_mgr = MagicMock()
        mock_listing_mgr.get_repository_details.return_value = activated_status

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.repository_listing_manager = mock_listing_mgr
            result = handle_repository_status(
                {"alias": "my-repo", "detail": "basic"}, normal_user
            )

        data = _parse(result)

        assert data["success"] is True
        assert data["kind"] == "activated"
        # The activated status dict as returned by listing manager must be passed
        # through unchanged — listing manager does not include next_refresh/enable_scip
        assert "next_refresh" not in data["status"], (
            "Activated repo status must NOT include next_refresh (global-only field)"
        )
        assert "enable_scip" not in data["status"], (
            "Activated repo status must NOT include enable_scip (global-only field)"
        )
