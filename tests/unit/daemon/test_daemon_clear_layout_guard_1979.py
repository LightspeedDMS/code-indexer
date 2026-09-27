"""Bug #1979 round 8 (Codex round-7 P2, coordinator ruling): cheap
defense-in-depth in the daemon's own semantic-indexing entry points, so a
full clear (``force_full=True``) delivered directly over the daemon RPC
(bypassing the CLI's own `--clear` guard in `cli.py`, rounds 6/7) can never
silently rebuild a collection on the legacy SHARDED_JSON layout.

The coordinator verified `cli_daemon_delegation.py` (the CLI, over a
per-user local socket) is the ONLY caller of `exposed_index_blocking` /
`exposed_index`, so the authoritative "every configured provider was
genuinely rebuilt" verification correctly stays in the CLI process after
delegation returns (`provider_rebuild_check.find_providers_not_rebuilt_since`)
and is NOT moved into the daemon. This test module only proves the daemon's
own two entry points that hand the layout to `BackendFactory.create`
(`exposed_index_blocking`'s semantic branch and `_run_indexing_background`)
refuse the one combination the maintainer's acceptance criterion forbids,
and otherwise default `force_full=True` + no explicit layout to CHUNKS_DB --
matching the CLI's own `--clear` default (Bug #1979 round 6).

Setup mirrors the established pattern in
tests/unit/cli/test_daemon_new_collection_layout_1488.py: a minimal fake
`self`, real collaborators except `BackendFactory.create` (a recording spy)
and the heavy I/O-bound ones (config load, embedding provider, SmartIndexer),
so the real method logic under test runs for real.
"""

from __future__ import annotations

import contextlib
import threading
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_daemon_self():
    class _FakeSelf:
        def __init__(self):
            self.cache_lock = threading.RLock()
            self.cache_entry = None
            self.mutation_lock = threading.RLock()

    return _FakeSelf()


def _make_fake_daemon_self_for_background():
    class _FakeSelf:
        def __init__(self):
            self.cache_lock = threading.RLock()
            self.cache_entry = None
            self.mutation_lock = threading.RLock()
            self.indexing_lock_internal = threading.Lock()
            self.current_files_processed = 0
            self.total_files = 0
            self.indexing_error = None
            self.indexing_stats = None
            self.indexing_thread = None
            self.indexing_project_path = None

    return _FakeSelf()


@contextlib.contextmanager
def _daemon_index_env(tmp_path, backend_spy):
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)

    fake_config = MagicMock()

    fake_stats = MagicMock()
    fake_stats.files_processed = 3
    fake_stats.chunks_created = 12
    fake_stats.failed_files = 0
    fake_stats.duration = 1.0
    fake_stats.cancelled = False

    fake_indexer = MagicMock()
    fake_indexer.smart_index.return_value = fake_stats

    with (
        patch(
            "code_indexer.config.ConfigManager.load_verified_config",
            return_value=fake_config,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            side_effect=backend_spy,
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=fake_indexer,
        ),
    ):
        yield


def _backend_spy(recorded):
    def _spy(*args, **kwargs):
        recorded.append(kwargs.get("use_chunks_db_for_new_collections"))
        backend = MagicMock()
        backend.get_vector_store_client.return_value = MagicMock()
        return backend

    return _spy


