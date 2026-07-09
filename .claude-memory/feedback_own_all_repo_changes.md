---
name: feedback-own-all-repo-changes
description: NEVER revert or checkout changes from other subagents — own ALL changes found in repo
metadata: 
  node_type: memory
  type: feedback
  originSessionId: abca105e-1e61-4aeb-ae5f-c7a7915e83df
---

NEVER use git checkout/restore/revert on changes you didn't make. All changes in the repo are from legitimate sibling subagents.

**Why:** User explicitly corrected this behavior when main context attempted to `git checkout --` files modified by the TDD engineer subagent (xray-core language additions). The anti-rogue-checkout rule in CLAUDE.md is absolute — there are NO unauthorized agents, every change is legitimate work.

**How to apply:** When preparing a commit, stage ALL modified and untracked files. If a subagent added changes beyond the original scope (e.g., language additions alongside bug fixes), include them in the commit with proper changelog documentation. Never classify sibling subagent work as "unrelated" and revert it. Related: [[feedback_never_touch_other_repos]]
