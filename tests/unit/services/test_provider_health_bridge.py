"""Tests for Bug #678: provider_health_bridge cross-process health telemetry.

Tests: write, drain, truncation, error handling, concurrency, malformed input.
"""

import json
import queue
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def repo_dir(tmp_path):
    """Temporary repo directory with .code-indexer sub-dir."""
    ci_dir = tmp_path / ".code-indexer"
    ci_dir.mkdir()
    return str(tmp_path)


class TestWriteProviderHealthEvent:
    def test_write_event_creates_file(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 123.4)
        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        assert health_file.exists()
        data = json.loads(health_file.read_text().strip())
        assert data["provider"] == "voyage-ai"
        assert data["success"] is True
        assert abs(data["latency_ms"] - 123.4) < 0.01
        assert "timestamp" in data

    def test_write_multiple_events_appends(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 10.0)
        write_provider_health_event(repo_dir, "cohere", False, 20.0)
        write_provider_health_event(repo_dir, "voyage-ai", True, 30.0)
        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        lines = health_file.read_text().strip().splitlines()
        assert len(lines) == 3
        providers = [json.loads(line)["provider"] for line in lines]
        assert providers == ["voyage-ai", "cohere", "voyage-ai"]

    def test_write_survives_io_error(self, repo_dir):
        """write_provider_health_event must not raise even on IOError."""
        from code_indexer.services.provider_health_bridge import (
            write_provider_health_event,
        )

        with patch("builtins.open", side_effect=IOError("disk full")):
            # Must not raise
            write_provider_health_event(repo_dir, "voyage-ai", True, 50.0)

    def test_write_records_timestamp(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            write_provider_health_event,
        )

        before = time.time()
        write_provider_health_event(repo_dir, "voyage-ai", True, 5.0)
        after = time.time()
        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        data = json.loads(health_file.read_text().strip())
        assert before <= data["timestamp"] <= after


class TestDrainProviderHealthEvents:
    def test_drain_returns_events_and_clears_file(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 100.0)
        write_provider_health_event(repo_dir, "cohere", False, 200.0)
        events = drain_provider_health_events(repo_dir)
        assert len(events) == 2
        assert events[0].provider == "voyage-ai"
        assert events[0].success is True
        assert abs(events[0].latency_ms - 100.0) < 0.01
        assert events[1].provider == "cohere"
        assert events[1].success is False
        # File must be gone after drain
        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        assert not health_file.exists()

    def test_drain_noop_if_no_file(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
        )

        events = drain_provider_health_events(repo_dir)
        assert events == []

    def test_drain_atomic_rename_failure(self, repo_dir):
        """If os.rename fails, drain returns empty list without raising."""
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 50.0)
        with patch(
            "code_indexer.services.provider_health_bridge.os.replace",
            side_effect=OSError("cross-device"),
        ):
            events = drain_provider_health_events(repo_dir)
        assert events == []

    def test_drain_skips_malformed_json(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
        )

        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        health_file.write_text(
            '{"provider":"voyage-ai","success":true,"latency_ms":10.0,"timestamp":1234567890.0}\n'
            "not-valid-json\n"
            '{"provider":"cohere","success":false,"latency_ms":20.0,"timestamp":1234567891.0}\n'
        )
        events = drain_provider_health_events(repo_dir)
        assert len(events) == 2
        assert events[0].provider == "voyage-ai"
        assert events[1].provider == "cohere"

    def test_drain_skips_binary_garbage_lines(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
        )

        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        # Write a valid line followed by garbage (missing required keys)
        health_file.write_text(
            '{"provider":"voyage-ai","success":true,"latency_ms":10.0,"timestamp":1.0}\n'
            '{"broken":true}\n'
        )
        events = drain_provider_health_events(repo_dir)
        assert len(events) == 1
        assert events[0].provider == "voyage-ai"

    def test_drain_handles_empty_lines(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
        )

        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        health_file.write_text(
            "\n"
            '{"provider":"voyage-ai","success":true,"latency_ms":5.0,"timestamp":1.0}\n'
            "\n\n"
        )
        events = drain_provider_health_events(repo_dir)
        assert len(events) == 1

    def test_drain_deletes_read_file_after_processing(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            drain_provider_health_events,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 10.0)
        drain_provider_health_events(repo_dir)
        read_file = Path(repo_dir) / ".code-indexer" / ".provider_health.jsonl.read"
        assert not read_file.exists()


class TestWriteDrainRoundtrip:
    def test_write_then_drain_roundtrip(self, repo_dir):
        from code_indexer.services.provider_health_bridge import (
            HealthEvent,
            drain_provider_health_events,
            write_provider_health_event,
        )

        before = time.time()
        write_provider_health_event(repo_dir, "voyage-ai", True, 42.0)
        after = time.time()
        events = drain_provider_health_events(repo_dir)
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, HealthEvent)
        assert ev.provider == "voyage-ai"
        assert ev.success is True
        assert abs(ev.latency_ms - 42.0) < 0.01
        assert before <= ev.timestamp <= after


