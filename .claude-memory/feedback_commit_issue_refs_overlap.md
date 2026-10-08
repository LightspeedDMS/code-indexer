---
name: feedback-commit-issue-refs-overlap
description: "Never write a bare #N (or Fixes #N) in a commit message: private and public issue numbers overlap and both repos' default branch is development, so it links or auto-closes the WRONG issue"
metadata:
  type: feedback
---
The private dev tracker (origin) and the public repo have overlapping issue numbers (e.g. private #2087 is the re-embed bug, public #2087 is a git-tools bug), and both repos use `development` as the default branch. A commit landing on either repo's development resolves `#N` against THAT repo: `Fixes #N` would auto-close an unrelated issue there, and a plain `#N` mislinks.

**Why:** on 2026-10-07 two unpushed commits carried "Fixes #2038" (meant public #2038; would have closed private #2038) and "(issue #2087)" (meant private #2087; public #2087 is unrelated). Rewritten before push.

**How to apply:**
- Public issue: write `Public issue LightspeedDMS/code-indexer#N.` (no closing keyword; close public issues explicitly at release).
- Private work: reference the design doc path or describe the change; never `#N`.
- Before every push, `git log origin/development..HEAD --format=%B | grep -nE '(^|[^/A-Za-z])#[0-9]+'` must return nothing.
- Related: [[feedback-issue-routing-public-vs-security]].
