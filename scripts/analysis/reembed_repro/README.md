# Re-embed reproduction harness (crash-recovery refreshes)

Operator/dev tooling, not part of CI. It reproduces, locally and
deterministically, interrupted refreshes of a large non-git (`local://`)
golden repository that re-embed almost the whole repository on every
crash-recovery run, and it is the acceptance gate of the crash-safe,
pay-once design (`plans/designs/crash_safe_reconcile_2087_20261006.md`,
section 15.1).

## What it checks

It drives the REAL `cidx` CLI from a code tree (unmodified) through the same
command sequence the server uses for a golden repo, and interrupts refreshes
the way systemd, server shutdown, a job cancel and a manual restart do
(signals to the child's process group). It then evaluates, across the whole
sequence, these version-agnostic checks. Each one runs unchanged against
12.83.0 and against the fix; which of them fail on 12.83.0 in which scenario
is stated exactly in "What is demonstrated on 12.83.0" below.

| Id | Check |
|----|-------|
| R-a | every recovery run plans no more files than were unindexed (path not in the store) at its start. Per recovery run: **N/A (killed before planning)** only when it was interrupted by an `early:` trigger, no plan was observed, AND the harness observed that the run never rewrote its metadata file (mtime unchanged) -- it planned nothing, so it cannot over-plan; never inferred from missing output alone. **UNASSESSED** when a plan should exist (any other kill, or a completed run) but was not observed. Otherwise the run is assessed. Overall: any violation FAIL; else any UNASSESSED run `SKIPPED-UNASSESSED` (INCOMPLETE, exit 3); else no assessed run `SKIPPED-NOTHING-TO-ASSESS`; else PASS |
| R-a content | every run sends only keys in `K_r - D_r` (K_r: chunk keys of the files on disk at the run's start; D_r: keys durable in the store or `pending_vectors` then), and no more inputs than `|K_r - D_r|` (no provider faults are injected, so a key needs one send). Duplicate copies of stored content owe nothing, which the path-based R-a cannot see. Never N/A: the bound comes from the disk and the durable state, which exist for every run however early it was killed; a run without a bound is UNASSESSED (`SKIPPED-UNASSESSED`, never PASS) |
| R-b | `re_idx == 0` asserted on every run: no chunk whose content was in the store at the run's start is sent to the provider |
| R-c | (A6-core) after the final runs, for every non-empty disk file with file hash `H` and `n` chunks (current chunker), the ids `md5(f"{project_id}_{H}_{i}")`, `i < n`, exist in the store and are not hidden on the current branch key. Content-addressed, so duplicate-content files pass on every version |
| R-d | a SIGTERM while the fake holds responses in flight (`SIGTERM@inflight:<n>`, deterministic) yields: exit 75; every held key is in `pending_vectors` after exit (read-only); no hot `chunks.db-journal`; the next run sends none of the held keys |
| R-12 | (A12-core) after every kill, the next run re-sends only keys that were in flight at a kill and not durable at its start, and the first successful run after the kill leaves R-c satisfied (the per-interruption loss check) |

Assertions implementable today: A1 exact budget (`sum inputs <= |introduced
keys| + in-flight items over all kills`, no generic slack), A2 paid once
(every re-sent key was in flight at a kill since its previous send; none was
durable), A3 single-flight (`dup_same_run == 0`), A5 convergence (exactly one
`converge-reconcile` and one `converge-incremental` run, each exit 0 with an
OBSERVED plan of 0 files and 0 inputs; an unobserved plan, a failed child or
a missing/duplicate run fails). A "final run" check requires the last refresh
to exit 0 with status `completed`. A plan is observable only through
`--progress-json`: the fix must print its plan there, including `0/0 files`
when it plans nothing, or R-a and A5 cannot observe it (an unobserved plan
is UNASSESSED for R-a and fails A5).

**In flight** (the pay-once definition, bounded in-flight loss): a key sent
during a run and not durable (store or `pending_vectors`) after that run's
child exited, whether or not its response arrived.

**Provider-boundary ledger** (Codex E3). The fake records every request: its
keys, when it was processed and when its response bytes were fully written.
The harness marks each kill instant and classifies every KEY sent by a killed
run, durability first: `saved` (durable, response delivered),
`durable_undelivered` (durable, e.g. saved by another request, response not
delivered: not in flight), `received_unsaved` (delivered, not durable: in
flight) or `unanswered` (neither: in flight). Response delivery per request
(`responses_delivered` / `responses_undelivered`) is reported separately, and
a mixed request is split per key. `report.json` holds the per-key rows.

**Later checks** (`later_checks.py`). The assertions whose mechanisms do not
exist yet (A4, A6, A7, A8, A10a-f, A11-A23 including A21b-i and A22b-e) are
scaffolded. Each names its story and probes the tree under test for the
mechanism (for example `storage/pending_vectors.py` for A7, S4):

* probe absent: `SKIPPED-NOT-IMPLEMENTED` (12.83.0 today);
* probe present but no evaluator registered: FAIL ("mechanism present, harness
  check not enabled"), so a later check can never pass silently;
* evaluator registered in `later_checks.ENABLED`: its result.

**Verdict and exit codes.** `REPRODUCED` (exit 1) when any check failed;
`INCOMPLETE` (exit 3) when none failed but any check was skipped (a skipped
required check is never a success); `NOT REPRODUCED` (exit 0) only when every
check passed; exit 2 for a harness or guard failure.

The per-run table shows the node, the code version, planned files
(`--progress-json` / metadata), chunks sent, `dup_prior`, `dup_same`,
`re_idx`, `inflight` (keys in flight at the run's kill), index points before
and after, status, and `missing` (R-c files missing after a successful run).
After the initial index the harness checks that the index's `content_hash`
set equals the texts the fake embedded and that the content model finds 0
files missing, and aborts otherwise, so the content-addressed ids are
validated against the real indexer on every run.

## What is demonstrated on 12.83.0

Gate runs on committed trees of version 12.83.0 pinned with `--src-ref`
(`default`: `ba34f3d7e`; `git`: `ccfb3195f`; `dup`: `efecb823a`); every
failure below is on runs tagged `[12.83.0]` (in `default`, the first two
refreshes run 12.82.0 by design and fail too). FAIL = demonstrated. In
`git`, path R-a fails on the stored-list replay (`cycle-19` planned 1009 >
406 unindexed) while edits and branch reverts are counted as legitimately
re-planned.

| Check | `default` (20k) | `git --files 3000` | `dup --files 3000` |
|-------|-----------------|--------------------|--------------------|
| R-a | FAIL | FAIL | PASS: duplicate copies have no stored path, so the path bound cannot be exceeded; R-a content covers it |
| R-a content | FAIL | FAIL | FAIL |
| R-b | FAIL | FAIL | FAIL |
| R-c | FAIL (20 files) | PASS: 12.83.0's git path discovers the committed sync files itself; R-12 is the per-interruption loss check here (200 files missing after a recovery) | FAIL (566 files) |
| R-d | FAIL | FAIL | FAIL |
| R-12 | FAIL | FAIL | FAIL |
| A1 / A2 / A3 / A5 | FAIL / FAIL / PASS / FAIL | PASS / FAIL / PASS / FAIL | FAIL / FAIL / FAIL / FAIL |

The R-c failures in `default` and `dup` come from the required recovery
delay of the scenario (below); without it the final incremental of 12.83.0
may still catch the files a resume skipped.

## Scenarios

`--scenario NAME` presets options (explicit options still win):

| Scenario | What it runs |
|----------|--------------|
| `default` | upgrade-like and cluster-like: an older release (12.82.0) builds the index and runs the first two refreshes (the first interrupted right after it planned), later refreshes run the tree under test on another node; ends with the R-d yield probe |
| `dup` | 100 duplicate-content groups of 2-50 byte-identical copies, and 30% of every sync's files copy existing content |
| `git` | git repository: every sync is committed, then one seeded operation: uncommitted edits (always new content), committed renames, or a branch switch (`main` <-> `feature-N`) |
| `cancel` | every interrupt is a server job cancel: SIGTERM, SIGKILL after 2 s |
| `restart` | every interrupt is a manual `systemctl restart`: SIGTERM, SIGKILL after 90 s |
| `soak` | 199,000 files, 2,000-file syncs |

The random scenarios run 20 seeded cycles (`--random-cycles`, `--cycle-seed`)
of the tree under test only, all on one node (a recovery meets its own
sealed resume state), each interrupting at the hash pass (`early`,
`planned`), after `k` inputs (`chunks:k`), during finalization (`final`), or
not at all; then the R-d yield probe `SIGTERM@inflight:4`; then a required
**recovery delay** of 65 s (`--recovery-delay-seconds`) before the final
runs, as when the recovery runs on a later scheduler cycle. The delay exceeds
the indexer's 60 s incremental safety buffer, so a resume that ignores files
synced while it was pending finishes too late for the next incremental to
see them. After the finals every scenario runs `converge-reconcile` and
`converge-incremental` (A5).

## Reproduction recipes

```bash
# the gate on a committed sha (pinning keeps concurrent edits out of the run)
SHA=$(git rev-parse HEAD)
python3 scripts/analysis/reembed_repro/run_repro.py --src-ref $SHA
python3 scripts/analysis/reembed_repro/run_repro.py --src-ref $SHA --scenario git --files 3000
python3 scripts/analysis/reembed_repro/run_repro.py --src-ref $SHA --scenario dup --files 3000
python3 scripts/analysis/reembed_repro/run_repro.py --src-ref $SHA --scenario soak   # 199k files
```

Large-sync recipe from the original investigation:

```bash
python3 scripts/analysis/reembed_repro/run_repro.py --name head-bigsync --src-ref HEAD \
  --old-ref none --node-sequence 0 --sync-files 3000 \
  --cycles "SIGTERM@chunks:1500,SIGKILL@chunks:1500,SIGTERM@chunks:1500,SIGKILL@chunks:1500,SIGTERM@inflight:4"
```

## Safety: no real embedding provider can be reached

* The whole harness (fake provider and every `cidx` child) runs inside a
  private user + network + mount namespace (`unshare`). The network
  namespace has only loopback: no route and no DNS to the internet.
* The sandboxed half is entered only through a private parent-to-child
  handshake: the parent writes a one-time random token into a pipe, passes
  the read end as an inherited fd and only its SHA-256 on the command line;
  the child reads the pipe (non-blocking, then closes it) and compares. No
  environment variable selects the mode, so nothing a shell can carry does.
* Before starting the fake or any child, the sandboxed half PROVES isolation
  from inside its network namespace and exits 2 otherwise: only the `lo`
  interface exists, `api.voyageai.com` resolves only to 127.0.0.1, a TEST-NET
  address has no route, and public DNS fails.
* A namespace-private `/etc/hosts` (bind mount; the host file is untouched)
  maps `api.voyageai.com` to 127.0.0.1, where the fake serves TLS on 443
  with a certificate from a throwaway harness CA. The child trusts that CA
  through `SSL_CERT_FILE`. This is needed because the server flag
  `--server-managed-provider-settings` pins the real endpoint URL.
* The child environment drops every credential-like variable and sets
  `VOYAGE_API_KEY` to a sentinel. The fake rejects any other key.
* A read-only audit hook (`child_audit/sitecustomize.py`, PEP 578) logs any
  non-loopback connect or foreign DNS lookup by a child. The run fails with
  exit 2 if that log is non-empty, if the fake recorded any violation, or if
  the fake received no embedding calls. A response the fake cannot deliver
  because the child was killed (connection reset, TLS `SSLError`) is counted
  as aborted, not as a violation.
* The voyage tokenizer is read from the existing local Hugging Face cache
  (`HF_HOME`), never downloaded.

The child runs with a scratch HOME and a scratch `CIDX_SERVER_DATA_DIR`, so
it never touches `~/.cidx-server` or the local server. `--name` must be one
path component of `[A-Za-z0-9._-]` (not `.` or `..`), and the work directory
is re-checked to resolve directly under `~/.tmp/reembed-repro/` before every
removal. Extracted trees are published by an atomic rename to an absent
target; a stale target is renamed aside first, never deleted before
publication.

## Options

| Option | Meaning |
|--------|---------|
| `--scenario NAME` | preset (table above) |
| `--files N` | synthetic repo size (default 20000; 199000 = large-repo soak) |
| `--dup-groups G` | duplicate-content groups of 2-50 copies under `dups/` |
| `--sync-files K` | files the sync job adds before each refresh (default 5) |
| `--sync-dup-fraction F` | fraction of each sync's files that copy existing content |
| `--git` | git variant |
| `--cycles LIST` | one refresh per entry: `SIGTERM\|SIGKILL\|CANCEL\|RESTART@early:<s>`, `@planned:<s>`, `@chunks:<n>`, `@inflight:<n>` (fake holds responses; fires when n are held), `@final:<s>` (after every planned file is processed), or `none` |
| `--random-cycles N`, `--cycle-seed S`, `--random-kinds LIST`, `--max-chunks-trigger K` | N seeded random cycles plus the yield probe, instead of `--cycles` |
| `--hold-seconds S` | how long the fake holds each response of an `inflight` run (default 5) |
| `--recovery-delay-seconds S` | pause before the final runs, a finite value within 0-600 (random presets: 65) |
| `--final-runs N` | uninterrupted refreshes at the end (default 2) |
| `--name NAME` | scratch sub-directory, one safe path component |
| `--rotate-node-key` | fresh resume-seal key per run, as when refreshes land on different cluster nodes |
| `--node-sequence LIST` | node per run in order (init, initial, cycles, finals; the last entry repeats); each node has its own resume-seal key |
| `--src DIR` / `--src-ref REF` | code_indexer tree under test (default: this checkout's `src`); `--src-ref` pins it to a git ref via read-only `git archive` |
| `--old-ref REF` / `--old-src-runs N` | release used for init, the initial index and the first N refreshes (default `v12.82.0`, N=2; `--old-ref none` disables) |
| `--initial-src DIR` | explicit old tree instead of `--old-ref` |
| `--no-cluster-mode` | do not set `CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE=1` |
| `--disk` | keep the repo on disk (default: a private RAM tmpfs inside the sandbox) |
| `--keep` | keep the scratch directory |

Exit codes: 0 NOT REPRODUCED (every check passed), 1 REPRODUCED, 2 harness
or guard failure, 3 INCOMPLETE (nothing failed, some checks skipped).

## Modules

| Module | Role |
|--------|------|
| `run_repro.py` | sequence driver (`Harness`), handshake dispatch and sandbox entry |
| `cli_args.py`, `scenarios.py` | options, presets, seeded random cycles, tree extraction, work-dir containment |
| `child_runner.py` | spawns `cidx`, interrupt kinds and triggers, kill capture |
| `fake_voyage_server.py` | fake provider, per-run key counts, response hold, provider-boundary ledger |
| `index_inspector.py` | read-only store snapshot (ids, `content_hash`, `hidden_branches`), `pending_vectors` keys, metadata, progress |
| `content_model.py` | disk-side content model (file hash, chunk keys, expected ids) with the tree under test's chunker |
| `run_facts.py` | per-run durability facts, content-key bound, finalization probe |
| `invariant.py` | the verdict: R-a, R-a content, R-b, R-c, R-d, R-12, A1, A2, A3, A5 (pure) |
| `later_checks.py` | scaffolding of the later-story assertions |
| `sandbox.py` | namespace command, isolation proof, handshake, child environment |
| `synthetic_repo.py`, `git_variant.py` | repository generators and git operations |
| `report.py` | run table, verdict, report files |

## Sequence

1. Generate the repository (optionally with duplicate groups; optionally a
   git repository with `.code-indexer/` ignored).
2. `cidx init --embedding-provider voyage-ai --no-override-file`, then set
   `embedding_providers: ["voyage-ai"]` as `GoldenRepoManager` does.
3. Initial index with the server's registration command; validate the
   content model and the hash sets against it.
4. Per cycle: sync (new files, optionally copies; git: commit plus one
   operation), choose the command exactly as `RefreshScheduler._index_source`
   does (any `in_progress`/`failed` status adds `--reconcile`), built with the
   real `append_server_layout_args`, and interrupt it as configured. The
   harness never writes metadata.
5. The recovery delay, the final uninterrupted refreshes, the final R-c
   state, the two convergence runs, then the verdict.

## Findings (2026-10-06, 20k files)

Measured with this harness; numbers are from the per-run tables.

1. **12.82.0 crash recovery plans every file and deletes before
   re-embedding.** On untrusted resume state, the 12.82.0 reconcile marks
   every non-git file modified (disk-side content id
   `working_dir_<float mtime>_<size>` differs from the stored format; fixed
   by dd7f59603), then deletes the chunks of all "modified" files before
   embedding. One interrupted run took the index from 27,035 to about 2,990
   points; each further 12.82.0 recovery repeats this, with no progress.
2. **The resume path restarts from file 0.** Refresh paths never record
   `completed_files` (only `process_files_incrementally`, used by watch mode,
   calls `mark_file_completed`), so a resumed run re-plans the stored list
   (all 20,010 files after a 12.82.0 recovery) on every restart.
3. **The reuse cache misses chunks an interrupted run already wrote.**
   `get_existing_content_hashes` resolves a file's points through
   `path_index.bin`, which an indexing session persists only at
   `end_indexing()`. Files first written by an interrupted session are
   absent from it, so every resume re-embeds them (about 1,490 chunks per
   recovery run in the large-sync recipe, pure 12.83.0). Files that were in
   the last completed `path_index.bin` keep deterministic point ids and hit
   the cache again once re-created.
4. **Files synced during a resume are never indexed.** The resume path
   processes only the stored list; when it completes, the next incremental
   uses `last_index_timestamp - 60 s`, which is already past those files'
   mtimes (9,000 of 32,000 files missing in the large-sync recipe; 15 in
   the default scenario).
5. **A trusted (server-sealed) in-progress state wins over `--reconcile`.**
   On the node that sealed it, the crash-recovery command resumes the stored
   list instead of reconciling disk against the index; on another node the
   12.83.0 reconcile plans exactly the unindexed files.
6. `cidx index` installs no SIGTERM handler; SIGTERM and SIGKILL leave the
   same state (status `in_progress`, sometimes a hot `chunks.db-journal`).

## Outputs

Scratch data lives under `~/.tmp/reembed-repro/<name>/` and is deleted at
the end unless `--keep`. `report.txt` and `report.json` (per-run rows,
provider-boundary ledger, metadata timelines, stderr tails, verdict) are
copied to `~/.tmp/reembed-repro/samples/`. Both files, and the report the
run prints, have the home directory prefix replaced by `~`, so they can be
shared without the user's home path.

## Self-tests

```bash
python3 -m pytest scripts/analysis/reembed_repro/tests -q
```

They are not collected by the project gates (`testpaths = ["tests"]`).

## Requirements and limits

* Linux with unprivileged user namespaces (`unshare --user --net --mount`),
  `ip`, `openssl`, `git`, and a populated Hugging Face cache for
  `voyage-code-3`.
* `cidx` must be on PATH; the harness points it at the tree under test with
  `PYTHONPATH` and verifies the import location.
* Single-node simulation: cluster effects other than the per-node resume
  seal key (`--rotate-node-key`, `--node-sequence`) and the cluster-mode env
  flag are not modelled. The cancel and restart variants model the signals
  the server and systemd send, not the server process itself (A19 covers
  the end-to-end path once S20 lands).
* The git variant DOES revert files: switching from `feature-N` back to
  `main` restores `main`'s committed content, which was indexed before
  (uncommitted edits always produce new content). A2's "never sent again
  after being durable" therefore depends on reverted content staying stored
  (on git it is hidden on the other branch, not deleted), and the path R-a
  bound counts reverted and edited stored files as legitimately re-planned.
* R-a content assumes no provider faults (one send per key); A13 scenarios
  that inject faults will need their retries counted separately.
