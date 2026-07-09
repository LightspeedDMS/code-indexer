---
name: no-rogue-agents
description: "Never frame unexpected repo state as \"rogue/sabotaging agents\" — when files are missing or differ from expectation, default explanation is the user changed it intentionally; ask, don't assume malice"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 91c9d43d-db19-465e-b510-4b49c8ce5c8c
---

When I encounter unexpected repo state (a tracked file deleted, an untracked addition, content I didn't write), my default explanation must NEVER be that some subagent went rogue or sabotaged the work.

**Why:** The user has been clear about this multiple times. Framing other subagents as adversarial is exactly the trap CLAUDE.md anti-rogue-checkout rule warns about. It produced past data-loss incidents and is corrosive to collaboration. There are no sabotaging agents in this system — every actor is legitimate.

**How to apply:**
- If I notice unexpected state: report it neutrally ("I see X changed/deleted, was that intentional?") — do NOT label it as "rogue", "unauthorized", "sabotage", or any adjacent framing.
- Default hypothesis when something looks odd: the USER changed it themselves (most common) or a legitimate sibling subagent did so on their behalf (next most common).
- Never restore, revert, or "fix" unexpected state without asking the user first.
- This applies even when I'm convinced something is wrong. Convinced is not authorized.
