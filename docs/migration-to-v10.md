# Migrating from v9.x to v10.0.0

This guide covers the breaking changes and operator-facing migrations needed to
upgrade a CIDX deployment from v9.x to v10.0.0.

## Breaking Changes

### MCPB Subsystem Removed (Epic #756)

The MCP Bridge subsystem has been fully removed in v10.0.0 with no deprecation
window. The following are gone:

- `cidx-bridge` console-script entry point
- `cidx-token-refresh` console-script entry point
- `src/code_indexer/mcpb/` Python module (~2,053 LOC across 12 files)
- `tests/mcpb/` and `tests/installer/` test trees
- `install-mcpb.sh`, `scripts/setup-mcpb.sh`, `scripts/installer/mcpb-installer.nsi`
- `scripts/build_binary.py` MCPB-bundle build script
- `.github/workflows/release-mcpb.yml` CI workflow
- `docs/mcpb/` documentation tree (6 files, ~5,862 lines)

### Migration Path

Any MCP-aware client (Claude.ai, Claude Desktop, Claude Code, Codex CLI) should
connect directly to the CIDX server's native MCP endpoints:

- `https://your-server/mcp` - JWT-Bearer-authenticated (POST /auth/login first)
- `https://your-server/mcp-public` - unauthenticated (read-only operations only)

The `cidx-bridge` stdio-to-HTTP shim is no longer needed because every modern MCP
client supports streaming HTTP/SSE transports natively.

### Operator Action Required

If your deployment depended on `cidx-bridge` or `cidx-token-refresh` binaries:

1. **Client-side**: Reconfigure your MCP client to connect to `/mcp` directly
   (most clients have an "Add MCP server" UI that takes an HTTP URL + bearer token)
2. **Server-side**: No action needed - the server's native MCP endpoints have
   always existed; the bridge was a client-side compatibility shim
3. **CI/installation pipelines**: If you used `install-mcpb.sh`, switch to the
   standard `pipx install` or `pip install` paths from the README

Past GitHub Release artifacts that bundle `install-mcpb.sh` remain downloadable
per the repository's tag-immutability policy, but no new MCPB installer will be
built going forward.

## Architectural Changes (Non-Breaking)

These additions don't break existing operator workflows but add new capabilities
operators may want to enable.

### Research Assistant Security Hardening (Story #929)

The Research Assistant now uses a wrapper script `scripts/cidx-curl.sh` for HTTP
fetches instead of raw curl. The wrapper enforces a closed-set whitelist of safe
curl flags + DNS rebinding mitigation + always-on loopback + operator-configured
CIDR allowlist.

**Operator configuration**: Set `ra_curl_allowed_cidrs` in `config.json` under
`claude_integration_config`. Loopback (127.0.0.0/8 + ::1/128) is always allowed -
operators cannot disable. Restart cidx-server after change.

**Migration**: No action required. The wrapper architecture is transparent to
non-RA-using deployments. RA users get tighter exfil containment by default.

### Auto-Trigger Dep-Map Repair (Story #927)

Scheduled delta/refinement jobs can now optionally trigger a single repair pass
when anomalies are detected. Default-off; operator opts in via Web UI:
`dep_map_auto_repair_enabled = true`.

**Cluster operators**: Ensure `pg_pool` is wired into `DependencyMapService`
(handled automatically by lifespan startup when `storage_mode=postgres`). The
auto-repair feature will refuse to fire (loud ERROR) if cluster mode is detected
without an injected pg_pool - anti-fallback safeguard against per-node solo locks
firing duplicate jobs across nodes.

### Memory Store + Retrieval Pipeline (Stories #877, #883)

New MCP tools: `create_memory`, `edit_memory`, `delete_memory`, `search_code` (now
includes a `relevant_memories` field when search_mode is semantic or hybrid).

**Operator configuration**: `memory_retrieval_enabled` defaults to `true`. Kill
switch effective immediately (no restart). VoyageAI is required for memory
retrieval - Cohere reranker optional.

