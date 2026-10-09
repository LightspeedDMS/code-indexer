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

**Status 2026-10-08 (late):** local front-door plan executed (all scenarios pass); its findings were fixed and re-reviewed (snapshot write guard incl. every snapshot layout, untracked-only dirty check, access-filter log level); remaining minor items are filed as private follow-ups. All local gates are green on the 12.84.0 tree (2026-10-09): lint, fast (19,224 passed), server-fast (26,018 passed, after two test-only commits giving tests with real waits or one-time app setup a load-tolerant time limit), e2e all 7 phases. The version bump and CHANGELOG `[12.84.0]` entry are committed as the tip of development; CI tagged v12.84.0.

**Status 2026-10-09:** 12.84.0 merged to staging and running on staging solo and every staging cluster node (auto-update re-enabled everywhere). Before the cluster key went back, the bring-up checklist was followed: Langfuse lookback set to 3 days, trace sessions older than 3 days archived then deleted (exposure now ~1.6k files). New provider keys installed through the front door on both environments. Front-door validation of every reachable 12.84.0 change passed on both (results in gitignored `.analysis/staging_validation_12.84.0.md` and `reports/reviews/`). Follow-ups filed privately: a saved provider key reaches only the worker that handled the save (restart every node after a key change until fixed), REST push failure body, multi-repo provider fallback.

**Production (2026-10-09):** the owner authorized the promotion; 12.84.0 (which also carries 12.83.0, never promoted on its own) is on public master and production reports 12.84.0. Development was back-merged from public master.

**12.85.0 progress:** epic and stories are in the private tracker (G1-lite). Slice 1a of the first story (index-time spend attributed to repository, job and node; refresh-time temporal spend recorded as `temporal_refresh`) is committed on development after three Codex rounds. Next: slice 1b (volume alarm summing `refresh` and `temporal_refresh`, pause of system-triggered refreshes, health reason), then the pay-once stories.

**The re-embed cost bug is NOT fixed in 12.84.0; it is only capped.** The Langfuse sync never deletes trace files from disk (the age window limits fetching only), so the trace repo regrows ~2.4k files/day and the cap erodes over weeks. The fix is 12.85.0 (G1-lite: reuse stored vectors before any provider call, no resume-list replay, per-run budget, volume alarm). Owner's standing ask (2026-10-09): "make sure the runaway voyage cost issue is fixed and they don't go nuts again". Never promote to master without the owner's explicit per-push authorization. Gate lessons: never run gates while an agent edits the tree; the e2e Phase 7 is SIEM delivery; `fast-automation.sh` takes ~50 min and server-fast ~22 min.

**Mechanics that work:**
- Mixed-file commits: an assembler builds HEAD + one group's hunks as blobs (`patch --fuzz=0`, `hash-object`, `update-index`; preserve CRLF files), exports the index with `checkout-index`, runs imports, ruff, mypy and the group's tests there, then commits with `--no-verify` ([[feedback-partial-stage-exact-content]]).
- Commit messages never carry a bare `#N` ([[feedback-commit-issue-refs-overlap]]).
- Review every memory file before committing it: this file is public.
