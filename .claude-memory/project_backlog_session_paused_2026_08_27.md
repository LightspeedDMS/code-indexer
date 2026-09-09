---
name: project-backlog-session-paused-2026-08-27
description: "Exact resume state for the /implement-backlog session paused 2026-08-27 by explicit user 'stop the agents' -- what's done, what's mid-flight, what's next"
metadata:
  type: project
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-27T13:05:33.037Z
---

Session was running the same long `/implement-backlog` sweep from 2026-08-26 (continued after a VM-migration pause -- see [[project_backlog_session_paused_for_vm_migration]], now superseded by this file) when the user said "stop the agents" with no further context. Both running agents were stopped cleanly via TaskStop; working tree confirmed clean (`git status --short` shows only pre-existing modified/untracked files that predate this whole session -- nothing from either stopped agent).

**How to apply**: on resume, read this memory, then continue the backlog loop from "Next up" below -- no need to re-derive the whole plan.

## Git state at pause

`development` HEAD = `41f7f151`, 4 commits ahead of `origin/development`:
```
41f7f151 fix(#1698): compare branch medians instead of raw min/max to fix timing-test flake for real
c964e416 fix(#1689): defer AutoWatchManager thread and FileCRUDService ActivatedRepoManager construction
2906aec9 fix(#1698): warm up real bcrypt path before timing measurements to kill cold-start flake
5b287bfc fix(#1698): eliminate cross-file and standalone pollution across tests/unit/server/auth/
```
Everything through `b74eb466` (#1687) is already pushed and closed. **None of the 4 commits above are pushed yet** -- do NOT push until #1698's round-3 review (see below) completes and approves, since `41f7f151` sits on top of the whole stack and `c964e416` (#1689, already independently APPROVED) is sandwiched in the middle of an otherwise-still-open #1698 chain.

## Mid-flight at the moment of pause (both stopped cleanly, zero uncommitted WIP)

**#1698 round-3 review** (agent `ac54f0c8249a8b6f8`, code-reviewer): stopped right after establishing a timing baseline (`Load average 2.14 on 12 cores`), before running its real verification. **Next step: re-dispatch a fresh code-reviewer for commit `41f7f151`** -- the original round-3 review prompt is fully reusable (it asks for: confirm 1-file diff scope, verify the median-based comparison is implemented correctly with no divide-by-zero risk, verify the unit-level warm-up was cleanly reverted, run the FULL `tests/unit/server/auth/` directory 4-5+ times to confirm `test_timing_attack_real.py` never fails, and confirm no weakening of round-1's already-approved substance). This is the THIRD round for #1698 -- round 1 (`5b287bfc`) was substantively approved but round 2's own timing-flake fix (`2906aec9`) was rejected as insufficient (fixed only the position-1 cold-start case, not the general outlier-at-any-position case); round 3 switched to a median-of-3-samples-per-branch comparison specifically to close that gap. If round 3 is APPROVED, push the whole 4-commit stack together (`5b287bfc`+`2906aec9`+`c964e416`+`41f7f151`) and close both #1698 and #1689 (see below -- #1689 is already approved, just blocked on push ordering).

**#1692** (implementer agent `a8b7d423f48cbdb16`, tdd-engineer): stopped mid-investigation, BEFORE writing any code -- it was still checking git history/existing tests for a legitimate reason `file_crud_service.py`'s orphan-write permissiveness might be intentional, per the dispatch prompt's explicit instruction to investigate before fixing. **Zero uncommitted changes exist for this item** -- it needs a fresh dispatch from scratch (the original prompt is fully reusable; read `python3 ~/.claude/scripts/utils/issue_manager.py read 1692` again for full context). Note: `file_crud_service.py` was very recently touched by the now-approved #1689 fix (`c964e416`) -- any fresh dispatch for #1692 should read the CURRENT (post-#1689) state of that file, not assume the pre-#1689 shape.

## Already fully closed this session (2026-08-26 continuation, do not re-open)

#1663, #1684, #1681 (both rounds), #1686 (both rounds), #1687, #1682 (closed as duplicate of #1698), plus everything listed as closed in [[project_backlog_session_paused_for_vm_migration]] before that.

## Approved-but-unpushed this session (blocked only on #1698 round 3, see above)

**#1689** (`c964e416`): reviewed and APPROVED with an unusually strong structural argument -- both fixed singletons keep their eager module-level binding with NO PEP-562 `__getattr__`, so the #1686 "missed from-import consumer" crash class is structurally impossible here (no `None` sentinel exists for a stale import to bind to). Reviewer also found an unclaimed bonus: this fixes a real per-request thread-explosion hazard (`routers/files.py` constructs a fresh `FileCRUDService()`, and therefore a fresh `ActivatedRepoManager`, on every single REST create/edit/delete call -- was ~7 threads + a golden-repo SQLite load per request, now near-zero). Two non-blocking documentation nits noted (a boot-test's discriminating claim was slightly overstated; the 2 pre-existing test errors in the regression sweep were mis-attributed to the wrong sibling agent's work in the commit's own claim) -- worth a one-line correction in the eventual closing comment but not blocking. **Just needs the push once #1698 unblocks.**

## Priority queue remaining (not yet started)

Priority-3: #1690, #1693 -- wait, check current state, these were priority-4 -- re-verify via `issue_manager.py read` before trusting this list, it may have drifted. As of last check: **#1697** (priority-3, "11+ PostgreSQL backends carry the same dead/drifting `_ensure_schema()` pattern already fixed twice -- Messi Rule 4 three-strike sweep needed").

Priority-4, not yet touched: **#1688** (stale `scip_audit_repository` mock-target name mismatch, 5 failures/12 errors in `-k scip` sweep), **#1690** (~10 other `ConfigManager.create_with_backtrack()` call sites blind-trust the result, same class as #1683, read-only paths only), **#1693** (`xray.py`'s `_get_xray_executor`/`_get_xray_cell_limiter` readers still use side-effectful `getattr()`, unlike their now-fixed setters -- follow-up from #1678), **#1695** (7 stale-mock-shaped standing-red tests in `test_auth_endpoints.py`), **#1699** (separate eager `GoldenRepoManager` singleton reachable via `inline_routes.py`'s import chain -- same defect class as #1686/#1638/#1650/#1689, filed but not investigated), **#1700** (102 failed/18 errors pre-existing baseline across the FULL `tests/unit/server/` sweep, found during #1686's regression testing -- likely another instance of the stale-fixture-auth class already fixed for `tests/unit/server/auth/` via #1681/#1698, but spanning a much wider unknown set of files; explicitly flagged as possibly needing its own dedicated multi-session sweep like #1685/#1696, assess file/failure-cause diversity first before committing to a single session).

Deliberately deferred, multi-session scope (do NOT casually pick these up):
- **#1615**: needs live clustered-staging diagnosis (3-node PG/HAProxy), deferred all session.
- **#1685**: large systematic audit of ~260 `fast-automation.sh` `--deselect` entries, explicitly multi-session scoped.
- **#1696**: mypy cross-module `Any`-typing gap -- investigated and SCOPED (not fixed): the "fix" (`mypy_path = "src"`) actually crashes mypy outright because 308 files (mostly tests) already use the hazardous `src.code_indexer.*` import spelling. Full scoping report posted to the issue with a recommended 3-session split (eliminate the 308 bad imports -> land the mypy_path fix -> triage the newly-surfaced type errors). Left open, tree untouched.
- **#1659**: stays open with investigation notes only (a latent, unexploited hazard in `middleware/__init__.py`'s lazy singleton, noted but not actioned).

## Standing rules that applied all session (still apply on resume)

- 2-parallel-subagent dispatch cap, always -- verify via `ListAgents` before every new dispatch.
- Every dispatch prompt: "Do NOT invoke Agent/Task tool yourself" + "NEVER use git worktree in this repo" (editable-install dual-import trap) + "stage/commit your own exact file list, never git add -A".
- **Phantom-background-wait is BY FAR the single most common agent failure mode this session** -- occurred on nearly every long-running dispatch, sometimes twice for the same agent. When a resumed/dispatched agent says anything like "waiting for a Monitor/background notification," always correct via SendMessage telling it to check/poll/block synchronously itself -- there is no notification mechanism reaching a dispatched subagent.
- tdd-engineer -> code-reviewer -> push -> close issue, every item (manual-test-executor mostly skipped this session for pure test-infra/backend fixes with exceptionally thorough review-level verification instead).
- **Commit-ordering discipline**: this session hit real cases where an approved fix sat ON TOP of a still-rejected/incomplete sibling commit in linear git history (both #1681-on-#1686 and #1689-on-#1698). Always check `git log origin/development..development --oneline` before pushing anything -- if a broken/incomplete commit sits between origin's tip and your approved commit, you cannot push the approved one alone; wait for the blocker to clear, then push the whole verified stack together.
- Push to `origin/development` after every approval (and after resolving ordering above). NEVER push to staging/master without a fresh, explicit, in-the-moment user phrase -- not touched or implied all session.
- File a follow-up GitHub issue for every new defect discovered mid-fix rather than silently fixing-or-ignoring it out of scope; this session filed #1698, #1699, #1700 this way (plus more from the prior 2026-08-26 segment).
- When two issues describe the same underlying defect (discovered independently), close the less-complete one as a duplicate pointing to the more-complete one rather than working both -- done for #1682 -> #1698 this session.
