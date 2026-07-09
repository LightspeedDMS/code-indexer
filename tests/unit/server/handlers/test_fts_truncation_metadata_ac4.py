"""
Tests for AC4 (Bug #1202): _apply_search_truncation must return
(results, meta) tuple where meta contains preview_size_chars and rows_capped
for fts/hybrid search modes.

This surfaces the payload-preview threshold and the capped-row count in the
MCP response query_metadata so callers can see at a glance how many snippets
were truncated and what size threshold was applied.
"""

from unittest.mock import MagicMock, patch


def _make_payload_cache(preview_size: int = 500):
    """Build a minimal payload_cache mock."""
    mock_cache = MagicMock()
    mock_cfg = MagicMock()
    mock_cfg.preview_size_chars = preview_size
    mock_cache.config = mock_cfg
    # store_batch returns list of handle strings
    mock_cache.store_batch.side_effect = lambda contents: [
        f"handle_{i}" for i in range(len(contents))
    ]
    return mock_cache


def _make_result(code_snippet: str = "", match_text: str = "") -> dict:
    r = {"file_path": "src/foo.py", "line_number": 1, "similarity_score": 0.9}
    if code_snippet:
        r["code_snippet"] = code_snippet
    if match_text:
        r["match_text"] = match_text
    return r


class TestAC4_FtsTruncationMeta:
    """
    AC4: _apply_search_truncation must return a 2-tuple (results, meta).
    meta["preview_size_chars"] = payload_cache.config.preview_size_chars
    meta["rows_capped"] = number of rows that had at least one field truncated
    For non-fts modes, meta must be an empty dict {}.
    """

    def test_returns_tuple_for_fts_mode(self):
        """Return type is 2-tuple for search_mode='fts'."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        cache = _make_payload_cache(preview_size=500)
        results = [_make_result(code_snippet="short")]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            ret = _apply_search_truncation(results, "fts", {})

        assert isinstance(ret, tuple) and len(ret) == 2, (
            f"_apply_search_truncation must return 2-tuple for fts, got {type(ret)}"
        )

    def test_returns_tuple_for_hybrid_mode(self):
        """Return type is 2-tuple for search_mode='hybrid'."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        cache = _make_payload_cache(preview_size=500)
        results = [_make_result(code_snippet="short")]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            ret = _apply_search_truncation(results, "hybrid", {})

        assert isinstance(ret, tuple) and len(ret) == 2

    def test_meta_has_preview_size_chars_from_cache_config(self):
        """meta['preview_size_chars'] matches payload_cache.config.preview_size_chars."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        cache = _make_payload_cache(preview_size=800)
        results = [_make_result(code_snippet="short")]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            _results, meta = _apply_search_truncation(results, "fts", {})

        assert meta.get("preview_size_chars") == 800, (
            f"Expected preview_size_chars=800, got {meta.get('preview_size_chars')}"
        )

    def test_rows_capped_zero_when_all_snippets_fit(self):
        """rows_capped=0 when no snippet exceeds preview_size."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        cache = _make_payload_cache(preview_size=500)
        results = [
            _make_result(code_snippet="x" * 100),
            _make_result(code_snippet="y" * 200),
        ]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            _results, meta = _apply_search_truncation(results, "fts", {})

        assert meta.get("rows_capped") == 0, (
            f"Expected rows_capped=0 when all fit, got {meta.get('rows_capped')}"
        )

    def test_rows_capped_counts_truncated_rows(self):
        """rows_capped counts rows where at least one field was truncated."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        preview = 500
        cache = _make_payload_cache(preview_size=preview)
        results = [
            _make_result(code_snippet="x" * (preview + 1)),  # over limit -> capped
            _make_result(code_snippet="y" * 50),  # under limit -> not capped
            _make_result(code_snippet="z" * (preview + 100)),  # over -> capped
        ]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            _results, meta = _apply_search_truncation(results, "fts", {})

        assert meta.get("rows_capped") == 2, (
            f"Expected rows_capped=2, got {meta.get('rows_capped')}"
        )

    def test_rows_capped_counts_match_text_truncation_too(self):
        """rows_capped counts a row if match_text is truncated (not just code_snippet)."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        preview = 500
        cache = _make_payload_cache(preview_size=preview)
        results = [
            _make_result(match_text="m" * (preview + 1)),  # match_text over limit
            _make_result(match_text="n" * 50),  # under limit
        ]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            _results, meta = _apply_search_truncation(results, "fts", {})

        assert meta.get("rows_capped") == 1, (
            f"Expected rows_capped=1 for match_text truncation, got {meta.get('rows_capped')}"
        )

    def test_rows_capped_not_double_counted_for_both_fields(self):
        """A row with both code_snippet AND match_text truncated counts as 1 capped row."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        preview = 500
        cache = _make_payload_cache(preview_size=preview)
        results = [
            _make_result(
                code_snippet="x" * (preview + 1),
                match_text="m" * (preview + 1),
            ),
        ]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = cache
            _results, meta = _apply_search_truncation(results, "fts", {})

        assert meta.get("rows_capped") == 1, (
            f"Expected rows_capped=1 (not double-counted), got {meta.get('rows_capped')}"
        )

    def test_semantic_mode_returns_empty_meta(self):
        """For search_mode='semantic', meta must be an empty dict (no preview info)."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        results = [{"file_path": "a.py", "code_snippet": "short"}]

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = None
            ret = _apply_search_truncation(results, "semantic", {})

        assert isinstance(ret, tuple) and len(ret) == 2
        _r, meta = ret
        assert meta == {}, f"semantic mode must return empty meta dict, got {meta}"

    def test_no_cache_returns_empty_meta(self):
        """When payload_cache is None, meta is empty dict for fts mode."""
        from code_indexer.server.mcp.handlers.search import _apply_search_truncation

        results = [_make_result(code_snippet="x" * 5000)]

        with patch(
            "code_indexer.server.mcp.handlers.app_module.app.state"
        ) as mock_state:
            mock_state.payload_cache = None
            ret = _apply_search_truncation(results, "fts", {})

        assert isinstance(ret, tuple) and len(ret) == 2
        _r, meta = ret
        assert meta == {}, f"When no payload_cache, meta must be empty, got {meta}"
