---
name: feedback_prove_root_cause_before_fix
description: Prove a server-stall/concurrency root cause with direct runtime evidence (py-spy thread dump) BEFORE building/committing a fix — never infer it from architecture
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a76c9d1b-1728-40f2-b629-06af8d4b9f01
---

When diagnosing a server stall / concurrency collapse, prove the root cause with DIRECT RUNTIME EVIDENCE before committing a fix. Do not infer a cause from architecture and do not conclude "no X happening" from grepping logs unless you've confirmed that X is actually logged.

**Why:** During bug #1078 a whole `ProviderConcurrencyGovernor` + 429-backoff fix (v10.106.0) was designed by two expert agents, implemented, reviewed, and committed for a "VoyageAI 429 storm" that was NOT happening. The 429 code paths emit no log lines, so "zero 429s in the log" proved nothing. The user (rightly) challenged: "are you sure you are getting 429s? are you logging those? no bullshit, only facts." The REAL cause — `SQLiteLogHandler.emit()` doing a synchronous SQLite write while holding the Python logging handler lock — was found in minutes with `py-spy dump`, which showed 32 request threads parked on `logging/__init__.py:901` `Handler.acquire()`.

**How to apply:**
- For a hung/stalled server, run `py-spy dump --pid <PID>` (py-spy 0.4.1 is installed at `~/.local/bin/py-spy`) while it's stalled. Filter to the request-path threads and read where they're parked (sleep / socket recv / lock acquire / semaphore). Take 2-3 dumps a few seconds apart — identical stacks = a hard stall, not progress.
- Reproduce the stall with a persistent background burst (run_in_background curls behind the front door), NOT inline `&` curls (those die when the Bash call returns, leaving all threads idle at dump time).
- Before claiming "X isn't happening" from logs, verify X is actually logged. If not, instrument or use py-spy.
- Validate the fix the same way: re-dump under the burst and assert the stall is gone (32 -> 0 threads on the lock; 48/48 requests succeed).

See [[feedback_verify_codex_actually_ran]] for the related "verify, don't assume" pattern.
