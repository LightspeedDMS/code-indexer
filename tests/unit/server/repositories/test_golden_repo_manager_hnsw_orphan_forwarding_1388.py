"""
Bug #1388 (remediation after review rejection): forward the HNSW
finalize-time orphan detect+repair marker (HNSW_ORPHAN_REPAIR_MARKER,
storage/hnsw_index_manager.py) from a `cidx index` child subprocess into
the SERVER's own admin-visible logs.db.

REJECTED first attempt: `_wrap_progress_callback_with_hnsw_orphan_logging`
wrapped `progress_callback` and detected the marker via a `detail=` kwarg
that only arrived if the marker survived the --progress-json wire protocol
end to end -- which it never did (see
tests/unit/storage/test_hnsw_index_manager_1388_orphan_marker.py and
tests/unit/services/test_progress_subprocess_runner_hnsw_orphan_forwarding_1388.py
for the two independent real-boundary gates that dropped it).

This remediation replaces that wrapper with `_make_hnsw_orphan_event_logger`,
a factory that builds a `callable(line: str) -> None` suitable for the NEW
`orphan_event_callback` parameter on `run_with_popen_progress`
(progress_subprocess_runner.py) -- a channel entirely separate from
`progress_callback`, fed by the parent scraping the child's real stderr.
`add_golden_repo`'s `background_worker` passes both `progress_callback`
(unwrapped, passed straight through) AND this new `orphan_event_callback`
into `_execute_post_clone_workflow`, which threads it down to the shared
`run_with_popen_progress` call.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    _make_hnsw_orphan_event_logger,
)
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.hnsw_index_manager import HNSW_ORPHAN_REPAIR_MARKER

_MARKER_LINE = (
    f"{HNSW_ORPHAN_REPAIR_MARKER}: context=rebuild_from_vectors:/x/y "
    f"orphan_count=3 repaired=true"
)
_ALIAS = "typer-global-repro"


def test_marker_line_is_logged_tagged_with_alias(caplog):
    """Direct, non-mocked unit test of the factory itself -- no subprocess,
    no GoldenRepoManager instantiation required."""
    log_event = _make_hnsw_orphan_event_logger(_ALIAS)

    with caplog.at_level(
        logging.INFO,
        logger="code_indexer.server.repositories.golden_repo_manager",
    ):
        log_event(_MARKER_LINE)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    matching = [r for r in info_records if _ALIAS in r.getMessage()]
    assert len(matching) == 1, (
        f"expected exactly one INFO log record tagging alias {_ALIAS!r}, "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert _MARKER_LINE in matching[0].getMessage()


def test_add_golden_repo_wires_alias_bound_orphan_event_callback_to_subprocess_boundary(
    tmp_path, caplog
):
    """Wiring test: add_golden_repo's REAL background_worker, driving the
    REAL _execute_post_clone_workflow (not mocked -- that is the SUT), must
    thread an alias-bound orphan_event_callback all the way down to the
    shared run_with_popen_progress subprocess boundary. Only external
    collaborators are mocked: git clone/validate/branch-resolve, the
    module-level subprocess runner, and config lookup -- matching the
    precedent mocking boundary already used by
    test_golden_repo_manager_subprocess_env_sanitization_1325.py.
    """
    data_dir = tmp_path / "data"
    (data_dir / "golden-repos").mkdir(parents=True)
    manager = GoldenRepoManager(data_dir=str(data_dir))

    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(manager.db_path).initialize_database()

    def mock_submit_job(operation_type, func, submitter_username, **kwargs):
        func(progress_callback=lambda *a, **kw: None)
        return "test-job-id"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = mock_submit_job

    clone_path = tmp_path / "clone"
    clone_path.mkdir()

    popen_calls: list = []

    def _capture_popen(*, command, phase_name, orphan_event_callback=None, **kwargs):
        popen_calls.append(orphan_event_callback)
        return 100

    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    with (
        patch.object(manager, "_validate_git_repository", return_value=True),
        patch.object(manager, "_clone_repository", return_value=str(clone_path)),
        patch.object(manager, "_resolve_cloned_branch", return_value="main"),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_capture_popen,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
        caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.repositories.golden_repo_manager",
        ),
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        manager.add_golden_repo(
            repo_url="https://github.com/user/repo.git",
            alias=_ALIAS,
            default_branch="main",
            submitter_username="test-user",
            skip_pre_flight_git_validation=True,
        )

        assert len(popen_calls) >= 1, (
            "expected at least one run_with_popen_progress call"
        )
        orphan_event_callback = popen_calls[0]
        assert orphan_event_callback is not None, (
            "add_golden_repo must thread a real orphan_event_callback down to "
            "run_with_popen_progress"
        )

        # Simulate the marker arriving exactly as run_with_popen_progress's
        # real stderr scraper forwards it -- invoked INSIDE the at_level
        # block so caplog's effective level is still raised to INFO.
        orphan_event_callback(_MARKER_LINE)

    matching = [
        r
        for r in caplog.records
        if _ALIAS in r.getMessage() and HNSW_ORPHAN_REPAIR_MARKER in r.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one orphan-repair marker re-logged tagged with "
        f"alias {_ALIAS!r}, got records: {[r.getMessage() for r in caplog.records]}"
    )
