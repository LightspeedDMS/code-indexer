"""Hot-path INFO-log removal guard (py-spy logging-lock follow-up to Bug #1078).

py-spy under concurrent /api/query load showed ~5 logger.info() calls PER QUERY
on the semantic-search serving path, each acquiring the per-Handler lock. These
per-request INFO logs are removed (or demoted to debug) so the hot path no longer
pays the logging cost.

These tests assert:
1. Behavioral: a semantic search invocation emits NO INFO records from
   search_service, while still emitting the orphaned-repo WARNING it should.
2. Source guards: the specific removed INFO message strings are gone, and the
   WARNING/ERROR diagnostic logs are preserved (scope discipline -- Messi #9).
"""

from __future__ import annotations

import logging
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_SEARCH_SERVICE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "search_service.py"
)
_FILESYSTEM_BACKEND_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "backends" / "filesystem_backend.py"
)

_SEARCH_SERVICE_LOGGER = "code_indexer.server.services.search_service"


# ---------------------------------------------------------------------------
# Behavioral: no INFO from the search-serving path; WARNING still fires
# ---------------------------------------------------------------------------


def test_semantic_search_emits_no_info_but_emits_warning_on_invalid_repo(
    caplog, tmp_path, monkeypatch
) -> None:
    """Driving the real search path on a repo with no valid index must:

    - emit ZERO INFO records from search_service (the per-query INFO logs were
      removed), and
    - emit the orphaned-repo WARNING (MCP-GENERAL-171) -- proving we removed only
      the INFO noise, not the diagnostic WARNING.

    We deterministically trigger the orphaned-repo graceful path by making the
    repository config load raise ValueError (a bare temp dir would backtrack to
    the surrounding project's real config and hit unrelated app.state wiring).
    """
    import code_indexer.server.services.search_service as ss
    from code_indexer.server.services.search_service import SemanticSearchService

    def _raise_value_error(_path):
        raise ValueError("no valid .code-indexer config (orphaned repo)")

    monkeypatch.setattr(
        ss.ConfigManager, "create_with_backtrack", staticmethod(_raise_value_error)
    )

    svc = SemanticSearchService()
    invalid_repo = tmp_path / "no_index_repo"
    invalid_repo.mkdir()

    with caplog.at_level(logging.INFO, logger=_SEARCH_SERVICE_LOGGER):
        results = svc._perform_semantic_search(
            repo_path=str(invalid_repo),
            query="anything",
            limit=5,
            include_source=False,
        )

    assert results == []

    info_records = [
        r
        for r in caplog.records
        if r.name == _SEARCH_SERVICE_LOGGER and r.levelno == logging.INFO
    ]
    assert info_records == [], (
        "search_service must emit NO INFO records on the query serving path "
        f"(found: {[r.getMessage() for r in info_records]})"
    )

    warning_records = [
        r
        for r in caplog.records
        if r.name == _SEARCH_SERVICE_LOGGER and r.levelno == logging.WARNING
    ]
    assert any("MCP-GENERAL-171" in r.getMessage() for r in warning_records), (
        "orphaned-repo WARNING (MCP-GENERAL-171) must still be emitted -- only "
        "the per-query INFO logs were removed, not the diagnostic WARNING."
    )


# ---------------------------------------------------------------------------
# Source guards: removed INFO strings gone; WARNING/ERROR preserved
# ---------------------------------------------------------------------------


def test_removed_hotpath_info_strings_absent_from_search_service() -> None:
    source = _SEARCH_SERVICE_PATH.read_text()
    removed = [
        "Loaded repository config from",
        "Using backend:",
        "Using collection:",
        'results"',  # the f"Found {len(...)} results" INFO
    ]
    for needle in removed:
        # These strings must no longer appear inside a logger.info(...) call.
        # We assert the whole literal is gone (the f-strings were unique).
        if needle == 'results"':
            assert "Found {len(search_results)} results" not in source, (
                "hot-path INFO 'Found N results' must be removed from search_service"
            )
        else:
            assert needle not in source, (
                f"hot-path INFO string {needle!r} must be removed from search_service"
            )


def test_search_service_keeps_warning_and_error_logs() -> None:
    source = _SEARCH_SERVICE_PATH.read_text()
    assert "MCP-GENERAL-171" in source, "orphaned-repo WARNING must be preserved"
    assert "MCP-GENERAL-170" in source, "search-failure ERROR must be preserved"
    assert "logger.warning(" in source
    assert "logger.error(" in source


_SEARCH_HANDLER_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "handlers" / "search.py"
)


def test_search_code_handler_audit_logs_demoted_to_debug() -> None:
    """The Bug #881 per-call audit logs must be DEBUG, not INFO.

    These carry deliberate operator-audit value (every search_code call), so they
    are demoted rather than deleted -- but they must not be INFO, or they would
    acquire the logging handler lock on the hot path at default log levels.
    """
    source = _SEARCH_HANDLER_PATH.read_text()

    # Locate each audit line and assert the logger call immediately preceding the
    # message string is .debug( not .info(.
    for marker in (
        "search_code entry:",
        "search_code complete:",
        "_omni_search_code post-expansion:",
    ):
        idx = source.find(marker)
        assert idx != -1, f"audit log {marker!r} not found in search.py"
        preceding = source[:idx]
        debug_pos = preceding.rfind("logger.debug(")
        info_pos = preceding.rfind("logger.info(")
        assert debug_pos > info_pos, (
            f"audit log {marker!r} must use logger.debug(...) (py-spy hot-path "
            f"fix), not logger.info(...)."
        )


def test_hnsw_caching_info_removed_from_filesystem_backend() -> None:
    source = _FILESYSTEM_BACKEND_PATH.read_text()
    assert "HNSW index caching enabled (server mode)" not in source, (
        "per-query INFO 'HNSW index caching enabled (server mode)' must be removed "
        "from filesystem_backend (fires once per backend construction = per query)."
    )
