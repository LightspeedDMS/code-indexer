"""Tests for the NFS read-after-create visibility barrier (Bug #1084 regression).

Background
----------
Bug #1084 introduced the canonical versioned-snapshot layout
``{mount}/.versioned/{ns}/v_<ts>``. On a cow-daemon (NFS) deployment the daemon
creates the snapshot on its LOCAL XFS; the scheduler node reaches it over NFS.
Because the new canonical path nests under a freshly-created ``.versioned/`` and
``.versioned/{ns}/`` parent that the scheduler's NFS client has never looked up,
the scheduler's NFS dcache holds a NEGATIVE entry and a deep ``chdir`` lookup
ENOENTs even though the daemon already created the directory — a classic
read-after-create NFS dcache race that surfaced as::

    RuntimeError: Failed to create snapshot for cidx-meta-global:
    FileNotFoundError: [Errno 2] No such file or directory:
    '/mnt/cow-storage/.versioned/cidx-meta/v_1781127219'

The fix adds a bounded read-after-create visibility barrier that stats the
parent chain (to bust the negative dcache) and polls ``os.path.isdir(dest)``
until the path becomes visible, before any caller uses it.

These tests use injectable clock / stat seams — NO real sleeps, NO real NFS.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# wait_for_nfs_visibility helper
# ---------------------------------------------------------------------------


class TestWaitForNfsVisibilityHelper:
    """Direct unit tests for the bounded read-after-create visibility helper."""

    def test_returns_immediately_when_already_visible(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        sleeps: List[float] = []
        # isdir True on first check
        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/ns/v_1",
            timeout=15.0,
            isdir_fn=lambda p: True,
            stat_fn=lambda p: None,
            monotonic_fn=lambda: 0.0,
            sleep_fn=sleeps.append,
        )
        # No sleeping needed — already visible on the first poll.
        assert sleeps == []

    def test_blocks_until_visible_after_n_polls(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        # Path becomes isdir-true only after 3 polls (simulating NFS delay).
        calls = {"isdir": 0}

        def isdir_fn(_p: str) -> bool:
            calls["isdir"] += 1
            return calls["isdir"] >= 4

        clock = {"t": 0.0}

        def monotonic_fn() -> float:
            return clock["t"]

        sleeps: List[float] = []

        def sleep_fn(seconds: float) -> None:
            sleeps.append(seconds)
            clock["t"] += seconds

        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/ns/v_2",
            timeout=15.0,
            isdir_fn=isdir_fn,
            stat_fn=lambda p: None,
            monotonic_fn=monotonic_fn,
            sleep_fn=sleep_fn,
        )

        # 4 isdir checks (3 misses + 1 hit) -> 3 sleeps between them.
        assert calls["isdir"] == 4
        assert len(sleeps) == 3

    def test_stats_parent_chain_to_bust_negative_dcache(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        statted: List[str] = []

        # Becomes visible on the 2nd poll so at least one stat round happens.
        calls = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/cidx-meta/v_3",
            timeout=15.0,
            isdir_fn=isdir_fn,
            stat_fn=statted.append,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=sleep_fn,
        )

        # The mount root, .versioned, .versioned/{ns} and the leaf must each be
        # stat-ed to refresh the NFS dcache lookups along the chain.
        assert "/mnt/cow" in statted
        assert "/mnt/cow/.versioned" in statted
        assert "/mnt/cow/.versioned/cidx-meta" in statted
        assert "/mnt/cow/.versioned/cidx-meta/v_3" in statted

    def test_raises_runtime_error_when_never_visible_within_deadline(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        with pytest.raises(RuntimeError, match="v_never"):
            wait_for_nfs_visibility(
                "/mnt/cow/.versioned/ns/v_never",
                timeout=2.0,
                isdir_fn=lambda p: False,  # never appears
                stat_fn=lambda p: None,
                monotonic_fn=lambda: clock["t"],
                sleep_fn=sleep_fn,
            )

    def test_terminates_with_bounded_poll_count_no_infinite_loop(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        clock = {"t": 0.0}
        poll_count = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            poll_count["n"] += 1
            return False

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        with pytest.raises(RuntimeError):
            wait_for_nfs_visibility(
                "/mnt/cow/.versioned/ns/v_bound",
                timeout=1.0,
                isdir_fn=isdir_fn,
                stat_fn=lambda p: None,
                monotonic_fn=lambda: clock["t"],
                sleep_fn=sleep_fn,
            )

        # Anti-unbounded-loop #14: with a 1s deadline and sub-second backoff the
        # poll count is small and finite (definitely well under 1000).
        assert 0 < poll_count["n"] < 1000

    def test_stat_errors_are_swallowed_and_do_not_abort_wait(self):
        """A stat() that raises (e.g. ENOENT on a not-yet-visible parent) must
        not abort the wait — it is best-effort dcache busting only."""
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        calls = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        def stat_fn(_p: str) -> None:
            raise FileNotFoundError("not yet")

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        # Should complete (not raise) despite stat_fn raising every time.
        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/ns/v_staterr",
            timeout=15.0,
            isdir_fn=isdir_fn,
            stat_fn=stat_fn,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=sleep_fn,
        )
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# CowDaemonBackend integration — visibility barrier before returning
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data if json_data is not None else {}
    mock.raise_for_status = MagicMock()
    return mock


def _make_cow_backend(visibility_waiter=None):
    from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    config = CowDaemonConfig(
        daemon_url="http://daemon:8081",
        api_key="test-api-key",
        mount_point="/mnt/nfs/cidx",
        poll_interval_seconds=1,
        timeout_seconds=30,
        # Bug #1320: co-located (identity) translation -- this suite tests the
        # NFS visibility barrier, not path translation.
        daemon_storage_path="/mnt/nfs/cidx",
    )
    return CowDaemonBackend(config=config, visibility_waiter=visibility_waiter)


def _mock_requests_module(post_resp, get_resp):
    mock_req = MagicMock()
    mock_req.post.return_value = post_resp
    mock_req.get.return_value = get_resp
    return mock_req


class TestCowDaemonBackendVisibilityBarrier:
    """create_clone / create_clone_at_path must wait for NFS visibility of the
    returned dest before handing it back to the caller (Bug #1084 regression)."""

    def test_create_clone_at_path_invokes_visibility_waiter_with_dest(self):
        seen = {}

        def waiter(path: str) -> None:
            seen["path"] = path

        backend = _make_cow_backend(visibility_waiter=waiter)
        post_resp = _make_response(202, {"job_id": "j"})
        done_resp = _make_response(200, {"status": "completed", "clone_path": "x"})
        mock_req = _mock_requests_module(post_resp, done_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.create_clone_at_path(
                "/mnt/nfs/cidx/src", "/mnt/nfs/cidx/.versioned/ns/v_42"
            )

        assert result == "/mnt/nfs/cidx/.versioned/ns/v_42"
        assert seen["path"] == "/mnt/nfs/cidx/.versioned/ns/v_42"

    def test_create_clone_waits_before_returning_canonical_path(self):
        seen = {}

        def waiter(path: str) -> None:
            seen["path"] = path

        backend = _make_cow_backend(visibility_waiter=waiter)
        post_resp = _make_response(202, {"job_id": "j"})
        done_resp = _make_response(200, {"status": "completed", "clone_path": "x"})
        mock_req = _mock_requests_module(post_resp, done_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.create_clone("/mnt/nfs/cidx/src", "myns", "v_777")

        assert result == "/mnt/nfs/cidx/.versioned/myns/v_777"
        assert seen["path"] == "/mnt/nfs/cidx/.versioned/myns/v_777"

    def test_create_clone_at_path_propagates_visibility_timeout(self):
        def waiter(path: str) -> None:
            raise RuntimeError(f"NFS visibility timeout for {path}")

        backend = _make_cow_backend(visibility_waiter=waiter)
        post_resp = _make_response(202, {"job_id": "j"})
        done_resp = _make_response(200, {"status": "completed", "clone_path": "x"})
        mock_req = _mock_requests_module(post_resp, done_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(RuntimeError, match="NFS visibility timeout"):
                backend.create_clone_at_path(
                    "/mnt/nfs/cidx/src", "/mnt/nfs/cidx/.versioned/ns/v_to"
                )

    def test_default_visibility_waiter_uses_real_helper(self, tmp_path):
        """When no waiter is injected, the backend uses the real bounded helper.

        Here the dest is created on a local tmp dir BEFORE create, so the real
        helper sees it immediately and returns without raising."""
        from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        mount = tmp_path
        dest = mount / ".versioned" / "ns" / "v_real"
        dest.mkdir(parents=True)

        config = CowDaemonConfig(
            daemon_url="http://daemon:8081",
            api_key="k",
            mount_point=str(mount),
            poll_interval_seconds=1,
            timeout_seconds=30,
            # Bug #1320: co-located (identity) translation -- this test verifies
            # the real NFS visibility wait, not path translation.
            daemon_storage_path=str(mount),
        )
        backend = CowDaemonBackend(config=config)

        post_resp = _make_response(202, {"job_id": "j"})
        done_resp = _make_response(200, {"status": "completed", "clone_path": "x"})
        mock_req = _mock_requests_module(post_resp, done_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.create_clone_at_path(str(mount / "src"), str(dest))

        assert result == str(dest)


# ---------------------------------------------------------------------------
# OntapCloneBackend integration — FlexClone volumes are NFS-mounted
# ---------------------------------------------------------------------------


class TestOntapCloneBackendVisibilityBarrier:
    """ONTAP FlexClone volumes are reached over NFS, so create_clone must wait
    for the new junction path to be visible before returning (Bug #1084)."""

    def _make_ontap_backend(self, visibility_waiter=None):
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = MagicMock()
        client.create_clone.return_value = {"clone_path": "/mnt/ontap/my-clone"}
        return OntapCloneBackend(
            flexclone_client=client,
            mount_point="/mnt/ontap",
            visibility_waiter=visibility_waiter,
        )

    def test_create_clone_invokes_visibility_waiter_with_mount_path(self):
        seen = {}

        def waiter(path: str) -> None:
            seen["path"] = path

        backend = self._make_ontap_backend(visibility_waiter=waiter)
        result = backend.create_clone("/ignored/src", "ns", "my-clone")

        assert result == "/mnt/ontap/my-clone"
        assert seen["path"] == "/mnt/ontap/my-clone"

    def test_create_clone_propagates_visibility_timeout(self):
        def waiter(path: str) -> None:
            raise RuntimeError(f"NFS visibility timeout for {path}")

        backend = self._make_ontap_backend(visibility_waiter=waiter)
        with pytest.raises(RuntimeError, match="NFS visibility timeout"):
            backend.create_clone("/ignored/src", "ns", "my-clone")
