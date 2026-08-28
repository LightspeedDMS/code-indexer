"""Bug #1704 regression: REST write endpoints must honor
file_crud_service's write-exception registrations, matching MCP's
behavior.

Discovery (per the issue): routers/files.py's three write endpoints
(create/edit/delete) each construct a FRESH FileCRUDService() per REST
request, with an empty `_global_write_exceptions` map. Meanwhile, the
MCP front door (mcp/handlers/files.py) imports and calls the SAME
module-level singleton `file_crud_service` that
startup/service_init.py's Story #197 AC1/AC4 code registers
'cidx-meta-global' against at real server boot. Net effect: a write to
a registered write-exception alias SUCCEEDS via MCP but is REFUSED via
REST.

This test uses the REAL app lifespan (TestClient(app) as a context
manager, mirroring the established pattern in
test_files_and_watch_singleton_lifespan_boot_1689.py) so
app.state.activated_repo_manager is genuinely wired, and registers a
throwaway write-exception alias directly on the REAL file_crud_service
singleton -- exactly what service_init.py does for cidx-meta-global --
then drives the REST endpoint with NO mocking of FileCRUDService. Real
filesystem I/O against a temp directory stands in for a golden repo,
per the anti-mock principle (real system, not a test double).
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.file_crud_service import file_crud_service

_TEST_ALIAS = "bug1704-write-exception-global"
_TEST_ALIAS_WITHOUT_GLOBAL = "bug1704-write-exception"


@pytest.fixture
def write_exception_repo() -> Iterator[Tuple[str, Path]]:
    """Register a throwaway write-exception alias on the REAL
    file_crud_service singleton, mirroring exactly what
    startup/service_init.py does for 'cidx-meta-global'. Saves and
    restores the singleton's prior state so this test cannot leak into
    other tests sharing the same process-wide singleton.
    """
    saved_exceptions = dict(file_crud_service._global_write_exceptions)
    saved_golden_repos_dir = file_crud_service._golden_repos_dir

    tmp_root = Path(tempfile.mkdtemp(prefix="cidx_bug1704_"))
    try:
        golden_repos_dir = tmp_root / "golden-repos"
        repo_dir = golden_repos_dir / "bug1704-write-exception"
        repo_dir.mkdir(parents=True)

        # Story #231 write-mode marker: required for
        # _check_write_mode_active() to allow the write through.
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True)
        (write_mode_dir / f"{_TEST_ALIAS_WITHOUT_GLOBAL}.json").write_text("{}")

        file_crud_service.register_write_exception(_TEST_ALIAS, repo_dir)
        file_crud_service.set_golden_repos_dir(golden_repos_dir)

        yield _TEST_ALIAS, repo_dir
    finally:
        file_crud_service._global_write_exceptions = saved_exceptions
        file_crud_service._golden_repos_dir = saved_golden_repos_dir
        shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture
def mock_user() -> User:
    return User(
        username="bug1704-testuser",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


class TestRestWriteExceptionParityWithMcp:
    """REST write endpoints must accept writes to a registered
    write-exception alias, exactly as MCP does."""

    def test_create_file_endpoint_succeeds_for_registered_write_exception_alias(
        self, write_exception_repo, mock_user
    ) -> None:
        """BUG #1704: a REST create_file to a file_crud_service-registered
        write-exception alias must succeed, not be refused as though the
        alias were an ordinary, non-activated repository.

        Before the fix: routers/files.py's create_file endpoint
        constructs a fresh FileCRUDService() whose own
        `_global_write_exceptions` map is empty, so `_resolve_repo_path`
        falls through to the activated-repo registry gate (#1692),
        which correctly reports this alias as not activated for the
        user -> 404. That refusal is the bug: the identical operation
        via MCP succeeds because MCP calls the same populated singleton.

        After the fix: the REST endpoint must resolve the write
        exception and create the file successfully (real filesystem
        write against the temp repo dir -- no mocking).
        """
        alias, repo_dir = write_exception_repo

        app.dependency_overrides[get_current_user] = lambda: mock_user
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/api/v1/repos/{alias}/files",
                    json={
                        "file_path": "bug1704_probe.txt",
                        "content": "bug 1704 write-exception parity probe",
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_201_CREATED, (
            "BUG #1704 REGRESSION: REST create_file must succeed for a "
            "registered write-exception alias, matching MCP's behavior. "
            f"Got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["file_path"] == "bug1704_probe.txt"

        created_file = repo_dir / "bug1704_probe.txt"
        assert created_file.exists(), (
            "File must have been written to the write-exception's "
            "canonical path on disk, exactly as the MCP front door does."
        )
        assert created_file.read_text() == "bug 1704 write-exception parity probe"

    def test_create_file_endpoint_still_refused_for_non_exception_unactivated_alias(
        self, mock_user
    ) -> None:
        """Regression guard: a REST write to an ORDINARY alias that is
        neither a registered write exception nor an activated repository
        for the user must still be refused via the #1692 registry gate.
        The #1704 fix (reusing the file_crud_service singleton) must not
        loosen this check.
        """
        app.dependency_overrides[get_current_user] = lambda: mock_user
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/repos/bug1704-not-a-real-repo/files",
                    json={
                        "file_path": "probe.txt",
                        "content": "should never be written",
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_404_NOT_FOUND, (
            "Non-exception, non-activated alias must still be refused "
            f"(#1692 registry gate). Got {response.status_code}: "
            f"{response.text}"
        )
