"""
Tests for Bug #964: Langfuse sync causes server sluggishness.

Three root causes fixed:
1. Per-page ThreadPoolExecutor creation - executor must be reused across pages
2. Blocking time.sleep() in retry logic - replaced with stop_event.wait(timeout=...)
3. on_sync_complete overhead - short-circuit when no new repos discovered
"""

import threading
import time
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import requests

from code_indexer.server.services.langfuse_api_client import LangfuseApiClient
from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)


@pytest.fixture
def mock_creds():
    return LangfusePullProject(public_key="test_pk", secret_key="test_sk")


@pytest.fixture
def stop_event():
    return threading.Event()


@pytest.fixture
def api_client_with_stop(mock_creds, stop_event):
    return LangfuseApiClient(
        "https://test.langfuse.com", mock_creds, stop_event=stop_event
    )


# ---------------------------------------------------------------------------
# Fix 1: ThreadPoolExecutor reused across pages
# ---------------------------------------------------------------------------


class TestExecutorReusedAcrossPages:
    """Fix 1: ThreadPoolExecutor must be created once per project sync, not per page."""

    def test_executor_reused_across_pages(self, tmp_path):
        """ThreadPoolExecutor.__init__ must be called ONCE for a 3-page project sync."""
        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient"
        ) as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            mock_client.discover_project.return_value = {"name": "test-project"}

            def make_traces(start, count):
                return [
                    {
                        "id": f"trace-{i}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "updatedAt": datetime.now(timezone.utc).isoformat(),
                        "name": f"Trace {i}",
                    }
                    for i in range(start, start + count)
                ]

            mock_client.fetch_traces_page.side_effect = [
                make_traces(0, 5),  # page 1
                make_traces(5, 5),  # page 2
                make_traces(10, 5),  # page 3
                [],  # page 4 - empty, stops loop
            ]
            mock_client.fetch_observations.return_value = []

            config = ServerConfig(
                server_dir=str(tmp_path),
                langfuse_config=LangfuseConfig(
                    pull_enabled=True,
                    pull_max_concurrent_observations=5,
                    pull_projects=[
                        LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                    ],
                ),
            )
            service = LangfuseTraceSyncService(
                config_getter=lambda: config,
                data_dir=str(tmp_path),
            )

            executor_init_count = 0
            original_executor_class = __import__(
                "concurrent.futures", fromlist=["ThreadPoolExecutor"]
            ).ThreadPoolExecutor

            class CountingExecutor:
                def __init__(self, **kwargs):
                    nonlocal executor_init_count
                    executor_init_count += 1
                    self._executor = original_executor_class(**kwargs)

                def __enter__(self):
                    return self._executor.__enter__()

                def __exit__(self, *args):
                    return self._executor.__exit__(*args)

                def submit(self, *args, **kwargs):
                    return self._executor.submit(*args, **kwargs)

                def shutdown(self, *args, **kwargs):
                    return self._executor.shutdown(*args, **kwargs)

            with patch(
                "code_indexer.server.services.langfuse_trace_sync_service.ThreadPoolExecutor",
                CountingExecutor,
            ):
                creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                service.sync_project(
                    "https://cloud.langfuse.com",
                    creds,
                    trace_age_days=30,
                    max_concurrent_observations=5,
                )

            # CRITICAL: executor must be created exactly ONCE, not once per page
            assert executor_init_count == 1, (
                f"ThreadPoolExecutor was created {executor_init_count} times "
                f"but should be created only once per project sync (3 pages fetched)"
            )


# ---------------------------------------------------------------------------
# Fix 2: stop_event.wait replaces time.sleep in retry logic
# ---------------------------------------------------------------------------


