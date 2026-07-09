"""
Unit tests for SharedJobSentinel.read_active() retry logic (Bug fix for race condition).

The O_CREAT|O_EXCL write in try_claim() has a window between file creation (0 bytes,
visible to concurrent readers) and os.write() completing. A loser thread calling
read_active() during this window reads an empty file, json.loads("") raises, and
the existing code returned None immediately — causing the loser to not learn the
winner's job_id.

The fix adds bounded retries (up to _READ_ACTIVE_MAX_RETRIES attempts at
_READ_ACTIVE_RETRY_DELAY_S intervals) before declaring persistently corrupt.

TDD RED PHASE: Tests written before the retry production code is added.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import pytest

from code_indexer.server.services.shared_job_sentinel import (
    SharedJobSentinel,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sentinel_dir(tmp_path: Path) -> Path:
    """Return a temp directory for sentinel files."""
    d = tmp_path / "dep-map-retry"
    d.mkdir()
    return d


@pytest.fixture
def sentinel(sentinel_dir: Path) -> SharedJobSentinel:
    """Return a SharedJobSentinel backed by tmp dir."""
    return SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)


# ---------------------------------------------------------------------------
# Sanity: no regression on the common paths
# ---------------------------------------------------------------------------


def test_read_active_returns_none_when_file_absent(
    sentinel: SharedJobSentinel,
) -> None:
    """read_active() returns None immediately when sentinel file does not exist.

    Regression guard — must stay fast (no retries on absent file).
    """
    start = time.monotonic()
    info = sentinel.read_active("analysis")
    elapsed = time.monotonic() - start

    assert info is None
    # Absent file must return immediately — well under one retry interval (10ms)
    assert elapsed < 0.05, f"Absent-file path took {elapsed:.3f}s — should be instant"


def test_read_active_returns_info_when_file_has_valid_json(
    sentinel: SharedJobSentinel,
    sentinel_dir: Path,
) -> None:
    """read_active() returns SentinelInfo on first attempt when file is valid.

    Regression guard — must stay fast (no unnecessary retries).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    sentinel_path = sentinel_dir / "_active_analysis.lock"
    sentinel_path.write_text(
        json.dumps(
            {
                "op_type": "analysis",
                "job_id": "job-001",
                "node_id": "node-A",
                "started_at": now_iso,
            }
        )
    )

    start = time.monotonic()
    info = sentinel.read_active("analysis")
    elapsed = time.monotonic() - start

    assert info is not None
    assert info.job_id == "job-001"
    assert info.node_id == "node-A"
    assert info.op_type == "analysis"
    # Valid file must parse on first attempt — well under one retry interval
    assert elapsed < 0.05, f"Valid-file path took {elapsed:.3f}s — should be instant"


# ---------------------------------------------------------------------------
# Core: transient empty file then valid content (the race window)
# ---------------------------------------------------------------------------


def test_read_active_retries_on_transient_empty_file_then_succeeds(
    sentinel: SharedJobSentinel,
    sentinel_dir: Path,
) -> None:
    """read_active() waits and retries when it initially finds an empty file.

    Simulates the try_claim() race window: winner creates the file (0 bytes)
    then writes content 5ms later. The loser calling read_active() must retry
    and return the winner's SentinelInfo, not None.

    This test FAILS before the retry fix is applied.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    valid_json = json.dumps(
        {
            "op_type": "analysis",
            "job_id": "race-winner",
            "node_id": "node-W",
            "started_at": now_iso,
        }
    )
    sentinel_path = sentinel_dir / "_active_analysis.lock"

    # Create the file empty (simulates O_CREAT|O_EXCL before os.write)
    sentinel_path.write_text("")

    # Background thread writes valid content 5ms later (simulates os.write + os.fsync)
    def write_content_after_delay() -> None:
        time.sleep(0.005)
        sentinel_path.write_text(valid_json)

    writer = Thread(target=write_content_after_delay, daemon=True)
    writer.start()

    # read_active() must retry and eventually see the valid content
    info = sentinel.read_active("analysis")

    writer.join(timeout=1.0)

    assert info is not None, (
        "read_active() returned None on transient-empty file — "
        "retry logic is missing or not working"
    )
    assert info.job_id == "race-winner"
    assert info.node_id == "node-W"


# ---------------------------------------------------------------------------
# Core: persistently corrupt — retries exhausted, returns None
# ---------------------------------------------------------------------------


def test_read_active_returns_none_when_persistently_corrupt(
    sentinel: SharedJobSentinel,
    sentinel_dir: Path,
) -> None:
    """read_active() returns None after exhausting all retries on permanently invalid content.

    After retries are exhausted the existing absent-on-corrupt behaviour is preserved.
    This test FAILS before the retry fix if the retry logic is absent (it would pass
    immediately but with wrong timing), and validates the correct ~30ms budget is spent.
    """
    sentinel_path = sentinel_dir / "_active_analysis.lock"
    sentinel_path.write_bytes(b"not valid json at all !!!!")

    start = time.monotonic()
    info = sentinel.read_active("analysis")
    elapsed = time.monotonic() - start

    assert info is None
    # Should spend ~30ms retrying (3 attempts × 10ms).  Allow 5× headroom for CI.
    assert elapsed >= 0.015, (
        f"read_active returned too fast ({elapsed:.3f}s) — retry budget not spent. "
        "Expected at least 2 retry sleep intervals (~20ms)."
    )


# ---------------------------------------------------------------------------
# Core: persistently corrupt — WARNING log fires
# ---------------------------------------------------------------------------


def test_read_active_persistent_corrupt_logs_warning(
    sentinel: SharedJobSentinel,
    sentinel_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """read_active() emits a WARNING log mentioning retries when persistently corrupt.

    Verifies the updated log message contains "after N retries" so operators
    know the retry budget was spent before giving up.

    This test FAILS before the retry fix because the log message does not mention retries.
    """
    sentinel_path = sentinel_dir / "_active_analysis.lock"
    sentinel_path.write_bytes(b"{bad json}")

    with caplog.at_level(
        logging.WARNING,
        logger="code_indexer.server.services.shared_job_sentinel",
    ):
        info = sentinel.read_active("analysis")

    assert info is None

    warning_messages = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warning_messages, "Expected at least one WARNING log — none found"

    # The updated message must mention retries
    assert any("retries" in msg.lower() for msg in warning_messages), (
        f"WARNING log does not mention 'retries'. Got: {warning_messages}"
    )
