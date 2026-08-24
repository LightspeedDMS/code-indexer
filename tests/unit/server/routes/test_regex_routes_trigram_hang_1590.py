"""Bug #1590 review round 2 (finding F4): the non-xray front doors (REST
POST /api/regex/search, MCP regex_search) call the SAME
RegexSearchService.search() that xray_search's Phase 1 content driver
uses, so they inherit the SAME pre-fix trigram-prefilter hang bug -- and
now inherit the SAME fix. Both already had an `except TimeoutError`
handler (proven with a MOCKED TimeoutError in test_regex_routes.py's
TestSearchTimeout), but neither had coverage proving a REAL trigram-index
hang actually reaches that handler rather than blocking the request
forever. This file closes that gap for the REST route.

Real repo, real ripgrep, real on-disk trigram index -- only
sqlite3.connect is monkeypatched to block, exactly the reproduction
technique used in test_regex_search_trigram_prefilter_timeout_1590.py.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routes.regex_routes import RegexSearchRequest, regex_search

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _build_repo_with_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auth.java").write_text("public class LSAuthenticator {}\n")
    mgr = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    mgr.build(repo)
    return repo


def _patch_config_with_timeout(timeout_seconds: int) -> Any:
    mock_config = MagicMock()
    mock_config.search_limits_config.timeout_seconds = timeout_seconds
    mock_get_config_service = MagicMock()
    mock_get_config_service.return_value.get_config.return_value = mock_config
    return patch(
        "code_indexer.server.routes.regex_routes.get_config_service",
        mock_get_config_service,
    )


class TestRegexRouteDegradesOnTrigramHang:
    async def test_regex_search_route_degrades_gracefully_on_trigram_hang(
        self, tmp_path, monkeypatch
    ):
        repo = _build_repo_with_index(tmp_path)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        BLOCK_SECONDS = 3.0
        TIMEOUT_SECONDS = 1

        def _slow_connect(*args, **kwargs):
            time.sleep(BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(
            pattern="LSAuthenticator", repository_alias="myrepo-global"
        )

        with (
            patch(
                "code_indexer.server.routes.regex_routes._resolve_repo_path",
                return_value=str(repo),
            ),
            _patch_config_with_timeout(TIMEOUT_SECONDS),
        ):
            start = time.monotonic()
            with pytest.raises(HTTPException) as exc_info:
                await regex_search(body, user)
            elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS, (
            f"regex_search route took {elapsed:.2f}s -- expected to be "
            f"bounded well under the {BLOCK_SECONDS}s blocking trigram "
            f"connect(), proving the fix protects this front door too, "
            f"not just xray_search"
        )
        assert exc_info.value.status_code == 408
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("error_code") == "search_timeout"
