# Fault Injection Operator Guide

Story #746 -- Fault Injection Harness for External Provider Resilience Testing

---

## Overview

The CIDX server includes an optional fault injection harness that intercepts outbound HTTP calls to external providers (VoyageAI embeddings, Voyage rerank, Cohere rerank) at the httpx transport layer. It lets operators exercise deterministic failure scenarios -- HTTP errors, timeouts, DNS failures, TLS errors, malformed responses, stream disconnects -- against a running server without waiting for an actual provider outage.

The harness is controlled via admin-only REST endpoints under `/admin/fault-injection/`. Every injected fault is logged to the server logs database (`~/.cidx-server/logs.db`) with `source='fault_injection'` for post-test audit correlation.

---

## Safety Posture

The harness has hard safety guards that prevent accidental use in production.

### Bootstrap Gate

The harness is controlled by two keys in `config.json` that require a server restart to take effect.

Both keys must be explicitly set. Neither defaults to true.

If `fault_injection_enabled=true` is set on a server whose `telemetry_config.deployment_environment` is `"production"`, the server refuses to start and calls `sys.exit(1)` with a CRITICAL log. There is no override.

If `fault_injection_enabled=true` is set without `fault_injection_nonprod_ack=true`, the server also refuses to start.

### Four Startup Scenarios

| Scenario | config.json state | Server behavior |
|----------|------------------|-----------------|
| 1 (default) | `fault_injection_enabled=false` | Harness inactive. Endpoints return 404. |
| 2 (ack missing) | `fault_injection_enabled=true`, `fault_injection_nonprod_ack=false` | CRITICAL log + `sys.exit(1)` |
| 3 (production) | `fault_injection_enabled=true`, `deployment_environment=production` | CRITICAL log + `sys.exit(1)` |
| 4 (live) | Both true, non-production | Harness active. WARNING logged on startup. |

---

## Configuration

Edit `~/.cidx-server/config.json` and restart the server.

Minimum configuration to enable the harness on a non-production server:

```json
{
  "server_dir": "~/.cidx-server",
  "host": "127.0.0.1",
  "port": 8000,
  "fault_injection_enabled": true,
  "fault_injection_nonprod_ack": true
}
```

To verify the harness is active after restart:

```bash
grep "FAULT INJECTION HARNESS ACTIVE" /tmp/cidx-server.log
```

### Bootstrap Keys Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `fault_injection_enabled` | bool | `false` | Master switch. Restart required. Must be `false` on production. |
| `fault_injection_nonprod_ack` | bool | `false` | Explicit non-prod acknowledgment. Required when enabled. |

---

## Failure Modes

The harness supports 13 configurable fault modes per target.

### Terminating Modes

These modes replace the real HTTP response with a synthetic error. Their rates are mutually exclusive and must sum to at most 1.0.

| fault_type | Rate field | Description |
|------------|------------|-------------|
| `http_error` | `error_rate` | Return an HTTP error response (4xx/5xx). `error_codes` list is required. Optional `retry_after_sec_range` adds Retry-After header. |
| `connect_timeout` | `connect_timeout_rate` | Raise `httpx.ConnectTimeout` immediately. |
| `read_timeout` | `read_timeout_rate` | Raise `httpx.ReadTimeout` immediately. |
| `write_timeout` | `write_timeout_rate` | Raise `httpx.WriteTimeout` immediately. |
| `pool_timeout` | `pool_timeout_rate` | Raise `httpx.PoolTimeout` immediately. |
| `connect_error` | `connect_error_rate` | Raise `httpx.ConnectError` immediately. |
| `dns_failure` | `dns_failure_rate` | Raise `httpx.ConnectError` caused by `socket.gaierror` (DNS resolution failure). |
| `tls_error` | `tls_error_rate` | Raise `httpx.ConnectError` caused by `ssl.SSLError`. |
| `malformed_json` | `malformed_rate` | Return HTTP 200 with a corrupted JSON body. `corruption_modes` list required. |
| `stream_disconnect` | `stream_disconnect_rate` | Fetch the real response then truncate the stream mid-body. |
| `redirect_loop` | `redirect_loop_rate` | Return HTTP 302 whose Location points back to the original URL. |

### Additive Modes

These modes add latency without replacing the response. They are independent of the terminating mode and of each other.

| fault_type | Rate field | Description |
|------------|------------|-------------|
| `latency` | `latency_rate` | Add a uniform random delay before the response (range in ms). |
| `slow_tail` | `slow_tail_rate` | Add a second larger latency injection simulating slow-tail requests. |

### Corruption Modes (for `malformed_json`)

