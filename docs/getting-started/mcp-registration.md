# Connecting an MCP client to a CIDX server

A CIDX server exposes its search and repository tools over the Model Context Protocol (MCP). This guide covers the
endpoints, how clients authenticate, and how to register the server in Claude Code. It assumes a running server
(see [Server deployment](../server/deployment.md)) and an account on it. For a purely local setup with no server,
see [teach-ai](teach-ai.md) instead.

In the examples, `https://cidx.example.com` stands for your server's base URL.

## What the server offers

| Endpoint | Authentication | Tools listed |
|----------|---------------|--------------|
| `POST /mcp` (JSON-RPC 2.0, MCP Streamable HTTP; `GET /mcp` for SSE) | Required; see below. Unauthenticated requests get HTTP 401 with a `WWW-Authenticate` header that points to the OAuth metadata. | Every tool the account's role and configuration allow. |
| `POST /mcp-public` | None required. | Without a session cookie: only `authenticate` (takes `username` and an API key `cidx_sk_...`). A successful `authenticate` sets a `cidx_session` cookie; later requests carrying it see the role-filtered tool list. |

The server accepts MCP protocol versions `2024-11-05`, `2025-03-26` and `2025-06-18`
(`SUPPORTED_PROTOCOL_VERSIONS` in `src/code_indexer/server/mcp/protocol.py`). One tool document per tool lives in
`src/code_indexer/server/mcp/tool_docs/` (148 at the time of writing), grouped as admin, cicd, depmap, files,
git, guides, memory, repos, scip, search, ssh and tracing.

What an account can call depends on its role:

| Role | Permissions |
|------|------------|
| `normal_user` | query repositories, read repository status, activate and sync its own workspaces |
| `power_user` | the above plus repository writes (file edits, commits, pushes) |
| `admin` | the above plus user, golden-repository and destructive operations |

Some write and credential tools additionally require TOTP step-up elevation when the server enforces it; see
[Login and elevation](../server/auth/login-and-elevation.md).

## Authentication on `/mcp`

`get_current_user_for_mcp` in `src/code_indexer/server/auth/dependencies.py` tries, in order:

1. **MCP credentials**: `Authorization: Basic base64(client_id:client_secret)`, or `client_id` and
   `client_secret` fields in the JSON body (`client_secret_post`). Recommended for long-lived clients such as
   Claude Code. A request authenticated this way gets a full elevation window automatically.
2. **Bearer token**: `Authorization: Bearer <token>`, where the token is one of
   - a personal API key (prefix `cidx_sk_`);
   - an OAuth 2.1 access token issued by the server's own authorization server (see below);
   - a JWT from `POST /auth/login`. These expire after `jwt_expiration_minutes` (default 10), so they suit
     scripts, not a registered client.
3. **Web UI session cookie** (`cidx_session`).

### Creating MCP credentials

An MCP credential is a `client_id` (prefix `mcp_`) and a `client_secret` (prefix `mcp_sec_`) owned by one
account. The secret is shown once, at creation.

- **Web UI**: the MCP Credentials page, `/user/mcp-credentials` for every user or `/admin/mcp-credentials` for
  admins.
- **REST**:

  ```bash
  TOKEN=$(curl -s -X POST https://cidx.example.com/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username": "alice", "password": "..."}' | jq -r .access_token)

  curl -s -X POST https://cidx.example.com/api/mcp-credentials \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name": "alice-laptop"}'
  ```

  Response (HTTP 201): `client_id`, `client_secret`, `credential_id`, `name`, `created_at`, `message`.
  Admins can create a credential for another account with `POST /api/admin/users/{username}/mcp-credentials`.
  `GET /api/mcp-credentials` lists your credentials and `DELETE /api/mcp-credentials/{credential_id}` revokes one.

Creating or deleting a credential is a self-service credential change: when TOTP elevation enforcement is on, the
caller needs an active elevation window first (`require_self_elevation`). Without one the server answers HTTP 403
`elevation_required` (or `totp_setup_required` if the account has no TOTP yet).

## Registering the server in Claude Code

Build the Basic header from the credential and add the server with `claude mcp add`:

