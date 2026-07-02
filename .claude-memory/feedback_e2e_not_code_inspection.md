---
name: E2E testing means functional execution, NEVER code inspection
description: Code inspection (checking source exists, reading methods) is NOT E2E testing — must execute real functionality and verify real outputs
type: feedback
---

Code inspection is NOT E2E testing. EVER. Real E2E means:
- Execute actual commands/API calls against a running system
- Verify actual outputs, responses, side effects
- Check actual data changes (files created, DB updated, vectors stored)

**Why:** User was furious when 8/9 "E2E tests" were just source code inspection (verifying methods exist, checking if strings are in source). This is deployment verification at best, not functional testing.

**How to apply:** For every fix/feature, the E2E test must:
1. Trigger the actual functionality (API call, CLI command, UI action)
2. Observe the actual result (response body, log output, file changes)
3. Compare against expected behavior
4. Report PASS/FAIL with actual evidence (not "code confirms X exists")

Examples of REAL E2E:
- Bug fix: Reproduce the bug scenario, verify it no longer fails
- Feature: Use the feature end-to-end, verify the output
- Performance: Run the operation, measure actual time

Examples of NOT E2E:
- "Source code contains the fix" — that's deployment verification
- "Method exists on class" — that's API surface check
- "Log shows no errors" — that's passive monitoring, not active testing
