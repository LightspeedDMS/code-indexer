"""
Bug #1388 (remediation after review rejection, refresh-path gap): forward
the HNSW finalize-time orphan detect+repair marker
(HNSW_ORPHAN_REPAIR_MARKER, storage/hnsw_index_manager.py) from a `cidx
index` child subprocess into the SERVER's own admin-visible logs.db -- for
the REFRESH path (global_repos/refresh_scheduler.py), not just the
golden-repo add/registration path.

REJECTED first attempt: `_wrap_progress_callback_with_hnsw_orphan_logging`
wrapped `progress_callback` and detected the marker via a `detail=` kwarg
that only arrived if the marker survived the --progress-json wire protocol
end to end -- which it never did (see
tests/unit/storage/test_hnsw_index_manager_1388_orphan_marker.py and
tests/unit/services/test_progress_subprocess_runner_hnsw_orphan_forwarding_1388.py
for the two independent real-boundary gates that dropped it).

This remediation reuses `_make_hnsw_orphan_event_logger` (defined once in
golden_repo_manager.py, imported here -- Messi Rule #4 anti-duplication,
never a second copy) to build an alias-bound `callable(line: str) -> None`
threaded through `_execute_refresh` -> `_index_source` -> `_run_popen_c`
into the NEW `orphan_event_callback` parameter of run_with_popen_progress.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.repositories.golden_repo_manager import (
    _make_hnsw_orphan_event_logger,
)
from code_indexer.storage.hnsw_index_manager import (
    HNSWIndexManager,
    HNSW_ORPHAN_REPAIR_MARKER,
)
from tests.utils.hnsw_orphan_corpus import near_tie_corpus

_ALIAS_BASE = "typer"
CORPUS_DIM = 1024
_MARKER_LINE = (
    f"{HNSW_ORPHAN_REPAIR_MARKER}: context=rebuild_from_vectors:/x/y "
    f"orphan_count=3 repaired=true"
)


@pytest.fixture
def golden_repos_dir(tmp_path):
    grd = tmp_path / "golden_repos"
    grd.mkdir(parents=True)
    return grd


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def registry(golden_repos_dir):
    return GlobalRegistry(str(golden_repos_dir))


@pytest.fixture
def scheduler(golden_repos_dir, config_mgr, query_tracker, cleanup_manager, registry):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )


def _write_alias_pointer(
    golden_repos_dir: Path, alias_name: str, target_path: str
) -> None:
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(exist_ok=True)
    (aliases_dir / f"{alias_name}.json").write_text(
        json.dumps({"target_path": target_path})
    )


def test_refresh_scheduler_module_reuses_the_same_factory_object():
    """Messi Rule #4 anti-duplication: refresh_scheduler.py must import and
    reuse the SAME factory golden_repo_manager.py defines -- never a
    second, drifted copy."""
    import code_indexer.global_repos.refresh_scheduler as rs_module

    assert hasattr(rs_module, "_make_hnsw_orphan_event_logger"), (
        "refresh_scheduler.py must import _make_hnsw_orphan_event_logger "
        "from golden_repo_manager.py (reuse, not duplication)."
    )
    assert rs_module._make_hnsw_orphan_event_logger is _make_hnsw_orphan_event_logger, (
        "refresh_scheduler.py must reuse the exact same function object, not a copy."
    )


def test_execute_refresh_wires_alias_bound_orphan_event_callback_to_subprocess_boundary(
    scheduler, registry, golden_repos_dir, tmp_path, caplog
):
    """Wiring test: _execute_refresh's REAL flow, driving the REAL
    _index_source (SUT, not mocked), must thread an alias-bound
    orphan_event_callback all the way down to the shared
    run_with_popen_progress subprocess boundary. Only external
    collaborators are mocked: git pull/updater, alias swap, cleanup
    scheduling, and the module-level subprocess runner.
    """
    alias_name = f"{_ALIAS_BASE}-refresh-wiring-global"
    repo_name = alias_name.removesuffix("-global")
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()

    registry.register_global_repo(
        repo_name,
        alias_name,
        "git@github.com:org/repo.git",
        str(source_repo),
    )
    _write_alias_pointer(golden_repos_dir, alias_name, str(source_repo))

    popen_calls: list = []

    def _capture_popen(*, command, phase_name, orphan_event_callback=None, **kwargs):
        popen_calls.append(orphan_event_callback)
        return 100

    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_capture_popen,
        ),
        patch.object(
            scheduler, "_create_snapshot", return_value=str(tmp_path / "snap")
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_check_extension_drift", return_value=False),
        patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_gpu,
        caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.repositories.golden_repo_manager",
        ),
    ):
        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = True
        mock_gpu.return_value = mock_updater

        scheduler._execute_refresh(alias_name)

        assert len(popen_calls) >= 1, (
            "expected at least one run_with_popen_progress call"
        )
        orphan_event_callback = popen_calls[0]
        assert orphan_event_callback is not None, (
            "_execute_refresh must thread a real orphan_event_callback down "
            "to run_with_popen_progress"
        )

        # Simulate the marker arriving exactly as run_with_popen_progress's
        # real stderr scraper forwards it -- invoked INSIDE the at_level
        # block so caplog's effective level is still raised to INFO.
        orphan_event_callback(_MARKER_LINE)

    matching = [
        r
        for r in caplog.records
        if r.name == "code_indexer.server.repositories.golden_repo_manager"
        and alias_name in r.getMessage()
        and HNSW_ORPHAN_REPAIR_MARKER in r.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one orphan-repair marker re-logged tagged with "
        f"alias {alias_name!r} during REFRESH, got records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_real_near_tie_corpus_orphan_marker_forwarded_during_refresh(
    scheduler, registry, golden_repos_dir, tmp_path, caplog
):
    """Deeper regression test: drives the REAL _execute_refresh /
    _index_source / _run_popen_c plumbing (not faked) so a genuine
    HNSWIndexManager orphan detect+repair event -- produced by the SAME
    near-tie corpus recipe proven in
    tests/unit/storage/test_hnsw_index_manager_1388_orphan_marker.py to
    reproducibly orphan a real, single-threaded hnswlib build -- flows
    through refresh_scheduler.py's actual orphan_event_callback wiring
    chain into the server's admin-visible logger, tagged with the repo
    alias.

    Only the subprocess boundary (run_with_popen_progress, standing in for
    the real `cidx index` child process Popen call) is faked; every layer
    above it inside _execute_refresh()/_index_source() runs for real,
    including the actual HNSWIndexManager.rebuild_from_vectors orphan
    detect+repair pass AND the real stderr-scraping helper
    (_forward_hnsw_orphan_events, imported from progress_subprocess_runner,
    reused rather than reimplemented) that turns the genuine stderr output
    into the orphan_event_callback invocation.
    """
    alias_name = f"{_ALIAS_BASE}-refresh-real-global"
    repo_name = alias_name.removesuffix("-global")

    master_path = golden_repos_dir / repo_name
    master_path.mkdir(parents=True)

    size = 1000
    vectors = near_tie_corpus(
        size=size, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=1.0, seed=42
    )
    for i, vec in enumerate(vectors):
        with open(master_path / f"vector_{i}.json", "w") as f:
            json.dump({"id": f"vec_{i}", "vector": vec.tolist()}, f)
    with open(master_path / "collection_meta.json", "w") as f:
        json.dump({"vector_dim": CORPUS_DIM}, f)

    registry.register_global_repo(
        repo_name,
        alias_name,
        "git@github.com:org/repo.git",
        str(master_path),
    )
    _write_alias_pointer(golden_repos_dir, alias_name, str(master_path))

    def fake_run_with_popen_progress(**kwargs):
        """Stand-in for the real `cidx index --fts --progress-json` child
        PLUS the parent's stderr-capture: runs the REAL production HNSW
        rebuild against the real near-tie corpus, captures its genuine
        stderr, and forwards it through the REAL
        _forward_hnsw_orphan_events scraper -- reproducing exactly what
        run_with_popen_progress itself does, without re-implementing the
        scraping logic here.
        """
        from code_indexer.services.progress_subprocess_runner import (
            _forward_hnsw_orphan_events,
        )

        cwd = kwargs["cwd"]
        orphan_event_callback = kwargs.get("orphan_event_callback")

        hnsw_manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        captured_stderr = io.StringIO()
        with contextlib.redirect_stderr(captured_stderr):
            vector_count = hnsw_manager.rebuild_from_vectors(Path(cwd))
        assert vector_count == size

        _forward_hnsw_orphan_events(captured_stderr.getvalue(), orphan_event_callback)
        return 50

    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=fake_run_with_popen_progress,
        ),
        patch.object(
            scheduler, "_create_snapshot", return_value=str(tmp_path / "snap")
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_check_extension_drift", return_value=False),
        patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_gpu,
        caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.repositories.golden_repo_manager",
        ),
    ):
        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = True
        mock_gpu.return_value = mock_updater

        scheduler._execute_refresh(alias_name)

    matching = [
        r
        for r in caplog.records
        if r.name == "code_indexer.server.repositories.golden_repo_manager"
        and alias_name in r.getMessage()
        and HNSW_ORPHAN_REPAIR_MARKER in r.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one REAL orphan-repair marker (from an actual "
        f"HNSWIndexManager rebuild over a near-tie corpus) re-logged "
        f"tagged with alias {alias_name!r} during REFRESH, got records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "repaired=true" in matching[0].getMessage()
