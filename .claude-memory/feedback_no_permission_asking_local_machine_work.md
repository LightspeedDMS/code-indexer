---
name: feedback-no-permission-asking-local-machine-work
description: "Never ask permission before doing routine local-machine/local-server work (starting isolated test servers, running local test suites, etc.) — just do it and report."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 619213c0-5c48-4e14-aa62-f32f1fb7fa0e
  modified: 2026-09-02T12:53:28.918Z
---

Don't ask the user for permission before performing routine work on this local machine, including anything involving the local dev server or spinning up isolated local server instances for testing.

**Why:** User said explicitly: "don't ask in the future for doing stuff in this machine with the local server." This extends the existing standing rules [[feedback_no_unnecessary_questions]] and [[feedback_no_confirmation_on_commands]] specifically to local-machine operations — starting/stopping isolated local server instances for E2E testing, running background test suites, etc. are routine execution steps, not decisions requiring sign-off.

**How to apply:** For local-machine work (isolated test servers on non-default ports, local test runs, temp directories), just do it and report what was done — don't pre-announce as a question or wait for confirmation. This does NOT relax the separate, harder rules that still require explicit authorization: never touch the existing dev server on :8000, never push to master without the two-confirmation protocol, never touch staging/production without the established authorization phrases. Those are different categories (destructive/irreversible or affecting shared state) and remain fully in force — this memory only covers routine, reversible, local-only work.
