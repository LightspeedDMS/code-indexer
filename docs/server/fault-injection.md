# Fault Injection

The fault-injection harness lets an operator of a **non-production** CIDX Server make outbound provider calls fail
on purpose (HTTP errors, timeouts, DNS and TLS failures, malformed bodies, truncated streams, redirect loops, added
latency) to test how the server behaves, without waiting for a real provider outage.

Code: `src/code_indexer/server/fault_injection/` (`startup.py` gate, `router.py` endpoints, `fault_profile.py`
profiles and matching, `fault_injection_service.py` counters and history). The e2e suite's fault-injection phase
uses the same harness (`./e2e-automation.sh --phase 5`).

## What it intercepts

Outbound HTTP calls that the server makes through the harness-aware `HttpClientFactory` it creates at startup. The
VoyageAI and Cohere embedding and reranking clients accept that factory; a client constructed without it uses a
factory that injects nothing. Each call is matched against the registered profiles by the hostname of its URL; a matching profile
decides, at random according to its rates, whether to inject a fault.

## Enabling it

The harness is controlled by two bootstrap keys in `~/.cidx-server/config.json`, read only at server start:

| Key | Default | Meaning |
|-----|---------|---------|
| `fault_injection_enabled` | `false` | Master switch |
| `fault_injection_nonprod_ack` | `false` | Explicit confirmation that this server is not production |

Add both to the existing `config.json` (keep its other keys) and restart the server:

```json
{
  "fault_injection_enabled": true,
  "fault_injection_nonprod_ack": true
}
```

Startup outcomes:

| Configuration | Result |
|---------------|--------|
| `fault_injection_enabled` false or absent | Harness off. The `/admin/fault-injection/*` routes are not registered (HTTP 404) |
| Enabled, and the runtime OpenTelemetry setting `deployment_environment` is `production` | CRITICAL log, the server exits (status 1) |
| Enabled without `fault_injection_nonprod_ack` | CRITICAL log, the server exits (status 1) |
| Enabled, acknowledged, not production | Harness on; WARNING `FAULT INJECTION HARNESS ACTIVE (non-prod mode)` at startup |

