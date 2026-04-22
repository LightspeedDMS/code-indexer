---
name: edit_memory
category: memory
required_permission: repository:write
tl_dr: Update mutable fields on an existing memory entry with optimistic concurrency.
inputSchema:
  type: object
  properties:
    memory_id:
      type: string
      description: uuid4 of the memory to edit (from create_memory output or from the memory's frontmatter id field).
    expected_content_hash:
      type: string
      description: SHA-256 hash of the memory's current on-disk content for optimistic concurrency. Obtain via get_file_content. If the hash does not match current state, the edit fails with a stale-content error and the caller must re-read and retry.
    type:
      type: string
      enum:
      - architectural-fact
      - gotcha
      - config-behavior
      - api-contract
      - performance-note
      description: Memory type classifier (replaces prior value on successful edit).
    scope:
      type: string
      enum:
      - global
      - repo
      - file
      description: Scope of applicability (replaces prior value on successful edit).
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
      description: Short, pointer-style claim (replaces prior value on successful edit).
    evidence:
      type: array
      minItems: 1
      maxItems: 10
      description: List of evidence entries. Each entry is either {file, lines} or {commit}. Replaces prior evidence on successful edit (PUT semantics, not merge).
      items:
        type: object
        oneOf:
        - properties:
            file:
              type: string
              description: Repository-relative path to the file that proves the claim.
            lines:
              type: string
              description: Line range in the form "start-end".
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
      description: Optional markdown body (replaces prior body on successful edit).
      default: ''
  required:
  - memory_id
  - expected_content_hash
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
      description: Whether the memory edit succeeded.
    id:
      type: string
      description: uuid4 of the edited memory (present when success=true).
    content_hash:
      type: string
      description: SHA-256 hash of the updated memory file for optimistic concurrency on future edits (present when success=true).
    path:
      type: string
      description: Repository-relative path of the edited memory file (present when success=true).
    error:
      type: string
      description: Error message - includes stale-content indication with current content_hash when expected_content_hash did not match (present when success=false).
  required:
  - success
---

Update mutable fields on an existing memory entry.

WHEN TO CALL THIS TOOL:
- The user provides new evidence that supersedes or refines an existing memory
- An existing memory's claim is confirmed correct but its evidence is incomplete
- A previous memory contains a factual error you can now disprove with code evidence
- The scope, type, or scope_target was recorded incorrectly

WHEN NOT TO CALL THIS TOOL:
- You disagree with the memory but have no new evidence - leave it and tell the user
- You want to append commentary rather than correct a verifiable claim
- The change would remove the last evidence entry - delete and recreate instead

FACT-CHECK DISCIPLINE:
Same requirement as create_memory. Any field you change must be backed by evidence you
have verified in the current session. Do not edit a memory to match a new assumption;
edit it to match newly discovered facts.

CONCURRENCY:
Provide expected_content_hash from the memory's current state. If the hash does not
match (another writer edited it first), the call fails. Re-read the memory via
get_file_content, reconcile the changes, and retry with the new hash.

Mutable fields: summary, evidence, type, scope, scope_target, referenced_repo.
Immutable fields: id, created_by, created_at (server-enforced).

PUT semantics: all mutable fields are replaced by the payload, not merged.

EXAMPLE: {"memory_id": "7f3a2c1e-4b5d-4e8a-9f2b-8c1a3d5e7f09", "expected_content_hash": "abc123def456", "type": "config-behavior", "scope": "global", "summary": "config.json is bootstrap-only; runtime settings live in the DB. Auto-migrated on first boot after upgrade.", "evidence": [{"file": "CLAUDE.md", "lines": "248-292"}, {"file": "src/code_indexer/server/services/config_service.py", "lines": "1-200"}]}
