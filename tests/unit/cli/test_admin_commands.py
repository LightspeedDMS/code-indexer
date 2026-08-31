"""
Tests for admin CLI commands with real server integration.

Foundation #1 compliant tests with no mocks - testing actual CLI commands
with real CIDX server and authentication flows.

Zero mocks - uses real project directories and file system operations.
"""

import asyncio
import pytest
import tempfile
import os
import threading
from pathlib import Path
from click.testing import CliRunner

from code_indexer.cli import cli
from tests.infrastructure.test_cidx_server import CIDXServerTestContext

pytestmark = pytest.mark.slow


class AsyncServerWrapper:
    """Wrapper to run a real uvicorn-backed CIDXServerTestContext on a
    dedicated background thread with its own continuously-running asyncio
    event loop.

    Bug #1469: the previous implementation created an event loop and drove
    it only long enough for CIDXServerTestContext.__aenter__() to complete
    (via run_until_complete()), then returned control to the synchronous
    test thread. run_until_complete() always stops the loop the instant its
    awaited coroutine finishes, so uvicorn's already-bound listening socket
    was left with nothing to accept()/process further connections -- any
    real HTTP request issued afterwards from the test thread hung until the
    client's read timeout fired ("Request timed out. Check your network
    connection or try again later."). Running the loop on its own thread
    via run_forever() keeps it pumping for the server's entire lifetime, so
    it can service requests concurrently with the calling thread's
    synchronous HTTP client calls.
    """

    def __init__(self):
        self.server = None
        self.server_url = None
        self.loop = None
        self._context = None
        self._thread = None

    def start_server(self):
        """Start the server on a dedicated background thread whose event
        loop keeps running for the lifetime of the wrapper."""
        self.loop = asyncio.new_event_loop()
        loop_ready = threading.Event()

        def _run_loop():
            assert self.loop is not None
            loop = self.loop
            asyncio.set_event_loop(loop)
            loop.call_soon(loop_ready.set)
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run_loop, daemon=True)
        self._thread.start()
        if not loop_ready.wait(timeout=5.0):
            raise RuntimeError("Background event loop failed to start in time")

        async def _start():
            self._context = CIDXServerTestContext()
            self.server = await self._context.__aenter__()
            self.server_url = self._context.base_url
            return self.server

        future = asyncio.run_coroutine_threadsafe(_start(), self.loop)
        return future.result(timeout=10.0)

    def stop_server(self):
        """Stop the server and shut down its background event-loop thread."""
        if self._context and self.loop and not self.loop.is_closed():

            async def _stop():
                try:
                    await self._context.__aexit__(None, None, None)  # type: ignore[union-attr]
                except Exception:
                    # Suppress cleanup errors that occur during shutdown
                    pass

            try:
                future = asyncio.run_coroutine_threadsafe(_stop(), self.loop)
                future.result(timeout=5.0)
            except Exception:
                # Suppress cleanup errors/timeouts - the thread stop below
                # still runs unconditionally.
                pass

        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=5.0)

        # Clean up references
        self.server = None
        self.server_url = None
        self._context = None
        self._thread = None