### TOTP Step-Up Elevation (Epic #922)

Admin operations require a TOTP elevation window. Rolling 5-min idle, 30-min
absolute max. Both runtime-configurable via Web UI Config Screen.

**Operator action**: Each admin user must enroll in TOTP via the user profile UI.
Recovery codes are generated at enrollment time - store securely (only displayed
once). Recovery codes grant a narrow `scope=totp_repair` window - usable ONLY for
TOTP reset/regenerate/disable.

**Kill switch**: `elevation_enforcement_enabled` defaults to `false` for backward
compatibility. Operators flip to `true` after all admins have enrolled. Hot-reload
via 30s reload thread.

### Maintenance Mode Localhost-Only (Story #924)

Maintenance mode write endpoints (`POST /api/admin/maintenance/enter` and `/exit`)
are now restricted to loopback callers. The MCP `enter_maintenance_mode` and
`exit_maintenance_mode` tools have been removed entirely.

**Operator impact**: Direct REST calls to `enter`/`exit` from non-loopback hosts
will be rejected (HTTP 403). Use the auto-updater (`--update-cidx-server-systemd`
flag) or run the curl from the server itself.

### cidx-meta Backup to Remote Git (Story #926)

Operators can now configure a remote git repository as a continuous backup target
for the cidx-meta directory. Configuration via Web UI; auto-conflict-resolution
via Claude CLI on push failures.

**Operator action**: Optional. To enable, set the remote URL via Web UI Config
Screen -> cidx-meta backup section. Requires SSH key access to the remote.

### Codex CLI Integration (Epic #843)

Codex GPT-5 background agents are now wired into the dep-map analyzer + description
refresh scheduler with persistent Basic-auth credentials.

**Operator action**: Optional. Codex CLI auto-install runs on every auto-updater
cycle (`enable_codex_cli=true` default). Configure auth via Web UI: API key mode
(set `OPENAI_API_KEY`), subscription mode (OAuth), or none.

### CLI Rerank + Embedder Provider Chain (Epic #689)

CLI now supports configurable rerank with Voyage + Cohere providers, plus an
embedder provider chain for resilience.

**Operator action**: None required. Existing CLI workflows continue to work.
Optional: configure rerank via the new CLI flags or Web UI.

## Stability Mitigations

These are enabled by default; operators don't need to act unless customizing.

- **Bug #897 mitigations** (default ON): `enable_malloc_trim` + `enable_malloc_arena_max`
  bootstrap-only flags reduce HNSW cache fragmentation. Disable in `config.json` if
  needed.
- **Bug #878 cleanup daemon**: single thread sweeps stale SQLite connections across
  all singletons every 60s. Always-on, lifecycle-managed by lifespan.
- **Bug #881/#894 omni caps**: `omni_wildcard_expansion_cap` (default 50) and
  `omni_max_repos_per_search` (default 50). Adjust via Web UI if your deployment
  has very large alias counts.

## Bug Fixes

This release also closes:

- **Bug #930**: dep-map dashboard `finalize 0.0s` for delta runs (display-only fix)
- **Bug #931**: refinement scheduler bootstrap gap (manual triggers now seed schedule;
  scheduled runs now register JobTracker entries)
- **Bug #932**: memory CRUD AttributeError (Protocol/real-class signature drift; the
  `is_write_lock_held` typo + 3 related drifts now caught by parametrized
  conformance test)
- **Bug #897**: HNSW cache fragmentation pinned RSS at ~23 GB after bulk lifecycle
  backfill - closed via glibc malloc_trim + MALLOC_ARENA_MAX
- **Bug #878**: FD/connection hygiene - single cleanup daemon replaces piggyback
  trigger
- **Bug #879**: CIDX_DATA_DIR IPC alignment for cidx-server vs cidx-auto-update
  running as different OS users
- See `CHANGELOG.md` for the complete list

