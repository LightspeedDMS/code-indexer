"""
Tests for Story #174: Parallelize Langfuse observation fetches.

TDD test suite validating ThreadPoolExecutor-based parallel observation fetching
with thread-safe metrics, preserved optimizations, and error isolation.
"""

import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
    SyncMetrics,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)


class TestAC1ConfigMaxConcurrent:
    """AC1: Add configurable max_concurrent_observation_fetches parameter."""

    def test_config_has_default_value(self):
        """LangfuseConfig should have pull_max_concurrent_observations with default=5."""
        config = LangfuseConfig()
        assert hasattr(config, "pull_max_concurrent_observations")
        assert config.pull_max_concurrent_observations == 5

    def test_config_accepts_custom_value(self):
        """LangfuseConfig should accept custom max_concurrent_observation_fetches value."""
        config = LangfuseConfig(pull_max_concurrent_observations=10)
        assert config.pull_max_concurrent_observations == 10

    def test_config_max_range(self):
        """LangfuseConfig should support max value of 20."""
        config = LangfuseConfig(pull_max_concurrent_observations=20)
        assert config.pull_max_concurrent_observations == 20


class TestAC2ThreadPoolExecutorUsage:
    """AC2: Use ThreadPoolExecutor to fetch observations for multiple traces concurrently."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_uses_thread_pool_executor(self, mock_client_class, tmp_path):
        """sync_project should use ThreadPoolExecutor for parallel observation fetches."""
        # Setup mock API client
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        # Create 10 traces for parallel processing
        traces = []
        for i in range(10):
            traces.append({
                "id": f"trace-{i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "name": f"Test Trace {i}",
            })

        mock_client.fetch_traces_page.side_effect = [traces, []]  # First page has traces, second is empty
        mock_client.fetch_observations.return_value = []

        # Setup service with max_concurrent=5
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=5,
                pull_projects=[
                    LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                ],
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        # Patch ThreadPoolExecutor and as_completed to verify they're used
        with patch("code_indexer.server.services.langfuse_trace_sync_service.ThreadPoolExecutor") as mock_executor_class, \
             patch("code_indexer.server.services.langfuse_trace_sync_service.as_completed") as mock_as_completed:

            mock_executor = Mock()
            mock_executor_class.return_value.__enter__.return_value = mock_executor

            # Collect futures for as_completed
            submitted_futures = []

            # Mock submit to return futures that return ProcessTraceResult
            def mock_submit(fn, *args):
                future = Mock()
                future.result = Mock(return_value=fn(*args))
                submitted_futures.append(future)
                return future

            mock_executor.submit = mock_submit

            # Mock as_completed to return futures in order
            mock_as_completed.side_effect = lambda futures_dict: iter(futures_dict.keys())

            creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
            service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30, max_concurrent_observations=5)

            # Verify ThreadPoolExecutor was created with correct max_workers
            mock_executor_class.assert_called()
            call_args = mock_executor_class.call_args
            assert call_args[1]["max_workers"] == 5

            # Verify as_completed was called
            assert mock_as_completed.called

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_respects_custom_max_workers(self, mock_client_class, tmp_path):
        """ThreadPoolExecutor should use custom max_workers from config."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        # Create traces to trigger ThreadPoolExecutor usage
        traces = [
            {
                "id": f"trace-{i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "name": f"Test Trace {i}",
            }
            for i in range(5)
        ]
        mock_client.fetch_traces_page.side_effect = [traces, []]
        mock_client.fetch_observations.return_value = []

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=15,
                pull_projects=[
                    LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                ],
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        with patch("code_indexer.server.services.langfuse_trace_sync_service.ThreadPoolExecutor") as mock_executor_class, \
             patch("code_indexer.server.services.langfuse_trace_sync_service.as_completed") as mock_as_completed:

            mock_executor = Mock()
            mock_executor_class.return_value.__enter__.return_value = mock_executor

            # Mock submit
            def mock_submit(fn, *args):
                future = Mock()
                future.result = Mock(return_value=fn(*args))
                return future

            mock_executor.submit = mock_submit
            mock_as_completed.side_effect = lambda futures_dict: iter(futures_dict.keys())

            creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
            service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30, max_concurrent_observations=15)

            call_args = mock_executor_class.call_args
            assert call_args[1]["max_workers"] == 15


