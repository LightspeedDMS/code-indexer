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

**How to apply:** To prove a real Codex run, check `~/.codex/sessions/YYYY/MM/DD/` for a NEW rollout `.jsonl` dated today, containing the subject keyword, with `"model":"gpt-5.4"`. Capture the newest-session timestamp BEFORE the run as a baseline so a new file is unambiguous. For certainty, drive Codex directly instead of via the wrapper: `codex exec -s read-only -c approval_policy="never" -o <outfile> - < <promptfile>` (read-only sandbox + no approval prompts = non-interactive, no hang; session persists as proof — do NOT pass `--ephemeral`). Related: [[feedback_trust_codex_first_pass.md]], [[feedback_use_code_reviewer.md]].
