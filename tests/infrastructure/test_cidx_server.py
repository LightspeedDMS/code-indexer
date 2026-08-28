"""Real CIDX Server for Testing.

Provides real HTTP server infrastructure to replace all mocks in Foundation #1 compliance.
This server responds to actual HTTP requests with real JWT tokens and authentication flows.
"""

import asyncio
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, cast
from dataclasses import dataclass, asdict

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator

# Bug #1725: reuse the real production password-complexity validator so the
# fake's AdminChangePasswordRequest mirrors server/models/auth.py's model
# exactly (min_length + complexity), instead of drifting from it again.
from code_indexer.server.auth.password_validator import (
    validate_password_complexity,
    get_password_complexity_error_message,
)

# Configure logging to avoid interference with test output
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# JWT Configuration
JWT_ALGORITHM = "RS256"
JWT_EXPIRATION_MINUTES = 10
REFRESH_TOKEN_EXPIRATION_DAYS = 30

# Test user credentials for authentication
TEST_USERS = {
    "testuser": {
        "password": "testpass123",
        "username": "testuser",
        "user_id": "test-user-123",
        "role": "normal_user",
        "created_at": "2024-01-01T00:00:00Z",
    },
    "admin": {
        "password": "admin123",
        "username": "admin",
        "user_id": "admin-user-456",
        "role": "admin",
        "created_at": "2024-01-01T00:00:00Z",
    },
}


@dataclass
class TestRepository:
    """Test repository data structure."""

    id: str
    name: str
    path: str
    branches: List[str]
    default_branch: str
    created_at: datetime
    indexed_at: Optional[datetime] = None
    status: str = "active"


@dataclass
class TestJob:
    """Test job data structure."""

    id: str
    repository_id: str
    status: str
    progress: int
    created_at: datetime
    updated_at: datetime
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class LoginRequest(BaseModel):
    """Login request model."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenRefreshRequest(BaseModel):
    """Token refresh request model."""

    refresh_token: str = Field(..., min_length=1)


class JobCancelRequest(BaseModel):
    """Job cancellation request model."""

    reason: str = Field(default="User requested cancellation")


class QueryRequest(BaseModel):
    """Query request model.

    Bug #1725: field renamed from ``query`` to ``query_text`` to match the
    real production request body (SemanticQueryRequest,
    server/models/query.py) and the real client's actual payload
    (RemoteQueryClient.execute_query() sends "query_text", not "query" --
    see remote_query_client.py). The route existed on both sides pre-#1708;
    only this request-body field name had drifted.
    """

    query_text: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    language: Optional[str] = None
    path_filter: Optional[str] = None


class CreateUserRequest(BaseModel):
    """Create user request model."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)


class UpdateUserRequest(BaseModel):
    """Update user request model."""

    role: str = Field(..., min_length=1)


class AdminChangePasswordRequest(BaseModel):
    """Admin change password request model.

    Bug #1725 (supersedes #1720 Finding 2): mirrors the real production
    model (server/models/auth.py's AdminChangePasswordRequest) exactly --
    ``min_length=1``, ``max_length=1000``, plus the shared complexity
    validator -- so an empty or weak password produces the same HTTP 422
    the real server returns, instead of the fake's previous deliberate 400.

    Deliberately does NOT mirror the real route's ``require_elevation()``
    (TOTP step-up) gating: this fake server has no TOTP/MFA simulation
    infrastructure at all (no TOTP setup flow, no elevation token
    issuance/verification), so replicating that gate here would mean
    building a large, unrelated subsystem to cover a single admin-only
    endpoint. Every existing test against this endpoint only exercises
    role-based (403) and validation (422) failure paths, never elevation --
    so the gap is currently inert. If a future test needs to assert on
    ``elevation_required`` specifically, add that as a deliberate, documented
    fake-server extension at that time rather than assuming it already works
    here.
    """

    new_password: str = Field(..., min_length=1, max_length=1000)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        """Validate new password complexity (mirrors the real model exactly)."""
        if not v or not v.strip():
            raise ValueError("Password cannot be empty or contain only whitespace")
        if not validate_password_complexity(v):
            raise ValueError(get_password_complexity_error_message())
        return v


