# What we got wrong merging the cidx v12.78.0 snapshot, and the corrections

A record of every claim made during the fork↔upstream snapshot merge that turned
out to be false, what was actually true, and how it was caught. Kept because the
*pattern* of the errors repeats more than any individual fact does.

Companion docs: [CIDX_GIT_SEAM_GUARD.md](git-seam-guard.md),
[CIDX_XRAY_QUERY_TIME_TOOLCHAIN.md](xray-query-time-toolchain.md).

## 1. Facts asserted without checking

| Claimed | Actually | Caught by |
|---|---|---|
| "The GitHub upstream is months stale" | A stale local cache of the **inaccessible** `jsbattig/code-indexer` was mistaken for the real upstream `LightspeedDMS/code-indexer`, which was current (v12.78.0, one day old) | The human: *"you mentioned that's months old which can't be true"* |
| "There are only two remotes" | Three. `upstream` had existed since May — `git remote -v \| head -4` truncated it | Re-reading without `head` |
| "xray will now report BinaryNotFound as a new consequence", then "nothing changed, the Python AST path handles it" | **Both wrong.** `AstSearchEngine` is a tree-sitter *parser* wrapper; there is **no Python evaluation fallback** for xray query-time execution (`deployment_executor.py` says so explicitly) | The human: *"we supported xray though without this tool"* |
| "The subagent can't run a real docker build" | Docker 29.8.1 was installed and the user was in the `docker` group | The human: *"why can't it run a real docker build"* |
| "`sync/conflict_resolution.py` and `git_topology_service.py` are seam bypasses" | Neither. `sync/` is unreachable from the server (genuinely client-side); `git hash-object --stdin-paths` has **no `-w`** so it writes nothing — the *classifier* was wrong | Building the real import closure instead of trusting a directory listing |
| "No working compile bomb exists" (relayed from one agent) | A 150,000-statement straight-line evaluator reaches **4.0 GiB RSS in 58s**, inside the 120s timeout, against a 4Gi pod limit | A second agent actually building one |

**Rule:** a claim about the state of the world costs one command to check. Relaying
another agent's negative result (*"I tried and couldn't"*) as a fact is the same
error one layer removed.

## 2. The recurring pattern: fixing the shape, not the class

The git-seam guard took **six review rounds**, each closing the exact shape the
reviewer named. An adversarial pass then found **41 more**. Two root causes
explained nearly all of them, and both were reachable at any point by enumerating
Python's binding forms:

- `_git_argv` returned `(None, None)` for anything unreadable, which the scanner
  could not distinguish from "not a git call". **Silence was the bug.**
- Only `Assign`/`AnnAssign` counted as bindings, so `for`, `with ... as`, walrus,
  tuple unpack, `except ... as` and comprehension targets all looked *unbound*,
  which the guard read as "parameter, someone else's problem".

The identical mistake then repeated **in the fix for the credential leak**: the
guard written to close that class inspected only f-strings and cleared an entire
f-string if any part was redacted. Review round 7 caught it; broadening it found
**24 more sites, twelve in the clone path that carries the PAT**.

And again in the Rust validator: `use std as s;` was closed, but
`extern crate std as s;` — a different `syn` item with no visitor — was not.

**Rule:** when a reviewer names a shape, ask what class it belongs to and
enumerate the class. If the answer is "the language's binding forms" or "the
ways to build a string", enumerate those, in a test.

## 3. Things claimed as verified that verified nothing

- **The Dockerfile rustc assertion.** Reported to the user as "the byte-equality
  assertion passed", with both sides reading `rustc 1.98.0 (88d9e12ae …)` offered
  as proof. It compared a sysroot copied out of the builder against a file written
  by that same builder — **a toolchain against itself**. An agent proved the
  consequence by building a green image shipping an `xray-cli` that could not
  `exec`. Both sides agreeing was the tautology, not the evidence.
- **"Enforced in CI".** A commit titled *"make the git seam the only door, and
  enforce it in CI"* touched **zero CI files**. The guard was referenced by no CI
  job, no pre-commit hook and no script, while `fast-automation.sh` passes
  `--ignore=tests/unit/server/`.
- **Credential redaction "fixed".** It was fixed for the argv channel the reviewer
  named; `stdout=` and `stderr=` two lines away in the same function stayed
  verbatim.

**Rule:** state what a check *proves*, not what it is named after. If the proof is
"X equals X", there is no check.

- **A mutation harness that restored nothing.** The harness applied each mutation,
  ran the tests, then "reverted" with `git checkout -- src/`. The module under
  test was **new and untracked**, so that command silently did nothing for it --
  `git checkout` has no opinion about files git has never seen, and exits 0. Three
  mutations stacked up in the file, later runs were scored against a tree that was
  already broken, and the final *unmutated* baseline printed `12 failed` while
  being reported as the clean reference. Rewritten to snapshot by `shutil.copy2`
  and restore from that copy, with the baseline re-run and printed at the end so a
  contaminated tree cannot pass unnoticed.