```bash
AUTH=$(printf '%s' "$CLIENT_ID:$CLIENT_SECRET" | base64 | tr -d '\n')

claude mcp add --transport http --scope user cidx https://cidx.example.com/mcp \
  --header "Authorization: Basic $AUTH"
```

`--header` accepts several values, so it consumes every following argument up to the next option: put it after the
name and URL (or follow it with another option). Written before the name, the command fails with
`missing required argument 'name'`.

`--scope` decides where Claude Code stores the entry (observed with Claude Code 2.1.291):

| Scope | Stored in | Visible in |
|-------|-----------|-----------|
| `local` (default) | `~/.claude.json`, under the current project's entry | this project, for you only |
| `user` | `~/.claude.json`, top-level `mcpServers` | every project, for you only |
| `project` | `.mcp.json` in the project directory | everyone who uses the project's `.mcp.json` |

Do not put a credential header in `project` scope: `.mcp.json` is meant to be committed. The resulting `user`
entry looks like this:

```json
{
  "mcpServers": {
    "cidx": {
      "type": "http",
      "url": "https://cidx.example.com/mcp",
      "headers": { "Authorization": "Basic <base64 of client_id:client_secret>" }
    }
  }
}
```

Check and remove the registration with `claude mcp get cidx` (exit 0 when registered, 1 when not) and
`claude mcp remove cidx --scope user`. Claude Code's own options may change between releases; `claude mcp add --help`
is authoritative.

## Clients that use OAuth

Clients that implement MCP's OAuth flow can be given only the URL `https://cidx.example.com/mcp`. The server
provides:

| Endpoint | Purpose |
|----------|---------|
| `/.well-known/oauth-protected-resource` | Protected-resource metadata (RFC 9728), naming the authorization server. |
| `/.well-known/oauth-authorization-server` (also served at `/.well-known/oauth-authorization-server/mcp`) | Authorization-server metadata (RFC 8414). |
| `POST /oauth/register` | Dynamic client registration. |
| `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke` | Authorization code with PKCE (`S256`), refresh token and client credentials grants. |

The user signs in through the browser (password with MFA, or SSO) during the authorization step. The URLs in the
metadata are built from the server's issuer URL (`CIDX_ISSUER_URL` in the server's environment, default
`http://localhost:8000`). If the issuer is wrong, OAuth clients are sent to the wrong host; operators set it during
installation (`cidx install-server --issuer-url ...`, see [Server deployment](../server/deployment.md)).

## Server self-registration (`cidx-local`)

When the server itself runs Claude CLI jobs (for example dependency-map analysis), it registers itself in that
Claude CLI under the name `cidx-local`, so treat that name as reserved. `MCPSelfRegistrationService`
(`src/code_indexer/server/services/mcp_self_registration_service.py`):

1. checks `claude --version`, then `claude mcp get cidx-local`;
2. reuses the stored credential if it still exists, otherwise generates one for `admin` named `cidx-local-auto`
   and stores it in the runtime configuration (`mcp_self_registration`, in the server database);
3. runs `claude mcp add --transport http --header "Authorization: Basic ..." --scope user cidx-local http://localhost:<port>/mcp`.

The check runs once per server process. If the Claude CLI is missing, the server logs
`Claude CLI not available - skipping MCP self-registration` and retries on a later job.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| HTTP 401 on every call | The `Authorization` header is missing or wrong. Re-encode `client_id:client_secret` without a trailing newline (`printf '%s'`, not `echo`). A revoked credential also returns 401. |
| HTTP 403 `Non-SSO accounts are restricted to Web UI access only` | The server restricts non-SSO accounts; use an SSO account. |
| HTTP 403 `elevation_required` when creating a credential | Complete TOTP elevation first (Web UI or `POST /auth/elevate`). |
| Tools list contains only `authenticate` | You are on `/mcp-public` without a session. Use `/mcp`, or call `authenticate` first. |
| Expected tools are missing | The account's role does not allow them, or the tool depends on a feature the server has turned off. |
| Is the server up? | `curl https://cidx.example.com/healthz` needs no authentication and returns only a status. `/health` returns details and requires authentication. |

A direct check of a credential, without Claude Code:

```bash
curl -s -X POST https://cidx.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic $AUTH" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}' | jq '.result.tools | length'
```

A number greater than 0 means the credential works.
