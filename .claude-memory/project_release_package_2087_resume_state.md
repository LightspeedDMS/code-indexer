---
name: project-release-package-2087-resume-state
description: "Resume state for the post-12.83.0 release work: 12.84.0 (near-done fixes) then 12.85.0 (G1-lite pay-once for solo). Read first after a reset."
metadata:
  type: project
---

**Owner decisions (2026-10-07, final):**
- TWO CUTS. 12.84.0 = the near-done work below. 12.85.0 = "G1-lite": pay-once indexing for SOLO (reuse stored vectors before any provider call, no up-front delete, no implicit wipe, retire resume-from-list, a slim side-state store, spend meter plus volume alarm). Codex's draft story set: gitignored `.analysis/g1lite_plan_codex_20261007.md` (unverified).
- DEFERRED to later releases: the S15 per-repo lease (park its uncommitted code on a feature branch after a git-safety backup) and every cluster-grade story of the design `plans/designs/crash_safe_reconcile_2087_20261006.md` (S16, S14, S5, S17, S19, S13, S9, S10, S20/public #2045, S18); private #2093 and #2095; the remaining dedup-epic items (private #2103); S22 (public #2049); small cleanups (orphan Tantivy methods; sanitizing and committing the design doc).
- Review loops: after a structural round, an Opus arbiter rules materiality; non-material variants become follow-ups.
- RELEASE GATING: test everything locally first (fast, server-fast, e2e incl. the PostgreSQL phase, local front-door REST/MCP runs of each new capability with a FAKE embedding provider, log audit). Only then ask the owner for new Voyage credentials for staging, then staging front-door validation (solo and cluster). No staging before that.
- Working mode on the 5x account: ONE worker agent + at most ONE reviewer ([[feedback-parallel-dispatch-cap-4-on-20x]]); fresh short-brief agents; mechanical staging/committing in the main context.

**12.84.0 status:**
- Committed on development (unpushed): the installer /healthz check, S12 stall watchdog, S0 reproduction harness, public #2038 #2039 #2060 #2064 #2076 #2094, the public #2056/#2057 FTS-integrity series, auto-updater non-interactive git, one git URL parser, URL credential stripping, S21 `file_extensions` (public #2047), git confirmation tokens (public #2087 part 2, #2099), plus security hardening commits (status only in the private security tracker).
- Remaining for 12.84.0, in order: commit the push consolidation and the telemetry/log redaction work after their final review; merge `origin/feature/docs-overhaul` (blocked until push work, S15 parking and memory notes are committed; resolve cluster docs by porting the /healthz text, and keep development's wording in the git tool docs and bulk provider-index doc); fix private #2109 (multi-index query reports every error as a timeout) and private #2108 (parallel strategy caps each provider fetch at 40; root-cause notes in gitignored `.analysis/rootcause_2108_2109_codex_20261007.md`); full local gates; version bump 12.84.0 (CHANGELOG operator note: after rolling back below this version and upgrading again, run `cidx index --rebuild-fts-index`); then ask for keys.

**Status 2026-10-09:** all 12.84.0 code committed and reviewed (about 80 unpushed commits on development; the S15 lease is parked on local branch `feature/s15-lease`). Gates: server-fast green (25,978 passed), e2e all 7 phases green (phase 7 re-run after a scoped log-audit allowance for the deliberate crash test), fast green except one load timeout since fixed with per-test limits. Remaining: local front-door plan execution (`.analysis/frontdoor_validation_12_84_0.md`, fake provider in a loopback-only namespace) -> log audit -> version bump 12.84.0 with CHANGELOG operator note -> final fast + server-fast on the bumped tree -> ask the owner for Voyage keys -> staging. Gate lessons: never run gates while an agent edits the tree; the e2e Phase 7 is SIEM delivery; `fast-automation.sh` takes ~50 min and server-fast ~22 min.

**Mechanics that work:**
- Mixed-file commits: an assembler builds HEAD + one group's hunks as blobs (`patch --fuzz=0`, `hash-object`, `update-index`; preserve CRLF files), exports the index with `checkout-index`, runs imports, ruff, mypy and the group's tests there, then commits with `--no-verify` ([[feedback-partial-stage-exact-content]]).
- Commit messages never carry a bare `#N` ([[feedback-commit-issue-refs-overlap]]).
- Review every memory file before committing it: this file is public.
