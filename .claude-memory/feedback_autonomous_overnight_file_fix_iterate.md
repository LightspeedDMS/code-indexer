---
name: feedback_autonomous_overnight_file_fix_iterate
description: "Work autonomously without asking; every defect found must be filed, fixed, and iterated to clean — never defer, never sandbag"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ffe9e7f2-e6bc-4fbe-b9f5-45e1f7b8661d
---

When the user hands off a body of work (especially "I'm going to bed / work all night"), operate FULLY AUTONOMOUSLY: do not ask scoping/prioritization questions, do not defer findings, do not sandbag. For EVERY defect discovered: file the bug issue, fix it through the full workflow (tdd-engineer -> code-reviewer -> gates -> deploy -> manual validation), and ITERATE until the bar is met.

**The bar the user wants to wake up to:** a solid, complete package of work — properly working, with CLEAN server logs (local AND staging), thorough and verified. Clean logs is the acceptance criterion, not "tests pass."

**Why:** The user was emphatic ("don't sandbag, don't be fucking lazy ... that's your job") after I asked which of several confirmed/candidate defects to fix. Asking him to triage findings is exactly the laziness he rejects. If evidence points to a defect, it is in scope by default — fix it, don't ask whether to.

**How to apply:**
- Do not use AskUserQuestion to triage defects or prioritize fixes. Decide and execute.
- Each defect: file -> fix -> review -> gate (fast-automation / server-fast as applicable) -> version bump -> push dev -> merge staging -> deploy -> validate on the real environment -> close the issue.
- After all fixes, re-audit local AND staging logs; if anything is still dirty, iterate. Repeat until logs are clean.
- Only stop for a genuine hard blocker (e.g. production-push authorization, which still requires explicit per-push approval) or destructive/irreversible outward actions.
- Keep momentum overnight; never sit idle waiting — actively monitor long-running work.

Related: [[feedback_review_local_and_staging_logs_after_testing]], [[feedback_no_unnecessary_questions]], [[feedback_no_confirmation_on_commands]], [[feedback_implement_story_agentic_no_stops]].
