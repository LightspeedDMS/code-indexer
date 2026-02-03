"""
Unit tests for SCIPQueryService.

Story #38: Create SCIPQueryService with Unified SCIP File Discovery

Tests the SCIPQueryService class which provides unified SCIP file discovery
logic that can be shared between MCP handlers and REST routes.

Following TDD methodology - these tests are written FIRST before implementation.
"""

from pathlib import Path
from unittest.mock import MagicMock


class TestSCIPQueryServiceInitialization:
    """Tests for SCIPQueryService initialization (AC4: Service initialization with configuration)."""

    def test_initialization_with_golden_repos_dir_and_access_filtering(self):
        """AC4: SCIPQueryService initializes with golden_repos_dir and AccessFilteringService."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        # Create mock AccessFilteringService
        mock_access_filtering_service = MagicMock(spec=AccessFilteringService)

        # Create service
        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=mock_access_filtering_service,
        )

        # Verify service is properly configured
        assert service.get_golden_repos_dir() == Path("/data/golden-repos")
        assert service.access_filtering_service is mock_access_filtering_service

    def test_initialization_without_access_filtering(self):
        """Service can be initialized without access filtering (backward compatibility)."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        # Create service without access filtering
        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=None,
        )

        assert service.get_golden_repos_dir() == Path("/data/golden-repos")
        assert service.access_filtering_service is None

    def test_get_golden_repos_dir_returns_path(self):
        """Service returns golden repos directory as Path object."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        service = SCIPQueryService(
            golden_repos_dir="/custom/data/golden-repos",
            access_filtering_service=None,
        )

        # The service should expose the golden repos directory as Path
        assert service.get_golden_repos_dir() == Path("/custom/data/golden-repos")
        assert isinstance(service.get_golden_repos_dir(), Path)


class TestFindScipFilesWithoutAccessControl:
    """Tests for find_scip_files() without access control (backward compatibility)."""

    def test_find_all_scip_files_when_no_username_provided(self):
        """Find SCIP files for all repositories when username is None."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup: Create directory structure with SCIP files
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repo-a with SCIP index
            repo_a = golden_repos / "repo-a" / ".code-indexer" / "scip"
            repo_a.mkdir(parents=True)
            (repo_a / "index.scip.db").touch()

            # Create repo-b with SCIP index
            repo_b = golden_repos / "repo-b" / ".code-indexer" / "scip"
            repo_b.mkdir(parents=True)
            (repo_b / "index.scip.db").touch()

            # Create repo-c with SCIP index
            repo_c = golden_repos / "repo-c" / ".code-indexer" / "scip"
            repo_c.mkdir(parents=True)
            (repo_c / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find all SCIP files (no username = no access filtering)
            scip_files = service.find_scip_files(username=None)

            # Assert: All 3 SCIP files should be returned
            assert len(scip_files) == 3
            scip_file_names = {f.parent.parent.parent.name for f in scip_files}
            assert scip_file_names == {"repo-a", "repo-b", "repo-c"}

    def test_find_scip_files_returns_empty_when_no_scip_indexes_exist(self):
        """Returns empty list when no SCIP indexes exist."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repo without SCIP index
            repo_a = golden_repos / "repo-a"
            repo_a.mkdir()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            scip_files = service.find_scip_files()

            assert scip_files == []

    def test_find_scip_files_returns_empty_when_golden_repos_dir_not_exists(self):
        """Returns empty list when golden repos directory doesn't exist."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        service = SCIPQueryService(
            golden_repos_dir="/nonexistent/path/golden-repos",
            access_filtering_service=None,
        )

        scip_files = service.find_scip_files()

        assert scip_files == []


