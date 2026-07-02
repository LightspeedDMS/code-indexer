"""Story #905 — Wire CLI temporal path through the unified rerank funnel.

Tests confirm that after _execute_temporal_fusion returns results, the CLI
applies _apply_cli_rerank_and_filter when --rerank-query is supplied, and
skips it (returning original order) when no rerank flag is given.

Test naming (mirrors Bundle 4 convention):
  1. test_temporal_rerank_query_flag_reorders_results
  2. test_temporal_results_returned_in_original_order_without_rerank_query
  3. test_temporal_rerank_uses_cohere_when_voyage_unavailable
  4. test_temporal_sinbinned_voyage_returns_original_order

Anti-mock compliance (MESSI Rule 1):
  - ProviderHealthMonitor: real instance (tmp_path persistence).
  - CliRerankConfigService: real instance wrapping real GlobalCliConfig.
  - Reranker HTTP boundary patched at VoyageRerankerClient._post (returns
    MagicMock(spec=httpx.Response)) -- same pattern as test_cli_query_rerank_and_chain.
  - Temporal fusion patched at execute_temporal_query_with_fusion (external I/O
    boundary; real implementation needs live VoyageAI + filesystem index).
  - ConfigManager.create_with_backtrack patched to inject deterministic config.
  - FilesystemVectorStore.__init__ patched (needs real filesystem collections).
"""

import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import code_indexer.server.clients.reranker_clients as rc_module

from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VOYAGE_SENTINEL = "test-voyage-key-story905"
_COHERE_SENTINEL = "test-cohere-key-story905"

SINBIN_DURATION_SECONDS = 3600


# ---------------------------------------------------------------------------
# Helpers: test-data builders
# ---------------------------------------------------------------------------


def _make_temporal_result(
    file_path: str, score: float, content: str = "def example(): pass"
) -> TemporalSearchResult:
    """Build a minimal TemporalSearchResult with commit_diff metadata."""
    return TemporalSearchResult(
        file_path=file_path,
        chunk_index=0,
        content=content,
        score=score,
        metadata={"type": "commit_diff", "commit_hash": "abc1234"},
        temporal_context={"commit_date": "2025-01-01", "author_name": "Test"},
    )


def _make_temporal_results(
    file_paths_and_scores: List[Tuple[str, float]],
) -> TemporalSearchResults:
    """Build a TemporalSearchResults from (file_path, score) pairs."""
    results = [_make_temporal_result(fp, score) for fp, score in file_paths_and_scores]
    return TemporalSearchResults(
        results=results,
        query="test query",
        filter_type="time_range",
        filter_value=("1970-01-01", "2100-12-31"),
        total_found=len(results),
    )


def _ordered_files(output: str) -> List[str]:
    """Extract file paths from quiet-mode commit_diff output lines.

    Each commit_diff line has format: '{n}. {score:.3f} {file_path}'
    Returns file paths in output order (top result first).
    """
    files: List[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith(".") and parts[0][:-1].isdigit():
            try:
                float(parts[1])
                files.append(parts[2])
            except ValueError:
                pass
    return files


def _make_voyage_rerank_response(
    body: Dict[str, Any], *, reverse_order: bool = False
) -> MagicMock:
    """Build an httpx.Response mock for the Voyage rerank API.

    Voyage response shape: {"data": [{"index": int, "relevance_score": float}]}.
    reverse_order=False: scores descend (doc[0] highest — original order).
    reverse_order=True:  scores ascend (last doc highest — full reversal).
    """
    docs = body.get("documents", [])
    top_k = body.get("top_k", len(docs))
    n = min(top_k, len(docs))
    if reverse_order:
        data = [
            {"index": len(docs) - 1 - i, "relevance_score": float(n - i) / n}
            for i in range(n)
        ]
    else:
        data = [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(n)]
    data.sort(key=lambda x: x["relevance_score"], reverse=True)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"data": data}
    resp.raise_for_status = MagicMock()
    return resp


def _make_cohere_rerank_response(
    body: Dict[str, Any], *, reverse_order: bool = False
) -> MagicMock:
    """Build an httpx.Response mock for the Cohere rerank API.

    Cohere response shape: {"results": [{"index": int, "relevance_score": float}]}.
    reverse_order=False: scores descend (doc[0] highest — original order).
    reverse_order=True:  scores ascend (last doc highest — full reversal).
    """
    docs = body.get("documents", [])
    top_k = body.get("top_k", len(docs))
    n = min(top_k, len(docs))
    if reverse_order:
        results = [
            {"index": len(docs) - 1 - i, "relevance_score": float(n - i) / n}
            for i in range(n)
        ]
    else:
        results = [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(n)]
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"results": results}
    resp.raise_for_status = MagicMock()
    return resp


