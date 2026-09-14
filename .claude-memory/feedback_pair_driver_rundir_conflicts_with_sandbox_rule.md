---
name: feedback_pair_driver_rundir_conflicts_with_sandbox_rule
description: "The pair driver's RUN_DIR is under /tmp while the CLAUDE.md it injects forbids writing outside the project, so codex may refuse to write WORKING_AGREEMENT.md and the driver silently imposes its default role split"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-14T00:58:15.011Z
---

Observed 2026-09-13 across two consecutive `tdd-paired-engineer` runs on the same mission (#1854).

`claude-pair-run` places `$RUN_DIR` (and therefore `WORKING_AGREEMENT.md`) under `/tmp/...`, outside
the project working directory. The driver also injects this project's CLAUDE.md into every codex
turn, and that file opens with a hard Sandbox Rule: "NEVER modify files outside this project's
working directory."

Those two facts conflict. When codex draws the turn that is supposed to author
`WORKING_AGREEMENT.md`, it may refuse on sandbox grounds and state its plan in the handoff message
instead. The driver then falls back to its own DEFAULT role split.

The fallback IS flagged, but only in the run log, not in the handoff: `lib/pair-turn.sh:183` writes
`"DEFAULT AGREEMENT IMPOSED: no WORKING_AGREEMENT.md produced by turn 6; driver wrote the default
split from pair-default-working-agreement.md"` to `run.log`. Grepping that line is the fastest way
to detect this, faster than reading the turn-6 message.

The behaviour is NOT deterministic: in run 1 codex wrote the file normally; in run 2, same mission
and same prompt shape, it refused. So its absence proves nothing about whether negotiation happened.

**Why it matters:** an absent `WORKING_AGREEMENT.md` looks exactly like "the negotiation phase
failed" or "the pair skipped its own methodology". It is usually neither. Reading it that way leads
to re-running a pair that actually did converge, or to distrusting a correct result.

**How to apply:**
- Detect it with `grep "DEFAULT AGREEMENT IMPOSED" run.log`. If present, read the turn-6 handoff
  message before concluding anything — the plan is normally stated there in full.
- PREVENT it: `claude-pair-run` accepts `--session-dir` (line 97; it otherwise defaults to
  `mktemp -d "${TMPDIR:-/tmp}/claude-pair-run.XXXXXX"` at line 155). Passing a base directory inside
  the project, e.g. `--session-dir <project>/.tmp/pair-run`, puts `$RUN_DIR` inside the sandbox and
  the conflict disappears with no driver change. Note it is a BASE directory — per-run artifacts land
  in `<session-dir>/runs/<run-id>`.
- Compare the driver's imposed default against the plan in that message. If they are materially the
  same (they were in run 2: alternating RED/GREEN across the two agents), nothing is lost and the
  run should continue rather than be restarted.
- Do not "fix" this by telling codex to ignore the sandbox rule. The sandbox rule is the more
  important of the two, and codex refusing was correct behaviour, not a malfunction.

Related: [[feedback_pair_codex_never_sees_claude_md]] — that note records the older state where
codex got NO CLAUDE.md at all. The driver now injects it, which fixed that gap and created this
one. [[feedback_paired_engineer_two_models_for_quality]] covers why the pair is worth its cost.
