# TOTP Step-Up Elevation

This document captures the TOTP step-up elevation invariants extracted from project CLAUDE.md. It defines server-side `ElevatedSessionManager` semantics (Story #923) and the CLI-side auto-retry helper (Story #980) that together implement step-up admin elevation across both REST/MCP clients and the `cidx` CLI.

## TOTP Step-Up Elevation (Epic #922 / Story #923)

**ElevatedSessionManager** (`src/code_indexer/server/auth/elevated_session_manager.py`):
- Dual-backend (SQLite solo / PostgreSQL cluster), mirrors `MfaChallengeManager`
- Atomic touch: PostgreSQL `UPDATE...WHERE last_touched_at > cutoff RETURNING`; SQLite `BEGIN EXCLUSIVE`
- session_key = JWT `jti` (Bearer) OR `cidx_session` cookie (Web UI)
- Rolling 5-min idle timeout, 30-min absolute max age (both runtime-configurable via Web UI Config Screen)
- `INSERT ... ON CONFLICT (session_key) DO UPDATE` for atomic re-elevation (Codex M1)
- SQLite db lives at `~/.cidx-server/elevated_sessions.db` (NOT tempfile — survives restarts)

**Three error codes** (NEVER refactor to two or four):
- `totp_setup_required` (403, with `setup_url`) — admin has no TOTP secret enabled
- `elevation_required` (403) — no active elevation window for this session
- `elevation_failed` (401) — wrong code / replay / expired

**Kill switch returns HTTP 503 NOT 403** when `elevation_enforcement_enabled=false`. 403 misleadingly implies "forbidden"; 503 correctly signals "feature administratively off" (Codex M4/M12).

**Recovery code narrow elevation**: 10 codes generated at TOTP registration, stored as bcrypt hashes in `totp_recovery_codes` table (separate table, not column). Recovery code grants `scope=totp_repair` window — usable ONLY for TOTP reset/regenerate/disable. Full-scope endpoints reject. `verify_recovery_code` uses atomic CAS via single `UPDATE ... WHERE used_at IS NULL` (Codex M1) — no TOCTOU race.

**TOTP replay prevention**: `last_used_otp_timestamp` column on totp_secrets table. Atomic CAS rejects same-window replay (Codex C1). `verify_enabled_code()` rejects unactivated secrets (Codex C4).

**Rate limiting**: `POST /auth/elevate` chains through `login_rate_limiter` (per-IP+username key) — 429 when locked out, counter cleared on success (Codex H3).

**Revocation hooks**: `revoke_all_for_username()` called on logout / password change / role change to immediately invalidate active windows (Codex H2).

**Cluster deployment order**:
1. Apply `022_elevated_sessions.sql` + `023_totp_replay_prevention.sql` to all nodes (additive `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` — harmless on old code)
2. Deploy new code to all nodes (kill switch OFF by default — no behavior change)
3. Confirm version on every node via `/health`
4. Flip `elevation_enforcement_enabled=true` in Web UI Config Screen (hot-reload via 30s reload thread, no restart needed)

Files: `src/code_indexer/server/auth/elevated_session_manager.py`, `src/code_indexer/server/auth/elevation_routes.py`, `src/code_indexer/server/web/elevation_web_routes.py`, `src/code_indexer/server/auth/dependencies.py::require_elevation`.

## CLI Elevation Retry (Story #980)

CLI admin commands in remote mode auto-elevate when the server returns 403 `elevation_required`. The retry helper is in `src/code_indexer/api_clients/elevation.py`.

**Pattern** (`with_elevation_retry`): try API call → on `ElevationRequiredError` → prompt user for TOTP → call `POST /auth/elevate` → single retry. On `totp_setup_required` or `elevation_failed`: print clear error and `sys.exit(1)` (no retry loop).

**Error detection**: `AdminAPIClient` and `GroupAPIClient` both raise `ElevationRequiredError` when they see `{"detail": {"error": "elevation_required"}}` or `{"detail": {"error": "totp_setup_required"}}` in a 403 response. FastAPI wraps `HTTPException(detail={...})` as `{"detail": {...}}` — always unwrap via `body.get("detail", {})`.

**Scope**: All `cidx admin users` commands (create, list, show, update, delete, change-password) and all `cidx admin groups` commands are wrapped with `with_elevation_retry`.

Files: `src/code_indexer/api_clients/elevation.py`, `src/code_indexer/api_clients/admin_client.py`, `src/code_indexer/api_clients/group_client.py`, `src/code_indexer/cli.py` (admin users + groups sections).
