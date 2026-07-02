"""Delegation handler functions for CIDX MCP server.

Covers: list/execute delegation functions, poll delegation job,
open delegation (single/collaborative/competitive modes),
and Claude Server proxy tools (cs_register_repository,
cs_list_repositories, cs_check_health).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id

from . import _utils
from ._utils import _mcp_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid engine and mode values per Claude Server API contract
_VALID_DELEGATION_ENGINES = {"claude-code", "codex", "gemini", "opencode", "q"}
_VALID_DELEGATION_MODES = {"single", "collaborative", "competitive"}
_DEFAULT_DELEGATION_ENGINE = "claude-code"
_DEFAULT_DELEGATION_MODE = "single"

# Repo readiness poll interval (seconds)
_REPO_READY_POLL_INTERVAL = 2.0

# Languages recognised for approved-package lists (Story #457)
_GUARDRAILS_SUPPORTED_LANGUAGES = {
    "python",
    "nodejs",
    "java",
    "go",
    "ruby",
    "rust",
    "dotnet",
    "system",
}

_VALID_DISTRIBUTION_STRATEGIES = {"round-robin", "decomposer-decides"}
_DEFAULT_APPROACH_COUNT = 3


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_delegation_function_repo_path() -> Optional[Path]:
    """
    Get the path to the delegation function repository.

    Returns:
        Path to the function repository, or None if not configured
    """
    from ...services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        delegation_config = delegation_manager.load_config()

        if delegation_config is None or not delegation_config.is_configured:
            return None

        # Get the function repo alias from config
        function_repo_alias = delegation_config.function_repo_alias
        if not function_repo_alias:
            return None

        # Get the actual path from golden repo manager
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            logger.warning(
                format_error_log("MCP-GENERAL-119", "Golden repo manager not available")
            )
            return None

        # Try to get the repo path
        try:
            repo_path = golden_repo_manager.get_actual_repo_path(function_repo_alias)
            return Path(repo_path) if repo_path else None
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-120",
                    f"Function repository '{function_repo_alias}' not found: {e}",
                )
            )
            return None

    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-121", f"Error getting delegation function repo path: {e}"
            )
        )
        return None


def _get_user_groups(user: User) -> set:
    """
    Get the groups the user belongs to.

    Args:
        user: The user to get groups for

    Returns:
        Set of group names the user belongs to
    """
    try:
        group_manager = getattr(_utils.app_module.app.state, "group_manager", None)
        if not group_manager:
            logger.warning(
                format_error_log("MCP-GENERAL-122", "Group manager not available")
            )
            return set()

        group = group_manager.get_user_group(user.username)
        if group:
            return {group.name}
        return set()

    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-123", f"Error getting user groups for {user.username}: {e}"
            )
        )
        return set()


def _get_delegation_config():
    """
    Get the Claude Delegation configuration.

    Returns:
        ClaudeDelegationConfig if configured, None otherwise
    """
    from ...services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        return delegation_manager.load_config()
    except Exception as e:
        logger.warning(
            format_error_log("MCP-GENERAL-124", f"Error getting delegation config: {e}")
        )
        return None


def _validate_function_parameters(
    target_function, parameters: Dict[str, Any]
) -> Optional[str]:
    """
    Validate required parameters are present.

    Returns:
        Error message if validation fails, None if valid
    """
    for param in target_function.parameters:
        if param.get("required", False):
            param_name = param.get("name", "")
            if param_name and param_name not in parameters:
                return f"Missing required parameter: {param_name}"
    return None


async def _ensure_repos_registered(
    client, required_repos: List[Dict[str, Any]]
) -> List[str]:
    """
    Ensure required repositories are registered in Claude Server.

    Returns:
        List of repository aliases
    """
    repo_aliases = []
    for repo_def in required_repos:
        # Support both string (alias only) and dict (full repo definition)
        if isinstance(repo_def, str):
            alias = repo_def
            remote = ""
            branch = "main"
        else:
            alias = repo_def.get("alias", "")
            remote = repo_def.get("remote", "")
            branch = repo_def.get("branch", "main")
        if not alias:
            continue
        repo_aliases.append(alias)
        exists = await client.check_repository_exists(alias)
        if not exists and remote:
            # Only register if we have remote URL and repo doesn't exist
            await client.register_repository(alias, remote, branch)
    return repo_aliases


def _get_cidx_callback_base_url() -> Optional[str]:
    """
    Get the base URL for CIDX callback endpoints from delegation config.

    Story #720: Callback-Based Delegation Job Completion

    Returns:
        The CIDX callback URL from delegation config, or None if not configured
    """
    from ...services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        delegation_config = delegation_manager.load_config()

        if delegation_config and delegation_config.cidx_callback_url:
            return delegation_config.cidx_callback_url
        return None
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-125",
                f"Failed to get CIDX callback URL from delegation config: {e}",
            )
        )
        return None


def _load_packages_context(repo_path: str) -> str:
    """
    Load approved package lists from <repo_path>/packages/<language>/approved.txt.

    Only languages listed in _GUARDRAILS_SUPPORTED_LANGUAGES are included.
    Non-standard language directories are silently ignored.

    Returns:
        Formatted string of approved packages per language, or a
        'No pre-approved packages' message when none are found.
    """
    packages_dir = Path(repo_path) / "packages"
    if not packages_dir.is_dir():
        return "No pre-approved packages configured for this workspace."

    sections: List[str] = []
    for lang_dir in sorted(packages_dir.iterdir()):
        if not lang_dir.is_dir():
            continue
        if lang_dir.name not in _GUARDRAILS_SUPPORTED_LANGUAGES:
            continue
        approved_file = lang_dir / "approved.txt"
        if not approved_file.is_file():
            continue
        packages = [
            line.strip()
            for line in approved_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if packages:
            sections.append(
                f"   Approved {lang_dir.name} packages: {', '.join(packages)}"
            )

    if not sections:
        return "No pre-approved packages configured for this workspace."

    return "\n".join(sections)


def _resolve_guardrails(
    config: Any,
    golden_repo_manager: Any,
) -> tuple:
    """
    Resolve the guardrails system-prompt for an open delegation job.

    Story #457: Safety guardrails prepended to delegated prompts.

    Resolution order:
      1. guardrails_enabled=False  -> return ("", None)  — disabled
      2. delegation_guardrails_repo is set AND get_actual_repo_path succeeds
         AND guardrails/system-prompt.md exists  -> return (resolved_text, alias)
      3. Anything else (no repo, repo missing, file missing) ->
         return (DEFAULT_GUARDRAILS_TEMPLATE resolved with packages_context, None)
         and log a warning when a repo was configured but the file was not found.

    Args:
        config: ClaudeDelegationConfig instance.
        golden_repo_manager: GoldenRepoManager (or None) from _utils.app_module.

    Returns:
        Tuple of (guardrails_text, guardrails_repo_alias).
        guardrails_text is "" when disabled.
        guardrails_repo_alias is the golden repo alias when loaded from repo, else None.
    """
    from code_indexer.server.config.delegation_config import DEFAULT_GUARDRAILS_TEMPLATE

    if not getattr(config, "guardrails_enabled", True):
        return ("", None)

    repo_alias = getattr(config, "delegation_guardrails_repo", "")

    if repo_alias and golden_repo_manager is not None:
        try:
            repo_path = golden_repo_manager.get_actual_repo_path(repo_alias)
            prompt_file = Path(repo_path) / "guardrails" / "system-prompt.md"
            if prompt_file.is_file():
                template = prompt_file.read_text(encoding="utf-8")
                packages_context = _load_packages_context(repo_path)
                resolved = template.replace("{packages_context}", packages_context)
                return (resolved, repo_alias)
            else:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-132",
                        f"Guardrails repo '{repo_alias}' configured but "
                        f"guardrails/system-prompt.md not found at {repo_path}; "
                        "falling back to default guardrails template.",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-133",
                    f"Failed to load guardrails from repo '{repo_alias}': {e}; "
                    "falling back to default guardrails template.",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    # Default: use built-in template.
    # Best-effort: load packages from the repo if a path was already resolved
    # (operator has packages/ but forgot system-prompt.md).
    packages_context = "No pre-approved packages configured for this workspace."
    resolved_repo_alias = None
    if repo_alias and golden_repo_manager is not None:
        try:
            repo_path = golden_repo_manager.get_actual_repo_path(repo_alias)
            packages_context = _load_packages_context(repo_path)
            resolved_repo_alias = repo_alias
        except Exception:
            pass  # Best-effort; keep default packages_context
    resolved_default = DEFAULT_GUARDRAILS_TEMPLATE.replace(
        "{packages_context}", packages_context
    )
    return (resolved_default, resolved_repo_alias)


def _get_repo_ready_timeout() -> float:
    """
    Get the timeout for repository readiness polling from server config.

    Returns the golden repo registration timeout from server config if available,
    otherwise falls back to 300s (5 minutes).

    Returns:
        Timeout in seconds as float
    """
    try:
        from ...services.config_service import get_config_service

        config_service = get_config_service()
        server_config = config_service.get_server_config()  # type: ignore[attr-defined]
        if hasattr(server_config, "golden_repo_registration_timeout"):
            return float(server_config.golden_repo_registration_timeout)
    except Exception as e:
        logger.debug(
            f"Could not retrieve repo ready timeout from config, using default 300s: {e}"
        )
    return 300.0


def _validate_collaborative_params(
    args: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Validate collaborative mode parameters (DAG-based steps).

    Story #462: Collaborative delegation mode validation.

    Returns:
        Error MCP response dict if validation fails, None if valid.
    """
    steps = args.get("steps")
    if not steps or not isinstance(steps, list):
        return _mcp_response(
            {
                "success": False,
                "error": "Collaborative mode requires non-empty 'steps' list",
            }
        )

    if len(steps) > 10:
        return _mcp_response(
            {"success": False, "error": "Collaborative mode supports at most 10 steps"}
        )

    step_ids: List[str] = []
    for i, step in enumerate(steps):
        step_id = step.get("step_id")
        if not step_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Step {i}: missing required field 'step_id'",
                }
            )
        engine = step.get("engine")
        if not engine:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Step '{step_id}': missing required field 'engine'",
                }
            )
        if engine not in _VALID_DELEGATION_ENGINES:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Step '{step_id}': invalid engine '{engine}'. "
                        f"Supported: {', '.join(sorted(_VALID_DELEGATION_ENGINES))}"
                    ),
                }
            )
        if not step.get("prompt"):
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Step '{step_id}': missing required field 'prompt'",
                }
            )
        if step_id in step_ids:
            return _mcp_response(
                {"success": False, "error": f"Duplicate step_id '{step_id}'"}
            )
        step_ids.append(step_id)

    step_id_set = set(step_ids)
    depended_on: set = set()
    for step in steps:
        deps = step.get("depends_on", [])
        for dep in deps:
            if dep == step["step_id"]:
                return _mcp_response(
                    {
                        "success": False,
                        "error": f"Step '{step['step_id']}' depends on itself",
                    }
                )
            if dep not in step_id_set:
                return _mcp_response(
                    {
                        "success": False,
                        "error": f"Step '{step['step_id']}' depends on '{dep}' which does not exist",
                    }
                )
            depended_on.add(dep)

    terminal_steps = [sid for sid in step_ids if sid not in depended_on]
    if len(terminal_steps) != 1:
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Collaborative DAG must have exactly 1 terminal step, "
                    f"found {len(terminal_steps)}: {', '.join(terminal_steps)}"
                ),
            }
        )

    # Cycle detection via DFS
    step_deps = {s["step_id"]: s.get("depends_on", []) for s in steps}
    visited: set = set()
    in_stack: set = set()

    def _has_cycle(node_id: str) -> bool:
        visited.add(node_id)
        in_stack.add(node_id)
        for dep in step_deps.get(node_id, []):
            if dep in in_stack:
                return True
            if dep not in visited and _has_cycle(dep):
                return True
        in_stack.discard(node_id)
        return False

    for sid in step_ids:
        if sid not in visited:
            if _has_cycle(sid):
                return _mcp_response(
                    {"success": False, "error": "collaborative DAG contains a cycle"}
                )

    return None


