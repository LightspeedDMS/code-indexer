# Data Migration Playbook (Production)

Operator procedure for the two on-disk data migrations that convert legacy storage layouts to the
current ones. Both are OFF by default, both are Web-UI controlled, and both delete real data once
authorized -- so they are deliberately manual, operator-gated actions.

Every statement in this document was verified end to end on the staging cluster on 2026-08-10
(v12.11.0). Where something is NOT verified, it says so.

## The two migrations are independent -- do not confuse them

They are separate config sections, separate schedulers, separate semantics. Folding them together is
the single easiest mistake to make.

| | Temporal Legacy Migration | Fleet Migration |
|---|---|---|
| Config section | `temporal_legacy_migration` | `fleet_migration` |
| Issue | #1548 (Bug #1529 follow-through) | Story #1458 (Epic #1454) |
| What it does | MOVES in-repo temporal shards to the fixed sister root | CONSOLIDATES `vector_*.json` into one `chunks.db` per collection |
| Changes layout? | NO -- relocation preserves the existing layout | YES -- that is its whole purpose |
| Scope | temporal shards only | semantic collections (and in-repo temporal namespaces) |
| Flags | `relocation_enabled`, `cleanup_authorized` | `enabled`, `tick_interval_minutes`, `canary_gate_enabled` |

**Relocation is not consolidation.** After the temporal migration, shards sit in the correct sister
location but are still in whatever layout they were in. Verified on staging: `mock-test`'s shards moved
to `.temporal/mock-test/` and remained `vector_*.json` with no `chunks.db`. They are queryable in that
state (both layouts are supported readers), but the JSON is only removed later, by consolidation.

## Answers to the questions operators actually ask

**Do I have to restart the server after flipping the flag?**
No. Both schedulers re-read the DB-backed runtime config on every tick and act on the new value.
`FleetMigrationScheduler._read_cycle_config()` is called inside its run loop each cycle, and the code
carries an explicit "poll cadence used while the scheduler is disabled, so re-enabling it" path.
`TemporalLegacyMigrationScheduler` likewise calls `self._config_service.get_config()` per cycle.
Verified: flags flipped at 20:00 local, migration jobs began appearing within ~1 minute with no
restart of any node.

**How is the work divided across nodes and workers so it does not run twice?**
Two independent layers, both DB-level, so they hold across processes -- multiple nodes AND multiple
uvicorn workers on the same node:

1. `register_job_if_no_conflict()` against the `idx_active_job_per_repo` unique index. The fleet
   scheduler registers under a FIXED sentinel `repo_alias`, which serializes the whole tick fleet-wide;
   `DuplicateJobError` is caught and the tick is skipped.
2. The per-alias write lock. A second job that reaches a repo already being migrated completes with
   `status: "lock_held"` and a `detail` naming the holder, instead of double-migrating.

Verified on staging: while one job migrated a large repo, a concurrent tick returned
`{'status': 'lock_held', 'detail': "write lock for '<alias>' is already held by another writer"}`.
No coordination beyond the shared database is required or used.

**Does the config change reach the other nodes?**
Yes -- runtime config lives in the shared database (PostgreSQL in cluster mode), and each scheduler
reads it per tick. The write itself is a whole-blob read-modify-write, so see the concurrency warning
under Gotchas before making several changes at once.

## Pre-flight

1. **Back up.** These operations delete files after a verified consolidation. Follow the existing
   dev/staging backup precaution; production has no automatic pre-migration backup by design.
2. **Confirm the fleet is on a version that can READ the target layout** before authorizing deletion --
   the rollout gate procedure in `CLAUDE.md` (per-node `server_version` on the admin dashboard). An old
   node that predates the dual-layout reader would see an empty collection after deletion.
