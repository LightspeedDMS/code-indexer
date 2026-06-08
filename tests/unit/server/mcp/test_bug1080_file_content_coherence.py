"""
Bug #1080 Tier 1: get_file_content pagination coherence.

Tests reproduce the incoherence bug FIRST (RED), then the fix must make them pass (GREEN).

Root cause: _build_file_content_response uses TruncationHelper which byte-cuts
content[:max_chars] and then overwrites has_more with the byte-envelope value,
while returned_lines/next_offset remain computed against the full pre-cut slice.

Fix contract (read_chunk algorithm):
- Body: whole lines only, never content[:max_chars] mid-line cut
- returned_lines == actual body line count
- next_offset == offset + returned_lines when has_more else None (ONE meaning)
- Pathological single line > budget: returned whole, next_offset advances past it
- total_pages: coherent with has_more (no contradictory byte-envelope signal)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch, MagicMock, Mock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


CHARS_PER_TOKEN = 4
LOW_TOKEN_LIMIT = 50  # 50 * 4 = 200 chars budget
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
    cache.store = Mock(return_value="cache-handle-bug1080")
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


def _service_response(
    content: str, total_lines: int, offset: int = 1, limit=None
) -> dict:
    """Minimal skip_truncation=True style file-service response."""
    returned_lines = content.count("\n") + (
        1 if content and not content.endswith("\n") else 0
    )
    has_more = (offset + returned_lines - 1) < total_lines
    next_offset = offset + returned_lines if has_more else None
    return {
        "content": content,
        "metadata": {
            "size": len(content),
            "modified_at": "2025-12-29T12:00:00Z",
            "language": "python",
            "path": "test.py",
            "total_lines": total_lines,
            "returned_lines": returned_lines,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": next_offset,
            "truncated": False,
            "truncated_at_line": None,
            "estimated_tokens": len(content) // CHARS_PER_TOKEN,
            "max_tokens_per_request": 5000,
            "requires_pagination": has_more,
            "pagination_hint": None,
        },
    }


def _extract(mcp_response: dict) -> dict:
    if "content" in mcp_response and mcp_response["content"]:
        txt = mcp_response["content"][0].get("text", "")
        try:
            return cast(dict, json.loads(txt))
        except json.JSONDecodeError:
            return {"text": txt}
    return mcp_response


def _call(
    content: str, total_lines: int, token_limit: int, offset=None, limit=None
) -> dict:
    """Call get_file_content handler with mocked dependencies."""
    from code_indexer.server.mcp import handlers

    params: dict = {"repository_alias": "test-repo", "file_path": "test.py"}
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit
    eff_offset = offset or 1

    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
        mock_app.file_service = MagicMock()
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_app.file_service.get_file_content.return_value = _service_response(
            content, total_lines, eff_offset, limit
        )
        mock_app.app.state.payload_cache = _mock_payload_cache()

        with patch(
            "code_indexer.server.mcp.handlers.files.get_config_service"
        ) as mock_cfg_svc:
            mock_cfg_svc.return_value.get_config.return_value = _mock_cfg(token_limit)
            return _extract(handlers.get_file_content(params, _user()))


class TestGetFileContentBodyCoherence:
    """Body and metadata must describe the SAME content after the fix."""

    def test_body_ends_on_line_boundary_not_mid_line(self):
        """Body must end with newline (whole lines), never mid-line byte cut."""
        lines = [f"line {i:03d}\n" for i in range(1, 301)]
        data = _call("".join(lines), total_lines=300, token_limit=LOW_TOKEN_LIMIT)

        assert data["success"] is True
        body = data["file_content"][0]["text"]
        assert body == "" or body.endswith("\n"), (
            f"Body must end on line boundary, got tail: {repr(body[-30:])}"
        )

    def test_returned_lines_matches_actual_body_line_count(self):
        """metadata.returned_lines must equal number of newlines actually in body."""
        lines = [f"line {i:03d}\n" for i in range(1, 301)]
        data = _call("".join(lines), total_lines=300, token_limit=LOW_TOKEN_LIMIT)

        body = data["file_content"][0]["text"]
        actual_count = body.count("\n")
        assert data["metadata"]["returned_lines"] == actual_count, (
            f"returned_lines={data['metadata']['returned_lines']} != body line count={actual_count}"
        )

    def test_has_more_true_when_budget_cuts_content(self):
        """has_more must be True when token budget prevents returning all lines."""
        lines = [f"line {i:03d}\n" for i in range(1, 301)]
        data = _call("".join(lines), total_lines=300, token_limit=LOW_TOKEN_LIMIT)

        assert data["metadata"]["has_more"] is True

    def test_next_offset_equals_offset_plus_returned_lines(self):
        """next_offset must equal offset + returned_lines (first line not yet returned)."""
        lines = [f"line {i:03d}\n" for i in range(1, 301)]
        data = _call("".join(lines), total_lines=300, token_limit=LOW_TOKEN_LIMIT)

        meta = data["metadata"]
        expected = meta["offset"] + meta["returned_lines"]
        assert meta["next_offset"] == expected, (
            f"next_offset={meta['next_offset']} expected {expected} "
            f"(offset={meta['offset']} + returned_lines={meta['returned_lines']})"
        )

    def test_has_more_true_implies_next_offset_not_none(self):
        """has_more=True must be accompanied by a non-None next_offset."""
        lines = [f"line {i:03d}\n" for i in range(1, 301)]
        data = _call("".join(lines), total_lines=300, token_limit=LOW_TOKEN_LIMIT)

        meta = data["metadata"]
        assert meta["has_more"] is True  # pre-condition
        assert meta["next_offset"] is not None, "has_more=True but next_offset is None"


class TestGetFileContentPaginationLoop:
    """next_offset loop must reconstruct the file exactly."""

    def test_loop_reconstructs_file_with_no_gap_or_overlap(self):
        """Paginating via next_offset until has_more=False must reproduce original file."""
        lines = [f"line {i:03d}\n" for i in range(1, 81)]
        full_content = "".join(lines)
        total_lines = 80

        from code_indexer.server.mcp import handlers

        collected_bodies = []
        offset = None

        for _ in range(200):  # safety bound
            eff_offset = offset or 1
            start_idx = max(0, eff_offset - 1)
            slice_content = "".join(lines[start_idx:])

            params: dict = {"repository_alias": "test-repo", "file_path": "test.py"}
            if offset is not None:
                params["offset"] = offset

            with patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app:
                mock_app.file_service = MagicMock()
                mock_app.activated_repo_manager = None
                mock_app.golden_repo_manager = None
                mock_app.file_service.get_file_content.return_value = _service_response(
                    slice_content, total_lines, eff_offset
                )
                mock_app.app.state.payload_cache = _mock_payload_cache()

                with patch(
                    "code_indexer.server.mcp.handlers.files.get_config_service"
                ) as mock_cfg_svc:
                    mock_cfg_svc.return_value.get_config.return_value = _mock_cfg(
                        LOW_TOKEN_LIMIT
                    )
                    data = _extract(handlers.get_file_content(params, _user()))

            body = data["file_content"][0]["text"]
            collected_bodies.append(body)
            meta = data["metadata"]

            if not meta["has_more"]:
                break

            next_off = meta["next_offset"]
            assert next_off is not None, "has_more=True but next_offset is None"
            assert next_off > eff_offset, (
                f"next_offset={next_off} must strictly advance past current={eff_offset}"
            )
            offset = next_off
        else:
            pytest.fail("Pagination did not terminate within 200 iterations")

        reconstructed = "".join(collected_bodies)
        assert reconstructed == full_content, (
            f"Reconstructed ({len(reconstructed)} chars) != original ({len(full_content)} chars)"
        )


class TestGetFileContentEdgeCases:
    """Edge cases: small file, explicit limit, pathological single line."""

    def test_small_file_under_budget_returns_fully(self):
        """Tiny file fully under budget: has_more=False, next_offset=None."""
        content = "line one\nline two\nline three\n"
        data = _call(content, total_lines=3, token_limit=LOW_TOKEN_LIMIT)

        meta = data["metadata"]
        assert data["file_content"][0]["text"] == content
        assert meta["has_more"] is False
        assert meta["next_offset"] is None
        assert meta["returned_lines"] == 3

    def test_explicit_limit_exceeding_budget_still_whole_lines(self):
        """limit=500 with budget 200 chars: fewer lines, all whole, metadata coherent."""
        lines = [f"line {i:05d}\n" for i in range(1, 501)]
        content = "".join(lines)  # each line 11 chars
        data = _call(content, total_lines=500, token_limit=LOW_TOKEN_LIMIT, limit=500)

        body = data["file_content"][0]["text"]
        meta = data["metadata"]
        assert body == "" or body.endswith("\n")
        actual = body.count("\n")
        assert meta["returned_lines"] == actual
        assert meta["returned_lines"] < 500
        assert meta["has_more"] is True
        assert meta["next_offset"] == meta["offset"] + meta["returned_lines"]

    def test_pathological_single_line_over_budget_returned_whole(self):
        """Single line > budget must be returned whole (not byte-cut)."""
        single_line = "x" * 499 + "\n"  # 500 chars > 200 char budget
        data = _call(single_line, total_lines=1, token_limit=LOW_TOKEN_LIMIT)

        body = data["file_content"][0]["text"]
        assert body == single_line, (
            f"Single over-budget line must be returned whole. "
            f"Got {len(body)} chars, expected {len(single_line)}"
        )

    def test_pathological_line_followed_by_more_lines_advances_next_offset(self):
        """First line > budget: must return it whole, next_offset=2 so pagination continues."""
        big_line = "x" * 499 + "\n"
        small_lines = [f"line {i}\n" for i in range(2, 8)]
        all_lines = [big_line] + small_lines
        full_content = "".join(all_lines)
        total_lines = len(all_lines)

        data = _call(full_content, total_lines=total_lines, token_limit=LOW_TOKEN_LIMIT)

        body = data["file_content"][0]["text"]
        meta = data["metadata"]
        assert body == big_line, "Big first line must be returned whole"
        assert meta["has_more"] is True
        assert meta["next_offset"] == 2

    def test_has_more_false_implies_next_offset_none(self):
        """has_more=False must mean next_offset is None (no contradiction)."""
        content = "line one\nline two\n"
        data = _call(content, total_lines=2, token_limit=LOW_TOKEN_LIMIT)

        meta = data["metadata"]
        assert meta["has_more"] is False
        assert meta["next_offset"] is None

    def test_total_pages_coherent_with_has_more_when_all_returned(self):
        """
        When has_more=False (small file fully returned), total_pages must not be > 1.
        Bug: total_pages was byte-based and could be > 1 while has_more=False.
        """
        content = "line one\nline two\n"
        data = _call(content, total_lines=2, token_limit=LOW_TOKEN_LIMIT)

        assert data["metadata"]["has_more"] is False
        total_pages = data.get("total_pages", 0)
        assert total_pages <= 1, (
            f"total_pages={total_pages} implies more byte-pages but has_more=False"
        )


# ---------------------------------------------------------------------------
# Helpers and constants shared by TestFormFeedLineCountInvariant
# ---------------------------------------------------------------------------
_FF_CHARS_PER_TOKEN = 4
_FF_HUGE_BUDGET_MULTIPLIER = 10  # budget = len(content) * this → fits all
_FF_TIGHT_MAX_TOKENS = 6  # 6 * 4 = 24 chars — forces chunking
_FF_SVC_MAX_TOKENS = 10_000  # service side: no truncation
_FF_MAX_PAGINATION_ITERS = 200


def _ff_make_service(repo_dir: Path) -> object:
    """Real FileListingService with ActivatedRepoManager mocked to serve repo_dir."""
    from code_indexer.server.services.file_service import FileListingService

    svc = FileListingService.__new__(FileListingService)
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(repo_dir)
    svc.activated_repo_manager = arm
    return svc


def _ff_call_handler(svc: object, file_name: str, max_tokens: int, offset: int) -> dict:
    """Invoke get_file_content against real FileService; return the extracted JSON dict."""
    from code_indexer.server.mcp import handlers

    svc_lim = MagicMock()
    svc_lim.file_content_max_tokens = _FF_SVC_MAX_TOKENS
    svc_lim.chars_per_token = _FF_CHARS_PER_TOKEN
    svc_cfg = MagicMock()
    svc_cfg.content_limits_config = svc_lim

    h_lim = MagicMock()
    h_lim.file_content_max_tokens = max_tokens
    h_lim.chars_per_token = _FF_CHARS_PER_TOKEN
    h_cfg = MagicMock()
    h_cfg.content_limits_config = h_lim

    params: dict = {"repository_alias": "test-repo", "file_path": file_name}
    if offset != 1:
        params["offset"] = offset

    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        patch("code_indexer.server.mcp.handlers.files.get_config_service") as mock_hcfg,
        patch(
            "code_indexer.server.services.file_service.get_config_service"
        ) as mock_scfg,
    ):
        mock_app.file_service = svc
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None
        mock_app.app.state.payload_cache = None
        mock_hcfg.return_value.get_config.return_value = h_cfg
        mock_scfg.return_value.get_config.return_value = svc_cfg
        result = handlers.get_file_content(params, _user())

    raw = result["content"][0]["text"]
    return cast(dict, json.loads(raw))


class TestFormFeedLineCountInvariant:
    """INVARIANT: _read_chunk line count == service total_lines for any byte content.

    The service counts lines via f.readlines() (\\n-only in text mode).
    Bug: splitlines(keepends=True) also splits on \\x0c/\\v, over-counting.
    Fix: \\n-only split must be used in _read_chunk.
    All tests below FAIL on the buggy code and PASS after the fix.
    """

    def test_formfeed_split_count_matches_readlines(self, tmp_path):
        """_read_chunk count must equal f.readlines() count for \\x0c content."""
        from code_indexer.server.mcp.handlers.files import _read_chunk

        content = "alpha\nbeta\x0cgamma\ndelta\n"
        ff = tmp_path / "ff.txt"
        ff.write_text(content, encoding="utf-8")
        with open(str(ff), "r", encoding="utf-8") as fh:
            service_total = len(fh.readlines())

        huge = len(content) * _FF_HUGE_BUDGET_MULTIPLIER
        _, returned_lines, has_more, next_offset, _ = _read_chunk(
            content=content, offset=1, total_lines=service_total, max_chars=huge
        )

        assert returned_lines == service_total, (
            f"_read_chunk returned_lines={returned_lines} != service total={service_total}; "
            "splitlines() treats \\x0c as a separator — must use \\n-only split"
        )
        assert has_more is False
        assert next_offset is None

    def test_vt_split_count_matches_readlines(self, tmp_path):
        """Same invariant for \\v (\\x0b) characters inside a line body."""
        from code_indexer.server.mcp.handlers.files import _read_chunk

        content = "line1\nsome\x0bembedded\x0bvt\nline3\n"
        ff = tmp_path / "vt.txt"
        ff.write_text(content, encoding="utf-8")
        with open(str(ff), "r", encoding="utf-8") as fh:
            service_total = len(fh.readlines())

        huge = len(content) * _FF_HUGE_BUDGET_MULTIPLIER
        _, returned_lines, has_more, _, _ = _read_chunk(
            content=content, offset=1, total_lines=service_total, max_chars=huge
        )

        assert returned_lines == service_total, (
            f"_read_chunk returned_lines={returned_lines} != service total={service_total} "
            "for \\v content — splitlines() over-counts"
        )
        assert has_more is False

    def test_real_file_returned_lines_never_exceeds_total(self, tmp_path):
        """Real FileService: metadata.returned_lines must never exceed total_lines."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        content = "alpha\nbeta\x0cgamma\ndelta\nepsilon\x0bzeta\n"
        (repo_dir / "ff.py").write_text(content, encoding="utf-8")

        svc = _ff_make_service(repo_dir)
        data = _ff_call_handler(svc, "ff.py", _FF_TIGHT_MAX_TOKENS, offset=1)

        assert data["success"] is True
        meta = data["metadata"]
        assert meta["returned_lines"] <= meta["total_lines"], (
            f"returned_lines={meta['returned_lines']} > total_lines={meta['total_lines']}; "
            "splitlines() over-counts \\x0c/\\v as line separators"
        )

    def test_real_file_pagination_loop_exact_reconstruction(self, tmp_path):
        """Pagination loop via real FileService must reconstruct \\x0c/\\v file exactly."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        original = (
            "first normal\n"
            "second\x0cformfeed inside\n"
            "third\x0bverticaltab\n"
            "fourth normal\n"
            "fifth\x0csecond\x0cff\n"
            "sixth normal\n"
        )
        (repo_dir / "special.py").write_text(original, encoding="utf-8")
        svc = _ff_make_service(repo_dir)

        collected: list = []
        offset = 1
        for _ in range(_FF_MAX_PAGINATION_ITERS):
            data = _ff_call_handler(svc, "special.py", _FF_TIGHT_MAX_TOKENS, offset)
            assert data["success"] is True, f"handler failed at offset={offset}"
            collected.append(data["file_content"][0]["text"])
            if not data["metadata"]["has_more"]:
                break
            nxt = data["metadata"]["next_offset"]
            assert nxt is not None and nxt > offset, (
                f"next_offset={nxt} must strictly advance past {offset}"
            )
            offset = nxt
        else:
            pytest.fail("Pagination did not terminate within 200 iterations")

        reconstructed = "".join(collected)
        assert reconstructed == original, (
            f"Reconstructed ({len(reconstructed)} chars) != original ({len(original)} chars).\n"
            f"  original:      {repr(original)}\n"
            f"  reconstructed: {repr(reconstructed)}"
        )