| Mode | Description |
|------|-------------|
| `truncate` | Valid JSON prefix, truncated mid-token. |
| `invalid_utf8` | Raw bytes that are not valid UTF-8. |
| `wrong_schema` | Valid JSON object with unexpected structure. |
| `empty` | Empty body. |

---

## REST API Reference

All endpoints require admin authentication. Pass `Authorization: Bearer <token>` obtained from `POST /auth/login`.

When the harness is inactive (Scenario 1), all endpoints return `404 Not Found`.

### GET /admin/fault-injection/status

Returns harness status, active profile count, injection counters, and docs URL.

```
GET /admin/fault-injection/status
Authorization: Bearer <token>
```

Response:
```json
{
  "enabled": true,
  "profile_count": 1,
  "counters": {
    "api.voyageai.com:http_error": 42
  },
  "docs_url": "/docs/fault-injection-operator-guide.md"
}
```

### GET /admin/fault-injection/profiles

Returns all registered fault profiles.

```
GET /admin/fault-injection/profiles
Authorization: Bearer <token>
```

Response: `{"profiles": [<profile>, ...]}`

### GET /admin/fault-injection/profiles/{target}

Returns the fault profile for a single target. Returns 404 if not found.

```
GET /admin/fault-injection/profiles/api.voyageai.com
Authorization: Bearer <token>
```

### PUT /admin/fault-injection/profiles/{target}

Create or replace a fault profile for the given target. The target in the URL path takes precedence over any `target` field in the body.

```
PUT /admin/fault-injection/profiles/api.voyageai.com
Content-Type: application/json
Authorization: Bearer <token>

{
  "target": "api.voyageai.com",
  "error_rate": 1.0,
  "error_codes": [429],
  "retry_after_sec_range": [1, 3]
}
```

Returns the stored profile.

### PATCH /admin/fault-injection/profiles/{target}

Partial update: merge supplied fields into the existing profile. Omitted fields are preserved. Returns 404 if the profile does not exist.

```
PATCH /admin/fault-injection/profiles/api.voyageai.com
Content-Type: application/json
Authorization: Bearer <token>

{
  "error_rate": 0.5
}
```

### DELETE /admin/fault-injection/profiles/{target}

Remove a single fault profile by target name.

```
DELETE /admin/fault-injection/profiles/api.voyageai.com
Authorization: Bearer <token>
```

Response: `{"deleted": "api.voyageai.com"}`

### DELETE /admin/fault-injection/profiles

Remove all registered fault profiles. Does not reset counters or history.

```
DELETE /admin/fault-injection/profiles
Authorization: Bearer <token>
```

Response: `{"cleared": 1}`

### POST /admin/fault-injection/reset

Clear all profiles, counters, and history atomically. Use this after each test to return to a clean state.

```
POST /admin/fault-injection/reset
Authorization: Bearer <token>
```

Response: `{"reset": true}`

### POST /admin/fault-injection/preview

Dry-run: return the profile that would match the given URL without recording any event.

```
POST /admin/fault-injection/preview
Content-Type: application/json
Authorization: Bearer <token>

{
  "url": "https://api.voyageai.com/v1/embeddings"
}
```

Returns `{"matched": <profile>}` or `{"matched": null}` when no profile applies.

### GET /admin/fault-injection/history

Return the bounded ring buffer of the 100 most recent injection events.

```
GET /admin/fault-injection/history
Authorization: Bearer <token>
```

Response: `{"history": [{"target": "...", "fault_type": "...", "correlation_id": "..."}, ...]}`

### POST /admin/fault-injection/seed

Re-seed the internal RNG for deterministic injection sequences. Use this before a test that requires a reproducible fault sequence.

```
POST /admin/fault-injection/seed
Content-Type: application/json
Authorization: Bearer <token>

{
  "seed": 42
}
```

Response: `{"seeded": 42}`

---

## Correlation ID

Every injected fault is assigned a UUID correlation ID at the transport layer. The same ID appears in:

- The in-memory ring buffer (`GET /admin/fault-injection/history`)
- The SQLite logs database (`source='fault_injection'`, `extra_data` column contains the correlation_id)

Use the correlation ID to cross-reference a specific injection event with the server log entry and the client-side request that triggered it.

---

## Target Matching

Profiles are matched against the hostname of the outbound request URL.

Exact match: `"api.voyageai.com"` matches only that hostname.

Wildcard suffix: `"*.voyageai.com"` matches `api.voyageai.com`, `proxy.voyageai.com`, and the apex `voyageai.com`.

Substring matching is never performed. `"voyage"` does not match `api.voyageai.com`.

---

## Worked Playbooks