class TestExposedIndexBlockingClearLayoutGuard:
    def test_force_full_with_false_layout_is_refused(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            result = CIDXDaemonService.exposed_index_blocking(
                _make_fake_daemon_self(),
                project_path=str(tmp_path),
                callback=None,
                force_full=True,
                use_chunks_db_for_new_collections=False,
            )

        assert result["status"] != "completed", (
            f"force_full=True with an explicit legacy sharded_json layout "
            f"must never report completed; got {result}"
        )
        assert recorded == [], (
            "BackendFactory.create must never be reached for the refused "
            f"combination; got calls with {recorded}"
        )

    def test_force_full_with_none_layout_defaults_to_chunks_db(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            result = CIDXDaemonService.exposed_index_blocking(
                _make_fake_daemon_self(),
                project_path=str(tmp_path),
                callback=None,
                force_full=True,
                use_chunks_db_for_new_collections=None,
            )

        assert result["status"] == "completed", (
            f"a valid force_full=True request must complete; got {result}"
        )
        assert recorded == [True], (
            "force_full=True with no explicit layout must default to "
            f"CHUNKS_DB (True); got {recorded}"
        )

    def test_non_full_index_with_none_layout_stays_none(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            result = CIDXDaemonService.exposed_index_blocking(
                _make_fake_daemon_self(),
                project_path=str(tmp_path),
                callback=None,
                force_full=False,
                use_chunks_db_for_new_collections=None,
            )

        assert result["status"] == "completed", (
            f"a non-clear request must complete unaffected; got {result}"
        )
        assert recorded == [None], (
            "A non-full index with no explicit layout must be unaffected -- "
            f"still None (ambient default); got {recorded}"
        )


class TestRunIndexingBackgroundClearLayoutGuard:
    def test_force_full_with_false_layout_is_refused(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []
        fake_self = _make_fake_daemon_self_for_background()

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            CIDXDaemonService._run_indexing_background(
                fake_self,
                str(tmp_path),
                {
                    "force_full": True,
                    "use_chunks_db_for_new_collections": False,
                },
            )

        assert fake_self.indexing_error is not None, (
            "force_full=True with an explicit legacy sharded_json layout "
            "must record an error, never complete silently"
        )
        assert fake_self.indexing_stats is None
        assert recorded == [], (
            f"BackendFactory.create must never be reached; got {recorded}"
        )

    def test_force_full_with_none_layout_defaults_to_chunks_db(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []
        fake_self = _make_fake_daemon_self_for_background()

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            CIDXDaemonService._run_indexing_background(
                fake_self,
                str(tmp_path),
                {
                    "force_full": True,
                    "use_chunks_db_for_new_collections": None,
                },
            )

        assert fake_self.indexing_error is None, (
            f"Background indexing failed unexpectedly: {fake_self.indexing_error}"
        )
        assert recorded == [True], (
            "force_full=True with no explicit layout must default to "
            f"CHUNKS_DB (True); got {recorded}"
        )

    def test_non_full_index_with_none_layout_stays_none(self, tmp_path):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []
        fake_self = _make_fake_daemon_self_for_background()

        with _daemon_index_env(tmp_path, _backend_spy(recorded)):
            CIDXDaemonService._run_indexing_background(
                fake_self,
                str(tmp_path),
                {
                    "force_full": False,
                    "use_chunks_db_for_new_collections": None,
                },
            )

        assert fake_self.indexing_error is None, (
            f"Background indexing failed unexpectedly: {fake_self.indexing_error}"
        )
        assert recorded == [None], (
            f"A non-full index must be unaffected -- still None; got {recorded}"
        )


class TestResolveDaemonClearLayoutHelper:
    """Direct unit coverage of the pure helper function itself."""

    def test_non_full_passes_through_unchanged(self):
        from code_indexer.daemon.service import resolve_daemon_clear_layout

        assert resolve_daemon_clear_layout(False, None) is None
        assert resolve_daemon_clear_layout(False, True) is True
        assert resolve_daemon_clear_layout(False, False) is False

    def test_full_clear_with_none_defaults_to_true(self):
        from code_indexer.daemon.service import resolve_daemon_clear_layout

        assert resolve_daemon_clear_layout(True, None) is True

    def test_full_clear_with_true_stays_true(self):
        from code_indexer.daemon.service import resolve_daemon_clear_layout

        assert resolve_daemon_clear_layout(True, True) is True

    def test_full_clear_with_false_raises(self):
        from code_indexer.daemon.service import resolve_daemon_clear_layout

        with pytest.raises(ValueError):
            resolve_daemon_clear_layout(True, False)
