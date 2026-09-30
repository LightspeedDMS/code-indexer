"""Audited golden-repository and provider-index entry points.

Every front door (REST, MCP, Web) that changes a golden repository or a
provider index reaches the change through ONE audited entry point that takes
the acting user as a required keyword argument and records exactly one row:
``success`` once the job is submitted (``details.job_id``; a job-based row
means "submitted", never "completed"), or ``failure`` when the request is
refused, after which the original exception propagates unchanged.

Rows carry only verified identifiers (an alias the operation found or
created, else the ``unresolved`` placeholder) and never a URL: the clone URL
contributes only its host name, parsed here, so URL credentials and query
strings can never reach a row.

Internal maintenance (the scheduler's own refreshes, meta-description and
dependency-map refreshes) calls the unaudited primitives directly and writes
no row, so no per-repository audit work happens at fleet scale.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlsplit

from code_indexer.server.services.audit_outcome import (
    AuditActor,
    conforming_details,
    record_outcome,
)

logger = logging.getLogger(__name__)

_URL_SCHEMES_WITH_HOST = frozenset({"http", "https", "ssh", "git", "git+ssh"})
# scp-like ``user@host:path`` (no scheme): the host sits between "@" and ":".
_SCP_LIKE = re.compile(r"^[^@/\s]+@([^:/\s]+):")


def repo_host(repo_url: object) -> Optional[str]:
    """The host name of a clone URL, or None; never the URL or its userinfo."""
    if not isinstance(repo_url, str) or not repo_url:
        return None
    scp = _SCP_LIKE.match(repo_url)
    if scp is not None and "://" not in repo_url:
        return scp.group(1).lower()
    try:
        parts = urlsplit(repo_url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in _URL_SCHEMES_WITH_HOST or not host:
        return None
    return host.lower()


def record_repo_outcome(
    actor: AuditActor,
    action_type: str,
    alias: Optional[str],
    outcome: str,
    **detail_candidates: Any,
) -> None:
    """Record one golden-repo row; *alias* is None unless verified."""
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="repo",
        target_id=alias,
        outcome=outcome,
        details=conforming_details(action_type, **detail_candidates),
    )


# ---------------------------------------------------------------------------
# Golden-repo refresh (the human-initiated door; internal refreshes call
# RefreshScheduler.trigger_refresh_for_repo directly and are not audited)
# ---------------------------------------------------------------------------


def request_golden_repo_refresh(
    refresh_scheduler: Any, alias: str, *, actor: str, force_reset: bool = False
) -> Optional[str]:
    """Submit a refresh of *alias* for *actor*; record ``golden_repo_refreshed``."""
    try:
        job_id = refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=actor, force_reset=force_reset
        )
    except Exception:
        record_repo_outcome(
            actor, "golden_repo_refreshed", None, "failure", force_reset=force_reset
        )
        raise
    record_repo_outcome(
        actor,
        "golden_repo_refreshed",
        alias,
        "success",
        job_id=job_id,
        force_reset=force_reset,
    )
    return cast(Optional[str], job_id)


def submit_provider_scoped_index_job(
    job_manager: Any, *, actor: str, alias: str, index_type: str, **submit_kwargs: Any
) -> str:
    """Submit one provider-scoped index job for *alias*; record one row.

    Used for the per-provider semantic and temporal jobs of an add-index
    request; the other index types go through the manager's audited
    ``add_indexes_to_golden_repo``.
    """
    try:
        job_id = str(
            job_manager.submit_job(
                submitter_username=actor, repo_alias=alias, **submit_kwargs
            )
        )
    except Exception:
        record_repo_outcome(
            actor, "golden_repo_index_added", None, "failure", index_types=[index_type]
        )
        raise
    record_repo_outcome(
        actor,
        "golden_repo_index_added",
        alias,
        "success",
        index_types=[index_type],
        job_id=job_id,
    )
    return job_id


# ---------------------------------------------------------------------------
# Provider indexes (one shared implementation for REST and MCP)
# ---------------------------------------------------------------------------

INVALID_PROVIDER = "invalid_provider"
REPO_NOT_FOUND = "repo_not_found"
BASE_CLONE_UNRESOLVED = "base_clone_unresolved"
CONFIG_WRITE_FAILED = "config_write_failed"
JOB_MANAGER_UNAVAILABLE = "job_manager_unavailable"

_PROVIDER_JOB_ACTIONS = {
    "add": "provider_index_added",
    "recreate": "provider_index_recreated",
}
_BULK_LIST_MAX = 100


class ProviderIndexRequestError(Exception):
    """A provider-index request was refused; each door maps *kind* to its shape.

    ``detail`` is the provider validation message (``INVALID_PROVIDER``) or
    the base clone path (``CONFIG_WRITE_FAILED``); it is never recorded.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


