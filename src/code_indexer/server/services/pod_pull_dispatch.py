"""The pod-pull dispatch table (PR #1424) run by IndexJobClaimLoop.

Each executor reconstructs a memory-heavy index op from its claimed
background_jobs row's metadata and runs it on THIS (claiming) pod. Every
executor takes the row's metadata, a per-job progress_callback that the
claim loop routes to the shared row (so a work-stolen op reports incremental
progress to the originating node's dashboard instead of a 0 -> 100 jump),
and the claimed row's job id, which the indexing executors record on their
children's embedding_call_stats rows (Story #2118).

Built by ``build_index_dispatch`` (installed by startup/lifespan.py) so the
cluster dispatch path is one real, testable function.

IndexJobClaimLoop also injects the job id into the metadata (issue-66105), so
an executor that spreads the metadata into a call drops that key first
(``_without_job_id``) -- spread alongside ``job_id=`` it would be passed twice.

The internal pod-pull ops ride the same table: the scheduled golden-repo
refresh (any pod claims distinct repos under the memory gate), the FTS
backfill build (issue-66445) and the semantic self-heal re-embed.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.index_job_claim_loop import DispatchTable


def _without_job_id(md: Dict[str, Any]) -> Dict[str, Any]:
    """*md* without the claim loop's injected ``job_id`` (passed explicitly)."""
    return {key: value for key, value in md.items() if key != "job_id"}


def build_index_dispatch(
    golden_repo_manager: Any, refresh_scheduler: Optional[Any] = None
) -> DispatchTable:
    """The executor for every POD_PULL_OPS op, bound to this pod's manager
    and refresh scheduler (None: a claimed refresh fails loudly)."""
    from code_indexer.global_repos.refresh_scheduler import (
        GLOBAL_REPO_REFRESH_OP,
        global_repo_refresh_executor,
    )
    from code_indexer.server.services.fts_backfill.policy import (
        FTS_BACKFILL_BUILD_OP,
        fts_backfill_build_executor,
    )
    from code_indexer.server.services.semantic_backfill.policy import (
        SEMANTIC_BACKFILL_REINDEX_OP,
        semantic_backfill_reindex_executor,
    )
    from code_indexer.server.app_helpers import (
        _execute_repository_sync,
        build_sync_progress_webhook_callback,
    )
    from code_indexer.server.mcp.handlers.repos import (
        _provider_index_job,
        _provider_temporal_index_job,
    )

    def _pp_add_golden_repo(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return golden_repo_manager.execute_add_golden_repo_work(  # type: ignore[no-any-return]
            **_without_job_id(md), progress_callback=progress_callback, job_id=job_id
        )

    def _pp_change_branch(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        # change_branch takes no job id; its re-index records
        # nojob:golden_repo_change_branch (as on the local pool).
        return golden_repo_manager.change_branch(  # type: ignore[no-any-return]
            md["alias"],
            md["target_branch"],
            progress_callback=progress_callback,
        )

    def _pp_sync_repository(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        # Rebuild the client webhook on THIS (executing) node from the
        # metadata options, then fan progress out to BOTH the DB-backed
        # dashboard callback and the webhook.
        options = md.get("options") or {}
        webhook_cb = build_sync_progress_webhook_callback(
            options.get("progress_webhook"),
            md["repo_id"],
            md["username"],
        )

        def _combined(progress: int) -> None:
            progress_callback(progress)
            if webhook_cb is not None:
                webhook_cb(progress)

        return _execute_repository_sync(  # type: ignore[no-any-return]
            repo_id=md["repo_id"],
            username=md["username"],
            options=options,
            activated_repo_manager=getattr(
                golden_repo_manager, "activated_repo_manager", None
            ),
            progress_callback=_combined,
        )

    def _pp_provider_index(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return _provider_index_job(
            **_without_job_id(md), progress_callback=progress_callback, job_id=job_id
        )

    def _pp_provider_temporal(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return _provider_temporal_index_job(
            **_without_job_id(md), progress_callback=progress_callback, job_id=job_id
        )

    def _pp_global_repo_refresh(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        # The metadata carries job_id (claim loop injection), which the
        # executor threads into the refresh's embedding rows.
        if refresh_scheduler is None:
            raise RuntimeError(
                "global_repo_refresh claimed on a pod with no refresh scheduler wired"
            )
        result: Optional[Dict[str, Any]] = global_repo_refresh_executor(
            refresh_scheduler, md, progress_callback
        )
        return result

    def _pp_fts_backfill(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return fts_backfill_build_executor(md, progress_callback)

    def _pp_semantic_backfill(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return semantic_backfill_reindex_executor(
            golden_repo_manager, md, progress_callback
        )

    return {
        GLOBAL_REPO_REFRESH_OP: _pp_global_repo_refresh,
        FTS_BACKFILL_BUILD_OP: _pp_fts_backfill,
        SEMANTIC_BACKFILL_REINDEX_OP: _pp_semantic_backfill,
        "add_golden_repo": _pp_add_golden_repo,
        "change_branch": _pp_change_branch,
        "sync_repository": _pp_sync_repository,
        "provider_index_add": _pp_provider_index,
        "provider_temporal_index_rebuild": _pp_provider_temporal,
    }
