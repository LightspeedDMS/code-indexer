---
name: feedback_claude_session_trailer_ok
description: the Claude-Session commit trailer is approved for public commits; do not re-raise it when reviewers flag it
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-03T13:33:37.332Z
---

The owner approved keeping the `Claude-Session: <url>` trailer (and the Claude co-author trailer) in commit messages on the public repository (2026-10-03).

**Why:** reviewers (Codex) repeatedly flagged the trailer as a possible disclosure; the owner judged it acceptable.

**How to apply:** keep adding the attribution trailers as instructed. When a reviewer raises the trailer, note that it is owner-approved and do not ask again or treat it as a disclosure finding.

Related: [[feedback_no_secrets_in_memory]], [[feedback_no_unnecessary_questions]].