def _record_provider(
    actor: str,
    action_type: str,
    target: Optional[str],
    outcome: str,
    **detail_candidates: Any,
) -> None:
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="provider_index",
        target_id=target,
        outcome=outcome,
        details=conforming_details(action_type, **detail_candidates),
    )


def _validated_repo_path(service: Any, provider: str, alias: str) -> str:
    """Check the provider is configured and *alias* resolves; return its path."""
    from code_indexer.server.mcp.handlers import _resolve_golden_repo_path

    error = service.validate_provider(provider)
    if error:
        raise ProviderIndexRequestError(INVALID_PROVIDER, error)
    repo_path = _resolve_golden_repo_path(alias)
    if not repo_path:
        raise ProviderIndexRequestError(REPO_NOT_FOUND)
    return cast(str, repo_path)


def _writable_base_clone(alias: str) -> str:
    from code_indexer.server.mcp.handlers import _resolve_golden_repo_base_clone

    base_clone = _resolve_golden_repo_base_clone(alias)
    if not base_clone:
        raise ProviderIndexRequestError(BASE_CLONE_UNRESOLVED)
    return cast(str, base_clone)


def submit_provider_index_job(
    *,
    job_manager: Any,
    service: Any,
    action: str,
    provider: str,
    alias: str,
    actor: str,
) -> str:
    """Add or recreate *provider*'s index for *alias* (a background job).

    Writes the provider into the base clone's config, submits the job and
    records ``provider_index_added`` / ``provider_index_recreated``.

    Raises:
        ProviderIndexRequestError: the request was refused (row: failure).
    """
    from code_indexer.server.mcp.handlers import (
        _append_provider_to_config,
        _provider_index_job,
    )

    action_type = _PROVIDER_JOB_ACTIONS[action]
    verified: Optional[str] = None
    try:
        repo_path = _validated_repo_path(service, provider, alias)
        verified = alias
        if job_manager is None:
            raise ProviderIndexRequestError(JOB_MANAGER_UNAVAILABLE)
        base_clone = _writable_base_clone(alias)
        if not _append_provider_to_config(base_clone, provider):
            raise ProviderIndexRequestError(CONFIG_WRITE_FAILED, base_clone)
        job_id = str(
            job_manager.submit_job(
                operation_type=f"provider_index_{action}",
                func=_provider_index_job,
                submitter_username=actor,
                repo_alias=alias,
                repo_path=repo_path,
                provider_name=provider,
                clear=action == "recreate",
            )
        )
    except Exception:
        _record_provider(actor, action_type, verified, "failure", provider=provider)
        raise
    _record_provider(
        actor, action_type, alias, "success", provider=provider, job_id=job_id
    )
    return job_id


def remove_provider_index_audited(
    *, service: Any, provider: str, alias: str, actor: str
) -> Dict[str, Any]:
    """Remove *provider*'s collection from *alias*; record ``provider_index_removed``.

    Returns the service result (``removed``, ``collection_name``,
    ``message``); a result that removed nothing is recorded as a failure.
    """
    from code_indexer.server.mcp.handlers import _remove_provider_from_config

    verified: Optional[str] = None
    try:
        _validated_repo_path(service, provider, alias)
        verified = alias
        base_clone = _writable_base_clone(alias)
        _remove_provider_from_config(base_clone, provider)
        result = cast(
            Dict[str, Any], service.remove_provider_index(base_clone, provider)
        )
    except Exception:
        _record_provider(
            actor, "provider_index_removed", verified, "failure", provider=provider
        )
        raise
    _record_provider(
        actor,
        "provider_index_removed",
        alias,
        "success" if result.get("removed") else "failure",
        provider=provider,
    )
    return result


