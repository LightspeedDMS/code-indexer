---
name: project-backlog-session-paused-for-vm-migration
description: "Exact resume state for the /implement-backlog session paused 2026-08-26 for a VM host migration -- what's done, what's mid-flight, what's next"
metadata:
  type: project
  originSessionId: 94835e39-16ea-40b6-873e-c9e117eefb19
  modified: 2026-08-26T20:22:28.574Z
---

Session was running a long `/implement-backlog` sweep (fix every discovered defect, priority order, keep working agentically) when the user paused it to migrate this VM to another host. Two agents were cleanly stopped via `TaskStop` before the pause; nothing was lost.

**Git state at pause**: `development` HEAD = `9c906050` (fix #1673), 1 commit ahead of `origin/development` (pushed everything through `fc4e258e` already; `9c906050` still needs its review to finish before push).

**How to apply**: on resume, read this memory, then continue the backlog loop from "Next up" below -- no need to re-derive the whole plan.

## Completed and closed this session (do not re-open)
#1676 (whole 8-AC OTEL story), #1679, #1671, #1658/#1659(partial-left-open), #1666, #1667, #1669, #1670, #1683 (4 rounds), #1660, #1661, #1672, #1677, #1691, #1678, #1675, #1673 (fix landed, review was mid-flight at pause -- see below).

## Mid-flight at the moment of pause

**#1673** (`tests/unit/server/auth/oidc/conftest.py`, commit `9c906050`): fix is committed and NOT pushed. Its code-reviewer agent (`a3cee3a5081f5c291`) was stopped mid-verification -- it had confirmed the implementer's "32 failed/876 passed/10 skipped/14 errors pre-existing" claim reproduces exactly (Phase 1 done), but Phase 2 (re-verifying with the fix restored) was not reached. **Next step: re-dispatch a fresh code-reviewer for commit `9c906050`** (the original review prompt is fully reusable, just re-issue it) rather than trying to resume the killed agent's context.

**#1694** (routers/ DependencyMapService leak, follow-up to #1675): implementer agent (`adb4ea45eb1b6629a`) was stopped mid-verification, ~59 min in. **Real uncommitted WIP exists in the working tree, deliberately left untouched**:
- `M tests/unit/server/conftest.py` -- likely the shared-fixture approach the prompt suggested as preferred
- `?? tests/unit/server/test_app_state_leak_protection_1694.py` -- new test file, uncommitted

**Next step**: do NOT discard this WIP. Inspect `git diff tests/unit/server/conftest.py` and the new test file to see how far the implementer got, then either resume/re-dispatch a tdd-engineer with instructions to pick up from this exact partial state (read the existing diff first, finish the fix, run the full 3-directory sweep, commit), or if the partial approach looks wrong, have a fresh agent evaluate it and decide whether to build on it or start over -- but always read it first, per this project's "own all repo changes, never discard" rule.

## Priority queue remaining (not yet started)

Priority-2/3 still open: none identified beyond what's listed below.

Priority-3/4, not yet touched: **#1615** (needs live clustered-staging diagnosis, 3-node PG/HAProxy -- deferred all session, needs dedicated focused pass, likely via SSH MCP to staging), **#1657**, **#1662**, **#1663**, **#1674**, **#1680**, **#1681**, **#1682** (overlaps significantly with #1673's own confirmed-pre-existing 32/14 failures in `tests/unit/server/auth/` -- worth checking if #1682 already covers this or needs updating), **#1684** (symptom 2 only, symptom 1 already resolved via #1661), **#1685** (large systematic audit of ~260 fast-automation.sh `--deselect` entries, explicitly scoped as multi-session), **#1686**, **#1687**, **#1688**, **#1689**, **#1690**, **#1692**, **#1693**.

**#1659** stays open with notes (not a bug to fix, an audit trail -- see the issue's own comments for the `middleware/__init__.py` latent hazard noted but not actioned).

## Standing rules that applied all session (still apply on resume)
- 2-parallel-subagent dispatch cap, always.
- Every dispatch prompt: "Do NOT invoke Agent/Task tool yourself" + "NEVER use git worktree in this repo" (editable-install dual-import trap) + "stage/commit your own exact file list, never git add -A".
- Phantom-background-wait is the single most common agent failure mode this session -- when a resumed/dispatched agent says "waiting for a notification," always correct via SendMessage telling it to block synchronously itself.
- tdd-engineer -> code-reviewer -> (manual-test-executor when warranted, sometimes skipped for pure-test-infra fixes with exceptionally thorough review-level verification) -> push -> close issue, every item.
- Push to `origin/development` after every approval. NEVER push to staging/master without a fresh, explicit, in-the-moment user phrase (see project CLAUDE.md's hardened master-push protocol) -- not touched or implied all session.
