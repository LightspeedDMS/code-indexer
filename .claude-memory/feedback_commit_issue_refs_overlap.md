---
name: feedback-commit-issue-refs-overlap
description: "Never write a bare #N (or Fixes #N) in a commit message: private and public issue numbers overlap and both repos' default branch is development, so it links or auto-closes the WRONG issue"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-21T18:49:21.510Z
---

The private dev tracker (origin) and the public repo have overlapping issue numbers (e.g. private #2087 is the re-embed bug, public #2087 is a git-tools bug), and both repos use `development` as the default branch. A commit landing on either repo's development resolves `#N` against THAT repo: `Fixes #N` would auto-close an unrelated issue there, and a plain `#N` mislinks.

**Why:** on 2026-10-07 two unpushed commits carried "Fixes #2038" (meant public #2038; would have closed private #2038) and "(issue #2087)" (meant private #2087; public #2087 is unrelated). Rewritten before push.

**How to apply:**
- Public issue: write `Public issue LightspeedDMS/code-indexer#N.` (no closing keyword; close public issues explicitly at release).
- Private work: reference the design doc path or describe the change; never `#N`, and never the private tracker's `owner/repo#N` form either -- naming the private tracker in a commit headed for the public repo is a disclosure (a reviewer rejected one on 2026-10-21).
- Before every push, `git log origin/development..HEAD --format=%B | grep -nE '(^|[^/A-Za-z])#[0-9]+'` must return nothing, and neither may a grep for the private tracker's repository name.
- Related: [[feedback-issue-routing-public-vs-security]].
