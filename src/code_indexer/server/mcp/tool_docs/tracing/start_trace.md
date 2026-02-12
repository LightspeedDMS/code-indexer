---
name: start_trace
category: tracing
required_permission: query_repos
tl_dr: Start a Langfuse trace for research session - enables automatic span logging for all subsequent tool calls.
inputSchema:
  type: object
  properties:
    topic:
      type: string
      description: Research topic or goal for this trace (e.g., "authentication flow", "performance optimization", "bug investigation"). This helps organize traces in Langfuse dashboard and provides context for the research session.
    strategy:
      type: string
      description: 'Optional research strategy or approach (e.g., "depth-first exploration", "comparative analysis", "root cause investigation"). Stored as trace metadata for later analysis.'
    metadata:
      type: object
      description: 'Optional additional metadata as key-value pairs. Examples: {"priority": "high", "project": "backend-refactor", "ticket": "JIRA-123"}. Metadata appears in Langfuse dashboard for filtering and analysis.'
  required:
  - topic
outputSchema:
  type: object
  properties:
    status:
      type: string
      description: 'Trace status: "active" if trace started successfully, "disabled" if Langfuse is not configured'
    trace_id:
      type: string
      description: Unique identifier for the started trace (only present when status is "active")
    message:
      type: string
      description: Human-readable status message
  required:
  - status
---

Start a Langfuse trace for a research session. When active, all subsequent MCP tool calls are automatically logged as spans with timing, inputs, and outputs.

TRACE LIFECYCLE: start_trace(topic) -> [all tool calls logged as spans] -> end_trace(score, outcome)

NESTED TRACES: Starting a new trace while one is active creates a stack. end_trace() ends only the most recent.

LANGFUSE DISABLED: If Langfuse not configured, returns status="disabled" and continues without error. No tracing overhead.
