# REST API

A short guide to the CIDX server's REST API: how to authenticate, where the full OpenAPI description is served, the
main endpoint families with one example each, and the shape of error responses. The OpenAPI schema is the complete
and current list of endpoints and fields; this page does not repeat it.

Audience: developers scripting against a CIDX server. AI assistants normally use the MCP endpoint instead (see
[MCP registration](../getting-started/mcp-registration.md)); the `cidx` CLI in remote mode uses this API (see
[Remote CLI](../guides/remote-cli.md)).

The examples use `https://cidx.example.com` as the server address.

## OpenAPI schema and interactive docs

| Path | Content |
|------|---------|
| `GET /openapi.json` | the OpenAPI schema |
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc |

All three require authentication: a Web UI session cookie or a bearer token. Without one, `/docs` and `/redoc`
redirect to the login page and `/openapi.json` answers 401 (`src/code_indexer/server/routers/api_docs.py`). Behind a
reverse proxy with a path prefix, the schema lists the prefix as its first server so "Try it out" works.

```bash
curl -s https://cidx.example.com/openapi.json -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head
```

## Authentication

Log in with a JSON body (not a form) at `POST /auth/login`:

```bash
curl -s -X POST https://cidx.example.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "your-password"}'
```

Response (`LoginResponse` in `src/code_indexer/server/models/auth.py`):

```json
{"access_token": "<jwt>", "token_type": "bearer", "user": {"username": "alice", "role": "power_user", "...": "..."},
 "refresh_token": "<token>", "refresh_token_expires_in": <seconds>}
```

Send the access token on every request: `Authorization: Bearer <access_token>`.

- **MFA.** If the account has TOTP MFA, the login answers `{"mfa_required": true, "mfa_token": "<token>"}` instead.
  Complete it with `POST /auth/mfa/verify` and `{"mfa_token": "...", "totp_code": "123456"}` (or
  `"recovery_code"`); the response is the same `LoginResponse`.
- **Lifetime.** Access tokens expire after `jwt_expiration_minutes`, a runtime setting with default 10 minutes,
  read when the server starts (`JWTManager` construction in `src/code_indexer/server/startup/service_init.py`).
  Get a new one with `POST /auth/refresh` and `{"refresh_token": "..."}`, or log in again.
- **API keys.** A bearer value starting with `cidx_sk_` is treated as an API key instead of a token
  (`src/code_indexer/server/auth/dependencies.py`); see
  [Accounts and access](../server/auth/accounts-and-access.md#api-keys).
- **Throttling.** Repeated failures answer 429 with `Retry-After`, and a busy throttle store answers 503; see
  [Login and elevation](../server/auth/login-and-elevation.md#login-throttle).
- **Elevation.** Administrative changes need a TOTP step-up window when the server enforces it:
  `POST /auth/elevate` with `{"totp_code": "123456"}`; see
  [Login and elevation](../server/auth/login-and-elevation.md#step-up-elevation).

## Endpoint families

| Prefix | Purpose |
|--------|---------|
| `/auth/...` | login, MFA verification, token refresh, step-up elevation |
| `POST /api/query`, `POST /api/query/multi` | semantic, full-text and hybrid search in one or several repositories |
| `/api/regex`, `/api/xray`, `/api/scip/multi` | regex search, X-Ray AST search, SCIP code intelligence across repositories |
| `/api/repos/...` | your activated repositories: activate, list, switch branch, sync, browse files |
| `/api/v1/repos/{alias}/git/...` | git operations on an activated repository |
| `/api/admin/golden-repos`, `/api/admin/users`, `/api/admin/...` | administration (admin role) |
| `/api/jobs`, `/api/jobs/{job_id}` | background job status and cancellation |
| `/api/v1/groups`, `/api/v1/users`, `/api/v1/audit-logs` | groups, users, audit log |
| `/api/ssh-keys`, `/api/api-keys`, `/api/cicd` | server SSH keys, provider API keys, CI/CD |
| `/health`, `/healthz`, `/api/system/health` | health checks; see [Observability](../server/observability.md) |
| `/mcp` | the MCP endpoint (JSON-RPC), not a REST resource |

### Query

```bash
curl -s -X POST https://cidx.example.com/api/query \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query_text": "token refresh logic", "repository_alias": "example-repo-global", "limit": 5}'
```

The text field is `query_text` (1 to 1000 characters); `limit` is 1 to 100 (default 10). `repository_alias` is one
of your activated repositories or a global alias ending in `-global`; without it the server searches every
repository available to you. `search_mode` is `semantic` (default), `fts` or `hybrid`. A semantic response carries
`results` (each with `file_path`, `line_number`, `code_snippet`, `similarity_score`, `repository_alias`, ...),
`total_results` and `query_metadata` (`SemanticQueryRequest` / `SemanticQueryResponse` in
`src/code_indexer/server/models/query.py`). Every parameter: [Query parameter
inventory](../guides/query.md#query-parameter-inventory).

### Long-running operations: add a golden repository

Operations that clone or index run as background jobs. The request answers 202 with a job id:

```bash
curl -s -X POST https://cidx.example.com/api/admin/golden-repos \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"repo_url": "https://git.example.com/team/example-repo.git", "alias": "example-repo", "default_branch": "main"}'
```

```json
{"job_id": "<job-id>", "message": "Golden repository 'example-repo' addition started"}
```

This endpoint needs the admin role and, when elevation is enforced, an open elevation window
(`add_golden_repo` in `src/code_indexer/server/routers/inline_admin_ops.py`). `POST /api/repos/activate` (activate
a repository for yourself) answers the same way.

### Jobs

```bash
curl -s https://cidx.example.com/api/jobs/<job-id> -H "Authorization: Bearer $TOKEN"
```

The response (`JobStatusResponse` in `src/code_indexer/server/models/jobs.py`) has `job_id`, `operation_type`,
`status`, `created_at`, `started_at`, `completed_at`, `progress`, `result`, `error`, `username`, and while running
`current_phase` and `phase_detail`. `status` is one of `pending`, `running`, `completed`, `completed_partial`,
`failed`, `cancelled`, `resolving_prerequisites` or `interrupted` (`JobStatus` in
`src/code_indexer/server/repositories/background_jobs.py`). Poll until it is no longer `pending`, `running` or
`resolving_prerequisites`.

You see your own jobs; an admin sees all. A job you cannot see answers 404 `Job not found: <job-id>`.
`GET /api/jobs` lists jobs and `DELETE /api/jobs/{job_id}` cancels one.

## Errors

| Origin | Body |
|--------|------|
| an endpoint refuses the request (`HTTPException`: 400, 401, 403, 404, 409, 429, 503, ...) | `{"detail": "<message>"}`; a few endpoints put an object in `detail`, for example the elevation errors `{"detail": {"error": "elevation_required", ...}}` |
| request body or parameters fail validation (422) | `{"error": ..., "message": "Request validation failed. ...", "detail": [{"loc": ..., "msg": ..., "type": ..., "input": ...}], "correlation_id": ..., "timestamp": ..., "details": {"field_errors": [...], "error_count": N}}` |
| unhandled server error (500) | `{"error": ..., "message": "An internal server error occurred. ...", "correlation_id": ..., "timestamp": ...}` |

The global error middleware builds the 422 and 500 bodies (`src/code_indexer/server/middleware/error_handler.py`,
`error_formatters.py`); `HTTPException` responses keep FastAPI's `detail` format. Quote the `correlation_id` when
you report a 500: it identifies the request in the server logs. The `SUBSYSTEM-CATEGORY-NNN` codes in the
[error-code reference](error-codes/README.md) appear in server log lines, not in response bodies.