`deployment_environment` is a runtime setting (Web UI, Configuration, OpenTelemetry Export; default `development`;
see [Observability](observability.md#opentelemetry)). The harness starts with no profiles, so nothing is injected
until one is registered.

To confirm it is on:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, message FROM logs WHERE message LIKE '%FAULT INJECTION HARNESS ACTIVE%' ORDER BY id DESC LIMIT 5"
```

To turn it off, set `fault_injection_enabled` to `false` (or remove both keys) and restart.

## Fault profiles

A profile targets one hostname pattern. Fields (all rates are probabilities from 0.0 to 1.0):

### Terminating faults

At most one terminating fault applies to a request; the sum of these rates must not exceed 1.0.

| Rate field | Fault recorded as | Effect | Related fields |
|------------|-------------------|--------|----------------|
| `error_rate` | `http_error` | Synthetic HTTP error response | `error_codes` (required when `error_rate` > 0), `retry_after_sec_range` (default `[1, 5]`) adds `Retry-After` |
| `connect_timeout_rate` | `connect_timeout` | Raises `httpx.ConnectTimeout` | |
| `read_timeout_rate` | `read_timeout` | Raises `httpx.ReadTimeout` | |
| `write_timeout_rate` | `write_timeout` | Raises `httpx.WriteTimeout` | |
| `pool_timeout_rate` | `pool_timeout` | Raises `httpx.PoolTimeout` | |
| `connect_error_rate` | `connect_error` | Raises `httpx.ConnectError` | |
| `dns_failure_rate` | `dns_failure` | `httpx.ConnectError` caused by a name-resolution error | |
| `tls_error_rate` | `tls_error` | `httpx.ConnectError` caused by an SSL error | |
| `malformed_rate` | `malformed_json` | HTTP 200 with a corrupted body | `corruption_modes` (required when `malformed_rate` > 0): `truncate`, `invalid_utf8`, `wrong_schema`, `empty` |
| `stream_disconnect_rate` | `stream_disconnect` | The real response, cut off mid-body | `truncate_after_bytes_range` (default `[50, 200]`) |
| `redirect_loop_rate` | `redirect_loop` | HTTP 302 back to the same URL | |

### Additive faults

Independent of the terminating fault and of each other; they delay the response.

| Rate field | Fault recorded as | Related field |
|------------|-------------------|---------------|
| `latency_rate` | `latency` | `latency_ms_range` (default `[100, 500]`) |
| `slow_tail_rate` | `slow_tail` | `slow_tail_ms_range` (default `[1000, 5000]`) |

Ranges are `[min, max]` pairs with `0 <= min <= max`. A profile also has `enabled` (default `true`); a disabled
profile never matches. An invalid profile is rejected with HTTP 400.

### Target matching

- A plain target (`api.voyageai.com`) matches that hostname exactly, case-insensitively.
- A `*.` target (`*.voyageai.com`) matches every subdomain and the domain itself.
- Substrings never match: `voyage` does not match `api.voyageai.com`.
- Matching uses only the hostname. VoyageAI embedding and reranking both call `api.voyageai.com`, and Cohere
  embedding and reranking both call `api.cohere.com`, so a profile affects both kinds of call to that provider.

## REST endpoints

All under `/admin/fault-injection`, admin role required (`Authorization: Bearer <token>` from `POST /auth/login`).
They do not require step-up elevation. While the harness is off they answer HTTP 404.

| Method and path | Purpose | Response |
|-----------------|---------|----------|
| `GET /status` | Harness state | `enabled`, `profile_count`, `counters` (keys `<target>:<fault type>`), `docs_url` |
| `GET /profiles` | All profiles | `{"profiles": [...]}` |
| `GET /profiles/{target}` | One profile | the profile, or 404 |
| `PUT /profiles/{target}` | Create or replace. The body must contain `target`; the path value wins | the stored profile |
| `PATCH /profiles/{target}` | Change only the fields sent | the profile, or 404 when absent |
| `DELETE /profiles/{target}` | Remove one profile | `{"deleted": "<target>"}` |
| `DELETE /profiles` | Remove all profiles; keeps counters and history | `{"cleared": <count>}` |
| `POST /reset` | Remove profiles, counters and history | `{"reset": true}` |
| `POST /preview` with `{"url": "..."}` | Which profile would match, without injecting | `{"matched": <profile or null>}` |
| `GET /history` | The last 100 injections | `{"history": [{"target", "fault_type", "correlation_id"}, ...]}` |
| `POST /seed` with `{"seed": <int>}` | Re-seed the random source | `{"seeded": <int>}` |

A target containing `/` is rejected with HTTP 400.

`POST /seed` is accepted, but the live harness uses the operating system's random source, which ignores seeding, so
it does not make injection sequences reproducible.

## Tracing an injection

Each injection gets a UUID correlation id. It appears in `GET /history` and in one log row: `source`
`fault_injection`, message `fault_injection: target=<target> fault_type=<type> correlation_id=<id>`, with the id
also in the row's `correlation_id` column. Log rows go to `~/.cidx-server/logs.db` on a standalone server and to
PostgreSQL in a cluster ([Observability](observability.md#logs)).

## Playbooks

Get a token first:

```bash
export CIDX_URL="http://127.0.0.1:8000"
TOKEN=$(curl -s -X POST "$CIDX_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<password>"}' | jq -r '.access_token')
```

### Provider rate limiting (HTTP 429 from VoyageAI)

```bash
# 1. Every VoyageAI call answers 429 with Retry-After of 1-3 seconds
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"target": "api.voyageai.com", "error_rate": 1.0, "error_codes": [429], "retry_after_sec_range": [1, 3]}' | jq .

# 2. Run a semantic search against a repository indexed with VoyageAI
curl -s -X POST "$CIDX_URL/mcp" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_code", "arguments": {"query_text": "example query", "repository_alias": "example-repo-global", "limit": 3}}}'

# 3. Confirm the injections and read the related log rows
curl -s "$CIDX_URL/admin/fault-injection/status" -H "Authorization: Bearer $TOKEN" | jq '.counters'
curl -s "$CIDX_URL/admin/fault-injection/history" -H "Authorization: Bearer $TOKEN" | jq '.history[-5:]'
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs WHERE level IN ('WARNING','ERROR') ORDER BY id DESC LIMIT 20"

# 4. Clean up
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" -H "Authorization: Bearer $TOKEN" | jq .
```

### DNS outage

Same steps with a DNS-failure profile:

```bash
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.voyageai.com" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"target": "api.voyageai.com", "dns_failure_rate": 1.0}' | jq .
```

### Added latency on a fraction of calls

```bash
curl -s -X PUT "$CIDX_URL/admin/fault-injection/profiles/api.cohere.com" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"target": "api.cohere.com", "latency_rate": 0.5, "latency_ms_range": [500, 1500]}' | jq .
```

## After testing

```bash
curl -s -X POST "$CIDX_URL/admin/fault-injection/reset" -H "Authorization: Bearer $TOKEN" | jq .
curl -s "$CIDX_URL/admin/fault-injection/status" -H "Authorization: Bearer $TOKEN" | jq '{profile_count, counters}'
# Expected: {"profile_count": 0, "counters": {}}
```

Then turn the harness off in `config.json` and restart, and review the log store for errors and warnings from the
test period:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY id DESC LIMIT 100"
```
