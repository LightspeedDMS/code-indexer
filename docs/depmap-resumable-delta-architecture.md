# Resumable Delta Dep-Map Analysis Architecture

Status: implemented in Story #1053 (v10.91.15+)
Spec: https://github.com/LightspeedDMS/code-indexer/issues/1053

## Problem this design solves

`run_delta_analysis` invokes the `claude` CLI once per affected domain plus one monolithic Claude call for new-repo discovery. On a large change set (e.g. 33 affected domains + 12 new repos) the total wall-clock and token cost is multi-hour. If the cidx-server process dies mid-flight — auto-updater `systemctl restart`, OOM, `pkill -KILL`, manual restart, machine reboot — the prior naive implementation re-ran from scratch on the next trigger, throwing away every domain Claude had already finished.

This document describes the resume mechanism that eliminates that waste.

## High-level approach

**The artefact IS the journal.** Each `cidx-meta/dependency-map/<domain>.md` carries YAML frontmatter at the top of the file recording which delta was last applied to it. On a resumed run, the per-domain loop reads each affected file's frontmatter and skips domains whose `last_delta_applied` matches the current delta's fingerprint.

There is **no separate cursor file**. The cursor-vs-file ambiguity window (file written successfully but cursor save fails before crash) is eliminated by writing the frontmatter and body together in a single atomic `os.replace`.

## Five primitives

All implemented in `src/code_indexer/server/services/dep_map_delta_journal.py`.

### 1. `compute_delta_fingerprint(changed, new, removed) -> str`

```
sha256(
  json.dumps(
    {"changed": sorted([r.alias for r in changed]),
     "new":     sorted([r.alias for r in new]),
     "removed": sorted(removed)},
    sort_keys=True
  ).encode()
).hexdigest()
```

Deterministic across runs. Order-independent within each list. Used as the resume key. A different repo set → different fingerprint → journal invalidated, fresh run.

### 2. `parse_frontmatter(md_text) -> (dict, str)`

Extracts the YAML frontmatter block delimited by `---\n`/`---\n` at the start of the file. Returns `({}, original_text)` on malformed YAML or absent frontmatter (with a structured WARNING log line in the malformed case). Tolerant by design: corruption recovers automatically by treating the file as "no journal recorded, must re-process".

### 3. `render_md(frontmatter: dict, body: str) -> str`

`"---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body`. Order of frontmatter keys is preserved (operator-managed keys round-trip).

### 4. `write_atomic(path: Path, content: str) -> None`

The central correctness primitive:

```
tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
try:
    with os.fdopen(tmp_fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, str(path))
except Exception:
    try:
        os.unlink(tmp_path)
    finally:
        raise
```

- **Same parent directory**: `os.replace` is atomic only within a single filesystem; using the same parent guarantees this on local FS and on NFSv4.
- **`fsync` before `os.replace`**: ensures the temp file's bytes are flushed to the NFS server's stable storage before the rename.
- **Temp-file cleanup on failure**: no orphan temp files on disk.

### 5. `all_new_repos_have_domain_assignments(new_repos, domains_json_path) -> bool`

Returns True iff every alias in `new_repos` appears as a member of some entry in `_domains.json`. Defensive against three corruption modes:

| Condition | Result |
|---|---|
| File missing | False |
| `json.JSONDecodeError` (truncated / bad UTF-8) | False (+ structured WARNING log) |
| Wrong shape (top-level is not list-of-dicts) | False (+ structured WARNING log) |
| Valid but incomplete | False |
| Valid and complete | True |

Returning False forces the monolithic new-repo discovery Claude call to re-run, which overwrites `_domains.json` cleanly.

## The resume loop

In `dependency_map_service.py::_update_affected_domains` (the existing per-affected-domain refinement loop), when called with `fingerprint != None`:

```python
for i, domain_name in enumerate(sorted(affected_domains)):
    if _cancel_event.is_set():
        break  # pause; frontmatter preserves what's done

    domain_file = dependency_map_dir / f"{domain_name}.md"
    existing_text = domain_file.read_text() if domain_file.exists() else ""
    fm, body = parse_frontmatter(existing_text)

    if fm.get("last_delta_applied") == fingerprint:
        activity_journal.log(f"Resume: skipping {domain_name} (already applied)")
        continue

    # Invoke Claude with existing body as baseline (no special prompt hint —
    # the file content IS the input; Claude treats it the same regardless of
    # whether it came from pre-delta or post-partial-prior-run).
    claude_raw = invoke_claude_cli(prompt, ...)

    # Strip any frontmatter Claude echoed back (it often reproduces the
    # entire file as part of its output; without this strip we would stack
    # frontmatter blocks).
    _, new_body = parse_frontmatter(claude_raw)

    # Empty / whitespace-only Claude response = failed domain; do NOT write
    # frontmatter; do NOT advance the journal.
    if not new_body.strip():
        errors.append(f"{domain_name}: empty Claude response")
        continue

    # Build new frontmatter: preserve operator-added keys, overwrite only the
    # two journal keys.
    new_fm = {k: v for k, v in fm.items()
              if k not in ("last_delta_applied", "last_applied_at")}
    new_fm["domain"] = domain_name
    new_fm["last_delta_applied"] = fingerprint
    new_fm["last_applied_at"] = datetime.now(timezone.utc).isoformat()

    write_atomic(domain_file, render_md(new_fm, new_body))
```

