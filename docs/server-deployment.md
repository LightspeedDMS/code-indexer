# CIDX Server Deployment Guide

This guide covers deploying and configuring the CIDX server for multi-user team collaboration with server-side performance optimizations.

Note: This guide covers standalone (single-node) deployment with SQLite storage. For multi-node cluster deployment with PostgreSQL, see [Cluster Architecture](cluster-architecture.md) and [Cluster Setup Guide](cluster-setup.md).

## Overview

CIDX server provides:
- Multi-user semantic code search
- Server-side HNSW index caching (100-1800x speedup)
- OAuth 2.0 authentication
- RESTful API and MCP protocol support
- Per-repository isolation
- Automatic cache management with TTL-based eviction

## System Requirements

### Minimum Requirements
- Python 3.10 or later
- 4GB RAM (8GB+ recommended for large repositories)
- 10GB disk space (scales with repository size)
- Linux/macOS/Windows (Linux recommended for production)

### Network Requirements
- Port 8000 for HTTP API (configurable)
- Port 8383 for MCP protocol (configurable)
- Outbound HTTPS for VoyageAI API (embedding generation)

## Installation

### Option 1: pipx (Recommended)

```bash
# Install code-indexer
pipx install git+https://github.com/LightspeedDMS/code-indexer.git@v8.13.0

# Verify installation
cidx --version
```

### Option 2: pip with virtual environment

```bash
# Create virtual environment
python3 -m venv cidx-venv
source cidx-venv/bin/activate

# Install code-indexer
pip install git+https://github.com/LightspeedDMS/code-indexer.git@v8.13.0

# Verify installation
cidx --version
```

## Configuration

### Environment Variables

Create `/etc/cidx-server/config.env` (or `~/.cidx-server/config.env` for user-level):

```bash
# VoyageAI API Key (required for embedding generation)
VOYAGE_API_KEY=your-voyage-api-key-here

# Server Data Directory
CIDX_SERVER_DATA_DIR=/var/lib/cidx-server  # Default: ~/.cidx-server

# Server Ports
CIDX_SERVER_PORT=8000     # HTTP API port
CIDX_MCP_PORT=8383        # MCP protocol port

# Logging
CIDX_LOG_LEVEL=INFO       # DEBUG, INFO, WARNING, ERROR
CIDX_LOG_FILE=/var/log/cidx-server/server.log
```

### Configuration File

Alternative to environment variables, create `~/.cidx-server/config.json`:

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8000,
    "data_dir": "/var/lib/cidx-server",
    "log_level": "INFO"
  },
  "cache": {
    "hnsw_index_ttl_seconds": 600,
    "enable_auto_cleanup": true,
    "cleanup_interval_seconds": 60
  },
  "embedding": {
    "provider": "voyageai",
    "model": "voyage-3",
    "api_key_env": "VOYAGE_API_KEY"
  },
  "auth": {
    "enabled": true,
    "jwt_secret_key": "generate-with-openssl-rand-hex-32",
    "token_expiry_minutes": 10
  }
}
```

## HNSW Index Cache Configuration

The server includes automatic HNSW index caching for massive query performance improvements.

### Cache Behavior

**Without Cache (CLI Mode)**:
- Each query loads HNSW index from disk
- Typical query time: 200-400ms (with OS page cache)
- Suitable for individual developers, single-user workflows

**With Cache (Server Mode)**:
- HNSW indexes cached in memory after first query
- Cold query (cache miss): ~277ms
- Warm query (cache hit): <1ms
- Speedup: 100-1800x for repeated queries
- Suitable for multi-user teams, high-query workloads

### Cache Configuration Options

#### TTL (Time-To-Live)

Configure how long HNSW indexes remain in cache via the Web UI Configuration Screen or `config.json`:

```json
{
  "cache": {
    "hnsw_index_ttl_seconds": 600
  }
}
```

**Recommendations**:
- **Small teams (1-5 users)**: 600 seconds (10 minutes)
- **Medium teams (5-20 users)**: 1800 seconds (30 minutes)
- **Large teams (20+ users)**: 3600 seconds (1 hour)
- **High-frequency queries**: 7200 seconds (2 hours)

**Memory Considerations**:
- Each cached HNSW index: 50-500MB (depends on repository size)
- Monitor memory usage and adjust TTL accordingly
- Longer TTL = better performance but higher memory usage

#### Per-Repository Isolation

Cache automatically isolates HNSW indexes by repository path:
- Each repository has independent cache entry
- No cross-repository cache contamination
- Independent TTL tracking per repository

Example:
```bash
# Repository A and Repository B each have separate cache entries
# Query to Repo A doesn't affect Repo B's cache
```

#### Background Cleanup

Automatic background thread removes expired cache entries:

```json
{
  "cache": {
    "enable_auto_cleanup": true,
    "cleanup_interval_seconds": 60  # Check every 60 seconds
  }
}
```

### Monitoring Cache Performance

#### Cache Statistics Endpoint

Query real-time cache statistics:

```bash
curl http://localhost:8000/cache/stats
```

Response:
```json
{
  "total_hits": 1234,
  "total_misses": 56,
  "hit_ratio": 0.957,
  "active_entries": 12,
  "per_repository": {
    "/path/to/repo1": {
      "hits": 500,
      "misses": 10,
      "last_access": "2025-11-30T12:34:56Z"
    },
    "/path/to/repo2": {
      "hits": 734,
      "misses": 46,
      "last_access": "2025-11-30T12:35:12Z"
    }
  }
}
```

#### Key Metrics

- **hit_ratio**: Percentage of queries served from cache (target: >80%)
- **total_hits**: Number of cache hits (warm queries)
- **total_misses**: Number of cache misses (cold queries)
- **active_entries**: Number of repositories currently cached

#### Performance Expectations

| Scenario | Response Time | Cache Status | Notes |
|----------|---------------|--------------|-------|
| First query to repository | 200-400ms | Miss | Loads from disk, benefits from OS cache |
| Subsequent queries (within TTL) | <1ms | Hit | Served from memory cache |
| Query after TTL expiration | 200-400ms | Miss | Cache rebuild required |
| Concurrent queries to same repo | <1ms | Hit | Shared cache across users |

### Troubleshooting Cache Issues

#### Cache Not Activating

**Symptom**: All queries show cache miss behavior (slow)

**Diagnosis**:
```bash
# 1. Check cache configuration via Web UI Configuration Screen

