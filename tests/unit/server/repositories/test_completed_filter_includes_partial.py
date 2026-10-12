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


def _fail() -> Dict[str, object]:
    raise RuntimeError("example failure")


def _run_failing_to_end(bgm: BackgroundJobManager, alias: str) -> str:
    job_id = bgm.submit_job(
        "refresh_golden_repo",
        _fail,
        submitter_username="admin",
        is_admin=True,
        repo_alias=alias,
    )
    deadline = time.monotonic() + 30
    while job_id in bgm.jobs:
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.02)
    return job_id


def seed_full_partial_failed(bgm: BackgroundJobManager) -> Tuple[str, str, str]:
    """One finished job per stored status, each on its own repo_alias
    (idx_active_job_per_repo), all gone from memory so only the store
    answers the listing."""
    full = _run_to_end(bgm, {"success": True}, "example-full-global")
    partial = _run_to_end(
        bgm,
        {"success": True, "partial": True, "failed_files": 1},
        "example-part-global",
    )
    failed = _run_failing_to_end(bgm, "example-fail-global")
    return full, partial, failed


def run_list_jobs_scenario(bgm: BackgroundJobManager) -> None:
    """The REST listing's path: the backend ``list_jobs`` and the manager's
    ``list_jobs`` (whose ``total`` counts the listed rows)."""
    full, partial, failed = seed_full_partial_failed(bgm)
    backend = bgm._sqlite_backend
    assert backend is not None

    def backend_rows(status: str) -> Dict[str, str]:
        return {j["job_id"]: j["status"] for j in backend.list_jobs(status=status)}

    assert backend_rows("completed") == {
        full: "completed",
        partial: "completed_partial",
    }
    assert backend_rows("completed_partial") == {partial: "completed_partial"}
    assert backend_rows("failed") == {failed: "failed"}

    def listed(status_filter: str) -> Tuple[set, int]:
        page = bgm.list_jobs(
            username="admin", status_filter=status_filter, is_admin=True
        )
        return {j["job_id"] for j in page["jobs"]}, page["total"]

    assert listed("completed") == ({full, partial}, 2)
    assert listed("completed_partial") == ({partial}, 1)
    assert listed("failed") == ({failed}, 1)


def _sqlite_bgm(tmp_path: Path) -> BackgroundJobManager:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return BackgroundJobManager(
        use_sqlite=True, db_path=db_path, job_tracker=JobTracker(db_path)
    )


def test_list_jobs_completed_includes_partial_sqlite(tmp_path: Path) -> None:
    bgm = _sqlite_bgm(tmp_path)
    try:
        run_list_jobs_scenario(bgm)
    finally:
        bgm.shutdown()


def test_rest_api_jobs_completed_lists_partial(tmp_path: Path) -> None:
    """GET /api/jobs?status=completed through the real route lists the
    partial completion, and ``total`` matches the listed rows."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _find_route_handler,
        _make_admin,
        _patch_closure,
    )
    from fastapi.testclient import TestClient

    from code_indexer.server.app import app
    from code_indexer.server.auth.dependencies import get_current_user

    bgm = _sqlite_bgm(tmp_path)
    admin = _make_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        full, partial, failed = seed_full_partial_failed(bgm)
        handler = _find_route_handler("/api/jobs", "GET")
        with _patch_closure(handler, "background_job_manager", bgm):
            client = TestClient(app)

            def get(status: str) -> Dict[str, str]:
                response = client.get("/api/jobs", params={"status": status})
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["total"] == len(body["jobs"])
                return {j["job_id"]: j["status"] for j in body["jobs"]}

            assert get("completed") == {
                full: "completed",
                partial: "completed_partial",
            }
            assert get("completed_partial") == {partial: "completed_partial"}
            assert get("failed") == {failed: "failed"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        bgm.shutdown()


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
