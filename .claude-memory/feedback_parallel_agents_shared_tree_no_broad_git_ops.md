---
name: feedback-parallel-agents-shared-tree-no-broad-git-ops
description: "When running parallel subagents on the same uncommitted working tree, explicitly forbid any git checkout/restore/reset/clean/stash on paths outside their own assignment"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9f2bc45f-0085-4f49-90f3-7e65bdd67bcf
---

When dispatching multiple parallel tdd-engineer/fix agents against the SAME uncommitted working tree (e.g. splitting one bug's fix into independent file-scoped streams), each agent's prompt must explicitly forbid `git checkout`/`restore`/`reset`/`clean`/`stash` on any path outside its own exact assigned file list — and must instruct it to ignore, not "clean up," any unfamiliar modified files it sees in `git status`.

**Why**: During a 4-way parallel doc-fix batch (#1356), one agent's own subagent left ~19 files modified beyond its 3-file assignment. The parent agent wrongly concluded this was scope creep from its own subagent and ran `git checkout -- <19 paths>` to "clean up" — but those 19 files were actually 2 sibling agents' legitimate, already-completed work (AC2/AC3) plus 6 files a third sibling (AC4) was still writing. This silently reverted two fully-completed, already-reported fix streams with zero trace in git log/reflog (a working-tree-only revert, not a commit), discovered only because a later `git diff --stat` unexpectedly came back empty. Recovery required re-deriving the exact fixes from the agents' own prior completion reports and redispatching narrowly-scoped restore agents.

**How to apply**: Any time you fan out N agents to edit different files in the SAME repo without committing between them, bake this into every prompt: "ONLY touch these exact files: [list]. Do NOT run git checkout/restore/reset/clean/stash on ANY path — if you see unexpected modified files outside your list, IGNORE them, they belong to sibling agents." Cheaper to over-specify this every time than to lose completed work mid-batch. See [[feedback_own_all_repo_changes]] and [[feedback_no_rogue_agents]] for the related "don't revert what you don't recognize" principle — this extends it to agent-vs-agent, not just agent-vs-human, uncommitted state.
