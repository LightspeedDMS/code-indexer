# Login and Elevation

How users sign in to a CIDX Server, how TOTP multi-factor authentication (MFA) works at sign-in, how the server
throttles repeated attempts, and how TOTP step-up elevation protects administrative operations.

Related: [Accounts and Access](accounts-and-access.md) (users, roles, API keys, MCP credentials),
[OIDC](oidc.md) (single sign-on), [Admin Guide](../admin-guide.md).

## Sign-in methods

| Method | Front door | Notes |
|--------|------------|-------|
| Password | REST `POST /auth/login` (JSON `username`, `password`), Web UI `/login`, OAuth `/oauth/authorize` | Subject to the login throttle below; MFA challenge when the account has TOTP enabled |
| SSO (OpenID Connect) | Web UI `/login/sso`, provider returns to `/auth/sso/callback` | See [OIDC](oidc.md); MFA challenge when the account has TOTP enabled |
| Personal API key | `Authorization: Bearer cidx_sk_...` on REST, or the MCP `authenticate` tool | See [Accounts and Access](accounts-and-access.md#api-keys) |
| MCP credential | `Authorization: Basic base64(client_id:client_secret)` on `/mcp` | See [MCP registration](../../getting-started/mcp-registration.md) |

## TOTP MFA

MFA is enabled per account. Each user enrols an authenticator app on their own MFA page: `/admin/mfa/setup` for
administrators, `/user/mfa/setup` for other roles. Enrolment issues 10 single-use recovery codes; they can be
regenerated from the same page (`.../mfa/recovery-codes`).

### Password plus MFA

When the password is correct and the account has MFA enabled, no token is issued yet. The REST door answers:

```json
{"mfa_required": true, "mfa_token": "<challenge token>"}
```

The client completes the login with exactly one of `totp_code` (6 digits) or `recovery_code`:

```bash
curl -s -X POST http://localhost:8000/auth/mfa/verify \
  -H "Content-Type: application/json" \
  -d '{"mfa_token": "<challenge token>", "totp_code": "123456"}'
```

The Web UI shows a challenge page (submitted to `/admin/mfa/challenge/verify`); the OAuth flow uses
`/oauth/mfa/verify`.

A challenge:

- is valid for 5 minutes;
- must be answered from the same client IP address that passed the password;
- is single-use: a wrong code ends it (REST answers HTTP 401 `Invalid MFA code`), and the user signs in again.

### SSO plus MFA

After a successful SSO sign-in, an account with MFA enabled gets the same challenge page before a session or OAuth
authorization code is issued. A challenge started by SSO counts its codes under a separate throttle key (see below),
so failed password attempts on an account never block that account's SSO sign-in, while wrong codes after SSO are
still throttled.

## Login throttle

Code: `src/code_indexer/server/auth/login_rate_limiter.py`.

The throttle covers:

- password attempts at REST `/auth/login`, Web `/login` and OAuth `/oauth/authorize`;
- every code (TOTP or recovery) answered at a login challenge;
- TOTP step-up attempts (see [Step-up throttle](#step-up-throttle)).

### How it counts

- **Key.** The username as typed. Unknown usernames are throttled exactly like real ones. Each key lives in one
  scope: password logins and their challenges (`login`), challenges started by SSO (`sso-mfa`), and step-up
  (`stepup`). A scope never throttles another scope's key.
- **Reserve, then check.** Each attempt is reserved in the throttle before the password or code is checked, so
  concurrent requests cannot exceed the allowance.
- **Schedule.** Within the counting window the first four attempts are admitted freely. The 5th admitted attempt
  starts a backoff window of 5 seconds, and each further admitted attempt doubles it, up to 120 seconds:

  | Admitted attempt | Window it starts |
  |------------------|------------------|
  | 5th | 5 s |
  | 6th | 10 s |
  | 7th | 20 s |
  | 8th | 40 s |
  | 9th | 80 s |
  | 10th and later | 120 s |

- **During a window** every attempt for that key is refused without being checked, a correct password included. A
  refused attempt is not counted.
- **Forgetting.** 15 minutes without an attempt forget the count. A completed login (password alone when no MFA is
  enrolled, or password and code) clears the key. A correct password with MFA still pending clears nothing.
- **No lock state.** There is no account lock and no unlock step: every window ends on its own. While a username is
  throttled its owner can still authenticate with an API key, MCP credentials or SSO.

State is in the database so every worker process and cluster node sees it: the `login_throttle` table in
`cidx_server.db` on a standalone server, or in PostgreSQL in a cluster.

### What clients see

| Situation | REST and Web response |
|-----------|-----------------------|
| Attempt refused during a backoff window | HTTP 429, `Too many attempts, try again in N seconds.`, header `Retry-After: N` |
| Throttle store cannot take the reservation | HTTP 503, `Login is busy, try again shortly.`, header `Retry-After: 1` |

At a login challenge, a refused code keeps the challenge: the user can answer it once the window ends (while the
challenge is still valid).

The 503 answer applies to a standalone server, where the throttle state is in SQLite: it appears when the server
process already took 10 new reservations in the last second (bursts of 10), or when the SQLite write lock is not
available within 2 seconds. The password or code is not checked in that case. Refusals during a backoff window do
not use that budget. In a cluster the throttle state is in PostgreSQL and this per-process cap does not apply.

REST `/auth/login` and the MCP `authenticate` tool also apply a per-username request bucket before the throttle:
10 attempts, refilled at one every 6 seconds, with a successful login refunding its attempt. Beyond it, REST
answers HTTP 429 `Too many login attempts. Please try again later.` with `Retry-After`, and `authenticate` returns
the tool result `{"success": false, "error": "Rate limit exceeded. Try again in N seconds", "retry_after": N}`.

### Audit

A refused attempt writes no audit row. The admitted attempt that starts a backoff window is audited with the
reason `rate_limited`; other failed attempts carry their own reason (for example a wrong code at a challenge is
`mfa_code_invalid`).

## Step-up elevation

Step-up elevation is a short, time-boxed window that a signed-in user opens by entering a current TOTP code. When
enforcement is on, the server requires an open window for:

- administrative mutations in the Web UI, REST API and MCP tools, and some sensitive administrative reads (see the
  [Admin Guide](../admin-guide.md#elevation-policy));
- self-service credential changes by any user: creating or deleting their own API keys, MCP credentials and git
  credentials (Web UI and MCP `configure_git_credential` / `delete_git_credential`).

### Settings

Runtime settings, Web UI, Configuration, TOTP Step-Up Elevation (section `totp_elevation`):

| Setting | Default | Allowed | Meaning |
|---------|---------|---------|---------|
| `elevation_enforcement_enabled` | `false` | `true` / `false` | Master switch (kill switch when `false`) |
| `elevation_idle_timeout_seconds` | `300` | 60 to 3600, not above the maximum age | The window closes after this much time without a protected request |
| `elevation_max_age_seconds` | `1800` | 300 to 7200, not below the idle timeout | Absolute lifetime of a window |

The switch is read from the runtime configuration on every request. Cluster nodes reload runtime configuration every
30 seconds. On a standalone server running more than one worker process, restart the server after changing it so
every worker uses the new value.

### Enforcement off (kill switch)

With `elevation_enforcement_enabled` set to `false`:

- every protected REST route, Web UI page and MCP tool runs with no elevation check;
- `POST /auth/elevate` answers HTTP 503 with error `elevation_enforcement_disabled`, and the MCP tool
  `elevate_session` returns the same error;
- `GET /auth/elevation-status` reports `{"elevated": false}`.

Turning enforcement off therefore does not make administrative operations fail; it removes the step-up requirement.

### Error codes

Exactly three error codes describe the elevation contract:

| Code | HTTP status | Meaning | What to do |
|------|-------------|---------|------------|
| `totp_setup_required` | 403 | The caller has no TOTP enrolled. The body carries `setup_url` (`/admin/mfa/setup` for administrators, `/user/mfa/setup` otherwise) | Enrol TOTP |
| `elevation_required` | 403 | No open window for this session, or the window's scope is too narrow | Elevate, then retry |
| `elevation_failed` | 401 | Returned by the step-up doors only: the code was wrong, already used, or expired | Enter a fresh code |

REST and Web routes return them as `{"detail": {"error": "<code>", ...}}`. MCP tools return them in the tool
result (`{"error": "<code>", "message": ...}`). In the Web UI, an `elevation_required` answer opens the TOTP prompt,
and the page `/admin/elevate` opens a window directly.

### Opening a window

REST, with the bearer token from `/auth/login`:

```bash
curl -s -X POST http://localhost:8000/auth/elevate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"totp_code": "123456"}'
```

Response: `elevated`, `elevated_until`, `max_until` (Unix timestamps) and `scope`. Send exactly one of `totp_code`
or `recovery_code`; neither or both answer HTTP 400 (`missing_code` / `ambiguous_code`).
`GET /auth/elevation-status` reports the current window without extending it. MCP clients call the
`elevate_session` tool.

The window belongs to the session that opened it (the bearer token, or the Web UI session cookie) and to the user
who opened it; another user's request cannot use it.

Requests to `/mcp` authenticated with an MCP credential, or with an access token issued by the server's OAuth flow,
count as elevated: the server opens a `full` window for them on each request. A password login token must elevate
explicitly.

### Scopes and recovery codes

| Code entered | Window scope | Accepted for |
|--------------|--------------|--------------|
| TOTP code | `full` | Every elevation-protected operation |
| Recovery code | `totp_repair` | Only MFA repair: disabling MFA and regenerating recovery codes |

A `totp_repair` window used on a `full` operation answers `elevation_required`. A user who lost their
authenticator signs in, elevates with a recovery code, and re-enrols. Each recovery code works once.

### Step-up throttle

Every step-up door (`POST /auth/elevate`, the Web form `/auth/elevate-form`, the Web UI prompt
`/auth/elevate-ajax`, MCP `elevate_session`) uses the login throttle schedule above, keyed by the username in the
`stepup` scope. A granted window clears the count.

| Situation | REST `POST /auth/elevate` | Web form and `/auth/elevate-ajax` | MCP `elevate_session` |
|-----------|---------------------------|-----------------------------------|-----------------------|
| Backoff window running | HTTP 429, error `rate_limited`, `Retry-After` | HTTP 429, `Retry-After`, message `Too many elevation attempts. Try again later.` (AJAX: `{"success": false, "error": "<message>"}`; the form re-renders with it) | `{"error": "rate_limited", ...}` |
| Throttle store busy | HTTP 503, error `busy`, `Retry-After` | HTTP 503, `Retry-After`, message `Elevation is busy, try again shortly.` (same shapes as above) | `{"error": "busy", ...}` |

Each step-up that checks a code writes one audit row, `elevation_granted` or `elevation_failed`, recording the scope
and whether a recovery code was used (never the code).

### When windows end

- Idle timeout or maximum age.
- A role change (`PUT /api/admin/users/{username}`) or a password change through the REST API
  (`PUT /api/users/change-password`, `PUT /api/admin/users/{username}/change-password`) ends all of that user's
  windows.

### CLI

In remote mode, the `cidx admin users` and `cidx admin groups` commands handle elevation: on `elevation_required`
they prompt for a TOTP code, call `/auth/elevate` and retry the command once. On `totp_setup_required` or
`elevation_failed` they print the error and exit with status 1. See the [admin CLI reference](../../reference/cli/admin.md).

## Turning enforcement on

1. Every administrator enrols TOTP at `/admin/mfa/setup` and stores the recovery codes. An administrator without
   TOTP receives `totp_setup_required` on every protected operation once enforcement is on.
2. In the Web UI, Configuration, TOTP Step-Up Elevation, set enforcement to Yes and save (this save is itself a
   protected operation only while enforcement is already on).
3. Check it: a protected call without a window answers 403 `elevation_required`; after `POST /auth/elevate` it
   succeeds.
