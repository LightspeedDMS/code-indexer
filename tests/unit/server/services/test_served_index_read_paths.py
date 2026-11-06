"""Every index read path holds what it serves for the read's whole duration.

A served-index checkout may be evicted to free the pod's disk
(``served_index_cache``). A read that served it must keep it -- in every
server process on the pod -- until the read is over, not only for the
stamp/idle window. Each read path below runs inside a read scope
(``reads_served_indexes`` / ``served_index_reads``): whatever it serves is held
by its readers lock SHARED until it returns.

Per path, the REAL entry point is called (its real decorator, the real cache,
a real checkout); only the body behind the decorator is stood in for, by one
that serves a checkout and then -- mid-read -- runs an eviction pass. The
eviction must be refused while the read runs and succeed once it returned.
Removing a path's scope fails its test.

Also pinned: the background jobs a request hands its read to (the live
temporal worker, the health check job) serve again inside their own scope,
because a job thread inherits nothing from the request.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Tuple, cast

import pytest

from code_indexer.server.utils.route_table import iter_routes
from code_indexer.server.services import served_index_cache as sic
from tests.unit.server._scip_access import GRANT_ALL
from tests.unit.server.services import test_served_index_cache as cache_tests
from tests.unit.server.app_state_patch import patch_app_state


def _scip_service(service_class: Any, golden: Path) -> Any:
    """A SCIPQueryService whose caller may read every repository."""
    return service_class(golden_repos_dir=golden, access_filtering_service=GRANT_ALL)


#: The fixture the cache tests build a pod with (a real cache and store).
pod = cache_tests.pod

_cache = cache_tests._cache
_golden = cache_tests._golden
_serve_golden = cache_tests._serve_golden
_later = cache_tests._later
_served = cache_tests._served
KB = cache_tests.KB


def _route(router: Any, path: str, method: str) -> Callable[..., Any]:
    for route_path, route in iter_routes(router.routes):
        if route_path == path and method in (getattr(route, "methods", None) or ()):
            endpoint: Callable[..., Any] = route.endpoint
            return endpoint
    raise LookupError(f"{method} {path}")


def _app_route(path: str, method: str) -> Callable[..., Any]:
    from code_indexer.server.app import app

    return _route(app, path, method)


def _web_route(path: str, method: str) -> Callable[..., Any]:
    from code_indexer.server.web.routes import web_router

    return _route(web_router, path, method)


def _attr(
    module: str, *names: str, package: str = "code_indexer.server"
) -> Callable[[], Callable[..., Any]]:
    def get() -> Callable[..., Any]:
        obj: Any = importlib.import_module(f"{package}.{module}")
        for name in names:
            obj = getattr(obj, name)
        fn: Callable[..., Any] = obj
        return fn

    return get


#: Where the SCIP feature's server modules live (cidx-scip, modular phase 4d).
_SCIP = "cidx_scip.server"

#: (read path, how to reach its real entry point)
READ_PATHS: List[Tuple[str, Callable[[], Callable[..., Any]]]] = [
    ("REST /api/query", lambda: _app_route("/api/query", "POST")),
    (
        "SemanticQueryManager._perform_search (MCP search, query jobs)",
        _attr(
            "query.semantic_query_manager", "SemanticQueryManager", "_perform_search"
        ),
    ),
    (
        "MCP global search",
        _attr("mcp.handlers.search.repo_search", "_search_global_repo"),
    ),
    (
        "MCP temporal search (live dispatch)",
        _attr(
            "mcp.handlers.search.temporal_search",
            "_execute_temporal_via_live_dispatch",
        ),
    ),
    (
        "live temporal dispatch (freshness)",
        _attr("services.temporal_live_dispatch", "execute_live_temporal_search"),
    ),
    ("live temporal worker", _attr("services.temporal_worker", "run_temporal_worker")),
    (
        "multi-repo semantic",
        _attr(
            "multi.multi_search_service", "MultiSearchService", "_search_semantic_sync"
        ),
    ),
    (
        "multi-repo FTS",
        _attr("multi.multi_search_service", "MultiSearchService", "_search_fts_sync"),
    ),
    (
        "multi-repo temporal",
        _attr(
            "multi.multi_search_service", "MultiSearchService", "_search_temporal_sync"
        ),
    ),
    (
        "multi-repo regex (subprocess)",
        _attr(
            "multi.multi_search_service",
            "MultiSearchService",
            "_search_single_repo_subprocess",
        ),
    ),
    (
        "repository indexes status",
        _attr("routers.repository_health", "_check_indexes"),
    ),
    (
        "repository health check job",
        _attr("routers.repository_health", "_health_check_job"),
    ),
    (
        "diagnostics HNSW validation",
        _attr(
            "services.diagnostics_service",
            "DiagnosticsService",
            "_validate_hnsw_indexes",
        ),
    ),
    (
        "activated index status",
        _attr(
            "services.activated_repo_index_manager",
            "ActivatedRepoIndexManager",
            "get_index_status",
        ),
    ),
    (
        "activated indexes route",
        _attr("routers.activated_repos", "get_indexes_status"),
    ),
    *[
        (
            f"SCIP {name}",
            _attr("query_service", "SCIPQueryService", name, package=_SCIP),
        )
        for name in (
            "find_definition",
            "find_references",
            "get_dependencies",
            "get_dependents",
            "analyze_impact",
            "trace_callchain",
            "get_context",
        )
    ],
    *[
        (
            f"multi-repo SCIP {name}",
            _attr("multi_service", "SCIPMultiService", name, package=_SCIP),
        )
        for name in (
            "_find_definition_in_repo",
            "_find_references_in_repo",
            "_get_dependencies_in_repo",
            "_get_dependents_in_repo",
            "_trace_callchain_in_repo",
        )
    ],
    (
        "REST repository search (v2)",
        _attr("services.search_service", "SemanticSearchService", "search_repository"),
    ),
    ("web UI query", lambda: _web_route("/query", "POST")),
    ("web UI query results", lambda: _web_route("/partials/query-results", "POST")),
]


def _reading_body(pod: SimpleNamespace, seen: List[str]) -> Any:
    """Stands in for a read path's body: serves a checkout, then an
    eviction pass runs while the read is still going."""

    def read(a: Path) -> None:
        _serve_golden(pod, a, "a")
        _later(pod)  # the read takes a while
        seen.append(pod.cache.run_pass().skipped.get(str(a), "evicted"))

    return read


@pytest.mark.parametrize("label,entry", READ_PATHS, ids=[p[0] for p in READ_PATHS])
def test_a_read_path_keeps_what_it_serves_until_it_returns(
    pod: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    entry: Callable[[], Callable[..., Any]],
) -> None:
    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    seen: List[str] = []
    read = _reading_body(pod, seen)
    fn = entry()
    assert hasattr(fn, "__wrapped__"), f"{label} is not a read scope"
    if asyncio.iscoroutinefunction(fn):

        async def async_body(*args: Any, **kwargs: Any) -> str:
            read(a)
            return "done"

        monkeypatch.setattr(fn, "__wrapped__", async_body)
        result = asyncio.run(fn("arg", key="value"))
    else:

        def body(*args: Any, **kwargs: Any) -> str:
            read(a)
            return "done"

        monkeypatch.setattr(fn, "__wrapped__", body)
        result = fn("arg", key="value")
    assert result == "done"  # the body's result comes back unchanged
    assert seen == [sic.SKIP_IN_USE], f"{label}: evicted mid-read"
    _later(pod)
    assert pod.cache.run_pass().evicted == [str(a)], f"{label}: held after return"


# ── jobs serve again inside their own scope ────────────────────────────────


def test_the_live_temporal_worker_serves_a_golden_repo_in_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import alias_manager
    from code_indexer.server.services import temporal_worker

    served: List[Tuple[str, str, bool]] = []

    def ensure(self: Any, alias: str, target: str) -> None:
        served.append((alias, target, sic._read_scope.get() is not None))

    monkeypatch.setattr(alias_manager.AliasManager, "ensure_served_here", ensure)
    manager = SimpleNamespace(activated_repos_dir=str(tmp_path / "activated-repos"))
    worker_input = SimpleNamespace(
        repository_alias="repo-global", repo_path="/data/golden/v_1", username="u"
    )
    with sic.served_index_reads():
        temporal_worker._serve_worker_repo(cast(Any, worker_input), manager)
    assert served == [("repo-global", "/data/golden/v_1", True)]


def test_the_live_temporal_worker_serves_an_activated_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import activated_index_store, temporal_worker

    served: List[Tuple[Any, str, str]] = []
    monkeypatch.setattr(
        activated_index_store,
        "serve_activated_index",
        lambda manager, user, alias: served.append((manager, user, alias)),
    )
    manager = SimpleNamespace(activated_repos_dir="/data/activated-repos")
    worker_input = SimpleNamespace(
        repository_alias="mine", repo_path="/data/activated-repos/u/mine", username="u"
    )
    temporal_worker._serve_worker_repo(cast(Any, worker_input), manager)
    temporal_worker._serve_worker_repo(cast(Any, worker_input), None)
    assert served == [(manager, "u", "mine")]


def test_the_worker_serves_before_it_builds_its_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import temporal_worker

    order: List[str] = []
    manager = object()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(temporal_worker, "_build_ctx", lambda wi: None)
    monkeypatch.setattr(
        temporal_worker,
        "_resolve_golden_temporal_context",
        lambda wi, job, activated_repo_manager=None: SimpleNamespace(
            activated_repo_manager=manager
        ),
    )
    monkeypatch.setattr(
        temporal_worker,
        "_serve_worker_repo",
        lambda wi, m: order.append(f"serve {m is manager}"),
    )

    def build(*args: Any) -> Any:
        order.append("build")
        raise _Stop

    monkeypatch.setattr(temporal_worker, "_build_temporal_backend", build)
    with pytest.raises(_Stop):
        temporal_worker.run_temporal_worker(
            cast(Any, SimpleNamespace()), payload_cache=cast(Any, None), job_id="j"
        )
    assert order == ["serve True", "build"]


def test_the_health_check_job_serves_again_inside_its_scope(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.routers import repository_health

    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    seen: List[str] = []

    def resolve(alias: str, user: Any) -> Tuple[str, Path]:
        _serve_golden(pod, a, "a")  # the job's own serve
        return "a", a

    def compute(*args: Any, **kwargs: Any) -> Any:
        _later(pod)
        seen.append(pod.cache.run_pass().skipped.get(str(a), "evicted"))
        return SimpleNamespace(model_dump=lambda: {"ok": True})

    monkeypatch.setattr(repository_health, "_resolve_repository_path", resolve)
    monkeypatch.setattr(repository_health, "compute_repository_health", compute)
    monkeypatch.setattr(repository_health, "get_shared_health_service", lambda: None)
    result = repository_health._health_check_job("a", cast(Any, None), "a", a, False)
    assert result == {"ok": True} and seen == [sic.SKIP_IN_USE]


def test_temporal_status_reads_hold_the_temporal_root(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.services.temporal import temporal_status

    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    seen: List[str] = []

    def body(*args: Any, **kwargs: Any) -> str:
        _serve_golden(pod, a, "a")
        _later(pod)
        seen.append(pod.cache.run_pass().skipped.get(str(a), "evicted"))
        return "status"

    for name in ("_get_temporal_repo_status", "_get_temporal_repo_max_commits"):
        monkeypatch.setattr(temporal_status, name, body)
    assert (
        temporal_status.get_temporal_repo_status(Path("g"), "a", Path("l")) == "status"
    )
    assert (
        temporal_status.get_temporal_repo_max_commits(Path("g"), "a", Path("l"))
        == "status"
    )
    assert seen == [sic.SKIP_IN_USE, sic.SKIP_IN_USE]


# ── real bodies: the path itself serves what it reads, inside its scope ─────
#
# The tests above prove each entry point opens a scope. These run the REAL
# body of the paths whose decorator was their only change, with real alias
# pointers and real serves; only the leaf that reads the index files is
# replaced, by a probe that runs an eviction pass mid-read. Removing a path's
# serve call (or its scope) leaves the checkout unprotected and fails here.


class _Probe(Exception):
    """Raised by a probe after it recorded the eviction outcome."""


def _probe(pod: SimpleNamespace, a: Path, seen: List[str]) -> Callable[..., Any]:
    def probe(*args: Any, **kwargs: Any) -> Any:
        _later(pod)
        seen.append(pod.cache.run_pass().skipped.get(str(a), "evicted"))
        raise _Probe

    return probe


def _aliased(pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A golden checkout ``a`` this pod holds, named by the ``a-global`` alias
    pointer with its published version, under a delegated store."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.services import index_materializer as im
    from code_indexer.server.startup import index_store_wiring
    from code_indexer.utils.index_store import published_index_fields

    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    version = (a / im.CHECKED_OUT_MARKER).read_text()
    AliasManager(str(pod.golden / "aliases")).create_alias(
        "a-global", str(a), repo_name="a", extra=published_index_fields("a", version)
    )
    _later(pod)
    return a


def _evictable_after(pod: SimpleNamespace, a: Path) -> None:
    _later(pod)
    assert pod.cache.run_pass().evicted == [str(a)]


def _scip_db(a: Path) -> Path:
    from cidx_scip.paths import SCIP_DB_FILENAME, scip_index_dir

    db = scip_index_dir(a) / SCIP_DB_FILENAME
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"scip")
    return db


@pytest.mark.parametrize(
    "method",
    [
        "find_definition",
        "find_references",
        "get_dependencies",
        "get_dependents",
        "analyze_impact",
        "trace_callchain",
        "get_context",
    ],
)
def test_a_scip_query_serves_and_holds_its_alias_target(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    from cidx_scip.query import composites, primitives
    from cidx_scip.server.query_service import SCIPQueryService

    a = _aliased(pod, monkeypatch)
    _scip_db(a)
    seen: List[str] = []
    probe = _probe(pod, a, seen)

    class Engine:
        def __init__(self, path: Path) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            return probe

    monkeypatch.setattr(primitives, "SCIPQueryEngine", Engine)
    monkeypatch.setattr(composites, "analyze_impact", probe)
    monkeypatch.setattr(composites, "get_smart_context", probe)
    service = _scip_service(SCIPQueryService, pod.golden)
    args = ("Symbol", "Other") if method == "trace_callchain" else ("Symbol",)
    try:
        getattr(service, method)(*args, repository_alias="a", username="u")
    except _Probe:
        pass
    assert seen == [sic.SKIP_IN_USE]
    _evictable_after(pod, a)


@pytest.mark.parametrize(
    "worker",
    [
        "_find_definition_in_repo",
        "_find_references_in_repo",
        "_get_dependencies_in_repo",
        "_get_dependents_in_repo",
        "_trace_callchain_in_repo",
    ],
)
def test_a_multi_repo_scip_worker_serves_and_holds_its_repo(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    from code_indexer.server import app as app_module
    from cidx_scip.server import multi_service as scip_multi_service

    a = _aliased(pod, monkeypatch)
    _scip_db(a)
    seen: List[str] = []
    probe = _probe(pod, a, seen)

    class Engine:
        def __init__(self, path: Path) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            return probe

    monkeypatch.setattr(scip_multi_service, "SCIPQueryEngine", Engine)
    monkeypatch.setattr(
        scip_multi_service, "_get_golden_repos_dir", lambda: str(pod.golden)
    )
    registry = SimpleNamespace(
        global_repos=SimpleNamespace(get_repo=lambda repo_id: {"alias_name": repo_id})
    )
    patch_app_state(monkeypatch, app_module.app.state, "backend_registry", registry)
    service = scip_multi_service.SCIPMultiService(max_workers=1)
    request = SimpleNamespace(
        symbol="Symbol",
        from_symbol="Symbol",
        to_symbol="Other",
        limit=10,
        max_depth=2,
        repositories=["a-global"],
        timeout_seconds=30,
    )
    try:
        getattr(service, worker)("a-global", request)
    except Exception:  # the probe's _Probe, or a worker's own wrapping of it
        pass
    service.thread_executor.shutdown(wait=False)
    assert seen == [sic.SKIP_IN_USE]
    _evictable_after(pod, a)


def test_the_v2_repository_search_serves_and_holds_its_repo(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server import app as app_module
    from code_indexer.server.services import search_service

    a = _aliased(pod, monkeypatch)
    seen: List[str] = []
    monkeypatch.setattr(
        search_service, "_get_golden_repos_dir", lambda: str(pod.golden)
    )
    registry = SimpleNamespace(
        global_repos=SimpleNamespace(
            list_repos=lambda: {"a-global": {"alias_name": "a-global"}}
        )
    )
    patch_app_state(monkeypatch, app_module.app.state, "backend_registry", registry)
    monkeypatch.setattr(
        search_service.SemanticSearchService,
        "search_repository_path",
        lambda self, path, request: _probe(pod, a, seen)(),
    )
    with pytest.raises(_Probe):
        search_service.SemanticSearchService().search_repository(
            "a-global", cast(Any, None)
        )
    assert seen == [sic.SKIP_IN_USE]
    _evictable_after(pod, a)


def test_the_indexes_status_route_serves_and_holds_its_repo(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.routers import repository_health

    a = _aliased(pod, monkeypatch)
    seen: List[str] = []
    golden_manager = SimpleNamespace(
        golden_repos_dir=str(pod.golden),
        get_golden_repo=lambda alias: object() if alias == "a" else None,
        get_actual_repo_path=lambda alias: str(a),
    )
    monkeypatch.setattr(
        repository_health, "_get_golden_repo_manager", lambda: golden_manager
    )
    monkeypatch.setattr(
        repository_health, "detect_semantic_index", _probe(pod, a, seen)
    )
    with pytest.raises(_Probe):
        repository_health._check_indexes("a", cast(Any, SimpleNamespace(username="u")))
    assert seen == [sic.SKIP_IN_USE]
    _evictable_after(pod, a)


def test_the_activated_index_status_serves_and_holds_the_copy(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )
    from code_indexer.server.startup import index_store_wiring

    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: pod.store)
    cache = _cache(pod)
    cache_tests._publish(pod, "activated.alice", 10 * KB)
    copy = pod.activated / "alice" / "repo"
    cache_tests._serve_activated(pod, copy, "activated.alice")
    cache_tests._limit(cache, 5 * KB)
    _later(pod)
    seen: List[str] = []

    def serve_index_here(user: str, alias: str) -> str:
        # The manager's registry gate is stubbed; the serve is the real one.
        cache_tests._serve_activated(pod, copy, "activated.alice")
        return str(copy)

    manager = SimpleNamespace(
        get_activated_repo_path=lambda user, alias: str(copy),
        serve_index_here=serve_index_here,
    )
    fake_self = SimpleNamespace(
        activated_repo_manager=manager,
        _index_is_delegated=lambda: True,
        get_index_status_for_path=_probe(pod, copy, seen),
    )
    with pytest.raises(_Probe):
        ActivatedRepoIndexManager.get_index_status(
            cast(Any, fake_self), "repo", "alice"
        )
    assert seen == [sic.SKIP_IN_USE]
    _evictable_after(pod, copy)


def _diagnostics(
    pod: SimpleNamespace,
    a: Path,
    seen: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from contextlib import contextmanager

    from code_indexer.server.services import repository_health_aggregator
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    @contextmanager
    def guarded() -> Any:
        yield SimpleNamespace(
            execute=lambda sql: SimpleNamespace(fetchall=lambda: [("a", str(a))])
        )

    def check(collection_dir: Path, repo: str, name: str) -> None:
        _later(pod)
        seen.append(pod.cache.run_pass().skipped.get(str(a), "evicted"))

    fake_self = SimpleNamespace(
        _conn_manager=SimpleNamespace(guarded_connection=guarded),
        _check_collection_health=check,
    )
    monkeypatch.setattr(
        repository_health_aggregator,
        "discover_sister_temporal_collections",
        lambda *args: [],
    )
    return lambda: DiagnosticsService._validate_hnsw_indexes(
        cast(Any, fake_self), pod.golden
    )


def test_the_diagnostics_check_holds_each_index_it_checks(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    from code_indexer.utils.index_store import INDEX_DIRNAME

    (a / INDEX_DIRNAME / "index" / "voyage-code-3").mkdir()
    _later(pod)
    seen: List[str] = []
    result = _diagnostics(pod, a, seen, monkeypatch)()
    assert seen == [sic.SKIP_IN_USE]
    assert result["repos_checked"] == 1 and "repos_not_on_this_pod" not in result
    _evictable_after(pod, a)


def test_the_diagnostics_check_reports_an_evicted_index_as_not_on_this_pod(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache(pod, max_bytes=5 * KB, low_bytes=5 * KB)
    a = _golden(pod, "a")
    _later(pod)
    assert pod.cache.run_pass().evicted == [str(a)]
    checkouts = len(cache_tests._checkouts(pod))
    seen: List[str] = []
    result = _diagnostics(pod, a, seen, monkeypatch)()
    assert result["repos_not_on_this_pod"] == ["a"]
    assert result["repos_checked"] == 0 and result["repos_with_issues"] == []
    assert (
        seen == [] and len(cache_tests._checkouts(pod)) == checkouts
    )  # nothing fetched


def test_hold_if_present_records_no_access_and_needs_the_index(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    a = _golden(pod, "a")
    with sic.served_index_reads():
        assert cache.hold_if_present(a)
        assert cache.hits == 0 and str(a) not in cache._access
        with sic._readers_free([a]) as free:
            assert not free
    assert not cache.hold_if_present(pod.golden / "nowhere" / "x")
    with sic._readers_free([a]) as free:
        assert free


# ── work that outlives its read: the SCIP context watchdog ──────────────────


def test_an_abandoned_scip_context_worker_keeps_its_checkout(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_context times out and returns while its worker thread still reads
    the .scip.db files: the worker carries the checkout until it ends."""
    import threading
    import time

    from cidx_scip.database.queries import QueryTimeoutError
    from cidx_scip.query import composites
    from cidx_scip.server.query_service import SCIPQueryService

    a = _aliased(pod, monkeypatch)
    _scip_db(a)
    started, release, finished = (threading.Event() for _ in range(3))

    def slow_impl(**kwargs: Any) -> Any:
        started.set()
        release.wait(10)
        finished.set()
        raise RuntimeError("abandoned")

    monkeypatch.setattr(composites, "_get_smart_context_impl", slow_impl)
    service = _scip_service(SCIPQueryService, pod.golden)
    with pytest.raises(QueryTimeoutError):
        service.get_context(
            "Symbol", repository_alias="a", username="u", timeout_seconds=1
        )
    assert started.is_set() and not finished.is_set()
    _later(pod)
    assert pod.cache.run_pass().skipped.get(str(a)) == sic.SKIP_IN_USE
    release.set()
    assert finished.wait(10)
    deadline = time.monotonic() + 5
    while True:  # the worker releases its copy as it exits
        _later(pod)
        if pod.cache.run_pass().evicted == [str(a)]:
            break
        assert time.monotonic() < deadline, "the worker never released it"
        time.sleep(0.02)