def _validate_competitive_params(
    args: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Validate competitive mode parameters.

    Story #462: Competitive delegation mode validation.

    Returns:
        Error MCP response dict if validation fails, None if valid.
    """
    engines = args.get("engines")
    if not engines or not isinstance(engines, list) or len(engines) == 0:
        return _mcp_response(
            {
                "success": False,
                "error": "Competitive mode requires non-empty 'engines' list",
            }
        )

    for eng in engines:
        if eng not in _VALID_DELEGATION_ENGINES:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Invalid engine '{eng}' in engines list. "
                        f"Supported: {', '.join(sorted(_VALID_DELEGATION_ENGINES))}"
                    ),
                }
            )

    dist_strategy = args.get("distribution_strategy")
    if (
        dist_strategy is not None
        and dist_strategy not in _VALID_DISTRIBUTION_STRATEGIES
    ):
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Invalid distribution_strategy '{dist_strategy}'. "
                    f"Supported: {', '.join(sorted(_VALID_DISTRIBUTION_STRATEGIES))}"
                ),
            }
        )

    approach_count = args.get("approach_count")
    effective_approach_count = (
        approach_count if approach_count is not None else _DEFAULT_APPROACH_COUNT
    )
    if approach_count is not None and (approach_count < 2 or approach_count > 10):
        return _mcp_response(
            {"success": False, "error": "approach_count must be between 2 and 10"}
        )

    min_threshold = args.get("min_success_threshold")
    if min_threshold is not None:
        if min_threshold < 1 or min_threshold > effective_approach_count:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"min_success_threshold must be between 1 and "
                        f"{effective_approach_count} (approach_count)"
                    ),
                }
            )

    timeout_secs = args.get("approach_timeout_seconds")
    if timeout_secs is not None and timeout_secs < 1:
        return _mcp_response(
            {"success": False, "error": "approach_timeout_seconds must be >= 1"}
        )

    decomposer = args.get("decomposer")
    if decomposer is not None:
        dec_engine = decomposer.get("engine", "")
        if dec_engine not in _VALID_DELEGATION_ENGINES:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Invalid decomposer engine '{dec_engine}'. "
                        f"Supported: {', '.join(sorted(_VALID_DELEGATION_ENGINES))}"
                    ),
                }
            )

    judge = args.get("judge")
    if judge is not None:
        judge_engine = judge.get("engine", "")
        if judge_engine not in _VALID_DELEGATION_ENGINES:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Invalid judge engine '{judge_engine}'. "
                        f"Supported: {', '.join(sorted(_VALID_DELEGATION_ENGINES))}"
                    ),
                }
            )

    return None


def _validate_open_delegation_params(
    args: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Validate parameters for execute_open_delegation.

    Returns:
        Error MCP response dict if validation fails, None if all params are valid.
    """
    # Check mode first — different modes have different required fields
    mode = args.get("mode", _DEFAULT_DELEGATION_MODE)
    if mode not in _VALID_DELEGATION_MODES:
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Invalid mode '{mode}'. "
                    f"Supported: {', '.join(sorted(_VALID_DELEGATION_MODES))}"
                ),
            }
        )

    # Mode-specific validation — collaborative has per-step fields, not top-level
    if mode == "collaborative":
        return _validate_collaborative_params(args)

    # Top-level prompt/repositories/engine required for single and competitive modes
    prompt = args.get("prompt", "")
    if not prompt:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: prompt"}
        )

    repositories = args.get("repositories")
    if not repositories:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: repositories (must be a non-empty list)",
            }
        )

    engine = args.get("engine", _DEFAULT_DELEGATION_ENGINE)
    if engine not in _VALID_DELEGATION_ENGINES:
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Invalid engine '{engine}'. "
                    f"Supported: {', '.join(sorted(_VALID_DELEGATION_ENGINES))}"
                ),
            }
        )
    elif mode == "competitive":
        return _validate_competitive_params(args)

    return None  # All params valid (single mode)


