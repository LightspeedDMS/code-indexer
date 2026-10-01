---
name: feedback-design-for-900-repo-scale
description: Design EVERYTHING for ~900 production repos — the 30-repo dev server is 3% and proves nothing about scale; never put sync I/O on the event loop
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 3d216558-2b0c-4da6-83b0-c76caa9c86c9
  modified: 2026-08-12T19:17:06.688Z
---

**Production runs ~900 repositories with ONE operator.** Every design decision must be made against
that number. The user was explicit: *"you have to design EVERYTHING thinking about that scale!"*

**The local dev server holds ~30 repos -- 3% of production. NEVER extrapolate performance or safety
from it.** A local measurement can demonstrate CORRECTNESS; it demonstrates nothing about behaviour
at fleet scale. Work that looks instantaneous on 30 repos can freeze production for minutes.

**Hard consequences:**

- **Never call a synchronous filesystem/network function directly inside `async def`.** It blocks the
  WHOLE event loop -- the server answers nothing until it returns. Offload with
  `anyio.to_thread.run_sync(...)`, the idiom already established in `startup/lifespan.py`.
- **Any O(number-of-repos) work must be offloaded and paced**, especially on the startup path, where
  it delays readiness for the entire fleet's scan and a failure takes the node down at boot.
- **Treat `hard` NFS as able to block forever.** The cow-storage mount is `hard` NFSv3, so `os.stat`
  blocks in UNINTERRUPTIBLE kernel retry when the server is unresponsive -- it never times out. On
  the event loop that is a permanently hung node, not a slow one.
- **No settings, no manual steps** -- one operator cannot flip switches or sweep leftovers across 900
  repos. See [[feedback_no_settings_to_gate_bug_fixes]].
- **Cleanup/repair must self-heal and converge**; anything left behind accumulates forever.

**The failure that produced this rule (2026-08-12):** I shipped the Bug #1567/#1570 versioned-snapshot
sweep as a synchronous filesystem walk called directly inside `async def lifespan`. On the 30-repo dev
server it finished instantly and I reported it as verified. At ~18 filesystem ops per namespace x 900
repos that is ~16,000 NFS metadata ops -- roughly 80 seconds of frozen event loop on every boot, and
an indefinite hang if the NFS host is unresponsive. The CORRECT pattern already existed 480 lines
above in the same function. I did not catch it because my dataset hid it.

**How to apply:** before shipping anything that touches repos, ask "what does this do at 900?" for
BOTH runtime and blocking behaviour. In review, treat sync I/O inside `async def` as a defect no
matter how fast it looks locally. Recorded as a binding project invariant in CLAUDE.md
("Production Scale -- DESIGN EVERYTHING FOR IT") so it binds subagents too, not just me.

Related: [[project_own_local_dev_cidx_server]] (the dev server is mine to test on -- but it is small),
[[feedback_no_artificial_work_budgets]], [[project_nfs_host_down_hangs_systemd]].