class TestMaxFileSizeTruncation:
    def test_max_file_size_truncation(self, repo_dir):
        """When file exceeds 1MB, truncation occurs and subsequent write succeeds."""
        from code_indexer.services.provider_health_bridge import (
            MAX_HEALTH_FILE_BYTES,
            write_provider_health_event,
        )

        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        # Write enough lines to exceed MAX_HEALTH_FILE_BYTES
        line = (
            json.dumps(
                {
                    "provider": "voyage-ai",
                    "success": True,
                    "latency_ms": 10.0,
                    "timestamp": 1.0,
                }
            )
            + "\n"
        )
        lines_needed = (MAX_HEALTH_FILE_BYTES // len(line)) + 10
        health_file.write_text(line * lines_needed)
        size_before = health_file.stat().st_size
        assert size_before > MAX_HEALTH_FILE_BYTES

        # Trigger write which should truncate
        write_provider_health_event(repo_dir, "cohere", False, 5.0)
        size_after = health_file.stat().st_size
        assert size_after < size_before


class TestConcurrentWrites:
    def test_concurrent_writes_dont_corrupt(self, repo_dir):
        """Multiple threads writing simultaneously must produce valid JSONL."""
        from code_indexer.services.provider_health_bridge import (
            write_provider_health_event,
        )

        n_threads = 10
        n_per_thread = 20
        error_queue: queue.Queue = queue.Queue()

        def writer(provider: str) -> None:
            try:
                for i in range(n_per_thread):
                    write_provider_health_event(repo_dir, provider, True, float(i))
            except Exception as exc:
                error_queue.put(exc)

        threads = [
            threading.Thread(target=writer, args=(f"provider-{i}",))
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert error_queue.empty(), f"Thread errors occurred: {list(error_queue.queue)}"
        health_file = Path(repo_dir) / ".code-indexer" / "provider_health.jsonl"
        # All lines must be valid JSON
        valid = 0
        for line in health_file.read_text().strip().splitlines():
            line = line.strip()
            if line:
                data = json.loads(line)  # Must not raise
                assert "provider" in data
                valid += 1
        assert valid == n_threads * n_per_thread


class TestDrainAndFeedMonitor:
    def test_drain_and_feed_calls_record_call_for_each_event(self, repo_dir):
        """drain_and_feed_monitor feeds all drained events into ProviderHealthMonitor."""
        from unittest.mock import MagicMock, patch

        from code_indexer.services.provider_health_bridge import (
            drain_and_feed_monitor,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 42.0)
        write_provider_health_event(repo_dir, "cohere", False, 99.0)

        mock_monitor = MagicMock()
        with patch(
            "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_monitor
            drain_and_feed_monitor(repo_dir)

        assert mock_monitor.record_call.call_count == 2
        call_args_list = mock_monitor.record_call.call_args_list
        providers = [c.args[0] for c in call_args_list]
        assert "voyage-ai" in providers
        assert "cohere" in providers

    def test_drain_and_feed_noop_when_no_events(self, repo_dir):
        """drain_and_feed_monitor does nothing when no health file exists."""
        from unittest.mock import MagicMock, patch

        from code_indexer.services.provider_health_bridge import drain_and_feed_monitor

        mock_monitor = MagicMock()
        with patch(
            "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_monitor
            drain_and_feed_monitor(repo_dir)

        mock_monitor.record_call.assert_not_called()

    def test_drain_and_feed_survives_monitor_exception(self, repo_dir):
        """drain_and_feed_monitor must not raise even if ProviderHealthMonitor raises."""
        from unittest.mock import MagicMock, patch

        from code_indexer.services.provider_health_bridge import (
            drain_and_feed_monitor,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 10.0)

        mock_monitor = MagicMock()
        mock_monitor.record_call.side_effect = RuntimeError("monitor exploded")
        with patch(
            "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_monitor
            # Must not raise
            drain_and_feed_monitor(repo_dir)

    def test_drain_and_feed_passes_correct_fields(self, repo_dir):
        """drain_and_feed_monitor passes provider, latency_ms, success to record_call."""
        from unittest.mock import MagicMock, patch

        from code_indexer.services.provider_health_bridge import (
            drain_and_feed_monitor,
            write_provider_health_event,
        )

        write_provider_health_event(repo_dir, "voyage-ai", True, 55.5)

        mock_monitor = MagicMock()
        with patch(
            "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_monitor
            drain_and_feed_monitor(repo_dir)

        mock_monitor.record_call.assert_called_once_with("voyage-ai", 55.5, True)
