"""NFS read-after-create visibility: parent-readdir cache-busting + runtime timeout.

Staging (post daemon-500 fix) PROVED that under CONCURRENT load (a large langfuse
reflink running simultaneously with another create) a freshly-created versioned
directory can stay invisible to the scheduler node for >15s over NFS — the NFS
client's directory ATTRIBUTE cache / negative-lookup cache is stale and a bare
``os.stat`` (GETATTR) on the parent does not refresh the parent's directory entry
list. A READDIR RPC (``os.listdir`` / ``os.scandir``) on the immediate parent
refreshes the client's directory-entry cache far more aggressively, so a child the
daemon already created becomes visible immediately.

These tests use injectable clock / stat / listdir seams — NO real sleeps, NO real
NFS. They drive:

1. The barrier forces a READDIR of the immediate parent on each poll (the
   stat-only code path would NOT call listdir → RED).
2. The READDIR busting actually makes the child appear (isdir flips True only
   once listdir(parent) has run).
3. listdir errors are tolerated (parent itself momentarily not-yet-visible).
4. The default timeout is raised to 60.0s.
5. The runtime config knob ``nfs_visibility_timeout_seconds`` (default 60.0,
   NOT bootstrap) drives the deadline, with the module constant as fallback.
"""

from __future__ import annotations

from typing import List


