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
      description: Agent engine to use for single mode (claude-code, codex, gemini, opencode, q). Defaults to claude-code.
      enum:
      - claude-code
      - codex
      - gemini
      - opencode
      - q
    mode:
      type: string
      description: "Execution mode: single (one agent), collaborative (DAG-based multi-step), competitive (parallel competing agents). Defaults to single."
      enum:
      - single
      - collaborative
      - competitive
    model:
      type: string
      description: Optional model override for the agent engine (e.g., claude-opus-4-5). Used in single mode.
    timeout:
      type: integer
      description: Optional job timeout in seconds for single mode (default 5400 / 90 minutes)
    steps:
      type: array
      description: "Required for collaborative mode. List of DAG steps (max 10). Each step has step_id, engine, prompt, and optional depends_on, repository, repositories, timeout_seconds, options."
      items:
        type: object
        properties:
          step_id:
            type: string
            description: Unique identifier for this step
          engine:
            type: string
            description: Agent engine for this step
            enum:
            - claude-code
            - codex
            - gemini
            - opencode
            - q
          prompt:
            type: string
            description: Task description for this step
          depends_on:
            type: array
            items:
              type: string
            description: List of step_ids this step depends on
          repository:
            type: string
            description: Single repository alias for this step
          repositories:
            type: array
            items:
              type: string
            description: List of repository aliases for this step
          timeout_seconds:
            type: integer
            description: Timeout for this step in seconds
          options:
            type: object
            description: Additional options for this step
        required:
        - step_id
        - engine
        - prompt
    engines:
      type: array
      items:
        type: string
      description: "Required for competitive mode. List of engine names for competing agents."
    distribution_strategy:
      type: string
      description: "Competitive mode: how work is distributed. round-robin or decomposer-decides."
      enum:
      - round-robin
      - decomposer-decides
    approach_count:
      type: integer
      description: "Competitive mode: number of parallel approaches (2-10, default 3)."
    min_success_threshold:
      type: integer
      description: "Competitive mode: minimum successful approaches needed (1 to approach_count, default 3)."
    approach_timeout_seconds:
      type: integer
      description: "Competitive mode: timeout per approach in seconds."
    decomposer:
      type: object
      description: "Competitive mode: engine configuration for the decomposer step."
      properties:
        engine:
          type: string
          description: Engine name for decomposer
    judge:
      type: object
      description: "Competitive mode: engine configuration for the judge step."
      properties:
        engine:
          type: string
          description: Engine name for judge
        model:
          type: string
          description: Optional model override for judge
    options:
      type: object
      description: "Competitive mode: additional options."
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
- single: Single agent executes the task (default)
- collaborative: DAG-based multi-step orchestration where multiple agents execute steps with dependencies
- competitive: Parallel competing agents with decompose-compete-judge pipeline

EXECUTION FLOW (single mode):
1. Validates delegate_open permission (power_user or admin required)
2. Validates required parameters (prompt, repositories)
3. Validates engine and mode values
4. Checks repository readiness on Claude Server (polls if cloning)
5. Creates job via POST /jobs with engine/model/timeout options
6. Registers callback URL if configured
7. Starts job and registers in job tracker
8. Returns job_id for async polling via poll_delegation_job

EXECUTION FLOW (collaborative mode):
1. Validates delegate_open permission
2. Validates steps (non-empty, max 10, required fields, valid engines, valid DAG)
3. Checks repository readiness for all repos referenced in steps
4. Applies guardrails to each step prompt
5. Creates orchestrated job via POST /jobs/orchestrated
6. Registers callback, starts job, registers in tracker
7. Returns job_id

EXECUTION FLOW (competitive mode):
1. Validates delegate_open permission
2. Validates engines list, distribution_strategy, approach_count, etc.
3. Checks repository readiness for all repos
4. Applies guardrails to prompt
5. Creates competitive job via POST /jobs/competitive
6. Registers callback, starts job, registers in tracker
7. Returns job_id

COLLABORATIVE MODE DETAILS:
- steps: Array of step objects, each with step_id, engine, prompt
- depends_on: Step dependencies form a DAG (directed acyclic graph)
- Must have exactly one terminal step (no other step depends on it)
- Each step can specify its own repository/repositories and timeout
- Maximum 10 steps per job

COMPETITIVE MODE DETAILS:
- engines: List of engines that will compete on the task
- distribution_strategy: "round-robin" distributes evenly, "decomposer-decides" lets decomposer choose
- approach_count: Number of parallel approaches (2-10, default 3)
- min_success_threshold: Minimum approaches that must succeed (default: approach_count)
- decomposer/judge: Optional engine config for decomposition and judging steps

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
- 'Collaborative mode requires non-empty steps list' -> steps missing or empty for collaborative mode
- 'Collaborative mode supports at most 10 steps' -> Too many steps
- 'Duplicate step_id' -> Step IDs must be unique
- 'Step depends on itself' -> Self-dependency not allowed
- 'Collaborative DAG must have exactly 1 terminal step' -> DAG structure invalid
- 'Competitive mode requires non-empty engines list' -> engines missing for competitive mode
- 'Invalid distribution_strategy' -> Must be round-robin or decomposer-decides
- 'approach_count must be between 2 and 10' -> Out of range
- 'Repository failed to become ready' -> Repository clone timed out or failed
- 'Claude Delegation not configured' -> Delegation configuration not set up
- 'Claude Server error' -> Communication error with Claude Server
