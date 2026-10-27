"""Story #2203: every X-Ray door records which evaluator code ran.

Each test drives a real front door (MCP handler or REST route) with the REAL
X-Ray engine over a small repository. The search event row lands in a REAL
store through the REAL SearchEventLogWriter (SQLite and PostgreSQL); the
audit row lands in a REAL audit store bound as the process audit sink.
"""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from tests.unit.server.audit._audit_accounts_support import (
    AuditRow,
    AuditStore,
    bound_audit_store,
)
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
_AUDIT_ACTION = "xray_evaluator_executed"
_REPO = "example-repo-global"


def _sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class DoorEnv(SimpleNamespace):
    rec: Recorder
    audit: AuditStore
    bjm: FakeBackgroundJobManager
    tracker: FakeJobTracker
    state: SimpleNamespace
    cidx_meta: Path

    def audit_rows(self) -> List[AuditRow]:
        self.audit.service.flush()
        return self.audit.rows(_AUDIT_ACTION)


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[DoorEnv]:
    repo = make_repo(tmp_path, files=3)
    cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    (tmp_path / "audit").mkdir()
    executor = ThreadPoolExecutor(max_workers=2)
    bjm = FakeBackgroundJobManager()
    tracker = FakeJobTracker()
    audit_iter = bound_audit_store(tmp_path / "audit" / "groups.db")
    audit = next(audit_iter)
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
        # ^ why List[Any], not inferred; patch() of mixed `new` values infers object (no start/stop)
        patches: List[Any] = [
            patch("code_indexer.server.mcp.handlers._utils.app_module", app_module),
            patch(
                "code_indexer.server.mcp.handlers.xray._infra._get_cidx_meta_path",
                return_value=cidx_meta,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=cidx_meta,
            ),
            # ^ why seeds marked ensured; seeding commits to git, not under test here
            patch("code_indexer.server.mcp.handlers.xray._infra._seeds_ensured", True),
        ] + [
            patch(f"{mod}._resolve_repo_path", return_value=str(repo))
            for mod in _XRAY_MODULES
        ]
        for p in patches:
            p.start()
        try:
            yield DoorEnv(
                rec=rec,
                audit=audit,
                bjm=bjm,
                tracker=tracker,
                state=state,
                cidx_meta=cidx_meta,
            )
        finally:
            for p in reversed(patches):
                p.stop()
            executor.shutdown(wait=True)
            for _ in audit_iter:
                pass


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mcp_body(response: Dict[str, Any]) -> Dict[str, Any]:
    return dict(json.loads(response["content"][0]["text"]))