async def _register_open_delegation_callback(client: Any, job_id: str) -> None:
    """
    Register callback URL with Claude Server for open delegation job completion.

    Best-effort: logs warning on failure but does not raise.
    """
    callback_base_url = _get_cidx_callback_base_url()
    if not callback_base_url:
        return

    callback_url = f"{callback_base_url.rstrip('/')}/api/delegation/callback/{job_id}"
    try:
        await client.register_callback(job_id, callback_url)
        logger.debug(f"Registered callback for open delegation job {job_id}")
    except Exception as callback_err:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-130",
                f"Failed to register callback for job {job_id}: {callback_err}",
                extra={"correlation_id": get_correlation_id()},
            )
        )


async def _submit_open_delegation_job(
    client: Any,
    prompt: str,
    repositories: List[str],
    engine: str,
    model: Optional[str],
    job_timeout: Optional[int],
    repo_ready_timeout: float,
) -> Dict[str, Any]:
    """
    Check repository readiness, create, register callback, and start the delegation job.

    Returns:
        MCP response dict (success with job_id, or error)
    """
    golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
    for alias in repositories:
        git_url: Optional[str] = None
        branch = "main"
        if golden_repo_manager:
            try:
                golden_repo = golden_repo_manager.get_golden_repo(alias)
                if golden_repo:
                    git_url = golden_repo.repo_url or None
                    branch = golden_repo.default_branch or "main"
            except Exception:
                pass  # Best-effort: proceed without git_url if lookup fails
        ready = await client.wait_for_repo_ready(
            alias=alias,
            timeout=repo_ready_timeout,
            git_url=git_url,
            branch=branch,
            poll_interval=_REPO_READY_POLL_INTERVAL,
        )
        if not ready:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Repository '{alias}' failed to become ready "
                        f"within timeout ({int(repo_ready_timeout)}s)"
                    ),
                }
            )

    job_result = await client.create_job_with_options(
        prompt=prompt,
        repositories=repositories,
        engine=engine,
        model=model,
        timeout=job_timeout,
    )
    job_id = job_result.get("jobId") or job_result.get("job_id")
    if not job_id:
        return _mcp_response(
            {"success": False, "error": "Job created but no job_id returned"}
        )

    await _register_open_delegation_callback(client, job_id)

    from ...services.delegation_job_tracker import DelegationJobTracker

    tracker = DelegationJobTracker.get_instance()
    await tracker.register_job(job_id)
    await client.start_job(job_id)

    return _mcp_response({"success": True, "job_id": job_id})


