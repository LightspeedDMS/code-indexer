---
name: start_trace
category: admin
required_permission: query_repos
tl_dr: Start a Langfuse trace for a research session.
---

Start a Langfuse trace for a research session. When active, all subsequent MCP tool calls are automatically logged as spans with timing, inputs, and outputs.

IDEAL TRACE LIFECYCLE:
  start_trace(name="Task Name", input="user prompt")
  -> [tool calls automatically logged as spans]
  -> end_trace(output="Claude response", score=0.8)

FULL PROMPT OBSERVABILITY:
  Use 'input' parameter to capture the user's original prompt/request.
  Use 'intel' object to add prompt quality metadata for analysis.
  Use 'tags' to categorize traces for easier filtering in Langfuse.
  At trace end, use 'output' to capture Claude's complete response.

NESTED TRACES: Starting a new trace while one is active creates a stack. end_trace() ends only the most recent.

LANGFUSE DISABLED: If Langfuse not configured, returns status="disabled" and continues without error. No tracing overhead.

EXAMPLE WITH FULL OBSERVABILITY:
  start_trace(
    name="Authentication Bug Fix",
    input="Fix the login timeout issue in auth module",
    tags=["bugfix", "authentication", "high-priority"],
    intel={
      "frustration": 0.6,
      "specificity": "surg",
      "task_type": "bug",
      "quality": 0.8,
      "iteration": 2
    }
  )