class TestParentReaddirBustsDirCache:
    """The barrier must READDIR the immediate parent each poll to bust the NFS
    directory-entry cache under concurrent-load staleness."""

    def test_listdir_of_parent_called_each_poll(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        listed: List[str] = []
        # Becomes visible on the 3rd poll so several readdir rounds happen.
        calls = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        def record_listdir(p: str) -> list:
            listed.append(p)
            return []

        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/cidx-meta/v_readdir",
            timeout=60.0,
            isdir_fn=isdir_fn,
            stat_fn=lambda p: None,
            listdir_fn=record_listdir,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=sleep_fn,
        )

        # The immediate parent must be READDIR'd on EVERY poll (3 polls here).
        parent = "/mnt/cow/.versioned/cidx-meta"
        assert listed.count(parent) == 3, (
            f"expected immediate parent readdir on each of 3 polls, got {listed}"
        )

    def test_child_becomes_visible_only_after_parent_readdir(self):
        """Simulate the real bug: isdir(leaf) stays False until listdir(parent)
        has run (the READDIR is what refreshes the dir-entry cache). Stat-only
        code (no listdir call) would loop until timeout → this proves the readdir
        is load-bearing."""
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        parent = "/mnt/cow/.versioned/cidx-meta"
        leaf = f"{parent}/v_lazy"
        state = {"readdir_ran": False}

        def listdir_fn(p: str) -> list:
            if p == parent:
                state["readdir_ran"] = True
            return []

        def isdir_fn(p: str) -> bool:
            # Leaf only resolvable AFTER a readdir of its parent refreshed cache.
            return p == leaf and state["readdir_ran"]

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        # Must succeed (not raise) precisely because listdir(parent) is called.
        wait_for_nfs_visibility(
            leaf,
            timeout=60.0,
            isdir_fn=isdir_fn,
            stat_fn=lambda p: None,
            listdir_fn=listdir_fn,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=sleep_fn,
        )
        assert state["readdir_ran"] is True

    def test_listdir_errors_are_swallowed(self):
        """A listdir() that raises (parent itself momentarily not-yet-visible)
        must not abort the wait — keep polling."""
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        calls = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        def listdir_fn(_p: str) -> list:
            raise FileNotFoundError("parent not yet visible")

        clock = {"t": 0.0}

        def sleep_fn(seconds: float) -> None:
            clock["t"] += seconds

        # Completes despite listdir raising every time.
        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/ns/v_lderr",
            timeout=60.0,
            isdir_fn=isdir_fn,
            stat_fn=lambda p: None,
            listdir_fn=listdir_fn,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=sleep_fn,
        )
        assert calls["n"] == 2

    def test_listdir_generic_oserror_swallowed(self):
        from code_indexer.server.storage.shared.nfs_visibility import (
            wait_for_nfs_visibility,
        )

        calls = {"n": 0}

        def isdir_fn(_p: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        def listdir_fn(_p: str) -> list:
            raise OSError("stale NFS handle")

        clock = {"t": 0.0}
        wait_for_nfs_visibility(
            "/mnt/cow/.versioned/ns/v_oserr",
            timeout=60.0,
            isdir_fn=isdir_fn,
            stat_fn=lambda p: None,
            listdir_fn=listdir_fn,
            monotonic_fn=lambda: clock["t"],
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        assert calls["n"] == 2


class TestVisibilityTimeoutDefault:
    def test_module_default_timeout_is_sixty_seconds(self):
        from code_indexer.server.storage.shared import nfs_visibility

        assert nfs_visibility.NFS_VISIBILITY_TIMEOUT_SECONDS == 60.0


class TestVisibilityTimeoutConfigKnob:
    def test_server_config_default_is_sixty(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test")
        assert cfg.nfs_visibility_timeout_seconds == 60.0

    def test_knob_is_runtime_not_bootstrap(self):
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "nfs_visibility_timeout_seconds" not in BOOTSTRAP_KEYS

    def test_configured_timeout_reads_config_knob(self):
        from unittest.mock import MagicMock, patch

        from code_indexer.server.storage.shared import nfs_visibility

        fake_cfg = MagicMock()
        fake_cfg.nfs_visibility_timeout_seconds = 42.0
        fake_service = MagicMock()
        fake_service.get_config.return_value = fake_cfg

        with patch(
            "code_indexer.server.storage.shared.nfs_visibility.get_config_service",
            return_value=fake_service,
        ):
            assert nfs_visibility._configured_visibility_timeout() == 42.0

    def test_configured_timeout_falls_back_to_constant_on_error(self):
        from unittest.mock import patch

        from code_indexer.server.storage.shared import nfs_visibility

        with patch(
            "code_indexer.server.storage.shared.nfs_visibility.get_config_service",
            side_effect=RuntimeError("no config service yet"),
        ):
            assert (
                nfs_visibility._configured_visibility_timeout()
                == nfs_visibility.NFS_VISIBILITY_TIMEOUT_SECONDS
            )

    def test_configured_timeout_falls_back_on_nonpositive(self):
        """A non-positive / nonsense configured value falls back to the safe
        constant (never a zero/negative deadline that would instantly time out)."""
        from unittest.mock import MagicMock, patch

        from code_indexer.server.storage.shared import nfs_visibility

        fake_cfg = MagicMock()
        fake_cfg.nfs_visibility_timeout_seconds = 0.0
        fake_service = MagicMock()
        fake_service.get_config.return_value = fake_cfg

        with patch(
            "code_indexer.server.storage.shared.nfs_visibility.get_config_service",
            return_value=fake_service,
        ):
            assert (
                nfs_visibility._configured_visibility_timeout()
                == nfs_visibility.NFS_VISIBILITY_TIMEOUT_SECONDS
            )


class TestBackendDefaultWaiterUsesConfiguredTimeout:
    """The backends' DEFAULT visibility waiter must pass the runtime-configured
    timeout (read at call time, so the Web UI knob / hot-reload drives it) to
    ``wait_for_nfs_visibility`` — NOT the hardcoded module constant."""

    def test_cow_daemon_default_waiter_passes_configured_timeout(self):
        import sys
        from unittest.mock import MagicMock, patch

        from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        config = CowDaemonConfig(
            daemon_url="http://daemon:8081",
            api_key="k",
            mount_point="/mnt/nfs/cidx",
            poll_interval_seconds=1,
            timeout_seconds=30,
        )
        backend = CowDaemonBackend(config=config)  # no injected waiter -> default

        captured = {}

        def fake_wait(path, *, timeout, **kwargs):
            captured["timeout"] = timeout

        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"job_id": "j"}
        post_resp.raise_for_status = MagicMock()
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"status": "completed", "clone_path": "x"}
        get_resp.raise_for_status = MagicMock()
        mock_req = MagicMock()
        mock_req.post.return_value = post_resp
        mock_req.get.return_value = get_resp

        with patch.dict(sys.modules, {"requests": mock_req}):
            with patch(
                "code_indexer.server.storage.shared.clone_backend.wait_for_nfs_visibility",
                side_effect=fake_wait,
            ):
                with patch(
                    "code_indexer.server.storage.shared.clone_backend._configured_visibility_timeout",
                    return_value=37.5,
                ):
                    backend.create_clone_at_path(
                        "/mnt/nfs/cidx/src", "/mnt/nfs/cidx/.versioned/ns/v_cfg"
                    )

        assert captured["timeout"] == 37.5

    def test_ontap_default_waiter_passes_configured_timeout(self):
        from unittest.mock import MagicMock, patch

        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = MagicMock()
        client.create_clone.return_value = {"clone_path": "/mnt/ontap/c"}
        backend = OntapCloneBackend(
            flexclone_client=client,
            mount_point="/mnt/ontap",
        )  # no injected waiter -> default

        captured = {}

        def fake_wait(path, *, timeout, **kwargs):
            captured["timeout"] = timeout

        with patch(
            "code_indexer.server.storage.shared.clone_backend.wait_for_nfs_visibility",
            side_effect=fake_wait,
        ):
            with patch(
                "code_indexer.server.storage.shared.clone_backend._configured_visibility_timeout",
                return_value=21.0,
            ):
                backend.create_clone("/ignored", "ns", "c")

        assert captured["timeout"] == 21.0


class TestRefreshSchedulerPassesConfiguredTimeout:
    """RefreshScheduler._create_snapshot must pass the runtime-configured timeout
    to the defense-in-depth visibility barrier."""

    def test_create_snapshot_passes_configured_timeout(self, tmp_path):
        import shutil
        import time as _time
        from unittest.mock import MagicMock, patch

        from code_indexer.config import ConfigManager
        from code_indexer.global_repos.cleanup_manager import CleanupManager
        from code_indexer.global_repos.global_registry import GlobalRegistry
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        golden_repos_dir = tmp_path / "golden_repos"
        golden_repos_dir.mkdir(parents=True)
        src = tmp_path / "source_repo"
        src.mkdir()
        (src / "README.md").write_text("# Test")
        (src / ".git").mkdir()

        from pathlib import Path as _P

        snap_mgr = MagicMock()

        def _create_snapshot(repo_name, source_path):
            vp = golden_repos_dir / ".versioned" / repo_name / f"v_{int(_time.time())}"
            vp.mkdir(parents=True, exist_ok=True)
            for item in _P(source_path).iterdir():
                dest = vp / item.name
                if item.is_dir():
                    shutil.copytree(str(item), str(dest))
                else:
                    shutil.copy2(str(item), str(dest))
            (vp / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            return str(vp)

        snap_mgr.create_snapshot.side_effect = _create_snapshot

        query_tracker = QueryTracker()
        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
            query_tracker=query_tracker,
            cleanup_manager=CleanupManager(query_tracker),
            registry=GlobalRegistry(str(golden_repos_dir)),
            snapshot_manager=snap_mgr,
        )

        captured = {}

        def fake_wait(path, *, timeout, **kwargs):
            captured["timeout"] = timeout

        def fake_subprocess(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch(
            "code_indexer.global_repos.refresh_scheduler.wait_for_nfs_visibility",
            side_effect=fake_wait,
        ):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=fake_subprocess,
            ):
                with patch(
                    "code_indexer.global_repos.refresh_scheduler._configured_visibility_timeout",
                    return_value=55.0,
                ):
                    scheduler._create_snapshot("myrepo-global", str(src))

        assert captured["timeout"] == 55.0