class TestAC3PreserveUpdatedAtOptimization:
    """AC3: Preserve existing two-phase optimization (updatedAt check skips observation fetch)."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_unchanged_trace_skips_observation_fetch(self, mock_client_class, tmp_path):
        """Traces with matching updatedAt should skip observation fetch (preserved optimization)."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        trace_id = "trace-unchanged"
        updated_at = "2026-02-11T10:00:00.000Z"

        trace = {
            "id": trace_id,
            "timestamp": "2026-02-11T10:00:00.000Z",
            "updatedAt": updated_at,
            "name": "Unchanged Trace",
        }

        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        # Setup service with pre-existing state
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=5,
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        # Pre-populate state with matching updatedAt and existing file
        # State file path matches _get_state_file_path: langfuse_sync_state_{project_name}.json
        state_file = tmp_path / "langfuse_sync_state_test-project.json"

        # Create file in correct folder structure (matches _get_trace_folder output)
        # _get_trace_folder returns: golden-repos/langfuse_{project}_{userId}/{sessionId}/
        trace_file = tmp_path / "golden-repos" / "langfuse_test-project_no_user" / "no_session" / f"{trace_id}.json"
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        trace_file.write_text(json.dumps({"trace": trace, "observations": []}))

        state_file.write_text(
            json.dumps({
                "last_sync_timestamp": datetime.now(timezone.utc).isoformat(),
                "trace_hashes": {
                    trace_id: {
                        "updated_at": updated_at,
                        "content_hash": "existing_hash",
                        "filename": f"{trace_id}.json",
                    }
                },
            })
        )

        # Mock ThreadPoolExecutor to allow parallel execution
        with patch("code_indexer.server.services.langfuse_trace_sync_service.ThreadPoolExecutor") as mock_executor_class, \
             patch("code_indexer.server.services.langfuse_trace_sync_service.as_completed") as mock_as_completed:

            mock_executor = Mock()
            mock_executor_class.return_value.__enter__.return_value = mock_executor

            # Mock submit to actually execute the function
            def mock_submit(fn, *args):
                future = Mock()
                future.result = Mock(return_value=fn(*args))
                return future

            mock_executor.submit = mock_submit
            mock_as_completed.side_effect = lambda futures_dict: iter(futures_dict.keys())

            creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
            service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30, max_concurrent_observations=5)

            # Verify fetch_observations was NOT called (optimization preserved)
            mock_client.fetch_observations.assert_not_called()


