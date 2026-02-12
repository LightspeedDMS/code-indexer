---
name: end_trace
category: tracing
required_permission: query_repos
requires_config: langfuse_enabled
tl_dr: End the active Langfuse trace with optional scoring and feedback - captures research session outcome.
inputSchema:
  type: object
  properties:
    score:
      type: number
      description: 'Optional success score for the research session (0.0 to 1.0). Use to quantify research effectiveness: 1.0 = fully successful (found answer/solution), 0.5 = partially successful (found leads), 0.0 = unsuccessful (no progress). Helps analyze which research strategies work best.'
      minimum: 0
      maximum: 1
    summary:
      type: string
      description: 'Optional human-readable summary of the research session outcome. Examples: "Found root cause in auth module", "Need to investigate caching layer next", "Dead end - API not used in this codebase". Appears in Langfuse dashboard for context.'
    outcome:
      type: string
      description: 'Optional structured outcome description. Examples: "bug_found", "implementation_complete", "needs_more_investigation", "blocked". Useful for categorizing trace results and generating reports.'
    output:
      type: string
      description: 'Optional: Claude''s complete response to the user. Captures the full AI-generated output for prompt observability analysis in Langfuse.'
    tags:
      type: array
      items:
        type: string
      description: 'Optional list of additional tags to add at trace end (e.g., ["completed", "verified"]). These are merged with any tags provided at start_trace.'
    intel:
      type: object
      description: 'Optional prompt intelligence metadata updates at trace end. Can update or add new intelligence metrics based on final results.'
      properties:
        frustration:
          type: number
          minimum: 0
          maximum: 1
          description: 'User frustration level: 0.0 = calm/satisfied, 1.0 = very frustrated. Can update if frustration changed during session.'
        specificity:
          type: string
          enum: [surg, const, outc, expl]
          description: 'Prompt type: surg=surgical (specific fix), const=constructive (building), outc=outcome-focused (goal-oriented), expl=exploratory (discovery).'
        task_type:
          type: string
          enum: [bug, feat, refac, research, test, docs, debug, conf, other]
          description: 'Task classification: bug=bug fix, feat=new feature, refac=refactoring, research=investigation, test=testing, docs=documentation, debug=debugging, conf=configuration, other=miscellaneous.'
        quality:
          type: number
          minimum: 0
          maximum: 1
          description: 'Prompt quality score: 0.0 = poor/vague, 1.0 = excellent/clear. Can be updated based on final assessment.'
        iteration:
          type: integer
          minimum: 1
          maximum: 9
          description: 'Task iteration count: How many attempts at this task (1 = first attempt, 2+ = retry/refinement).'
  required: []
outputSchema:
  type: object
  properties:
    status:
      type: string
      description: 'End status: "ended" if trace was active and ended successfully, "no_active_trace" if no trace was running, "disabled" if Langfuse not configured'
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
