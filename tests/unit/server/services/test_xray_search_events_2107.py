"""Story #2107: completed X-Ray searches are recorded in search_event_log.

Each test drives a real front door (MCP handler or REST route) with the REAL
X-Ray engine over a small repository, then reads the row that the real
SearchEventLogWriter wrote to a REAL store (SQLite and PostgreSQL).

Recording rules exercised here:
- one row per completed single-repo search/explore job (a multi-repo call
  submits one job per alias, so one row per alias);
- one row per batch call;
- query_text is the driver pattern(s), never the evaluator source;
- result_count is the number of matches;
- a search that did not complete (handled error) writes no row.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    ALWAYS_MATCH_EVALUATOR,
    FakeBackgroundJobManager,
    FakeJobTracker,
    Recorder,
    make_repo,
    make_user,
    recorder,
)

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_XRAY_MODULES = (
    "code_indexer.server.mcp.handlers.xray._search",
    "code_indexer.server.mcp.handlers.xray._explore",
    "code_indexer.server.mcp.handlers.xray_batch",
    "code_indexer.server.routes.xray_routes",
)


class XRayEnv(SimpleNamespace):
    rec: Recorder
    repo: Path
    bjm: FakeBackgroundJobManager
    tracker: FakeJobTracker
    state: SimpleNamespace


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[XRayEnv]:
    repo = make_repo(tmp_path, files=3)
    executor = ThreadPoolExecutor(max_workers=2)
    bjm = FakeBackgroundJobManager()
    tracker = FakeJobTracker()
    with recorder(request.param, tmp_path) as rec:
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            xray_executor=executor,
            xray_cell_limiter=None,
            payload_cache=None,
            access_filtering_service=SimpleNamespace(is_admin_user=lambda u: True),
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            background_job_manager=bjm,
            job_tracker=tracker,
            activated_repo_manager=None,
            golden_repo_manager=SimpleNamespace(golden_repos_dir=str(tmp_path)),
        )
        patches = [
            patch("code_indexer.server.mcp.handlers._utils.app_module", app_module)
        ] + [
            patch(f"{mod}._resolve_repo_path", return_value=str(repo))
            for mod in _XRAY_MODULES
        ]
        for p in patches:
            p.start()
        try:
            yield XRayEnv(rec=rec, repo=repo, bjm=bjm, tracker=tracker, state=state)
        finally:
            for p in reversed(patches):
                p.stop()
            executor.shutdown(wait=True)


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mcp_body(response: Dict[str, Any]) -> Dict[str, Any]:
    import json

    return dict(json.loads(response["content"][0]["text"]))


class TestMcpXraySearch:
    def test_mcp_xray_search_records_one_xray_row(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        body = _mcp_body(
            _run_async(
                handle_xray_search(
                    {
                        "repository_alias": "example-repo-global",
                        "pattern": r"def example_",
                        "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                        "search_target": "content",
                        "await_seconds": 30,
                    },
                    make_user("alice"),
                )
            )
        )
        assert len(body["matches"]) == 3

        [row] = env.rec.rows(search_type="xray")
        assert row["username"] == "alice"
        assert row["repo_alias"] == "example-repo-global"
        assert row["query_text"] == r"def example_"
        assert row["result_count"] == 3
        assert row["voyage_cache_hit"] is None
        assert row["cohere_cache_hit"] is None

    def test_mcp_multi_repo_xray_search_records_one_row_per_alias(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        async def _search_and_wait() -> Dict[str, Any]:
            response = await handle_xray_search(
                {
                    "repository_alias": ["first-repo-global", "second-repo-global"],
                    "pattern": r"def example_",
                    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                    "search_target": "content",
                },
                make_user("bob"),
            )
            body = _mcp_body(response)
            for _ in range(600):
                if len(env.tracker.completed) == len(body["job_ids"]):
                    break
                await asyncio.sleep(0.05)
            return body

        body = _run_async(_search_and_wait())
        assert len(body["job_ids"]) == 2

        rows = env.rec.rows(search_type="xray")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo-global", 3),
            ("second-repo-global", 3),
        ]
        assert {r["username"] for r in rows} == {"bob"}


class TestMcpXrayExplore:
    def test_mcp_xray_explore_records_one_xray_row(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        body = _mcp_body(
            _run_async(
                handle_xray_explore(
                    {
                        "repository_alias": "example-repo-global",
                        "pattern": r"def example_",
                        "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                        "search_target": "content",
                        "await_seconds": 30,
                    },
                    make_user("carol"),
                )
            )
        )
        assert len(body["matches"]) == 3

        [row] = env.rec.rows(search_type="xray")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "carol",
            "example-repo-global",
            3,
        )
        assert row["query_text"] == r"def example_"

    def test_mcp_multi_repo_xray_explore_records_one_row_per_alias(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        async def _explore_and_wait() -> Dict[str, Any]:
            body = _mcp_body(
                await handle_xray_explore(
                    {
                        "repository_alias": ["first-repo-global", "second-repo-global"],
                        "pattern": r"def example_",
                        "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                        "search_target": "content",
                    },
                    make_user("carol"),
                )
            )
            for _ in range(600):
                if len(env.tracker.completed) == len(body["job_ids"]):
                    break
                await asyncio.sleep(0.05)
            return body

        assert len(_run_async(_explore_and_wait())["job_ids"]) == 2

        rows = env.rec.rows(search_type="xray")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo-global", 3),
            ("second-repo-global", 3),
        ]


def _rest_client(env: XRayEnv, username: str) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes.xray_routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.search_event_log_writer = env.state.search_event_log_writer
    app.state.access_filtering_service = env.state.access_filtering_service
    app.dependency_overrides[get_current_user] = lambda: make_user(username)
    return TestClient(app)


class TestXRayJobThatRaises:
    """Story #2117: an X-Ray job that raises before producing a result is a
    failure row (its exception's class). The engine is the stand-in."""

    def test_rest_xray_job_that_raises_records_a_failure_row(self, env):
        from code_indexer.xray.search_engine import XRaySearchEngine

        def _boom(self: Any, **_: Any) -> Any:
            raise RuntimeError("engine crashed")

        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": "example-repo-global",
                "driver_regex": r"def example_",
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text
        with patch.object(XRaySearchEngine, "run", _boom):
            with pytest.raises(RuntimeError):
                env.bjm.run_pending()

        assert _failure(env.rec.rows(search_type="xray")) == (
            "POST /api/xray/search",
            "failure",
            "RuntimeError",
        )

    def test_batch_job_that_raises_records_a_failure_row(self, env):
        from code_indexer.server.mcp.handlers import xray_batch

        def _boom(**_: Any) -> Any:
            raise RuntimeError("batch crashed")

        xray_batch.handle_xray_search_batch(
            {"repository_alias": "example-repo-global", "scans": _BATCH_SCANS},
            make_user("erin"),
        )
        with patch.object(xray_batch, "_run_xray_batch_job", _boom):
            with pytest.raises(RuntimeError):
                env.bjm.run_pending()

        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search_batch",
            "failure",
            "RuntimeError",
        )

    def test_mcp_xray_job_that_raises_records_a_failure_row(self, env):
        """MCP xray_search runs its job on the X-Ray executor and awaits it
        inside the handler (no BackgroundJobManager); the job's exception
        propagates out of the handler to the MCP dispatcher."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.xray.search_engine import XRaySearchEngine

        def _boom(self: Any, **_: Any) -> Any:
            raise RuntimeError("engine crashed")

        with patch.object(XRaySearchEngine, "run", _boom):
            with pytest.raises(RuntimeError):
                _run_async(handle_xray_search(dict(_SINGLE_SEARCH), make_user()))

        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search",
            "failure",
            "RuntimeError",
        )


class TestRestXraySearch:
    def test_rest_xray_search_records_one_row_when_job_completes(self, env):
        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": "example-repo-global",
                "driver_regex": r"def example_",
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text
        assert env.rec.rows(search_type="xray") == []

        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "dave",
            "example-repo-global",
            3,
        )
        assert row["query_text"] == r"def example_"


_BATCH_SCANS = [
    {
        "driver_regex": r"def example_",
        "evaluator_code": ALWAYS_MATCH_EVALUATOR,
        "search_target": "content",
    },
    {
        "driver_regex": r"return",
        "evaluator_code": ALWAYS_MATCH_EVALUATOR,
        "search_target": "content",
    },
]


class TestXrayBatch:
    def test_mcp_batch_records_one_row_per_call(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        body = _mcp_body(
            handle_xray_search_batch(
                {"repository_alias": "example-repo-global", "scans": _BATCH_SCANS},
                make_user("erin"),
            )
        )
        assert "job_id" in body, body
        assert env.rec.rows(search_type="xray") == []

        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "erin",
            "example-repo-global",
            6,
        )
        assert row["query_text"] == r"def example_ | return"

    def test_rest_batch_records_one_row_per_call(self, env):
        response = _rest_client(env, "frank").post(
            "/api/xray/search/batch",
            json={"repository_alias": "example-repo-global", "scans": _BATCH_SCANS},
        )
        assert response.status_code == 202, response.text

        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "frank",
            "example-repo-global",
            6,
        )


_TWO_REPOS = ["first-repo-global", "second-repo-global"]


class TestXrayBatchPerRepository:
    """Bug #2111: a batch over several repositories writes one row per
    repository, with that repository's own match count and outcome."""

    def test_two_repository_batch_writes_one_row_per_repository(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        handle_xray_search_batch(
            {"repository_alias": list(_TWO_REPOS), "scans": _BATCH_SCANS},
            make_user("erin"),
        )
        env.bjm.run_pending()

        rows = env.rec.rows(search_type="xray")
        assert {r["repo_alias"]: (r["result_count"], r["outcome"]) for r in rows} == {
            "first-repo-global": (6, "success"),
            "second-repo-global": (6, "success"),
        }
        assert len({r["correlation_id"] for r in rows}) == 1

    def test_two_repository_batch_that_raises_writes_a_failure_row_each(self, env):
        from code_indexer.server.mcp.handlers import xray_batch

        def _boom(**_: Any) -> Any:
            raise RuntimeError("batch crashed")

        xray_batch.handle_xray_search_batch(
            {"repository_alias": list(_TWO_REPOS), "scans": _BATCH_SCANS},
            make_user("erin"),
        )
        with patch.object(xray_batch, "_run_xray_batch_job", _boom):
            with pytest.raises(RuntimeError):
                env.bjm.run_pending()

        rows = env.rec.rows(search_type="xray")
        assert sorted(
            (r["repo_alias"], r["outcome"], r["error_class"]) for r in rows
        ) == [
            ("first-repo-global", "failure", "RuntimeError"),
            ("second-repo-global", "failure", "RuntimeError"),
        ]


_SINGLE_SEARCH = {
    "repository_alias": "example-repo-global",
    "pattern": r"def example_",
    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
    "search_target": "content",
    "await_seconds": 30,
}


class TestExportFilter:
    def test_export_filtered_to_xray_contains_the_recorded_search(self, env, tmp_path):
        import uuid

        import openpyxl

        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.server.services.query_analytics_export_service import (
            QueryAnalyticsExportService,
        )

        _run_async(handle_xray_search(dict(_SINGLE_SEARCH), make_user("grace")))
        env.rec.rows()  # flush the writer into the store

        service = QueryAnalyticsExportService(
            backend=env.rec.store.export_backend, golden_repos_dir=str(tmp_path)
        )
        export_id = str(uuid.uuid4())
        service.run_export(
            export_id=export_id,
            filters={"search_type": "xray"},
            initiated_by="admin",
            export_retention_days=7,
            search_event_log_backend=env.rec.store.backend,
        )
        [record] = service.list_exports(export_id=export_id)
        sheet = openpyxl.load_workbook(record["file_path"]).worksheets[0]
        header, *data = [list(r) for r in sheet.iter_rows(values_only=True)]

        assert record["row_count"] == 1
        row = dict(zip(header, data[0]))
        assert (row["user"], row["search_type"], row["result_count"]) == (
            "grace",
            "xray",
            3,
        )


# Python's re accepts a look-ahead, ripgrep's default engine rejects it (exit
# 2): the engine turns that Phase 1 failure into a normal-looking result with
# no matches (phase1_failed). The search never ran, so it records nothing.
_RIPGREP_REJECTED = r"def example_(?=\d)"


def _failure(rows: List[Dict[str, Any]]) -> tuple:
    """(tool, outcome, error_class) of the one row recorded."""
    [row] = rows
    return (row["tool"], row["outcome"], row["error_class"])


class TestFailedSearchesRecordAFailureRow:
    """Story #2117 (replacing Bug #2113's success-only rule): a search that
    ran and failed is a failure row with its error class."""

    def test_mcp_xray_search_whose_driver_failed_records_a_failure_row(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        body = _mcp_body(
            _run_async(
                handle_xray_search(
                    {**_SINGLE_SEARCH, "pattern": _RIPGREP_REJECTED}, make_user()
                )
            )
        )

        assert body.get("phase1_failed") is True, body
        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search",
            "failure",
            "phase1_failed",
        )

    def test_mcp_xray_explore_whose_driver_failed_records_a_failure_row(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        body = _mcp_body(
            _run_async(
                handle_xray_explore(
                    {**_SINGLE_SEARCH, "pattern": _RIPGREP_REJECTED}, make_user()
                )
            )
        )

        assert body.get("phase1_failed") is True, body
        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_explore",
            "failure",
            "phase1_failed",
        )

    def test_rest_xray_search_whose_driver_failed_records_a_failure_row(self, env):
        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": "example-repo-global",
                "driver_regex": _RIPGREP_REJECTED,
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text

        env.bjm.run_pending()

        [job_result] = env.bjm.results.values()
        assert job_result.get("phase1_failed") is True, job_result
        assert _failure(env.rec.rows(search_type="xray")) == (
            "POST /api/xray/search",
            "failure",
            "phase1_failed",
        )

    def test_batch_whose_every_cell_failed_records_a_failure_row(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        scans = [{**scan, "driver_regex": _RIPGREP_REJECTED} for scan in _BATCH_SCANS]
        handle_xray_search_batch(
            {"repository_alias": "example-repo-global", "scans": scans},
            make_user("erin"),
        )

        env.bjm.run_pending()

        [job_result] = env.bjm.results.values()
        assert [e["error"] for e in job_result["errors"]] == ["phase1_failed"] * 2
        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search_batch",
            "failure",
            "no_cell_ran",
        )

    def test_batch_with_one_failed_cell_records_the_cells_that_ran(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        scans = [
            _BATCH_SCANS[0],
            {**_BATCH_SCANS[1], "driver_regex": _RIPGREP_REJECTED},
        ]
        handle_xray_search_batch(
            {"repository_alias": "example-repo-global", "scans": scans},
            make_user("erin"),
        )

        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert row["result_count"] == 3


_TIMEOUT_ERROR = {
    "file_path": "",
    "line_number": 0,
    "error_type": "EvaluatorTimeout",
    "error_message": "Job timed out with no results.",
}


class TestXrayResultOutcome:
    """The engine's timeout shapes (xray/search_engine.py), which a test
    cannot produce quickly end to end, classified by the shared outcome
    classifier (None = success, else the error class)."""

    def test_phase1_timeout_is_a_timeout(self):
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        assert (
            xray_result_outcome(
                {"matches": [], "files_total": 2, "partial": True, "timeout": True}
            )
            == "timeout"
        )

    def test_phase2_timeout_without_matches_is_a_timeout(self):
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        result = {
            "matches": [],
            "evaluation_errors": [_TIMEOUT_ERROR],
            "partial": True,
            "timeout": True,
        }
        assert xray_result_outcome(result) == "timeout"
        assert xray_result_outcome({**result, "matches": [{"line_number": 1}]}) is None

    def test_batch_whose_only_cell_timed_out_ran_no_cell(self):
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        cell_error = {**_TIMEOUT_ERROR, "repository_alias": "a", "scan_index": 0}
        batch = {
            "matches": [],
            "errors": [],
            "evaluation_errors": [cell_error],
            "total_cells": 1,
            "timeout": False,
            "cancelled": False,
        }
        assert xray_result_outcome(batch) == "no_cell_ran"
        assert xray_result_outcome({**batch, "total_cells": 2}) is None

    def test_malformed_result_never_raises_and_records_nothing(self, env):
        import time

        from code_indexer.server.services.search_event_recording import (
            SearchOrigin,
            record_xray_search,
        )

        record_xray_search(
            env.rec.writer,
            origin=SearchOrigin(tool="xray_search", door="mcp"),
            username="alice",
            repo_alias="example-repo-global",
            pattern=r"def example_",
            result={"matches": 5},
            started_monotonic=time.monotonic(),
            correlation_id=None,
        )

        assert env.rec.rows(search_type="xray") == []

    def test_batch_cell_with_a_per_file_timeout_still_completed(self):
        """A per-file EvaluatorTimeout (5 s sandbox) names its file: the cell
        ran over the other files, so the batch is a success."""
        from code_indexer.server.services.search_event_outcomes import (
            xray_result_outcome,
        )

        per_file = {
            **_TIMEOUT_ERROR,
            "file_path": "module_0.py",
            "repository_alias": "a",
            "scan_index": 0,
        }
        batch = {
            "matches": [],
            "errors": [],
            "evaluation_errors": [per_file],
            "total_cells": 1,
            "timeout": False,
            "cancelled": False,
        }
        assert xray_result_outcome(batch) is None


# validate_rust_evaluator accepts it (the whitelist is lexical); rustc rejects
# it, so the engine reports one whole-run CompileError with file_path "" and
# never evaluates a file.
_COMPILE_ERROR_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    let x: u32 = "not a number";\n'
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


class TestEvaluatorThatFailsRecordsAFailureRow:
    """An evaluator that never ran (compile or xray-cli failure) is not a
    0-result search: Story #2117 records it as a failure."""

    def test_mcp_xray_search_whose_evaluator_failed_to_compile_records_a_failure(
        self, env
    ):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        body = _mcp_body(
            _run_async(
                handle_xray_search(
                    {**_SINGLE_SEARCH, "evaluator_code": _COMPILE_ERROR_EVALUATOR},
                    make_user(),
                )
            )
        )

        assert body["matches"] == [], body
        assert [
            (e["file_path"], e["error_type"]) for e in body["evaluation_errors"]
        ] == [("", "CompileError")], body
        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search",
            "failure",
            "compile_error",
        )

    def test_batch_whose_every_cell_failed_to_compile_records_a_failure(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        scans = [
            {**scan, "evaluator_code": _COMPILE_ERROR_EVALUATOR}
            for scan in _BATCH_SCANS
        ]
        handle_xray_search_batch(
            {"repository_alias": "example-repo-global", "scans": scans},
            make_user("erin"),
        )

        env.bjm.run_pending()

        [job_result] = env.bjm.results.values()
        assert {e["error_type"] for e in job_result["evaluation_errors"]} == {
            "CompileError"
        }, job_result
        assert _failure(env.rec.rows(search_type="xray")) == (
            "xray_search_batch",
            "failure",
            "no_cell_ran",
        )


class TestCancelledMcpJobsRecordNothing:
    """Bug #2113: a cancelled job's (partial) result is not a search result."""

    @pytest.mark.parametrize(
        "handler_name", ["handle_xray_search", "handle_xray_explore"]
    )
    def test_job_cancelled_while_running_records_no_row(self, env, handler_name):
        from code_indexer.server.mcp.handlers import xray

        env.tracker.start_error = "cancelled"

        _run_async(getattr(xray, handler_name)(dict(_SINGLE_SEARCH), make_user()))

        assert list(env.tracker.failed.values()) == ["cancelled"]
        assert env.rec.rows(search_type="xray") == []

    def test_job_failed_for_another_reason_still_records_its_row(self, env):
        """Only a cancellation (BackgroundJobManager.cancel_job fails the job
        with "cancelled") suppresses the row; a job the tracker failed for
        any other reason is not a cancellation."""
        from code_indexer.server.mcp.handlers import xray

        env.tracker.start_error = "Orphaned by server restart"

        _run_async(xray.handle_xray_search(dict(_SINGLE_SEARCH), make_user()))

        [row] = env.rec.rows(search_type="xray")
        assert (row["outcome"], row["result_count"]) == ("success", 3)

    def test_status_update_that_raises_before_the_search_records_nothing(self, env):
        from code_indexer.server.mcp.handlers import xray

        def _unwritable(job_id: str, status: str, **_: Any) -> None:
            raise RuntimeError("job store unwritable")

        with patch.object(env.tracker, "update_status", _unwritable):
            with pytest.raises(RuntimeError):
                _run_async(xray.handle_xray_search(dict(_SINGLE_SEARCH), make_user()))

        assert env.rec.rows(search_type="xray") == []


class TestCancelledBackgroundJobsRecordNothing:
    """Bug #2113: BackgroundJobManager jobs check cancellation before recording."""

    def test_rest_xray_search_cancelled_while_running_records_no_row(self, env):
        env.bjm.cancel_on_progress = True
        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": "example-repo-global",
                "driver_regex": r"def example_",
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text

        env.bjm.run_pending()

        assert len(env.bjm.cancelled) == 1
        assert env.rec.rows(search_type="xray") == []

    def test_batch_cancelled_while_running_records_no_row(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        env.bjm.cancel_on_progress = True
        handle_xray_search_batch(
            {"repository_alias": "example-repo-global", "scans": _BATCH_SCANS},
            make_user("erin"),
        )

        env.bjm.run_pending()

        assert len(env.bjm.cancelled) == 1
        assert env.rec.rows(search_type="xray") == []


class TestRecordingNeverChangesTheSearch:
    def test_job_without_worker_slot_records_no_row(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        env.state.xray_cell_limiter = SimpleNamespace(
            acquire=lambda timeout: False, release=lambda: None
        )

        body = _mcp_body(
            _run_async(handle_xray_search(dict(_SINGLE_SEARCH), make_user()))
        )

        assert body["error"] == "xray_cell_queue_timeout"
        assert env.rec.rows(search_type="xray") == []

    def test_recording_failure_never_fails_the_search(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        def _broken_enqueue(record: Any) -> None:
            raise RuntimeError("writer unavailable")

        env.state.search_event_log_writer = SimpleNamespace(enqueue=_broken_enqueue)

        body = _mcp_body(
            _run_async(handle_xray_search(dict(_SINGLE_SEARCH), make_user()))
        )

        # The inline result is the job's own return value: the job finished
        # normally although its recording raised.
        assert len(body["matches"]) == 3