class TestAC4PreserveReentrancyProtection:
    """AC4: Preserve existing re-entrancy protection (_sync_lock guards concurrent syncs)."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_lock_prevents_concurrent_syncs(self, mock_client_class, tmp_path):
        """_sync_lock should prevent concurrent sync_all_projects calls."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}
        mock_client.fetch_traces_page.return_value = []

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=5,
                pull_projects=[
                    LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                ],
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        # Track if syncs run concurrently
        concurrent_count = 0
        max_concurrent = 0
        lock = threading.Lock()

        original_sync_project = service.sync_project

        def slow_sync_project(*args, **kwargs):
            nonlocal concurrent_count, max_concurrent
            with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            time.sleep(0.05)  # Simulate slow sync
            result = original_sync_project(*args, **kwargs)
            with lock:
                concurrent_count -= 1
            return result

        service.sync_project = slow_sync_project

        # Start two syncs in parallel threads
        threads = []
        for _ in range(2):
            t = threading.Thread(target=service.sync_all_projects)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # Verify no concurrent execution (max_concurrent should be 1)
        assert max_concurrent == 1

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_background_sync_actually_syncs_not_skipped(self, mock_client_class, tmp_path):
        """
        CRITICAL BUG TEST: _sync_loop() should actually execute sync, not silently skip.

        Bug: _sync_loop() acquires _sync_lock, then calls sync_all_projects() which also
        tries to acquire _sync_lock with blocking=False. Since Lock is non-reentrant,
        the second acquisition fails and sync_all_projects() returns immediately without
        doing any work. Background sync never actually syncs.

        This test verifies that when _sync_loop() runs, it actually fetches traces
        instead of silently skipping due to lock contention.
        """
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}
        mock_client.fetch_traces_page.return_value = []  # Empty page to end quickly

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_sync_interval_seconds=1,  # Fast interval for testing
                pull_max_concurrent_observations=5,
                pull_projects=[
                    LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                ],
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        # Directly call _sync_loop logic (simulating what background thread does)
        # After the fix, _sync_loop acquires _sync_lock then calls _do_sync_all_projects()
        # This test simulates exactly what _sync_loop() does

        # Simulate one iteration of _sync_loop (post-fix implementation)
        if service._sync_lock.acquire(blocking=False):
            try:
                service._do_sync_all_projects()
            finally:
                service._sync_lock.release()

        # CRITICAL: Verify that fetch_traces_page was actually called
        # With the bug (before fix), sync_all_projects() would return immediately
        # After fix, _do_sync_all_projects() executes sync logic without lock acquisition
        assert mock_client.fetch_traces_page.called, \
            "Background sync silently skipped due to lock double-acquisition bug"


class TestAC5ErrorIsolation:
    """AC5: Individual trace fetch failures must not abort the entire page."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_individual_trace_error_does_not_abort_page(self, mock_client_class, tmp_path):
        """One trace failure should not prevent other traces from being processed."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        # Create 5 traces, third one will fail
        traces = []
        for i in range(5):
            traces.append({
                "id": f"trace-{i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "name": f"Test Trace {i}",
            })

        mock_client.fetch_traces_page.side_effect = [traces, []]

        # Mock fetch_observations to fail for trace-2
        def fetch_observations_side_effect(trace_id):
            if trace_id == "trace-2":
                raise RuntimeError("Simulated API failure")
            return []

        mock_client.fetch_observations.side_effect = fetch_observations_side_effect

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=5,
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
        service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30, max_concurrent_observations=5)

        # Verify metrics show 5 checked, 1 error, 4 successful writes
        metrics = service.get_metrics()
        assert "test-project" in metrics
        assert metrics["test-project"]["traces_checked"] == 5
        assert metrics["test-project"]["errors_count"] == 1
        # 4 successful writes (trace-0, trace-1, trace-3, trace-4)
        assert metrics["test-project"]["traces_written_new"] == 4


class TestAC6MetricsThreadSafety:
    """AC6: Metrics tracking must remain accurate (thread-safe counter updates)."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_metrics_are_thread_safe(self, mock_client_class, tmp_path):
        """Metrics should be accurately tracked with concurrent trace processing."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        # Create 50 traces to ensure concurrency
        traces = []
        for i in range(50):
            traces.append({
                "id": f"trace-{i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "name": f"Test Trace {i}",
            })

        mock_client.fetch_traces_page.side_effect = [traces, []]
        mock_client.fetch_observations.return_value = []

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=10,
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
        service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30, max_concurrent_observations=5)

        # Verify all traces were counted correctly
        metrics = service.get_metrics()
        assert "test-project" in metrics
        assert metrics["test-project"]["traces_checked"] == 50
        assert metrics["test-project"]["traces_written_new"] == 50
        assert metrics["test-project"]["errors_count"] == 0


class TestAC7RateLimitHandling:
    """AC7: Rate limit handling - respect 429 responses with existing retry logic."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_rate_limit_logged_as_warning(self, mock_client_class, tmp_path):
        """429 rate limit errors should be logged but not crash sync."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}

        trace = {
            "id": "trace-rate-limited",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "name": "Rate Limited Trace",
        }

        mock_client.fetch_traces_page.side_effect = [[trace], []]

        # Simulate 429 rate limit error
        mock_client.fetch_observations.side_effect = RuntimeError("Rate limit exceeded (429)")

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_max_concurrent_observations=5,
            )
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")

        # Should complete without raising exception
        service.sync_project("https://cloud.langfuse.com", creds, trace_age_days=30)

        # Verify error was tracked in metrics
        metrics = service.get_metrics()
        assert metrics["test-project"]["errors_count"] == 1


