---
name: feedback-parallel-dispatch-cap-4-on-20x
description: "User's Claude subscription is 20x tier -- parallel subagent dispatch cap raised from 2 to 4 concurrent agents"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-27T13:30:52.646Z
---

The user is on a 20x Claude subscription and explicitly raised the standing parallel-subagent dispatch cap from 2 to 4 concurrent agents: "you can go to four agents at a time now. we are on a 20x subscription."

**Why**: Higher subscription tier supports more concurrent usage; the 2-agent cap used throughout the prior `/implement-backlog` sessions was a conservative default, not a hard platform limit.

**How to apply**: In this project (code-indexer), default the parallel-subagent dispatch cap to 4 (not 2) for `/implement-backlog` and similar multi-item sweeps, verified via `ListAgents` before each new dispatch same as before. All the same safety rules still apply at the higher cap: each dispatch prompt must still forbid touching sibling agents' files, forbid `git worktree` (dual-import trap), forbid broad `git add -A`, and must instruct agents to block synchronously rather than expect a background-notification mechanism (see [[feedback_no_subagent_to_subagent_delegation]] and the phantom-background-wait pattern noted in [[project_backlog_session_paused_2026_08_27]]). If a future session finds 4 concurrent agents causing resource contention (CPU/memory pressure, flaky shared-tree test runs), that's worth flagging back to the user rather than silently reverting to 2.
