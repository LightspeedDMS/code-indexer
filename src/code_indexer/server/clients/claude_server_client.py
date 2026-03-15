"""
Claude Server API Client.

Story #719: Execute Delegation Function with Async Job

Provides async HTTP client for communicating with Claude Server
for delegated job execution.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class ClaudeServerError(Exception):
    """Base exception for Claude Server client errors."""

    pass


class ClaudeServerAuthError(ClaudeServerError):
    """Raised when authentication to Claude Server fails."""

    pass


class ClaudeServerNotFoundError(ClaudeServerError):
    """Raised when a resource is not found (404)."""

    pass


class ClaudeServerClient:
    """
    Async client for Claude Server API communication.

    Handles authentication, JWT token management, repository operations,
    and job creation/management for delegation function execution.
    """

    def __init__(
        self, base_url: str, username: str, password: str, skip_ssl_verify: bool = False
    ):
        """
        Initialize the Claude Server client.

        Args:
            base_url: Base URL of the Claude Server (e.g., https://claude.example.com)
            username: Username for authentication
            password: Decrypted password/credential for authentication
            skip_ssl_verify: If True, skip SSL certificate verification (for E2E testing)
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.skip_ssl_verify = skip_ssl_verify
        self._jwt_token: Optional[str] = None
        self._jwt_expires: Optional[datetime] = None

        # Story #732: HTTP connection pooling
        # Single shared client for all requests, enabling connection reuse
        self._client = httpx.AsyncClient(
            verify=not skip_ssl_verify,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )

    def __repr__(self) -> str:
        """Prevent accidental credential exposure in logs and debugging output."""
        return f"ClaudeServerClient(base_url={self.base_url!r}, username={self.username!r})"

    async def authenticate(self) -> str:
        """
        Authenticate with Claude Server and obtain JWT token.

        Returns:
            JWT access token string

        Raises:
            ClaudeServerAuthError: If authentication fails
            ClaudeServerError: If connection fails
        """
        login_url = f"{self.base_url}/auth/login"

        try:
            # Story #732: Use shared client for connection pooling
            # Timeout is configured at client level in __init__ (30s total, 10s connect)
            response = await self._client.post(
                login_url,
                json={"username": self.username, "password": self.password},
            )

            if response.status_code == 200:
                data = response.json()
                # Support both Claude Server ("token") and standard OAuth ("access_token")
                self._jwt_token = data.get("token") or data.get("access_token")
                if not self._jwt_token:
                    raise ClaudeServerError("No token in authentication response")
                # Calculate expiration time
                # Claude Server returns "expires" (ISO datetime), standard returns "expires_in" (seconds)
                if "expires" in data:
                    from dateutil.parser import parse as parse_datetime  # type: ignore[import-untyped]

                    self._jwt_expires = parse_datetime(data["expires"])
                else:
                    expires_in = data.get("expires_in", 3600)
                    self._jwt_expires = datetime.now(timezone.utc) + timedelta(
                        seconds=expires_in
                    )
                return self._jwt_token
            elif response.status_code == 401:
                raise ClaudeServerAuthError(
                    f"Authentication failed: {response.status_code}"
                )
            else:
                raise ClaudeServerError(
                    f"Authentication error: HTTP {response.status_code}"
                )

        except httpx.ConnectError as e:
            raise ClaudeServerError(f"Connection error to Claude Server: {e}")
        except httpx.TimeoutException as e:
            raise ClaudeServerError(f"Connection timeout to Claude Server: {e}")

    async def ensure_authenticated(self) -> str:
        """
        Return valid JWT token, refreshing if needed.

        Returns:
            Valid JWT access token

        Raises:
            ClaudeServerAuthError: If authentication fails
            ClaudeServerError: If connection fails
        """
        if self._jwt_token and self._jwt_expires:
            # Check if token is still valid (with 60s buffer)
            if datetime.now(timezone.utc) < self._jwt_expires - timedelta(seconds=60):
                return self._jwt_token

        # Token expired or not set, authenticate
        return await self.authenticate()

    async def _make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        retry_on_401: bool = True,
    ) -> httpx.Response:
        """
        Make an authenticated request to Claude Server.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without base URL)
            json_data: Optional JSON body data
            retry_on_401: Whether to retry on 401 (default True)

        Returns:
            httpx Response object

        Raises:
            ClaudeServerError: On connection or server errors
        """
        token = await self.ensure_authenticated()
        url = f"{self.base_url}{endpoint}"

        try:
            # Story #732: Use shared client for connection pooling
            # Timeout is configured at client level in __init__ (30s total, 10s connect)
            headers = {"Authorization": f"Bearer {token}"}

            if method.upper() == "GET":
                response = await self._client.get(url, headers=headers)
            elif method.upper() == "POST":
                response = await self._client.post(url, headers=headers, json=json_data)
            else:
                raise ClaudeServerError(f"Unsupported HTTP method: {method}")

            # Handle 401 with retry
            if response.status_code == 401 and retry_on_401:
                # Clear token and re-authenticate
                self._jwt_token = None
                self._jwt_expires = None
                return await self._make_authenticated_request(
                    method, endpoint, json_data, retry_on_401=False
                )
            elif response.status_code == 401 and not retry_on_401:
                # Second 401 means auth truly failed - raise exception
                raise ClaudeServerAuthError("Authentication failed after token refresh")

            return response

        except httpx.ConnectError as e:
            raise ClaudeServerError(f"Connection error to Claude Server: {e}")
        except httpx.TimeoutException as e:
            raise ClaudeServerError(f"Connection timeout to Claude Server: {e}")

    async def check_repository_exists(self, alias: str) -> bool:
        """
        Check if a repository is registered in Claude Server.

        Args:
            alias: Repository alias to check

        Returns:
            True if repository exists, False otherwise
        """
        response = await self._make_authenticated_request(
            "GET", f"/repositories/{alias}"
        )
        return response.status_code == 200  # type: ignore[no-any-return]

    async def register_repository(
        self, alias: str, remote: str, branch: str
    ) -> Dict[str, Any]:
        """
        Register a repository with Claude Server.

        Args:
            alias: Unique alias for the repository
            remote: Git remote URL
            branch: Default branch name

        Returns:
            Dictionary with registration result

        Raises:
            ClaudeServerError: On registration failure
        """
        response = await self._make_authenticated_request(
            "POST",
            "/repositories/register",
            json_data={"name": alias, "gitUrl": remote, "branch": branch},
        )

        if response.status_code in (200, 201):
            return response.json()  # type: ignore[no-any-return]
        else:
            raise ClaudeServerError(
                f"Repository registration failed: HTTP {response.status_code}"
            )

    async def create_job(
        self, prompt: str, repositories: List[str], model: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new job with the given prompt.

        Args:
            prompt: The rendered prompt for the job
            repositories: List of repository aliases to use
            model: Optional Claude model to use (opus or sonnet) - Story #76 AC6

        Returns:
            Dictionary with job info including job_id

        Raises:
            ClaudeServerError: On job creation failure
        """
        # Claude Server expects capitalized field names
        json_data: Dict[str, Any] = {"prompt": prompt, "Repositories": repositories}

        # Story #76 AC6: Include Model field if provided
        if model:
            json_data["Model"] = model

        response = await self._make_authenticated_request(
            "POST",
            "/jobs",
            json_data=json_data,
        )

        if response.status_code in (200, 201):
            return response.json()  # type: ignore[no-any-return]
        elif response.status_code >= 500:
            raise ClaudeServerError(f"Claude Server error: HTTP {response.status_code}")
        else:
            raise ClaudeServerError(f"Job creation failed: HTTP {response.status_code}")

    async def start_job(self, job_id: str) -> Dict[str, Any]:
        """
        Start execution of a created job.

        Args:
            job_id: The ID of the job to start

        Returns:
            Dictionary with updated job status

        Raises:
            ClaudeServerError: On job start failure
        """
        response = await self._make_authenticated_request(
            "POST", f"/jobs/{job_id}/start"
        )

        if response.status_code in (200, 201):
            return response.json()  # type: ignore[no-any-return]
        else:
            raise ClaudeServerError(f"Job start failed: HTTP {response.status_code}")

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get current job status from Claude Server.

        Args:
            job_id: The ID of the job to check

        Returns:
            Dictionary with job status and progress info

        Raises:
            ClaudeServerNotFoundError: If job not found (404)
            ClaudeServerError: If server error
        """
        response = await self._make_authenticated_request("GET", f"/jobs/{job_id}")

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        elif response.status_code == 404:
            raise ClaudeServerNotFoundError(f"Job not found: {job_id}")
        else:
            raise ClaudeServerError(
                f"Failed to get job status: HTTP {response.status_code}"
            )

    async def get_job_conversation(self, job_id: str) -> Dict[str, Any]:
        """
        Get job conversation/result from Claude Server.

        Args:
            job_id: The ID of the job

        Returns:
            Dictionary with job result and conversation exchanges

        Raises:
            ClaudeServerNotFoundError: If job not found (404)
            ClaudeServerError: If server error
        """
        response = await self._make_authenticated_request(
            "GET", f"/jobs/{job_id}/conversation"
        )

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        elif response.status_code == 404:
            raise ClaudeServerNotFoundError(f"Job not found: {job_id}")
        else:
            raise ClaudeServerError(
                f"Failed to get job conversation: HTTP {response.status_code}"
            )

    async def get_repo_status(self, alias: str) -> Dict[str, Any]:
        """
        Get repository status from Claude Server.

        Story #456: Open-ended delegation with engine and mode selection

        Args:
            alias: Repository alias to check

        Returns:
            Dictionary with repository info including cloneStatus field.
            cloneStatus values: "unknown", "cloning", "success", "completed", "failed"
            Note: production Claude Server uses "completed" for ready repos;
            "success" is also accepted for backward compatibility.

        Raises:
            ClaudeServerNotFoundError: If repository not found (404)
            ClaudeServerError: On server errors
        """
        response = await self._make_authenticated_request(
            "GET", f"/repositories/{alias}"
        )

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        elif response.status_code == 404:
            raise ClaudeServerNotFoundError(f"Repository not found: {alias}")
        else:
            raise ClaudeServerError(
                f"Failed to get repository status: HTTP {response.status_code}"
            )

    async def wait_for_repo_ready(
        self,
        alias: str,
        timeout: float = 300.0,
        git_url: Optional[str] = None,
        branch: str = "main",
        poll_interval: float = 2.0,
    ) -> bool:
        """
        Wait for a repository to become ready on Claude Server.

        Story #456: Open-ended delegation with engine and mode selection

        Checks if the repository is registered with cloneStatus="success" or "completed".
        If not registered (404), registers it via POST /repositories/register.
        Polls until cloneStatus is ready, "failed", or timeout expires.

        Note: production Claude Server uses "completed" to indicate a ready repo.
        "success" is also accepted for backward compatibility with older versions.

        Args:
            alias: Repository alias to check
            timeout: Maximum seconds to wait (default 300s / 5 minutes)
            git_url: Git URL for registration if repo not found (optional)
            branch: Branch name for registration (default "main")
            poll_interval: Seconds between polling attempts (default 2.0)

        Returns:
            True if repository is ready (cloneStatus="success" or "completed")
            False if timeout expired or cloneStatus="failed"
        """
        start_time = time.monotonic()

        # Check initial status
        try:
            status_data = await self.get_repo_status(alias)
            clone_status = status_data.get("cloneStatus", "unknown")

            if clone_status in ("success", "completed"):
                return True
            elif clone_status == "failed":
                return False
            # cloneStatus is "cloning" or "unknown" - fall through to polling

        except ClaudeServerNotFoundError:
            # Repository not found - register if we have a git_url
            if git_url:
                try:
                    await self.register_repository(alias, git_url, branch)
                except ClaudeServerError as e:
                    # Check if it's a 409 conflict (already exists - race condition)
                    if "409" in str(e):
                        # Someone else registered it, just poll
                        pass
                    else:
                        raise
            # Fall through to polling regardless (either we registered or someone else did)

        # Poll until ready, failed, or timeout
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                return False

            await asyncio.sleep(min(poll_interval, timeout - elapsed))

            # Check again after sleeping
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                return False

            try:
                status_data = await self.get_repo_status(alias)
                clone_status = status_data.get("cloneStatus", "unknown")

                if clone_status in ("success", "completed"):
                    return True
                elif clone_status == "failed":
                    return False
                # Still cloning/unknown - continue polling

            except ClaudeServerNotFoundError:
                # Still not registered - continue polling
                pass

    async def create_job_with_options(
        self,
        prompt: str,
        repositories: List[str],
        engine: str = "claude-code",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        mcp_servers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new job with engine and mode options.

        Story #456: Open-ended delegation with engine and mode selection

        Separate from existing create_job() for backward compatibility.
        Supports engine, model, timeout, mcp_servers in JobOptionsDto.

        Args:
            prompt: The prompt for the job
            repositories: List of repository aliases
            engine: Agent engine to use (claude-code, codex, gemini, opencode, q)
            model: Optional Claude model to use
            timeout: Optional timeout in seconds (default 5400 / 90 min on server)
            mcp_servers: Optional list of MCP server names

        Returns:
            Dictionary with job info including jobId

        Raises:
            ClaudeServerError: On job creation failure
        """
        # Build Options object per Claude Server API contract
        options: Dict[str, Any] = {"agentEngine": engine}

        if model is not None:
            options["model"] = model
        if timeout is not None:
            options["timeout"] = timeout
        if mcp_servers is not None:
            options["mcpServers"] = mcp_servers

        json_data: Dict[str, Any] = {
            "Prompt": prompt,
            "Repositories": repositories,
            "Options": options,
        }

        response = await self._make_authenticated_request(
            "POST",
            "/jobs",
            json_data=json_data,
        )

        if response.status_code in (200, 201):
            return response.json()  # type: ignore[no-any-return]
        elif response.status_code >= 500:
            raise ClaudeServerError(f"Claude Server error: HTTP {response.status_code}")
        else:
            raise ClaudeServerError(f"Job creation failed: HTTP {response.status_code}")

    async def list_repositories(self) -> List[Dict[str, Any]]:
        """
        List all repositories registered on Claude Server.

        Story #460: Claude Server proxy tools

        Returns:
            List of repository dicts with name, cloneStatus, cidxAware, gitUrl, branch, etc.

        Raises:
            ClaudeServerError: On connection or server errors
        """
        response = await self._make_authenticated_request("GET", "/repositories")

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        else:
            raise ClaudeServerError(
                f"Failed to list repositories: HTTP {response.status_code}"
            )

    async def get_health(self) -> Dict[str, Any]:
        """
        Get Claude Server health status.

        Story #460: Claude Server proxy tools

        The /health endpoint on Claude Server is anonymous (no auth required),
        so we make a direct unauthenticated GET request using the shared client.

        Returns:
            Dict with status, nodeId, version, checks, and metrics fields.

        Raises:
            ClaudeServerError: On connection or server errors
        """
        url = f"{self.base_url}/health"
        try:
            response = await self._client.get(url, timeout=10.0)
            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            else:
                raise ClaudeServerError(
                    f"Health check failed: HTTP {response.status_code}"
                )
        except httpx.ConnectError as e:
            raise ClaudeServerError(f"Connection error to Claude Server: {e}")
        except httpx.TimeoutException as e:
            raise ClaudeServerError(f"Connection timeout to Claude Server: {e}")

    async def register_callback(self, job_id: str, callback_url: str) -> None:
        """
        Register a callback URL with Claude Server for job completion notification.

        Story #720: Callback-Based Delegation Job Completion

        When the job completes (success or failure), Claude Server will POST
        the result to the registered callback URL.

        Args:
            job_id: The ID of the job to register callback for
            callback_url: URL that Claude Server will POST to on completion

        Raises:
            ClaudeServerNotFoundError: If job not found (404)
            ClaudeServerError: If server error
        """
        response = await self._make_authenticated_request(
            "POST",
            f"/jobs/{job_id}/callbacks",
            json_data={"url": callback_url},
        )

        if response.status_code in (200, 201):
            logger.debug(f"Registered callback for job {job_id}: {callback_url}")
            return
        elif response.status_code == 404:
            raise ClaudeServerNotFoundError(f"Job not found: {job_id}")
        else:
            raise ClaudeServerError(
                f"Failed to register callback: HTTP {response.status_code}"
            )

    # Story #732: Connection pool lifecycle management

    async def close(self) -> None:
        """
        Close the HTTP client and release connections.

        Should be called when the client is no longer needed to properly
        clean up connection pool resources. Safe to call multiple times.
        """
        await self._client.aclose()

    async def __aenter__(self) -> "ClaudeServerClient":
        """Support async context manager for automatic cleanup."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close client on context exit."""
        await self.close()
