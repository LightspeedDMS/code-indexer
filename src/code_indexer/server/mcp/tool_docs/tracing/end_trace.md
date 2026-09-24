---
name: end_trace
category: tracing
required_permission: query_repos
requires_config: langfuse_enabled
tl_dr: End the active Langfuse trace with optional scoring and session outcome capture.
slim_description: "End the active Langfuse trace with optional score, summary, outcome, and metadata."
inputSchema:
  type: object
  properties:
    score:
      type: number
      description: 'Optional success score for the research session (0.0 to 1.0). 1.0 = fully successful (found answer/solution), 0.5 = partially successful (found leads), 0.0 = unsuccessful (no progress).'
      minimum: 0
      maximum: 1
    summary:
      type: string
      description: 'Optional human-readable summary of the research session outcome (e.g., "Found root cause in auth module"). Appears in Langfuse dashboard for context.'
    outcome:
      type: string
      description: 'Optional structured outcome description (e.g., "bug_found"). Useful for categorizing trace results and generating reports.'
    output:
      type: string
      description: 'Optional: Claude''s complete response to the user. Captures the full AI-generated output for prompt observability analysis in Langfuse.'
    tags:
      type: array
      items:
        type: string
      description: 'Optional list of additional tags to add at trace end. These are merged with any tags provided at start_trace.'
    intel:
      type: object
      description: 'Optional prompt intelligence metadata updates at trace end. Can update or add new intelligence metrics based on final results.'
      properties:
        frustration:
          type: number
          minimum: 0
          maximum: 1
          description: 'User frustration level (0.0-1.0). Can update if frustration changed during session. See "INTEL CODES" below.'
        specificity:
          type: string
          enum: [surg, const, outc, expl]
          description: 'Prompt type classification. See "INTEL CODES" below for code meanings.'
        task_type:
          type: string
          enum: [bug, feat, refac, research, test, docs, debug, conf, other]
          description: 'Task classification. See "INTEL CODES" below for code meanings.'
        quality:
          type: number
          minimum: 0
          maximum: 1
          description: 'Prompt quality score (0.0-1.0). Can be updated based on final assessment. See "INTEL CODES" below.'
        iteration:
          type: integer
          minimum: 1
          maximum: 9
          description: 'Task iteration count. See "INTEL CODES" below.'
  required: []
outputSchema:
  type: object
  properties:
    status:
      type: string
      enum:
      - ended
      - no_active_trace
      - disabled
      - error
      description: 'End status: "ended" if trace was active and ended successfully, "no_active_trace" if no trace was running, "disabled" if Langfuse not configured, "error" if ending the trace failed (e.g. no session context, internal error)'
    trace_id:
      type: string
      description: Unique identifier of the trace that was ended (only present when status is "ended")
    message:
      type: string
      description: Human-readable status message
  required:
  - status
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

INTEL CODES: same `specificity`, `task_type`, `frustration`, `quality`, and `iteration` semantics as `start_trace` -- see its "INTEL CODES" section for the full code-to-meaning legend and numeric anchors.

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
