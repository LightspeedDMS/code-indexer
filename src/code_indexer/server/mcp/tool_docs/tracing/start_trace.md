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

Start a Langfuse trace for a research session. When a trace is active, all subsequent MCP tool calls are automatically logged as spans with timing, inputs, and outputs. This enables deep observability into research workflows, performance analysis, and debugging.

WHEN TO USE: (1) Beginning a focused research session on a specific topic, (2) Investigating a bug or issue that requires multiple tool calls, (3) Analyzing research workflow performance, (4) Creating audit trails for important investigations.

TRACE LIFECYCLE:
1. start_trace(topic="authentication") - Starts trace, returns trace_id
2. [All tool calls automatically logged as spans under this trace]
3. end_trace(score=0.8, outcome="success") - Ends trace with scoring

NESTED TRACES: You can start multiple traces in the same session. Traces form a stack - the most recent trace is active. When you end a trace, the previous trace becomes active again. This supports hierarchical research workflows (e.g., main investigation with focused sub-investigations).

AUTOMATIC SPAN CREATION: While a trace is active, every tool call creates a span automatically:
- Span name: Tool name (e.g., "search_code", "get_file_content")
- Span input: Tool arguments (with sensitive fields like password/token removed)
- Span output: Tool results (large result lists are summarized)
- Span timing: Execution latency captured automatically
- Span errors: Exceptions captured with error details

LANGFUSE DISABLED: If Langfuse is not configured in server settings, start_trace returns status="disabled" and continues without error. Tool execution proceeds normally without tracing overhead.

EXAMPLE WORKFLOW:
```
# Start research session
start_trace(topic="OAuth implementation")
-> {"status": "active", "trace_id": "trace-abc123"}

# Research tools (automatically traced)
search_code(query="OAuth client", repository_alias="backend-global")
get_file_content(repository_alias="backend-global", file_path="auth/oauth.py")
search_code(query="token validation", repository_alias="backend-global")

# End with scoring
end_trace(score=0.9, outcome="found implementation, well-structured")
-> {"status": "ended", "trace_id": "trace-abc123"}
```

RELATED TOOLS: end_trace (complete trace with scoring), search_code (code search with automatic span logging), get_file_content (file reading with automatic span logging).