**Rule:** a harness that cleans up after itself must prove it did. Re-run the
baseline *last* and assert it still passes -- restoring is a step that can fail.

### Testing something adjacent to what ships

Three errors in one hour, one family. Each time the thing under test differed
from the thing that would run.

| | |
|---|---|
| Probed the image with `bash -lc` and reported `rustc: command not found` | A login shell sources `/etc/profile`, which rewrites `PATH` out from under the image's `ENV`. Under the non-login shell the server uses, it resolves. A **false alarm** about a real image. |
| "Verified the in-image CI step locally" | The local command said `sh -c`; the CI copy said `sh -lc`. The two were never the same command, so the local run proved nothing about the shipped one. |
| Diagnosed a CI failure from the Dockerfile | `pip` and `python` were said to resolve to different interpreters. Pulling the image the pipeline actually built showed `python` is `/usr/local/bin/python` and `/usr/bin/python` does not exist. The real cause was an **apostrophe inside a comment** in a `sh -c '...'` body -- `the image's own ENV` closes the quote, so the rest ran on the Alpine runner. Two CI failures, one wrong diagnosis, and a fix for a problem that did not exist. |

The repair is mechanical: run the artifact, not a retyping of it. The step that
finally worked was verified by parsing the YAML, extracting the block, and
running it verbatim against the image the pipeline had produced.

Related trap worth naming on its own: a quoted shell body (`sh -c '...'`) is a
loaded gun in CI, because the failure surfaces as a confusing error from the
WRONG machine rather than as a syntax error. Feed scripts over a quoted heredoc
(`sh -s <<'EOF'`) so apostrophes stay literal.

### The source-text assertion, four times in one day

A reviewer found the same defect in three of four MRs, and the fourth had it
too. Every instance was a test asserting on `inspect.getsource` text instead of
behaviour, and every instance passed on the exact change it claimed to forbid.

| Claimed to pin | Why it could not fail |
|---|---|
| "deferring does not advance `next_refresh`" | `assert "next_refresh" not in between or "unadvanced" in between` — the deferral's own log message contains *"unadvanced"*, so the right side was always true |
| "no refresh is submitted while denied" | the test called the helper and never ran the loop, so `assert_not_called()` held trivially |
| "the temporal lane is not divided" | `"per_process_budget(\n" not in src.split("temporal_lane_concurrency")[-1]` — written multi-line, the call opener sits *before* the last token, so `split(...)[-1]` excludes it |
| "the gate's result is acted on" | asserted only that *some* `ast.Return` existed anywhere in the function; `logger.info(...)` plus any unrelated early exit stayed green |

Three of the four shipped with an MR description asserting the behaviour was
pinned. The mutation tables were honest about what they ran and silent about
what they did not: **no mutation in any of those runs targeted the claim the
weak test was making.** A mutation suite only covers what you think to mutate,
so "all mutations red" is evidence about the mutations, not about the tests.

What actually distinguishes the four: a source-text assertion answers *"does the
code look like this?"*, which is a question about an implementation that is free
to change. The repair in each case was to ask the runtime instead — drive the
loop, count the live threads by name, match the guard's AST shape rather than a
substring.

**Rules:**
- A test that reads source text is a last resort, and it must be mutated against
  the *natural* alternative spelling, not a strawman.
- Before writing "pinned by a test" in a description, write the mutation that
  would break the pin and watch it go red. If you cannot think of one, the pin
  is a comment.

### Fail-open that fails worse

`ScheduledCatchupService` put a claim and its follow-up status write in one
`try`, with a comment explaining that fail-open avoids a correctness problem.
A successful claim followed by a failing `update_status` then took the fail-open
branch, which nulled the job id — and the release calls below were guarded by
that id, so the row was abandoned `pending` forever. `idx_active_job_per_repo`
matches `pending`, so every later run on every process hit `DuplicateJobError`
and returned: catch-up stopped cluster-wide. Before the change, the same failure
cost one duplicate run.

**Rule:** fail-open is a property of a *decision*, not of a block. Once a side
effect has happened, the handler owns it. Group `try` blocks by what must be
undone, not by what reads well.

### Hid an error with `tail -1`

Two pipelines failed CI lint on an unused import. `ruff check src tests` *was*
run both times, piped through `tail -1`, which printed `[*] 1 fixable with the
--fix option.` and dropped the `Found 1 error.` line above it. The hint looked
like a suggestion rather than a failure.

