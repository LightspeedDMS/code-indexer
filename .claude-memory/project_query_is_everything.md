---
name: Query capability is the core business value
description: Query is everything — all CLI query features MUST have MCP/REST parity. NEVER remove/break query functionality.
type: project
---

Query capability = core value of CIDX. All CLI query features MUST be available in MCP/REST APIs.

NEVER remove or break query functionality. Query degradation = product failure.

**Why:** Query is what users interact with. The indexing is a means to an end — the end is search. If query breaks or regresses, the product is worthless regardless of how good the indexing is.

**How to apply:** When adding query features to CLI, always add the corresponding MCP/REST parameter. When refactoring query code, verify all existing parameters still work. When reviewing PRs that touch query paths, check for regressions.