Before running any playbook, obtain a token:

```bash
export CIDX_URL="http://127.0.0.1:8099"
TOKEN=$(curl -s -X POST "$CIDX_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<from .local-testing>"}' | jq -r '.access_token')
```

### Playbook 1: Test Voyage Embed 429 Handling

Verify the server handles Voyage embed 429 responses with Retry-After correctly.

```bash
# Step 1: configure 100% 429 on VoyageAI with Retry-After 1-3s
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "api.voyageai.com",
    "error_rate": 1.0,
    "error_codes": [429],
    "retry_after_sec_range": [1, 3]
  }' | jq .

# Step 2: issue an embed request (via MCP or REST)
# Observe that the server returns an error or retries appropriately

# Step 3: read counters
curl -s "$CIDX_URL/admin/fault-injection/status" \
  -H "Authorization: Bearer $TOKEN" | jq '.counters'

# Step 4: read history
curl -s "$CIDX_URL/admin/fault-injection/history" \
  -H "Authorization: Bearer $TOKEN" | jq '.history[-5:]'

# Step 5: check logs DB
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, message FROM logs \
   WHERE source='fault_injection' ORDER BY timestamp DESC LIMIT 10;"

# Step 6: clean up
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Playbook 2: Dual-Provider RRF Fallback Verified

Verify that when Voyage rerank returns 503, the server falls back to Cohere rerank.

```bash
# Step 1: inject 100% 503 on Voyage rerank endpoint
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "api.voyageai.com",
    "error_rate": 1.0,
    "error_codes": [503]
  }' | jq .

# Step 2: no profile on Cohere (real Cohere will be called)

# Step 3: run a search_code MCP call and observe reranked results

# Step 4: verify Voyage 503 was injected
curl -s "$CIDX_URL/admin/fault-injection/status" \
  -H "Authorization: Bearer $TOKEN" | jq '.counters'

# Step 5: check server logs for Voyage 503 + Cohere invocation
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs \
   WHERE level IN ('WARNING','ERROR') ORDER BY timestamp DESC LIMIT 20;"

# Step 6: clean up
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Playbook 3: Retry-After Backoff Verification

Verify that the server respects the Retry-After header from a 429 response.

```bash
# Step 1: seed RNG for reproducibility
curl -s -X POST "$CIDX_URL/admin/fault-injection/seed" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"seed": 42}' | jq .

# Step 2: inject 429 with short Retry-After range
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "api.voyageai.com",
    "error_rate": 1.0,
    "error_codes": [429],
    "retry_after_sec_range": [1, 2]
  }' | jq .

# Step 3: trigger embed request and observe timing
time curl -s -X POST "$CIDX_URL/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_code","arguments":{"query_text":"test query","repository_alias":"test-global","limit":3}}}'

# Step 4: read history to see Retry-After values used
curl -s "$CIDX_URL/admin/fault-injection/history" \
  -H "Authorization: Bearer $TOKEN" | jq .

# Step 5: clean up
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Playbook 4: DNS Outage on VoyageAI

Verify the server returns a meaningful error when DNS resolution fails for VoyageAI.

```bash
# Step 1: inject 100% DNS failure on VoyageAI
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "api.voyageai.com",
    "dns_failure_rate": 1.0
  }' | jq .

# Step 2: trigger an embed request -- expect connection error

# Step 3: verify DNS failure was injected
curl -s "$CIDX_URL/admin/fault-injection/status" \
  -H "Authorization: Bearer $TOKEN" | jq '.counters'

# Step 4: check fault injection logs
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, message FROM logs \
   WHERE source='fault_injection' ORDER BY timestamp DESC LIMIT 5;"

# Step 5: clean up
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

---

## Post-Test Cleanup

Always reset the harness after each test session to avoid interference with subsequent requests:

```bash
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Verify the reset was successful:

```bash
curl -s "$CIDX_URL/admin/fault-injection/status" \
  -H "Authorization: Bearer $TOKEN" | jq '{profile_count, counters}'
# Expected: {"profile_count": 0, "counters": {}}
```

After testing, restore the original `config.json` and restart the server to deactivate the harness:

```bash
cp ~/.cidx-server/config.json.pre-story-746 ~/.cidx-server/config.json
# Restart the server
```

---

## Logs DB Audit

After any test session, audit the logs database for unexpected errors:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs \
   WHERE level IN ('ERROR','WARNING') \
   ORDER BY timestamp DESC LIMIT 100;"
```

To see only fault injection events:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, message FROM logs \
   WHERE source='fault_injection' \
   ORDER BY timestamp DESC LIMIT 50;"
```
