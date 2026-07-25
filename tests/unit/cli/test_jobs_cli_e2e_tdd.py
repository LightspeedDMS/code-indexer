"""
End-to-End TDD tests for jobs CLI command with real server integration.

Tests the complete workflow from CLI command to server response,
validating the full Story 8 implementation with real components.
"""

import asyncio
import threading

import pytest
from click.testing import CliRunner
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import json

from code_indexer.cli import cli

# Import real infrastructure (no mocks)
from tests.infrastructure.test_cidx_server import CIDXServerTestContext

pytestmark = pytest.mark.slow


class AsyncServerWrapper:
    """Wrapper to run a real uvicorn-backed CIDXServerTestContext on a
    dedicated background thread with its own continuously-running asyncio
    event loop.

    Bug #1474: this file's ``real_server_with_jobs`` fixture used to be a
    ``pytest_asyncio.fixture`` async generator consumed by *synchronous*
    ``def test_...`` methods. pytest-asyncio only drives the fixture's
    event loop long enough to run each phase (setup up to the ``yield``,
    then teardown) via ``run_until_complete()`` -- between those two calls,
    while the synchronous test body executes, the loop is not running at
    all. The uvicorn server task created inside ``CIDXServerTestContext``
    is bound to that same loop, so its listening socket has nothing left
    to accept()/process connections while the test runs: every real HTTP
    request made by the CLI during the test hangs until the client's read
    timeout fires. This is the same underlying architectural defect fixed
    for ``AsyncServerWrapper`` in ``test_admin_commands.py`` under bug
    #1469, just manifesting through pytest-asyncio's fixture scoping
    instead of a hand-rolled ``run_until_complete()`` call -- and it was
    masked here by lenient ``assert result.exit_code in [0, 1]``
    assertions that tolerate the resulting timeout-driven failure path
    instead of failing outright.

    Running the loop on its own thread via ``run_forever()`` keeps it
    pumping for the server's entire lifetime, so it can service requests
    concurrently with the calling (synchronous) test thread.
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
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(loop_ready.set)
            try:
                self.loop.run_forever()
            finally:
                self.loop.close()

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


class TestJobsCLIEndToEndTDD:
    """End-to-end testing for jobs CLI command with real server integration."""

    @pytest.fixture
    def real_server_with_jobs(self):
        """Real CIDX server with test jobs for E2E testing."""
        wrapper = AsyncServerWrapper()
        server = wrapper.start_server()
        server.server_url = wrapper.server_url  # Add server_url to server object
        try:
            # Add test repositories
            server.add_test_repository(
                repo_id="test-repo-1",
                name="Test Repository",
                path="/test/repo",
                branches=["main", "develop"],
                default_branch="main",
            )

            # Add test jobs with various statuses
            server.add_test_job(
                job_id="job-running-123",
                repository_id="test-repo-1",
                job_status="running",
                progress=45,
            )
            server.add_test_job(
                job_id="job-completed-456",
                repository_id="test-repo-1",
                job_status="completed",
                progress=100,
            )
            server.add_test_job(
                job_id="job-failed-789",
                repository_id="test-repo-1",
                job_status="failed",
                progress=75,
            )
            server.add_test_job(
                job_id="job-cancelled-012",
                repository_id="test-repo-1",
                job_status="cancelled",
                progress=30,
            )
            yield server
        finally:
            wrapper.stop_server()

    @pytest.fixture
    def temp_project_dir(self):
        """Create temporary project directory with remote credentials."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create .code-indexer directory
            config_dir = project_path / ".code-indexer"
            config_dir.mkdir()

            # Create .remote-config file to trigger remote mode detection
            remote_config = {
                "server_url": "http://localhost:8000",
                "encrypted_credentials": "dummy",
            }
            remote_config_path = config_dir / ".remote-config"
            with open(remote_config_path, "w") as f:
                json.dump(remote_config, f)

            yield project_path

    @pytest.fixture
    def cli_runner(self):
        """Provide CLI runner for testing."""
        return CliRunner()

    @pytest.fixture
    def setup_remote_credentials(self, real_server_with_jobs, temp_project_dir):
        """Setup remote credentials for E2E testing."""
        # Create mock credential file
        credentials = {
            "username": "testuser",
            "password": "testpass123",
            "server_url": real_server_with_jobs.base_url,
            "encrypted": False,  # For testing
        }

        # Mock the credential loading chain properly
        with (
            patch("code_indexer.cli.find_project_root") as mock_find_root,
            patch(
                "code_indexer.disabled_commands.find_project_root"
            ) as mock_find_root_disabled,
            patch(
                "code_indexer.remote.config.load_remote_configuration"
            ) as mock_load_remote_config,
            patch(
                "code_indexer.remote.credential_manager.load_encrypted_credentials"
            ) as mock_load_encrypted_creds,
            patch(
                "code_indexer.remote.credential_manager.ProjectCredentialManager"
            ) as mock_cred_manager,
        ):
            # Setup mocks
            mock_find_root.return_value = temp_project_dir
            mock_find_root_disabled.return_value = temp_project_dir
            mock_load_remote_config.return_value = {
                "username": "testuser",
                "server_url": real_server_with_jobs.base_url,
            }
            mock_load_encrypted_creds.return_value = b"mock_encrypted_data"

            # Mock credential manager
            mock_manager = MagicMock()
            from types import SimpleNamespace

            mock_creds = SimpleNamespace(
                username="testuser",
                password="testpass123",
                server_url=real_server_with_jobs.base_url,
            )
            mock_manager.decrypt_credentials.return_value = mock_creds
            mock_cred_manager.return_value = mock_manager

            yield credentials

    def test_jobs_list_e2e_basic_functionality(
        self, cli_runner, setup_remote_credentials
    ):
        """Test basic jobs list functionality end-to-end with real server."""
        # Execute the CLI command
        result = cli_runner.invoke(cli, ["jobs", "list"])

        # Bug #1474: with the event-loop fix in place, a real HTTP round
        # trip against the real test server succeeds reliably -- assert the
        # actual success path (exit_code == 0), not a tolerated failure.
        # A recurrence of the event-loop defect would produce a slow
        # exit_code == 1 timeout; only a strict check catches that.
        assert result.exit_code == 0, result.output

        # Verify the command reaches the server and renders the real job
        # data seeded by the real_server_with_jobs fixture (4 jobs total).
        assert "Background Jobs (4 total)" in result.output
        for column in ["Job ID", "Type", "Status", "Progress", "Started", "Username"]:
            assert column in result.output
        for job_id_prefix in ["job-runn", "job-comp", "job-fail", "job-canc"]:
            assert job_id_prefix in result.output

    def test_jobs_list_e2e_with_status_filter(
        self, cli_runner, setup_remote_credentials
    ):
        """Test jobs list with status filtering end-to-end."""
        # Test filtering by running status
        result = cli_runner.invoke(cli, ["jobs", "list", "--status", "running"])

        assert result.exit_code == 0, result.output
        # Only the single "running" job (of the 4 seeded jobs) should be
        # rendered -- proves server-side filtering actually happened, not
        # just that the command didn't crash.
        assert "Background Jobs (1 total)" in result.output
        assert "Filtered by status: running" in result.output
        assert "job-runn" in result.output
        for excluded_prefix in ["job-comp", "job-fail", "job-canc"]:
            assert excluded_prefix not in result.output

    def test_jobs_list_e2e_with_limit_parameter(
        self, cli_runner, setup_remote_credentials
    ):
        """Test jobs list with limit parameter end-to-end."""
        # Test with limit parameter
        result = cli_runner.invoke(cli, ["jobs", "list", "--limit", "2"])

        assert result.exit_code == 0, result.output
        # The "total" reflects all matching jobs regardless of the limit
        # applied to the displayed page (server-side behavior).
        assert "Background Jobs (4 total)" in result.output
        # Exactly 2 rows must be rendered -- one "test-repo-1" Type-column
        # value is emitted per row by _display_jobs_table.
        assert result.output.count("test-repo-1") == 2

    def test_jobs_list_e2e_table_formatting(self, cli_runner, setup_remote_credentials):
        """Test that job table formatting works correctly end-to-end."""
        result = cli_runner.invoke(cli, ["jobs", "list"])

        assert result.exit_code == 0, result.output

        # Real formatted table output -- all columns must be present.
        output = result.output
        assert "Background Jobs (4 total)" in output
        for column in ["Job ID", "Type", "Status", "Progress", "Started", "Username"]:
            assert column in output

    def test_jobs_list_e2e_status_icons(self, cli_runner, setup_remote_credentials):
        """Test that status icons are displayed correctly."""
        result = cli_runner.invoke(cli, ["jobs", "list"])

        assert result.exit_code == 0, result.output

        # The fixture seeds exactly one job per status, so all four status
        # icons must be present in the real rendered output.
        output = result.output
        for icon in ["🔄", "✅", "❌", "⏹️"]:
            assert icon in output

    def test_jobs_list_e2e_error_handling_no_credentials(
        self, cli_runner, temp_project_dir
    ):
        """Test error handling when no credentials are found."""
        with (
            patch("code_indexer.cli.find_project_root") as mock_find_root,
            patch(
                "code_indexer.disabled_commands.find_project_root"
            ) as mock_find_root_disabled,
            patch(
                "code_indexer.remote.config.load_remote_configuration"
            ) as mock_load_remote_config,
            patch(
                "code_indexer.remote.credential_manager.load_encrypted_credentials"
            ) as mock_load_encrypted_creds,
        ):
            mock_find_root.return_value = temp_project_dir
            # Bug #1474: require_mode("remote")'s detect_current_mode() calls
            # disabled_commands.find_project_root independently of cli.py's
            # find_project_root. Without mocking it too, mode detection falls
            # through to the real cwd (this repo, which is in "local" mode),
            # so DisabledCommandError fires before the command body's own
            # credential-loading logic ever runs.
            mock_find_root_disabled.return_value = temp_project_dir
            mock_load_remote_config.return_value = {
                "username": "testuser",
                "server_url": "http://test.example.com",
            }
            mock_load_encrypted_creds.side_effect = FileNotFoundError(
                "No credentials file"
            )

            result = cli_runner.invoke(cli, ["jobs", "list"])

            assert result.exit_code == 1
            assert "Failed to load credentials" in result.output

    def test_jobs_list_e2e_error_handling_no_project(
        self, cli_runner, temp_project_dir
    ):
        """Test error handling when no project configuration is found."""
        with (
            patch("code_indexer.cli.find_project_root") as mock_find_root,
            patch(
                "code_indexer.disabled_commands.find_project_root"
            ) as mock_find_root_disabled,
        ):
            # Bug #1474: require_mode("remote")'s mode detection must resolve
            # to "remote" (via a directory with a valid .remote-config) for
            # the command body to even run -- otherwise DisabledCommandError
            # fires first and this test's target assertion is never reached.
            # cli.py's own find_project_root is separately mocked to None to
            # simulate the "no project configuration" scenario this test
            # actually exercises.
            mock_find_root.return_value = None
            mock_find_root_disabled.return_value = temp_project_dir

            result = cli_runner.invoke(cli, ["jobs", "list"])

            assert result.exit_code == 1
            assert "No project configuration found" in result.output

    def test_jobs_list_e2e_authentication_flow(
        self, cli_runner, setup_remote_credentials
    ):
        """Test that authentication flow works correctly end-to-end."""
        # This test validates that the JobsAPIClient can authenticate
        # and retrieve jobs from the real server
        result = cli_runner.invoke(cli, ["jobs", "list"])

        # Bug #1474: a real successful HTTP round trip is the only proof
        # that authentication actually worked -- exit_code == 0 is the only
        # correct outcome now that the event-loop defect is fixed.
        assert result.exit_code == 0, result.output
        assert "Background Jobs (4 total)" in result.output

    def test_jobs_list_e2e_complete_workflow_validation(
        self, cli_runner, setup_remote_credentials
    ):
        """Test complete workflow validation covering all Story 8 acceptance criteria."""
        # Acceptance Criteria 1: Job Listing Command
        result = cli_runner.invoke(cli, ["jobs", "list"])
        assert result.exit_code == 0, result.output
        assert "Background Jobs (4 total)" in result.output

        # Acceptance Criteria 2: Job Filtering by status
        result = cli_runner.invoke(cli, ["jobs", "list", "--status", "completed"])
        assert result.exit_code == 0, result.output
        assert "completed" in result.output.lower()
        assert "Background Jobs (1 total)" in result.output
        assert "Filtered by status: completed" in result.output

        # Acceptance Criteria 3: Comprehensive Display
        result = cli_runner.invoke(cli, ["jobs", "list"])
        assert result.exit_code == 0, result.output
        required_columns = ["Job ID", "Type", "Status", "Progress", "Started"]
        for column in required_columns:
            assert column in result.output

        # Acceptance Criteria 4: CLI Integration with error handling
        # (already tested in error handling tests above)

        # Acceptance Criteria 5: API Integration
        # (validated by successful execution of above tests with real server)
