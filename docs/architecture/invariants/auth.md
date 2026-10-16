# Auth Invariants

Rules for TOTP step-up elevation, token revocation and the maintenance endpoints. Operator view of the same
flows (login, MFA, elevation, throttling): [Login and Elevation](../../server/auth/login-and-elevation.md).
Index of all invariant groups: [README](README.md).

## TOTP step-up elevation

- Exactly three error codes: `totp_setup_required` (403), `elevation_required` (403), `elevation_failed` (401).
- Kill switch (enforcement off): `require_elevation()` (`src/code_indexer/server/auth/dependencies.py`) lets
  protected routes run with no elevation check. Only `POST /auth/elevate` answers 503
  `elevation_enforcement_disabled` (`src/code_indexer/server/auth/elevation_routes.py`). Protected routes do not
  return 503.
- Recovery codes (10 per user by default, stored as HMAC-SHA256 digests, `src/code_indexer/server/auth/totp_service.py`)
  grant only the narrow `totp_repair` scope, never full scope.
- TOTP replay is prevented by an atomic compare-and-set on `last_used_otp_counter`.

## CLI elevation retry

`with_elevation_retry` wraps every `cidx admin users` and `cidx admin groups` command. On a 403 `elevation_required`
it prompts for a TOTP code, calls `POST /auth/elevate` and retries once. On `totp_setup_required` or
`elevation_failed` it exits non-zero without retrying. The error body is read through `body.get("detail", {})`
because FastAPI wraps `HTTPException` detail.

## JWT logout revocation

- Both logout routes in `src/code_indexer/server/web/routes.py`, `GET /admin/logout` (`web_router`, mounted at
  `/admin`) and `GET /user/logout` (`user_router`, mounted at `/user`; mounts in `routers/inline_routes.py`), add the
  token's `jti` to the blacklist with `get_token_blacklist().add(jti)`. `TokenBlacklist`
  (`src/code_indexer/server/app.py`) is database-backed, so a revoked `jti` is rejected by every worker and node.
- `_extract_jti_from_request` tries the `Authorization: Bearer` header, then the `cidx_session` cookie, and returns
  `None` on any decode error. The blacklist block is wrapped in `try/except`: a failure logs a WARNING and never
  blocks the redirect or the session clear.
- `blacklisted_at` is a numeric Unix timestamp, not an ISO string. Pruning goes through
  `TokenBlacklist.prune_expired(ttl_seconds)`, called by `DataRetentionScheduler._safe_prune_token_blacklist` with a
  TTL derived from the live `jwt_expiration_minutes`. The generic ISO-string `_cleanup_table` helper must not be used
  for this table.

## Maintenance endpoints are loopback-only

`POST .../maintenance/enter` and `POST .../maintenance/exit` are restricted to loopback callers (`127.0.0.0/8`,
`::1`, `::ffff:127.x.x.x`) by the `require_localhost` dependency. They exist for each node's local auto-updater; a
reverse proxy must not forward them. There are no MCP tools for entering or leaving maintenance mode. The read
endpoints are unaffected. Operator view: [Maintenance and Jobs](../../server/maintenance-and-jobs.md).
