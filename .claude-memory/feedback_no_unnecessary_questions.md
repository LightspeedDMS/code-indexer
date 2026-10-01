---
name: No unnecessary questions
description: Never stop to ask permission for obvious next steps - commit, continue, proceed without asking
type: feedback
originSessionId: 1540f2bd-70b0-43d2-8f44-6cbc3b68cc10
modified: 2026-09-26T20:43:18.092Z
---
NEVER stop to ask the user obvious questions like "should I commit and continue?" during epic implementation. Just do it.

**Why:** User explicitly said "you drive me crazy when you stop for idiotic questions." Stopping for obvious confirmations breaks flow and wastes time.

**How to apply:** During epic/story implementation, commit completed work and proceed to the next story automatically. Only stop if genuinely blocked (missing credentials, ambiguous requirements, destructive operations on shared state like pushing to master). Commit to development, close issues, and move to next story without pausing.

Also: when one of MY OWN changes has an implementation side effect, fix it by restoring the previous behaviour; don't escalate it as a jargon-heavy A/B choice. Only bring a decision to the owner when it changes what the PRODUCT does, and then explain it in plain language.
