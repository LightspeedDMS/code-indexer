---
name: project_chunk_storage_write_mode_context
description: "chunk-storage write mode is context-dependent: server enforces sqlite, CLI/daemon default json, json->sqlite conversion is ALWAYS explicit (never implicit)"
metadata: 
  node_type: memory
  type: project
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

Epic #1454 chunk-storage layout (SHARDED_JSON `vector_*.json` vs CHUNKS_DB single-file `chunks.db`) write mode is CONTEXT-DEPENDENT, per an explicit user decision (2026-07-28):

- **Server mode** -> enforces CHUNKS_DB (sqlite) for NEW collections.
- **CLI mode AND daemon mode** (daemon is a CLI client, NOT a server) -> default NEW collections to SHARDED_JSON (json). The `CIDX_CHUNKS_DB_NEW_COLLECTIONS` env var remains an explicit per-process opt-in.
- **Existing collections always win**: a plain `cidx index` writes a collection in whatever layout its committed discriminator already says (`resolve_chunk_layout` / `_is_chunks_db_collection`), regardless of the context default. Auto-detect, never override.
- **json -> sqlite conversion is ALWAYS an EXPLICIT act, never implicit.** The server does it automatically (fleet migration, it owns the storage). A standalone CLI user does it by running `cidx index --migrate-chunks-to-sqlite` (Story #1488), which migrates ALL collections (semantic + multimodal + temporal), migrates-then-exits, and deletes legacy only after the Bug #1486 durable-verify gate. It NEVER happens just because a new binary shipped.

**Why:** forcing a non-server CLI to silently rewrite a user's local index into a new on-disk format on upgrade would be semantically contradictory and is exactly the implicit-behavior-change class Bug #1486's dual review flagged (Codex Finding 4). CLI users must opt in explicitly.

**How to apply:** the global `_parse_use_chunks_db_for_new_collections_env` unset default must be False (NOT True); the server enables CHUNKS_DB explicitly at its own creation boundary. Never re-flip the global default to True as a shortcut. See [[feedback_no_half_wired_features]]. Story #1488 delivers the CLI migrate command + formalizes this contract; the default-context correctness is also the Bug #1486 Finding-4 remediation.
