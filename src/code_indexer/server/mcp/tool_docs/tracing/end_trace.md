---
name: end_trace
category: tracing
required_permission: query_repos
tl_dr: End the active Langfuse trace with optional scoring and feedback - captures research session outcome.
inputSchema:
  type: object
  properties:
    score:
      type: number
      description: 'Optional success score for the research session (0.0 to 1.0). Use to quantify research effectiveness: 1.0 = fully successful (found answer/solution), 0.5 = partially successful (found leads), 0.0 = unsuccessful (no progress). Helps analyze which research strategies work best.'
      minimum: 0
      maximum: 1
    feedback:
      type: string
      description: 'Optional human-readable feedback about the research session. Examples: "Found root cause in auth module", "Need to investigate caching layer next", "Dead end - API not used in this codebase". Appears in Langfuse dashboard for context.'
    outcome:
      type: string
      description: 'Optional structured outcome description. Examples: "bug_found", "implementation_complete", "needs_more_investigation", "blocked". Useful for categorizing trace results and generating reports.'
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

NESTED TRACES: Ends only the most recent trace. Previous trace remains active. Call multiple times to unwind nested traces.

SCORING: 0.0 (failed) to 1.0 (fully successful). Optional but helps analyze research effectiveness.

EXAMPLE: end_trace(score=0.8, feedback='Found auth implementation', outcome='bug_found')
