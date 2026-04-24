---
name: edit_memory
category: admin
required_permission: repository:write
tl_dr: 'Update mutable fields on an existing memory entry.


  WHEN TO CALL THIS TOOL:

  - The user provides new evidence that supersedes or refines an existing memory

  - An existing memory''s claim is confirmed correct but its evidence is incomplete

  - A previous memory contains a factual error you can now disprove with code evidence

  - The scope, type, or scope_target was recorded incorrectly


  WHEN NOT TO CALL THIS TOOL:

  - You disagree with the memory but have no new evidence - leave it and tell the
  user

  - You want to append commentary rather than correct a verifiable claim

  - The change would remove the last evidence entry - delete and recreate instead


  FACT-CHECK DISCIPLINE:

  Same requirement as create_memory.'
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