class TestAdminUsersCreateCommand:
    """Test 'cidx admin users create' command with real server."""

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            yield project_path

    def setup_remote_config(self, project_path: Path, server_url: str):
        """Setup remote configuration for testing."""
        from code_indexer.remote.config import create_remote_configuration

        # Create remote configuration files with placeholder encrypted credentials
        # This will be updated when actual credentials are stored
        create_remote_configuration(
            project_root=project_path,
            server_url=server_url,
            username="admin",  # Default username for testing
            encrypted_credentials="placeholder",  # Will be updated by credential setup
        )

    def setup_admin_credentials(self, project_path: Path):
        """Setup admin credentials for testing."""
        from code_indexer.remote.credential_manager import (
            ProjectCredentialManager,
            store_encrypted_credentials,
        )

        credential_manager = ProjectCredentialManager()

        # Get server URL from remote config to ensure consistency
        from code_indexer.remote.config import load_remote_configuration

        config = load_remote_configuration(project_path)
        server_url = config["server_url"]

        # Encrypt and store credentials
        encrypted_data = credential_manager.encrypt_credentials(
            username="admin",
            password="admin123",
            server_url=server_url,
            repo_path=str(project_path),
        )

        store_encrypted_credentials(project_path, encrypted_data)

    def test_admin_users_create_success(self, test_server, temp_project_dir):
        """Test successful user creation via CLI command."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Debug: Check what files were created
        config_dir = temp_project_dir / ".code-indexer"
        print(f"Config dir exists: {config_dir.exists()}")
        if config_dir.exists():
            print(f"Files in config dir: {list(config_dir.iterdir())}")
            remote_config_file = config_dir / ".remote-config"
            if remote_config_file.exists():
                print(f"Remote config content: {remote_config_file.read_text()}")
            creds_file = config_dir / ".creds"
            if creds_file.exists():
                print(f"Creds file exists: {creds_file.stat().st_size} bytes")

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "testuser123",
                    "--role",
                    "normal_user",
                    "--password",
                    "TestPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        # Debug output for troubleshooting
        print(f"Exit code: {result.exit_code}")
        print(f"Output: {result.output}")
        if result.exception:
            print(f"Exception: {result.exception}")

        assert result.exit_code == 0, f"Command failed with output: {result.output}"
        assert "Successfully created user: testuser123" in result.output
        assert "Role: normal_user" in result.output

    def test_admin_users_create_with_email(self, test_server, temp_project_dir):
        """Test user creation with email option."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "emailuser",
                    "--email",
                    "test@example.com",
                    "--role",
                    "power_user",
                    "--password",
                    "TestPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Successfully created user: emailuser" in result.output
        assert "Role: power_user" in result.output
        assert "Email: test@example.com" in result.output

    def test_admin_users_create_interactive_password(
        self, test_server, temp_project_dir
    ):
        """Test user creation with interactive password prompt."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            # Simulate password input
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "interactiveuser",
                    "--role",
                    "normal_user",
                ],
                input="TestPass123!\nTestPass123!\n",
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Successfully created user: interactiveuser" in result.output

    def test_admin_users_create_validation_error(self, test_server, temp_project_dir):
        """Test user creation with validation error."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            # Test with invalid username (too short)
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "ab",
                    "--role",
                    "normal_user",
                    "--password",
                    "TestPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "Validation error" in result.output
        assert "at least 3 characters" in result.output

    def test_admin_users_create_duplicate_user(self, test_server, temp_project_dir):
        """Test user creation fails for duplicate username."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            # Create first user
            result1 = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "duplicate123",
                    "--role",
                    "normal_user",
                    "--password",
                    "TestPass123!",
                ],
            )
            assert result1.exit_code == 0

            # Attempt to create duplicate user
            result2 = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "duplicate123",
                    "--role",
                    "normal_user",
                    "--password",
                    "AnotherPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result2.exit_code == 1
        assert "User creation failed" in result2.output
        assert "already exists" in result2.output.lower()

    def test_admin_users_create_invalid_role(self, test_server, temp_project_dir):
        """Test user creation with invalid role."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "invalidroleuser",
                    "--role",
                    "invalid_role",
                    "--password",
                    "TestPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        # Click should catch this at option validation level
        assert result.exit_code != 0
        assert "is not one of" in result.output.lower()

    def test_admin_users_create_weak_password(self, test_server, temp_project_dir):
        """Test user creation with weak password."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "weakpassuser",
                    "--role",
                    "normal_user",
                    "--password",
                    "password",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "Password validation error" in result.output
        assert "too common" in result.output

    def test_admin_users_create_no_credentials(self, test_server, temp_project_dir):
        """Test user creation fails without credentials."""
        # Setup only remote config, no credentials
        self.setup_remote_config(temp_project_dir, test_server.server_url)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "nocredsuser",
                    "--role",
                    "normal_user",
                    "--password",
                    "TestPass123!",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "No credentials found" in result.output
        assert "auth login" in result.output

    def test_admin_users_create_no_project_config(self):
        """Test user creation fails without project configuration."""
        # Create temp directory WITHOUT .code-indexer to test uninitialized mode
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                result = runner.invoke(
                    cli,
                    [
                        "admin",
                        "users",
                        "create",
                        "noprojectuser",
                        "--role",
                        "normal_user",
                        "--password",
                        "TestPass123!",
                    ],
                )
            finally:
                os.chdir(old_cwd)

        assert result.exit_code == 1
        # Real behavior: admin commands are not available in local mode
        assert "not available in local mode" in result.output
        assert "remote" in result.output.lower()


class TestAdminUsersListCommand:
    """Test 'cidx admin users list' command with real server."""

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            yield project_path

    def setup_remote_config(self, project_path: Path, server_url: str):
        """Setup remote configuration for testing."""
        from code_indexer.remote.config import create_remote_configuration

        # Create remote configuration files with placeholder encrypted credentials
        # This will be updated when actual credentials are stored
        create_remote_configuration(
            project_root=project_path,
            server_url=server_url,
            username="admin",  # Default username for testing
            encrypted_credentials="placeholder",  # Will be updated by credential setup
        )

    def setup_admin_credentials(self, project_path: Path):
        """Setup admin credentials for testing."""
        from code_indexer.remote.credential_manager import (
            ProjectCredentialManager,
            store_encrypted_credentials,
        )

        credential_manager = ProjectCredentialManager()

        # Get server URL from remote config to ensure consistency
        from code_indexer.remote.config import load_remote_configuration

        config = load_remote_configuration(project_path)
        server_url = config["server_url"]

        # Encrypt and store credentials
        encrypted_data = credential_manager.encrypt_credentials(
            username="admin",
            password="admin123",
            server_url=server_url,
            repo_path=str(project_path),
        )

        store_encrypted_credentials(project_path, encrypted_data)

    def test_admin_users_list_success(self, test_server, temp_project_dir):
        """Test successful user listing via CLI command."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            # First create some users to list
            runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "listuser1",
                    "--role",
                    "normal_user",
                    "--password",
                    "TestPass123!",
                ],
            )

            runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    "listuser2",
                    "--role",
                    "power_user",
                    "--password",
                    "TestPass123!",
                ],
            )

            # Test user listing
            result = runner.invoke(cli, ["admin", "users", "list"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Users" in result.output
        assert "Username" in result.output
        assert "Role" in result.output
        assert "Created" in result.output
        assert "listuser1" in result.output
        assert "listuser2" in result.output

    def test_admin_users_list_with_pagination(self, test_server, temp_project_dir):
        """Test user listing with pagination options."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            # Create multiple users
            for i in range(5):
                runner.invoke(
                    cli,
                    [
                        "admin",
                        "users",
                        "create",
                        f"pageuser{i}",
                        "--role",
                        "normal_user",
                        "--password",
                        "TestPass123!",
                    ],
                )

            # Test pagination
            result = runner.invoke(
                cli, ["admin", "users", "list", "--limit", "2", "--offset", "0"]
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Users" in result.output

    def test_admin_users_list_no_users(self, test_server, temp_project_dir):
        """Test user listing when no users exist (edge case)."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "list"])
        finally:
            os.chdir(old_cwd)

        # Should succeed even if no users (may show system users)
        assert result.exit_code == 0

    def test_admin_users_list_no_credentials(self, test_server, temp_project_dir):
        """Test user listing fails without credentials."""
        # Setup only remote config, no credentials
        self.setup_remote_config(temp_project_dir, test_server.server_url)

        # Run CLI from within project directory for real project detection
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "list"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "No credentials found" in result.output
        assert "auth login" in result.output


class TestAdminUsersShowCommand:
    """Test 'cidx admin users show' command with real server."""

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            yield project_path

    def setup_remote_config(self, project_path: Path, server_url: str):
        """Setup remote configuration for testing."""
        from code_indexer.remote.config import create_remote_configuration

        # Create remote configuration files with placeholder encrypted credentials
        # This will be updated when actual credentials are stored
        create_remote_configuration(
            project_root=project_path,
            server_url=server_url,
            username="admin",  # Default username for testing
            encrypted_credentials="placeholder",  # Will be updated by credential setup
        )

    def setup_admin_credentials(self, project_path: Path):
        """Setup admin credentials for testing."""
        from code_indexer.remote.credential_manager import (
            ProjectCredentialManager,
            store_encrypted_credentials,
        )

        credential_manager = ProjectCredentialManager()

        # Get server URL from remote config to ensure consistency
        from code_indexer.remote.config import load_remote_configuration

        config = load_remote_configuration(project_path)
        server_url = config["server_url"]

        # Encrypt and store credentials
        encrypted_data = credential_manager.encrypt_credentials(
            username="admin",
            password="admin123",
            server_url=server_url,
            repo_path=str(project_path),
        )

        store_encrypted_credentials(project_path, encrypted_data)

    def create_test_user(
        self, runner, temp_project_dir, username: str, role: str = "normal_user"
    ):
        """Helper to create a test user."""
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    username,
                    "--role",
                    role,
                    "--password",
                    "TestPass123!",
                ],
            )
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_admin_users_show_success(self, test_server, temp_project_dir):
        """Test successful user details display via CLI command."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create a test user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "testdetailuser", "power_user")

        # Test showing user details
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "show", "testdetailuser"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "User Details" in result.output
        assert "testdetailuser" in result.output
        assert "power_user" in result.output
        assert "Created:" in result.output

    def test_admin_users_show_nonexistent_user(self, test_server, temp_project_dir):
        """Test showing details for non-existent user."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Test showing non-existent user
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "show", "nonexistentuser"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_admin_users_show_no_credentials(self, test_server, temp_project_dir):
        """Test showing user fails without credentials."""
        # Setup only remote config, no credentials
        self.setup_remote_config(temp_project_dir, test_server.server_url)

        # Test showing user without credentials
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "show", "testuser"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "No credentials found" in result.output
        assert "auth login" in result.output