async def _submit_collaborative_delegation_job(
    client: Any,
    steps: List[Dict[str, Any]],
    guardrails_text: str,
    guardrails_repo: Optional[str],
    repo_ready_timeout: float,
) -> Dict[str, Any]:
    """
    Check repo readiness, apply guardrails, create orchestrated job, start it.

    Story #462: Collaborative delegation mode.

    Returns:
        MCP response dict (success with job_id, or error).
    """
    # Collect unique repos from all steps
    all_repos: List[str] = []
    for step in steps:
        if step.get("repository") and step["repository"] not in all_repos:
            all_repos.append(step["repository"])
        for r in step.get("repositories", []):
            if r not in all_repos:
                all_repos.append(r)

    golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
    for alias in all_repos:
        git_url: Optional[str] = None
        branch = "main"
        if golden_repo_manager:
            try:
                golden_repo = golden_repo_manager.get_golden_repo(alias)
                if golden_repo:
                    git_url = golden_repo.repo_url or None
                    branch = golden_repo.default_branch or "main"
            except Exception as e:
                logger.debug(f"Could not retrieve golden repo info for '{alias}': {e}")
        ready = await client.wait_for_repo_ready(
            alias=alias,
            timeout=repo_ready_timeout,
            git_url=git_url,
            branch=branch,
            poll_interval=_REPO_READY_POLL_INTERVAL,
        )
        if not ready:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Repository '{alias}' failed to become ready "
                        f"within timeout ({int(repo_ready_timeout)}s)"
                    ),
                }
            )

    # Apply guardrails to each step prompt
    prepared_steps = []
    for step in steps:
        prepared = dict(step)
        if guardrails_text:
            prepared["prompt"] = guardrails_text + step["prompt"]
        prepared_steps.append(prepared)

    # Append guardrails repo to each step's repositories list
    if guardrails_repo:
        for step in prepared_steps:
            repos = step.get("repositories", [])
            if guardrails_repo not in repos:
                repos.append(guardrails_repo)
                step["repositories"] = repos

    job_result = await client.create_orchestrated_job(prepared_steps)
    job_id = job_result.get("jobId") or job_result.get("job_id")
    if not job_id:
        return _mcp_response(
            {
                "success": False,
                "error": "Orchestrated job created but no job_id returned",
            }
        )

    await _register_open_delegation_callback(client, job_id)

    from ...services.delegation_job_tracker import DelegationJobTracker

    tracker = DelegationJobTracker.get_instance()
    await tracker.register_job(job_id)
    await client.start_job(job_id)

    return _mcp_response({"success": True, "job_id": job_id})


