---
name: Zero failures means zero — no excuses
description: NEVER dismiss test failures as "pre-existing" — CLAUDE.md says zero failures, fix them all before marking complete
type: feedback
---

NEVER claim test failures are "pre-existing" or dismiss them as unrelated. CLAUDE.md is explicit: zero failures, period.

**Why:** User was furious when 41 temporal test failures were dismissed as "pre-existing." The failures were caused by a regression from Bug #469 commits (file_extensions parameter added to TemporalDiffScanner broke all temporal tests using mocked configs). Dismissing them wasted time and broke trust.

**How to apply:** When fast-automation.sh shows ANY failures, investigate and fix ALL of them. If tests broke because of your changes (even indirectly via a new parameter that mocked tests don't set), that's YOUR responsibility. The rule is simple: 0 failures = done, >0 failures = not done.
