---
name: feedback_rest_model_changes_need_fast_automation
description: "Server REST/MCP query-model or query-param changes require fast-automation.sh, not just server-fast — the param-parity guard lives in the CLI suite"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9f3e846a-213a-4733-9159-8696ede6081c
---

When a server change adds/removes a field on a query REQUEST model or a query parameter — `server/models/api_models.py::SemanticSearchRequest`, `server/models/query.py::SemanticQueryRequest`, the omni `MultiSearchRequest`, or an MCP search tool doc (`search_code.md`) — run `./fast-automation.sh` IN ADDITION to `./server-fast-automation.sh`.

**Why:** `tests/unit/query/test_query_parameter_parity.py` (CLI/core suite, only run by fast-automation — server-fast ignores `tests/unit/server/` but this test is under `tests/unit/query/`) asserts the REST and MCP query endpoints expose no parameters beyond an allowlist. Adding a new REST/MCP-only param (e.g. `no_embedding_cache_shortcut`, Story #1108) trips `test_no_extra_rest_parameters` / `test_no_extra_mcp_parameters` until the new param is added to that test's `normalized_expected` allowlist.

**How to apply:** Touching query request models -> gate with BOTH suites. If the new param is intentionally REST/MCP-only (no CLI equivalent), add it to the parity allowlist in `test_no_extra_rest_parameters` and (if MCP-exposed) `test_no_extra_mcp_parameters` — NOT to the shared base `API_EXPECTED_PARAMETERS` (that would wrongly require the CLI to expose it too).

**How it bit:** Epic #1103 S4/S5 (#1108/#1109) were gated only with server-fast, so this latent CLI-suite parity failure slipped through and only surfaced when S6 (#1110) touched `storage/filesystem_vector_store.py` and triggered fast-automation — forcing an extra ~9-min re-gate. Related: [[feedback_zero_failures_no_excuses]].
