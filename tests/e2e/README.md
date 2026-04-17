# CIDX E2E Test Suite

## Purpose

This directory contains the end-to-end (E2E) test suite for the CIDX server and CLI. These tests exercise the full system stack — real CLI subprocesses, real HTTP requests, real filesystem operations, real embedding provider calls — with no mocking. They complement the unit and integration tests in `tests/unit/` by validating the system behaves correctly from a user's perspective.

The suite is designed to run both locally (during development) and in CI. All configuration is supplied via environment variables so no secrets are embedded in source code.

## Scope

The E2E suite covers:

- CLI standalone operations: init, index, query, FTS, watch (Phase 1)
- CLI daemon mode operations: start, stop, query via daemon socket (Phase 2)
- Server API operations via FastAPI TestClient: MCP JSON-RPC, REST endpoints, authentication, golden repos, background jobs (Phase 3)
- CLI remote mode operations against a live uvicorn subprocess: full round-trip CLI-to-server-to-CLI flows (Phase 4)

## Out of Scope

The following are explicitly excluded from this E2E suite:

| Item | Reason |
|------|--------|
| Web UI (browser automation) | Requires Playwright/Selenium; separate concern |
| CI/CD pipeline integration testing | Platform-specific; tested via GitHub Actions workflow files |
| Agent delegation and research assistant | Requires live Anthropic API; Claude CLI not available in standard CI |
| Langfuse tracing | Requires live Langfuse instance; covered by integration tests |
| Teach-AI workflows | Depend on external LLM services not suitable for automated E2E |
| OAuth/OIDC flows | Require external identity provider; not automatable in isolation |
| MFA authentication | Requires interactive user presence |
| Production deployment verification | Out of scope for local test runner |

## Architecture

The suite is organized into 4 phases that mirror increasing integration depth:

```
tests/e2e/
    README.md                  # This file
    conftest.py                # Shared session-scoped fixtures (e2e_ prefix)
    helpers.py                 # Stateless helper functions
    cli_standalone/            # Phase 1: cidx CLI without daemon
    cli_daemon/                # Phase 2: cidx CLI with daemon running
    server/                    # Phase 3: server via FastAPI TestClient
    cli_remote/                # Phase 4: CLI against live uvicorn subprocess
```

### Phase descriptions

**Phase 1 — CLI Standalone** (`cli_standalone/`): Runs `cidx` commands via subprocess against fresh working copies of the seed repositories. No daemon, no server. Tests init, index, query, and FTS operations.

**Phase 2 — CLI Daemon** (`cli_daemon/`): Same as Phase 1 but starts the cidx daemon first and exercises daemon-mode query paths (Unix socket, in-memory cache).

**Phase 3 — Server In-Process** (`server/`): Uses FastAPI `TestClient` to call server endpoints directly in the same process. Fast, no subprocess overhead. Covers MCP JSON-RPC, REST API, authentication, golden repo management, and background jobs.

**Phase 4 — CLI Remote** (`cli_remote/`): Starts a real uvicorn subprocess on port `E2E_SERVER_PORT`, waits for health check, runs CLI commands that talk to it over HTTP, then shuts the server down. Tests the full network path.

### Orchestration script

`e2e-automation.sh` at the repository root runs all 4 phases sequentially:

1. Clones seed repositories to a persistent cache (`~/.tmp/cidx-e2e-seed-repos/`)
2. Copies fresh working copies for this run (`~/.tmp/cidx-e2e-work/`)
3. Runs Phase 1 and Phase 2 via pytest (no server needed)
4. Runs Phase 3 via pytest (TestClient, no subprocess)
5. Starts uvicorn subprocess, waits for health, runs Phase 4, stops server
6. Reports overall pass/fail and exits with code 0 (all pass) or 1 (any fail)

The script stops at the first failing phase when running all phases, so failures surface immediately rather than cascading.

### Seed repositories

Three small OSS repositories are used as test fixtures:

| Name | URL | Tag | Language |
|------|-----|-----|----------|
| markupsafe | pallets/markupsafe | 2.1.5 | Python |
| type-fest | sindresorhus/type-fest | 4.8.3 | TypeScript |
| tries | LightspeedDMS/tries | HEAD | Mixed |

Seed repos are cloned once to the cache directory and never modified. Before each run, fresh copies are made into the work directory so tests can modify repo contents freely.

## Configuration

All configuration is supplied via environment variables. `e2e-automation.sh` sets these automatically. When running phases manually, export them first.

