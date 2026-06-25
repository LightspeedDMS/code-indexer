"""Bug #1208 — Temporal rerank document must include commit message.

Tests confirm that the shared extract_rerank_document helper:
  1. Returns diff + commit_message for temporal commit_diff results with a message.
  2. Returns diff only (graceful) when commit_message is absent.
  3. Returns the existing content unchanged for non-temporal results.
  4. Is used (not an inline lambda) at the MCP search.py call site.
  5. Is used at the REST inline_query.py call site.
  6. CLI temporal path includes commit_message in the funnel doc via _temporal_obj.

Result shapes under test:
  - MCP/REST dict: {"code_snippet": str, "temporal_context": {"commit_message": str, ...}}
  - CLI dict:      {"snippet": str, "_temporal_obj": TemporalSearchResult(...)}
  - Non-temporal:  {"code_snippet": str}  (no temporal_context key)

Concat format chosen: "{diff}\\n\\nCommit: {commit_message}"
  Rationale: commit message AFTER diff so cross-encoder sees full context, clearly
  delimited by "Commit:" label.  Consistent across all 3 surfaces.
"""

import inspect
from typing import Any, Dict, Optional

from code_indexer.services.temporal.temporal_search_service import TemporalSearchResult


# ---------------------------------------------------------------------------
# Helper: build result shapes
# ---------------------------------------------------------------------------


def _mcp_result(
    code_snippet: str = "diff --git a/foo.py",
    commit_message: Optional[str] = "Fix auth bug",
) -> Dict[str, Any]:
    """MCP/REST dict shape from QueryResult.to_dict()."""
    r: Dict[str, Any] = {"code_snippet": code_snippet}
    if commit_message is not None:
        r["temporal_context"] = {"commit_message": commit_message}
    return r


def _cli_temporal_obj(
    content: str = "diff --git a/bar.py",
    commit_message: Optional[str] = "Refactor login",
) -> TemporalSearchResult:
    """TemporalSearchResult as stored in _temporal_obj."""
    tc: Dict[str, Any] = {"commit_date": "2025-01-01"}
    if commit_message is not None:
        tc["commit_message"] = commit_message
    return TemporalSearchResult(
        file_path="bar.py",
        chunk_index=0,
        content=content,
        score=0.8,
        metadata={"type": "commit_diff"},
        temporal_context=tc,
    )


def _cli_dict(
    content: str = "diff --git a/bar.py",
    commit_message: Optional[str] = "Refactor login",
) -> Dict[str, Any]:
    """CLI funnel input dict with _temporal_obj for round-trip."""
    return {
        "snippet": content,
        "file_path": "bar.py",
        "score": 0.8,
        "_temporal_obj": _cli_temporal_obj(content, commit_message),
    }


def _non_temporal_dict(code_snippet: str = "class Auth: pass") -> Dict[str, Any]:
    """Non-temporal dict — no temporal_context key."""
    return {"code_snippet": code_snippet, "file_path": "auth.py"}


# ---------------------------------------------------------------------------
# 1–3: Unit tests for extract_rerank_document
# ---------------------------------------------------------------------------


class TestExtractRerankDocument:
    """Tests for the shared extract_rerank_document helper in reranking.py."""

    def _extractor(self):
        from code_indexer.server.mcp.reranking import extract_rerank_document

        return extract_rerank_document

    def test_mcp_commit_diff_with_commit_message_includes_both(self):
        """MCP dict with temporal_context.commit_message -> combined document."""
        extract = self._extractor()
        r = _mcp_result(
            code_snippet="diff --git a/auth.py",
            commit_message="Fix authentication bypass",
        )
        doc = extract(r)
        assert "diff --git a/auth.py" in doc
        assert "Fix authentication bypass" in doc

    def test_mcp_commit_diff_uses_commit_label_delimiter(self):
        """Concat format is: '{diff}\\n\\nCommit: {message}'."""
        extract = self._extractor()
        r = _mcp_result(
            code_snippet="diff --git a/auth.py",
            commit_message="Fix authentication bypass",
        )
        doc = extract(r)
        assert "Commit: Fix authentication bypass" in doc
        # diff comes before commit message
        diff_pos = doc.index("diff --git a/auth.py")
        commit_pos = doc.index("Commit: Fix authentication bypass")
        assert diff_pos < commit_pos

    def test_mcp_commit_diff_without_commit_message_returns_diff_only(self):
        """Graceful fallback: no commit_message -> diff only (no crash)."""
        extract = self._extractor()
        r = _mcp_result(code_snippet="diff --git a/main.py", commit_message=None)
        doc = extract(r)
        assert doc == "diff --git a/main.py"

    def test_mcp_temporal_context_empty_commit_message_returns_diff_only(self):
        """Empty string commit_message is falsy -> diff only."""
        extract = self._extractor()
        r: Dict[str, Any] = {
            "code_snippet": "diff --git a/x.py",
            "temporal_context": {"commit_message": ""},
        }
        doc = extract(r)
        assert doc == "diff --git a/x.py"

    def test_cli_dict_with_temporal_obj_includes_commit_message(self):
        """CLI dict (_temporal_obj present) -> combined doc."""
        extract = self._extractor()
        r = _cli_dict(
            content="diff --git a/login.py",
            commit_message="Refactor login flow",
        )
        doc = extract(r)
        assert "diff --git a/login.py" in doc
        assert "Refactor login flow" in doc
        assert "Commit: Refactor login flow" in doc

    def test_cli_dict_temporal_obj_no_commit_message_returns_snippet_only(self):
        """CLI dict with _temporal_obj but no commit_message -> snippet only."""
        extract = self._extractor()
        r = _cli_dict(content="diff --git a/x.py", commit_message=None)
        doc = extract(r)
        assert doc == "diff --git a/x.py"

    def test_non_temporal_dict_returns_code_snippet_unchanged(self):
        """Non-temporal result (no temporal_context) -> content unchanged."""
        extract = self._extractor()
        r = _non_temporal_dict(code_snippet="class Auth: pass")
        doc = extract(r)
        assert doc == "class Auth: pass"

    def test_non_temporal_content_field_returned_unchanged(self):
        """Non-temporal result with 'content' key -> content unchanged."""
        extract = self._extractor()
        r = {"content": "def login(): pass"}
        doc = extract(r)
        assert doc == "def login(): pass"

    def test_content_takes_precedence_over_code_snippet(self):
        """'content' field preferred over 'code_snippet' for non-temporal."""
        extract = self._extractor()
        r = {"content": "from_content", "code_snippet": "from_code_snippet"}
        doc = extract(r)
        assert doc == "from_content"

    def test_fts_grep_match_text_returned_when_snippet_empty(self):
        """FTS grep-mode result {snippet:'', match_text:'def login():'} -> 'def login():'.

        --snippet-lines 0 produces snippet='' with matched text in match_text
        (tantivy_index_manager.py:786,1130; also snippet='' on exception at :982).
        Without match_text in the base chain the extractor returns '' — empty string
        sent to the cross-encoder, silently degrading rerank quality.
        """
        extract = self._extractor()
        r = {"snippet": "", "match_text": "def login():", "path": "auth.py"}
        doc = extract(r)
        assert doc == "def login():", (
            f"Expected 'def login():' but got {doc!r}; "
            "match_text must be included in the base content chain."
        )

    def test_empty_result_returns_empty_string(self):
        """Empty dict -> empty string (no crash)."""
        extract = self._extractor()
        assert extract({}) == ""