## Upgrade Procedure

### Solo / Standalone Deployment

```bash
# 1. Stop the cidx-server
sudo systemctl stop cidx-server

# 2. Upgrade the package
pipx install --force git+https://github.com/LightspeedDMS/code-indexer.git@v10.0.0

# 3. Start the cidx-server (will run any pending DB migrations on startup)
sudo systemctl start cidx-server

# 4. Verify
curl -s http://localhost:8000/docs | head -5
sqlite3 ~/.cidx-server/logs.db "SELECT timestamp, level, message FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY timestamp DESC LIMIT 20;"
```

### Cluster Deployment (PostgreSQL)

Per the project's rolling-restart contract, all schema migrations are
backward-compatible (CREATE TABLE IF NOT EXISTS, ADD COLUMN, etc.). Order:

```bash
# 1. Apply DB migrations on one node (idempotent - others will skip)
ssh node1 "sudo systemctl stop cidx-server && pipx install --force ... && sudo systemctl start cidx-server"

# 2. Wait for node1 to come up healthy + complete migrations
# 3. Roll the remaining nodes one at a time
for node in node2 node3 ...; do
    ssh $node "sudo systemctl stop cidx-server && pipx install --force ... && sudo systemctl start cidx-server"
    # Wait for healthy before continuing
done

# 4. (Optional) Enable new feature flags via Web UI Config Screen:
#    - dep_map_auto_repair_enabled (default false; opt-in)
#    - elevation_enforcement_enabled (default false; opt-in after admin TOTP enrollment)
#    - ra_curl_allowed_cidrs (default []; loopback-only; expand to your cluster CIDR)
```

### Auto-Updater Path

Most operators use the auto-updater (`/api/admin/auto-update/*` endpoints), which
handles the upgrade idempotently:

```bash
# Trigger via REST or via the systemd auto-update timer
curl -X POST http://localhost:8000/api/admin/auto-update/run -H "Authorization: Bearer $TOKEN"

# Auto-updater performs: git pull -> pip install -> DeploymentExecutor.execute() -> systemctl restart
# All config-step changes are idempotent
```

## Post-Upgrade Verification

```bash
# 1. Server health
curl -s http://localhost:8000/docs | head -5
curl -s http://localhost:8000/health

# 2. No new ERROR/WARNING since deploy
sqlite3 ~/.cidx-server/logs.db \
    "SELECT timestamp, level, source, message FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY timestamp DESC LIMIT 50;"

# (cluster: same query against postgres_dsn)

# 3. Memory CRUD smoke test (validates Bug #932 fix end-to-end)
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"<your-pass>"}' | jq -r '.access_token')
curl -s -X POST http://localhost:8000/mcp -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"create_memory","arguments":{"type":"gotcha","scope":"global","summary":"upgrade validation","evidence":[{"file":"none","lines":"1-1"}]}}}'
# Expect: {"success": true, "id": "<uuid>", "content_hash": "<sha>", ...}

# 4. (Optional) RA curl wrapper smoke test
# (assumes you've enabled the RA - admin only, MFA required)
```

## Rollback Path

v10.0.0 introduces only backward-compatible schema changes (CREATE TABLE IF NOT
EXISTS, ADD COLUMN). Rolling back to v9.x is supported:

```bash
# Solo
sudo systemctl stop cidx-server
pipx install --force git+https://github.com/LightspeedDMS/code-indexer.git@v9.23.11
sudo systemctl start cidx-server

# Cluster: roll back one node at a time, same procedure
```

The new tables and columns added by v10.0.0 will sit unused on v9.x - harmless.

## Need Help?

- Read the full [CHANGELOG.md](../CHANGELOG.md) for the per-feature breakdown
- Review the [Architecture document](architecture.md) for the v10.0 architectural additions section
- Operator runbooks: see `docs/operating-modes.md` and `docs/server-deployment.md`
- Cluster operators: `docs/cluster-architecture.md`
