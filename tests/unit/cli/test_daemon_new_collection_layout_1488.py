"""Story #1488 / Codex Finding: the daemon-delegation `cidx index` path must
honor the explicit ``--new-collection-layout`` exactly as the foreground path
does.

Story #1488 (Finding D2) threaded the resolved
``use_chunks_db_for_new_collections`` value to every provider's
``BackendFactory.create(...)`` on the LOCAL foreground ``cidx index`` path. But
when the daemon is enabled, ``cidx index`` DELEGATES to the daemon, and that
path DROPPED the layout at three seams:

1. ``cli.py``'s daemon-delegation branch (``_index_via_daemon(...)``) did not
   forward the resolved layout.
2. The RPC mapping in ``cli_daemon_delegation.py`` (``daemon_kwargs``) omitted
   it.
3. The daemon (``daemon/service.py``'s ``exposed_index_blocking``) constructed
   ``BackendFactory`` WITHOUT the explicit param.

Consequence: with ``daemon.enabled=true`` and ``CIDX_CHUNKS_DB_NEW_COLLECTIONS``
unset, ``cidx index --new-collection-layout=chunks_db`` created a NEW semantic
collection as SHARDED_JSON instead of CHUNKS_DB -- violating AC1's explicit
layout contract. The reverse held too (daemon env enables CHUNKS_DB, caller
explicitly requests sharded_json -> wrong layout).

These tests exercise each seam of the real delegation chain with recording
spies on the collaborator boundaries (never on the SUT wiring itself),
mirroring the style of
``tests/unit/cli/test_cli_new_collection_layout_multi_provider_1488.py`` and
the direct-delegation-call convention already established in
``tests/unit/cli/test_daemon_delegation.py``.

Precedence: an explicit ``--new-collection-layout`` (True/False) must win end to
end; an ABSENT flag (None) must pass through untouched so the daemon-side
env/default applies -- identical to the foreground path.
"""

import contextlib
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Seam 3 (daemon service): exposed_index_blocking honors the explicit layout
# by passing it to BackendFactory.create.
# ---------------------------------------------------------------------------


def _make_fake_daemon_self():
    """Minimal stand-in for CidxDaemonService instances.

    ``exposed_index_blocking``'s semantic path only touches ``cache_lock`` and
    ``cache_entry`` on ``self`` -- constructing a real service spins up threads
    and a health monitor, so we call the real UNBOUND method with this tiny
    fake self and let the real method logic run for real.
    """

    class _FakeSelf:
        def __init__(self):
            self.cache_lock = threading.RLock()
            self.cache_entry = None
            # Codex Finding (#1488): exposed_index_blocking now acquires this
            # daemon-wide chunk-mutation serializer as its outermost lock
            # (`self.mutation_lock.acquire()` / `finally: release()`) around
            # the entire method body. A real RLock is required here so the
            # bound-unbound-method call against this fake self can acquire
            # and release it just like it would on a real CIDXDaemonService.
            self.mutation_lock = threading.RLock()

    return _FakeSelf()


def _make_fake_daemon_self_for_background():
    """Minimal stand-in for CidxDaemonService instances used by
    ``_run_indexing_background``.

    That method (unlike ``exposed_index_blocking``) additionally touches
    ``mutation_lock`` (the outermost daemon-wide chunk-mutation serializer,
    acquired/released around the whole method body) and
    ``indexing_lock_internal`` plus the progress/stats fields it reads and
    writes while the run executes and in its ``finally`` cleanup.
    """

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
    """Patch the collaborators exposed_index_blocking imports internally, so the
    real method reaches BackendFactory.create for real.
    """
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)

    # Bug #1718: exposed_index_blocking's semantic branch and
    # _run_indexing_background now call ConfigManager.load_verified_config()
    # directly (returning the Config itself), not
    # create_with_backtrack().get_config().
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


