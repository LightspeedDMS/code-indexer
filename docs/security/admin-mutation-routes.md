# Admin Web UI Mutation Routes — Security Inventory

Bug #956: All POST/PUT/DELETE/PATCH routes registered on `web_router` that perform
state-changing operations must carry `dependencies=[Depends(dependencies.require_elevation())]`
to enforce TOTP step-up elevation before any administrative mutation can succeed.

This document is the canonical inventory. The CI gate test
`tests/unit/server/web/test_admin_elevation_gating_956.py::TestAdminElevationGating::test_ungated_routes_table`
enforces this list programmatically — any new mutation route added without elevation gating
will cause that test to fail.

---

## Gated Routes (require_elevation)

All routes below require a valid TOTP elevation window for the requesting admin session.
Requests without an active window receive HTTP 403 with `elevation_required`.

| Method | Path | Description |
|--------|------|-------------|
| POST | /users/create | Create a new user account |
| POST | /users/{username}/role | Change a user's role |
| POST | /users/{username}/password | Change a user's password |
| POST | /users/{username}/email | Change a user's email |
| POST | /users/{username}/delete | Delete a user account |
| POST | /groups/create | Create a new group |
| POST | /groups/{group_id}/update | Update a group's name/description |
| POST | /groups/{group_id}/delete | Delete a group |
| POST | /groups/users/{user_id:path}/assign | Assign a user to a group |
| POST | /groups/repo-access/grant | Grant a group access to a repository |
| POST | /groups/repo-access/revoke | Revoke a group's access to a repository |
| POST | /golden-repos/add | Add a new golden repository |
| POST | /golden-repos/batch-create | Batch-add golden repositories |
| POST | /golden-repos/{alias}/delete | Delete a golden repository |
| POST | /golden-repos/{alias}/refresh | Trigger a manual refresh of a golden repo |
| POST | /golden-repos/{alias}/force-resync | Force a full resync of a golden repo |
| POST | /golden-repos/{alias}/wiki-toggle | Enable or disable wiki for a golden repo |
| POST | /golden-repos/{alias}/temporal-options | Update temporal indexing options |
| POST | /golden-repos/{alias}/wiki-refresh | Trigger a wiki refresh |
| POST | /golden-repos/{alias}/change-branch | Change the tracked branch |
| POST | /golden-repos/activate | Activate a golden repo for a user |
| POST | /repos/{username}/{user_alias}/deactivate | Deactivate an activated repository |
| POST | /activated-repos/{username}/{alias}/wiki-toggle | Toggle wiki for an activated repo |
| POST | /jobs/{job_id}/cancel | Cancel a background job |
| POST | /api/discovery/{platform}/enrich | Enrich a discovered repository |
| POST | /api/discovery/hide | Hide a discovered repository |
| POST | /api/discovery/unhide | Unhide a previously hidden repository |
| POST | /api/discovery/branches | Update branch settings for a discovered repo |
| POST | /config/claude_delegation | Update Claude delegation settings |
| POST | /config/reset | Reset configuration to defaults |
| POST | /config/langfuse_pull | Pull Langfuse configuration |
| POST | /config/cidx_meta_backup | Update cidx-meta backup configuration |
| POST | /config/{section} | Update a named configuration section |
| POST | /config/api-keys/{platform} | Add or update an API key |
| DELETE | /config/api-keys/{platform} | Delete an API key |
| POST | /git-credentials | Add git credentials |
| DELETE | /git-credentials/{credential_id} | Delete git credentials |
| POST | /ssh-keys/create | Generate a new SSH key pair |
| POST | /ssh-keys/delete | Delete an SSH key |
| POST | /ssh-keys/assign-host | Assign an SSH key to a host |
| POST | /self-monitoring | Update self-monitoring configuration |
| POST | /self-monitoring/run-now | Trigger an immediate self-monitoring check |
| POST | /restart | Restart the CIDX server process |
| POST | /config/totp | Update TOTP/elevation configuration |

---

## Exempt Routes (no elevation required)

These mutation-shaped routes are intentionally exempt from elevation gating.

| Method | Path | Justification |
|--------|------|---------------|
| GET | /logout | Session termination — requires no elevation; blocking a logout would be a security anti-pattern |
| GET | /elevate | The elevation prompt page itself — must be reachable without elevation to initiate the flow |
| POST | /query | Search query — read-only semantic operation; no state mutation |
| POST | /partials/query-results | HTMX partial for search results — read-only; no state mutation |

---

## Enforcement

The CI gate test inspects the FastAPI router at import time using:

```python
from code_indexer.server.web.routes import web_router

for route in web_router.routes:
    if route.methods & MUTATION_METHODS and route.path not in EXEMPT_PATHS:
        assert has_require_elevation(route.dependencies), f"Route {route.path} is ungated"
```

This means adding a new mutation route without `dependencies=[Depends(dependencies.require_elevation())]`
will fail CI immediately. There is no grace period.

To add an exempt route, update both the decorator (omitting the dependency) and the
`EXEMPT_PATHS` set in `tests/unit/server/web/test_admin_elevation_gating_956.py`, with a
comment explaining why elevation is inappropriate for that route.
