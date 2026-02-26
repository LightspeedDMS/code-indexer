"""
Tests for group-based access control in MCP handlers (Story #300).

Verifies that the group-based access control system (GroupAccessManager +
AccessFilteringService) is enforced in MCP protocol handlers, not just
REST API and Wiki routes.

AC1: search_code MCP handler enforces group-based filtering
AC2: list_repositories MCP handler enforces group-based filtering
AC3: discover_repositories MCP handler enforces group-based filtering
AC4: Repository activation checks group membership
AC5: _omni_search_code handler enforces group-based filtering
AC6: Admin group bypass works correctly (no filtering for admins)
AC7: cidx-meta always accessible to any authenticated user
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from code_indexer.server.mcp.handlers import (
    search_code,
    list_repositories,
    discover_repositories,
    activate_repository,
    _omni_search_code,
)
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_regular_user():
    """Create a mock regular user for testing."""
    user = Mock(spec=User)
    user.username = "regularuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user for testing."""
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_access_filtering_service():
    """Create a mock AccessFilteringService."""
    service = Mock()
    # Default: filter_query_results returns only allowed results
    service.filter_query_results = Mock(side_effect=lambda results, user_id: results)
    # Default: filter_repo_listing returns only allowed repos
    service.filter_repo_listing = Mock(side_effect=lambda repos, user_id: repos)
    # Default: is_admin_user returns False
    service.is_admin_user = Mock(return_value=False)
    # Default: get_accessible_repos returns limited set
    service.get_accessible_repos = Mock(return_value={"allowed-repo-global", "cidx-meta"})
    # Default: calculate_over_fetch_limit doubles the limit
    service.calculate_over_fetch_limit = Mock(side_effect=lambda limit: limit * 2)
    return service


def _make_search_result(repo_alias: str, file_path: str = "src/foo.py") -> dict:
    """Build a minimal search result dict."""
    return {
        "repository_alias": repo_alias,
        "file_path": file_path,
        "code_snippet": "some code",
        "similarity_score": 0.9,
    }


# ---------------------------------------------------------------------------
# AC1: search_code enforces group-based filtering (global repo path)
# ---------------------------------------------------------------------------