**Rule:** check the exit status, not the last line. `cmd || echo FAILED` beats
any amount of `tail`.

## 4. Measurement errors

| Claimed | Measured | Lesson |
|---|---|---|
| The missing index on `compiled_at` is a regression | **1.7 ms** — `so_bytes` TOASTs out of line so the scan never touches blobs. The real cost is a *matching* DELETE: 2.0 s per 500 rows | Measure the hypothesis before fixing it |
| "+987 MB" (Dockerfile), "+972 MB" (commit), "~683 MiB" (agent) | All three are "right" for different methods: `docker images` counts retained compressed blobs under containerd, `docker export` counts file content. The Dockerfile's number matched **neither** | Quote the method with the number |
| "13 of 18 `test_rust_backend.py` failures are binary-absent" | **All 18** were fixable, and one hid a *second* defect — it mocked `subprocess.run` while the code spawns via `Popen` | Don't estimate a split you can count |
| A 61%-complete test sweep, nearly reported as a result | It had been killed by its own timeout. A later "baseline: 0 failures" was an **empty file** from the same cause | Check a run finished before reading its output |
| "78 files are pre-existing failures" | Stale on-disk files from `git checkout-index`, which writes but never deletes. Several classifications were wrong until `git clean -fd` | Clean the tree before classifying failures |

## 5. Fixes that broke things

- **The blanket redaction wrap broke 7 drift/reconcile tests.** `redact_url_credentials`
  was called with `Mock`, `None` and `bytes`; `re.sub` raises `TypeError` on all
  three, turning message formatting into a **crash on the error path where the
  diagnostic is built** — strictly worse than the leak it replaced.
- **Broadening the guard's spawn detection flagged `asyncio.run(coro)`** — the event
  loop runner, not a spawn — producing 17 false positives in one pass.
- **The dual-form verb rule fired before argv was known to be git**, so non-git
  commands were reported as unreadable.
- **A "broad except" survey flagged 92 sites**, counting `_handle_crud_exception(e, …)`
  as silent. Reverted and rebuilt around "exception value referenced nowhere",
  giving 13 real targets. `logger.exception()` already carries the traceback.
- **`_fd_anchored_rmtree` deleted as unused.** It is a deliberate re-export with
  `# noqa: F401` and a test imports it.

**Rule:** a guard that cries wolf gets deleted, and a fix that crashes the error
path is worse than the bug. Test both directions — what must be caught *and* what
must stay quiet.

## 6. Process failures

- **CI's lint command was never run locally.** `ruff check src` was used; CI runs
  `ruff check src tests`, which failed on an undefined name in a test file.
- **`pyflakes` caught two latent `NameError`s that `ast.parse` passed.** Parsing is
  not checking.
- **CLAUDE.md Step 5 documentation was skipped** until it was audited for, despite
  four of its five triggers being hit.
- **Skipped tests went unnoticed** until the human asked. ~46 Postgres-gated and
  ~30 xray-binary-gated tests had **never executed in CI**, because both test jobs
  run an image with neither Postgres nor cargo. They skipped silently through the
  release that shipped xray with no binary at all.
- **`git checkout <branch> -- .` was used to "get the fix back".** It overwrote the
  working tree from another branch, silently reverting the source change that was
  the entire point of the commit. The commit that followed bundled a file belonging
  to a different MR, included two tests, and **omitted the one source file it was
  named after** -- a commit that claimed a fix it did not contain, pushed to a
  shared remote. Recovered from a scratchpad copy; the branch was force-pushed
  with the correct two-file commit and the overwrite disclosed rather than quietly
  replaced. `git checkout <tree-ish> -- .` with a bare `.` is never the right
  recovery: name the paths.
- **Ran `black` on a repo whose pre-commit runs `ruff-format`.** It reformatted
  **1,889 files** before being killed, burying a 2-file change. The formatter a
  repo uses is in `.pre-commit-config.yaml`; `black` merely being installed (it is
  a transitive dev dependency here) is not evidence that it is the one. Also: never
  point a whole-tree formatter at a tree mid-change -- scope it to the files
  touched, where a stray reformat is visible in the diff.

## 7. The one thing that worked, and why

The guard's self-tests were the only code in the range that survived adversarial
review: **22/22 targeted mutations went red.** Everything built without that
discipline broke. The standard adopted afterwards, and the reason the later fixes
held:

> Write the test. **Show it red.** Implement. Show it green. Revert the fix.
> **Confirm it goes red again.** A test that cannot fail is not done.

Mutation testing found two things nothing else did: a blind `_scan()` returning
`([], [])` left all 26 self-tests passing, and a fabricated allowlist entry left
everything green. It also caught a *fix* that passed vacuously — the worker-count
bound, which every in-process test was blind to on a single-worker host.
