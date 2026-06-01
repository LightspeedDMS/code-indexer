"""
Unit tests for SharedJobSentinel (Story #1035).

Tests atomic O_CREAT|O_EXCL claim, release, stale detection,
concurrent race resolution, and owner-only release safety.

TDD RED PHASE: Tests written before production code.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Thread
from typing import List

import pytest

from code_indexer.server.services.shared_job_sentinel import (
    ClaimResult,
    SentinelInfo,
    SharedJobSentinel,
    AnalysisAlreadyRunningError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sentinel_dir(tmp_path: Path) -> Path:
    """Return a temp directory for sentinel files."""
    d = tmp_path / "dep-map"
    d.mkdir()
    return d


@pytest.fixture
def sentinel(sentinel_dir: Path) -> SharedJobSentinel:
    """Return a SharedJobSentinel backed by tmp dir."""
    return SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)


@pytest.fixture
def fast_stale_sentinel(sentinel_dir: Path) -> SharedJobSentinel:
    """Return a SharedJobSentinel with very short stale timeout for testing."""
    return SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=1)


# ---------------------------------------------------------------------------
# AC8: try_claim — successful first claim
# ---------------------------------------------------------------------------


def test_try_claim_succeeds_when_no_sentinel_exists(
    sentinel: SharedJobSentinel,
) -> None:
    """Claiming with no sentinel present returns ClaimResult(success=True)."""
    result = sentinel.try_claim("analysis", "job-001", "node-A")

    assert result.success is True
    assert result.active is not None
    assert result.active.job_id == "job-001"
    assert result.active.node_id == "node-A"
    assert result.active.op_type == "analysis"


def test_try_claim_creates_sentinel_file(
    sentinel: SharedJobSentinel, sentinel_dir: Path
) -> None:
    """Successful claim writes a JSON file at expected path."""
    sentinel.try_claim("analysis", "job-001", "node-A")

    sentinel_file = sentinel_dir / "_active_analysis.lock"
    assert sentinel_file.exists()
    payload = json.loads(sentinel_file.read_text())
    assert payload["job_id"] == "job-001"
    assert payload["node_id"] == "node-A"
    assert payload["op_type"] == "analysis"
    assert "started_at" in payload


# ---------------------------------------------------------------------------
# AC8: try_claim — failed claim when sentinel already exists
# ---------------------------------------------------------------------------


def test_try_claim_fails_when_sentinel_already_exists(
    sentinel: SharedJobSentinel,
) -> None:
    """Second claim attempt on same op_type returns ClaimResult(success=False)."""
    sentinel.try_claim("analysis", "job-001", "node-A")
    result = sentinel.try_claim("analysis", "job-002", "node-B")

    assert result.success is False
    assert result.active is not None
    assert result.active.job_id == "job-001"  # First owner's job_id


def test_try_claim_different_op_types_are_independent(
    sentinel: SharedJobSentinel,
) -> None:
    """Claiming 'dashboard' does not block 'analysis' and vice versa."""
    r1 = sentinel.try_claim("analysis", "job-001", "node-A")
    r2 = sentinel.try_claim("dashboard", "job-002", "node-A")

    assert r1.success is True
    assert r2.success is True


# ---------------------------------------------------------------------------
# AC9: Stale sentinel recovery
# ---------------------------------------------------------------------------


def test_try_claim_replaces_stale_sentinel(sentinel_dir: Path) -> None:
    """Claim replaces stale sentinel atomically."""
    old_started_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    old_payload = {
        "op_type": "analysis",
        "job_id": "old-job",
        "node_id": "crashed-node",
        "started_at": old_started_at,
    }
    sentinel_file = sentinel_dir / "_active_analysis.lock"
    sentinel_file.write_text(json.dumps(old_payload))

    # Use a sentinel with 4h stale timeout — old payload is 5h old
    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)
    result = snt.try_claim("analysis", "new-job", "new-node")

    assert result.success is True
    assert result.replaced_stale is True
    # Sentinel now contains new owner
    updated = json.loads(sentinel_file.read_text())
    assert updated["job_id"] == "new-job"
    assert updated["node_id"] == "new-node"


def test_try_claim_returns_false_for_fresh_sentinel(sentinel_dir: Path) -> None:
    """Claim returns False when existing sentinel is fresh (not stale)."""
    fresh_started_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "op_type": "analysis",
        "job_id": "fresh-job",
        "node_id": "live-node",
        "started_at": fresh_started_at,
    }
    sentinel_file = sentinel_dir / "_active_analysis.lock"
    sentinel_file.write_text(json.dumps(payload))

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)
    result = snt.try_claim("analysis", "new-job", "new-node")

    assert result.success is False
    assert result.active is not None
    assert result.active.job_id == "fresh-job"


# ---------------------------------------------------------------------------
# AC10: release — owner-only safety
# ---------------------------------------------------------------------------


def test_release_removes_sentinel_when_job_id_matches(
    sentinel: SharedJobSentinel, sentinel_dir: Path
) -> None:
    """release() with matching job_id removes the sentinel file."""
    sentinel.try_claim("analysis", "job-001", "node-A")
    sentinel.release("analysis", expected_job_id="job-001")

    sentinel_file = sentinel_dir / "_active_analysis.lock"
    assert not sentinel_file.exists()


def test_release_does_not_remove_sentinel_when_job_id_mismatches(
    sentinel: SharedJobSentinel, sentinel_dir: Path
) -> None:
    """release() with wrong job_id must NOT delete the sentinel."""
    sentinel.try_claim("analysis", "job-001", "node-A")
    sentinel.release("analysis", expected_job_id="wrong-job")

    sentinel_file = sentinel_dir / "_active_analysis.lock"
    assert sentinel_file.exists()  # Sentinel must survive


def test_release_is_idempotent_when_no_sentinel_exists(
    sentinel: SharedJobSentinel,
) -> None:
    """release() when no sentinel exists logs warning but does NOT raise."""
    # Should not raise
    sentinel.release("analysis", expected_job_id="job-001")


# ---------------------------------------------------------------------------
# read_active
# ---------------------------------------------------------------------------


def test_read_active_returns_none_when_no_file(sentinel: SharedJobSentinel) -> None:
    """read_active() returns None when sentinel file is absent."""
    info = sentinel.read_active("analysis")
    assert info is None


def test_read_active_returns_sentinel_info(sentinel: SharedJobSentinel) -> None:
    """read_active() returns SentinelInfo with correct fields."""
    sentinel.try_claim("analysis", "job-001", "node-A")
    info = sentinel.read_active("analysis")

    assert info is not None
    assert info.job_id == "job-001"
    assert info.node_id == "node-A"
    assert info.op_type == "analysis"
    assert isinstance(info.started_at, datetime)


def test_read_active_returns_none_on_corrupt_payload(sentinel_dir: Path) -> None:
    """read_active() returns None on corrupt JSON without raising."""
    sentinel_file = sentinel_dir / "_active_analysis.lock"
    sentinel_file.write_text("{not valid json")

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)
    info = snt.read_active("analysis")
    assert info is None


def test_read_active_returns_none_on_missing_fields(sentinel_dir: Path) -> None:
    """read_active() returns None when JSON is valid but fields are missing."""
    sentinel_file = sentinel_dir / "_active_analysis.lock"
    sentinel_file.write_text('{"some": "other"}')

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)
    info = snt.read_active("analysis")
    assert info is None


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_is_stale_returns_true_for_old_sentinel(sentinel: SharedJobSentinel) -> None:
    """is_stale() returns True when started_at is older than timeout."""
    old_time = datetime.now(timezone.utc) - timedelta(hours=5)
    info = SentinelInfo(
        op_type="analysis",
        job_id="job-001",
        node_id="node-A",
        started_at=old_time,
    )
    assert sentinel.is_stale(info, 14400) is True  # 4h timeout, 5h old


def test_is_stale_returns_false_for_fresh_sentinel(sentinel: SharedJobSentinel) -> None:
    """is_stale() returns False when started_at is within timeout."""
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    info = SentinelInfo(
        op_type="analysis",
        job_id="job-001",
        node_id="node-A",
        started_at=fresh_time,
    )
    assert sentinel.is_stale(info, 14400) is False  # 4h timeout, 30min old


# ---------------------------------------------------------------------------
# AC8: Concurrent race — single winner
# ---------------------------------------------------------------------------


def test_concurrent_claim_race_single_winner(sentinel_dir: Path) -> None:
    """Concurrent try_claim calls result in exactly one winner."""
    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)

    results: List[ClaimResult] = []
    errors: List[Exception] = []

    def claim(node_id: str, job_id: str) -> None:
        try:
            r = snt.try_claim("analysis", job_id, node_id)
            results.append(r)
        except Exception as e:
            errors.append(e)

    threads = [Thread(target=claim, args=(f"node-{i}", f"job-{i}")) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Unexpected errors: {errors}"
    winners = [r for r in results if r.success]
    losers = [r for r in results if not r.success]
    assert len(winners) == 1, f"Expected exactly 1 winner, got {len(winners)}"
    assert len(losers) == 9, f"Expected 9 losers, got {len(losers)}"
    # All losers must reference the winner's job_id
    winner_job_id = winners[0].active.job_id
    for loser in losers:
        assert loser.active.job_id == winner_job_id


# ---------------------------------------------------------------------------
# AnalysisAlreadyRunningError
# ---------------------------------------------------------------------------


def test_analysis_already_running_error_has_active_job_id() -> None:
    """AnalysisAlreadyRunningError carries active_job_id attribute."""
    err = AnalysisAlreadyRunningError(active_job_id="job-001")
    assert err.active_job_id == "job-001"
    assert "job-001" in str(err)


# ---------------------------------------------------------------------------
# SentinelInfo dataclass
# ---------------------------------------------------------------------------


def test_sentinel_info_fields() -> None:
    """SentinelInfo stores all required fields."""
    now = datetime.now(timezone.utc)
    info = SentinelInfo(op_type="analysis", job_id="j1", node_id="n1", started_at=now)
    assert info.op_type == "analysis"
    assert info.job_id == "j1"
    assert info.node_id == "n1"
    assert info.started_at == now


def test_claim_result_fields() -> None:
    """ClaimResult has success, active, and replaced_stale fields."""
    info = SentinelInfo(
        op_type="analysis",
        job_id="j1",
        node_id="n1",
        started_at=datetime.now(timezone.utc),
    )
    r = ClaimResult(success=True, active=info)
    assert r.success is True
    assert r.active is info
    assert r.replaced_stale is False  # default


def test_claim_result_replaced_stale_field() -> None:
    """ClaimResult.replaced_stale is True when stale sentinel was replaced."""
    info = SentinelInfo(
        op_type="analysis",
        job_id="j1",
        node_id="n1",
        started_at=datetime.now(timezone.utc),
    )
    r = ClaimResult(success=True, active=info, replaced_stale=True)
    assert r.replaced_stale is True


# ---------------------------------------------------------------------------
# Lines 117-120: retry gives up conservatively when file disappears repeatedly
# ---------------------------------------------------------------------------


def test_try_claim_retry_gives_up_when_file_disappears_repeatedly(
    sentinel_dir: Path,
) -> None:
    """When FileExistsError fires then read_active returns None twice, give up (success=False, active=None).

    Covers lines 117-120: _retry=True path where the sentinel vanishes before
    we can read it on the second attempt — conservatively return ClaimResult(False, None).
    """
    from unittest.mock import patch

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)

    # Create the lock file so os.open raises FileExistsError on every attempt
    sentinel_path = sentinel_dir / "_active_ghost.lock"
    sentinel_path.write_text("{}")

    # Patch read_active to always return None (file "disappears" before each read)
    with patch.object(snt, "read_active", return_value=None):
        result = snt.try_claim("ghost", "job-new", "node-new")

    assert result.success is False
    assert result.active is None


# ---------------------------------------------------------------------------
# Lines 161-162: release logs warning when sentinel file disappears between
#                read_active and os.unlink
# ---------------------------------------------------------------------------


def test_release_logs_warning_when_sentinel_file_vanishes_during_unlink(
    sentinel_dir: Path,
    caplog,
) -> None:
    """release() logs WARNING when the sentinel disappears between read_active and unlink.

    Covers lines 161-162: FileNotFoundError branch in release() after ownership verified.
    """
    import logging
    from unittest.mock import patch

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)

    # Write a sentinel so read_active succeeds
    sentinel_path = sentinel_dir / "_active_analysis.lock"
    import json

    sentinel_path.write_text(
        json.dumps(
            {
                "op_type": "analysis",
                "job_id": "job-vanish",
                "node_id": "node-X",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )

    # Patch os.unlink to raise FileNotFoundError (simulates race between nodes)
    with patch("os.unlink", side_effect=FileNotFoundError("gone")):
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.services.shared_job_sentinel"
        ):
            snt.release("analysis", expected_job_id="job-vanish")

    # Must log a WARNING, must NOT raise
    assert any("already gone" in r.message.lower() for r in caplog.records), (
        f"Expected 'already gone' warning, got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Line 197: is_stale handles naive datetime (no tzinfo) by treating as UTC
# ---------------------------------------------------------------------------


def test_is_stale_handles_naive_datetime(sentinel: SharedJobSentinel) -> None:
    """is_stale() correctly processes a naive (no tzinfo) started_at datetime.

    Covers line 197: the `started = started.replace(tzinfo=timezone.utc)` branch.
    """
    # Naive datetime 5 hours ago
    naive_old = datetime.utcnow() - timedelta(hours=5)
    assert naive_old.tzinfo is None  # confirm it is naive

    info = SentinelInfo(
        op_type="analysis",
        job_id="job-naive",
        node_id="node-A",
        started_at=naive_old,
    )
    # 4h timeout, 5h old naive datetime → stale
    assert sentinel.is_stale(info, 14400) is True


# ---------------------------------------------------------------------------
# Lines 216-222: _force_replace cleans up tmp file on failure and re-raises
# ---------------------------------------------------------------------------


def test_force_replace_reraises_on_os_replace_failure(sentinel_dir: Path) -> None:
    """_force_replace cleans up tmp file and re-raises when os.replace fails.

    Covers lines 216-222: the except block in _force_replace that unlinks tmp
    and re-raises the original exception.
    """
    from unittest.mock import patch

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)

    # Set up a stale sentinel so _force_replace is called
    old_started_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    import json

    sentinel_path = sentinel_dir / "_active_analysis.lock"
    sentinel_path.write_text(
        json.dumps(
            {
                "op_type": "analysis",
                "job_id": "old-job",
                "node_id": "crashed-node",
                "started_at": old_started_at,
            }
        )
    )

    # Patch os.replace to raise OSError — _force_replace must re-raise
    with patch("os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            snt.try_claim("analysis", "new-job", "new-node")


def test_force_replace_handles_unlink_failure_during_cleanup(
    sentinel_dir: Path,
) -> None:
    """_force_replace swallows OSError from tmp unlink and still re-raises original.

    Covers lines 220-221: the except OSError: pass inside the cleanup try block,
    reached when os.replace fails AND the subsequent os.unlink of tmp also fails.
    """
    from unittest.mock import patch
    import json

    snt = SharedJobSentinel(sentinel_dir=sentinel_dir, stale_timeout_seconds=14400)

    # Set up a stale sentinel so _force_replace is called
    old_started_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    sentinel_path = sentinel_dir / "_active_analysis.lock"
    sentinel_path.write_text(
        json.dumps(
            {
                "op_type": "analysis",
                "job_id": "old-job",
                "node_id": "crashed-node",
                "started_at": old_started_at,
            }
        )
    )

    # Both os.replace and os.unlink raise — the OSError from unlink must be swallowed,
    # and the original OSError from os.replace must be re-raised.
    with patch("os.replace", side_effect=OSError("replace failed")):
        with patch("os.unlink", side_effect=OSError("unlink also failed")):
            with pytest.raises(OSError, match="replace failed"):
                snt.try_claim("analysis", "new-job", "new-node")