3. **Record a baseline** so you can prove what changed:

   ```bash
   GR=<golden_repos_dir>          # e.g. /mnt/cow-storage/golden-repos
   for r in $(ls $GR | grep -v '^\.'); do
     IDX=$GR/$r/.code-indexer/index
     [ -d "$IDX" ] || continue
     echo "$r | json=$(find $IDX -name 'vector_*.json' | wc -l)" \
          "chunks_db=$(find $IDX -maxdepth 2 -name chunks.db | wc -l)" \
          "in_repo_temporal=$(ls $IDX | grep -c code-indexer-temporal)"
   done
   du -sh $GR/.temporal/* 2>/dev/null
   ```

4. **Check for in-flight work.** A repo being refreshed or indexed will make migration defer (which is
   safe), but a quiet window makes the run easier to read.

## Procedure

Run the temporal relocation FIRST, then fleet migration. Relocation moves temporal data out of the
repo tree; doing it first means fleet migration afterwards sees a simpler tree.

### Step 1 -- Log in and ELEVATE the web session

The config endpoints are behind TOTP step-up elevation. This trips people up:

- `POST /auth/elevate` (JSON, Bearer token) elevates the **API/JWT** principal.
- It does **NOT** elevate a **web session**. A web-session config write will still return
  `403 elevation_required`.
- For the Web UI, elevate through the UI (it will prompt), which posts to `/auth/elevate-form`.

Verified on staging: JWT elevation returned `{"elevated": true, "scope": "full"}` and the subsequent
web-session config POST still returned 403 until the session itself was elevated.

### Step 2 -- Temporal legacy relocation

Web UI: Config screen -> "Temporal Legacy Migration".

- `relocation_enabled = Yes` -- the non-destructive copy/publish step.
- `cleanup_authorized` -- the INDEPENDENT gate for deleting the legacy in-repo copy.

Conservative order: set `relocation_enabled = Yes` alone, confirm the sister root is populated and the
repo answers temporal queries, and only then set `cleanup_authorized = Yes`. Setting both at once is
what was exercised on staging and it worked, but it removes your ability to compare old against new.

### Step 3 -- Fleet migration (chunks.db consolidation)

**Requires v12.12.0 or later.** Older versions are unsafe on large collections -- see the history note
at the end of this step.

Expect this step to run for HOURS on a large repo and plan the window accordingly. Measured on
staging against a 343,604-file collection: scan plus `chunks.db` write took **2h11m**, followed by
roughly 4 minutes to delete the 343,561 legacy files. That is the normal, healthy shape of the
operation, not a stall.

Pre-flight, count the legacy files per collection so you know what you are committing to:

```bash
find <repo>/.code-indexer/index -name 'vector_*.json' | wc -l
```

Legacy `SHARDED_JSON` collections remain fully readable either way. Consolidation is an optimization,
not a correctness requirement, so deferring a large repo to a quieter window costs nothing.

History -- why this step carried a hard STOP before v12.12.0: Bug #1558. Step 0's dedup repair
retained every parsed record, embedding vector included, for the entire scan/plan/apply lifecycle. On
this same 343,604-file collection one uvicorn worker reached 6.6 GB RSS on a 7.5 GB node, the memory
governor went RED, the worker was recycled, and the scheduler re-triggered the repo indefinitely
without ever migrating it -- with no OOM-kill to surface it as a failure. The scan now retains only
the identity fields it actually uses and re-reads the full record at apply time. If you are on
anything older than v12.12.0, upgrade before enabling this step.

Web UI: Config screen -> "Fleet Migration".

- `enabled = Yes`
- `tick_interval_minutes` -- how often a tick fires. 30 is the default; staging runs 1.
- `canary_gate_enabled` -- **leave this No.** When on, the sweep pauses after the first repo pending an
  explicit `confirm_canary()`, and there is currently no admin REST/MCP endpoint exposed to issue that
  confirmation, so the sweep would stall with no front-door way to resume it. Revisit once that
  endpoint exists.

The sweep is deliberately one repo at a time, and a large repo holds it for hours.

