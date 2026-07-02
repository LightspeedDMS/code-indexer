"""
Structural tests for Phase 1 of app.py modularization (Story #409).

Verifies that Pydantic model classes extracted from app.py are importable
from the new domain submodules under code_indexer.server.models.

These tests were written FIRST (TDD red phase) and drive the extraction.
"""

import pytest


class TestAuthModelsImportable:
    """Auth-related models must be importable from models.auth submodule."""

    def test_login_request_importable(self):
        from code_indexer.server.models.auth import LoginRequest

        assert LoginRequest is not None

    def test_login_response_importable(self):
        from code_indexer.server.models.auth import LoginResponse

        assert LoginResponse is not None

    def test_refresh_token_request_importable(self):
        from code_indexer.server.models.auth import RefreshTokenRequest

        assert RefreshTokenRequest is not None

    def test_refresh_token_response_importable(self):
        from code_indexer.server.models.auth import RefreshTokenResponse

        assert RefreshTokenResponse is not None

    def test_user_info_importable(self):
        from code_indexer.server.models.auth import UserInfo

        assert UserInfo is not None

    def test_create_user_request_importable(self):
        from code_indexer.server.models.auth import CreateUserRequest

        assert CreateUserRequest is not None

    def test_update_user_request_importable(self):
        from code_indexer.server.models.auth import UpdateUserRequest

        assert UpdateUserRequest is not None

    def test_change_password_request_importable(self):
        from code_indexer.server.models.auth import ChangePasswordRequest

        assert ChangePasswordRequest is not None

    def test_user_response_importable(self):
        from code_indexer.server.models.auth import UserResponse

        assert UserResponse is not None

    def test_message_response_importable(self):
        from code_indexer.server.models.auth import MessageResponse

        assert MessageResponse is not None

    def test_registration_request_importable(self):
        from code_indexer.server.models.auth import RegistrationRequest

        assert RegistrationRequest is not None

    def test_password_reset_request_importable(self):
        from code_indexer.server.models.auth import PasswordResetRequest

        assert PasswordResetRequest is not None

    def test_create_api_key_request_importable(self):
        from code_indexer.server.models.auth import CreateApiKeyRequest

        assert CreateApiKeyRequest is not None

    def test_create_api_key_response_importable(self):
        from code_indexer.server.models.auth import CreateApiKeyResponse

        assert CreateApiKeyResponse is not None

    def test_api_key_list_response_importable(self):
        from code_indexer.server.models.auth import ApiKeyListResponse

        assert ApiKeyListResponse is not None

    def test_create_mcp_credential_request_importable(self):
        from code_indexer.server.models.auth import CreateMCPCredentialRequest

        assert CreateMCPCredentialRequest is not None

    def test_create_mcp_credential_response_importable(self):
        from code_indexer.server.models.auth import CreateMCPCredentialResponse

        assert CreateMCPCredentialResponse is not None

    def test_mcp_credential_list_response_importable(self):
        from code_indexer.server.models.auth import MCPCredentialListResponse

        assert MCPCredentialListResponse is not None


