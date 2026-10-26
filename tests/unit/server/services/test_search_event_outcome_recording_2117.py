"""Story #2117 / Bug #2111: what the shared recording module writes.

Recording rule (owner ruling, replaces Bug #2113's success-only rule): a
search that ran and succeeded is recorded `outcome=success`; a search that
failed, was rejected after the door resolved its repository, or answered
without having run is recorded `outcome=failure` with a short neutral
`error_class`. A cancelled job writes nothing. Every row carries the `tool`
and `door` of the SearchOrigin the door passes.

Bug #2111: a search over several repositories writes one row per repository
(the alias the product shows, that repository's own result count, the
call's latency and correlation id); a repository that failed gets a failure
row.

Real SearchEventLogWriter over the real stores (SQLite and PostgreSQL).
"""

import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.server.services import search_event_recording as ser
from code_indexer.server.services.search_event_context import SearchEventContext
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

MCP_SEARCH = ser.SearchOrigin(tool="search_code", door="mcp")
REST_QUERY = ser.SearchOrigin(tool="POST /api/query", door="rest")
WEB_QUERY = ser.SearchOrigin(tool="web:query", door="web")


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as real:
        yield real


def _row(rec: Recorder) -> Dict[str, Any]:
    [row] = rec.rows()
    return row


def _outcome(row: Dict[str, Any]) -> tuple:
    return (row["tool"], row["door"], row["outcome"], row["error_class"])


def _context(**overrides: Any) -> SearchEventContext:
    values: Dict[str, Any] = dict(
        username="alice",
        repo_alias="example-repo-global",
        search_type="semantic",
        query_text="token refresh",
    )
    values.update(overrides)
    return SearchEventContext(**values)


def _payload_record(rec: Recorder, payload: Dict[str, Any], **kw: Any) -> None:
    ser.record_search_payload(
        rec.writer,
        payload,
        origin=MCP_SEARCH,
        search_type="semantic",
        context=kw.pop("context", _context()),
        total_latency_ms=7,
        correlation_id="corr-1",
    )


class TestOriginAndSuccess:
    def test_success_row_carries_tool_door_and_outcome(self, rec):
        _payload_record(rec, {"success": True, "results": [{"file_path": "a.py"}]})

        row = _row(rec)
        assert _outcome(row) == ("search_code", "mcp", "success", None)
        assert row["result_count"] == 1

    def test_unknown_door_is_refused(self, rec):
        ser.record_completed_search(
            rec.writer,
            origin=ser.SearchOrigin(tool="regex_search", door="ftp"),
            search_type="regex",
            username="alice",
            repo_alias="example-repo-global",
            query_text="def",
            result_count=1,
            started_monotonic=time.monotonic(),
            correlation_id=None,
        )
        assert rec.rows() == []


class TestSearchCodePayloadOutcomes:
    def test_providers_unavailable_is_a_failure(self, rec):
        _payload_record(
            rec,
            {
                "success": True,
                "results": {
                    "results": [],
                    "query_metadata": {"completeness": "providers_unavailable"},
                },
            },
        )
        row = _row(rec)
        assert _outcome(row) == (
            "search_code",
            "mcp",
            "failure",
            "providers_unavailable",
        )
        assert row["result_count"] == 0

    @pytest.mark.parametrize(
        "error_code",
        [
            "REPOSITORY_NOT_FOUND",
            "SERVICE_UNAVAILABLE",
            "TEMPORAL_ALIAS_REQUIRED",
            "TEMPORAL_SINGLE_REPO_REQUIRED",
        ],
    )
    def test_rejection_payload_writes_nothing(self, rec, error_code):
        """A request refused before its repository resolved (unknown
        repository, a required service not wired, a temporal alias the
        door refuses) is not a search: no row."""
        _payload_record(
            rec,
            {
                "success": False,
                "error": "Repository 'x' not found",
                "error_code": error_code,
                "results": [],
            },
        )
        assert rec.rows() == []

    def test_handled_error_after_resolution_is_a_failure(self, rec):
        _payload_record(
            rec, {"success": False, "error": "index unreadable", "results": []}
        )
        row = _row(rec)
        assert _outcome(row) == ("search_code", "mcp", "failure", "search_error")

    def test_omni_search_that_searched_nothing_is_a_failure(self, rec):
        _payload_record(
            rec,
            {
                "success": True,
                "results": {"total_repos_searched": 0, "results": [], "errors": {}},
            },
            context=_context(repo_alias=None),
        )
        row = _row(rec)
        assert _outcome(row)[2:] == ("failure", "no_repository_searched")

    def test_deferred_handoff_writes_nothing_now(self, rec):
        _payload_record(
            rec,
            {"success": False, "error_code": "TEMPORAL_QUERY_DEFERRED", "job_id": "j"},
        )
        assert rec.rows() == []