async def _submit_competitive_delegation_job(
    client: Any,
    prompt: str,
    repositories: List[str],
    args: Dict[str, Any],
    repo_ready_timeout: float,
) -> Dict[str, Any]:
    """
    Check repo readiness, create competitive job, start it.

    Story #462: Competitive delegation mode.
    Caller applies guardrails to prompt before passing it here.

    Returns:
        MCP response dict (success with job_id, or error).
    """
    golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
    for alias in repositories:
        git_url: Optional[str] = None
        branch = "main"
        if golden_repo_manager:
            try:
                golden_repo = golden_repo_manager.get_golden_repo(alias)
                if golden_repo:
                    git_url = golden_repo.repo_url or None
                    branch = golden_repo.default_branch or "main"
            except Exception as e:
                logger.debug(f"Could not retrieve golden repo info for '{alias}': {e}")
        ready = await client.wait_for_repo_ready(
            alias=alias,
            timeout=repo_ready_timeout,
            git_url=git_url,
            branch=branch,
            poll_interval=_REPO_READY_POLL_INTERVAL,
        )
        if not ready:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Repository '{alias}' failed to become ready "
                        f"within timeout ({int(repo_ready_timeout)}s)"
                    ),
                }
            )

    job_result = await client.create_competitive_job(
        prompt=prompt,
        repositories=repositories,
        engines=args.get("engines", []),
        distribution_strategy=args.get("distribution_strategy"),
        min_success_threshold=args.get("min_success_threshold"),
        approach_count=args.get("approach_count"),
        approach_timeout_seconds=args.get("approach_timeout_seconds"),
        decomposer=args.get("decomposer"),
        judge=args.get("judge"),
        options=args.get("options"),
    )
    job_id = job_result.get("jobId") or job_result.get("job_id")
    if not job_id:
        return _mcp_response(
            {
                "success": False,
                "error": "Competitive job created but no job_id returned",
            }
        )

    await _register_open_delegation_callback(client, job_id)

    from ...services.delegation_job_tracker import DelegationJobTracker

    tracker = DelegationJobTracker.get_instance()
    await tracker.register_job(job_id)
    await client.start_job(job_id)

    return _mcp_response({"success": True, "job_id": job_id})