# 2. Verify process is running
PID=$(pgrep -f "code_indexer.server.app")

# 3. Check cache stats endpoint
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8000/cache/stats
# If "cached_repositories" is always 0, cache not working
```

**Solution**:
```bash
# 1. Configure cache TTL via Web UI Configuration Screen

# 2. Reload systemd configuration
sudo systemctl daemon-reload

# 3. Restart service
sudo systemctl restart cidx-server

# 4. Verify cache is configured
# Check Web UI Configuration Screen for cache settings

# 5. Test cache activation
# Make a query, then check /cache/stats for hit_count > 0
```

#### High Memory Usage

**Symptom**: Server consuming excessive memory

**Diagnosis**:
```bash
# Check number of active cache entries
curl http://localhost:8000/cache/stats | jq '.active_entries'

# Monitor memory usage
ps aux | grep cidx-server
```

**Solutions**:
1. Reduce TTL to evict entries more frequently via Web UI Configuration Screen (set cache TTL to 300 seconds / 5 minutes)

2. Restart server to clear cache:
   ```bash
   systemctl restart cidx-server
   ```

3. Limit number of repositories indexed on server

#### Low Hit Ratio

**Symptom**: hit_ratio below 50%

**Diagnosis**:
```bash
curl http://localhost:8000/cache/stats | jq '.hit_ratio'
```

**Possible Causes**:
1. TTL too short (entries evicted before reuse)
2. Low query volume (few repeat queries)
3. Many different repositories queried (cache fragmentation)

**Solutions**:
1. Increase TTL for high-query-volume environments
2. Analyze query patterns to optimize cache usage
3. Consider increasing server memory to cache more repositories

## Running the Server

### Development Mode

```bash
# Start server in foreground (for testing)
python3 -m code_indexer.server.app
```

### Production Deployment with systemd

**RECOMMENDED**: Use the provided deployment script and template:

```bash
cd deployment/
sudo ./deploy-server.sh YOUR_VOYAGE_API_KEY
```

This script automatically handles deployment configuration and verification.

**Manual Installation** (if automated script cannot be used):

Create `/etc/systemd/system/cidx-server.service`:

```ini
[Unit]
Description=CIDX Semantic Code Search Server
After=network.target

[Service]
Type=simple
User=cidx-server
Group=cidx-server
WorkingDirectory=/var/lib/cidx-server

# Load additional environment variables from file
EnvironmentFile=/etc/cidx-server/config.env

ExecStart=/usr/local/bin/python3 -m code_indexer.server.app

Restart=always
RestartSec=10

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/cidx-server /var/log/cidx-server

StandardOutput=append:/var/log/cidx-server/server.log
StandardError=append:/var/log/cidx-server/error.log

[Install]
WantedBy=multi-user.target
```


Start and enable service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable cidx-server
sudo systemctl start cidx-server
sudo systemctl status cidx-server
```

### Server Management

