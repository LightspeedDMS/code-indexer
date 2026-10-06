# Data Migration Playbook

Operator procedure for the two on-disk data migrations that convert legacy storage layouts. Both are off by
default, both are switched on in the Web UI configuration screen, and both delete legacy files once enabled, so they
are deliberate, operator-gated actions.

## The two migrations

| | Temporal Legacy Migration | Fleet Migration |
|---|---|---|
| Web UI section | Temporal Legacy Migration | Fleet Migration |
| Config object | `temporal_legacy_migration_config` | `fleet_migration_config` |
| What it does | Moves temporal shards stored inside a golden repository to the fixed server location `<golden_repos_dir>/.temporal/<alias>/` | Consolidates each collection's `vector_*.json` files into one `chunks.db`, one golden repository at a time |
| Flags (defaults) | `relocation_enabled` (`false`), `cleanup_authorized` (`false`) | `enabled` (`false`), `tick_interval_minutes` (`30`), `canary_gate_enabled` (`false`) |
| Deletes | The in-repo copy, only when `cleanup_authorized` is on | The legacy JSON files after a verified consolidation, only while `enabled` is on |

`relocation_enabled` gates the non-destructive copy and publish; `cleanup_authorized` independently gates deleting
the old in-repo copy. Relocation moves shards; it does not change their storage layout.

Both layouts (`vector_*.json` and `chunks.db`) are fully readable, so neither migration is needed for queries to
work. Consolidation reduces file count; it is not a correctness fix.

## How a change takes effect

Each scheduler reads its section from the server's configuration on every tick; no restart is needed to start or
stop them, with one exception:

- The process that served the Web UI write sees it immediately.
- In cluster mode, every other process re-reads the shared configuration every 30 seconds.
- In standalone mode with more than one uvicorn worker, workers do not reload configuration from the database.
  Restart the server after changing either section.

Configuration writes are compare-and-set against the stored version: a write based on an outdated copy is retried
on the current row or rejected with a conflict, so two changes made close together do not overwrite each other.
The rendered configuration page can still show an older value for a few seconds when it is served by a process
that has not re-read the row yet; confirm a change by its effect (jobs appearing, files moving).

## Duplicate prevention

Two layers, both in the shared database, hold across workers and nodes:

1. Job registration through `register_job_if_no_conflict()` on the `idx_active_job_per_repo` unique index. The
   fleet scheduler registers under the fixed alias `fleet-migration-scheduler`, so only one tick runs fleet-wide; a
   duplicate is skipped.
2. The per-repository write lock. A migration that reaches a repository whose lock is held completes with result
   status `lock_held`; one that finds a refresh in progress completes with `refresh_in_flight`. Both are deferrals,
   and the repository is retried on a later tick.

## Pre-flight

1. **Back up** the golden repositories directory. Once deletion runs, the only way back is a restore.
2. **Confirm every node runs a version that reads `chunks.db`** before enabling fleet migration in a cluster: the
   admin dashboard shows each node's server version. A node without the dual-layout reader would see a
   consolidated collection as empty.
3. **Take an inventory** of the legacy files. This is a rough count for planning, not the layout authority (the
   server decides layout per collection through `resolve_chunk_layout()`):

   ```bash
   GR=~/.cidx-server/data/golden-repos
   for r in "$GR"/*/; do
     idx="$r.code-indexer/index"; [ -d "$idx" ] || continue
     echo "$(basename "$r") json=$(find "$idx" -name 'vector_*.json' | wc -l) chunks_db=$(find "$idx" -maxdepth 2 -name chunks.db | wc -l)"
   done
   ```

4. Prefer a window without running refreshes or indexing; migrations defer around them, which is safe but slower.

## Procedure

Run the temporal relocation first, then fleet migration.

### Step 1: Elevate

Configuration writes require TOTP step-up elevation when enforcement is on. In the Web UI, the configuration
screen prompts for it (`POST /auth/elevate-form`). `POST /auth/elevate` elevates an API (JWT) session only, not a
Web UI session. See [Login and Elevation](auth/login-and-elevation.md).

A configuration write that is rejected returns a non-200 status (400, 401 or 403).

### Step 2: Temporal relocation

Web UI, Configuration, Temporal Legacy Migration:

1. Set `relocation_enabled` to Yes.
2. Wait until the relocation jobs complete and the repositories answer temporal queries (see Verification).
3. Then set `cleanup_authorized` to Yes to delete the in-repo copies.

Setting both at once also works, but leaves no window to compare old and new.

### Step 3: Fleet migration

Web UI, Configuration, Fleet Migration:

- `enabled`: Yes.
- `tick_interval_minutes`: how often the scheduler picks the next repository (each tick submits at most one
  repository).
- `canary_gate_enabled`: leave it No. When on, the sweep pauses after the first repository until
  `FleetMigrationScheduler.confirm_canary()` is called, and no REST, MCP or Web UI endpoint calls it, so the sweep
  cannot be resumed.

A large repository can take hours; the job has no wall-clock timeout. The sweep handles one repository at a time.

### Step 4: Switch the flags off

When the inventory shows no `vector_*.json` left and temporal data sits under `.temporal/`, set both sections back
to their defaults so nothing can delete files unattended.

## Verification

On disk, per repository:

- Semantic collections: `<repo>/.code-indexer/index/<collection>/chunks.db`, with `hnsw_index.bin` and
  `collection_meta.json`, and no `vector_*.json`.
- Temporal data: `<golden_repos_dir>/.temporal/<alias>/code-indexer-temporal-<embedder>-<quarter>/`. Two
  kinds of directory there are bookkeeping, not shards, and normally hold no vector data: the bare
  `code-indexer-temporal` (the shared temporal metadata store) and the per-embedder `code-indexer-temporal-<embedder>`
  without a quarter suffix. Fleet migration skips both; if one does contain vector data it is reported as an anomaly
  and is neither consolidated nor deleted. Temporal relocation, by contrast, moves them to `.temporal/<alias>/`
  together with the shards.

Through the REST front door:

```bash
BASE=http://localhost:8000
T=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<password>"}' | jq -r .access_token)

# semantic
curl -s -X POST $BASE/api/query -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"query_text":"authentication","repository_alias":"example-repo-global","limit":3}'

# temporal
curl -s -X POST $BASE/api/query -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"query_text":"authentication","repository_alias":"example-repo-global","time_range_all":true,"limit":3}'
```

Also query an activated repository: activations contain only semantic collections and read temporal data from the
golden repository's `.temporal/` location.

Job outcomes in the admin jobs view (`/admin/jobs`):

| Outcome | Meaning |
|---------|---------|
| `completed`, progress 100 | Migrated |
| `completed`, result status `lock_held` | Deferred: another writer held the repository's lock |
| `completed`, result status `refresh_in_flight` | Deferred: a refresh was running |
| `failed` | Investigate the job error in the job details and the server log |

## Rollback

There is no un-migrate operation.

- Setting the flags back to No stops further work on the next tick.
- Anything not yet consolidated or relocated is untouched and readable.
- Until deletion runs, the legacy files are still on disk.
- After deletion, recovery is a restore from the pre-flight backup.

## Related documents

- [Architecture invariants](../architecture/invariants.md): chunk storage layout and temporal path rules
- [Cluster Architecture](../architecture/cluster.md)
- [Maintenance and Jobs](maintenance-and-jobs.md)
