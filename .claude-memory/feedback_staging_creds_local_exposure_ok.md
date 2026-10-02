---
name: feedback-staging-creds-local-exposure-ok
description: test-environment credentials appearing in local session output are not reported as an incident; credentials still never get committed
metadata:
  type: feedback
---

When test-environment (staging) credentials show up in local session output, transcripts or SSH command history, do not report it as an exposure or ask about rotation.

**Why:** the owner assessed the staging environment's risk and asked for this (2026-10-01).

**How to apply:**
- Stop flagging local appearances of staging credentials.
- Keep the hard rules for anything that leaves the machine: never write a credential into a committed file, an issue, a PR, a memory file or the security tracker.
- Production credentials are not covered by this note.
- Still give agents secrets through 600-mode files rather than inlining them, because inlined secrets trigger approval prompts that stall agents. See [[feedback_no_secrets_in_memory]].
