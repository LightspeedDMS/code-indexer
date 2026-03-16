---
name: execute_open_delegation
category: admin
required_permission: delegate_open
tl_dr: Submit any free-form coding objective to Claude Server with engine and mode selection.
inputSchema:
  type: object
  properties:
    prompt:
      type: string
      description: Free-form coding objective or task description for the delegated agent
    repositories:
      type: array
      items:
        type: string
      description: List of repository aliases the agent should have access to
    engine:
      type: string
      description: Agent engine to use (claude-code, codex, gemini, opencode, q). Defaults to claude-code.
      enum:
      - claude-code
      - codex
      - gemini
      - opencode
      - q
    mode:
      type: string
      description: "Execution mode (single, collaborative, competitive). Defaults to single. Note: collaborative and competitive are not yet supported."
      enum:
      - single
      - collaborative
      - competitive
    model:
      type: string
      description: Optional model override for the agent engine (e.g., claude-opus-4-5)
    timeout:
      type: integer
      description: Optional job timeout in seconds (default 5400 / 90 minutes)
  required:
  - prompt
  - repositories
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: True if job was created and started
    job_id:
      type: string
      description: ID of the created job for async polling via poll_delegation_job
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
---

Submit any free-form coding objective to Claude Server with engine and mode selection. Creates an async job that can be polled for results via poll_delegation_job.

VALUE PROPOSITION: This tool enables power users and admins to leverage Claude Server's full agent capabilities for any coding objective. Unlike execute_delegation_function which uses pre-defined function templates, this tool accepts any free-form prompt and allows selecting the agent engine and execution mode.

WHEN TO USE: Use this tool when you need to submit a custom coding task that does not fit a pre-defined delegation function template, or when you need to select a specific agent engine (codex, gemini, etc.) for the task.

REQUIRED PERMISSION: delegate_open (power_user or admin role)

SUPPORTED ENGINES:
- claude-code: Claude Code agent (default)
- codex: OpenAI Codex agent
- gemini: Google Gemini agent
- opencode: OpenCode agent
- q: Amazon Q agent

SUPPORTED MODES:
- single: Single agent executes the task (default, currently the only supported mode)
- collaborative: Multi-agent collaboration (not yet supported by Claude Server)
- competitive: Competing agents with best-result selection (not yet supported by Claude Server)

EXECUTION FLOW:
1. Validates delegate_open permission (power_user or admin required)
2. Validates required parameters (prompt, repositories)
3. Validates engine and mode values
4. Checks repository readiness on Claude Server (polls if cloning)
5. Returns error if mode is collaborative or competitive (not yet supported)
6. Creates job via POST /jobs with engine/model/timeout options
7. Registers callback URL if configured
8. Starts job and registers in job tracker
9. Returns job_id for async polling via poll_delegation_job

REPOSITORY READINESS:
Repositories are checked for registration and clone status on Claude Server before job creation. If a repository is not yet registered, it will be registered automatically. The tool waits for cloneStatus="completed" before proceeding. If a repository fails to become ready within the timeout, an error is returned.

GUARDRAILS:
When guardrails are enabled in the server configuration, safety guardrails are automatically prepended to your prompt before it is sent to Claude Server. This includes rules about filesystem safety, process safety, git safety, system safety, package authorization, and secrets handling. The guardrails are loaded from a configured golden repo or use a default template. This happens transparently -- you do not need to include safety instructions in your prompt.

ERRORS:
- 'Access denied' -> User does not have delegate_open permission (requires power_user or admin)
- 'Missing required parameter: prompt' -> prompt is required
- 'Missing required parameter: repositories' -> repositories list is required and cannot be empty
- 'Invalid engine' -> Engine not in supported list
- 'Invalid mode' -> Mode not in supported list
- 'Mode not yet supported by Claude Server' -> collaborative or competitive mode requested
- 'Repository failed to become ready' -> Repository clone timed out or failed
- 'Claude Delegation not configured' -> Delegation configuration not set up
- 'Claude Server error' -> Communication error with Claude Server
