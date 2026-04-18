"""
Shared pytest fixtures for the CIDX E2E test suite.

Configuration is read exclusively from environment variables.  The
``e2e-automation.sh`` orchestration script sets all required variables
before invoking pytest for each phase.  Developers can also export them
manually when running individual phases during development.

All fixture names are prefixed ``e2e_`` to avoid collisions with
project-level conftest fixtures in parent directories.

Fixture dependency graph:
  e2e_config
      |
      +-- e2e_seed_repo_paths
      +-- e2e_server_url
      +-- e2e_cli_env
      +-- e2e_http_client
      +-- e2e_admin_token  (also needs e2e_server_url)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from tests.e2e.helpers import login


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class E2EConfig:
    """Immutable snapshot of all E2E configuration values.

    All fields are populated from environment variables that
    ``e2e-automation.sh`` sets before invoking pytest.
    """

    server_host: str
    server_port: int
    admin_user: str
    admin_pass: str
    seed_cache_dir: Path
    server_data_dir: Path
    work_dir: Path
    voyage_api_key: str
    golden_repo_job_timeout: float
    """Maximum seconds to wait for a golden repository indexing job (E2E_GOLDEN_REPO_JOB_TIMEOUT)."""
    golden_repo_job_poll_interval: float
    """Seconds between polls when waiting for a golden repo job (E2E_GOLDEN_REPO_JOB_POLL_INTERVAL)."""

    @property
    def server_url(self) -> str:
        """Full base URL for the CIDX server, e.g. http://127.0.0.1:8899."""
        return f"http://{self.server_host}:{self.server_port}"

    @property
    def health_url(self) -> str:
        """Full URL for the server health endpoint."""
        return f"{self.server_url}/health"


def _require_env(name: str) -> str:
    """Return the value of environment variable ``name`` or raise ``RuntimeError``.

    All required configuration must be supplied via environment variables set
    by ``e2e-automation.sh``.  No hardcoded defaults exist in this file;
    defaults live exclusively in the orchestration script.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Run via e2e-automation.sh or export it manually before invoking pytest."
        )
    return value


def _optional_env(name: str) -> str:
    """Return the value of environment variable ``name`` or empty string.

    Used only for genuinely optional settings (e.g. voyage_api_key) where
    absence is valid and individual tests that require the value will fail
    with their own descriptive messages if it is not present.
    """
    return os.environ.get(name, "")


# ---------------------------------------------------------------------------
# Seed repo path container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeedRepoPaths:
    """Paths to the per-run working copies of the three seed repositories."""

    markupsafe: Path
    type_fest: Path
    tries: Path

    def all_paths(self) -> list[Path]:
        """Return all seed repo paths as a list."""
        return [self.markupsafe, self.type_fest, self.tries]


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Return the immutable E2E configuration for this test session.

    Reads all values from environment variables set by e2e-automation.sh.
    Raises RuntimeError for missing required variables so tests fail fast
    with a clear message rather than hitting obscure connection errors.
    VoyageAI API key is optional -- tests that require it fail individually.
    """
    return E2EConfig(
        server_host=_require_env("E2E_SERVER_HOST"),
        server_port=int(_require_env("E2E_SERVER_PORT")),
        admin_user=_require_env("E2E_ADMIN_USER"),
        admin_pass=_require_env("E2E_ADMIN_PASS"),
        seed_cache_dir=Path(_require_env("E2E_SEED_CACHE_DIR")),
        server_data_dir=Path(_require_env("E2E_SERVER_DATA_DIR")),
        work_dir=Path(_require_env("E2E_WORK_DIR")),
        voyage_api_key=_optional_env("E2E_VOYAGE_API_KEY"),
        golden_repo_job_timeout=float(
            os.environ.get("E2E_GOLDEN_REPO_JOB_TIMEOUT", "180.0")
        ),
        golden_repo_job_poll_interval=float(
            os.environ.get("E2E_GOLDEN_REPO_JOB_POLL_INTERVAL", "2.0")
        ),
    )


@pytest.fixture(scope="session")
def e2e_seed_repo_paths(e2e_config: E2EConfig) -> SeedRepoPaths:
    """Return paths to the per-run working copies of the seed repositories.

    The working copies live under ``e2e_config.work_dir`` and are created
    fresh for each run by ``e2e-automation.sh``.  Tests may modify these
    paths freely; the originals in ``seed_cache_dir`` are never touched.
    """
    return SeedRepoPaths(
        markupsafe=e2e_config.work_dir / "markupsafe",
        type_fest=e2e_config.work_dir / "type-fest",
        tries=e2e_config.work_dir / "tries",
    )


@pytest.fixture(scope="session")
def e2e_server_url(e2e_config: E2EConfig) -> str:
    """Return the base URL of the CIDX server under test."""
    return e2e_config.server_url


@pytest.fixture(scope="session")
def e2e_http_client(e2e_server_url: str) -> Iterator[httpx.Client]:
    """Yield a session-scoped httpx.Client bound to the server base URL.

    The client is closed automatically when the test session ends.
    Individual tests that do not need an authenticated client use this
    fixture directly.  Tests needing auth call helpers.login() or use
    the e2e_admin_token fixture.
    """
    with httpx.Client(base_url=e2e_server_url) as client:
        yield client


@pytest.fixture(scope="session")
def e2e_admin_token(e2e_config: E2EConfig, e2e_server_url: str) -> str:
    """Authenticate once per session and return a valid JWT access token.

    Uses the admin credentials from ``e2e_config``.  The token is minted
    once per test session to avoid hammering the auth endpoint.
    """
    return login(
        base_url=e2e_server_url,
        username=e2e_config.admin_user,
        password=e2e_config.admin_pass,
    )


@pytest.fixture(scope="session")
def e2e_cli_env(e2e_config: E2EConfig) -> dict[str, str]:
    """Return an environment dict suitable for CLI subprocess invocations.

    Includes the VoyageAI API key (if configured) and ensures PYTHONPATH
    includes the project ``src/`` directory so ``python3 -m code_indexer.cli``
    resolves correctly without requiring an installed package.
    """
    src_dir = str(Path(__file__).parent.parent.parent / "src")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath = f"{src_dir}:{existing_pythonpath}"
    else:
        pythonpath = src_dir

    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath

    if e2e_config.voyage_api_key:
        env["VOYAGE_API_KEY"] = e2e_config.voyage_api_key

    return env