class TestPerRepositoryRows:
    def test_noted_repositories_get_one_row_each_with_their_own_count(self, rec):
        context = _context(repo_alias=None, search_type="fts")
        context.note_repositories(
            searched=["first-repo-global", "second-repo-global", "empty-repo-global"],
            failed=["broken-repo-global"],
        )
        _payload_record(
            rec,
            {
                "success": True,
                "results": {
                    "total_repos_searched": 3,
                    "results_by_repo": {
                        "first-repo-global": {"count": 2, "results": [{}, {}]},
                        "second-repo-global": {"count": 1, "results": [{}]},
                    },
                    "total_results": 3,
                    "errors": {"broken-repo-global": "failed"},
                },
            },
            context=context,
        )

        rows = rec.rows()
        by_repo = {
            r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
            for r in rows
        }
        assert by_repo == {
            "first-repo-global": (2, "success", None),
            "second-repo-global": (1, "success", None),
            "empty-repo-global": (0, "success", None),
            "broken-repo-global": (0, "failure", "repository_failed"),
        }
        assert {r["correlation_id"] for r in rows} == {"corr-1"}
        assert {r["total_latency_ms"] for r in rows} == {7}

    def test_flat_results_are_counted_by_source_repository(self, rec):
        context = _context(repo_alias=None)
        context.note_repositories(searched=["a-global", "b-global"], failed=[])
        _payload_record(
            rec,
            {
                "success": True,
                "results": {
                    "total_repos_searched": 2,
                    "results": [
                        {"source_repo": "a-global"},
                        {"source_repo": "a-global"},
                        {"source_repo": "b-global"},
                    ],
                },
            },
            context=context,
        )
        counts = {r["repo_alias"]: r["result_count"] for r in rec.rows()}
        assert counts == {"a-global": 2, "b-global": 1}

    def test_query_results_dict_is_counted_by_repository_alias(self, rec):
        def _search() -> Dict[str, Any]:
            from code_indexer.server.services.search_event_context import (
                get_search_event_ctx,
            )

            ctx = get_search_event_ctx()
            assert ctx is not None
            ctx.note_repositories(searched=["one", "two"], failed=["three"])
            return {
                "results": [
                    {"repository_alias": "one"},
                    {"repository_alias": "two"},
                    {"repository_alias": "two"},
                ]
            }

        ser.run_recorded_search(
            rec.writer,
            _search,
            origin=WEB_QUERY,
            username="alice",
            repo_alias=None,
            search_type="semantic",
            query_text="q",
            correlation_id="c",
        )
        by_repo = {
            r["repo_alias"]: (r["result_count"], r["outcome"]) for r in rec.rows()
        }
        assert by_repo == {
            "one": (1, "success"),
            "two": (2, "success"),
            "three": (0, "failure"),
        }