def _lookup_golden_repo_for_cs(alias: str) -> tuple:
    """
    Look up git URL and branch for a CIDX golden repo alias.

    Returns (git_url, branch, error_message).
    error_message is None on success; non-None string on failure.
    """
    golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
    if not golden_repo_manager:
        return None, "main", f"Alias '{alias}' not found in CIDX golden repos"
    try:
        golden_repo = golden_repo_manager.get_golden_repo(alias)
        if golden_repo is None:
            return None, "main", f"Alias '{alias}' not found in CIDX golden repos"
        git_url = golden_repo.repo_url or None
        branch = golden_repo.default_branch or "main"
        return git_url, branch, None
    except Exception as e:
        return None, "main", f"Failed to look up golden repo '{alias}': {e}"


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def handle_list_delegation_functions(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    List available delegation functions for the current user.

    Functions are filtered based on the effective user's group memberships.
    When impersonation is active, the impersonated user's groups are used.

    Args:
        args: Tool arguments (currently unused)
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for accessing effective user

    Returns:
        MCP response with list of accessible functions
    """
    from ...services.delegation_function_loader import DelegationFunctionLoader

    try:
        # Get the function repository path
        repo_path = _get_delegation_function_repo_path()
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        # Determine effective user for group lookup (CRITICAL-1 fix)
        # When impersonating, use the impersonated user's groups
        effective_user = user
        if session_state and session_state.is_impersonating:
            effective_user = session_state.effective_user

        # Get effective user's groups
        user_groups = _get_user_groups(effective_user)

        # Load and filter functions
        loader = DelegationFunctionLoader()
        all_functions = loader.load_functions(repo_path)
        accessible_functions = loader.filter_by_groups(all_functions, user_groups)

        # Format response
        functions_data = [
            {
                "name": func.name,
                "description": func.description,
                "parameters": func.parameters,
            }
            for func in accessible_functions
        ]

        return _mcp_response({"success": True, "functions": functions_data})

    except Exception as e:
        logger.exception(
            f"Error in list_delegation_functions: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_execute_delegation_function(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Execute a delegation function by delegating to Claude Server.

    Args:
        args: Tool arguments with function_name, parameters, prompt
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for impersonation

    Returns:
        MCP response with job_id on success or error details
    """
    from ...services.delegation_function_loader import DelegationFunctionLoader
    from ...services.prompt_template_processor import PromptTemplateProcessor
    from ...clients.claude_server_client import ClaudeServerClient, ClaudeServerError

    try:
        # Configuration validation
        repo_path = _get_delegation_function_repo_path()
        delegation_config = _get_delegation_config()

        if (
            repo_path is None
            or delegation_config is None
            or not delegation_config.is_configured
        ):
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        function_name = args.get("function_name", "")
        parameters = args.get("parameters", {})
        user_prompt = args.get("prompt", "")

        # Load and find function
        loader = DelegationFunctionLoader()
        all_functions = loader.load_functions(repo_path)
        target_function = next(
            (f for f in all_functions if f.name == function_name), None
        )

        if target_function is None:
            return _mcp_response(
                {"success": False, "error": f"Function not found: {function_name}"}
            )

        # Access validation
        effective_user = (
            session_state.effective_user
            if session_state and session_state.is_impersonating
            else user
        )
        user_groups = _get_user_groups(effective_user)

        if not (user_groups & set(target_function.allowed_groups)):
            return _mcp_response(
                {"success": False, "error": "Access denied: insufficient permissions"}
            )

        # Parameter validation
        param_error = _validate_function_parameters(target_function, parameters)
        if param_error:
            return _mcp_response({"success": False, "error": param_error})

        # Create client and ensure repos registered
        # Story #732: Use async context manager for proper connection cleanup
        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            repo_aliases = await _ensure_repos_registered(
                client, target_function.required_repos
            )

            # Render prompt and create job
            processor = PromptTemplateProcessor()
            impersonation_user = (
                target_function.impersonation_user or effective_user.username
            )
            rendered_prompt = processor.render(
                template=target_function.prompt_template,
                parameters=parameters,
                user_prompt=user_prompt,
                impersonation_user=impersonation_user,
            )

            job_result = await client.create_job(
                prompt=rendered_prompt, repositories=repo_aliases
            )
            # Claude Server returns camelCase "jobId"
            job_id = job_result.get("jobId") or job_result.get("job_id")
            if not job_id:
                return _mcp_response(
                    {"success": False, "error": "Job created but no job_id returned"}
                )

            # Story #720: Register callback URL with Claude Server for completion notification
            callback_base_url = _get_cidx_callback_base_url()
            if callback_base_url:
                callback_url = (
                    f"{callback_base_url.rstrip('/')}/api/delegation/callback/{job_id}"
                )
                try:
                    await client.register_callback(job_id, callback_url)
                    logger.debug(
                        f"Registered callback URL for job {job_id}: {callback_url}"
                    )
                except Exception as callback_err:
                    # Log but don't fail - callback registration is best-effort
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-126",
                            f"Failed to register callback for job {job_id}: {callback_err}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            # Story #720: Register job in tracker for callback-based completion
            from ...services.delegation_job_tracker import DelegationJobTracker

            tracker = DelegationJobTracker.get_instance()
            await tracker.register_job(job_id)

            await client.start_job(job_id)

            return _mcp_response({"success": True, "job_id": job_id})

    except ClaudeServerError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-127",
                f"Claude Server error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": f"Claude Server error: {e}"})
    except Exception as e:
        logger.exception(
            f"Error in execute_delegation_function: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_poll_delegation_job(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Non-blocking poll for delegation job result.

    Story #720: Callback-Based Delegation Job Completion
    Story #50: This handler remains async (justified exception) because
    DelegationJobTracker uses asyncio.Future for callback-based completion.

    Returns immediately with the result if available, or status='waiting' if not.
    The job stays in the tracker so the client can poll again without losing state.

    timeout_seconds is silently ignored (kept for backward compatibility).

    Args:
        args: Tool arguments with job_id (timeout_seconds silently ignored)
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for impersonation

    Returns:
        MCP response with result if available, or waiting status to retry
    """
    from ...services.delegation_job_tracker import DelegationJobTracker

    job_id = ""
    try:
        # Configuration validation
        delegation_config = _get_delegation_config()

        if delegation_config is None or not delegation_config.is_configured:
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        job_id = args.get("job_id", "")
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # timeout_seconds silently ignored — polling is now non-blocking
        tracker = DelegationJobTracker.get_instance()

        # Check if job exists (in tracker or cache)
        job_exists = await tracker.has_job(job_id)
        if not job_exists:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Job {job_id} not found or expired",
                }
            )

        # Non-blocking: check if result is ready
        result = await tracker.get_result(job_id)

        if result is None:
            # Not ready yet — tell client to try again
            return _mcp_response(
                {
                    "status": "waiting",
                    "message": "Job still running, try again",
                    "continue_polling": True,
                }
            )

        # Result available — return based on status from callback
        if result.status == "completed":
            response_dict: Dict[str, Any] = {
                "status": "completed",
                "result": result.output,
                "continue_polling": False,
            }
            # Apply PayloadCache truncation for large results
            result_text = result.output
            if result_text:
                payload_cache = getattr(
                    _utils.app_module.app.state, "payload_cache", None
                )
                if payload_cache is not None:
                    try:
                        truncated = payload_cache.truncate_result(result_text)
                        if truncated.get("has_more", False):
                            response_dict["preview"] = truncated["preview"]
                            response_dict["cache_handle"] = truncated["cache_handle"]
                            response_dict["has_more"] = True
                            response_dict["total_size"] = truncated["total_size"]
                            del response_dict["result"]
                        else:
                            response_dict["has_more"] = False
                            response_dict["cache_handle"] = None
                    except Exception:
                        response_dict["has_more"] = False
                        response_dict["cache_handle"] = None
            return _mcp_response(response_dict)
        else:
            # Failed or other status
            return _mcp_response(
                {
                    "status": "failed",
                    "error": result.error or result.output,
                    "continue_polling": False,
                }
            )

    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-128",
                f"Error polling delegation job {job_id}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Error polling job: {str(e)}"}
        )


async def handle_execute_open_delegation(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Execute open-ended delegation: submit any free-form prompt to Claude Server.

    Story #456: Open-ended delegation with engine and mode selection

    Args:
        args: Tool arguments with prompt, repositories, engine, mode, model, timeout
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for impersonation

    Returns:
        MCP response with job_id on success or error details
    """
    from ...clients.claude_server_client import ClaudeServerClient, ClaudeServerError

    try:
        delegation_config = _get_delegation_config()
        if delegation_config is None or not delegation_config.is_configured:
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        effective_user = (
            session_state.effective_user
            if session_state and session_state.is_impersonating
            else user
        )
        if not effective_user.has_permission("delegate_open"):
            return _mcp_response(
                {
                    "success": False,
                    "error": "Access denied: open delegation requires power_user or admin role",
                }
            )

        validation_error = _validate_open_delegation_params(args)
        if validation_error is not None:
            return validation_error

        # Story #457: resolve safety guardrails and prepend to user prompt
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        guardrails_text, guardrails_repo_alias = _resolve_guardrails(
            delegation_config, golden_repo_manager
        )

        user_prompt = args.get("prompt", "")
        if guardrails_text:
            effective_prompt = guardrails_text + user_prompt
        else:
            effective_prompt = user_prompt

        repositories = list(args.get("repositories", []))
        if guardrails_repo_alias and guardrails_repo_alias not in repositories:
            repositories.append(guardrails_repo_alias)

        engine = str(
            args.get("engine")
            or getattr(
                delegation_config,
                "delegation_default_engine",
                _DEFAULT_DELEGATION_ENGINE,
            )
        )
        mode = args.get("mode") or getattr(
            delegation_config, "delegation_default_mode", _DEFAULT_DELEGATION_MODE
        )
        guardrails_enabled = getattr(delegation_config, "guardrails_enabled", True)

        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            repo_ready_timeout = _get_repo_ready_timeout()
            if mode == "collaborative":
                result = await _submit_collaborative_delegation_job(
                    client=client,
                    steps=args.get("steps", []),
                    guardrails_text=guardrails_text,
                    guardrails_repo=guardrails_repo_alias,
                    repo_ready_timeout=repo_ready_timeout,
                )
            elif mode == "competitive":
                result = await _submit_competitive_delegation_job(
                    client=client,
                    prompt=effective_prompt,
                    repositories=repositories,
                    args=args,
                    repo_ready_timeout=repo_ready_timeout,
                )
            else:
                result = await _submit_open_delegation_job(
                    client=client,
                    prompt=effective_prompt,
                    repositories=repositories,
                    engine=engine,
                    model=args.get("model"),
                    job_timeout=args.get("timeout"),
                    repo_ready_timeout=repo_ready_timeout,
                )

        # Story #458: Audit trail — log after successful job creation
        try:
            result_data = json.loads(result["content"][0]["text"])
            if result_data.get("success"):
                job_id = result_data.get("job_id", "")
                audit_service = getattr(
                    _utils.app_module.app.state, "audit_service", None
                )
                if audit_service is not None:
                    audit_service.log(
                        admin_id=effective_user.username,
                        action_type="open_delegation_executed",
                        target_type="delegation",
                        target_id=str(job_id),
                        details=json.dumps(
                            {
                                "prompt": user_prompt[:500],
                                "engine": engine,
                                "mode": mode,
                                "repositories": list(args.get("repositories", [])),
                                "guardrails_enabled": guardrails_enabled,
                            }
                        ),
                    )
        except Exception as audit_err:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-134",
                    f"Failed to write open delegation audit log: {audit_err}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        return result

    except ClaudeServerError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-131",
                f"Claude Server error in open delegation: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": f"Claude Server error: {e}"})
    except Exception as e:
        logger.exception(
            f"Error in execute_open_delegation: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_cs_register_repository(
    args: Dict[str, Any], user: User, *, session_state=None, **kwargs
) -> Dict[str, Any]:
    """
    Register a CIDX golden repo alias on Claude Server.

    Story #460: Claude Server proxy tools
    Looks up git URL and branch from CIDX, checks if already registered,
    registers if not found (404).
    """
    from ...clients.claude_server_client import (
        ClaudeServerClient,
        ClaudeServerError,
        ClaudeServerNotFoundError,
    )

    delegation_config = _get_delegation_config()
    if delegation_config is None or not delegation_config.is_configured:
        return _mcp_response(
            {"success": False, "error": "Claude Delegation not configured"}
        )

    effective_user = (
        session_state.effective_user
        if session_state and session_state.is_impersonating
        else user
    )
    if not effective_user.has_permission("delegate_open"):
        return _mcp_response(
            {
                "success": False,
                "error": "Access denied: requires power_user or admin role",
            }
        )

    alias = args.get("alias")
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    git_url, branch, lookup_error = _lookup_golden_repo_for_cs(alias)
    if lookup_error is not None:
        return _mcp_response({"success": False, "error": lookup_error})

    if not git_url:
        return _mcp_response(
            {
                "success": False,
                "error": f"Golden repo '{alias}' has no git URL configured",
            }
        )

    try:
        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            try:
                status_data = await client.get_repo_status(alias)
                clone_status = status_data.get("cloneStatus", "unknown")
                return _mcp_response(
                    {
                        "success": True,
                        "clone_status": clone_status,
                        "message": f"Repository '{alias}' is already registered (cloneStatus={clone_status})",
                        "repository": status_data,
                    }
                )
            except ClaudeServerNotFoundError:
                pass
            result = await client.register_repository(alias, git_url or "", branch)
            clone_status = result.get("cloneStatus", "cloning")
            return _mcp_response(
                {
                    "success": True,
                    "clone_status": clone_status,
                    "message": f"Repository '{alias}' registered successfully (cloneStatus={clone_status})",
                    "repository": result,
                }
            )
    except ClaudeServerError as e:
        return _mcp_response(
            {"success": False, "error": f"Failed to register repository: {e}"}
        )
    except Exception as e:
        logger.exception(f"Unexpected error in cs_register_repository: {e}")
        return _mcp_response({"success": False, "error": str(e)})


