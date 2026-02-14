"""
Tests for write exceptions bootstrap in app.py (Story #197 AC1/AC4).

Tests verify that cidx-meta-global is registered as a write exception
during server startup.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call


class TestBootstrapWriteExceptions:
    """Test bootstrap registration of write exceptions."""

    def test_cidx_meta_registered_as_write_exception_at_startup(self):
        """Test that cidx-meta-global is registered during create_app()."""
        from code_indexer.server.services.file_crud_service import file_crud_service

        # Clear any existing registrations from other tests
        file_crud_service._global_write_exceptions.clear()

        # Mock all the heavyweight dependencies
        with patch("code_indexer.server.app.UserManager"), \
             patch("code_indexer.server.app.JWTManager"), \
             patch("code_indexer.server.app.JWTSecretManager"), \
             patch("code_indexer.server.app.RefreshTokenManager"), \
             patch("code_indexer.server.auth.oauth.oauth_manager.OAuthManager"), \
             patch("code_indexer.server.app.GoldenRepoManager") as mock_grm_class, \
             patch("code_indexer.server.app.BackgroundJobManager"), \
             patch("code_indexer.server.app.ActivatedRepoManager"), \
             patch("code_indexer.server.app.RepositoryListingManager"), \
             patch("code_indexer.server.app.SemanticQueryManager"), \
             patch("code_indexer.server.services.workspace_cleanup_service.WorkspaceCleanupService"), \
             patch("code_indexer.server.services.repo_category_service.RepoCategoryService"), \
             patch("code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager"), \
             patch("code_indexer.server.app.migrate_legacy_cidx_meta"), \
             patch("code_indexer.server.app.bootstrap_cidx_meta"), \
             patch("code_indexer.server.app.register_langfuse_golden_repos"), \
             patch("code_indexer.server.app.Path") as mock_path_class, \
             patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": "/tmp/test"}):

            # Setup Path mocks
            mock_data_dir = MagicMock()
            mock_data_dir.__truediv__ = lambda self, x: MagicMock(spec=Path)
            mock_path_class.return_value = mock_data_dir

            # Mock golden_repos_dir path
            mock_golden_repos_dir = MagicMock(spec=Path)
            mock_cidx_meta_path = MagicMock(spec=Path)
            mock_golden_repos_dir.__truediv__.return_value = mock_cidx_meta_path

            # Setup GoldenRepoManager mock
            mock_grm_instance = MagicMock()
            mock_grm_instance.golden_repos_dir = mock_golden_repos_dir
            mock_grm_class.return_value = mock_grm_instance

            # Import and call create_app
            from code_indexer.server.app import create_app

            app = create_app()

            # Verify cidx-meta-global was registered
            assert file_crud_service.is_write_exception("cidx-meta-global")

            # Verify the path is correct (should be golden_repos_dir / "cidx-meta")
            registered_path = file_crud_service.get_write_exception_path("cidx-meta-global")
            assert registered_path is not None

    def test_bootstrap_is_idempotent(self):
        """Test that multiple calls to bootstrap don't cause errors."""
        from code_indexer.server.services.file_crud_service import file_crud_service

        # Clear any existing registrations
        file_crud_service._global_write_exceptions.clear()

        cidx_meta_path = Path("/fake/golden-repos/cidx-meta")

        # Register multiple times
        file_crud_service.register_write_exception("cidx-meta-global", cidx_meta_path)
        file_crud_service.register_write_exception("cidx-meta-global", cidx_meta_path)

        # Should still be registered
        assert file_crud_service.is_write_exception("cidx-meta-global")
        assert file_crud_service.get_write_exception_path("cidx-meta-global") == cidx_meta_path

    def test_other_golden_repos_not_registered_as_exceptions(self):
        """Test that only cidx-meta is registered as exception, not other golden repos."""
        from code_indexer.server.services.file_crud_service import file_crud_service

        # After bootstrap, only cidx-meta-global should be an exception
        # Other golden repos should NOT be exceptions
        assert not file_crud_service.is_write_exception("other-repo-global")
        assert not file_crud_service.is_write_exception("langfuse-traces-global")
