---
name: CoW Storage Daemon Architecture
description: How CoW Storage Daemon integrates with CIDX — REST API for lifecycle, NFS for filesystem access
type: reference
originSessionId: 04fcbccb-cd14-4e4f-94da-218d94a53f94
---
# CoW Storage Daemon — Architecture and Integration Model

## What It Is
Lightweight REST daemon replacing ONTAP/FSx for dev/non-prod clusters. Runs on any Linux with XFS reflink=1 or btrfs.

Repo: `cow-storage-daemon` (separate repo, sibling checkout)

## Integration Model (SAME as ONTAP pattern)

| Layer | ONTAP | CoW Daemon |
|-------|-------|-----------|
| Create clone | ONTAP REST API (`storage/volumes`) | `POST /api/v1/clones` |
| Delete clone | ONTAP REST API | `DELETE /api/v1/clones/{ns}/{name}` |
| Read/write clone contents | NFS mount | NFS mount (same `base_path` exported) |
| Auth | basic auth | Bearer token (`Authorization: Bearer <api_key>`) |

## Key Details

- Daemon listens on port **8081** by default
- Config: `/etc/cow-storage-daemon/config.json` (installed via `scripts/install-cow-daemon.sh`)
- Config fields: `base_path`, `api_key`, `port` (8081), `host` (bind-all), `allowed_source_roots`, `health_requires_auth`
- API key stored in config — restart daemon to change
- Returns **relative** clone paths (e.g., `cidx/clone-name`) — clients prepend NFS mount point
- Never auto-deletes clones — clients responsible for lifecycle

## Clone Creation Flow
1. `POST /api/v1/clones` with `{source_path, namespace, name}` → returns `job_id` (202)
2. Poll `GET /api/v1/jobs/{job_id}` until `completed` or `failed`
3. Prepend NFS mount to `clone_path` for absolute filesystem path

## Host Setup Prerequisites
- Filesystem: XFS (or btrfs) with `reflink=1` on the storage root — required for CoW clones
- Install: `./scripts/install-cow-daemon.sh --storage-path <storage-root>` (a reflink-capable XFS/btrfs path)
- Credentials stored in `.local-testing`

## CIDX Integration Status — WIRED (Story #510, as of 2026-04-14)

CIDX HAS a working CoW daemon client. Do NOT assume it still needs to be built.

**Implementation**:
- `CowDaemonBackend` class: `src/code_indexer/server/storage/shared/clone_backend.py:215` — full REST client (create_clone, delete_clone, list_clones, clone_exists, `_poll_job` with exponential backoff)
- `CloneBackendFactory.create(clone_backend_type="cow-daemon", cow_daemon_config=...)` at `clone_backend.py:355`
- Startup wiring: `src/code_indexer/server/startup/clone_backend_wiring.py:65` `build_snapshot_manager()` — selects backend from `config.clone_backend` ∈ {"local", "ontap", "cow-daemon"}
- Fail-fast health check: `_check_daemon_health()` calls `GET /api/v1/health` at startup — RuntimeError if unreachable, NO fallback
- Fail-fast NFS check: `_check_nfs_mount()` validates NFS mount via `NfsMountValidator`
- Injected into `VersionedSnapshotManager(clone_backend=backend)` — used by all golden-repo lifecycle services
- Config: `CowDaemonConfig` already exists in `config_manager.py` (`daemon_url`, `api_key`, `mount_point`, `poll_interval_seconds`, `timeout_seconds`)
- Uses lazy-imported `requests` library inside `CowDaemonBackend._requests()` to keep startup fast

**Hot-path endpoints actually called**:
- `POST /api/v1/clones` — create
- `GET /api/v1/jobs/{job_id}` — poll (exponential backoff, max 30s)
- `DELETE /api/v1/clones/{namespace}/{name}` — delete (404 = success, idempotent)
- `GET /api/v1/clones?namespace={ns}` — list
- `GET /api/v1/clones/{namespace}/{name}` — exists (200/404)
- `GET /api/v1/health` — startup only

**Coexists with**: `LocalCloneBackend` (`cp --reflink=auto`) and `OntapCloneBackend` (wraps `OntapFlexCloneClient`). All three implement the `CloneBackend` Protocol.

**Before citing this file**: verify `clone_backend.py:215` still shows `class CowDaemonBackend` — if it moved, update line refs.
