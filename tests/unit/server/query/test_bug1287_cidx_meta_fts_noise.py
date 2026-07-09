"""
Unit tests for Bug #1287 Defect B — cidx-meta-global FTS ERROR/WARNING log noise.

An implicit (no explicit repository_alias) omni/fan-out search over
`query_user_repositories` iterates every visible repository. When the search
mode requires an FTS index (fts/hybrid) and the fan-out includes the internal,
auto-bootstrapped `cidx-meta-global` bookkeeping repo (a git-metadata store
that is never FTS-indexed by design -- it holds AI-generated descriptions and
dependency-map markdown, not real user source code), the per-repo FTS lookup
raises `SemanticQueryError("FTS index not available for repository "
"'cidx-meta-global'. ...")`. This propagates through three log sites:

    1. [QUERY-MIGRATE-008] ERROR in `_search_single_repository`'s broad
       except-and-reraise (semantic_query_manager.py).
    2. [QUERY-MIGRATE-006] WARNING in `_perform_search`'s per-repo
       catch-and-continue loop (semantic_query_manager.py).
    3. "Error in search_code: ..." ERROR in the MCP handler's outer
       exception logger (mcp/handlers/search.py) -- only reached when
       cidx-meta-global is the sole (or only failing) repo, so the
       exception propagates all the way up.

This is real production log noise for a benign, by-design condition. These
tests prove:

  - An implicit FTS/hybrid fan-out that only sees cidx-meta* bookkeeping
    repos produces NO WARNING/ERROR log noise and completes without raising.
  - A genuine FTS failure for a REAL (non cidx-meta*) repo during the same
    kind of fan-out is NOT masked -- it still logs QUERY-MIGRATE-006/008 and
    still surfaces as the terminal error when it is the only repo searched.
  - An EXPLICIT single-target query for repository_alias='cidx-meta-global'
    with search_mode='fts' still fails loud (informative error), because the
    user directly asked for that specific repo/mode combination.
  - A mixed fan-out (one cidx-meta* repo + one real repo, both missing an
    FTS index) skips the cidx-meta* repo silently while still surfacing the
    real repo's genuine failure.

No mocking of the code under test: real temporary directories on disk stand
in for repository paths, and the real `_perform_search` / `_execute_fts_search`
implementation runs unmodified. Only `activated_repo_manager` /
`background_job_manager` collaborators are test doubles (per the existing
`test_semantic_query_manager_global_repos_404.py` convention in this package).
"""

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    SemanticQueryError,
)

LOGGER_NAME = "code_indexer.server.query.semantic_query_manager"


