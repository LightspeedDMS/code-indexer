---
name: feedback_verify_codex_actually_ran
description: "When a codex review is requested, verify it actually ran on real Codex — the wrapper agents fall back to Claude silently"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 770fe111-6a01-41c4-b295-9b03298a6924
---

The `elite-codex-*` / `*-codex-*` subagents fall back to a Claude backend when Codex is unavailable, WITHOUT announcing it. Do not claim "codex reviewed it" based on invoking the agent alone.

**Why:** This session I implied a story review was Codex when it was actually a Claude fallback; the user caught it ("did you review with codex?"). The fallback review was still useful, but it was not Codex — and when re-run on real Codex GPT-5.4 it found concrete API-signature bugs (`max_files` vs `max_results`, `line_number` vs `line`, no `job_ctx`) the Claude pass had missed.

**How to apply:** Most reliable proof for a `codex exec` run (2026-06-17, codex-cli 0.139.0): capture `stat -c '%Y' ~/.codex/logs_2.sqlite` BEFORE, and after the run confirm it changed AND stdout printed a `tokens used N` line. A plain `codex exec "<prompt>"` (no `-o`) does NOT write a `~/.codex/sessions/.../rollout-*.jsonl` file — only `logs_2.sqlite` telemetry — so checking `sessions/` alone gives FALSE NEGATIVES for exec runs (it's the interactive-session artifact). `sessions/` is still valid proof IF you pass `-o`/run interactively. UPDATE (2026-06-24, codex-cli v0.139.0, model gpt-5.5): a plain `codex exec --sandbox read-only` (no `-o`) DID write `~/.codex/sessions/2026/06/24/rollout-*.jsonl` this session — so the sessions/ check is NOT always a false negative; a fresh-DATED rollout appearing IS proof. STRONGEST + simplest proof: `codex exec` prints a header to stdout at start — `OpenAI Codex vX` / `model: <id>` / `provider: openai` / `session id: <uuid>` — a silent Claude fallback never prints that header, so the header alone confirms real Codex without touching any file. This session Codex earned its keep: it REFUTED one of my bug claims (re-migration "crash" — missed an AC5 skip-guard) and narrowed two others, before I filed.

Drive Codex directly to bypass the flaky wrapper: `codex exec --sandbox read-only "<prompt>"` (non-interactive, no hang). GOTCHAS hit this session: (1) `-m gpt-5` is REJECTED on a ChatGPT-account plan ("model not supported when using Codex with a ChatGPT account") — OMIT `-m` to use the account default. (2) The codex-AGENT subagent wrapper currently falls back to Claude SILENTLY because its MCP transport errors with `Auth(AuthorizationRequired)` (`rmcp::transport::worker ... Transport channel closed`) — the bare `codex exec` CLI still works fine. Both the fallback review and the genuine Codex review this session converged on the same core findings, but Codex added sharper specifics (named call sites, exact divergent error strings, behavior matrix). Related: [[feedback_trust_codex_first_pass.md]], [[feedback_use_code_reviewer.md]].
