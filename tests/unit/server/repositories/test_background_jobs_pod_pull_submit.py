"""cluster-mode submit leaves pod-pull ops PENDING (not enqueued locally)."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    POD_PULL_OPS,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


@pytest.mark.slow
class TestPodPullSubmit:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage = Path(self.temp_dir) / "jobs.json"
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _manager(self, cluster_mode: bool):
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.return_value = MagicMock()
        self.manager = BackgroundJobManager(
            storage_path=str(self.storage),
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=1
            ),
            job_tracker=tracker,
            cluster_mode=cluster_mode,
        )
        # Neutralize actual execution and spy on local-pool enqueues.
        self.manager._execute_job = lambda *a, **k: None
        self.enqueued: List[Any] = []
        orig = self.manager._pending_job_queue.put

        def spy(item, *a, **k):
            if item is not None:
                self.enqueued.append(item)
            return orig(item, *a, **k)

        self.manager._pending_job_queue.put = spy
        return self.manager, tracker

    def _submit(self, m, op, *, metadata=None):
        return m.submit_job(
            operation_type=op,
            func=lambda progress_callback=None: {"ok": True},
            submitter_username="admin",
            is_admin=True,
            repo_alias="repoA",
            metadata=metadata,
        )

    def test_cluster_pod_pull_op_with_metadata_not_enqueued(self):
        m, tracker = self._manager(cluster_mode=True)
        job_id = self._submit(m, "add_golden_repo", metadata={"alias": "repoA"})
        assert job_id  # returns id
        tracker.register_job_if_no_conflict.assert_called_once()
        # Left PENDING for pod-pull — NOT put on the local pool.
        assert all(item[0] != job_id for item in self.enqueued)
        assert self.enqueued == []

    def test_cluster_pod_pull_without_metadata_falls_back_to_local(self):
        m, _ = self._manager(cluster_mode=True)
        job_id = self._submit(m, "add_golden_repo", metadata=None)
        assert any(item[0] == job_id for item in self.enqueued)

    def test_cluster_cheap_op_still_enqueued(self):
        m, _ = self._manager(cluster_mode=True)
        # xray_search is not a pod-pull op.
        job_id = self._submit(m, "xray_search", metadata={"x": 1})
        assert any(item[0] == job_id for item in self.enqueued)

    def test_non_cluster_pod_pull_op_still_enqueued(self):
        m, _ = self._manager(cluster_mode=False)
        job_id = self._submit(m, "add_golden_repo", metadata={"alias": "repoA"})
        assert any(item[0] == job_id for item in self.enqueued)

<<<<<<< HEAD
    def test_the_exact_pod_pull_op_set(self):
        # The memory-heavy index builds (incl. semantic_backfill_reindex — a re-embed is
        # as heavy as add), PLUS two that are pod-pull WITHOUT being memory-heavy:
        # fts_backfill_build (EVO-66445, a cheap local tantivy rebuild) and
        # global_repo_refresh (background-work audit — _MEMORY_HEAVY_OPS gates the LOCAL
        # pool worker, which a pod-pull row never reaches, and adding refresh there would
        # newly throttle a solo/sqlite server that has always run them ungated; the
        # cluster backpressure arrives anyway because DistributedJobClaimer memory-gates
        # EVERY claim).
        #
        # Spelled out in full on purpose: every entry must also have a lifespan dispatch
        # executor (validate_dispatch_covers enforces that pairing at startup) and a
        # submit call that passes metadata=, so adding one is never a one-line change.
=======
    def test_all_five_heavy_ops_are_pod_pull(self):
>>>>>>> upstream/master
        assert POD_PULL_OPS == frozenset(
            {
                "add_golden_repo",
                "provider_index_add",
                "provider_temporal_index_rebuild",
                "sync_repository",
                "change_branch",
<<<<<<< HEAD
                "semantic_backfill_reindex",
                "fts_backfill_build",
                "global_repo_refresh",
            }
        )

    def test_semantic_backfill_reindex_is_pod_pull_and_memory_heavy(self):
        from code_indexer.server.repositories.background_jobs import _MEMORY_HEAVY_OPS

        # Unlike the light FTS build, a semantic re-embed IS memory-gated.
        assert "semantic_backfill_reindex" in POD_PULL_OPS
        assert "semantic_backfill_reindex" in _MEMORY_HEAVY_OPS

    def test_fts_backfill_is_pod_pull_but_not_memory_heavy(self):
        from code_indexer.server.repositories.background_jobs import _MEMORY_HEAVY_OPS

        assert "fts_backfill_build" in POD_PULL_OPS
        assert "fts_backfill_build" not in _MEMORY_HEAVY_OPS  # not memory-gated
=======
            }
        )
>>>>>>> upstream/master