# ── an all-repository SCIP query would check every repository out ──────────


_SCIP_METHODS = [
    "find_definition",
    "find_references",
    "get_dependencies",
    "get_dependents",
    "analyze_impact",
    "trace_callchain",
    "get_context",
]


@pytest.mark.parametrize("method", _SCIP_METHODS)
def test_an_unscoped_scip_query_fails_loud_on_a_delegated_store(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    from code_indexer.server.services import index_materializer as im
    from cidx_scip.server.query_service import (
        SCIPQueryService,
        UnscopedScipQueryError,
    )

    _aliased(pod, monkeypatch)
    checkouts = len([c for c in pod.artifacts.calls if c[0] == "checkout"])
    monkeypatch.setattr(
        im, "ensure_published_index", lambda *a, **k: pytest.fail("checked out")
    )
    service = _scip_service(SCIPQueryService, pod.golden)
    args = ("Symbol", "Other") if method == "trace_callchain" else ("Symbol",)
    with pytest.raises(UnscopedScipQueryError, match="repository_alias"):
        getattr(service, method)(*args, username="u")
    assert len([c for c in pod.artifacts.calls if c[0] == "checkout"]) == checkouts


def test_a_scoped_scip_query_still_resolves_on_a_delegated_store(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cidx_scip.server.query_service import SCIPQueryService

    a = _aliased(pod, monkeypatch)
    db = _scip_db(a)
    service = _scip_service(SCIPQueryService, pod.golden)
    assert service.find_scip_files(repository_alias="a-global", username="u") == [db]


def test_an_unscoped_scip_query_is_unchanged_under_the_local_store(
    tmp_path: Path,
) -> None:
    from cidx_scip.paths import SCIP_DB_FILENAME, scip_index_dir
    from cidx_scip.server.query_service import SCIPQueryService

    sic.install_served_index_cache(None)
    db = scip_index_dir(tmp_path / "repo") / SCIP_DB_FILENAME
    db.parent.mkdir(parents=True)
    db.write_bytes(b"scip")
    service = _scip_service(SCIPQueryService, tmp_path)
    assert service.find_scip_files(username="u") == [db]


def test_hold_if_present_creates_no_lock_file_for_an_index_not_here(
    pod: SimpleNamespace,
) -> None:
    cache = _cache(pod)
    absent = pod.golden / "never-served"
    absent.mkdir()
    with sic.served_index_reads():
        assert not cache.hold_if_present(absent)
    assert not sic.readers_lock_path(absent).exists()


def _vector_storage(tmp_path: Path, validation: dict) -> Any:
    from unittest.mock import Mock, patch

    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    (tmp_path / "data" / "golden-repos").mkdir(parents=True, exist_ok=True)
    config = Mock(server_dir=str(tmp_path))
    with (
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as get_config_service,
        patch.object(
            DiagnosticsService, "_validate_hnsw_indexes", return_value=validation
        ),
    ):
        get_config_service.return_value.get_config.return_value = config
        service = DiagnosticsService(db_path=str(tmp_path / "cidx_server.db"))
        return asyncio.run(service.check_vector_storage())


def _validation(checked: int, healthy: int, issues: list, **extra: Any) -> dict:
    return {
        "repos_checked": checked,
        "repos_with_healthy_indexes": healthy,
        "repos_with_issues": issues,
        **extra,
    }


def test_a_pod_holding_no_index_is_not_reported_as_no_repositories(
    tmp_path: Path,
) -> None:
    from code_indexer.server.services.diagnostics_service import DiagnosticStatus

    result = _vector_storage(
        tmp_path, _validation(0, 0, [], repos_not_on_this_pod=["a", "b"])
    )
    assert result.status == DiagnosticStatus.NOT_APPLICABLE
    assert "No HNSW index is on this pod" in result.message
    assert "2 registered repositories' indexes are not on this pod" in result.message


@pytest.mark.parametrize("issues", [[], [{"repo": "x", "issue": "bad"}]])
def test_the_message_names_the_repositories_not_on_this_pod(
    tmp_path: Path, issues: list
) -> None:
    result = _vector_storage(
        tmp_path, _validation(1, 1 - len(issues), issues, repos_not_on_this_pod=["b"])
    )
    assert result.message.endswith(
        "; 1 registered repositories' indexes are not on this pod "
        "(checked out on their next query)"
    )


def test_the_local_store_messages_are_unchanged(tmp_path: Path) -> None:
    from code_indexer.server.services.diagnostics_service import DiagnosticStatus

    empty = _vector_storage(tmp_path, _validation(0, 0, []))
    assert (empty.status, empty.message) == (
        DiagnosticStatus.NOT_CONFIGURED,
        "No golden repositories found",
    )
    healthy = _vector_storage(tmp_path, _validation(2, 2, []))
    assert healthy.message == "All 2 repositories have healthy HNSW indexes"