def prepare_bulk_add_jobs(
    global_repos: List[Dict[str, Any]],
    filter_str: Optional[str],
    provider: str,
    service: Any,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Per-repo preparation for a bulk add: resolve each repo's paths, skip
    repos that already have *provider*, and write *provider* into each base
    clone's config.

    Returns (to_submit, skipped): to_submit holds {"alias", "repo_path"}
    for repos whose config write succeeded.
    """
    from code_indexer.server.mcp.handlers import (
        _append_provider_to_config,
        _resolve_golden_repo_base_clone,
        _resolve_golden_repo_path,
    )

    to_submit: List[Dict[str, str]] = []
    skipped: List[str] = []
    for repo in global_repos:
        alias = repo.get("alias_name", "")
        if filter_str and filter_str.startswith("category:"):
            filter_cat = filter_str.split(":", 1)[1]
            if filter_cat.lower() not in repo.get("category", "").lower():
                continue
        repo_path = _resolve_golden_repo_path(alias)
        if not repo_path:
            continue
        repo_status = service.get_provider_index_status(repo_path, alias)
        if repo_status.get(provider, {}).get("exists"):
            skipped.append(alias)
            continue
        base_clone = _resolve_golden_repo_base_clone(alias)
        if not base_clone or not _append_provider_to_config(base_clone, provider):
            logger.warning("bulk provider-index add: skipping %s", alias)
            skipped.append(alias)
            continue
        to_submit.append({"alias": alias, "repo_path": repo_path})
    return to_submit, skipped


def _bulk_details(provider: str, jobs: List[Dict[str, str]]) -> Dict[str, Any]:
    aliases = [job["alias"] for job in jobs]
    candidates: Dict[str, Any] = {
        "provider": provider,
        "aliases": aliases[:_BULK_LIST_MAX],
        "job_ids": [job["job_id"] for job in jobs][:_BULK_LIST_MAX],
    }
    if len(aliases) > _BULK_LIST_MAX:
        candidates["aliases_truncated"] = len(aliases) - _BULK_LIST_MAX
    return conforming_details("provider_index_bulk_added", **candidates)


def bulk_add_provider_index_audited(
    *,
    job_manager: Any,
    service: Any,
    provider: str,
    filter_str: Optional[str],
    actor: str,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Add *provider*'s index to every golden repo lacking it; ONE row per request.

    Returns (jobs, skipped) where jobs holds {"alias", "job_id"}.
    """
    from code_indexer.server.mcp.handlers import (
        _list_global_repos,
        _provider_index_job,
    )

    jobs: List[Dict[str, str]] = []
    try:
        error = service.validate_provider(provider)
        if error:
            raise ProviderIndexRequestError(INVALID_PROVIDER, error)
        if job_manager is None:
            raise ProviderIndexRequestError(JOB_MANAGER_UNAVAILABLE)
        to_submit, skipped = prepare_bulk_add_jobs(
            _list_global_repos(), filter_str, provider, service
        )
        for item in to_submit:
            job_id = job_manager.submit_job(
                operation_type="provider_index_add",
                func=_provider_index_job,
                submitter_username=actor,
                repo_alias=item["alias"],
                repo_path=item["repo_path"],
                provider_name=provider,
                clear=False,
                # Pod-pull: reconstruction params for _provider_index_job.
                metadata={
                    "repo_path": item["repo_path"],
                    "provider_name": provider,
                    "clear": False,
                },
            )
            jobs.append({"alias": item["alias"], "job_id": str(job_id)})
    except Exception:
        _record_bulk(actor, provider, "failure", jobs)
        raise
    _record_bulk(actor, provider, "success", jobs)
    return jobs, skipped


def _record_bulk(
    actor: str, provider: str, outcome: str, jobs: List[Dict[str, str]]
) -> None:
    record_outcome(
        actor=actor,
        action_type="provider_index_bulk_added",
        target_type="provider_index",
        target_id=provider,
        outcome=outcome,
        details=_bulk_details(provider, jobs),
    )
