---
name: project_test_gates_flake_under_load
description: "fast-automation/server-fast flake under concurrent CPU load — run them ALONE; failures that pass in isolation are load artifacts, not regressions"
metadata: 
  node_type: memory
  type: project
  originSessionId: 9f3e846a-213a-4733-9159-8696ede6081c
---

`fast-automation.sh` and `server-fast-automation.sh` flake when run while other heavy work (subagents, parallel pytest chunks, a just-finished sibling suite) competes for CPU. Observed signatures of LOAD (not real defects):
- `fast-automation.sh` overran 10 min and timed out at ~67% with everything PASSED; ran ALONE+settled it finished in 9:01 (11459 passed, 0 failed).
- `server-fast-automation.sh` chunk 4 reported 53 `OperationalError: unable to open database file` ERRORS at test setup (POST /login) — a SQLite temp-file/fd contention artifact from chunks running in parallel right after fast-automation; a clean settled re-run was all-green (all 6 chunks pass).
- A chunk-4-alone run then showed 2 `test_depmap_activity_journal_endpoint` assertion FAILURES that PASS in isolation (test-ordering flakiness).

**How to apply:** Run each gate ALONE (no concurrent subagents/test runs), system settled. When a gate fails, re-run the exact failing tests/chunk in isolation BEFORE concluding regression — if they pass alone, it's load/ordering, not your change. Don't dismiss as "pre-existing" without the isolation re-run (see [[feedback_zero_failures_no_excuses]]); but a settled all-green run is the honest gate. Bash tool caps foreground commands at 600000ms, so for a legitimately >10-min suite run detached (`nohup ... &` or a self-contained `run_in_background` with an exact-PID `until ! kill -0 $PID` wait) and monitor the log — never fire-and-forget.

Related: omni `*` / wildcard search is an MCP-tool feature (`search_code`/`regex_search` via `POST /mcp`), NOT REST `/api/query` — test #1119-type omni behavior through the MCP front door (Basic-auth MCP creds), not `/api/query` (which 404s on `*`).