On v12.14.0 and later, progress ticks WITHIN the dominant phase rather than only at phase boundaries
(Bug #1562), so a healthy long run advances visibly and a genuinely stuck one is distinguishable.
Before that fix a correctly-working migration sat at a constant progress 25 for hours -- the exact
signature the Bug #1558 hang produced -- which is why the two were indistinguishable from the
dashboard alone.

### Step 4 -- Turn the flags back OFF when the fleet is converted

Neither scheduler needs to stay on. Once the baseline command reports `json=0` everywhere and the
sister roots are populated, set both sections back to their defaults so no future run can delete
anything unattended.

## Verification

Verify by EFFECT, not by the rendered config page (see Gotchas).

On-disk, per repo:

- semantic collections live INSIDE the repo: `<repo>/.code-indexer/index/<collection>/chunks.db`,
  with `hnsw_index.bin`, `collection_meta.json`, and zero `vector_*.json`
- `collection_meta.json` carries the discriminator `"chunks_db": {"version": 1}`
- temporal lives OUTSIDE the repo: `<golden_repos_dir>/.temporal/<alias>/code-indexer-temporal-<embedder>-<quarter>/`
- the quarter-less `code-indexer-temporal-<embedder>` directory is the shared bookkeeping directory and
  correctly has no vector data -- do not treat it as an empty shard

Through the front door:

```bash
# semantic
curl -sX POST $BASE/api/query -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"query_text":"...","repository_alias":"<alias>-global","limit":3}'

# temporal
curl -sX POST $BASE/api/query -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"query_text":"...","repository_alias":"<alias>-global","time_range_all":true,"limit":3}'
```

Also verify through an ACTIVATED alias. A correct activation contains only semantic collections and
ZERO temporal directories, yet still answers temporal queries -- because the read resolves to the
golden sister location. If an activation clone contains temporal directories, something regressed.

Job outcomes to expect in the admin jobs view:

- `completed` with `progress: 100` -- migrated
- `completed` with `result.status = "lock_held"` -- correctly deferred, not an error
- `completed` with `status = "refresh_in_flight"` -- correctly deferred
- `failed` -- investigate; see the known duplicate-job case under Gotchas

## Gotchas verified on staging

**The config page can render stale values.** After a successful write the page may still show the old
value, because the server runs multiple uvicorn workers and the GET can be served by a worker whose
cached config predates the write. Observed: a write that PostgreSQL confirmed as `True` still rendered
as "No" seconds later on the same node. Confirm a change by its effect (jobs appearing, files moving),
or by reading the shared config row directly -- not by trusting the rendered page.

**HTTP status is now meaningful (Bug #1554).** A rejected config write returns 403/400/500, not 200.
If you get a 200 from `POST /admin/config/<section>`, the write was accepted.

**Whole-blob config writes can lose a concurrent change.** Each section write is a read-modify-write of
the entire config document. Two writes issued back to back through a load balancer can land on
different nodes, and the second can overwrite the first with its own slightly stale copy. Make config
changes one at a time and confirm each before the next.

**A benign collision can surface as a failed job.** Observed once: a `temporal_legacy_migration` job
failed with `Duplicate job: global_repo_refresh for <alias> (existing: <id>)` when it collided with an
in-flight refresh. The work is not lost -- the next tick retries -- but the failed job is noise in the
dashboard rather than a graceful deferral like `lock_held`.

**Deletion is gated separately for a reason.** `cleanup_authorized` (temporal) and the fleet sweep's
deletion step both remove real files only after a verified read-back. Leaving them off gives you a bake
window where the new layout is live and the old files are still present.

## Rollback

There is no "un-migrate" operation. What you have instead:

- Setting the flags back to `No` stops any further migration immediately (verified: the schedulers pick
  up config changes per tick, in both directions).
- Anything not yet consolidated is untouched and still readable.
- If deletion has NOT been authorized, the legacy files are still on disk and a version rollback keeps
  working.
- Once deletion has run, recovery means restoring from your pre-migration backup -- which is why the
  pre-flight backup step is not optional.

## Related documents

- `docs/architecture-invariants.md` -- chunk storage layout, temporal fixed-path rules
- `docs/cluster-architecture.md` -- node topology and shared storage
- `CLAUDE.md` -- the per-node `server_version` rollout-confirmation procedure