class TestRaisingSearches:
    def test_run_recorded_search_that_raises_records_a_failure(self, rec):
        class SearchFailedError(Exception):
            pass

        def _boom() -> Dict[str, Any]:
            raise SearchFailedError("disk path /srv/example leaked in message")

        with pytest.raises(SearchFailedError):
            ser.run_recorded_search(
                rec.writer,
                _boom,
                origin=WEB_QUERY,
                username="alice",
                repo_alias="example-repo",
                search_type="semantic",
                query_text="q",
                correlation_id=None,
            )
        row = _row(rec)
        assert _outcome(row) == ("web:query", "web", "failure", "SearchFailedError")
        assert row["repo_alias"] == "example-repo"

    def test_pending_search_failure_and_cancel(self, rec):
        pending = ser.PendingSearchEvent(
            writer=rec.writer,
            context=_context(),
            correlation_id=None,
            origin=REST_QUERY,
        )
        pending.record_failure(TimeoutError("late"), cancelled=lambda: True)
        assert rec.rows() == []

        pending.record_failure(TimeoutError("late"))
        assert _outcome(_row(rec)) == (
            "POST /api/query",
            "rest",
            "failure",
            "TimeoutError",
        )

    def _recording(self, rec: Recorder) -> Any:
        return ser.recording_failure(
            rec.writer,
            origin=ser.SearchOrigin(tool="regex_search", door="mcp"),
            search_type="regex",
            username="alice",
            repo_alias="example-repo-global",
            query_text="def",
            started_monotonic=time.monotonic(),
            correlation_id="corr-2",
        )

    def test_recording_failure_records_the_raised_class(self, rec):
        with pytest.raises(RuntimeError):
            with self._recording(rec):
                raise RuntimeError("index unreadable at /srv/example")
        row = _row(rec)
        assert _outcome(row) == ("regex_search", "mcp", "failure", "RuntimeError")
        assert (row["repo_alias"], row["result_count"]) == ("example-repo-global", 0)

    def test_recording_failure_records_nothing_for_a_rejection(self, rec):
        from code_indexer.server.query.semantic_query_manager import (
            SearchRepositoryNotFoundError,
        )

        with pytest.raises(SearchRepositoryNotFoundError):
            with self._recording(rec):
                raise SearchRepositoryNotFoundError("missing-repo")
        assert rec.rows() == []

    def test_recording_failure_records_nothing_when_the_block_completes(self, rec):
        with self._recording(rec):
            pass
        assert rec.rows() == []


def _raised_from(outer: BaseException, cause: Optional[BaseException]) -> BaseException:
    """`outer` as raised by `raise outer from cause` (a door translating)."""
    try:
        try:
            if cause is not None:
                raise cause
            raise outer
        except BaseException as inner:
            if inner is outer:
                raise
            raise outer from inner
    except BaseException as raised:
        return raised


def _rejections() -> List[Any]:
    from fastapi import HTTPException

    from code_indexer.server.query.semantic_query_manager import (
        SearchRepositoryNotFoundError,
        SearchRequestError,
    )
    from code_indexer.services.temporal.temporal_worker_input_adapters import (
        TemporalAliasRejectedError,
    )

    return [
        SearchRepositoryNotFoundError("missing-repo"),
        SearchRequestError("limit must be > 0"),
        TemporalAliasRejectedError("TEMPORAL_ALIAS_REQUIRED", "alias required"),
        _raised_from(
            HTTPException(status_code=404), SearchRepositoryNotFoundError("missing")
        ),
        _raised_from(HTTPException(status_code=400), None),
    ]


class TestHttpResponseOutcome:
    """A REST answer is a rejection only when its body says so (a rejection
    error code); a status code alone never hides a search that ran and
    failed (e.g. a 503 or 404 raised after the search started)."""

    @pytest.mark.parametrize(
        "status_code, body, expected",
        [
            (503, {"error_code": "SERVICE_UNAVAILABLE"}, None),
            (404, {"error_code": "REPOSITORY_NOT_FOUND"}, None),
            (400, {"error_code": "TEMPORAL_ALIAS_REQUIRED"}, None),
            (503, {"success": False}, "search_error"),
            (404, {"success": False}, "search_error"),
            (
                502,
                {"error_code": "TEMPORAL_DEDUP_CAPACITY_EXHAUSTED"},
                "temporal_dedup_capacity_exhausted",
            ),
        ],
    )
    def test_only_a_rejection_code_is_a_rejection(self, status_code, body, expected):
        from code_indexer.server.services.search_event_outcomes import (
            http_response_failure_class,
        )

        assert http_response_failure_class(status_code, body) == expected


class TestRejectionsWriteNothing:
    """Story #2117 (owner ruling): a request rejected before its repository
    resolved is not a search; whether an exception is such a rejection is
    decided in one shared place."""

    @pytest.mark.parametrize("index", range(5))
    def test_rejection_writes_nothing(self, rec, index):
        pending = ser.PendingSearchEvent(
            writer=rec.writer,
            context=_context(),
            correlation_id=None,
            origin=REST_QUERY,
        )
        pending.record_failure(_rejections()[index])
        assert rec.rows() == []

    def test_failure_translated_by_the_door_keeps_its_class(self, rec):
        from fastapi import HTTPException

        pending = ser.PendingSearchEvent(
            writer=rec.writer,
            context=_context(),
            correlation_id=None,
            origin=REST_QUERY,
        )
        pending.record_failure(
            _raised_from(HTTPException(status_code=500), RuntimeError("disk"))
        )
        assert _outcome(_row(rec))[2:] == ("failure", "RuntimeError")

    def test_run_recorded_search_rejection_writes_nothing(self, rec):
        from code_indexer.server.query.semantic_query_manager import (
            SearchRepositoryNotFoundError,
        )

        def _rejected() -> Dict[str, Any]:
            raise SearchRepositoryNotFoundError("missing-repo")

        with pytest.raises(SearchRepositoryNotFoundError):
            ser.run_recorded_search(
                rec.writer,
                _rejected,
                origin=WEB_QUERY,
                username="alice",
                repo_alias="missing-repo",
                search_type="semantic",
                query_text="q",
                correlation_id=None,
            )
        assert rec.rows() == []