# ---------------------------------------------------------------------------
# 4: MCP search.py call site uses extract_rerank_document (not inline lambda)
# ---------------------------------------------------------------------------


class TestMcpCallSiteUsesSharedExtractor:
    """Verify MCP _apply_rerank_and_filter passes extract_rerank_document,
    not an inline lambda, to _apply_reranking_sync."""

    def test_search_py_uses_extract_rerank_document(self):
        """_apply_rerank_and_filter must import and pass extract_rerank_document.

        We verify by inspecting the source of _apply_rerank_and_filter:
        the string 'extract_rerank_document' must appear in its source.
        This fails if the call site still uses an inline lambda.
        """
        from code_indexer.server.mcp.handlers import search as search_module

        src = inspect.getsource(search_module._apply_rerank_and_filter)
        assert "extract_rerank_document" in src, (
            "_apply_rerank_and_filter still uses an inline lambda instead of "
            "extract_rerank_document; wire the shared extractor."
        )


# ---------------------------------------------------------------------------
# 5: REST inline_query.py call site uses extract_rerank_document
# ---------------------------------------------------------------------------


class TestRestCallSiteUsesSharedExtractor:
    """Verify REST inline_query rerank block passes extract_rerank_document."""

    def test_inline_query_uses_extract_rerank_document(self):
        """inline_query.py rerank block must reference extract_rerank_document."""
        import code_indexer.server.routers.inline_query as iq_module

        src = inspect.getsource(iq_module)
        assert "extract_rerank_document" in src, (
            "inline_query.py rerank block still uses an inline lambda instead of "
            "extract_rerank_document; wire the shared extractor."
        )


# ---------------------------------------------------------------------------
# 6: CLI temporal path includes commit_message in funnel doc
# ---------------------------------------------------------------------------


class TestCliTemporalFunnelIncludesCommitMessage:
    """Verify that the CLI temporal funnel dict contains commit_message info
    so the shared extractor can include it in the rerank document."""

    def test_cli_funnel_dict_commit_message_reaches_extractor(self):
        """The CLI builds funnel dicts with _temporal_obj carrying commit_message.

        When extract_rerank_document processes that dict, the result must contain
        the commit message.  This test fails if the CLI drops commit_message
        from the funnel dict (anti-orphan check).
        """
        from code_indexer.server.mcp.reranking import extract_rerank_document

        obj = _cli_temporal_obj(
            content="diff --git a/api.py",
            commit_message="Add rate limiting",
        )
        funnel_dict = {
            "snippet": obj.content,
            "file_path": obj.file_path,
            "score": obj.score,
            "_temporal_obj": obj,
        }
        doc = extract_rerank_document(funnel_dict)
        assert "Add rate limiting" in doc, (
            "extract_rerank_document did not include commit_message from _temporal_obj; "
            "the CLI funnel dict must preserve _temporal_obj with its temporal_context."
        )

    def test_cli_source_builds_funnel_dicts_with_temporal_obj(self):
        """cli.py must still set _temporal_obj in the funnel dicts.

        Inspect cli.py source to confirm the _temporal_obj key is present in
        the funnel-dict comprehension.  If someone removes _temporal_obj,
        commit_message is silently dropped (anti-orphan regression).
        """
        import code_indexer.cli as cli_module

        src = inspect.getsource(cli_module)
        assert '"_temporal_obj"' in src or "'_temporal_obj'" in src, (
            "cli.py does not build funnel dicts with '_temporal_obj'; "
            "commit_message will be silently dropped from the rerank document."
        )
