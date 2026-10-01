---
name: feedback-never-ship-unwired-work
description: NEVER build work that is not wired to a user-reachable surface — a capability only reachable from unit tests is not done
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-07T20:23:34.922Z
---

The user, verbatim: **"I will never fucking ask you to do work NOT TO BE FUCKING WIRED."**

Every piece of work must be reachable by a user through a real front door (MCP tool / REST
endpoint / CLI command) before it can be called complete. A feature that exists only in library
code and unit tests is NOT done, no matter how many green tests it has.

**Why:** green tests over an unreachable capability prove the code compiles, not that anyone can
use it. They carry the authority of coverage without the substance, so the gap survives review
and ships.

**The incident (2026-09-07, Epic #1786 X-Ray multi-file code graph).** Four stories were
implemented, reviewed, closed and promoted to staging — S2 #1787, S2b #1806, S3 #1792, S4 #1793:
CSR arena, binder, receiver-type resolution, inheritance families, refine phase, killable
analyze process, 500+ passing Rust tests. Then the user asked me to test the multi-file use
cases through the front door and there was no front door. Verified absence:

- `grep -rn "analyze_graph\|collect_facts\|build_repo_graph" src/code_indexer/ --include=*.py`
  returned ZERO hits — no Python caller anywhere
- Python invoked `xray-cli` in exactly two places: `--print-cache-identity` and the per-file
  legacy scan; neither is graph mode
- the MCP handler, REST route and every tool doc had no mention of it
- even Rust-side, `--analyze-graph` needs a prebuilt `--graph-in` file and NOTHING built one:
  `build_repo_graph` was called only by its own unit tests and one example binary

The irony that makes this unforgivable: this epic's own story #1785 fixed exactly this defect for
`FactKey::Custom` (a type with serde support, unit tests, zero writers and no reader), and I
wrote the anti-orphan analysis for it — then failed to apply the same test to the epic's headline
feature.

**How to apply.** Before calling ANY story complete, trace the capability from the user's entry
point inward and name each hop: MCP tool -> handler -> service -> library. If any hop is missing,
the story is not done. The mechanical check is a grep for the core function names across the
layer that should call them; zero hits means unwired. Do this BEFORE closing the issue, not when
someone asks to use the feature.

Related: [[feedback-never-claim-ready-without-staging-e2e]], [[feedback_no_half_wired_features]],
Messi Rule 12 (anti-orphan: wire it or don't write it), Bug #1665's registered-but-unwired trap.
