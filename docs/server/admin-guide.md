# Admin Guide

Starting point for CIDX Server administrators: what each area of the administration Web UI does, where it is
documented, and which administrative operations require TOTP step-up elevation.

The administration Web UI is at `/admin` and is available to accounts with the `admin` role. Other users get a
smaller self-service UI at `/user` (API keys, MCP credentials, git credentials, MFA).

## Web UI areas

| Area (path) | What it is for | Documentation |
|-------------|----------------|---------------|
| Dashboard (`/admin/`) | Health, job and node summary; Langfuse sync card when trace pull is on | [Observability](observability.md), [Langfuse Trace Sync](langfuse-trace-sync.md) |
| Users (`/admin/users`) | Create accounts, change roles, passwords and emails, delete accounts | [Accounts and Access](auth/accounts-and-access.md#accounts) |
| Groups (`/admin/groups`) | Groups, group membership, repository grants | [Accounts and Access](auth/accounts-and-access.md#groups-and-repository-access) |
| Golden Repos, Auto-Discovery, Repositories, Repo Categories | Register and refresh shared repositories; view activated repositories | [Repository Lifecycle](../architecture/repository-lifecycle.md) |
| Jobs (`/admin/jobs`) | Background jobs, cancellation | [Maintenance and Jobs](maintenance-and-jobs.md) |
| Logs, Audit Logs, Embedding Stats | Application logs, audit events, provider call statistics | [Observability](observability.md) |
| Diagnostics (`/admin/diagnostics`) | On-demand checks of CLI tools, SDK prerequisites, external APIs, credentials and infrastructure | - |
| Query (`/admin/query`) | Run searches from the browser | [Query Guide](../guides/query.md) |
| Dependency Map (`/admin/dependency-map`) | Cross-repository dependency analysis | [Dependency Map](../architecture/dependency-map.md) |
| Config (`/admin/config`) | Runtime settings stored in the database, including SSO, TOTP elevation, Web Security, OpenTelemetry, Langfuse and SIEM delivery | [Deployment](deployment.md#bootstrap-configuration-configjson), [OIDC](auth/oidc.md), [Login and Elevation](auth/login-and-elevation.md#settings), [SIEM Operations](siem/operations.md) |
| API Keys, MCP Credentials | The signed-in administrator's own API keys and MCP credentials (the MCP page also lists the server's system credentials) | [Accounts and Access](auth/accounts-and-access.md#api-keys) |
| Git Credentials, SSH Keys | Credentials the server uses towards git forges | - |
| Analytics, Self-Monitoring, Research Assistant | Analytics export, scheduled self-monitoring scans, interactive research sessions | - |
| MFA (`/admin/mfa/setup`) | Enrol TOTP for the signed-in administrator | [Login and Elevation](auth/login-and-elevation.md#totp-mfa) |

Maintenance mode is not in the Web UI: its write endpoints accept only loopback callers
([Maintenance and Jobs](maintenance-and-jobs.md#maintenance-mode)). The fault-injection harness exists only on
non-production servers that enable it ([Fault Injection](fault-injection.md)).

## Elevation policy

When step-up elevation enforcement is on (runtime setting `elevation_enforcement_enabled`, default off; see
[Login and Elevation](auth/login-and-elevation.md#step-up-elevation)):

- **Web UI writes.** Every `POST`, `PUT`, `DELETE` and `PATCH` route of the administration Web UI requires an open
  elevation window, with these exceptions:
  - routes that cannot require it without a deadlock: logout, the elevation page `/admin/elevate`, the TOTP setup
    activation and the login MFA challenge;
  - search form submissions (`/admin/query`, the query results partial), which change nothing;
  - disabling MFA and regenerating recovery codes, which check the window inside the handler and also accept a
    window opened with a recovery code.
- **Sensitive Web UI reads.** The SSH Keys, Logs and Query Analytics Export pages send an unelevated administrator
  to `/admin/elevate`, which returns to the page afterwards. The log list, the log export, the users list and the
  group membership list (the User Assignments tab of the Groups page, which the page itself never renders) require
  a window; without one the TOTP dialog opens. The analytics export page downloads its files through the same
  dialog-aware request path.
- **REST and MCP writes.** Administrative REST writes carry the same requirement, and so do the MCP tools marked
  with it in the [MCP tool reference](../reference/mcp-tools/README.md). Self-service credential changes (own API
  keys, MCP credentials and git credentials) require it for every role.
- **REST and MCP reads of user data.** Administrative reads that return user lists, group membership, search
  history, audit content or credentials require a window: REST `GET /api/admin/users`, `GET /api/v1/users`,
  `GET /api/admin/search-events`, `POST /api/admin/search-events/export`, `GET /api/admin/search-events/exports`
  and its download, `GET /api/admin/activated-repos`, `GET /api/v1/groups/{group_id}`, `GET /api/v1/audit-logs`,
  `GET /api/admin/scip-pr-history` and `GET /api/admin/scip-git-cleanup-history`; and the MCP tools `list_users`,
  `get_group`, `query_audit_logs`, `scip_pr_history` and `scip_cleanup_history`. Without a window they answer
  `elevation_required`. An API key cannot hold an elevation window, so these reads refuse an administrator's API
  key while enforcement is on. A completed analytics export is downloaded under a window too.

With enforcement off, all of these run without an elevation check.

The routes themselves are the authority, not this page. The test
`tests/unit/server/web/test_admin_elevation_gating_956.py` inspects the administration router and fails when a
mutation route lacks the elevation dependency and is not on its exemption list (`_EXEMPT_ROUTES`), and it pins the
elevation-gated reads. Adding an exemption means editing that list with a justification.

For REST reads, `tests/unit/server/routers/test_admin_get_routes_require_elevation.py` enumerates every `GET`
route under `/api/admin/` and `/api/v1/` on the real application and fails when one does not require elevation
(read from the route's dependency tree) and is not on its `ALLOWLIST`. Every allowlist entry carries a written
reason, for example operational counters, maintenance status polled by the auto-updater, or repository reads
governed by repository permissions. Nothing returning user lists, emails, group membership, search history, audit
content or credentials may be allowlisted, and an entry for a route that no longer exists or is now gated fails the
test.