class TestAdminUsersUpdateCommand:
    """Test 'cidx admin users update' command with real server and safety features."""

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            yield project_path

    def setup_remote_config(self, project_path: Path, server_url: str):
        """Setup remote configuration for testing."""
        from code_indexer.remote.config import create_remote_configuration

        create_remote_configuration(
            project_root=project_path,
            server_url=server_url,
            username="admin",
            encrypted_credentials="placeholder",
        )

    def setup_admin_credentials(self, project_path: Path):
        """Setup admin credentials for testing."""
        from code_indexer.remote.credential_manager import (
            ProjectCredentialManager,
            store_encrypted_credentials,
        )

        credential_manager = ProjectCredentialManager()

        from code_indexer.remote.config import load_remote_configuration

        config = load_remote_configuration(project_path)
        server_url = config["server_url"]

        encrypted_data = credential_manager.encrypt_credentials(
            username="admin",
            password="admin123",
            server_url=server_url,
            repo_path=str(project_path),
        )

        store_encrypted_credentials(project_path, encrypted_data)

    def create_test_user(
        self, runner, temp_project_dir, username: str, role: str = "normal_user"
    ):
        """Helper to create a test user."""
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    username,
                    "--role",
                    role,
                    "--password",
                    "TestPass123!",
                ],
            )
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_admin_users_update_role_success(self, test_server, temp_project_dir):
        """Test successful user role update via CLI command."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create a test user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "updateuser", "normal_user")

        # Test updating user role
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "update",
                    "updateuser",
                    "--role",
                    "power_user",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Successfully updated user 'updateuser'" in result.output
        assert "power_user" in result.output

    def test_admin_users_update_admin_demotion_warning(
        self, test_server, temp_project_dir
    ):
        """Test admin demotion shows warning message."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create an admin user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "adminuser", "admin")

        # Test demoting admin user (should show warning)
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "update",
                    "adminuser",
                    "--role",
                    "normal_user",
                ],
                input="y\n",  # Confirm the demotion
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        # Should contain warning about admin demotion
        assert "admin" in result.output.lower()
        assert "confirm" in result.output.lower() or "warning" in result.output.lower()

    def test_admin_users_update_nonexistent_user(self, test_server, temp_project_dir):
        """Test updating non-existent user."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Test updating non-existent user
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "update",
                    "nonexistentuser",
                    "--role",
                    "power_user",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_admin_users_update_invalid_role(self, test_server, temp_project_dir):
        """Test updating user with invalid role."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create a test user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "invalidroleuser")

        # Test updating with invalid role
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "update",
                    "invalidroleuser",
                    "--role",
                    "invalid_role",
                ],
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code != 0
        assert "is not one of" in result.output.lower()


