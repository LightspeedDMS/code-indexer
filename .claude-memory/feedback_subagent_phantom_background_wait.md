---
name: feedback-subagent-phantom-background-wait
description: "Dispatched subagents sometimes end their turn believing an async mechanism (their own background shell job, or even a Monitor task) will notify THEM -- it never does, only the orchestrator receives those signals; also covers a confirmed real case of a tdd-engineer nested-delegating to another tdd-engineer"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-17T16:42:49.726Z
---

Dispatched subagents (via the `Agent` tool) repeatedly end their turn believing some
asynchronous mechanism will wake them up and let them continue — six separate times in one
session (2026-08-16/17), across different agents and different unrelated fixes. Two
variants observed:

1. **Own background job**: launches its own `nohup pytest ... &` and says "I'll wait for
   the monitor's completion notification" or "the agent has resumed in the background and
   will notify me."
2. **Monitor tool**: legitimately arms a real `Monitor` task (a tool it does have access
   to), then says "I'll wait for their completion notifications" — but Monitor
   notifications are delivered to the ORCHESTRATOR, never to the subagent that armed them.

**Why this is wrong**: the harness's completion-notification mechanism (for the agent
itself) fires when *that agent* has zero live background children — it is not a
notification service for arbitrary jobs or Monitor tasks the agent itself started. Both
variants leave the agent sitting stopped forever with unread output, believing work
remains when nothing will ever advance it. Functionally identical to (but a different root
cause from) `feedback_agent_stall_detection_needs_reply_not_just_mtime` and
`feedback_verify_codex_actually_ran` — all are cases of trusting a notification that
structurally cannot arrive at that recipient.

**How to apply**: when a dispatched subagent's completion `<result>` text says anything
like "I'll wait for X to notify me," treat it as a stalled/confused turn, not real
completion — even though the harness reports `status: completed`. Resume it (`SendMessage`)
with an explicit instruction: no notification is coming to YOU; stop/ignore any Monitor
tasks you armed, find and read the actual log file directly, or re-run synchronously in
the foreground, then report real results. Escalate the explicitness each time it recurs.

**Separate but related confirmed incident**: one resumed agent's honest reply to a direct
question revealed a REAL violation, not just sloppy language — a dispatched `tdd-engineer`
had itself called the `Agent` tool to spawn ANOTHER nested `tdd-engineer` to do the actual
implementation, then relayed that nested agent's summary as its own, and was the one that
ended up phantom-waiting on the nested agent's own background test run. This is exactly
what `feedback_no_subagent_to_subagent_delegation.md` prohibits. When a subagent's language
distances itself from the work ("the engineer," "the background tdd-engineer," "before
dispatching") — as opposed to first-person description of hook feedback like "the Senior
Coding Nanny blocked me" — ask directly: "did you call the Agent or Task tool?" Two prior
similar-sounding cases this session ("the code-reviewer subagent caught this") turned out
to be sloppy mislabeling of the pace-maker hook, not real delegation — so verify by asking,
don't assume either way from the phrasing alone. When a real violation is confirmed, do not
assume the underlying work is wrong just because the process was — have the offending agent
(or yourself) independently re-verify the actual diff/tests directly rather than trusting
the nested agent's summary a second time.

Related: [[feedback_agent_stall_detection_needs_reply_not_just_mtime]],
[[feedback_active_monitoring_check_back]], [[feedback_no_subagent_to_subagent_delegation]].
