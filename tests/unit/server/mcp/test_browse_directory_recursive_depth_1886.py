"""
Unit tests for browse_directory's wiring of FileListQueryParams.direct_children_of
(Bug #1886).

recursive:false is documented as "returns only immediate children (single
level)" but nothing was ever wired to enforce that. These tests pin down the
exact contract between browse_directory's normalized params (path, recursive)
and the direct_children_of value passed to FileListQueryParams, following the
existing mock-based wiring pattern in test_browse_directory_filters.py.
"""

from unittest.mock import Mock, MagicMock, patch
from code_indexer.server.mcp.handlers import browse_directory
from code_indexer.server.auth.user_manager import User, UserRole


def _mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _query_params_for(extra_params: dict):
    """Run browse_directory with a mocked file_service and return the
    FileListQueryParams it was called with (shared setup for every test
    below -- Bug #1886 wiring is a pure translation of params, so each
    scenario differs only in the input dict and the field asserted)."""
    mock_service = MagicMock()
    mock_service.list_files.return_value = Mock(files=[])
    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app_module:
        mock_app_module.file_service = mock_service
        params = {"repository_alias": "test-repo", **extra_params}
        browse_directory(params, _mock_user())
        return mock_service.list_files.call_args.kwargs["query_params"]


class TestDirectChildrenOfNonRecursiveWiring:
    """recursive=False must set direct_children_of to the browse target."""

    def test_root_non_recursive_sets_empty_string(self):
        qp = _query_params_for({"recursive": False})
        assert qp.direct_children_of == ""

    def test_subdir_non_recursive_sets_path_value(self):
        qp = _query_params_for({"path": "examples", "recursive": False})
        assert qp.direct_children_of == "examples"

    def test_subdir_trailing_slash_non_recursive_is_normalized(self):
        qp = _query_params_for({"path": "examples/", "recursive": False})
        assert qp.direct_children_of == "examples"


class TestDirectChildrenOfRecursiveWiring:
    """recursive=True (default or explicit) must leave depth unrestricted."""

    def test_recursive_true_default_leaves_direct_children_of_none(self):
        qp = _query_params_for({"path": "examples"})
        assert qp.direct_children_of is None

    def test_recursive_explicit_true_leaves_direct_children_of_none(self):
        qp = _query_params_for({"path": "examples", "recursive": True})
        assert qp.direct_children_of is None

    def test_non_recursive_with_path_pattern_still_sets_direct_children_of(self):
        qp = _query_params_for(
            {"path": "examples", "path_pattern": "*.java", "recursive": False}
        )
        assert qp.direct_children_of == "examples"
        # path_pattern building itself is unchanged (existing contract).
        assert qp.path_pattern == "examples/*.java"