async def handle_cs_list_repositories(
    args: Dict[str, Any], user: User, *, session_state=None, **kwargs
) -> Dict[str, Any]:
    """
    List all repositories registered on Claude Server.

    Story #460: Claude Server proxy tools
    Calls GET /repositories and returns a normalized list.
    """
    from ...clients.claude_server_client import ClaudeServerClient, ClaudeServerError

    delegation_config = _get_delegation_config()
    if delegation_config is None or not delegation_config.is_configured:
        return _mcp_response(
            {"success": False, "error": "Claude Delegation not configured"}
        )

    effective_user = (
        session_state.effective_user
        if session_state and session_state.is_impersonating
        else user
    )
    if not effective_user.has_permission("delegate_open"):
        return _mcp_response(
            {
                "success": False,
                "error": "Access denied: requires power_user or admin role",
            }
        )

    try:
        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            raw_list = await client.list_repositories()

        repositories = [
            {
                "name": repo.get("name", ""),
                "clone_status": repo.get("cloneStatus", "unknown"),
                "cidx_aware": repo.get("cidxAware", False),
                "git_url": repo.get("gitUrl", ""),
                "branch": repo.get("branch", ""),
                "current_branch": repo.get("currentBranch", ""),
                "registered_at": repo.get("registeredAt", ""),
            }
            for repo in raw_list
        ]
        return _mcp_response({"success": True, "repositories": repositories})
    except ClaudeServerError as e:
        return _mcp_response(
            {"success": False, "error": f"Failed to list repositories: {e}"}
        )
    except Exception as e:
        logger.exception(f"Unexpected error in cs_list_repositories: {e}")
        return _mcp_response({"success": False, "error": str(e)})