_LONG_NAME = "Example" + "VeryLong" * 9  # 79 characters


class TestErrorClassToken:
    """An exception class name that is not a valid token as written (over 64
    characters, a leading underscore) is reduced to one; the row is never
    dropped."""

    def test_bare_exception_wrapper_takes_its_cause_class(self):
        """A bare `Exception` wrapper (MCP search_code wraps a timeout as
        `Exception("Query timed out: ...") from e`) names no class of its own:
        the row carries its cause's class, so every door's timeout reads the
        same."""
        from code_indexer.server.services.search_event_outcomes import (
            failure_error_class,
        )

        wrapped = _raised_from(Exception("Query timed out: x"), TimeoutError("x"))
        assert failure_error_class(wrapped) == "TimeoutError"
        assert failure_error_class(RuntimeError("x")) == "RuntimeError"

    @pytest.mark.parametrize(
        "class_name, expected",
        [(_LONG_NAME, _LONG_NAME[:64]), ("_HiddenFailure", "HiddenFailure")],
    )
    def test_failure_row_keeps_a_valid_token(self, rec, class_name, expected):
        error_type = type(class_name, (Exception,), {})

        def _boom() -> Dict[str, Any]:
            raise error_type("failed")

        with pytest.raises(error_type):
            ser.run_recorded_search(
                rec.writer,
                _boom,
                origin=WEB_QUERY,
                username="alice",
                repo_alias="example-repo",
                search_type="semantic",
                query_text="q",
                correlation_id=None,
            )
        assert _outcome(_row(rec))[2:] == ("failure", expected)


_MULTI = ser.SearchOrigin(tool="POST /api/query/multi", door="rest")


def _by_repo(rec: Recorder) -> Dict[Any, tuple]:
    return {
        r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
        for r in rec.rows()
    }


class TestGroupedFailures:
    def test_failed_repositories_get_failure_rows(self, rec):
        context = _context(repo_alias=None)
        context.note_repositories(searched=["ok-repo"], failed=["bad-repo"])
        ser.record_multi_repository_search(
            rec.writer,
            {"ok-repo": [1, 2]},
            origin=_MULTI,
            context=context,
            started_monotonic=time.monotonic(),
            correlation_id="c",
        )
        assert _by_repo(rec) == {
            "ok-repo": (2, "success", None),
            "bad-repo": (0, "failure", "repository_failed"),
        }

    def test_whole_call_failure_after_resolution_is_a_failure_row_each(self, rec):
        context = _context(repo_alias=None)
        context.note_repositories(searched=["ok-repo"], failed=["bad-repo"])
        with pytest.raises(RuntimeError):
            with ser.recording_multi_repository_failure(
                rec.writer,
                origin=_MULTI,
                context=context,
                started_monotonic=time.monotonic(),
                correlation_id="c",
            ):
                raise RuntimeError("aggregation failed")
        assert _by_repo(rec) == {
            "ok-repo": (0, "failure", "RuntimeError"),
            "bad-repo": (0, "failure", "RuntimeError"),
        }

    def test_failure_before_resolution_records_nothing(self, rec):
        with pytest.raises(RuntimeError):
            with ser.recording_multi_repository_failure(
                rec.writer,
                origin=_MULTI,
                context=_context(repo_alias=None),
                started_monotonic=time.monotonic(),
                correlation_id="c",
            ):
                raise RuntimeError("failed before the fan-out")
        assert rec.rows() == []


def _xray(rec: Recorder, result: Dict[str, Any], cancelled: bool = False) -> None:
    ser.record_xray_search(
        rec.writer,
        origin=ser.SearchOrigin(tool="xray_search", door="mcp"),
        username="alice",
        repo_alias="example-repo-global",
        pattern="def",
        result=result,
        # ^ hazard 0.0 => latency = host uptime ms > INT32 max after 24.8 days; PG insert fails
        started_monotonic=time.monotonic(),
        correlation_id=None,
        cancelled=(lambda: cancelled),
    )


