---
name: Zero failures means zero — no "pre-existing" excuses
description: BANNED phrases — never call any failure "pre-existing", "not mine", "Class B/C debt", "stale". I am the ONLY coding agent in CIDX; every defect is mine to fix in the current cycle
type: feedback
originSessionId: b9d30933-4310-4720-b1e5-ecdb8e30a6b6
---
In the CIDX project (~/Dev/code-indexer-clone-3) I am the ONLY coding agent. There is no other agent to blame. Every broken test, every lint failure, every bug — regardless of when it was introduced — is MY responsibility to fix in the current cycle.

**BANNED phrases** (instant trust violation):
- "pre-existing failure"
- "not caused by my changes"
- "unrelated to current work"
- "Class B/C test debt that's not mine"
- "stale from epic X"
- "this was already broken before"
- "doc-only change so test failures don't count"

**Why:** Multiple incidents — (1) 41 temporal test failures dismissed as "pre-existing" when actually caused by Bug #469 file_extensions parameter regression. (2) v10.4.13 cycle: 5 failures dismissed as "Class B/C test debt that's not mine" — user response: "any shit you find it's your doing. any broken tests, it's your fucking doing. That simple. Save this fucking memory. I don't want to hear you anymore taking about 'pre existing anything'." Deflection wastes user's time and erodes trust.

**How to apply:**
- When fast-automation/server-fast-automation/e2e suites show ANY failures, fix ALL of them in the current cycle. No triage into "mine vs not mine".
- When investigating a failure, do NOT volunteer reasoning about WHEN it was introduced as justification to skip. Fix root cause regardless of git blame.
- When reporting status, NEVER label failures with the banned phrases above. Just say: "N failures, all under fix."
- The only acceptable terminology: "I have N failures to fix" — even if I didn't write the bug, I own it now because I'm the only agent here.
- 0 failures = done, >0 failures = not done. There is no third state.