class TestAdminUsersDeleteCommand:
    """Test 'cidx admin users delete' command with real server and self-deletion prevention."""

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            yield project_path

    def setup_remote_config(self, project_path: Path, server_url: str):
        """Setup remote configuration for testing."""
        from code_indexer.remote.config import create_remote_configuration

        create_remote_configuration(
            project_root=project_path,
            server_url=server_url,
            username="admin",
            encrypted_credentials="placeholder",
        )

    def setup_admin_credentials(self, project_path: Path):
        """Setup admin credentials for testing."""
        from code_indexer.remote.credential_manager import (
            ProjectCredentialManager,
            store_encrypted_credentials,
        )

        credential_manager = ProjectCredentialManager()

        from code_indexer.remote.config import load_remote_configuration

        config = load_remote_configuration(project_path)
        server_url = config["server_url"]

        encrypted_data = credential_manager.encrypt_credentials(
            username="admin",
            password="admin123",
            server_url=server_url,
            repo_path=str(project_path),
        )

        store_encrypted_credentials(project_path, encrypted_data)

    def create_test_user(
        self, runner, temp_project_dir, username: str, role: str = "normal_user"
    ):
        """Helper to create a test user."""
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                [
                    "admin",
                    "users",
                    "create",
                    username,
                    "--role",
                    role,
                    "--password",
                    "TestPass123!",
                ],
            )
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_admin_users_delete_success(self, test_server, temp_project_dir):
        """Test successful user deletion via CLI command."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create a test user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "deleteuser")

        # Test deleting user with confirmation
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                ["admin", "users", "delete", "deleteuser"],
                input="y\n",  # Confirm deletion
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "Successfully deleted user 'deleteuser'" in result.output

    def test_admin_users_delete_self_prevention(self, test_server, temp_project_dir):
        """Test self-deletion prevention mechanism."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Test attempting to delete own user (admin)
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "delete", "admin"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "cannot delete your own account" in result.output.lower()

    def test_admin_users_delete_confirmation_required(
        self, test_server, temp_project_dir
    ):
        """Test deletion requires confirmation prompt."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Create a test user first
        runner = CliRunner()
        self.create_test_user(runner, temp_project_dir, "confirmuser")

        # Test deleting user without confirmation (should abort)
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                ["admin", "users", "delete", "confirmuser"],
                input="n\n",  # Decline deletion
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        assert "confirm" in result.output.lower() or "delete" in result.output.lower()

    def test_admin_users_delete_last_admin_prevention(
        self, test_server, temp_project_dir
    ):
        """Test prevention of deleting the last admin user."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Test attempting to delete admin user (should be prevented if last admin)
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                ["admin", "users", "delete", "admin"],
                input="y\n",  # Confirm deletion
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        # Should prevent deletion with appropriate message
        assert (
            "cannot delete" in result.output.lower()
            and "admin" in result.output.lower()
        )

    def test_admin_users_delete_nonexistent_user(self, test_server, temp_project_dir):
        """Test deleting non-existent user."""
        # Setup configuration
        self.setup_remote_config(temp_project_dir, test_server.server_url)
        self.setup_admin_credentials(temp_project_dir)

        # Test deleting non-existent user
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(
                cli,
                ["admin", "users", "delete", "nonexistentuser"],
                input="y\n",  # Confirm deletion attempt so the 404 path is reached
            )
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_admin_users_delete_no_credentials(self, test_server, temp_project_dir):
        """Test deleting user fails without credentials."""
        # Setup only remote config, no credentials
        self.setup_remote_config(temp_project_dir, test_server.server_url)

        # Test deleting user without credentials
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(temp_project_dir))
            result = runner.invoke(cli, ["admin", "users", "delete", "testuser"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 1
        assert "No credentials found" in result.output
        assert "auth login" in result.output


class TestAsyncServerWrapperRealHTTPRoundTrip:
    """Bug #1469: the shared AsyncServerWrapper fixture must keep its
    background uvicorn server's asyncio event loop running for the full
    lifetime of the test, not just while start_server() polls for
    readiness.

    Before the fix, start_server()'s asyncio.new_event_loop() +
    run_until_complete(_start()) stops driving the loop the instant
    _start() returns (run_until_complete() always halts the loop once its
    awaited coroutine completes). uvicorn's listening socket is already
    bound at that point (TCP connects succeed), but nothing is left to
    accept()/process further connections, so any real HTTP request made
    afterwards from the synchronous test thread hangs until the client's
    read timeout fires -- exactly the "Request timed out" pattern reported
    in bug #1469.
    """

    @pytest.fixture
    def test_server(self):
        """Start real CIDX server for testing (mirrors the other fixtures
        in this module -- exercises the exact same shared
        AsyncServerWrapper implementation)."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url
        try:
            yield server
        finally:
            wrapper.stop_server()

    def test_health_endpoint_responds_without_read_timeout(self, test_server):
        """A real synchronous HTTP GET issued well after start_server()
        returns must receive an actual response quickly, proving the
        server's event loop is still being driven concurrently with the
        calling thread instead of sitting idle until the client times out."""
        import httpx

        response = httpx.get(f"{test_server.server_url}/health", timeout=5.0)

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
