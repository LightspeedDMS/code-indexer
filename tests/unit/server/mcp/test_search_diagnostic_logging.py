"""
Tests for Phase 1 Diagnostic Logging on MCP search_code hot path.

Bug #881: HNSW cache memory leak — Phase 1 adds 4 INFO log lines so operators
can observe wildcard expansion blowup live and diagnose Mechanism B.

The 4 expected INFO lines (one test per line):
1. search_code() entry: correlation_id, user_id, query_text (truncated to 100 chars),
   repository_alias, limit, accuracy
2. _expand_wildcard_patterns(): correlation_id, pattern, matched_count (promoted from DEBUG to INFO)
3. _omni_search_code() post-expansion: correlation_id, user_id, expanded_count, expanded_aliases
4. search_code() exit: correlation_id, result_count, elapsed_ms
"""

import logging
import re
import tempfile
from unittest.mock import MagicMock, patch

# Named constants — no magic numbers in tests
QUERY_LONG = "x" * 200
QUERY_LOG_TRUNCATION_LIMIT = 100
QUERY_NORMAL = "find authentication logic in the user service"
EXPANDED_REPO_COUNT = 3
CORRELATION_ID_ENTRY = "corr-entry-001"
CORRELATION_ID_EXPAND = "corr-expand-001"
CORRELATION_ID_OMNI = "corr-omni-001"
CORRELATION_ID_EXIT = "corr-exit-001"
FAKE_REPOS = [
    {"alias_name": "repo-a-global"},
    {"alias_name": "repo-b-global"},
    {"alias_name": "repo-c-global"},
]
EXPANDED_ALIASES = [r["alias_name"] for r in FAKE_REPOS]
LIMIT = 10
ACCURACY = "balanced"
WILDCARD_PATTERN = "*-global"


def _make_user(username: str) -> MagicMock:
    user = MagicMock()
    user.username = username
    return user


def _single_repo_params(query_text: str = QUERY_NORMAL) -> dict:
    return {
        "repository_alias": "my-repo-global",
        "query_text": query_text,
        "limit": LIMIT,
        "accuracy": ACCURACY,
    }


def _empty_search_response() -> dict:
    return {"content": [{"type": "text", "text": '{"success":true,"results":[]}'}]}


def _make_omni_patches():
    """Return context managers for all omni-search collaborators."""
    fake_multi_response = MagicMock()
    fake_multi_response.errors = None
    fake_multi_response.results = {}
    fake_multi_response.metadata = MagicMock()
    fake_multi_response.metadata.total_repos_searched = EXPANDED_REPO_COUNT

    return fake_multi_response, [
        patch(
            "code_indexer.server.mcp.handlers.search._expand_wildcard_patterns",
            return_value=EXPANDED_ALIASES,
        ),
        patch(
            "code_indexer.server.mcp.handlers.search.get_correlation_id",
            return_value=CORRELATION_ID_OMNI,
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._format_omni_response",
            return_value={
                "results": [],
                "total_repos_searched": EXPANDED_REPO_COUNT,
                "errors": {},
            },
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._flatten_multi_results",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._load_category_map",
            return_value={},
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._get_wiki_enabled_repos",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._filter_errors_for_user",
            return_value={},
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._aggregate_results",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._get_access_filtering_service",
            return_value=None,
        ),
    ]


# ---------------------------------------------------------------------------
# Test 1 — search_code() entry log
# ---------------------------------------------------------------------------


def test_entry_log_fires_with_all_required_fields(caplog):
    """INFO log at search_code() entry must include: correlation_id, user_id,
    query_text truncated to QUERY_LOG_TRUNCATION_LIMIT chars, repository_alias,
    limit, accuracy.  Full QUERY_LONG string must NOT appear.
    """
    from code_indexer.server.mcp.handlers.search import search_code

    user = _make_user("alice")
    params = _single_repo_params(query_text=QUERY_LONG)

    with (
        patch(
            "code_indexer.server.mcp.handlers.search._search_global_repo",
            return_value=_empty_search_response(),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search.get_correlation_id",
            return_value=CORRELATION_ID_ENTRY,
        ),
        caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers.search"),
    ):
        search_code(params, user)

    entry_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and "alice" in r.getMessage()
        and CORRELATION_ID_ENTRY in r.getMessage()
    ]
    assert entry_records, (
        f"Expected entry INFO log with user='alice' and correlation_id={CORRELATION_ID_ENTRY!r}. "
        f"INFO records: {[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
    )
    msg = entry_records[0].getMessage()

    assert CORRELATION_ID_ENTRY in msg, f"correlation_id missing: {msg}"
    assert "alice" in msg, f"user_id missing: {msg}"
    assert "my-repo-global" in msg, f"repository_alias missing: {msg}"
    assert str(LIMIT) in msg, f"limit missing: {msg}"
    assert ACCURACY in msg, f"accuracy missing: {msg}"

    # Full 200-char query must NOT appear — it must be truncated
    assert QUERY_LONG not in msg, (
        f"query_text was not truncated — full {len(QUERY_LONG)}-char query found in entry log"
    )
    # Truncated prefix must appear
    query_prefix = QUERY_LONG[:QUERY_LOG_TRUNCATION_LIMIT]
    assert query_prefix in msg, (
        f"Truncated query prefix (first {QUERY_LOG_TRUNCATION_LIMIT} chars) missing: {msg}"
    )


# ---------------------------------------------------------------------------
# Test 2 — _expand_wildcard_patterns() INFO log on wildcard match
# ---------------------------------------------------------------------------