class TestXRayOutcomes:
    @pytest.mark.parametrize(
        "result, error_class",
        [
            ({"phase1_failed": True, "matches": []}, "phase1_failed"),
            ({"timeout": True, "matches": []}, "timeout"),
            (
                {
                    "matches": [],
                    "evaluation_errors": [
                        {"file_path": "", "error_type": "CompileError"}
                    ],
                },
                "compile_error",
            ),
            (
                {
                    "matches": [],
                    "evaluation_errors": [
                        {"file_path": "", "error_type": "XRayCliError"}
                    ],
                },
                "xray_cli_error",
            ),
            (
                {
                    "total_cells": 1,
                    "matches": [],
                    "errors": [{"error_level": "cell", "repository_alias": "r"}],
                },
                "no_cell_ran",
            ),
        ],
    )
    def test_search_that_did_not_run_is_a_failure(self, rec, result, error_class):
        _xray(rec, result)
        row = _row(rec)
        assert (row["outcome"], row["error_class"]) == ("failure", error_class)

    def test_matches_are_a_success(self, rec):
        _xray(rec, {"matches": [{"file_path": "a.py"}], "timeout": True})
        row = _row(rec)
        assert (row["outcome"], row["result_count"]) == ("success", 1)

    @pytest.mark.parametrize(
        "result, cancelled",
        [({"cancelled": True, "matches": []}, False), ({"matches": []}, True)],
    )
    def test_cancelled_search_writes_nothing(self, rec, result, cancelled):
        _xray(rec, result, cancelled=cancelled)
        assert rec.rows() == []


class TestScipOutcomes:
    def test_scip_error_payload_is_a_failure(self, rec):
        ser.record_scip_search(
            rec.writer,
            {"success": False, "error": "No SCIP index found"},
            origin=ser.SearchOrigin(tool="scip_definition", door="mcp"),
            username="alice",
            repo_alias="example-repo-global",
            query_text="ExampleService",
            started_monotonic=time.monotonic(),
            correlation_id=None,
        )
        assert _outcome(_row(rec)) == (
            "scip_definition",
            "mcp",
            "failure",
            "search_error",
        )


class TestDeferredSearchKeepsItsDoor:
    def test_deferred_temporal_row_carries_the_submitting_door(self, rec, tmp_path):
        from tests.unit.server.services.search_event_recording_harness import (
            payload_cache_of,
        )

        with payload_cache_of("sqlite", tmp_path) as cache:
            ser.remember_deferred_search(
                cache,
                {"error_code": "TEMPORAL_QUERY_DEFERRED", "job_id": "job-1"},
                origin=REST_QUERY,
                context=_context(search_type="temporal"),
                correlation_id="c",
                started_at=__import__("time").time(),
            )
            ser.record_deferred_search(
                cache, rec.writer, job_id="job-1", username="alice", result_count=4
            )
        row = _row(rec)
        assert _outcome(row) == ("POST /api/query", "rest", "success", None)
        assert row["result_count"] == 4

    def test_pending_record_from_an_older_node_is_still_recorded(self, rec, tmp_path):
        """A node predating Story #2117 stores the pending record without
        tool/door (rolling restart). It is recorded, with tool/door NULL."""
        import json
        import time

        from tests.unit.server.services.search_event_recording_harness import (
            payload_cache_of,
        )

        legacy_pending = {
            "username": "alice",
            "repo_alias": "example-repo-global",
            "query_text": "token refresh",
            "voyage_cache_hit": None,
            "voyage_cache_mode": None,
            "voyage_latency_ms": None,
            "cohere_cache_hit": None,
            "cohere_cache_mode": None,
            "cohere_latency_ms": None,
            "started_at": time.time(),
            "correlation_id": "c-old",
        }
        with payload_cache_of("sqlite", tmp_path) as cache:
            cache.store_expiring_key(
                ser._deferred_key("job-old", "alice"),
                json.dumps(legacy_pending),
                ser.DEFERRED_SEARCH_TTL_SECONDS,
            )
            ser.record_deferred_search(
                cache, rec.writer, job_id="job-old", username="alice", result_count=2
            )
        row = _row(rec)
        assert _outcome(row) == (None, None, "success", None)
        assert (row["search_type"], row["result_count"]) == ("temporal", 2)
