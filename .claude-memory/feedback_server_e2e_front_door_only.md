---
name: feedback-server-e2e-front-door-only
description: "Server E2E tests MUST use REST API/MCP front door, never CLI tools — CLI/SSH only for troubleshooting"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 31410a0b-3e6e-492f-b329-34dc7f08b796
---

Server E2E testing (local or staging) MUST exercise the REST API / MCP front door via HTTP requests. NEVER use CLI tools (cidx init, cidx index, cidx query) or SSH shell commands as the primary test mechanism.

**Why:** CLI-based "E2E" tests bypass the entire HTTP stack (auth, routing, middleware, serialization) and test a completely different code path. They give false confidence about server correctness. User was emphatic: "you MUST exercise the code front door, using the API, and ssh only to troubleshoot, otherwise your tests are simply junk."

**How to apply:** When asked to test the server end-to-end, write test scripts that make HTTP requests (curl, httpx, requests) to REST API and MCP JSON-RPC endpoints. CLI/SSH is allowed ONLY for troubleshooting failures, inspecting logs, or verifying process state — never as the primary test mechanism.

Related: [[feedback_e2e_not_code_inspection]], [[feedback_e2e_verify_indexes_work]]