@pytest.fixture
def temp_dirs():
    """Create temporary directories for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir) / "data"
        activated_repos_dir = data_dir / "activated-repos"
        activated_repos_dir.mkdir(parents=True, exist_ok=True)
        yield {
            "data_dir": str(data_dir),
            "activated_repos_dir": str(activated_repos_dir),
        }


def _make_repo_dir(base: Path, name: str) -> Path:
    """Create a real repository directory on disk with NO FTS index."""
    repo_dir = base / name
    (repo_dir / ".code-indexer").mkdir(parents=True, exist_ok=True)
    return repo_dir


@pytest.fixture
def activated_repo_manager_mock(temp_dirs):
    """Mock activated repo manager; list_activated_repositories is set per-test."""
    mock = MagicMock()
    mock.activated_repos_dir = temp_dirs["activated_repos_dir"]
    mock.list_activated_repositories.return_value = []
    return mock


@pytest.fixture
def semantic_query_manager(temp_dirs, activated_repo_manager_mock):
    """Real SemanticQueryManager (no mocking of _perform_search itself)."""
    return SemanticQueryManager(
        data_dir=temp_dirs["data_dir"],
        activated_repo_manager=activated_repo_manager_mock,
        background_job_manager=MagicMock(),
    )


def _warning_or_error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestCidxMetaFtsFanoutLogNoise:
    """Defect B: implicit FTS fan-out over cidx-meta* must not log noise."""

    def test_cidx_meta_global_only_fts_fanout_no_log_noise(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
        caplog,
    ):
        """The exact log_audit_gate.py repro: only cidx-meta-global visible,
        implicit (no repository_alias) FTS search — must not raise and must
        not log any WARNING/ERROR referencing cidx-meta-global."""
        base = Path(temp_dirs["data_dir"])
        meta_dir = _make_repo_dir(base, "cidx-meta-global")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "cidx-meta-global", "repo_path": str(meta_dir)}
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="test",
                search_mode="fts",
                limit=10,
            )

        assert result["total_results"] == 0
        noisy = [
            r
            for r in _warning_or_error_records(caplog)
            if "cidx-meta-global" in r.getMessage()
        ]
        assert noisy == [], (
            f"Expected zero WARNING/ERROR log records mentioning "
            f"cidx-meta-global, got: {[r.getMessage() for r in noisy]}"
        )

    def test_real_repo_missing_fts_index_still_logged_in_fanout(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
        caplog,
    ):
        """A genuine (non cidx-meta*) repo missing its FTS index during the
        same style of implicit fan-out must NOT be masked: it still logs
        QUERY-MIGRATE-006/008 and still surfaces as the terminal error."""
        base = Path(temp_dirs["data_dir"])
        real_dir = _make_repo_dir(base, "real-repo-alpha")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "real-repo-alpha", "repo_path": str(real_dir)}
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            with pytest.raises(SemanticQueryError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    search_mode="fts",
                    limit=10,
                )

        assert "real-repo-alpha" in str(exc_info.value)

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "QUERY-MIGRATE-006" in r.getMessage()
        ]
        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "QUERY-MIGRATE-008" in r.getMessage()
        ]
        assert warning_records, "Expected QUERY-MIGRATE-006 WARNING to still fire"
        assert error_records, "Expected QUERY-MIGRATE-008 ERROR to still fire"
        assert any("real-repo-alpha" in r.getMessage() for r in warning_records)
        assert any("real-repo-alpha" in r.getMessage() for r in error_records)

    def test_explicit_cidx_meta_global_fts_query_still_fails_loud(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
    ):
        """An EXPLICIT single-target FTS query for cidx-meta-global must still
        surface the 'FTS index not available' error to the caller -- the skip
        only applies to the implicit no-alias fan-out, never to a direct ask."""
        base = Path(temp_dirs["data_dir"])
        meta_dir = _make_repo_dir(base, "cidx-meta-global")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "cidx-meta-global", "repo_path": str(meta_dir)}
        ]

        with pytest.raises(SemanticQueryError) as exc_info:
            semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="test",
                repository_alias="cidx-meta-global",
                search_mode="fts",
                limit=10,
            )

        assert "FTS index not available for repository 'cidx-meta-global'" in str(
            exc_info.value
        )

    def test_mixed_fanout_skips_only_cidx_meta_but_surfaces_real_repo_error(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
        caplog,
    ):
        """One implicit fan-out call with BOTH a cidx-meta* repo and a real
        repo (both missing their FTS index): cidx-meta-global is silently
        skipped, but the real repo's genuine failure is not masked -- it is
        the sole surfaced error and the only one logged."""
        base = Path(temp_dirs["data_dir"])
        meta_dir = _make_repo_dir(base, "cidx-meta-global")
        real_dir = _make_repo_dir(base, "real-repo-beta")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "cidx-meta-global", "repo_path": str(meta_dir)},
            {"user_alias": "real-repo-beta", "repo_path": str(real_dir)},
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            with pytest.raises(SemanticQueryError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    search_mode="fts",
                    limit=10,
                )

        assert "real-repo-beta" in str(exc_info.value)
        noisy_meta = [
            r
            for r in _warning_or_error_records(caplog)
            if "cidx-meta-global" in r.getMessage()
        ]
        assert noisy_meta == [], (
            f"cidx-meta-global must be skipped silently in a mixed fan-out, "
            f"got: {[r.getMessage() for r in noisy_meta]}"
        )
        assert any(
            "real-repo-beta" in r.getMessage()
            for r in _warning_or_error_records(caplog)
        ), "Expected real-repo-beta's genuine FTS failure to still be logged"


class TestAnchoredPredicateDoesNotOverMatch:
    """Code-reviewer finding 1: the internal-meta-repo skip must be an
    ANCHORED/EXACT match, never a prefix/substring check. A repo whose real
    name merely starts with the same characters as the internal bookkeeping
    repo (e.g. 'cidx-metadata-global', 'cidx-meta-analytics-global') is a
    legitimate user repo and must still be searched in an implicit FTS
    fan-out -- never silently dropped."""

    def test_cidx_metadata_global_still_searched_in_fts_fanout(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
        caplog,
    ):
        base = Path(temp_dirs["data_dir"])
        real_dir = _make_repo_dir(base, "cidx-metadata-global")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "cidx-metadata-global", "repo_path": str(real_dir)}
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            with pytest.raises(SemanticQueryError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    search_mode="fts",
                    limit=10,
                )

        # A loose startswith("cidx-meta") check would have silently excluded
        # this repo from the fan-out entirely, leaving 0 repos and raising
        # "No activated repositories found" instead -- the giveaway of the bug.
        assert "cidx-metadata-global" in str(exc_info.value)
        assert any(
            "cidx-metadata-global" in r.getMessage()
            for r in _warning_or_error_records(caplog)
        ), (
            "Expected cidx-metadata-global's genuine FTS failure to be logged (not skipped)"
        )

    def test_cidx_meta_analytics_global_still_searched_in_fts_fanout(
        self,
        semantic_query_manager,
        activated_repo_manager_mock,
        temp_dirs,
        caplog,
    ):
        base = Path(temp_dirs["data_dir"])
        real_dir = _make_repo_dir(base, "cidx-meta-analytics-global")
        activated_repo_manager_mock.list_activated_repositories.return_value = [
            {"user_alias": "cidx-meta-analytics-global", "repo_path": str(real_dir)}
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            with pytest.raises(SemanticQueryError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    search_mode="fts",
                    limit=10,
                )

        assert "cidx-meta-analytics-global" in str(exc_info.value)
        assert any(
            "cidx-meta-analytics-global" in r.getMessage()
            for r in _warning_or_error_records(caplog)
        ), (
            "Expected cidx-meta-analytics-global's genuine FTS failure to be logged (not skipped)"
        )