def _write_sinbin_file(path: Path, providers: List[str]) -> None:
    """Pre-populate a sinbin persistence file with sin-binned providers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        p: {
            "sinbin_until_wall_seconds": time.time() + SINBIN_DURATION_SECONDS,
            "last_failure_kind": "sinbin",
        }
        for p in providers
    }
    path.write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers: patch-list builder + raw invocation
# ---------------------------------------------------------------------------


def _build_infra_patches(
    tmp_project: Path,
    shim: Any,
    monitor: Any,
    fusion_results: TemporalSearchResults,
) -> List[Any]:
    """Return the shared infrastructure patch list for CLI temporal tests.

    All patches are context managers, not yet entered.
    """
    mock_config = MagicMock()
    mock_config.codebase_dir = tmp_project
    mock_config.embedding_provider = "voyage-ai"
    mock_config.voyage_api = MagicMock(api_key=_VOYAGE_SENTINEL)
    mock_config.filesystem = MagicMock(port=6333)
    mock_config.daemon = MagicMock(enabled=False)
    mock_config.vector_store = (
        None  # triggers FilesystemBackend default in BackendFactory
    )

    mock_cm = MagicMock()
    mock_cm.get_config.return_value = mock_config
    mock_cm.load.return_value = mock_config
    mock_cm.get_daemon_config.return_value = {"enabled": False}

    # Build a mock FilesystemVectorStore instance with base_path set so attribute
    # access inside the temporal path does not raise AttributeError.
    mock_vector_store = MagicMock()
    mock_vector_store.base_path = tmp_project / ".code-indexer" / "index"

    # Class-level patch: FilesystemVectorStore(base_path=...) returns mock_vector_store.
    # Patched at the defining module because cli.py imports it lazily inside the function
    # body (so the module-level name in cli.py does not exist at import time).
    mock_fsvs_class = MagicMock(return_value=mock_vector_store)

    return [
        patch(
            "code_indexer.cli.ConfigManager.create_with_backtrack",
            return_value=mock_cm,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion",
            return_value=fusion_results,
        ),
        patch(
            "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
            mock_fsvs_class,
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service"
            ".TemporalSearchService.has_temporal_index",
            return_value=True,
        ),
        patch.object(rc_module, "get_config_service", return_value=shim),
        patch(
            "code_indexer.services.provider_health_monitor"
            ".ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ),
    ]


def _invoke_cli(
    runner: CliRunner,
    tmp_project: Path,
    extra_args: List[str],
) -> Any:
    """Invoke `cidx query ... --time-range-all --quiet` from tmp_project cwd."""
    from code_indexer.cli import cli

    old_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_project))
        return runner.invoke(
            cli,
            ["query", "test query", "--time-range-all", "--quiet"] + extra_args,
        )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Fixtures: environment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset ProviderHealthMonitor singleton before/after each test."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture(autouse=True)
def _set_voyage_env(monkeypatch):
    """Ensure VOYAGE_API_KEY is set for all tests in this module."""
    monkeypatch.setenv("VOYAGE_API_KEY", _VOYAGE_SENTINEL)


@pytest.fixture
def _set_cohere_env(monkeypatch):
    """Set CO_API_KEY — injected only into tests that need Cohere."""
    monkeypatch.setenv("CO_API_KEY", _COHERE_SENTINEL)


# ---------------------------------------------------------------------------
# Fixtures: infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def tmp_project(tmp_path):
    """Minimal project layout with config + stub temporal index dir."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(parents=True)
    index_dir = config_dir / "index"
    index_dir.mkdir()

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "codebase_dir": str(tmp_path),
                "filesystem": {"port": 6333, "grpc_port": 6334},
                "voyage_api": {"api_key": _VOYAGE_SENTINEL},
                "embedding_provider": "voyage",
            }
        ),
        encoding="utf-8",
    )

    temporal_dir = index_dir / "code-indexer-temporal"
    temporal_dir.mkdir()
    (temporal_dir / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "code-indexer-temporal",
                "vector_count": 2,
                "file_count": 2,
                "indexed_at": "2025-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def _shim():
    """Real CliRerankConfigService wrapping real GlobalCliConfig."""
    from code_indexer.config_global import GlobalCliConfig, RerankSettings
    from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService

    global_cfg = GlobalCliConfig(
        rerank=RerankSettings(
            auto_populate_rerank_query=False,
            overfetch_multiplier=2,
            preferred_vendor_order=["voyage", "cohere"],
            voyage_reranker_model="rerank-2.5",
            cohere_reranker_model="rerank-v3.5",
        )
    )
    return CliRerankConfigService(global_cfg)


@pytest.fixture
def _monitor(tmp_path):
    """Real ProviderHealthMonitor — no providers sinbinned."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    return ProviderHealthMonitor(persistence_path=tmp_path / "sinbin.json")


@pytest.fixture
def _sinbinned_monitor(tmp_path):
    """Real ProviderHealthMonitor with 'voyage-reranker' pre-sinbinned."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    sinbin_path = tmp_path / "sinbin_voyage.json"
    _write_sinbin_file(sinbin_path, ["voyage-reranker"])
    return ProviderHealthMonitor(persistence_path=sinbin_path)


# ---------------------------------------------------------------------------
# Unified invocation fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def invoke_cli(runner, tmp_project, _shim, _monitor):
    """Return a callable that drives the CLI temporal path with all infra patched.

    Callable signature:
        run(
            fusion_results,
            extra_http_patches=None,
            extra_args=None,
            monitor=None,          # defaults to _monitor (healthy)
        ) -> CliResult
    """

    def _run(
        fusion_results: TemporalSearchResults,
        extra_http_patches: Optional[List[Any]] = None,
        extra_args: Optional[List[str]] = None,
        monitor: Optional[Any] = None,
    ) -> Any:
        effective_monitor = monitor if monitor is not None else _monitor
        infra = _build_infra_patches(
            tmp_project, _shim, effective_monitor, fusion_results
        )
        http = extra_http_patches or []
        args = extra_args or []
        with ExitStack() as stack:
            for p in infra + http:
                stack.enter_context(p)
            return _invoke_cli(runner, tmp_project, args)

    return _run


# ---------------------------------------------------------------------------
# Test 1: --rerank-query reverses result ordering via Voyage reranker.
#
# Fusion returns [auth.py (0.9), login.py (0.8)].
# Voyage _post stub assigns higher score to login.py -> reversal.
# Assertion: output order becomes [login.py, auth.py].
# ---------------------------------------------------------------------------


def test_temporal_rerank_query_flag_reorders_results(invoke_cli):
    """--rerank-query routes temporal results through the rerank funnel.

    Fusion returns auth.py first; Voyage scores login.py higher -> reversal.
    """
    fusion = _make_temporal_results([("auth.py", 0.9), ("login.py", 0.8)])
    voyage_stub = patch.object(
        rc_module.VoyageRerankerClient,
        "_post",
        lambda self_c, body: _make_voyage_rerank_response(body, reverse_order=True),
    )

    result = invoke_cli(
        fusion,
        extra_http_patches=[voyage_stub],
        extra_args=["--rerank-query", "authentication login flow"],
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    files = _ordered_files(result.output)
    assert files[:2] == ["login.py", "auth.py"], (
        f"Expected rerank reversal [login.py, auth.py], got {files}"
    )


# ---------------------------------------------------------------------------
# Test 2: No --rerank-query -> results returned in original fusion order.
#
# Fusion returns [auth.py, login.py]. No reranker stub needed.
# Assertion: output preserves [auth.py, login.py].
# ---------------------------------------------------------------------------


def test_temporal_results_returned_in_original_order_without_rerank_query(invoke_cli):
    """Without --rerank-query, temporal results preserve fusion order."""
    fusion = _make_temporal_results([("auth.py", 0.9), ("login.py", 0.8)])

    result = invoke_cli(fusion)

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    files = _ordered_files(result.output)
    assert files[:2] == ["auth.py", "login.py"], (
        f"Expected original order [auth.py, login.py], got {files}"
    )


# ---------------------------------------------------------------------------
# Test 3: --rerank-query with Cohere (VOYAGE_API_KEY removed, CO_API_KEY set).
#
# Voyage is unavailable (key removed via monkeypatch.delenv) so the reranker
# chain falls through to Cohere. Cohere _post stub reverses order.
# Assertion: output becomes [login.py, auth.py].
# ---------------------------------------------------------------------------


def test_temporal_rerank_uses_cohere_when_voyage_unavailable(
    invoke_cli, _set_cohere_env, _sinbinned_monitor
):
    """--rerank-query selects Cohere when Voyage reranker is sinbinned.

    voyage-reranker is pre-sinbinned via _sinbinned_monitor so the chain skips
    Voyage and falls through to Cohere.  Cohere _post stub uses the correct
    Cohere response shape ({"results": [...]}) and reverses result order.
    """
    fusion = _make_temporal_results([("auth.py", 0.9), ("login.py", 0.8)])
    cohere_stub = patch.object(
        rc_module.CohereRerankerClient,
        "_post",
        lambda self_c, body: _make_cohere_rerank_response(body, reverse_order=True),
    )

    result = invoke_cli(
        fusion,
        extra_http_patches=[cohere_stub],
        extra_args=["--rerank-query", "authentication login flow"],
        monitor=_sinbinned_monitor,
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    files = _ordered_files(result.output)
    assert files[:2] == ["login.py", "auth.py"], (
        f"Expected Cohere rerank reversal [login.py, auth.py], got {files}"
    )


# ---------------------------------------------------------------------------
# Test 4: Voyage sin-binned -> rerank stage skips gracefully, original order.
#
# _sinbinned_monitor has voyage-reranker pre-sinbinned.
# No Cohere key set -> reranker finds no available provider -> graceful skip.
# Assertion: original order [auth.py, login.py] preserved.
# ---------------------------------------------------------------------------


def test_temporal_sinbinned_voyage_returns_original_order(
    invoke_cli, _sinbinned_monitor
):
    """Sinbinned Voyage + no Cohere key -> graceful rerank skip -> original order."""
    fusion = _make_temporal_results([("auth.py", 0.9), ("login.py", 0.8)])

    result = invoke_cli(
        fusion,
        extra_args=["--rerank-query", "authentication login flow"],
        monitor=_sinbinned_monitor,
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"
    files = _ordered_files(result.output)
    assert files[:2] == ["auth.py", "login.py"], (
        f"Expected original order preserved when provider sinbinned, got {files}"
    )