class TestFindScipFilesWithAccessControl:
    """Tests for find_scip_files() with access control (AC1, AC3)."""

    def test_find_scip_files_filters_by_user_accessible_repos(self):
        """AC1: Find SCIP files only for accessible repositories."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create 3 repos with SCIP indexes
            for repo_name in ["repo-a", "repo-b", "repo-c"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            # Setup mock access filtering service
            mock_access_filtering_service = MagicMock(spec=AccessFilteringService)
            # Developer has access to repo-a and repo-b only
            mock_access_filtering_service.get_accessible_repos.return_value = {
                "repo-a",
                "repo-b",
            }

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=mock_access_filtering_service,
            )

            # Act: Find SCIP files for developer
            scip_files = service.find_scip_files(username="developer")

            # Assert: Only repo-a and repo-b SCIP files should be returned
            assert len(scip_files) == 2
            scip_file_repos = {f.parent.parent.parent.name for f in scip_files}
            assert scip_file_repos == {"repo-a", "repo-b"}
            assert "repo-c" not in scip_file_repos

            # Verify access filtering was called with correct username
            mock_access_filtering_service.get_accessible_repos.assert_called_once_with(
                "developer"
            )

    def test_find_scip_files_returns_empty_for_user_with_no_access(self):
        """AC3: Returns empty list for user with no access to any repository."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repo with SCIP index
            repo_scip = golden_repos / "private-repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            # Setup mock access filtering service
            mock_access_filtering_service = MagicMock(spec=AccessFilteringService)
            # Guest has no access to any repos
            mock_access_filtering_service.get_accessible_repos.return_value = set()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=mock_access_filtering_service,
            )

            # Act: Find SCIP files for guest user
            scip_files = service.find_scip_files(username="guest")

            # Assert: No SCIP files should be returned (no error raised)
            assert scip_files == []
            mock_access_filtering_service.get_accessible_repos.assert_called_once_with(
                "guest"
            )

    def test_find_scip_files_without_username_when_access_service_exists(self):
        """When username is None but access service exists, return all SCIP files."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repo with SCIP index
            repo_scip = golden_repos / "repo-a" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            mock_access_filtering_service = MagicMock(spec=AccessFilteringService)

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=mock_access_filtering_service,
            )

            # Act: Find SCIP files without username (backward compatibility)
            scip_files = service.find_scip_files(username=None)

            # Assert: All SCIP files returned, access filtering NOT called
            assert len(scip_files) == 1
            mock_access_filtering_service.get_accessible_repos.assert_not_called()


class TestFindScipFilesWithRepositoryAlias:
    """Tests for find_scip_files() with repository_alias filter (AC2)."""

    def test_find_scip_files_for_specific_repository(self):
        """AC2: Find SCIP files only for specific repository."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create 3 repos with SCIP indexes
            for repo_name in ["my-repo", "other-repo", "third-repo"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find SCIP files for specific repository
            scip_files = service.find_scip_files(repository_alias="my-repo")

            # Assert: Only my-repo SCIP file should be returned
            assert len(scip_files) == 1
            assert scip_files[0].parent.parent.parent.name == "my-repo"

    def test_find_scip_files_for_nonexistent_repository(self):
        """Returns empty list when repository_alias doesn't exist."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repo with SCIP index
            repo_scip = golden_repos / "existing-repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find SCIP files for nonexistent repository
            scip_files = service.find_scip_files(repository_alias="nonexistent-repo")

            # Assert: Empty list returned
            assert scip_files == []

    def test_find_scip_files_with_repository_alias_and_access_control(self):
        """Combines repository_alias filter with access control."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repos with SCIP indexes
            for repo_name in ["allowed-repo", "forbidden-repo"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            mock_access_filtering_service = MagicMock(spec=AccessFilteringService)
            mock_access_filtering_service.get_accessible_repos.return_value = {
                "allowed-repo"
            }

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=mock_access_filtering_service,
            )

            # Act: Try to access forbidden repo with repository_alias
            scip_files = service.find_scip_files(
                repository_alias="forbidden-repo", username="user"
            )

            # Assert: Empty list because user doesn't have access
            assert scip_files == []


class TestFindScipFilesDirectoryFiltering:
    """Tests for find_scip_files() directory filtering behavior."""

    def test_find_scip_files_skips_non_directories(self):
        """Verify files in golden repos root are skipped during iteration."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create a regular file in the golden repos root (should be skipped)
            (golden_repos / "some_file.txt").touch()
            (golden_repos / "README.md").touch()

            # Create a valid repo with SCIP index
            repo_scip = golden_repos / "valid-repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find SCIP files
            scip_files = service.find_scip_files()

            # Assert: Only valid-repo SCIP file found, files are skipped
            assert len(scip_files) == 1
            assert scip_files[0].parent.parent.parent.name == "valid-repo"

    def test_find_scip_files_skips_hidden_directories(self):
        """Verify hidden directories (e.g., .git) are skipped."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create hidden directories with SCIP files (should be skipped)
            for hidden_name in [".git", ".hidden", ".cache"]:
                hidden_scip = golden_repos / hidden_name / ".code-indexer" / "scip"
                hidden_scip.mkdir(parents=True)
                (hidden_scip / "index.scip.db").touch()

            # Create a valid repo with SCIP index
            repo_scip = golden_repos / "visible-repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find SCIP files
            scip_files = service.find_scip_files()

            # Assert: Only visible-repo SCIP file found, hidden dirs are skipped
            assert len(scip_files) == 1
            assert scip_files[0].parent.parent.parent.name == "visible-repo"

    def test_find_scip_files_includes_versioned_directory(self):
        """Verify .versioned is NOT skipped (special case for versioned repos)."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create .versioned directory with SCIP index (should NOT be skipped)
            versioned_scip = golden_repos / ".versioned" / ".code-indexer" / "scip"
            versioned_scip.mkdir(parents=True)
            (versioned_scip / "index.scip.db").touch()

            # Create a regular repo with SCIP index
            repo_scip = golden_repos / "regular-repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            # Create a hidden directory (should be skipped)
            hidden_scip = golden_repos / ".hidden" / ".code-indexer" / "scip"
            hidden_scip.mkdir(parents=True)
            (hidden_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            # Act: Find SCIP files
            scip_files = service.find_scip_files()

            # Assert: Both .versioned and regular-repo found, .hidden skipped
            assert len(scip_files) == 2
            scip_file_repos = {f.parent.parent.parent.name for f in scip_files}
            assert ".versioned" in scip_file_repos
            assert "regular-repo" in scip_file_repos
            assert ".hidden" not in scip_file_repos


class TestGetAccessibleReposMethod:
    """Tests for the get_accessible_repos() public method."""

    def test_get_accessible_repos_returns_none_when_no_service(self):
        """When no AccessFilteringService, returns None."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=None,
        )

        # Act: Call get_accessible_repos
        result = service.get_accessible_repos("any_user")

        # Assert: Returns None (no access service configured)
        assert result is None

    def test_get_accessible_repos_delegates_to_access_service(self):
        """When AccessFilteringService exists, delegates to it."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        mock_access_service = MagicMock(spec=AccessFilteringService)
        mock_access_service.get_accessible_repos.return_value = {"repo-a", "repo-b"}

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=mock_access_service,
        )

        # Act: Call get_accessible_repos
        result = service.get_accessible_repos("developer")

        # Assert: Delegates to access service and returns result
        assert result == {"repo-a", "repo-b"}
        mock_access_service.get_accessible_repos.assert_called_once_with("developer")


class TestUserWithNoGroupMemberships:
    """Tests for edge case: user with no group memberships."""

    def test_find_scip_files_for_user_with_no_group_memberships(self):
        """
        Edge case: User exists but has no group memberships.

        This is different from an empty access set - the user is authenticated
        but not assigned to any groups, which may result in different behavior
        depending on access control policy.
        """
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            # Create repos with SCIP indexes
            for repo_name in ["repo-a", "repo-b"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            # Setup mock access filtering service
            # User with no group memberships gets empty set (no access)
            mock_access_service = MagicMock(spec=AccessFilteringService)
            mock_access_service.get_accessible_repos.return_value = set()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=mock_access_service,
            )

            # Act: Find SCIP files for user with no group memberships
            scip_files = service.find_scip_files(username="user_no_groups")

            # Assert: Empty list (user has no access through any groups)
            assert scip_files == []
            mock_access_service.get_accessible_repos.assert_called_once_with(
                "user_no_groups"
            )

    def test_get_accessible_repos_for_user_with_no_group_memberships(self):
        """
        Edge case: get_accessible_repos for user with no group memberships.

        The access service returns an empty set for users without group
        memberships, indicating they have no repository access.
        """
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        mock_access_service = MagicMock(spec=AccessFilteringService)
        # User with no group memberships returns empty set
        mock_access_service.get_accessible_repos.return_value = set()

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=mock_access_service,
        )

        # Act: Get accessible repos for user with no groups
        result = service.get_accessible_repos("orphan_user")

        # Assert: Returns empty set (not None - user exists but has no access)
        assert result == set()
        assert result is not None  # Explicitly verify it's empty set, not None
        mock_access_service.get_accessible_repos.assert_called_once_with("orphan_user")


# ============================================================================
# Story #39: Query Execution Method Tests
# ============================================================================


class TestQueryResultToDictHelper:
    """Tests for _query_result_to_dict() helper method."""

    def test_converts_query_result_to_dict(self):
        """Verify QueryResult is converted to a dictionary with all fields."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=None,
        )

        query_result = QueryResult(
            symbol="UserService",
            project="my-project",
            file_path="src/services/user.py",
            line=45,
            column=6,
            kind="definition",
            relationship="defines",
            context="class UserService:",
        )

        # Act: Convert QueryResult to dict
        result_dict = service._query_result_to_dict(query_result)

        # Assert: All fields are present and correct
        assert result_dict["symbol"] == "UserService"
        assert result_dict["project"] == "my-project"
        assert result_dict["file_path"] == "src/services/user.py"
        assert result_dict["line"] == 45
        assert result_dict["column"] == 6
        assert result_dict["kind"] == "definition"
        assert result_dict["relationship"] == "defines"
        assert result_dict["context"] == "class UserService:"

    def test_converts_path_object_to_string(self):
        """Verify Path objects in file_path are converted to strings."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=None,
        )

        query_result = QueryResult(
            symbol="handler",
            project="api",
            file_path=Path("/project/src/handlers.py"),
            line=10,
            column=0,
            kind="reference",
            relationship="calls",
            context="handler()",
        )

        result_dict = service._query_result_to_dict(query_result)

        # Assert: file_path is a string, not Path
        assert isinstance(result_dict["file_path"], str)
        assert result_dict["file_path"] == "/project/src/handlers.py"

    def test_handles_none_optional_fields(self):
        """Verify None values for optional fields are handled correctly."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=None,
        )

        query_result = QueryResult(
            symbol="func",
            project="proj",
            file_path="src/main.py",
            line=1,
            column=0,
            kind="definition",
            relationship=None,
            context=None,
        )

        result_dict = service._query_result_to_dict(query_result)

        # Assert: None values are preserved
        assert result_dict["relationship"] is None
        assert result_dict["context"] is None


