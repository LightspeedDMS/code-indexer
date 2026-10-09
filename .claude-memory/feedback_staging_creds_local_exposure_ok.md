---
name: feedback-staging-creds-local-exposure-ok
description: secrets (staging creds, API keys, tokens) appearing in local session output or agent transcripts are not an incident; never raise rotation; still never commit or publish secrets
metadata:
  type: feedback
---

When credentials or API keys show up in local session output, agent transcripts, test output or SSH command history, do not report it as an exposure, do not stop work over it, and do not ask the owner to rotate anything.

**Why:** the owner assessed it and decided: "don't worry about api keys or secrets going into the transcript ... that stuff stays in this machine" (2026-10-07; earlier the same for staging credentials, 2026-10-01). A better secret-handling mechanism may come later.

**How to apply:**
- No flagging, no rotation requests for secrets seen only in local output/transcripts.
- The hard rules for anything that LEAVES the machine or the tree still apply: never write a secret into a committed file, an issue, a PR, a tracked memory file or the security tracker.
- Product code must still never log or export secrets (that is a product defect); this note is only about the development session's own transcripts.
- Still give agents secrets through 600-mode files rather than inlining them (inlined secrets trigger approval prompts that stall agents). See [[feedback-no-secrets-in-memory]].
