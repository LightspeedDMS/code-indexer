"""Per-provider temporal indexing job helpers -- extracted from repos.py
(Story #641; cross-process race investigation deferred from Bug #1580).

Extracted purely to keep repos.py under the 2500-line anti-file-bloat limit
(Messi Rule #6). This module holds the non-orchestrating helpers plus the
entry point (``_provider_temporal_index_job``) and its orchestrator.

Imports from ``.repos`` are FUNCTION-LOCAL (deferred): repos.py imports this
module's functions back for re-export, so a module-level import here would
be circular. By call time both modules are already fully loaded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ._utils import golden_repo_write_lock_guard

#: Mirrors repos.py's own _GLOBAL_SUFFIX (defined locally here -- _utils.py
#: has no such name -- to avoid a needless cross-module dependency for a
#: single string literal).
_GLOBAL_SUFFIX = "-global"


def _resolve_provider_temporal_target(
    repo_path: str, provider_name: str, repo_alias: str
) -> Tuple[Optional[Dict[str, Any]], str, str, bool]:
    """Validate inputs + resolve the actual index path.

    Returns (error_response_or_None, actual_path, repo_alias, is_versioned).
    """
    from .repos import _resolve_provider_job_repo_path

    if not repo_path or not provider_name:
        err = {
            "success": False,
            "error": "Missing repo_path or provider_name",
            "provider": provider_name,
        }
        return err, "", repo_alias, False

    actual_path, repo_alias, is_versioned = _resolve_provider_job_repo_path(
        repo_path, repo_alias
    )
    if is_versioned and not Path(actual_path).exists():
        err = {
            "success": False,
            "error": f"Base clone not found for {repo_path}",
            "provider": provider_name,
        }
        return err, actual_path, repo_alias, is_versioned
    return None, actual_path, repo_alias, is_versioned


def _build_provider_temporal_env_and_cmd(
    provider_name: str, clear: bool, repo_alias: str, kwargs: Dict[str, Any]
) -> Tuple[list, Dict[str, str]]:
    """Build the `cidx index --index-commits` command + subprocess env."""
    from .repos import (
        _build_provider_api_key_env,
        _build_temporal_index_cmd,
        get_config_service,
    )
    from code_indexer.server.storage.postgres.temporal_child_wiring import (
        build_temporal_child_env,
    )

    env = _build_provider_api_key_env(provider_name)
    server_config = get_config_service().get_config()
    merged_env = build_temporal_child_env(server_config, base_env=env)
    if merged_env is not None:
        env = merged_env

    gate_on = bool(
        getattr(server_config.indexing_config, "temporal_all_branches_enabled", False)
    )
    floor_date = getattr(
        server_config.temporal_indexing_config, "index_floor_date", None
    )
    temporal_options = kwargs.get("temporal_options", {}) or {}
    cmd = _build_temporal_index_cmd(
        clear,
        temporal_options,
        all_branches_gate_enabled=gate_on,
        alias=repo_alias,
        global_floor_date=floor_date,
    )
    return cmd, env


def _apply_provider_temporal_index_success_effects(
    *, repo_alias: str, actual_path: str, repo_path: str, is_versioned: bool
) -> None:
    """Post-success side effects: versioned-snapshot publish + enable_temporal."""
    from .repos import _post_provider_index_snapshot, _set_enable_temporal_flag, logger

    if is_versioned and actual_path != repo_path:
        try:
            _post_provider_index_snapshot(
                repo_alias=repo_alias,
                base_clone_path=actual_path,
                old_snapshot_path=repo_path,
            )
        except Exception as exc:
            logger.warning(
                "Post-temporal-index snapshot failed for %s: %s", repo_alias, exc
            )
    _set_enable_temporal_flag(repo_alias)


def _run_and_finish_provider_temporal_index(
    *,
    actual_path: str,
    repo_alias: str,
    provider_name: str,
    clear: bool,
    progress_callback,
    repo_path: str,
    is_versioned: bool,
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the subprocess (under the caller's write lock) and build result."""
    from .repos import (
        _PROVIDER_JOB_OUTPUT_TAIL_CHARS,
        _run_provider_subprocess,
        logger,
    )

    cmd, env = _build_provider_temporal_env_and_cmd(
        provider_name, clear, repo_alias, kwargs
    )
    success, stdout_out, stderr_out = _run_provider_subprocess(
        cmd, actual_path, env, "temporal", ["temporal"], progress_callback
    )

    if success:
        _apply_provider_temporal_index_success_effects(
            repo_alias=repo_alias,
            actual_path=actual_path,
            repo_path=repo_path,
            is_versioned=is_versioned,
        )
    else:
        logger.warning(
            "Temporal provider index failed for provider=%s repo=%s",
            provider_name,
            repo_path,
        )

    return {
        "success": success,
        "provider": provider_name,
        "stdout": stdout_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stdout_out else "",
        "stderr": stderr_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stderr_out else "",
    }


def _provider_temporal_index_job(
    repo_path: str,
    provider_name: str,
    clear: bool = False,
    progress_callback=None,
    **kwargs,
) -> Dict[str, Any]:
    """Background job for per-provider temporal index (Story #641).

    Bug #1580 follow-up: holds the golden repo's write lock for its whole
    duration -- mover.py's TemporalLegacyMigrationScheduler relocates
    legacy temporal shards under that SAME lock.
    """
    repo_alias = kwargs.get("repo_alias", "")
    error, actual_path, repo_alias, is_versioned = _resolve_provider_temporal_target(
        repo_path, provider_name, repo_alias
    )
    if error is not None:
        return error

    bare_alias = (
        repo_alias[: -len(_GLOBAL_SUFFIX)]
        if repo_alias.endswith(_GLOBAL_SUFFIX)
        else repo_alias
    )
    with golden_repo_write_lock_guard(
        bare_alias, owner_name="provider_temporal_index"
    ) as lock_held:
        if not lock_held:
            return {
                "success": False,
                "error": f"Repository '{bare_alias}' is busy. Try again later.",
                "provider": provider_name,
            }
        return _run_and_finish_provider_temporal_index(
            actual_path=actual_path,
            repo_alias=repo_alias,
            provider_name=provider_name,
            clear=clear,
            progress_callback=progress_callback,
            repo_path=repo_path,
            is_versioned=is_versioned,
            kwargs=kwargs,
        )
