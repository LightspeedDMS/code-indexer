"""Test UserManager dependency injection in OAuth routes."""

from code_indexer.server.auth.oauth.routes import get_user_manager
from code_indexer.server.auth.user_manager import UserManager


class TestUserManagerInjection:
    """Test that UserManager can be overridden via dependency injection."""

    def test_get_user_manager_dependency_exists(self):
        """Test that get_user_manager dependency function exists."""
        # This test will fail if get_user_manager doesn't exist
        assert callable(get_user_manager)

    def test_get_user_manager_returns_the_servers_account_store(self, tmp_path):
        """OAuth routes use the server's account store object itself."""
        from fastapi import FastAPI
        from starlette.requests import Request

        app = FastAPI()
        app.state.user_manager = UserManager(
            users_file_path=str(tmp_path / "users.json")
        )
        request = Request({"type": "http", "headers": [], "app": app})

        assert get_user_manager(request) is app.state.user_manager