class TestSearchCodeFiltersResultsByGroup:
    """AC1: search_code MCP handler enforces group-based filtering."""

    def test_search_code_filters_results_by_group(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC1: After results obtained from a global repo, apply
        access_filtering_service.filter_query_results(results, user.username).

        Regular user searching a global repo should have results filtered.
        The filter_query_results method MUST be called with the results and
        the user's username.
        """
        allowed_result = _make_search_result("allowed-repo-global")

        # Mock the service to return the allowed result
        mock_access_filtering_service.filter_query_results = Mock(
            return_value=[allowed_result]
        )

        mock_query_result = Mock()
        mock_query_result.to_dict = Mock(return_value=allowed_result)

        # Patch app.state to inject mock access_filtering_service
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service
        mock_app_state.payload_cache = None

        mock_repo_entry = {
            "alias_name": "allowed-repo-global",
            "repo_name": "allowed-repo",
        }

        mock_alias_manager = Mock()
        mock_alias_manager.read_alias = Mock(return_value="/mock/path/allowed-repo")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
            ) as mock_alias_cls,
            patch("pathlib.Path.exists", return_value=True),
        ):
            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=[mock_repo_entry])
            mock_registry_factory.return_value = mock_registry

            mock_alias_cls.return_value = mock_alias_manager

            # Set up app_module with state
            mock_app_module.app.state = mock_app_state
            mock_app_module.semantic_query_manager._perform_search = Mock(
                return_value=[mock_query_result]
            )
            mock_app_module.golden_repo_manager = None

            params = {
                "repository_alias": "allowed-repo-global",
                "query_text": "some query",
            }
            result = search_code(params, mock_regular_user)

        # Verify filter_query_results was called with username
        mock_access_filtering_service.filter_query_results.assert_called_once()
        call_args = mock_access_filtering_service.filter_query_results.call_args
        assert call_args[0][1] == "regularuser"  # user.username passed


# ---------------------------------------------------------------------------
# AC6: Admin sees all (no filtering for admin users in search_code)
# ---------------------------------------------------------------------------


class TestSearchCodeAdminSeesAll:
    """AC6: Admin group bypass works correctly - no filtering for admins."""

    def test_search_code_admin_no_filtering_applied(
        self, mock_admin_user, mock_access_filtering_service
    ):
        """
        AC6: When admin user calls search_code on a global repo, the
        access_filtering_service.filter_query_results is called with admin
        username (the service internally handles the bypass).
        """
        result_1 = _make_search_result("repo-a-global")

        # For admin: filter returns ALL results (no filtering)
        mock_access_filtering_service.filter_query_results = Mock(
            return_value=[result_1]
        )
        mock_access_filtering_service.is_admin_user = Mock(return_value=True)

        mock_query_result = Mock()
        mock_query_result.to_dict = Mock(return_value=result_1)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service
        mock_app_state.payload_cache = None

        mock_repo_entry = {
            "alias_name": "repo-a-global",
            "repo_name": "repo-a",
        }

        mock_alias_manager = Mock()
        mock_alias_manager.read_alias = Mock(return_value="/mock/path/repo-a")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
            ) as mock_alias_cls,
            patch("pathlib.Path.exists", return_value=True),
        ):
            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=[mock_repo_entry])
            mock_registry_factory.return_value = mock_registry
            mock_alias_cls.return_value = mock_alias_manager
            mock_app_module.app.state = mock_app_state
            mock_app_module.semantic_query_manager._perform_search = Mock(
                return_value=[mock_query_result]
            )
            mock_app_module.golden_repo_manager = None

            params = {
                "repository_alias": "repo-a-global",
                "query_text": "some query",
            }
            result = search_code(params, mock_admin_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        # Admin user: filter_query_results called with admin username
        mock_access_filtering_service.filter_query_results.assert_called_once()
        call_args = mock_access_filtering_service.filter_query_results.call_args
        assert call_args[0][1] == "admin"


# ---------------------------------------------------------------------------
# AC2: list_repositories enforces group-based filtering
# ---------------------------------------------------------------------------


class TestListRepositoriesFiltersByGroup:
    """AC2: list_repositories MCP handler enforces group-based filtering."""

    def test_list_repositories_filters_by_group(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC2: After merging activated + global repos, apply
        filter_repo_listing(repo_aliases, user.username).

        Regular user should only see repos their group has access to.
        filter_repo_listing MUST be called with the list of aliases and username.
        """
        activated_repos = [
            {
                "user_alias": "allowed-repo",
                "golden_repo_alias": "allowed-repo",
                "is_global": False,
            },
            {
                "user_alias": "blocked-repo",
                "golden_repo_alias": "blocked-repo",
                "is_global": False,
            },
        ]
        global_repos_data = []

        # Mock the filter to only allow "allowed-repo"
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["allowed-repo"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.activated_repo_manager.list_activated_repositories = Mock(
                return_value=activated_repos
            )
            mock_app_module.golden_repo_manager = None

            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=global_repos_data)
            mock_registry_factory.return_value = mock_registry

            result = list_repositories({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing MUST have been called
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "regularuser"  # username passed

        # Only allowed repo should be present
        aliases = [r["user_alias"] for r in data["repositories"]]
        assert "allowed-repo" in aliases
        assert "blocked-repo" not in aliases


# ---------------------------------------------------------------------------
# AC3: discover_repositories enforces group-based filtering
# ---------------------------------------------------------------------------


class TestDiscoverRepositoriesFiltersByGroup:
    """AC3: discover_repositories MCP handler enforces group-based filtering."""

    def test_discover_repositories_filters_by_group(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC3: Golden repos list should be filtered through filter_repo_listing()
        before returning to the user.

        Regular user should only see golden repos their group has access to.
        """
        all_golden_repos = [
            {"alias": "allowed-golden-repo", "url": "https://example.com/allowed"},
            {"alias": "blocked-golden-repo", "url": "https://example.com/blocked"},
        ]

        # Mock filter to only allow "allowed-golden-repo"
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["allowed-golden-repo"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.golden_repo_manager.list_golden_repos = Mock(
                return_value=all_golden_repos
            )

            result = discover_repositories({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing MUST have been called
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "regularuser"  # username passed

        # Only allowed repo should be visible
        aliases = [r["alias"] for r in data["repositories"]]
        assert "allowed-golden-repo" in aliases
        assert "blocked-golden-repo" not in aliases


# ---------------------------------------------------------------------------
# AC5: _omni_search_code enforces group-based filtering
# ---------------------------------------------------------------------------


class TestOmniSearchFiltersResultsByGroup:
    """AC5: _omni_search_code handler enforces group-based filtering."""

    def test_omni_search_filters_by_group(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC5: Combined multi-repo results from _omni_search_code should be
        filtered with filter_query_results() before returning.

        The filter_query_results method MUST be called with combined results
        and user.username.
        """
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        allowed_result = {
            "source_repo": "allowed-repo-global",
            "repository_alias": "allowed-repo-global",
            "file_path": "src/a.py",
            "similarity_score": 0.9,
        }
        blocked_result = {
            "source_repo": "blocked-repo-global",
            "repository_alias": "blocked-repo-global",
            "file_path": "src/b.py",
            "similarity_score": 0.8,
        }

        # Filter returns only allowed result
        mock_access_filtering_service.filter_query_results = Mock(
            return_value=[allowed_result]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service
        mock_app_state.payload_cache = None

        # Build a proper MultiSearchResponse
        mock_response = MultiSearchResponse(
            results={
                "allowed-repo-global": [dict(allowed_result)],
                "blocked-repo-global": [dict(blocked_result)],
            },
            metadata=MultiSearchMetadata(
                total_results=2,
                total_repos_searched=2,
                execution_time_ms=50,
            ),
            errors=None,
        )

        mock_multi_search_service = Mock()
        mock_multi_search_service.search = Mock(return_value=mock_response)

        # Config service must return proper integer values for MultiSearchConfig
        mock_config_service = Mock()
        mock_server_config = Mock()
        mock_limits = Mock()
        mock_limits.multi_search_max_workers = 2
        mock_limits.multi_search_timeout_seconds = 30
        mock_server_config.multi_search_limits_config = mock_limits
        mock_config_service.get_config.return_value = mock_server_config

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=mock_config_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns",
                side_effect=lambda patterns: patterns,
            ),
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_mss_cls,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.golden_repo_manager = None
            mock_mss_cls.return_value = mock_multi_search_service

            params = {
                "repository_alias": ["allowed-repo-global", "blocked-repo-global"],
                "query_text": "some query",
                "limit": 10,
            }
            result = _omni_search_code(params, mock_regular_user)

        # filter_query_results MUST have been called
        mock_access_filtering_service.filter_query_results.assert_called_once()
        call_args = mock_access_filtering_service.filter_query_results.call_args
        assert call_args[0][1] == "regularuser"


# ---------------------------------------------------------------------------
# AC4: activate_repository checks group membership
# ---------------------------------------------------------------------------


class TestActivateRepositoryGroupCheck:
    """AC4: Repository activation checks group membership."""

    def test_activate_repository_rejects_unauthorized_user(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC4: activate_repository should reject activation when the user's group
        does NOT include the requested golden_repo_alias in accessible repos.

        The response should indicate the repo is not accessible and the user
        should contact their administrator.
        """
        # User cannot access "restricted-repo" - only cidx-meta accessible
        mock_access_filtering_service.get_accessible_repos = Mock(
            return_value={"cidx-meta"}
        )
        mock_access_filtering_service.is_admin_user = Mock(return_value=False)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
        ):
            mock_app_module.app.state = mock_app_state

            params = {
                "golden_repo_alias": "restricted-repo",
                "user_alias": "my-restricted-repo",
            }
            result = activate_repository(params, mock_regular_user)

        data = json.loads(result["content"][0]["text"])

        # Should fail with an access message referencing administrator
        assert data["success"] is False
        error_msg = data.get("error", "").lower()
        assert (
            "accessible" in error_msg
            or "access" in error_msg
            or "administrator" in error_msg
        )

    def test_activate_repository_allows_authorized_user(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC4: activate_repository should allow activation when the user's group
        DOES include the requested golden_repo_alias in accessible repos.
        """
        # User can access "allowed-repo"
        mock_access_filtering_service.get_accessible_repos = Mock(
            return_value={"allowed-repo", "cidx-meta"}
        )
        mock_access_filtering_service.is_admin_user = Mock(return_value=False)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
        ):
            mock_app_module.app.state = mock_app_state
            # Mock the actual activation to succeed
            mock_app_module.activated_repo_manager.activate_repository = Mock(
                return_value="job-123"
            )

            params = {
                "golden_repo_alias": "allowed-repo",
                "user_alias": "my-allowed-repo",
            }
            result = activate_repository(params, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

    def test_activate_repository_admin_bypasses_check(
        self, mock_admin_user, mock_access_filtering_service
    ):
        """
        AC4/AC6: Admin users bypass the group membership check and can activate
        any repository regardless of group assignments.
        """
        mock_access_filtering_service.is_admin_user = Mock(return_value=True)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.activated_repo_manager.activate_repository = Mock(
                return_value="job-456"
            )

            params = {
                "golden_repo_alias": "any-repo",
                "user_alias": "my-any-repo",
            }
            result = activate_repository(params, mock_admin_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["job_id"] == "job-456"


# ---------------------------------------------------------------------------
# Integration tests: AccessFilteringService suffix handling (Story #300)
# ---------------------------------------------------------------------------


class TestFilterQueryResultsSuffixHandling:
    """Integration test: verify -global suffix is correctly stripped during filtering (Story #300)."""

    def _make_service(self, accessible_repo_names):
        """Build a real AccessFilteringService with mocked GroupAccessManager."""
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )
        from code_indexer.server.services.group_access_manager import Group
        from datetime import datetime

        mock_gam = MagicMock()
        # Return a regular (non-admin) group
        mock_group = Group(
            id=1,
            name="users",
            description="regular users",
            is_default=True,
            created_at=datetime(2024, 1, 1),
        )
        mock_gam.get_user_group.return_value = mock_group
        mock_gam.get_group_repos.return_value = list(accessible_repo_names)
        return AccessFilteringService(mock_gam)

    def test_filter_query_results_strips_global_suffix(self):
        """Results with -global suffix repository_alias are correctly matched to accessible repos."""
        service = self._make_service(["my-repo", "cidx-meta"])

        # Results have -global suffix (as MCP handlers produce)
        results = [
            {"file_path": "file1.py", "score": 0.9, "repository_alias": "my-repo-global"},
            {"file_path": "file2.py", "score": 0.8, "repository_alias": "restricted-repo-global"},
            {"file_path": "file3.py", "score": 0.7, "repository_alias": "cidx-meta-global"},
        ]

        filtered = service.filter_query_results(results, "test_user")

        # my-repo and cidx-meta should pass; restricted-repo should be filtered
        assert len(filtered) == 2
        aliases = [r["repository_alias"] for r in filtered]
        assert "my-repo-global" in aliases
        assert "cidx-meta-global" in aliases
        assert "restricted-repo-global" not in aliases

    def test_filter_query_results_handles_source_repo_field(self):
        """Results with source_repo field (omni-search) are correctly filtered."""
        service = self._make_service(["my-repo", "cidx-meta"])

        results = [
            {"file_path": "file1.py", "score": 0.9, "source_repo": "my-repo-global"},
            {"file_path": "file2.py", "score": 0.8, "source_repo": "restricted-global"},
        ]

        filtered = service.filter_query_results(results, "test_user")
        assert len(filtered) == 1
        assert filtered[0]["source_repo"] == "my-repo-global"


# ---------------------------------------------------------------------------
# Integration test: composite activation also checks group access (Story #300)
# ---------------------------------------------------------------------------


class TestActivateCompositeRepoGroupCheck:
    """Test that composite activation (plural aliases) also checks group access (Story #300)."""

    def test_activate_composite_rejects_unauthorized_alias(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """Composite activation with unauthorized alias in golden_repo_aliases is rejected."""
        # User cannot access "restricted-repo"
        mock_access_filtering_service.get_accessible_repos = Mock(
            return_value={"cidx-meta"}
        )
        mock_access_filtering_service.is_admin_user = Mock(return_value=False)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
        ):
            mock_app_module.app.state = mock_app_state

            params = {
                "golden_repo_aliases": ["restricted-repo"],
                "user_alias": "my-composite-repo",
            }
            result = activate_repository(params, mock_regular_user)

        data = json.loads(result["content"][0]["text"])

        # Should fail with an access message
        assert data["success"] is False
        error_msg = data.get("error", "").lower()
        assert (
            "accessible" in error_msg
            or "access" in error_msg
            or "administrator" in error_msg
        )


# ---------------------------------------------------------------------------
# AC7: cidx-meta always accessible
# ---------------------------------------------------------------------------


class TestCidxMetaAlwaysAccessible:
    """AC7: cidx-meta always accessible to any authenticated user."""

    def test_cidx_meta_always_accessible_in_search(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC7: When searching cidx-meta (a global repo), even a regular user
        with no group assignment should be able to access it.

        The access_filtering_service.get_accessible_repos() always includes
        'cidx-meta' in its return set, so results from cidx-meta should pass
        through filter_query_results unchanged.
        """
        cidx_meta_result = _make_search_result("cidx-meta-global")

        # Service returns cidx-meta result (AC7 enforced by service internals)
        mock_access_filtering_service.filter_query_results = Mock(
            return_value=[cidx_meta_result]
        )
        mock_access_filtering_service.is_admin_user = Mock(return_value=False)
        mock_access_filtering_service.get_accessible_repos = Mock(
            return_value={"cidx-meta"}  # Only cidx-meta accessible
        )

        mock_query_result = Mock()
        mock_query_result.to_dict = Mock(return_value=cidx_meta_result)

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service
        mock_app_state.payload_cache = None

        mock_repo_entry = {
            "alias_name": "cidx-meta-global",
            "repo_name": "cidx-meta",
        }
        mock_alias_manager = Mock()
        mock_alias_manager.read_alias = Mock(return_value="/mock/path/cidx-meta")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
            ) as mock_alias_cls,
            patch("pathlib.Path.exists", return_value=True),
        ):
            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=[mock_repo_entry])
            mock_registry_factory.return_value = mock_registry
            mock_alias_cls.return_value = mock_alias_manager
            mock_app_module.app.state = mock_app_state
            mock_app_module.semantic_query_manager._perform_search = Mock(
                return_value=[mock_query_result]
            )
            mock_app_module.golden_repo_manager = None

            params = {
                "repository_alias": "cidx-meta-global",
                "query_text": "some query",
            }
            result = search_code(params, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # Filtering was applied (the service handles cidx-meta pass-through)
        mock_access_filtering_service.filter_query_results.assert_called_once()

        # Result should include cidx-meta result (service passes it through)
        results = data.get("results", {}).get("results", [])
        assert len(results) >= 1