Copy the template to create your local config:

```bash
cp .e2e-automation.template .e2e-automation
# Edit .e2e-automation to set E2E_VOYAGE_API_KEY if needed
```

### Required variables

| Variable | Description |
|----------|-------------|
| `E2E_ADMIN_USER` | Admin username for the E2E server instance |
| `E2E_ADMIN_PASS` | Admin password for the E2E server instance |
| `E2E_SERVER_HOST` | Server bind host (default: `127.0.0.1`) |
| `E2E_SERVER_PORT` | Server port for Phase 4 (default: `8899`) |
| `E2E_SEED_CACHE_DIR` | Persistent directory for cloned seed repos |
| `E2E_SERVER_DATA_DIR` | Isolated data directory for the E2E server (wiped each run) |
| `E2E_WORK_DIR` | Per-run working copies of seed repos |

### Optional variables

| Variable | Description |
|----------|-------------|
| `E2E_VOYAGE_API_KEY` | VoyageAI API key. Falls back to `VOYAGE_API_KEY`. Tests requiring semantic search fail individually if absent. |

## Running Tests

### Full suite (all 4 phases)

```bash
./e2e-automation.sh
```

### Single phase

```bash
./e2e-automation.sh --phase 1   # CLI standalone
./e2e-automation.sh --phase 2   # CLI daemon
./e2e-automation.sh --phase 3   # Server in-process
./e2e-automation.sh --phase 4   # CLI remote (starts/stops server)
```

### Manual pytest run (development)

Export the required variables first, then run pytest directly:

```bash
export E2E_ADMIN_USER=admin
export E2E_ADMIN_PASS=admin
export E2E_SERVER_HOST=127.0.0.1
export E2E_SERVER_PORT=8899
export E2E_SEED_CACHE_DIR=$HOME/.tmp/cidx-e2e-seed-repos
export E2E_SERVER_DATA_DIR=$HOME/.tmp/cidx-e2e-server-data
export E2E_WORK_DIR=$HOME/.tmp/cidx-e2e-work

PYTHONPATH=./src python3 -m pytest tests/e2e/cli_standalone/ -v --tb=short
```

### Phase 4 manual run (requires running server)

Phase 4 tests expect a live server at `E2E_SERVER_HOST:E2E_SERVER_PORT`. Start it first:

```bash
PYTHONPATH=./src CIDX_TEST_FAST_SQLITE=1 \
  python3 -m uvicorn code_indexer.server.app:app \
    --host 127.0.0.1 --port 8899 --workers 1 &

# Wait for health, then run:
PYTHONPATH=./src python3 -m pytest tests/e2e/cli_remote/ -v --tb=short
```

## Adding Tests

### Choosing the right phase directory

| Test involves | Place in |
|---------------|----------|
| `cidx init`, `cidx index`, `cidx query` (no daemon, no server) | `cli_standalone/` |
| `cidx` with daemon socket, `cidx start/stop` | `cli_daemon/` |
| MCP JSON-RPC, REST API, auth, golden repos, background jobs | `server/` |
| CLI commands that talk to a running CIDX server over HTTP | `cli_remote/` |

### Using shared fixtures

All fixtures from `conftest.py` are available. Declare them as function parameters:

```python
def test_something(e2e_config, e2e_seed_repo_paths, e2e_cli_env):
    result = run_cidx("query", "authentication", cwd=str(e2e_seed_repo_paths.markupsafe), env=e2e_cli_env)
    assert result.returncode == 0
```

Server tests that need HTTP access use `e2e_http_client` and `e2e_admin_token`:

```python
from tests.e2e.helpers import mcp_call

def test_list_repos(e2e_http_client, e2e_admin_token):
    result = mcp_call(e2e_http_client, "tools/call",
                      params={"name": "list_repositories", "arguments": {}},
                      token=e2e_admin_token)
    assert result is not None
```

### Conventions

- Test files must be named `test_*.py`
- Test functions must be named `test_*`
- No mocking — use real services, real filesystem, real CLI
- Each test must clean up any state it creates (use `tmp_path` or `e2e_work_dir` subdirectories)
- Tests must not depend on execution order
- Tests that require `E2E_VOYAGE_API_KEY` must skip gracefully if it is not set:

```python
import pytest, os

pytestmark = pytest.mark.skipif(
    not os.environ.get("E2E_VOYAGE_API_KEY") and not os.environ.get("VOYAGE_API_KEY"),
    reason="VOYAGE_API_KEY not configured"
)
```
