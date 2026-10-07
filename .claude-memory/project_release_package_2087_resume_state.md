---
name: project-release-package-2087-resume-state
description: "Resume state for the post-12.83.0 release package (re-embed fix #2087 G1, S12, S21, docs-overhaul merge, defect-list validation) -- read first after a reset (2026-10-06 night, owner asleep)"
metadata:
  node_type: memory
  type: project
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-07T03:54:00.014Z
---

Owner instruction (2026-10-06 night): continue agentically; no staging needed until the owner provides NEW Voyage keys (all staging provider keys were revoked after the re-embed incident [[project-reembed-cost-incident-2026-10-06]]). Never push to master. Agents: Opus for engineering; Codex available for reviews/pairing (relays must use unique scratch filenames, verdict from own session log [[feedback-concurrent-codex-relays-clobber-scratch]]). Release inclusion bar: [[feedback-release-inclusion-bar]].

Committed on development (not pushed after the 12.83.0 tag): 8b0431f2f installer /healthz, fda620da4 CLAUDE.md /healthz rule, 1da289710 a security-hardening commit (see its message), e5a4116f4 CLAUDE.md note + memory notes. Security item status lives ONLY in the private security tracker, never here.

In progress (uncommitted, separate agents): S12 stall watchdog (Codex re-review fixes: separate faulthandler dump file, failed-unlink recovery, caps); S21 #2047 file_extensions filter (Codex P2 fixes); S0 harness extensions (`scripts/analysis/reembed_repro/`, version-agnostic checks must FAIL on 12.83.0); S15 per-repo indexing lease (paired Claude+Codex engineer).

Design: `plans/designs/crash_safe_reconcile_2087_20261006.md` v3.5 (final; owner decisions in section 14). Story order: S0, S0b census, S12 independent; then deployment gate G1 (shipped together, dark until complete): S15 lease, S2 store/registry/writer, S16 HNSW gen, S14 FTS gen (#2056), S3, S4 reuse, S5 ownership, S17 multimodal, S6 resume retired, S7, S19, S13, S8, S9, S10, S11 alarm, S20 drain (#2045), S18 hard mounts; S21 (#2047) and S22 (#2049) separate query releases.

Pending merges: branch `feature/docs-overhaul` on origin (22 docs commits, owner-approved incl. removal of the disclosure checker) merges into development once S21 is committed (blocking dirty files: cli.py, search_code.md). Conflicts: docs/cluster-architecture.md and docs/cluster-setup.md (deleted by branch, modified by the /healthz fix) -> take branch layout and port the /healthz text.

Pending validation: Codex and Opus independently validating an owner-provided list (two private security-tracker items plus public P2 issues #2038-#2099; the full list is in the session scratchpad, not in git). Include per the bar; anything security-related is tracked and described only in the private security tracker.

Staging bring-up checklist (gitignored): `.analysis/staging_bringup_2087_20261006.md`; Langfuse lookback 3 days first [[project-staging-langfuse-lookback-3-days]].
