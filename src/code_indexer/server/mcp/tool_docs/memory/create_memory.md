---
name: create_memory
category: memory
required_permission: repository:write
tl_dr: Create a persistent, evidence-backed technical memory in the shared memory store.
inputSchema:
  type: object
  properties:
    type:
      type: string
      enum:
      - architectural-fact
      - gotcha
      - config-behavior
      - api-contract
      - performance-note
      description: Memory type classifier - pick the one that best fits the claim being captured.
    scope:
      type: string
      enum:
      - global
      - repo
      - file
      description: Scope of applicability. global = all repos; repo = single repository; file = single file path.
    scope_target:
      type:
      - string
      - 'null'
      description: Repository alias (when scope=repo), repo-relative file path (when scope=file), or null when scope=global.
      default: null
    referenced_repo:
      type:
      - string
      - 'null'
      description: Repository alias for access filtering. Required when scope in [repo, file]; null when scope=global.
      default: null
    summary:
      type: string
      maxLength: 1000
      description: Short, pointer-style claim. Aim for under 200 characters; never exceed 1000.
    evidence:
      type: array
      minItems: 1
      maxItems: 10
      description: List of evidence entries. Each entry is either {file, lines} or {commit}. At least one required.
      items:
        type: object
        oneOf:
        - properties:
            file:
              type: string
              description: Repository-relative path to the file that proves the claim.
            lines:
              type: string
              description: Line range in the form "start-end" (e.g., "88-102").
          required:
          - file
          - lines
        - properties:
            commit:
              type: string
              description: Commit SHA that proves the claim.
          required:
          - commit
    body:
      type: string
      description: Optional markdown body with additional context (pointers to code, not prose dumps).
      default: ''
  required:
  - type
  - scope
  - summary
  - evidence
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the memory creation succeeded.
    id:
      type: string
      description: Server-generated uuid4 for the new memory (present when success=true).
    content_hash:
      type: string
      description: SHA-256 hash of the memory file for optimistic concurrency on future edits (present when success=true).
    path:
      type: string
      description: Repository-relative path of the written memory file, e.g. cidx-meta/memories/{uuid}.md (present when success=true).
    error:
      type: string
      description: Error message (present when success=false).
  required:
  - success
---

Create a persistent, evidence-backed technical memory in the shared memory store.

WHEN TO CALL THIS TOOL (trigger-driven - act on these):
- The user corrects an assumption you made about architecture, behavior, or code structure
- The user explains a non-obvious gotcha, ordering constraint, or failure mode
- The user clarifies how a config key behaves, including edge cases or defaults
- The user describes an API contract, parameter limit, or undocumented behavior
- You observe a verified performance characteristic (measured, not guessed)
- You discover an architectural fact that a future agent would need to know

Before calling, search existing memories on the topic (search_code with
path_filter="cidx-meta/memories/*.md"). If a matching memory exists, call
edit_memory instead of creating a duplicate.

WHEN NOT TO CALL THIS TOOL:
- You have a hunch but have not verified it against code or commits
- The information is business context, not a technical fact
- The note is prose commentary rather than a verifiable claim
- You are summarizing a conversation rather than extracting a discrete fact

FACT-CHECK DISCIPLINE (mandatory before writing):
Every memory MUST include at least one evidence entry pointing to a specific file and
line range, or a specific commit hash. This is not optional. If you cannot name the
file and lines that prove the claim, stop, find them first, then write the memory.
A memory without evidence is speculation and will mislead future agents.

SUMMARY DISCIPLINE:
The summary field is a pointer, not an essay. Write one tight claim (aim for under 200
characters, never more than 1000). Include a file:line reference inline if it fits.
Do not restate the evidence list in prose form.

GOOD summary:
  "VoyageAI batch limit is 120,000 tokens enforced in embedded_voyage_tokenizer.py:88-102.
   Exceeding it raises BatchSizeError, not a warning."

BAD summary:
  "There are some considerations around how VoyageAI handles batching that might be
   worth knowing about in certain scenarios when you're working with embeddings."

SCOPE:
- global: fact applies across all repos; scope_target and referenced_repo MUST be null
- repo: fact is specific to one repository; provide referenced_repo alias as scope_target
- file: fact is specific to one file path; provide referenced_repo and scope_target path

BIAS TOWARD CAPTURING: sessions end, memories persist. If the user taught you something
non-obvious, capture it. Future agent sessions depend on it.

After writing, confirm the uuid and summary to the user.

EXAMPLE: {"type": "config-behavior", "scope": "global", "summary": "VoyageAI batch limit is 120,000 tokens enforced in embedded_voyage_tokenizer.py:88-102. Exceeding it raises BatchSizeError, not a warning.", "evidence": [{"file": "src/code_indexer/embedded_voyage_tokenizer.py", "lines": "88-102"}]}
