---
name: end_trace
category: admin
required_permission: query_repos
tl_dr: End active Langfuse trace with optional scoring and feedback.
---

End active Langfuse trace with optional scoring and feedback. Safe to call without active trace (returns status="no_active_trace").

IDEAL TRACE LIFECYCLE:
  start_trace(name="Task Name", input="user prompt")
  -> [tool calls automatically logged as spans]
  -> end_trace(output="Claude response", score=0.8, summary="Task completed successfully")

FULL PROMPT OBSERVABILITY:
  Use 'output' parameter to capture Claude's complete response.
  Use 'summary' to provide human-readable outcome description.
  Use 'score' to quantify success (0.0 = failed, 1.0 = perfect).
  Use 'outcome' for structured categorization (e.g., "bug_found", "needs_more_work").
  Use 'intel' to update prompt quality metrics based on final results.
  Use 'tags' to add completion markers (e.g., ["completed", "verified"]).

NESTED TRACES: Ends only the most recent trace. Previous trace remains active. Call multiple times to unwind nested traces.

SCORING: 0.0 (failed) to 1.0 (fully successful). Optional but helps analyze research effectiveness.

EXAMPLE WITH FULL OBSERVABILITY:
  end_trace(
    output="I found the authentication bug in src/auth/login.py line 42. The session timeout is hardcoded to 5 minutes instead of using the configuration value. Fix: Replace the hardcoded value with config.session_timeout.",
    score=0.9,
    summary="Found root cause and provided fix",
    outcome="bug_found",
    tags=["completed", "verified"],
    intel={
      "frustration": 0.2,
      "quality": 0.9
    }
  )