## Cluster correctness

Resumability is single-writer-safe because the entire delta run executes inside the existing **`cidx-meta` write lock** acquired via `RefreshScheduler.acquire_write_lock("cidx-meta")`. That lock is backed by `WriteLockManager` (atomic `os.open(O_CREAT|O_EXCL|O_WRONLY)` on the NFS-shared `cidx-meta` filesystem — NFSv4-safe per RFC 7530). The same lock is already in production use across `MemoryStoreService`, `XrayPatternService`, dep-map full analysis, and the dashboard sentinel.

Two concurrent runs (delta or full) cannot interleave because the second blocks on lock acquisition. Without this lock, two runs with different fingerprints could write conflicting `last_delta_applied` markers to the same domain file — so the per-domain frontmatter approach **depends on** the single-writer guarantee, it does NOT replace it.

## Crash-durability scope (honest)

The atomic co-write of frontmatter and body guarantees that completed-domain state survives:

| Failure mode | Survives? |
|---|---|
| Process crash | ✅ |
| `pkill -KILL` | ✅ |
| `systemctl restart cidx-server` (auto-updater path) | ✅ |
| Graceful node reboot | ✅ |
| **Sudden node power loss while writes are in-flight** | ⚠️ NFS server export-mode dependent |
| **NFS server crash during a write RPC** | ⚠️ `soft,timeo=30` returns an error rather than hanging — completed prior domains remain durable but the in-flight one is lost |

Parent-directory `fsync(2)` after `os.replace` is intentionally **NOT** added. NFS client support for directory fsync is implementation-defined; adding it would create a false sense of safety without a real guarantee. The honest scope statement above is the chosen design.

The recovery path for the unsupported failure modes is the same as any in-flight crash: the resumed run re-processes one domain at worst.

## What the design does NOT do (rejected during 4 rounds of design + Codex pressure-test review)

These were considered and explicitly rejected. Re-introducing any of them is a regression:

- **No backup-by-N domains on resume.** A "redo the last N completed domains defensively" mechanism was proposed and rejected because (a) atomic co-write eliminates the cursor-vs-file window the backup was meant to defend against, (b) it wastes Claude calls re-doing work that was already correctly applied.
- **No prompt context hint to Claude.** Telling Claude "the file may be from a partial prior run" adds prompt tokens for no behaviour change — the file IS the input either way.
- **No separate cursor file.** This is the alternative the design was specifically chosen against. The cursor-vs-file atomicity window the cursor introduces is exactly what frontmatter eliminates.
- **No batched / per-repo new-repo discovery.** The monolithic Claude call stays monolithic; skip-or-redo only.
- **No fingerprint intersection / partial credit.** When the delta set changes between runs (e.g., an additional repo had a refresh-pulled commit), the entire journal is invalidated and a fresh run starts. No half-credit.
- **No `run_full_analysis` hardening.** Out of scope. Full analysis has its own resume mechanism (separate journal under `cidx-meta/dependency-map.staging/`).
- **No parent-directory `fsync`** — see scope statement above.

## Regression guards

| Layer | Location |
|---|---|
| Unit + integration tests (40 tests, all 16 AC scenarios) | `tests/unit/server/services/test_dep_map_1053_delta_journal.py` |
| Multi-domain delta fixture provisioner (`--dry-run` capable) | `tests/e2e/manual/provision_delta_fixture.sh` |
| Cidx-server process-tree audit (matches `claude .*--print`, optional `--port` narrowing) | `tests/e2e/manual/audit_processes.sh` |
| Manual E2E with SIGKILL (Scenario 16) | Story #1053 spec: trigger delta, wait for "Delta: domain 2/N complete", `sudo systemctl kill -s KILL cidx-server`, verify ALL DOWN, restart, re-trigger, assert wall-clock reduction + skip log lines + frontmatter fingerprint correctness |

## How a resumed run is observed in production

1. **Activity journal** (`<cidx-meta>/.scratch/dep_map_repair_journal.jsonl`) carries one `Resume: skipping {domain} (already applied)` line per skipped domain, plus a `Resume: skipping new-repo discovery (already complete)` line when Phase C is skipped.
2. **Wall-clock**: a resumed run with K of N domains already completed takes proportionally less time than the first run (subject to Claude latency variance).
3. **On-disk evidence**: every affected `<domain>.md` ends with frontmatter `last_delta_applied = <current_fingerprint>` after the resumed run completes, and exactly two `---\n` delimiters (no double frontmatter — the Claude-echo strip is the safeguard).

## File layout

- `src/code_indexer/server/services/dep_map_delta_journal.py` — helper module (the 5 primitives above)
- `src/code_indexer/server/services/dependency_map_service.py` — modified `_update_affected_domains` to accept an optional `fingerprint` and engage the journal path when present
- `tests/unit/server/services/test_dep_map_1053_delta_journal.py` — 40 tests
- `tests/e2e/manual/provision_delta_fixture.sh` — multi-domain fixture provisioner
- `tests/e2e/manual/audit_processes.sh` — process-tree audit
