---
name: project-backlog-clear-to-zero-mandate
description: "User wants the ENTIRE code-indexer GitHub bug backlog cleared to zero (or low-priority diminishing-returns) -- standing mandate for the ongoing /implement-backlog session, with a running discovered-vs-closed tally"
metadata:
  type: project
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-27T18:49:14.145Z
---

User's explicit instruction (2026-08-27): "I want the entire backlog clear, so keep the running tally of new stories discovered in the queue. hopefully at some point we converge into zero open, or low priority open (diminishing returns)."

**How to apply**: this is the standing goal for the ongoing session, not a one-off request. Keep dispatching into the 4-agent parallel cap continuously, pulling from `gh issue list --state open --label bug` (excluding stories/epics per the user's own scoping) rather than only the items already known from in-session discovery -- periodically re-run that full list query (not just the priority queue built up so far) to catch anything filed outside the active investigation thread, since #1649/#1651/#1654 were found this way after being missed for a long stretch.

**Track a running tally** at natural checkpoints (after a batch of closes, or when asked to /report): total open bug count, count closed this session, count newly discovered/filed this session, net change. Report this tally proactively when it's a meaningful milestone (e.g. crossing under 10 open, or after closing a batch), not just when asked.

**Convergence definition**: stop condition is either (a) zero open bug-labeled issues, or (b) all remaining open issues are low-priority (priority-4) items with genuine diminishing returns (e.g. multi-session-scoped systematic sweeps like #1685/#1696 that have no more bounded next-batch available, or issues requiring live infra access not available in this session like #1615's clustered-staging diagnosis). Do not artificially stop early -- keep pulling the next-highest-priority item into every freed slot.

**Standing rules still apply** (see [[project_backlog_session_paused_2026_08_27]] for the full list): 4-agent parallel cap (raised from 2 this session per [[feedback_parallel_dispatch_cap_4_on_20x]]), every dispatch prompt forbids git worktree/broad git-add, phantom-background-wait correction via SendMessage whenever an agent ends its turn expecting a notification, push only after review approval and only to `origin/development` (never staging/master without explicit fresh authorization), file a follow-up issue for every new defect discovered mid-fix rather than silently fixing or ignoring it out of scope.
