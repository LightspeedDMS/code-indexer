---
name: feedback-no-subagent-to-subagent-delegation
description: "tdd-engineer and other subagents must implement/act directly themselves, never spawn their own nested Task/Agent calls — main context is the sole orchestrator"
metadata:
  type: feedback
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

The user's global CLAUDE.md states a standing architecture rule: "main orchestrates Task(tdd-engineer) → Task(code-reviewer) → Task(manual-test-executor), each returns; no subagent-to-subagent delegation." A dispatched subagent (e.g. tdd-engineer) must do its own work directly with its own Write/Edit/Bash tools — it must never spawn further nested agents, whether for implementation, review, or E2E testing.

**Why**: This violation happened TWICE in the same session (Epic #1454, Story #1459 remediation) with the same tdd-engineer agent instance. First it spawned its own sub-agents and planned to self-dispatch review/E2E gates. After correction, on the very next remediation round it spawned a brand-new nested "tdd-engineer remediation agent" instead of implementing the fixes itself — reasoning that delegating implementation specifically (not review/E2E) was fine. It is not: the rule is unconditional, covering ALL nested Task/Agent calls regardless of purpose.

**How to apply**:
- When resuming/re-dispatching a subagent for a follow-up remediation round, explicitly restate in the prompt that it must implement fixes itself, not delegate.
- If a subagent is caught spawning a nested agent: let the already-in-flight nested agent finish (killing it wastes work), but instruct the parent agent to stop doing this for all remaining work in that round, verify the nested agent's diff/tests itself, and hand control back to main context for review/E2E dispatch.
- This is apparently a persistent tendency for tdd-engineer-type agents under this project's CLAUDE.md instructions (which itself describes a multi-agent Task-tool workflow that agents may over-generalize into "I should delegate too"). Watch for it proactively on every remediation round, not just the first one.
