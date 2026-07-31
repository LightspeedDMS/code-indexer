"""EVO-65021 stream-05: both golden-repo SCIP entrypoints route through the funnel.

Verifies that under ``scip_backend='delegated'`` neither entrypoint runs the
local ``cidx scip generate`` subprocess, and that a fail-soft skip does not fail
the enclosing job; under the default ``local`` backend the subprocess still runs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services import scip_lifecycle
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.services.scip_lifecycle import ScipDelegationOutcome

_REFRESH_MOD = "code_indexer.global_repos.refresh_scheduler"
_ACT_MOD = "code_indexer.server.services.activated_repo_index_manager"
_FUNNEL = "code_indexer.server.services.scip_lifecycle.funnel_scip_generation"


# --------------------------------------------------------------------------- #
# refresh_scheduler._run_source_scip (golden-repo entrypoint)
# --------------------------------------------------------------------------- #
def _scheduler() -> RefreshScheduler:
    # Bypass the heavy __init__: _run_source_scip only uses its params + module
    # functions, none of the scheduler's instance state.
    return RefreshScheduler.__new__(RefreshScheduler)


def _allocator():
    alloc = Mock()
    alloc.phase_start.return_value = 10
    alloc.phase_end.return_value = 90
    return alloc


class TestRefreshSchedulerEntrypoint:
    def test_delegated_skips_local_subprocess(self):
        sched = _scheduler()
        pcb = Mock()
        with (
            patch(f"{_REFRESH_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
            patch(f"{_REFRESH_MOD}.subprocess.run") as srun,
        ):
            sched._run_source_scip("my-repo", "/src", _allocator(), pcb)
        funnel.assert_called_once()
        assert funnel.call_args.kwargs["alias"] == "my-repo"
        srun.assert_not_called()  # delegated build enqueued, not run inline
        pcb.assert_called_once()  # phase_end progress marker emitted

    def test_delegated_fail_soft_does_not_raise(self):
        sched = _scheduler()
        with (
            patch(f"{_REFRESH_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.SKIPPED),
            patch(f"{_REFRESH_MOD}.subprocess.run") as srun,
        ):
            # Must not raise — the enclosing golden-repo index job is not failed.
            sched._run_source_scip("my-repo", "/src", _allocator(), None)
        srun.assert_not_called()

    def test_local_backend_runs_subprocess(self):
        sched = _scheduler()
        with (
            patch(f"{_REFRESH_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.NOT_DELEGATED),
            patch(f"{_REFRESH_MOD}.subprocess.run") as srun,
            patch(f"{_REFRESH_MOD}.build_cidx_subprocess_env", return_value={}),
        ):
            srun.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            sched._run_source_scip("my-repo", "/src", _allocator(), None)
        srun.assert_called_once()
        assert srun.call_args.args[0] == ["cidx", "scip", "generate"]

    def test_global_suffix_normalized_to_bare_alias(self):
        # CRITICAL-1 regression: the entrypoint is handed the -global registry
        # alias, but the funnel's golden resolver is keyed by the BARE alias. It
        # must be normalized before funnelling, or every golden repo silently
        # loses SCIP under the delegated backend.
        sched = _scheduler()
        with (
            patch(f"{_REFRESH_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
            patch(f"{_REFRESH_MOD}.subprocess.run"),
        ):
            sched._run_source_scip("my-repo-global", "/src", _allocator(), None)
        assert funnel.call_args.kwargs["alias"] == "my-repo"


# --------------------------------------------------------------------------- #
# activated_repo_index_manager._execute_scip_indexing (activated-repo entrypoint)
# --------------------------------------------------------------------------- #
def _index_manager(tmp_path) -> ActivatedRepoIndexManager:
    return ActivatedRepoIndexManager(
        data_dir=str(tmp_path),
        background_job_manager=Mock(),
        activated_repo_manager=Mock(),
    )


class TestActivatedRepoEntrypoint:
    def test_delegated_skips_local_subprocess_and_succeeds(self, tmp_path):
        mgr = _index_manager(tmp_path)
        with (
            patch(f"{_ACT_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
            patch("subprocess.run") as srun,
        ):
            result = mgr._execute_scip_indexing(
                "/repo", clear=False, repo_alias="my-repo"
            )
        srun.assert_not_called()
        assert result["success"] is True
        assert "enqueued" in result["message"]
        # alias is threaded through so the delegated backend can resolve it.
        assert funnel.call_args.kwargs["alias"] == "my-repo"

    def test_delegated_fail_soft_still_reports_success(self, tmp_path):
        mgr = _index_manager(tmp_path)
        with (
            patch(f"{_ACT_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.SKIPPED),
            patch("subprocess.run") as srun,
        ):
            result = mgr._execute_scip_indexing(
                "/repo", clear=False, repo_alias="my-repo"
            )
        srun.assert_not_called()
        # Fail-soft: the reindex job is NOT failed by a SCIP-backend gap.
        assert result["success"] is True

    def test_local_backend_runs_subprocess(self, tmp_path):
        mgr = _index_manager(tmp_path)
        with (
            patch(f"{_ACT_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.NOT_DELEGATED),
            patch("subprocess.run") as srun,
        ):
            srun.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = mgr._execute_scip_indexing(
                "/repo", clear=False, repo_alias="my-repo"
            )
        srun.assert_called_once()
        assert result["success"] is True
        assert "SCIP generation completed" in result["message"]

    def test_activated_alias_is_flagged_and_clear_threaded_to_funnel(self, tmp_path):
        # CRITICAL-2 + MAJOR-5 regression: the activated entrypoint must tell the
        # funnel this is an ACTIVATED (non-golden) alias so it is not resolved
        # through the golden namespace, and must thread ``clear`` so a rebuild is
        # not short-circuited as fresh.
        mgr = _index_manager(tmp_path)
        with (
            patch(f"{_ACT_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.SKIPPED) as funnel,
            patch("subprocess.run") as srun,
        ):
            result = mgr._execute_scip_indexing(
                "/repo", clear=True, repo_alias="my-repo"
            )
        assert funnel.call_args.kwargs["activated_repo"] is True
        assert funnel.call_args.kwargs["clear"] is True
        srun.assert_not_called()
        assert result["success"] is True

    def test_scip_lifecycle_funnel_module_importable(self):
        # Guard: the entrypoints import these symbols lazily at call time.
        assert hasattr(scip_lifecycle, "funnel_scip_generation")
        assert hasattr(scip_lifecycle, "ScipDelegationOutcome")
