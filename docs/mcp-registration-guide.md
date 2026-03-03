# Registering CIDX Server as an MCP Server in Claude Code

This guide explains how to register a CIDX server instance as an MCP server
so that Claude Code can use CIDX tools (semantic search, SCIP intelligence,
code browsing) during interactive sessions and automated workflows like
dependency map analysis.

## Overview

The registration process has two steps:

1. **Generate MCP credentials** on the CIDX server (client_id + client_secret)
2. **Register the MCP server** in Claude Code's configuration (`~/.claude.json`)

Authentication uses HTTP Basic auth: the client_id and client_secret are
base64-encoded and sent as an `Authorization: Basic <token>` header on every
MCP request.

---

## Step 1: Generate MCP Credentials

MCP credentials are API-level credentials stored in the CIDX server database.
They are separate from the user's login password. Each credential has a
`client_id` (prefixed `mcp_`) and a `client_secret` (prefixed `mcp_sec_`).

There are three ways to generate credentials:

### Option A: Web UI

1. Log in to the CIDX server admin panel (e.g. `https://your-server:8000/admin/`)
2. Navigate to **MCP Credentials** page (`/admin/mcp-credentials`)
3. Click **Generate New Credential**
4. Optionally enter a name (e.g. "My Laptop", "CI Pipeline")
5. Copy the `client_id` and `client_secret` shown in the modal

The secret is displayed only once. Save it immediately.

### Option B: REST API

```bash
# 1. Authenticate to get a JWT token
TOKEN=$(curl -s -X POST https://your-server:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASSWORD"}' | jq -r '.access_token')

# 2. Create an MCP credential
curl -s -X POST https://your-server:8000/api/mcp-credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-workstation"}' | jq .
```

Response:

```json
{
  "credential_id": "cred-abc123",
  "client_id": "mcp_de387f5e1139f53414a1ff56c68f476f",
  "client_secret": "mcp_sec_8db2b89ab1d01eb898874af8d26c30c154c1bb...",
  "name": "my-workstation",
  "created_at": "2026-03-02T10:00:00Z"
}
```

### Option C: CIDX CLI (remote admin)

```bash
cidx admin mcp-credentials create --description "my-workstation" --json
```

This requires a configured CIDX CLI profile pointing to the server.

### Admin: Create credentials for other users

Admins can create credentials on behalf of any user:

```bash
curl -s -X POST https://your-server:8000/api/admin/users/USERNAME/mcp-credentials \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"auto-provisioned"}'
```

---

## Step 2: Register in Claude Code

### Option A: Claude CLI (recommended)

The `claude mcp add` command writes the entry to `~/.claude.json` for you:

```bash
# Build the base64 auth token
AUTH_TOKEN=$(echo -n "CLIENT_ID:CLIENT_SECRET" | base64)

# Register with user scope (available in all projects)
claude mcp add \
  --transport http \
  --header "Authorization: Basic $AUTH_TOKEN" \
  --scope user \
  cidx-local \
  http://localhost:8000/mcp

# Or register with project scope (only available in a specific project)
claude mcp add \
  --transport http \
  --header "Authorization: Basic $AUTH_TOKEN" \
  --scope project \
  cidx-production \
  https://your-server:8000/mcp
```

#### Scope options

| Scope | Storage location in `~/.claude.json` | Visibility |
|-------|--------------------------------------|------------|
| `user` | Top-level `mcpServers` object | All projects on this machine |
| `project` | `projects.{project_path}.mcpServers` | Only that project directory |
| `local` (default) | `.claude/settings.local.json` in project | Only that project, gitignored |

#### Verify registration

```bash
claude mcp get cidx-local
```

Returns exit code 0 if registered, non-zero if not found.

#### Remove registration

```bash
claude mcp remove cidx-local
```

### Option B: Manual edit of ~/.claude.json

If the Claude CLI is not available, you can edit `~/.claude.json` directly.

#### Structure

The file is a JSON object. MCP servers are stored in `mcpServers` objects at
different nesting levels depending on scope.

**User scope** (available everywhere):

```json
{
  "mcpServers": {
    "cidx-local": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Basic BASE64_ENCODED_CREDENTIALS"
      }
    }
  }
}
```

**Project scope** (only for a specific project directory):

```json
{
  "projects": {
    "/home/user/my-project": {
      "mcpServers": {
        "cidx-production": {
          "type": "http",
          "url": "https://your-server:8000/mcp",
          "headers": {
            "Authorization": "Basic BASE64_ENCODED_CREDENTIALS"
          }
        }
      }
    }
  }
}
```

