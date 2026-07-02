# MCP Spoke Template

Starter template for adding a new MCP spoke service to the Lightspeed hub-and-spoke agent architecture. Clone this repo, run one script, and you have a fully wired NestJS Lambda ready to implement.

## Quick Start

```bash
git clone git@gitlab.com:lightspeeddms/private/project-templates/mcp-template1.git
cd mcp-template1
./scripts/rename.sh <servicename>
# → creates ../<servicename>-mcp/ (sibling of this repo)

cd ../<servicename>-mcp
npm install
```

**Example — Datadog:**
```bash
./scripts/rename.sh datadog
# → ../datadog-mcp/
```

## What You Get

A complete NestJS Lambda spoke with:

| Endpoint | Path | Auth |
|---|---|---|
| Health | `GET /health` | None |
| MCP server | `POST /v1/{source}/mcp` | Lambda Authorizer |
| MCP tools/list | `POST /v1/{source}/mcp/tools/list` | Lambda Authorizer |
| MCP tools/call | `POST /v1/{source}/mcp/tools/call` | Lambda Authorizer |
| Callback | `POST /v1/{source}/callback` | None (API GW IP restriction) |

## After Renaming

1. **`src/modules/{SOURCE}/{source}-api.service.ts`** — Add real API calls (HTTP/SDK). Wire credentials via `ConfigService` from `serverless.yml` env vars.

2. **`src/modules/{SOURCE}/services/{source}-mcp.service.ts`** — Replace the two example stubs in `TOOL_DEFINITIONS` with real tools. Register handlers in `this.handlers`.

3. **`src/modules/{SOURCE}/services/{source}-callback.service.ts`** — Implement `notify()` to post job results back to the platform (e.g., post a comment, update a ticket).

4. **`serverless.yml`** — Add SSM parameters for your API credentials to the `environment` block:
   ```yaml
   environment:
     SOURCENAME_API_KEY: ${ssm:/cicd/sourcename-mcp/api_key}
   ```

5. **Register the MCP** — Two independent registries must both be updated:

   **a) Deploy** the Lambda (`sls deploy --stage tools-test --region us-west-2`)

   **b) Register in agent-router DynamoDB** (`agent-mcp-servers-{stage}`) via the `McpRegistryController` REST API — no redeploy needed, cache invalidates immediately:
   ```bash
   curl -X POST https://<agent-router-api-gw>/<stage>/v1/agent-router/mcp-registry/<service-id> \
     -H "Authorization: Bearer <API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{"name":"My MCP","executablePath":"https://<api-gw>/<stage>/v1/<source>/mcp","timeoutSeconds":1800}'
   ```

   **c) Seed the batch server** `mcp_registry` PostgreSQL table via SSM port-forward to the batch server:
   ```bash
   POST http://localhost:<local-port>/mcp-registry
   { "id": "<source>-mcp", "executablePath": "https://...", "timeoutSeconds": 1800 }
   ```

   **d) Add to agent-router playbook** — edit `playbooks/playbooks/configs/<playbook>.yml`, add the server id under `mcp.servers[]`, then push the playbook via the playbook API.

## Development

```bash
npm install
npm run start:dev           # Local NestJS on :3000
npm test                    # Unit tests
npm run test:e2e            # End-to-end tests (requires .env.test)
npm run lint                # ESLint
npm run tsc                 # TypeScript check
npm run serverless-build:dev  # Package for Lambda (stage=dev, region=us-west-2)
```

**Local MCP testing** (connects Claude Code directly to the running service):
```bash
npm run start:dev &
node mcp-server-wrapper.js   # stdio MCP bridge → http://localhost:3000
```

## Architecture

```
Claude Batch Server
      │
      │  MCP JSON-RPC  POST /v1/{source}/mcp
      ▼
  SourcenameMcpController
      │
      ▼
  SourcenameMcpService         ←── TOOL_DEFINITIONS + handler map
      │
      ▼
  SourcenameApiService         ←── all external HTTP/SDK calls


Agent Router  ──  POST /v1/{source}/callback
      │
      ▼
  SourcenameCallbackService    ←── posts result back to platform
```

Authentication is enforced at the API Gateway layer by the `general-lambda-authorizer`. Requests that reach the Lambda have already been validated — `LsAuthGuard` always returns `true`.

## File Map

| File | What to change |
|---|---|
| `src/modules/SOURCENAME/services/sourcename-mcp.service.ts` | Replace `TOOL_DEFINITIONS` stubs; implement handlers |
| `src/modules/SOURCENAME/services/sourcename-api.service.ts` | Implement `getResource()`, `searchResources()` with real API calls |
| `src/modules/SOURCENAME/services/sourcename-callback.service.ts` | Implement `notify()` |
| `serverless.yml` | Add SSM env vars; update function name/description |
| `serverless_helper.js` | Update `credentialsSecretName` if needed (`// TEMPLATE:` comments mark every spot) |
| `test/app.e2e-spec.ts` | Already templated; passes after rename |

Files you should **not** need to modify:
- `src/modules/SOURCENAME/controllers/` — routing is generic
- `src/modules/SOURCENAME/utils/json-rpc.utils.ts` — JSON-RPC builders
- `src/common/guards/lsauth.guard.ts` — passthrough guard
- `src/app.module.ts`, `src/app.ts`, `src/serverless.ts` — boilerplate wiring
- `webpack.*.js`, `tsconfig*.json`, `nest-cli.json` — build config

## Pre-commit Hooks

Husky runs on every commit:
- `prettier` — format check
- `tsc` — type check
- `eslint` — lint
- `jest --coverage` — coverage must stay ≥ 50% statements/functions/lines, 40% branches

And on push: `npm test`.

## Architecture Context

**Hub-and-spoke:** `agent-router` receives webhooks (Jira, GitLab, Slack, etc.), creates a job, and sends it to `claude-batch-server`. The batch server starts MCP servers (spokes) and gives Claude tools to act on the source system. Each spoke is a NestJS Lambda with two surfaces: an MCP endpoint for tool calls, and a callback endpoint for receiving completed job results.

**URL convention:** The agent-router derives the callback URL from the MCP URL by replacing `/mcp` → `/callback`. So `SOURCENAME_MCP_URL` set in SSM (`/cicd/agent-router/<source>-mcp-url`) automatically gives the router both endpoints.

**Auth:** The `general-lambda-authorizer` Lambda sits in front of all API Gateway routes. It validates the `Authorization: Bearer` token against Secrets Manager. The callback route is intentionally excluded from auth — the unpublished URL is the access control. `LsAuthGuard` in the NestJS app is a passthrough; real enforcement happens before the Lambda is invoked.

**Tool flow:** `SourcenameMcpService.getToolDefinitions()` → batch server discovers tools via `tools/list` → Claude calls `tools/call` → `callTool()` dispatches to a handler → `SourcenameApiService` makes the actual API call → result returned as JSON-RPC 2.0 response.