class TestQueryModelsImportable:
    """Query-related models must be importable from models.query submodule."""

    def test_semantic_query_request_importable(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        assert SemanticQueryRequest is not None

    def test_query_metadata_importable(self):
        from code_indexer.server.models.query import QueryMetadata

        assert QueryMetadata is not None

    def test_semantic_query_response_importable(self):
        from code_indexer.server.models.query import SemanticQueryResponse

        assert SemanticQueryResponse is not None

    def test_fts_result_item_importable(self):
        from code_indexer.server.models.query import FTSResultItem

        assert FTSResultItem is not None

    def test_unified_search_metadata_importable(self):
        from code_indexer.server.models.query import UnifiedSearchMetadata

        assert UnifiedSearchMetadata is not None

    def test_unified_search_response_importable(self):
        from code_indexer.server.models.query import UnifiedSearchResponse

        assert UnifiedSearchResponse is not None


class TestReposModelsImportable:
    """Repository-related models must be importable from models.repos submodule."""

    def test_add_golden_repo_request_importable(self):
        from code_indexer.server.models.repos import AddGoldenRepoRequest

        assert AddGoldenRepoRequest is not None

    def test_golden_repo_info_importable(self):
        from code_indexer.server.models.repos import GoldenRepoInfo

        assert GoldenRepoInfo is not None

    def test_activate_repository_request_importable(self):
        from code_indexer.server.models.repos import ActivateRepositoryRequest

        assert ActivateRepositoryRequest is not None

    def test_activated_repository_info_importable(self):
        from code_indexer.server.models.repos import ActivatedRepositoryInfo

        assert ActivatedRepositoryInfo is not None

    def test_switch_branch_request_importable(self):
        from code_indexer.server.models.repos import SwitchBranchRequest

        assert SwitchBranchRequest is not None

    def test_repository_info_importable(self):
        from code_indexer.server.models.repos import RepositoryInfo

        assert RepositoryInfo is not None

    def test_repository_details_response_importable(self):
        from code_indexer.server.models.repos import RepositoryDetailsResponse

        assert RepositoryDetailsResponse is not None

    def test_repository_list_response_importable(self):
        from code_indexer.server.models.repos import RepositoryListResponse

        assert RepositoryListResponse is not None

    def test_available_repository_list_response_importable(self):
        from code_indexer.server.models.repos import AvailableRepositoryListResponse

        assert AvailableRepositoryListResponse is not None

    def test_repository_sync_response_importable(self):
        from code_indexer.server.models.repos import RepositorySyncResponse

        assert RepositorySyncResponse is not None

    def test_repository_branches_response_importable(self):
        from code_indexer.server.models.repos import RepositoryBranchesResponse

        assert RepositoryBranchesResponse is not None

    def test_repository_statistics_importable(self):
        from code_indexer.server.models.repos import RepositoryStatistics

        assert RepositoryStatistics is not None

    def test_git_info_importable(self):
        from code_indexer.server.models.repos import GitInfo

        assert GitInfo is not None

    def test_repository_configuration_importable(self):
        from code_indexer.server.models.repos import RepositoryConfiguration

        assert RepositoryConfiguration is not None

    def test_repository_details_v2_response_importable(self):
        from code_indexer.server.models.repos import RepositoryDetailsV2Response

        assert RepositoryDetailsV2Response is not None

    def test_component_repo_info_importable(self):
        from code_indexer.server.models.repos import ComponentRepoInfo

        assert ComponentRepoInfo is not None

    def test_composite_repository_details_importable(self):
        from code_indexer.server.models.repos import CompositeRepositoryDetails

        assert CompositeRepositoryDetails is not None

    def test_repository_sync_request_importable(self):
        from code_indexer.server.models.repos import RepositorySyncRequest

        assert RepositorySyncRequest is not None

    def test_repository_sync_job_response_importable(self):
        from code_indexer.server.models.repos import RepositorySyncJobResponse

        assert RepositorySyncJobResponse is not None

    def test_general_repository_sync_request_importable(self):
        from code_indexer.server.models.repos import GeneralRepositorySyncRequest

        assert GeneralRepositorySyncRequest is not None

    def test_branch_info_importable(self):
        from code_indexer.server.models.repos import BranchInfo

        assert BranchInfo is not None


class TestJobsModelsImportable:
    """Job-related models must be importable from models.jobs submodule."""

    def test_add_index_request_importable(self):
        from code_indexer.server.models.jobs import AddIndexRequest

        assert AddIndexRequest is not None

    def test_add_index_response_importable(self):
        from code_indexer.server.models.jobs import AddIndexResponse

        assert AddIndexResponse is not None

    def test_index_info_importable(self):
        from code_indexer.server.models.jobs import IndexInfo

        assert IndexInfo is not None

    def test_index_status_response_importable(self):
        from code_indexer.server.models.jobs import IndexStatusResponse

        assert IndexStatusResponse is not None

    def test_job_response_importable(self):
        from code_indexer.server.models.jobs import JobResponse

        assert JobResponse is not None

    def test_job_status_response_importable(self):
        from code_indexer.server.models.jobs import JobStatusResponse

        assert JobStatusResponse is not None

    def test_job_list_response_importable(self):
        from code_indexer.server.models.jobs import JobListResponse

        assert JobListResponse is not None

    def test_job_cancellation_response_importable(self):
        from code_indexer.server.models.jobs import JobCancellationResponse

        assert JobCancellationResponse is not None

    def test_job_cleanup_response_importable(self):
        from code_indexer.server.models.jobs import JobCleanupResponse

        assert JobCleanupResponse is not None

    def test_sync_progress_importable(self):
        from code_indexer.server.models.jobs import SyncProgress

        assert SyncProgress is not None

    def test_sync_job_options_importable(self):
        from code_indexer.server.models.jobs import SyncJobOptions

        assert SyncJobOptions is not None


class TestModelsPackageReExports:
    """All models must also be importable directly from code_indexer.server.models."""

    def test_login_request_from_package(self):
        from code_indexer.server.models import LoginRequest

        assert LoginRequest is not None

    def test_semantic_query_request_from_package(self):
        from code_indexer.server.models import SemanticQueryRequest

        assert SemanticQueryRequest is not None

    def test_activate_repository_request_from_package(self):
        from code_indexer.server.models import ActivateRepositoryRequest

        assert ActivateRepositoryRequest is not None

    def test_add_index_request_from_package(self):
        from code_indexer.server.models import AddIndexRequest

        assert AddIndexRequest is not None

    def test_job_response_from_package(self):
        from code_indexer.server.models import JobResponse

        assert JobResponse is not None

    def test_component_repo_info_from_package(self):
        from code_indexer.server.models import ComponentRepoInfo

        assert ComponentRepoInfo is not None

    def test_change_password_request_from_package(self):
        from code_indexer.server.models import ChangePasswordRequest

        assert ChangePasswordRequest is not None

    def test_repository_details_response_from_package(self):
        from code_indexer.server.models import RepositoryDetailsResponse

        assert RepositoryDetailsResponse is not None


class TestAppPyBackwardCompatibility:
    """Models imported from app.py must still work (backward compat for existing tests)."""

    def test_semantic_query_request_still_in_app(self):
        from code_indexer.server.app import SemanticQueryRequest

        assert SemanticQueryRequest is not None

    def test_activate_repository_request_still_in_app(self):
        from code_indexer.server.app import ActivateRepositoryRequest

        assert ActivateRepositoryRequest is not None

    def test_add_golden_repo_request_still_in_app(self):
        from code_indexer.server.app import AddGoldenRepoRequest

        assert AddGoldenRepoRequest is not None

    def test_add_index_request_still_in_app(self):
        from code_indexer.server.app import AddIndexRequest

        assert AddIndexRequest is not None

    def test_change_password_request_still_in_app(self):
        from code_indexer.server.app import ChangePasswordRequest

        assert ChangePasswordRequest is not None

    def test_component_repo_info_still_in_app(self):
        from code_indexer.server.app import ComponentRepoInfo

        assert ComponentRepoInfo is not None

    def test_repository_details_response_still_in_app(self):
        from code_indexer.server.app import RepositoryDetailsResponse

        assert RepositoryDetailsResponse is not None

    def test_semantic_query_response_still_in_app(self):
        from code_indexer.server.app import SemanticQueryResponse

        assert SemanticQueryResponse is not None

    def test_query_result_item_still_in_app(self):
        from code_indexer.server.app import QueryResultItem

        assert QueryResultItem is not None


class TestModelIntegrity:
    """Verify moved models still behave correctly (not just importable)."""

    def test_login_request_validation_rejects_empty_username(self):
        from code_indexer.server.models.auth import LoginRequest

        with pytest.raises(Exception):
            LoginRequest(username="", password="somepassword")

    def test_login_request_strips_whitespace_from_username(self):
        from code_indexer.server.models.auth import LoginRequest

        req = LoginRequest(username="  admin  ", password="somepassword")
        assert req.username == "admin"

    def test_semantic_query_request_rejects_empty_query(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        with pytest.raises(Exception):
            SemanticQueryRequest(query_text="")

    def test_activate_repository_request_requires_alias(self):
        from code_indexer.server.models.repos import ActivateRepositoryRequest

        with pytest.raises(Exception):
            ActivateRepositoryRequest()

    def test_add_index_request_requires_index_type(self):
        from code_indexer.server.models.jobs import AddIndexRequest

        with pytest.raises(Exception):
            AddIndexRequest()

    def test_add_index_request_get_index_types_single(self):
        from code_indexer.server.models.jobs import AddIndexRequest

        req = AddIndexRequest(index_type="semantic")
        assert req.get_index_types() == ["semantic"]

    def test_add_index_request_get_index_types_multi(self):
        from code_indexer.server.models.jobs import AddIndexRequest

        req = AddIndexRequest(index_types=["semantic", "fts"])
        assert req.get_index_types() == ["semantic", "fts"]
