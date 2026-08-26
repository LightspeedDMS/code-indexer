# OTEL Testing Infrastructure

Local OpenTelemetry testing stack for CIDX Server telemetry development and E2E testing.

## Components

- **OTEL Collector**: Receives OTLP telemetry (traces, metrics, logs) from CIDX Server
- **Jaeger**: Trace visualization and analysis (UI at http://localhost:16686)
- **Prometheus**: Metrics storage and querying (UI at http://localhost:9090)

All three images are pinned to an exact tag AND content digest in
`docker-compose.yml` (no `latest`), with health checks so stack readiness is
deterministic.

## Quick Start

Every command below uses `docker compose` (the Docker Compose v2 plugin,
space-separated) -- the legacy hyphenated `docker-compose` v1 binary is not
assumed to be installed and is not used anywhere in this repo.

```bash
# Start the stack (waits on real health checks, not a sleep)
docker compose up -d --wait --wait-timeout 120

# Verify services are healthy
docker compose ps

# View logs
docker compose logs -f

# Stop the stack
docker compose down

# Stop and remove volumes (clean slate -- always use this for test runs)
docker compose down -v
```

## Endpoints

| Service | Port | Description |
|---------|------|-------------|
| OTLP gRPC | 4317 | CIDX sends telemetry here (default) |
| OTLP HTTP | 4318 | Alternative HTTP endpoint |
| Jaeger UI | 16686 | View traces at http://localhost:16686 |
| Jaeger admin/health | 14269 | Jaeger's own health check, `GET /` -> HTTP 200 |
| Prometheus | 9090 | View metrics at http://localhost:9090 |
| Collector self-metrics | 8888 | OTEL Collector's own metrics (`otelcol_receiver_accepted_*` acceptance counters) |
| Collector health check | 13133 | `GET /` -> `{"status":"Server available",...}` |
| Prometheus exporter | 8889 | Collector's exported metrics, scraped by the prometheus service |

Note: the otel-collector service intentionally has **no Docker-level
`healthcheck:`** directive. The `otel/opentelemetry-collector-contrib` image
is distroless -- it ships no shell and no wget/curl/busybox at all (verified:
`docker run --rm --entrypoint /bin/sh otel/opentelemetry-collector-contrib:0.112.0`
fails with `executable file not found in $PATH`), so there is no in-container
binary a `healthcheck:` directive could exec. Its readiness is instead polled
from outside the container, against its real `:13133/` health_check
extension endpoint -- see `_wait_for_collector_health`
in `tests/e2e/server/test_21_otel_live_collector_1676.py`. That test's own
polling loop IS the readiness check; `e2e-automation.sh`'s
`run_otel_live_collector_subcheck()` does not implement a separate
health-poll helper -- it simply invokes the test file via `pytest`.

## CIDX Server Configuration

To send telemetry to this local stack, configure CIDX with:

```json
{
  "telemetry_config": {
    "enabled": true,
    "collector_endpoint": "http://localhost:4317",
    "collector_protocol": "grpc",
    "service_name": "cidx-server",
    "export_traces": true,
    "export_metrics": true,
    "export_logs": true
  }
}
```

Telemetry configuration is managed exclusively via the Web UI Config Screen
(DB-backed) -- there is no environment-variable override (Story #1676 AC1
removed the legacy `CIDX_TELEMETRY_*`/`CIDX_OTEL_*`/
`CIDX_DEPLOYMENT_ENVIRONMENT` overrides entirely; setting any of them now
only produces one aggregated startup WARNING naming them as ignored). Log in
to the server's Web UI, open the Config Screen, and set:

- Telemetry Enabled: on
- Collector Endpoint: `http://localhost:4317`
- Collector Protocol: `grpc`
- Export Logs: on (Story #1676 AC3 -- real OTLP log export; off by default)

Saving the form persists these values to the database immediately; these
fields are in `restart_required_fields`, so a server **restart** is required
for a running process to pick up a changed `collector_endpoint` (or any
other field in that list) -- no environment variable is ever read for this.

## Verifying Telemetry

### Traces (Jaeger)

1. Open http://localhost:16686
2. Select "cidx-server" from the Service dropdown
3. Click "Find Traces" to see recent traces

### Metrics (Prometheus)

1. Open http://localhost:9090
2. Query for `cidx_*` metrics
3. Example: `cidx_cidx_fts_requests_total` (the exporter's `namespace: cidx`
   setting is prepended to the metric's own dotted name, e.g.
   `cidx.fts.requests` -> `cidx_cidx_fts_requests_total`; verified
   empirically against this exact collector config, not assumed)

### Logs (Story #1676 AC3/AC8)

There is no log-query UI anywhere in this stack. Log delivery is verified
via two corroborating pieces of evidence instead of a content-level query:

1. The collector's own self-metrics acceptance counter
   (`otelcol_receiver_accepted_log_records` on port 8888) increases.
2. A real `LogRecord #` block appears in the collector's `debug` exporter
   container output (`docker compose logs otel-collector`).

### API Verification

```bash
# Check if Jaeger has received traces from cidx-server
curl -s http://localhost:16686/api/services | jq '.data | contains(["cidx-server"])'

# Check Prometheus targets
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[].health'

# Check the collector's own health_check extension
curl -s http://localhost:13133/

# Check the collector's self-metrics acceptance counters
curl -s http://localhost:8888/metrics | grep otelcol_receiver_accepted
```

## E2E Test Usage

Prior to Story #1676 AC8, this section claimed the E2E suite already started
and verified this stack automatically. **That claim was false** -- this
`docker-compose.yml` did not exist until AC8 added it, and no automated test
exercised it.

As of Story #1676 AC8, `tests/e2e/server/test_21_otel_live_collector_1676.py`
is the real automation described here. It:

1. Brings this exact compose stack up and waits on its real health checks
   (Jaeger + Prometheus via Docker `healthcheck:`, the collector via a
   bounded host-side poll of its `:13133` endpoint -- never a sleep).
2. Constructs a fresh, isolated CIDX server instance with telemetry enabled
   and `collector_endpoint` pointed at this live stack.
3. Registers and activates a small, self-contained local git repo, then
   drives a real MCP `regex_search` call through the real front door.
4. Force-flushes the tracer/meter/logger providers so the round trip is
   deterministic, then verifies all three signals using each backend's own
   real query surface: Jaeger's `/api/traces` for the trace, a Prometheus
   `/api/v1/query` for the metric, and the collector's self-metrics counter
   plus its debug-exporter output for the log (see "Logs" above).
5. Tears the stack down (`docker compose down -v`) in a `finally` block on
   both the success and failure path -- verified manually: zero orphaned
   containers or volumes after both a passing run and a deliberately
   broken one.
6. Skips cleanly (not a failure) with an unambiguous message when Docker or
   `docker compose` is unavailable.

It is wired into `e2e-automation.sh` as an explicit Docker-dependent
sub-check of Phase 3 (run as its own separate `pytest` invocation, excluded
from Phase 3's normal directory sweep via `--ignore` -- see
`run_otel_live_collector_subcheck()` in that script) so it genuinely
executes wherever Docker is guaranteed present, rather than being folded
silently into the shared Phase 3 session. It is deliberately **not** wired
into `fast-automation.sh` or `server-fast-automation.sh` -- both must stay
fast and Docker-independent.

## Troubleshooting

### No traces appearing in Jaeger

1. Check collector logs: `docker compose logs otel-collector`
2. Verify CIDX is configured with telemetry enabled
3. Ensure collector_endpoint points to http://localhost:4317

### Connection refused

1. Verify containers are running: `docker compose ps`
2. Check for port conflicts: `netstat -tlnp | grep -E '4317|4318|16686|9090|13133'`
3. Restart the stack: `docker compose restart`

### Debug mode

The collector is configured with debug export enabled for all three signal
pipelines (traces, metrics, logs). Check collector logs for detailed
telemetry data: `docker compose logs -f otel-collector`