class TestCIDXServer:
    """Real CIDX server for testing with authentic JWT and HTTP operations.

    This server provides:
    - Real JWT token generation and validation using RSA keys
    - Real HTTP endpoints for authentication, repositories, and queries
    - Real database simulation for repositories and jobs
    - Real network error simulation capabilities
    - Zero mocks - all operations use real implementations
    """

    def __init__(self, port: int = 0):
        """Initialize test server with real RSA key generation.

        Args:
            port: Server port (0 for auto-assignment)
        """
        self.port = port
        self.server_process: Optional[uvicorn.Server] = None
        # Bug #1720 investigation: uvicorn runs on its OWN thread (with its
        # own event loop, via uvicorn.Server.run()) rather than as an
        # asyncio.Task on the test's event loop. Running it on the same loop
        # deterministically deadlocked every synchronous real-network client
        # call (httpx.Client) made from the test coroutine: the blocking
        # socket read holds the only thread able to service the server's
        # request handling, so the client's request never gets answered
        # until its own read-timeout fires.
        self._server_thread: Optional[threading.Thread] = None
        self.actual_port: Optional[int] = None
        self.base_url: Optional[str] = None

        # Generate real RSA key pair for JWT signing
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self.public_key = self.private_key.public_key()

        # Convert keys to PEM format for JWT operations
        self.private_key_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public_key_pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        # Real data storage (simulates database)
        self.repositories: Dict[str, TestRepository] = {}
        self.jobs: Dict[str, TestJob] = {}
        self.active_tokens: Dict[str, Dict[str, Any]] = {}
        self.refresh_tokens: Dict[str, Dict[str, Any]] = {}
        # Initialize with test users for admin operations
        self.users: Dict[str, Dict[str, Any]] = dict(TEST_USERS)
        # Bug #1725: golden repository store backing the admin golden-repos
        # maintenance routes (list/refresh) added below.
        self.golden_repos: Dict[str, Dict[str, Any]] = {}

        # Server configuration
        self.app = self._create_app()
        self.security = HTTPBearer()

        # Error simulation capabilities
        self.should_simulate_network_error = False
        self.should_simulate_server_error = False
        self.should_simulate_timeout = False
        self.error_endpoints: List[str] = []

    def _create_app(self) -> FastAPI:
        """Create FastAPI application with real endpoints."""
        app = FastAPI(title="Test CIDX Server", version="1.0.0")

        # Authentication endpoints
        app.post("/auth/login")(self._login)
        app.post("/auth/refresh")(self._refresh_token)

        # Repository endpoints
        # NOTE: real production server registers this list endpoint at
        # "/api/repos" (see RemoteQueryClient.list_repositories()), not
        # "/api/repositories" -- kept aligned with the real route (Bug #1708).
        app.get("/api/repos")(self._list_repositories)
        app.get("/api/repositories/{repo_id}")(self._get_repository)
        app.post("/api/repositories/{repo_id}/sync")(self._sync_repository)

        # Job management endpoints
        app.get("/api/jobs")(self._list_jobs)
        # Matches the real production server's only job-status route (see
        # inline_jobs.py: "GET /api/jobs/{job_id}", no "/status" suffix).
        # base_client.py's get_job_status() was fixed to call this exact URL
        # (Bug #1720 Finding 1); this fake route was renamed to match.
        app.get("/api/jobs/{job_id}")(self._get_job_status)
        app.delete("/api/jobs/{job_id}")(self._cancel_job)

        # Query endpoints
        app.post("/api/query")(self._query_code)

        # Admin endpoints - Foundation #1 compliant (no mocking, real implementation)
        app.post("/api/admin/users", status_code=201)(self._create_user)
        app.get("/api/admin/users")(self._list_users)
        app.put("/api/admin/users/{username}")(self._update_user)
        app.delete("/api/admin/users/{username}")(self._delete_user)
        # Matches the real production route registered in
        # inline_admin_users.py: "PUT /api/admin/users/{username}/change-password"
        # (Bug #1720 Finding 2 -- added to replace the pre-#1708 fictional
        # "POST /api/admin/users/{username}/password" route that had no real
        # production counterpart).
        app.put("/api/admin/users/{username}/change-password")(
            self._change_user_password
        )

        # Golden repository maintenance endpoints (Bug #1725). Matches the
        # real production routes registered in inline_admin_ops.py:
        # "GET /api/admin/golden-repos" and
        # "POST /api/admin/golden-repos/{alias}/refresh" (202). These were
        # entirely absent from the fake, so any golden-repos-maintenance
        # request against it 404'd immediately.
        app.get("/api/admin/golden-repos")(self._list_golden_repos)
        app.post(
            "/api/admin/golden-repos/{alias}/refresh",
            status_code=202,
        )(self._refresh_golden_repo)

        # Health endpoint
        app.get("/health")(self._health_check)

        return app

    def _find_available_port(self) -> int:
        """Find an available port for the server."""
        if self.port != 0:
            return self.port

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            s.listen(1)
            port = cast(int, s.getsockname()[1])
        return port

    async def start(self) -> str:
        """Start the test server and return base URL.

        Returns:
            Base URL of the started server
        """
        if self.server_process is not None:
            return self.base_url  # type: ignore[return-value]

        self.actual_port = self._find_available_port()
        self.base_url = f"http://localhost:{self.actual_port}"

        # Configure uvicorn server
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.actual_port,
            log_level="warning",
            access_log=False,
        )

        self.server_process = uvicorn.Server(config)

        # Start server on its own thread with its own event loop (Bug #1720
        # investigation fix -- see __init__'s comment on _server_thread for
        # why this must NOT be asyncio.create_task() on the test's loop).
        # uvicorn.Server.run() internally does asyncio.run(self.serve()),
        # giving this thread a fresh event loop independent of the caller's.
        self._server_thread = threading.Thread(
            target=self.server_process.run, daemon=True
        )
        self._server_thread.start()

        # Wait for server to be ready with timeout
        max_wait_time = 5.0
        start_time = time.time()

        while not self._is_server_ready() and time.time() - start_time < max_wait_time:
            await asyncio.sleep(0.1)

        if not self._is_server_ready():
            raise RuntimeError(f"Server failed to start within {max_wait_time} seconds")

        return self.base_url

    def _is_server_ready(self) -> bool:
        """Check if server is ready to accept connections."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                result = s.connect_ex(("127.0.0.1", self.actual_port))
                return result == 0
        except Exception:
            return False

    async def stop(self):
        """Stop the test server and cleanup resources."""
        if self.server_process is not None:
            # Graceful shutdown
            self.server_process.should_exit = True

            # Wait for shutdown with timeout
            max_wait = 3.0
            start_time = time.time()

            while self.server_process.started and time.time() - start_time < max_wait:
                await asyncio.sleep(0.1)

            if self._server_thread is not None:
                # Join off the event loop thread so a slow shutdown never
                # blocks this coroutine's own loop.
                await asyncio.to_thread(self._server_thread.join, 3.0)
                if self._server_thread.is_alive():
                    logging.getLogger(__name__).warning(
                        "TestCIDXServer: server thread did not stop within "
                        "3.0s shutdown timeout -- leaking thread"
                    )
                self._server_thread = None

            self.server_process = None

        # Clean up sensitive data
        self.active_tokens.clear()
        self.refresh_tokens.clear()

    def add_test_repository(
        self,
        repo_id: str,
        name: str,
        path: str,
        branches: List[str],
        default_branch: str = "main",
    ) -> TestRepository:
        """Add a test repository to the server.

        Args:
            repo_id: Unique repository ID
            name: Repository name
            path: Repository path
            branches: List of available branches
            default_branch: Default branch name

        Returns:
            Created test repository
        """
        repo = TestRepository(
            id=repo_id,
            name=name,
            path=path,
            branches=branches,
            default_branch=default_branch,
            created_at=datetime.now(timezone.utc),
        )
        self.repositories[repo_id] = repo
        return repo

    def add_test_job(
        self,
        job_id: str,
        repository_id: str,
        job_status: str = "pending",
        progress: int = 0,
    ) -> TestJob:
        """Add a test job to the server.

        Args:
            job_id: Unique job ID
            repository_id: Associated repository ID
            job_status: Job status
            progress: Job progress percentage

        Returns:
            Created test job
        """
        job = TestJob(
            id=job_id,
            repository_id=repository_id,
            status=job_status,
            progress=progress,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.jobs[job_id] = job
        return job

    def update_job_status(
        self,
        job_id: str,
        job_status: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        """Update job status in the server.

        Args:
            job_id: Job ID to update
            job_status: New status
            progress: New progress percentage
            result: Job result data
            error: Error message if job failed
        """
        if job_id in self.jobs:
            job = self.jobs[job_id]
            job.status = job_status
            job.updated_at = datetime.now(timezone.utc)
            if progress is not None:
                job.progress = progress
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error

    def set_error_simulation(self, endpoint: str, error_type: str):
        """Configure error simulation for specific endpoints.

        Args:
            endpoint: API endpoint to simulate errors for
            error_type: Type of error ('network', 'server', 'timeout')
        """
        if error_type == "network":
            self.should_simulate_network_error = True
        elif error_type == "server":
            self.should_simulate_server_error = True
        elif error_type == "timeout":
            self.should_simulate_timeout = True

        if endpoint not in self.error_endpoints:
            self.error_endpoints.append(endpoint)

    def clear_error_simulation(self):
        """Clear all error simulation settings."""
        self.should_simulate_network_error = False
        self.should_simulate_server_error = False
        self.should_simulate_timeout = False
        self.error_endpoints.clear()

    def _generate_jwt_token(self, user_data: Dict[str, Any]) -> str:
        """Generate real JWT token using RSA private key.

        Args:
            user_data: User data to include in token

        Returns:
            Signed JWT token
        """
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_data["user_id"],
            "username": user_data["username"],
            "iat": now,
            "exp": now + timedelta(minutes=JWT_EXPIRATION_MINUTES),
            "type": "access",
        }

        token = jwt.encode(payload, self.private_key_pem, algorithm=JWT_ALGORITHM)

        # Store active token
        self.active_tokens[token] = {
            "user_data": user_data,
            "created_at": now,
            "expires_at": payload["exp"],
        }

        return token

    def _generate_refresh_token(self, user_data: Dict[str, Any]) -> str:
        """Generate real refresh token.

        Args:
            user_data: User data to include in token

        Returns:
            Signed refresh token
        """
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_data["user_id"],
            "username": user_data["username"],
            "iat": now,
            "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRATION_DAYS),
            "type": "refresh",
        }

        token = jwt.encode(payload, self.private_key_pem, algorithm=JWT_ALGORITHM)

        # Store refresh token
        self.refresh_tokens[token] = {
            "user_data": user_data,
            "created_at": now,
            "expires_at": payload["exp"],
        }

        return token

    def _verify_jwt_token(self, token: str) -> Dict[str, Any]:
        """Verify and decode JWT token using RSA public key.

        Args:
            token: JWT token to verify

        Returns:
            Decoded token payload

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            payload = jwt.decode(token, self.public_key_pem, algorithms=[JWT_ALGORITHM])

            # Verify token is in active tokens list
            if token not in self.active_tokens:
                raise HTTPException(
                    status_code=401, detail="Token not found in active tokens"
                )

            return cast(Dict[str, Any], payload)
        except jwt.ExpiredSignatureError:
            # Remove expired token from active tokens
            self.active_tokens.pop(token, None)
            raise HTTPException(status_code=401, detail="Token has expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    # API Endpoints

    async def _login(self, login_request: LoginRequest):
        """Authenticate user and return JWT tokens.

        Args:
            login_request: Login credentials

        Returns:
            JWT access and refresh tokens
        """
        username = login_request.username
        password = login_request.password

        # Check users (including dynamically created ones)
        if username not in self.users or self.users[username]["password"] != password:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        user_data = self.users[username]

        # Generate real tokens
        access_token = self._generate_jwt_token(user_data)
        refresh_token = self._generate_refresh_token(user_data)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": JWT_EXPIRATION_MINUTES * 60,
        }

    async def _refresh_token(self, refresh_request: TokenRefreshRequest):
        """Refresh JWT access token using refresh token.

        Args:
            refresh_request: Refresh token request

        Returns:
            New access token
        """
        refresh_token = refresh_request.refresh_token

        if refresh_token not in self.refresh_tokens:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        token_data = self.refresh_tokens[refresh_token]
        user_data = token_data["user_data"]

        # Generate new access token
        new_access_token = self._generate_jwt_token(user_data)

        return {
            "access_token": new_access_token,
            "token_type": "bearer",
            "expires_in": JWT_EXPIRATION_MINUTES * 60,
        }

    async def _list_repositories(self, user=Depends(lambda: None)):
        """List all repositories.

        Returns:
            List of repositories
        """
        return {
            "repositories": [asdict(repo) for repo in self.repositories.values()],
            "total": len(self.repositories),
        }

    async def _get_repository(self, repo_id: str, user=Depends(lambda: None)):
        """Get repository by ID.

        Args:
            repo_id: Repository ID

        Returns:
            Repository data
        """
        if repo_id not in self.repositories:
            raise HTTPException(status_code=404, detail="Repository not found")

        return asdict(self.repositories[repo_id])

    async def _sync_repository(self, repo_id: str, user=Depends(lambda: None)):
        """Start repository synchronization.

        Args:
            repo_id: Repository ID

        Returns:
            Sync job data
        """
        if repo_id not in self.repositories:
            raise HTTPException(status_code=404, detail="Repository not found")

        # Create sync job
        job_id = f"sync-{repo_id}-{int(time.time())}"
        self.add_test_job(job_id, repo_id, "running", 0)

        return {
            "job_id": job_id,
            "status": "started",
            "repository_id": repo_id,
        }

    async def _list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
        user=Depends(lambda: None),
    ):
        """List jobs with filtering and pagination.
        Args:
            status: Filter by job status (optional)
            limit: Maximum number of jobs to return
            offset: Number of jobs to skip for pagination
        Returns:
            Job list response
        """
        # Get all jobs
        all_jobs = list(self.jobs.values())

        # Filter by status if specified
        if status:
            all_jobs = [job for job in all_jobs if job.status == status]

        # Sort by created_at descending (newest first)
        all_jobs.sort(key=lambda x: x.created_at, reverse=True)

        # Apply pagination
        total = len(all_jobs)
        paginated_jobs = all_jobs[offset : offset + limit]

        # Convert jobs to dictionary format
        jobs_data = []
        for job in paginated_jobs:
            job_dict = asdict(job)
            job_dict["created_at"] = job.created_at.isoformat()
            job_dict["updated_at"] = job.updated_at.isoformat()
            # Add fields expected by API specification
            job_dict["job_id"] = job_dict["id"]
            job_dict["operation_type"] = f"operation_{job.repository_id}"
            job_dict["started_at"] = job.created_at.isoformat()
            job_dict["completed_at"] = (
                job.updated_at.isoformat()
                if job.status in ["completed", "failed", "cancelled"]
                else None
            )
            job_dict["username"] = "testuser"  # Default test user
            jobs_data.append(job_dict)

        return {
            "jobs": jobs_data,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def _get_job_status(self, job_id: str, user=Depends(lambda: None)):
        """Get job status by ID.

        Args:
            job_id: Job ID

        Returns:
            Job status data
        """
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = self.jobs[job_id]
        job_dict = asdict(job)

        # Convert datetime objects to strings
        job_dict["created_at"] = job.created_at.isoformat()
        job_dict["updated_at"] = job.updated_at.isoformat()

        return job_dict

    async def _cancel_job(self, job_id: str, user=Depends(lambda: None)):
        """Cancel job by ID.

        Args:
            job_id: Job ID

        Returns:
            Cancellation confirmation
        """
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="Job not found")

        job = self.jobs[job_id]

        # Check if job can be cancelled
        if job.status in ["completed", "failed", "cancelled"]:
            raise HTTPException(
                status_code=409,
                detail=f"Job cannot be cancelled - current status: {job.status}",
            )

        # Cancel the job
        self.update_job_status(job_id, "cancelled", error="User requested cancellation")

        return {
            "message": f"Job {job_id} cancelled successfully",
            "id": job_id,
            "status": "cancelled",
        }

    async def _query_code(
        self, query_request: QueryRequest, user=Depends(lambda: None)
    ):
        """Execute semantic code query.

        Args:
            query_request: Query parameters

        Returns:
            Query results
        """
        # Check for error simulation on /api/query endpoint
        if "/api/query" in self.error_endpoints and self.should_simulate_server_error:
            raise HTTPException(status_code=500, detail="Simulated server error")

        # Simulate query results with correct QueryResultItem schema
        mock_results = [
            {
                "file_path": "/src/main.py",
                "line_number": 42,
                "code_snippet": f"def example_function(): # matches '{query_request.query_text}'",
                "similarity_score": 0.95,
                "repository_alias": "default",
                "file_last_modified": None,
                "indexed_timestamp": None,
            },
            {
                "file_path": "/src/utils.py",
                "line_number": 15,
                "code_snippet": f"class ExampleClass: # related to '{query_request.query_text}'",
                "similarity_score": 0.78,
                "repository_alias": "default",
                "file_last_modified": None,
                "indexed_timestamp": None,
            },
        ]

        # Filter by minimum score
        filtered_results = [
            r
            for r in mock_results
            if r["similarity_score"] >= query_request.min_score  # type: ignore[operator]
        ]

        # Apply limit
        limited_results = filtered_results[: query_request.limit]

        return {
            "results": limited_results,
            "total": len(limited_results),
            "query_text": query_request.query_text,
        }

    def _require_admin_user(self, user_data: Dict[str, Any]) -> None:
        """Check if current user has admin privileges.

        Args:
            user_data: Current user data from token

        Raises:
            HTTPException: If user is not admin
        """
        if user_data.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin privileges required")

    def _authenticate_admin_user(
        self, credentials: HTTPAuthorizationCredentials
    ) -> Dict[str, Any]:
        """Verify the bearer token and enforce admin role in one call.

        Shared by the golden-repos maintenance handlers (Bug #1725) to
        avoid re-inlining the verify-token/lookup-user/require-admin
        sequence used throughout this file.

        Returns:
            The current user's data dict.

        Raises:
            HTTPException: If the token is invalid or the user isn't admin.
        """
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)
        return cast(Dict[str, Any], current_user)

    # Admin User Management Endpoints

    async def _create_user(
        self,
        user_request: CreateUserRequest,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """Create new user (admin only).

        Args:
            user_request: User creation data
            credentials: JWT token credentials
        """
        # Verify JWT token and get current user
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)

        username = user_request.username
        if username in self.users:
            # Bug #1725: real production returns 400 (not 409) here -- see
            # inline_admin_users.py's create_user route, which converts
            # UserManager.create_user()'s ValueError("User already exists:
            # {username}") into HTTPException(400, str(e)). The fake's prior
            # 409 + differently-worded detail was fictional.
            raise HTTPException(
                status_code=400, detail=f"User already exists: {username}"
            )

        # Validate role
        valid_roles = ["admin", "power_user", "normal_user"]
        if user_request.role not in valid_roles:
            raise HTTPException(
                status_code=400, detail=f"Invalid role. Must be one of: {valid_roles}"
            )

        # Create new user
        new_user = {
            "username": username,
            "password": user_request.password,
            "user_id": f"user-{len(self.users)}-{int(time.time())}",
            "role": user_request.role,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        self.users[username] = new_user

        # Return user info without password
        user_response = {
            "username": new_user["username"],
            "user_id": new_user["user_id"],
            "role": new_user["role"],
            "created_at": new_user["created_at"],
        }

        return {"user": user_response}

    async def _list_users(
        self,
        limit: int = 10,
        offset: int = 0,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """List all users (admin only).

        Args:
            limit: Maximum number of users to return
            offset: Number of users to skip for pagination
            credentials: JWT token credentials
        """
        # Verify JWT token and get current user
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)

        # Get all users without passwords
        all_users = []
        for user_data in self.users.values():
            user_info = {
                "username": user_data["username"],
                "user_id": user_data["user_id"],
                "role": user_data["role"],
                "created_at": user_data["created_at"],
            }
            all_users.append(user_info)

        # Apply pagination
        total = len(all_users)
        paginated_users = all_users[offset : offset + limit]

        return {
            "users": paginated_users,
            "total": total,
        }

    async def _update_user(
        self,
        username: str,
        update_request: UpdateUserRequest,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """Update user role (admin only).

        Args:
            username: Username to update
            update_request: Update data
            credentials: JWT token credentials
        """
        # Verify JWT token and get current user
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)

        if username not in self.users:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")

        # Validate role
        valid_roles = ["admin", "power_user", "normal_user"]
        if update_request.role not in valid_roles:
            raise HTTPException(
                status_code=400, detail=f"Invalid role. Must be one of: {valid_roles}"
            )

        # Update user role
        self.users[username]["role"] = update_request.role

        return {"message": f"User '{username}' updated successfully"}

    async def _delete_user(
        self,
        username: str,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """Delete user (admin only).

        Args:
            username: Username to delete
            credentials: JWT token credentials
        """
        # Verify JWT token and get current user
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)

        if username not in self.users:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")

        user_to_delete = self.users[username]

        # Prevent deletion of last admin
        if user_to_delete["role"] == "admin":
            admin_count = sum(1 for u in self.users.values() if u["role"] == "admin")
            if admin_count <= 1:
                raise HTTPException(
                    status_code=400, detail="Cannot delete the last admin user"
                )

        # Delete user
        del self.users[username]

        return {"message": f"User '{username}' deleted successfully"}

    async def _change_user_password(
        self,
        username: str,
        password_request: AdminChangePasswordRequest,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """Change a user's password (admin only).

        Args:
            username: Username whose password to change
            password_request: New password data
            credentials: JWT token credentials
        """
        # Verify JWT token and get current user
        self._verify_jwt_token(credentials.credentials)
        current_user = self.active_tokens[credentials.credentials]["user_data"]
        self._require_admin_user(current_user)

        if username not in self.users:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")

        # Note: an empty/weak new_password never reaches this point -- the
        # AdminChangePasswordRequest model's min_length=1 + complexity
        # field_validator (Bug #1725) rejects it at request-validation time
        # (HTTP 422) before the handler body runs.

        # Update the user's password
        self.users[username]["password"] = password_request.new_password

        return {"message": f"Password changed successfully for user '{username}'"}

    async def _list_golden_repos(
        self,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """List all golden repositories (admin only).

        Mirrors the real production route: GET /api/admin/golden-repos
        (see inline_admin_ops.py's list_golden_repos). Bug #1725.
        """
        self._authenticate_admin_user(credentials)

        repos = list(self.golden_repos.values())
        return {
            "golden_repositories": repos,
            "total": len(repos),
        }

    async def _refresh_golden_repo(
        self,
        alias: str,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ):
        """Refresh a golden repository (admin only) -- async job stub.

        Mirrors the real production route:
        POST /api/admin/golden-repos/{alias}/refresh (see
        inline_admin_ops.py's refresh_golden_repo). Bug #1725.
        """
        self._authenticate_admin_user(credentials)

        if alias not in self.golden_repos:
            raise HTTPException(
                status_code=404, detail=f"Golden repository '{alias}' not found"
            )

        job_id = f"refresh-{alias}-{int(time.time())}"
        self.add_test_job(job_id, alias, "pending", 0)

        return {
            "job_id": job_id,
            "message": f"Golden repository '{alias}' refresh started",
        }

    async def _health_check(self):
        """Health check endpoint.

        Returns:
            Health status
        """
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repositories": len(self.repositories),
            "active_jobs": len(
                [j for j in self.jobs.values() if j.status == "running"]
            ),
        }


# Test helper functions


def create_test_server(port: int = 0) -> TestCIDXServer:
    """Create a new test CIDX server instance.

    Args:
        port: Server port (0 for auto-assignment)

    Returns:
        TestCIDXServer instance
    """
    return TestCIDXServer(port=port)


async def start_test_server(server: TestCIDXServer) -> str:
    """Start test server and return base URL.

    Args:
        server: TestCIDXServer instance

    Returns:
        Base URL of started server
    """
    return await server.start()


async def stop_test_server(server: TestCIDXServer):
    """Stop test server and cleanup resources.

    Args:
        server: TestCIDXServer instance
    """
    await server.stop()


# Context manager for test server lifecycle
class CIDXServerTestContext:
    """Context manager for test CIDX server lifecycle."""

    def __init__(self, port: int = 0):
        """Initialize context manager.

        Args:
            port: Server port (0 for auto-assignment)
        """
        self.port = port
        self.server: Optional[TestCIDXServer] = None
        self.base_url: Optional[str] = None

    async def __aenter__(self) -> TestCIDXServer:
        """Start server and return instance."""
        self.server = create_test_server(self.port)
        self.base_url = await start_test_server(self.server)
        return self.server

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop server and cleanup."""
        if self.server:
            await stop_test_server(self.server)
