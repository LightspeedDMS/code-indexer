---
name: delete_memory
category: memory
required_permission: repository:write
tl_dr: Permanently remove a memory from the store with optimistic concurrency.
inputSchema:
  type: object
  properties:
    memory_id:
      type: string
      description: uuid4 of the memory to delete (from create_memory output or from the memory's frontmatter id field).
    expected_content_hash:
      type: string
      description: SHA-256 hash of the memory's current on-disk content for optimistic concurrency. Obtain via get_file_content. If the hash does not match current state, the delete fails and the caller must re-read and retry.
  required:
  - memory_id
  - expected_content_hash
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the memory deletion succeeded.
    id:
      type: string
      description: uuid4 of the deleted memory (present when success=true).
    message:
      type: string
      description: Confirmation message including the summary of what was removed so the user can verify (present when success=true).
    current_content_hash:
      type: string
      description: Current SHA-256 hash of the memory file when expected_content_hash did not match - caller should re-read and retry (present when success=false due to stale hash).
    error:
      type: string
      description: Error message (present when success=false).
  required:
  - success
---

Permanently remove a memory from the store.

WHEN TO CALL THIS TOOL:
- The user explicitly states the memory is wrong and should not exist
- The memory describes a behavior that has been removed from the codebase (verified via code evidence)
- The memory is a duplicate of a more accurate, higher-evidence memory

WHEN NOT TO CALL THIS TOOL:
- You merely disagree with the memory - tell the user and let them decide
- The memory is outdated but you have not verified the current behavior
- You are consolidating memories without user awareness

CONCURRENCY:
Provide expected_content_hash from the memory's current state. The operation fails if
the hash does not match. Re-read via get_file_content and retry with the new hash.

After deletion, confirm the uuid and summary of what was removed so the user can verify.

EXAMPLE: {"memory_id": "7f3a2c1e-4b5d-4e8a-9f2b-8c1a3d5e7f09", "expected_content_hash": "abc123def456"}