def test_wildcard_expansion_info_log_fires_with_pattern_count_and_correlation_id(
    caplog,
):
    """Wildcard expansion must emit INFO (not just DEBUG) with: correlation_id,
    pattern, matched_count.
    """
    from code_indexer.server.mcp.handlers._utils import _expand_wildcard_patterns

    user = _make_user("charlie")
    with tempfile.TemporaryDirectory() as fake_golden_dir:
        with (
            patch(
                "code_indexer.server.mcp.handlers._utils._list_global_repos",
                return_value=FAKE_REPOS,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_golden_repos_dir",
                return_value=fake_golden_dir,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils.get_correlation_id",
                return_value=CORRELATION_ID_EXPAND,
            ),
            caplog.at_level(
                logging.INFO, logger="code_indexer.server.mcp.handlers._utils"
            ),
        ):
            result = _expand_wildcard_patterns([WILDCARD_PATTERN], user)

    assert len(result) == EXPANDED_REPO_COUNT, (
        f"Expected {EXPANDED_REPO_COUNT} expanded repos, got {len(result)}: {result}"
    )

    expansion_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and WILDCARD_PATTERN in r.getMessage()
    ]
    assert expansion_records, (
        f"Expected INFO log with pattern={WILDCARD_PATTERN!r}. "
        f"INFO records: {[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
    )
    msg = expansion_records[0].getMessage()

    assert CORRELATION_ID_EXPAND in msg, f"correlation_id missing: {msg}"
    assert WILDCARD_PATTERN in msg, f"pattern missing: {msg}"
    assert str(EXPANDED_REPO_COUNT) in msg, (
        f"matched_count={EXPANDED_REPO_COUNT} missing: {msg}"
    )


# ---------------------------------------------------------------------------
# Test 3 — _omni_search_code() post-expansion log
# ---------------------------------------------------------------------------


def test_omni_post_expansion_info_log_fires_with_all_fields(caplog):
    """_omni_search_code() must emit INFO after expansion with: correlation_id,
    user_id, expanded_count, and ALL expanded_aliases shown (first 10, elided if more).
    """
    from code_indexer.server.mcp.handlers.search import search_code

    params = {
        "repository_alias": [WILDCARD_PATTERN],
        "query_text": QUERY_NORMAL,
        "limit": LIMIT,
        "accuracy": ACCURACY,
    }
    user = _make_user("diana")

    fake_multi_response, omni_patches = _make_omni_patches()

    with (
        omni_patches[0],
        omni_patches[1],
        omni_patches[2],
        omni_patches[3],
        omni_patches[4],
        omni_patches[5],
        omni_patches[6],
        omni_patches[7],
        omni_patches[8],
        patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService",
            autospec=True,
        ) as mock_cls,
        caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers.search"),
    ):
        mock_svc = MagicMock()
        mock_svc.search.return_value = fake_multi_response
        mock_cls.return_value = mock_svc
        search_code(params, user)

    omni_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and CORRELATION_ID_OMNI in r.getMessage()
        and str(EXPANDED_REPO_COUNT) in r.getMessage()
    ]
    assert omni_records, (
        f"Expected omni post-expansion INFO log with correlation_id={CORRELATION_ID_OMNI!r} "
        f"and expanded_count={EXPANDED_REPO_COUNT}. "
        f"INFO records: {[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
    )
    msg = omni_records[0].getMessage()

    assert CORRELATION_ID_OMNI in msg, f"correlation_id missing: {msg}"
    assert "diana" in msg, f"user_id missing: {msg}"
    assert str(EXPANDED_REPO_COUNT) in msg, f"expanded_count missing: {msg}"
    # All 3 aliases must appear in the log (<=10 so no elision)
    for alias in EXPANDED_ALIASES:
        assert alias in msg, f"expanded alias {alias!r} missing in omni log: {msg}"


# ---------------------------------------------------------------------------
# Test 4 — search_code() exit log
# ---------------------------------------------------------------------------


def test_exit_log_fires_with_correlation_id_result_count_and_elapsed_ms(caplog):
    """INFO log at search_code() exit must include: correlation_id,
    result_count=<N> field pattern, and elapsed_ms as '<digits>ms'.
    """
    from code_indexer.server.mcp.handlers.search import search_code

    params = _single_repo_params()
    user = _make_user("eve")

    with (
        patch(
            "code_indexer.server.mcp.handlers.search._search_global_repo",
            return_value=_empty_search_response(),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search.get_correlation_id",
            return_value=CORRELATION_ID_EXIT,
        ),
        caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers.search"),
    ):
        search_code(params, user)

    exit_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and CORRELATION_ID_EXIT in r.getMessage()
        and re.search(r"\d+\s*ms", r.getMessage())
    ]
    assert exit_records, (
        f"Expected exit INFO log with correlation_id={CORRELATION_ID_EXIT!r} and elapsed_ms pattern. "
        f"INFO records: {[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
    )
    msg = exit_records[0].getMessage()

    assert CORRELATION_ID_EXIT in msg, f"correlation_id missing: {msg}"

    # result_count must appear as a field=value pattern (e.g. "result_count=0")
    assert re.search(r"result_count\s*=\s*0", msg), (
        f"result_count=0 field pattern missing in exit log: {msg}"
    )

    # elapsed_ms must appear as digits followed by ms
    assert re.search(r"\d+\s*ms", msg), f"elapsed_ms pattern missing in exit log: {msg}"
