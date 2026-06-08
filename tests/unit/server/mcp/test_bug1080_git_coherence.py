"""
Bug #1080 Tier 2: git_diff / git_log / git_blame byte-envelope coherence.
Regression guard: TruncationHelper search content_type byte-cut behavior unchanged.

Root cause: handlers layer byte-envelope has_more/total_pages from TruncationHelper
on top of domain-paginated data. After fix, git_diff/git_log/git_blame must NOT
surface has_more=True / total_pages>0 from the byte-envelope because clients have
no usable next_offset to navigate it.

Fix contract for Tier 2 (retire byte-envelope for these three tools):
- has_more=False and total_pages=0 when all requested domain records are returned,
  regardless of whether serialized JSON exceeds the byte budget.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock, Mock

from code_indexer.server.auth.user_manager import User, UserRole


CHARS_PER_TOKEN = 4
TINY_TOKEN_LIMIT = 5  # forces serialized JSON to exceed byte budget
NORMAL_TOKEN_LIMIT = 5000
MAX_FETCH_SIZE_CHARS = 50_000


def _user() -> User:
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _mock_payload_cache() -> MagicMock:
    cache = MagicMock()
    cache.store = Mock(return_value="cache-handle-bug1080-git")
    cache.config = MagicMock()
    cache.config.max_fetch_size_chars = MAX_FETCH_SIZE_CHARS
    return cache


def _mock_cfg(token_limit: int) -> MagicMock:
    cfg = MagicMock()
    lim = MagicMock()
    lim.file_content_max_tokens = token_limit
    lim.git_diff_max_tokens = token_limit
    lim.git_log_max_tokens = token_limit
    lim.search_result_max_tokens = token_limit
    lim.chars_per_token = CHARS_PER_TOKEN
    cfg.content_limits_config = lim
    return cfg


def _extract(mcp_response: dict) -> dict:
    if "content" in mcp_response and mcp_response["content"]:
        txt = mcp_response["content"][0].get("text", "")
        try:
            return cast(dict, json.loads(txt))
        except json.JSONDecodeError:
            return {"text": txt}
    return mcp_response


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _make_diff_result(num_files: int = 1) -> object:
    @dataclass
    class Hunk:
        old_start: int = 1
        old_count: int = 3
        new_start: int = 1
        new_count: int = 3
        content: str = "@@ -1,3 +1,3 @@\n-old\n+new\n ctx\n"

    @dataclass
    class FileDiff:
        path: str = "file.py"
        old_path: str = None  # type: ignore[assignment]
        status: str = "modified"
        insertions: int = 3
        deletions: int = 3
        hunks: list = None  # type: ignore[assignment]

        def __post_init__(self):
            if self.hunks is None:
                self.hunks = [Hunk()]

    @dataclass
    class DiffResult:
        from_revision: str = "abc123"
        to_revision: str = "def456"
        files: list = None  # type: ignore[assignment]
        total_insertions: int = 3
        total_deletions: int = 3
        stat_summary: str = "1 file changed"

        def __post_init__(self):
            if self.files is None:
                self.files = [FileDiff(path=f"f{i}.py") for i in range(num_files)]

    return DiffResult()


def _make_log_result(num_commits: int) -> object:
    @dataclass
    class Commit:
        hash: str
        short_hash: str
        author_name: str = "Author"
        author_email: str = "a@b.com"
        author_date: str = "2025-01-01T00:00:00Z"
        committer_name: str = "Author"
        committer_email: str = "a@b.com"
        committer_date: str = "2025-01-01T00:00:00Z"
        subject: str = "Subject"
        body: str = "Body"

    @dataclass
    class LogResult:
        commits: list
        total_count: int
        truncated: bool = False

    commits = [
        Commit(hash=f"a{i:039d}", short_hash=f"a{i:06d}") for i in range(num_commits)
    ]
    return LogResult(commits=commits, total_count=num_commits)


def _make_blame_result(num_lines: int) -> object:
    @dataclass
    class BlameLine:
        line_number: int
        commit_hash: str = "abc123"
        short_hash: str = "abc"
        author_name: str = "Author"
        author_email: str = "a@b.com"
        author_date: str = "2025-01-01"
        original_line_number: int = 1
        content: str = "code here"

    @dataclass
    class BlameResult:
        path: str = "test.py"
        revision: str = "HEAD"
        lines: list = None  # type: ignore[assignment]
        unique_commits: int = 1
        total_lines: int = 0

        def __post_init__(self):
            if self.lines is None:
                self.lines = [BlameLine(line_number=i) for i in range(1, num_lines + 1)]
            self.total_lines = len(self.lines)

    return BlameResult()


# ---------------------------------------------------------------------------
# Call helpers — use handlers facade + _utils.app_module mock (correct pattern)
# ---------------------------------------------------------------------------


def _call_git_diff(num_files: int, token_limit: int) -> dict:
    from code_indexer.server.mcp.handlers.git_read import handle_git_diff

    with (
        patch("code_indexer.server.mcp.handlers.git_read._get_legacy") as mock_leg,
        patch(
            "code_indexer.global_repos.git_operations.GitOperationsService"
        ) as mock_git,
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        patch(
            "code_indexer.server.mcp.handlers.git_read.get_config_service"
        ) as mock_cfg,
    ):
        mock_leg.return_value._resolve_git_repo_path.return_value = ("/fake/repo", None)
        mock_git.return_value.get_diff.return_value = _make_diff_result(num_files)
        mock_app.app.state.payload_cache = _mock_payload_cache()
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_cfg.return_value.get_config.return_value = _mock_cfg(token_limit)

        return _extract(
            handle_git_diff(
                {"repository_alias": "r", "from_revision": "a", "to_revision": "b"},
                _user(),
            )
        )


def _call_git_log(num_commits: int, token_limit: int) -> dict:
    from code_indexer.server.mcp.handlers.git_read import handle_git_log

    with (
        patch("code_indexer.server.mcp.handlers.git_read._get_legacy") as mock_leg,
        patch(
            "code_indexer.global_repos.git_operations.GitOperationsService"
        ) as mock_git,
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        patch(
            "code_indexer.server.mcp.handlers.git_read.get_config_service"
        ) as mock_cfg,
    ):
        mock_leg.return_value._resolve_git_repo_path.return_value = ("/fake/repo", None)
        mock_git.return_value.get_log.return_value = _make_log_result(num_commits)
        mock_app.app.state.payload_cache = _mock_payload_cache()
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_cfg.return_value.get_config.return_value = _mock_cfg(token_limit)

        return _extract(
            handle_git_log({"repository_alias": "r", "limit": num_commits}, _user())
        )


def _call_git_blame_small(num_lines: int) -> dict:
    """Call git_blame with no truncation (small file, no payload_cache)."""
    from code_indexer.server.mcp.handlers.git_read import handle_git_blame

    with (
        patch("code_indexer.server.mcp.handlers.git_read._get_legacy") as mock_leg,
        patch(
            "code_indexer.global_repos.git_operations.GitOperationsService"
        ) as mock_git,
        patch(
            "code_indexer.server.mcp.handlers.git_read.get_config_service"
        ) as mock_cfg,
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
    ):
        mock_leg.return_value._resolve_git_repo_path.return_value = ("/fake/repo", None)
        mock_git.return_value.get_blame.return_value = _make_blame_result(num_lines)
        mock_app.app.state.payload_cache = None  # no cache => no truncation path
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_cfg.return_value.get_config.return_value.content_limits_config = (
            MagicMock()
        )

        return _extract(
            handle_git_blame({"repository_alias": "r", "path": "test.py"}, _user())
        )


def _call_git_blame_large_mocked_truncation(
    num_lines: int, tr_has_more: bool, tr_total_pages: int
) -> dict:
    """
    Call git_blame for a large file (>BLAME_TRUNCATION_LINE_THRESHOLD).
    TruncationHelper is mocked to inject specific has_more/total_pages values,
    allowing tests to verify whether the handler surfaces them or suppresses them.
    """
    from code_indexer.server.mcp.handlers.git_read import handle_git_blame
    import json as _json

    blame = _make_blame_result(num_lines)
    # Build a valid JSON preview for _apply_blame_truncation to parse
    from code_indexer.server.mcp.handlers.git_read import _serialize_blame_lines

    lines_data = _serialize_blame_lines(blame.lines)  # type: ignore[attr-defined]
    valid_preview = _json.dumps({"lines": lines_data[:5]})

    mock_tr = MagicMock()
    mock_tr.truncated = True
    mock_tr.preview = valid_preview
    mock_tr.cache_handle = "cache-handle-blame-bug1080"
    mock_tr.original_tokens = 10000
    mock_tr.preview_tokens = 500
    mock_tr.total_pages = tr_total_pages
    mock_tr.has_more = tr_has_more

    with (
        patch("code_indexer.server.mcp.handlers.git_read._get_legacy") as mock_leg,
        patch(
            "code_indexer.global_repos.git_operations.GitOperationsService"
        ) as mock_git,
        patch(
            "code_indexer.server.mcp.handlers.git_read.get_config_service"
        ) as mock_cfg,
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        patch(
            "code_indexer.server.cache.truncation_helper.TruncationHelper"
        ) as mock_th_cls,
    ):
        mock_leg.return_value._resolve_git_repo_path.return_value = ("/fake/repo", None)
        mock_git.return_value.get_blame.return_value = blame
        mock_app.app.state.payload_cache = _mock_payload_cache()
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_cfg.return_value.get_config.return_value.content_limits_config = (
            MagicMock()
        )
        mock_th_cls.return_value.truncate_and_cache.return_value = mock_tr

        return _extract(
            handle_git_blame({"repository_alias": "r", "path": "test.py"}, _user())
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitDiffCoherence:
    def test_small_diff_has_more_false_total_pages_zero(self):
        data = _call_git_diff(num_files=1, token_limit=NORMAL_TOKEN_LIMIT)
        assert data["success"] is True
        assert data.get("has_more") is False
        assert data.get("total_pages", 0) == 0

    def test_large_diff_has_more_false_total_pages_zero_after_fix(self):
        """
        Large diff JSON exceeds byte budget.
        Bug: has_more=True, total_pages>1 from byte-envelope.
        Fix: has_more=False, total_pages=0 (byte-envelope retired).
        """
        data = _call_git_diff(num_files=10, token_limit=TINY_TOKEN_LIMIT)
        assert data["success"] is True
        assert data.get("has_more") is False, (
            f"has_more={data.get('has_more')} total_pages={data.get('total_pages')} — "
            "byte-envelope must not override domain has_more for git_diff"
        )
        assert data.get("total_pages", 0) == 0, (
            f"total_pages={data.get('total_pages')} — must be 0 after byte-envelope retired"
        )


class TestGitLogCoherence:
    def test_small_log_has_more_false_total_pages_zero(self):
        data = _call_git_log(num_commits=3, token_limit=NORMAL_TOKEN_LIMIT)
        assert data["success"] is True
        assert data.get("has_more") is False
        assert data.get("total_pages", 0) == 0

    def test_large_log_has_more_false_total_pages_zero_after_fix(self):
        """
        Many commits whose JSON exceeds byte budget.
        Bug: has_more=True, total_pages>1 from byte-envelope.
        Fix: has_more=False, total_pages=0 (byte-envelope retired).
        """
        data = _call_git_log(num_commits=50, token_limit=TINY_TOKEN_LIMIT)
        assert data["success"] is True
        assert data.get("has_more") is False, (
            f"has_more={data.get('has_more')} total_pages={data.get('total_pages')} — "
            "byte-envelope must not override domain has_more for git_log"
        )
        assert data.get("total_pages", 0) == 0, (
            f"total_pages={data.get('total_pages')} — must be 0 after byte-envelope retired"
        )


class TestGitBlameCoherence:
    def test_small_blame_has_more_false_total_pages_zero(self):
        """Small blame (under threshold): has_more=False, total_pages=0."""
        data = _call_git_blame_small(num_lines=5)
        assert data["success"] is True
        assert data.get("has_more") is False
        assert data.get("total_pages", 0) == 0

    def test_large_blame_has_more_false_total_pages_zero_after_fix(self):
        """
        Large blame (>BLAME_TRUNCATION_LINE_THRESHOLD=200): TruncationHelper injects
        has_more=True and total_pages=10 via mock — this simulates the byte-envelope signal.
        Bug: _apply_blame_truncation surfaces has_more=True / total_pages=10 in response.
        Fix: has_more=False, total_pages=0 (byte-envelope retired from blame response).
        """
        # TruncationHelper mock injects has_more=True, total_pages=10 (the byte-envelope signal)
        data = _call_git_blame_large_mocked_truncation(
            num_lines=250, tr_has_more=True, tr_total_pages=10
        )
        assert data["success"] is True
        assert data.get("has_more") is False, (
            f"has_more={data.get('has_more')} total_pages={data.get('total_pages')} — "
            "byte-envelope has_more=True must not be surfaced for git_blame"
        )
        assert data.get("total_pages", 0) == 0, (
            f"total_pages={data.get('total_pages')} — must be 0 after byte-envelope retired"
        )


class TestSearchPreviewUnchanged:
    """
    Regression guard: TruncationHelper for content_type='search' must still
    byte-cut (NOT line-aware). The Bug #1080 fix must not alter search behavior.
    """

    def test_search_preview_still_byte_cut_to_max_chars(self):
        """search preview must still be byte-cut to max_chars, has_more=True."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        cache = _mock_payload_cache()
        cfg = _mock_cfg(TINY_TOKEN_LIMIT)
        helper = TruncationHelper(cache, cfg.content_limits_config)

        content = "s" * 1000  # 1000 chars >> 20-char budget
        result = helper.truncate_and_cache(content, content_type="search")

        max_chars = TINY_TOKEN_LIMIT * CHARS_PER_TOKEN  # 20
        assert result.truncated is True
        assert result.has_more is True
        assert len(result.preview) == max_chars, (
            f"Search preview must be byte-cut to {max_chars}; got {len(result.preview)}"
        )
        assert result.cache_handle is not None

    def test_search_preview_is_not_line_aware(self):
        """Search preview does NOT end on a line boundary (byte-cut, expected behavior)."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        cache = _mock_payload_cache()
        cfg = _mock_cfg(TINY_TOKEN_LIMIT)
        helper = TruncationHelper(cache, cfg.content_limits_config)

        # Lines longer than budget => preview cuts mid-line
        content = "a" * 20 + "\n" + "b" * 20 + "\n" + "c" * 20 + "\n"
        result = helper.truncate_and_cache(content, content_type="search")

        max_chars = TINY_TOKEN_LIMIT * CHARS_PER_TOKEN  # 20
        assert len(result.preview) == max_chars
        assert not result.preview.endswith("\n"), (
            "Search preview must NOT be line-aware; expected mid-line byte cut"
        )
