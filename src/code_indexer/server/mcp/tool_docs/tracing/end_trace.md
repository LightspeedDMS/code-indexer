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

End the active Langfuse trace for the current research session. Captures the research outcome with optional scoring and feedback. If multiple nested traces are active (trace stack), ends only the most recent trace and makes the previous trace active again.

WHEN TO USE: (1) Completed research session on a topic, (2) Switching to a different research focus, (3) Session interrupted or blocked, (4) Want to capture research effectiveness metrics.

SCORING GUIDELINES:
- 1.0: Fully successful - achieved research goal completely
- 0.9: Very successful - found comprehensive answer with minor gaps
- 0.8: Successful - found main answer but missing some details
- 0.7: Mostly successful - found significant information
- 0.6: Partially successful - found some useful information
- 0.5: Mixed results - found leads but no definitive answers
- 0.4: Limited success - minimal useful information
- 0.3: Mostly unsuccessful - very little progress
- 0.2: Unsuccessful - no significant progress
- 0.1: Completely unsuccessful - wasted effort
- 0.0: Failed - no progress or wrong direction

NESTED TRACE BEHAVIOR: If you have nested traces (e.g., started a sub-investigation within a main research session), end_trace ends only the most recent trace. The previous trace remains active and continues logging spans. Call end_trace multiple times to end nested traces.

NO ACTIVE TRACE: If no trace is running, end_trace returns status="no_active_trace" without error. This is safe to call even if you're unsure whether a trace is active.

EXAMPLE USAGE:
```
# Simple end without scoring
end_trace()
-> {"status": "ended", "trace_id": "trace-abc123"}

# End with success scoring
end_trace(score=0.9, feedback="Found OAuth implementation, well-documented")
-> {"status": "ended", "trace_id": "trace-abc123"}

# End with failure scoring
end_trace(score=0.2, feedback="Dead end - feature not implemented yet", outcome="blocked")
-> {"status": "ended", "trace_id": "trace-abc123"}

# No trace running
end_trace()
-> {"status": "no_active_trace", "message": "No active trace to end"}
```

TRACE STACK EXAMPLE (Nested traces):
```
# Start main research
start_trace(topic="authentication system")

# Start focused sub-investigation
start_trace(topic="OAuth token validation")
# ... research OAuth ...
end_trace(score=0.8)  # Ends OAuth trace, auth trace still active

# Continue main research
# ... more research ...
end_trace(score=0.9)  # Ends auth trace
```

LANGFUSE DASHBOARD: After ending a trace, view it in the Langfuse dashboard:
- Trace timeline with all spans (tool calls)
- Individual span details (inputs, outputs, timing)
- Trace score and feedback
- Metadata for filtering and analysis

RELATED TOOLS: start_trace (begin trace), search_code (auto-logged as span), get_file_content (auto-logged as span).
