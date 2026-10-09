"""Written reasons for the routes that write no audit row by design.

Used by ``test_catalog_completeness``, which inventories EVERY registered
mutating route and requires each to be mapped to catalog action types,
EXEMPT (below, with a one-line reason), or NON-ADMIN SELF-SERVICE (below,
with a one-line reason).  Keys are ``"METHOD /path"`` as mounted.
"""

from __future__ import annotations

from typing import Dict

READ_ONLY = "read-only: lists or reports state, changes nothing"
WORKSPACE_GIT = (
    "git operation inside the caller's activated working copy; changes "
    "repository content, not accounts, credentials, access or configuration"
)
SCIP_SCRATCH_CLEANUP = (
    "deletes expired temporary SCIP self-healing workspaces (scratch copies "
    "only; no index, access or configuration change)"
)

_PROBE_ONLY = "connectivity probe with the supplied or stored key; stores nothing"
_CATEGORY = "presentation grouping of repositories; no access or credential change"
_DIAGNOSTICS = "runs read-only diagnostic checks and stores their report"
_DEPMAP = (
    "controls the dependency-map analysis job; rewrites generated domain "
    "documents, not access or configuration"
)
_WIKI_TOGGLE = (
    "turns the generated wiki pages of one {kind} repository on or off; its "
    "readers and their access are unchanged"
)
_JOB_CANCEL = (
    "stops one background job (owner ruling: operational); the job's own "
    "record holds its outcome"
)
_PROVIDER_HEALTH = "resets in-memory embedding-provider health counters"
_RESEARCH = "interactive research-assistant session"
_FAULT_INJECTION = (
    "non-production fault-injection harness; refused at startup outside non-prod"
)

# Admin doors that change operational state only (owner rulings included).
ROUTE_EXEMPT: Dict[str, str] = {
    "POST /api/admin/search-events/export": READ_ONLY,
    "POST /admin/query": READ_ONLY,
    "POST /admin/partials/query-results": READ_ONLY,
    "POST /api/v1/repos/{alias}/git/reset": WORKSPACE_GIT,
    "POST /api/v1/repos/{alias}/git/clean": WORKSPACE_GIT,
    "DELETE /api/v1/repos/{alias}/git/branches/{name:path}": WORKSPACE_GIT,
    **{
        f"POST /api/api-keys/{provider}/{probe}": _PROBE_ONLY
        for provider in ("anthropic", "voyageai", "cohere")
        for probe in ("test", "test-configured")
    },
    "POST /api/llm-creds/test-connection": _PROBE_ONLY,
    "POST /api/v1/repo-categories": _CATEGORY,
    "PUT /api/v1/repo-categories/{category_id}": _CATEGORY,
    "DELETE /api/v1/repo-categories/{category_id}": _CATEGORY,
    "POST /api/v1/repo-categories/reorder": _CATEGORY,
    "POST /api/v1/repo-categories/re-evaluate": _CATEGORY,
    "POST /admin/repo-categories/create": _CATEGORY,
    "POST /admin/repo-categories/{category_id}/update": _CATEGORY,
    "POST /admin/repo-categories/{category_id}/delete": _CATEGORY,
    "POST /admin/repo-categories/reorder": _CATEGORY,
    "POST /admin/repo-categories/re-evaluate": _CATEGORY,
    "POST /admin/golden-repos/{alias}/category": _CATEGORY,
    "POST /admin/diagnostics/run-all": _DIAGNOSTICS,
    "POST /admin/diagnostics/run/{category}": _DIAGNOSTICS,
    "POST /admin/diagnostics/generate-missing-descriptions": (
        "queues generation of missing repository description documents"
    ),
    "POST /admin/langfuse-sync/trigger": (
        "starts one pull of trace data from the configured observability service"
    ),
    "POST /admin/self-monitoring/run-now": (
        "starts one self-monitoring log scan (same as its scheduled run)"
    ),
    "POST /admin/partials/depmap-job-status/retry": _DEPMAP,
    "POST /admin/dependency-map/repair": _DEPMAP,
    "POST /admin/dependency-map/trigger": _DEPMAP,
    "POST /admin/dependency-map/cancel": _DEPMAP,
    "POST /admin/dependency-map/trigger-refinement": _DEPMAP,
    "POST /admin/golden-repos/{alias}/wiki-refresh": (
        "clears the rendered-wiki cache of one golden repository (re-rendered "
        "on the next view)"
    ),
    "POST /admin/golden-repos/{alias}/wiki-toggle": _WIKI_TOGGLE.format(kind="golden"),
    "POST /admin/activated-repos/{username}/{alias}/wiki-toggle": (
        _WIKI_TOGGLE.format(kind="activated")
    ),
    "POST /admin/golden-repos/{alias}/temporal-options": (
        "sets how much commit history one golden repository indexes; changes "
        "index content, not who can read it"
    ),
    "POST /api/admin/scip-cleanup-workspaces": SCIP_SCRATCH_CLEANUP,
    "DELETE /api/admin/jobs/cleanup": (
        "deletes finished background-job records past their retention age "
        "(job history, not security state)"
    ),
    "POST /admin/jobs/{job_id}/cancel": _JOB_CANCEL,
    "DELETE /api/jobs/{job_id}": (
        _JOB_CANCEL + "; a user may stop only their own jobs, an admin any"
    ),
    "POST /admin/config/query-embedding-cache/clear": (
        "empties the query-embedding cache (recomputed on demand)"
    ),
    "POST /admin/provider-health/clear-sinbin": _PROVIDER_HEALTH,
    "POST /admin/provider-health/reset-state": _PROVIDER_HEALTH,
    "POST /api/admin/diagnostics/dedup-warnings/clear-all": (
        "clears the active storage-migration duplicate warnings"
    ),
    "POST /api/admin/reaper/trigger": (
        "starts one cycle of the idle activated-repository reaper, which "
        "applies the configured idle policy (same as its scheduled run)"
    ),
    **{
        f"POST /admin/research/{path}": _RESEARCH
        for path in ("send", "sessions", "sessions/{session_id}/upload")
    },
    "PUT /admin/research/sessions/{session_id}": _RESEARCH,
    "DELETE /admin/research/sessions/{session_id}": _RESEARCH,
    "DELETE /admin/research/sessions/{session_id}/files/{filename}": _RESEARCH,
    **{
        f"{method} /admin/fault-injection/{path}": _FAULT_INJECTION
        for method, path in (
            ("PUT", "profiles/{target}"),
            ("PATCH", "profiles/{target}"),
            ("DELETE", "profiles/{target}"),
            ("DELETE", "profiles"),
            ("POST", "reset"),
            ("POST", "preview"),
            ("POST", "seed"),
        )
    },
    **{
        f"POST /admin/api/discovery/{path}": "browses or hides forge repositories"
        for path in (
            "{platform}/start",
            "{platform}/enrich",
            "hide",
            "unhide",
            "branches",
        )
    },
}

