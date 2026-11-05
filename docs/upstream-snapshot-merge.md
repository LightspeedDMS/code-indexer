# Upstream snapshot merge: procedure and the merge-base trap

How the fork takes a new `LightspeedDMS/code-indexer` release. Lessons from
earlier snapshots: [upstream-merge-corrections.md](upstream-merge-corrections.md).
The guards that must pass afterwards: [git-seam-guard.md](git-seam-guard.md),
[content-seam-inventory.md](content-seam-inventory.md).

## Symptom: hundreds of conflicts and "restore internal feature" commits

The first v12.89.0 attempt (, first push) reported 636 conflicting files,
took upstream's side wholesale for 540 of them, and then needed 30 follow-up
commits restoring internal code it had overwritten, while CI smoke still had
92 failures.

**Root cause: the wrong merge base.** Every snapshot is a single-parent commit
on internal master (`Update cidx snapshot to vX (LightspeedDMS/code-indexer @
<sha>)`), so internal master never contains an upstream commit after the first
fork point. `git merge-base internal/master upstream/master` returns
**v11.30.0**, not the last snapshot, and git then three-way merges five
releases of upstream against internal code that already contains four of
them. With the correct base (the `@ <sha>` the previous snapshot names) the
same merge had **84** conflicting files.

## Procedure

```bash
# full clone with both remotes (upstream = LightspeedDMS/code-indexer)
git fetch internal && git fetch upstream --tags
# 1. the upstream commit the LAST snapshot was taken from
git log --format='%h %s' internal/master | grep -m1 'Update cidx snapshot'
BASE=ed9977512            # "@ <sha>" from that subject (v12.78.0 + 7)
TARGET=v12.89.0
# 2. a throwaway commit: internal master's TREE with the upstream base as parent
TMP=$(git commit-tree -p "$BASE" -m tmp internal/master^{tree})
git worktree add -b tmp/parity "$SCRATCH/wt" "$TMP"
cd "$SCRATCH/wt" && git merge --no-commit --no-ff "$TARGET"   # base == $BASE
# 3. resolve, run the guards and suites, then commit the RESULT TREE as a
#    single-parent snapshot on internal master (the history convention):
git commit-tree -p internal/master -F msg.txt "$(git write-tree)"
```

Resolve every hunk from both intents, never by picking a side per file:
`git log ed9977512..v12.89.0 -- <file>` for upstream, `git log
ed9977512..internal/master -- <file>` for the fork. Then re-read what merged
**without** conflict: upstream code that runs `subprocess.run(["git", ...])`,
`run_with_cancel(["git", ...])` or reads repository files directly merges
cleanly and bypasses the seams. The git-seam guard, the unredacted-stream
guard and the content-seam inventory find most of them; read the rest.

## v12.89.0 adaptations worth knowing

| Upstream change | Through the seam |
|---|---|
| Bug #2012: every refresh git subprocess cancellable (`run_with_cancel`) | `utils.git_runner.git_cancel_scope(check)`: every git command reaching `run_git_command` inside the block is cancellable, whichever `GitBackend` method built it. A delegated (git-mcp) request is checked before it is sent. |
| Credentials supplied at run time, never stored in a clone | `GitBackend.clone/fetch/pull/ls_remote(credentials_url=)`; `LocalGitBackend` passes it to `run_git_command`, git-mcp ignores it (`owns_credentials`). `ensure_remote_url_without_credentials` is a no-op on a delegated server. |
| Bug #2148: refresh keeps the clone on its REGISTERED branch | `_ensure_registered_branch` reads the branch with `head_branch` and checks out through the backend. |
| One push implementation (`server/git/git_push.py`) | Used when the working copy is local; a delegated copy pushes through `DelegatedGitTools` with git-mcp's credential (`_delegated_push`). |
| Pod-pull dispatch moved to `pod_pull_dispatch.build_index_dispatch` | The fork's refresh, FTS-backfill and semantic-backfill ops are added there; executors drop the claim loop's injected `job_id` before spreading metadata. |
| In-process restore of `chunks.db` (no clone backend) | Kept; the fork's `previous_points` collapse check rides along (`run_integrity_gate_against_published(previous_points=, healthy_index_dir=)`). |
