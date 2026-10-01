---
name: feedback-work-agentically-to-staging-no-questions
description: "During an active bug-fixing mission, do not stop to ask process/workflow questions (commit timing, review sequencing, etc.) -- the goal is the full local package through staging; only stop for something truly critical"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-17T13:28:18.786Z
---

Mid-mission (driving GitHub issues on code-indexer to zero, 2026-08-17), I asked the user
a process question about commit timing (batch everything vs. commit reviewed fixes
incrementally) using AskUserQuestion. The user's reaction was sharp and immediate: "stop
asking stupid questions and losing time... only when something is really critical and
needs my answer, ask a question, otherwise, just work. you have a goal. all the way to
staging. respect the goal, stop asking stupid questions."

**Why:** the user has already set the goal (drive to zero open issues, "all the way to
staging") and expects autonomous execution within that goal. A commit-sequencing question
was not a genuine blocker -- it was a judgment call I could and should have made myself
(default to committing reviewed work incrementally, since that's standard hygiene and the
user had already endorsed dual-review-before-trust as the bar). Asking it interrupted
momentum and read as indecision on something within my own authority to decide.

**How to apply:** during an active, already-scoped mission with a stated end goal, do NOT
use AskUserQuestion for: commit/staging timing, review sequencing, which order to tackle
sub-tasks, whether to fix an incidentally-found bug now or later (the fix-everything-found
rule already answers that), or any other decision where a sensible default exists and the
downside of guessing wrong is low/reversible (uncommitted local work, a re-orderable task
list). RESERVE AskUserQuestion for irreversible or destructive actions (force-push,
dropping data, deleting branches) or genuine architectural forks with no clear default
and real cost on both sides (the #1575 fast-path abandon-vs-fix decision earlier in this
same session was a legitimate use — 6 rounds of real bugs, real tradeoff, user's call).
The line is reversibility + whether a default answer is obvious, not "is this a decision."

Related: [[feedback_no_unnecessary_questions]], [[feedback_no_confirmation_on_commands]],
[[feedback_implement_story_agentic_no_stops]] — this is the same standing preference,
now confirmed to extend to process/workflow questions during an active mission, not just
to "should I proceed with the obvious next step" questions.
