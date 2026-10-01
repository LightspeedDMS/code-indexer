---
name: feedback-never-retry-loop-auth-endpoint
description: "Never poll an auth endpoint in a retry loop — auth rejection is terminal, and repetition converts a harmless failure into an account lockout"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 3d216558-2b0c-4da6-83b0-c76caa9c86c9
  modified: 2026-08-12T10:12:29.933Z
---

A polling loop that authenticates on every iteration MUST treat an auth rejection as TERMINAL and
stop immediately. Only transport failures (connection refused, timeout, 5xx) are retryable.

**Why:** on 2026-08-12 I wrote a loop to poll the staging server's version until it reported a new
release. It re-authenticated each iteration and retried on ANY failure. The early attempts failed
while the server was mid-deploy, the loop kept going, and the repeated `/auth/login` calls tripped
the server's own brute-force protection:

```
account_locked: Too many failed attempts. Try again in 804 seconds.
```

I locked the shared staging admin account for ~13 minutes, blocking myself and anyone else. The
credentials were correct the whole time — the loop, not the password, caused the outage.

**Two compounding mistakes, both avoidable:**

1. **No failure-type discrimination.** "Server unreachable" and "credentials rejected" were handled
   identically. A rejected credential never succeeds on repetition; retrying it can only do harm.
2. **The loop logged a GUESS instead of the response.** Every iteration printed my assumed
   explanation ("server may be restarting mid-deploy") rather than the actual response body. Forty
   iterations passed before I saw the real error. A diagnostic that prints an assumption is worse
   than no diagnostic — it actively delays discovery.

**How to apply:**

- Authenticate ONCE outside the loop; reuse the token. Re-auth only on a 401 mid-poll, and at most
  once or twice with backoff.
- Break immediately on any 4xx from the auth endpoint. Print the response body.
- Never let a poll log a hypothesis in place of the server's own error text.
- When a long poll fails repeatedly, stop and inspect a single real response before iterating again —
  see [[feedback_study_anomalies_deeply]].
- Waiting out a lockout is the only fix; do not "try a different account" or hammer harder.

Related: [[feedback_active_monitoring_check_back]] (poll at bounded intervals, watch for real
signals), [[reference_staging_totp_programmatic_auth]] (the two-step TOTP login this loop was
performing).
