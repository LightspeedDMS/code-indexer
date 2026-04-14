"""
Unit tests for Bug #679 AC2-AC4: Per-provider isolation in CLI temporal indexing loop.

Covers:
- test_provider_failure_does_not_abort_other_providers
- test_exit_code_2_on_partial_failure
- test_exit_code_0_on_full_success
- test_exit_code_1_on_total_failure
- test_provider_results_file_written_atomically (via os.replace spy)
- test_provider_results_contains_per_provider_breakdown (end-to-end helper call)
- test_failed_provider_metadata_state_set_to_failed

Tests drive run_extra_provider_temporal_loop() from cli_provider_loop, which
encapsulates the per-provider loop logic so it can be unit tested independently.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.cli_provider_loop import (
    ProviderResult,
    run_extra_provider_temporal_loop,
    write_provider_results_atomic,
    compute_exit_code,
)
from src.code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexing_result(total_commits=5):
    """Return a fake TemporalIndexingResult-like object."""
    r = MagicMock()
    r.total_commits = total_commits
    r.files_processed = 10
    r.approximate_vectors_created = 50
    r.skip_ratio = 0.0
    r.branches_indexed = ["main"]
    return r


def _make_config(base_path: Path, primary_provider: str = "voyage-ai") -> MagicMock:
    """Return a minimal mock config with portable codebase_dir."""
    cfg = MagicMock()
    cfg.embedding_provider = primary_provider
    cfg.codebase_dir = base_path
    return cfg


def _healthy_epf() -> MagicMock:
    """Return a mock EmbeddingProviderFactory whose providers always pass health check."""
    epf = MagicMock()
    epf.create.return_value = MagicMock(health_check=MagicMock(return_value=True))
    return epf


def _unhealthy_epf() -> MagicMock:
    """Return a mock EmbeddingProviderFactory whose providers always fail health check."""
    epf = MagicMock()
    epf.create.return_value = MagicMock(health_check=MagicMock(return_value=False))
    return epf


def _invoke_loop(
    tmp_path: Path,
    extra_providers: list,
    indexer_factory,
    epf=None,
):
    """Invoke run_extra_provider_temporal_loop with standard test arguments."""
    (tmp_path / ".code-indexer").mkdir(exist_ok=True)
    return run_extra_provider_temporal_loop(
        extra_providers=extra_providers,
        config=_make_config(tmp_path),
        config_manager=MagicMock(),
        vector_store=MagicMock(),
        indexer_factory=indexer_factory,
        embedding_factory=epf or _healthy_epf(),
        resolve_collection_fn=MagicMock(return_value="test-collection"),
        progress_callback=None,
        all_branches=False,
        max_commits=None,
        since_date=None,
        reconcile=False,
        repo_path=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Tests: provider isolation (AC2)
# ---------------------------------------------------------------------------


class TestProviderIsolation:
    """AC2: one failing provider must not abort subsequent providers."""

    def test_provider_failure_does_not_abort_other_providers(self, tmp_path):
        """When voyage-ai raises, cohere must still run and succeed."""
        cohere_ran = [False]
        call_order = []

        def indexer_factory(config_manager, vector_store, collection_name):
            call_order.append(len(call_order))
            idx = MagicMock()
            if len(call_order) == 1:
                # First call = voyage-ai -> raise
                idx.index_commits.side_effect = RuntimeError("voyage-ai API down")
                idx.temporal_dir = tmp_path / "temporal" / "voyage-ai"
            else:
                # Second call = cohere -> succeed
                def run(**kw):
                    cohere_ran[0] = True
                    return _make_indexing_result()

                idx.index_commits.side_effect = run
                idx.temporal_dir = tmp_path / "temporal" / "cohere"
            idx.temporal_dir.mkdir(parents=True, exist_ok=True)
            idx.close.return_value = None
            return idx

        results = _invoke_loop(tmp_path, ["voyage-ai", "cohere"], indexer_factory)

        assert cohere_ran[0], (
            "cohere must run even after voyage-ai raises. "
            "Bug #679 AC2: per-provider exception isolation."
        )
        assert results["voyage-ai"].status == "failed"
        assert results["cohere"].status == "success"

    def test_failed_provider_result_contains_error_message(self, tmp_path):
        """AC2: failed provider result captures the exception message."""

        def indexer_factory(config_manager, vector_store, collection_name):
            idx = MagicMock()
            idx.index_commits.side_effect = RuntimeError("Connection refused")
            idx.temporal_dir = tmp_path / "temporal" / "voyage-ai"
            idx.temporal_dir.mkdir(parents=True, exist_ok=True)
            idx.close.return_value = None
            return idx

        results = _invoke_loop(tmp_path, ["voyage-ai"], indexer_factory)

        assert results["voyage-ai"].status == "failed"
        assert "Connection refused" in (results["voyage-ai"].error or "")

    def test_health_check_failure_produces_skipped_result(self, tmp_path):
        """Provider failing health check is recorded as 'skipped' in results."""

        def indexer_factory(config_manager, vector_store, collection_name):
            pytest.fail("indexer_factory must not be called for unhealthy provider")

        results = _invoke_loop(
            tmp_path, ["voyage-ai"], indexer_factory, epf=_unhealthy_epf()
        )

        assert "voyage-ai" in results
        assert results["voyage-ai"].status == "skipped"


# ---------------------------------------------------------------------------
# Tests: exit code semantics (AC4)
# ---------------------------------------------------------------------------


class TestExitCodeSemantics:
    """AC4: compute_exit_code() returns 0/1/2 per spec."""

    def test_exit_code_0_on_full_success(self):
        """All providers succeeded -> 0."""
        results = {
            "voyage-ai": ProviderResult(status="success"),
            "cohere": ProviderResult(status="success"),
        }
        assert compute_exit_code(results) == 0

    def test_exit_code_1_on_total_failure(self):
        """All providers failed -> 1."""
        results = {
            "voyage-ai": ProviderResult(status="failed"),
            "cohere": ProviderResult(status="failed"),
        }
        assert compute_exit_code(results) == 1

    def test_exit_code_2_on_partial_failure(self):
        """Some succeeded, some failed -> 2."""
        results = {
            "voyage-ai": ProviderResult(status="success"),
            "cohere": ProviderResult(status="failed"),
        }
        assert compute_exit_code(results) == 2

    def test_exit_code_0_single_success(self):
        assert compute_exit_code({"voyage-ai": ProviderResult(status="success")}) == 0

    def test_exit_code_1_single_failure(self):
        assert compute_exit_code({"voyage-ai": ProviderResult(status="failed")}) == 1

    def test_exit_code_0_empty_results(self):
        """No providers -> 0 (nothing failed)."""
        assert compute_exit_code({}) == 0

    def test_skipped_providers_treated_as_success_for_exit_code(self):
        """Skipped providers (health-check fail) do not count as failures."""
        results = {
            "voyage-ai": ProviderResult(status="success"),
            "cohere": ProviderResult(status="skipped"),
        }
        assert compute_exit_code(results) == 0

    def test_helper_returns_results_enabling_exit_code_computation(self, tmp_path):
        """run_extra_provider_temporal_loop returns a results dict usable with compute_exit_code."""

        def success_factory(config_manager, vector_store, collection_name):
            idx = MagicMock()
            idx.index_commits.return_value = _make_indexing_result()
            idx.temporal_dir = tmp_path / "temporal" / "voyage-ai"
            idx.temporal_dir.mkdir(parents=True, exist_ok=True)
            idx.close.return_value = None
            return idx

        results = _invoke_loop(tmp_path, ["voyage-ai"], success_factory)
        exit_code = compute_exit_code(results)
        assert exit_code == 0, (
            f"Helper returned results that produce exit_code={exit_code}, expected 0"
        )


# ---------------------------------------------------------------------------
# Tests: provider_results.json (AC3)
# ---------------------------------------------------------------------------


class TestProviderResultsFile:
    """AC3: provider_results.json written atomically with per-provider breakdown."""

    def test_provider_results_file_written_atomically(self, tmp_path):
        """write_provider_results_atomic uses os.replace for atomic write."""
        (tmp_path / ".code-indexer").mkdir()
        results = {"voyage-ai": ProviderResult(status="success", latency_seconds=10.0)}
        replace_calls = []
        original_replace = __import__("os").replace

        def spy_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        with patch("os.replace", side_effect=spy_replace):
            write_provider_results_atomic(str(tmp_path), results)

        assert len(replace_calls) == 1, (
            f"os.replace must be called exactly once for atomic write, got {len(replace_calls)}"
        )
        results_file = tmp_path / ".code-indexer" / "provider_results.json"
        assert results_file.exists()

    def test_no_temp_files_remain_after_write(self, tmp_path):
        """Atomic write leaves no temp files in .code-indexer/ after completion."""
        (tmp_path / ".code-indexer").mkdir()
        results = {"voyage-ai": ProviderResult(status="success")}
        write_provider_results_atomic(str(tmp_path), results)

        ci_dir = tmp_path / ".code-indexer"
        leftover = [f for f in ci_dir.iterdir() if f.name != "provider_results.json"]
        assert leftover == [], f"Temp files remain after atomic write: {leftover}"

    def test_helper_writes_provider_results_json_end_to_end(self, tmp_path):
        """run_extra_provider_temporal_loop writes provider_results.json to disk."""

        def success_factory(config_manager, vector_store, collection_name):
            idx = MagicMock()
            idx.index_commits.return_value = _make_indexing_result(total_commits=42)
            idx.temporal_dir = tmp_path / "temporal" / "voyage-ai"
            idx.temporal_dir.mkdir(parents=True, exist_ok=True)
            idx.close.return_value = None
            return idx

        _invoke_loop(tmp_path, ["voyage-ai"], success_factory)

        results_file = tmp_path / ".code-indexer" / "provider_results.json"
        assert results_file.exists(), (
            "provider_results.json must be written by the helper"
        )
        data = json.loads(results_file.read_text())
        assert "provider_results" in data
        assert "voyage-ai" in data["provider_results"]
        entry = data["provider_results"]["voyage-ai"]
        for key in (
            "status",
            "error",
            "latency_seconds",
            "files_indexed",
            "chunks_indexed",
        ):
            assert key in entry, f"provider_results entry missing key: {key!r}"
        assert entry["status"] == "success"

    def test_provider_results_contains_per_provider_breakdown(self, tmp_path):
        """Written file correctly separates successful and failed provider entries."""
        (tmp_path / ".code-indexer").mkdir()

        results = {
            "voyage-ai": ProviderResult(
                status="success",
                error=None,
                latency_seconds=142.3,
                files_indexed=4821,
                chunks_indexed=12451,
            ),
            "cohere": ProviderResult(
                status="failed",
                error="TimeoutError after 30s",
                latency_seconds=30.1,
                files_indexed=0,
                chunks_indexed=0,
            ),
        }
        write_provider_results_atomic(str(tmp_path), results)

        data = json.loads(
            (tmp_path / ".code-indexer" / "provider_results.json").read_text()
        )
        pr = data["provider_results"]

        assert pr["voyage-ai"]["status"] == "success"
        assert pr["voyage-ai"]["error"] is None
        assert pr["voyage-ai"]["files_indexed"] == 4821

        assert pr["cohere"]["status"] == "failed"
        assert pr["cohere"]["error"] == "TimeoutError after 30s"
        assert pr["cohere"]["files_indexed"] == 0


# ---------------------------------------------------------------------------
# Tests: metadata state on failure (AC2)
# ---------------------------------------------------------------------------


class TestFailedProviderMetadataState:
    """AC2: failed provider sets TemporalProgressiveMetadata state to 'failed'."""

    def test_failed_provider_metadata_state_set_to_failed(self, tmp_path):
        """On provider exception, the helper sets metadata state to 'failed'."""
        temporal_dir = tmp_path / "temporal" / "voyage-ai"
        temporal_dir.mkdir(parents=True, exist_ok=True)
        metadata = TemporalProgressiveMetadata(temporal_dir)
        assert metadata.get_state() == "idle"

        def failing_factory(config_manager, vector_store, collection_name):
            idx = MagicMock()
            idx.index_commits.side_effect = RuntimeError("Simulated failure")
            idx.temporal_dir = temporal_dir
            idx.close.return_value = None
            return idx

        _invoke_loop(tmp_path, ["voyage-ai"], failing_factory)

        assert metadata.get_state() == "failed", (
            "TemporalProgressiveMetadata must be in 'failed' state after provider exception. "
            "Bug #679 AC2."
        )

    def test_successful_provider_does_not_set_failed_state(self, tmp_path):
        """Successful provider leaves metadata in non-failed state."""
        temporal_dir = tmp_path / "temporal" / "voyage-ai"
        temporal_dir.mkdir(parents=True, exist_ok=True)
        metadata = TemporalProgressiveMetadata(temporal_dir)

        def success_factory(config_manager, vector_store, collection_name):
            idx = MagicMock()
            idx.index_commits.return_value = _make_indexing_result()
            idx.temporal_dir = temporal_dir
            idx.close.return_value = None
            return idx

        _invoke_loop(tmp_path, ["voyage-ai"], success_factory)

        assert metadata.get_state() != "failed", (
            "Successful provider must not set metadata state to 'failed'."
        )
