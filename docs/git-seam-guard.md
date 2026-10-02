# cidx git seam: the CI guard, and why it kept failing open

**Symptom** — a raw `subprocess.run(["git", "push", ...])` under
`src/code_indexer/server/` or `global_repos/` reaches CI green. Or: a credential
appears in a job error, an API response or a log line.

## The seam

`utils/git_runner.run_git_command` is the ONE chokepoint git calls bottom out at.
It asks `is_mutating_git()` whether a command mutates and delegates mutating ones
to git-mcp. Reads stay local — deliberate and credential-driven: a read needs the
working tree (already in the pod on shared NFS) and no PAT; a mutation needs the
PAT, which EVO-64616 confines to git-mcp.

Upstream has no reason to know the seam exists, so **every upstream snapshot can
reintroduce raw calls in code that merges without conflict**. The v12.78.0
snapshot brought four into `golden_repo_manager.py` alone.

## The guard, and the lesson

`tests/unit/server/test_git_seam_no_raw_subprocess.py` is a static AST check.
Its contract is FAIL CLOSED: anything it cannot statically read is reported as a
violation or as `unknown`, never silently skipped.

It took **six review rounds** to stop failing open, because each round closed the
one shape the reviewer named. An adversarial pass then found **41 more**. Two root
causes explained nearly all of them:

1. **`_git_argv` returned `(None, None)` for anything it could not read**, which
   the scanner cannot tell apart from "this call is not git at all". Silence was
   the bug.
2. **Only `Assign`/`AnnAssign` were treated as bindings.** Every other binding
   form — `for` target, `with ... as`, walrus, tuple unpacking, `except ... as`,
   comprehension target — looked *unbound*, which the guard read as "parameter,
   someone else's problem". The one legitimate silence is a real parameter, and
   that must be decided from the **signature**, not inferred from the absence of
   an assignment.

Two shapes worth knowing about specifically:

- **The dual-form smuggle.** `cmd = ["git","remote"]` then
  `cmd.extend(["add", name, url])`. The append/extend exemption reasoned that
  those only add to the END so the program and verb survive — true, and beside
  the point: for `remote`/`branch`/`tag`/`config`/`stash`/`submodule` the
  **sub-verb decides**, and it arrives in the appended tail.
  `is_mutating_git(["git","remote"])` is `False`. This is the house idiom —
  `storage/shared/git_backend.py:481` is literally this.
- **`asyncio.run(coro)` is not a spawn.** Matching a bare `run` on any receiver
  flags every event-loop call in the tree. Scope generic spawn names by receiver
  module; only distinctive names (`create_subprocess_exec`, `posix_spawn`) may
  match any receiver. For an unidentified receiver, report only a literal `git`
  argv — a guard that cries wolf gets deleted.

```bash
# run the guard
cd code-indexer && PYTHONPATH=src python3 -m pytest \
  tests/unit/server/test_git_seam_no_raw_subprocess.py -q
```

## It must be wired to CI

For a long time the guard was referenced by **no CI job, no pre-commit hook and no
script**; `fast-automation.sh` passes `--ignore=tests/unit/server/`. The commit
that added it was titled "...and enforce it in CI" and touched zero CI files. A
guard whose whole purpose is catching what the *next snapshot merge* reintroduces
— precisely when nobody runs a local script — is worth zero until CI runs it.
It is now in `.gitlab-ci.yml`'s test job; `build:image` is gated on that stage.

## Mutation testing is the standard

The guard's self-tests were the only code in this range that survived adversarial
review: 22/22 targeted mutations went RED. Everything built without that
discipline broke. **A test that cannot fail is not done.** For every fix: write
the test, *show* it red, implement, show green, revert the fix, confirm red again.

Two gaps mutation testing found that nothing else did:
- Making `_scan()` return `([], [])` — the guard fully blind — left all self-tests
  passing, because the production assertions are vacuous while the tree is clean.
  There is now a liveness control.
- A fabricated `ALLOWED_UNREADABLE_ARGV` entry left everything green; only
  `ALLOWED_READS` was audited for staleness. Both are audited now.

## Credential redaction

`utils/subprocess_diagnostics` redacts at the **formatting boundary**, so every
caller is covered. Redact *before* truncating: capping first can sever a
credentialed URL before the `@` the pattern anchors on, leaving a live token
prefix.

The leak is reachable because `LocalGitBackend.owns_credentials is False` in the
default deployment, so the PAT stays in `repo_url`, lands in `git clone` argv and
in the clone's `.git/config` origin — and git echoes it back in
`fatal: unable to access 'https://oauth2:<PAT>@host/...'` on exactly the auth
failures operators are told to investigate.

`tests/unit/server/test_no_unredacted_subprocess_streams.py` is a static guard for
the other shape: code interpolating `e.stderr` / `result.stderr` straight into an
f-string, bypassing the formatter. It found **32 such sites**. Note its limit: it
sees direct attribute interpolation, not a stream assigned to a local first.

## Classifier gotcha

`git remote <unrecognised-subverb>` used to classify as a **read** and run
locally, with the credentials the seam exists to confine — because the check
asked whether the sub-verb was in a WRITE set. `branch`/`tag`/`stash` default the
other way. Unknown means write.

## Related

- [CIDX_XRAY_QUERY_TIME_TOOLCHAIN.md](xray-query-time-toolchain.md)
- [CIDX_UPSTREAM_MERGE_CORRECTIONS.md](upstream-merge-corrections.md) — claims from this work that proved false, and why