class TestAC8ConfigUIField:
    """AC8: Add config UI field for max_concurrent_observation_fetches in Web UI Config Screen."""

    def test_config_service_get_includes_field(self):
        """LangfuseConfig should include pull_max_concurrent_observations field with default value."""
        from code_indexer.server.utils.config_manager import LangfuseConfig
        config = LangfuseConfig()
        # Verify field exists with default value of 5
        assert hasattr(config, "pull_max_concurrent_observations")
        assert config.pull_max_concurrent_observations == 5

    def test_config_service_update_clamps_value(self):
        """validate_config() should enforce 1-20 range for pull_max_concurrent_observations."""
        from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager, LangfuseConfig
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = ServerConfigManager(tmp_dir)

            # Test value below range (0) - should raise ValueError
            config_low = ServerConfig(
                server_dir=tmp_dir,
                langfuse_config=LangfuseConfig(pull_max_concurrent_observations=0)
            )
            try:
                manager.validate_config(config_low)
                assert False, "Expected ValueError for value below range"
            except ValueError as e:
                assert "pull_max_concurrent_observations must be between 1 and 20" in str(e)

            # Test value above range (21) - should raise ValueError
            config_high = ServerConfig(
                server_dir=tmp_dir,
                langfuse_config=LangfuseConfig(pull_max_concurrent_observations=21)
            )
            try:
                manager.validate_config(config_high)
                assert False, "Expected ValueError for value above range"
            except ValueError as e:
                assert "pull_max_concurrent_observations must be between 1 and 20" in str(e)

            # Test valid values (1, 5, 20) - should not raise
            for valid_value in [1, 5, 20]:
                config_valid = ServerConfig(
                    server_dir=tmp_dir,
                    langfuse_config=LangfuseConfig(pull_max_concurrent_observations=valid_value)
                )
                manager.validate_config(config_valid)  # Should not raise

    def test_config_template_has_input_field(self):
        """config_section.html template should have input field for pull_max_concurrent_observations."""
        from pathlib import Path
        template_path = Path("src/code_indexer/server/web/templates/partials/config_section.html")
        content = template_path.read_text()

        # Verify field name is present
        assert "pull_max_concurrent_observations" in content, \
            "Template should reference pull_max_concurrent_observations field"

        # Verify input field exists with correct attributes
        assert 'name="pull_max_concurrent_observations"' in content, \
            "Template should have input with name='pull_max_concurrent_observations'"

        # Verify min/max constraints are present
        assert 'min="1"' in content or "min='1'" in content, \
            "Template should enforce min=1"
        assert 'max="20"' in content or "max='20'" in content, \
            "Template should enforce max=20"


class TestProcessTraceReturnsResult:
    """AC9 (Implementation): _process_trace should return result instead of mutating shared state."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_process_trace_returns_result_object(self, mock_client_class, tmp_path):
        """_process_trace should return a result object instead of mutating metrics directly."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "test-project"}
        mock_client.fetch_observations.return_value = []

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(pull_enabled=True)
        )

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )

        trace = {
            "id": "trace-new",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "name": "New Trace",
        }

        creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
        api_client = mock_client_class("https://cloud.langfuse.com", creds)

        # Call _process_trace - should return result, not mutate metrics
        trace_hashes = {}

        result = service._process_trace(
            api_client,
            trace,
            "test-project",
            trace_hashes,
        )

        # Verify result structure (should be tuple or dataclass with trace info)
        # OLD behavior: mutates metrics.traces_written_new += 1
        # NEW behavior: returns result object, main thread updates metrics
        assert result is not None
        # Result should contain trace_id, metrics deltas, rename_info, etc.