class TestDaemonHonorsNewCollectionLayout:
    @pytest.mark.parametrize(
        "layout_value, expected",
        [
            (True, True),  # --new-collection-layout=chunks_db
            (False, False),  # --new-collection-layout=sharded_json (explicit beats env)
            (None, None),  # flag absent -> daemon-side env/default applies
        ],
    )
    def test_exposed_index_blocking_passes_layout_to_backend_factory(
        self, tmp_path, layout_value, expected
    ):
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []

        def backend_spy(*args, **kwargs):
            recorded.append(kwargs.get("use_chunks_db_for_new_collections"))
            backend = MagicMock()
            backend.get_vector_store_client.return_value = MagicMock()
            return backend

        with _daemon_index_env(tmp_path, backend_spy):
            result = CIDXDaemonService.exposed_index_blocking(
                _make_fake_daemon_self(),
                project_path=str(tmp_path),
                callback=None,
                use_chunks_db_for_new_collections=layout_value,
            )

        assert result["status"] == "completed", (
            f"Daemon indexing did not complete cleanly: {result}"
        )
        assert recorded == [expected], (
            "The daemon's exposed_index_blocking must pass the caller's explicit "
            f"use_chunks_db_for_new_collections={expected!r} through to "
            f"BackendFactory.create; got {recorded}. Dropping it silently "
            "misclassifies a fresh collection's layout (Codex Finding)."
        )

    @pytest.mark.parametrize(
        "layout_value, expected",
        [
            (True, True),  # --new-collection-layout=chunks_db
            (False, False),  # --new-collection-layout=sharded_json (explicit beats env)
            (None, None),  # flag absent -> daemon-side env/default applies
        ],
    )
    def test_run_indexing_background_passes_layout_to_backend_factory(
        self, tmp_path, layout_value, expected
    ):
        """The BACKGROUND index path (`exposed_index` -> `_run_indexing_background`)
        must honor the caller's explicit layout exactly like the BLOCKING path
        (`exposed_index_blocking`) already does. `kwargs` is in scope in
        `_run_indexing_background` (passed by `exposed_index` as
        `args=(project_path, kwargs)`), but the BackendFactory.create call there
        was constructed WITHOUT it -- silently misclassifying a fresh
        collection's layout when a daemon BACKGROUND index is used.
        """
        from code_indexer.daemon.service import CIDXDaemonService

        recorded = []

        def backend_spy(*args, **kwargs):
            recorded.append(kwargs.get("use_chunks_db_for_new_collections"))
            backend = MagicMock()
            backend.get_vector_store_client.return_value = MagicMock()
            return backend

        fake_self = _make_fake_daemon_self_for_background()

        with _daemon_index_env(tmp_path, backend_spy):
            CIDXDaemonService._run_indexing_background(
                fake_self,
                str(tmp_path),
                {"use_chunks_db_for_new_collections": layout_value},
            )

        assert fake_self.indexing_error is None, (
            f"Background indexing failed unexpectedly: {fake_self.indexing_error}"
        )
        assert recorded == [expected], (
            "The daemon's _run_indexing_background must pass the caller's explicit "
            f"use_chunks_db_for_new_collections={expected!r} through to "
            f"BackendFactory.create; got {recorded}. Dropping it silently "
            "misclassifies a fresh collection's layout on the BACKGROUND index "
            "path."
        )


# ---------------------------------------------------------------------------
# Seam 2 (delegation RPC): _index_via_daemon forwards the layout into the RPC
# kwargs that reach the daemon.
# ---------------------------------------------------------------------------