_OWN_COPY = (
    "acts on the caller's own activated repository copy (activate, sync, "
    "branch, reindex, health check, remove)"
)
_OWN_FILES = "edits files in the caller's own activated working copy"
_SEARCH = "read-only search as the caller over repositories the caller can read"
_MEMORY = "shared technical memory notes (repository knowledge, not access)"
_MCP_TRANSPORT = (
    "MCP transport; every tool call is classified per tool by the MCP inventory"
)

# Non-admin doors: the caller acting on their own resources, or searching.
ROUTE_SELF_SERVICE: Dict[str, str] = {
    **{
        f"{method} {path}": _OWN_COPY
        for method, path in (
            ("POST", "/api/repos/activate"),
            ("DELETE", "/api/repos/{user_alias}"),
            ("POST", "/api/repos/sync"),
            ("PUT", "/api/repos/{user_alias}/branch"),
            ("PUT", "/api/repos/{user_alias}/sync"),
            ("POST", "/api/repositories/{repo_id}/sync"),
            ("POST", "/api/activated-repos/{user_alias}/branch"),
            ("POST", "/api/activated-repos/{user_alias}/health/check"),
            ("POST", "/api/activated-repos/{user_alias}/indexes/{index_type}"),
            ("POST", "/api/activated-repos/{user_alias}/reindex"),
            ("POST", "/api/activated-repos/{user_alias}/sync"),
            ("POST", "/api/repositories/{repo_alias}/health/check"),
            ("POST", "/api/v1/repos/{alias}/reindex"),
        )
    },
    "POST /api/v1/repos/{alias}/files": _OWN_FILES,
    "PATCH /api/v1/repos/{alias}/files/{file_path:path}": _OWN_FILES,
    "DELETE /api/v1/repos/{alias}/files/{file_path:path}": _OWN_FILES,
    **{
        f"POST /api/v1/repos/{{alias}}/git/{op}": WORKSPACE_GIT
        for op in (
            "branches",
            "branches/{name}/switch",
            "checkout-file",
            "commit",
            "fetch",
            "merge-abort",
            "pull",
            "push",
            "stage",
            "unstage",
        )
    },
    **{
        f"POST {path}": _SEARCH
        for path in (
            "/api/query",
            "/api/query/multi",
            "/api/regex/search",
            "/api/repositories/{repo_id}/search",
            "/api/xray/search",
            "/api/xray/search/batch",
            *(
                f"/api/scip/multi/{kind}"
                for kind in (
                    "callchain",
                    "definition",
                    "dependencies",
                    "dependents",
                    "references",
                )
            ),
        )
    },
    "POST /api/v1/memories": _MEMORY,
    "PUT /api/v1/memories/{memory_id}": _MEMORY,
    "DELETE /api/v1/memories/{memory_id}": _MEMORY,
    "POST /mcp": _MCP_TRANSPORT,
    "POST /mcp-public": _MCP_TRANSPORT,
    "DELETE /mcp": _MCP_TRANSPORT + " (ends the caller's MCP session)",
    "POST /auth/refresh": "renews the caller's own session from a refresh token",
    "POST /auth/reset-password": (
        "public password-reset request; answers generically, changes no account"
    ),
}
