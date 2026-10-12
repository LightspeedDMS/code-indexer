"""The jobs page's "completed" status filter (the dashboard's completed card
links to it, and that card counts partial completions) selects
``completed_partial`` jobs too; "completed_partial" selects only those.

The PostgreSQL mirror is
tests/unit/server/storage/postgres/test_completed_filter_includes_partial_postgres.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Tuple

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema

_TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)


def _run_to_end(
    bgm: BackgroundJobManager, result: Dict[str, object], alias: str
) -> str:
    job_id = bgm.submit_job(
        "refresh_golden_repo",
        lambda: dict(result),
        submitter_username="admin",
        is_admin=True,
        repo_alias=alias,
    )
    deadline = time.monotonic() + 30
    while job_id in bgm.jobs:
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.02)
    return job_id


def run_completed_filter_scenario(bgm: BackgroundJobManager) -> None:
    full = _run_to_end(bgm, {"success": True}, "example-full-global")
    partial = _run_to_end(
        bgm,
        {"success": True, "partial": True, "failed_files": 1},
        "example-part-global",
    )

    def shown(status_filter: str) -> Tuple[set, set]:
        jobs, _total, _pages = bgm.get_jobs_for_display(
            status_filter=status_filter, is_admin=True
        )
        return {j["job_id"] for j in jobs}, bgm.list_display_job_ids(
            status_filter=status_filter
        )

    assert shown("completed") == ({full, partial}, {full, partial})
    assert shown("completed_partial") == ({partial}, {partial})


def test_completed_filter_includes_partial_sqlite(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    bgm = BackgroundJobManager(
        use_sqlite=True, db_path=db_path, job_tracker=JobTracker(db_path)
    )
    try:
        run_completed_filter_scenario(bgm)
    finally:
        bgm.shutdown()


def _run_in_memory(
    bgm: BackgroundJobManager, result: Dict[str, object], alias: str
) -> str:
    job_id = bgm.submit_job(
        "refresh_golden_repo",
        lambda: dict(result),
        submitter_username="admin",
        is_admin=True,
        repo_alias=alias,
    )
    deadline = time.monotonic() + 30
    while bgm.jobs[job_id].status.value not in ("completed", "completed_partial"):
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.02)
    return job_id


def test_list_jobs_in_memory_completed_includes_partial() -> None:
    """The REST listing with no storage backend filters the in-memory jobs
    by the same rule as the backends."""
    bgm = BackgroundJobManager()
    try:
        full = _run_in_memory(bgm, {"success": True}, "example-full-global")
        partial = _run_in_memory(
            bgm,
            {"success": True, "partial": True, "failed_files": 1},
            "example-part-global",
        )

        def listed(status_filter: str) -> set:
            page = bgm.list_jobs(
                username="admin", status_filter=status_filter, is_admin=True
            )
            return {j["job_id"] for j in page["jobs"]}

        assert listed("completed") == {full, partial}
        assert listed("completed_partial") == {partial}
    finally:
        bgm.shutdown()


def test_web_job_filter_completed_includes_partial() -> None:
    """The web route filters JobTracker-only jobs the same way."""
    from code_indexer.server.web.routes import _apply_job_filters

    jobs = [
        {"job_id": "a", "status": "completed"},
        {"job_id": "b", "status": "completed_partial"},
        {"job_id": "c", "status": "failed"},
    ]

    def ids(status_filter: str) -> list:
        return [
            j["job_id"] for j in _apply_job_filters(jobs, status_filter, None, None)
        ]

    assert ids("completed") == ["a", "b"]
    assert ids("completed_partial") == ["b"]


def test_jobs_page_offers_completed_partial_option() -> None:
    page = (_TEMPLATES / "jobs.html").read_text()
    assert (
        '<option value="completed_partial" {% if status_filter == '
        "'completed_partial' %}selected{% endif %}>Completed (partial)</option>"
    ) in page