class TestDelegationForwardsNewCollectionLayout:
    @pytest.mark.parametrize("layout_value", [True, False, None])
    def test_index_via_daemon_forwards_layout_in_rpc_kwargs(
        self, tmp_path, layout_value
    ):
        from code_indexer import cli_daemon_delegation

        recorded_rpc_kwargs = {}

        def fake_exposed_index_blocking(*args, **kwargs):
            recorded_rpc_kwargs.update(kwargs)
            return {
                "status": "completed",
                "stats": {
                    "files_processed": 1,
                    "chunks_created": 1,
                    "failed_files": 0,
                    "duration_seconds": 1,
                    "cancelled": False,
                },
            }

        fake_conn = MagicMock()
        fake_conn.root.exposed_index_blocking.side_effect = fake_exposed_index_blocking

        with (
            patch(
                "code_indexer.cli_daemon_delegation._find_config_file",
                return_value=tmp_path / ".code-indexer" / "config.json",
            ),
            patch(
                "code_indexer.cli_daemon_delegation._get_socket_path",
                return_value=str(tmp_path / "daemon.sock"),
            ),
            patch(
                "code_indexer.cli_daemon_delegation._connect_to_daemon",
                return_value=fake_conn,
            ),
            patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
            patch("code_indexer.progress.MultiThreadedProgressManager"),
        ):
            cli_daemon_delegation._index_via_daemon(
                force_reindex=False,
                daemon_config={"startup_timeout": 1.0, "retry_delays": [0.0]},
                enable_fts=False,
                batch_size=50,
                reconcile=False,
                index_commits=False,
                use_chunks_db_for_new_collections=layout_value,
            )

        assert "use_chunks_db_for_new_collections" in recorded_rpc_kwargs, (
            "_index_via_daemon must forward use_chunks_db_for_new_collections into "
            f"the daemon RPC kwargs; got keys {sorted(recorded_rpc_kwargs)}."
        )
        assert (
            recorded_rpc_kwargs["use_chunks_db_for_new_collections"] is layout_value
        ), (
            "The layout forwarded over the RPC boundary must match the caller's "
            f"value {layout_value!r}; got "
            f"{recorded_rpc_kwargs.get('use_chunks_db_for_new_collections')!r}."
        )


# ---------------------------------------------------------------------------
# Seam 1 (cli): `cidx index` in daemon mode resolves --new-collection-layout and
# passes the resolved bool/None into _index_via_daemon.
# ---------------------------------------------------------------------------


def _make_daemon_config():
    cfg = MagicMock()
    cfg.codebase_dir = "/tmp/does-not-matter"
    cfg.daemon = MagicMock()
    cfg.daemon.enabled = True
    cfg.daemon.model_dump.return_value = {"startup_timeout": 1.0, "retry_delays": [0.0]}
    return cfg


class TestCliResolvesLayoutForDaemon:
    @pytest.mark.parametrize(
        "layout_arg, expected",
        [
            ("chunks_db", True),
            ("sharded_json", False),
            (None, None),
        ],
    )
    def test_cli_index_daemon_mode_passes_resolved_layout(
        self, tmp_path, layout_arg, expected
    ):
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text("{}")

        cfg = _make_daemon_config()
        recorded = {}

        def fake_index_via_daemon(*args, **kwargs):
            recorded.update(kwargs)
            return 0

        with (
            patch("code_indexer.cli.ConfigManager") as mock_cm,
            patch(
                "code_indexer.cli._install_embedding_stats_writer_for_index",
            ),
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                side_effect=fake_index_via_daemon,
            ),
        ):
            mock_cm.create_with_backtrack.return_value.load.return_value = cfg
            mock_cm.create_with_backtrack.return_value.config_path = (
                config_dir / "config.json"
            )

            from click.testing import CliRunner
            from code_indexer.cli import cli

            argv = ["index"]
            if layout_arg is not None:
                argv += ["--new-collection-layout", layout_arg]

            result = CliRunner().invoke(cli, argv)

        assert result.exit_code == 0, (
            f"cidx index (daemon mode) exited {result.exit_code}\n{result.output}"
        )
        assert recorded.get("use_chunks_db_for_new_collections") is expected, (
            "cli.py's daemon-delegation branch must resolve --new-collection-layout "
            f"and pass use_chunks_db_for_new_collections={expected!r} into "
            f"_index_via_daemon; got "
            f"{recorded.get('use_chunks_db_for_new_collections')!r}. "
            "Dropping it means the daemon builds the collection in the wrong layout."
        )