```bash
# Start server
sudo systemctl start cidx-server

# Stop server
sudo systemctl stop cidx-server

# Restart server (clears cache)
sudo systemctl restart cidx-server

# Check status
sudo systemctl status cidx-server

# View logs
sudo journalctl -u cidx-server -f
```

## Security Considerations

### Authentication

CIDX server uses OAuth 2.0 with JWT tokens:

```bash
# User authentication flow
1. User logs in via browser
2. Server issues JWT access token
3. Client includes token in API requests
4. Server validates token on each request
```

### Network Security

Recommended production setup:

```bash
# Run server behind reverse proxy (nginx/haproxy)
# Terminate SSL at proxy level
# Forward to CIDX server on localhost:8000

# Example nginx config
server {
    listen 443 ssl;
    server_name cidx.example.com;

    ssl_certificate /etc/ssl/certs/cidx.crt;
    ssl_certificate_key /etc/ssl/private/cidx.key;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### File Permissions

```bash
# Create dedicated user for server
sudo useradd -r -s /bin/false cidx-server

# Set directory permissions
sudo mkdir -p /var/lib/cidx-server
sudo chown cidx-server:cidx-server /var/lib/cidx-server
sudo chmod 700 /var/lib/cidx-server

# Set log permissions
sudo mkdir -p /var/log/cidx-server
sudo chown cidx-server:cidx-server /var/log/cidx-server
sudo chmod 750 /var/log/cidx-server
```

## Performance Tuning

### Cache Optimization

For maximum cache benefit:

1. **Monitor hit ratio**: Target >80% for high-query environments
2. **Adjust TTL**: Balance memory usage vs. cache effectiveness
3. **Pre-warm cache**: Query common repositories during server startup
4. **Memory allocation**: Ensure sufficient RAM for expected cache size

### Repository Management

Best practices:

1. **Index frequently-queried repositories**: Priority indexing for active projects
2. **Schedule re-indexing**: Off-hours re-indexing to minimize cache disruption
3. **Repository isolation**: Separate large monorepos to independent cache entries

### Scaling Considerations

For large deployments:

1. **Horizontal scaling**: Run multiple server instances behind load balancer
2. **Shared storage**: Use shared filesystem for repository data
3. **Cache distribution**: Each server maintains independent cache (no shared cache needed)
4. **Monitoring**: Track per-server cache statistics and memory usage

## Monitoring and Maintenance

### Health Check Endpoint

```bash
# Check server health
curl http://localhost:8000/health

# Expected response
{
  "status": "healthy",
  "version": "8.13.0",
  "cache": {
    "enabled": true,
    "active_entries": 12
  }
}
```

### Log Analysis

Monitor server logs for issues:

```bash
# View recent logs
sudo tail -f /var/log/cidx-server/server.log

# Search for cache-related logs
sudo grep -i "cache" /var/log/cidx-server/server.log

# Monitor error rate
sudo grep -i "error" /var/log/cidx-server/error.log | wc -l
```

### Backup and Recovery

Critical data to backup:

1. **Repository indexes**: `/var/lib/cidx-server/repositories/`
2. **Configuration**: `/etc/cidx-server/config.env`, `~/.cidx-server/config.json`
3. **User data**: `/var/lib/cidx-server/users/`

Cache data (HNSW indexes) can be rebuilt, no backup needed.

## Troubleshooting

### Common Issues

#### Server Won't Start

```bash
# Check logs
sudo journalctl -u cidx-server -n 50

# Common causes:
# - Missing VOYAGE_API_KEY
# - Port already in use
# - Permission issues
```

#### Slow Query Performance

```bash
# Check if cache is working
curl http://localhost:8000/cache/stats

# If hit_ratio is low:
# 1. Increase TTL
# 3. Monitor memory usage
```

#### Memory Issues

```bash
# Check memory usage
free -h
ps aux | grep cidx-server

