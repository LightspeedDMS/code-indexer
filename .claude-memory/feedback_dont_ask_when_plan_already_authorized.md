---
name: feedback-dont-ask-when-plan-already-authorized
description: "Don't stop with a checkpoint question when the user already gave explicit multi-step instructions earlier in the same session — just execute the whole plan agentically"
metadata:
  type: feedback
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

When the user has already given explicit, detailed instructions for a multi-step plan (e.g. "run locally, AND run in staging, both solo and cluster"), do not pause at each major milestone to ask "how would you like to proceed?" — that authorization was already given. Treat the original instruction as standing authorization for the whole sequence and execute it end-to-end without re-confirming at each stage.

**Why**: After completing local validation of a multi-environment test plan the user had already fully specified earlier in the session, I stopped and asked whether to continue to staging, skip the cluster, or stop entirely. The user's reply was pointed: "yes, continue to staging, and don't ask again. work agentically. you lost 8 hours just waiting for me to answer the obvious." The plan was already unambiguous from the earlier instruction — pausing here cost real wall-clock time for no decision-quality benefit.

**How to apply**:
- If a multi-phase plan was explicitly laid out by the user earlier in the conversation (even several turns back), treat every phase as pre-authorized. Do not re-ask before starting the next phase.
- Reserve AskUserQuestion checkpoints for genuine forks the user has NOT already resolved — e.g. a newly-discovered ambiguity, a destructive action outside the original scope, or a real tradeoff the user hasn't weighed in on yet.
- A natural "checkpoint" in the work (finishing one environment before moving to the next) is not on its own a reason to stop and ask — only stop if there's an actual open question.
- This reinforces [[feedback_no_unnecessary_questions]] and [[feedback_implement_story_agentic_no_stops]] specifically for long-running, multi-environment validation work.
