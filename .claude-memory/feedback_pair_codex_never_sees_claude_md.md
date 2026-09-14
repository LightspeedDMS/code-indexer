---
name: feedback_pair_codex_never_sees_claude_md
description: "The pair driver gives claude CLAUDE.md automatically but codex only stdin — codex never sees project invariants unless the mission brief pastes them, which produces convention violations"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-11T15:07:25.343Z
---

`~/.claude/scripts/pair/lib/pair-invoke.sh` invokes the two agents asymmetrically:

```
claude: setsid env --chdir="$cwd" claude -p "$prompt" ... --add-dir "$cwd"
codex:  setsid env --chdir="$cwd" codex exec ...      < "$prompt_file"
```

Claude Code **auto-loads the project's CLAUDE.md**. `codex exec` does not — it receives only the
prompt piped to stdin. And the shared templates never mention it: `grep -c "CLAUDE.md"` on both
`~/.claude/prompts/pair-programming-protocol.md` and `pair-default-working-agreement.md`
returns **0**.

So codex sees the project's binding rules ONLY if the dispatching agent happens to paste them
into that run's mission brief. It is luck, not process.

**Evidence, two runs the same day on the same repo:**

| Run | Mission mentions CLAUDE.md | Outcome |
|---|---|---|
| Story #1593 | YES (in its `02_codex.md`) | codex found 3 spec errors, caught a real ordering defect |
| Bug #1845 | NO (zero mentions across 13 turns) | codex produced 3 convention violations + 6 regressions |

All three #1845 violations were CLAUDE.md rules codex had never been shown: the
single-authority `is_versioned_snapshot` predicate rule (it reinvented the predicate), the
"~900 repositories, design everything for it" rule (it spawned a thread per cache hit on the
query hot path), and the cidx-meta path convention (it derived a directory positionally via
`path.parents[2]`, which walked to `/` and would have written a stray `cidx-meta/` inside the
golden repo tree).

**Why this is not "codex ignored instructions":** claude told codex on turn 9 to reuse the
canonical predicate. Codex reinvented it anyway. A rule delivered as one line of peer feedback
carries nothing like the weight of the project's invariants document — and codex had no way to
know it was an invariant rather than a preference.

**How to apply:**
- When dispatching `tdd-paired-engineer` on this project, ALWAYS state in the mission brief
  that both agents must read the project's `CLAUDE.md` in full before their first action, and
  name the specific invariants that bind the task (single-predicate rule, 900-repo scale rule,
  cluster-aware-state rule, cidx-meta path convention, dual-backend rule).
- Expect convention violations from codex's contributions whenever that is omitted, and review
  its diffs specifically against project conventions rather than only against correctness.
- The permanent fix is one instruction in `~/.claude/prompts/pair-programming-protocol.md` so
  it cannot depend on what a dispatcher remembers. That file is global config affecting every
  project — get the operator's approval before editing it.

Compounds with [[feedback_codex_interpreter_lacks_project_deps]]: codex also cannot run pytest
here, so without CLAUDE.md its review is diff-reading with no conventions to review against.
See [[feedback_paired_engineer_two_models_for_quality]] for why the pair is used at all.

**SUPERSEDED 2026-09-12 -- THE DRIVER NOW FIXES THIS. Verify before acting on the rest of this note.**

`~/.claude/scripts/pair/lib/pair-rulebook.sh` (added 2026-09-11) implements deterministic
project-rulebook injection: `pair_inject_rulebook` prepends the project's CLAUDE.md chain into
the codex prompt before codex ever sees it. Its own header documents this exact defect and names
the same three #1845 violations (duplicated sole-authority predicate, violated concurrency
budget, hand-rolled path walk).

Properties worth knowing:
- codex-only by design; the claude turn skips it because Claude Code already auto-loads CLAUDE.md
- bounded walk, PAIR_RULEBOOK_MAX_DEPTH=32, citing Rule 14
- a failed injection ABORTS the turn loudly -- codex never runs on an unprepared prompt
- byte-identical to the manual relay contract in prompts/project-rulebook-injection.md
- run.log records `RULEBOOK injected: <paths>` or `RULEBOOK: none found under <cwd>` per turn

**How to apply now:** do NOT paste CLAUDE.md invariants into pair mission briefs -- that is
redundant with the driver and wastes prompt space on a weaker paraphrase of the authoritative
injected text. Instead CHECK the run.log for the `RULEBOOK injected` line to confirm it fired,
and only intervene if it says `none found`.

**Lesson about the error itself:** I kept prescribing the manual workaround, saw `RULEBOOK
injected` in the logs, and credited my own briefs for it -- attributing a tool's fix to my
intervention. When a note says "the permanent fix is X", re-read the code before assuming X is
still undone.