# If high memory usage:
# 1. Reduce TTL
# 2. Reduce number of cached repositories
# 3. Restart server to clear cache
```

## Post-Generation Verification Pass (Story #724)

The cidx-server supports an optional post-generation verification pass that
re-reads generated dependency-map artifacts and repo descriptions against
their actual source code and produces a corrected version with evidence
citations.

**Default: disabled.** Enable via the Admin Web UI Config Screen, Claude
CLI Integration section, "Post-generation verification pass (fact-check)"
toggle.

### Latency cost

Verification invokes Claude CLI once per generated document. Expected cost:

- Per-invocation: approximately 20-45 seconds (within the configured timeout).
- Per refresh cycle: adds verification time multiplied by the number of
  generated artifacts (per-domain for dependency maps, once for descriptions).

Before enabling in production, run the baseline measurement procedure
described in the story: measure the fabrication rate on a fixed 3-repo corpus
both with and without verification to confirm the corrections justify the cost.

### Configuration fields

- `dep_map_fact_check_enabled` (bool, default `false`): master toggle.
- `fact_check_timeout_seconds` (int, default `600`): per-invocation timeout.
  Accepts 60-3600.

### Known limitations

- Changes to `max_concurrent_claude_cli` require a service restart to take
  effect. The shared subprocess semaphore that bounds total Claude CLI
  concurrency is initialized once at process start and does not resize at
  runtime. Restart the `cidx-server` systemd unit after changing this field.
- Verification is a heuristic second pass using the same model class as the
  generator. It reduces hallucinated claims but does not guarantee factual
  correctness.
- Source-code bugs found by verification are NOT automatically corrected in
  deployed code; the feature only corrects the generated markdown artifacts.
  Code-level bugs are filed as GitHub issues per the existing bug-report flow.

## cidx-meta backup to remote git (Story #926)

The cidx-meta directory holds description files and metadata for every registered
golden repository.  You can configure the server to keep a continuous git backup
of this directory in a remote repository so that the metadata can be recovered
after data loss.

### Configuration

1. Navigate to the Web UI Config Screen: `/admin/config`.
2. Open the "cidx-meta backup" section.
3. Set "Enabled" to `true`.
4. Enter the remote URL in the "Remote URL" field (see URL formats below).
5. Click Save.

URL formats accepted:

- `git@github.com:org/repo.git` — SSH (requires SSH key, see below)
- `https://github.com/org/repo.git` — HTTPS
- `file:///path/to/bare.git` — local bare repository (useful for testing)

### SSH key prerequisite

For `git@host:` URLs, the server must have an SSH key registered for that
hostname before saving the configuration.  To add a key:

1. Go to the SSH Keys page in the admin UI.
2. Create or import a key and assign it to the target hostname.
3. Return to the Config Screen and save the cidx-meta backup settings.

`file://` and `https://` URLs skip the SSH key check.

### Bootstrap behavior

The first time a remote URL is saved (or when the URL changes), the server
runs `CidxMetaBackupBootstrap.bootstrap()`:

- If `.git/` does not exist in the cidx-meta directory, git is initialized,
  all current files are committed, and a force-push is made to the remote.
- If `.git/` exists and the remote URL is unchanged, the call is a no-op.
- If the URL changed, `git remote set-url` is issued and a force-push is made.

Bootstrap is idempotent: running it multiple times with the same URL is safe.

### Sync on refresh

Every time the cidx-meta refresh job runs and backup is enabled:

1. `MetaDirectoryUpdater` creates or removes description files on disk.
2. `CidxMetaBackupSync` stages all changes (`git add -A`), commits, fetches
   from origin, rebases local commits on top of remote changes, and pushes.
3. If the rebase produces conflicts, Claude CLI is invoked to resolve them
   (600 s timeout; SIGTERM then SIGKILL after 30 s if exceeded).
4. If conflict resolution fails, `git rebase --abort` reverts the repo and
   the refresh job is marked FAILED with the resolution error as the reason.
5. If the push itself fails (e.g. network error), the job is also marked FAILED
   but indexing still runs (deferred-failure pattern).

### Recovery procedure

To recover cidx-meta from the remote backup:

1. Stop the `cidx-server` service.
2. Remove or rename the existing cidx-meta directory:
   `mv ~/.cidx-server/data/golden-repos/cidx-meta ~/.cidx-server/data/golden-repos/cidx-meta.bak`
3. Clone the remote backup into that location:
   `git clone <remote-url> ~/.cidx-server/data/golden-repos/cidx-meta`
4. Restart `cidx-server`.
5. Trigger a manual refresh for `cidx-meta-global` from the admin UI to
   re-index the recovered metadata.

### Operational notes

- URL change idempotency: changing the URL in the Web UI triggers
  `CidxMetaBackupBootstrap.bootstrap()` at Save time.  The next scheduled
  refresh cycle also runs bootstrap at the start so URL changes applied via
  direct DB edits are picked up automatically.
- Deferred-failure pattern: indexing always runs after sync regardless of
  whether push succeeded.  A failed push surfaces as a FAILED job with the
  push error included in the failure reason.
- Claude conflict resolver timeout: 600 s per invocation.  On timeout,
  SIGTERM is sent first; SIGKILL follows after a 30 s grace period.
- The mutable base path for cidx-meta is always
  `<server_data_dir>/data/golden-repos/cidx-meta/`.  Git operations NEVER
  run inside `.versioned/` snapshot directories.

## Additional Resources

- [Main README](../README.md) - Project overview and features
- [GitHub Repository](https://github.com/LightspeedDMS/code-indexer) - Source code and issues
