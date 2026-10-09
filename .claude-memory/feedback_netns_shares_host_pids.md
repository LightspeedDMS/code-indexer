---
name: feedback-netns-shares-host-pids
description: "A user/net/mount namespace made with unshare (no --pid) shares the host PID space: pgrep 'uvicorn ...' inside it also matches the developer's real server; never kill by pgrep in test sandboxes"
metadata:
  type: feedback
---

`unshare --user --net --mount` (as the reproduction harness and front-door plans use) isolates the network and mounts but NOT process IDs. `pgrep -f 'uvicorn code_indexer.server.app'` run inside it returns the developer's own dev server (port 8000) as well as the test server.

**Why:** 2026-10-09 the 12.84.0 front-door plan's teardown was `kill -TERM "$(pgrep -f 'uvicorn code_indexer.server.app' | head -1)"`; the first match was the developer's dev server. The executor noticed and killed only the test server's PID; the plan was corrected.

**How to apply:**
- In any sandbox plan or script, stop processes by the PID file written at launch, or match the exact port AND verify `readlink /proc/<pid>/ns/net` equals the sandbox namespace before signalling.
- Never `pkill`/`kill $(pgrep ...)` by process name in a shared-PID sandbox.
- Related: [[project-own-local-dev-cidx-server]] (keep the dev server up).