class TestFindDefinitionMethod:
    """Tests for SCIPQueryService.find_definition() method."""

    def test_find_definition_returns_results_from_single_scip_file(self):
        """AC: Find symbol definition across repositories."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            repo_scip = golden_repos / "repo-a" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            mock_engine = MagicMock()
            mock_engine.find_definition.return_value = [
                QueryResult(
                    symbol="UserService",
                    project="repo-a",
                    file_path="src/services.py",
                    line=45,
                    column=6,
                    kind="definition",
                    relationship=None,
                    context="class UserService:",
                )
            ]

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                results = service.find_definition("UserService")

            assert len(results) == 1
            assert results[0]["symbol"] == "UserService"
            assert results[0]["file_path"] == "src/services.py"
            assert results[0]["line"] == 45
            mock_engine.find_definition.assert_called_once_with(
                "UserService", exact=False
            )

    def test_find_definition_aggregates_results_from_multiple_scip_files(self):
        """Aggregates results from multiple SCIP files across repositories."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            for repo_name in ["repo-a", "repo-b"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            def create_mock_engine(scip_file):
                mock = MagicMock()
                if "repo-a" in str(scip_file):
                    mock.find_definition.return_value = [
                        QueryResult(
                            symbol="Config",
                            project="repo-a",
                            file_path="repo-a/config.py",
                            line=10,
                            column=0,
                            kind="definition",
                            relationship=None,
                            context="class Config:",
                        )
                    ]
                else:
                    mock.find_definition.return_value = [
                        QueryResult(
                            symbol="Config",
                            project="repo-b",
                            file_path="repo-b/settings.py",
                            line=20,
                            column=0,
                            kind="definition",
                            relationship=None,
                            context="Config = Settings()",
                        )
                    ]
                return mock

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                side_effect=create_mock_engine,
            ):
                results = service.find_definition("Config")

            assert len(results) == 2
            file_paths = {r["file_path"] for r in results}
            assert "repo-a/config.py" in file_paths
            assert "repo-b/settings.py" in file_paths

    def test_find_definition_with_exact_match(self):
        """Passes exact parameter to SCIPQueryEngine."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            repo_scip = golden_repos / "repo" / ".code-indexer" / "scip"
            repo_scip.mkdir(parents=True)
            (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            mock_engine = MagicMock()
            mock_engine.find_definition.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.find_definition("ExactSymbol", exact=True)

            mock_engine.find_definition.assert_called_once_with(
                "ExactSymbol", exact=True
            )

    def test_find_definition_with_repository_alias_filter(self):
        """Filters SCIP files by repository_alias."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            for repo_name in ["target-repo", "other-repo"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            mock_engine = MagicMock()
            mock_engine.find_definition.return_value = [
                QueryResult(
                    symbol="Symbol",
                    project="target-repo",
                    file_path="src/file.py",
                    line=1,
                    column=0,
                    kind="definition",
                    relationship=None,
                    context="Symbol",
                )
            ]

            engine_calls = []

            def track_engine(scip_file):
                engine_calls.append(scip_file)
                return mock_engine

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                side_effect=track_engine,
            ):
                results = service.find_definition(
                    "Symbol", repository_alias="target-repo"
                )

            assert len(engine_calls) == 1
            assert "target-repo" in str(engine_calls[0])
            assert len(results) == 1

    def test_find_definition_returns_empty_list_when_no_scip_files(self):
        """Returns empty list when no SCIP files exist."""
        import tempfile
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            results = service.find_definition("AnySymbol")

            assert results == []

    def test_find_definition_handles_engine_exception_gracefully(self):
        """Continues processing other SCIP files when one fails."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            for repo_name in ["good-repo", "bad-repo"]:
                repo_scip = golden_repos / repo_name / ".code-indexer" / "scip"
                repo_scip.mkdir(parents=True)
                (repo_scip / "index.scip.db").touch()

            service = SCIPQueryService(
                golden_repos_dir=str(golden_repos),
                access_filtering_service=None,
            )

            def create_mock_engine(scip_file):
                if "bad-repo" in str(scip_file):
                    raise Exception("Corrupted SCIP file")
                mock = MagicMock()
                mock.find_definition.return_value = [
                    QueryResult(
                        symbol="Good",
                        project="good-repo",
                        file_path="src/good.py",
                        line=1,
                        column=0,
                        kind="definition",
                        relationship=None,
                        context="Good",
                    )
                ]
                return mock

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                side_effect=create_mock_engine,
            ):
                results = service.find_definition("Good")

            assert len(results) == 1
            assert results[0]["project"] == "good-repo"


class TestFindReferencesMethod:
    """Tests for SCIPQueryService.find_references() method."""

    def test_find_references_returns_all_references(self):
        """AC: Find all references to a symbol across repositories."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            for name in ["repo-a", "repo-b"]:
                scip = golden_repos / name / ".code-indexer" / "scip"
                scip.mkdir(parents=True)
                (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            # Return 2 results per engine call (simulating multi-repo)
            mock_engine.find_references.return_value = [
                QueryResult("auth", "repo", "auth.py", 10, 4, "ref", "call", "ctx")
            ]

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                results = service.find_references("authenticate")

            # 2 repos queried, 1 result each = 2 total
            assert len(results) == 2
            assert all(k in results[0] for k in ["file_path", "line", "column"])

    def test_find_references_with_limit(self):
        """Passes limit parameter to SCIPQueryEngine."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.find_references.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.find_references("symbol", limit=50)

            mock_engine.find_references.assert_called_once_with(
                "symbol", limit=50, exact=False
            )

    def test_find_references_default_limit_is_100(self):
        """Default limit is 100 when not specified."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.find_references.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.find_references("symbol")

            mock_engine.find_references.assert_called_once_with(
                "symbol", limit=100, exact=False
            )


class TestGetDependenciesMethod:
    """Tests for SCIPQueryService.get_dependencies() method."""

    def test_get_dependencies_returns_all_dependencies(self):
        """AC: Get symbol dependencies."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.get_dependencies.return_value = [
                QueryResult("Database", "repo", "db.py", 10, 0, "dep", "import", "ctx"),
                QueryResult("Logger", "repo", "log.py", 5, 0, "dep", "import", "ctx"),
            ]

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                results = service.get_dependencies("PaymentProcessor")

            assert len(results) == 2
            symbols = {r["symbol"] for r in results}
            assert symbols == {"Database", "Logger"}

    def test_get_dependencies_with_depth(self):
        """Passes depth parameter to SCIPQueryEngine."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.get_dependencies.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.get_dependencies("Symbol", depth=3)

            mock_engine.get_dependencies.assert_called_once_with(
                "Symbol", depth=3, exact=False
            )


class TestGetDependentsMethod:
    """Tests for SCIPQueryService.get_dependents() method."""

    def test_get_dependents_returns_all_dependents(self):
        """Returns symbols that depend on the target symbol."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.get_dependents.return_value = [
                QueryResult(
                    "OrderSvc", "repo", "orders.py", 100, 8, "dep", "call", "c"
                ),
                QueryResult(
                    "Checkout", "repo", "checkout.py", 50, 4, "dep", "call", "c"
                ),
            ]

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                results = service.get_dependents("PaymentProcessor")

            assert len(results) == 2
            symbols = {r["symbol"] for r in results}
            assert symbols == {"OrderSvc", "Checkout"}

    def test_get_dependents_with_depth(self):
        """Passes depth parameter to SCIPQueryEngine."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.get_dependents.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.get_dependents("Symbol", depth=2)

            mock_engine.get_dependents.assert_called_once_with(
                "Symbol", depth=2, exact=False
            )


class TestAnalyzeImpactMethod:
    """Tests for SCIPQueryService.analyze_impact() method."""

    def test_analyze_impact_returns_impact_analysis(self):
        """Returns impact analysis result."""
        import tempfile
        from unittest.mock import patch
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.composites import (
            ImpactAnalysisResult,
            AffectedSymbol,
            AffectedFile,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_result = ImpactAnalysisResult(
                target_symbol="DbConn",
                target_location=None,
                depth_analyzed=3,
                affected_symbols=[
                    AffectedSymbol("UserRepo", Path("user.py"), 25, 4, 1, "call", []),
                ],
                affected_files=[
                    AffectedFile(Path("user.py"), "repo", 1, 1, 1),
                ],
                truncated=False,
                total_affected=1,
            )

            with patch(
                "code_indexer.scip.query.composites.analyze_impact",
                return_value=mock_result,
            ) as mock_analyze:
                result = service.analyze_impact("DbConn")

            assert result["target_symbol"] == "DbConn"
            assert result["depth_analyzed"] == 3
            assert result["total_affected"] == 1
            mock_analyze.assert_called_once()

    def test_analyze_impact_with_depth(self):
        """Passes depth parameter to analyze_impact function."""
        import tempfile
        from unittest.mock import patch
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.composites import ImpactAnalysisResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_result = ImpactAnalysisResult("Sym", None, 5, [], [], False, 0)

            with patch(
                "code_indexer.scip.query.composites.analyze_impact",
                return_value=mock_result,
            ) as mock_analyze:
                service.analyze_impact("Symbol", depth=5)

            call_kwargs = mock_analyze.call_args[1]
            assert call_kwargs.get("depth") == 5


class TestTraceCallchainMethod:
    """Tests for SCIPQueryService.trace_callchain() method."""

    def test_trace_callchain_returns_call_chains(self):
        """AC: Trace call chain between symbols."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.backends import CallChain

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.trace_call_chain.return_value = [
                CallChain(
                    path=["handleReq", "validate", "sanitize"],
                    length=3,
                    has_cycle=False,
                )
            ]

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                results = service.trace_callchain("handleReq", "sanitize")

            assert len(results) == 1
            assert results[0]["path"] == ["handleReq", "validate", "sanitize"]
            assert results[0]["length"] == 3

    def test_trace_callchain_with_max_depth(self):
        """Passes max_depth parameter to engine."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_engine = MagicMock()
            mock_engine.trace_call_chain.return_value = []

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                service.trace_callchain("from", "to", max_depth=5)

            mock_engine.trace_call_chain.assert_called_once_with(
                "from", "to", max_depth=5, limit=100
            )


class TestGetContextMethod:
    """Tests for SCIPQueryService.get_context() method."""

    def test_get_context_returns_smart_context(self):
        """Returns smart context for a symbol."""
        import tempfile
        from unittest.mock import patch
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.composites import (
            SmartContextResult,
            ContextFile,
            ContextSymbol,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_result = SmartContextResult(
                target_symbol="UserService",
                summary="Read these 1 file(s)",
                files=[
                    ContextFile(
                        path=Path("user.py"),
                        project="repo",
                        relevance_score=1.0,
                        symbols=[
                            ContextSymbol("UserService", "def", "def", 10, 0, 1.0)
                        ],
                        read_priority=1,
                    )
                ],
                total_files=1,
                total_symbols=1,
                avg_relevance=1.0,
            )

            with patch(
                "code_indexer.scip.query.composites.get_smart_context",
                return_value=mock_result,
            ):
                result = service.get_context("UserService")

            assert result["target_symbol"] == "UserService"
            assert result["total_files"] == 1
            assert len(result["files"]) == 1

    def test_get_context_with_limit_and_min_score(self):
        """Passes limit and min_score parameters."""
        import tempfile
        from unittest.mock import patch
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.composites import SmartContextResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()
            scip = golden_repos / "repo" / ".code-indexer" / "scip"
            scip.mkdir(parents=True)
            (scip / "index.scip.db").touch()

            service = SCIPQueryService(str(golden_repos), None)
            mock_result = SmartContextResult("Sym", "", [], 0, 0, 0.0)

            with patch(
                "code_indexer.scip.query.composites.get_smart_context",
                return_value=mock_result,
            ) as mock_ctx:
                service.get_context("Symbol", limit=10, min_score=0.5)

            call_kwargs = mock_ctx.call_args[1]
            assert call_kwargs.get("limit") == 10
            assert call_kwargs.get("min_score") == 0.5


class TestAccessControlForQueryMethods:
    """Tests for access control integration in query methods."""

    def test_find_definition_respects_access_control(self):
        """Query methods only search accessible repositories."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            for repo_name in ["accessible-repo", "forbidden-repo"]:
                scip = golden_repos / repo_name / ".code-indexer" / "scip"
                scip.mkdir(parents=True)
                (scip / "index.scip.db").touch()

            mock_access = MagicMock(spec=AccessFilteringService)
            mock_access.get_accessible_repos.return_value = {"accessible-repo"}

            service = SCIPQueryService(str(golden_repos), mock_access)
            engine_calls = []

            def track_engine(scip_file):
                engine_calls.append(str(scip_file))
                mock = MagicMock()
                mock.find_definition.return_value = [
                    QueryResult(
                        "Sym", "accessible-repo", "f.py", 1, 0, "def", None, "c"
                    )
                ]
                return mock

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                side_effect=track_engine,
            ):
                service.find_definition("Symbol", username="user")

            assert len(engine_calls) == 1
            assert "accessible-repo" in engine_calls[0]
            assert "forbidden-repo" not in engine_calls[0]

    def test_query_without_access_service_returns_all_repos(self):
        """When no access service, all repositories are queried."""
        import tempfile
        from unittest.mock import patch, MagicMock
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.primitives import QueryResult

        with tempfile.TemporaryDirectory() as tmpdir:
            golden_repos = Path(tmpdir) / "golden-repos"
            golden_repos.mkdir()

            for repo_name in ["repo-a", "repo-b"]:
                scip = golden_repos / repo_name / ".code-indexer" / "scip"
                scip.mkdir(parents=True)
                (scip / "index.scip.db").touch()

            # No access filtering service
            service = SCIPQueryService(str(golden_repos), None)
            engine_calls = []

            def track_engine(scip_file):
                engine_calls.append(str(scip_file))
                mock = MagicMock()
                mock.find_definition.return_value = [
                    QueryResult("Sym", "repo", "f.py", 1, 0, "def", None, "c")
                ]
                return mock

            with patch(
                "code_indexer.scip.query.primitives.SCIPQueryEngine",
                side_effect=track_engine,
            ):
                service.find_definition("Symbol", username="user")

            # Both repos should be queried
            assert len(engine_calls) == 2
