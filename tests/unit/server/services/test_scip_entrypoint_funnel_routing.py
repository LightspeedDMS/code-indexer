"""EVO-65021 stream-05: both golden-repo SCIP entrypoints route through the funnel.

Verifies that under ``scip_backend='delegated'`` neither entrypoint runs the
local ``cidx scip generate`` subprocess, and that a fail-soft skip does not fail
the enclosing job; under the default ``local`` backend the subprocess still runs.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoError,
    GoldenRepoManager,
)
from code_indexer.server.services import scip_lifecycle
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.services.scip_lifecycle import ScipDelegationOutcome

_REFRESH_MOD = "code_indexer.global_repos.refresh_scheduler"
_ACT_MOD = "code_indexer.server.services.activated_repo_index_manager"
_GRM_MOD = "code_indexer.server.repositories.golden_repo_manager"
_CONFIG_MOD = "code_indexer.server.services.config_service"
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


# --------------------------------------------------------------------------- #
# golden_repo_manager.add_indexes_to_golden_repo (third golden-repo entrypoint)
# --------------------------------------------------------------------------- #
def _golden_repo_manager() -> GoldenRepoManager:
    # Bypass the heavy __init__: _run_scip_index only uses its params + module
    # functions, none of the manager's instance state.
    return GoldenRepoManager.__new__(GoldenRepoManager)


class TestGoldenRepoManagerAddIndexEntrypoint:
    def test_delegated_skips_local_subprocess(self):
        mgr = _golden_repo_manager()
        pcb = Mock()
        with (
            patch(f"{_CONFIG_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
            patch(f"{_GRM_MOD}.subprocess.run") as srun,
        ):
            out, err = mgr._run_scip_index("my-repo", "/repo", _allocator(), pcb)
        funnel.assert_called_once()
        assert funnel.call_args.kwargs["alias"] == "my-repo"
        srun.assert_not_called()  # delegated build enqueued, not run inline
        assert (out, err) == ("", "")  # nothing to accumulate for the caller
        # phase_end progress marker emitted carrying the delegated outcome
        assert pcb.call_args.kwargs["phase"] == "scip"
        assert pcb.call_args.kwargs["detail"] == "SCIP: enqueued"

    def test_local_backend_runs_subprocess(self):
        mgr = _golden_repo_manager()
        with (
            patch(f"{_CONFIG_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.NOT_DELEGATED),
            patch(f"{_GRM_MOD}.subprocess.run") as srun,
            patch(f"{_GRM_MOD}.build_cidx_subprocess_env", return_value={}),
        ):
            srun.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            mgr._run_scip_index("my-repo", "/repo", _allocator(), None)
        srun.assert_called_once()
        assert srun.call_args.args[0] == ["cidx", "scip", "generate"]

    def test_delegated_fail_soft_does_not_raise(self):
        mgr = _golden_repo_manager()
        with (
            patch(f"{_CONFIG_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.SKIPPED),
            patch(f"{_GRM_MOD}.subprocess.run") as srun,
        ):
            # Must not raise — the enclosing add-index job is not failed by a
            # SCIP-backend gap.
            mgr._run_scip_index("my-repo", "/repo", _allocator(), None)
        srun.assert_not_called()

    def test_global_suffix_normalized_to_bare_alias(self):
        # CRITICAL-1 regression (mirrors refresh_scheduler): a ``-global`` alias
        # would resolve to a non-existent sandbox path, so it must be normalized
        # to bare before funnelling.
        mgr = _golden_repo_manager()
        with (
            patch(f"{_CONFIG_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
            patch(f"{_GRM_MOD}.subprocess.run"),
        ):
            mgr._run_scip_index("my-repo-global", "/repo", _allocator(), None)
        assert funnel.call_args.kwargs["alias"] == "my-repo"

    def test_local_backend_failure_raises_golden_repo_error(self):
        # Local-backend failure path: NOT_DELEGATED + a non-zero cidx exit must
        # fail-loud with GoldenRepoError. The prior local test stubbed
        # returncode=0, so a deleted ``if returncode != 0: raise`` went unnoticed
        # — this pins the raise.
        mgr = _golden_repo_manager()
        with (
            patch(f"{_CONFIG_MOD}.get_config_service"),
            patch(_FUNNEL, return_value=ScipDelegationOutcome.NOT_DELEGATED),
            patch(f"{_GRM_MOD}.subprocess.run") as srun,
            patch(f"{_GRM_MOD}.build_cidx_subprocess_env", return_value={}),
        ):
            srun.return_value = SimpleNamespace(
                returncode=1, stdout="", stderr="scip toolchain exploded"
            )
            with pytest.raises(GoldenRepoError) as exc_info:
                mgr._run_scip_index("my-repo", "/repo", _allocator(), None)
        srun.assert_called_once()
        assert "Failed to create SCIP index" in str(exc_info.value)
        assert "scip toolchain exploded" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# add_indexes_to_golden_repo dispatches the ``scip`` branch to _run_scip_index
# --------------------------------------------------------------------------- #
def _registered_manager(tmp_path) -> GoldenRepoManager:
    """A GoldenRepoManager with one repo persisted (in-memory + SQLite backend).

    Mirrors tests/unit/golden_repos/test_add_index_commands.py::registered_manager
    — the background worker resolves the repo AUTHORITATIVELY via the SQLite
    backend (Bug #1316), so the repo must exist there, not only in the cache.
    No ``_refresh_scheduler`` is attached, so the write-lock and CoW/alias-swap
    blocks (both guarded by ``scheduler is not None``) are skipped — the lightest
    seam that still exercises the ``elif index_type == "scip":`` dispatch.
    """
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    repo_path = tmp_path / "repos" / "test-repo"
    repo_path.mkdir(parents=True)
    created_at = datetime.now(timezone.utc).isoformat()
    mgr.golden_repos["test-repo"] = GoldenRepo(
        alias="test-repo",
        repo_url="git@github.com:org/test-repo.git",
        clone_path=str(repo_path),
        default_branch="main",
        created_at=created_at,
    )
    mgr._sqlite_backend.add_repo(
        alias="test-repo",
        repo_url="git@github.com:org/test-repo.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at=created_at,
        enable_temporal=False,
        temporal_options=None,
    )
    mgr.background_job_manager = MagicMock()
    return mgr, str(repo_path)


class TestAddIndexesDispatchesScipBranch:
    def test_scip_branch_calls_run_scip_index(self, tmp_path):
        # WIRING: the ``scip`` index_type branch in add_indexes_to_golden_repo
        # must DISPATCH to _run_scip_index (the delegated funnel entrypoint). If
        # the call-site is reverted to an inline ``cidx scip generate``
        # subprocess, _run_scip_index is never called and this reddens.
        mgr, repo_path = _registered_manager(tmp_path)

        def fake_submit_job(operation_type, func, **kwargs):
            # cidx init runs a real subprocess.run — stub it to a clean exit.
            def recording_run(cmd, **kw):
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            with (
                patch("subprocess.run", side_effect=recording_run),
                patch.object(mgr, "get_actual_repo_path", return_value=repo_path),
                patch.object(mgr, "_run_scip_index", return_value=("", "")) as spy,
            ):
                func()
                fake_submit_job.spy = spy
            return "fake-job-id"

        with patch.object(
            mgr.background_job_manager, "submit_job", side_effect=fake_submit_job
        ):
            mgr.add_indexes_to_golden_repo(
                "test-repo", ["scip"], submitter_username="admin"
            )

        spy = fake_submit_job.spy
        spy.assert_called_once()
        args = spy.call_args.args
        # (alias, repo_path, allocator, progress_callback)
        assert args[0] == "test-repo"
        assert args[1] == repo_path
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        assert isinstance(args[2], ProgressPhaseAllocator)
        assert len(args) == 4  # progress_callback threaded through as the 4th arg