async def handle_cs_check_health(
    args: Dict[str, Any], user: User, *, session_state=None, **kwargs
) -> Dict[str, Any]:
    """
    Check Claude Server health status.

    Story #460: Claude Server proxy tools
    Calls GET /health (anonymous on Claude Server, gated at CIDX level).
    """
    from ...clients.claude_server_client import ClaudeServerClient, ClaudeServerError

    delegation_config = _get_delegation_config()
    if delegation_config is None or not delegation_config.is_configured:
        return _mcp_response(
            {"success": False, "error": "Claude Delegation not configured"}
        )

    effective_user = (
        session_state.effective_user
        if session_state and session_state.is_impersonating
        else user
    )
    if not effective_user.has_permission("delegate_open"):
        return _mcp_response(
            {
                "success": False,
                "error": "Access denied: requires power_user or admin role",
            }
        )

    try:
        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            health = await client.get_health()
        return _mcp_response({"success": True, "health": health})
    except ClaudeServerError as e:
        return _mcp_response(
            {"success": False, "error": f"Failed to check health: {e}"}
        )
    except Exception as e:
        logger.exception(f"Unexpected error in cs_check_health: {e}")
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register all delegation handlers into the provided HANDLER_REGISTRY."""
    registry["list_delegation_functions"] = handle_list_delegation_functions
    registry["execute_delegation_function"] = handle_execute_delegation_function
    registry["poll_delegation_job"] = handle_poll_delegation_job
    registry["execute_open_delegation"] = handle_execute_open_delegation
    registry["cs_register_repository"] = handle_cs_register_repository
    registry["cs_list_repositories"] = handle_cs_list_repositories
    registry["cs_check_health"] = handle_cs_check_health
