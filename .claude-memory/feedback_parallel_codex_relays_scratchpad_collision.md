---
name: feedback_parallel_codex_relays_scratchpad_collision
description: "parallel codex-agent relays share the session scratchpad and overwrite each other's fixed-name prompt files; give each relay its own subdir"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 87afb584-3ac2-483a-aa98-3b98d28f4d88
  modified: 2026-10-06T15:41:05.654Z
---

When several `codex-agent` (or other external-CLI relay) subagents run in parallel, they share the session
scratchpad and write fixed filenames (`codex_relay_payload.txt`, `codex_relay_prompt.txt`, `codex_out.txt`).
One relay's write can replace a sibling's prompt, so a sibling runs the WRONG brief (observed 2026-10-06:
run B's relay first executed run C's brief; it later self-corrected with uniquely named files).

**Why:** a relay that silently runs a sibling's task returns a plausible but off-scope report; the error is only
visible by inspecting which brief each codex session actually received.

**How to apply:** in every parallel relay brief, name a per-run scratch subdir (e.g. `<scratchpad>/run-A/`) and
tell the relay to keep all its payload/prompt/output files there. After dispatch, verify each
`~/.codex/sessions/<date>/rollout-*.jsonl` carries the expected scope marker (grep a unique "run X" string).
Related: [[feedback_verify_codex_actually_ran]], [[feedback_parallel_dispatch_cap_4_on_20x]].
