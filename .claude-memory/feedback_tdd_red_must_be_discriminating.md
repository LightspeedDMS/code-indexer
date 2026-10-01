---
name: feedback_tdd_red_must_be_discriminating
description: A TDD test must FAIL on the naive/buggy implementation — a test that passes on both correct and broken code is worthless and lets regressions through
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
---

Every TDD test must exercise the DISCRIMINATING case — the specific input that a naive or partially-correct implementation gets WRONG. A test whose inputs pass on both the correct and the buggy code proves nothing and lets regressions ship.

**Why:** During #1486/#1488 I introduced a pagination regression (scroll fast-path sliced candidates by `limit` BEFORE applying the full filter → empty non-terminal page → callers drop later matches). It shipped because the agent's own "RED" test used candidates that ALL matched the filter, so it was green on both the correct and the broken implementation. The user: "if you keep introducing so many bugs, it shows your tdd discipline is sub standard." Correct.

**How to apply:**
- The RED step must genuinely FAIL against the pre-fix code — verify the failure is real, not a test that happens to pass.
- Tests must hit the BOUNDARY / mixed case, not the happy uniform case: for a filter+pagination fix use MIXED match/non-match candidates where a non-match lands inside the first `limit` window (so slice-before-filter yields an empty page); for validation use the exact malformed shape; for concurrency the actual overlap.
- When directing a subagent, name the discriminating input explicitly (e.g. "p000 non-match, p001 match, limit=1 → page1 must return p001, not empty") — don't let it pick uniform-passing fixtures.
- A refactor/optimization must keep the SAME behavior on the adversarial input, not just the happy one. Relates to [[feedback_zero_failures_no_excuses]] and [[feedback_epic_fix_all_bugs_found]].
