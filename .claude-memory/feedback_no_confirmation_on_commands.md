---
name: feedback_no_confirmation_on_commands
description: NEVER ask for confirmation when user gives a direct command like /implement-story-spec - just execute immediately
type: feedback
---

When user issues a direct command (slash commands, explicit instructions), execute immediately without asking "do you want to proceed?" or "should I start?". The command IS the approval.

**Why:** User was frustrated when asked "Want me to proceed?" after issuing /implement-story-spec #685. Direct commands are not proposals — they are instructions to execute.

**How to apply:** If the user types a command or gives explicit instructions, start working immediately. Only ask for clarification if the instructions are genuinely ambiguous or missing critical information.
