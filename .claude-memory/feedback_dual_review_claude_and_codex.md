---
name: feedback-dual-review-claude-and-codex
description: "User wants every code review gate to run BOTH a Claude code-reviewer AND an independent Codex review in parallel, not Claude alone"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

Every story/PR review gate should dispatch a Claude `code-reviewer` AND an independent `codex-code-reviewer` in parallel (independent context, not sharing the Claude reviewer's transcript), then consolidate both sets of findings before relaying to the implementing agent.

**Why:** During Epic #1454 Story #1457 (a very large, 12-round TDD story), the Claude code-reviewer approved the implementation as genuinely wired with "no dead/inert code." A parallel Codex (gpt-5.6-sol) review the user explicitly requested found the opposite for the single most important mechanism: the live query path pinned one storage location but actually read from a different, unprotected one — the safety guarantee was inert in production despite passing "real infrastructure" TDD tests. Root cause: the specific "E2E" test that should have caught it used a mocked vector store instead of the real one. Codex also found a non-thread-safe concurrency bug and an active data-safety gap that Claude's review missed entirely. The user's directive after this: "for all stories the protocol is dual code reviews using codex, don't forget."

**How to apply:** For every code-review gate in this project (not just this epic) going forward, launch `code-reviewer` and `codex-code-reviewer` (via `REQUEST_MODEL:` prefix if the user specifies a model) in the same message so they run in parallel. Verify the Codex run actually used Codex CLI (see [[feedback_verify_codex_actually_ran]]) before trusting its verdict — silent fallback to Claude would defeat the whole point of getting a second, independent perspective. If either reviewer returns REQUEST CHANGES, consolidate both sets of findings into one remediation message rather than relaying them separately.

This supersedes the narrower [[feedback_use_code_reviewer]] guidance ("Codex credits running low, use code-reviewer only") when the user has current Codex credits available and hasn't said otherwise — check for a more recent explicit instruction if credits become constrained again.
