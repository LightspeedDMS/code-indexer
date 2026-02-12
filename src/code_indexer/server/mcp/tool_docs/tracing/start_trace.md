---
name: start_trace
category: tracing
required_permission: query_repos
requires_config: langfuse_enabled
tl_dr: Start a Langfuse trace for research session - enables automatic span logging for all subsequent tool calls.
inputSchema:
  type: object
  properties:
    name:
      type: string
      description: Trace name describing the task or investigation (e.g., "Authentication Bug Investigation", "Performance Optimization", "Feature Implementation"). This appears as the trace title in Langfuse dashboard.
    input:
      type: string
      description: 'Optional: The user prompt or request that initiated this task. Captures the original question/instruction for full prompt observability in Langfuse.'
    strategy:
      type: string
      description: 'Optional research strategy or approach (e.g., "depth-first exploration", "comparative analysis", "root cause investigation"). Stored as trace metadata for later analysis.'
    metadata:
      type: object
      description: 'Optional additional metadata as key-value pairs. Examples: {"priority": "high", "project": "backend-refactor", "ticket": "JIRA-123"}. Metadata appears in Langfuse dashboard for filtering and analysis.'
    tags:
      type: array
      items:
        type: string
      description: 'Optional list of tags for categorizing the trace (e.g., ["bugfix", "high-priority", "authentication"]). Tags enable filtering and organization in Langfuse dashboard.'
    intel:
      type: object
      description: 'Optional prompt intelligence metadata for quality analysis. Provides insights into the nature and quality of the user request.'
      properties:
        frustration:
          type: number
          minimum: 0
          maximum: 1
          description: 'User frustration level: 0.0 = calm/satisfied, 1.0 = very frustrated. Helps identify problematic interactions.'
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
          description: 'Prompt quality score: 0.0 = poor/vague, 1.0 = excellent/clear. Indicates how well-structured the user request is.'
        iteration:
          type: integer
          minimum: 1
          maximum: 9
          description: 'Task iteration count: How many attempts at this task (1 = first attempt, 2+ = retry/refinement).'
  required:
  - name
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
