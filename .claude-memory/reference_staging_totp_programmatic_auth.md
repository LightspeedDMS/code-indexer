# Programmatic TOTP / MFA Auth Against a CIDX Server (headless, front-door)

> **STALE AS OF 2026-09-08 — VERIFY BEFORE RELYING ON THIS.** The procedure below depends on a
> `TOTP_CODE` / `totp` / `secret_key` entry in `.local-testing`. **Those keys no longer exist.** A
> direct check that day found ONLY `E2E_ADMIN_USERNAME` and `E2E_ADMIN_PASSWORD` (lines 258-259);
> `grep -nE '^(TOTP_CODE|totp|secret_key)' .local-testing` returns nothing, so `TOTP_CODE` evaluates
> empty and `/auth/mfa/verify` fails. `.local-testing`'s own section 12 (line ~500) now states the
> current truth: "REST auth requires MFA/TOTP ... headless `curl` cannot complete a login. Use the
> MCP front door for cluster work."
>
> **What to do instead:** use the `mcp__claude_ai_Neo_Staging__*` MCP tools for all cluster work.
> If a NEWLY DEPLOYED tool is missing from that tool list, the MCP client bound its list at session
> start and needs a reconnect — that is a client-session limitation, not a server defect. Confirm
> the tool really is deployed with `get_tool_categories` before concluding anything is broken.
>
> **Do NOT retry-loop the login** trying credential variations: the admin account is MFA-protected
> and repetition risks lockout. See [[feedback_never_retry_loop_auth_endpoint]]. Keep the rest of
> this note only as a record of the handshake CONTRACT (it is still accurate about the endpoints
> and shapes) for whenever a TOTP secret is restored to the file.

**Do this ALWAYS, autonomously — never ask the user to authenticate or run the staging E2E.** The admin account has MFA enabled, so a bare `POST /auth/login` deliberately returns `{"mfa_required": true, "mfa_token": "..."}` with NO access token. You complete a second factor yourself.

## The credentials file stores the TOTP as a SHELL COMMAND, not a static seed

`.local-testing` (project root, gitignored — declare `SECRET_FILE` before reading) is a **shell-sourceable env file**. The TOTP entry is a command-substitution, e.g. `TOTP_CODE=$(... pyotp ...)` — it *computes a live 6-digit code at source time*. The real base32 seed lives INSIDE that command.

- Do NOT try to parse `totp` / `TOTP2` / `TOTP_CODE` as static base32/base64/hex — you will fail every time (this is the recurring "dance"). The `totp` key is prose; the `TOTP*` keys are `$(...)` expressions (the `(` `)` chars are the tell).
- Correct way to get a fresh code: let the shell evaluate the assignment lines:
  ```bash
  set -a
  eval "$(grep -E '^(E2E_ADMIN_USERNAME|E2E_ADMIN_PASSWORD|totp|secret_key|TOTP_CODE)=' .local-testing)"
  set +a
  # $TOTP_CODE now holds a fresh 6-digit code (regenerated each source; use within ~30s)
  ```
- Base URL: parse the `Public URL` line from `.local-testing` (the `E2E_SERVER_URL` key points elsewhere and 401s on login — do not use it for the front door).

## Two-step handshake (Bug #1150 contract — mirror exactly)

1. `POST /auth/login` JSON `{"username","password"}` -> `200 {"mfa_required": true, "mfa_token": "<tok>"}` (single-use, consume-first — fetch a fresh one per attempt).
2. `POST /auth/mfa/verify` JSON `{"mfa_token": "<tok>", "totp_code": "<6-digits>"}` -> `200 {"access_token": ..., "token_type": "bearer"}`. Wrong/expired code -> 401; MFA service down -> 503.
3. Use `Authorization: Bearer <access_token>` for all subsequent front-door calls. Token expiry ~10 min.

Notes: auth is JSON body (not form-urlencoded); endpoint is `/auth/login` (not `/admin/login`). `/health` needs the Bearer token and returns `{"version","status"}` — the fastest deployed-version check. Admin write endpoints additionally need TOTP step-up elevation (`/auth/elevate`), a separate window — see [[feedback_server_e2e_front_door_only]].

## Leak-safe discipline

Never echo the computed code, token, seed, password, or URL to stdout. Do login+verify inside one script that reads creds from env/file in-process and prints only HTTP codes + non-secret fields (version/status). Related: [[feedback_no_secrets_in_memory]].
