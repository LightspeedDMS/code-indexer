# MCP Spoke Template — CLAUDE.md

Template for MCP spoke services in the Lightspeed hub-and-spoke agent architecture. See `documentation/agent/AGENT_ARCHITECTURE.md` and `documentation/agent/MCP_SERVER_REGISTRATION.md` for the full system context.

---

## Instantiating the Template

```bash
./scripts/rename.sh <servicename>
# → creates ../<servicename>-mcp/ with all SOURCENAME placeholders replaced
```

The script handles three casing variants:
- `SOURCENAME` → `DATADOG` (uppercase, used in env var names and error strings)
- `Sourcename` → `Datadog` (PascalCase, used in class names)
- `sourcename` → `datadog` (lowercase, used in paths, file names, module names)

---

## Implementation Order

After running the rename script and `npm install`:

1. **Define tools** in `TOOL_DEFINITIONS` inside `{source}-mcp.service.ts` — name, description, inputSchema
2. **Register handlers** in `this.handlers` map (constructor) — one entry per tool
3. **Implement handlers** as private `handleXxx()` methods
4. **Implement API calls** in `{source}-api.service.ts` — all external HTTP/SDK calls go here
5. **Implement `notify()`** in `{source}-callback.service.ts` — posts job results back to the platform
6. **Add credentials** to `serverless.yml` environment block as SSM parameters
7. **Register the MCP** — follow the 5-step checklist in `MCP_SERVER_REGISTRATION.md`

---

## Architecture Rules (must not break)

| Layer | May call | Must NOT call |
|---|---|---|
| Controller | Service only | ApiService, HTTP clients |
| MCP Service | ApiService | HTTP clients directly |
| ApiService | HTTP clients, SDK | Business logic, Services |
| CallbackService | ApiService | Controllers |

- Tool errors → `jsonRpcToolError()` (HTTP 200, `isError: true`) — never throw from `callTool()`
- Log field names only, never values: `Object.keys(credentials)`
- `ConfigService` for all env vars — no `process.env` direct access outside bootstrap

---

## Commands

```bash
npm run build                  # Compile TypeScript
npm run start:dev             # Local dev on :3000
npm run serverless-build:dev  # Package for Lambda (stage=dev, region=us-west-2)
npm test                      # Unit tests
npm run test:e2e              # E2E tests (requires .env.test)
npm run lint                  # ESLint
npm run tsc                   # TypeScript check
```

---

## Step 5 Checklist (before every commit)

- [ ] `npm run lint` — zero warnings
- [ ] `npm run tsc` — zero errors
- [ ] No `eslint-disable`, `@ts-ignore`, or `@ts-expect-error`
- [ ] Every new tool/handler has a unit test
- [ ] Coverage ≥ 50% statements/functions/lines, 40% branches

---

## File Map

| File | Purpose | Modify? |
|---|---|---|
| `src/modules/SOURCENAME/services/sourcename-mcp.service.ts` | TOOL_DEFINITIONS + handler dispatch | **Yes — add tools here** |
| `src/modules/SOURCENAME/services/sourcename-api.service.ts` | External API client | **Yes — implement calls** |
| `src/modules/SOURCENAME/services/sourcename-callback.service.ts` | Posts results back to platform | **Yes — implement notify()** |
| `serverless.yml` | Lambda config, env vars, IAM | **Yes — add SSM params** |
| `serverless_helper.js` | TF-managed API GW wiring | Yes if needed (`// TEMPLATE:` marks spots) |
| `src/modules/SOURCENAME/controllers/` | JSON-RPC routing | No — generic |
| `src/modules/SOURCENAME/utils/json-rpc.utils.ts` | JSON-RPC builders | No |
| `src/common/guards/lsauth.guard.ts` | Passthrough guard (auth at API GW) | No |
| `src/app.module.ts`, `src/app.ts`, `src/serverless.ts` | NestJS + Lambda bootstrap | No |
| `webpack.*.js`, `tsconfig*.json`, `nest-cli.json` | Build config | No |
| `mcp-server-wrapper.js` | stdio MCP bridge for local dev | No |

---

## serverless_helper.js — TEMPLATE markers

Search for `// TEMPLATE:` in `serverless_helper.js` — there are 7 spots:
1. Top of file — `credentialsSecretName` pattern
2. Callback path, MCP URL description, and export name (`sourcename`)
3. CF resource — Lambda function logical name for API GW permission (`SOURCENAMEMcpLambdaFunction`)
4. SSM key, description, and path (`sourcename`)
5. Callback path (`/v1/sourcename/callback`)
6. CF output description, path, and export name (`sourcename`)
7. Bottom — `credentialsSecretName` string value

All are handled automatically by the rename script; check them if you need non-standard secret naming.
