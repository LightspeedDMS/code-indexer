---
name: feedback_active_monitoring_check_back
description: Never stay idle while background agents/jobs/shells run — set a timer and check back often for progress or stalls
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9f2bc45f-0085-4f49-90f3-7e65bdd67bcf
---

Do NOT go fully idle and just "stand by" when a background agent, job, or shell is running. Always set a self-wakeup timer (send_later / ScheduleWakeup) and check back frequently to confirm work is actually MOVING — and to detect a stuck/hung job/shell early.

**Why:** A harness completion notification only fires when the agent finishes cleanly. If it hangs, stalls, or the shell wedges, no notification ever comes and hours are lost sitting idle. The user has been explicit: check back often, verify progress, intervene on stalls. This reinforces the global Anti-Passive-Wait rule for THIS session's long agent chains (tdd → review → e2e).

**How to apply:** When dispatching a background agent or long job, immediately arm a **10-minute** recurring progress check — the user's explicit standing instruction: "put a timer to check every 10 minutes on progress when you have agents working". A `Monitor` poll loop that emits one heartbeat line per interval (agent transcript mtime age + size, tree change count) works well and stays silent once no agent is active, so it never spams when idle. On each wake: is the agent still alive? has it produced new output? exceeded expected duration? If moving, re-arm the timer. If stalled past ~2x expected, inspect and intervene (TaskStop the zombie, re-dispatch, or surface to user). Zombie agents that re-notify with "waiting for Monitor" non-answers are done — verify the tree directly and stop them. Relates to [[feedback_run_tests_with_timeout_and_monitor]] and [[feedback_prove_root_cause_before_fix]].