#### Computing the Authorization header value

The header value is `Basic ` followed by a base64 encoding of
`CLIENT_ID:CLIENT_SECRET`:

```bash
echo -n "mcp_de387f5e1139f53414a1ff56c68f476f:mcp_sec_8db2b89ab1d01eb898874af8d26c30c154c1bb3b9d56dafe11fff7c33afad39b" | base64
```

The output is a single base64 string (no newlines) that goes into the
`Authorization` header as `Basic <that string>`.

#### Required fields for an HTTP MCP server entry

| Field | Value | Description |
|-------|-------|-------------|
| `type` | `"http"` | Transport protocol (not `sse` or `stdio`) |
| `url` | `"http[s]://host:port/mcp"` | Full URL to the CIDX MCP endpoint |
| `headers` | `{"Authorization": "Basic ..."}` | HTTP Basic auth with MCP credentials |

The MCP endpoint path is always `/mcp` (authenticated). There is also a
`/mcp-public` endpoint for unauthenticated access, but it has restricted
tool availability.

---

## Naming Conventions

| Name | Used by | Description |
|------|---------|-------------|
| `cidx-local` | Self-registration service | Auto-registered by CIDX server for local dep analysis |
| `cidx` | Manual project registration | Typical name for a remote/production server |
| `cidx-staging` | Manual registration | Staging environment |

You can use any name, but `cidx-local` is reserved for the automatic
self-registration process.

---

## Automatic Self-Registration (How the Server Does It Internally)

When the CIDX server runs dependency map analysis or golden repo description
jobs, it needs Claude CLI to have access to CIDX tools via MCP. The server
handles this automatically through the `MCPSelfRegistrationService`:

1. **Trigger**: First time `ClaudeCliManager._worker_loop()` processes a job,
   or first time `DependencyMapAnalyzer._run_claude_cli()` is called
2. **CLI check**: Runs `claude --version` to verify the CLI is installed
3. **Registration check**: Runs `claude mcp get cidx-local` to see if already
   registered
4. **Credential management**: Checks `~/.cidx-server/config.json` for stored
   credentials under the `mcp_self_registration` key. If none exist or the
   stored ones are no longer valid in the database, generates new ones via
   `MCPCredentialManager.generate_credential(user_id="admin", name="cidx-local-auto")`
5. **Registration**: Runs `claude mcp add --transport http --header "Authorization: Basic ..." --scope user cidx-local http://localhost:{port}/mcp`
6. **Caching**: Sets an in-memory flag so subsequent calls skip all checks
   for the lifetime of the process

Credentials are persisted in `~/.cidx-server/config.json`:

```json
{
  "mcp_self_registration": {
    "client_id": "mcp_...",
    "client_secret": "mcp_sec_..."
  }
}
```

This survives server restarts. On restart, the service re-validates the
stored credentials against the database and re-checks the Claude CLI
registration (the in-memory cache resets on restart, but the `claude mcp get`
check avoids re-registering if already present).

### Key source files

| File | Purpose |
|------|---------|
| `src/code_indexer/server/services/mcp_self_registration_service.py` | Core service: check, create, register |
| `src/code_indexer/server/auth/mcp_credential_manager.py` | Credential generation and validation |
| `src/code_indexer/server/utils/config_manager.py` | `MCPSelfRegistrationConfig` dataclass, persistent storage |
| `src/code_indexer/server/services/claude_cli_manager.py` | Worker loop trigger (line 642) |
| `src/code_indexer/global_repos/dependency_map_analyzer.py` | Dep analysis trigger (line 2022) |

---

## Troubleshooting

### "Claude CLI not available"

The server logs `Claude CLI not available - skipping MCP self-registration`.
This means `claude --version` failed. Install Claude Code CLI or ensure it
is on the PATH for the user running the CIDX server process.

### Credentials work but tools are not visible

Check that the MCP endpoint URL is correct (`/mcp` not `/mcp-public`) and
that the credential's user has the `query_repos` permission.

### Stale credentials after database reset

If the CIDX server database is reset, stored credentials in
`~/.cidx-server/config.json` become invalid. The self-registration service
detects this automatically and generates new ones. For manual registrations,
generate a new credential and update `~/.claude.json`.

### Verifying the registration works

```bash
# Check registration exists
claude mcp get cidx-local

# Test the MCP endpoint directly
AUTH_TOKEN=$(echo -n "CLIENT_ID:CLIENT_SECRET" | base64)
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic $AUTH_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools | length'
```

If the tools/list call returns a count (typically 100+), the registration
and credentials are working correctly.
