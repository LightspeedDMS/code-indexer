"""#2047 (S21): CLI remote mode sends ``file_extensions`` as the REST
``/api/query`` field.

Real execute_remote_query, real RemoteQueryClient, real HTTP to the
in-process test CIDX server (tests/infrastructure/test_cidx_server.py),
which records each request body. Replaced: only the local-file lookups of
remote mode (remote config, repository link, decrypted credentials), which
point the query at that server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from tests.infrastructure.test_cidx_server import CIDXServerTestContext


async def _remote_query(
    tmp_path: Path, file_extensions: Optional[List[str]]
) -> Dict[str, Any]:
    from code_indexer.remote import query_execution
    from code_indexer.remote.repository_linking import (
        RepositoryLink,
        RepositoryType,
    )

    async with CIDXServerTestContext() as server:
        base_url = server.base_url
        assert base_url is not None
        link = RepositoryLink(
            alias="example-repo",
            git_url="https://example.com/example/repo.git",
            branch="main",
            repository_type=RepositoryType.ACTIVATED,
            server_url=base_url,
            linked_at="2026-01-01T00:00:00Z",
            display_name="Example Repository",
            description="Example repository",
            access_level="read",
        )
        with (
            patch.object(
                query_execution,
                "_load_remote_configuration",
                return_value={"server_url": server.base_url, "username": "testuser"},
            ),
            patch.object(query_execution, "load_repository_link", return_value=link),
            patch.object(
                query_execution,
                "_get_decrypted_credentials",
                return_value={"username": "testuser", "password": "testpass123"},
            ),
        ):
            query_execution.execute_remote_query(
                "widget", 5, tmp_path, file_extensions=file_extensions
            )
        assert len(server.query_requests) == 1
        return server.query_requests[0]


@pytest.mark.asyncio
async def test_remote_query_sends_file_extensions_field(tmp_path: Path) -> None:
    body = await _remote_query(tmp_path, ["py", "md"])
    assert body["file_extensions"] == ["py", "md"]


@pytest.mark.asyncio
async def test_remote_query_without_extensions_sends_no_filter(tmp_path: Path) -> None:
    body = await _remote_query(tmp_path, None)
    assert body["file_extensions"] is None


@pytest.mark.asyncio
async def test_remote_multi_repo_query_sends_file_extensions_field(
    tmp_path: Path,
) -> None:
    from code_indexer import cli_multi_repo
    from code_indexer.remote import query_execution

    async with CIDXServerTestContext() as server:
        with (
            patch.object(
                query_execution,
                "_load_remote_configuration",
                return_value={"server_url": server.base_url, "username": "testuser"},
            ),
            patch.object(
                query_execution,
                "_get_decrypted_credentials",
                return_value={"username": "testuser", "password": "testpass123"},
            ),
        ):
            cli_multi_repo.execute_multi_repo_query(
                query_text="widget",
                repos=["repo-a", "repo-b"],
                limit=5,
                project_root=tmp_path,
                file_extensions=["py", "md"],
            )
        assert len(server.query_requests) == 1
        assert server.query_requests[0]["file_extensions"] == ["py", "md"]
