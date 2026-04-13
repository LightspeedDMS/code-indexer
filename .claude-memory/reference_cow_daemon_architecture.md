---
name: CoW Storage Daemon Architecture
description: How CoW Storage Daemon integrates with CIDX — REST API for lifecycle, NFS for filesystem access
type: reference
---

# CoW Storage Daemon — Architecture and Integration Model

## What It Is
Lightweight REST daemon replacing ONTAP/FSx for dev/non-prod clusters. Runs on any Linux with XFS reflink=1 or btrfs.

Repo: `/home/jsbattig/Dev/cow-storage-daemon`

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
- Config fields: `base_path`, `api_key`, `port` (8081), `host` (0.0.0.0), `allowed_source_roots`, `health_requires_auth`
- API key stored in config — restart daemon to change
- Returns **relative** clone paths (e.g., `cidx/clone-name`) — clients prepend NFS mount point
- Never auto-deletes clones — clients responsible for lifecycle

## Clone Creation Flow
1. `POST /api/v1/clones` with `{source_path, namespace, name}` → returns `job_id` (202)
2. Poll `GET /api/v1/jobs/{job_id}` until `completed` or `failed`
3. Prepend NFS mount to `clone_path` for absolute filesystem path

## This Machine Setup
- Filesystem: XFS on `/home` with `reflink=1` — confirmed compatible
- Install: `./scripts/install-cow-daemon.sh --storage-path /home/jsbattig/cow-storage`
- Credentials stored in `.local-testing`

## CIDX Integration Status
As of 2026-04-11: CIDX does NOT yet have a CowStorageDaemonClient.
`VersionedSnapshotManager` has ONTAP mode (via `OntapFlexCloneClient`) and direct `cp --reflink=auto` mode.
A `CowStorageDaemonClient` needs to be built — mirrors `OntapFlexCloneClient` structure.
Config: `CowDaemonConfig` dataclass needs adding to `config_manager.py` with `endpoint`, `api_key`, `namespace`, `mount_point`.
