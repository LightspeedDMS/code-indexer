---
name: feedback-issue-routing-public-vs-security
description: "ALL regular issues now live in the PRIVATE dev repo (issue_manager's default origin); security issues stay in the private security-tracking repo; never file on the public repo"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-04T23:46:09.329Z
---

Owner rule, updated 2026-10-04: **all regular issues** (functional bugs, ops problems, test
hygiene, epics, stories) live in the **private** dev repository, i.e. the repo's `origin`
remote. The owner moved every public issue there on 2026-10-04. **Security issues** stay
in the private security-tracking repository (the one the project CLAUDE.md names). Nothing
is filed on the public repository any more.

Superseded rule (2026-09-30): regular bugs on the public repo. Do not use the old
`~/.tmp/public-issues-ctx` scratch dir for filing; it targets the public repo.

**Why:** keep operator and production detail out of any public tracker.

**How to apply:**
- Run `issue_manager.py` from the repo itself: it picks the first GitHub remote, `origin`
  = the private dev repo, which is now correct.
- Decide first whether a write-up could help someone exploit the product. If so, it goes to
  the security tracker only, with neutral wording.
- Commit messages, code and docs are still PUBLIC (the public repo mirrors `master` and
  `development`): keep "Fixes #N" references and wording neutral, and remember issue
  numbers in old commits now redirect to the private repo.

Related: [[feedback_no_secrets_in_memory]], [[feedback_bug_report_means_report_not_fix]].
