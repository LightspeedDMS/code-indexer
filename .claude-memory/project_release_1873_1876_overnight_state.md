---
name: project_release_1873_1876_overnight_state
description: Resume state for the 2026-09-16/17 overnight release (X-Ray binder #1873/#1875, glob scoping #1876, browse #1886, config race #1889, path-confinement security #1891) shipped to staging as 12.63.0 - read first after a reset
metadata:
  type: project
---

Overnight goal: fix all open P1/P2 (+ any new P1/P2 found), validate on staging, leave ready for
production; user decides the master push in the morning. NEVER push master without the literal
per-version two-confirmation authorization.

## SHIPPED to staging as 12.63.0 (2026-09-17 ~08:20 CDT), tag v12.63.0 on development commit 84bb0d18
CI green (lint incl. mypy, rust, smoke); auto-updater deployed 12.63.0 to ALL 4 staging servers
(solo + cluster nodes 20/22/23), all running. master/production UNTOUCHED.

Commits on development (origin/development == staging content):
- e587ca51 #1873/#1875 X-Ray Java binder (constructors/method-refs/super/visibility)
- 529e0b75 #1876 regex/xray include-exclude glob selector (dual-approved, F1-F12)
- b1ae7b9b #1889 ConfigService.get_config lazy-init thread race
- e9a43960 #1886 browse_directory/list_files recursive:false direct-children + REST path subtree
- aeedbe41 #1891 SECURITY path confinement (REST content, MCP get_file_content/directory_tree, CRUD, .git symlink)
- 7f08510c #1876 CI-mypy fix: pathspec _DIR_MARK via getattr (12.62.0 lint failed on this; runtime-inert)
- 84bb0d18 bump 12.63.0  (12.62.0 was pushed but its lint failed -> no tag; re-cut as 12.63.0)

## Staging verification (front door, solo/SQLite): ALL PASS, no P1/P2
- #1873/#1875: analyze_graph jsoup dead 72->42; exactly the 30 baseline false-dead constructors now
  alive; 0 new dead verdicts; S3-S7 exact & set-consistent (S3|S7==S2). Re-confirmed on 12.63.0.
- #1876: include *.md -> only .md; file-path & **/ includes now honoured; exclude/context/xray/
  bare-string-reject all correct. Re-confirmed on 12.63.0.
- #1886: recursive:false -> direct children only; leading-slash normalised; recursive unchanged.
- #1891: traversal/sibling/encoded/.git-symlink all "Access denied", no content, no 500; legit reads work.
- Log audit: 0 post-deploy ERROR; all WARNINGs are probes or pre-existing.
- Cluster (postgres) front door VERIFIED via the admin MFA handshake (self-service; procedure in
  project_staging_cluster_mfa_is_self_serviceable). CLUSTER 12.63.0; #1876 include *.md -> only .md;
  #1891 traversal -> Access denied no leak; #1886 recursive:false -> 8 entries no nesting. ALL PASS.
  (MFA is ALWAYS self-serviceable - never call it a blocker or a gap.)

## Gates run (local, on this code): fast-automation PASS (16333), server-fast PASS, rust PASS,
## e2e phases 1-6 PASS. (server-fast chunk4 flaked ONCE on a slow fixture -> #1892, passed on rerun.)

## Follow-ups filed (all P3/P4, none block): #1881 (1876 P3s), #1887 (self-loop), #1888 (REST non-recursive
## dirs), #1890 (cluster config lost-update), #1892 (P2 slow-fixture gate flake, test-only), #1893 (Type::method
## ref binds to enclosing class - new in 1873/1875, safe direction). #1877/#1879 have P3 notes.

## LEFT FOR THE USER (morning): decide whether to promote staging->master (production). If yes, it needs the
## literal authorization phrase + the two-confirmation protocol, per version. #1891 is a production security
## fix (path traversal readable by any authed user) - weigh first.

## Housekeeping: ~95GB stale Rust build dirs + reviewer scratch under ~/.tmp await manual rm (Senior Coding
## Nanny blocks rm -rf from the agent). Disk 85% / ~31GB free.

Related: [[feedback_review_findings_fix_p1_p2_tolerate_p3_p4]], [[feedback_version_bump_must_be_push_tip]],
[[project_staging_cluster_mfa_is_self_serviceable]], [[project_verify_both_staging_environments]],
[[feedback_never_claim_ready_without_staging_e2e]].
