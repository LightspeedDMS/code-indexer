---
name: project_test_gates_flake_under_load
description: "fast-automation/server-fast gate flakiness is a hardcoded 15s per-test pytest timeout under load, NOT SQLite contention or test ordering — grep .test-telemetry logs for 'from pytest-timeout' before re-rolling"
metadata:
  node_type: memory
  type: project
  originSessionId: 9f3e846a-213a-4733-9159-8696ede6081c
  modified: 2026-09-15T05:28:25.372Z
---

`fast-automation.sh` and `server-fast-automation.sh` flake when run while other heavy work (subagents, parallel pytest chunks, a just-finished sibling suite) competes for CPU. Observed signatures of LOAD (not real defects):
- `fast-automation.sh` overran 10 min and timed out at ~67% with everything PASSED; ran ALONE+settled it finished in 9:01 (11459 passed, 0 failed).
- `server-fast-automation.sh` chunk 4 reported 53 `OperationalError: unable to open database file` ERRORS at test setup (POST /login) — a SQLite temp-file/fd contention artifact from chunks running in parallel right after fast-automation; a clean settled re-run was all-green (all 6 chunks pass).
- A chunk-4-alone run then showed 2 `test_depmap_activity_journal_endpoint` assertion FAILURES that PASS in isolation (test-ordering flakiness).

**ROOT CAUSE (proven 2026-09-15, #1863): the failures are pytest TIMEOUTS, not assertion failures.** Both gates hardcode a 15-second per-test wall-clock ceiling — `server-fast-automation.sh` via `PYTEST_TIMEOUT="${PYTEST_TIMEOUT:-15}"` and `fast-automation.sh` via a literal `--timeout=15` — and server-fast runs its six chunks CONCURRENTLY on one box, so the suite starves itself with no external load required. Evidence from three runs of an unchanged tree: per-chunk timeout counts equalled failures+errors exactly in every chunk, the all-green run had zero timeouts, and one test measured 5.55s green vs 27.75s red. The tests that trip are the deliberately timing-sensitive lock-contention ones (`test_sqlite_lock_timeout_*`, `alias_lock_store_*_concurrency_*`, `TestCrashRecovery`) — the slowest tests, hence nearest the ceiling. "Every failure touched the DB" is SELECTION BIAS, not a locking signal: that reading (and line 13's SQLite fd-contention explanation above) is what sent an investigation down the wrong path twice. Collection order is deterministic here (no `pytest-randomly`/`random-order`/`xdist` installed), so differing failure sets across runs can NEVER be test-ordering — that fact alone rules out the cross-test-pollution class in one check.

**The one-command diagnostic:** `grep -c "from pytest-timeout" .test-telemetry/*.log`. Nonzero means load, full stop — no isolation re-run needed to know. The per-chunk logs under `.test-telemetry/` persist across runs and carry a `slowest N durations` table, so the same test's duration can be compared green-run vs red-run directly. Check this BEFORE spending a 13-minute re-roll.

**"Alone" is not the same as "idle" — check the MACHINE, not just your own work.** A gate can be the only thing you are running and still be starved by a runaway left behind by an EARLIER session. Real instance (2026-09-08): an orphaned `/bin/bash -c ... eval 'until false; do :; done ... &'` — a backgrounded no-op infinite loop from a shell snapshot 5.5 days old, reparented to systemd (PPID 1), cwd in a *different* repo — had been burning a full core at 99.6% CPU (~470,600s of user time) for the entire session. It was invisible to "run the gate alone" because it was nobody's current work. Two timing-sensitive gate failures being chased that night ([[feedback_tdd_red_must_be_discriminating]]-adjacent, exact-call-count and thread-join-deadline assertions) were both load-sensitive, and `kill` dropped load average 4.00 -> 2.95 instantly.

Before trusting OR blaming a gate run, check `ps -eo pid,etimes,pcpu,args --sort=-pcpu | head` and look for anything with large ELAPSED and high %CPU that is not yours. Confirm before killing: a no-op loop body, zero children, PPID 1, and a cmdline that cannot be doing real work. Killing an orphaned busy-loop is safe; killing something you have not identified is not (see [[feedback_own_all_repo_changes]] and the anti-rogue-checkout rule — the same caution applies to processes).

**How to apply:** Run each gate ALONE (no concurrent subagents/test runs), system settled. When a gate fails, re-run the exact failing tests/chunk in isolation BEFORE concluding regression — if they pass alone, it's load/ordering, not your change. Don't dismiss as "pre-existing" without the isolation re-run (see [[feedback_zero_failures_no_excuses]]); but a settled all-green run is the honest gate. Bash tool caps foreground commands at 600000ms, so for a legitimately >10-min suite run detached (`nohup ... &` or a self-contained `run_in_background` with an exact-PID `until ! kill -0 $PID` wait) and monitor the log — never fire-and-forget.

Related: omni `*` / wildcard search is an MCP-tool feature (`search_code`/`regex_search` via `POST /mcp`), NOT REST `/api/query` — test #1119-type omni behavior through the MCP front door (Basic-auth MCP creds), not `/api/query` (which 404s on `*`).