class TestMcpXraySearch:
    def test_mcp_xray_search_records_hash_and_one_audit_event(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        body = _mcp_body(
            _run_async(
                handle_xray_search(
                    {
                        "repository_alias": _REPO,
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
        assert row["evaluator_sha256"] == _sha(ALWAYS_MATCH_EVALUATOR)
        assert row["xray_pattern_name"] is None
        [audit] = env.audit_rows()
        assert (audit.actor, audit.target_type, audit.target_id, audit.outcome) == (
            "alice",
            "repo",
            _REPO,
            "success",
        )
        assert audit.details == {
            "evaluator_sha256s": [_sha(ALWAYS_MATCH_EVALUATOR)],
            "pattern_names": [],
        }
        assert ALWAYS_MATCH_EVALUATOR not in env.audit.all_raw_text()

    def test_mcp_xray_search_with_stored_pattern_records_its_name(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        _store_pattern(env.cidx_meta)
        body = _mcp_body(
            _run_async(
                handle_xray_search(
                    {
                        "repository_alias": _REPO,
                        "pattern": r"def example_",
                        "pattern_name": _PATTERN,
                        "search_target": "content",
                        "await_seconds": 30,
                    },
                    make_user("alice"),
                )
            )
        )
        assert len(body["matches"]) == 3, body

        [row] = env.rec.rows(search_type="xray")
        assert row["xray_pattern_name"] == _PATTERN
        assert row["evaluator_sha256"] == _sha(ALWAYS_MATCH_EVALUATOR)
        [audit] = env.audit_rows()
        assert audit.details["pattern_names"] == [_PATTERN]


_PATTERN = "example-pattern"
_PATTERN_YAML = (
    f"name: {_PATTERN}\n"
    'description: "matches every candidate file"\n'
    "language: python\n"
    "evaluator_code: |\n"
    + "".join(f"  {line}\n" for line in ALWAYS_MATCH_EVALUATOR.splitlines())
)


def _store_pattern(cidx_meta: Path) -> None:
    scope = cidx_meta / "xray-patterns" / "__any__"
    scope.mkdir(parents=True)
    (scope / f"{_PATTERN}.yaml").write_text(_PATTERN_YAML)


class TestMcpXrayExplore:
    def test_mcp_xray_explore_records_hash_and_one_audit_event(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        body = _mcp_body(
            _run_async(
                handle_xray_explore(
                    {
                        "repository_alias": _REPO,
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
        assert row["evaluator_sha256"] == _sha(ALWAYS_MATCH_EVALUATOR)
        [audit] = env.audit_rows()
        assert (audit.actor, audit.target_id, audit.outcome) == (
            "carol",
            _REPO,
            "success",
        )


def _rest_client(env: DoorEnv, username: str) -> Any:
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


class TestRestXraySearch:
    def test_rest_xray_search_with_stored_pattern_records_hash_and_name(self, env):
        _store_pattern(env.cidx_meta)
        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": _REPO,
                "driver_regex": r"def example_",
                "pattern_name": _PATTERN,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text
        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["evaluator_sha256"], row["xray_pattern_name"]) == (
            _sha(ALWAYS_MATCH_EVALUATOR),
            _PATTERN,
        )
        [audit] = env.audit_rows()
        assert (audit.actor, audit.target_id) == ("dave", _REPO)
        assert audit.details["pattern_names"] == [_PATTERN]


# ^ invariant batch strips inline code before it runs; stripped == submitted here, != the pattern's source
_INLINE_EVALUATOR = ALWAYS_MATCH_EVALUATOR.rstrip("\n")
_TWO_REPOS = ["first-repo-global", "second-repo-global"]
_BATCH_SCANS = [
    {
        "driver_regex": r"def example_",
        "evaluator_code": _INLINE_EVALUATOR,
        "search_target": "content",
    },
    {"driver_regex": r"return", "pattern_name": _PATTERN, "search_target": "content"},
]


class TestXrayBatch:
    def test_mcp_batch_row_per_repository_lists_every_scans_hash(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        _store_pattern(env.cidx_meta)
        body = _mcp_body(
            handle_xray_search_batch(
                {"repository_alias": list(_TWO_REPOS), "scans": _BATCH_SCANS},
                make_user("erin"),
            )
        )
        assert "job_id" in body, body
        env.bjm.run_pending()

        both = f"{_sha(_INLINE_EVALUATOR)},{_sha(ALWAYS_MATCH_EVALUATOR)}"
        rows = env.rec.rows(search_type="xray")
        assert {
            r["repo_alias"]: (r["evaluator_sha256"], r["xray_pattern_name"])
            for r in rows
        } == {alias: (both, _PATTERN) for alias in _TWO_REPOS}
        audits = env.audit_rows()
        assert sorted(a.target_id for a in audits) == _TWO_REPOS
        assert {tuple(a.details["evaluator_sha256s"]) for a in audits} == {
            tuple(both.split(","))
        }
        stored = env.rec.store.backend.evaluator_sources(both.split(","))
        assert {sha: source for sha, (source, _cut) in stored.items()} == {
            _sha(_INLINE_EVALUATOR): _INLINE_EVALUATOR,
            _sha(ALWAYS_MATCH_EVALUATOR): ALWAYS_MATCH_EVALUATOR,
        }

    def test_rest_batch_records_hash_and_one_audit_event(self, env):
        response = _rest_client(env, "frank").post(
            "/api/xray/search/batch",
            json={"repository_alias": _REPO, "scans": _BATCH_SCANS[:1]},
        )
        assert response.status_code == 202, response.text
        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["evaluator_sha256"], row["xray_pattern_name"]) == (
            _sha(_INLINE_EVALUATOR),
            None,
        )
        [audit] = env.audit_rows()
        assert (audit.actor, audit.target_id, audit.outcome) == (
            "frank",
            _REPO,
            "success",
        )


_SEARCH = {
    "repository_alias": _REPO,
    "pattern": r"def example_",
    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
    "search_target": "content",
    "await_seconds": 30,
}


class TestOutcomes:
    def test_job_that_raises_audits_a_failure_with_the_hash(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.xray.search_engine import XRaySearchEngine

        def _boom(self: Any, **_: Any) -> Any:
            raise RuntimeError("engine crashed")

        with patch.object(XRaySearchEngine, "run", _boom):
            with pytest.raises(RuntimeError):
                _run_async(handle_xray_search(dict(_SEARCH), make_user("grace")))

        [row] = env.rec.rows(search_type="xray")
        assert (row["outcome"], row["evaluator_sha256"]) == (
            "failure",
            _sha(ALWAYS_MATCH_EVALUATOR),
        )
        [audit] = env.audit_rows()
        assert audit.outcome == "failure"
        assert audit.details["evaluator_sha256s"] == [_sha(ALWAYS_MATCH_EVALUATOR)]

    def test_cancelled_job_records_neither_row_nor_audit(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        env.tracker.start_error = "cancelled"
        _run_async(handle_xray_search(dict(_SEARCH), make_user("grace")))

        assert env.rec.rows(search_type="xray") == []
        assert env.audit_rows() == []


class TestAuditDelivery:
    def test_audit_row_is_handed_to_the_writer_not_written_inline(self, env):
        """While the audit store is write-locked, recording returns at once
        (a synchronous insert would wait on the lock); the row lands once the
        lock is released. So an X-Ray result never waits on the audit insert."""
        import sqlite3
        import threading

        from code_indexer.server.services.xray_execution_audit import (
            record_xray_execution,
        )

        def _record() -> None:
            record_xray_execution(
                username="alice",
                repo_alias=_REPO,
                evaluator_sha256s=[_sha(ALWAYS_MATCH_EVALUATOR)],
                pattern_names=[],
                succeeded=True,
            )

        blocker = sqlite3.connect(str(env.audit.db_path), timeout=1)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            worker = threading.Thread(target=_record, daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            returned_while_locked = not worker.is_alive()
        finally:
            blocker.rollback()
            blocker.close()
        worker.join(timeout=60.0)

        assert returned_while_locked
        [audit] = env.audit_rows()
        assert audit.details["evaluator_sha256s"] == [_sha(ALWAYS_MATCH_EVALUATOR)]


# ^ invariant staged as `.<name>.yaml.XXXXXXXX.tmp`; 236 + 19 bytes = the 255-byte filename limit
_LONGEST_PATTERN_NAME = "n" * 236


class TestAuditPatternNames:
    def test_names_the_pattern_service_accepts_reach_the_audit_row(self, env):
        from code_indexer.server.services.xray_execution_audit import (
            record_xray_execution,
        )

        record_xray_execution(
            username="alice",
            repo_alias=_REPO,
            evaluator_sha256s=[_sha(ALWAYS_MATCH_EVALUATOR)],
            pattern_names=["space pattern", _LONGEST_PATTERN_NAME],
            succeeded=True,
        )

        [audit] = env.audit_rows()
        assert audit.details["pattern_names"] == [
            "space pattern",
            _LONGEST_PATTERN_NAME,
        ]


class _OneSlotLimiter:
    """X-Ray cell limiter double: grants the first cell its slot, then
    refuses, so the batch stops (timed out) before its second cell runs."""

    def __init__(self) -> None:
        self.granted = 0

    def acquire(self, timeout: float) -> bool:
        self.granted += 1
        return self.granted == 1

    def release(self) -> None:
        pass


class TestBatchExecutedOnly:
    def test_batch_row_lists_only_scans_whose_evaluator_ran(self, env):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        _store_pattern(env.cidx_meta)
        env.state.xray_cell_limiter = _OneSlotLimiter()
        handle_xray_search_batch(
            {"repository_alias": _REPO, "scans": _BATCH_SCANS}, make_user("erin")
        )
        env.bjm.run_pending()

        [row] = env.rec.rows(search_type="xray")
        assert (row["evaluator_sha256"], row["xray_pattern_name"]) == (
            _sha(_INLINE_EVALUATOR),
            None,
        )
        [audit] = env.audit_rows()
        assert audit.details["evaluator_sha256s"] == [_sha(_INLINE_EVALUATOR)]


def _batch(env: DoorEnv, aliases: Any, scans: List[Dict[str, Any]]) -> None:
    from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

    body = _mcp_body(
        handle_xray_search_batch(
            {"repository_alias": aliases, "scans": scans}, make_user("erin")
        )
    )
    assert "job_id" in body, body
    env.bjm.run_pending()


class TestBatchAuditOnlyWhatRan:
    def test_engine_construction_failure_names_no_evaluator(self, env):
        from code_indexer.xray.search_engine import XRaySearchEngine

        def _cannot_construct(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("engine unavailable")

        with patch.object(XRaySearchEngine, "__init__", _cannot_construct):
            _batch(env, _REPO, _BATCH_SCANS[:1])

        [row] = env.rec.rows(search_type="xray")
        assert row["evaluator_sha256"] is None
        assert env.audit_rows() == []

    def test_repository_no_cell_reached_is_recorded_but_not_audited(self, env):
        env.state.xray_cell_limiter = _OneSlotLimiter()
        _batch(env, list(_TWO_REPOS), _BATCH_SCANS[:1])

        rows = env.rec.rows(search_type="xray")
        assert {r["repo_alias"]: r["evaluator_sha256"] for r in rows} == {
            "first-repo-global": _sha(_INLINE_EVALUATOR),
            "second-repo-global": None,
        }
        assert [a.target_id for a in env.audit_rows()] == ["first-repo-global"]

    def test_scan_whose_pattern_did_not_resolve_is_recorded_but_not_audited(self, env):
        missing = {"driver_regex": r"def example_", "pattern_name": "no-such-pattern"}
        _batch(env, _REPO, [missing])

        [row] = env.rec.rows(search_type="xray")
        assert (row["evaluator_sha256"], row["outcome"]) == (None, "failure")
        assert env.audit_rows() == []


def _store_named_pattern(cidx_meta: Path, name: str, source: str) -> None:
    scope = cidx_meta / "xray-patterns" / "__any__"
    scope.mkdir(parents=True, exist_ok=True)
    (scope / f"{name}.yaml").write_text(
        f'name: "{name}"\n'
        'description: "example"\n'
        "language: python\n"
        "evaluator_code: |\n" + "".join(f"  {line}\n" for line in source.splitlines())
    )


class TestPatternNameColumn:
    def test_batch_pattern_names_join_with_a_separator_names_cannot_hold(self, env):
        _store_named_pattern(env.cidx_meta, "comma, name", ALWAYS_MATCH_EVALUATOR)
        _store_named_pattern(env.cidx_meta, "plain", ALWAYS_MATCH_EVALUATOR + "\n")
        scans = [
            {"driver_regex": r"def example_", "pattern_name": "comma, name"},
            {"driver_regex": r"return", "pattern_name": "plain"},
        ]
        _batch(env, _REPO, scans)

        [row] = env.rec.rows(search_type="xray")
        assert row["xray_pattern_name"] == "comma, name/plain"
        assert row["xray_pattern_name"].split("/") == ["comma, name", "plain"]


def _write_pattern(cidx_meta: Path, scope: str, source: str, extra: str = "") -> None:
    directory = cidx_meta / "xray-patterns" / scope
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{_PATTERN}.yaml").write_text(
        f"name: {_PATTERN}\n"
        'description: "example"\n'
        "language: python\n"
        + extra
        + "evaluator_code: |\n"
        + "".join(f"  {line}\n" for line in source.splitlines())
    )


def _prepared(env: DoorEnv, alias: str, params: Any = None) -> str:
    """The evaluator the pattern service hands the engine for this alias."""
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    code, _ = XrayPatternService(env.cidx_meta).resolve_and_prepare_pattern(
        repo_alias=alias, pattern_name=_PATTERN, pattern_params=params
    )
    return str(code)


class TestBatchResolvedSources:
    def test_batch_with_a_repo_scoped_override_records_each_repository_source(
        self, env
    ):
        _write_pattern(env.cidx_meta, "__any__", ALWAYS_MATCH_EVALUATOR)
        override = ALWAYS_MATCH_EVALUATOR.replace('"any"', '"override"')
        _write_pattern(env.cidx_meta, "first-repo-global", override)
        scan = {"driver_regex": r"def example_", "pattern_name": _PATTERN}
        _batch(env, list(_TWO_REPOS), [scan])

        rows = env.rec.rows(search_type="xray")
        expected = {alias: _sha(_prepared(env, alias)) for alias in _TWO_REPOS}
        assert {r["repo_alias"]: r["evaluator_sha256"] for r in rows} == expected
        assert len(set(expected.values())) == 2

    def test_batch_with_pattern_params_records_the_prepared_source(self, env):
        declared = "parameters:\n  - name: DEPTH\n    type: usize\n    default: 2\n"
        _write_pattern(env.cidx_meta, "__any__", ALWAYS_MATCH_EVALUATOR, declared)
        scan = {
            "driver_regex": r"def example_",
            "pattern_name": _PATTERN,
            "pattern_params": {"DEPTH": 3},
        }
        _batch(env, _REPO, [scan])

        [row] = env.rec.rows(search_type="xray")
        prepared = _prepared(env, _REPO, {"DEPTH": 3})
        assert prepared != ALWAYS_MATCH_EVALUATOR
        assert row["evaluator_sha256"] == _sha(prepared)


class TestAuditAttribution:
    def test_request_attribution_reaches_the_audit_row_across_the_worker_hop(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.server.middleware.audit_request_context import (
            AuditRequestContext,
            bind_audit_request_context,
            note_mcp_principal,
            reset_audit_request_context,
        )

        token = bind_audit_request_context(
            AuditRequestContext(source="mcp", client_ip="192.0.2.10", auth_method="jwt")
        )
        try:
            note_mcp_principal("example-admin", "alice")
            _run_async(handle_xray_search(dict(_SEARCH), make_user("alice")))
        finally:
            reset_audit_request_context(token)

        [audit] = env.audit_rows()
        assert (audit.actor, audit.source, audit.auth_method) == (
            "example-admin",
            "mcp",
            "jwt",
        )
        raw = env.audit.all_raw_text()
        assert "192.0.2.10" in raw
        assert "alice" in raw


def _cannot_construct(self: Any, *args: Any, **kwargs: Any) -> None:
    raise RuntimeError("engine unavailable")


def _assert_failure_names_no_evaluator(env: DoorEnv) -> None:
    [row] = env.rec.rows(search_type="xray")
    assert (row["outcome"], row["evaluator_sha256"]) == ("failure", None)
    assert env.audit_rows() == []


class TestSingleDoorsRecordOnlyStartedRuns:
    def test_mcp_xray_search_engine_construction_failure(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.xray.search_engine import XRaySearchEngine

        with patch.object(XRaySearchEngine, "__init__", _cannot_construct):
            with pytest.raises(RuntimeError):
                _run_async(handle_xray_search(dict(_SEARCH), make_user("alice")))

        _assert_failure_names_no_evaluator(env)

    def test_mcp_xray_explore_engine_construction_failure(self, env):
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore
        from code_indexer.xray.search_engine import XRaySearchEngine

        with patch.object(XRaySearchEngine, "__init__", _cannot_construct):
            with pytest.raises(RuntimeError):
                _run_async(handle_xray_explore(dict(_SEARCH), make_user("alice")))

        _assert_failure_names_no_evaluator(env)

    def test_rest_xray_search_engine_construction_failure(self, env):
        from code_indexer.xray.search_engine import XRaySearchEngine

        response = _rest_client(env, "dave").post(
            "/api/xray/search",
            json={
                "repository_alias": _REPO,
                "driver_regex": r"def example_",
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
        )
        assert response.status_code == 202, response.text
        with patch.object(XRaySearchEngine, "__init__", _cannot_construct):
            with pytest.raises(RuntimeError):
                env.bjm.run_pending()

        _assert_failure_names_no_evaluator(env)


class TestPerRowAuditGuard:
    def test_one_failing_audit_row_does_not_skip_the_rest(self, env):
        from code_indexer.server.services import search_event_recording as ser

        real_audit = ser.audit_xray_run
        calls: List[str] = []

        def _first_fails(username: str, row: Any, *args: Any) -> None:
            calls.append(str(row[0]))
            if len(calls) == 1:
                raise RuntimeError("audit store unavailable")
            real_audit(username, row, *args)

        with patch.object(ser, "audit_xray_run", _first_fails):
            _batch(env, list(_TWO_REPOS), _BATCH_SCANS[:1])

        assert len(calls) == 2
        assert [a.target_id for a in env.audit_rows()] == [calls[1]]
