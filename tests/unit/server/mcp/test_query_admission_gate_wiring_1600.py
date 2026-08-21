"""Query-path admission-gate wiring at all 15 MCP handler entry points
(Story #1600).

Runs the REAL check_query_admission() (never mocked) against a controlled
_StubGovernor installed via the existing set_memory_governor() seam --
mirroring the established pattern in
tests/unit/server/repositories/test_background_jobs_admission_gate.py.
The only test doubles here are the governor and config_service, which is
exactly the "designed-in test seam" the story calls for, not a mock of the
feature under test.

Each handler must call check_query_admission() BEFORE any real work
executes. For every one of the 15 (module, function) pairs:

  - DENIED: the handler returns the memory_pressure MCP envelope, and the
    stub governor's admission_allowed()/increment_query_admissions_denied()
    were each consulted exactly once with the expected watermark -- proof
    the real gate fired, not a coincidental early return.

  - ALLOWED (no governor installed -> fail-open): execution reaches the
    handler's real body. With empty params/None user, real logic either
    raises AttributeError (verified: search_code and both depmap handlers
    dereference user.username / app.state attributes before any other
    validation) or returns a DIFFERENT, non-memory_pressure error envelope
    (verified for the remaining 12) -- proving the gate does not swallow
    normal traffic.

Story #1600 review remediation (H2/H4/H5) adds three further test classes
below the original wiring class:

  - TestCollaboratorNotCalledOnDeniedProvesRealGateOrdering (H2): the class
    above uses params={}, user=None, under which NO handler could reach
    real work anyway (param validation fails first) -- so a regression that
    moved check_query_admission() to AFTER the real expensive collaborator
    would still pass every one of those 30 cases. This class supplies
    realistic params/mocks that genuinely reach each handler's real
    collaborator (file service, tree-walk service, background-job
    dispatch, git subprocess service, SCIP/dep-map query service,
    activated-repo-manager query) and proves ordering: DENIED never
    invokes it, ALLOWED (same params) does.

  - TestFreshGovernorPreFirstTickDeniesAtHandlerLevel (H4): proves AC
    Scenario 4 (pre-first-sample fail-safe) at the COMPOSITION level (gate
    + a real, never-ticked MemoryGovernor + a real handler), not just at
    the MemoryGovernor primitive level or via a stub.

  - TestAllowedResponseHasNoAdmissionFieldsAtAll (H5, MCP side): proves a
    genuine successful response carries NEITHER retry_after_seconds NOR
    error_code at all, not merely error_code != "memory_pressure".
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, List, Tuple, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import (
    depmap,
    files,
    git_read,
    repos,
    scip,
    search,
    xray,
    xray_batch,
)
from code_indexer.server.mcp.handlers import _utils as _handlers_utils
from code_indexer.server.services import config_service as cfg_service_module
from code_indexer.server.services import memory_governor as mg

_WATERMARK_PCT = 80.0
_RED_MIN_DWELL_SECONDS = 42.7
_EXPECTED_RETRY_AFTER_SECONDS = math.ceil(_RED_MIN_DWELL_SECONDS)  # 43
_EXPECTED_ADMISSION_CALL_COUNT = 1
_FAKE_REPO_PATH = "/tmp/fake-repo-1600"

_HANDLERS: List[Tuple[Any, str]] = [
    (files, "browse_directory"),
    (files, "list_files"),
    (files, "get_file_content"),
    (files, "handle_directory_tree"),
    (search, "handle_regex_search"),
    (search, "search_code"),
    (xray, "handle_xray_search"),
    (xray, "handle_xray_explore"),
    (xray_batch, "handle_xray_search_batch"),
    (scip, "scip_impact"),
    (depmap, "depmap_get_cross_domain_graph_handler"),
    (depmap, "depmap_get_hub_domains_handler"),
    (git_read, "handle_git_search_commits"),
    (git_read, "handle_git_search_diffs"),
    (repos, "get_all_repositories_status"),
]

# The 3 handlers whose real body (once the gate lets execution through)
# dereferences an attribute that is genuinely absent under empty
# params/None user in this unit-test environment (verified via direct
# invocation): search_code -> user.username; both depmap handlers ->
# app.state.dependency_map_service. All 12 others return an error envelope
# instead of raising.
_HANDLERS_THAT_RAISE_ATTRIBUTE_ERROR_WHEN_ALLOWED = {
    ("search", "search_code"),
    ("depmap", "depmap_get_cross_domain_graph_handler"),
    ("depmap", "depmap_get_hub_domains_handler"),
}


def _handler_id(pair: Tuple[Any, str]) -> str:
    module, name = pair
    return f"{module.__name__.rsplit('.', 1)[-1]}.{name}"


class _StubGovernor:
    """Test double you control (mocking-hierarchy tier 3): a real governor
    is deliberately NOT used here (no cgroup/psutil I/O needed), and
    check_query_admission() itself is never mocked -- it runs for real
    against this stub, exactly like BackgroundJobManager's own
    _StubGovernor-based tests."""

    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed
        self.last_red_min_dwell_seconds = _RED_MIN_DWELL_SECONDS
        self.increment_calls = 0
        self.admission_calls: List[float] = []

    def admission_allowed(self, max_used_pct: float) -> bool:
        self.admission_calls.append(max_used_pct)
        return self._allowed

    def increment_query_admissions_denied(self) -> None:
        self.increment_calls += 1


class _StubBackgroundJobsConfig:
    job_admission_memory_max_used_pct = _WATERMARK_PCT
    # B1 fix (kill switch, code review remediation): check_query_admission()
    # now reads this flag before consulting the governor at all. Must be
    # True here so this stub keeps behaving like the gate enabled by
    # default in production -- omitting it would raise AttributeError on
    # the real check_query_admission() call, which its own outer
    # try/except turns into an unintended fail-open (allowed=True) on
    # every DENIED test in this file.
    job_admission_memory_gate_enabled = True


class _StubServerConfig:
    background_jobs_config = _StubBackgroundJobsConfig()


class _StubConfigService:
    def get_config(self) -> _StubServerConfig:
        return _StubServerConfig()


def _unwrap(mcp_response: dict) -> dict:
    """Unwrap the {"content": [{"type": "text", "text": "<json>"}]} envelope."""
    return cast(dict, json.loads(mcp_response["content"][0]["text"]))


async def _invoke(module: Any, name: str, params: dict, user: Any) -> dict:
    func = getattr(module, name)
    result = func(params, user)
    if asyncio.iscoroutine(result):
        result = await result
    return cast(dict, result)


@pytest.fixture(autouse=True)
def _admission_gate_seams():
    """Install the real check_query_admission()'s two lazy-import
    dependencies as controlled test doubles; restore process-level
    singletons on teardown so no state leaks to other test modules."""
    cfg_service_module.set_config_service(_StubConfigService())  # type: ignore[arg-type]
    yield
    mg.clear_memory_governor()
    cfg_service_module.reset_config_service()


@pytest.mark.parametrize("handler", _HANDLERS, ids=_handler_id)
class TestAdmissionGateWiringAllFifteenHandlers:
    @pytest.mark.asyncio
    async def test_denied_returns_memory_pressure_envelope_without_real_work(
        self, handler
    ):
        module, name = handler
        stub_gov = _StubGovernor(allowed=False)
        mg.set_memory_governor(stub_gov)

        result = await _invoke(module, name, {}, None)

        body = _unwrap(result)
        assert body["success"] is False
        assert body["error_code"] == "memory_pressure"
        assert body["retry_after_seconds"] == _EXPECTED_RETRY_AFTER_SECONDS
        assert isinstance(body["error"], str) and body["error"]
        # Proves the real gate fired against the real governor object
        # (not a coincidental early return from unrelated validation).
        assert stub_gov.increment_calls == _EXPECTED_ADMISSION_CALL_COUNT
        assert stub_gov.admission_calls == [_WATERMARK_PCT]

    @pytest.mark.asyncio
    async def test_allowed_proceeds_past_the_gate_to_real_handler_logic(self, handler):
        module, name = handler
        mg.clear_memory_governor()  # no governor -> check_query_admission fails open

        key = (module.__name__.rsplit(".", 1)[-1], name)
        if key in _HANDLERS_THAT_RAISE_ATTRIBUTE_ERROR_WHEN_ALLOWED:
            with pytest.raises(AttributeError):
                await _invoke(module, name, {}, None)
            return

        result = await _invoke(module, name, {}, None)
        body = _unwrap(result)
        assert body.get("error_code") != "memory_pressure"


def _permissive_user(username: str = "alice") -> Any:
    """A user object satisfying both User.username reads and the
    has_permission("query_repos") gate the xray handlers require."""
    user = MagicMock()
    user.username = username
    user.has_permission.return_value = True
    return user


async def _run_denied_then_allowed(invoke_denied, invoke_allowed, collaborator) -> None:
    """Shared two-phase runner (H2 fix): DENIED must never reach
    `collaborator`; ALLOWED must reach it at least once -- the second half
    is what makes this discriminating, proving the chosen params/mocks
    genuinely exercise the real code path rather than an early validation
    failure that would make "not called" trivially true either way."""
    mg.set_memory_governor(_StubGovernor(allowed=False))
    await invoke_denied()
    collaborator.assert_not_called()

    collaborator.reset_mock()
    mg.clear_memory_governor()  # fail-open: allowed
    try:
        await invoke_allowed()
    except Exception:
        # Downstream real-service plumbing (executors, DB connections) is
        # deliberately NOT fully mocked here -- we only need proof the
        # collaborator itself was reached before any later failure. Print
        # (not silently swallow) so a genuine setup bug -- e.g. the
        # collaborator never being reached at all because something EARLIER
        # raised -- is visible in the failure output instead of invisible.
        import traceback

        print(
            "[_run_denied_then_allowed] allowed-phase raised (tolerated, "
            "printed for diagnostics):\n" + traceback.format_exc()
        )
    collaborator.assert_called()


class TestCollaboratorNotCalledOnDeniedProvesRealGateOrdering:
    """H2 fix (Story #1600 review remediation).

    For each of the 15 gated handlers: denied -> real collaborator never
    invoked; allowed (with the SAME realistic params) -> real collaborator
    IS invoked.
    """

    @pytest.mark.asyncio
    async def test_files_list_files(self):
        mock_fs = MagicMock()
        with patch.object(_handlers_utils.app_module, "file_service", mock_fs):
            params = {"repository_alias": "myrepo"}

            async def _denied():
                await _invoke(files, "list_files", params, _permissive_user())

            async def _allowed():
                await _invoke(files, "list_files", params, _permissive_user())

            await _run_denied_then_allowed(_denied, _allowed, mock_fs.list_files)

    @pytest.mark.asyncio
    async def test_files_get_file_content(self):
        mock_fs = MagicMock()
        with patch.object(_handlers_utils.app_module, "file_service", mock_fs):
            params = {"repository_alias": "myrepo", "file_path": "foo.py"}

            async def _denied():
                await _invoke(files, "get_file_content", params, _permissive_user())

            async def _allowed():
                await _invoke(files, "get_file_content", params, _permissive_user())

            await _run_denied_then_allowed(_denied, _allowed, mock_fs.get_file_content)

    @pytest.mark.asyncio
    async def test_files_browse_directory(self):
        mock_fs = MagicMock()
        with patch.object(_handlers_utils.app_module, "file_service", mock_fs):
            params = {"repository_alias": "myrepo"}

            async def _denied():
                await _invoke(files, "browse_directory", params, _permissive_user())

            async def _allowed():
                await _invoke(files, "browse_directory", params, _permissive_user())

            await _run_denied_then_allowed(_denied, _allowed, mock_fs.list_files)

    @pytest.mark.asyncio
    async def test_files_handle_directory_tree(self):
        fake_legacy = MagicMock()
        fake_legacy._resolve_repo_path.return_value = _FAKE_REPO_PATH
        mock_tree_service_cls = MagicMock()
        with (
            patch.object(files, "_get_legacy", return_value=fake_legacy),
            patch(
                "code_indexer.global_repos.directory_explorer.DirectoryExplorerService",
                mock_tree_service_cls,
            ),
            patch.object(
                _handlers_utils.app_module.app.state,
                "golden_repos_dir",
                "/tmp/fake-golden-repos-1600",
                create=True,
            ),
        ):
            params = {"repository_alias": "myrepo"}

            async def _denied():
                await _invoke(
                    files, "handle_directory_tree", params, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    files, "handle_directory_tree", params, _permissive_user()
                )

            await _run_denied_then_allowed(_denied, _allowed, mock_tree_service_cls)

    @pytest.mark.asyncio
    async def test_search_search_code(self):
        mock_search_activated = MagicMock(
            return_value={"content": [{"type": "text", "text": "{}"}]}
        )
        with patch.object(search, "_search_activated_repo", mock_search_activated):
            params = {"query_text": "hello", "repository_alias": "myrepo"}

            async def _denied():
                await _invoke(search, "search_code", params, _permissive_user())

            async def _allowed():
                await _invoke(search, "search_code", params, _permissive_user())

            await _run_denied_then_allowed(_denied, _allowed, mock_search_activated)

    @pytest.mark.asyncio
    async def test_search_handle_regex_search(self):
        fake_legacy = MagicMock()
        fake_legacy._resolve_repo_path.return_value = _FAKE_REPO_PATH
        mock_execute_regex = AsyncMock(return_value=([], {}, MagicMock()))
        with (
            patch.object(search, "_get_legacy", return_value=fake_legacy),
            patch.object(search, "_execute_regex_search", mock_execute_regex),
            patch.object(
                _handlers_utils.app_module.app.state,
                "golden_repos_dir",
                "/tmp/fake-golden-repos-1600",
                create=True,
            ),
        ):
            params = {"pattern": "foo", "repository_alias": "myrepo"}

            async def _denied():
                await _invoke(search, "handle_regex_search", params, _permissive_user())

            async def _allowed():
                await _invoke(search, "handle_regex_search", params, _permissive_user())

            await _run_denied_then_allowed(_denied, _allowed, mock_execute_regex)

    @pytest.mark.asyncio
    async def test_xray_handle_xray_search(self):
        mock_job_tracker = MagicMock()
        with (
            patch(
                "code_indexer.server.mcp.handlers.repos._resolve_golden_repo_path",
                return_value=_FAKE_REPO_PATH,
            ),
            # xray.handle_xray_search resolves the repo path via its own
            # module-local _resolve_repo_path(alias) -- explicitly patched
            # here (matching every other xray test in this directory, e.g.
            # test_xray_search_handler.py) so this test is self-contained
            # and immune to cross-test pollution of the ambient binding.
            patch.object(
                xray,
                "_resolve_repo_path",
                return_value=_FAKE_REPO_PATH,
            ),
            patch.object(_handlers_utils.app_module, "job_tracker", mock_job_tracker),
            # _get_xray_executor() is called BEFORE job_tracker.register_job()
            # and raises RuntimeError when app.state.xray_executor is unset
            # (real lifespan never ran in this unit test) -- must be patched
            # so execution reaches register_job at all.
            patch.object(
                _handlers_utils.app_module.app.state,
                "xray_executor",
                MagicMock(),
                create=True,
            ),
        ):
            params = {
                "repository_alias": "myrepo",
                "pattern": "foo",
                "search_target": "content",
            }

            async def _denied():
                await _invoke(xray, "handle_xray_search", params, _permissive_user())

            async def _allowed():
                await _invoke(xray, "handle_xray_search", params, _permissive_user())

            await _run_denied_then_allowed(
                _denied, _allowed, mock_job_tracker.register_job
            )

    @pytest.mark.asyncio
    async def test_xray_handle_xray_explore(self):
        mock_job_tracker = MagicMock()
        with (
            patch(
                "code_indexer.server.mcp.handlers.repos._resolve_golden_repo_path",
                return_value=_FAKE_REPO_PATH,
            ),
            # xray.handle_xray_explore resolves the repo path via its own
            # module-local _resolve_repo_path(alias) -- explicitly patched
            # here (matching every other xray test in this directory, e.g.
            # test_xray_search_handler.py) so this test is self-contained
            # and immune to cross-test pollution of the ambient binding.
            patch.object(
                xray,
                "_resolve_repo_path",
                return_value=_FAKE_REPO_PATH,
            ),
            patch.object(_handlers_utils.app_module, "job_tracker", mock_job_tracker),
            # _get_xray_executor() is called BEFORE job_tracker.register_job()
            # and raises RuntimeError when app.state.xray_executor is unset
            # (real lifespan never ran in this unit test) -- must be patched
            # so execution reaches register_job at all.
            patch.object(
                _handlers_utils.app_module.app.state,
                "xray_executor",
                MagicMock(),
                create=True,
            ),
        ):
            params = {
                "repository_alias": "myrepo",
                "pattern": "foo",
                "search_target": "content",
            }

            async def _denied():
                await _invoke(xray, "handle_xray_explore", params, _permissive_user())

            async def _allowed():
                await _invoke(xray, "handle_xray_explore", params, _permissive_user())

            await _run_denied_then_allowed(
                _denied, _allowed, mock_job_tracker.register_job
            )

    @pytest.mark.asyncio
    async def test_xray_batch_handle_xray_search_batch(self):
        mock_bjm = MagicMock()
        with (
            patch(
                "code_indexer.server.mcp.handlers.repos._resolve_golden_repo_path",
                return_value=_FAKE_REPO_PATH,
            ),
            patch.object(
                xray_batch, "_get_background_job_manager", return_value=mock_bjm
            ),
        ):
            params = {
                "repository_alias": "myrepo",
                "scans": [{"driver_regex": "foo"}],
            }

            async def _denied():
                await _invoke(
                    xray_batch, "handle_xray_search_batch", params, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    xray_batch, "handle_xray_search_batch", params, _permissive_user()
                )

            await _run_denied_then_allowed(_denied, _allowed, mock_bjm.submit_job)

    @pytest.mark.asyncio
    async def test_scip_scip_impact(self):
        mock_service = MagicMock()
        mock_service.analyze_impact.return_value = {
            "affected_symbols": [],
            "affected_files": [],
        }
        with patch.object(scip, "_get_scip_query_service", return_value=mock_service):
            params = {"symbol": "Foo"}

            async def _denied():
                await _invoke(scip, "scip_impact", params, _permissive_user())

            async def _allowed():
                await _invoke(scip, "scip_impact", params, _permissive_user())

            await _run_denied_then_allowed(
                _denied, _allowed, mock_service.analyze_impact
            )

    @pytest.mark.asyncio
    async def test_depmap_depmap_get_cross_domain_graph_handler(self):
        mock_parser = MagicMock()
        mock_parser.get_cross_domain_graph_with_channels.return_value = (
            [],
            [],
            [],
            [],
        )
        with patch.object(depmap, "_resolve_parser", return_value=(mock_parser, None)):

            async def _denied():
                await _invoke(
                    depmap,
                    "depmap_get_cross_domain_graph_handler",
                    {},
                    _permissive_user(),
                )

            async def _allowed():
                await _invoke(
                    depmap,
                    "depmap_get_cross_domain_graph_handler",
                    {},
                    _permissive_user(),
                )

            await _run_denied_then_allowed(
                _denied, _allowed, mock_parser.get_cross_domain_graph_with_channels
            )

    @pytest.mark.asyncio
    async def test_depmap_depmap_get_hub_domains_handler(self, tmp_path):
        mock_compute_hub_domains = MagicMock(return_value=[])
        mock_dep_map_service = MagicMock()
        mock_dep_map_service.cidx_meta_read_path = tmp_path
        with (
            patch.object(
                _handlers_utils.app_module.app.state,
                "dependency_map_service",
                mock_dep_map_service,
                create=True,
            ),
            patch.object(depmap, "_compute_hub_domains", mock_compute_hub_domains),
        ):

            async def _denied():
                await _invoke(
                    depmap, "depmap_get_hub_domains_handler", {}, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    depmap, "depmap_get_hub_domains_handler", {}, _permissive_user()
                )

            await _run_denied_then_allowed(_denied, _allowed, mock_compute_hub_domains)

    @pytest.mark.asyncio
    async def test_git_read_handle_git_search_commits(self):
        fake_legacy = MagicMock()
        fake_legacy._resolve_git_repo_path.return_value = (_FAKE_REPO_PATH, None)
        mock_git_ops_cls = MagicMock()
        with (
            patch.object(git_read, "_get_legacy", return_value=fake_legacy),
            patch.object(git_read, "GitOperationsService", mock_git_ops_cls),
            # handle_git_search_commits does a FRESH local
            # `from code_indexer.global_repos.git_operations import
            # GitOperationsService` at call time (unlike
            # handle_git_search_diffs, which uses the module-level
            # top-of-file import bound to git_read.GitOperationsService) --
            # so the origin module attribute must ALSO be patched for this
            # call site to observe the mock.
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService",
                mock_git_ops_cls,
            ),
        ):
            params = {"repository_alias": "myrepo", "query": "fix bug"}

            async def _denied():
                await _invoke(
                    git_read, "handle_git_search_commits", params, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    git_read, "handle_git_search_commits", params, _permissive_user()
                )

            await _run_denied_then_allowed(_denied, _allowed, mock_git_ops_cls)

    @pytest.mark.asyncio
    async def test_git_read_handle_git_search_diffs(self):
        fake_legacy = MagicMock()
        fake_legacy._resolve_git_repo_path.return_value = (_FAKE_REPO_PATH, None)
        mock_git_ops_cls = MagicMock()
        with (
            patch.object(git_read, "_get_legacy", return_value=fake_legacy),
            patch.object(git_read, "GitOperationsService", mock_git_ops_cls),
        ):
            params = {"repository_alias": "myrepo", "search_string": "foo"}

            async def _denied():
                await _invoke(
                    git_read, "handle_git_search_diffs", params, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    git_read, "handle_git_search_diffs", params, _permissive_user()
                )

            await _run_denied_then_allowed(_denied, _allowed, mock_git_ops_cls)

    @pytest.mark.asyncio
    async def test_repos_get_all_repositories_status(self):
        mock_arm = MagicMock()
        mock_arm.list_activated_repositories.return_value = []
        with patch.object(
            _handlers_utils.app_module, "activated_repo_manager", mock_arm
        ):

            async def _denied():
                await _invoke(
                    repos, "get_all_repositories_status", {}, _permissive_user()
                )

            async def _allowed():
                await _invoke(
                    repos, "get_all_repositories_status", {}, _permissive_user()
                )

            await _run_denied_then_allowed(
                _denied, _allowed, mock_arm.list_activated_repositories
            )


class TestFreshGovernorPreFirstTickDeniesAtHandlerLevel:
    """H4 fix (Story #1600 review remediation).

    AC Scenario 4 (pre-first-sample fail-safe) proven at the COMPOSITION
    level -- gate + a REAL (never-mocked) MemoryGovernor + a real handler --
    not just at the MemoryGovernor primitive level (already covered by
    test_memory_governor_admission.py::test_blocks_before_first_sample_failsafe)
    nor via a StubGovernor. A freshly constructed MemoryGovernor that has
    NEVER been ticked (zero completed samples) must deny a real gated
    handler call exactly like a denying stub would.

    Distinct from the CRITICAL fix's regression test
    (test_memory_governor_dwell.py::TestSyntheticStartupRedFastTrack): that
    test drives the governor through ONE completed healthy tick and expects
    ALLOW; this test never ticks the governor at all and expects DENY --
    deliberately different points on the same governor lifecycle.
    """

    @pytest.mark.asyncio
    async def test_fresh_untouched_governor_denies_real_handler_call(self):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        real_governor = MemoryGovernor(start_sampler=False)
        assert real_governor._first_tick is True  # zero completed samples
        mg.set_memory_governor(real_governor)

        result = await _invoke(files, "list_files", {}, None)

        body = _unwrap(result)
        assert body["success"] is False
        assert body["error_code"] == "memory_pressure"


class TestAllowedResponseHasNoAdmissionFieldsAtAll:
    """H5 fix, MCP front door (Story #1600 review remediation, AC Scenario 1).

    A genuine allowed/successful MCP response must carry NEITHER
    admission-decision field at all -- not merely
    body.get("error_code") != "memory_pressure" (the wiring test above),
    which would still pass even if the gate leaked a stray
    retry_after_seconds: null into every successful response.
    """

    @pytest.mark.asyncio
    async def test_scip_impact_success_has_no_admission_fields(self):
        mock_service = MagicMock()
        mock_service.analyze_impact.return_value = {
            "affected_symbols": ["Foo.bar"],
            "affected_files": ["foo.py"],
        }
        mg.clear_memory_governor()  # fail-open: allowed
        with patch.object(scip, "_get_scip_query_service", return_value=mock_service):
            result = await _invoke(
                scip, "scip_impact", {"symbol": "Foo"}, _permissive_user()
            )

        body = _unwrap(result)
        assert body["success"] is True
        assert "retry_after_seconds" not in body
        assert "error_code" not in body

    @pytest.mark.asyncio
    async def test_get_all_repositories_status_success_has_no_admission_fields(self):
        """In-scope replacement for the scip_impact case above: scip.py
        belongs to bug #1603's concurrent work and was excluded from #1600's
        review, so this class's only regression coverage must not rest
        solely on an out-of-scope handler. repos.get_all_repositories_status
        is the cheapest in-scope candidate -- the admission gate is its
        first statement (repos.py:512)."""
        mock_arm = MagicMock()
        mock_arm.list_activated_repositories.return_value = []
        mg.clear_memory_governor()  # fail-open: allowed
        with patch.object(
            _handlers_utils.app_module, "activated_repo_manager", mock_arm
        ):
            result = await _invoke(
                repos, "get_all_repositories_status", {}, _permissive_user()
            )

        body = _unwrap(result)
        assert body["success"] is True
        assert "retry_after_seconds" not in body
        assert "error_code" not in body
