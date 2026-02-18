# Code-Indexer (CIDX) Project Instructions

## 0. DOCUMENTATION STANDARDS

**NO EMOJI/ICONOGRAPHY** in documentation (README.md, CLAUDE.md, CHANGELOG.md, docs/*.md).

Forbidden: 🔍 🕰️ ⚡ 🎯 🔄 🤖 ✅ ❌ 👍 🚀 🔧 🔐 | ✓ ✗ ★ ● ◆ → ← | decorative characters

Use plain text headers: `### Performance Improvements`

---

## CRITICAL: SSH CONNECTION POLICY

**NEVER** use `ssh` command via Bash tool. Causes authentication failures.

**USE** MCP SSH tools for ALL remote connections:
- `mcp__ssh__ssh_connect` / `ssh_disconnect` - Session management
- `mcp__ssh__ssh_exec` - Remote commands
- `mcp__ssh__ssh_upload_file` / `ssh_download_file` - SFTP transfers

---

## CRITICAL: CREDENTIALS - ALWAYS READ FROM .local-testing

**NEVER** assume or guess SSH passwords, server credentials, usernames, or connection details.

**ALWAYS** read `.local-testing` (gitignored, project root) for:
- SSH usernames and passwords for staging (.20), production (.30), Mac laptop
- CIDX admin credentials per environment
- API keys (Langfuse, GitHub, GitLab, Anthropic)
- MCPB deployment details and encrypted credential paths
- E2E test credentials

**Workflow**: Before connecting to ANY server or using ANY credential, read `.local-testing` first. Declare it as a secret file before reading.

*Recorded 2026-02-18*

---

## CRITICAL: LOCAL TESTING BEFORE DEPLOYMENT

**NEVER** deploy/test on production until user explicitly approves.

**Local workflow**:
- Development/testing: localhost:8000
- Callbacks: dev machine's external IP (visible to Claude Server)
- Production (.30): firewalled, DO NOT TOUCH

**Deploy ONLY when** user says: "commit and push to master" OR "deploy manually to production server"

---

## CRITICAL: ADMIN PASSWORD - NEVER CHANGE

**NEVER** change admin password during local dev. Causes "session_expired" failures.

**Credentials** (localhost:8000): `admin` / `admin`

**Recovery** (if broken):
```bash
# Generate hash
python3 -c "from src.code_indexer.server.auth.password_manager import PasswordManager; print(PasswordManager().hash_password('admin'))"

# Update DB (replace HASH)
sqlite3 ~/.cidx-server/data/cidx_server.db "UPDATE users SET password_hash='HASH' WHERE username='admin';"
```
*Recorded 2026-01-26*

---

## CRITICAL: STAGING SERVER ADMIN PASSWORD - ABSOLUTELY FORBIDDEN TO CHANGE

**NEVER, UNDER ANY CIRCUMSTANCES**, change the admin password on the staging server (192.168.60.20).

**Staging server credentials**: `admin` / `Calatrava123!`

This password is used by:
- MCPB (MCP Bridge) on the Mac laptop for auto-login
- E2E test automation
- All REST/MCP API testing
- Encrypted credentials stored on client machines

Changing this password breaks ALL of the above and requires manual recovery on every client.

**ABSOLUTE PROHIBITION**: Do not reset, change, update, or modify this password via any method -- not through the Web UI, not through the database, not through any API call, not through any script. EVER.

*Recorded 2026-02-15*

---

## SSH SERVER RESTART - CRITICAL PROCEDURE

**NEVER** use `kill -15 && nohup ...` for restarts. Causes SSH lockups.

**Use systemd**:
```bash
mcp__ssh__ssh_connect(...)
echo "PASSWORD" | sudo -S systemctl restart cidx-server
systemctl status cidx-server --no-pager
```
*Recorded 2025-11-29*

---

## CRITICAL: AUTO-UPDATER IDEMPOTENT DEPLOYMENT

**NEVER** require manual intervention on production. All systemd/env/config changes via auto-updater.

**Auto-updater workflow**: `git pull` → `pip install` → `DeploymentExecutor.execute()` → `systemctl restart`

**Pattern** (in `deployment_executor.py`):
```python
def _ensure_new_config(self) -> bool:
    """Idempotent: check if configured, if not add it, daemon-reload."""
    # 1. Check if already configured - return True if so
    # 2. If not, add the configuration
    # 3. Run sudo systemctl daemon-reload
    # 4. Return True on success, False on error

def execute(self) -> bool:
    self._ensure_workers_config()
    self._ensure_cidx_repo_root()
    self._ensure_new_config()  # ADD NEW CONFIG HERE
```

**Examples**: `CIDX_REPO_ROOT` → `_ensure_cidx_repo_root()`, `--workers 1` → `_ensure_workers_config()`

*Recorded 2026-01-30 (Bug #87)*

---

## CIDX SERVER PORT CONFIGURATION - DO NOT CHANGE

**NEVER** change port config for cidx-server, HAProxy, or firewall.

**Locked config** (verified 2025-11-30):
- cidx-server systemd: port 8000
- HAProxy backend: forwards to .30 on port 8000
- Firewall: allows 8000 from HAProxy

Any port change = HAProxy 503 errors.

---

## SCIP INDEX FILE LIFECYCLE

**SCIP files are DELETED after database conversion.**

`cidx scip generate` flow:
1. Language indexer creates `index.scip` (protobuf)
2. CIDX converts to `index.scip.db` (SQLite)
3. Original `index.scip` **DELETED**

**NEVER** look for `.scip` files - only `.scip.db` remains after generation.

*Recorded 2025-12-21*

---

## CIDX SERVER CONFIGURATION - NO ENVIRONMENT VARIABLES

**NEVER** use env variables for CIDX server settings. They're invisible, not persisted, inconsistent.

**Use Web UI Configuration Screen** (single source of truth):
1. Add setting to Web UI Config Screen
2. Store in server's persistence layer
3. Access via configuration API

**Wrong**:
```python
os.environ["CIDX_SETTING"] = "value"  # NEVER
```

**Right**:
```python
from code_indexer.server.config import get_config
config = get_config()
setting = config.some_setting
```

*Recorded 2025-01-14*

---

## CRITICAL: TESTING WORKFLOW

**NEVER** run fast-automation.sh iteratively. Wastes hours.

**Hierarchy**:
1. **Targeted tests** (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. **Manual testing**: Verify feature works E2E
3. **fast-automation.sh** (final gate): Must pass before done

**Examples**:
| File Changed | Run |
|--------------|-----|
| `base_client.py` | `pytest tests/unit/api_clients/test_base_*.py -v --tb=short` |
| `auth_client.py` | `pytest tests/unit/api_clients/test_auth_*.py -v --tb=short` |
| `handlers.py` | `pytest tests/unit/server/mcp/test_handlers*.py -v --tb=short` |

**Times**: Targeted=seconds, fast-automation=6-7min (865+ tests)

*Recorded 2026-01-27*

---

## CRITICAL: fast-automation.sh EXECUTION

**Goal**: Zero failures, <10 minutes. If not met, fix before marking complete.

### Execution

```bash
# MANDATORY: 600000ms (10 min) timeout
./fast-automation.sh
```

### Outcomes

| Outcome | Action |
|---------|--------|
| SUCCESS (<10min, 0 failures) | Done |
| TIMEOUT (hit 10min) | Timeout remediation |
| FAILURES | Failure remediation |

### Timeout Remediation

**NEVER** "continue monitoring" after timeout. Process is DEAD.

1. Extract slowest: `pytest tests/ --durations=20 --collect-only -q`
2. Thresholds: >30s MUST exclude, >10s SHOULD optimize, >5s note
3. Fix or mark `@pytest.mark.slow` and exclude: `pytest tests/ -m "not slow"`
4. Re-run with 10min timeout until SUCCESS

### Failure Remediation

1. Identify all failures from output
2. Fix root cause (not symptoms)
3. Verify: `pytest tests/path/to/failing_test.py -v --tb=short`
4. Re-run fast-automation.sh until SUCCESS

### Performance Standards

| Metric | Target | Investigate | Exclude |
|--------|--------|-------------|---------|
| Individual test | <5s | >10s | >30s |
| Total suite | <10min | - | HARD KILL |

### Red Flags

- fast-automation.sh >10 min (HARD KILL)
- Any test >30s (exclude)
- Failures on untouched code (regression)
- Flaky tests (fix or exclude)

*Recorded 2026-01-28*

---

## GIT BRANCHING AND DEPLOYMENT

### Branch Structure

| Branch | Purpose | Direct Commits |
|--------|---------|----------------|
| `development` | Active work, version bumps | YES |
| `staging` | Staging env (.20 server) | NO (merge only) |
| `master` | Production env (.30 server) | NO (merge only) |

### Auto-Deployment

| Branch | Tag | Deploys To |
|--------|-----|------------|
| `staging` | v8.x.x | .20 server |
| `master` | v8.x.x | .30 server |
| `development` | any | No auto-deploy |

Tags transfer automatically during merges.

### Deploy to Staging

```bash
# 1. Ensure version tag in development
git checkout development
git describe --exact-match HEAD 2>/dev/null
# If no tag: bump version, commit, tag vX.Y.Z, push --tags

# 2. Merge to staging
git checkout staging && git merge development

# 3. Push (triggers .20 deployment)
git push origin staging
```

### Deploy to Production

```bash
# 1. Ensure staging has tag (if not, go back to development first)
git checkout staging && git describe --exact-match HEAD

# 2. Merge to master
git checkout master && git merge staging

# 3. Push (triggers .30 deployment)
git push origin master
```

### Branch Verification (MANDATORY)

Before ANY work: `git branch --show-current`

- OK: `development`, `feature/*`, `bugfix/*`
- WRONG: `staging`, `master` - STOP, ask user before proceeding

### CRITICAL: Deployment Workflow - NEVER SKIP

**ABSOLUTE PROHIBITION**: NEVER commit directly to `master` or `staging`. ALL changes flow through `development` first.

**The ONLY correct workflow**:

```
development → staging → master
     ↓            ↓          ↓
  (develop)    (.20)    (auto-deploy)
   bump/tag    test     production
```

**Step-by-step**:

1. **Development** (on `development` branch):
   - Make code changes
   - Run tests locally
   - Bump version in `__init__.py`, `CHANGELOG.md`, `README.md`
   - Create git tag: `git tag v8.8.X`
   - Push: `git push origin development --tags`

2. **Staging** (merge to `staging`):
   - `git checkout staging && git merge development && git push origin staging`
   - Auto-deploys to 192.168.60.20 (staging server)
   - **E2E TEST ON STAGING** - verify fix works in production-like environment

3. **Production** (merge to `master` ONLY after staging validation):
   - `git checkout master && git merge staging && git push origin master`
   - Production auto-deploys by pulling from master (NO direct access)

**WHY THIS MATTERS**: Committing directly to master skips staging validation and can deploy untested code to production.

**VIOLATION = DEPLOYMENT FAILURE**: If you commit to master without going through staging, you've broken the deployment pipeline.

*Recorded 2026-02-01, Updated 2026-02-03*

---

## 1. CRITICAL BUSINESS INSIGHT - Query is Everything

**Query capability = core value**. All CLI query features MUST be in MCP/REST APIs.

**Status** (2025-11-18):
- CLI: 23 params | MCP: 11 params (48% parity)
- P0 implemented: language, exclude_language, path_filter, exclude_path, file_extensions, accuracy
- Gap: FTS-specific (8), temporal (4)

**NEVER** remove/break query functionality. Query degradation = product failure.

---

## 2. Operational Modes

**Two modes** (simplified from three in v7.x):

| Mode | Storage | Use Case | Commands |
|------|---------|----------|----------|
| **CLI** | FilesystemVectorStore (`.code-indexer/index/`) | Single dev, local | `cidx init/index/query` |
| **Daemon** | Same + in-memory cache | Faster queries, watch | `cidx config --daemon`, `start`, `watch` |

**Shared characteristics**:
- Container-free, instant setup
- Vectors as JSON in `.code-indexer/index/{collection}/`
- Git-aware: blob hashes (clean), text content (dirty)

**Daemon-specific**:
- Unix socket: `.code-indexer/daemon.sock`
- ~5ms cached vs ~1s disk
- Auto-starts on first query when enabled

---

## 3. Architecture Details

**Full docs**: `/docs/architecture.md`

### Vector Storage (FilesystemVectorStore)

- Vectors as JSON in `.code-indexer/index/{collection}/`
- Quantization: Model dims (1024/1536) → 64-dim → 2-bit → filesystem path
- Performance: <1s query, <20ms incremental HNSW updates
- Thread-safe, atomic writes
- VoyageAI dimensions: 1024 (voyage-3), 1536 (voyage-3-large)

### Key Topics (See Docs)

- Incremental HNSW updates, change tracking, git-aware strategies (`architecture.md`)

---

## 4. Daily Development Workflows

### Test Suites

| Script | Tests | Time | Run From |
|--------|-------|------|----------|
| fast-automation.sh | 865+ | ~6-7min | Claude (600000ms timeout) |
| server-fast-automation.sh | Server-specific | varies | Claude |
| full-automation.sh | Complete | 10+min | User (1200000ms timeout) |
| GitHub Actions CI | ~814 | varies | Automatic |

**Principles**: Tests don't clean state (perf), container-free, E2E uses `cidx` CLI, slow tests excluded.

**Order**: Targeted unit tests → Manual testing → fast-automation.sh (final gate)

### Test Performance Management

**Thresholds**: <5s target, >10s investigate, >30s exclude

**Telemetry**: `pytest tests/ --durations=20 --tb=short -v`

**Analysis**:
1. Review 20 slowest tests
2. >5s: determine cause (I/O, missing fixtures, inefficient setup, inherent)
3. Action: optimize OR mark `@pytest.mark.slow` OR move to full-automation.sh

**Slow test config** (`pytest.ini`):
```toml
[tool.pytest.ini_options]
markers = ["slow: deselected by fast-automation.sh", "integration: may be slow"]
```

**Monitoring**:
```bash
pytest tests/ --durations=0 | grep "s call" | sort -rn -k1  # Identify slow
pytest tests/path/test.py::test_name --durations=0 -v       # Benchmark specific
```

**Red flags**: Suite >3min total, test >10s, duration +20% without changes, >50% variance (flaky)

### Linting & Quality

```bash
./lint.sh                           # ruff, black, mypy
git push && gh run list --limit 5   # Monitor CI
gh run view <run-id> --log-failed   # Debug failures
ruff check --fix src/ tests/        # Fix linting
```

**Zero tolerance**: Never leave GitHub Actions failed. Fix same session.

### Python Compatibility

Always: `python3 -m pip install --break-system-packages` (never bare `pip`)

### Documentation Updates

After feature: `./lint.sh` → verify README.md → verify `--help` → fix → re-verify

### Version Bump Checklist

**Update together**:
```
src/code_indexer/__init__.py:9    # __version__ (source of truth)
README.md:5                       # Version badge
CHANGELOG.md                      # New entry at top
docs/architecture.md:435          # Server response example
docs/query-guide.md:739,883       # Version references
```

**Check for stale**: `docs/mcpb/setup.md`, `docs/server-deployment.md`

**DO NOT change for CIDX bump**:
- `mcpb/__init__.py` (separate version 1.0.0)
- `server/app.py` (OpenAPI spec)
- `test-fixtures/` (test data)

**Verify**: `grep -r "OLD_VERSION" --include="*.md" --include="*.py" .`

---

## 5. Critical Rules (NEVER BREAK)

### Performance

**NEVER** add `time.sleep()` to production for UI visibility. Fix display logic.

### Progress Reporting (DELICATE)

**Pattern**:
- Setup: `progress_callback(0, 0, Path(""), info="Setup")` → scrolling
- Progress: `progress_callback(current, total, file, info="X/Y files...")` → progress bar

**Rules**:
- Single line at bottom with progress bar + metrics
- NO scrolling console feedback EVER
- Ask confirmation before ANY changes to progress
- Files: BranchAwareIndexer, SmartIndexer, HighThroughputProcessor

### Git-Awareness (CORE FEATURE)

**NEVER remove**:
- Git-awareness aspects
- Branch processing optimization
- Relationship tracking
- Deduplication of indexing

This makes CIDX unique. If refactoring removes this, **STOP**.

### Smart Indexer Consistency

Always consider `--reconcile` (non git-aware) and maintain feature parity.

### Configuration

- Temporary files: `~/.tmp` (NOT `/tmp`)
- Container-free: No ports, no containers

---

## 6. Performance & Optimization

### FTS Lazy Import

**NEVER** import Tantivy/FTS at module level in CLI startup files.

**Pattern**:
```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tantivy_index_manager import TantivyIndexManager

# Inside method:
if enable_fts:
    from .tantivy_index_manager import TantivyIndexManager
    fts_manager = TantivyIndexManager(fts_index_dir)
```

**Why**: Keeps `cidx --help` fast (~1.3s vs 2-3s)

**Verify**: `python3 -c "import sys; from src.code_indexer.cli import cli; print('tantivy' in sys.modules)"` → `False`

### Import Optimization

- voyageai: 440-630ms → 0ms (eliminated)
- CLI lazy loading: 736ms → 329ms
- Current: 329ms startup (acceptable)

---

## 7. Embedding Provider

### VoyageAI (ONLY PROVIDER in v8.0+)

**Token Counting**:
- Use `embedded_voyage_tokenizer.py`, NOT voyageai library
- Critical for 120,000 token/batch API limit
- Lazy imports, caches per model (0.03ms)
- 100% identical to `voyageai.Client.count_tokens()`
- **DO NOT remove/replace** without extensive testing

**Batch Processing**: 120,000 token limit enforced, automatic batching, transparent splitting

**Models**:
| Model | Dimensions | Notes |
|-------|------------|-------|
| voyage-3 (default) | 1024 | Best balance |
| voyage-3-large | 1536 | Highest quality |

---

## 8. CIDX Usage Quick Reference

### CLI Mode

```bash
cidx init                           # Create .code-indexer/
cidx index                          # Index codebase
cidx query "authentication" --quiet # Semantic search
cidx query "def.*" --fts --regex    # FTS/regex search
```

**Key flags** (ALWAYS use `--quiet`):
- `--limit N` - Results (start 5-10 to conserve context)
- `--language python` - Filter by language
- `--path-filter */tests/*` - Path pattern
- `--min-score 0.8` - Similarity threshold
- `--accuracy high` - Higher precision

**Search decision**:
- "What code does", "Where is X" → CIDX
- Exact strings (variables, config) → grep/find

### Daemon Mode

```bash
cidx config --daemon   # Enable
cidx start             # Start (auto-starts on first query)
cidx query "..."       # Uses cached indexes
cidx watch             # Real-time indexing
cidx watch-stop        # Stop watch
cidx stop              # Stop daemon
```

---

## 9. Miscellaneous

### Local Testing and Deployment

**Config file**: `.local-testing` (gitignored) - contains CIDX server credentials, Mac laptop credentials, deployment commands.

### Running Local CIDX Server

```bash
# Start (from project root)
PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000

# Background
PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000 &
```

**Why this command**:
- `PYTHONPATH=./src` - code_indexer not installed as package in dev
- `python3 -m uvicorn` - uses correct Python env
- `--host 0.0.0.0` - external access
- `--port 8000` - standard local dev port

**Verify**:
```bash
curl -s http://localhost:8000/docs | head -5
curl -s -X POST http://localhost:8000/mcp-public -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}'
```

**Stop**: `pkill -f "uvicorn code_indexer.server.app"`

**Errors**:
- `No module named 'code_indexer'` → Missing `PYTHONPATH=./src`
- `No module named 'fastapi'` → Use `python3 -m uvicorn`
- Exits immediately → Port 8000 in use

*Recorded 2026-01-23*

### Claude CLI Integration

The CIDX server uses two separate mechanisms for executing Claude CLI commands, each optimized for different use cases:

#### 1. ClaudeCliManager (Queue-Based)

**Location**: `src/code_indexer/server/services/claude_cli_manager.py`

**Architecture**:
- Thread pool with configurable worker count (default: 2)
- Work queue for job submission
- Controlled concurrency via `max_concurrent_claude_cli` setting

**Used For**:
- Golden repository registration (generates repo description)
- Catch-up processing for repos registered before Claude integration

**Configuration**:
- Web UI Config Screen: "Max Concurrent Claude CLI" setting
- Respects server-wide concurrency limits
- Jobs queued when all workers busy

**Key Methods**:
```python
manager.submit_work(job_id, repo_path, prompt)  # Queues work
manager.get_job_status(job_id)                  # Check status
```

#### 2. ResearchAssistantService (Direct Threading)

**Location**: `src/code_indexer/server/services/research_assistant_service.py`

**Architecture**:
- Direct `threading.Thread(daemon=True)` per request
- No queue, no concurrency limits
- Messages persisted to SQLite immediately
- In-memory `_jobs` dict for active job tracking

**Used For**:
- Admin Web UI "Research Assistant" tab
- Interactive chat investigations with Claude

**Behavior**:
- Submit message → immediate thread spawn
- Response stored in SQLite when complete
- Navigate away and back → response persists (fetched from DB)
- If job not in `_jobs` memory dict, falls back to database lookup

**Why Separate Systems**:
- ClaudeCliManager: Batch processing, needs rate limiting for API costs
- ResearchAssistantService: Interactive UX, immediate response expected

#### General Guidelines

- NO FALLBACKS - research and propose solutions
- JSON errors: Use `_validate_and_debug_prompt()`, check non-ASCII/long lines/quotes

*Recorded 2026-02-06*

---

## 10. MCP Tool Documentation

Externalized to markdown files in `src/code_indexer/server/mcp/tool_docs/`:

```
admin/    # Users, groups, auth, maintenance
cicd/     # GitHub Actions, GitLab CI
files/    # File CRUD
git/      # Git operations
guides/   # User guides, quick reference
repos/    # Repository management
scip/     # SCIP code intelligence
search/   # Search and browse
ssh/      # SSH key management
```

### Format

```yaml
---
name: tool_name
category: search
required_permission: query_repos
tl_dr: Brief description.
quick_reference: true  # Optional
---
Full description here.
```

### Scripts

- Convert: `python3 tools/convert_tool_docs.py` (generates 128 .md from TOOL_REGISTRY)
- Verify: `python3 tools/verify_tool_docs.py` (CI gate)

### Adding Tools

1. Add to TOOL_REGISTRY in `src/code_indexer/server/mcp/tools.py`
2. Run convert script
3. Run verify script
4. Tests validate backward compatibility

### Related Files

- `tool_doc_loader.py` - Runtime loader with caching
- `tests/unit/tools/test_convert_tool_docs.py`, `test_verify_tool_docs.py`
- `tests/unit/server/mcp/test_tool_doc_*.py`

---

## 11. CIDX Server E2E Testing Workflow

**Prerequisites**: Server at localhost:8000, credentials admin/admin

### Workflow

```bash
# 1. Login
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r '.access_token')

# 2. List repos
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_repositories","arguments":{}}}'

# 3. Add golden repo (if needed)
curl -s -X POST http://localhost:8000/admin/golden-repos/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "url=git@github.com:org/repo.git&alias=my-repo&description=Description"

# 4. Query (NOTE: use "query_text" not "query")
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_code","arguments":{"query_text":"your query","repository_alias":"repo-alias-global","limit":5}}}'
```

### Key Notes

- Auth: `/auth/login` with JSON (NOT form-urlencoded, NOT `/admin/login`)
- Query param: `query_text` (not `query`)
- Global repos: `-global` suffix (e.g., `code-indexer-python-global`)
- Token expiry: 10 minutes
- Timing display: CLI only, not in MCP/REST

*Recorded 2026-01-29*

---

## 12. Where to Find More

- **Architecture**: `/docs/architecture.md`
- **This file**: Day-to-day development essentials and mode-specific context