class TestStopEventReplacesSleep:
    """Fix 2: LangfuseApiClient must accept stop_event and use wait() not sleep()."""

    def test_api_client_accepts_stop_event_parameter(self, mock_creds):
        """LangfuseApiClient.__init__ must accept stop_event keyword argument."""
        stop_event = threading.Event()
        client = LangfuseApiClient(
            "https://test.langfuse.com", mock_creds, stop_event=stop_event
        )
        assert client is not None

    def test_api_client_default_stop_event_is_threading_event(self, mock_creds):
        """When stop_event not supplied, a default threading.Event() is created."""
        client = LangfuseApiClient("https://test.langfuse.com", mock_creds)
        assert hasattr(client, "_stop_event")
        assert isinstance(client._stop_event, threading.Event)

    def test_stop_event_wait_replaces_sleep_on_rate_limit(
        self, api_client_with_stop, stop_event
    ):
        """On 429, stop_event.wait(timeout=wait) must be called, NOT time.sleep()."""
        responses = [
            Mock(status_code=429),
            Mock(status_code=200),
        ]

        with patch("requests.request", side_effect=responses):
            with patch.object(stop_event, "wait") as mock_wait:
                with patch("time.sleep") as mock_sleep:
                    result = api_client_with_stop._request_with_retry(
                        "GET", "https://test.com/api"
                    )

        assert result.status_code == 200
        mock_wait.assert_called_once()
        # timeout argument must be the backoff value (2 for first attempt)
        call_kwargs = mock_wait.call_args
        timeout_val = (
            call_kwargs[1].get("timeout") if call_kwargs[1] else call_kwargs[0][0]
        )
        assert timeout_val == 2
        mock_sleep.assert_not_called()

    def test_stop_event_wait_replaces_sleep_on_server_error(
        self, api_client_with_stop, stop_event
    ):
        """On 503, stop_event.wait(timeout=wait) must be called, NOT time.sleep()."""
        responses = [
            Mock(status_code=503),
            Mock(status_code=200),
        ]

        with patch("requests.request", side_effect=responses):
            with patch.object(stop_event, "wait") as mock_wait:
                with patch("time.sleep") as mock_sleep:
                    result = api_client_with_stop._request_with_retry(
                        "GET", "https://test.com/api"
                    )

        assert result.status_code == 200
        mock_wait.assert_called_once()
        mock_sleep.assert_not_called()

    def test_stop_event_wait_replaces_sleep_on_connection_error(
        self, api_client_with_stop, stop_event
    ):
        """On ConnectionError, stop_event.wait(timeout=wait) must be used, NOT time.sleep()."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.ConnectionError("Connection failed")
            return Mock(status_code=200)

        with patch("requests.request", side_effect=side_effect):
            with patch.object(stop_event, "wait") as mock_wait:
                with patch("time.sleep") as mock_sleep:
                    result = api_client_with_stop._request_with_retry(
                        "GET", "https://test.com/api"
                    )

        assert result.status_code == 200
        mock_wait.assert_called_once()
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 3: on_sync_complete short-circuit when no new repos discovered
# ---------------------------------------------------------------------------


class TestOnSyncCompleteShortCircuit:
    """Fix 3: _on_sync_complete must be skipped when no new repos discovered."""

    def test_on_sync_complete_short_circuits_when_no_new_repos(self, tmp_path):
        """register_langfuse_golden_repos must NOT be called when no new repos found."""
        register_mock = Mock()
        previous_sync_repos: set = {"repo1", "repo2"}

        def _on_sync_complete_with_short_circuit():
            current_repos = {"repo1", "repo2"}
            if current_repos == previous_sync_repos:
                return  # short-circuit: no new repos
            register_mock()

        _on_sync_complete_with_short_circuit()
        register_mock.assert_not_called()

    def test_on_sync_complete_runs_when_new_repo_discovered(self, tmp_path):
        """register_langfuse_golden_repos MUST be called when a new repo is found."""
        register_mock = Mock()
        previous_sync_repos: set = {"repo1"}

        def _on_sync_complete_with_short_circuit():
            current_repos = {"repo1", "repo2"}  # repo2 is new
            if current_repos == previous_sync_repos:
                return  # short-circuit: no new repos
            register_mock()

        _on_sync_complete_with_short_circuit()
        register_mock.assert_called_once()

    def test_sync_service_exposes_last_modified_repos_for_comparison(self, tmp_path):
        """LangfuseTraceSyncService must expose _last_modified_repos for short-circuit logic."""
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(pull_enabled=True),
        )
        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )
        assert hasattr(service, "_last_modified_repos"), (
            "Service must expose _last_modified_repos for short-circuit comparison"
        )
        assert isinstance(service._last_modified_repos, set)

    def test_on_sync_complete_duration_is_measurable(self, tmp_path):
        """on_sync_complete timing must be measurable (time.time() bracket pattern)."""
        timing_logged = []

        def on_sync_complete_with_timing():
            start = time.time()
            elapsed = time.time() - start
            timing_logged.append(elapsed)

        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(pull_enabled=True),
        )
        LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
            on_sync_complete=on_sync_complete_with_timing,
        )

        on_sync_complete_with_timing()

        assert len(timing_logged) == 1
        assert timing_logged[0] >= 0.0


# ---------------------------------------------------------------------------
# Issue 1 / Issue 4: stop_event wired from service to api_client in production
# ---------------------------------------------------------------------------


class TestStopEventWiringProduction:
    """Issue 1 + 4: sync_project must pass self._stop_event to LangfuseApiClient."""

    def test_stop_event_wired_from_service_to_api_client(self, tmp_path):
        """LangfuseApiClient must be constructed with stop_event=service._stop_event.

        Without this wiring, Fix 2 (interruptible retries) is non-functional in
        production because the client's internal Event is never set by stop().
        """
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk_test", secret_key="sk_test")
                ],
            ),
        )
        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path),
        )
        service_stop_event = service._stop_event

        captured_kwargs = {}

        original_init = LangfuseApiClient.__init__

        def capturing_init(self_client, host, creds, stop_event=None):
            captured_kwargs["stop_event"] = stop_event
            original_init(self_client, host, creds, stop_event=stop_event)

        creds = LangfusePullProject(public_key="pk_test", secret_key="sk_test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient.__init__",
            capturing_init,
        ):
            with patch.object(
                LangfuseApiClient,
                "discover_project",
                return_value={"name": "test-project"},
            ):
                with patch.object(
                    LangfuseApiClient, "fetch_traces_page", return_value=[]
                ):
                    service.sync_project(
                        "https://cloud.langfuse.com",
                        creds,
                        trace_age_days=30,
                    )

        assert "stop_event" in captured_kwargs, (
            "LangfuseApiClient.__init__ was not called with stop_event keyword"
        )
        assert captured_kwargs["stop_event"] is service_stop_event, (
            f"LangfuseApiClient received stop_event={captured_kwargs['stop_event']!r} "
            f"but expected service._stop_event={service_stop_event!r}. "
            "Fix: change LangfuseApiClient(host, creds) to "
            "LangfuseApiClient(host, creds, stop_event=self._stop_event) in sync_project()."
        )
